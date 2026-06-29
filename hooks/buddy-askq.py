#!/usr/bin/env python3
"""Claude Code PreToolUse hook for AskUserQuestion -> desk buddy.

Tap-to-answer WITH a keyboard escape (best of both):
  - The buddy shows the question and its options. Tap one -> we answer it for you
    (deny the tool with the chosen label as the reason).
  - The buddy also shows an "Answer on keyboard instead" button. Tap that (or let
    it time out) -> we defer (exit 0, no decision) so Claude Code's terminal picker
    appears and you answer there with full input (typing, multi-select, "Other").
  - Multi-question prompts always defer to the terminal (the buddy shows one).

So you're never locked out of the terminal, but simple questions are one tap away.
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

# Only single-question prompts are buddy-answerable; defer the rest to the terminal.
qs = (ev.get("tool_input") or {}).get("questions")
if isinstance(qs, list) and len(qs) > 1:
    sys.exit(0)

ev["_timeout"] = 85   # tap within ~85s, else it defers to the terminal
try:
    req = urllib.request.Request(
        URL, data=json.dumps(ev).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=90))
except Exception:
    sys.exit(0)   # bridge down -> let the terminal handle it

label = resp.get("label")
if resp.get("defer") or not label:
    sys.exit(0)   # keyboard escape / timeout -> terminal handles the answer

# NOTE: PreToolUse can only inject an answer by denying the tool, and Claude Code
# always prefixes the reason with "Error:". There is no field to supply a clean
# tool result (confirmed against the hooks docs). So we make the text obviously an
# ANSWER rather than a failure, and add context so the model treats it as such.
reason = (f'Buddy answer: "{label}"  (this is NOT an error — the user answered on '
          f'their hardware buddy). Use "{label}" as the answer to the '
          f'AskUserQuestion and continue; do not call AskUserQuestion again.')
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": reason,
    "additionalContext": f'The user answered the AskUserQuestion on their hardware buddy: "{label}".',
    "systemMessage": f'Buddy answered: {label}',
}}))
sys.exit(0)
