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
  kMax_ = std::min((int)floorf(cfg.fMaxHz * n / fs), n / 2 - 2);
  early_.assign(kMax_ - kMin_ + 1, 0.0f);
  late_.assign(kMax_ - kMin_ + 1, 0.0f);
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

bool StrikeSpectrum::push(const float *x, size_t n) {
  if (n == 0 || capture_.empty()) return false;  // not configured yet
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

  switch (state_) {
    case State::Learning:
      baseline_ += (e - baseline_) / ++learnChunks_;
      if (sampleCount_ >= learnUntil_) state_ = State::Armed;
      return false;
    case State::Holdoff:
      if (sampleCount_ >= holdUntil_) state_ = State::Armed;
      return false;
    case State::Armed:
      if (e > 0 && e >= baseline_ * pow(10.0, cfg_.triggerDb / 10.0)) {
        state_ = State::Capturing;
        captureFill_ = 0;
        result_ = SpecResult();
        result_.triggerSample = chunkStart;
        result_.triggerDb = (float)(10 * log10(e / (baseline_ > 1e-30 ? baseline_ : 1e-30)));
      } else {
        baseline_ += (1 - exp(-(double)n / (cfg_.baselineTauS * cfg_.sampleRateHz))) * (e - baseline_);
        return false;
      }
      // The trigger chunk is the start of the capture.
      // fall through
    case State::Capturing: {
      size_t take = std::min(n, capture_.size() - captureFill_);
      for (size_t i = 0; i < take; i++) {
        float v = x[i] * 32767.0f;
        capture_[captureFill_ + i] = (int16_t)lroundf(v > 32767.0f ? 32767.0f : (v < -32767.0f ? -32767.0f : v));
      }
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

// Hann-windowed spectrum of capture_[offset, offset + fftSize), as sine amplitude in dBFS.
void StrikeSpectrum::spectrumDb(int offset, std::vector<float> &db) {
  const int n = cfg_.fftSize, half = n / 2;
  for (int i = 0; i < n; i++) {
    float c = i < half ? cos_[i] : -cos_[i - half];  // cos(2 pi i / n)
    re_[i] = capture_[offset + i] * (1.0f / 32767.0f) * (0.5f - 0.5f * c);
    im_[i] = 0;
  }
  fftInPlace(re_.data(), im_.data(), n, cos_.data(), sin_.data());
  const float scale = 2.0f / half;  // sum of the periodic Hann window = n / 2
  for (int k = kMin_; k <= kMax_; k++) {
    float mag = scale * sqrtf(re_[k] * re_[k] + im_[k] * im_[k]);
    db[k - kMin_] = 20.0f * log10f(mag > 1e-12f ? mag : 1e-12f);
  }
}

void StrikeSpectrum::analyse() {
  spectrumDb(earlyOffset_, early_);
  spectrumDb(lateOffset_, late_);
  const int size = (int)early_.size();

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
