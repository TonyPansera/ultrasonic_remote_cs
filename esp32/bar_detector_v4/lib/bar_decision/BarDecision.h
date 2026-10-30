// Bar decision: settles the ToneDetector's ambiguous strikes (BOTH) with each bar's own lines, as
// measured by the strike spectrum of the same strike.
//
// Pure C++11 with no Arduino headers, so the same code runs on the ESP32 (src/main.cpp) and in the
// host unit tests (test/test_decision, test/test_pdm).
//
// Measured on 2026-10-30 (esp32/bar_detector_v3/measurements/REPORT.md): a strike also rings the other
// bar, sometimes as loud at first (one strike in 68 reached the BOTH tile). But the struck bar keeps
// ringing, while the other bar's lines collapse within ~40 ms (median drop 19 dB against 3 dB). So:
//   - Each bar has a fundamental (its detector band) and an overtone (its other mode, at a fixed
//     frequency ratio that does not move with temperature: 0.88313 and 0.96331, +-0.3 % over 88
//     strikes).
//   - A line counts when it rose at the strike (early level over the "before" window: a steady
//     background line at the same frequency did not) and stands over the capture's floor.
//   - The overtone counts only if its frequency over the fundamental's is the bar's ratio: then both
//     lines come from that bar, not from the other bar or the background.
//   - A bar's evidence is the power sum of its counted lines in the late window (40-60 ms after the
//     click): what still rings.
//   - A or B from the ToneDetector is kept. BOTH becomes the bar whose evidence leads by marginDb
//     (struck - other: >= 19 dB on all 88 strikes, median 41 dB), else stays BOTH.
// Matching: the detector's event and the capture of the same strike start within a few blocks of each
// other (the capture at the click or the first block over the threshold, the event at its first clean
// block). The decision waits for the capture; without one it keeps the detector's band.
#pragma once

#include <StrikeSpectrum.h>
#include <ToneDetector.h>
#include <stdint.h>

namespace bardec {

struct BarLines {
  float fundamentalHz;  // the watched line of the bar's band (its target in the spectrum config)
  float overtoneHz;     // the watched line of its other mode
  float ratio;          // overtone / fundamental frequency of this bar
};

struct DecisionConfig {
  float sampleRateHz = 192000.0f;
  BarLines bar[2] = {{38500.0f, 33900.0f, 0.88313f}, {47110.0f, 45370.0f, 0.96331f}};
  float ratioTol = 0.003f;      // relative
  float minRiseDb = 10.0f;      // early level over the before window
  float minLineDb = 6.0f;       // early level over the capture's floor
  float marginDb = 10.0f;       // evidence lead that settles BOTH
  // The capture may start up to matchBeforeS after the event's start, or matchAfterS before it. On
  // 2026-10-30 (S3-S6b, 85 strikes) the event started 0-32 ms after its capture; a capture 40 ms ahead
  // (a knock just before the strike, S1b) had its early window (10-31 ms) before the strike.
  float matchBeforeS = 0.016f;
  float matchAfterS = 0.036f;
};

enum class Reason : uint8_t { Detector = 0, Lines = 1, Close = 2, NoSpectrum = 3 };

struct BarEvidence {
  bool fundamental;  // the fundamental counted
  bool overtone;     // the overtone counted (rose, over the floor, at the bar's ratio)
  float ratio;       // measured overtone / fundamental frequency (0 if either line is missing)
  float lateDbfs;    // power sum of the counted lines' late levels (-240: none)
};

struct Decision {
  bardet::Event event;  // the detector's Detect event
  bardet::Band band;    // the decision
  bardet::Band detectorBand;
  Reason reason;
  bool spectrum;  // a matching capture was found
  BarEvidence bar[2];
  float marginDb;  // evidence A - B (0 without a spectrum)
};

const char *reasonName(Reason reason);

// The decision for one strike; spectrum may be null (no capture).
Decision decide(const bardet::Event &event, const spec::SpecResult *spectrum, const DecisionConfig &cfg);

// Pairs Detect events with captures that may come before or after them.
class BarDecision {
 public:
  explicit BarDecision(const DecisionConfig &cfg = DecisionConfig()) : cfg_(cfg) {}
  void configure(const DecisionConfig &cfg) { cfg_ = cfg; }
  const DecisionConfig &config() const { return cfg_; }

  // A Detect waits for its capture, unless one already came: onDetect returns the number of decisions
  // ready in out (0-2: a Detect still waiting for its capture is settled without one first). A capture
  // is kept for a Detect that may follow; onSpectrum returns true when it settles the waiting one.
  int onDetect(const bardet::Event &event, Decision out[2]);
  bool onSpectrum(const spec::SpecResult &result, Decision &out);
  // The waiting Detect, decided without a spectrum (call it when its capture is overdue).
  bool expire(Decision &out);
  bool waiting() const { return waiting_; }

 private:
  bool matches(const bardet::Event &event, const spec::SpecResult &result) const;

  DecisionConfig cfg_;
  bool waiting_ = false;
  bardet::Event event_ = {};
  static const int kKeep = 2;
  spec::SpecResult recent_[kKeep] = {};  // the last captures, newest at index 0
  int recentCount_ = 0;
};

}  // namespace bardec
