# 🐸 Bufo — `claude-bufo-esp32`

A physical desk companion that shows your **live Claude Code & Codex usage** and
lets you **approve/deny tool permissions** — and even **answer Claude's
questions** — by tapping the screen. A little frog (Bufo) reacts to whatever
Claude is doing.

It runs on a cheap 4" touch display and is driven entirely by **Claude Code CLI
hooks over USB serial** — no cloud, no BLE, no extra account.

```
Claude Code CLI ──hook (stdin JSON)──▶ bridge.py ──USB serial──▶ Bufo
       ▲                                                          │
       └──────────── allow / deny / answer ◀──── your touch ◀─────┘
```

## What it shows

- **Mood frog** — sleeps when idle, *thinks* while Claude generates, gets *busy*
  on a tool, *alerts* when waiting on you, *celebrates* when a turn finishes.
- **Four real usage gauges** — Claude 5h / 7d and Codex 5h / 7d, read from the
  *same* rate-limit sources the menubar apps use (so the % matches exactly).
- **Tokens & cost today**, plus a live **recent activity** feed.
- **Touch APPROVE / DENY** for permission prompts, and **tap-to-answer** for
  multiple-choice questions — both come straight from your `claude` session.

## Hardware

- **Guition ESP32-4848S040** — ESP32-S3, 4.0" 480×480 ST7701 RGB LCD, GT911
  capacitive touch, 16MB flash, 8MB octal PSRAM.
- A USB cable to your Mac (that's the data link, too).

## How it's wired

| Piece | Role |
|---|---|
| `firmware/` | PlatformIO project (ESP32_Display_Panel + LVGL 9). The UI + serial protocol. |
| `collector.py` | Reads local usage ledgers and Claude/Codex rate-limit caches; computes the gauges, tokens, and cost. |
| `bridge.py` | Owns the USB-serial port, serves a localhost endpoint the hooks call, and relays your touch decisions back. |
| `hooks/` | Claude Code hook scripts (`PermissionRequest`, `PreToolUse` for questions, activity) — register these in `~/.claude/settings.json`. |

## Quick start

1. **Flash the firmware** (needs [PlatformIO](https://platformio.org/)):
   ```bash
   cd firmware && pio run -e esp32-4848S040CIY3 -t upload
   ```
2. **Run the bridge** (owns the serial link; keep it running):
   ```bash
   ./run-bridge.sh
   ```
3. **Register the hooks** by adding `hooks/buddy-*.py` to your
   `~/.claude/settings.json` under `PermissionRequest`, `PreToolUse`
   (matcher `AskUserQuestion`), and the activity events.

Then just use `claude` as usual — permission prompts and questions appear on the
frog.

## Notes

- The display and touch run on the **pioarduino** platform (arduino-esp32 3.1 /
  IDF 5.3), which `firmware/platformio.ini` pins automatically.
- The RGB panel + PSRAM framebuffer is tuned to avoid the "cache disabled" crash
  (no bounce buffer, 12.5 MHz pixel clock, flash reads done before the DMA
  starts). Avoid writing NVS while the display is live.

## Credits

- **Bufo** the frog character is from Anthropic's
  [`anthropics/claude-desktop-buddy`](https://github.com/anthropics/claude-desktop-buddy)
  sample.
- Dashboard styling iterated with [claude.ai/design](https://claude.ai/design).
- Built with [Claude Code](https://claude.com/claude-code).

---

*A hobby project — not affiliated with or endorsed by Anthropic.*
