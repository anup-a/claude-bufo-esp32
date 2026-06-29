#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

OBSERVATION-ONLY (the model Flux Island uses): AskUserQuestion needs the
terminal's native input (typing, multi-select, "Other"), so this hook must NOT
block or decide. It just surfaces the question to the buddy for at-a-glance
awareness, then exits 0 with no decision — so Claude Code shows its normal
in-terminal picker and you answer there with full control.

(Permissions are different: they're binary, so buddy-permission.py *does* block
the PermissionRequest event and lets you approve/deny by touch.)
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

sys.exit(0)   # no decision -> the terminal handles the answer
