// Strike spectrum: finds where a struck bar actually rings, anywhere in a wide band.
//
// Pure C++11 with no Arduino headers, so the same code runs on the ESP32 (src/main.cpp, `spec on`)
// and in the host unit tests (test/test_spectrum, `pio test -e native`).
//
// The ToneDetector only looks at two 1 kHz bands; this diagnostic looks everywhere from fMinHz to
// fMaxHz, to find (and later re-centre) the bands:
//   - Trigger: the ultrasonic energy of each pushed chunk (mean square of the second difference,
//     which suppresses audio: -60 dB at 1 kHz, +3.5 dB at 40 kHz at 192 kS/s) against its own
//     baseline (tracked while armed). A rise of triggerDb or more starts a capture at that chunk.
//   - Capture: from the start of the trigger chunk, long enough for two FFT windows: "early" at
//     earlyMs and "late" at lateMs after the capture start. The click sits somewhere in the trigger
//     chunk, so with 8 ms chunks the early window starts 2-10 ms after it.
//   - Analysis: Hann-windowed FFTs (fftSize points, fs / fftSize bins), levels as sine amplitude in
//     dBFS. Peaks are local maxima of the early spectrum at least minProminenceDb above its median in
//     fMinHz..fMaxHz, strongest first, none within guardBins of a stronger one (Hann sidelobes), with
//     parabolic interpolation of the frequency. Each peak's late level is the late spectrum's maximum
//     within 2 bins. A ringing mode loses level between the windows; a steady line does not.
//   - Then holdoffMs without triggering, and armed again.
// The measured values (frequency, levels) are raw: deciding which line is which bar is left to the
// reader.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <vector>

namespace spec {

const int kMaxPeaks = 8;

struct SpecConfig {
  float sampleRateHz = 192000.0f;
  int fftSize = 4096;             // power of two: 21.3 ms and 46.9 Hz bins at 192 kS/s
  float earlyMs = 10.0f;          // early window start, from the start of the trigger chunk
  float lateMs = 50.0f;           // late window start
  float fMinHz = 20000.0f;
  float fMaxHz = 90000.0f;
  float triggerDb = 10.0f;        // ultrasonic energy over its baseline that starts a capture
  float minProminenceDb = 15.0f;  // peak level over the early spectrum's median
  int guardBins = 4;              // no two peaks closer than this (Hann main lobe: +-2 bins)
  int maxPeaks = 6;               // at most kMaxPeaks
  float learnS = 0.5f;            // baseline learning after configure() / reset()
  float baselineTauS = 1.0f;      // baseline tracking while armed
  float holdoffMs = 300.0f;       // after a capture
};

struct SpecPeak {
  float freqHz;
  float earlyDbfs;  // sine amplitude in the early window, dBFS
  float lateDbfs;   // the same line in the late window
};

struct SpecResult {
  uint64_t triggerSample;  // first sample of the trigger chunk
  float triggerDb;         // ultrasonic energy of that chunk over the baseline
  float floorDbfs;         // median of the early spectrum in fMinHz..fMaxHz
  int count;
  SpecPeak peaks[kMaxPeaks];
};

// In-place radix-2 complex FFT (forward, no scaling); cosT/sinT hold cos/sin(2 pi k / n), k < n/2.
void fftInPlace(float *re, float *im, int n, const float *cosT, const float *sinT);

class StrikeSpectrum {
 public:
  // Allocates nothing (~90 KB at the defaults): a global instance must not allocate before setup(),
  // where the ESP32 heap is not complete yet (std::bad_alloc in the static constructors).
  StrikeSpectrum();
  explicit StrikeSpectrum(const SpecConfig &cfg);

  // Allocates the buffers. push() ignores samples until this is called.
  void configure(const SpecConfig &cfg);
  // Forgets the baseline and any capture, then learns again for learnS.
  void reset();

  // Feeds samples in full-scale units. Returns true when a capture has just been analysed: the
  // result is then in result() until the next one.
  bool push(const float *x, size_t n);
  const SpecResult &result() const { return result_; }
  const SpecConfig &config() const { return cfg_; }

 private:
  enum class State { Learning, Armed, Capturing, Holdoff };

  void analyse();
  void spectrumDb(int offset, std::vector<float> &db);
  static float parabolicPeak(const std::vector<float> &db, int k, float &delta);

  SpecConfig cfg_;
  State state_ = State::Learning;
  float prev1_ = 0, prev2_ = 0;  // last two samples, for the second difference
  double baseline_ = 0;
  uint64_t sampleCount_ = 0;
  uint64_t learnUntil_ = 0;
  uint32_t learnChunks_ = 0;
  uint64_t holdUntil_ = 0;
  int kMin_ = 0, kMax_ = 0;
  int earlyOffset_ = 0, lateOffset_ = 0;
  std::vector<int16_t> capture_;  // full scale = 32767
  size_t captureFill_ = 0;
  std::vector<float> re_, im_, cos_, sin_;
  std::vector<float> early_, late_;  // dBFS for bins kMin_..kMax_
  SpecResult result_ = {};
};

}  // namespace spec
