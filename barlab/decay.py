"""Per-peak ringdown fits.

For each peak and each hit:
  1. zero-phase Butterworth band-pass around the peak (sosfiltfilt), width
     B = clip(bw_factor * max(width, resolution), bw_min, bw_max_frac * f), kept clear of
     neighbouring peaks;
  2. Hilbert analytic signal a(t); envelope power |a|^2 smoothed over ~1/B (the band's
     correlation time), minus the band's noise power measured the same way in the noise
     window (removes the noise-floor bias of the envelope);
  3. linear least-squares fit of the log envelope against time, over the region that starts
     after the attack, the envelope maximum and 2 filter time constants, and ends where the
     noise-subtracted power falls to fit_floor_db above the noise power:
         ln env(t) = c - t / tau   ->   tau,  Q = pi f tau,  R^2;
     each sample is weighted by 1/std(ln P_s) = P_s / sqrt(2 P_s P_n + P_n^2) (signal plus
     complex Gaussian noise), so the noisy tail neither biases nor dominates the slope;
  4. per-hit frequency from the power-weighted slope of the unwrapped analytic phase.

Aggregates: median and robust spread over hits (decay fits with R^2 >= r2_min, no clipping).
A weak mode may not give >= 3 usable per-hit fits; the hit-averaged envelope power
(sum of A_h^2 exp(-2t/tau): same tau, noise reduced by sqrt(N)) is then fitted the same
way ("stacked" fit) and used for tau and Q; `q_source` says which estimate is reported.

Slow decays: when a hit decays by less than min_decay_db over the fitted span T, tau cannot
be measured but is bounded: tau > T / (min_decay_db in nepers), so Q > pi f T * 8.69 / min_decay_db
(`q_lower`). No decay at all over >= steady_min_s (or an implied Q above q_max_plausible) marks
the line `steady`: a constant tone, not the ringdown of a struck metal bar.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy import fft as sfft
from scipy import signal

from barlab.params import Params
from barlab.segment import Segmentation, centered_mean, samples
from barlab.spectra import Peak, Spectra


@dataclass
class HitFit:
    hit: int
    ok: bool
    reason: str | None = None
    f_hz: float | None = None
    tau_s: float | None = None
    q: float | None = None
    r2: float | None = None
    t0_s: float | None = None  # fit start, relative to the hit onset
    t1_s: float | None = None
    decay_db: float | None = None
    q_lower: float | None = None  # lower bound when the decay is too small to measure
    steady_s: float | None = None  # span with no measurable decay
    trace: tuple | None = None  # (t_ms, dB) display-only, decimated
    fit_db: tuple | None = None  # (t_ms[2], dB[2]) fitted line, display-only


@dataclass
class PeakDecay:
    f_center: float
    band_hz: tuple[float, float]
    n_tried: int
    n_fitted: int = 0  # per-hit decay fits with R^2 >= r2_min
    n_freq: int = 0  # hits with a frequency estimate
    fits: list[HitFit] = field(default_factory=list)
    noise_db: float | None = None  # band noise power, dB (display)
    f_hz: float | None = None
    f_std_hz: float | None = None
    f_sem_hz: float | None = None
    f_range_rel: float | None = None
    tau_s: float | None = None
    tau_std_s: float | None = None
    q: float | None = None
    q_std: float | None = None
    q_lo: float | None = None
    q_hi: float | None = None
    r2: float | None = None
    q_source: str | None = None  # "hits" | "stack"
    tau_stack_s: float | None = None
    r2_stack: float | None = None
    q_lower: float | None = None  # set when no Q could be measured but the decay is slow
    steady_s: float | None = None  # set when the line does not decay at all
    narrow_band: bool = False


def robust_std(v: np.ndarray) -> float:
    v = np.asarray(v, float)
    if len(v) < 2:
        return float("nan")
    return float(1.4826 * np.median(np.abs(v - np.median(v))))


def _reject_outliers(v: np.ndarray, k: float = 5.0) -> np.ndarray:
    """Drop values more than k robust SDs from the median (needs >= 4 values)."""
    if len(v) < 4:
        return v
    s = robust_std(v)
    if not np.isfinite(s) or s == 0:
        return v
    keep = np.abs(v - np.median(v)) <= k * s
    return v[keep] if keep.sum() >= 2 else v


def _analytic(y: np.ndarray) -> np.ndarray:
    n = len(y)
    return signal.hilbert(y, N=sfft.next_fast_len(n))[:n]


def _band(peak: Peak, spec: Spectra, others: list[float], fs: float, params: Params) -> tuple[float, float, bool]:
    width = peak.width_hz if np.isfinite(peak.width_hz) else spec.resolution_hz
    B = params.bw_factor * max(width, spec.resolution_hz)
    B = min(max(B, params.bw_min_hz), params.bw_max_frac * peak.f_hz)
    narrow = False
    if others:
        dmin = min(abs(peak.f_hz - f) for f in others)
        if B > 1.2 * dmin:
            B = max(1.2 * dmin, 2.0 * spec.resolution_hz)
            narrow = True
    lo = max(peak.f_hz - B / 2, 1.0)
    hi = min(peak.f_hz + B / 2, 0.999 * fs / 2)
    return lo, hi, narrow


def _log_fit(t: np.ndarray, p_sig: np.ndarray, pn: float, n_avg) -> tuple[float, float, float]:
    """Weighted LSQ of ln(envelope) = 0.5 ln P_s against t. Returns slope, intercept, R^2."""
    wts = p_sig / np.sqrt((2 * p_sig * pn + pn**2) / n_avg)
    ln_env = 0.5 * np.log(p_sig)
    slope, icpt = np.polyfit(t, ln_env, 1, w=wts)
    w2 = wts**2
    resid = ln_env - (slope * t + icpt)
    mean_w = np.sum(w2 * ln_env) / np.sum(w2)
    ss_tot = float(np.sum(w2 * (ln_env - mean_w) ** 2))
    r2 = 1.0 - float(np.sum(w2 * resid**2)) / ss_tot if ss_tot > 0 else 0.0
    return float(slope), float(icpt), r2


def fit_peak(x: np.ndarray, fs: float, seg: Segmentation, spec: Spectra, peak: Peak,
             others: list[float], params: Params, want_traces: bool = False) -> PeakDecay:
    lo, hi, narrow = _band(peak, spec, others, fs, params)
    B = hi - lo
    sos = signal.butter(params.filt_order, [lo, hi], "bandpass", fs=fs, output="sos")
    tau_f = 1.0 / (np.pi * B * np.sin(np.pi / (2 * params.filt_order)))
    n_tf = int(np.ceil(tau_f * fs))
    w_s = max(samples(fs, 0.1), int(round(fs / B)))
    min_len = max(int(20 * fs / peak.f_hz), 3 * w_s)

    # band noise power, measured exactly like the signal
    pn_parts, pn_w = [], []
    for a, b in seg.noise:
        if b - a < 8 * n_tf + 2 * w_s:
            continue
        pa_n = np.abs(_analytic(signal.sosfiltfilt(sos, x[a:b]))) ** 2
        core = pa_n[3 * n_tf: len(pa_n) - 3 * n_tf]
        pn_parts.append(core.mean())
        pn_w.append(len(core))
    pn = float(np.average(pn_parts, weights=pn_w)) if pn_parts else None

    hits = seg.hits
    out = PeakDecay(f_center=peak.f_hz, band_hz=(lo, hi), n_tried=len(hits),
                    noise_db=None if pn is None else 10 * np.log10(pn + 1e-30), narrow_band=narrow)
    guard = samples(fs, params.pre_guard_ms)
    edge = samples(fs, params.edge_ms)
    max_ring = samples(fs, params.max_ringdown_s * 1e3)
    floor = None if pn is None else pn * 10 ** (params.fit_floor_db / 10)
    stack_sum = np.zeros(0)
    stack_cnt = np.zeros(0)
    stack_starts: list[int] = []

    for i, h in enumerate(hits):
        if h.clipped_ringdown:
            out.fits.append(HitFit(hit=h.index, ok=False, reason="clipped"))
            continue
        if pn is None:
            out.fits.append(HitFit(hit=h.index, ok=False, reason="no noise reference"))
            continue
        hard_end = hits[i + 1].onset - guard if i + 1 < len(hits) else len(x) - edge
        hard_end = min(hard_end, h.attack[1] + max_ring)
        s0 = max(0, h.onset - max(samples(fs, 2.0), 3 * n_tf))
        a = _analytic(signal.sosfiltfilt(sos, x[s0:hard_end]))
        pa = np.abs(a) ** 2
        ps = centered_mean(pa, w_s)
        ps_slow = centered_mean(pa, 3 * w_s)
        on = h.onset - s0
        look = min(len(ps), on + samples(fs, 50.0))
        t_max = on + int(np.argmax(ps[on:look])) if look > on else on
        start = max(h.ringdown[0] - s0, t_max) + 2 * n_tf + w_s // 2  # ringdown[0]: after any clipping
        stop_lim = len(ps) - 3 * n_tf
        if start >= stop_lim:
            out.fits.append(HitFit(hit=h.index, ok=False, reason="ringdown too short"))
            continue

        piece = ps[on:stop_lim]  # stacked fit accumulators, aligned on the onset
        if len(piece) > len(stack_sum):
            grow = len(piece) - len(stack_sum)
            stack_sum = np.concatenate([stack_sum, np.zeros(grow)])
            stack_cnt = np.concatenate([stack_cnt, np.zeros(grow)])
        stack_sum[: len(piece)] += piece
        stack_cnt[: len(piece)] += 1
        stack_starts.append(start - on)

        below = np.flatnonzero(ps_slow[start:stop_lim] - pn < floor)
        end = start + (int(below[0]) if len(below) else stop_lim - start)
        if end - start < min_len:
            out.fits.append(HitFit(hit=h.index, ok=False, reason="not above noise"))
            continue
        p_sig = np.maximum(ps[start:end] - pn, 0.05 * pn)
        t = (np.arange(start, end) - on) / fs
        slope, icpt, r2 = _log_fit(t, p_sig, pn, 1)
        decay_db = float(-20 * slope * (t[-1] - t[0]) / np.log(10))
        phase = np.unwrap(np.angle(a[start:end]))
        f_hit = float(np.polyfit(t, phase, 1, w=np.sqrt(p_sig))[0] / (2 * np.pi))
        fit = HitFit(hit=h.index, ok=True, f_hz=f_hit, r2=r2, t0_s=float(t[0]), t1_s=float(t[-1]),
                     decay_db=decay_db)
        span = float(t[-1] - t[0])
        q_est = np.pi * f_hit * (-1.0 / slope) if slope < 0 else np.inf
        if slope >= 0 or (span >= params.steady_min_s and q_est >= params.q_max_plausible):
            fit.ok, fit.reason = False, "no decay"
            fit.steady_s = span if span >= params.steady_min_s else None
        elif decay_db < params.min_decay_db:
            fit.ok, fit.reason = False, "insufficient decay"
            fit.q_lower = float(np.pi * f_hit * span * 20.0 / (np.log(10) * params.min_decay_db))
        else:
            fit.tau_s = -1.0 / slope
            fit.q = float(np.pi * f_hit * fit.tau_s)
            if r2 < params.r2_min:
                fit.reason = "low R2"
        if want_traces:
            tt = (np.arange(len(ps)) - on) / fs
            step = max(1, len(ps) // 1500)
            fit.trace = (tt[::step] * 1e3, 10 * np.log10(ps[::step] + 1e-30))
            if fit.tau_s:
                ends = np.array([t[0], t[-1]])
                fit.fit_db = (ends * 1e3, 20 * (slope * ends + icpt) / np.log(10))
        out.fits.append(fit)

    # ---- aggregates over hits
    good = [f for f in out.fits if f.ok and f.tau_s and f.r2 is not None and f.r2 >= params.r2_min]
    freq = [f for f in out.fits if f.f_hz is not None]
    out.n_fitted, out.n_freq = len(good), len(freq)
    if freq:
        fv = np.array([f.f_hz for f in freq])
        kept = _reject_outliers(fv)
        out.f_hz = float(np.median(kept))
        if len(fv) > 1:
            # ordinary SD of the outlier-cleaned hits (a MAD-based spread under-states it for
            # small n); 1.2533 SD / sqrt(n) is the standard error of their median
            out.f_std_hz = float(np.std(kept, ddof=1)) if len(kept) > 1 else robust_std(fv)
            out.f_sem_hz = 1.2533 * out.f_std_hz / np.sqrt(max(len(kept), 1))
            lo_f, hi_f = np.percentile(fv, [10, 90]) if len(fv) >= 5 else (fv.min(), fv.max())
            out.f_range_rel = float((hi_f - lo_f) / out.f_hz)  # P10-P90 (>= 5 hits): one bad hit can't break it
    if good:
        tv = np.array([f.tau_s for f in good])
        qv = np.array([f.q for f in good])
        out.tau_s, out.q = float(np.median(tv)), float(np.median(qv))
        out.q_lo, out.q_hi = float(qv.min()), float(qv.max())
        out.r2 = float(np.median([f.r2 for f in good]))
        out.q_source = "hits"
        if len(good) > 1:
            out.tau_std_s, out.q_std = robust_std(tv), robust_std(qv)

    # ---- stacked fit of the hit-averaged envelope power (weak modes)
    if len(stack_starts) >= 2 and pn is not None:
        n_st = len(stack_starts)
        valid = stack_cnt >= max(2, int(np.ceil(0.5 * n_st)))
        P = stack_sum / np.maximum(stack_cnt, 1)
        P_slow = centered_mean(P, 3 * w_s)
        s_c = int(np.median(stack_starts))
        stop = int(np.argmin(valid[s_c:])) + s_c if not valid[s_c:].all() else len(P)
        if stop - s_c > min_len:
            thr = 2 * pn / np.sqrt(stack_cnt[s_c:stop])
            below = np.flatnonzero(P_slow[s_c:stop] - pn < thr)
            e_c = s_c + (int(below[0]) if len(below) else stop - s_c)
            if e_c - s_c >= min_len:
                cnt = stack_cnt[s_c:e_c]
                p_sig = np.maximum(P[s_c:e_c] - pn, 0.05 * pn / np.sqrt(cnt))
                t = np.arange(s_c, e_c) / fs
                slope, _, r2 = _log_fit(t, p_sig, pn, cnt)
                decay_db = -20 * slope * (t[-1] - t[0]) / np.log(10)
                if slope < 0 and decay_db >= params.min_decay_db:
                    out.tau_stack_s, out.r2_stack = -1.0 / slope, r2
        if out.tau_stack_s and out.n_fitted < 3:
            f_ref = out.f_hz if out.f_hz is not None else peak.f_hz
            out.tau_s = out.tau_stack_s
            out.q = float(np.pi * f_ref * out.tau_stack_s)
            out.r2 = out.r2_stack
            out.q_source = "stack"

    # ---- no measurable Q: a lower bound (slow decay) or no decay at all (steady line)
    if out.q is None:
        lower = [f.q_lower for f in out.fits if f.q_lower]
        steady = [f.steady_s for f in out.fits if f.steady_s]
        if steady and len(steady) >= len(lower):
            out.steady_s = float(max(steady))
        elif lower:
            out.q_lower = float(np.median(lower))
    return out


def fit_peaks(x: np.ndarray, fs: float, seg: Segmentation, spec: Spectra | None, peaks: list[Peak],
              params: Params, traces_for: int = 4) -> list[PeakDecay | None]:
    """Decay fits for the strongest `max_decay_peaks` peaks (others: None)."""
    if spec is None or not seg.hits:
        return [None] * len(peaks)
    order = sorted(range(len(peaks)), key=lambda i: -peaks[i].above_floor_db)
    chosen = set(order[: params.max_decay_peaks])
    traced = set(order[:traces_for])
    out = []
    for i, p in enumerate(peaks):
        if i not in chosen:
            out.append(None)
            continue
        others = [q.f_hz for j, q in enumerate(peaks) if j != i]
        out.append(fit_peak(x, fs, seg, spec, p, others, params, want_traces=i in traced))
    return out
