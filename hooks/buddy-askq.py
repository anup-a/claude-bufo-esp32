#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

OBSERVATION-ONLY (Flux Island's model): this hook never blocks or decides. It
exits 0 so Claude Code shows its normal in-terminal picker (full input: typing,
multi-select, "Other"), and it mirrors the question to the buddy for at-a-glance
awareness. You answer in the terminal; the buddy card clears when you do
(buddy-activity.py reports PostToolUse(AskUserQuestion) -> the bridge clears it).
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8787/show_question"

try:
    ev = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if ev.get("tool_name") == "AskUserQuestion":
    try:
        req = urllib.request.Request(
            URL, data=json.dumps(ev).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass

sys.exit(0)   # no decision -> the terminal handles the answer natively
