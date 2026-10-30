// TV remote: turns the bar decisions into TV commands, like the remote the bars come from.
//   - the 38 bar (band A): power on / off;
//   - the 47 bar (band B): next channel (only while on);
//   - both bars at once: an explosion.
//
// Pure C++11 with no Arduino headers, so the same code runs on the ESP32 (src/main.cpp) and in the
// host unit tests (test/test_tv).
//
// A decided strike is held for a short while before it becomes a command, because two things are only
// known later. Both were measured on 2026-10-30 (measurements/REPORT.md in bar_detector_v3):
//   - "Both at once": a person striking both bars is a few ms apart, so the detector decides on the
//     first bar. The second bar then rings as a clean line of its own: the weaker band's clean peak SNR
//     reached 48-60 dB on all 5 "both" strikes (S6b), against at most 41 dB on 111 single strikes (the
//     other bar ringing in sympathy). So a strike whose two bands both peak over bothSnrDb within the
//     hold, or which the decision left BOTH, is an explosion.
//   - Weak extra strikes: twice a weak 38 (26-37 dB) came 0.4 s before a real strike (the key mechanism
//     touching the bar?), and once a weak one (22 dB) 0.5 s after a strong 38. Strikes under weakSnrDb
//     wait longer, and a stronger strike within that wait replaces them; within refractoryMs after a
//     command, they are dropped.
#pragma once

#include <BarDecision.h>
#include <ToneDetector.h>
#include <stdint.h>

namespace tv {

enum class Command : uint8_t { None = 0, Power = 1, Channel = 2, Boom = 3 };

struct RemoteConfig {
  uint32_t holdMs = 300;      // a strike waits this long before it acts (a second bar may follow)
  uint32_t weakHoldMs = 500;  // a weak strike waits this long (a stronger one may replace it)
  float weakSnrDb = 40.0f;    // under this peak SNR (its band, at Detect) a strike is weak
  float bothSnrDb = 44.0f;    // both bands' clean peaks over this: both bars were struck
  uint32_t refractoryMs = 800;  // after a command, weak strikes are dropped for this long
};

// The commands, from the bar decisions and the bands' clean peak SNRs of the strike under way.
class Remote {
 public:
  explicit Remote(const RemoteConfig &cfg = RemoteConfig()) : cfg_(cfg) {}

  // A decided strike, at nowMs. If one is already waiting, the stronger of the two is kept. A weak one
  // within refractoryMs of the last command is dropped.
  void onDecision(const bardec::Decision &d, uint32_t nowMs);
  // The clean peak SNR of each band since the strike under way started (the sampling task tracks it).
  // Taken into the waiting strike's peaks.
  void onPeaks(const float peakSnrDb[2]);
  // The command of the waiting strike once its hold is over, else None.
  Command poll(uint32_t nowMs);
  bool waiting() const { return waiting_; }
  // The waiting strike's peaks (for the screen).
  const float *peaks() const { return peak_; }

 private:
  static float strength(const bardec::Decision &d);

  RemoteConfig cfg_;
  bool waiting_ = false;
  bardec::Decision pending_ = {};
  uint32_t sinceMs_ = 0;
  bool commanded_ = false;  // a command was given, at lastCommandMs_
  uint32_t lastCommandMs_ = 0;
  float peak_[2] = {0, 0};
};

// What the TV shows.
struct TvState {
  bool on = false;
  int channel = 0;  // 0 .. channels - 1
};

// Applies a command: Power toggles, Channel steps to the next channel (wrapping) and does nothing while
// the TV is off, Boom leaves the state as it is (the explosion is drawn over it).
TvState apply(TvState state, Command command, int channels);

const char *commandName(Command command);

}  // namespace tv
