// Host tests for lib/pdm_decimator and the whole prototype-2 chain (`pio test -e native`).
//
// Test signals are turned into a PDM bitstream at the mic clock (3.072 MHz) by a 2nd-order 1-bit
// sigma-delta modulator, packed into 32-bit words the way the ESP32's I2S DMA delivers them (first
// bit at bit 31), decimated to 192 kS/s and, for the end-to-end tests, fed to the ToneDetector with
// the firmware's own settings (lib/device_config). The SPH0641's modulator is of higher order, so
// its in-band noise is lower than this one's: the tests err on the pessimistic side.

#include <DeviceConfig.h>
#include <PdmDecimator.h>
#include <ToneDetector.h>
#include <unity.h>

#include <math.h>
#include <stdio.h>

#include <functional>
#include <random>
#include <vector>

using bardet::Band;
using bardet::Event;
using bardet::Phase;

namespace {

const double PI_D = 3.14159265358979323846;
const pdm::PdmConfig MIC = v2::micConfig();
const double FCLK = MIC.pdmClockHz;
const float FS = MIC.pdmClockHz / MIC.decimation;  // 192 kS/s
const size_t DMA_WORDS = 768;                       // one ESP32 DMA buffer: 384 frames of 2 slots
const bardet::Config DET = v2::detectorConfig(FS);
const float F_A = DET.centerHz[0];
const float F_B = DET.centerHz[1];
// Amplitude time constants from `spec` on the board (2026-10-23): 8.8 dB and ~11.2 dB lost in 40 ms.
const float TAU_A = 0.039f;
const float TAU_B = 0.031f;

typedef std::function<double(double)> Signal;  // sound pressure in full-scale units at time t (s)

// 2nd-order 1-bit modulator, Y = X + E (1 - z^-1)^2; stable for |x| up to ~0.7.
class SigmaDelta {
 public:
  bool step(double x) {
    i1_ += x - y_;
    i2_ += i1_ - y_;
    y_ = i2_ >= 0 ? 1.0 : -1.0;
    return y_ > 0;
  }

 private:
  double i1_ = 0, i2_ = 0, y_ = 1;
};

// Streams a signal as PDM words, first bit at bit 31.
class PdmSource {
 public:
  explicit PdmSource(Signal signal) : signal_(signal) {}
  void fill(uint32_t *words, size_t n) {
    for (size_t w = 0; w < n; w++) {
      uint32_t v = 0;
      for (int b = 31; b >= 0; b--, bit_++) v |= (uint32_t)sd_.step(signal_(bit_ / FCLK)) << b;
      words[w] = v;
    }
  }

 private:
  Signal signal_;
  SigmaDelta sd_;
  uint64_t bit_ = 0;
};

std::vector<uint32_t> modulate(double seconds, Signal signal) {
  std::vector<uint32_t> words((size_t)(seconds * FCLK / 32) & ~(size_t)1);
  PdmSource(signal).fill(words.data(), words.size());
  return words;
}

std::vector<uint32_t> randomWords(size_t n, unsigned seed) {
  std::mt19937 rng(seed);
  std::vector<uint32_t> words(n);
  for (uint32_t &w : words) w = rng();
  return words;
}

// The same words in the order the DMA may store them: each frame's two slots reversed.
std::vector<uint32_t> swapPairs(const std::vector<uint32_t> &words) {
  std::vector<uint32_t> out(words);
  for (size_t i = 0; i + 1 < out.size(); i += 2) std::swap(out[i], out[i + 1]);
  return out;
}

std::vector<float> decimate(const std::vector<uint32_t> &words, const pdm::PdmConfig &cfg) {
  pdm::PdmDecimator dec(cfg);
  std::vector<float> y(dec.outputsFor(words.size()));
  y.resize(dec.push(words.data(), words.size(), y.data()));
  return y;
}

// Amplitude of the sine at f in y[start, start + n), Hann window.
double amplitudeAt(const std::vector<float> &y, size_t start, size_t n, double f) {
  double re = 0, im = 0, wsum = 0;
  for (size_t i = 0; i < n; i++) {
    double w = 0.5 - 0.5 * cos(2 * PI_D * i / n);
    double ph = 2 * PI_D * f * (start + i) / FS;
    re += w * y[start + i] * cos(ph);
    im -= w * y[start + i] * sin(ph);
    wsum += w;
  }
  return 2 * sqrt(re * re + im * im) / wsum;
}

// ---------- the decimator ----------

void test_filter_response() {
  pdm::PdmDecimator dec(MIC);
  double sum = 0;
  for (float h : dec.coefficients()) sum += h;
  TEST_ASSERT_FLOAT_WITHIN(1e-5f, 1.0f, (float)sum);
  float pbWorst = 0;
  for (float f = 0; f <= 60000; f += 250) pbWorst = fmaxf(pbWorst, fabsf(dec.responseDb(f)));
  float at80k = dec.responseDb(80000);
  // From 132 kHz on, everything folds back into 0-60 kHz at 192 kS/s.
  float sbWorst = -300;
  for (float f = 132000; f <= FCLK / 2; f += 100) sbWorst = fmaxf(sbWorst, dec.responseDb(f));
  printf("filter: passband 0-60 kHz within %.3f dB, %.2f dB at 80 kHz, stopband 132 kHz-%.3f MHz <= %.1f dB\n",
         pbWorst, at80k, FCLK / 2e6, sbWorst);
  TEST_ASSERT_TRUE(pbWorst <= 0.05f);
  TEST_ASSERT_TRUE(at80k >= -1.0f);
  TEST_ASSERT_TRUE(sbWorst <= -85.0f);
}

void test_dma_word_order() {
  // swapWords undoes the reversed slot order exactly.
  std::vector<uint32_t> words = randomWords(8 * DMA_WORDS, 1);
  pdm::PdmConfig swapped = MIC;
  swapped.swapWords = true;
  std::vector<float> a = decimate(words, MIC);
  std::vector<float> b = decimate(swapPairs(words), swapped);
  TEST_ASSERT_EQUAL_size_t(words.size() * 32 / MIC.decimation, a.size());
  TEST_ASSERT_EQUAL_size_t(a.size(), b.size());
  for (size_t i = 0; i < a.size(); i++) TEST_ASSERT_EQUAL_FLOAT(a[i], b[i]);
}

void test_chunks_are_seamless() {
  std::vector<uint32_t> words = randomWords(8 * DMA_WORDS, 2);
  std::vector<float> whole = decimate(words, MIC);
  pdm::PdmDecimator dec(MIC);
  std::vector<float> parts;
  const size_t sizes[] = {2, 6, 34, 768, 100, 4, 1000};
  size_t at = 0, s = 0;
  while (at < words.size()) {
    size_t n = sizes[s++ % 7];
    if (n > words.size() - at) n = words.size() - at;
    std::vector<float> y(dec.outputsFor(n));
    size_t got = dec.push(&words[at], n, y.data());
    TEST_ASSERT_EQUAL_size_t(n * 32 / MIC.decimation, got);
    parts.insert(parts.end(), y.begin(), y.begin() + got);
    at += n;
  }
  TEST_ASSERT_EQUAL_size_t(whole.size(), parts.size());
  for (size_t i = 0; i < whole.size(); i++) TEST_ASSERT_EQUAL_FLOAT(whole[i], parts[i]);
}

void test_silence_is_half_ones() {
  std::vector<uint32_t> words = modulate(0.05, [](double) { return 0.0; });
  pdm::PdmDecimator dec(MIC);
  std::vector<float> y(dec.outputsFor(words.size()));
  size_t n = dec.push(words.data(), words.size(), y.data());
  TEST_ASSERT_FLOAT_WITHIN(0.001f, 0.5f, dec.onesDensity());
  // Skip the first FIR span: the start-up history meets the modulator's idle pattern out of phase.
  double peak = 0;
  for (size_t i = MIC.taps / MIC.decimation; i < n; i++) peak = fmax(peak, fabs(y[i]));
  TEST_ASSERT_TRUE(peak < 1e-3);
}

void test_tone_40k_level_and_frequency() {
  const double amp = 0.1, f0 = 40000;  // -20 dBFS
  std::vector<float> y = decimate(modulate(0.15, [=](double t) { return amp * sin(2 * PI_D * f0 * t); }), MIC);
  const size_t start = (size_t)(0.01 * FS), n = (size_t)(0.1 * FS);
  double best = 0, bestF = 0;
  for (double f = f0 - 300; f <= f0 + 300; f += 2) {
    double a = amplitudeAt(y, start, n, f);
    if (a > best) best = a, bestF = f;
  }
  double floor30k = amplitudeAt(y, start, n, 30000);
  printf("40 kHz at -20 dBFS: %.2f dBFS at %.0f Hz; 30 kHz bin %.1f dBFS\n", 20 * log10(best), bestF,
         20 * log10(floor30k));
  TEST_ASSERT_FLOAT_WITHIN(4.0f, (float)f0, (float)bestF);
  TEST_ASSERT_FLOAT_WITHIN(0.5f, -20.0f, (float)(20 * log10(best)));
  TEST_ASSERT_TRUE(floor30k < amp * 1e-3);  // >= 60 dB below the tone
}

void test_alias_150k_is_rejected() {
  // 150 kHz would fold onto 42 kHz, between the two bars. At -30 dBFS: louder tones that high make
  // this 2nd-order test modulator itself put a spur on 42 kHz (-74 dB at -10 dBFS), which is not the
  // decimator's doing (the FIR alone gives -102 dB at 150 kHz).
  const double amp = 0.03;
  std::vector<float> y = decimate(modulate(0.45, [=](double t) { return amp * sin(2 * PI_D * 150000 * t); }), MIC);
  double alias = amplitudeAt(y, (size_t)(0.01 * FS), (size_t)(0.4 * FS), FS - 150000.0);
  printf("150 kHz at %.1f dBFS: %.1f dBFS at %.0f Hz (%.1f dB down)\n", 20 * log10(amp), 20 * log10(alias),
         FS - 150000.0, 20 * log10(alias / amp));
  TEST_ASSERT_TRUE(alias < amp * 1e-4);  // >= 80 dB down
}

// ---------- the whole chain: mic bits -> decimator -> detector ----------

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

// Runs `seconds` of signal through the firmware's chain, one DMA buffer at a time.
Collector runChain(const char *name, double seconds, Signal signal) {
  pdm::PdmDecimator dec(MIC);
  bardet::ToneDetector det(DET);
  Collector c;
  det.setCallback(collect, &c);
  PdmSource src(signal);
  std::vector<uint32_t> words(DMA_WORDS);
  std::vector<float> pcm(dec.outputsFor(DMA_WORDS));
  for (double t = 0; t < seconds; t += DMA_WORDS * 32 / FCLK) {
    src.fill(words.data(), DMA_WORDS);
    size_t n = dec.push(words.data(), DMA_WORDS, pcm.data());
    bool clipped = false;
    for (size_t i = 0; i < n; i++) clipped = clipped || fabsf(pcm[i]) >= 0.89f;
    det.push(pcm.data(), n, clipped);
  }
  for (const Event &e : c.events) {
    printf("%s: %s t=%.3fs band=%s f=%.1f/%.1f snr=%.1f/%.1f margin=%.1f dur=%.0fms confirmed=%d steady=%d\n", name,
           e.phase == Phase::Detect ? "DETECT" : "END   ", e.startSample / FS, bardet::bandName(e.band), e.freqHz[0],
           e.freqHz[1], e.peakSnrDb[0], e.peakSnrDb[1], e.marginDb, e.durationMs, e.confirmed, e.steady);
  }
  return c;
}

// Room noise at the mic, white up to the bit rate's Nyquist: ~-90 dB per 125 Hz bin at the bars,
// about the same as the modulator's own noise there.
struct Noise {
  explicit Noise(unsigned seed, double sigma = 0.003) : rng(seed), gauss(0.0, sigma) {}
  double operator()() { return gauss(rng); }
  std::mt19937 rng;
  std::normal_distribution<double> gauss;
};

double ring(double t, double t0, double f, double amp, double tau) {
  if (t < t0 || t > t0 + 12 * tau) return 0;
  return amp * exp(-(t - t0) / tau) * sin(2 * PI_D * f * (t - t0) + 0.3);
}

// Metal-on-metal contact: a 10 us doublet, flat well past the bars.
double click(double t, double t0, double amp) {
  if (t < t0 || t >= t0 + 10e-6) return 0;
  return t < t0 + 5e-6 ? amp : -amp;
}

// The FSK remote measured by ultrasonic_probe: bursts of ~19 ms on / ~1 ms off.
double fsk(double t, double t0, double t1, double f, double amp) {
  if (t < t0 || t >= t1 || fmod(t - t0, 0.020) >= 0.019) return 0;
  return amp * sin(2 * PI_D * f * t);
}

// The same, heard through the air: when it stops, the room's reverberation makes the tone fade
// exponentially (tau), which looks like a ringdown.
double fskInRoom(double t, double t0, double t1, double f, double amp, double tau) {
  if (t < t1) return fsk(t, t0, t1, f, amp);
  if (t > t1 + 12 * tau) return 0;
  return amp * exp(-(t - t1) / tau) * sin(2 * PI_D * f * t);
}

void test_chain_strike_A_gives_A() {
  Noise noise(1);
  Collector c = runChain("chain A", 3.0, [&](double t) {
    return noise() + click(t, 1.5, 0.5) + ring(t, 1.5, F_A - 40, 0.03, TAU_A);  // off-centre on purpose
  });
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_FLOAT_WITHIN(60.0f, F_A - 40, c.firstDetect()->freqHz[0]);
  TEST_ASSERT_FLOAT_WITHIN(0.05f, 1.5f, c.firstDetect()->startSample / FS);
  TEST_ASSERT_FALSE(c.firstDetect()->clipped);
}

void test_chain_strike_B_gives_B() {
  Noise noise(2);
  Collector c = runChain("chain B", 3.0, [&](double t) {
    return noise() + click(t, 1.5, 0.5) + ring(t, 1.5, F_B, 0.03, TAU_B);
  });
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::B));
  TEST_ASSERT_FLOAT_WITHIN(60.0f, F_B, c.firstDetect()->freqHz[1]);
}

void test_chain_weak_strike_still_detected() {
  Noise noise(3);
  Collector c = runChain("chain weak A", 3.0, [&](double t) {
    return noise() + click(t, 1.5, 0.1) + ring(t, 1.5, F_A, 0.004, TAU_A);  // ~35 dB above the floor per bin
  });
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
}

void test_chain_two_strikes_two_events() {
  Noise noise(4);
  Collector c = runChain("chain A then B", 4.0, [&](double t) {
    return noise() + click(t, 1.3, 0.5) + ring(t, 1.3, F_A, 0.03, TAU_A) + click(t, 2.6, 0.5) +
           ring(t, 2.6, F_B, 0.03, TAU_B);
  });
  TEST_ASSERT_EQUAL_INT(2, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::B));
}

void test_chain_noise_and_clicks_give_nothing() {
  Noise noise(5);
  Collector c = runChain("chain clicks", 5.0, [&](double t) {
    double x = noise();
    for (double t0 = 1.2; t0 < 4.8; t0 += 0.37) x += click(t, t0, 0.5);
    return x;
  });
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

void test_chain_fsk_remote_gives_nothing() {
  Noise noise(6);
  Collector c = runChain("chain fsk", 5.0, [&](double t) {
    return noise() + fsk(t, 1.2, 2.2, 42000, 0.3) + fsk(t, 2.8, 3.8, 38000, 0.3) + fsk(t, 4.0, 4.6, 42000, 0.3);
  });
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

void test_chain_strike_after_fsk_gives_A() {
  // The remote's 38 kHz tone sits in band A: it must not count, nor leave band A deaf afterwards.
  Noise noise(7);
  Collector c = runChain("chain fsk38+strike", 5.0, [&](double t) {
    return noise() + fsk(t, 1.2, 2.4, 38000, 0.3) + click(t, 3.4, 0.5) + ring(t, 3.4, F_A, 0.03, TAU_A);
  });
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::A));
  TEST_ASSERT_FLOAT_WITHIN(0.05f, 3.4f, c.firstDetect()->startSample / FS);
}

void test_chain_reverberant_remote_is_not_a_bar() {
  // Seen on the board on 2026-10-23 with band A at 38.0 kHz: clickless 38.00 kHz tones lasting
  // 0.06-1.7 s, with a faint 42.7-43.2 kHz line, detected as A at 30-45 dB (the FSK remote, fading
  // in the room's reverberation). Band A now sits ~600 Hz above it, with +-300 Hz of tolerance. The
  // last burst is 14 dB louder, like the one that reached 31 dB in A's bottom bin on the board.
  Noise noise(8);
  Collector c = runChain("chain remote in room", 7.0, [&](double t) {
    return noise() + fskInRoom(t, 1.2, 2.0, 38000, 0.01, 0.03) + fskInRoom(t, 1.2, 2.0, 42000, 0.001, 0.03) +
           fskInRoom(t, 3.0, 4.6, 38000, 0.01, 0.03) + fskInRoom(t, 5.0, 5.6, 38000, 0.05, 0.03);  // +14 dB
  });
  TEST_ASSERT_EQUAL_INT(0, c.detects());
}

void test_chain_47k_strike_with_sympathetic_38k_gives_B() {
  // On the board, every strike of the ~47 kHz bar also rang the ~38 kHz bar's line, 25-40 dB weaker
  // (sympathetic ringing), plus its own second line at ~45.37 kHz, 15-30 dB weaker.
  Noise noise(9);
  Collector c = runChain("chain 47k + sympathetic 38k", 3.0, [&](double t) {
    return noise() + click(t, 1.5, 0.5) + ring(t, 1.5, F_B, 0.1, TAU_B) + ring(t, 1.5, 45370, 0.005, 0.025) +
           ring(t, 1.5, F_A, 0.003, TAU_A);
  });
  TEST_ASSERT_EQUAL_INT(1, c.detects());
  TEST_ASSERT_EQUAL_INT(1, c.detects(Band::B));
  TEST_ASSERT_FLOAT_WITHIN(30.0f, F_B, c.firstDetect()->freqHz[1]);
}

}  // namespace

void setUp() {}
void tearDown() {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_filter_response);
  RUN_TEST(test_dma_word_order);
  RUN_TEST(test_chunks_are_seamless);
  RUN_TEST(test_silence_is_half_ones);
  RUN_TEST(test_tone_40k_level_and_frequency);
  RUN_TEST(test_alias_150k_is_rejected);
  RUN_TEST(test_chain_strike_A_gives_A);
  RUN_TEST(test_chain_strike_B_gives_B);
  RUN_TEST(test_chain_weak_strike_still_detected);
  RUN_TEST(test_chain_two_strikes_two_events);
  RUN_TEST(test_chain_noise_and_clicks_give_nothing);
  RUN_TEST(test_chain_fsk_remote_gives_nothing);
  RUN_TEST(test_chain_strike_after_fsk_gives_A);
  RUN_TEST(test_chain_reverberant_remote_is_not_a_bar);
  RUN_TEST(test_chain_47k_strike_with_sympathetic_38k_gives_B);
  return UNITY_END();
}
