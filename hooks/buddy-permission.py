#!/usr/bin/env python3
"""Claude Code PermissionRequest hook -> desk buddy.

Forwards the permission request to the local buddy bridge, which shows an
approve/deny card on the device and waits for the user's touch. Returns the
decision to Claude Code. If the bridge is down or times out, emits nothing and
exits 0 -> Claude falls through to the normal terminal permission prompt.
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8787/permission"

try:
    ev = json.load(sys.stdin)
except Exception:
    sys.exit(0)

# AskUserQuestion isn't an approve/deny decision — it's handled observation-only
# by buddy-askq.py. Don't show a permission card for it.
if ev.get("tool_name") == "AskUserQuestion":
    sys.exit(0)

ev["_timeout"] = 580
try:
    req = urllib.request.Request(
        URL, data=json.dumps(ev).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=595))
    decision = resp.get("decision", "ask")
except Exception:
    sys.exit(0)   # bridge unreachable -> defer to normal flow

if decision in ("allow", "deny"):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": decision},
        }
    }))
# "ask"/anything else -> no output -> normal permission flow
sys.exit(0)
