// Ringing-bar detector: tells a ~40 kHz bar from a ~50 kHz bar in a stream of ADC samples.
//
// Pure C++11 with no Arduino headers, so the same code runs on the ESP32 (src/main.cpp) and in the
// host unit tests (test/test_detector, `pio test -e native`), including on the real recordings.
//
// Per block of N samples (DC removed, Hann window), generalised Goertzel bins measure:
//   - each band: bins spaced fs/N across center +- tolerance;
//   - guard bins on both sides of each band (center +- 1..2 kHz).
// Every bin has its own noise baseline (linear power, learned at start, then tracked slowly and
// frozen during an event), so steady lines (environment, the board's own ~41 kHz supply line, the
// 38/42 kHz FSK remote) are absorbed instead of detected. SNR = power / baseline, in dB.
//
// A block qualifies for a band when its best band bin is >= thresholdDb above baseline AND beats
// every guard bin's SNR by guardMarginDb. The guard rule rejects broadband clicks (all bins rise)
// and strong neighbouring tones (the FSK remote next to band A); a block passing it is "clean".
// Peaks, frequencies and the decay test below only use clean blocks. triggerBlocks consecutive
// qualifying blocks open an event. The event is reported (Phase::Detect) only once the strongest
// band rings down like a bar: it is decayDb or more below its peak yet above the release level, and
// its last confirmBlocks clean blocks fall on a straight line in dB (least squares: slope <=
// -minDecayDbPerBlock, R^2 >= minDecayR2). A switched tone stops in one block; a fluctuating line
// (the FSK remote's harmonics, which alias onto exactly 51.0 kHz at 249 kS/s) has no clean trend. The event closes (Phase::End) when both bands stay below
// thresholdDb - releaseDb for releaseBlocks blocks. An event still open after maxEventMs is a steady
// tone: it closes unconfirmed and the baselines are re-learned with it.
// The band decision compares the peak SNRs: the winner must lead by decisionMarginDb, else BOTH.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <vector>

namespace bardet {

enum class Band : uint8_t { None = 0, A = 1, B = 2, Both = 3 };
enum class Phase : uint8_t { Detect = 0, End = 1 };

const int kMaxConfirmBlocks = 16;

struct Config {
  float sampleRateHz = 249000.0f;  // the real rate (the ESP32 measures it at boot)
  int blockSize = 2048;            // 8.2 ms and 122 Hz bins at 249 kS/s
  // Band centres from barlab: rec5 40 741.9 Hz (the ~40 kHz bar), rec4 51 014.2 Hz (the ~50 kHz bar).
  float centerHz[2] = {40741.9f, 51014.2f};
  float toleranceHz = 500.0f;    // band bins cover center +- tolerance (temperature, sensor, unit spread)
  float guardInnerHz = 1000.0f;  // guard bins from center +- guardInnerHz ...
  float guardOuterHz = 2000.0f;  // ... to center +- guardOuterHz
  float thresholdDb = 15.0f;     // band SNR needed to qualify
  float guardMarginDb = 6.0f;    // band SNR must beat the guard SNR by this much
  float decayDb = 4.0f;          // drop from the peak that confirms a ringdown ...
  int confirmBlocks = 5;         // ... with this many consecutive clean blocks (at most kMaxConfirmBlocks) ...
  float minDecayDbPerBlock = 0.1f;  // ... decaying at least this fast (12 dB/s at 8.2 ms blocks) ...
  float minDecayR2 = 0.6f;       // ... along a straight line in dB
  float releaseDb = 3.0f;        // the event ends below thresholdDb - releaseDb
  float decisionMarginDb = 10.0f;  // lead of the winning band over the other, else BOTH
  int triggerBlocks = 2;
  int releaseBlocks = 2;
  float learnS = 1.0f;          // baseline learning time at start and after reset()
  float noiseTauS = 0.5f;       // baseline tracking when the bin is near its baseline
  float noiseSlowTauS = 5.0f;   // baseline tracking when the bin is above the threshold (pulls towards +3 dB only)
  float maxEventMs = 3000.0f;   // longer events are steady tones
};

struct Event {
  Phase phase;
  Band band;             // decision from the peak SNRs so far (A, B or Both)
  bool confirmed;        // the line decayed like a ringing bar (always true for Detect)
  bool steady;           // End only: closed by maxEventMs without decaying
  bool clipped;          // some block of the event had a sample at the ADC limit
  float freqHz[2];       // frequency of each band's best bin at its peak block (parabolic interpolation)
  float peakSnrDb[2];    // peak SNR of each band during the event
  float marginDb;        // peak SNR of the stronger band minus the other band's
  float durationMs;      // from the first qualifying block to now (Detect) / to the last block above release (End)
  uint64_t startSample;  // first sample of the first qualifying block
};

struct Levels {
  bool learning;
  bool active;           // an event is open
  bool clipped;          // the last block had a sample at the ADC limit
  float snrDb[2];        // best band bin SNR in the last block
  float guardSnrDb[2];   // best guard bin SNR in the last block
  float levelDb[2];      // amplitude of the best band bin, 20 log10(input units)
};

typedef void (*EventCallback)(const Event &event, void *ctx);

class ToneDetector {
 public:
  ToneDetector();
  explicit ToneDetector(const Config &cfg);

  // Rebuilds the bins and restarts learning.
  void configure(const Config &cfg);
  // Forgets the baselines and any open event, then learns again for learnS.
  void reset();
  void setCallback(EventCallback cb, void *ctx);

  // Feeds samples in input units (ADC codes, PCM values ...). clipped tells whether any of them sat
  // at the converter limit. Processes every block completed by these samples.
  void push(const float *x, size_t n, bool clipped);

  const Config &config() const { return cfg_; }
  const Levels &levels() const { return levels_; }
  float binHz() const { return binHz_; }
  int bandBins(int band) const { return bandCount_[band]; }
  uint64_t blocksProcessed() const { return blockIndex_; }

 private:
  struct Bin {
    float freqHz;
    float coeff;  // 2 cos(2 pi f / fs)
  };

  void addBin(float freqHz);
  void processBlock();
  void updateBaselines();
  float interpolatedFreq(int band, int k) const;
  bool ringsDown(int band) const;
  void emit(Phase phase, bool steady);
  void closeEvent(bool steady);

  Config cfg_;
  int n_ = 0;
  float binHz_ = 0;
  float blockMs_ = 0;
  float powerScale_ = 0;  // turns |X|^2 into squared sine amplitude
  float alphaFast_ = 0;
  float alphaSlow_ = 0;
  std::vector<float> window_;
  std::vector<float> block_;
  std::vector<Bin> bins_;
  std::vector<float> power_;
  std::vector<float> baseline_;
  int bandFirst_[2] = {0, 0};
  int bandCount_[2] = {0, 0};
  int guardFirst_[2] = {0, 0};
  int guardCount_[2] = {0, 0};

  size_t fill_ = 0;
  bool blockClipped_ = false;
  uint64_t blockIndex_ = 0;

  bool learning_ = true;
  int learnBlocks_ = 0;
  int learnTarget_ = 1;

  // per-block results
  float snr_[2] = {0, 0};
  int snrBin_[2] = {0, 0};

  // trigger and event state
  int run_[2] = {0, 0};
  float runPeakSnr_[2] = {0, 0};
  float runPeakFreq_[2] = {0, 0};
  bool active_ = false;
  bool confirmed_ = false;
  bool eventClipped_ = false;
  uint64_t startBlock_ = 0;
  uint64_t lastAboveBlock_ = 0;
  int belowBlocks_ = 0;
  float history_[2][kMaxConfirmBlocks];  // SNR of the last consecutive clean blocks, per band (ring buffer)
  int historyCount_[2] = {0, 0};
  float peakSnr_[2] = {0, 0};
  float peakFreq_[2] = {0, 0};

  Levels levels_ = {};
  EventCallback callback_ = nullptr;
  void *callbackCtx_ = nullptr;
};

const char *bandName(Band band);

}  // namespace bardet
