#!/bin/bash
# Start the Claude-buddy bridge: owns the buddy's USB serial port and serves
# the localhost endpoint the Claude Code hooks talk to. Keep this running.
cd "$(dirname "$0")"
export BUDDY_SERIAL="${BUDDY_SERIAL:-/dev/cu.usbserial-2110}"
exec .venv/bin/python bridge.py
