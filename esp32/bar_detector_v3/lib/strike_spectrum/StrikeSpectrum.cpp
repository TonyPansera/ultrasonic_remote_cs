#include "StrikeSpectrum.h"

#include <math.h>
#include <stdlib.h>

#include <algorithm>

namespace spec {

namespace {

const double kPi = 3.14159265358979323846;

}  // namespace

void fftInPlace(float *re, float *im, int n, const float *cosT, const float *sinT) {
  for (int i = 1, j = 0; i < n; i++) {
    int bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      std::swap(re[i], re[j]);
      std::swap(im[i], im[j]);
    }
  }
  for (int len = 2; len <= n; len <<= 1) {
    const int half = len >> 1, step = n / len;
    for (int i = 0; i < n; i += len) {
      for (int k = 0; k < half; k++) {
        const float wr = cosT[k * step], wi = -sinT[k * step];
        const int a = i + k, b = a + half;
        const float tr = re[b] * wr - im[b] * wi;
        const float ti = re[b] * wi + im[b] * wr;
        re[b] = re[a] - tr;
        im[b] = im[a] - ti;
        re[a] += tr;
        im[a] += ti;
      }
    }
  }
}

StrikeSpectrum::StrikeSpectrum() {}

StrikeSpectrum::StrikeSpectrum(const SpecConfig &cfg) { configure(cfg); }

void StrikeSpectrum::configure(const SpecConfig &cfg) {
  cfg_ = cfg;
  if (cfg_.maxPeaks > kMaxPeaks) cfg_.maxPeaks = kMaxPeaks;
  setWatch(cfg.watch, cfg.watchCount);
  const int n = cfg.fftSize;
  const float fs = cfg.sampleRateHz;
  cos_.resize(n / 2);
  sin_.resize(n / 2);
  for (int k = 0; k < n / 2; k++) {
    cos_[k] = (float)cos(2 * kPi * k / n);
    sin_[k] = (float)sin(2 * kPi * k / n);
  }
  re_.assign(n, 0.0f);
  im_.assign(n, 0.0f);
  kMin_ = (int)ceilf(cfg.fMinHz * n / fs);
  kTop_ = n / 2 - 2;
  kMax_ = std::min((int)floorf(cfg.fMaxHz * n / fs), kTop_);
  early_.assign(kTop_ - kMin_ + 1, 0.0f);
  late_.assign(kTop_ - kMin_ + 1, 0.0f);
  before_.assign(kTop_ - kMin_ + 1, -240.0f);
  history_.assign(n, 0);
  historyPos_ = 0;
  earlyOffset_ = (int)lroundf(cfg.earlyMs * 1e-3f * fs);
  lateOffset_ = (int)lroundf(cfg.lateMs * 1e-3f * fs);
  capture_.assign((size_t)(std::max(earlyOffset_, lateOffset_) + n), 0);
  reset();
}

void StrikeSpectrum::reset() {
  state_ = State::Learning;
  prev1_ = prev2_ = 0;
  baseline_ = 0;
  learnChunks_ = 0;
  learnUntil_ = sampleCount_ + (uint64_t)(cfg_.learnS * cfg_.sampleRateHz);
  captureFill_ = 0;
  result_ = SpecResult();
}

void StrikeSpectrum::setWatch(const WatchLine *lines, int count) {
  if (count < 0) count = 0;
  if (count > kMaxWatch) count = kMaxWatch;
  for (int i = 0; i < count; i++) cfg_.watch[i] = lines[i];
  cfg_.watchCount = count;
}

bool StrikeSpectrum::push(const float *x, size_t n) {
  if (n == 0 || capture_.empty()) return false;  // not configured yet
  const bool done = step(x, n);
  // The last fftSize samples, whatever the state: the "before" window of the next capture.
  for (size_t i = 0; i < n; i++) {
    history_[historyPos_] = toPcm16(x[i]);
    if (++historyPos_ == history_.size()) historyPos_ = 0;
  }
  return done;
}

bool StrikeSpectrum::step(const float *x, size_t n) {
  // Ultrasonic energy: mean square of the second difference. In float: the ESP32 has no double FPU,
  // and a double accumulator here cost ~16 % of a core.
  float acc = 0;
  float p1 = prev1_, p2 = prev2_;
  for (size_t i = 0; i < n; i++) {
    float d = x[i] - 2 * p1 + p2;
    acc += d * d;
    p2 = p1;
    p1 = x[i];
  }
  prev1_ = p1;
  prev2_ = p2;
  const double e = (double)acc / n;
  const uint64_t chunkStart = sampleCount_;
  sampleCount_ += n;
  const bool external = triggerPending_;
  triggerPending_ = false;  // only ever for this chunk
  // An outside onset outranks a holdoff and a capture started two or more chunks earlier: a knock just
  // before the strike (handling, the mechanism) must not take the strike's capture. A capture started
  // in the previous chunk is kept: that is the click, one chunk before the band rises.
  if (external && (state_ == State::Holdoff || (state_ == State::Capturing && captureFill_ >= 2 * n)))
    state_ = State::Armed;

  switch (state_) {
    case State::Learning:
      baseline_ += (e - baseline_) / ++learnChunks_;
      if (sampleCount_ >= learnUntil_) state_ = State::Armed;
      return false;
    case State::Holdoff:
      if (sampleCount_ >= holdUntil_) state_ = State::Armed;
      return false;
    case State::Armed: {
      const bool rose = e > 0 && e >= baseline_ * pow(10.0, cfg_.triggerDb / 10.0);
      if (forcePending_ || rose || external) {
        state_ = State::Capturing;
        captureFill_ = 0;
        result_ = SpecResult();
        result_.triggerSample = chunkStart;
        result_.triggerDb = (float)(10 * log10((e > 1e-30 ? e : 1e-30) / (baseline_ > 1e-30 ? baseline_ : 1e-30)));
        result_.forced = forcePending_;
        result_.external = !forcePending_ && !rose;
        forcePending_ = false;
        // The fftSize samples just before this chunk (the click is in it): one FFT now, two later.
        spectrumDb(history_, historyPos_, before_);
      } else {
        baseline_ += (1 - exp(-(double)n / (cfg_.baselineTauS * cfg_.sampleRateHz))) * (e - baseline_);
        return false;
      }
    }
      // The trigger chunk is the start of the capture.
      // fall through
    case State::Capturing: {
      size_t take = std::min(n, capture_.size() - captureFill_);
      for (size_t i = 0; i < take; i++) capture_[captureFill_ + i] = toPcm16(x[i]);
      captureFill_ += take;
      if (captureFill_ < capture_.size()) return false;
      analyse();
      state_ = State::Holdoff;
      holdUntil_ = sampleCount_ + (uint64_t)(cfg_.holdoffMs * 1e-3f * cfg_.sampleRateHz);
      return true;
    }
  }
  return false;
}

// Hann-windowed spectrum of buf[start, start + fftSize) (wrapping round buf), as sine amplitude in dBFS.
void StrikeSpectrum::spectrumDb(const std::vector<int16_t> &buf, size_t start, std::vector<float> &db) {
  const int n = cfg_.fftSize, half = n / 2;
  const size_t size = buf.size();
  for (int i = 0; i < n; i++) {
    float c = i < half ? cos_[i] : -cos_[i - half];  // cos(2 pi i / n)
    re_[i] = buf[(start + i) % size] * (1.0f / 32767.0f) * (0.5f - 0.5f * c);
    im_[i] = 0;
  }
  fftInPlace(re_.data(), im_.data(), n, cos_.data(), sin_.data());
  const float scale = 2.0f / half;  // sum of the periodic Hann window = n / 2
  for (int k = kMin_; k <= kTop_; k++) {
    float mag = scale * sqrtf(re_[k] * re_[k] + im_[k] * im_[k]);
    db[k - kMin_] = 20.0f * log10f(mag > 1e-12f ? mag : 1e-12f);
  }
}

void StrikeSpectrum::analyse() {
  spectrumDb(capture_, earlyOffset_, early_);
  spectrumDb(capture_, lateOffset_, late_);
  const int size = kMax_ - kMin_ + 1;  // floor and peaks stop at fMaxHz

  // Median of the early spectrum, using re_ as scratch.
  std::copy(early_.begin(), early_.end(), re_.begin());
  std::nth_element(re_.begin(), re_.begin() + size / 2, re_.begin() + size);
  result_.floorDbfs = re_[size / 2];
  const float threshold = result_.floorDbfs + cfg_.minProminenceDb;

  int picked[kMaxPeaks];
  result_.count = 0;
  while (result_.count < cfg_.maxPeaks) {
    int best = -1;
    for (int k = 1; k < size - 1; k++) {
      if (early_[k] < threshold || early_[k] <= early_[k - 1] || early_[k] < early_[k + 1]) continue;
      if (best >= 0 && early_[k] <= early_[best]) continue;
      bool near = false;
      for (int p = 0; p < result_.count && !near; p++) near = abs(k - picked[p]) <= cfg_.guardBins;
      if (!near) best = k;
    }
    if (best < 0) break;
    float delta;
    SpecPeak &peak = result_.peaks[result_.count];
    peak.earlyDbfs = parabolicPeak(early_, best, delta);
    peak.freqHz = (kMin_ + best + delta) * cfg_.sampleRateHz / cfg_.fftSize;
    // The same line in the late window: its highest bin within 2 bins, interpolated the same way, so
    // that both levels are free of the window's scalloping loss (up to 1.4 dB with Hann).
    int lateBest = best;
    for (int k = std::max(1, best - 2); k <= std::min(size - 2, best + 2); k++)
      if (late_[k] > late_[lateBest]) lateBest = k;
    float lateDelta;
    peak.lateDbfs = parabolicPeak(late_, lateBest, lateDelta);
    picked[result_.count++] = best;
  }

  result_.lineCount = cfg_.watchCount;
  for (int i = 0; i < cfg_.watchCount; i++) measureLine(cfg_.watch[i], result_.lines[i]);
}

// The watched line's highest early bin within hz +- tolHz (at least the nearest bin), interpolated,
// and the late spectrum's highest bin within 2 of it.
void StrikeSpectrum::measureLine(const WatchLine &w, SpecLine &line) const {
  const float binsPerHz = cfg_.fftSize / cfg_.sampleRateHz;
  const int size = (int)early_.size();
  const int centre = (int)lroundf(w.hz * binsPerHz) - kMin_;
  int lo = std::min(centre, (int)ceilf((w.hz - w.tolHz) * binsPerHz) - kMin_);
  int hi = std::max(centre, (int)floorf((w.hz + w.tolHz) * binsPerHz) - kMin_);
  lo = std::max(lo, 1);
  hi = std::min(hi, size - 2);
  line.targetHz = w.hz;
  if (lo > hi) {  // outside the analysed spectrum
    line.freqHz = 0;
    line.earlyDbfs = line.lateDbfs = line.beforeDbfs = -240.0f;
    return;
  }
  int best = lo;
  for (int k = lo + 1; k <= hi; k++)
    if (early_[k] > early_[best]) best = k;
  float delta;
  line.earlyDbfs = parabolicPeak(early_, best, delta);
  line.freqHz = (kMin_ + best + delta) * cfg_.sampleRateHz / cfg_.fftSize;
  line.lateDbfs = levelNear(late_, best);
  line.beforeDbfs = levelNear(before_, best);
}

// The highest bin of db within 2 of bin k, interpolated: the same line in another window.
float StrikeSpectrum::levelNear(const std::vector<float> &db, int k) const {
  const int size = (int)db.size();
  int best = k;
  for (int j = std::max(1, k - 2); j <= std::min(size - 2, k + 2); j++)
    if (db[j] > db[best]) best = j;
  float delta;
  return parabolicPeak(db, best, delta);
}

// Peak value of the parabola through db[k - 1], db[k], db[k + 1]; delta is its offset from k in bins.
float StrikeSpectrum::parabolicPeak(const std::vector<float> &db, int k, float &delta) {
  const float a = db[k - 1], b = db[k], c = db[k + 1];
  const float den = a - 2 * b + c;
  delta = den < 0 ? 0.5f * (a - c) / den : 0.0f;
  if (delta > 0.5f || delta < -0.5f) delta = 0;  // not a local maximum: keep the bin
  return b - 0.25f * (a - c) * delta;
}

}  // namespace spec
