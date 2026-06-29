#!/usr/bin/env python3
"""USB-serial bridge between Claude Code CLI hooks and the desk buddy.

Owns the buddy's serial port and serves a localhost HTTP endpoint that the
Claude Code hooks POST to:

  POST /permission  (blocking)  shows an approve/deny card on the buddy and
                                waits for the user's touch, returns the decision
  POST /activity    (fast)      live session/tool activity for the display
  GET  /usage                   the existing Flux cost/token payload

A background thread pushes activity + cost/token state to the buddy every 2s.

Transport: newline-delimited JSON over USB serial. Buddy -> collector decision
lines are prefixed with \\x02BUDDY so they can be picked out of boot/log noise.
"""
import json
import os
import subprocess
import sys
import time
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from collector import build_payload
except Exception:                       # pragma: no cover
    def build_payload():
        return {}

import serial  # pyserial

def _ascii(s):
    """Fold common unicode to ASCII and drop the rest — the buddy font is
    ASCII-only, so anything else renders as a box."""
    s = str(s)
    for a, b in (("—", "-"), ("–", "-"), ("‘", "'"), ("’", "'"),
                 ("“", '"'), ("”", '"'), ("…", "..."), ("•", "-"),
                 ("·", "-"), ("→", "->"), (" ", " ")):
        s = s.replace(a, b)
    return s.encode("ascii", "ignore").decode("ascii")


PORT = int(os.environ.get("BUDDY_PORT", "8787"))
SERIAL_PORT = os.environ.get("BUDDY_SERIAL", "/dev/cu.usbserial-2110")
SERIAL_BAUD = 115200
PROMPT_PREFIX = b"\x02BUDDY "

# --- serial link ------------------------------------------------------------
_ser = None
_wlock = threading.Lock()
_decisions = {}
_dcv = threading.Condition()


def _open_serial():
    global _ser
    try:
        s = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=0.2)
        try:
            s.dtr = False
            s.rts = False
        except Exception:
            pass
        _ser = s
        print(f"[buddy] serial open {SERIAL_PORT}")
        return True
    except Exception as exc:
        print(f"[buddy] serial open failed: {exc}")
        return False


def send(obj):
    line = (json.dumps(obj) + "\n").encode("utf-8")
    with _wlock:
        if _ser:
            try:
                _ser.write(line)
            except Exception as exc:
                print(f"[buddy] write err: {exc}")


def reader():
    global _ser
    buf = b""
    while True:
        if not _ser:
            if not _open_serial():
                time.sleep(2)
                continue
        try:
            data = _ser.read(256)
        except Exception:
            _ser = None
            time.sleep(1)
            continue
        if not data:
            continue
        buf += data
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            if os.environ.get("BUDDY_DEBUG") and ln.strip():
                print(f"[rx-raw] {ln[:120]!r}")
            i = ln.find(PROMPT_PREFIX)
            if i < 0:
                continue
            print(f"[buddy] decision line: {ln[i:][:80]!r}")
            try:
                d = json.loads(ln[i + len(PROMPT_PREFIX):].decode("utf-8", "replace"))
            except Exception:
                continue
            if "id" in d and "qchoice" in d:
                # Buddy answered a (non-blocking) question -> type it into the
                # live terminal picker. Does not unblock any waiter.
                handle_qchoice(d["id"], d["qchoice"])
            elif "id" in d and "decision" in d:
                with _dcv:
                    _decisions[d["id"]] = d["decision"]
                    _dcv.notify_all()


def wait_decision(pid, timeout):
    end = time.time() + timeout
    with _dcv:
        while pid not in _decisions:
            rem = end - time.time()
            if rem <= 0:
                return None
            _dcv.wait(rem)
        return _decisions.pop(pid)


# --- buddy answers the live terminal picker by injecting keystrokes ----------
_active_q = [None]   # {"id", "labels", "target"} while an AskUserQuestion is open


def _inject_keys(target, idx, n):
    """Drive Claude Code's AskUserQuestion picker to option `idx` (0-based): home
    the cursor to the top, step down `idx`, confirm. Routed to THIS terminal via
    tmux or kitty remote control (whichever the hook captured)."""
    target = target or {}
    pane = target.get("tmux_pane")
    listen = target.get("kitty_listen")
    win = target.get("kitty_window")
    up = max(n, idx) + 3
    try:
        if pane:
            seq = ["Up"] * up + ["Down"] * idx + ["Enter"]
            subprocess.run(["tmux", "send-keys", "-t", pane, *seq],
                           timeout=4, check=False)
            print(f"[buddy] injected via tmux pane={pane} idx={idx}")
        elif listen:
            keys = ["up"] * up + ["down"] * idx + ["enter"]
            cmd = ["kitty", "@", "--to", listen, "send-key"]
            if win:
                cmd += ["--match", f"id:{win}"]
            subprocess.run(cmd + keys, timeout=4, check=False)
            print(f"[buddy] injected via kitty win={win} idx={idx}")
        else:
            print("[buddy] buddy tap ignored: no tmux/kitty target "
                  "(enable kitty remote control or run claude in tmux)")
    except Exception as exc:
        print(f"[buddy] inject err: {exc}")


def handle_qchoice(qid, idx):
    with _slock:
        aq = _active_q[0]
        if not aq or aq.get("id") != qid:
            return
        _active_q[0] = None
    labels = aq.get("labels") or []
    if isinstance(idx, int) and 0 <= idx < len(labels):
        _inject_keys(aq.get("target"), idx, len(labels))
    send({"qclear": True})


# --- activity tracking ------------------------------------------------------
_sessions = {}          # session_id -> last_seen epoch
_pending = {}           # prompt id -> True
_last_msg = ["idle"]
_completed_until = [0.0]
_transcript = [None]              # latest transcript_path seen from a hook
_slock = threading.Lock()
_prompt_gate = threading.Lock()   # one approve/deny card on screen at a time


def _read_entries(path, n=6):
    """Last n message snippets from a Claude Code JSONL transcript, newest first."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for ln in reversed(lines):
        if len(out) >= n:
            break
        try:
            o = json.loads(ln)
        except Exception:
            continue
        role = o.get("type") or (o.get("message") or {}).get("role")
        msg = o.get("message") if isinstance(o.get("message"), dict) else o
        content = msg.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text = b.get("text", "")
                    break
                if b.get("type") == "tool_use":
                    text = f"[{b.get('name', 'tool')}]"
                    break
        text = " ".join(str(text).split())[:42]
        if not text:
            continue
        tag = "you" if role == "user" else "claude" if role == "assistant" else str(role)
        out.append(_ascii(f"{tag}: {text}"))
    return out


def note_activity(sid, msg, completed=False):
    with _slock:
        if sid:
            _sessions[sid] = time.time()
        if msg:
            _last_msg[0] = _ascii(msg)
        if completed:
            _completed_until[0] = time.time() + 6


_test_hold = [0.0]


def pusher():
    while True:
        try:
            now = time.time()
            if now < _test_hold[0]:      # a /test state is being demoed; don't clobber it
                time.sleep(0.4)
                continue
            with _slock:
                total = sum(1 for t in _sessions.values() if now - t < 300)
                running = sum(1 for t in _sessions.values() if now - t < 25)
                waiting = len(_pending)
                msg = _last_msg[0]
                completed = now < _completed_until[0]
            st = {
                "total": total,
                "running": max(running - waiting, 0),
                "waiting": waiting,
                "msg": msg[:38],
                "completed": completed,
            }
            ents = _read_entries(_transcript[0])
            if ents:
                st["entries"] = ents
            try:
                p = build_payload()
                st["tokens_today"] = int(p["total"]["tokens"])
                st["cost_usd"] = float(p["total"]["cost_usd"])
                w = p.get("window5h", {}) or {}
                c = w.get("claude", {}) or {}
                x = w.get("codex", {}) or {}
                st["win_c_tok"] = int(c.get("tokens", 0))
                st["win_c_min"] = int(c.get("reset_min", -1))
                st["win_c_pct"] = float(c.get("pct_used", 0))
                st["win_x_tok"] = int(x.get("tokens", 0))
                st["win_x_min"] = int(x.get("reset_min", -1))
                st["win_x_pct"] = float(x.get("pct_used", 0))
                q = p.get("quota") or {}
                if q:   # real Claude rate-limit %, same source as Flux
                    st["q5h"] = int(q["five_hour"]["used_pct"])
                    st["q7d"] = int(q["seven_day"]["used_pct"])
                    st["q5h_min"] = int(q["five_hour"]["reset_min"])
                    st["q7d_min"] = int(q["seven_day"]["reset_min"])
                qx = p.get("quota_codex") or {}
                if qx:  # real Codex rate-limit % (~/.codex/sessions)
                    st["qx5h"] = int(qx["five_hour"]["used_pct"])
                    st["qx7d"] = int(qx["seven_day"]["used_pct"])
            except Exception:
                pass
            send(st)
        except Exception as exc:
            print(f"[buddy] push err: {exc}")
        time.sleep(2)


def _opt_labels(opts):
    out = []
    for o in opts or []:
        if isinstance(o, dict):
            out.append(str(o.get("label", o.get("name", o))))
        else:
            out.append(str(o))
    return out[:4]


def _question_of(ti):
    """Return (question_text, [labels]) from an AskUserQuestion tool_input,
    handling both the `questions:[{question,options:[{label}]}]` shape and a
    flat `{question, options}` shape. Sanitized to ASCII (the buddy font)."""
    ti = ti or {}
    qs = ti.get("questions")
    if isinstance(qs, list) and qs:
        q0 = qs[0] or {}
        q, opts = str(q0.get("question", "?")), _opt_labels(q0.get("options", []))
    else:
        q, opts = str(ti.get("question", "?")), _opt_labels(ti.get("options", []))
    return _ascii(q), [_ascii(o) for o in opts]


def _hint_for(tool, ti):
    ti = ti or {}
    if tool == "Bash":
        h = str(ti.get("command", ""))
    elif tool in ("Edit", "Write", "Read", "NotebookEdit"):
        h = str(ti.get("file_path", ""))
    elif tool == "WebFetch":
        h = str(ti.get("url", ""))
    elif tool == "AskUserQuestion":
        opts = ti.get("options", []) or []
        labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in opts]
        h = str(ti.get("question", "")) + "  |  " + " / ".join(labels)
    else:
        h = next((v for v in ti.values() if isinstance(v, str) and v), tool)
    return _ascii(h)[:180]   # buddy font is ASCII-only


# --- HTTP endpoint for the hooks -------------------------------------------
class H(BaseHTTPRequestHandler):
    def _read(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):  # noqa: N802
        path = self.path.rstrip("/")
        try:
            ev = self._read()
        except Exception:
            ev = {}
        sid = ev.get("session_id", "")
        if path == "/permission":
            tool = ev.get("tool_name", "tool")
            hint = _hint_for(tool, ev.get("tool_input", {}))
            note_activity(sid, f"approve: {tool}")
            timeout = float(ev.get("_timeout", 580))
            with _prompt_gate:               # serialize cards onto the one screen
                pid = uuid.uuid4().hex[:8]
                with _slock:
                    _pending[pid] = True
                print(f"[buddy] -> prompt id={pid} tool={tool} hint={hint[:50]!r}")
                send({"prompt": {"id": pid, "tool": tool, "hint": hint}})
                dec = wait_decision(pid, timeout)
                print(f"[buddy] <- decision id={pid}: {dec}")
                with _slock:
                    _pending.pop(pid, None)
                send({"pclear": True})
            self._send({"decision": dec or "ask"})
        elif path == "/question":
            qtext, labels = _question_of(ev.get("tool_input", {}))
            note_activity(sid, "question")
            if not _ser:
                # Buddy not connected -> don't block; let the terminal handle it.
                self._send({"label": None, "defer": True})
            else:
                timeout = float(ev.get("_timeout", 85))
                with _prompt_gate:
                    qid = uuid.uuid4().hex[:8]
                    with _slock:
                        _pending[qid] = True
                    print(f"[buddy] -> question id={qid} {qtext[:40]!r} opts={labels}")
                    send({"question": {"id": qid, "q": qtext[:150], "opts": labels, "kbd": 1}})
                    choice = wait_decision(qid, timeout)
                    with _slock:
                        _pending.pop(qid, None)
                    send({"qclear": True})
                print(f"[buddy] <- choice id={qid}: {choice}")
                if isinstance(choice, int) and 0 <= choice < len(labels):
                    self._send({"label": labels[choice], "index": choice})
                else:
                    # -1 (tapped "keyboard") or None (timeout) -> defer to the terminal
                    self._send({"label": None, "defer": True})
        elif path == "/show_question":
            # Mirror the question to the buddy with a unique id. The terminal picker
            # stays live; a buddy tap gets injected into it (see handle_qchoice).
            qtext, labels = _question_of(ev.get("tool_input", {}))
            note_activity(sid, "asking...")
            qid = uuid.uuid4().hex[:8]
            with _slock:
                _active_q[0] = {"id": qid, "labels": labels, "target": ev.get("_target", {})}
            send({"question": {"id": qid, "q": qtext[:150], "opts": labels, "kbd": 1}})
            self._send({"ok": True})
        elif path == "/activity":
            if ev.get("transcript_path"):
                _transcript[0] = ev["transcript_path"]
            # AskUserQuestion finished (answered anywhere) -> clear the card + state
            if ev.get("event") == "PostToolUse" and ev.get("tool") == "AskUserQuestion":
                with _slock:
                    _active_q[0] = None
                send({"qclear": True})
            note_activity(sid, ev.get("msg", ""), completed=bool(ev.get("completed")))
            self._send({"ok": True})
        elif path == "/test":
            _test_hold[0] = time.time() + float(ev.get("hold", 4))
            send(ev.get("state", ev))   # forward a raw state dict to the buddy
            self._send({"ok": True})
        else:
            self._send({"ok": False}, 404)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("", "/usage", "/api/usage"):
            try:
                self._send(build_payload())
            except Exception as exc:
                self._send({"ok": False, "error": str(exc)}, 500)
        else:
            self._send({"ok": False}, 404)

    def log_message(self, *_a):
        pass


def main():
    threading.Thread(target=reader, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"[buddy] bridge on http://127.0.0.1:{PORT}  (serial {SERIAL_PORT})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
