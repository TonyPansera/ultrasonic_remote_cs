// Host tests for lib/tv_remote (`pio test -e native`).
//
// The replay tests feed the strikes of the 2026-10-30 sessions (strikes_remote.h, exported from
// bar_detector_v3/measurements/ by tools/analyze_sessions.py --export-remote) to the remote, in order and
// on their own clock, and check the commands against what the user struck. The board's per-band peaks
// are only known at Detect and at End there, so each strike's End peaks stand for the peaks reached
// within the hold (to be checked on the board: the explosion needs the second bar's peak within it).

#include <TvRemote.h>
#include <unity.h>

#include <stdio.h>

#include <string>

#include "strikes_remote.h"

using bardet::Band;
using tv::Command;
using tv::Remote;
using tv::TvState;

namespace {

bardec::Decision decision(Band band, float snrA, float snrB) {
  bardec::Decision d = {};
  d.band = band;
  d.detectorBand = band;
  d.event.band = band;
  d.event.peakSnrDb[0] = snrA;
  d.event.peakSnrDb[1] = snrB;
  return d;
}

char letter(Command c) {
  switch (c) {
    case Command::Power:
      return 'P';
    case Command::Channel:
      return 'C';
    case Command::Boom:
      return 'B';
    default:
      return '-';
  }
}

// Polls every 10 ms from *now up to untilMs, appending the commands.
void advance(Remote &r, uint32_t &now, uint32_t untilMs, std::string &out) {
  for (; now < untilMs; now += 10) {
    Command c = r.poll(now);
    if (c != Command::None) out += letter(c);
  }
}

std::string replay(const RemoteSession &s) {
  Remote r;
  std::string out;
  uint32_t now = (uint32_t)(s.strikes[0].t * 1000);
  for (int i = 0; i < s.count; i++) {
    const RemoteStrike &k = s.strikes[i];
    uint32_t t = (uint32_t)(k.t * 1000);
    advance(r, now, t, out);
    Band band = k.band == 'A' ? Band::A : (k.band == 'B' ? Band::B : Band::Both);
    r.onDecision(decision(band, k.snrA, k.snrB), t);
    const float end[2] = {k.endSnrA, k.endSnrB};
    r.onPeaks(end);
  }
  advance(r, now, now + 2000, out);
  return out;
}

void test_replay_sessions_give_the_commands_struck() {
  for (int i = 0; i < kRemoteSessionCount; i++) {
    const RemoteSession &s = kRemoteSessions[i];
    std::string got = replay(s);
    printf("%-10s %2d strikes -> %s\n           expected   %s\n", s.name, s.count, got.c_str(), s.expected);
    TEST_ASSERT_EQUAL_STRING_MESSAGE(s.expected, got.c_str(), s.name);
  }
}

void test_strike_acts_after_its_hold() {
  Remote r;
  r.onDecision(decision(Band::A, 55, 10), 1000);
  TEST_ASSERT_EQUAL(Command::None, r.poll(1290));
  TEST_ASSERT_EQUAL(Command::Power, r.poll(1300));
  TEST_ASSERT_EQUAL(Command::None, r.poll(1310));
}

void test_weak_strike_replaced_by_stronger_one() {
  // S7 #30: a weak 38 (26.6 dB), then the real 47 strike 0.4 s later.
  Remote r;
  r.onDecision(decision(Band::A, 26.6f, 0), 1000);
  TEST_ASSERT_EQUAL(Command::None, r.poll(1300));  // a weak strike waits 500 ms
  r.onDecision(decision(Band::B, 23.8f, 57.0f), 1400);
  TEST_ASSERT_EQUAL(Command::None, r.poll(1600));
  TEST_ASSERT_EQUAL(Command::Channel, r.poll(1700));
  TEST_ASSERT_EQUAL(Command::None, r.poll(2500));
}

void test_weak_echo_after_command_is_dropped() {
  // S3: a weak 38 (22 dB) 0.5 s after a strong one.
  Remote r;
  r.onDecision(decision(Band::A, 42.8f, 10.5f), 1000);
  TEST_ASSERT_EQUAL(Command::Power, r.poll(1300));
  r.onDecision(decision(Band::A, 22.4f, 0), 1512);
  TEST_ASSERT_FALSE(r.waiting());
  // A strong strike right after is a command of its own.
  r.onDecision(decision(Band::A, 50, 0), 1600);
  TEST_ASSERT_EQUAL(Command::Power, r.poll(1900));
  // A weak one once the refractory time is over counts too.
  r.onDecision(decision(Band::B, 0, 30), 2800);
  TEST_ASSERT_EQUAL(Command::Channel, r.poll(3300));
}

void test_both_bands_ringing_clean_is_an_explosion() {
  // S6b: the detector decided on the first bar; the second rang clean at 48-60 dB within the strike.
  Remote r;
  r.onDecision(decision(Band::A, 64.0f, 0), 1000);
  const float peaks[2] = {64.0f, 60.4f};
  r.onPeaks(peaks);
  TEST_ASSERT_EQUAL(Command::Boom, r.poll(1300));
}

void test_sympathetic_ringing_is_no_explosion() {
  // The loudest sympathetic ring of the 111 single strikes: 40.5 dB (S5 #17, a 38).
  Remote r;
  r.onDecision(decision(Band::A, 45.4f, 40.5f), 1000);
  const float peaks[2] = {45.4f, 40.5f};
  r.onPeaks(peaks);
  TEST_ASSERT_EQUAL(Command::Power, r.poll(1300));
}

void test_decision_left_both_is_an_explosion() {
  Remote r;
  r.onDecision(decision(Band::Both, 50, 48), 1000);
  TEST_ASSERT_EQUAL(Command::Boom, r.poll(1300));
}

void test_peaks_of_an_earlier_strike_do_not_leak() {
  // Peaks only count for the strike waiting; with none waiting, they are ignored.
  Remote r;
  const float peaks[2] = {60, 60};
  r.onPeaks(peaks);
  r.onDecision(decision(Band::B, 5, 55), 1000);
  TEST_ASSERT_EQUAL(Command::Channel, r.poll(1300));
}

void test_tv_state() {
  TvState s;
  TEST_ASSERT_FALSE(s.on);
  s = tv::apply(s, Command::Channel, 5);  // off: channels do nothing
  TEST_ASSERT_EQUAL_INT(0, s.channel);
  s = tv::apply(s, Command::Power, 5);
  TEST_ASSERT_TRUE(s.on);
  for (int i = 0; i < 4; i++) s = tv::apply(s, Command::Channel, 5);
  TEST_ASSERT_EQUAL_INT(4, s.channel);
  s = tv::apply(s, Command::Channel, 5);  // wraps
  TEST_ASSERT_EQUAL_INT(0, s.channel);
  s = tv::apply(s, Command::Boom, 5);  // drawn over the state, changes nothing
  TEST_ASSERT_TRUE(s.on);
  TEST_ASSERT_EQUAL_INT(0, s.channel);
  s = tv::apply(s, Command::Power, 5);
  TEST_ASSERT_FALSE(s.on);
  TEST_ASSERT_EQUAL_INT(0, s.channel);  // the channel is kept for the next power-on
}

}  // namespace

void setUp() {}
void tearDown() {}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_replay_sessions_give_the_commands_struck);
  RUN_TEST(test_strike_acts_after_its_hold);
  RUN_TEST(test_weak_strike_replaced_by_stronger_one);
  RUN_TEST(test_weak_echo_after_command_is_dropped);
  RUN_TEST(test_both_bands_ringing_clean_is_an_explosion);
  RUN_TEST(test_sympathetic_ringing_is_no_explosion);
  RUN_TEST(test_decision_left_both_is_an_explosion);
  RUN_TEST(test_peaks_of_an_earlier_strike_do_not_leak);
  RUN_TEST(test_tv_state);
  return UNITY_END();
}
