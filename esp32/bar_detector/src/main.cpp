// Bar detector for the LilyGO T-Display v1.1.
//
// Listens on GPIO32 (ADC1 channel 4) through the I2S built-in-ADC DMA path (~249 kS/s; ADC setup
// adapted from ../ultrasonic_probe) and tells the remote's ~38 kHz bar from its ~47 kHz
// bar (seen by its 42.78 kHz line) with lib/tone_detector. Every DMA buffer is analysed, with no
// gaps, by a task on core 0; the screen and the serial protocol run in loop() on core 1.
//
// Screen: LISTENING shows one SNR meter per band. A detected strike fills the screen for 2 s with
// the band's colour: blue "38 kHz", orange "47 kHz", or grey "38+47?" when neither band leads by
// decisionMarginDb. The measured frequency, peak SNR, ring time and hit count are shown under it.
// Right button (GPIO35): forget the noise baselines and the counters, learn again (1 s, keep quiet).
//
// Serial protocol (921600 baud, one command per line, replies are "KEY k=v ..." lines):
//   info              -> INFO line: rate, bands, thresholds, counters, input DC level, CPU load of the detector
//   lvl <on|off>      -> stream LVL lines (~20/s): per-band SNR, guard SNR, level, DC, state
//   thr <dB>          -> detection threshold over the noise baseline (6-40); THR db=..
//   band <a|b> <Hz>   -> move a band centre (relearns); BAND a=.. b=..
//   reset             -> same as the right button; RESET ok=1
// Unsolicited: READY after boot, then EVT lines: phase=detect when a strike is recognised and
// phase=end when its ringing stops (confirmed=0 for onsets that did not ring down like a bar).

#include <Arduino.h>
#include <TFT_eSPI.h>
#include <ToneDetector.h>
#include <driver/adc.h>
#include <driver/i2s.h>
#include <esp_adc_cal.h>

using bardet::Band;
using bardet::Event;
using bardet::Phase;

constexpr int PIN_SIGNAL = 32;
constexpr adc1_channel_t SIGNAL_CHANNEL = ADC1_CHANNEL_4;  // GPIO32
constexpr i2s_port_t I2S_PORT = I2S_NUM_0;
constexpr uint32_t ADC_RATE_CFG = 250000;  // the ADC tops out at ~249 kS/s; faster I2S rates only repeat samples
constexpr int DMA_BUF_LEN = 1024;          // samples per DMA buffer (4.1 ms)
constexpr int DMA_BUF_COUNT = 8;           // 33 ms of slack if the task is late

constexpr int PIN_BTN_RESET = 35;  // right button, external pull-up on the board
constexpr uint32_t TILE_HOLD_MS = 2000;
constexpr uint32_t METER_PERIOD_MS = 100;
constexpr uint32_t LVL_PERIOD_MS = 50;
constexpr uint32_t CLIP_SHOW_MS = 1000;

// Tile names of the two bars (font 8 has digits only). The measured frequency of the tracked line is
// shown under them: the "47" bar is detected by its 42.78 kHz line.
const char *BAND_NAME[2] = {"38", "47"};
const uint16_t BAND_COLOR[2] = {TFT_BLUE, TFT_ORANGE};
const uint16_t BAND_TEXT[2] = {TFT_WHITE, TFT_BLACK};
const uint16_t METER_COLOR[2] = {TFT_SKYBLUE, TFT_ORANGE};

constexpr int METER_X = 52;
constexpr int METER_W = 140;
constexpr int METER_H = 20;
constexpr int METER_Y[2] = {38, 66};
constexpr float METER_MAX_DB = 60;

TFT_eSPI tft;
esp_adc_cal_characteristics_t adcChars;
float rateMeas = 0;
bardet::ToneDetector detector;  // owned by the sampling task after setup()
bardet::Config activeCfg;       // owned by loop(); sent to the task through `pending`
QueueHandle_t eventQueue;

// Shared between the sampling task (core 0) and loop() (core 1), guarded by mux.
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
struct Shared {
  bardet::Levels levels;
  float dcCode;       // mean code of the last DMA buffer
  uint16_t minCode;   // since loop() last cleared them
  uint16_t maxCode;
  bool clipSeen;      // a sample at 0 or 4095 since loop() last cleared it
  uint32_t badWords;  // samples carrying the wrong ADC channel bits
  float loadPct;      // detector time per DMA buffer / buffer duration (average); above 100 % samples are lost
  float loadMaxPct;   // worst buffer since boot; a block is processed every second buffer, so up to ~200 % is fine
} shared;
struct Pending {
  bool reset;
  bool configure;
  bardet::Config cfg;
} pending;

enum class Screen { Listening, Tile };
Screen screen = Screen::Listening;
uint32_t tileUntil = 0;
uint64_t tileStartSample = 0;
uint32_t hits[3] = {0, 0, 0};  // A, B, BOTH
uint32_t lastClipMs = 0;
bool lvlStream = false;

// ---------- sampling (core 0) ----------

bool startAdc() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_ADC_BUILT_IN);
  cfg.sample_rate = ADC_RATE_CFG;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = DMA_BUF_COUNT;
  cfg.dma_buf_len = DMA_BUF_LEN;
  cfg.use_apll = false;
  if (i2s_driver_install(I2S_PORT, &cfg, 0, nullptr) != ESP_OK) return false;
  i2s_set_adc_mode(ADC_UNIT_1, SIGNAL_CHANNEL);
  adc1_config_channel_atten(SIGNAL_CHANNEL, ADC_ATTEN_DB_12);
  i2s_adc_enable(I2S_PORT);
  return true;
}

void readChunk(uint16_t *dst) {
  size_t got = 0;
  i2s_read(I2S_PORT, dst, DMA_BUF_LEN * sizeof(uint16_t), &got, portMAX_DELAY);
}

// Real sample rate = samples delivered per second of CPU time. The I2S clock dividers do not hit the
// requested rate exactly, and every bin frequency of the detector depends on it.
float measureRate() {
  static uint16_t scratch[DMA_BUF_LEN];
  for (int i = 0; i < DMA_BUF_COUNT + 1; i++) readChunk(scratch);  // drop what queued up
  const int chunks = 64;
  uint32_t t0 = micros();
  for (int i = 0; i < chunks; i++) readChunk(scratch);
  uint32_t dt = micros() - t0;
  return chunks * (float)DMA_BUF_LEN * 1e6f / dt;
}

void onDetectorEvent(const Event &e, void *) { xQueueSend(eventQueue, &e, 0); }

void samplingTask(void *) {
  static uint16_t raw[DMA_BUF_LEN];
  static float x[DMA_BUF_LEN];
  const float bufferUs = DMA_BUF_LEN * 1e6f / rateMeas;
  for (;;) {
    bool doReset, doConfigure;
    bardet::Config cfg;
    portENTER_CRITICAL(&mux);
    doReset = pending.reset;
    doConfigure = pending.configure;
    if (doConfigure) cfg = pending.cfg;
    pending.reset = pending.configure = false;
    portEXIT_CRITICAL(&mux);
    if (doConfigure) {
      detector.configure(cfg);
    } else if (doReset) {
      detector.reset();
    }

    readChunk(raw);
    bool clipped = false;
    uint32_t bad = 0;
    uint32_t sum = 0;
    uint16_t lo = 4095, hi = 0;
    for (int i = 0; i < DMA_BUF_LEN; i++) {
      uint16_t w = raw[i ^ 1];  // the I2S FIFO swaps each pair of 16-bit samples
      bad += (w >> 12) != SIGNAL_CHANNEL;
      uint16_t v = w & 0x0FFF;
      clipped = clipped || v == 0 || v == 4095;
      lo = min(lo, v);
      hi = max(hi, v);
      sum += v;
      x[i] = v;
    }
    uint32_t t0 = micros();
    detector.push(x, DMA_BUF_LEN, clipped);
    float load = (micros() - t0) * 100.0f / bufferUs;

    portENTER_CRITICAL(&mux);
    shared.loadPct += 0.01f * (load - shared.loadPct);
    shared.loadMaxPct = max(shared.loadMaxPct, load);
    shared.levels = detector.levels();
    shared.dcCode = (float)sum / DMA_BUF_LEN;
    shared.minCode = min(shared.minCode, lo);
    shared.maxCode = max(shared.maxCode, hi);
    shared.clipSeen = shared.clipSeen || clipped;
    shared.badWords += bad;
    portEXIT_CRITICAL(&mux);
  }
}

// ---------- display (core 1) ----------

uint32_t codeToMv(float code) { return esp_adc_cal_raw_to_voltage((uint32_t)(code + 0.5f), &adcChars); }

Shared readShared(bool clearRange, bool clearClip) {
  portENTER_CRITICAL(&mux);
  Shared s = shared;
  if (clearRange) {
    shared.minCode = 4095;
    shared.maxCode = 0;
  }
  if (clearClip) shared.clipSeen = false;
  portEXIT_CRITICAL(&mux);
  return s;
}

void drawListeningFrame() {
  char text[24];
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("LISTENING", 4, 2, 4);
  for (int b = 0; b < 2; b++) {
    tft.setTextColor(METER_COLOR[b], TFT_BLACK);
    snprintf(text, sizeof(text), "%.1fk", activeCfg.centerHz[b] / 1000);
    tft.drawString(text, 4, METER_Y[b] + 2, 2);
    tft.drawRect(METER_X, METER_Y[b], METER_W, METER_H, TFT_DARKGREY);
  }
}

void drawStatus(const char *status, uint16_t color) {
  tft.setTextDatum(TR_DATUM);
  tft.setTextColor(color, TFT_BLACK);
  tft.setTextPadding(70);
  tft.drawString(status, tft.width() - 4, 8, 2);
  tft.setTextPadding(0);
}

void drawMeters(const Shared &s) {
  char text[40];
  const bardet::Levels &lv = s.levels;
  int tick = (int)(activeCfg.thresholdDb / METER_MAX_DB * (METER_W - 2));
  for (int b = 0; b < 2; b++) {
    float snr = lv.learning ? 0 : lv.snrDb[b];
    int w = (int)(constrain(snr, 0.0f, METER_MAX_DB) / METER_MAX_DB * (METER_W - 2));
    bool above = !lv.learning && snr >= activeCfg.thresholdDb;
    tft.fillRect(METER_X + 1, METER_Y[b] + 1, w, METER_H - 2, above ? TFT_GREEN : METER_COLOR[b]);
    tft.fillRect(METER_X + 1 + w, METER_Y[b] + 1, METER_W - 2 - w, METER_H - 2, TFT_BLACK);
    tft.drawFastVLine(METER_X + 1 + tick, METER_Y[b] + 1, METER_H - 2, TFT_WHITE);
    tft.setTextDatum(TR_DATUM);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);
    tft.setTextPadding(40);
    snprintf(text, sizeof(text), lv.learning ? "--" : "%.0fdB", snr);
    tft.drawString(text, tft.width() - 4, METER_Y[b] + 2, 2);
  }
  tft.setTextPadding(0);

  if (lv.learning) {
    drawStatus("LEARN", TFT_YELLOW);
  } else if (millis() - lastClipMs < CLIP_SHOW_MS) {
    drawStatus("CLIP", TFT_ORANGE);
  } else {
    drawStatus("READY", TFT_GREEN);
  }

  tft.setTextDatum(TL_DATUM);
  tft.setTextColor(TFT_LIGHTGREY, TFT_BLACK);
  tft.setTextPadding(tft.width() - 8);
  snprintf(text, sizeof(text), "hits 38k:%lu 47k:%lu ?:%lu", (unsigned long)hits[0], (unsigned long)hits[1],
           (unsigned long)hits[2]);
  tft.drawString(text, 4, 94, 2);
  float vpp = s.maxCode >= s.minCode ? (float)codeToMv(s.maxCode) - (float)codeToMv(s.minCode) : 0;
  snprintf(text, sizeof(text), "DC %.2fV Vpp %.0fmV %.0fk", codeToMv(s.dcCode) / 1000.0f, vpp, rateMeas / 1000);
  tft.drawString(text, 4, 114, 2);
  tft.setTextPadding(0);
}

void drawTile(const Event &e) {
  char line[48];
  int idx = e.band == Band::A ? 0 : (e.band == Band::B ? 1 : -1);
  uint16_t bg = idx >= 0 ? BAND_COLOR[idx] : TFT_DARKGREY;
  uint16_t fg = idx >= 0 ? BAND_TEXT[idx] : TFT_WHITE;
  tft.fillScreen(bg);
  tft.setTextColor(fg, bg);
  tft.setTextDatum(TL_DATUM);
  if (idx >= 0) {
    // Font 8 (75 px digits) for the name, font 4 for the unit, on a shared baseline.
    int nameW = tft.textWidth(BAND_NAME[idx], 8);
    int x = (tft.width() - (nameW + 6 + tft.textWidth("kHz", 4))) / 2;
    tft.drawString(BAND_NAME[idx], x, 4, 8);
    tft.drawString("kHz", x + nameW + 6, 4 + 75 - 26, 4);
  } else {
    tft.setTextDatum(TC_DATUM);
    tft.setTextSize(2);
    tft.drawString("38+47?", tft.width() / 2, 16, 4);
    tft.setTextSize(1);
  }

  tft.setTextDatum(TC_DATUM);
  if (idx >= 0) {
    snprintf(line, sizeof(line), "%.3f kHz", e.freqHz[idx] / 1000);
  } else {
    snprintf(line, sizeof(line), "%.2f + %.2f kHz", e.freqHz[0] / 1000, e.freqHz[1] / 1000);
  }
  tft.drawString(line, tft.width() / 2, 84, 4);

  int counter = idx >= 0 ? idx : 2;
  float peak = idx >= 0 ? e.peakSnrDb[idx] : max(e.peakSnrDb[0], e.peakSnrDb[1]);
  if (e.phase == Phase::End) {
    snprintf(line, sizeof(line), "+%.0f dB  ring %.0f ms  #%lu%s", peak, e.durationMs, (unsigned long)hits[counter],
             e.clipped ? "  CLIP" : "");
  } else {
    snprintf(line, sizeof(line), "+%.0f dB  ringing...  #%lu%s", peak, (unsigned long)hits[counter],
             e.clipped ? "  CLIP" : "");
  }
  tft.drawString(line, tft.width() / 2, 114, 2);
}

// ---------- events and serial (core 1) ----------

void printEvent(const Event &e) {
  Serial.printf("EVT phase=%s band=%s f_a=%.1f f_b=%.1f snr_a=%.1f snr_b=%.1f margin_db=%.1f dur_ms=%.0f clip=%d",
                e.phase == Phase::Detect ? "detect" : "end", bardet::bandName(e.band), e.freqHz[0], e.freqHz[1],
                e.peakSnrDb[0], e.peakSnrDb[1], e.marginDb, e.durationMs, e.clipped);
  if (e.phase == Phase::End) Serial.printf(" confirmed=%d steady=%d", e.confirmed, e.steady);
  Serial.println();
}

void handleEvent(const Event &e) {
  printEvent(e);
  if (e.phase == Phase::Detect) {
    hits[e.band == Band::A ? 0 : (e.band == Band::B ? 1 : 2)]++;
  } else if (!e.confirmed || e.startSample != tileStartSample) {
    return;  // an onset that never rang down like a bar: serial only
  }
  // Detect shows the tile; the matching End redraws it with the ring time. Each holds it 2 s.
  tileStartSample = e.startSample;
  tileUntil = millis() + TILE_HOLD_MS;
  screen = Screen::Tile;
  drawTile(e);
}

void requestConfigure() {
  portENTER_CRITICAL(&mux);
  pending.cfg = activeCfg;
  pending.configure = true;
  portEXIT_CRITICAL(&mux);
}

void requestReset() {
  portENTER_CRITICAL(&mux);
  pending.reset = true;
  portEXIT_CRITICAL(&mux);
  hits[0] = hits[1] = hits[2] = 0;
  if (screen == Screen::Listening) drawListeningFrame();
}

void printInfo() {
  Shared s = readShared(false, false);
  Serial.printf(
      "INFO rate_meas=%.1f block=%d bin_hz=%.1f band_a=%.1f band_b=%.1f tol_hz=%.0f thr_db=%.1f "
      "hits_a=%lu hits_b=%lu hits_both=%lu dc_mv=%lu bad_words=%lu load_pct=%.0f load_max_pct=%.0f\n",
      rateMeas, activeCfg.blockSize, rateMeas / activeCfg.blockSize, activeCfg.centerHz[0], activeCfg.centerHz[1],
      activeCfg.toleranceHz, activeCfg.thresholdDb, (unsigned long)hits[0], (unsigned long)hits[1],
      (unsigned long)hits[2], (unsigned long)codeToMv(s.dcCode), (unsigned long)s.badWords, s.loadPct, s.loadMaxPct);
}

void handleCommand(char *cmd) {
  if (strcmp(cmd, "info") == 0) {
    printInfo();
  } else if (strncmp(cmd, "lvl ", 4) == 0) {
    lvlStream = strcmp(cmd + 4, "on") == 0;
    Serial.printf("LVL on=%d\n", lvlStream);
  } else if (strncmp(cmd, "thr ", 4) == 0) {
    float db = atof(cmd + 4);
    if (db >= 6 && db <= 40) {
      activeCfg.thresholdDb = db;
      requestConfigure();
    }
    Serial.printf("THR db=%.1f\n", activeCfg.thresholdDb);
  } else if (strncmp(cmd, "band ", 5) == 0 && (cmd[5] == 'a' || cmd[5] == 'b') && cmd[6] == ' ') {
    float hz = atof(cmd + 7);
    float maxHz = rateMeas / 2 - activeCfg.guardOuterHz - activeCfg.toleranceHz;
    if (hz >= 5000 && hz <= maxHz) {
      activeCfg.centerHz[cmd[5] - 'a'] = hz;
      requestConfigure();
      if (screen == Screen::Listening) drawListeningFrame();
    }
    Serial.printf("BAND a=%.1f b=%.1f\n", activeCfg.centerHz[0], activeCfg.centerHz[1]);
  } else if (strcmp(cmd, "reset") == 0) {
    requestReset();
    Serial.println("RESET ok=1");
  } else if (cmd[0]) {
    Serial.printf("ERR unknown command '%s'\n", cmd);
  }
}

void pollSerial() {
  static char buf[48];
  static size_t len = 0;
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      buf[len] = 0;
      handleCommand(buf);
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    }
  }
}

void setup() {
  Serial.begin(921600);
  tft.init();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("Starting ADC...", 4, 4, 2);
  pinMode(PIN_BTN_RESET, INPUT);  // external pull-up on the board

  esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_12, ADC_WIDTH_BIT_12, 1100, &adcChars);
  eventQueue = xQueueCreate(8, sizeof(Event));
  if (!eventQueue || !startAdc()) {
    Serial.println("ERR init failed");
    tft.setTextColor(TFT_RED, TFT_BLACK);
    tft.drawString("INIT FAILED", 4, 24, 4);
    while (true) delay(1000);
  }
  rateMeas = measureRate();
  activeCfg.sampleRateHz = rateMeas;
  detector.configure(activeCfg);
  detector.setCallback(onDetectorEvent, nullptr);
  shared.minCode = 4095;
  shared.maxCode = 0;
  // Core 0 is free (no Wi-Fi). The task blocks in i2s_read between buffers, so the idle task still runs,
  // as long as load_pct stays under 100 %. Above it the task never blocks, IDLE0 starves and the task
  // watchdog resets the board every ~12 s (seen on the first board run, at ~140 %).
  xTaskCreatePinnedToCore(samplingTask, "sampling", 8192, nullptr, 5, nullptr, 0);

  drawListeningFrame();
  printInfo();
  Serial.println("READY");
}

void loop() {
  static uint32_t lastMeter = 0;
  static uint32_t lastLvl = 0;
  static uint32_t lastPress = 0;
  static bool wasPressed = false;

  pollSerial();

  Event e;
  while (xQueueReceive(eventQueue, &e, 0) == pdTRUE) handleEvent(e);

  bool pressed = digitalRead(PIN_BTN_RESET) == LOW;
  if (pressed && !wasPressed && millis() - lastPress > 250) {
    lastPress = millis();
    requestReset();
  }
  wasPressed = pressed;

  if (screen == Screen::Tile && (int32_t)(millis() - tileUntil) >= 0) {
    screen = Screen::Listening;
    drawListeningFrame();
  }

  if (millis() - lastLvl >= LVL_PERIOD_MS) {
    lastLvl = millis();
    Shared s = readShared(false, true);
    if (s.clipSeen) lastClipMs = millis();
    if (lvlStream) {
      Serial.printf(
          "LVL snr_a=%.1f snr_b=%.1f guard_a=%.1f guard_b=%.1f level_a=%.1f level_b=%.1f dc_mv=%lu learning=%d "
          "active=%d clip=%d\n",
          s.levels.snrDb[0], s.levels.snrDb[1], s.levels.guardSnrDb[0], s.levels.guardSnrDb[1], s.levels.levelDb[0],
          s.levels.levelDb[1], (unsigned long)codeToMv(s.dcCode), s.levels.learning, s.levels.active, s.clipSeen);
    }
  }
  if (screen == Screen::Listening && millis() - lastMeter >= METER_PERIOD_MS) {
    lastMeter = millis();
    drawMeters(readShared(true, false));
  }
  delay(1);
}
