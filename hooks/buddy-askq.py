#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

Never blocks/denies: exits 0 so Claude Code shows its normal in-terminal picker
(full input: typing, multi-select, "Other"). It also mirrors the question to the
buddy with tappable options, and — if this session is running under bufo-claude —
passes the pty socket so a buddy tap can be typed into the live picker (native
answer, no "Error:"). If not under bufo-claude, buddy taps just dismiss the card
and you answer in the terminal. The card clears when answered either way.
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

# How the bridge can type into THIS session's pty (set by bufo-claude).
ev["_target"] = {"pty_sock": os.environ.get("BUFO_PTY_SOCK", "")}

try:
    req = urllib.request.Request(
        URL, data=json.dumps(ev).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()
except Exception:
    pass

sys.exit(0)   # defer -> the terminal picker handles the answer natively
