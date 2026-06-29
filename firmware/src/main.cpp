// Claude Code / Codex desk buddy — COMBINED (BLE companion + Wi-Fi cost)
// Guition ESP32-4848S040 (4.0" 480x480 ST7701 RGB + GT911 touch)
//
//   BLE  : speaks the official Claude desktop "Hardware Buddy" protocol over
//          Nordic UART (reuses ble_bridge.* + stats.h from
//          github.com/anthropics/claude-desktop-buddy). Shows live sessions,
//          current activity, recent transcript lines, tokens-today, and a
//          touch APPROVE / DENY card for permission prompts.
//   Wi-Fi: polls the local collector (collector.py) for the $ cost today,
//          which the BLE feed doesn't carry (it reports tokens, not dollars).
//
// Pair from Claude for macOS: Help -> Troubleshooting -> Enable Developer Mode,
// then Developer -> Open Hardware Buddy -> Connect -> pick "Claude-Buddy".

#include <Arduino.h>
#include <esp_display_panel.hpp>   // Espressif board lib — correct GT911 init for this board
#include <lvgl.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "stats.h"
#include "bufo_gifs.h"   // embedded bufo character animations (from claude-desktop-buddy)

using namespace esp_panel::drivers;
using namespace esp_panel::board;

// SF Mono fonts (generated via lv_font_conv) — monospace dashboard look.
extern "C" {
  extern const lv_font_t mono_14, mono_20, mono_28, mono_40;
}

// ---- Config -----------------------------------------------------------------
// The buddy is driven entirely over USB serial by collector.py on the Mac,
// which is fed by Claude Code CLI hooks. No BLE, no Wi-Fi.

// ---- Palette ----------------------------------------------------------------
// Cool (blue) theme — your pick on the buddy. Alerts stay red for clarity.
#define COL_BG       0x081420
#define COL_CARD     0x132533
#define COL_TEXT     0xE7EEF6
#define COL_MUTED    0x7E93A8
#define COL_CLAUDE   0x4FA3E3
#define COL_CODEX    0x35C2C2
#define COL_AMBER    0x6FB7F2
#define COL_GOOD     0x4FC3F7
#define COL_ALERT    0xE5534B
// Gauge ring accents — matched to the Claude Monitor design
#define COL_G_C5H    0x818CF8   // Claude 5h (indigo)
#define COL_G_C7D    0xA78BFA   // Claude 7d (violet)
#define COL_G_X5H    0x2DD4BF   // Codex 5h (teal)
#define COL_G_X7D    0x38BDF8   // Codex 7d (sky)

// ---- Live state (from BLE protocol) ----------------------------------------
struct Buddy {
  uint8_t total = 0, running = 0, waiting = 0;
  bool completed = false;
  uint32_t tokensToday = 0;
  char msg[40] = "";
  char lines[6][92];
  uint8_t nLines = 0;
  char promptId[40] = "", promptTool[24] = "", promptHint[200] = "";
  char questionId[40] = "", questionText[160] = "";
  char qOpts[4][48];
  uint8_t qOptCount = 0;
  bool questionInfo = false;        // observation-only (answer on the computer)
  uint32_t questionShownMs = 0;
  uint32_t lastLiveMs = 0;
  uint32_t promptShownMs = 0;
} B;
static Board *board = nullptr;
static LCD *lcd = nullptr;
static Touch *touch = nullptr;

// LVGL v9 flush: partial updates into the single PSRAM framebuffer. At the low
// (12.5MHz) pclk the scanout keeps up from PSRAM, so there's no drift, and with
// one framebuffer there's no swap, so no full-frame flashing.
static void lvFlushCb(lv_display_t *disp, const lv_area_t *area, uint8_t *px_map) {
  int w = area->x2 - area->x1 + 1;
  int h = area->y2 - area->y1 + 1;
  lcd->drawBitmap(area->x1, area->y1, w, h, px_map);
  lv_display_flush_ready(disp);
}

// LVGL v9 touch read via ESP32_Display_Panel's GT911 driver.
static void lvTouchCb(lv_indev_t *, lv_indev_data_t *data) {
  TouchPoint p;
  int n = touch ? touch->readPoints(&p, 1, 0) : 0;
  if (n > 0) {
    data->point.x = p.x;
    data->point.y = p.y;
    data->state = LV_INDEV_STATE_PRESSED;
    static uint32_t lastLog = 0;
    if (millis() - lastLog > 200) { Serial.printf("[touch] (%d,%d)\n", p.x, p.y); lastLog = millis(); }
  } else {
    data->state = LV_INDEV_STATE_RELEASED;
  }
}
static float costToday = -1.0f;       // from Wi-Fi collector; <0 = unknown
static long  msgsToday = 0;
// 5h usage windows (from Flux Island ledger via the collector)
static uint32_t winClaudeTok = 0, winCodexTok = 0;
static int winClaudeReset = -1, winCodexReset = -1;  // minutes; <0 = unknown
static float winClaudePct = -1, winCodexPct = -1;    // % of 5h budget used; <0 = unknown
static int q5hPct = -1, q7dPct = -1;                 // real Claude 5h / 7d % (from Flux's cache)
static int qx5hPct = -1, qx7dPct = -1;               // real Codex 5h / weekly %
static uint32_t timeBaseLocal = 0, timeBaseMs = 0;  // from {"time":[...]}

// ---- LVGL widgets ----------------------------------------------------------
static lv_obj_t *lblClock, *lblNet;
static lv_obj_t *bufo;                 // animated bufo character (lv_gif)
static const lv_image_dsc_t *bufoSrc;  // currently shown animation
static lv_obj_t *lblBig, *lblMsg, *lblActive, *statusDot, *heroCard;
static lv_obj_t *listBox, *lblTokens, *lblCost;
static lv_obj_t *arc[4], *arcPct[4];   // Claude 5h, Claude 7d, Codex 5h, Codex 7d
static lv_obj_t *promptCard, *lblPromptTool, *lblPromptHint;
static lv_obj_t *passkeyCard, *lblPasskey;
static lv_obj_t *questionCard, *lblQuestion, *lblQTitle, *optBtn[4], *optLbl[4], *kbdBtn;

static bool dataConnected() { return B.lastLiveMs && (millis() - B.lastLiveMs) <= 30000; }

// ---- Serial protocol: outgoing (buddy -> collector) -------------------------
// Lines are prefixed with a marker so the collector can pick them out of the
// boot/log noise on the same UART.
static void sendJson(const char *json) {
  Serial.print("\x02" "BUDDY ");   // split literal: \x02 must not absorb 'B' as a hex digit
  Serial.println(json);
}

static void sendPermission(const char *id, const char *decision) {
  char buf[96];
  snprintf(buf, sizeof(buf), "{\"id\":\"%s\",\"decision\":\"%s\"}", id, decision);
  sendJson(buf);
}

// ---- Serial protocol: incoming (collector -> buddy) -------------------------
static void applyLine(const char *line) {
  JsonDocument doc;
  if (deserializeJson(doc, line)) return;

  // Time sync: {"time":[epoch_sec, tz_offset_sec]}
  JsonArray t = doc["time"];
  if (!t.isNull() && t.size() == 2) {
    timeBaseLocal = (uint32_t)((int64_t)t[0].as<uint32_t>() + (int32_t)t[1]);
    timeBaseMs = millis();
    B.lastLiveMs = millis();
    return;
  }

  // Owner name push: {"owner":"Anup"}
  const char *ow = doc["owner"];
  if (ow) { ownerSet(ow); B.lastLiveMs = millis(); return; }

  // Heartbeat snapshot
  B.total     = doc["total"]     | B.total;
  B.running   = doc["running"]   | B.running;
  B.waiting   = doc["waiting"]   | B.waiting;
  B.completed = doc["completed"] | false;
  if (doc["tokens"].is<uint32_t>()) statsOnBridgeTokens(doc["tokens"].as<uint32_t>());
  B.tokensToday = doc["tokens_today"] | B.tokensToday;
  // Cost + 5h windows pushed from the collector (Flux ledger).
  if (!doc["cost_usd"].isNull()) costToday = doc["cost_usd"].as<float>();
  winClaudeTok   = doc["win_c_tok"] | winClaudeTok;
  winClaudeReset = doc["win_c_min"] | winClaudeReset;
  winCodexTok    = doc["win_x_tok"] | winCodexTok;
  winCodexReset  = doc["win_x_min"] | winCodexReset;
  if (!doc["win_c_pct"].isNull()) winClaudePct = doc["win_c_pct"].as<float>();
  if (!doc["win_x_pct"].isNull()) winCodexPct  = doc["win_x_pct"].as<float>();
  if (!doc["q5h"].isNull()) q5hPct = doc["q5h"].as<int>();
  if (!doc["q7d"].isNull()) q7dPct = doc["q7d"].as<int>();
  if (!doc["qx5h"].isNull()) qx5hPct = doc["qx5h"].as<int>();
  if (!doc["qx7d"].isNull()) qx7dPct = doc["qx7d"].as<int>();
  const char *m = doc["msg"];
  if (m) { strncpy(B.msg, m, sizeof(B.msg) - 1); B.msg[sizeof(B.msg) - 1] = 0; }

  JsonArray la = doc["entries"];
  if (!la.isNull()) {
    uint8_t n = 0;
    for (JsonVariant v : la) {
      if (n >= 6) break;
      const char *s = v.as<const char *>();
      strncpy(B.lines[n], s ? s : "", 91); B.lines[n][91] = 0; n++;
    }
    B.nLines = n;
  }

  // Prompt is set/cleared only by explicit messages so periodic state
  // heartbeats (which carry no "prompt" key) don't wipe a live prompt.
  JsonObject pr = doc["prompt"];
  if (!pr.isNull()) {
    const char *pid = pr["id"], *pt = pr["tool"], *ph = pr["hint"];
    if (strcmp(B.promptId, pid ? pid : "") != 0) B.promptShownMs = millis();
    strncpy(B.promptId,   pid ? pid : "", sizeof(B.promptId) - 1);   B.promptId[sizeof(B.promptId) - 1] = 0;
    strncpy(B.promptTool, pt  ? pt  : "", sizeof(B.promptTool) - 1); B.promptTool[sizeof(B.promptTool) - 1] = 0;
    strncpy(B.promptHint, ph  ? ph  : "", sizeof(B.promptHint) - 1); B.promptHint[sizeof(B.promptHint) - 1] = 0;
  }
  if (doc["pclear"] | false) {
    B.promptId[0] = B.promptTool[0] = B.promptHint[0] = 0;
  }

  // AskUserQuestion: {"question":{"id","q","opts":["A","B",...]}}
  JsonObject q = doc["question"];
  if (!q.isNull()) {
    const char *qid = q["id"], *qt = q["q"];
    strncpy(B.questionId,   qid ? qid : "", sizeof(B.questionId) - 1);   B.questionId[sizeof(B.questionId) - 1] = 0;
    strncpy(B.questionText, qt  ? qt  : "", sizeof(B.questionText) - 1); B.questionText[sizeof(B.questionText) - 1] = 0;
    uint8_t n = 0;
    for (JsonVariant v : q["opts"].as<JsonArray>()) {
      if (n >= 4) break;
      const char *s = v.as<const char *>();
      strncpy(B.qOpts[n], s ? s : "", 47); B.qOpts[n][47] = 0; n++;
    }
    B.qOptCount = n;
    B.questionInfo = q["info"] | false;   // observation-only (answer on computer)
    B.questionShownMs = millis();
  }
  if (doc["qclear"] | false) { B.questionId[0] = 0; B.qOptCount = 0; }

  B.lastLiveMs = millis();
}

static char btLine[1024];
static uint16_t btLen = 0;
static void pumpSerial() {
  while (Serial.available()) {
    int c = Serial.read();
    if (c < 0) break;
    if (c == '\n' || c == '\r') {
      if (btLen > 0) { btLine[btLen] = 0; if (btLine[0] == '{') applyLine(btLine); btLen = 0; }
    } else if (btLen < sizeof(btLine) - 1) {
      btLine[btLen++] = (char)c;
    }
  }
}

// ---- UI helpers -------------------------------------------------------------
static lv_obj_t *mkLabel(lv_obj_t *p, const lv_font_t *f, uint32_t c) {
  lv_obj_t *l = lv_label_create(p);
  lv_obj_set_style_text_font(l, f, 0);
  lv_obj_set_style_text_color(l, lv_color_hex(c), 0);
  lv_label_set_text(l, "");
  return l;
}
static void styleCard(lv_obj_t *o, uint32_t c) {
  lv_obj_remove_style_all(o);
  lv_obj_set_style_bg_color(o, lv_color_hex(c), 0);
  lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(o, 16, 0);
  lv_obj_set_style_pad_all(o, 12, 0);
  lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
}
static String humanTokens(uint32_t t) {
  char b[16];
  if (t >= 1000000) snprintf(b, sizeof(b), "%.1fM", t / 1e6);
  else if (t >= 1000) snprintf(b, sizeof(b), "%.1fK", t / 1e3);
  else snprintf(b, sizeof(b), "%lu", (unsigned long)t);
  return String(b);
}

static void setBufo(const lv_image_dsc_t *src) {
  if (src == bufoSrc) return;          // lv_gif_set_src restarts the gif; only on change
  bufoSrc = src;
  lv_gif_set_src(bufo, src);
}

// ---- Touch handlers ---------------------------------------------------------
static void onApprove(lv_event_t *) {
  if (!B.promptId[0]) return;
  sendPermission(B.promptId, "allow");
  uint32_t secs = (millis() - B.promptShownMs) / 1000;
  statsOnApproval(secs);
  B.promptId[0] = 0;
}
static void onDeny(lv_event_t *) {
  if (!B.promptId[0]) return;
  sendPermission(B.promptId, "deny");
  statsOnDenial();
  B.promptId[0] = 0;
}

// Observation-only (Flux model): the buddy displays the question, you answer in
// the terminal. A tap just dismisses the card.
static void onOption(lv_event_t *) {
  B.questionId[0] = 0; B.qOptCount = 0;
}
static void onKbd(lv_event_t *) {
  B.questionId[0] = 0; B.qOptCount = 0;
}

// ---- Build UI ---------------------------------------------------------------
// Rounded card with a subtle vertical gradient + hairline border.
static lv_obj_t *mkGradCard(lv_obj_t *p, int x, int y, int w, int h, uint32_t top, uint32_t bot) {
  lv_obj_t *c = lv_obj_create(p);
  lv_obj_remove_style_all(c);
  lv_obj_set_size(c, w, h);
  lv_obj_align(c, LV_ALIGN_TOP_LEFT, x, y);
  lv_obj_set_style_radius(c, 18, 0);
  lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(c, lv_color_hex(top), 0);
  lv_obj_set_style_bg_grad_color(c, lv_color_hex(bot), 0);
  lv_obj_set_style_bg_grad_dir(c, LV_GRAD_DIR_VER, 0);
  lv_obj_set_style_border_width(c, 1, 0);
  lv_obj_set_style_border_color(c, lv_color_hex(0x294056), 0);
  lv_obj_set_style_pad_all(c, 14, 0);
  lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
  return c;
}

// A 270-degree arc gauge with a % label in the middle and a caption below.
static lv_obj_t *mkArc(lv_obj_t *p, int x, int y, int d, uint32_t color, const char *cap, lv_obj_t **pctOut) {
  lv_obj_t *a = lv_arc_create(p);
  lv_obj_set_size(a, d, d);
  lv_obj_align(a, LV_ALIGN_TOP_LEFT, x, y);
  lv_arc_set_rotation(a, 135);
  lv_arc_set_bg_angles(a, 0, 270);
  lv_arc_set_range(a, 0, 100);
  lv_arc_set_value(a, 0);
  lv_obj_remove_flag(a, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_arc_width(a, 9, LV_PART_MAIN);
  lv_obj_set_style_arc_color(a, lv_color_hex(0x1E3346), LV_PART_MAIN);
  lv_obj_set_style_arc_rounded(a, true, LV_PART_MAIN);
  lv_obj_set_style_arc_width(a, 9, LV_PART_INDICATOR);
  lv_obj_set_style_arc_color(a, lv_color_hex(color), LV_PART_INDICATOR);
  lv_obj_set_style_arc_rounded(a, true, LV_PART_INDICATOR);
  lv_obj_set_style_bg_opa(a, LV_OPA_TRANSP, LV_PART_KNOB);
  lv_obj_set_style_pad_all(a, 0, LV_PART_KNOB);
  lv_obj_t *pct = mkLabel(a, &mono_20, COL_TEXT);
  lv_obj_center(pct);
  *pctOut = pct;
  lv_obj_t *c = mkLabel(p, &lv_font_montserrat_14, COL_MUTED);
  lv_label_set_text(c, cap);
  lv_obj_set_width(c, d);
  lv_obj_set_style_text_align(c, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(c, LV_ALIGN_TOP_LEFT, x, y + d + 1);
  return a;
}

static void buildUI() {
  lv_obj_t *scr = lv_screen_active();
  lv_obj_remove_style_all(scr);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(scr, lv_color_hex(COL_BG), 0);
  lv_obj_set_style_bg_grad_color(scr, lv_color_hex(0x03070D), 0);
  lv_obj_set_style_bg_grad_dir(scr, LV_GRAD_DIR_VER, 0);
  lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);

  // ---- Header: status dot + LIVE + active + clock ----
  statusDot = lv_obj_create(scr);
  lv_obj_remove_style_all(statusDot);
  lv_obj_set_size(statusDot, 12, 12);
  lv_obj_set_style_radius(statusDot, 6, 0);
  lv_obj_set_style_bg_opa(statusDot, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(statusDot, lv_color_hex(COL_GOOD), 0);
  lv_obj_align(statusDot, LV_ALIGN_TOP_LEFT, 18, 15);
  lblNet = mkLabel(scr, &mono_14, COL_TEXT);
  lv_obj_align(lblNet, LV_ALIGN_TOP_LEFT, 37, 12);
  lblActive = mkLabel(scr, &mono_14, COL_MUTED);
  lv_obj_align(lblActive, LV_ALIGN_TOP_LEFT, 92, 12);
  lblClock = mkLabel(scr, &mono_20, COL_TEXT);
  lv_obj_align(lblClock, LV_ALIGN_TOP_RIGHT, -18, 8);

  // ---- Hero card: status/activity (left of the floating bufo) + tokens/cost (right) ----
  heroCard = mkGradCard(scr, 12, 44, 456, 110, 0x163143, 0x0D1A27);
  lblBig = mkLabel(heroCard, &mono_28, COL_CLAUDE);
  lv_obj_align(lblBig, LV_ALIGN_TOP_LEFT, 102, 6);
  lblMsg = mkLabel(heroCard, &mono_20, COL_MUTED);
  lv_obj_set_size(lblMsg, 200, 26);                 // one line height -> ellipsis, never wraps
  lv_label_set_long_mode(lblMsg, LV_LABEL_LONG_DOT);
  lv_obj_align(lblMsg, LV_ALIGN_TOP_LEFT, 104, 44);
  lblTokens = mkLabel(heroCard, &mono_28, COL_TEXT);
  lv_obj_align(lblTokens, LV_ALIGN_TOP_RIGHT, -2, 2);
  lv_obj_t *tcap = mkLabel(heroCard, &mono_14, COL_MUTED);
  lv_label_set_text(tcap, "tokens today");
  lv_obj_align(tcap, LV_ALIGN_TOP_RIGHT, -2, 34);
  lblCost = mkLabel(heroCard, &mono_28, COL_AMBER);
  lv_obj_align(lblCost, LV_ALIGN_TOP_RIGHT, -2, 54);

  // bufo floats on the screen over the hero's left so the card never clips it
  bufo = lv_gif_create(scr);
  bufoSrc = &bufo_idle;
  lv_gif_set_src(bufo, &bufo_idle);
  lv_image_set_scale(bufo, 256);          // 1x -> 96x100
  lv_obj_set_style_radius(bufo, 16, 0);
  lv_obj_set_style_clip_corner(bufo, true, 0);   // rounded corners on the gif
  lv_obj_align(bufo, LV_ALIGN_TOP_LEFT, 16, 50);
  lv_obj_move_foreground(bufo);

  // ---- Four arc gauges (design colors): Claude 5h / 7d + Codex 5h / 7d ----
  const int AY = 176, AD = 76;
  arc[0] = mkArc(scr,  20, AY, AD, COL_G_C5H, "Claude 5h", &arcPct[0]);
  arc[1] = mkArc(scr, 140, AY, AD, COL_G_C7D, "Claude 7d", &arcPct[1]);
  arc[2] = mkArc(scr, 260, AY, AD, COL_G_X5H, "Codex 5h",  &arcPct[2]);
  arc[3] = mkArc(scr, 380, AY, AD, COL_G_X7D, "Codex 7d",  &arcPct[3]);

  // ---- Recent activity card (fills the lower area) ----
  listBox = mkGradCard(scr, 12, 296, 456, 172, 0x122230, 0x0A141E);
  lv_obj_set_style_pad_all(listBox, 14, 0);
  lv_obj_set_flex_flow(listBox, LV_FLEX_FLOW_COLUMN);
  lv_obj_set_style_pad_row(listBox, 6, 0);

  // Permission overlay card (hidden until a prompt arrives)
  promptCard = lv_obj_create(scr);
  styleCard(promptCard, 0x182634);
  lv_obj_set_style_border_width(promptCard, 3, 0);
  lv_obj_set_style_border_color(promptCard, lv_color_hex(COL_ALERT), 0);
  lv_obj_set_size(promptCard, 440, 300);
  lv_obj_center(promptCard);
  lv_obj_t *pTitle = mkLabel(promptCard, &mono_20, COL_ALERT);
  lv_label_set_text(pTitle, "PERMISSION NEEDED");
  lv_obj_align(pTitle, LV_ALIGN_TOP_MID, 0, 0);
  lblPromptTool = mkLabel(promptCard, &mono_40, COL_TEXT);
  lv_obj_align(lblPromptTool, LV_ALIGN_TOP_MID, 0, 36);
  lblPromptHint = mkLabel(promptCard, &mono_20, COL_MUTED);
  lv_obj_set_width(lblPromptHint, 410);
  lv_label_set_long_mode(lblPromptHint, LV_LABEL_LONG_WRAP);
  lv_obj_set_style_text_align(lblPromptHint, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(lblPromptHint, LV_ALIGN_CENTER, 0, 0);
  lv_obj_t *btnA = lv_button_create(promptCard);
  lv_obj_set_size(btnA, 190, 64);
  lv_obj_align(btnA, LV_ALIGN_BOTTOM_LEFT, 0, 0);
  lv_obj_set_style_bg_color(btnA, lv_color_hex(COL_GOOD), 0);
  lv_obj_add_event_cb(btnA, onApprove, LV_EVENT_CLICKED, NULL);
  lv_obj_t *la = mkLabel(btnA, &mono_28, 0x06281C); lv_label_set_text(la, "APPROVE"); lv_obj_center(la);
  lv_obj_t *btnD = lv_button_create(promptCard);
  lv_obj_set_size(btnD, 190, 64);
  lv_obj_align(btnD, LV_ALIGN_BOTTOM_RIGHT, 0, 0);
  lv_obj_set_style_bg_color(btnD, lv_color_hex(COL_ALERT), 0);
  lv_obj_add_event_cb(btnD, onDeny, LV_EVENT_CLICKED, NULL);
  lv_obj_t *ld = mkLabel(btnD, &mono_28, 0x2A0B0A); lv_label_set_text(ld, "DENY"); lv_obj_center(ld);
  lv_obj_add_flag(promptCard, LV_OBJ_FLAG_HIDDEN);

  // AskUserQuestion overlay — question + up to 4 tappable option buttons.
  questionCard = lv_obj_create(scr);
  styleCard(questionCard, 0x16202A);
  lv_obj_set_style_border_width(questionCard, 3, 0);
  lv_obj_set_style_border_color(questionCard, lv_color_hex(COL_GOOD), 0);
  lv_obj_set_size(questionCard, 464, 452);
  lv_obj_center(questionCard);
  lblQTitle = mkLabel(questionCard, &mono_20, COL_GOOD);
  lv_label_set_text(lblQTitle, "Claude asks");
  lv_obj_align(lblQTitle, LV_ALIGN_TOP_MID, 0, 0);
  lblQuestion = mkLabel(questionCard, &mono_20, COL_TEXT);
  lv_obj_set_width(lblQuestion, 430);
  lv_label_set_long_mode(lblQuestion, LV_LABEL_LONG_WRAP);
  lv_obj_set_style_text_align(lblQuestion, LV_TEXT_ALIGN_CENTER, 0);
  lv_obj_align(lblQuestion, LV_ALIGN_TOP_MID, 0, 30);
  for (int i = 0; i < 4; i++) {
    optBtn[i] = lv_button_create(questionCard);
    lv_obj_set_size(optBtn[i], 430, 64);
    lv_obj_align(optBtn[i], LV_ALIGN_TOP_MID, 0, 118 + i * 74);
    lv_obj_set_style_bg_color(optBtn[i], lv_color_hex(0x23425C), 0);
    lv_obj_add_event_cb(optBtn[i], onOption, LV_EVENT_CLICKED, (void *)(intptr_t)i);
    optLbl[i] = mkLabel(optBtn[i], &mono_20, COL_TEXT);
    lv_obj_set_width(optLbl[i], 410);
    lv_label_set_long_mode(optLbl[i], LV_LABEL_LONG_DOT);
    lv_obj_center(optLbl[i]);
  }
  // Keyboard escape — defer the answer to the terminal (full typing / multi-select).
  kbdBtn = lv_button_create(questionCard);
  lv_obj_set_size(kbdBtn, 430, 40);
  lv_obj_align(kbdBtn, LV_ALIGN_BOTTOM_MID, 0, -4);
  lv_obj_set_style_bg_color(kbdBtn, lv_color_hex(0x10202C), 0);
  lv_obj_set_style_border_width(kbdBtn, 1, 0);
  lv_obj_set_style_border_color(kbdBtn, lv_color_hex(COL_MUTED), 0);
  lv_obj_add_event_cb(kbdBtn, onKbd, LV_EVENT_CLICKED, NULL);
  lv_obj_t *kl = mkLabel(kbdBtn, &mono_14, COL_MUTED);
  lv_label_set_text(kl, "Answer on keyboard instead");
  lv_obj_center(kl);
  lv_obj_add_flag(questionCard, LV_OBJ_FLAG_HIDDEN);
}

// ---- Render -----------------------------------------------------------------
static uint16_t lastLineHash = 0xFFFF;
static void rebuildList() {
  lv_obj_clean(listBox);
  uint8_t shown = 0;
  for (uint8_t i = 0; i < B.nLines && shown < 8; i++) {
    bool isYou = strncmp(B.lines[i], "you:", 4) == 0;
    lv_obj_t *l = mkLabel(listBox, &lv_font_montserrat_14, isYou ? COL_GOOD : COL_TEXT);
    lv_obj_set_width(l, 426);
    lv_label_set_long_mode(l, LV_LABEL_LONG_DOT);
    lv_label_set_text(l, B.lines[i]);
    shown++;
  }
  if (shown == 0) {
    lv_obj_t *l = mkLabel(listBox, &lv_font_montserrat_14, COL_MUTED);
    lv_label_set_text(l, dataConnected() ? "(no recent activity)" : "waiting for the collector...");
  }
}

static void render() {
  // Clock
  if (timeBaseLocal) {
    uint32_t local = timeBaseLocal + (millis() - timeBaseMs) / 1000;
    time_t tt = (time_t)local; struct tm lt; gmtime_r(&tt, &lt);
    lv_label_set_text_fmt(lblClock, "%02d:%02d", lt.tm_hour, lt.tm_min);
  }

  // Header: status dot + LIVE/OFFLINE + active sessions
  bool conn = dataConnected();
  lv_label_set_text(lblNet, conn ? "LIVE" : "OFFLINE");
  lv_obj_set_style_text_color(lblNet, lv_color_hex(conn ? COL_GOOD : COL_MUTED), 0);
  lv_obj_set_style_bg_color(statusDot, lv_color_hex(conn ? COL_GOOD : 0x3A4A5A), 0);
  lv_label_set_text_fmt(lblActive, "%u active", B.total);

  // Bufo pose follows Claude activity
  if (!conn)                setBufo(&bufo_sleep);      // nothing connected -> asleep
  else if (B.waiting > 0)   setBufo(&bufo_attention);  // permission waiting -> alert
  else if (B.running > 0) {                            // a session generating
    if (strncmp(B.msg, "think", 5) == 0) setBufo(&bufo_thinking);  // pondering a reply
    else                                 setBufo(&bufo_busy);      // running a tool
  }
  else if (B.completed)     setBufo(&bufo_celebrate);  // just finished -> celebrate
  else                      setBufo(&bufo_sleep);      // idle -> dozes off

  // Big status word + colour
  const char *state; uint32_t sc;
  if      (!conn)          { state = "OFFLINE"; sc = COL_MUTED;  }
  else if (B.waiting > 0)  { state = "WAITING"; sc = COL_ALERT;  }
  else if (B.running > 0)  { state = "RUNNING"; sc = COL_GOOD;   }
  else if (B.completed)    { state = "DONE";    sc = COL_CLAUDE; }
  else                     { state = "IDLE";    sc = COL_CLAUDE; }
  lv_label_set_text(lblBig, state);
  lv_obj_set_style_text_color(lblBig, lv_color_hex(sc), 0);
  lv_obj_set_style_border_color(heroCard, lv_color_hex(sc), 0);   // hero glows the state colour

  lv_label_set_text(lblMsg, conn ? B.msg : "no claude connected");

  // Primary metrics (LVGL's built-in printf has no %f, so format $ with libc).
  lv_label_set_text(lblTokens, humanTokens(B.tokensToday).c_str());
  char cost[16];
  if (costToday >= 0) {
    long cents = (long)(costToday * 100 + 0.5f);
    snprintf(cost, sizeof(cost), "$%ld.%02ld", cents / 100, cents % 100);
    lv_label_set_text(lblCost, cost);
  } else {
    lv_label_set_text(lblCost, "$--");
  }

  uint16_t h = B.nLines;
  for (uint8_t i = 0; i < B.nLines; i++) for (const char *p = B.lines[i]; *p; p++) h = h * 31 + *p;
  if (h != lastLineHash) { rebuildList(); lastLineHash = h; }

  // Usage arcs: real Claude 5h/7d + Codex 5h/7d % (Flux's exact sources).
  int qv[4] = { q5hPct, q7dPct, qx5hPct, qx7dPct };
  for (int i = 0; i < 4; i++) {
    if (qv[i] >= 0) { lv_arc_set_value(arc[i], qv[i]); lv_label_set_text_fmt(arcPct[i], "%d%%", qv[i]); }
    else            { lv_arc_set_value(arc[i], 0);     lv_label_set_text(arcPct[i], "--"); }
  }

  // Permission overlay
  if (B.promptId[0]) {
    lv_label_set_text(lblPromptTool, B.promptTool[0] ? B.promptTool : "tool");
    lv_label_set_text(lblPromptHint, B.promptHint);
    lv_obj_clear_flag(promptCard, LV_OBJ_FLAG_HIDDEN);
  } else {
    lv_obj_add_flag(promptCard, LV_OBJ_FLAG_HIDDEN);
  }

  // AskUserQuestion overlay — observation-only: shows what Claude is asking,
  // you answer in the terminal. Auto-dismisses after 45s (or tap to dismiss).
  if (B.questionId[0] && B.questionInfo && millis() - B.questionShownMs > 45000)
    B.questionId[0] = 0;
  if (B.questionId[0]) {
    lv_label_set_text(lblQTitle, B.questionInfo ? "ANSWER IN YOUR TERMINAL" : "Claude asks");
    lv_obj_set_style_text_color(lblQTitle, lv_color_hex(B.questionInfo ? COL_AMBER : COL_GOOD), 0);
    lv_label_set_text(lblQuestion, B.questionText);
    for (int i = 0; i < 4; i++) {
      if (i < B.qOptCount) {
        // In observation mode the options are a read-only list, not buttons,
        // so it's clear you answer on the computer (the buddy can't submit).
        lv_label_set_text_fmt(optLbl[i], B.questionInfo ? "  -  %s" : "%s", B.qOpts[i]);
        lv_obj_set_style_bg_opa(optBtn[i], B.questionInfo ? LV_OPA_TRANSP : LV_OPA_COVER, 0);
        lv_obj_set_style_text_color(optLbl[i], lv_color_hex(B.questionInfo ? COL_MUTED : COL_TEXT), 0);
        if (B.questionInfo) lv_obj_set_style_text_align(optLbl[i], LV_TEXT_ALIGN_LEFT, 0);
        lv_obj_clear_flag(optBtn[i], LV_OBJ_FLAG_HIDDEN);
      } else {
        lv_obj_add_flag(optBtn[i], LV_OBJ_FLAG_HIDDEN);
      }
    }
    lv_obj_add_flag(kbdBtn, LV_OBJ_FLAG_HIDDEN);   // Flux model: no buddy answering
    lv_obj_clear_flag(questionCard, LV_OBJ_FLAG_HIDDEN);
  } else {
    lv_obj_add_flag(questionCard, LV_OBJ_FLAG_HIDDEN);
  }

  if (passkeyCard) lv_obj_add_flag(passkeyCard, LV_OBJ_FLAG_HIDDEN);
}

// ---- Setup / loop -----------------------------------------------------------
void setup() {
  Serial.begin(115200);

  // Display + touch via Espressif's board library (correct GT911 init for this
  // exact board — BOARD_JINGCAI_ESP32_4848S040C_I_Y_3).
  // The board preset's RGB bounce buffer is disabled (set to 0 in the board
  // header) so the GDMA reads the PSRAM framebuffer directly — a non-zero bounce
  // buffer makes the refill ISR copy PSRAM via the CPU and crashes ("Cache
  // disabled...") whenever an NVS/flash read disables the cache.
  board = new Board();
  board->init();

  // CRITICAL ordering: once board->begin() starts the RGB DMA, ANY flash/NVS
  // access (which disables the cache) crashes the RGB GDMA ISR ("Cache
  // disabled..."). So do every one-time flash read NOW, before begin().
  statsLoad();
  petNameLoad();

  // Now start the RGB panel + GT911 touch, then bring up LVGL.
  board->begin();
  lcd = board->getLCD();
  touch = board->getTouch();
  const int W = lcd->getFrameWidth(), H = lcd->getFrameHeight();

  // LVGL v9: partial-mode draw buffer in PSRAM, flush via drawBitmap.
  lv_init();
  static const uint32_t LV_BUF_PX = 480 * 80;   // ~1/6 screen
  void *buf1 = heap_caps_malloc(LV_BUF_PX * 2, MALLOC_CAP_SPIRAM);
  lv_display_t *disp = lv_display_create(W, H);
  lv_display_set_flush_cb(disp, lvFlushCb);
  lv_display_set_buffers(disp, buf1, NULL, LV_BUF_PX * 2, LV_DISPLAY_RENDER_MODE_PARTIAL);
  lv_indev_t *ti = lv_indev_create();
  lv_indev_set_type(ti, LV_INDEV_TYPE_POINTER);
  lv_indev_set_read_cb(ti, lvTouchCb);

  buildUI();
  lv_label_set_text(lblNet, "USB serial — waiting for collector");
  lv_refr_now(disp);
}

void loop() {
  static uint32_t lastTick = millis();
  uint32_t now = millis();
  lv_tick_inc(now - lastTick);
  lastTick = now;

  pumpSerial();

  render();
  lv_timer_handler();
  delay(5);
}
