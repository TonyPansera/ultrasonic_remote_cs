#include "BarDecision.h"

#include <math.h>

namespace bardec {

namespace {

const float kNone = -240.0f;

const spec::SpecLine *findLine(const spec::SpecResult &r, float targetHz) {
  for (int i = 0; i < r.lineCount; i++)
    if (r.lines[i].targetHz == targetHz) return &r.lines[i];
  return nullptr;
}

// The line rose at the strike and stands over the capture's floor.
bool rang(const spec::SpecLine *line, const spec::SpecResult &r, const DecisionConfig &cfg) {
  return line && line->freqHz > 0 && line->earlyDbfs - line->beforeDbfs >= cfg.minRiseDb &&
         line->earlyDbfs - r.floorDbfs >= cfg.minLineDb;
}

float powerSumDb(float a, float b) {
  if (a <= kNone) return b;
  if (b <= kNone) return a;
  return 10.0f * log10f(powf(10.0f, a / 10.0f) + powf(10.0f, b / 10.0f));
}

BarEvidence evidence(const spec::SpecResult &r, const BarLines &bar, const DecisionConfig &cfg) {
  BarEvidence ev = {false, false, 0.0f, kNone};
  const spec::SpecLine *f = findLine(r, bar.fundamentalHz);
  const spec::SpecLine *o = findLine(r, bar.overtoneHz);
  ev.fundamental = rang(f, r, cfg);
  if (f && o && f->freqHz > 0 && o->freqHz > 0) ev.ratio = o->freqHz / f->freqHz;
  // Both lines must have rung for the ratio to mean anything.
  ev.overtone = ev.fundamental && rang(o, r, cfg) && fabsf(ev.ratio / bar.ratio - 1.0f) <= cfg.ratioTol;
  if (ev.fundamental) ev.lateDbfs = f->lateDbfs;
  if (ev.overtone) ev.lateDbfs = powerSumDb(ev.lateDbfs, o->lateDbfs);
  return ev;
}

}  // namespace

const char *reasonName(Reason reason) {
  switch (reason) {
    case Reason::Detector:
      return "detector";
    case Reason::Lines:
      return "lines";
    case Reason::Close:
      return "close";
    default:
      return "no_spectrum";
  }
}

Decision decide(const bardet::Event &event, const spec::SpecResult *spectrum, const DecisionConfig &cfg) {
  Decision d;
  d.event = event;
  d.detectorBand = event.band;
  d.band = event.band;
  d.spectrum = spectrum != nullptr;
  d.marginDb = 0;
  for (int b = 0; b < 2; b++) d.bar[b] = BarEvidence{false, false, 0.0f, kNone};
  if (spectrum) {
    for (int b = 0; b < 2; b++) d.bar[b] = evidence(*spectrum, cfg.bar[b], cfg);
    float a = d.bar[0].lateDbfs, b = d.bar[1].lateDbfs;
    d.marginDb = (a <= kNone && b <= kNone) ? 0.0f : a - b;
  }
  if (event.band != bardet::Band::Both) {
    d.reason = Reason::Detector;  // a colour is never changed
  } else if (!spectrum) {
    d.reason = Reason::NoSpectrum;
  } else if (d.marginDb >= cfg.marginDb) {
    d.band = bardet::Band::A;
    d.reason = Reason::Lines;
  } else if (d.marginDb <= -cfg.marginDb) {
    d.band = bardet::Band::B;
    d.reason = Reason::Lines;
  } else {
    d.reason = Reason::Close;
  }
  return d;
}

bool BarDecision::matches(const bardet::Event &event, const spec::SpecResult &result) const {
  // Forced captures (background surveys) are no strike.
  if (result.forced) return false;
  double dt = ((double)event.startSample - (double)result.triggerSample) / cfg_.sampleRateHz;
  return dt >= -cfg_.matchBeforeS && dt <= cfg_.matchAfterS;
}

int BarDecision::onDetect(const bardet::Event &event, Decision out[2]) {
  int n = 0;
  if (waiting_) {
    // The previous strike's capture never came: settle it without one.
    out[n++] = decide(event_, nullptr, cfg_);
    waiting_ = false;
  }
  for (int i = 0; i < recentCount_; i++) {
    if (matches(event, recent_[i])) {
      out[n++] = decide(event, &recent_[i], cfg_);
      return n;
    }
  }
  waiting_ = true;
  event_ = event;
  return n;
}

bool BarDecision::onSpectrum(const spec::SpecResult &result, Decision &out) {
  for (int i = kKeep - 1; i > 0; i--) recent_[i] = recent_[i - 1];
  recent_[0] = result;
  if (recentCount_ < kKeep) recentCount_++;
  if (waiting_ && matches(event_, result)) {
    waiting_ = false;
    out = decide(event_, &recent_[0], cfg_);
    return true;
  }
  return false;
}

bool BarDecision::expire(Decision &out) {
  if (!waiting_) return false;
  waiting_ = false;
  // A capture that arrived before the Detect and was pushed out of recent_ is lost: no spectrum.
  out = decide(event_, nullptr, cfg_);
  return true;
}

}  // namespace bardec
