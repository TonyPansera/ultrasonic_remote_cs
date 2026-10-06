"""Peak classification and the per-file summary.

Primary class, first rule that applies:
  possible_alias        fs < 96 kHz, or f > alias_frac * Nyquist
  noise_or_environment  ringdown PSD within env_snr_db of the noise PSD at the peak:
                        the line is present in the noise window at a comparable level
  unresolved (steady)   no measurable decay at all: a constant tone, not a struck-bar ringdown
  broadband_resonance   decay Q < q_min, or (no decay Q measurable and) the peak is wider than
                        max(3*resolution, f/q_min)
  bar_mode_candidate    Q >= q_min (measured, or a lower bound when the decay is too slow
                        to measure), >= min_hits_fitted hits with a frequency, per-hit
                        frequency spread (max-min)/median < f_spread_max
  unresolved            anything else (no valid decay fit, single hit, inconsistent hits);
                        `reasons` says why. Never a confident label that was not earned.
`flags` carry the non-exclusive evidence; `theory` the nearest expected mode (when the bar
length is known) or the implied bar lengths (when it is not).
"""

from __future__ import annotations

import numpy as np

from barlab.decay import PeakDecay
from barlab.params import Params
from barlab.spectra import Peak, Spectra
from barlab.theory import Bar, implied_lengths, nearest_mode

MAX_IMPLIED = 5


def _peak_class(p: Peak, d: PeakDecay | None, f: float, fs: float, spec: Spectra, params: Params):
    nyq = fs / 2
    flags, reasons = [], []
    fs_low = fs < params.fs_min_hz
    near_nyq = f > params.alias_frac * nyq
    if fs_low:
        flags.append("fs_below_96k")
    if near_nyq:
        flags.append("near_nyquist")
    if p.noise_line_db >= params.noise_line_db:
        flags.append("line_in_noise")
    if p.resolution_limited:
        flags.append("resolution_limited")
    wide = bool(np.isfinite(p.width_hz) and p.width_hz > max(3 * spec.resolution_hz, f / params.q_min))
    if wide:
        flags.append("wide")
    q = d.q if d else None
    q_low = d.q_lower if d else None
    steady = d.steady_s if d else None
    high_q = (q is not None and q >= params.q_min) or (q is None and q_low is not None and q_low >= params.q_min)
    if high_q:
        flags.append("high_q")
    if q is None and q_low is not None:
        flags.append("q_lower_bound")
    if steady:
        flags.append("steady")
    consistent = bool(d and d.f_range_rel is not None and d.f_range_rel < params.f_spread_max)
    if consistent:
        flags.append("consistent")
    if d is None:
        flags.append("not_fitted")
    else:
        if d.q_source == "stack":
            flags.append("q_from_stack")
        if d.narrow_band:
            flags.append("narrow_band")
        if d.n_freq == 1:
            flags.append("single_hit")

    if fs_low or near_nyq:
        cls = "possible_alias"
        reasons.append("fs < 96 kHz: content above fs/2 folds back, so this peak may be an alias"
                       if fs_low else f"f > {params.alias_frac:.0%} of Nyquist: may be an alias or ADC artefact")
    elif p.snr_db < params.env_snr_db:
        cls = "noise_or_environment"
        reasons.append(f"also in the noise window at a comparable level (ringdown/noise {p.snr_db:.1f} dB "
                       f"< {params.env_snr_db:.0f} dB)")
    elif steady:
        cls = "unresolved"
        reasons.append(f"no measurable decay over {steady:.2f} s: a steady tone (electronic/external source?) "
                       f"or Q > {params.q_max_plausible:.0e}, implausible for a struck metal bar")
    elif q is not None and q < params.q_min:
        cls = "broadband_resonance"
        reasons.append(f"Q = {q:.0f} < {params.q_min:.0f}: decays too fast for a bar mode")
    elif wide and not high_q:  # a measured high decay-Q outranks a PSD width broadened by windows/beating
        cls = "broadband_resonance"
        reasons.append(f"-3 dB width {p.width_hz:.0f} Hz is wide and no decay Q could be measured")
    elif high_q and d.n_freq >= params.min_hits_fitted and consistent:
        cls = "bar_mode_candidate"
        if q is None:
            reasons.append(f"decay too slow to measure within the windows: Q > {q_low:.3g} (lower bound)")
    else:
        cls = "unresolved"
        if d is None:
            reasons.append("no decay fit (not among the strongest peaks)")
        elif q is None and q_low is None:
            reasons.append("no valid decay fit (" + ", ".join(sorted({x.reason for x in d.fits if x.reason})) + ")")
        elif d.n_freq < params.min_hits_fitted:
            reasons.append(f"only {d.n_freq} hit(s) with a frequency estimate: consistency not testable")
        elif not consistent:
            reasons.append(f"per-hit frequency spread {d.f_range_rel:.2%} >= {params.f_spread_max:.0%}")
    return cls, flags, reasons


def classify(peaks: list[Peak], decays: list[PeakDecay | None], fs: float, spec: Spectra | None,
             modes: list[dict], bars: list[Bar], params: Params) -> list[dict]:
    """One dict per peak (frequency order): measured values + class + theory (unrounded)."""
    out = []
    unknown_L = [b for b in bars if b.L is None]
    for i, (p, d) in enumerate(zip(peaks, decays)):
        f = d.f_hz if d is not None and d.f_hz is not None else p.f_hz
        cls, flags, reasons = _peak_class(p, d, f, fs, spec, params)
        theory = {"nearest": None, "matches": False, "implied_L_mm": None}
        if modes and cls != "possible_alias":  # a possibly folded frequency can't be compared with theory
            nm = nearest_mode(f, modes, params.theory_unambiguous)
            theory["nearest"] = nm
            theory["matches"] = bool(abs(nm["rel_err"]) <= params.theory_tol and nm["unambiguous"])
            if theory["matches"]:
                flags.append("matches_theory")
        out.append({"i": i, "f_hz": f, "peak": p, "decay": d, "class": cls, "flags": flags,
                    "reasons": reasons, "theory": theory})
    # bar length unknown: implied lengths for the strongest candidates (FE root-finding is not free)
    if unknown_L and fs >= params.fs_min_hz:
        b = unknown_L[0]
        cands = sorted((po for po in out if po["class"] == "bar_mode_candidate"),
                       key=lambda po: -po["peak"].above_floor_db)[:MAX_IMPLIED]
        for po in cands:
            po["theory"]["implied_L_mm"] = [{"family": h["family"], "n": h["n"], "L_mm": h["L_mm"]}
                                            for h in implied_lengths(po["f_hz"], b.d, b.material, b.bc)]
    return out


def summarize(peaks_out: list[dict], warnings: list[dict], n_hits: int, fs: float, params: Params) -> dict:
    cands = [p for p in peaks_out if p["class"] == "bar_mode_candidate"]
    crit = [w["code"] for w in warnings if w["severity"] == "CRITICAL"]
    s = {"determinable": False, "reason": "", "critical": crit, "n_candidates": len(cands),
         "primary_peak": None, "ringing_hz": None, "ringing_std_hz": None, "ringing_sem_hz": None,
         "q": None, "q_lower": None, "theory_match": None}
    if fs < params.fs_min_hz:
        s["reason"] = (f"fs = {fs:.0f} Hz < 96 kHz: ultrasonic bar modes cannot be observed and any "
                       "peak found may be an alias")
    elif n_hits == 0:
        s["reason"] = "no impacts detected"
    elif not cands:
        s["reason"] = (f"no peak meets the bar-mode criteria (Q >= {params.q_min:.0f}, consistent over "
                       f">= {params.min_hits_fitted} hits, not present in the noise window)")
    else:
        best = max(cands, key=lambda p: p["peak"].above_floor_db)
        d = best["decay"]
        s.update(determinable=True, primary_peak=best["i"], ringing_hz=best["f_hz"],
                 ringing_std_hz=d.f_std_hz, ringing_sem_hz=d.f_sem_hz, q=d.q, q_lower=d.q_lower,
                 reason=f"{len(cands)} bar-mode candidate(s); strongest at {best['f_hz']:.1f} Hz")
        nm = best["theory"]["nearest"]
        if nm is not None:
            s["theory_match"] = f"{nm['family']} n={nm['n']} ({nm['rel_err']:+.2%})"
    return s
