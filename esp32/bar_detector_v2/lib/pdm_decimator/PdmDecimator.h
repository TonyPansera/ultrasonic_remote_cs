// PDM-to-PCM decimator for a 1-bit MEMS microphone read as a raw bitstream.
//
// Pure C++11 with no Arduino headers, so the same code runs on the ESP32 (src/main.cpp) and in the
// host unit tests (test/test_pdm, `pio test -e native`).
//
// The ESP32's own PDM receiver decimates by 64 or 128 only, so at the SPH0641's ultrasonic clock
// (3.072 MHz) its PCM stops at 48 kS/s, below the bars. Instead, I2S0 runs as a standard 32-bit stereo
// receiver whose BCK is the mic clock: every clock cycle lands in a slot, and the DMA delivers the
// raw bitstream as 32-bit words, first bit at bit 31. This class turns those words into PCM:
//   - each bit is +1 (one) or -1 (zero), so the output is in full-scale units (+-1 = 0 dBFS);
//   - a linear-phase FIR low-pass (Kaiser-windowed sinc, unity DC gain) runs at the bit rate and is
//     evaluated once every `decimation` bits: 3.072 MHz / 16 = 192 kS/s;
//   - the FIR is computed with one 256-entry table per byte of its span (taps / 8 tables), so one
//     output costs taps / 8 lookups and adds instead of `taps` multiply-adds.
// With the defaults (256 taps, Kaiser beta 9, cutoff 96 kHz), the response is flat within 0.01 dB up
// to 60 kHz, -0.7 dB at 80 kHz, and below -90 dB from 132 kHz (the lowest frequency that folds back
// into 0-60 kHz at 192 kS/s) up to the bit rate's Nyquist.
//
// The bytes of the previous call are kept, so chunk boundaries are seamless: every push of n words
// gives exactly n * 32 / decimation samples. After configure() or reset() the history is a zero-mean
// pattern (0x55), so the first taps / decimation outputs settle from silence.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include <vector>

namespace pdm {

struct PdmConfig {
  float pdmClockHz = 3072000.0f;  // the mic clock: 3.072-4.8 MHz is the SPH0641's ultrasonic mode
  int decimation = 16;            // bits per output sample (a multiple of 8): 192 kS/s at 3.072 MHz
  int taps = 256;                 // FIR length in bits (a multiple of 8, at least `decimation`)
  float cutoffHz = 96000.0f;      // -6 dB point of the windowed sinc
  float kaiserBeta = 9.0f;        // stopband ~ -90 dB
  // The I2S DMA may store the two 32-bit slots of a frame in reverse time order. With swapWords set,
  // each pair of words is taken second word first (push() then needs an even word count).
  bool swapWords = false;
};

class PdmDecimator {
 public:
  PdmDecimator();
  explicit PdmDecimator(const PdmConfig &cfg);

  // Designs the FIR, builds the tables and resets the history.
  void configure(const PdmConfig &cfg);
  // Forgets the history (back to the zero-mean pattern).
  void reset();

  // Decimates n words (32 bits each, first bit at bit 31) into out, which must hold
  // outputsFor(n) floats. Returns the number of samples written.
  size_t push(const uint32_t *words, size_t n, float *out);
  size_t outputsFor(size_t words) const { return (words * 4 + hopBytes_ - 1) / hopBytes_; }

  const PdmConfig &config() const { return cfg_; }
  float outputRateHz() const { return cfg_.pdmClockHz / cfg_.decimation; }
  // Fraction of one bits in the last push: ~0.5 for silence; stuck at 0 or 1 means no mic data.
  float onesDensity() const { return onesDensity_; }
  // The designed FIR's magnitude response at hz (relative to DC), in dB.
  float responseDb(float hz) const;
  const std::vector<float> &coefficients() const { return h_; }

 private:
  PdmConfig cfg_;
  int spanBytes_ = 0;  // taps / 8
  int hopBytes_ = 0;   // decimation / 8
  std::vector<float> h_;
  std::vector<float> lut_;    // spanBytes_ tables of 256 entries: the FIR over one byte of bits
  std::vector<uint8_t> buf_;  // history (spanBytes_ - hopBytes_ bytes), then the bytes of one push
  size_t fill_ = 0;           // history bytes at the start of buf_
  float onesDensity_ = 0.5f;
};

}  // namespace pdm
