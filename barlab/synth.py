"""Synthetic bar-impact recordings with known ground truth.

Each hit = a short broadband click + damped sinusoids (bar modes) + a low-Q
"mount/mic" resonance; a continuous tone stands in for an environmental line;
white noise sets the SNR. Signals are evaluated analytically at the target fs,
so a 48 kHz file really aliases the ultrasonic modes (nothing is resampled).

Used by the tests and by `barlab demo` (writes into data/synthetic/, never data/raw/).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf


@dataclass(frozen=True)
class Mode:
    f_hz: float
    q: float
    amp: float

    @property
    def tau_s(self) -> float:
        """Amplitude decay time constant: Q = pi * f * tau."""
        return self.q / (np.pi * self.f_hz)


@dataclass(frozen=True)
class Scenario:
    fs: int = 192_000
    snr_db: float = 40.0                # 20*log10(reference mode amplitude / noise sigma)
    lead_s: float = 0.6                 # quiet lead-in (noise window)
    n_hits: int = 8
    spacing_s: float = 0.30
    jitter_s: float = 0.03
    tail_s: float = 0.4
    modes: tuple[Mode, ...] = (Mode(38_000.0, 500.0, 1.0), Mode(76_000.0, 800.0, 0.4))
    lowq: tuple[Mode, ...] = (Mode(15_000.0, 60.0, 3.0),)
    click_amp: float = 3.0
    click_sigma_s: float = 10e-6        # Gaussian click width (broadband)
    env_tones: tuple[tuple[float, float], ...] = ((30_000.0, 1.0),)  # (f_hz, amplitude / noise sigma)
    hit_amp_range: tuple[float, float] = (0.6, 1.0)
    peak_fs: float = 0.5                # final peak level as a fraction of full scale
    seed: int = 0
    extra_hits_s: tuple[float, ...] = field(default=())  # optional explicit extra onsets (e.g. bounces)


def alias_hz(f_hz: float, fs: float) -> float:
    """Frequency at which a real tone at f_hz appears when sampled at fs."""
    r = f_hz % fs
    return float(min(r, fs - r))


def make_signal(sc: Scenario) -> tuple[np.ndarray, dict]:
    """Return (signal in [-1, 1], ground-truth dict)."""
    rng = np.random.default_rng(sc.seed)
    fs = sc.fs
    offsets = np.arange(sc.n_hits) * sc.spacing_s
    jitter = rng.uniform(-sc.jitter_s, sc.jitter_s, sc.n_hits)
    jitter[0] = 0.0
    hit_times = sc.lead_s + offsets + jitter
    hit_amps = rng.uniform(*sc.hit_amp_range, sc.n_hits)
    if sc.extra_hits_s:
        hit_times = np.concatenate([hit_times, np.asarray(sc.extra_hits_s, float)])
        hit_amps = np.concatenate([hit_amps, np.full(len(sc.extra_hits_s), hit_amps.mean())])
        order = np.argsort(hit_times)
        hit_times, hit_amps = hit_times[order], hit_amps[order]

    duration = float(hit_times.max() + sc.spacing_s + sc.tail_s)
    n = int(round(duration * fs))
    t = np.arange(n) / fs
    x = np.zeros(n)

    resonators = sc.modes + sc.lowq
    tau_max = max((m.tau_s for m in resonators), default=1e-3)
    for th, a in zip(hit_times, hit_amps):
        i0 = max(int(np.floor((th - 10 * sc.click_sigma_s) * fs)), 0)
        i1 = min(int(np.ceil((th + 16 * tau_max) * fs)) + 1, n)
        tt = t[i0:i1] - th
        seg = a * sc.click_amp * np.exp(-0.5 * (tt / sc.click_sigma_s) ** 2)
        on = tt >= 0
        for m in resonators:
            ph = rng.uniform(0.0, 2 * np.pi)
            seg[on] += a * m.amp * np.exp(-tt[on] / m.tau_s) * np.sin(2 * np.pi * m.f_hz * tt[on] + ph)
        x[i0:i1] += seg

    ref_amp = sc.modes[0].amp if sc.modes else 1.0
    sigma = ref_amp * 10 ** (-sc.snr_db / 20)
    for f, rel in sc.env_tones:
        x += rel * sigma * np.sin(2 * np.pi * f * t + rng.uniform(0.0, 2 * np.pi))
    x += rng.normal(0.0, sigma, n)

    scale = sc.peak_fs / float(np.max(np.abs(x)))
    x *= scale

    ultrasonic_ok = fs >= 96_000

    def mode_rec(m: Mode) -> dict:
        return {"f_hz": m.f_hz, "q": m.q, "amp": m.amp, "tau_ms": round(m.tau_s * 1e3, 4),
                "observed_f_hz": alias_hz(m.f_hz, fs)}

    observed_modes = [alias_hz(m.f_hz, fs) for m in sc.modes]
    observed_env = [alias_hz(f, fs) for f, _ in sc.env_tones]
    observed_lowq = [alias_hz(m.f_hz, fs) for m in sc.lowq]
    truth = {
        "generator": "barlab.synth",
        "fs_hz": fs,
        "snr_db": sc.snr_db,
        "seed": sc.seed,
        "duration_s": round(n / fs, 6),
        "gain": scale,
        "noise_sigma_fs": sigma * scale,
        "hit_times_s": [round(float(v), 6) for v in hit_times],
        "hit_amps": [round(float(v), 4) for v in hit_amps],
        "modes": [mode_rec(m) for m in sc.modes],
        "lowq": [mode_rec(m) for m in sc.lowq],
        "env_tones": [{"f_hz": f, "amp_sigma": rel, "observed_f_hz": alias_hz(f, fs)} for f, rel in sc.env_tones],
        "click": {"amp": sc.click_amp, "sigma_us": sc.click_sigma_s * 1e6},
        "expected": {
            "bar_mode_candidate": observed_modes if ultrasonic_ok else [],
            "noise_or_environment": observed_env if ultrasonic_ok else [],
            "broadband_resonance": observed_lowq if ultrasonic_ok else [],
            "possible_alias": [] if ultrasonic_ok else observed_modes + observed_env + observed_lowq,
        },
    }
    return x, truth


# The demo set: same physical signal (seed) at several SNRs, plus a 48 kHz copy.
DEMO: dict[str, Scenario] = {
    "synth_fs192k_snr40": Scenario(snr_db=40.0, seed=1),
    "synth_fs192k_snr20": Scenario(snr_db=20.0, seed=2),
    "synth_fs192k_snr10": Scenario(snr_db=10.0, seed=3),
    "synth_fs48k_snr40": Scenario(fs=48_000, snr_db=40.0, seed=1),
}

SYNTH_BAR_D = "16mm"


def synthetic_bar_length_m() -> float:
    """Bar length whose Rayleigh-Love longitudinal n=1 mode is 38.0 kHz
    (aluminium, d = 16 mm, free-free) -- the 'known geometry' of the demo bar."""
    from barlab.theory import ALUMINIUM, implied_length_longitudinal

    return implied_length_longitudinal(38_000.0, d=0.016, material=ALUMINIUM, n=1, bc="free-free")


def _meta_toml(stem: str, L_m: float) -> str:
    return (
        f"# Written by `barlab demo` for {stem}; ground truth: data/synthetic/{stem}.truth.json\n"
        "[recording]\n"
        'mic = "synthetic (ideal, flat)"\n'
        'striking = "synthetic: click + damped sinusoids"\n'
        'notes = "bar modes 38.0 kHz (Q 500) and 76.0 kHz (Q 800); 15 kHz Q 60 mount resonance; '
        '30 kHz continuous line"\n\n'
        "[[bar]]\n"
        'name = "synthetic"\n'
        f'L = "{L_m * 1e3:.3f}mm"\n'
        f'd = "{SYNTH_BAR_D}"\n'
        'material = "aluminium"\n'
        'bc = "free-free"\n'
    )


def write_demo(out_dir: Path, meta_dir: Path | None, guard=None) -> list[Path]:
    """Write the demo WAVs (+ truth JSON, + optional meta sidecars).

    `guard` is a callable raising if a target is not allowed (Project.ensure_not_raw).
    """
    out_dir = Path(out_dir)
    targets = [out_dir] + ([Path(meta_dir)] if meta_dir else [])
    if guard is not None:
        for tgt in targets:
            guard(tgt)
    out_dir.mkdir(parents=True, exist_ok=True)
    L_m = synthetic_bar_length_m() if meta_dir else None
    written = []
    for stem, sc in DEMO.items():
        x, truth = make_signal(sc)
        wav = out_dir / f"{stem}.wav"
        sf.write(wav, x, sc.fs, subtype="PCM_16")
        truth["bar_L_m"] = L_m
        (out_dir / f"{stem}.truth.json").write_text(json.dumps(truth, indent=1) + "\n")
        if meta_dir:
            Path(meta_dir).mkdir(parents=True, exist_ok=True)
            (Path(meta_dir) / f"{stem}.toml").write_text(_meta_toml(stem, L_m))
        written.append(wav)
    return written
