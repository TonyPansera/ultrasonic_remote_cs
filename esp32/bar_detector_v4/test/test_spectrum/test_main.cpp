// Host tests for lib/strike_spectrum (`pio test -e native`).
//
// Signals are 192 kS/s PCM in full-scale units, as the decimator delivers them, pushed in 1536-sample
// chunks like the firmware's DMA buffers: white noise, an impact click, exponentially decaying modes
// and a steady environment line.

#include <StrikeSpectrum.h>
#include <unity.h>

#include <math.h>
#include <stdio.h>

#include <initializer_list>
#include <random>
#include <vector>

using spec::SpecConfig;
using spec::SpecPeak;
using spec::SpecResult;
using spec::StrikeSpectrum;

namespace {

const double PI_D = 3.14159265358979323846;
const float FS = 192000.0f;
const size_t CHUNK = 1536;

std::vector<float> noise(float seconds, float sigma, unsigned seed) {
  std::mt19937 rng(seed);
  std::normal_distribution<float> gauss(0.0f, sigma);
  std::vector<float> x((size_t)(seconds * FS));
  for (float &v : x) v = gauss(rng);
  return x;
}

// Exponentially decaying mode; tau is the amplitude time constant.
void addRing(std::vector<float> &x, double t0, double f, double amp, double tau) {
  for (size_t i = (size_t)(t0 * FS); i < x.size(); i++) {
    double t = (double)i / FS - t0;  // in float, i / FS jitters by ~0.1 us at 1.5 s: -42 dB spurs
    if (t > 12 * tau) break;
    x[i] += (float)(amp * exp(-t / tau) * sin(2 * PI_D * f * t + 0.3));
  }
}

void addClick(std::vector<float> &x, double t0, float amp) {
  size_t i0 = (size_t)(t0 * FS);
  const float shape[] = {1.0f, -0.8f, 0.6f, -0.5f, 0.3f, -0.2f};
  for (size_t k = 0; k < 6 && i0 + k < x.size(); k++) x[i0 + k] += amp * shape[k];
}

void addTone(std::vector<float> &x, double f, double amp) {
  for (size_t i = 0; i < x.size(); i++) x[i] += (float)(amp * sin(2 * PI_D * f * i / FS));
}

// Pushes x in DMA-sized chunks; forceAtS >= 0 calls forceCapture() before the chunk at that time.
std::vector<SpecResult> run(const char *name, const std::vector<float> &x, SpecConfig cfg = SpecConfig(),
                            double forceAtS = -1) {
  cfg.sampleRateHz = FS;
  StrikeSpectrum s(cfg);
  std::vector<SpecResult> out;
  bool forced = false;
  for (size_t i = 0; i < x.size(); i += CHUNK) {
    if (forceAtS >= 0 && !forced && i >= forceAtS * FS) {
      s.forceCapture();
      forced = true;
    }
    size_t n = x.size() - i < CHUNK ? x.size() - i : CHUNK;
    if (s.push(&x[i], n)) out.push_back(s.result());
  }
  for (const SpecResult &r : out) {
    printf("%s: t=%.4fs trig=%.1f dB forced=%d floor=%.1f dBFS peaks=%d\n", name, r.triggerSample / FS, r.triggerDb,
           r.forced, r.floorDbfs, r.count);
    for (int p = 0; p < r.count; p++)
      printf("    f=%.1f early=%.1f late=%.1f dBFS\n", r.peaks[p].freqHz, r.peaks[p].earlyDbfs, r.peaks[p].lateDbfs);
    for (int l = 0; l < r.lineCount; l++)
      printf("    line %.0f: f=%.1f early=%.1f late=%.1f dBFS\n", r.lines[l].targetHz, r.lines[l].freqHz,
             r.lines[l].earlyDbfs, r.lines[l].lateDbfs);
  }
  return out;
}

SpecConfig watching(std::initializer_list<spec::WatchLine> lines) {
  SpecConfig cfg;
  for (const spec::WatchLine &w : lines) cfg.watch[cfg.watchCount++] = w;
  return cfg;
}

const SpecPeak *peakNear(const SpecResult &r, float f, float tol) {
  for (int p = 0; p < r.count; p++)
    if (fabsf(r.peaks[p].freqHz - f) <= tol) return &r.peaks[p];
  return nullptr;
}

void test_fft_matches_dft() {
  const int n = 256;
  std::vector<float> cosT(n / 2), sinT(n / 2), re(n), im(n);
  for (int k = 0; k < n / 2; k++) {
    cosT[k] = (float)cos(2 * PI_D * k / n);
    sinT[k] = (float)sin(2 * PI_D * k / n);
  }
  std::mt19937 rng(7);
  std::uniform_real_distribution<float> u(-1.0f, 1.0f);
  std::vector<float> x(n), y(n);
  for (int i = 0; i < n; i++) {
    x[i] = re[i] = u(rng);
    y[i] = im[i] = u(rng);
  }
  spec::fftInPlace(re.data(), im.data(), n, cosT.data(), sinT.data());
  for (int k = 0; k < n; k++) {
    double sr = 0, si = 0;
    for (int i = 0; i < n; i++) {
      double ph = -2 * PI_D * k * i / n;
      sr += x[i] * cos(ph) - y[i] * sin(ph);
      si += x[i] * sin(ph) + y[i] * cos(ph);
    }
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, (float)sr, re[k]);
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, (float)si, im[k]);
  }
}

void test_strike_lines_found_and_steady_line_told_apart() {
  std::vector<float> x = noise(3.0f, 1e-4f, 1);
  addTone(x, 27340.0, 0.002);  // a steady environment line, like rec4/rec5's 27.34 kHz
  addClick(x, 1.503, 0.5f);
  addRing(x, 1.503, 38650.0, 0.03, 0.0673);
  addRing(x, 1.503, 45300.0, 0.01, 0.040);
  std::vector<SpecResult> r = run("two modes", x);
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  // The click is 7 ms into the 8 ms chunk that triggered.
  TEST_ASSERT_TRUE(r[0].triggerSample / FS <= 1.503f && r[0].triggerSample / FS > 1.503f - CHUNK / FS);
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 38650.0f, r[0].peaks[0].freqHz);  // the strongest comes first
  const SpecPeak *a = peakNear(r[0], 38650.0f, 10.0f);
  const SpecPeak *b = peakNear(r[0], 45300.0f, 10.0f);
  const SpecPeak *env = peakNear(r[0], 27340.0f, 10.0f);
  TEST_ASSERT_NOT_NULL(a);
  TEST_ASSERT_NOT_NULL(b);
  TEST_ASSERT_NOT_NULL(env);
  TEST_ASSERT_EQUAL_INT(3, r[0].count);  // nothing else stands 15 dB above the floor
  // 40 ms between the windows: 20 log10(e) * 0.040 / tau dB.
  TEST_ASSERT_FLOAT_WITHIN(1.0f, 8.686f * 0.040f / 0.0673f, a->earlyDbfs - a->lateDbfs);
  TEST_ASSERT_FLOAT_WITHIN(1.0f, 8.686f * 0.040f / 0.040f, b->earlyDbfs - b->lateDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 0.0f, env->earlyDbfs - env->lateDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, (float)(20 * log10(0.002)), env->earlyDbfs);
}

void test_noise_and_steady_line_do_not_trigger() {
  std::vector<float> x = noise(4.0f, 1e-4f, 2);
  addTone(x, 27340.0, 0.002);
  std::vector<SpecResult> r = run("quiet", x);
  TEST_ASSERT_EQUAL_INT(0, (int)r.size());
}

void test_two_strikes_two_captures() {
  std::vector<float> x = noise(4.0f, 1e-4f, 3);
  addClick(x, 1.5, 0.5f);
  addRing(x, 1.5, 38650.0, 0.03, 0.0673);
  addClick(x, 2.7, 0.5f);
  addRing(x, 2.7, 45300.0, 0.02, 0.040);
  std::vector<SpecResult> r = run("two strikes", x);
  TEST_ASSERT_EQUAL_INT(2, (int)r.size());
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 38650.0f, r[0].peaks[0].freqHz);
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 45300.0f, r[1].peaks[0].freqHz);
}

void test_weak_strike_found_by_its_click() {
  std::vector<float> x = noise(3.0f, 1e-4f, 4);
  addClick(x, 1.5, 0.05f);
  addRing(x, 1.5, 41000.0, 0.001, 0.05);  // -60 dBFS, ~45 dB above the noise per bin
  std::vector<SpecResult> r = run("weak", x);
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  TEST_ASSERT_NOT_NULL(peakNear(r[0], 41000.0f, 10.0f));
}

void test_watched_lines_are_measured_even_when_weak() {
  // A struck mode, a weak one (8 dB over the per-bin noise, under the peaks' 15 dB prominence: a bar
  // ringing faintly in sympathy), a steady line past fMaxHz, an empty frequency and one below fMinHz.
  std::vector<float> x = noise(3.0f, 1e-4f, 5);
  addClick(x, 1.503, 0.5f);
  addRing(x, 1.503, 38650.0, 0.03, 0.0673);
  addRing(x, 1.503, 45300.0, 1e-5, 0.040);
  addTone(x, 93000.0, 0.001);
  std::vector<SpecResult> r =
      run("watched", x,
          watching({{38600.0f, 250.0f}, {45370.0f, 250.0f}, {93000.0f, 300.0f}, {60000.0f, 250.0f}, {10000.0f, 250.0f}}));
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  TEST_ASSERT_FALSE(r[0].forced);
  TEST_ASSERT_EQUAL_INT(5, r[0].lineCount);
  const spec::SpecLine *l = r[0].lines;
  const SpecPeak *a = peakNear(r[0], 38650.0f, 10.0f);
  TEST_ASSERT_NOT_NULL(a);
  // The struck mode: the same frequency and levels as its peak.
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 38650.0f, l[0].freqHz);
  TEST_ASSERT_FLOAT_WITHIN(0.1f, a->earlyDbfs, l[0].earlyDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.1f, a->lateDbfs, l[0].lateDbfs);
  // The weak mode is no peak, but its line is measured: -100 dBFS at its frequency.
  TEST_ASSERT_NULL(peakNear(r[0], 45300.0f, 50.0f));
  TEST_ASSERT_FLOAT_WITHIN(25.0f, 45300.0f, l[1].freqHz);
  TEST_ASSERT_TRUE(l[1].earlyDbfs > r[0].floorDbfs + 4 && l[1].earlyDbfs < r[0].floorDbfs + 15);
  // Past fMaxHz: the steady line at its level, and no drop.
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 93000.0f, l[2].freqHz);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, -60.0f, l[2].earlyDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 0.0f, l[2].earlyDbfs - l[2].lateDbfs);
  // Nothing there: the highest noise bin, a few dB over the median.
  TEST_ASSERT_TRUE(l[3].earlyDbfs > r[0].floorDbfs - 3 && l[3].earlyDbfs < r[0].floorDbfs + 10);
  // Below fMinHz: not analysed.
  TEST_ASSERT_EQUAL_FLOAT(0.0f, l[4].freqHz);
}

void test_forced_capture_needs_no_click() {
  // The background survey (`spec now`): a steady line and noise, no strike.
  std::vector<float> x = noise(3.0f, 1e-4f, 6);
  addTone(x, 40000.0, 0.003);
  std::vector<SpecResult> r = run("forced", x, watching({{40000.0f, 500.0f}}), 1.2);
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  TEST_ASSERT_TRUE(r[0].forced);
  TEST_ASSERT_FLOAT_WITHIN(CHUNK / FS + 1e-4f, 1.2f, r[0].triggerSample / FS);
  TEST_ASSERT_FLOAT_WITHIN(5.0f, 40000.0f, r[0].peaks[0].freqHz);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, (float)(20 * log10(0.003)), r[0].lines[0].earlyDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, 0.0f, r[0].lines[0].earlyDbfs - r[0].lines[0].lateDbfs);
}

void test_forced_capture_waits_for_learning() {
  // Asked for during the learning time, the capture starts as soon as learning ends (0.5 s).
  std::vector<float> x = noise(2.0f, 1e-4f, 7);
  std::vector<SpecResult> r = run("forced early", x, SpecConfig(), 0.1);
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  TEST_ASSERT_TRUE(r[0].forced);
  TEST_ASSERT_FLOAT_WITHIN(CHUNK / FS + 1e-4f, 0.5f, r[0].triggerSample / FS);
}

void test_external_trigger_starts_capture_with_this_chunk() {
  // A soft strike whose click does not raise the energy by triggerDb: the band detector's onset
  // (triggerNext() before the chunk) starts the capture at the same chunk as a click would.
  std::vector<float> x = noise(3.0f, 1e-4f, 8);
  addRing(x, 1.503, 38400.0, 1e-4, 0.05);  // -80 dBFS, no click: +1 dB of second-difference energy
  SpecConfig cfg;
  cfg.sampleRateHz = FS;
  StrikeSpectrum s(cfg);
  std::vector<SpecResult> out;
  const size_t strikeChunk = (size_t)(1.503 * FS) / CHUNK * CHUNK;
  for (size_t i = 0; i + CHUNK <= x.size(); i += CHUNK) {
    if (i == strikeChunk || i == (size_t)(0.2 * FS) / CHUNK * CHUNK) s.triggerNext();  // 0.2 s: still learning
    if (s.push(&x[i], CHUNK)) out.push_back(s.result());
  }
  TEST_ASSERT_EQUAL_INT(1, (int)out.size());
  TEST_ASSERT_TRUE(out[0].external);
  TEST_ASSERT_FALSE(out[0].forced);
  TEST_ASSERT_EQUAL_UINT64(strikeChunk, out[0].triggerSample);
  TEST_ASSERT_NOT_NULL(peakNear(out[0], 38400.0f, 10.0f));
}

void test_external_trigger_is_forgotten_when_not_armed() {
  // Asked for during the learning time, it is dropped, not kept for later.
  std::vector<float> x = noise(2.0f, 1e-4f, 9);
  SpecConfig cfg;
  cfg.sampleRateHz = FS;
  StrikeSpectrum s(cfg);
  int captures = 0;
  for (size_t i = 0; i + CHUNK <= x.size(); i += CHUNK) {
    if (i == 10 * CHUNK) s.triggerNext();
    captures += s.push(&x[i], CHUNK);
  }
  TEST_ASSERT_EQUAL_INT(0, captures);
}

void test_before_window_tells_struck_line_from_steady_one() {
  // A steady background line (like the ~40 kHz one) and a struck mode: the struck line rose from the
  // floor at the click, the steady one was already there at the same level.
  std::vector<float> x = noise(3.0f, 1e-4f, 11);
  addTone(x, 40100.0, 0.003);
  addClick(x, 1.503, 0.5f);
  addRing(x, 1.503, 38400.0, 0.01, 0.2);
  std::vector<SpecResult> r = run("before", x, watching({{38400.0f, 300.0f}, {40000.0f, 800.0f}}));
  TEST_ASSERT_EQUAL_INT(1, (int)r.size());
  const spec::SpecLine &struck = r[0].lines[0], &steady = r[0].lines[1];
  printf("before: struck %.1f -> %.1f dBFS, steady %.1f -> %.1f dBFS\n", struck.beforeDbfs, struck.earlyDbfs,
         steady.beforeDbfs, steady.earlyDbfs);
  TEST_ASSERT_TRUE(struck.earlyDbfs - struck.beforeDbfs > 40.0f);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, steady.earlyDbfs, steady.beforeDbfs);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, (float)(20 * log10(0.003)), steady.beforeDbfs);
}

// A knock (click, no ring) at tKnock, then a soft strike at tStrike that only the outside onset
// detector sees: the strike must get the capture.
std::vector<SpecResult> knockThenStrike(double tKnock, double tStrike, double tOnset) {
  std::vector<float> x = noise(3.0f, 1e-4f, 10);
  addClick(x, tKnock, 0.5f);
  addRing(x, tStrike, 38400.0, 1e-4, 0.05);
  SpecConfig cfg;
  cfg.sampleRateHz = FS;
  StrikeSpectrum s(cfg);
  std::vector<SpecResult> out;
  const size_t onsetChunk = (size_t)(tOnset * FS) / CHUNK * CHUNK;
  for (size_t i = 0; i + CHUNK <= x.size(); i += CHUNK) {
    if (i == onsetChunk) s.triggerNext();
    if (s.push(&x[i], CHUNK)) out.push_back(s.result());
  }
  for (const SpecResult &r : out)
    printf("knock %.3f strike %.3f: capture t=%.4f external=%d peaks=%d\n", tKnock, tStrike, r.triggerSample / FS,
           r.external, r.count);
  return out;
}

void test_onset_restarts_capture_started_by_earlier_knock() {
  // Knock 40 ms before the strike: its capture is dropped for the strike's.
  std::vector<SpecResult> out = knockThenStrike(1.460, 1.503, 1.503);
  TEST_ASSERT_EQUAL_INT(1, (int)out.size());
  TEST_ASSERT_TRUE(out[0].external);
  TEST_ASSERT_EQUAL_UINT64((size_t)(1.503 * FS) / CHUNK * CHUNK, out[0].triggerSample);
  TEST_ASSERT_NOT_NULL(peakNear(out[0], 38400.0f, 10.0f));
}

void test_onset_overrides_holdoff() {
  // Knock 150 ms before the strike: its capture is done and in holdoff; the strike still gets one.
  std::vector<SpecResult> out = knockThenStrike(1.353, 1.503, 1.503);
  TEST_ASSERT_EQUAL_INT(2, (int)out.size());
  TEST_ASSERT_FALSE(out[0].external);
  TEST_ASSERT_TRUE(out[1].external);
  TEST_ASSERT_NOT_NULL(peakNear(out[1], 38400.0f, 10.0f));
}

void test_onset_one_chunk_after_click_keeps_click_capture() {
  // The band rises one chunk after the click's chunk (same strike): the click's capture stays.
  std::vector<SpecResult> out = knockThenStrike(1.503, 1.503, 1.503 + CHUNK / FS);
  TEST_ASSERT_EQUAL_INT(1, (int)out.size());
  TEST_ASSERT_FALSE(out[0].external);
  TEST_ASSERT_EQUAL_UINT64((size_t)(1.503 * FS) / CHUNK * CHUNK, out[0].triggerSample);
}

}  // namespace

void setUp() {}
void tearDown() {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_fft_matches_dft);
  RUN_TEST(test_strike_lines_found_and_steady_line_told_apart);
  RUN_TEST(test_noise_and_steady_line_do_not_trigger);
  RUN_TEST(test_two_strikes_two_captures);
  RUN_TEST(test_weak_strike_found_by_its_click);
  RUN_TEST(test_watched_lines_are_measured_even_when_weak);
  RUN_TEST(test_forced_capture_needs_no_click);
  RUN_TEST(test_forced_capture_waits_for_learning);
  RUN_TEST(test_external_trigger_starts_capture_with_this_chunk);
  RUN_TEST(test_external_trigger_is_forgotten_when_not_armed);
  RUN_TEST(test_before_window_tells_struck_line_from_steady_one);
  RUN_TEST(test_onset_restarts_capture_started_by_earlier_knock);
  RUN_TEST(test_onset_overrides_holdoff);
  RUN_TEST(test_onset_one_chunk_after_click_keeps_click_capture);
  return UNITY_END();
}
