// Host tests for lib/strike_spectrum (`pio test -e native`).
//
// Signals are 192 kS/s PCM in full-scale units, as the decimator delivers them, pushed in 1536-sample
// chunks like the firmware's DMA buffers: white noise, an impact click, exponentially decaying modes
// and a steady environment line.

#include <StrikeSpectrum.h>
#include <unity.h>

#include <math.h>
#include <stdio.h>

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

std::vector<SpecResult> run(const char *name, const std::vector<float> &x) {
  SpecConfig cfg;
  cfg.sampleRateHz = FS;
  StrikeSpectrum s(cfg);
  std::vector<SpecResult> out;
  for (size_t i = 0; i < x.size(); i += CHUNK) {
    size_t n = x.size() - i < CHUNK ? x.size() - i : CHUNK;
    if (s.push(&x[i], n)) out.push_back(s.result());
  }
  for (const SpecResult &r : out) {
    printf("%s: t=%.4fs trig=%.1f dB floor=%.1f dBFS peaks=%d\n", name, r.triggerSample / FS, r.triggerDb, r.floorDbfs,
           r.count);
    for (int p = 0; p < r.count; p++)
      printf("    f=%.1f early=%.1f late=%.1f dBFS\n", r.peaks[p].freqHz, r.peaks[p].earlyDbfs, r.peaks[p].lateDbfs);
  }
  return out;
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
  return UNITY_END();
}
