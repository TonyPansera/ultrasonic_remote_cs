// Host tests for lib/tone_detector (`pio test -e native`).
//
// Synthetic signals mimic the ESP32 input: 12-bit codes around mid-scale at 249 kS/s, with Gaussian
// noise, a broadband click at each strike and exponentially decaying bar modes. The real-data tests
// run the same detector on data/raw/rec4.wav and rec5.wav at their native 384 kHz (nothing is
// resampled); they are skipped when the recordings are not there (data/raw/ is not in git).

#include <ToneDetector.h>
#include <unity.h>

#include <math.h>
#include <stdio.h>
#include <stdlib.h>

#include <fstream>
#include <iterator>
#include <random>
#include <string>
#include <vector>

using namespace bardet;

namespace {

const float FS = 249000.0f;
const float MID = 2048.0f;
const float PI_F = 3.14159265358979f;
const float F_A = 40741.9f;  // rec5, the ~40 kHz bar
const float F_B = 51014.2f;  // rec4, the ~50 kHz bar

struct Collector {
  std::vector<Event> events;
  int detects(Band band) const {
    int n = 0;
    for (const Event &e : events) n += e.phase == Phase::Detect && e.band == band;
    return n;
  }
  int detects() const {
    int n = 0;
    for (const Event &e : events) n += e.phase == Phase::Detect;
    return n;
  }
  const Event *firstDetect() const {
    for (const Event &e : events)
      if (e.phase == Phase::Detect) return &e;
    return nullptr;
  }
};

void collect(const Event &e, void *ctx) { static_cast<Collector *>(ctx)->events.push_back(e); }

std::vector<float> noise(float seconds, float sigma, unsigned seed) {
  std::mt19937 rng(seed);
  std::normal_distribution<float> gauss(0.0f, sigma);
  std::vector<float> x((size_t)(seconds * FS));
  for (float &v : x) v = MID + gauss(rng);
  return x;
}

// Exponentially decaying mode; tau is the amplitude time constant (Q = pi f tau).
void addRing(std::vector<float> &x, float t0, float f, float amp, float tau) {
  size_t i0 = (size_t)(t0 * FS);
  for (size_t i = i0; i < x.size(); i++) {
    float t = (i - i0) / FS;
    if (t > 12 * tau) break;
    x[i] += amp * expf(-t / tau) * sinf(2 * PI_F * f * t + 0.3f);
  }
}

// Metal-on-metal contact: a few tens of microseconds, so flat far beyond 100 kHz.
void addClick(std::vector<float> &x, float t0, float amp) {
  size_t i0 = (size_t)(t0 * FS);
  const float shape[] = {1.0f, -0.8f, 0.6f, -0.5f, 0.3f, -0.2f};
  for (size_t k = 0; k < sizeof(shape) / sizeof(shape[0]) && i0 + k < x.size(); k++) x[i0 + k] += amp * shape[k];
}

void addTone(std::vector<float> &x, float t0, float t1, float f, float amp) {
  for (size_t i = (size_t)(t0 * FS); i < x.size() && i < (size_t)(t1 * FS); i++) x[i] += amp * sinf(2 * PI_F * f * i / FS);
}

// The FSK remote measured by ultrasonic_probe: bursts of ~19 ms on / ~1 ms off.
void addFsk(std::vector<float> &x, float t0, float t1, float f, float amp) {
  for (size_t i = (size_t)(t0 * FS); i < x.size() && i < (size_t)(t1 * FS); i++) {
    float tm = fmodf((i - t0 * FS) / FS, 0.020f);
    if (tm < 0.019f) x[i] += amp * sinf(2 * PI_F * f * i / FS);
  }
}

// Like the ESP32 ADC: codes 0..4095.
void clipToAdc(std::vector<float> &x) {
  for (float &v : x) v = v < 0 ? 0 : (v > 4095 ? 4095 : v);
}

Collector run(std::vector<float> &x, float fs, float lo, float hi, const Config *cfgIn = nullptr) {
  Config cfg = cfgIn ? *cfgIn : Config();
  cfg.sampleRateHz = fs;
  ToneDetector det(cfg);
  Collector c;
  det.setCallback(collect, &c);
  const size_t chunk = 1024;  // one ESP32 DMA buffer
  for (size_t i = 0; i < x.size(); i += chunk) {
    size_t n = x.size() - i < chunk ? x.size() - i : chunk;
    bool clipped = false;
    for (size_t k = 0; k < n; k++) clipped = clipped || x[i + k] <= lo || x[i + k] >= hi;
    det.push(&x[i], n, clipped);
  }
  return c;
}

Collector runAdc(std::vector<float> &x) { return run(x, FS, 0.0f, 4095.0f); }

void printEvents(const char *name, const Collector &c, float fs) {
  for (const Event &e : c.events) {
    printf("%s: %s t=%.3fs band=%s f=%.1f/%.1f snr=%.1f/%.1f margin=%.1f dur=%.0fms clip=%d confirmed=%d steady=%d\n",
           name, e.phase == Phase::Detect ? "DETECT" : "END   ", e.startSample / fs, bandName(e.band), e.freqHz[0],
           e.freqHz[1], e.peakSnrDb[0], e.peakSnrDb[1], e.marginDb, e.durationMs, e.clipped, e.confirmed, e.steady);
  }
}

// ---------- synthetic bars ----------

void test_ring_40k_gives_A() {
  std::vector<float> x = noise(3.0f, 3.0f, 1);
  addClick(x, 1.5f, 1500.0f);
  addRing(x, 1.5f, 40700.0f, 60.0f, 0.0673f);  // off-centre on purpose; rec5's tau
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("ring40", c, FS);
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  const Event *e = c.firstDetect();
  TEST_ASSERT_FLOAT_WITHIN(60.0f, 40700.0f, e->freqHz[0]);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, 1.5f, e->startSample / FS);
}

void test_ring_51k_gives_B() {
  std::vector<float> x = noise(3.0f, 3.0f, 2);
  addClick(x, 1.5f, 1500.0f);
  addRing(x, 1.5f, F_B, 60.0f, 0.145f);  // rec4's tau
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("ring51", c, FS);
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::B));
  TEST_ASSERT_FLOAT_WITHIN(60.0f, F_B, c.firstDetect()->freqHz[1]);
}

void test_weak_ring_still_detected() {
  std::vector<float> x = noise(3.0f, 3.0f, 3);
  addClick(x, 1.5f, 300.0f);
  addRing(x, 1.5f, F_A, 4.0f, 0.0673f);  // ~28 dB above the per-bin noise at the start (reliable from ~30 dB)
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("weak40", c, FS);
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_EQUAL_INT(1, c.detects());
}

void test_both_bars_together_give_BOTH() {
  std::vector<float> x = noise(3.0f, 3.0f, 4);
  addClick(x, 1.5f, 1500.0f);
  addRing(x, 1.5f, F_A, 60.0f, 0.1f);
  addRing(x, 1.5f, F_B, 60.0f, 0.1f);
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("both", c, FS);
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::Both));
}

void test_clipped_ring_keeps_its_band() {
  std::vector<float> x = noise(3.0f, 3.0f, 5);
  addClick(x, 1.5f, 3000.0f);
  addRing(x, 1.5f, F_A, 6000.0f, 0.0673f);  // rails at both ADC limits for the first ~80 ms
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("clipped40", c, FS);
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_TRUE(c.firstDetect()->clipped);
}

void test_two_strikes_two_events() {
  std::vector<float> x = noise(4.0f, 3.0f, 6);
  addClick(x, 1.3f, 1500.0f);
  addRing(x, 1.3f, F_A, 60.0f, 0.0673f);
  addClick(x, 2.6f, 1500.0f);
  addRing(x, 2.6f, F_B, 60.0f, 0.145f);
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("A then B", c, FS);
  TEST_ASSERT_EQUAL_INT(2, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::B));
}

// ---------- things that must not be reported ----------

void test_noise_only_gives_nothing() {
  std::vector<float> x = noise(10.0f, 3.0f, 10);
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("noise", c, FS);
  TEST_ASSERT_EQUAL_INT(0, (int)c.events.size());
}

void test_clicks_alone_give_nothing() {
  std::vector<float> x = noise(4.0f, 3.0f, 11);
  for (float t = 1.2f; t < 3.8f; t += 0.37f) addClick(x, t, 2000.0f);
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("clicks", c, FS);
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

void test_steady_tone_from_start_gives_nothing() {
  std::vector<float> x = noise(5.0f, 3.0f, 12);
  addTone(x, 0.0f, 5.0f, F_A, 60.0f);  // like rec1's steady tones
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("steady", c, FS);
  TEST_ASSERT_EQUAL_INT(0, (int)c.events.size());
}

void test_tone_switched_on_is_not_a_bar() {
  std::vector<float> x = noise(6.0f, 3.0f, 13);
  addTone(x, 1.5f, 6.0f, F_B, 60.0f);  // on and stays on
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("switched-on", c, FS);
  TEST_ASSERT_EQUAL_INT(0, c.detects());
  bool sawSteady = false;
  for (const Event &e : c.events) sawSteady = sawSteady || (e.phase == Phase::End && e.steady);
  TEST_ASSERT_TRUE(sawSteady);
}

void test_tone_burst_is_not_a_bar() {
  std::vector<float> x = noise(4.0f, 3.0f, 14);
  addTone(x, 1.5f, 2.3f, F_A, 60.0f);  // switched on, then off: no ringdown
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("burst", c, FS);
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

void test_fsk_remote_gives_nothing() {
  // The 38/42 kHz remote seen on GPIO32, rail to rail, so its harmonics alias too.
  std::vector<float> x = noise(5.0f, 3.0f, 15);
  addFsk(x, 1.2f, 2.2f, 42000.0f, 3000.0f);
  addFsk(x, 2.8f, 3.8f, 38000.0f, 3000.0f);
  addFsk(x, 4.0f, 4.6f, 42000.0f, 3000.0f);
  clipToAdc(x);
  Collector c = runAdc(x);
  printEvents("fsk", c, FS);
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

// ---------- real recordings (bat detector, 384 kHz) ----------

bool fileExists(const std::string &path) { return std::ifstream(path).good(); }

// The repo root is the nearest ancestor whose pyproject.toml declares name = "barlab" (like barlab's paths.py).
std::string findRawDir() {
  const char *env = getenv("BARLAB_ROOT");
  if (env) return std::string(env) + "/data/raw";
  std::string dir = ".";
  for (int depth = 0; depth < 8; depth++) {
    std::ifstream f(dir + "/pyproject.toml");
    if (f) {
      std::string text((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
      if (text.find("name = \"barlab\"") != std::string::npos) return dir + "/data/raw";
    }
    dir += "/..";
  }
  return "";
}

// Minimal RIFF reader: 16-bit PCM, first channel. Other chunks (GUANO metadata) are skipped.
bool readWav(const std::string &path, float &fs, std::vector<float> &x) {
  std::ifstream f(path, std::ios::binary);
  std::vector<unsigned char> d((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
  if (d.size() < 12 || std::string(d.begin(), d.begin() + 4) != "RIFF" || std::string(d.begin() + 8, d.begin() + 12) != "WAVE")
    return false;
  auto u16 = [&](size_t p) { return (unsigned)d[p] | ((unsigned)d[p + 1] << 8); };
  auto u32 = [&](size_t p) { return u16(p) | (u16(p + 2) << 16); };
  unsigned channels = 0, bits = 0;
  for (size_t p = 12; p + 8 <= d.size();) {
    std::string id(d.begin() + p, d.begin() + p + 4);
    size_t size = u32(p + 4);
    size_t body = p + 8;
    if (id == "fmt ") {
      channels = u16(body + 2);
      fs = (float)u32(body + 4);
      bits = u16(body + 14);
    } else if (id == "data") {
      if (bits != 16 || channels == 0) return false;
      size_t frames = size / (2 * channels);
      x.resize(frames);
      for (size_t i = 0; i < frames; i++) x[i] = (float)(int16_t)u16(body + 2 * channels * i);
      return true;
    }
    p = body + size + (size & 1);
  }
  return false;
}

Collector runRecording(const char *name) {
  std::string path = findRawDir() + "/" + name;
  if (findRawDir().empty() || !fileExists(path)) {
    Collector none;
    return none;
  }
  float fs = 0;
  std::vector<float> x;
  TEST_ASSERT_TRUE_MESSAGE(readWav(path, fs, x), "could not read the WAV file");
  Config cfg;
  cfg.blockSize = 3072;  // 8.0 ms and 125 Hz bins at 384 kHz, close to the ESP32's 8.2 ms / 122 Hz
  Collector c = run(x, fs, -32768.0f, 32767.0f, &cfg);
  printEvents(name, c, fs);
  return c;
}

void test_rec4_is_the_50k_bar() {
  if (findRawDir().empty() || !fileExists(findRawDir() + "/rec4.wav")) TEST_IGNORE_MESSAGE("data/raw/rec4.wav not found");
  Collector c = runRecording("rec4.wav");
  TEST_ASSERT_GREATER_OR_EQUAL_INT(1, c.detects(Band::B));
  TEST_ASSERT_EQUAL_INT(0, c.detects(Band::A));
  TEST_ASSERT_EQUAL_INT(0, c.detects(Band::Both));
}

void test_rec5_is_the_40k_bar() {
  if (findRawDir().empty() || !fileExists(findRawDir() + "/rec5.wav")) TEST_IGNORE_MESSAGE("data/raw/rec5.wav not found");
  Collector c = runRecording("rec5.wav");
  TEST_ASSERT_GREATER_OR_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_EQUAL_INT(0, c.detects(Band::B));
  TEST_ASSERT_EQUAL_INT(0, c.detects(Band::Both));
}

}  // namespace

void setUp() {}
void tearDown() {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_ring_40k_gives_A);
  RUN_TEST(test_ring_51k_gives_B);
  RUN_TEST(test_weak_ring_still_detected);
  RUN_TEST(test_both_bars_together_give_BOTH);
  RUN_TEST(test_clipped_ring_keeps_its_band);
  RUN_TEST(test_two_strikes_two_events);
  RUN_TEST(test_noise_only_gives_nothing);
  RUN_TEST(test_clicks_alone_give_nothing);
  RUN_TEST(test_steady_tone_from_start_gives_nothing);
  RUN_TEST(test_tone_switched_on_is_not_a_bar);
  RUN_TEST(test_tone_burst_is_not_a_bar);
  RUN_TEST(test_fsk_remote_gives_nothing);
  RUN_TEST(test_rec4_is_the_50k_bar);
  RUN_TEST(test_rec5_is_the_40k_bar);
  return UNITY_END();
}
