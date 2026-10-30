// Bar detector, prototype 4: the remote's bars drive a TV. LilyGO T-Display v1.1 with an SPH0641LU4H-1
// PDM MEMS mic (prototype 2's hardware), with prototype 3's detection unchanged.
//
// The mic's CLK is on GPIO22 and its DATA on GPIO21 (SELECT to GND: data is valid on the rising
// clock edge, where the I2S receiver samples). I2S0 runs as a standard 32-bit stereo master receiver
// whose BCK is the mic clock, so the DMA delivers the raw 1-bit stream; lib/pdm_decimator turns it
// into 192 kS/s PCM and lib/tone_detector (unchanged from prototype 1) tells the remote's ~38 kHz bar
// from its ~47 kHz bar. lib/strike_spectrum runs on every buffer too, and lib/bar_decision settles the
// detector's BOTH with each bar's overtone. Settings shared with the tests are in lib/device_config.
// Every DMA buffer is processed, with no gaps, by a task on core 0; the screen and the serial protocol
// run in loop() on core 1.
//
// The mic must not wake from sleep (clock < 250 kHz, e.g. while the ESP32 boots) straight into its
// ultrasonic mode (3.072-4.8 MHz): startMic() clocks it at 2.4 MHz (standard mode) first.
//
// The TV (lib/tv_remote turns decided strikes into commands, src/TvScreen draws):
//   - 38 kHz bar: power on / off (CRT-style), standby shows a red LED;
//   - 47 kHz bar: next of 5 channels (TEST CARD, SPACE, PLASMA, FIREPLACE, ULTRASONIC), through static;
//   - both bars at once: an explosion.
// Each strike acts 300 ms after it is decided (500 ms if weak), so that a second bar struck with it
// turns it into an explosion and a weak stray strike can be replaced (see TvRemote.h).
// Buttons: left (GPIO0) = power, held 1 s = explosion; right (GPIO35) = channel, held 1 s = forget
// the noise baselines and learn again (1 s, keep quiet).
//
// Serial protocol (921600 baud, one command per line, replies are "KEY k=v ..." lines):
//   info              -> INFO line: rates, bands, thresholds, counters, mic level, CPU load, TV state
//   lvl <on|off>      -> stream LVL lines (~20/s): per-band SNR, guard SNR, level, mic RMS, state
//   thr <dB>          -> detection threshold over the noise baseline (6-40); THR db=..
//   band <a|b> <Hz>   -> move a band centre (relearns); BAND a=.. b=..
//   pdm [swap <0|1>]  -> PDM line: ones density, RMS and peak in dBFS, word order (swap relearns)
//   spec <on|off>     -> print, after each strike, the strongest lines in 20-62 kHz (lib/strike_spectrum):
//                        a SPEC line, one SPEC_PEAK line per line (strongest first), then one SPEC_LINE
//                        per watched line
//   spec now          -> a capture without a click (background survey); printed even with spec off
//   watch             -> WATCH line: the watched lines, name=Hz:tol
//   watch add <Hz> [tol] / watch clear / watch default -> change the watched lines (WATCH line)
//   tv [power|ch|boom] -> the TV state (TV on=.. ch=.. name=..), after the command if one is given
//   shot [y0 y1]      -> the screen as RGB565 hex rows: SHOT ..., SHOT_ROW y hex..., SHOT end (~3.4 s)
//   reset             -> forget the noise baselines and the counters; RESET ok=1
// Unsolicited: READY after boot; EVT lines: phase=detect when a strike is recognised and phase=end
// when its ringing stops (confirmed=0 for onsets that did not ring down like a bar); one DEC line per
// strike (the decision, lib/bar_decision); one TV line per command (TV cmd=.. on=.. ch=.. peaks=..).

#include <Arduino.h>
#include <BarDecision.h>
#include <DeviceConfig.h>
#include <PdmDecimator.h>
#include <StrikeSpectrum.h>
#include <TFT_eSPI.h>
#include <ToneDetector.h>
#include <TvRemote.h>
#include <driver/i2s.h>
#include <esp_heap_caps.h>

#include "TvScreen.h"

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

constexpr int PIN_BTN_POWER = 0;    // left button (BOOT), LOW when pressed
constexpr int PIN_BTN_CHANNEL = 35; // right button, external pull-up on the board
constexpr uint32_t BTN_HOLD_MS = 1000;
constexpr uint32_t LVL_PERIOD_MS = 50;

TFT_eSPI tft;
tvscreen::Screen screen(tft);  // owned by loop()
tv::Remote remote;             // owned by loop()
tv::TvState tvState;           // owned by loop()
float rate = 0;      // PCM rate: mic clock / decimation
float rateMeas = 0;  // the same, measured from the DMA at boot
pdm::PdmDecimator decimator;    // owned by the sampling task after setup()
bardet::ToneDetector detector;  // owned by the sampling task after setup()
spec::StrikeSpectrum spectrum;  // owned by the sampling task after setup()
bardec::BarDecision decision;   // owned by loop()
pdm::PdmConfig activeMic;       // owned by loop(); sent to the task through `pending`
bardet::Config activeCfg;       // owned by loop(); sent to the task through `pending`
QueueHandle_t eventQueue;
QueueHandle_t specQueue;
bool specOn = false;    // owned by loop(): print the SPEC lines of click-triggered captures
struct ActiveLine {
  char name[8];
  spec::WatchLine line;
};
ActiveLine activeWatch[spec::kMaxWatch];  // owned by loop(); sent to the task through `pending`
int activeWatchCount = 0;

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
  // The clean peak SNR of each band since the detector's event under way opened (kept after it closes,
  // until the next one opens): a second bar struck with the first rings clean in its own band.
  float strikePeak[2];
} shared;
struct Pending {
  bool reset;
  bool configure;
  bardet::Config cfg;
  bool configureMic;
  pdm::PdmConfig mic;
  bool forceSpec;
  bool watchChange;
  int watchCount;
  spec::WatchLine watch[spec::kMaxWatch];
} pending;

uint32_t waitSinceMs = 0;    // when the Detect waiting in `decision` arrived
constexpr uint32_t DECISION_WAIT_MS = 200;  // its capture is ready ~80 ms after the click
uint32_t hits[3] = {0, 0, 0};  // decided strikes: A, B, BOTH
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
  static spec::WatchLine watch[spec::kMaxWatch];
  bool wasAbove = false;  // a band was over the detection threshold in the previous buffer
  bool wasActive = false;
  float strikePeak[2] = {0, 0};
  for (;;) {
    bool doReset, doConfigure, doMic, doForce, doWatch;
    int watchCount = 0;
    bardet::Config cfg;
    pdm::PdmConfig mic;
    portENTER_CRITICAL(&mux);
    doReset = pending.reset;
    doConfigure = pending.configure;
    doMic = pending.configureMic;
    if (doConfigure) cfg = pending.cfg;
    if (doMic) mic = pending.mic;
    doForce = pending.forceSpec;
    doWatch = pending.watchChange;
    if (doWatch) {
      watchCount = pending.watchCount;
      for (int i = 0; i < watchCount; i++) watch[i] = pending.watch[i];
    }
    pending.reset = pending.configure = pending.configureMic = pending.forceSpec = pending.watchChange = false;
    portEXIT_CRITICAL(&mux);
    if (doWatch) spectrum.setWatch(watch, watchCount);
    if (doForce) spectrum.forceCapture();
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
    // A band rising over the detection threshold in this buffer also starts a strike capture with this
    // buffer: soft clicks barely raise the spectrum's own (second-difference) trigger.
    const bardet::Levels &lv = detector.levels();
    bool above = !lv.learning && max(lv.snrDb[0], lv.snrDb[1]) >= detector.config().thresholdDb;
    if (above && !wasAbove) spectrum.triggerNext();
    wasAbove = above;
    // Each band's clean peak SNR during the detector's event (clean as the detector counts it: the band
    // beats its guard bins by guardMarginDb).
    if (lv.active && !wasActive) strikePeak[0] = strikePeak[1] = 0;
    wasActive = lv.active;
    if (lv.active) {
      for (int b = 0; b < 2; b++)
        if (lv.snrDb[b] - lv.guardSnrDb[b] >= detector.config().guardMarginDb && lv.snrDb[b] > strikePeak[b])
          strikePeak[b] = lv.snrDb[b];
    }
    // A capture's two FFTs take a few ms in one buffer: the DMA queue (64 ms) absorbs it.
    if (spectrum.push(pcm.data(), n)) xQueueSend(specQueue, &spectrum.result(), 0);
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
    shared.strikePeak[0] = strikePeak[0];
    shared.strikePeak[1] = strikePeak[1];
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

// ---------- events and serial (core 1) ----------

void printEvent(const Event &e) {
  Serial.printf(
      "EVT phase=%s band=%s t=%.3f f_a=%.1f f_b=%.1f snr_a=%.1f snr_b=%.1f margin_db=%.1f dur_ms=%.0f clip=%d",
      e.phase == Phase::Detect ? "detect" : "end", bardet::bandName(e.band), e.startSample / rate, e.freqHz[0],
      e.freqHz[1], e.peakSnrDb[0], e.peakSnrDb[1], e.marginDb, e.durationMs, e.clipped);
  if (e.phase == Phase::End) Serial.printf(" confirmed=%d steady=%d", e.confirmed, e.steady);
  Serial.println();
}

void printDecision(const bardec::Decision &d) {
  Serial.printf(
      "DEC t=%.3f band=%s detector=%s why=%s spec=%d ev_a=%.1f ev_b=%.1f margin_db=%.1f fund_a=%d fund_b=%d ov_a=%d "
      "ov_b=%d ratio_a=%.5f ratio_b=%.5f\n",
      d.event.startSample / rate, bardet::bandName(d.band), bardet::bandName(d.detectorBand),
      bardec::reasonName(d.reason), d.spectrum, d.bar[0].lateDbfs, d.bar[1].lateDbfs, d.marginDb, d.bar[0].fundamental,
      d.bar[1].fundamental, d.bar[0].overtone, d.bar[1].overtone, d.bar[0].ratio, d.bar[1].ratio);
}

// A strike is decided (the detector's band, settled with the bars' lines if it said BOTH): count it and
// hand it to the remote, which turns it into a command once its hold is over.
void handleDecision(const bardec::Decision &d) {
  printDecision(d);
  hits[d.band == Band::A ? 0 : (d.band == Band::B ? 1 : 2)]++;
  screen.markStrike(d.band == Band::A ? 0 : (d.band == Band::B ? 1 : 2));
  remote.onDecision(d, millis());
}

void handleEvent(const Event &e) {
  printEvent(e);
  bardec::Decision out[2];
  if (e.phase == Phase::Detect) {
    int n = decision.onDetect(e, out);
    for (int i = 0; i < n; i++) handleDecision(out[i]);
    if (decision.waiting()) waitSinceMs = millis();
    return;
  }
  // End: a strike whose capture is still overdue is decided without it first.
  if (decision.waiting() && decision.expire(out[0])) handleDecision(out[0]);
}

// A TV command (from the bars, the buttons or the serial port): update the state and animate.
void runCommand(tv::Command c, const float *peaks) {
  tv::TvState before = tvState;
  tvState = tv::apply(tvState, c, tvscreen::kChannels);
  uint32_t now = millis();
  if (c == tv::Command::Boom) {
    screen.boom(now);
  } else if (tvState.on != before.on) {
    if (tvState.on) {
      screen.powerOn(tvState.channel, now);
    } else {
      screen.powerOff(now);
    }
  } else if (tvState.channel != before.channel) {
    screen.changeChannel(tvState.channel, now);
  }
  Serial.printf("TV cmd=%s on=%d ch=%d name=%s peaks=%.1f/%.1f\n", tv::commandName(c), tvState.on,
                tvState.channel + 1, screen.channelName(tvState.channel), peaks ? peaks[0] : 0.0f,
                peaks ? peaks[1] : 0.0f);
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

void requestForceSpec() {
  portENTER_CRITICAL(&mux);
  pending.forceSpec = true;
  portEXIT_CRITICAL(&mux);
}

void requestWatch() {
  portENTER_CRITICAL(&mux);
  pending.watchCount = activeWatchCount;
  for (int i = 0; i < activeWatchCount; i++) pending.watch[i] = activeWatch[i].line;
  pending.watchChange = true;
  portEXIT_CRITICAL(&mux);
}

void setDefaultWatch() {
  activeWatchCount = 0;
  for (int i = 0; i < v3::kWatchCount && i < spec::kMaxWatch; i++, activeWatchCount++) {
    snprintf(activeWatch[i].name, sizeof(activeWatch[i].name), "%s", v3::kWatch[i].name);
    activeWatch[i].line = v3::kWatch[i].line;
  }
}

void printWatch() {
  Serial.printf("WATCH n=%d", activeWatchCount);
  for (int i = 0; i < activeWatchCount; i++)
    Serial.printf(" %s=%.0f:%.0f", activeWatch[i].name, activeWatch[i].line.hz, activeWatch[i].line.tolHz);
  Serial.println();
}

// The name of the watched line at index i, if the list still has that target (it may have changed
// since the capture).
const char *watchName(int i, float targetHz) {
  return i < activeWatchCount && activeWatch[i].line.hz == targetHz ? activeWatch[i].name : "?";
}

void printSpec(const spec::SpecResult &r) {
  Serial.printf("SPEC t=%.3f trig_db=%.1f forced=%d ext=%d floor_dbfs=%.1f peaks=%d lines=%d\n",
                r.triggerSample / rate, r.triggerDb, r.forced, r.external, r.floorDbfs, r.count, r.lineCount);
  for (int p = 0; p < r.count; p++) {
    const spec::SpecPeak &k = r.peaks[p];
    Serial.printf("SPEC_PEAK f=%.1f early_dbfs=%.1f late_dbfs=%.1f drop_db=%.1f\n", k.freqHz, k.earlyDbfs, k.lateDbfs,
                  k.earlyDbfs - k.lateDbfs);
  }
  for (int l = 0; l < r.lineCount; l++) {
    const spec::SpecLine &k = r.lines[l];
    Serial.printf(
        "SPEC_LINE name=%s target=%.0f f=%.1f early_dbfs=%.1f late_dbfs=%.1f drop_db=%.1f before_dbfs=%.1f\n",
        watchName(l, k.targetHz), k.targetHz, k.freqHz, k.earlyDbfs, k.lateDbfs, k.earlyDbfs - k.lateDbfs,
        k.beforeDbfs);
  }
}

void requestReset() {
  portENTER_CRITICAL(&mux);
  pending.reset = true;
  portEXIT_CRITICAL(&mux);
  hits[0] = hits[1] = hits[2] = 0;
}

void printInfo() {
  Shared s = readShared(false, false);
  Serial.printf(
      "INFO rate=%.1f rate_meas=%.1f pdm_clk=%.0f decim=%d swap=%d block=%d bin_hz=%.1f band_a=%.1f band_b=%.1f "
      "tol_hz=%.0f thr_db=%.1f hits_a=%lu hits_b=%lu hits_both=%lu ones=%.3f rms_dbfs=%.1f peak_dbfs=%.1f "
      "load_pct=%.0f load_max_pct=%.0f dec_pct=%.0f spec=%d watch=%d heap_free=%lu heap_max_block=%lu tv_on=%d "
      "tv_ch=%d\n",
      rate, rateMeas, activeMic.pdmClockHz, activeMic.decimation, activeMic.swapWords, activeCfg.blockSize,
      rate / activeCfg.blockSize, activeCfg.centerHz[0], activeCfg.centerHz[1], activeCfg.toleranceHz,
      activeCfg.thresholdDb, (unsigned long)hits[0], (unsigned long)hits[1], (unsigned long)hits[2], s.ones,
      dbfs(s.rms), dbfs(s.peak), s.loadPct, s.loadMaxPct, s.decPct, specOn, activeWatchCount,
      (unsigned long)ESP.getFreeHeap(), (unsigned long)ESP.getMaxAllocHeap(), tvState.on, tvState.channel + 1);
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
    }
    Serial.printf("BAND a=%.1f b=%.1f\n", activeCfg.centerHz[0], activeCfg.centerHz[1]);
  } else if (strcmp(cmd, "pdm") == 0) {
    printPdm();
  } else if (strncmp(cmd, "pdm swap ", 9) == 0 && (cmd[9] == '0' || cmd[9] == '1') && !cmd[10]) {
    activeMic.swapWords = cmd[9] == '1';
    requestMic();
    printPdm();  // still the old order: ask again after a few buffers
  } else if (strcmp(cmd, "spec now") == 0) {
    requestForceSpec();
    Serial.println("SPEC now=1");
  } else if (strncmp(cmd, "spec ", 5) == 0) {
    specOn = strcmp(cmd + 5, "on") == 0;
    Serial.printf("SPEC on=%d\n", specOn);
  } else if (strcmp(cmd, "watch") == 0) {
    printWatch();
  } else if (strcmp(cmd, "watch default") == 0) {
    setDefaultWatch();
    requestWatch();
    printWatch();
  } else if (strcmp(cmd, "watch clear") == 0) {
    activeWatchCount = 0;
    requestWatch();
    printWatch();
  } else if (strncmp(cmd, "watch add ", 10) == 0) {
    char *end;
    float hz = strtof(cmd + 10, &end);
    float tol = strtof(end, nullptr);
    if (tol <= 0) tol = 300;
    if (hz >= 1000 && hz < rate / 2 && activeWatchCount < spec::kMaxWatch) {
      ActiveLine &a = activeWatch[activeWatchCount];
      snprintf(a.name, sizeof(a.name), "x%.0f", hz / 100);  // e.g. x475 for 47 500 Hz
      a.line.hz = hz;
      a.line.tolHz = tol;
      activeWatchCount++;
      requestWatch();
    }
    printWatch();
  } else if (strncmp(cmd, "tv", 2) == 0 && (!cmd[2] || cmd[2] == ' ')) {
    const char *arg = cmd[2] ? cmd + 3 : "";
    if (strcmp(arg, "power") == 0) {
      runCommand(tv::Command::Power, nullptr);
    } else if (strcmp(arg, "ch") == 0) {
      runCommand(tv::Command::Channel, nullptr);
    } else if (strcmp(arg, "boom") == 0) {
      runCommand(tv::Command::Boom, nullptr);
    } else {
      Serial.printf("TV on=%d ch=%d name=%s\n", tvState.on, tvState.channel + 1, screen.channelName(tvState.channel));
    }
  } else if (strncmp(cmd, "shot", 4) == 0 && (!cmd[4] || cmd[4] == ' ')) {
    int y0 = 0, y1 = 134;
    if (cmd[4]) sscanf(cmd + 5, "%d %d", &y0, &y1);
    screen.dump(y0, y1);
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

void printHeap(const char *stage) {
  Serial.printf("HEAP stage=%s free=%u max=%u dma_free=%u dma_max=%u\n", stage,
                heap_caps_get_free_size(MALLOC_CAP_8BIT), heap_caps_get_largest_free_block(MALLOC_CAP_8BIT),
                heap_caps_get_free_size(MALLOC_CAP_DMA), heap_caps_get_largest_free_block(MALLOC_CAP_DMA));
}

void setup() {
  Serial.begin(921600);
  tft.init();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  pinMode(PIN_BTN_POWER, INPUT_PULLUP);
  pinMode(PIN_BTN_CHANNEL, INPUT);  // external pull-up on the board

  activeMic = v3::micConfig();
  rate = activeMic.pdmClockHz / activeMic.decimation;
  activeCfg = v3::detectorConfig(rate);
  decimator.configure(activeMic);
  eventQueue = xQueueCreate(8, sizeof(Event));
  specQueue = xQueueCreate(4, sizeof(spec::SpecResult));
  // Allocation order matters: the I2S DMA buffers need DMA-capable RAM, the screen's sprite 64.8 KB in
  // one piece, and the strike spectrum ~105 KB in smaller pieces. The sprite before the mic took the
  // DMA buffers' RAM ("I2S0 rx DMA buffer malloc failed").
  printHeap("boot");
  bool micOk = eventQueue && specQueue && startMic();
  printHeap("mic");
  bool screenOk = micOk && screen.begin();
  printHeap("screen");
  Serial.flush();
  setDefaultWatch();
  spectrum.configure(v3::specConfig(rate));
  printHeap("spectrum");
  decision.configure(v3::decisionConfig(rate));
  if (!micOk || !screenOk) {
    Serial.printf("ERR init failed mic=%d screen=%d heap_max_block=%lu\n", micOk, screenOk,
                  (unsigned long)ESP.getMaxAllocHeap());
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

  screen.splash();  // ~2 s, during which the detector learns the room's noise
  printInfo();
  Serial.println("READY");
}

// A button: a press acts on release (short) or after BTN_HOLD_MS (long), once.
struct Button {
  explicit Button(int p) : pin(p) {}
  int pin;
  bool down = false;
  bool longDone = false;
  uint32_t since = 0;
  // 1 = short press released, 2 = held long, 0 = nothing.
  int poll(uint32_t now) {
    bool pressed = digitalRead(pin) == LOW;
    int result = 0;
    if (pressed && !down) {
      down = true;
      longDone = false;
      since = now;
    } else if (pressed && down && !longDone && now - since >= BTN_HOLD_MS) {
      longDone = true;
      result = 2;
    } else if (!pressed && down) {
      down = false;
      if (!longDone && now - since >= 30) result = 1;
    }
    return result;
  }
};

void loop() {
  static uint32_t lastLvl = 0;
  static Button power(PIN_BTN_POWER), channel(PIN_BTN_CHANNEL);

  pollSerial();

  Event e;
  while (xQueueReceive(eventQueue, &e, 0) == pdTRUE) handleEvent(e);
  static spec::SpecResult r;  // ~350 bytes: off loop()'s stack
  while (xQueueReceive(specQueue, &r, 0) == pdTRUE) {
    if (specOn || r.forced) printSpec(r);
    bardec::Decision d;
    if (decision.onSpectrum(r, d)) handleDecision(d);
  }
  if (decision.waiting() && millis() - waitSinceMs >= DECISION_WAIT_MS) {
    bardec::Decision d;
    if (decision.expire(d)) handleDecision(d);
  }

  uint32_t now = millis();
  Shared s = readShared(false, false);
  remote.onPeaks(s.strikePeak);
  float peaks[2] = {remote.peaks()[0], remote.peaks()[1]};
  tv::Command c = remote.poll(now);
  if (c != tv::Command::None) runCommand(c, peaks);

  int p = power.poll(now);
  if (p == 1) runCommand(tv::Command::Power, nullptr);
  if (p == 2) runCommand(tv::Command::Boom, nullptr);
  int ch = channel.poll(now);
  if (ch == 1) runCommand(tv::Command::Channel, nullptr);
  if (ch == 2) {
    requestReset();
    Serial.println("RESET ok=1");
  }

  tvscreen::Live live;
  live.snrDb[0] = s.levels.snrDb[0];
  live.snrDb[1] = s.levels.snrDb[1];
  live.learning = s.levels.learning;
  live.thresholdDb = activeCfg.thresholdDb;
  live.centerHz[0] = activeCfg.centerHz[0];
  live.centerHz[1] = activeCfg.centerHz[1];
  for (int k = 0; k < 3; k++) live.hits[k] = hits[k];
  live.rmsDbfs = dbfs(s.rms);
  screen.sample(live, now);
  screen.frame(now);

  if (now - lastLvl >= LVL_PERIOD_MS) {
    lastLvl = now;
    Shared l = readShared(false, true);
    if (lvlStream) {
      Serial.printf(
          "LVL snr_a=%.1f snr_b=%.1f guard_a=%.1f guard_b=%.1f level_a=%.1f level_b=%.1f rms_dbfs=%.1f learning=%d "
          "active=%d clip=%d\n",
          l.levels.snrDb[0], l.levels.snrDb[1], l.levels.guardSnrDb[0], l.levels.guardSnrDb[1], l.levels.levelDb[0],
          l.levels.levelDb[1], dbfs(l.rms), l.levels.learning, l.levels.active, l.clipSeen);
    }
  }
  delay(1);
}
