// Bar detector, prototype 2: LilyGO T-Display v1.1 with an SPH0641LU4H-1 PDM MEMS mic.
//
// The mic's CLK is on GPIO22 and its DATA on GPIO21 (SELECT to GND: data is valid on the rising
// clock edge, where the I2S receiver samples). I2S0 runs as a standard 32-bit stereo master receiver
// whose BCK is the mic clock, so the DMA delivers the raw 1-bit stream; lib/pdm_decimator turns it
// into 192 kS/s PCM and lib/tone_detector (unchanged from prototype 1) tells the remote's ~38 kHz bar
// from its ~47 kHz bar (seen by its 42.78 kHz line). Settings shared with the tests are in
// lib/device_config. Every DMA buffer is processed, with no gaps, by a task on core 0; the screen and
// the serial protocol run in loop() on core 1.
//
// The mic must not wake from sleep (clock < 250 kHz, e.g. while the ESP32 boots) straight into its
// ultrasonic mode (3.072-4.8 MHz): startMic() clocks it at 2.4 MHz (standard mode) first.
//
// Screen: LISTENING shows one SNR meter per band. A detected strike fills the screen for 2 s with
// the band's colour: blue "38 kHz", orange "47 kHz", or grey "38+47?" when neither band leads by
// decisionMarginDb. The measured frequency, peak SNR, ring time and hit count are shown under it.
// Right button (GPIO35): forget the noise baselines and the counters, learn again (1 s, keep quiet).
//
// Serial protocol (921600 baud, one command per line, replies are "KEY k=v ..." lines):
//   info              -> INFO line: rates, bands, thresholds, counters, mic level, CPU load
//   lvl <on|off>      -> stream LVL lines (~20/s): per-band SNR, guard SNR, level, mic RMS, state
//   thr <dB>          -> detection threshold over the noise baseline (6-40); THR db=..
//   band <a|b> <Hz>   -> move a band centre (relearns); BAND a=.. b=..
//   pdm [swap <0|1>]  -> PDM line: ones density, RMS and peak in dBFS, word order (swap relearns)
//   spec <on|off>     -> after each strike, the strongest lines in 20-90 kHz (lib/strike_spectrum):
//                        a SPEC line, then one SPEC_PEAK line per line, strongest first
//   reset             -> same as the right button; RESET ok=1
// Unsolicited: READY after boot, then EVT lines: phase=detect when a strike is recognised and
// phase=end when its ringing stops (confirmed=0 for onsets that did not ring down like a bar).

#include <Arduino.h>
#include <DeviceConfig.h>
#include <PdmDecimator.h>
#include <StrikeSpectrum.h>
#include <TFT_eSPI.h>
#include <ToneDetector.h>
#include <driver/i2s.h>

#include <vector>

using bardet::Band;
using bardet::Event;
using bardet::Phase;

constexpr int PIN_MIC_CLK = 22;
constexpr int PIN_MIC_DATA = 21;
constexpr i2s_port_t I2S_PORT = I2S_NUM_0;
constexpr int SLOT_BITS = 64;               // BCK cycles per I2S frame: two 32-bit slots
constexpr uint32_t WAKE_FRAME_RATE = 37500;  // x 64 = 2.4 MHz, the mic's standard mode
constexpr int DMA_FRAMES = 384;              // 768 words = 1536 PCM samples = 8 ms, one detector block
constexpr int DMA_WORDS = DMA_FRAMES * 2;
constexpr int DMA_BUF_COUNT = 8;     // 64 ms of slack if the task is late
constexpr float CLIP_LEVEL = 0.89f;  // -1 dBFS: the modulator overloads near 120 dB SPL

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
float rate = 0;      // PCM rate: mic clock / decimation
float rateMeas = 0;  // the same, measured from the DMA at boot
pdm::PdmDecimator decimator;    // owned by the sampling task after setup()
bardet::ToneDetector detector;  // owned by the sampling task after setup()
spec::StrikeSpectrum spectrum;  // owned by the sampling task after setup()
pdm::PdmConfig activeMic;       // owned by loop(); sent to the task through `pending`
bardet::Config activeCfg;       // owned by loop(); sent to the task through `pending`
QueueHandle_t eventQueue;
QueueHandle_t specQueue;
bool specOn = false;  // owned by loop(); sent to the task through `pending`

// Shared between the sampling task (core 0) and loop() (core 1), guarded by mux.
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
struct Shared {
  bardet::Levels levels;
  float rms;         // RMS of the last DMA buffer, DC removed, full-scale units
  float peak;        // largest |sample| since loop() last cleared it
  float ones;        // fraction of one bits in the last DMA buffer: ~0.5 when the mic talks
  bool clipSeen;     // a sample at CLIP_LEVEL or above since loop() last cleared it
  float loadPct;     // task time per DMA buffer / buffer duration (average); above 100 % samples are lost
  float loadMaxPct;  // worst buffer since boot
  float decPct;      // the decimator's share of loadPct
} shared;
struct Pending {
  bool reset;
  bool configure;
  bardet::Config cfg;
  bool configureMic;
  pdm::PdmConfig mic;
  bool specChange;
  bool specOn;
} pending;

enum class Screen { Listening, Tile };
Screen screen = Screen::Listening;
uint32_t tileUntil = 0;
uint64_t tileStartSample = 0;
uint32_t hits[3] = {0, 0, 0};  // A, B, BOTH
uint32_t lastClipMs = 0;
bool lvlStream = false;

float dbfs(float a) { return 20.0f * log10f(a > 1e-7f ? a : 1e-7f); }

// ---------- sampling (core 0) ----------

bool startMic() {
  i2s_config_t cfg = {};
  cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX);
  cfg.sample_rate = WAKE_FRAME_RATE;
  cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_32BIT;
  cfg.channel_format = I2S_CHANNEL_FMT_RIGHT_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_MSB;
  cfg.intr_alloc_flags = 0;
  cfg.dma_buf_count = DMA_BUF_COUNT;
  cfg.dma_buf_len = DMA_FRAMES;
  cfg.use_apll = true;  // exact audio clocks: 48 000 x 64 = 3.072 MHz
  if (i2s_driver_install(I2S_PORT, &cfg, 0, nullptr) != ESP_OK) return false;
  i2s_pin_config_t pins = {};
  pins.mck_io_num = I2S_PIN_NO_CHANGE;  // left at 0, GPIO0 (the BOOT button) would output MCLK
  pins.bck_io_num = PIN_MIC_CLK;
  pins.ws_io_num = I2S_PIN_NO_CHANGE;  // the mic has no word select
  pins.data_out_num = I2S_PIN_NO_CHANGE;
  pins.data_in_num = PIN_MIC_DATA;
  if (i2s_set_pin(I2S_PORT, &pins) != ESP_OK) return false;
  delay(100);  // sleep -> standard mode: <= 15 ms
  uint32_t frameRate = (uint32_t)(activeMic.pdmClockHz / SLOT_BITS + 0.5f);
  if (i2s_set_clk(I2S_PORT, frameRate, I2S_BITS_PER_SAMPLE_32BIT, I2S_CHANNEL_STEREO) != ESP_OK) return false;
  delay(50);  // standard -> ultrasonic mode: <= 10 ms
  return true;
}

void readWords(uint32_t *dst) {
  size_t got = 0;
  i2s_read(I2S_PORT, dst, DMA_WORDS * sizeof(uint32_t), &got, portMAX_DELAY);
}

// PCM samples delivered per second of CPU time: a check of the I2S clock (the detector uses the
// nominal rate, which the APLL makes exact).
float measureRate() {
  static uint32_t scratch[DMA_WORDS];
  for (int i = 0; i < DMA_BUF_COUNT + 1; i++) readWords(scratch);  // drop what queued up
  const int chunks = 64;
  uint32_t t0 = micros();
  for (int i = 0; i < chunks; i++) readWords(scratch);
  uint32_t dt = micros() - t0;
  return chunks * (float)DMA_WORDS * 32 / activeMic.decimation * 1e6f / dt;
}

void onDetectorEvent(const Event &e, void *) { xQueueSend(eventQueue, &e, 0); }

void samplingTask(void *) {
  static uint32_t words[DMA_WORDS];
  std::vector<float> pcm(decimator.outputsFor(DMA_WORDS));
  const float bufferUs = DMA_WORDS * 32 * 1e6f / activeMic.pdmClockHz;
  bool specActive = false;
  for (;;) {
    bool doReset, doConfigure, doMic, doSpec;
    bardet::Config cfg;
    pdm::PdmConfig mic;
    portENTER_CRITICAL(&mux);
    doReset = pending.reset;
    doConfigure = pending.configure;
    doMic = pending.configureMic;
    if (doConfigure) cfg = pending.cfg;
    if (doMic) mic = pending.mic;
    doSpec = pending.specChange;
    if (doSpec) specActive = pending.specOn;
    pending.reset = pending.configure = pending.configureMic = pending.specChange = false;
    portEXIT_CRITICAL(&mux);
    if (doSpec && specActive) spectrum.reset();
    if (doMic) decimator.configure(mic);
    if (doConfigure) {
      detector.configure(cfg);
    } else if (doReset || doMic) {
      detector.reset();
    }

    readWords(words);
    uint32_t t0 = micros();
    size_t n = decimator.push(words, DMA_WORDS, pcm.data());
    uint32_t t1 = micros();
    float sum = 0, sumSq = 0, peak = 0;
    for (size_t i = 0; i < n; i++) {
      float v = pcm[i];
      sum += v;
      sumSq += v * v;
      peak = max(peak, fabsf(v));
    }
    bool clipped = peak >= CLIP_LEVEL;
    detector.push(pcm.data(), n, clipped);
    // A capture's two FFTs take a few ms in one buffer: the DMA queue (64 ms) absorbs it.
    if (specActive && spectrum.push(pcm.data(), n)) xQueueSend(specQueue, &spectrum.result(), 0);
    uint32_t t2 = micros();
    float mean = sum / n;
    float load = (t2 - t0) * 100.0f / bufferUs;
    float dec = (t1 - t0) * 100.0f / bufferUs;

    portENTER_CRITICAL(&mux);
    shared.loadPct += 0.01f * (load - shared.loadPct);
    shared.decPct += 0.01f * (dec - shared.decPct);
    shared.loadMaxPct = max(shared.loadMaxPct, load);
    shared.levels = detector.levels();
    shared.rms = sqrtf(max(sumSq / n - mean * mean, 0.0f));
    shared.peak = max(shared.peak, peak);
    shared.ones = decimator.onesDensity();
    shared.clipSeen = shared.clipSeen || clipped;
    portEXIT_CRITICAL(&mux);
  }
}

// ---------- display (core 1) ----------

Shared readShared(bool clearPeak, bool clearClip) {
  portENTER_CRITICAL(&mux);
  Shared s = shared;
  if (clearPeak) shared.peak = 0;
  if (clearClip) shared.clipSeen = false;
  portEXIT_CRITICAL(&mux);
  return s;
}

// The data line stuck at one level: no clock reaching the mic, no supply, or DATA not connected.
bool micSilent(const Shared &s) { return s.ones < 0.02f || s.ones > 0.98f; }

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

  if (micSilent(s)) {
    drawStatus("NO MIC", TFT_RED);
  } else if (lv.learning) {
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
  snprintf(text, sizeof(text), "rms %.0f pk %.0f dBFS %.0fk", dbfs(s.rms), dbfs(s.peak), rate / 1000);
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

void requestMic() {
  portENTER_CRITICAL(&mux);
  pending.mic = activeMic;
  pending.configureMic = true;
  portEXIT_CRITICAL(&mux);
}

void requestSpec() {
  portENTER_CRITICAL(&mux);
  pending.specOn = specOn;
  pending.specChange = true;
  portEXIT_CRITICAL(&mux);
}

void printSpec(const spec::SpecResult &r) {
  Serial.printf("SPEC t=%.3f trig_db=%.1f floor_dbfs=%.1f peaks=%d\n", r.triggerSample / rate, r.triggerDb,
                r.floorDbfs, r.count);
  for (int p = 0; p < r.count; p++) {
    const spec::SpecPeak &k = r.peaks[p];
    Serial.printf("SPEC_PEAK f=%.1f early_dbfs=%.1f late_dbfs=%.1f drop_db=%.1f\n", k.freqHz, k.earlyDbfs, k.lateDbfs,
                  k.earlyDbfs - k.lateDbfs);
  }
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
      "INFO rate=%.1f rate_meas=%.1f pdm_clk=%.0f decim=%d swap=%d block=%d bin_hz=%.1f band_a=%.1f band_b=%.1f "
      "tol_hz=%.0f thr_db=%.1f hits_a=%lu hits_b=%lu hits_both=%lu ones=%.3f rms_dbfs=%.1f peak_dbfs=%.1f "
      "load_pct=%.0f load_max_pct=%.0f dec_pct=%.0f spec=%d heap_free=%lu\n",
      rate, rateMeas, activeMic.pdmClockHz, activeMic.decimation, activeMic.swapWords, activeCfg.blockSize,
      rate / activeCfg.blockSize, activeCfg.centerHz[0], activeCfg.centerHz[1], activeCfg.toleranceHz,
      activeCfg.thresholdDb, (unsigned long)hits[0], (unsigned long)hits[1], (unsigned long)hits[2], s.ones,
      dbfs(s.rms), dbfs(s.peak), s.loadPct, s.loadMaxPct, s.decPct, specOn, (unsigned long)ESP.getFreeHeap());
}

void printPdm() {
  Shared s = readShared(true, false);
  Serial.printf("PDM ones=%.3f rms_dbfs=%.1f peak_dbfs=%.1f swap=%d\n", s.ones, dbfs(s.rms), dbfs(s.peak),
                activeMic.swapWords);
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
    float maxHz = rate / 2 - activeCfg.guardOuterHz - activeCfg.toleranceHz;
    if (hz >= 5000 && hz <= maxHz) {
      activeCfg.centerHz[cmd[5] - 'a'] = hz;
      requestConfigure();
      if (screen == Screen::Listening) drawListeningFrame();
    }
    Serial.printf("BAND a=%.1f b=%.1f\n", activeCfg.centerHz[0], activeCfg.centerHz[1]);
  } else if (strcmp(cmd, "pdm") == 0) {
    printPdm();
  } else if (strncmp(cmd, "pdm swap ", 9) == 0 && (cmd[9] == '0' || cmd[9] == '1') && !cmd[10]) {
    activeMic.swapWords = cmd[9] == '1';
    requestMic();
    printPdm();  // still the old order: ask again after a few buffers
  } else if (strncmp(cmd, "spec ", 5) == 0) {
    specOn = strcmp(cmd + 5, "on") == 0;
    requestSpec();
    Serial.printf("SPEC on=%d\n", specOn);
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
  tft.drawString("Waking the mic...", 4, 4, 2);
  pinMode(PIN_BTN_RESET, INPUT);  // external pull-up on the board

  activeMic = v2::micConfig();
  rate = activeMic.pdmClockHz / activeMic.decimation;
  activeCfg = v2::detectorConfig(rate);
  decimator.configure(activeMic);
  spec::SpecConfig specCfg;
  specCfg.sampleRateHz = rate;
  spectrum.configure(specCfg);
  eventQueue = xQueueCreate(8, sizeof(Event));
  specQueue = xQueueCreate(4, sizeof(spec::SpecResult));
  if (!eventQueue || !specQueue || !startMic()) {
    Serial.println("ERR init failed");
    tft.setTextColor(TFT_RED, TFT_BLACK);
    tft.drawString("INIT FAILED", 4, 24, 4);
    while (true) delay(1000);
  }
  rateMeas = measureRate();
  detector.configure(activeCfg);
  detector.setCallback(onDetectorEvent, nullptr);
  // Core 0 is free (no Wi-Fi). The task blocks in i2s_read between buffers, so the idle task still runs,
  // as long as load_pct stays under 100 %. Above it the task never blocks, IDLE0 starves and the task
  // watchdog resets the board every ~12 s (seen with prototype 1's first build, at ~140 %).
  xTaskCreatePinnedToCore(samplingTask, "sampling", 8192, nullptr, 5, nullptr, 0);

  drawListeningFrame();
  delay(50);  // a few buffers, so INFO shows the mic level
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
  spec::SpecResult r;
  while (xQueueReceive(specQueue, &r, 0) == pdTRUE) printSpec(r);

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
          "LVL snr_a=%.1f snr_b=%.1f guard_a=%.1f guard_b=%.1f level_a=%.1f level_b=%.1f rms_dbfs=%.1f learning=%d "
          "active=%d clip=%d\n",
          s.levels.snrDb[0], s.levels.snrDb[1], s.levels.guardSnrDb[0], s.levels.guardSnrDb[1], s.levels.levelDb[0],
          s.levels.levelDb[1], dbfs(s.rms), s.levels.learning, s.levels.active, s.clipSeen);
    }
  }
  if (screen == Screen::Listening && millis() - lastMeter >= METER_PERIOD_MS) {
    lastMeter = millis();
    drawMeters(readShared(true, false));
  }
  delay(1);
}
