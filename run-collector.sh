#!/usr/bin/env bash
# Start the Claude Code / Codex usage collector.
#
# Runs under Apple-signed /usr/bin/python3 on macOS so the Application Firewall
# allows inbound connections from the buddy (Homebrew's Python is unsigned and
# gets blocked -> the board sees "connection refused"). Falls back to python3.
set -euo pipefail
cd "$(dirname "$0")"
if [ -x /usr/bin/python3 ]; then
  exec /usr/bin/python3 collector.py
else
  exec python3 collector.py
fi
