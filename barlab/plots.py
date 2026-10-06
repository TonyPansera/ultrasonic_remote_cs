"""Per-file PNG figures (matplotlib, Agg).

Rules: linear frequency axes from 0 to Nyquist; one y-scale per panel (stacked panels,
never a twin axis); colour encodes the peak *class* only and always the same class ->
the same colour (plus a marker shape, so colour is never the only cue); data lines are
neutral ink; spectrograms use a single-hue light->dark ramp. Max-pooling below is for
display only - the analysis never resamples.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from numpy.lib.stride_tricks import sliding_window_view  # noqa: E402
from scipy import signal  # noqa: E402

from barlab.params import Params  # noqa: E402
from barlab.segment import Segmentation, pow2_at_least  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
CLASS_STYLE = {  # fixed: colour follows the class, never its rank
    "bar_mode_candidate": (BLUE, "o", "bar-mode candidate"),
    "broadband_resonance": (ORANGE, "s", "broadband resonance"),
    "noise_or_environment": (AQUA, "D", "noise / environment"),
    "possible_alias": (YELLOW, "^", "possible alias"),
    "unresolved": (MUTED, "X", "unresolved"),
}
SEQ = LinearSegmentedColormap.from_list(
    "barlab_blue", [SURFACE, "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"])
RC = {
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "grid.linestyle": "-",
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 9, "axes.titlesize": 10,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "legend.frameon": False,
    "legend.fontsize": 8, "lines.linewidth": 1.0, "lines.solid_capstyle": "round", "savefig.dpi": 120,
}
FAMILY_TAG = {"longitudinal": "L", "flexural": "F", "torsional": "T"}


def _save(fig, path: Path) -> str:
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path.name


def _placeholder(path: Path, title: str, text: str) -> str:
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(8, 2.2))
        ax.axis("off")
        ax.set_title(title)
        ax.text(0.5, 0.5, text, ha="center", va="center", color=INK2, transform=ax.transAxes)
        return _save(fig, path)


def _minmax(x: np.ndarray, fs: float, n_cols: int = 4000):
    k = max(1, len(x) // n_cols)
    m = len(x) // k
    blocks = x[: m * k].reshape(m, k)
    return (np.arange(m) * k + k / 2) / fs, blocks.min(axis=1), blocks.max(axis=1)


def waveform(path: Path, x: np.ndarray, fs: float, seg: Segmentation, title: str) -> str:
    with plt.rc_context(RC):
        fig, (a1, a2) = plt.subplots(2, 1, sharex=True, figsize=(11, 5.4), height_ratios=[3, 2])
        t, lo, hi = _minmax(x, fs)
        a1.fill_between(t, lo, hi, color=BLUE, lw=0, step="mid")
        for a, b in seg.noise:
            a1.axvspan(a / fs, b / fs, color=MUTED, alpha=0.12, lw=0)
        for h in seg.hits:
            a1.axvspan(h.attack[0] / fs, h.attack[1] / fs, color=ORANGE, alpha=0.55, lw=0)
            a1.axvspan(h.ringdown[0] / fs, h.ringdown[1] / fs, color=AQUA, alpha=0.22, lw=0)
        step = max(1, len(seg.hits) // 30)
        for h in seg.hits[::step]:
            a1.annotate(str(h.index), (h.onset / fs, 1.0), xycoords=("data", "axes fraction"),
                        ha="center", va="bottom", fontsize=7, color=INK2)
        a1.set_ylabel("amplitude (FS)")
        a1.set_title(f"{title}: waveform, {len(seg.hits)} hits (numbers = hit index)", pad=14)
        a1.legend(handles=[Patch(color=ORANGE, alpha=0.55, label="attack (excluded)"),
                           Patch(color=AQUA, alpha=0.3, label="ringdown (analysed)"),
                           Patch(color=MUTED, alpha=0.25, label="noise reference")],
                  loc="upper right", ncols=3)
        tr, rise = seg.traces["rise_db"]
        a2.plot(tr, rise, color=INK2, lw=0.8)
        a2.axhline(seg.threshold_db, color=ORANGE, lw=1.0)
        a2.annotate(f"threshold {seg.threshold_db:.1f} dB", (1.0, seg.threshold_db), xycoords=("axes fraction", "data"),
                    ha="right", va="bottom", fontsize=8, color=INK2)
        a2.set_ylabel("rise over trailing\n20 ms level (dB)")
        a2.set_xlabel("time (s)")
        a2.set_xlim(0, len(x) / fs)
        return _save(fig, path)


def _stft_maxpool(x: np.ndarray, fs: float, nper: int, hop: int, n_cols: int):
    """Power spectrogram [dB re FS^2/Hz], max-pooled over frames for display (bounded memory)."""
    win = signal.windows.hann(nper, sym=False)
    scale = 2.0 / (fs * np.sum(win**2))
    frames = sliding_window_view(x, nper)[::hop]
    n_frames = len(frames)
    k = max(1, int(np.ceil(n_frames / n_cols)))
    cols = []
    for c0 in range(0, n_frames, k * 64):
        block = frames[c0:c0 + k * 64]
        spec = np.abs(np.fft.rfft(block * win, axis=1)) ** 2 * scale
        m = len(spec) // k
        pooled = spec[: m * k].reshape(m, k, -1).max(axis=1) if m else spec.max(axis=0, keepdims=True)
        if m and len(spec) > m * k:
            pooled = np.vstack([pooled, spec[m * k:].max(axis=0, keepdims=True)])
        cols.append(pooled)
    S = np.vstack(cols).T
    return 10 * np.log10(S + 1e-30), k * hop / fs


def spectrogram(path: Path, x: np.ndarray, fs: float, seg: Segmentation, title: str) -> str:
    nper = pow2_at_least(fs * 2.6e-3)
    S, col_dt = _stft_maxpool(x, fs, nper, nper // 2, n_cols=2400)
    vmin = float(np.median(S))  # noise floor -> lightest; everything above it reads darker
    vmax = max(float(np.percentile(S, 99.99)), vmin + 30)
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(11, 4.6))
        im = ax.imshow(S, origin="lower", aspect="auto", cmap=SEQ, vmin=vmin, vmax=vmax,
                       extent=[0, S.shape[1] * col_dt, 0, fs / 2e3], interpolation="nearest")
        ax.grid(False)
        for h in seg.hits:
            ax.axvline(h.onset / fs, ymin=0.97, ymax=1.0, color=INK, lw=1.0)
        cb = fig.colorbar(im, ax=ax, pad=0.01)
        cb.set_label("dB re FS²/Hz", color=INK2)
        cb.outline.set_visible(False)
        ax.set_ylim(0, fs / 2e3)
        ax.set_xlabel("time (s)")
        ax.set_ylabel("frequency (kHz, linear to Nyquist)")
        ax.set_title(f"{title}: spectrogram (window {nper / fs * 1e3:.2f} ms, max over "
                     f"{col_dt * 1e3:.1f} ms per column); ticks on top = hit onsets")
        return _save(fig, path)


def _class_legend(classes) -> list:
    return [Line2D([], [], color=CLASS_STYLE[c][0], marker=CLASS_STYLE[c][1], lw=0, markersize=7,
                   markeredgecolor=SURFACE, label=CLASS_STYLE[c][2]) for c in CLASS_STYLE if c in classes]


def _mark_peaks(ax, peaks_out, label_top: int = 6, label_fmt="{:.3f}"):
    ranked = sorted(peaks_out, key=lambda p: -p["peak"].above_floor_db)
    labelled = {p["i"] for p in ranked[:label_top]} | {p["i"] for p in peaks_out if p["class"] == "bar_mode_candidate"}
    for p in peaks_out:
        color, marker, _ = CLASS_STYLE[p["class"]]
        f, lvl = p["f_hz"] / 1e3, p["peak"].level_db
        ax.plot([f], [lvl + 3], marker=marker, color=color, markersize=8, markeredgecolor=SURFACE,
                markeredgewidth=1.5, lw=0, zorder=5)
        if p["i"] in labelled:
            ax.annotate(label_fmt.format(f), (f, lvl + 3), xytext=(0, 7), textcoords="offset points",
                        ha="center", fontsize=7, color=INK2)


def psd(path: Path, spec, peaks_out: list[dict], modes: list[dict], fs: float, title: str,
        params: Params) -> str:
    if spec is None:
        return _placeholder(path, f"{title}: ringdown PSD", "no hits detected: no ringdown spectrum")
    fk = spec.freqs / 1e3
    with plt.rc_context(RC):
        fig = plt.figure(figsize=(11, 7.2))
        gs = fig.add_gridspec(2, 2, height_ratios=[3, 2], hspace=0.32, wspace=0.18)
        ax = fig.add_subplot(gs[0, :])
        ax.plot(fk, spec.noise_db, color=MUTED, lw=0.8, label="noise window")
        ax.plot(fk, spec.floor_db, color=AXIS, lw=1.2, label="noise floor (1 kHz median)")
        ax.plot(fk, spec.ring_db, color=INK, lw=0.9, label=f"ringdown, mean of {len(spec.hits_used)} hits")
        top = float(np.max(spec.ring_db))
        bottom = float(np.percentile(spec.noise_db, 1)) - 6
        for m in modes:
            ax.axvline(m["f_hz"] / 1e3, color=AXIS, lw=0.7, zorder=0)
            ax.annotate(f"{FAMILY_TAG[m['family']]}{m['n']}", (m["f_hz"] / 1e3, 1.0),
                        xycoords=("data", "axes fraction"), ha="center", va="bottom", fontsize=6.5, color=MUTED)
        _mark_peaks(ax, peaks_out)
        ax.set_xlim(0, fs / 2e3)
        ax.set_ylim(bottom, top + 14)
        ax.set_xlabel("frequency (kHz)")
        ax.set_ylabel("PSD (dB re FS²/Hz)")
        ttl = f"{title}: ringdown vs noise PSD (resolution {spec.resolution_hz:.0f} Hz)"
        if modes:
            ttl += "; grey lines = theory (L longitudinal, F flexural, T torsional)"
        ax.set_title(ttl, pad=12 if modes else 6)
        handles = ax.get_legend_handles_labels()[0] + _class_legend({p["class"] for p in peaks_out})
        ax.legend(handles=handles, loc="upper right", ncols=2)

        ax2 = fig.add_subplot(gs[1, 0])
        ax2.plot(fk, spec.snr_db, color=INK2, lw=0.7)
        ax2.axhline(params.env_snr_db, color=AQUA, lw=1.0)
        ax2.annotate(f"{params.env_snr_db:.0f} dB: below = also in noise window", (0.99, params.env_snr_db),
                     xycoords=("axes fraction", "data"), ha="right", va="bottom", fontsize=7, color=INK2)
        ax2.set_xlim(0, fs / 2e3)
        ax2.set_ylim(max(-15, float(np.percentile(spec.snr_db, 0.5)) - 3), float(np.max(spec.snr_db)) + 5)
        ax2.set_xlabel("frequency (kHz)")
        ax2.set_ylabel("ringdown / noise (dB)")
        ax2.set_title("per-bin SNR")

        ax3 = fig.add_subplot(gs[1, 1])
        if peaks_out:
            cands = [p for p in peaks_out if p["class"] == "bar_mode_candidate"]
            focus = max(cands or peaks_out, key=lambda p: p["peak"].above_floor_db)
            f0, half = focus["f_hz"] / 1e3, max(1.5, 30 * spec.resolution_hz / 1e3)
            sel = (fk >= f0 - half) & (fk <= f0 + half)
            for row in spec.hit_psd:
                ax3.plot(fk[sel], 10 * np.log10(row[sel] + 1e-30), color=INK, lw=0.5, alpha=0.18)
            ax3.plot(fk[sel], spec.noise_db[sel], color=MUTED, lw=0.8)
            ax3.plot(fk[sel], spec.ring_db[sel], color=INK, lw=1.2)
            _mark_peaks(ax3, [p for p in peaks_out if abs(p["f_hz"] / 1e3 - f0) <= half], label_fmt="{:.4f}")
            ax3.set_xlim(f0 - half, f0 + half)
            ax3.set_ylim(float(np.min(spec.noise_db[sel])) - 15, float(np.max(spec.ring_db[sel])) + 14)
            ax3.set_title(f"zoom on the strongest {'candidate' if cands else 'peak'} (thin = single hits)")
        else:
            ax3.text(0.5, 0.5, "no peaks above the noise floor", ha="center", va="center",
                     color=INK2, transform=ax3.transAxes)
            ax3.set_title("zoom")
        ax3.set_xlabel("frequency (kHz)")
        ax3.set_ylabel("PSD (dB re FS²/Hz)")
        return _save(fig, path)


def decay(path: Path, peaks_out: list[dict], title: str) -> str:
    traced = [p for p in sorted(peaks_out, key=lambda p: -p["peak"].above_floor_db)
              if p["decay"] is not None and any(f.trace is not None for f in p["decay"].fits)][:4]
    if not traced:
        return _placeholder(path, f"{title}: decay fits", "no decay fits available")
    n = len(traced)
    ncol = 2 if n > 1 else 1
    nrow = int(np.ceil(n / ncol))
    with plt.rc_context(RC):
        fig, axes = plt.subplots(nrow, ncol, figsize=(11 if ncol == 2 else 7, 3.4 * nrow), squeeze=False)
        for ax, p in zip(axes.ravel(), traced):
            d = p["decay"]
            color = CLASS_STYLE[p["class"]][0]
            t_end = max((f.t1_s or 0) for f in d.fits) * 1e3
            for f in d.fits:
                if f.trace is not None:
                    ax.plot(f.trace[0], f.trace[1], color=color, lw=0.8, alpha=0.45)
            for f in d.fits:  # accepted fits in ink; fits rejected (e.g. R^2 < r2_min) in grey
                if f.fit_db is not None:
                    ok = f.ok and f.reason is None
                    ax.plot(f.fit_db[0], f.fit_db[1], color=INK if ok else MUTED, lw=1.3 if ok else 0.9)
            if d.noise_db is not None:
                ax.axhline(d.noise_db, color=MUTED, lw=0.8)
                ax.annotate("band noise", (1.0, d.noise_db), xycoords=("axes fraction", "data"), ha="right",
                            va="bottom", fontsize=7, color=INK2)
            ax.set_xlim(-1, max(5.0, 1.4 * t_end))
            ymax = max(float(np.max(f.trace[1])) for f in d.fits if f.trace is not None)
            ax.set_ylim((d.noise_db if d.noise_db is not None else ymax - 60) - 8, ymax + 4)
            q = "n/a" if d.q is None else f"{d.q:.0f}"
            src = "" if d.q_source in (None, "hits") else f" ({d.q_source})"
            ax.set_title(f"{p['f_hz'] / 1e3:.3f} kHz · Q {q}{src} · {CLASS_STYLE[p['class']][2]} · "
                         f"{d.n_fitted}/{d.n_tried} hits fitted", fontsize=9)
            ax.set_xlabel("time since onset (ms)")
            ax.set_ylabel("band envelope power (dB)")
        for ax in axes.ravel()[n:]:
            ax.axis("off")
        fig.suptitle(f"{title}: ringdown decay per hit (colour = class; black = accepted fits, grey = rejected)",
                     x=0.01, ha="left", fontsize=10, fontweight="bold")
        fig.tight_layout()
        return _save(fig, path)


def hit_zoom(path: Path, x: np.ndarray, fs: float, seg: Segmentation, title: str) -> str:
    if not seg.hits:
        return _placeholder(path, f"{title}: single-hit spectrogram", "no hits detected")
    ok = [h for h in seg.hits if not h.clipped_ringdown] or seg.hits
    h = max(ok, key=lambda h: h.peak_db)
    a = max(0, h.onset - int(0.005 * fs))
    b = min(len(x), max(h.ringdown[1] + int(0.01 * fs), h.onset + int(0.03 * fs)), h.onset + int(0.3 * fs))
    nper = pow2_at_least(fs * 0.6e-3)
    f, t, S = signal.spectrogram(x[a:b], fs=fs, window="hann", nperseg=nper, noverlap=nper - nper // 8,
                                 scaling="density", mode="psd")
    S_db = 10 * np.log10(S + 1e-30)
    vmin = float(np.median(S_db))  # noise floor -> lightest
    vmax = max(float(S_db.max()), vmin + 30)
    t_ms = (t + a / fs - h.onset / fs) * 1e3
    with plt.rc_context(RC):
        fig, ax = plt.subplots(figsize=(9, 4.6))
        im = ax.pcolormesh(t_ms, f / 1e3, S_db, cmap=SEQ, vmin=vmin, vmax=vmax, shading="nearest")
        ax.grid(False)
        t_hi = float(t_ms.max())
        att_ms = (h.attack[1] - h.onset) / fs * 1e3
        r0_ms = (h.ringdown[0] - h.onset) / fs * 1e3
        r1_ms = min((h.ringdown[1] - h.onset) / fs * 1e3, t_hi)
        ax.axvspan(0.0, att_ms, ymin=0.965, ymax=1.0, color=ORANGE, lw=0)
        if r0_ms > att_ms:  # clipped start of the ringdown, excluded
            ax.axvspan(att_ms, r0_ms, ymin=0.965, ymax=1.0, color=MUTED, lw=0)
        ax.axvspan(r0_ms, r1_ms, ymin=0.965, ymax=1.0, color=AQUA, lw=0)
        ax.annotate("attack", (0.0, 1.0), xycoords=("data", "axes fraction"), ha="right", va="bottom",
                    fontsize=7, color=INK2, xytext=(-2, 2), textcoords="offset points")
        label = "ringdown window" + (" (continues)" if (h.ringdown[1] - h.onset) / fs * 1e3 > t_hi else "")
        ax.annotate(label, ((r0_ms + r1_ms) / 2, 1.0), xycoords=("data", "axes fraction"),
                    ha="center", va="bottom", fontsize=7, color=INK2, xytext=(0, 2), textcoords="offset points")
        ax.set_xlim(float(t_ms.min()), t_hi)
        cb = fig.colorbar(im, ax=ax, pad=0.01)
        cb.set_label("dB re FS²/Hz", color=INK2)
        cb.outline.set_visible(False)
        ax.set_ylim(0, fs / 2e3)
        ax.set_xlabel("time since onset (ms)")
        ax.set_ylabel("frequency (kHz)")
        ax.set_title(f"{title}: hit {h.index}, the broadband click (vertical) vs the ringing modes (horizontal)",
                     pad=14)
        return _save(fig, path)


def make_all(out_dir: Path, *, x, fs, seg, spec, peaks_out, modes, title, params) -> dict:
    out_dir = Path(out_dir)
    return {
        "waveform": waveform(out_dir / "waveform.png", x, fs, seg, title),
        "spectrogram": spectrogram(out_dir / "spectrogram.png", x, fs, seg, title),
        "psd": psd(out_dir / "psd.png", spec, peaks_out, modes, fs, title, params),
        "decay": decay(out_dir / "decay.png", peaks_out, title),
        "hit_zoom": hit_zoom(out_dir / "hit_zoom.png", x, fs, seg, title),
    }
