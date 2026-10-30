// Settings of prototype 3 (prototype 2's board and SPH0641 PDM mic on GPIO22/21, with bar-specific
// overtone checks), shared by src/main.cpp and the host tests, so the tests run the exact chain that
// is flashed.
#pragma once

#include <BarDecision.h>
#include <PdmDecimator.h>
#include <StrikeSpectrum.h>
#include <ToneDetector.h>

namespace v3 {

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
  // Re-measured on 2026-10-30, after the prototype was changed (sessions S1b / S2, the other bar held):
  //   A, the 38 bar: 38 377-38 431 Hz over 12 strikes (median 38 398 Hz; 38 361-38 478 in S1), so it
  //     moved down ~220 Hz from 2026-10-23's 38 618 Hz. Second line 33.92 kHz, often louder than A.
  //   B, the 47 bar: 47 087-47 228 Hz over 14 strikes (median 47 101 Hz). Second line 45.37 kHz, as
  //     loud as B or up to 14 dB louder (on 2026-10-23 it was 15-30 dB weaker).
  // A sits at 38 500 Hz, above the direct strikes: its band bins span +-ceil(tol / 125 Hz) = +-2 bins
  // (+-250 Hz), 38 250-38 750 Hz, which holds the strikes and keeps the bottom bin 250 Hz (the Hann
  // main lobe's half-width) off the clickless 38.00 kHz background tone (FSK-remote-like). Fading in
  // the room's reverberation, that tone fooled band A at 38.0 kHz +-500 Hz on 2026-10-23, and A at
  // 38 420 Hz (bottom bin 38 170 Hz) in test_chain_reverberant_remote_is_not_a_bar.
  c.centerHz[0] = 38500.0f;
  c.centerHz[1] = 47110.0f;
  c.toleranceHz = 200.0f;
  // Guards 1.0-1.375 kHz each side (4 bins) instead of 1-2 kHz: the 47 bar's own 45.37 kHz line, 1.73
  // kHz below B, sat in B's lower guard. As loud as B, it made the strike "not narrowband": 2 of 16
  // strikes were missed in S2 and others detected 100-150 ms late. Its nearest guard bin is now
  // 47 110 - 1 375 = 45 735 Hz, 365 Hz above it (Hann main lobe: +-250 Hz).
  c.guardInnerHz = 1000.0f;
  c.guardOuterHz = 1375.0f;
  return c;
}

// Lines the strike spectrum measures after every click, whether or not they stand out (`SPEC_LINE`).
// Bar 38 is 63 mm long and bar 47 is 50 mm (d = 16 mm), each held by a rod through a hole in its
// middle. Uniform-cylinder theory (barlab theory --L 63mm / 50mm --d 16mm) puts them 3-11 % above the
// lines measured on 2026-10-23, so the theory candidates get wide tolerances. A centre support sits on
// the antinode of the even longitudinal modes, so ~2f is expected to be weak; ~2.9f (n = 3) is above
// Nyquist and only reaches the board folded back (192 kHz - 2.9f), through the decimator's stopband.
struct NamedLine {
  const char *name;
  spec::WatchLine line;
};

const NamedLine kWatch[] = {
    {"A0", {38500.0f, 300.0f}},    // bar 38: its band A line
    {"A1", {33900.0f, 500.0f}},    // bar 38: second line, 33.92 kHz on 2026-10-30
    {"A2", {76300.0f, 1500.0f}},   // bar 38: n = 2 longitudinal, ~1.976 f (Rayleigh-Love)
    {"A3x", {79500.0f, 1500.0f}},  // bar 38: n = 3 (~2.92 f = ~112.5 kHz) folded back
    {"B0", {47110.0f, 300.0f}},    // bar 47: its band B line
    {"B1", {45370.0f, 400.0f}},    // bar 47: second line, 45.37 kHz on 2026-10-23 and 2026-10-30
    {"B2", {92800.0f, 1500.0f}},   // bar 47: n = 2 longitudinal, ~1.97 f, near Nyquist
    {"BG38", {38000.0f, 150.0f}},  // background: the FSK-like 38.00 kHz source
    {"BG40", {40000.0f, 800.0f}},  // background: the continuous ~40 kHz line reported on 2026-10-30
    {"BG43", {42900.0f, 400.0f}},  // background: the 38.00 kHz source's faint 42.7-43.2 kHz line
};
const int kWatchCount = sizeof(kWatch) / sizeof(kWatch[0]);

// The watched line with that name (0 if none).
inline float watchHz(const char *name) {
  for (int i = 0; i < kWatchCount; i++) {
    const char *a = kWatch[i].name, *b = name;
    while (*a && *a == *b) a++, b++;
    if (*a == *b) return kWatch[i].line.hz;
  }
  return 0;
}

// Each bar's two lines and their frequency ratio, measured on 2026-10-30 with the other bar free or
// held (88 strikes, measurements/REPORT.md): 38 bar 33 917 / 38 418 Hz = 0.88313 (41 of 47 strikes
// within +-0.3 %), 47 bar 45 386 / 47 110 Hz = 0.96331 (38 of 41). The other bar's lines passed this
// check in 14 of 88 strikes, and then rang far weaker.
inline bardec::DecisionConfig decisionConfig(float sampleRateHz) {
  bardec::DecisionConfig c;
  c.sampleRateHz = sampleRateHz;
  c.bar[0] = {watchHz("A0"), watchHz("A1"), 0.88313f};
  c.bar[1] = {watchHz("B0"), watchHz("B1"), 0.96331f};
  return c;
}

inline spec::SpecConfig specConfig(float sampleRateHz) {
  spec::SpecConfig c;
  c.sampleRateHz = sampleRateHz;
  // Peaks and floor up to 62 kHz only: the mic's shaped modulator noise rises with frequency (quiet
  // room, 2026-10-30: ~-100 dBFS at 34-38 kHz, -90 at 45-47, -78 at 66-72, -70 at 83-93 kHz), and
  // against one median floor its bumps above ~65 kHz filled all the peak slots. Both bars' known lines
  // are at 33-48 kHz; the watched lines still reach 93 kHz.
  c.fMaxHz = 62000.0f;
  for (int i = 0; i < kWatchCount && i < spec::kMaxWatch; i++) c.watch[c.watchCount++] = kWatch[i].line;
  return c;
}

}  // namespace v3
