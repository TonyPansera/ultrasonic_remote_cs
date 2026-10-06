"""barlab: deterministic DSP for ultrasonic bar-impact recordings.

Pipeline: io (load + checks) -> segment (hits, attack/ringdown/noise windows)
-> spectra (Welch PSD, noise floor, SNR, peaks) -> decay (per-peak ringdown fits)
-> theory (expected bar modes) -> classify -> schema/store/plots.

Never resamples, never uses mel scales; all frequency axes are linear up to Nyquist.
"""

__version__ = "0.1.0"
