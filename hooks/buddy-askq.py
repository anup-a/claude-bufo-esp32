#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

Shows the question + options on the buddy as tappable buttons, waits for the
user's tap, then BLOCKS the AskUserQuestion tool and returns the chosen answer
as the block reason. Claude reads the reason and continues with that answer
instead of prompting in the terminal — so the question is answered hands-free
from the device.

If the bridge is down or the user doesn't tap in time, emits nothing and exits
0 -> Claude falls through to the normal terminal question picker.
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8787/question"

try:
    ev = json.load(sys.stdin)
except Exception:
    sys.exit(0)

if ev.get("tool_name") != "AskUserQuestion":
    sys.exit(0)

ev["_timeout"] = 580
try:
    req = urllib.request.Request(
        URL, data=json.dumps(ev).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=595))
    label = resp.get("label")
except Exception:
    sys.exit(0)   # bridge unreachable -> normal flow

if not label:
    sys.exit(0)   # no tap -> normal flow

reason = (
    f"The user answered this question on their hardware buddy device. "
    f"Their selection was: \"{label}\". Use this as the answer to your "
    f"AskUserQuestion and continue — do not call AskUserQuestion again for this."
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
sys.exit(0)
