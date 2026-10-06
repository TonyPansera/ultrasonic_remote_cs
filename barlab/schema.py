"""result.json: build the rounded, compact result dict and serialise it.

Every key is documented in docs/result_schema.md (a test enforces this). Bump
SCHEMA_VERSION on any incompatible change.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import numpy as np

from barlab import __version__

SCHEMA_VERSION = 1
PER_HIT_PEAKS = 3  # per-hit arrays are kept for the strongest N peaks only
PER_HIT_MAX_HITS = 60
MAX_THEORY_MODES = 40


def num(x, nd: int):
    if x is None:
        return None
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def sig(x, n: int = 3):
    if x is None:
        return None
    x = float(x)
    if not math.isfinite(x):
        return None
    if x == 0:
        return 0.0
    v = round(x, n - 1 - int(math.floor(math.log10(abs(x)))))
    return float(int(v)) if abs(v) >= 10 ** (n - 1) and v == int(v) else v


def clean(o):
    """numpy -> python, tuples -> lists, non-finite floats -> None."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, (np.bool_, bool)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return float(o) if math.isfinite(float(o)) else None
    return o


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _theory_json(th: dict) -> dict:
    nm = th["nearest"]
    nearest = None if nm is None else {
        "bar": nm["bar"], "family": nm["family"], "n": nm["n"], "model": nm["model"],
        "f_hz": num(nm["f_hz"], 1), "rel_err": sig(nm["rel_err"]), "unambiguous": nm["unambiguous"],
        "next_family": nm["next_family"], "next_n": nm["next_n"], "next_f_hz": num(nm["next_f_hz"], 1)}
    implied = None if th["implied_L_mm"] is None else [
        {"family": h["family"], "n": h["n"], "L_mm": num(h["L_mm"], 2)} for h in th["implied_L_mm"]]
    return {"nearest": nearest, "matches": th["matches"], "implied_L_mm": implied}


def _decay_json(d, fs: float, per_hit: bool) -> dict:
    if d is None:
        return {"band_hz": None, "n_tried": 0, "n_fitted": 0, "n_freq": 0, "tau_ms": None, "tau_std_ms": None,
                "q": None, "q_std": None, "q_range": None, "r2": None, "q_source": None, "q_stack": None,
                "q_lower": None, "steady_s": None}
    f_ref = d.f_hz or d.f_center
    out = {
        "band_hz": [num(d.band_hz[0], 1), num(d.band_hz[1], 1)],
        "n_tried": d.n_tried, "n_fitted": d.n_fitted, "n_freq": d.n_freq,
        "tau_ms": sig(None if d.tau_s is None else d.tau_s * 1e3),
        "tau_std_ms": sig(None if d.tau_std_s is None else d.tau_std_s * 1e3),
        "q": sig(d.q), "q_std": sig(d.q_std),
        "q_range": None if d.q_lo is None else [sig(d.q_lo), sig(d.q_hi)],
        "r2": num(d.r2, 3), "q_source": d.q_source,
        "q_stack": sig(None if d.tau_stack_s is None else np.pi * f_ref * d.tau_stack_s),
        "q_lower": sig(d.q_lower), "steady_s": num(d.steady_s, 2),
    }
    if per_hit and len(d.fits) <= PER_HIT_MAX_HITS:
        out["per_hit"] = {
            "hit": [f.hit for f in d.fits],
            "f_hz": [num(f.f_hz, 1) for f in d.fits],
            "q": [sig(f.q) for f in d.fits],
            "r2": [num(f.r2, 3) for f in d.fits],
            "reason": [f.reason for f in d.fits],
        }
    return out


def peak_json(po: dict, fs: float, per_hit: bool) -> dict:
    p, d = po["peak"], po["decay"]
    return {
        "i": po["i"], "f_hz": num(po["f_hz"], 1), "f_psd_hz": num(p.f_hz, 1),
        "f_std_hz": sig(d.f_std_hz) if d else None, "f_sem_hz": sig(d.f_sem_hz) if d else None,
        "f_range_rel": sig(d.f_range_rel) if d else None,
        "level_db": num(p.level_db, 1), "floor_db": num(p.floor_db, 1),
        "above_floor_db": num(p.above_floor_db, 1), "snr_db": num(p.snr_db, 1),
        "noise_line_db": num(p.noise_line_db, 1), "prominence_db": num(p.prominence_db, 1),
        "width_hz": num(p.width_hz, 1), "q_width": sig(p.q_width),
        "decay": _decay_json(d, fs, per_hit),
        "class": po["class"], "flags": po["flags"], "reasons": po["reasons"],
        "theory": _theory_json(po["theory"]),
    }


def build_result(*, file_info: dict, warnings: list[dict], meta: dict, seg, spec, peaks_out: list[dict],
                 modes: list[dict], validity: list[str], bars_known: bool, summary: dict,
                 params: dict, plots: dict, ultra_ratio_db) -> dict:
    fs = file_info["fs_hz"]
    order = sorted(peaks_out, key=lambda po: -po["peak"].above_floor_db)
    per_hit_ids = {po["i"] for po in order[:PER_HIT_PEAKS]}
    hits = [{"i": h.index, "onset_s": num(h.onset / fs, 4), "peak_dbfs": num(h.peak_db, 1),
             "rise_db": num(h.rise_db, 1), "ringdown_ms": num((h.ringdown[1] - h.ringdown[0]) / fs * 1e3, 1),
             "end_reason": h.end_reason, "clip_trim_ms": num(h.clip_trim / fs * 1e3, 1),
             "clipped": bool(h.clipped_ringdown)} for h in seg.hits]
    spectra = None if spec is None else {
        "nperseg": int(np.median(spec.nperseg_hits)), "nperseg_target": spec.nperseg_target,
        "nperseg_noise": spec.nperseg_noise, "nfft": spec.nfft, "bin_hz": num(spec.bin_hz, 2),
        "resolution_hz": num(spec.resolution_hz, 1), "enbw_hz": num(spec.enbw_hz, 1),
        "resolution_ok": bool(spec.resolution_hz <= params["target_res_hz"] + 1e-9),
        "n_hits_used": len(spec.hits_used), "noise_s": num(spec.noise_seconds, 3),
        "ultrasonic_peak_snr_db": num(ultra_ratio_db, 1),
    }
    result = {
        "schema_version": SCHEMA_VERSION,
        "barlab_version": __version__,
        "analyzed_at": now_iso(),
        "file": file_info,
        "warnings": warnings,
        "meta": meta,
        "segmentation": {
            "n_hits": len(seg.hits), "threshold_db": num(seg.threshold_db, 1),
            "quiet_level_dbfs": num(seg.quiet_level_db, 1), "end_threshold_db": num(seg.end_threshold_db, 1),
            "end_frame_ms": num(seg.end_frame / fs * 1e3, 2),
            "noise_s": [[num(a / fs, 3), num(b / fs, 3)] for a, b in seg.noise],
            "noise_total_s": num(sum(b - a for a, b in seg.noise) / fs, 3),
            "noise_fallback": bool(seg.noise_fallback),
        },
        "hits": hits,
        "spectra": spectra,
        "peaks": [peak_json(po, fs, po["i"] in per_hit_ids) for po in peaks_out],
        "theory": {
            "available": bool(modes), "bars_known": bars_known, "validity": validity,
            "modes": [{"bar": m["bar"], "family": m["family"], "n": m["n"], "model": m["model"],
                       "f_hz": num(m["f_hz"], 1), "f_simple_hz": num(m["f_simple_hz"], 1)}
                      for m in modes[:MAX_THEORY_MODES]],
            "n_modes": len(modes),
        },
        "summary": {**summary, "ringing_hz": num(summary["ringing_hz"], 1),
                    "ringing_std_hz": sig(summary["ringing_std_hz"]),
                    "ringing_sem_hz": sig(summary["ringing_sem_hz"]), "q": sig(summary["q"]),
                    "q_lower": sig(summary["q_lower"])},
        "params": params,
        "plots": plots,
    }
    return clean(result)


def _dump(o, level: int, ind: int = 1, width: int = 130) -> str:
    pad = " " * (ind * level)
    if isinstance(o, dict):
        if not o:
            return "{}"
        flat = json.dumps(o, ensure_ascii=False)
        if level > 0 and len(flat) + len(pad) <= width:
            return flat
        items = [f"{pad}{' ' * ind}{json.dumps(k)}: {_dump(v, level + 1, ind, width)}" for k, v in o.items()]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    if isinstance(o, list):
        flat = json.dumps(o, ensure_ascii=False)
        if all(not isinstance(v, (dict, list)) for v in o) or len(flat) + len(pad) <= width:
            return flat
        items = [pad + " " * ind + _dump(v, level + 1, ind, width) for v in o]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    return json.dumps(o, ensure_ascii=False)


def dumps(result: dict) -> str:
    """Compact, readable JSON: one key per line at the top, short objects and scalar lists inline."""
    return _dump(result, 0) + "\n"


def load_result(path) -> dict:
    with open(path) as f:
        return json.load(f)
