#!/usr/bin/env python3
"""
Claude Code / Codex usage collector for the ESP32 desk buddy.

Primary data source: Flux Island's local stats ledger
(~/Library/Application Support/flux-desktop-app/stats/token_records.json) — a
unified, per-call token ledger across claude / codex / opencode with
millisecond timestamps and a full token breakdown. This is read-only; it is
your own local data on disk (no network interception).

Fallback (if Flux Island isn't installed): scrape ~/.claude transcripts and
~/.codex session logs directly.

Serves a compact JSON document at http://<mac-ip>:8787/usage that the buddy
polls. Cost is estimated from the pricing table below.
"""

from __future__ import annotations

import json
import os
import datetime as dt
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Configuration -----------------------------------------------------------

HOME = Path.home()
FLUX_STATS = HOME / "Library" / "Application Support" / "flux-desktop-app" / "stats"
FLUX_TOKENS = FLUX_STATS / "token_records.json"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"

PORT = int(os.environ.get("BUDDY_PORT", "8787"))
WINDOW_HOURS = 5  # the rolling usage window Flux Island displays

# 5h-window token budgets used to compute "% used" (to match Flux's display).
# Flux doesn't persist the limit, so these are calibrated to a live reading
# (claude 283.76M -> Flux 12%; codex 221.7K -> Flux 1%). Override via env if
# they drift from what Flux shows.
CLAUDE_5H_LIMIT = int(os.environ.get("BUDDY_CLAUDE_5H_LIMIT", str(2_200_000_000)))
CODEX_5H_LIMIT = int(os.environ.get("BUDDY_CODEX_5H_LIMIT", str(22_000_000)))

# Per-million-token USD pricing, keyed by tool (Flux records carry no model,
# so we apply one representative rate per tool — Claude Opus 4.8 for claude).
RATES = {
    "claude":   {"in": 5.0,  "out": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "opencode": {"in": 5.0,  "out": 25.0, "cache_read": 0.50,  "cache_write": 6.25},
    "codex":    {"in": 1.25, "out": 10.0, "cache_read": 0.125, "cache_write": 1.25},
}
DEFAULT_RATE = RATES["claude"]


def _today() -> dt.date:
    return dt.date.today()


def _rate(tool: str) -> dict:
    return RATES.get(tool, DEFAULT_RATE)


def _cost(tool: str, ti: int, to: int, cr: int, cw: int) -> float:
    r = _rate(tool)
    return (ti * r["in"] + to * r["out"] + cr * r["cache_read"] + cw * r["cache_write"]) / 1_000_000


# --- Flux Island source (preferred) -----------------------------------------

RL_CACHE = os.environ.get("BUDDY_RL_CACHE", "/tmp/flux-rl.json")


def _load_rl() -> dict | None:
    """Claude's real rate-limit usage, read from the same cache Flux uses
    (/tmp/flux-rl.json): {five_hour:{used_percentage,resets_at}, seven_day:{...}}.
    This is the exact source of Flux's % — no token estimation needed."""
    try:
        raw = json.loads(Path(RL_CACHE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    def pct(w):
        v = (w or {}).get("used_percentage", (w or {}).get("utilization", 0)) or 0
        return max(0, min(100, int(round(v))))

    def reset_min(w):
        ra = (w or {}).get("resets_at")
        if not ra:
            return -1
        return max(0, int((ra - dt.datetime.now().timestamp()) / 60))

    return {
        "five_hour": {"used_pct": pct(raw.get("five_hour")), "reset_min": reset_min(raw.get("five_hour"))},
        "seven_day": {"used_pct": pct(raw.get("seven_day")), "reset_min": reset_min(raw.get("seven_day"))},
    }


CODEX_SESSIONS = HOME / ".codex" / "sessions"


def _find_key(o, key):
    if isinstance(o, dict):
        if key in o:
            return o[key]
        for v in o.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def _load_codex_rl() -> dict | None:
    """Codex's real rate limits, parsed like Flux does: the latest `rate_limits`
    entry (primary=5h, secondary=weekly) in the most recent ~/.codex/sessions
    rollout file (mtime within 24h)."""
    try:
        now = dt.datetime.now().timestamp()
        files = [p for p in CODEX_SESSIONS.rglob("*.jsonl")
                 if now - p.stat().st_mtime <= 86400]
        if not files:
            return None
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for fp in files[:5]:
            last = None
            try:
                for line in fp.read_text(encoding="utf-8", errors="replace").splitlines():
                    if '"rate_limits"' in line:
                        last = line
            except OSError:
                continue
            if not last:
                continue
            rl = _find_key(json.loads(last), "rate_limits")
            if not rl:
                continue

            def w(win):
                if not isinstance(win, dict):
                    return {"used_pct": 0, "reset_min": -1}
                p = max(0, min(100, int(round(win.get("used_percent", 0) or 0))))
                ra = win.get("resets_at")
                return {"used_pct": p, "reset_min": max(0, int((ra - now) / 60)) if ra else -1}

            return {"five_hour": w(rl.get("primary")), "seven_day": w(rl.get("secondary"))}
        return None
    except Exception:
        return None


def _load_flux() -> list | None:
    try:
        data = json.loads(FLUX_TOKENS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, list) else None


def _agg_from_flux(records: list) -> dict:
    """Aggregate today's totals + the rolling window from Flux records."""
    now_ms = int(dt.datetime.now().timestamp() * 1000)
    window_start_ms = now_ms - WINDOW_HOURS * 3600 * 1000
    today = _today()

    # Per-tool accumulators
    tools = {}
    def slot(t):
        return tools.setdefault(t, {
            "cost": 0.0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0,
            "calls": 0, "win_tokens": 0, "win_first_ms": None,
        })

    all_time_tokens = 0
    for rec in records:
        tool = rec.get("tool", "claude")
        ti = int(rec.get("inputTokens", 0) or 0)
        to = int(rec.get("outputTokens", 0) or 0)
        cr = int(rec.get("cacheReadTokens", 0) or 0)
        cw = int(rec.get("cacheCreationTokens", 0) or 0)
        ts = int(rec.get("timestamp", 0) or 0)
        tok = ti + to + cr + cw
        all_time_tokens += tok

        # Rolling window (any tool, within last WINDOW_HOURS)
        if ts >= window_start_ms:
            s = slot(tool)
            s["win_tokens"] += tok
            if s["win_first_ms"] is None or ts < s["win_first_ms"]:
                s["win_first_ms"] = ts

        # Today's totals
        try:
            rec_date = dt.date.fromtimestamp(ts / 1000)
        except (OverflowError, OSError, ValueError):
            continue
        if rec_date == today:
            s = slot(tool)
            s["cost"] = round(s["cost"] + _cost(tool, ti, to, cr, cw), 4)
            s["in"] += ti; s["out"] += to
            s["cache_read"] += cr; s["cache_write"] += cw
            s["calls"] += 1

    def window(tool: str, limit: int) -> dict:
        s = tools.get(tool, {})
        toks = int(s.get("win_tokens", 0))
        if tool == "claude":  # opencode is Claude-backed, count it in the same window
            toks += int(tools.get("opencode", {}).get("win_tokens", 0))
        first = s.get("win_first_ms")
        reset_min = 0
        if first is not None:
            reset_ms = first + WINDOW_HOURS * 3600 * 1000 - now_ms
            reset_min = max(0, int(reset_ms / 60000))
        pct = round(toks * 100.0 / limit, 1) if limit > 0 else 0.0
        return {"tokens": toks, "reset_min": reset_min, "pct_used": pct}

    cl = tools.get("claude", {})
    # opencode usage rolls into the claude card (both are Claude-backed).
    oc = tools.get("opencode", {})
    cx = tools.get("codex", {})

    claude_cost = round(cl.get("cost", 0.0) + oc.get("cost", 0.0), 4)
    return {
        "source": "flux",
        "all_time_tokens": all_time_tokens,
        "claude": {
            "cost_usd": claude_cost,
            "tokens_in": cl.get("in", 0) + oc.get("in", 0),
            "tokens_out": cl.get("out", 0) + oc.get("out", 0),
            "cache_read": cl.get("cache_read", 0) + oc.get("cache_read", 0),
            "cache_write": cl.get("cache_write", 0) + oc.get("cache_write", 0),
            "messages": cl.get("calls", 0) + oc.get("calls", 0),
            "sessions": 0,
            "model": "claude-opus-4-8",
        },
        "codex": {
            "cost_usd": round(cx.get("cost", 0.0), 4),
            "tokens_in": cx.get("in", 0),
            "tokens_out": cx.get("out", 0),
            "cache_read": cx.get("cache_read", 0),
            "sessions": cx.get("calls", 0),
        },
        "window5h": {"claude": window("claude", CLAUDE_5H_LIMIT),
                     "codex": window("codex", CODEX_5H_LIMIT)},
    }


# --- Fallback source: scrape ~/.claude + ~/.codex ----------------------------

def _agg_from_logs() -> dict:
    """Minimal fallback when Flux Island data isn't present."""
    today = today_str = _today().isoformat()
    cl = {"cost": 0.0, "in": 0, "out": 0, "cr": 0, "cw": 0, "msgs": 0}
    if CLAUDE_PROJECTS.is_dir():
        for jf in CLAUDE_PROJECTS.rglob("*.jsonl"):
            try:
                if dt.date.fromtimestamp(jf.stat().st_mtime) != _today():
                    continue
            except (OSError, ValueError):
                continue
            try:
                for line in jf.open("r", encoding="utf-8", errors="replace"):
                    if '"usage"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = rec.get("timestamp", "")
                    if isinstance(ts, str) and not ts.startswith(today_str):
                        continue
                    msg = rec.get("message") or {}
                    u = msg.get("usage") or rec.get("usage")
                    if not isinstance(u, dict):
                        continue
                    ti = int(u.get("input_tokens", 0) or 0)
                    to = int(u.get("output_tokens", 0) or 0)
                    cr = int(u.get("cache_read_input_tokens", 0) or 0)
                    cw = int(u.get("cache_creation_input_tokens", 0) or 0)
                    cl["cost"] = round(cl["cost"] + _cost("claude", ti, to, cr, cw), 4)
                    cl["in"] += ti; cl["out"] += to; cl["cr"] += cr; cl["cw"] += cw
                    cl["msgs"] += 1
            except OSError:
                continue
    return {
        "source": "logs",
        "all_time_tokens": 0,
        "claude": {
            "cost_usd": cl["cost"], "tokens_in": cl["in"], "tokens_out": cl["out"],
            "cache_read": cl["cr"], "cache_write": cl["cw"], "messages": cl["msgs"],
            "sessions": 0, "model": "claude-opus-4-8",
        },
        "codex": {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "cache_read": 0, "sessions": 0},
        "window5h": {"claude": {"tokens": 0, "reset_min": 0}, "codex": {"tokens": 0, "reset_min": 0}},
    }


# --- Build the ESP32-facing payload -----------------------------------------

def build_payload() -> dict:
    records = _load_flux()
    agg = _agg_from_flux(records) if records else _agg_from_logs()

    total_cost = round(agg["claude"]["cost_usd"] + agg["codex"]["cost_usd"], 2)
    total_tokens = (agg["claude"]["tokens_in"] + agg["claude"]["tokens_out"]
                    + agg["claude"]["cache_read"] + agg["claude"]["cache_write"]
                    + agg["codex"]["tokens_in"] + agg["codex"]["tokens_out"]
                    + agg["codex"]["cache_read"])

    if total_cost >= 50:
        mood = "wired"
    elif total_cost >= 10:
        mood = "busy"
    elif total_cost > 0:
        mood = "happy"
    else:
        mood = "idle"

    now = dt.datetime.now()
    return {
        "ok": True,
        "ts": int(now.timestamp()),
        "time": now.strftime("%H:%M"),
        "date": _today().isoformat(),
        "source": agg["source"],
        "mood": mood,
        "claude": agg["claude"],
        "codex": agg["codex"],
        "window5h": agg["window5h"],
        "quota": _load_rl(),              # real Claude 5h + 7d % (same source as Flux)
        "quota_codex": _load_codex_rl(),  # real Codex 5h + weekly % (~/.codex/sessions)
        "total": {"cost_usd": total_cost, "tokens": total_tokens,
                  "all_time_tokens": agg["all_time_tokens"]},
    }


# --- HTTP server -------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") not in ("", "/usage", "/api/usage"):
            self.send_error(404, "not found")
            return
        try:
            body = json.dumps(build_payload()).encode("utf-8")
        except Exception as exc:
            body = json.dumps({"ok": False, "error": str(exc)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _lan_ip() -> str:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    src = "Flux Island ledger" if FLUX_TOKENS.exists() else "~/.claude + ~/.codex logs"
    print(f"Claude buddy collector — source: {src}")
    print(f"Listening on http://{_lan_ip()}:{PORT}/usage   (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
        server.shutdown()


if __name__ == "__main__":
    if os.environ.get("BUDDY_ONESHOT"):
        print(json.dumps(build_payload(), indent=2))
    else:
        main()
