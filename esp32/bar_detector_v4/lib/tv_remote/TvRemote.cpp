#include "TvRemote.h"

namespace tv {

const char *commandName(Command command) {
  switch (command) {
    case Command::Power:
      return "power";
    case Command::Channel:
      return "channel";
    case Command::Boom:
      return "boom";
    default:
      return "none";
  }
}

// The strike's own band SNR at Detect (the stronger band if the decision left BOTH).
float Remote::strength(const bardec::Decision &d) {
  const float *p = d.event.peakSnrDb;
  if (d.band == bardet::Band::A) return p[0];
  if (d.band == bardet::Band::B) return p[1];
  return p[0] > p[1] ? p[0] : p[1];
}

void Remote::onDecision(const bardec::Decision &d, uint32_t nowMs) {
  bool weak = strength(d) < cfg_.weakSnrDb;
  if (weak && commanded_ && nowMs - lastCommandMs_ < cfg_.refractoryMs) return;  // an echo of the last strike
  if (waiting_ && strength(d) <= strength(pending_)) return;  // a weaker strike right after: ignored
  waiting_ = true;
  pending_ = d;
  sinceMs_ = nowMs;
  for (int b = 0; b < 2; b++) peak_[b] = d.event.peakSnrDb[b];
}

void Remote::onPeaks(const float peakSnrDb[2]) {
  if (!waiting_) return;
  for (int b = 0; b < 2; b++)
    if (peakSnrDb[b] > peak_[b]) peak_[b] = peakSnrDb[b];
}

Command Remote::poll(uint32_t nowMs) {
  if (!waiting_) return Command::None;
  uint32_t hold = strength(pending_) < cfg_.weakSnrDb ? cfg_.weakHoldMs : cfg_.holdMs;
  if (nowMs - sinceMs_ < hold) return Command::None;
  waiting_ = false;
  commanded_ = true;
  lastCommandMs_ = nowMs;
  bool both = pending_.band == bardet::Band::Both || (peak_[0] >= cfg_.bothSnrDb && peak_[1] >= cfg_.bothSnrDb);
  if (both) return Command::Boom;
  return pending_.band == bardet::Band::A ? Command::Power : Command::Channel;
}

TvState apply(TvState state, Command command, int channels) {
  switch (command) {
    case Command::Power:
      state.on = !state.on;
      break;
    case Command::Channel:
      if (state.on) state.channel = (state.channel + 1) % channels;
      break;
    default:
      break;
  }
  return state;
}

}  // namespace tv
