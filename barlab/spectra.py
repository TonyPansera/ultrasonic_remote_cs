"""Welch PSDs of the ringdown windows (per hit and averaged) and of the noise window,
the smoothed noise floor, per-bin SNR, and peak picking.

All spectra share one linear frequency grid 0..Nyquist (common nfft). nperseg is the largest
power of two within the median ringdown, never coarser than target_res_hz when the window allows
it and never finer than fine_res_hz; each hit uses min(nperseg, its window length). Long
ringdowns therefore get fine resolution (close mode pairs, e.g. two similar bars, separate).
Per-hit PSDs are averaged with weights proportional to their window length. The achieved
resolution fs/median(nperseg per hit) is reported.

Peaks: prominence-based on the averaged ringdown PSD, kept only if >= min_above_floor_db
above the *smoothed* noise floor (median-filtered noise PSD). Detection uses the smoothed
floor so a stationary line (present in the noise window too) is still reported and can be
classified as environment; the per-bin SNR (ringdown vs noise PSD) is what reveals it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import ndimage, signal

from barlab.params import Params
from barlab.segment import Segmentation, pow2_at_least

HANN_ENBW = 1.5  # equivalent noise bandwidth of a Hann window, in bins


@dataclass
class Spectra:
    freqs: np.ndarray
    ring_psd: np.ndarray  # mean of per-hit ringdown PSDs [FS^2/Hz]
    hit_psd: np.ndarray  # (n_hits_used, n_bins)
    noise_psd: np.ndarray
    floor_db: np.ndarray  # smoothed noise floor [dB re FS^2/Hz]
    ring_db: np.ndarray
    noise_db: np.ndarray
    snr_db: np.ndarray  # per-bin ringdown / noise [dB]
    nperseg_target: int
    nperseg_hits: list[int]
    nperseg_noise: int
    nfft: int
    bin_hz: float
    resolution_hz: float  # fs / median(nperseg_hits)
    enbw_hz: float
    hits_used: list[int]
    noise_seconds: float


@dataclass
class Peak:
    f_hz: float  # parabolic-interpolated peak frequency
    bin: int
    level_db: float
    floor_db: float
    above_floor_db: float
    snr_db: float
    noise_line_db: float  # noise PSD above its own smoothed floor at this bin
    prominence_db: float
    width_hz: float  # -3 dB full width (nan if not resolved)
    q_width: float  # f / width (a lower bound on Q when resolution-limited)
    resolution_limited: bool


def target_nperseg(fs: float, target_res_hz: float) -> int:
    return pow2_at_least(fs / target_res_hz)


def _welch(seg: np.ndarray, fs: float, nperseg: int, nfft: int) -> np.ndarray:
    _, p = signal.welch(seg, fs=fs, window="hann", nperseg=nperseg, noverlap=nperseg // 2,
                        nfft=nfft, detrend="constant", scaling="density")
    return p


def compute_spectra(x: np.ndarray, fs: float, seg: Segmentation, params: Params) -> Spectra | None:
    hits = [h for h in seg.hits if h.ringdown[1] - h.ringdown[0] >= 64]
    if not hits:
        return None
    target = target_nperseg(fs, params.target_res_hz)
    finest = target_nperseg(fs, params.fine_res_hz)
    lengths = np.array([h.ringdown[1] - h.ringdown[0] for h in hits])
    win_pow2 = 2 ** int(np.floor(np.log2(max(np.median(lengths), 2))))  # largest power of two within the median window
    nper_common = int(min(max(win_pow2, target), finest))
    nfft = 2 * nper_common
    psds, nps, wts = [], [], []
    for h, L in zip(hits, lengths):
        seg_x = x[h.ringdown[0]:h.ringdown[1]]
        nper = int(min(nper_common, len(seg_x) // 64 * 64 or len(seg_x)))  # multiples of 64: few distinct sizes
        psds.append(_welch(seg_x, fs, nper, nfft))
        nps.append(nper)
        wts.append(float(L))
    hit_psd = np.array(psds)
    ring = np.average(hit_psd, axis=0, weights=wts)
    freqs = np.fft.rfftfreq(nfft, 1.0 / fs)
    used = hits

    # The noise PSD is built with the same segment lengths and weights as the ringdown average,
    # so a stationary line has the same shape in both and the per-bin SNR compares like with like.
    noise_cache: dict[int, np.ndarray | None] = {}

    def noise_for(nper: int) -> np.ndarray | None:
        if nper not in noise_cache:
            acc, wsum = np.zeros(len(freqs)), 0.0
            for a, b in seg.noise:
                L = b - a
                if L < 64:
                    continue
                npn = min(nper, L)
                weight = max(1.0, (L - npn) / max(npn // 2, 1) + 1)
                acc += weight * _welch(x[a:b], fs, npn, nfft)
                wsum += weight
            noise_cache[nper] = acc / wsum if wsum > 0 else None
        return noise_cache[nper]

    parts = [(noise_for(n), w) for n, w in zip(nps, wts)]
    parts = [(p, w) for p, w in parts if p is not None]
    if parts:
        noise = np.average([p for p, _ in parts], axis=0, weights=[w for _, w in parts])
    else:
        noise = ndimage.median_filter(ring, size=31, mode="nearest")
    nper_noise = int(np.median(nps))
    noise_n = sum(b - a for a, b in seg.noise if b - a >= 64)
    tiny = 1e-30
    ring, noise = ring + tiny, noise + tiny

    bin_hz = fs / nfft
    k = max(5, int(round(params.floor_smooth_hz / bin_hz)) | 1)
    noise_db = 10 * np.log10(noise)
    floor_db = ndimage.median_filter(noise_db, size=k, mode="nearest")
    ring_db = 10 * np.log10(ring)
    res = fs / float(np.median(nps))
    return Spectra(freqs=freqs, ring_psd=ring, hit_psd=hit_psd, noise_psd=noise, floor_db=floor_db,
                   ring_db=ring_db, noise_db=noise_db, snr_db=ring_db - noise_db,
                   nperseg_target=target, nperseg_hits=nps, nperseg_noise=nper_noise, nfft=nfft,
                   bin_hz=bin_hz, resolution_hz=res, enbw_hz=HANN_ENBW * res,
                   hits_used=[h.index for h in used], noise_seconds=noise_n / fs)


def _parabolic(y: np.ndarray, k: int) -> tuple[float, float]:
    if k <= 0 or k >= len(y) - 1:
        return 0.0, float(y[k])
    a, b, c = y[k - 1], y[k], y[k + 1]
    den = a - 2 * b + c
    if den >= 0:
        return 0.0, float(b)
    d = 0.5 * (a - c) / den
    return float(d), float(b - 0.25 * (a - c) * d)


def _half_power_width(y: np.ndarray, freqs: np.ndarray, k: int, level: float) -> float:
    target = level - 3.0
    i = k
    while i > 0 and y[i] > target:
        i -= 1
    j = k
    while j < len(y) - 1 and y[j] > target:
        j += 1
    if y[i] > target or y[j] > target:
        return float("nan")
    fl = freqs[i] + (target - y[i]) / (y[i + 1] - y[i]) * (freqs[i + 1] - freqs[i])
    fr = freqs[j - 1] + (y[j - 1] - target) / (y[j - 1] - y[j]) * (freqs[j] - freqs[j - 1])
    return float(fr - fl)


def _is_leakage(fk: float, yk: float, fj: float, yj: float, res: float, params: Params) -> bool:
    """True if the weaker peak (fk, yk) can be explained by window leakage of (fj, yj):
    inside the stronger peak's mainlobe (+ leak_guard_res resolutions when it is more than
    leak_keep_db stronger), or below its Hann sidelobe envelope
    (-31.5 dB at 2.5 resolutions, -60 dB/decade beyond) + leak_keep_db/2."""
    d = abs(fk - fj) / res
    if d < 2.0:
        return True
    if d < params.leak_guard_res and yj - yk > params.leak_keep_db:
        return True
    envelope = yj - 31.5 - 60.0 * np.log10(max(d, 2.5) / 2.5)
    return yk < envelope + params.leak_keep_db / 2


def find_peaks(spec: Spectra | None, fs: float, params: Params) -> list[Peak]:
    if spec is None:
        return []
    f, y = spec.freqs, spec.ring_db
    idx, props = signal.find_peaks(y, prominence=params.prominence_db)
    above = y[idx] - spec.floor_db[idx]
    ok = (f[idx] >= params.fmin_hz) & (f[idx] <= fs / 2 - 2 * spec.bin_hz) & (above >= params.min_above_floor_db)
    cand = sorted(zip(idx[ok], props["prominences"][ok]), key=lambda t: -y[t[0]])
    kept: list[tuple[int, float]] = []
    for k, prom in cand:  # strongest first; drop mainlobe/sidelobe leakage of a stronger peak
        if any(_is_leakage(f[k], y[k], f[j], y[j], spec.resolution_hz, params) for j, _ in kept):
            continue
        kept.append((int(k), float(prom)))
    kept.sort(key=lambda t: -(y[t[0]] - spec.floor_db[t[0]]))
    kept = kept[: params.max_peaks]

    peaks = []
    for k, prom in kept:
        d, level = _parabolic(y, k)
        f_ref = float(f[k] + d * spec.bin_hz)
        width = _half_power_width(y, f, k, level)
        peaks.append(Peak(
            f_hz=f_ref, bin=k, level_db=level, floor_db=float(spec.floor_db[k]),
            above_floor_db=float(level - spec.floor_db[k]), snr_db=float(spec.snr_db[k]),
            noise_line_db=float(spec.noise_db[k] - spec.floor_db[k]), prominence_db=prom,
            width_hz=width, q_width=float(f_ref / width) if width and np.isfinite(width) else float("nan"),
            resolution_limited=bool(np.isfinite(width) and width < 2.0 * spec.resolution_hz)))
    return sorted(peaks, key=lambda p: p.f_hz)
