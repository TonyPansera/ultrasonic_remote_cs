// Host tests for lib/bar_decision (`pio test -e native`).
//
// The table-driven tests replay the 88 strikes measured on the board on 2026-10-30 (strikes_2026_10_30.h,
// exported from measurements/ by tools/analyze_sessions.py), each with the bar that was struck. The
// other tests build captures by hand for the edge rules: no capture, a ratio mismatch, a steady line,
// the matching window, and the order in which events and captures arrive.

#include <BarDecision.h>
#include <DeviceConfig.h>
#include <unity.h>

#include <stdio.h>

#include "strikes_2026_10_30.h"

using bardec::BarDecision;
using bardec::Decision;
using bardec::Reason;
using bardet::Band;
using bardet::Event;
using bardet::Phase;

namespace {

const float FS = 192000.0f;
const bardec::DecisionConfig CFG = v3::decisionConfig(FS);

Band bandOf(char c) { return c == 'A' ? Band::A : (c == 'B' ? Band::B : Band::Both); }

spec::SpecLine line(float target, const LineVec &v) { return {target, v.f, v.early, v.late, v.before}; }

spec::SpecResult capture(const StrikeVec &s, uint64_t triggerSample = 100000) {
  spec::SpecResult r = {};
  r.triggerSample = triggerSample;
  r.floorDbfs = s.floor;
  r.lineCount = 4;
  r.lines[0] = line(CFG.bar[0].fundamentalHz, s.a0);
  r.lines[1] = line(CFG.bar[0].overtoneHz, s.a1);
  r.lines[2] = line(CFG.bar[1].fundamentalHz, s.b0);
  r.lines[3] = line(CFG.bar[1].overtoneHz, s.b1);
  return r;
}

Event detect(Band band, uint64_t startSample = 100000 + 1536) {
  Event e = {};
  e.phase = Phase::Detect;
  e.band = band;
  e.confirmed = true;
  e.startSample = startSample;
  return e;
}

// The decision for a real strike, paired the way the firmware pairs it: by the capture's and the
// event's start times (a capture outside the matching window is not used).
Decision pairAndDecide(const StrikeVec &s, Band detectorBand) {
  BarDecision dec(CFG);
  Decision out[2];
  dec.onSpectrum(capture(s, (uint64_t)(s.t * FS + 0.5f)), out[0]);
  if (dec.onDetect(detect(detectorBand, (uint64_t)(s.eventT * FS + 0.5f)), out) == 1) return out[0];
  dec.expire(out[0]);
  return out[0];
}

// A struck A (rings on: little drop, overtone at A's ratio) and a B that rang in sympathy (collapses).
StrikeVec syntheticA() {
  return {"synthetic", 0, 0, 'A', 'X', -75.0f,
          {38420.0f, -50.0f, -52.0f, -99.0f},
          {38420.0f * 0.88313f, -52.0f, -55.0f, -101.0f},
          {47120.0f, -52.0f, -80.0f, -93.0f},
          {45380.0f, -55.0f, -85.0f, -92.0f}};
}

void test_real_strikes_give_the_struck_bar() {
  int ok = 0, resolved = 0;
  for (int i = 0; i < kStrikeCount; i++) {
    const StrikeVec &s = kStrikes[i];
    Decision d = pairAndDecide(s, bandOf(s.detector));
    if (d.band == bandOf(s.truth)) ok++;
    if (s.detector == 'X') {
      resolved += d.band != Band::Both;
      printf("%s t=%.3f: detector BOTH -> %s (%s), evidence A %.1f B %.1f dBFS, margin %.1f dB\n", s.session, s.t,
             bardet::bandName(d.band), bardec::reasonName(d.reason), d.bar[0].lateDbfs, d.bar[1].lateDbfs, d.marginDb);
    }
  }
  printf("real strikes: %d of %d give the struck bar; %d BOTH settled\n", ok, kStrikeCount, resolved);
  TEST_ASSERT_EQUAL_INT(kStrikeCount, ok);
  TEST_ASSERT_EQUAL_INT(1, resolved);  // S5 #17: the one grey tile of the day
}

void test_real_strikes_lines_alone_never_name_the_wrong_bar() {
  // As if the detector had said BOTH every time: the lines alone settle the strike, or leave it BOTH,
  // but never pick the other bar.
  int right = 0, both = 0, wrong = 0, unpaired = 0, overtoneStruck = 0, overtoneOther = 0;
  for (int i = 0; i < kStrikeCount; i++) {
    const StrikeVec &s = kStrikes[i];
    Decision d = pairAndDecide(s, Band::Both);
    unpaired += !d.spectrum;
    int struck = s.truth == 'A' ? 0 : 1;
    overtoneStruck += d.bar[struck].overtone;
    overtoneOther += d.bar[1 - struck].overtone;
    if (d.band == bandOf(s.truth)) {
      right++;
    } else if (d.band == Band::Both) {
      both++;
      printf("left BOTH: %s t=%.3f evidence A %.1f B %.1f\n", s.session, s.t, d.bar[0].lateDbfs, d.bar[1].lateDbfs);
    } else {
      wrong++;
      printf("WRONG: %s t=%.3f evidence A %.1f B %.1f\n", s.session, s.t, d.bar[0].lateDbfs, d.bar[1].lateDbfs);
    }
  }
  printf("lines alone: %d right, %d BOTH (%d without a paired capture), %d wrong; overtone at the bar's ratio: "
         "struck %d, other %d of %d\n",
         right, both, unpaired, wrong, overtoneStruck, overtoneOther, kStrikeCount);
  TEST_ASSERT_EQUAL_INT(0, wrong);
  // Unpaired: S2's late events (64-144 ms after the capture, before the guard fix) and one S1b capture
  // that started 40 ms before its strike (before the onset trigger could restart it).
  TEST_ASSERT_TRUE(unpaired <= 8);
  TEST_ASSERT_EQUAL_INT(kStrikeCount - unpaired, right);
  TEST_ASSERT_TRUE(overtoneStruck >= 70);
  TEST_ASSERT_TRUE(overtoneOther <= 20);
}

void test_detector_colour_is_never_changed() {
  // A struck-A capture with the detector saying B: B stays (only BOTH is settled).
  StrikeVec s = syntheticA();
  spec::SpecResult r = capture(s);
  Decision d = bardec::decide(detect(Band::B), &r, CFG);
  TEST_ASSERT_EQUAL(Band::B, d.band);
  TEST_ASSERT_EQUAL(Reason::Detector, d.reason);
  TEST_ASSERT_TRUE(d.marginDb >= CFG.marginDb);  // the evidence is still reported
}

void test_both_settled_by_lines() {
  StrikeVec s = syntheticA();
  spec::SpecResult r = capture(s);
  Decision d = bardec::decide(detect(Band::Both), &r, CFG);
  TEST_ASSERT_EQUAL(Band::A, d.band);
  TEST_ASSERT_EQUAL(Reason::Lines, d.reason);
  TEST_ASSERT_TRUE(d.bar[0].fundamental && d.bar[0].overtone);
  TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.88313f, d.bar[0].ratio);
}

void test_no_capture_keeps_both() {
  Decision d = bardec::decide(detect(Band::Both), nullptr, CFG);
  TEST_ASSERT_EQUAL(Band::Both, d.band);
  TEST_ASSERT_EQUAL(Reason::NoSpectrum, d.reason);
}

void test_overtone_off_ratio_does_not_count() {
  // A's "overtone" 0.6 % off A's ratio (e.g. another source near 33.9 kHz): it is not A's, so it does
  // not count. Both fundamentals then ring alike: BOTH stays.
  StrikeVec s = syntheticA();
  s.a1.f = s.a0.f * 0.88313f * 1.006f;
  s.b0.late = s.a0.late + 3.0f;  // the fundamentals alone are within the margin
  spec::SpecResult r = capture(s);
  Decision d = bardec::decide(detect(Band::Both), &r, CFG);
  TEST_ASSERT_FALSE(d.bar[0].overtone);
  TEST_ASSERT_EQUAL(Band::Both, d.band);
  TEST_ASSERT_EQUAL(Reason::Close, d.reason);
  // At the right ratio, the same overtone settles it.
  s.a1.f = s.a0.f * 0.88313f;
  s.a1.late = s.b0.late + 12.0f;
  r = capture(s);
  d = bardec::decide(detect(Band::Both), &r, CFG);
  TEST_ASSERT_TRUE(d.bar[0].overtone);
  TEST_ASSERT_EQUAL(Band::A, d.band);
}

void test_steady_background_line_does_not_count() {
  // A steady line sitting on B's fundamental (as loud before the click as after it) must not make B
  // look struck: it did not rise at the strike.
  StrikeVec s = syntheticA();
  s.b0 = {47120.0f, -45.0f, -45.0f, -45.2f};  // steady: before = early = late
  s.b1 = {47120.0f * 0.96331f, -46.0f, -46.0f, -46.1f};
  spec::SpecResult r = capture(s);
  Decision d = bardec::decide(detect(Band::Both), &r, CFG);
  TEST_ASSERT_FALSE(d.bar[1].fundamental);
  TEST_ASSERT_FALSE(d.bar[1].overtone);
  TEST_ASSERT_EQUAL(Band::A, d.band);
}

void test_lines_missing_from_the_capture_keep_both() {
  // `watch clear` on the board: the capture has no watched lines.
  StrikeVec s = syntheticA();
  spec::SpecResult r = capture(s);
  r.lineCount = 0;
  Decision d = bardec::decide(detect(Band::Both), &r, CFG);
  TEST_ASSERT_EQUAL(Band::Both, d.band);
  TEST_ASSERT_EQUAL(Reason::Close, d.reason);
}

void test_capture_after_detect_is_paired() {
  BarDecision dec(CFG);
  Decision out[2];
  TEST_ASSERT_EQUAL_INT(0, dec.onDetect(detect(Band::Both, 100000 + 1536), out));
  TEST_ASSERT_TRUE(dec.waiting());
  StrikeVec s = syntheticA();
  TEST_ASSERT_TRUE(dec.onSpectrum(capture(s, 100000), out[0]));
  TEST_ASSERT_EQUAL(Band::A, out[0].band);
  TEST_ASSERT_TRUE(out[0].spectrum);
  TEST_ASSERT_FALSE(dec.waiting());
}

void test_capture_before_detect_is_paired() {
  BarDecision dec(CFG);
  Decision out[2];
  StrikeVec s = syntheticA();
  TEST_ASSERT_FALSE(dec.onSpectrum(capture(s, 100000), out[0]));
  TEST_ASSERT_EQUAL_INT(1, dec.onDetect(detect(Band::Both, 100000 + 4 * 1536), out));
  TEST_ASSERT_EQUAL(Band::A, out[0].band);
}

void test_capture_of_another_strike_is_not_paired() {
  // A capture 0.5 s earlier (handling noise, the previous strike's tail) belongs to something else.
  BarDecision dec(CFG);
  Decision out[2];
  StrikeVec s = syntheticA();
  TEST_ASSERT_FALSE(dec.onSpectrum(capture(s, 100000), out[0]));
  TEST_ASSERT_EQUAL_INT(0, dec.onDetect(detect(Band::Both, 100000 + 96000), out));
  TEST_ASSERT_TRUE(dec.expire(out[0]));
  TEST_ASSERT_EQUAL(Band::Both, out[0].band);
  TEST_ASSERT_EQUAL(Reason::NoSpectrum, out[0].reason);
  // A forced (background survey) capture right at the strike is not paired either.
  spec::SpecResult forced = capture(s, 200000);
  forced.forced = true;
  TEST_ASSERT_EQUAL_INT(0, dec.onDetect(detect(Band::Both, 200000 + 1536), out));
  TEST_ASSERT_FALSE(dec.onSpectrum(forced, out[0]));
  TEST_ASSERT_TRUE(dec.waiting());
}

void test_second_detect_settles_the_waiting_one() {
  BarDecision dec(CFG);
  Decision out[2];
  TEST_ASSERT_EQUAL_INT(0, dec.onDetect(detect(Band::A, 100000), out));
  StrikeVec s = syntheticA();
  TEST_ASSERT_FALSE(dec.onSpectrum(capture(s, 300000), out[0]));  // not the first strike's capture
  // The second strike's Detect: the first is settled without a capture, the second pairs at once.
  TEST_ASSERT_EQUAL_INT(2, dec.onDetect(detect(Band::Both, 300000 + 1536), out));
  TEST_ASSERT_EQUAL(Band::A, out[0].band);
  TEST_ASSERT_EQUAL(Reason::Detector, out[0].reason);
  TEST_ASSERT_FALSE(out[0].spectrum);
  TEST_ASSERT_EQUAL(Band::A, out[1].band);
  TEST_ASSERT_EQUAL(Reason::Lines, out[1].reason);
  TEST_ASSERT_FALSE(dec.waiting());
}

}  // namespace

void setUp() {}
void tearDown() {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_real_strikes_give_the_struck_bar);
  RUN_TEST(test_real_strikes_lines_alone_never_name_the_wrong_bar);
  RUN_TEST(test_detector_colour_is_never_changed);
  RUN_TEST(test_both_settled_by_lines);
  RUN_TEST(test_no_capture_keeps_both);
  RUN_TEST(test_overtone_off_ratio_does_not_count);
  RUN_TEST(test_steady_background_line_does_not_count);
  RUN_TEST(test_lines_missing_from_the_capture_keep_both);
  RUN_TEST(test_capture_after_detect_is_paired);
  RUN_TEST(test_capture_before_detect_is_paired);
  RUN_TEST(test_capture_of_another_strike_is_not_paired);
  RUN_TEST(test_second_detect_settles_the_waiting_one);
  return UNITY_END();
}
