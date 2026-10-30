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
//   - Watched lines: for each configured frequency (the bars' known lines, background lines, theory
//     candidates), the highest early bin within its tolerance, interpolated the same way, and the
//     late level next to it. They are reported whether or not they stand out, so weak lines (a bar
//     ringing in sympathy, a damped harmonic) are measured too. Unlike the peaks they may reach up
//     to 2 bins below Nyquist, past fMaxHz.
//   - A "before" spectrum of the fftSize samples just ahead of the trigger chunk (kept in a ring),
//     taken when the capture starts: each watched line's level there tells a line the strike made
//     (it rose) from a steady background line at the same frequency (it did not).
//   - forceCapture() starts a capture without a click, to survey the background.
//   - Then holdoffMs without triggering, and armed again.
// The measured values (frequency, levels) are raw: deciding which line is which bar is left to the
// reader.
#pragma once

#include <stddef.h>
#include <math.h>
#include <stdint.h>

#include <vector>

namespace spec {

const int kMaxPeaks = 10;
const int kMaxWatch = 12;

struct WatchLine {
  float hz;     // expected frequency
  float tolHz;  // searched within hz +- tolHz
};

struct SpecConfig {
  float sampleRateHz = 192000.0f;
  int fftSize = 4096;             // power of two: 21.3 ms and 46.9 Hz bins at 192 kS/s
  float earlyMs = 10.0f;          // early window start, from the start of the trigger chunk
  float lateMs = 50.0f;           // late window start
  float fMinHz = 20000.0f;
  float fMaxHz = 90000.0f;        // upper limit of the floor and the peaks (not of the watched lines)
  float triggerDb = 10.0f;        // ultrasonic energy over its baseline that starts a capture
  float minProminenceDb = 15.0f;  // peak level over the early spectrum's median
  int guardBins = 4;              // no two peaks closer than this (Hann main lobe: +-2 bins)
  int maxPeaks = 10;              // at most kMaxPeaks
  float learnS = 0.5f;            // baseline learning after configure() / reset()
  float baselineTauS = 1.0f;      // baseline tracking while armed
  float holdoffMs = 300.0f;       // after a capture
  int watchCount = 0;             // at most kMaxWatch
  WatchLine watch[kMaxWatch] = {};
};

struct SpecPeak {
  float freqHz;
  float earlyDbfs;  // sine amplitude in the early window, dBFS
  float lateDbfs;   // the same line in the late window
};

struct SpecLine {
  float targetHz;   // the watched frequency
  float freqHz;     // where its highest early bin is, within the tolerance (0: outside the spectrum)
  float earlyDbfs;  // sine amplitude there in the early window, dBFS
  float lateDbfs;   // the same line in the late window
  float beforeDbfs;  // the same line in the fftSize samples before the trigger chunk
};

struct SpecResult {
  uint64_t triggerSample;  // first sample of the trigger chunk
  float triggerDb;         // ultrasonic energy of that chunk over the baseline
  bool forced;             // started by forceCapture(), not by a click
  bool external;           // started by triggerNext() (the energy alone did not reach triggerDb)
  float floorDbfs;         // median of the early spectrum in fMinHz..fMaxHz
  int count;
  SpecPeak peaks[kMaxPeaks];
  int lineCount;  // = the config's watchCount at capture time
  SpecLine lines[kMaxWatch];
};

// In-place radix-2 complex FFT (forward, no scaling); cosT/sinT hold cos/sin(2 pi k / n), k < n/2.
void fftInPlace(float *re, float *im, int n, const float *cosT, const float *sinT);

class StrikeSpectrum {
 public:
  // Allocates nothing (~83 KB at the defaults): a global instance must not allocate before setup(),
  // where the ESP32 heap is not complete yet (std::bad_alloc in the static constructors).
  StrikeSpectrum();
  explicit StrikeSpectrum(const SpecConfig &cfg);

  // Allocates the buffers. push() ignores samples until this is called.
  void configure(const SpecConfig &cfg);
  // Forgets the baseline and any capture, then learns again for learnS.
  void reset();
  // Replaces the watched lines (at most kMaxWatch) without reallocating; applies from the next capture.
  void setWatch(const WatchLine *lines, int count);
  // The next chunk starts a capture whatever its energy, once learning and any holdoff are over.
  void forceCapture() { forcePending_ = true; }
  // The next chunk starts a capture, as if its energy had risen (forced stays false), if the spectrum
  // is armed, in holdoff, or capturing since two or more chunks (that capture is dropped); otherwise
  // (learning, or a capture started in the previous chunk) this is forgotten. For an outside onset
  // detector that sees a strike the energy trigger missed: the board's mic noise near Nyquist
  // dominates the second difference, so soft clicks rose by less than triggerDb on 2026-10-30 (6 of 15
  // strikes), and knocks 40 ms before a strike took its capture.
  void triggerNext() { triggerPending_ = true; }

  // Feeds samples in full-scale units. Returns true when a capture has just been analysed: the
  // result is then in result() until the next one.
  bool push(const float *x, size_t n);
  const SpecResult &result() const { return result_; }
  const SpecConfig &config() const { return cfg_; }

 private:
  enum class State { Learning, Armed, Capturing, Holdoff };

  bool step(const float *x, size_t n);
  void analyse();
  void measureLine(const WatchLine &w, SpecLine &line) const;
  float levelNear(const std::vector<int16_t> &db, int k) const;
  void spectrumDb(const std::vector<int16_t> &buf, size_t start, std::vector<int16_t> &db);
  // Spectra are kept in centi-dB (int16): half the RAM of float, 0.005 dB of rounding.
  static int16_t toCdb(float db) {
    db *= 100.0f;
    return (int16_t)lroundf(db > 32767.0f ? 32767.0f : (db < -32767.0f ? -32767.0f : db));
  }
  static float at(const std::vector<int16_t> &db, int k) { return db[k] * 0.01f; }
  static int16_t toPcm16(float v) {
    v *= 32767.0f;
    return (int16_t)lroundf(v > 32767.0f ? 32767.0f : (v < -32767.0f ? -32767.0f : v));
  }
  static float parabolicPeak(const std::vector<int16_t> &db, int k, float &delta);

  SpecConfig cfg_;
  State state_ = State::Learning;
  bool forcePending_ = false;
  bool triggerPending_ = false;
  float prev1_ = 0, prev2_ = 0;  // last two samples, for the second difference
  double baseline_ = 0;
  uint64_t sampleCount_ = 0;
  uint64_t learnUntil_ = 0;
  uint32_t learnChunks_ = 0;
  uint64_t holdUntil_ = 0;
  int kMin_ = 0, kMax_ = 0;  // floor and peaks: bins kMin_..kMax_
  int kTop_ = 0;             // the spectra are kept up to here, for the watched lines
  int earlyOffset_ = 0, lateOffset_ = 0;
  std::vector<int16_t> capture_;  // the early window, then the late one; full scale = 32767
  size_t captureSpan_ = 0;        // samples from the trigger chunk to the end of the later window
  size_t captureFill_ = 0;        // samples of that span seen so far
  std::vector<float> re_, im_, cos_, sin_;
  std::vector<int16_t> history_;  // the last fftSize samples (ring), full scale = 32767
  size_t historyPos_ = 0;         // oldest sample of history_
  std::vector<int16_t> early_, late_, before_;  // centi-dBFS for bins kMin_..kTop_
  SpecResult result_ = {};
};

}  // namespace spec
