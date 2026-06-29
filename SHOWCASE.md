# Building Bufo: A Desk Buddy for Claude Code

> A 4-inch frog that lives on my desk, watches my AI pair-programmer work, and
> lets me approve its actions with a tap. Here's how it actually got built —
> dead-ends, pivots, and all.

![Bufo, the Claude Code desk buddy](docs/hero.jpg)
<!-- TODO: drop in a photo of the device on your desk -->

---

## The idea

I spend my days in `claude` and `codex` on the terminal. Two things kept pulling
my attention back to a menubar: *how much am I burning today?* and *Claude is
blocked waiting for me to approve a command.* Both are glanceable, ambient
signals — exactly the kind of thing a screen-in-a-menubar is bad at and a little
object on your desk is good at.

So the goal: a physical companion that

- shows **live Claude Code + Codex usage** (rate-limit windows, tokens, cost),
- reacts with a **mood** to whatever Claude is doing, and
- lets me **approve / deny** tool permissions — and even **answer Claude's
  multiple-choice questions** — by tapping the screen.

I had a cheap Guition **ESP32-4848S040** in a drawer: an ESP32-S3 with a 4.0"
480×480 RGB touch panel, 16MB flash, 8MB PSRAM. Perfect canvas. What followed
was three architectures, two dead radios, and one chip that simply refused to
feel my finger.

---

## Dead end #1: BLE and Wi-Fi cannot coexist on this board

The first design was the "obvious" one. Anthropic ships a
[`claude-desktop-buddy`](https://github.com/anthropics/claude-desktop-buddy)
sample — the Claude **desktop app** pairs with a BLE device over a Nordic UART
service and streams it sessions, prompts, and approve/deny events. Meanwhile,
**cost** lives elsewhere, so I'd pull dollars over **Wi-Fi** from a local
collector. BLE for control, Wi-Fi for money. Clean.

It didn't work, and the reason took a while to nail down. With the ST7701 RGB
panel running, the Arduino/FreeRTOS core, and LVGL, **~312KB of the 320KB
internal RAM was already gone** before my code did anything. (The framebuffer
and LVGL draw buffers are already pushed into PSRAM via board flags — this is
*after* that optimization.) Bring Wi-Fi up and free internal RAM drops to
**7.5KB**. The BLE controller then dies on boot with:

```
E BLE_INIT: Malloc failed
```

I chased it as a radio-coexistence problem first — NimBLE instead of Bluedroid,
`WiFi.setSleep()`, `esp_coex_preference_set(PREFER_BT)`, longer BLE supervision
timeouts. None of it mattered, because **it was never about radio arbitration —
it was RAM.** Two heavy stacks simply don't fit in internal SRAM next to an RGB
framebuffer pipeline.

The fix was to stop pretending they could share. The firmware became **two
mutually-exclusive modes** behind a single `#define ENABLE_WIFI` in `main.cpp`:

- `0` → **BLE companion** (Wi-Fi off): live sessions, prompts, approve/deny.
- `1` → **Wi-Fi cost dashboard** (BLE off): dollars and rolling usage windows.

With Wi-Fi off, ~48KB internal RAM is free and BLE is rock-solid. One more
gotcha cost an evening: macOS would *connect* but never *bond*, so no data ever
flowed. The buddy's characteristics had been set encrypted-only; switching them
to **unencrypted** NOTIFY/WRITE (`setSecurityAuth(false,false,false)`) was what
finally let bytes through.

It worked. It was also annoying: BLE only carried what the **desktop app**
emitted, and I live in the **CLI**. Time for architecture #2.

---

## The pivot: CLI hooks over USB serial

The insight that reframed the whole project: **Claude Code already has a clean,
documented way to intercept exactly the events I cared about — hooks.** Every
permission request, every tool call, every question Claude asks fires a hook
with a JSON payload on stdin, and the hook's exit/output can *steer* the
session. No BLE. No desktop app. No cloud account.

So I threw BLE away entirely and rebuilt around a USB cable:

```
Claude Code CLI ──hook (stdin JSON)──▶ bridge.py ──USB serial──▶ Bufo
       ▲                                                          │
       └──────────── allow / deny / answer ◀──── your touch ◀─────┘
```

Three small pieces, each doing one job:

- **`bridge.py`** owns the serial port and serves a *localhost-only* HTTP
  endpoint the hooks POST to. `POST /permission` blocks, shows an approve/deny
  card on the frog, and returns the decision the user taps. A background thread
  pushes activity + cost/token state to the device every 2s. One lock
  (`_prompt_gate`) guarantees one card on screen at a time.
- **Hook scripts** (`hooks/buddy-*.py`) wired into `~/.claude/settings.json`:
  `PermissionRequest` → approve/deny; `PreToolUse` (matcher `AskUserQuestion`)
  → tap-to-answer; and a fire-and-forget activity hook on
  `PostToolUse`/`Notification`/`Stop`/`SessionStart`. **Every hook fails safe:**
  if the bridge is down or slow, it exits 0 and the normal CLI flow continues
  untouched.
- **`collector.py`** computes the usage numbers (more on its data source below).

A subtle but important detail: the localhost HTTP design dodged a macOS
**Application Firewall** problem that had bitten the earlier Wi-Fi collector —
the firewall refuses inbound connections to Homebrew Python but auto-allows
Apple-signed `/usr/bin/python3`. Keeping the bridge loopback-only sidesteps the
whole question.

### The hands-free trick: answering questions through a *denial*

Permission approve/deny was straightforward — the hook can return an `allow` or
`deny` decision directly. **Answering an `AskUserQuestion`** was not: hooks can
block a tool but can't *hand Claude an answer*.

The workaround is almost cheeky. When `AskUserQuestion` fires, the buddy shows
up to four option buttons. Whatever the user taps, the hook **denies** the tool
call with the reason `"user selected: <label>"`. Claude reads that denial
reason and continues as if the user had answered — hands-free, from a tap on a
frog. It's a hack, but it's a *reliable* hack, and it's the feature that makes
the thing feel magical.

---

## Where the numbers come from

The dashboard's four gauges (Claude 5h/7d, Codex 5h/7d) and the cost figure had
to **match the menubar apps exactly**, or they'd feel fake. The best source
turned out to be **Flux Island**, an internal menubar app that already persists
a unified, per-call token ledger as plain JSON on disk:

```
~/Library/Application Support/flux-desktop-app/stats/token_records.json
```

Each record has the tool (`claude`/`codex`/`opencode`), a millisecond
timestamp, and a full token breakdown (input, output, cache-read,
cache-creation). `collector.py` reads it **read-only** — your own local data,
no network interception — computes today's per-tool cost from a pricing table,
and reconstructs the same rolling 5-hour windows Flux displays (validated: the
window reset timers line up). If Flux isn't installed, it falls back to
scraping `~/.claude` and `~/.codex` logs directly.

---

## The hardware fights back

Software architecture settled, the *board itself* had two more lessons to
teach.

### The "Cache disabled" crash: RGB DMA vs. flash

The RGB panel streams pixels via GDMA, and its refill ISR lives in flash. The
moment **any** flash or NVS access happens while that DMA is running, the cache
is briefly disabled and the ISR — running *from* flash — faults:

```
Cache disabled but cached memory region accessed
```

Two fixes were both required:

1. **Set the RGB bounce buffer size to 0.** A non-zero bounce buffer makes the
   refill ISR memcpy framebuffer data out of PSRAM via the CPU, which trips the
   exact cache fault under flash load. (The runtime setter divides by zero on a
   size of 0, so this had to be patched in the board preset header directly.)
2. **Do every flash/NVS read *before* `board->begin()` starts the DMA.** BLE PHY
   calibration, loading saved stats, the pet name — all of it has to happen
   while the panel is still dark. Once the DMA is live, the device stays
   deliberately flash-free (which is also why runtime "save" operations are
   neutered to RAM-only — a stray NVS write on "approve" would crash the
   display).

### Touch: a chip-level dead end

This one I lost. The GT911 capacitive touch controller is alive on I2C — it
reports product ID `'911'`, its status register flips to "data-ready" on a
press — but the **touch-count nibble reads zero fingers on every single tap.**

I proved it wasn't software by running three completely different driver stacks
against it, including Espressif's own IDF `esp_lcd_touch_gt911` — *the exact
driver the board's original firmware used* — initialized correctly. Identical
result every time: data-ready, zero fingers. The chip also reports a garbage
resolution (1085×600 instead of 480×480), which points at a corrupt or unloaded
sensing config, made worse by this board routing the GT911 reset line off-pin
so a clean hardware reset isn't possible. The decisive next experiment is to
re-flash the **original factory firmware backup** (saved as an 8MB image before
any of this started) and see whether *it* can still sense touch — if it can, it
writes a good GT911 config I need to replicate byte-for-byte.

The honest status: **on-device touch isn't working yet on my unit.** The full
approve/deny/answer loop is proven end-to-end over the serial protocol; the
last mile is this one stubborn digitizer.

---

## Getting to the final port

The last big move was migrating the display layer onto Espressif's
**`ESP32_Display_Panel`** — the same library the factory firmware used — which
brought correct board init for the panel. That migration had its own checklist:
it needs the **pioarduino** platform fork (arduino-esp32 3.1 / IDF 5.3, because
the library uses IDF-5.3 LCD APIs missing from the stock platform), a couple of
extra `lib_deps` the dependency finder won't auto-pull, board selection via a
config header, and — since the library still ships LVGL v8 — a **hand-written
LVGL v9 bridge** (`lv_display_create` + a partial PSRAM draw buffer +
`drawBitmap` flush, plus a pointer input device). All of that is pinned in
`firmware/platformio.ini` so a fresh checkout builds with one command.

---

## What it is now

- **A mood frog** (Bufo, from Anthropic's `claude-desktop-buddy` sample,
  rendered as animated GIFs via LVGL): sleeps when idle, thinks while Claude
  generates, gets busy on a tool, alerts when it needs you, celebrates when a
  turn finishes.
- **Four live usage gauges** plus tokens and cost today, matching the menubar
  apps because they read the same ledger.
- **Touch approve / deny** and **tap-to-answer**, driven straight from a real
  `claude` session over a USB cable — no cloud, no BLE, no extra account.

```
~4,000 lines across:
  firmware/src/main.cpp        630   UI + serial protocol (C++ / LVGL 9)
  bridge.py                    394   serial owner + localhost hook endpoint
  collector.py                 384   usage/cost from local ledgers
  hooks/buddy-*.py                   Claude Code CLI hooks (stdlib only)
```

---

## Lessons worth keeping

1. **Constraints beat cleverness.** The BLE/Wi-Fi saga ended not when I tuned
   the radios but when I accepted that two stacks won't fit in 320KB and split
   them into modes. The RAM was the design, not the radios.
2. **Use the platform's real seams.** Claude Code's hooks turned a hardware
   integration into three small, fail-safe scripts. The best version of this
   project has *less* exotic tech than the first version, not more.
3. **Fail safe at every boundary.** Every hook exits 0 when the bridge is
   absent. The buddy can be unplugged mid-session and `claude` never notices.
4. **Know when a wall is a wall.** The GT911 is, as far as I can prove, a
   chip-level fault — and saying so plainly is more useful than another
   driver-swap that was never going to help.

---

*A hobby project — not affiliated with or endorsed by Anthropic. Bufo the frog
is from Anthropic's `claude-desktop-buddy` sample. Built with
[Claude Code](https://claude.com/claude-code).*
