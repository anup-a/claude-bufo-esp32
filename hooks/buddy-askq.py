#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

ANSWER ON BOTH SCREEN AND TERMINAL (no "Error:"):
  - This hook never blocks or denies. It exits 0 -> Claude Code shows its normal
    in-terminal picker, which stays fully usable (typing, multi-select, Other).
  - It also mirrors the question to the buddy with tappable options, and tells the
    bridge how to reach THIS terminal (tmux pane / kitty window) so that when you
    tap an option on the buddy, the bridge types that choice into the live picker.
  - So both surfaces are live at once; whichever you use first wins, and the answer
    is always native (no denial, no error). Multi-question prompts -> terminal only.
"""
import json
import os
import sys
import urllib.request

URL = "http://127.0.0.1:8787/show_question"

try:
    ev = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if ev.get("tool_name") != "AskUserQuestion":
    sys.exit(0)

# Only single-question prompts are buddy-tappable; multi-question -> terminal only.
qs = (ev.get("tool_input") or {}).get("questions")
if isinstance(qs, list) and len(qs) > 1:
    sys.exit(0)

# How the bridge can inject keystrokes back into THIS terminal session.
ev["_target"] = {
    "tmux_pane": os.environ.get("TMUX_PANE", ""),
    "kitty_listen": os.environ.get("KITTY_LISTEN_ON", ""),
    "kitty_window": os.environ.get("KITTY_WINDOW_ID", ""),
}

try:
    req = urllib.request.Request(
        URL, data=json.dumps(ev).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()
except Exception:
    pass

sys.exit(0)   # always defer -> the terminal picker handles the answer natively
