#include "ToneDetector.h"

#include <math.h>

namespace bardet {

namespace {

const float kPi = 3.14159265358979f;
const float kTiny = 1e-30f;

float toDb(float power) { return 10.0f * log10f(power > kTiny ? power : kTiny); }

// |X|^2 of a Goertzel recurrence (state s1, s2 after the last sample), times scale.
inline float goertzelPower(float c, float s1, float s2, float scale) {
  float p = (s1 * s1 + s2 * s2 - c * s1 * s2) * scale;
  return p > kTiny ? p : kTiny;
}

}  // namespace

const char *bandName(Band band) {
  switch (band) {
    case Band::A:
      return "A";
    case Band::B:
      return "B";
    case Band::Both:
      return "BOTH";
    default:
      return "NONE";
  }
}

ToneDetector::ToneDetector() { configure(Config()); }

ToneDetector::ToneDetector(const Config &cfg) { configure(cfg); }

void ToneDetector::setCallback(EventCallback cb, void *ctx) {
  callback_ = cb;
  callbackCtx_ = ctx;
}

void ToneDetector::addBin(float freqHz) {
  Bin bin;
  bin.freqHz = freqHz;
  bin.coeff = 2.0f * cosf(2.0f * kPi * freqHz / cfg_.sampleRateHz);
  bins_.push_back(bin);
}

void ToneDetector::configure(const Config &cfg) {
  cfg_ = cfg;
  n_ = cfg.blockSize;
  binHz_ = cfg.sampleRateHz / n_;
  blockMs_ = 1000.0f * n_ / cfg.sampleRateHz;

  window_.assign(n_, 0.0f);
  float windowSum = 0;
  for (int i = 0; i < n_; i++) {
    window_[i] = 0.5f - 0.5f * cosf(2.0f * kPi * i / n_);
    windowSum += window_[i];
  }
  // A sine of amplitude a gives |X| = a/2 * sum(w) at its frequency.
  powerScale_ = 4.0f / (windowSum * windowSum);
  block_.assign(n_, 0.0f);

  bins_.clear();
  for (int b = 0; b < 2; b++) {
    int half = (int)ceilf(cfg.toleranceHz / binHz_);
    bandFirst_[b] = (int)bins_.size();
    for (int k = -half; k <= half; k++) addBin(cfg.centerHz[b] + k * binHz_);
    bandCount_[b] = 2 * half + 1;
    int perSide = (int)floorf((cfg.guardOuterHz - cfg.guardInnerHz) / binHz_) + 1;
    guardFirst_[b] = (int)bins_.size();
    for (int side = -1; side <= 1; side += 2) {
      for (int j = 0; j < perSide; j++) addBin(cfg.centerHz[b] + side * (cfg.guardInnerHz + j * binHz_));
    }
    guardCount_[b] = 2 * perSide;
  }
  power_.assign(bins_.size(), 0.0f);
  baseline_.assign(bins_.size(), 0.0f);

  float blockS = n_ / cfg.sampleRateHz;
  alphaFast_ = 1.0f - expf(-blockS / cfg.noiseTauS);
  alphaSlow_ = 1.0f - expf(-blockS / cfg.noiseSlowTauS);
  learnTarget_ = (int)lroundf(cfg.learnS / blockS);
  if (learnTarget_ < 1) learnTarget_ = 1;

  fill_ = 0;
  blockClipped_ = false;
  reset();
}

void ToneDetector::reset() {
  learning_ = true;
  learnBlocks_ = 0;
  for (size_t j = 0; j < baseline_.size(); j++) baseline_[j] = 0.0f;
  active_ = false;
  for (int b = 0; b < 2; b++) {
    run_[b] = 0;
    snr_[b] = 0;
  }
  levels_ = Levels();
  levels_.learning = true;
}

void ToneDetector::push(const float *x, size_t n, bool clipped) {
  size_t i = 0;
  while (i < n) {
    size_t take = n - i;
    if (take > (size_t)n_ - fill_) take = (size_t)n_ - fill_;
    for (size_t k = 0; k < take; k++) block_[fill_ + k] = x[i + k];
    fill_ += take;
    i += take;
    blockClipped_ = blockClipped_ || clipped;
    if (fill_ == (size_t)n_) {
      processBlock();
      fill_ = 0;
      blockClipped_ = false;
    }
  }
}

// Parabolic interpolation on the dB power of the neighbouring bins of band bin k.
float ToneDetector::interpolatedFreq(int band, int k) const {
  int first = bandFirst_[band];
  float f = bins_[first + k].freqHz;
  if (k <= 0 || k >= bandCount_[band] - 1) return f;
  float a = toDb(power_[first + k - 1]);
  float m = toDb(power_[first + k]);
  float c = toDb(power_[first + k + 1]);
  float den = a - 2.0f * m + c;
  if (den >= 0) return f;  // k is not a local maximum of power
  float p = 0.5f * (a - c) / den;
  if (p > 0.5f) p = 0.5f;
  if (p < -0.5f) p = -0.5f;
  return f + p * binHz_;
}

void ToneDetector::updateBaselines() {
  float gate = powf(10.0f, cfg_.thresholdDb / 10.0f);
  for (size_t j = 0; j < bins_.size(); j++) {
    // A bin far above its baseline (an onset, a click, a burst of the FSK remote) would drag a linear
    // average up by orders of magnitude even at the slow rate, so it only pulls towards +3 dB.
    float limit = baseline_[j] * gate;
    if (power_[j] < limit) {
      baseline_[j] += alphaFast_ * (power_[j] - baseline_[j]);
    } else {
      baseline_[j] += alphaSlow_ * baseline_[j];
    }
    if (baseline_[j] < kTiny) baseline_[j] = kTiny;
  }
}

// Least-squares line through the band's last confirmBlocks clean SNRs (dB): a ringdown falls steadily.
bool ToneDetector::ringsDown(int band) const {
  int m = cfg_.confirmBlocks < kMaxConfirmBlocks ? cfg_.confirmBlocks : kMaxConfirmBlocks;
  if (m < 2 || historyCount_[band] < m) return false;
  float y[kMaxConfirmBlocks];
  for (int i = 0; i < m; i++) y[i] = history_[band][(historyCount_[band] - m + i) % kMaxConfirmBlocks];
  float xMean = (m - 1) / 2.0f;
  float yMean = 0;
  for (int i = 0; i < m; i++) yMean += y[i];
  yMean /= m;
  float sxy = 0, sxx = 0, syy = 0;
  for (int i = 0; i < m; i++) {
    sxy += (i - xMean) * (y[i] - yMean);
    sxx += (i - xMean) * (i - xMean);
    syy += (y[i] - yMean) * (y[i] - yMean);
  }
  if (syy <= 0) return false;
  float slope = sxy / sxx;
  float r2 = sxy * sxy / (sxx * syy);
  return slope <= -cfg_.minDecayDbPerBlock && r2 >= cfg_.minDecayR2;
}

void ToneDetector::emit(Phase phase, bool steady) {
  Event e;
  e.phase = phase;
  int winner = peakSnr_[0] >= peakSnr_[1] ? 0 : 1;
  e.marginDb = peakSnr_[winner] - peakSnr_[1 - winner];
  e.band = e.marginDb >= cfg_.decisionMarginDb ? (winner == 0 ? Band::A : Band::B) : Band::Both;
  e.confirmed = confirmed_;
  e.steady = steady;
  e.clipped = eventClipped_;
  for (int b = 0; b < 2; b++) {
    e.freqHz[b] = peakFreq_[b];
    e.peakSnrDb[b] = peakSnr_[b];
  }
  uint64_t lastBlock = phase == Phase::End ? lastAboveBlock_ : blockIndex_;
  e.durationMs = (float)(lastBlock - startBlock_ + 1) * blockMs_;
  e.startSample = startBlock_ * (uint64_t)n_;
  if (callback_) callback_(e, callbackCtx_);
}

void ToneDetector::closeEvent(bool steady) {
  emit(Phase::End, steady);
  active_ = false;
  for (int b = 0; b < 2; b++) run_[b] = 0;
}

void ToneDetector::processBlock() {
  // DC removal + Hann window, in place.
  float mean = 0;
  for (int i = 0; i < n_; i++) mean += block_[i];
  mean /= n_;
  for (int i = 0; i < n_; i++) block_[i] = (block_[i] - mean) * window_[i];

  // Goertzel, four bins per pass over the block. Their recurrences are independent, so the ESP32 FPU
  // overlaps them instead of waiting out each one's latency, and each sample is loaded once per four
  // bins. Each bin's arithmetic is unchanged.
  const float *x = block_.data();
  const size_t nBins = bins_.size();
  size_t j = 0;
  for (; j + 4 <= nBins; j += 4) {
    const float c0 = bins_[j].coeff, c1 = bins_[j + 1].coeff, c2 = bins_[j + 2].coeff, c3 = bins_[j + 3].coeff;
    float a1 = 0, a2 = 0, b1 = 0, b2 = 0, d1 = 0, d2 = 0, e1 = 0, e2 = 0;
    for (int i = 0; i < n_; i++) {
      const float xi = x[i];
      const float a0 = xi + c0 * a1 - a2;
      const float b0 = xi + c1 * b1 - b2;
      const float d0 = xi + c2 * d1 - d2;
      const float e0 = xi + c3 * e1 - e2;
      a2 = a1;
      a1 = a0;
      b2 = b1;
      b1 = b0;
      d2 = d1;
      d1 = d0;
      e2 = e1;
      e1 = e0;
    }
    power_[j] = goertzelPower(c0, a1, a2, powerScale_);
    power_[j + 1] = goertzelPower(c1, b1, b2, powerScale_);
    power_[j + 2] = goertzelPower(c2, d1, d2, powerScale_);
    power_[j + 3] = goertzelPower(c3, e1, e2, powerScale_);
  }
  for (; j < nBins; j++) {
    const float c = bins_[j].coeff;
    float s1 = 0, s2 = 0;
    for (int i = 0; i < n_; i++) {
      const float s0 = x[i] + c * s1 - s2;
      s2 = s1;
      s1 = s0;
    }
    power_[j] = goertzelPower(c, s1, s2, powerScale_);
  }

  levels_.clipped = blockClipped_;
  if (learning_) {
    for (size_t j = 0; j < bins_.size(); j++) baseline_[j] += power_[j];
    if (++learnBlocks_ >= learnTarget_) {
      for (size_t j = 0; j < bins_.size(); j++) {
        baseline_[j] /= learnBlocks_;
        if (baseline_[j] < kTiny) baseline_[j] = kTiny;
      }
      learning_ = false;
    }
    levels_.learning = true;
    levels_.active = false;
    for (int b = 0; b < 2; b++) {
      levels_.snrDb[b] = 0;
      levels_.guardSnrDb[b] = 0;
      levels_.levelDb[b] = toDb(power_[bandFirst_[b] + bandCount_[b] / 2]);
    }
    blockIndex_++;
    return;
  }

  float guardSnr[2];
  for (int b = 0; b < 2; b++) {
    snr_[b] = -1e9f;
    for (int k = 0; k < bandCount_[b]; k++) {
      int j = bandFirst_[b] + k;
      float s = toDb(power_[j] / baseline_[j]);
      if (s > snr_[b]) {
        snr_[b] = s;
        snrBin_[b] = k;
      }
    }
    guardSnr[b] = -1e9f;
    for (int k = 0; k < guardCount_[b]; k++) {
      int j = guardFirst_[b] + k;
      float s = toDb(power_[j] / baseline_[j]);
      if (s > guardSnr[b]) guardSnr[b] = s;
    }
    levels_.snrDb[b] = snr_[b];
    levels_.guardSnrDb[b] = guardSnr[b];
    levels_.levelDb[b] = toDb(power_[bandFirst_[b] + snrBin_[b]]);
  }
  levels_.learning = false;

  float releaseLevel = cfg_.thresholdDb - cfg_.releaseDb;
  // Narrowband: the band stands out from its guard bins (not a click, not a neighbouring tone's leakage).
  bool clean[2];
  for (int b = 0; b < 2; b++) clean[b] = snr_[b] - guardSnr[b] >= cfg_.guardMarginDb;
  if (!active_) {
    int trigger = -1;
    for (int b = 0; b < 2; b++) {
      if (clean[b] && snr_[b] >= cfg_.thresholdDb) {
        if (run_[b] == 0 || snr_[b] > runPeakSnr_[b]) {
          runPeakSnr_[b] = snr_[b];
          runPeakFreq_[b] = interpolatedFreq(b, snrBin_[b]);
        }
        run_[b]++;
        if (run_[b] >= cfg_.triggerBlocks && (trigger < 0 || run_[b] > run_[trigger])) trigger = b;
      } else {
        run_[b] = 0;
      }
    }
    if (trigger < 0) {
      updateBaselines();
    } else {
      active_ = true;
      confirmed_ = false;
      eventClipped_ = blockClipped_;
      belowBlocks_ = 0;
      for (int b = 0; b < 2; b++) historyCount_[b] = 0;
      startBlock_ = blockIndex_ + 1 - run_[trigger];
      lastAboveBlock_ = blockIndex_;
      for (int b = 0; b < 2; b++) {
        if (run_[b] > 0) {
          peakSnr_[b] = runPeakSnr_[b];
          peakFreq_[b] = runPeakFreq_[b];
        } else if (clean[b]) {
          peakSnr_[b] = snr_[b];
          peakFreq_[b] = interpolatedFreq(b, snrBin_[b]);
        } else {
          peakSnr_[b] = 0;  // no narrowband evidence for this band yet
          peakFreq_[b] = 0;
        }
      }
    }
  } else {
    eventClipped_ = eventClipped_ || blockClipped_;
    // Peaks and the decay test only use clean blocks: during a clipped attack or next to a strong
    // tone (the FSK remote's leakage and aliased harmonics) the band level says nothing about a bar.
    for (int b = 0; b < 2; b++) {
      if (clean[b] && snr_[b] > peakSnr_[b]) {
        peakSnr_[b] = snr_[b];
        peakFreq_[b] = interpolatedFreq(b, snrBin_[b]);
      }
      if (clean[b]) {
        history_[b][historyCount_[b] % kMaxConfirmBlocks] = snr_[b];
        historyCount_[b]++;
      } else {
        historyCount_[b] = 0;
      }
    }
    int winner = peakSnr_[0] >= peakSnr_[1] ? 0 : 1;
    bool decayed = clean[winner] && snr_[winner] <= peakSnr_[winner] - cfg_.decayDb && snr_[winner] >= releaseLevel;
    if (!confirmed_ && decayed && ringsDown(winner)) {
      confirmed_ = true;
      emit(Phase::Detect, false);
    }
    if (snr_[0] < releaseLevel && snr_[1] < releaseLevel) {
      belowBlocks_++;
    } else {
      belowBlocks_ = 0;
      lastAboveBlock_ = blockIndex_;
    }
    if (belowBlocks_ >= cfg_.releaseBlocks) {
      closeEvent(false);
    } else if ((blockIndex_ - startBlock_ + 1) * blockMs_ >= cfg_.maxEventMs) {
      // Still above the release level after maxEventMs: a steady tone (or a very long ring).
      // Learn the baselines again with it present so it stops looking like an onset.
      closeEvent(!confirmed_);
      blockIndex_++;
      reset();
      return;
    }
  }
  levels_.active = active_;
  blockIndex_++;
}

}  // namespace bardet
