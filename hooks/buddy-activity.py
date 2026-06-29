#!/usr/bin/env python3
"""Claude Code activity hook -> desk buddy (fire-and-forget).

Registered on PostToolUse / Notification / Stop / UserPromptSubmit / SessionStart
to keep the buddy's live activity (running/waiting/msg + the bufo mood) fresh.
Always fast and non-blocking; never affects Claude's flow.
"""
import json
import sys
import urllib.request

URL = "http://127.0.0.1:8787/activity"

try:
    ev = json.load(sys.stdin)
except Exception:
    sys.exit(0)

en = ev.get("hook_event_name", "")
tool = ev.get("tool_name", "")
msg, completed = "", False
if en == "UserPromptSubmit":
    msg = "thinking..."
elif en in ("PreToolUse", "PostToolUse"):
    msg = tool or "working"
elif en == "Notification":
    msg = str(ev.get("message", "needs you"))[:38]
elif en == "Stop":
    msg, completed = "done", True
elif en == "SessionStart":
    msg = "session start"

payload = {
    "session_id": ev.get("session_id", ""),
    "msg": msg,
    "completed": completed,
    "transcript_path": ev.get("transcript_path", ""),
    "event": en,
    "tool": tool,
}
try:
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()
except Exception:
    pass
sys.exit(0)
