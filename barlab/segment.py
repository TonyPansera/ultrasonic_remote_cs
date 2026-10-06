"""Impact detection and windowing.

Hits: the short RMS envelope of the high-passed signal must rise sharply above its own
trailing level. Rise function D(t) = env_dB(t) - trailing_dB(t); adaptive threshold
median(D) + k * MAD(D), floored at min_rise_db; rising edges closer than min_spacing are
merged (bounces). Detecting rises rather than levels still finds a hit while the previous
one is ringing.

Windows per hit:
  attack   [onset, onset + attack_ms]  (impact click: broadband, excluded from spectra)
  ringdown [attack end, end) where end is where the *narrowband* spectral excess over the
           noise spectrum (max over bins of a short STFT) drops into the noise, i.e. below
           the noise window's own 99th percentile of that statistic + end_margin_db; or the
           next hit, or max_ringdown_s. Narrowband, because a high-Q mode is detectable long
           after the broadband envelope has sunk into the noise; stationary lines do not
           extend it because they are in the noise spectrum too.
  noise    quiet stretches without hits (pre-trigger preferred). If none is long enough:
           NOISE_FALLBACK warning and pre-hit samples are used instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from scipy import signal

from barlab.io import warning
from barlab.params import Params

TINY = 1e-30


@dataclass
class Hit:
    index: int
    onset: int
    peak: int
    attack: tuple[int, int]
    ringdown: tuple[int, int]
    rise_db: float
    peak_db: float  # short-envelope (RMS) peak level, dBFS
    end_reason: str  # noise | next_hit | max_length | file_end
    short: bool = False
    clipped_ringdown: bool = False  # clipping persists into the ringdown: hit excluded from fits
    clip_trim: int = 0  # samples removed from the ringdown start because of clipping


@dataclass
class Segmentation:
    fs: int
    n: int
    hits: list[Hit]
    noise: list[tuple[int, int]]
    noise_fallback: bool
    warnings: list[dict]
    threshold_db: float
    quiet_level_db: float
    end_threshold_db: float | None
    end_frame: int
    traces: dict = field(default_factory=dict)  # display-only, max-pooled envelopes


def samples(fs: float, ms: float) -> int:
    return max(1, int(round(ms * 1e-3 * fs)))


def pow2_at_least(n: float) -> int:
    return int(2 ** np.ceil(np.log2(max(n, 2))))


def _window_means(p: np.ndarray, w: int) -> np.ndarray:
    """Means of p[j:j+w] for j = 0 .. len(p)-w."""
    c = np.concatenate(([0.0], np.cumsum(p, dtype=np.float64)))
    return (c[w:] - c[:-w]) / w


def centered_mean(p: np.ndarray, w: int) -> np.ndarray:
    w = min(w, len(p))
    s = _window_means(p, w)
    out = np.empty(len(p))
    half = w // 2
    out[half:half + len(s)] = s
    out[:half] = s[0]
    out[half + len(s):] = s[-1]
    return out


def trailing_mean(p: np.ndarray, w: int, gap: int) -> np.ndarray:
    """Mean of p[i-gap-w : i-gap]; NaN where not available."""
    out = np.full(len(p), np.nan)
    if len(p) <= w + gap:
        return out
    s = _window_means(p, w)
    j0 = w + gap
    out[j0:] = s[: len(p) - j0]
    return out


def maxpool_trace(y: np.ndarray, fs: float, n_points: int = 6000) -> tuple[np.ndarray, np.ndarray]:
    """Display-only max-pooling (keeps transients visible); not used for analysis."""
    k = max(1, len(y) // n_points)
    m = len(y) // k
    pooled = y[: m * k].reshape(m, k).max(axis=1)
    t = (np.arange(m) * k + k / 2) / fs
    return t, pooled


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if not mask.any():
        return []
    d = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    return list(zip(starts.tolist(), ends.tolist()))


class _Excess:
    """Narrowband spectral excess over a noise spectrum, frame by frame."""

    def __init__(self, fs: int, params: Params):
        self.nper = pow2_at_least(params.end_frame_ms * 1e-3 * fs)
        self.hop = self.nper // 4
        self.win = signal.windows.hann(self.nper, sym=False)
        f = np.fft.rfftfreq(self.nper, 1.0 / fs)
        self.band = (f >= max(params.fmin_hz, params.hp_hz)) & (f <= 0.98 * fs / 2)
        self.pn = None

    def frame_power(self, seg: np.ndarray) -> np.ndarray:
        if len(seg) < self.nper:
            return np.empty((0, int(self.band.sum())))
        frames = sliding_window_view(seg, self.nper)[:: self.hop]
        spec = np.fft.rfft(frames * self.win, axis=1)
        return (spec.real**2 + spec.imag**2)[:, self.band]

    @staticmethod
    def smooth(P: np.ndarray) -> np.ndarray:
        if len(P) < 3:
            return P
        S = P.copy()
        S[1:-1] = (P[:-2] + P[1:-1] + P[2:]) / 3.0
        return S

    def stat_db(self, P: np.ndarray) -> np.ndarray:
        if len(P) == 0:
            return np.empty(0)
        return 10 * np.log10(np.max(self.smooth(P) / self.pn, axis=1) + TINY)

    def fit_noise(self, x: np.ndarray, noise: list[tuple[int, int]]) -> float | None:
        blocks = [self.frame_power(x[a:b]) for a, b in noise]
        blocks = [b for b in blocks if len(b)]
        if not blocks:
            self.pn = None
            return None
        self.pn = np.mean(np.concatenate(blocks), axis=0) + TINY
        null = np.concatenate([self.stat_db(b) for b in blocks])
        return float(np.percentile(null, 99)) if len(null) >= 5 else None


def segment(x: np.ndarray, fs: int, params: Params) -> Segmentation:
    n = len(x)
    warns: list[dict] = []
    edge = samples(fs, params.edge_ms)
    guard = samples(fs, params.pre_guard_ms)
    att = samples(fs, params.attack_ms)
    min_ring = samples(fs, params.min_ringdown_ms)
    max_ring = samples(fs, params.max_ringdown_s * 1e3)

    hp = min(params.hp_hz, 0.4 * fs)
    sos = signal.butter(4, hp, "highpass", fs=fs, output="sos")
    xh = signal.sosfiltfilt(sos, x)
    p = xh * xh

    ms_short = centered_mean(p, samples(fs, params.env_win_ms))
    ms_trail = trailing_mean(p, samples(fs, params.trail_win_ms), samples(fs, params.trail_gap_ms))
    ms5 = centered_mean(p, samples(fs, params.quiet_win_ms))
    db_short = 10 * np.log10(ms_short + TINY)
    db5 = 10 * np.log10(ms5 + TINY)
    inner = slice(edge, max(edge + 1, n - edge))
    quiet_db = float(np.percentile(db5[inner], params.quiet_percentile))

    fill = np.nanmedian(ms_trail) if np.isfinite(ms_trail).any() else np.median(ms_short)
    ms_trail = np.where(np.isfinite(ms_trail), ms_trail, fill)
    rise = 10 * np.log10((ms_short + TINY) / (ms_trail + TINY))
    med = float(np.median(rise[inner]))
    mad = float(np.median(np.abs(rise[inner] - med)))
    thr = max(med + params.k_mad * mad, params.min_rise_db)

    above = rise > thr
    above[:edge] = False
    above[n - edge:] = False
    edges = np.flatnonzero(above[1:] & ~above[:-1]) + 1
    look = samples(fs, 5.0)
    spacing = samples(fs, params.min_spacing_ms)
    onsets: list[tuple[int, int, float, float]] = []
    for e in edges:
        if onsets and e - onsets[-1][0] < spacing:
            continue
        seg = db_short[e:e + look]
        pk = int(e + np.argmax(seg))
        if db_short[pk] < quiet_db + params.min_hit_snr_db:
            continue
        onsets.append((int(e), pk, float(np.max(rise[e:e + look])), float(db_short[pk])))

    exc = _Excess(fs, params)

    def hit_zones(ends):
        return [(o - guard, (ends[i] if ends else o + att + samples(fs, 20.0)) + guard)
                for i, (o, *_rest) in enumerate(onsets)]

    def pick_noise(zones):
        quiet = db5 <= quiet_db + params.quiet_margin_db
        quiet[:edge] = False
        quiet[n - edge:] = False
        for a, b in zones:
            quiet[max(a, 0):max(b, 0)] = False
        runs = [r for r in _runs(quiet) if r[1] - r[0] >= samples(fs, params.min_noise_ms)]
        first = onsets[0][0] if onsets else n
        runs.sort(key=lambda r: (r[0] >= first, -(r[1] - r[0])))
        chosen, total, budget = [], 0, samples(fs, params.max_noise_s * 1e3)
        for a, b in runs:
            if total >= budget:
                break
            b = min(b, a + budget - total)
            chosen.append((a, b))
            total += b - a
        return sorted(chosen)

    def fallback_noise():
        out = []
        prev = edge
        for o, *_rest in onsets:
            a, b = max(prev, o - guard - samples(fs, params.fallback_noise_ms)), o - guard
            if b - a >= max(exc.nper, samples(fs, 2.0)):
                out.append((a, b))
            prev = o + att
        return out

    def ringdown_ends(end_thr):
        ends, reasons = [], []
        for i, (o, *_rest) in enumerate(onsets):
            a0 = o + att
            nxt = onsets[i + 1][0] - guard if i + 1 < len(onsets) else n - edge
            cap = a0 + max_ring
            bound = min(nxt, cap, n - edge)
            reason = "next_hit" if bound == nxt and i + 1 < len(onsets) else (
                "max_length" if bound == cap else "file_end")
            end = bound
            if exc.pn is not None and end_thr is not None and bound - a0 >= exc.nper:
                stat = exc.stat_db(exc.frame_power(x[a0:bound]))
                below = stat < end_thr
                hold = params.end_hold_frames
                if len(below) >= hold:
                    run = np.convolve(below.astype(int), np.ones(hold, int), "valid") == hold
                    k = np.flatnonzero(run)
                    if len(k):
                        end = min(bound, a0 + int(k[0]) * exc.hop + exc.nper // 2)
                        reason = "noise"
            end = min(max(end, a0 + min_ring), max(bound, a0 + 1))
            ends.append(end)
            reasons.append(reason)
        return ends, reasons

    noise = pick_noise(hit_zones(None))
    fallback = not noise and bool(onsets)
    if fallback:
        noise = fallback_noise()
    end_thr = None
    ends: list[int] = []
    reasons: list[str] = []
    for _ in range(2):  # noise -> ringdown ends -> refined noise -> ends again
        null99 = exc.fit_noise(x, noise)
        end_thr = None if null99 is None else null99 + params.end_margin_db
        ends, reasons = ringdown_ends(end_thr)
        if fallback:
            break
        refined = pick_noise(hit_zones(ends))
        if refined == noise:
            break
        if not refined:
            fallback = bool(onsets)
            noise = fallback_noise() if fallback else []
        else:
            noise = refined
    if not noise and not onsets:
        noise = [(edge, max(edge + 1, n - edge))]
    if fallback:
        warns.append(warning(
            "WARNING", "NOISE_FALLBACK",
            f"no quiet segment >= {params.min_noise_ms:.0f} ms without hits: the noise reference uses "
            "pre-hit samples, which may still contain ringing; environment classification is less reliable"))
    if not onsets:
        warns.append(warning("WARNING", "NO_HITS", "no impacts detected"))

    hits = []
    for i, ((o, pk, r, pdb), end, why) in enumerate(zip(onsets, ends, reasons)):
        hits.append(Hit(index=i, onset=o, peak=pk, attack=(o, o + att), ringdown=(o + att, end),
                        rise_db=r, peak_db=pdb, end_reason=why, short=(end - o - att) < min_ring + 1))

    traces = {"env_db": maxpool_trace(db_short, fs), "rise_db": maxpool_trace(rise, fs)}
    return Segmentation(fs=fs, n=n, hits=hits, noise=noise, noise_fallback=fallback, warnings=warns,
                        threshold_db=float(thr), quiet_level_db=quiet_db, end_threshold_db=end_thr,
                        end_frame=exc.nper, traces=traces)
