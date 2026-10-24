// Settings of prototype 2 (SPH0641 PDM mic on GPIO22/21), shared by src/main.cpp and the host tests,
// so the tests run the exact chain that is flashed.
#pragma once

#include <PdmDecimator.h>
#include <ToneDetector.h>

namespace v2 {

// Mic clock 3.072 MHz (the SPH0641's ultrasonic mode starts there; its ultrasonic response is
// specified at that clock), decimated by 16 to 192 kS/s.
inline pdm::PdmConfig micConfig() {
  pdm::PdmConfig c;
  c.pdmClockHz = 3072000.0f;
  c.decimation = 16;
  return c;
}

// The detector of prototype 1, fed with 192 kS/s PCM in full-scale units. 1536-sample blocks keep
// v1's block length and bin spacing (8.0 ms / 125 Hz against 8.2 ms / 122 Hz), so the per-block
// thresholds (decay slope, trigger and release counts) mean the same as on the GPIO32 version.
inline bardet::Config detectorConfig(float sampleRateHz) {
  bardet::Config c;
  c.sampleRateHz = sampleRateHz;
  c.blockSize = 1536;
  // Measured with this mic (`spec on`) on 2026-10-23:
  //   A, the ~38 kHz bar: 38 595.5 Hz on a direct strike, 38 606-38 650 Hz when it rang in sympathy
  //     with the other bar (median of the 5: 38 618 Hz). Second line ~33.94 kHz.
  //   B, the other bar: 47 081-47 114 Hz over 11 strikes (median 47 088 Hz), second line ~45.37 kHz.
  //     Its 42.78 kHz line (prototype 1's band B) was not there any more.
  // Tolerance +-300 Hz instead of 500: a clickless 38.00 kHz background tone (FSK-remote-like),
  // fading in the room's reverberation, passed for a ringdown in band A at 38.0 kHz (30-45 dB on the
  // board). Band bins span +-ceil(tol / 125 Hz) = +-3 bins, i.e. +-375 Hz: the tone still reaches up
  // to 31 dB in A's bottom bin (38 243 Hz), but does not ring down there and is rejected. Verified on
  // the board with that tone on: B 15/15, A 8/8, no false detection (A 38 593-38 617 Hz, B 47 070-
  // 47 239 Hz). +-250 Hz would move the bottom bin away from the tone, but no test or board run needed
  // it, and it leaves the 47 bar's +151 Hz spread little room.
  c.centerHz[0] = 38618.0f;
  c.centerHz[1] = 47088.0f;
  c.toleranceHz = 300.0f;
  return c;
}

}  // namespace v2
