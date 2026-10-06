"""One file end to end:
load -> checks -> (per channel) segment -> spectra -> peaks -> decay fits -> theory ->
classify -> result.json + PNGs -> results/index.csv.

Deterministic (no LLM in the loop), never resamples, never writes below data/raw/.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from barlab import io, schema, store, theory
from barlab.classify import classify, summarize
from barlab.decay import fit_peaks
from barlab.meta import bars_from_meta, load_meta, meta_for_json
from barlab.params import Params
from barlab.paths import Project
from barlab.segment import samples, segment
from barlab.spectra import compute_spectra, find_peaks


def _clip_mask(rec: io.Recording, c: int) -> np.ndarray:
    x = rec.data[:, c]
    if rec.is_float or rec.bit_depth is None:
        return np.abs(x) >= 0.999
    top = (2**31 - 2 ** (32 - rec.bit_depth)) / 2**31
    return (x >= top - 1e-12) | (x <= -1.0)


def _result_dir(project: Project, stem: str, sha: str) -> Path:
    d = project.results / stem
    rj = d / "result.json"
    if rj.is_file():
        try:
            old = json.loads(rj.read_text())["file"]["sha256"]
        except (OSError, ValueError, KeyError, TypeError):
            old = None
        if old and old != sha:
            d = project.results / f"{stem}_{sha[:8]}"
    return d


def analyze_file(path: Path, project: Project, params: Params | None = None, meta_cli: dict | None = None,
                 make_plots: bool = True) -> dict:
    params = params or Params()
    path = Path(path).resolve()
    rec = io.load(path)
    fs = rec.fs
    warns = io.check_recording(rec, params)

    # --- every channel is segmented; the one with the clearest hits is analysed
    chans = []
    for c in range(rec.n_channels):
        xc = rec.channel(c)
        sc = segment(xc, fs, params)
        rise = float(np.median([h.rise_db for h in sc.hits])) if sc.hits else 0.0
        chans.append({"channel": c, "x": xc, "seg": sc, "rise": rise})
    best = max(chans, key=lambda ch: (len(ch["seg"].hits) > 0, ch["rise"], len(ch["seg"].hits)))
    c_used, x, seg = best["channel"], best["x"], best["seg"]
    channel_summary = [{"channel": ch["channel"], "n_hits": len(ch["seg"].hits),
                        "median_rise_db": schema.num(ch["rise"], 1),
                        "peak_dbfs": schema.num(rec.peak_dbfs[ch["channel"]], 1),
                        "rms_dbfs": schema.num(rec.rms_dbfs[ch["channel"]], 1),
                        "dc_offset_fs": schema.sig(rec.dc_offset[ch["channel"]]),
                        "clip_fraction": schema.sig(rec.clip_fraction[ch["channel"]])} for ch in chans]
    warns += seg.warnings

    # --- clipping: harmless in the attack; inside the ringdown it adds broadband distortion, so
    # the ringdown restarts after the last clipped sample (hit excluded if too little remains)
    clip = _clip_mask(rec, c_used)
    if clip.any() and seg.hits:
        n_trim = n_excl = n_att = 0
        guard = samples(fs, params.clip_guard_ms)
        min_ring = samples(fs, params.min_ringdown_ms)
        for h in seg.hits:
            n_att += bool(clip[h.attack[0]:h.attack[1]].any())
            a, b = h.ringdown
            hit_clip = np.flatnonzero(clip[a:b])
            if not len(hit_clip):
                continue
            new_a = a + int(hit_clip[-1]) + 1 + guard
            if b - new_a >= min_ring:
                h.ringdown, h.clip_trim = (new_a, b), new_a - a
                n_trim += 1
            else:
                h.clipped_ringdown = True
                n_excl += 1
        if n_trim or n_excl:
            warns.append(io.warning(
                "WARNING", "CLIPPING_RINGDOWN",
                f"{n_trim + n_excl} of {len(seg.hits)} hits clip after the attack: {n_trim} ringdown window(s) start "
                f"{params.clip_guard_ms:g} ms after the last clipped sample, {n_excl} hit(s) excluded; clipping "
                "spreads energy over the whole band (lower the gain or strike softer)"))
        elif n_att:
            warns.append(io.warning("INFO", "CLIPPING_ATTACK_ONLY",
                                    f"clipping only during the attack of {n_att} hit(s): harmless for the ringdown"))

    # --- spectra, peaks, decay
    spec = compute_spectra(x, fs, seg, params)
    ultra = None
    if spec is not None:
        ultra, w = io.check_ultrasonic_energy(spec.freqs, spec.ring_psd, spec.noise_psd, fs, params)
        if w:
            warns.append(w)
        if spec.resolution_hz > params.target_res_hz + 1e-9:
            warns.append(io.warning(
                "INFO", "RESOLUTION_COARSE",
                f"ringdown windows (median {np.median(spec.nperseg_hits) / fs * 1e3:.1f} ms) allow "
                f"{spec.resolution_hz:.0f} Hz PSD resolution, not <= {params.target_res_hz:.0f} Hz; per-hit "
                "frequencies come from phase fits and do not depend on it"))
    peaks = find_peaks(spec, fs, params)
    decays = fit_peaks(x, fs, seg, spec, peaks, params)

    # --- theory from metadata (sidecars / CLI); never invented
    meta = load_meta(project.meta, path.stem, meta_cli, root=project.root)
    bars = bars_from_meta(meta)
    known = [b for b in bars if b.L is not None]
    modes = theory.mode_table(known, fmax=fs / 2)
    validity = [v for b in known for v in theory.validity(b)]

    peaks_out = classify(peaks, decays, fs, spec, modes, bars, params)
    warns.sort(key=lambda w: -io.SEVERITY_ORDER[w["severity"]])
    summary = summarize(peaks_out, warns, len(seg.hits), fs, params)

    out_dir = _result_dir(project, path.stem, rec.sha256)
    project.ensure_not_raw(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    plots = {}
    if make_plots:
        from barlab import plots as plotting

        plots = plotting.make_all(out_dir, x=x, fs=fs, seg=seg, spec=spec, peaks_out=peaks_out,
                                  modes=modes, title=path.stem, params=params)

    file_info = {
        "path": project.rel(path), "stem": path.stem, "sha256": rec.sha256, "source": project.source_of(path),
        "result_dir": project.rel(out_dir), "fs_hz": fs, "nyquist_hz": fs / 2, "format": rec.format,
        "subtype": rec.subtype, "bit_depth": rec.bit_depth, "effective_bits": rec.effective_bits,
        "channels": rec.n_channels, "channel_used": c_used, "duration_s": schema.num(rec.duration_s, 4),
        "n_frames": rec.n_frames, "recorded_at": rec.device.get("Timestamp"), "device": rec.device,
        "channel_summary": channel_summary,
    }
    result = schema.build_result(
        file_info=file_info, warnings=warns, meta=meta_for_json(meta), seg=seg, spec=spec,
        peaks_out=peaks_out, modes=modes, validity=validity, bars_known=bool(known), summary=summary,
        params=params.to_dict(), plots=plots, ultra_ratio_db=ultra)
    (out_dir / "result.json").write_text(schema.dumps(result))
    store.upsert(project.index, store.rows_from_result(result))
    return result
