#include "PdmDecimator.h"

#include <math.h>
#include <string.h>

namespace pdm {

namespace {

const double kPi = 3.14159265358979323846;

// Modified Bessel function of the first kind, order 0 (power series), for the Kaiser window.
double besselI0(double x) {
  double sum = 1, term = 1;
  for (int k = 1; k < 100; k++) {
    double q = x / (2.0 * k);
    term *= q * q;
    sum += term;
    if (term < 1e-15 * sum) break;
  }
  return sum;
}

}  // namespace

PdmDecimator::PdmDecimator() { configure(PdmConfig()); }

PdmDecimator::PdmDecimator(const PdmConfig &cfg) { configure(cfg); }

void PdmDecimator::configure(const PdmConfig &cfg) {
  cfg_ = cfg;
  spanBytes_ = cfg.taps / 8;
  hopBytes_ = cfg.decimation / 8;
  const int n = spanBytes_ * 8;

  // Kaiser-windowed sinc, normalised to unity gain at DC.
  std::vector<double> h(n);
  const double fc = cfg.cutoffHz / cfg.pdmClockHz;  // cycles per bit
  const double centre = (n - 1) / 2.0;
  const double i0Beta = besselI0(cfg.kaiserBeta);
  double sum = 0;
  for (int i = 0; i < n; i++) {
    double t = i - centre;
    double sinc = 2 * fc * (t == 0 ? 1.0 : sin(2 * kPi * fc * t) / (2 * kPi * fc * t));
    double r = 2.0 * i / (n - 1) - 1.0;
    h[i] = sinc * besselI0(cfg.kaiserBeta * sqrt(1.0 - r * r)) / i0Beta;
    sum += h[i];
  }
  h_.resize(n);
  for (int i = 0; i < n; i++) h_[i] = (float)(h[i] / sum);

  // Table j holds, for every byte value, the FIR over bits 8j..8j+7 of the span (bit 7 is the earliest).
  lut_.resize((size_t)spanBytes_ * 256);
  for (int j = 0; j < spanBytes_; j++) {
    for (int b = 0; b < 256; b++) {
      double acc = 0;
      for (int k = 0; k < 8; k++) acc += (b >> (7 - k) & 1 ? 1.0 : -1.0) * h[8 * j + k] / sum;
      lut_[(size_t)j * 256 + b] = (float)acc;
    }
  }
  reset();
}

void PdmDecimator::reset() {
  fill_ = (size_t)(spanBytes_ - hopBytes_);
  buf_.assign(fill_, 0x55);
  onesDensity_ = 0.5f;
}

size_t PdmDecimator::push(const uint32_t *words, size_t n, float *out) {
  buf_.resize(fill_ + 4 * n);
  uint8_t *dst = &buf_[fill_];
  uint32_t ones = 0;
  for (size_t i = 0; i < n; i++) {
    size_t k = cfg_.swapWords && (i ^ 1) < n ? (i ^ 1) : i;
    uint32_t w = words[k];
    ones += __builtin_popcount(w);
    *dst++ = (uint8_t)(w >> 24);
    *dst++ = (uint8_t)(w >> 16);
    *dst++ = (uint8_t)(w >> 8);
    *dst++ = (uint8_t)w;
  }
  if (n) onesDensity_ = ones / (32.0f * n);

  const size_t total = fill_ + 4 * n;
  const size_t span = (size_t)spanBytes_;
  const float *lut = lut_.data();
  size_t pos = 0, count = 0;
  for (; pos + span <= total; pos += hopBytes_) {
    const uint8_t *p = &buf_[pos];
    // Two accumulators so consecutive adds do not wait on each other.
    float acc0 = 0, acc1 = 0;
    for (size_t j = 0; j < span; j += 2) {
      acc0 += lut[(j << 8) | p[j]];
      acc1 += lut[((j + 1) << 8) | p[j + 1]];
    }
    out[count++] = acc0 + acc1;
  }
  memmove(&buf_[0], &buf_[pos], total - pos);
  fill_ = total - pos;
  return count;
}

float PdmDecimator::responseDb(float hz) const {
  double re = 0, im = 0;
  const double w = 2 * kPi * hz / cfg_.pdmClockHz;
  for (size_t i = 0; i < h_.size(); i++) {
    re += h_[i] * cos(w * i);
    im -= h_[i] * sin(w * i);
  }
  double mag = sqrt(re * re + im * im);
  return (float)(20 * log10(mag > 1e-15 ? mag : 1e-15));
}

}  // namespace pdm
