#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["numpy>=2", "scipy>=1.13", "matplotlib>=3.9", "pyserial>=3.5"]
# ///
"""Capture and analyze the ultrasonic remote signal on GPIO32 of the LilyGO T-Display.

The remote sends two tones, 38 kHz and 42 kHz (FSK), as bursts. The script checks that both tones
are there (within +-500 Hz), measures their exact frequency, when each one plays and the burst timing,
and whether the signal stays inside the ADC range.

Talks to the ultrasonic_probe firmware over serial and runs, in order:
  1. a connection test: GPIO32 with no pull / internal pull-down / internal pull-up
     (a driven signal barely moves, a floating pin follows the pull),
  2. a sampler sweep: real ADC conversion rate for several I2S rates (the I2S ADC path
     repeats each conversion several times, so the DMA rate is not the true sample rate),
  3. long captures at the best rate, as wired (no pull) and, if that saturates, with pull-down,
  4. a trend run (repeated short captures over ~20 s),
  5. interference controls: GPIO33 (nothing connected, held at mid-rail by both internal pulls)
     and GPIO32, each with the ST7789 display awake and asleep. A line that also shows up on the
     unconnected pin is not the ultrasonic signal; one that vanishes when the display sleeps comes
     from the display's charge pump.
Then writes graphs, raw data, summary.json and REPORT.md into the output folder.

Run from the project folder:  uv run tools/analyze_signal.py
"""

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import serial
from scipy import signal

CHANNEL = 4  # ADC1 channel of GPIO32
CONTROL_CHANNEL = 5  # ADC1 channel of GPIO33
FULL_SCALE_CODE = 4095
TARGET_HZ = 40_000.0
EXPECTED_TONES_HZ = (38_000.0, 42_000.0)  # FSK pair sent by the ultrasonic remote
TONE_TOLERANCE_HZ = 500.0
BAND_HZ = (EXPECTED_TONES_HZ[0] - TONE_TOLERANCE_HZ, EXPECTED_TONES_HZ[-1] + TONE_TOLERANCE_HZ)
TONE_COLORS = {38_000.0: "tab:blue", 42_000.0: "tab:red"}
SEARCH_HZ = (35_000.0, 45_000.0)  # where the tone is looked for
MIN_VPP_MV = 20.0
MAX_CLIP_FRACTION = 0.001
MIN_TONE_SNR_DB = 15.0
MIN_SIGNAL_MV = 10.0  # a real remote tone is hundreds of mV; weaker lines are interference (~1 mV)
PULL_RESISTOR_OHM = 45_000  # ESP32 datasheet typical internal pull resistor
ASSUMED_RAIL_MV = 3300.0
SWEEP_RATES = [250_000, 500_000, 1_000_000, 2_000_000]
DEFAULT_RATE = 250_000  # firmware default: one DMA sample per ADC conversion
LONG_CAPTURE = 40960
SHORT_CAPTURE = 8192

COLORS = {"none": "tab:red", "down": "tab:blue", "up": "tab:orange"}
LABELS = {"none": "as wired (no pull)", "down": "internal pull-down", "up": "internal pull-up"}
CONTROL_COLOR = "0.45"
CONTROL_LABEL = "control: GPIO33 unconnected, mid-rail"
CONTROL_PLAN = [  # label, gpio, pull, display on?
    ("gpio33_floating", 33, "none", True),
    ("gpio33_display_on", 33, "both", True),
    ("gpio33_display_off", 33, "both", False),
    ("gpio32_display_off", 32, None, False),  # None = same pull as the GPIO32 analysis mode
]


# ---------------------------------------------------------------- serial link


class Probe:
    def __init__(self, port: str):
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 921600
        self.ser.timeout = 1
        self.ser.dtr = False
        self.ser.rts = False
        self.ser.open()
        # Opening the port resets the board (cdc_acm toggles DTR/RTS): wait for the boot to finish.
        deadline = time.time() + 12
        while time.time() < deadline:
            if self.ser.readline().startswith(b"READY"):
                break
        self.info, self.cal_raw, self.cal_mv = self.read_info()

    def command(self, cmd: str, terminator: str, timeout: float = 30) -> list[str]:
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\n").encode())
        lines = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="replace").strip()
            if line:
                lines.append(line)
            if line.startswith(terminator):
                return lines
        raise TimeoutError(f"no {terminator} reply to '{cmd}' (last lines: {lines[-2:]})")

    def read_info(self):
        lines = self.command("info", "CAL")
        info = parse_kv(next(l for l in lines if l.startswith("INFO")))
        pairs = [p.split(":") for p in lines[-1].split()[1:]]
        cal = np.array(pairs, dtype=float)
        return info, cal[:, 0], cal[:, 1]

    def set_pull(self, mode: str):
        self.command(f"pull {mode}", "PULL")
        time.sleep(0.2)

    def set_rate(self, rate: int) -> tuple[bool, float]:
        kv = parse_kv(self.command(f"rate {rate}", "RATE")[-1])
        return kv["ok"] == "1", float(kv["meas"])

    def capture(self, n: int, attempts: int = 3) -> tuple[np.ndarray, float]:
        # The USB/IP serial link occasionally drops a byte; the checksum catches it, then retry.
        for _ in range(attempts):
            lines = self.command(f"cap {n}", "END", timeout=60)
            start = max(i for i, l in enumerate(lines) if l.startswith("CAP"))
            header = parse_kv(lines[start])
            payload = "".join(lines[start + 1 : -1])
            if len(payload) != 4 * int(header["n"]):
                continue
            try:
                words = np.frombuffer(bytes.fromhex(payload), dtype=">u2").astype(np.uint16)
            except ValueError:
                continue
            if int(words.astype(np.uint64).sum()) % 2**32 == int(parse_kv(lines[-1])["sum"]):
                return words, float(header["rate"])
        raise ValueError(f"capture corrupted in transfer {attempts} times in a row")

    def close(self):
        self.ser.close()


def parse_kv(line: str) -> dict[str, str]:
    return dict(tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)


# ---------------------------------------------------------------- signal processing


def decode(words: np.ndarray, channel: int = CHANNEL) -> tuple[np.ndarray, float, bool]:
    """Return (12-bit codes in time order, fraction of words tagged with our channel, swapped?)."""
    valid = float(np.mean((words >> 12) == channel))
    codes = (words & 0x0FFF).astype(np.int32)
    swapped = codes.reshape(-1, 2)[:, ::-1].ravel()  # the I2S FIFO swaps each 16-bit sample pair
    tv = lambda x: np.abs(np.diff(x)).sum()
    use_swapped = bool(tv(swapped) <= tv(codes))
    return (swapped if use_swapped else codes), valid, use_swapped


def repeat_factor(codes: np.ndarray) -> int | None:
    """How many consecutive DMA samples share one ADC conversion (None if the signal never moves)."""
    changes = np.flatnonzero(np.diff(codes) != 0)
    if len(changes) < 100:
        return None
    values, counts = np.unique(np.diff(changes), return_counts=True)
    return int(values[np.argmax(counts)])


def true_samples(codes: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return codes
    changes = np.flatnonzero(np.diff(codes) != 0)
    phase = (changes[0] + 1) % k if len(changes) else 0
    return codes[phase::k]


def amplitude_spectrum(x: np.ndarray, fs: float) -> tuple[np.ndarray, np.ndarray]:
    w = np.hanning(len(x))
    spec = np.abs(np.fft.rfft((x - x.mean()) * w)) * 2 / w.sum()
    return np.fft.rfftfreq(len(x), 1 / fs), spec


def analyze(codes: np.ndarray, mv: np.ndarray, fs: float) -> dict:
    lo, hi = np.percentile(mv, [0.1, 99.9])
    m = {
        "dc_mv": float(mv.mean()),
        "vpp_mv": float(hi - lo),
        "ac_rms_mv": float(mv.std()),
        "clip_fraction": float(np.mean((codes <= 0) | (codes >= FULL_SCALE_CODE))),
        "code_min": int(codes.min()),
        "code_max": int(codes.max()),
        "tone_hz": None,
        "tone_mv": 0.0,
        "tone_snr_db": None,
        "floor_mv": None,
        "strongest_hz": None,
    }
    if np.ptp(codes) == 0:
        return m  # flat line: nothing to analyze
    f, spec = amplitude_spectrum(mv, fs)
    search = np.flatnonzero((f >= SEARCH_HZ[0]) & (f <= SEARCH_HZ[1]))
    k = search[np.argmax(spec[search])]
    a, b, c = np.log(spec[k - 1 : k + 2] + 1e-12)
    p = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    local = (f >= 30_000) & (f <= 50_000) & (np.abs(f - f[k]) > 1_000)
    floor = float(np.median(spec[local]))
    above = np.flatnonzero(f > 1_000)
    m.update(
        tone_hz=float((k + p) * fs / len(mv)),
        tone_mv=float(spec[k]),
        tone_snr_db=float(20 * np.log10(spec[k] / floor)),
        floor_mv=floor,
        strongest_hz=float(f[above[np.argmax(spec[above])]]),
    )
    return m


def tone_present(m: dict) -> bool:
    return m["tone_snr_db"] is not None and m["tone_snr_db"] >= MIN_TONE_SNR_DB


def signal_present(m: dict) -> bool:
    """A tone strong enough to be the remote's signal, not just a line above a quiet noise floor."""
    return tone_present(m) and m["tone_mv"] >= MIN_SIGNAL_MV


def nearest_expected(hz: float) -> float:
    return min(EXPECTED_TONES_HZ, key=lambda e: abs(e - hz))


def tone_label(m: dict) -> str:
    """'38 kHz' / '42 kHz' for an expected tone, the frequency for anything else, 'silence' otherwise."""
    if not signal_present(m):
        return "silence"
    e = nearest_expected(m["tone_hz"])
    return f"{e / 1e3:.0f} kHz" if abs(m["tone_hz"] - e) <= TONE_TOLERANCE_HZ else f"{m['tone_hz'] / 1e3:.1f} kHz?"


def burst_pattern(mv: np.ndarray, codes: np.ndarray, fs: float, win_s: float = 1e-3, hop_s: float = 0.5e-3) -> dict:
    """Dominant 30-50 kHz component per 1 ms window: separates bursts (tone on) from gaps (tone off)."""
    win, hop, nfft = int(fs * win_s), int(fs * hop_s), 8192
    w = np.hanning(win)
    ff = np.fft.rfftfreq(nfft, 1 / fs)
    band = np.flatnonzero((ff >= 30e3) & (ff <= 50e3))
    starts = np.arange(0, len(mv) - win, hop)
    freq, amp, sat = [], [], []
    for i in starts:
        seg = mv[i : i + win]
        spec = np.abs(np.fft.rfft((seg - seg.mean()) * w, nfft)) * 2 / w.sum()
        k = band[np.argmax(spec[band])]
        freq.append(ff[k])
        amp.append(spec[k])
        sat.append(np.mean((codes[i : i + win] >= FULL_SCALE_CODE) | (codes[i : i + win] <= 0)))
    freq, amp, sat = np.array(freq), np.array(amp), np.array(sat)
    # A burst window carries at least half the typical burst amplitude (gaps can be short dips, not silence).
    active = amp >= max(0.5 * np.percentile(amp, 90), 5.0)
    mask = np.zeros(len(mv), dtype=bool)
    for i in starts[active]:
        mask[i : i + win] = True
    bounds = np.r_[0, np.flatnonzero(np.diff(active.astype(int))) + 1, len(active)]
    runs = [(bool(active[a]), (b - a) * hop_s * 1e3) for a, b in zip(bounds[:-1], bounds[1:])]
    inner = runs[1:-1]  # the first and last runs are cut by the capture edges
    return {"t_ms": (starts + win / 2) / fs * 1e3, "freq": freq, "amp": amp, "sat": sat, "active": active,
            "mask": mask, "on_ms": [d for a, d in inner if a], "off_ms": [d for a, d in inner if not a]}


def wired_long(run) -> list[dict]:
    """All long as-wired captures: the main ones plus the display-off control when it is as wired."""
    caps = list(run["captures"]["none"])
    c = run["controls"]["gpio32_display_off"]
    if c["pull"] == "none":
        caps.append(c)
    return caps


def tone_clusters(entries: list[dict]) -> list[dict]:
    """Group the per-capture tones (closer than 1 kHz) and compare each group with the expected tones."""
    detected = sorted((e for e in entries if signal_present(e)), key=lambda e: e["tone_hz"])
    groups = []
    for e in detected:
        if groups and e["tone_hz"] - groups[-1][-1]["tone_hz"] < 1_000:
            groups[-1].append(e)
        else:
            groups.append([e])
    clusters = []
    for g in groups:
        hz = np.array([e["tone_hz"] for e in g])
        expected = nearest_expected(hz.mean())
        clusters.append({"hz": float(hz.mean()), "std_hz": float(hz.std()), "n": len(g),
                         "amp_mv": float(np.median([e["tone_mv"] for e in g])),
                         "snr_db": float(np.median([e["tone_snr_db"] for e in g])),
                         "expected_hz": expected, "error_hz": float(hz.mean() - expected),
                         "ok": bool(abs(hz.mean() - expected) <= TONE_TOLERANCE_HZ)})
    return clusters


def timeline(trend: list[dict]) -> list[list]:
    """Merge consecutive trend captures with the same tone into [label, start s, end s] segments."""
    segments = []
    for p in trend:
        label = tone_label(p)
        if segments and segments[-1][0] == label:
            segments[-1][2] = p["t"]
        else:
            segments.append([label, p["t"], p["t"]])
    return segments


# ---------------------------------------------------------------- measurement plan


def measure(probe: Probe, trend_runs: int, long_captures: int) -> dict:
    to_mv = lambda codes: np.interp(codes, probe.cal_raw, probe.cal_mv)
    run = {"info": probe.info, "cal": {"raw": probe.cal_raw.tolist(), "mv": probe.cal_mv.tolist()}}

    print("[1/5] connection test (pull none / down / up)")
    probe.set_rate(DEFAULT_RATE)
    connection = {}
    for mode in ["none", "down", "up"]:
        probe.set_pull(mode)
        words, rate_io = probe.capture(SHORT_CAPTURE)
        codes, valid, _ = decode(words)
        connection[mode] = {"valid": valid, **analyze(codes, to_mv(codes), rate_io)}
        print(f"      {mode:5s} dc={connection[mode]['dc_mv']:.0f} mV vpp={connection[mode]['vpp_mv']:.0f} mV "
              f"clip={connection[mode]['clip_fraction']:.3f}")
    run["connection"] = connection

    # Analyze as wired; if that is pinned to a rail, also analyze with the pull that brings it into range.
    modes = ["none"]
    if connection["none"]["clip_fraction"] > 0.5:
        alt = min(["down", "up"], key=lambda md: connection[md]["clip_fraction"])
        if connection[alt]["clip_fraction"] < 0.5:
            modes.append(alt)
    run["modes"] = modes
    sweep_mode = modes[-1]

    print(f"[2/5] sampler sweep (pull {sweep_mode})")
    probe.set_pull(sweep_mode)
    sweep = []
    for rate in SWEEP_RATES:
        ok, rate_io = probe.set_rate(rate)
        words, _ = probe.capture(16384)
        codes, valid, _ = decode(words)
        k = repeat_factor(codes) if ok else None
        sweep.append({"rate_cfg": rate, "ok": ok, "rate_io": rate_io, "valid": valid, "repeat": k,
                      "rate_true": rate_io / k if k else None})
        print(f"      cfg={rate:>8d} io={rate_io:>10.0f} repeat={k} true={sweep[-1]['rate_true']}")
    run["sweep"] = sweep
    usable = [s for s in sweep if s["repeat"] and s["valid"] > 0.99]
    best = None
    if usable:
        # The true conversion rate tops out; among configs reaching it, the one with the fewest repeated
        # samples gives the longest capture for the same DMA buffer.
        top = max(s["rate_true"] for s in usable)
        best = min((s for s in usable if s["rate_true"] >= 0.99 * top), key=lambda s: s["repeat"])
    main_rate = best["rate_cfg"] if best else DEFAULT_RATE
    k = best["repeat"] if best else 1  # DEFAULT_RATE gives one DMA sample per conversion
    run["main_rate_cfg"], run["repeat"] = main_rate, k

    print(f"[3/5] long captures at cfg {main_rate} Hz (true ~{main_rate / k / 1e3:.0f} kS/s)")
    _, rate_io = probe.set_rate(main_rate)
    fs = rate_io / k
    run["fs_true"] = fs
    captures = {}
    for mode in modes:
        probe.set_pull(mode)
        captures[mode] = []
        for i in range(long_captures):
            words, _ = probe.capture(LONG_CAPTURE)
            codes, valid, swapped = decode(words)
            x = true_samples(codes, k)
            captures[mode].append({"words": words, "codes": x, "mv": to_mv(x), "valid": valid,
                                   "swapped": swapped, **analyze(x, to_mv(x), fs)})
            c = captures[mode][-1]
            print(f"      {mode:5s} #{i} dc={c['dc_mv']:.0f} mV vpp={c['vpp_mv']:.0f} mV "
                  f"tone={c['tone_hz']} Hz snr={c['tone_snr_db']}")
    run["captures"] = captures

    print(f"[4/5] trend ({trend_runs} runs)")
    trend = {mode: [] for mode in modes}
    t0 = time.time()
    for _ in range(trend_runs):
        for mode in modes:
            probe.set_pull(mode)
            words, _ = probe.capture(SHORT_CAPTURE)
            codes, _, _ = decode(words)
            x = true_samples(codes, k)
            trend[mode].append({"t": time.time() - t0, **analyze(x, to_mv(x), fs)})
    run["trend"] = trend

    print("[5/5] interference controls (GPIO33 unconnected, display on/off)")
    controls = {}
    for label, gpio, pull, display in CONTROL_PLAN:
        pull = pull or modes[-1]
        probe.command(f"src {gpio}", "SRC")  # restarts the ADC at the current rate, pull reset to none
        probe.command(f"disp {'on' if display else 'off'}", "DISP")
        probe.set_pull(pull)
        words, _ = probe.capture(LONG_CAPTURE)
        codes, valid, _ = decode(words, CHANNEL if gpio == 32 else CONTROL_CHANNEL)
        x = true_samples(codes, k)
        controls[label] = {"gpio": gpio, "pull": pull, "display": display, "words": words, "codes": x,
                           "mv": to_mv(x), "valid": valid, **analyze(x, to_mv(x), fs)}
        c = controls[label]
        print(f"      {label:20s} dc={c['dc_mv']:.0f} mV vpp={c['vpp_mv']:.0f} mV tone={c['tone_hz']} Hz "
              f"snr={c['tone_snr_db']}")
    run["controls"] = controls
    probe.command("disp on", "DISP")
    probe.set_pull("none")
    probe.command("src 32", "SRC")
    probe.set_rate(DEFAULT_RATE)
    return run


# ---------------------------------------------------------------- graphs


def style_axes(ax):
    ax.grid(True, alpha=0.3)
    ax.spines[["top", "right"]].set_visible(False)


def plot_connection(run, out: Path):
    full_scale = run["cal"]["mv"][-1]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    modes = ["none", "down", "up"]
    for i, mode in enumerate(modes):
        c = run["connection"][mode]
        lo, hi = c["dc_mv"] - c["vpp_mv"] / 2, c["dc_mv"] + c["vpp_mv"] / 2
        ax.bar(i, c["dc_mv"], color=COLORS[mode], alpha=0.8, width=0.55)
        ax.errorbar(i, c["dc_mv"], yerr=[[c["dc_mv"] - lo], [hi - c["dc_mv"]]], color="k", capsize=6)
        ax.text(i, c["dc_mv"] / 2, f"{c['dc_mv']:.0f} mV\nVpp {c['vpp_mv']:.0f} mV\nclipped {c['clip_fraction'] * 100:.0f}%",
                ha="center", va="center", fontsize=10, color="white", fontweight="bold")
    ax.axhline(full_scale, color="k", ls="--", lw=1)
    ax.text(-0.45, full_scale - 30, f"ADC full scale {full_scale:.0f} mV", va="top", fontsize=8)
    ax.axhline(ASSUMED_RAIL_MV, color="grey", ls=":", lw=1)
    ax.text(-0.45, ASSUMED_RAIL_MV + 20, "3.3 V rail", va="bottom", fontsize=8, color="grey")
    ax.set_xlim(-0.5, 2.5)
    ax.set_xticks(range(3), [LABELS[m] for m in modes])
    ax.set_ylabel("GPIO32 voltage (mV)")
    ax.set_ylim(0, 3700)
    ax.set_title("Connection test: GPIO32 level with each internal pull\n(bar = DC, whisker = peak-to-peak)")
    style_axes(ax)
    fig.tight_layout()
    fig.savefig(out / "01_connection_test.png", dpi=120)
    plt.close(fig)


def signal_traces(run) -> list[tuple[dict, str, str]]:
    """(capture, color, name) to plot: one per detected tone as wired, else the first capture of each mode."""
    traces = []
    for mode in run["modes"]:
        if mode == "none" and run.get("reps"):
            for e, c in sorted(run["reps"].items()):
                traces.append((c, TONE_COLORS.get(e, COLORS[mode]), f"{LABELS[mode]}, {e / 1e3:.0f} kHz tone"))
        else:
            traces.append((run["captures"][mode][0], COLORS[mode], LABELS[mode]))
    return traces


def plot_waveforms(run, out: Path):
    fs = run["fs_true"]
    full_scale = run["cal"]["mv"][-1]
    traces = signal_traces(run)
    fig, axes = plt.subplots(len(traces), 2, figsize=(13, 3.6 * len(traces)), squeeze=False)
    for row, (c, color, name) in enumerate(traces):
        t_ms = np.arange(len(c["mv"])) / fs * 1e3
        # Zoom 500 us well inside a burst (after it has settled), or at the start if there is no burst.
        bp = burst_pattern(c["mv"], c["codes"], fs)
        steady = [i for i in range(len(bp["active"]) - 6) if bp["active"][i : i + 6].all()]
        t0 = bp["t_ms"][steady[0]] + 1.0 if steady else 0.0
        zoom = (t_ms >= t0) & (t_ms < t0 + 0.5)
        ax = axes[row, 0]
        ax.plot((t_ms[zoom] - t0) * 1e3, c["mv"][zoom], ".-", color=color, lw=0.8, ms=3)
        for t in np.arange(0, 500, 1e6 / TARGET_HZ):
            ax.axvline(t, color="grey", lw=0.5, alpha=0.4)
        ax.set_xlabel(f"time (us) from t = {t0:.1f} ms - grey lines every 25 us")
        ax.set_ylabel("mV")
        ax.set_title(f"{name}: 500 us {'inside a burst' if steady else 'from the start'}")
        if zoom.any():
            lo, hi = c["mv"][zoom].min(), c["mv"][zoom].max()
            pad = max(hi - lo, 40) * 0.15
            ax.set_ylim(lo - pad, max(hi, full_scale) + pad if hi >= full_scale - 1 else hi + pad)
        style_axes(ax)
        ax = axes[row, 1]
        ax.plot(t_ms, c["mv"], color=color, lw=0.4)
        ax.set_xlabel("time (ms)")
        ax.set_title(f"{name}: full capture ({t_ms[-1]:.0f} ms)")
        span = max(c["mv"].max() - c["mv"].min(), 40) * 0.1
        ax.set_ylim(c["mv"].min() - span, max(c["mv"].max(), full_scale) + span)
        style_axes(ax)
        for a in axes[row]:
            a.axhline(full_scale, color="k", ls="--", lw=0.8)
    fig.suptitle(f"GPIO32 waveform, true sample rate {fs / 1e3:.0f} kS/s (dashed = ADC full scale)")
    fig.tight_layout()
    fig.savefig(out / "02_waveform.png", dpi=120)
    plt.close(fig)


def plot_spectrum(run, out: Path):
    fs = run["fs_true"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5))
    for c, color, name in signal_traces(run):
        if np.ptp(c["codes"]) == 0:
            axes[0].plot([], [], color=color, label=f"{name}: flat line (no AC at all)")
            continue
        f, spec = amplitude_spectrum(c["mv"], fs)
        db = 20 * np.log10(spec + 1e-6)
        label = f"{name}: peak {c['tone_mv']:.2f} mV @ {c['tone_hz'] / 1e3:.3f} kHz, SNR {c['tone_snr_db']:.1f} dB"
        for ax in axes:
            ax.plot(f / 1e3, db, color=color, lw=0.5, alpha=0.85, label=label)
        floor_db = 20 * np.log10(c["floor_mv"])
        axes[1].axhline(floor_db + MIN_TONE_SNR_DB, color=color, ls="--", lw=1,
                        label=f"detection threshold (floor + {MIN_TONE_SNR_DB:.0f} dB)")
    ctl = run["controls"]["gpio33_display_on"]
    if np.ptp(ctl["codes"]) > 0:
        f, spec = amplitude_spectrum(ctl["mv"], fs)
        label = (f"{CONTROL_LABEL}: 35-45 kHz peak {ctl['tone_mv']:.2f} mV @ {ctl['tone_hz'] / 1e3:.2f} kHz, "
                 f"SNR {ctl['tone_snr_db']:.1f} dB")
        for ax in axes:
            ax.plot(f / 1e3, 20 * np.log10(spec + 1e-6), color=CONTROL_COLOR, lw=0.5, alpha=0.8, label=label)
    for ax in axes:
        for e in EXPECTED_TONES_HZ:
            ax.axvspan((e - TONE_TOLERANCE_HZ) / 1e3, (e + TONE_TOLERANCE_HZ) / 1e3, color="green", alpha=0.15,
                       label=f"expected tones 38 / 42 kHz +-{TONE_TOLERANCE_HZ:.0f} Hz")
        ax.set_xlabel("frequency (kHz)")
        ax.set_ylabel("amplitude (dB re 1 mV)")
        style_axes(ax)
    axes[0].set_xlim(0, fs / 2e3)
    axes[0].set_title("Spectrum, 0 - Nyquist")
    axes[1].set_xlim(30, 50)
    axes[1].set_title("Zoom around 40 kHz")
    handles, labels = axes[1].get_legend_handles_labels()
    uniq = dict(zip(labels, handles))
    axes[1].legend(uniq.values(), uniq.keys(), fontsize=7, loc="lower left", framealpha=0.9)
    h0, l0 = axes[0].get_legend_handles_labels()
    keep = [i for i, l in enumerate(l0) if not l.startswith("expected tones")]
    axes[0].legend([h0[i] for i in keep], [l0[i] for i in keep], fontsize=7, loc="lower left", framealpha=0.9)
    for ax in axes:
        ax.set_ylim(-90, 75)
    fig.tight_layout()
    fig.savefig(out / "03_spectrum.png", dpi=120)
    plt.close(fig)


def plot_spectrogram(run, out: Path):
    fs = run["fs_true"]
    mode = run["modes"][-1]
    c = run["captures"][mode][0]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    if np.ptp(c["codes"]) == 0:
        ax.text(0.5, 0.5, "signal is a flat line: no spectrogram", ha="center", transform=ax.transAxes)
    else:
        f, t, sxx = signal.spectrogram(c["mv"] - c["mv"].mean(), fs=fs, nperseg=512, noverlap=384)
        mesh = ax.pcolormesh(t * 1e3, f / 1e3, 10 * np.log10(sxx + 1e-12), shading="auto", cmap="magma")
        fig.colorbar(mesh, ax=ax, label="PSD (dB re 1 mV^2/Hz)")
        ax.axhline(TARGET_HZ / 1e3, color="cyan", ls="--", lw=0.8, label="40 kHz")
        ax.legend(loc="upper right", fontsize=8)
    ax.set_xlabel("time (ms)")
    ax.set_ylabel("frequency (kHz)")
    ax.set_title(f"Spectrogram ({LABELS[mode]})")
    fig.tight_layout()
    fig.savefig(out / "04_spectrogram.png", dpi=120)
    plt.close(fig)


def plot_histogram(run, out: Path):
    fig, ax = plt.subplots(figsize=(9, 4))
    for mode in run["modes"]:
        codes = run["captures"][mode][0]["codes"]
        width = 4 if np.ptp(codes) == 0 else 1
        ax.hist(codes, bins=np.arange(codes.min() - 2 * width, codes.max() + 3 * width, width) - 0.5,
                color=COLORS[mode], alpha=0.8, label=f"{LABELS[mode]} (min {codes.min()}, max {codes.max()})")
        if np.mean(codes >= FULL_SCALE_CODE) > 0.5:
            ax.annotate(f"{LABELS[mode]}: {np.mean(codes >= FULL_SCALE_CODE) * 100:.0f}% of samples at 4095",
                        (FULL_SCALE_CODE, np.sum(codes >= FULL_SCALE_CODE)), xytext=(-260, -10),
                        textcoords="offset points", color=COLORS[mode], fontsize=9,
                        arrowprops={"arrowstyle": "->", "color": COLORS[mode]})
    ax.axvline(FULL_SCALE_CODE, color="k", ls=":", lw=1, label="full scale (4095)")
    ax.set_xlabel("ADC code")
    ax.set_ylabel("samples")
    ax.set_yscale("log")
    ax.set_title("ADC code histogram (a pile-up at 4095 means the input is above the measurable range)")
    ax.legend(fontsize=8)
    style_axes(ax)
    fig.tight_layout()
    fig.savefig(out / "05_histogram.png", dpi=120)
    plt.close(fig)


def plot_trend(run, out: Path):
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    for mode in run["modes"]:
        tr = run["trend"][mode]
        t = [p["t"] for p in tr]
        axes[0].plot(t, [p["dc_mv"] for p in tr], "o-", color=COLORS[mode], ms=3, label=LABELS[mode])
        axes[1].plot(t, [p["vpp_mv"] for p in tr], "o-", color=COLORS[mode], ms=3)
        axes[2].plot(t, [p["tone_snr_db"] if p["tone_snr_db"] is not None else np.nan for p in tr], "o-",
                     color=COLORS[mode], ms=3)
    axes[0].axhline(run["cal"]["mv"][-1], color="k", ls="--", lw=0.8)
    axes[0].set_ylabel("DC (mV)")
    axes[0].legend(fontsize=8)
    axes[1].axhline(MIN_VPP_MV, color="k", ls="--", lw=0.8)
    axes[1].set_ylabel("Vpp (mV)")
    axes[2].axhline(MIN_TONE_SNR_DB, color="k", ls="--", lw=0.8)
    axes[2].set_ylabel("35-45 kHz peak SNR (dB)")
    axes[2].set_xlabel("time (s)")
    axes[0].set_title("Trend over repeated captures (dashed = full scale / pass thresholds)")
    for ax in axes:
        style_axes(ax)
    fig.tight_layout()
    fig.savefig(out / "06_trend.png", dpi=120)
    plt.close(fig)


def plot_sampler(run, out: Path):
    sw = run["sweep"]
    fig, ax = plt.subplots(figsize=(8, 4))
    cfg = [s["rate_cfg"] / 1e3 for s in sw]
    ax.plot(cfg, [s["rate_io"] / 1e3 for s in sw], "o-", label="DMA sample rate (measured)")
    ax.plot(cfg, [s["rate_true"] / 1e3 if s["rate_true"] else np.nan for s in sw], "s-",
            label="true ADC conversion rate")
    for s in sw:
        if s["repeat"]:
            ax.annotate(f"x{s['repeat']}", (s["rate_cfg"] / 1e3, s["rate_true"] / 1e3), textcoords="offset points",
                        xytext=(0, 8), ha="center", fontsize=8)
    ax.axhline(2 * TARGET_HZ / 1e3, color="grey", ls=":", label="Nyquist minimum for 40 kHz (80 kS/s)")
    ax.set_xlabel("configured I2S rate (kHz)")
    ax.set_ylabel("rate (kS/s)")
    ax.set_title("ESP32 I2S-ADC sampler: each conversion is repeated 'xN' times in the DMA stream")
    ax.legend(fontsize=8)
    style_axes(ax)
    fig.tight_layout()
    fig.savefig(out / "07_sampler.png", dpi=120)
    plt.close(fig)


def plot_interference(run, out: Path):
    fs = run["fs_true"]
    ctl = run["controls"]
    sig_mode = ctl["gpio32_display_off"]["pull"]
    sig_on = run["captures"][sig_mode][0] if sig_mode in run["captures"] else None
    panels = [
        (f"GPIO32, {LABELS[sig_mode]}", sig_on, ctl["gpio32_display_off"]),
        ("GPIO33, nothing connected (mid-rail)", ctl["gpio33_display_on"], ctl["gpio33_display_off"]),
    ]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), sharey=True)
    for ax, (title, on, off) in zip(axes, panels):
        for c, color, state in [(on, "tab:purple", "display on"), (off, "tab:green", "display asleep")]:
            if c is None or np.ptp(c["codes"]) == 0:
                continue
            f, spec = amplitude_spectrum(c["mv"], fs)
            snr = f"{c['tone_snr_db']:.1f} dB" if c["tone_snr_db"] is not None else "n/a"
            ax.plot(f / 1e3, 20 * np.log10(spec + 1e-6), color=color, lw=0.5, alpha=0.85,
                    label=f"{state}: 35-45 kHz peak SNR {snr}")
        ax.axvspan(BAND_HZ[0] / 1e3, BAND_HZ[1] / 1e3, color="green", alpha=0.12)
        ax.set_xlim(20, 100)
        ax.set_ylim(-90, 60)
        ax.set_xlabel("frequency (kHz)")
        ax.set_title(title)
        ax.legend(fontsize=8, loc="lower left", framealpha=0.9)
        style_axes(ax)
    axes[0].set_ylabel("amplitude (dB re 1 mV)")
    fig.suptitle("Interference controls: GPIO32 vs unconnected GPIO33, display on / asleep (green = expected band)")
    fig.tight_layout()
    fig.savefig(out / "08_interference.png", dpi=120)
    plt.close(fig)


def plot_fsk(run, out: Path):
    trend = run["trend"]["none"]
    fig, axes = plt.subplots(2, 1, figsize=(12, 7.5))
    ax = axes[0]
    for e in EXPECTED_TONES_HZ:
        ax.axhspan((e - TONE_TOLERANCE_HZ) / 1e3, (e + TONE_TOLERANCE_HZ) / 1e3, color=TONE_COLORS[e], alpha=0.1)
        ax.axhline(e / 1e3, color=TONE_COLORS[e], ls="--", lw=0.8)
    for p in trend:
        if signal_present(p):
            color = TONE_COLORS.get(nearest_expected(p["tone_hz"]), "k")
            ax.plot(p["t"], p["tone_hz"] / 1e3, "o", color=color, ms=6)
        else:
            ax.plot(p["t"], TARGET_HZ / 1e3, "x", color="0.4", ms=7)
    handles = [plt.Line2D([], [], marker="o", ls="", color=TONE_COLORS[e], label=f"{e / 1e3:.0f} kHz tone")
               for e in EXPECTED_TONES_HZ]
    handles.append(plt.Line2D([], [], marker="x", ls="", color="0.4", label="no tone (silence)"))
    ax.legend(handles=handles, fontsize=8, loc="upper right", framealpha=0.9)
    ax.set_ylim(36.5, 43.5)
    ax.set_xlabel("time (s)")
    ax.set_ylabel("tone frequency (kHz)")
    ax.set_title(f"Which tone is sent over time (one {SHORT_CAPTURE / run['fs_true'] * 1e3:.0f} ms capture per point, "
                 f"bands = +-{TONE_TOLERANCE_HZ:.0f} Hz)")
    style_axes(ax)

    ax = axes[1]
    for e, b in sorted(run.get("bursts", {}).items()):
        bp = b["pattern"]
        ax.plot(bp["t_ms"], bp["amp"], color=TONE_COLORS.get(e, "k"), lw=1,
                label=f"{e / 1e3:.0f} kHz: bursts ~{b['on_ms']:.1f} ms on / ~{b['off_ms']:.1f} ms off "
                      f"({b['bursts_per_s']:.0f} bursts/s)")
    ax.set_xlabel("time in capture (ms)")
    ax.set_ylabel("tone amplitude (mV, 1 ms windows)")
    ax.set_title("Burst pattern inside one long capture per tone")
    if run.get("bursts"):
        ax.legend(fontsize=8, loc="upper right", framealpha=0.9)
    style_axes(ax)
    fig.tight_layout()
    fig.savefig(out / "09_fsk.png", dpi=120)
    plt.close(fig)


# ---------------------------------------------------------------- verdict + report


def assess(run) -> dict:
    """Verdict for the FSK remote signal as wired, plus warnings and findings for the report."""
    info = run["info"]
    full_scale = run["cal"]["mv"][-1]
    fs = run["fs_true"]
    trend = run["trend"]["none"]
    wired = wired_long(run)
    entries = wired + trend
    clusters = tone_clusters(entries)
    failures, warnings, notes = [], [], []

    if min(c["valid"] for c in wired) < 0.99:
        failures.append("Some DMA words do not come from ADC1 channel 4: sampler problem.")
    if not clusters:
        nat = wired[0]
        failures.append(f"As wired, no tone stands out in 35-45 kHz in any of the {len(entries)} captures.")
        if nat["clip_fraction"] > 0.5:
            failures.append(f"As wired, {nat['clip_fraction'] * 100:.0f}% of samples sit at the ADC limit "
                            f"(full scale = {full_scale:.0f} mV): the input is at or beyond the measurable range.")
        elif nat["vpp_mv"] < MIN_VPP_MV:
            failures.append(f"As wired, peak-to-peak amplitude is only {nat['vpp_mv']:.1f} mV.")
    span_s = trend[-1]["t"] if trend else 0
    for e in EXPECTED_TONES_HZ:
        if clusters and not any(c["ok"] and c["expected_hz"] == e for c in clusters):
            failures.append(f"Expected tone {e / 1e3:.0f} kHz not found (+-{TONE_TOLERANCE_HZ:.0f} Hz) in "
                            f"{len(entries)} captures over {span_s:.0f} s.")
    for c in [c for c in clusters if not c["ok"] and c["n"] == 1]:
        notes.append(f"1 capture had its strongest 35-45 kHz component at {c['hz']:.0f} Hz (SNR {c['snr_db']:.0f} dB), "
                     "outside both tone bands. It is not counted as a tone.")
    clusters = [c for c in clusters if c["ok"] or c["n"] > 1]
    for c in clusters:
        if c["ok"]:
            notes.append(f"**{c['expected_hz'] / 1e3:.0f} kHz tone found at {c['hz']:.1f} Hz** "
                         f"(error {c['error_hz']:+.0f} Hz, spread +-{c['std_hz']:.1f} Hz over {c['n']} captures), "
                         f"amplitude ~{c['amp_mv']:.0f} mV, SNR {c['snr_db']:.0f} dB.")
        else:
            warnings.append(f"Unexpected tone at {c['hz']:.0f} Hz ({c['n']} captures), "
                            f"{c['error_hz']:+.0f} Hz from {c['expected_hz'] / 1e3:.0f} kHz.")

    # Representative long capture per tone, burst timing and levels
    reps = {}
    for c in wired:
        if signal_present(c) and abs(c["tone_hz"] - nearest_expected(c["tone_hz"])) <= TONE_TOLERANCE_HZ:
            e = nearest_expected(c["tone_hz"])
            if e not in reps or c["tone_snr_db"] > reps[e]["tone_snr_db"]:
                reps[e] = c
    run["reps"] = reps
    bursts = {}
    for e, c in sorted(reps.items()):
        bp = burst_pattern(c["mv"], c["codes"], fs)
        on = float(np.median(bp["on_ms"])) if bp["on_ms"] else float("nan")
        off = float(np.median(bp["off_ms"])) if bp["off_ms"] else float("nan")
        inside = c["mv"][bp["mask"]]
        bursts[e] = {"pattern": bp, "on_ms": on, "off_ms": off, "period_ms": on + off,
                     "bursts_per_s": 1000 / (on + off) if on + off > 0 else float("nan"),
                     "burst_center_mv": float(np.median(inside)) if len(inside) else float("nan"),
                     "burst_min_mv": float(np.percentile(inside, 0.5)) if len(inside) else float("nan"),
                     "burst_max_mv": float(np.percentile(inside, 99.5)) if len(inside) else float("nan"),
                     "sat_in_bursts": float(bp["sat"][bp["active"]].mean()) if bp["active"].any() else 0.0,
                     "sat_in_gaps": float(bp["sat"][~bp["active"]].mean()) if (~bp["active"]).any() else 0.0}
        b = bursts[e]
        if bp["on_ms"]:
            notes.append(f"{e / 1e3:.0f} kHz is sent in bursts of ~{on:.1f} ms separated by ~{off:.1f} ms "
                         f"(one burst every ~{on + off:.1f} ms, {b['bursts_per_s']:.0f} bursts/s). Inside a burst the "
                         f"signal swings {b['burst_min_mv']:.0f}-{b['burst_max_mv']:.0f} mV around {b['burst_center_mv']:.0f} mV.")
    run["bursts"] = bursts

    segments = timeline(trend)
    run["timeline"] = segments
    if clusters and trend:
        seq = " -> ".join(f"{lab} ({a:.1f}-{b:.1f} s)" if b > a else f"{lab} ({a:.1f} s)" for lab, a, b in segments)
        notes.append(f"Tone over the {span_s:.0f} s trend (one short capture every ~{span_s / max(len(trend) - 1, 1):.1f} s): {seq}.")
    idle = [p["dc_mv"] for p in trend if not signal_present(p) and p["clip_fraction"] < 0.01]
    if clusters and idle:
        notes.append(f"With no tone (silence between transmissions) GPIO32 rests at ~{np.median(idle):.0f} mV.")

    clip = float(np.mean([c["clip_fraction"] for c in wired]))
    clip_hi = float(np.mean([np.mean(c["codes"] >= FULL_SCALE_CODE) for c in wired]))
    clip_lo = float(np.mean([np.mean(c["codes"] <= 0) for c in wired]))
    if clusters and clip > MAX_CLIP_FRACTION:
        zero = run["cal"]["mv"][0]
        where = [f"{clip_hi * 100:.0f}% at the top ({full_scale:.0f} mV)"]
        if clip_lo > MAX_CLIP_FRACTION:
            where.append(f"{clip_lo * 100:.0f}% at the bottom ({zero:.0f} mV)")
        inb = np.mean([b["sat_in_bursts"] for b in bursts.values()]) if bursts else float("nan")
        warnings.append(
            f"{clip * 100:.0f}% of samples sit at an ADC limit ({', '.join(where)}); inside the bursts the signal is "
            f"beyond the measurable range {inb * 100:.0f}% of the time, so its peaks are flattened. Frequencies are "
            "unaffected, but the true peak levels are unknown and could leave the 0-3.3 V the pin tolerates.")
        run["range_warning"] = True
        run["clip_both_rails"] = clip_lo > MAX_CLIP_FRACTION

    if clusters:
        counts = [int(x) for x in info["pcnt_counts"].split(",")]
        gate_s = float(info["pcnt_gate_ms"]) / 1000
        if max(counts) == 0:
            notes.append("The PCNT edge counter saw 0 edges at boot: the signal never crossed the ESP32's digital "
                         "input thresholds during that second (or it was a silence). The frequencies above come from the "
                         "ADC captures.")
        else:
            duty = np.nanmean([b["on_ms"] / b["period_ms"] for b in bursts.values()]) if bursts else float("nan")
            notes.append(f"Independent check: the PCNT hardware counter (digital input) saw up to "
                         f"{max(counts) / gate_s / 1e3:.1f}k edges/s in one {gate_s * 1e3:.0f} ms gate at boot "
                         f"(gates: {', '.join(map(str, counts))}). That matches a 38-42 kHz tone that is on "
                         f"~{duty * 100:.0f}% of the time inside the burst train; gates near 0 fell in silences.")
        ctl = run["controls"]["gpio33_display_on"]
        sig = max(reps.values(), key=lambda c: c["tone_mv"]) if reps else None
        if sig is not None and tone_present(ctl):
            notes.append(f"GPIO33 (nothing connected, next to GPIO32) picks up {ctl['tone_mv']:.1f} mV at "
                         f"{ctl['tone_hz'] / 1e3:.2f} kHz, {20 * np.log10(sig['tone_mv'] / ctl['tone_mv']):.0f} dB "
                         "below GPIO32: crosstalk, not a second signal.")
    else:
        diagnose_no_signal(run, notes)

    status = "FAIL" if failures else "PASS with warnings" if warnings else "PASS"
    return {"status": status, "failures": failures, "warnings": warnings, "notes": notes,
            "tones": clusters, "clip_fraction": clip}


def diagnose_no_signal(run, notes: list[str]):
    """Connection-test and interference findings, useful when nothing is detected as wired."""
    info = run["info"]
    pcnt_hz = float(info["pcnt_hz"])
    if pcnt_hz == 0:
        notes.append("Hardware edge counter (PCNT, digital input) counted **0 edges in 1 s** at boot: "
                     "the pin never crossed the logic threshold.")
    else:
        notes.append(f"Hardware edge counter (PCNT) measured **{pcnt_hz:.1f} Hz** at boot.")
    oneshot_mv = float(np.interp(float(info["oneshot_mean"]), run["cal"]["raw"], run["cal"]["mv"]))
    notes.append(f"Independent `analogRead()` check at boot: mean code {float(info['oneshot_mean']):.0f} "
                 f"(~{oneshot_mv:.0f} mV), range {info['oneshot_min']}-{info['oneshot_max']}.")

    conn = run["connection"]
    if conn["none"]["clip_fraction"] > 0.5:
        drop = conn["none"]["dc_mv"] - conn["down"]["dc_mv"]
        if conn["down"]["dc_mv"] < 500:
            notes.append("With the internal pull-down the pin falls to ~0 V: GPIO32 is **floating** "
                         "(nothing, or something extremely high-impedance, is connected).")
        else:
            notes.append(
                f"With the internal ~45 kOhm pull-down the pin only drops by ~{drop:.0f} mV to "
                f"{conn['down']['dc_mv']:.0f} mV: GPIO32 is **actively driven high**, not floating.")
            if conn["down"]["dc_mv"] < ASSUMED_RAIL_MV:
                rs = PULL_RESISTOR_OHM * (ASSUMED_RAIL_MV / conn["down"]["dc_mv"] - 1)
                notes.append(f"If that node is tied to 3.3 V, its source resistance is roughly {rs / 1e3:.1f} kOhm "
                             "(rough estimate: the internal pull value varies +-50 %). That is the signature of a "
                             "pull-up resistor (4.7 kOhm is a standard value) with nothing pulling the line down.")
                run["stuck_high_via_resistor"] = True
    if "down" in run["captures"]:
        d = run["captures"]["down"][0]
        if tone_present(d):
            notes.append(f"With the pull-down (signal brought into range) a tone **is** visible at "
                         f"{d['tone_hz']:.0f} Hz, {d['tone_mv']:.2f} mV amplitude, SNR {d['tone_snr_db']:.1f} dB.")
        else:
            notes.append(
                f"With the pull-down the pin is inside the ADC range ({d['dc_mv']:.0f} mV DC, {d['ac_rms_mv']:.1f} mV RMS noise) "
                f"and there is **still no 40 kHz tone**: the strongest 35-45 kHz component is "
                f"{d['tone_mv']:.2f} mV at {d['tone_hz'] / 1e3:.2f} kHz, only {d['tone_snr_db']:.1f} dB above the noise floor "
                f"(threshold {MIN_TONE_SNR_DB:.0f} dB).")
    ctl = run["controls"]
    on, off = ctl["gpio33_display_on"], ctl["gpio33_display_off"]
    sig_off = ctl["gpio32_display_off"]
    if tone_present(on):
        where = (f"also appears on GPIO33, where nothing is connected ({on['tone_mv']:.2f} mV at "
                 f"{on['tone_hz'] / 1e3:.2f} kHz, SNR {on['tone_snr_db']:.1f} dB)")
        if not tone_present(off):
            notes.append(f"The ~41 kHz line {where}, and disappears when the ST7789 display is put to sleep "
                         f"(SNR {off['tone_snr_db']:.1f} dB): it is switching noise from the display's charge pump, "
                         "**not** the ultrasonic signal.")
        else:
            notes.append(f"The ~41 kHz line {where}, even with the display asleep: at least part of it is coupled through "
                         "the shared supply/ground (board or external circuit), not carried by the GPIO32 signal.")
    elif "down" in run["captures"] and tone_present(run["captures"]["down"][0]):
        notes.append("GPIO33 (nothing connected) shows no 35-45 kHz line: the component seen with the pull-down "
                     "is specific to the GPIO32 wire.")
    sig_caps = run["captures"].get(sig_off["pull"], []) + [sig_off]
    if tone_present(on) and sig_caps and all(tone_present(c) for c in sig_caps):
        sig_mv = float(np.mean([c["tone_mv"] for c in sig_caps]))
        ratio = sig_mv / on["tone_mv"]
        if ratio >= 3:
            notes.append(
                f"On GPIO32 that line averages {sig_mv:.2f} mV, {ratio:.1f}x the unconnected pin ({on['tone_mv']:.2f} mV): "
                "part of it probably arrives through the GPIO32 wire, i.e. a ~41 kHz source may be running upstream. "
                "Even so, it is about a millivolt on a line pinned at the top of the range, not a usable receiver signal.")
        else:
            notes.append(
                f"On GPIO32 that line averages {sig_mv:.2f} mV, {ratio:.1f}x the unconnected pin ({on['tone_mv']:.2f} mV). "
                "Given the different DC level and source impedance, shared supply/ground coupling can explain this: "
                "it is no evidence that the ultrasonic signal reaches GPIO32.")
    if sig_off["clip_fraction"] < 0.5:
        state = "still shows" if tone_present(sig_off) else "shows **no**"
        notes.append(f"With the display asleep, GPIO32 ({LABELS[sig_off['pull']]}) {state} 35-45 kHz component "
                     f"(best peak {sig_off['tone_mv']:.2f} mV at {sig_off['tone_hz'] / 1e3:.2f} kHz, "
                     f"SNR {sig_off['tone_snr_db']:.1f} dB).")


HARDWARE_CHECKS = """## What to check on the hardware

1. **Measure GPIO32 with a multimeter** while the ultrasonic circuit runs. A properly biased analog receiver output idles near mid-supply (~1.65 V on 3.3 V). About 3.3 V means the output is stuck at the rail. **Above 3.6 V means the module runs from 5 V: disconnect it**, because it can damage the ESP32 (power it from 3.3 V, or add a resistor divider).
2. **Check what GPIO32 is wired to.** The measurement looks like a ~4.7 kOhm pull-up to 3.3 V with nothing driving the line. That fits a wire landing on the module's VCC/pull-up pin instead of the amplifier output, or an open-collector comparator output (e.g. LM393) that never switches.
3. **If the module outputs a comparator/echo signal** (idle high, pulled low only when an echo is detected), it never toggled here: 0 edges counted in 1 s. Check that the transmitter fires and that something reflects back to the receiver.
4. **If it is meant to be an analog amplifier output,** check its bias: the amplifier needs a mid-supply reference (VCC/2 divider) and the transducer must be AC-coupled through a capacitor. A missing or shorted coupling capacitor, a wrong bias or too much gain drives the output to the positive rail.
5. **Check the 40 kHz source on its own** with an oscilloscope at the transmitter and at the receiver output, before the ESP32. The faint ~41 kHz line seen here also shows up on an unconnected pin, so it cannot confirm the transmitter is running.

"""


RANGE_ADVICE = """## Keeping the signal inside the ADC range

The ESP32 reads about 0.14-3.15 V on this pin (12 dB attenuation). Anything above reads 4095, anything below reads 0.
1. **Check the true peaks with an oscilloscope** at the receiver output. The pin must stay within 0-3.3 V: it is not 5 V tolerant and must not go negative. If the peaks leave that range, add a resistor divider (and/or a series resistor so the ESP32's clamp diodes limit the current).
2. **Keep the swing under ~2.5 Vpp.** If the bursts clip at both ends, the signal is simply too big: lower the receiver gain or attenuate with a divider. The frequency survives clipping, but amplitude-based decoding does not.
3. **Center the signal at mid-rail by AC-coupling it:** put a 100 nF capacitor in series, then on the GPIO32 side 100 kOhm to 3.3 V and 100 kOhm to GND. The signal then sits around 1.65 V whatever the receiver's own DC level. The high-pass corner is ~30 Hz, so 38/42 kHz pass untouched.

"""


def write_report(run, out: Path, timestamp: str, result: dict):
    failures, warnings, notes = result["failures"], result["warnings"], result["notes"]
    info = run["info"]
    rows = []
    for mode in run["modes"]:
        for i, c in enumerate(run["captures"][mode]):
            tone = f"{c['tone_hz'] / 1e3:.2f} kHz / {c['tone_mv']:.2f} mV / {c['tone_snr_db']:.1f} dB" \
                if c["tone_hz"] else "n/a (flat line)"
            rows.append(f"| {LABELS[mode]} #{i} | {c['dc_mv']:.0f} | {c['vpp_mv']:.1f} | {c['ac_rms_mv']:.2f} | "
                        f"{c['clip_fraction'] * 100:.1f} % | {c['code_min']}-{c['code_max']} | {tone} |")
    sweep_rows = "\n".join(
        f"| {s['rate_cfg'] / 1e3:.0f} | {s['rate_io'] / 1e3:.1f} | {s['repeat'] or 'n/a'} | "
        f"{(s['rate_true'] / 1e3) if s['rate_true'] else float('nan'):.1f} |" for s in run["sweep"])
    status = {"PASS": "PASS - both FSK tones present and measurable",
              "PASS with warnings": "PASS with warnings - both FSK tones present",
              "FAIL": "FAIL"}[result["status"]]
    nl = "\n"
    tone_rows = nl.join(
        f"| {c['expected_hz'] / 1e3:.0f} kHz | {c['hz']:.1f} | {c['error_hz']:+.1f} | +-{c['std_hz']:.1f} | {c['n']} | "
        f"{c['amp_mv']:.0f} | {c['snr_db']:.0f} | {'OK' if c['ok'] else 'OUT'} |" for c in result["tones"])
    burst_rows = nl.join(
        f"| {e / 1e3:.0f} kHz | {b['on_ms']:.1f} | {b['off_ms']:.1f} | {b['period_ms']:.1f} | {b['bursts_per_s']:.0f} | "
        f"{b['burst_center_mv']:.0f} | {b['burst_min_mv']:.0f}-{b['burst_max_mv']:.0f} | {b['sat_in_gaps'] * 100:.0f} % |"
        for e, b in sorted(run.get("bursts", {}).items()))
    tone_section = f"""## Detected tones

Expected: {' and '.join(f'{e / 1e3:.0f} kHz' for e in EXPECTED_TONES_HZ)}, +-{TONE_TOLERANCE_HZ:.0f} Hz. Each row groups every as-wired capture (long and trend) whose dominant 35-45 kHz component falls in that tone.

| Tone | Measured (Hz) | Error (Hz) | Spread (Hz) | Captures | Amplitude (mV) | SNR (dB) | |
|---|---|---|---|---|---|---|---|
{tone_rows}

### Burst pattern (one long capture per tone)

| Tone | Burst on (ms) | Gap (ms) | Period (ms) | Bursts/s | Burst center (mV) | Burst swing (mV) | Gap time above ADC range |
|---|---|---|---|---|---|---|---|
{burst_rows}

""" if result["tones"] else ""
    report = f"""# GPIO32 ultrasonic signal check

- **Date:** {timestamp}
- **Board:** LilyGO T-Display v1.1 (ESP32-D0WDQ6-V3), firmware `ultrasonic_probe`
- **Input:** GPIO32 = ADC1 channel 4, 12 dB attenuation (range ~{run['cal']['mv'][0]:.0f}-{run['cal']['mv'][-1]:.0f} mV, eFuse-calibrated)
- **Sampler:** I2S built-in ADC DMA, configured {run['main_rate_cfg'] / 1e3:.0f} kHz, true conversion rate **{run['fs_true'] / 1e3:.1f} kS/s** ({run['fs_true'] / TARGET_HZ:.1f} samples per 40 kHz period)

## Verdict: {status}

{nl.join(f'- **Failure:** {f}' for f in failures)}
{nl.join(f'- **Warning:** {w}' for w in warnings)}

### Findings

{nl.join(f'- {n}' for n in notes)}

{tone_section}{HARDWARE_CHECKS if run.get("stuck_high_via_resistor") and not result["tones"] else ""}{RANGE_ADVICE if run.get("range_warning") else ""}## Measurements

| Capture | DC (mV) | Vpp (mV) | AC RMS (mV) | Clipped | Code range | 35-45 kHz peak (freq / amplitude / SNR) |
|---|---|---|---|---|---|---|
{nl.join(rows)}

Pass criteria (as wired): each expected tone ({' / '.join(f'{e / 1e3:.0f} kHz' for e in EXPECTED_TONES_HZ)}) found within +-{TONE_TOLERANCE_HZ:.0f} Hz, at least {MIN_TONE_SNR_DB:.0f} dB above the local noise floor. Warning if more than {MAX_CLIP_FRACTION * 100:.1f} % of samples sit at the ADC limit.

### Connection test

| Pull | DC (mV) | Vpp (mV) | Clipped |
|---|---|---|---|
{nl.join(f"| {LABELS[m]} | {c['dc_mv']:.0f} | {c['vpp_mv']:.1f} | {c['clip_fraction'] * 100:.1f} % |" for m, c in run['connection'].items())}

### Interference controls

GPIO33 has nothing connected; with both internal pulls it sits at mid-rail through ~22 kOhm. The display is put to sleep with `SLPIN`, which also stops its internal charge pumps.

| Capture | Pin | Pull | Display | DC (mV) | Vpp (mV) | 35-45 kHz peak (freq / amplitude / SNR) |
|---|---|---|---|---|---|---|
{nl.join(f"| {lbl} | GPIO{c['gpio']} | {c['pull']} | {'on' if c['display'] else 'asleep'} | {c['dc_mv']:.0f} | {c['vpp_mv']:.1f} | " + (f"{c['tone_hz'] / 1e3:.2f} kHz / {c['tone_mv']:.2f} mV / {c['tone_snr_db']:.1f} dB" if c['tone_hz'] else "n/a (flat line)") + " |" for lbl, c in run['controls'].items())}

### Sampler sweep

The ESP32 I2S-ADC path repeats each conversion several times in the DMA stream. The true rate is the DMA rate divided by that repeat factor.

| Configured (kHz) | DMA rate (kS/s) | Repeat | True rate (kS/s) |
|---|---|---|---|
{sweep_rows}

Boot info: `PCNT counts per 250 ms gate = {info['pcnt_counts']}`, `analogRead mean = {info['oneshot_mean']}`, `ADC cal = {info['cal']} (Vref {info['vref']} mV)`.

## Graphs

![Connection test](01_connection_test.png)
![Waveform](02_waveform.png)
![Tones over time and burst pattern](09_fsk.png)
![Spectrum](03_spectrum.png)
![Spectrogram](04_spectrogram.png)
![Histogram](05_histogram.png)
![Trend](06_trend.png)
![Sampler](07_sampler.png)
![Interference controls](08_interference.png)

Raw captures: `data/*.npz` (`words` = raw 16-bit DMA words, `codes`/`mv` = un-swapped true samples, `fs` = true sample rate).
Re-run: `cd ultrasonic_probe && uv run tools/analyze_signal.py`.
"""
    (out / "REPORT.md").write_text(report)


def save(run, out: Path, timestamp: str, result: dict):
    data = out / "data"
    data.mkdir(exist_ok=True)
    for mode, caps in run["captures"].items():
        for i, c in enumerate(caps):
            np.savez_compressed(data / f"capture_{mode}_{i}.npz", words=c["words"], codes=c["codes"], mv=c["mv"],
                                fs=run["fs_true"], pull=mode)
    for label, c in run["controls"].items():
        np.savez_compressed(data / f"control_{label}.npz", words=c["words"], codes=c["codes"], mv=c["mv"],
                            fs=run["fs_true"], pull=c["pull"], gpio=c["gpio"], display=c["display"])
    strip = lambda c: {k: v for k, v in c.items() if k not in ("words", "codes", "mv")}
    summary = {
        "timestamp": timestamp,
        "verdict": result["status"],
        "failures": result["failures"],
        "warnings": result["warnings"],
        "notes": result["notes"],
        "tones": result["tones"],
        "bursts": {f"{e:.0f}": {k: v for k, v in b.items() if k != "pattern"} for e, b in run.get("bursts", {}).items()},
        "timeline": run.get("timeline", []),
        "info": run["info"],
        "fs_true": run["fs_true"],
        "main_rate_cfg": run["main_rate_cfg"],
        "repeat": run["repeat"],
        "connection": run["connection"],
        "sweep": run["sweep"],
        "modes": {m: [strip(c) for c in caps] for m, caps in run["captures"].items()},
        "trend": run["trend"],
        "controls": {m: strip(c) for m, c in run["controls"].items()},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "results"))
    ap.add_argument("--trend", type=int, default=40, help="number of trend runs (~0.65 s each)")
    ap.add_argument("--captures", type=int, default=3, help="long captures per pull mode")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().isoformat(timespec="seconds")
    probe = Probe(args.port)
    try:
        run = measure(probe, args.trend, args.captures)
    finally:
        probe.close()

    result = assess(run)  # also picks the per-tone captures the plots use
    plot_connection(run, out)
    plot_waveforms(run, out)
    plot_spectrum(run, out)
    plot_spectrogram(run, out)
    plot_histogram(run, out)
    plot_trend(run, out)
    plot_sampler(run, out)
    plot_interference(run, out)
    plot_fsk(run, out)
    write_report(run, out, timestamp, result)
    save(run, out, timestamp, result)

    print(f"\nVERDICT: {result['status']}")
    for line in [f"FAILURE: {f}" for f in result["failures"]] + [f"WARNING: {w}" for w in result["warnings"]] + result["notes"]:
        print(f"  - {line}")
    print(f"Report: {out / 'REPORT.md'}")


if __name__ == "__main__":
    main()
