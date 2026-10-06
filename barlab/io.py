"""Load WAV files at their native rate (never resampled) and run hard sanity checks.

Warnings are plain dicts {severity, code, message} with severity INFO | WARNING | CRITICAL.
Files are only ever opened for reading.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from barlab.params import Params

FS_PHRASE = "cannot represent >fs/2; ultrasonic bar modes not observable; any peak found may be aliased"

_INT_SUBTYPES = {"PCM_S8": 8, "PCM_U8": 8, "PCM_16": 16, "PCM_24": 24, "PCM_32": 32}
_FLOAT_SUBTYPES = {"FLOAT": 32, "DOUBLE": 64}
_OTHER_BITS = {"ULAW": 8, "ALAW": 8, "IMA_ADPCM": 4, "MS_ADPCM": 4, "GSM610": 2}
SEVERITY_ORDER = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}


def warning(severity: str, code: str, message: str) -> dict:
    assert severity in SEVERITY_ORDER
    return {"severity": severity, "code": code, "message": message}


def max_severity(warnings: list[dict]) -> str | None:
    if not warnings:
        return None
    return max((w["severity"] for w in warnings), key=SEVERITY_ORDER.__getitem__)


@dataclass
class Recording:
    path: Path
    fs: int
    n_channels: int
    n_frames: int
    format: str
    subtype: str
    bit_depth: int | None
    effective_bits: int | None
    is_float: bool
    data: np.ndarray  # (n_frames, n_channels), float64, full scale = 1.0, as stored (DC kept)
    sha256: str
    device: dict
    clip_fraction: list[float]
    dc_offset: list[float]
    peak_dbfs: list[float]
    rms_dbfs: list[float]

    @property
    def duration_s(self) -> float:
        return self.n_frames / self.fs

    def channel(self, c: int) -> np.ndarray:
        """Channel c with its DC offset removed (the only preprocessing applied)."""
        x = self.data[:, c]
        return x - x.mean()


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def riff_chunks(path: Path) -> list[tuple[bytes, int, int]]:
    """(chunk id, payload offset, payload size) for every top-level RIFF chunk."""
    out = []
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] not in (b"RIFF", b"RF64") or head[8:12] != b"WAVE":
            return out
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = struct.unpack("<4sI", hdr)
            pos = f.tell()
            out.append((cid, pos, size))
            f.seek(pos + size + (size & 1))
    return out


def read_guano(path: Path) -> dict:
    """GUANO metadata (bat-detector standard) as {key: value}. Location keys are dropped."""
    for cid, pos, size in riff_chunks(path):
        if cid != b"guan":
            continue
        with open(path, "rb") as f:
            f.seek(pos)
            text = f.read(size).decode("utf-8", errors="replace").replace("\x00", "")
        out = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key, val = key.strip(), val.strip()
            if key and not key.lower().startswith("loc"):
                out[key] = val
        return out
    return {}


def _effective_bits(raw: np.ndarray) -> int | None:
    acc = int(np.bitwise_or.reduce(raw.ravel().view(np.uint32)))
    if acc == 0:
        return None
    tz = (acc & -acc).bit_length() - 1
    return 32 - tz


def load(path: Path) -> Recording:
    path = Path(path)
    info = sf.info(str(path))
    st = info.subtype
    if st in _INT_SUBTYPES:
        raw, fs = sf.read(str(path), dtype="int32", always_2d=True)
        bits = _INT_SUBTYPES[st]
        eff = _effective_bits(raw)
        top = np.int64(2**31 - 2 ** (32 - bits))
        clipped = (raw.astype(np.int64) >= top) | (raw.astype(np.int64) <= -(2**31))
        data = raw.astype(np.float64) / 2**31
        is_float = False
    else:
        data, fs = sf.read(str(path), dtype="float64", always_2d=True)
        bits = _FLOAT_SUBTYPES.get(st, _OTHER_BITS.get(st))
        eff = None
        clipped = np.abs(data) >= 0.999
        is_float = st in _FLOAT_SUBTYPES
    absx = np.abs(data)
    with np.errstate(divide="ignore"):
        peak = 20 * np.log10(np.maximum(absx.max(axis=0), 1e-12))
        rms = 20 * np.log10(np.maximum(np.sqrt(np.mean(data**2, axis=0)), 1e-12))
    return Recording(
        path=path, fs=int(fs), n_channels=data.shape[1], n_frames=data.shape[0],
        format=info.format, subtype=st, bit_depth=bits, effective_bits=eff, is_float=is_float,
        data=data, sha256=sha256_file(path), device=read_guano(path),
        clip_fraction=[float(v) for v in clipped.mean(axis=0)],
        dc_offset=[float(v) for v in data.mean(axis=0)],
        peak_dbfs=[float(v) for v in peak], rms_dbfs=[float(v) for v in rms],
    )


def check_recording(rec: Recording, params: Params) -> list[dict]:
    w = []
    nyq = rec.fs / 2
    if rec.fs < params.fs_min_hz:
        w.append(warning("CRITICAL", "FS_BELOW_96K",
                         f"fs = {rec.fs} Hz is below 96 kHz: {FS_PHRASE}. Nyquist = {nyq:.0f} Hz."))
    elif rec.fs < 192_000:
        w.append(warning("INFO", "FS_BELOW_192K",
                         f"fs = {rec.fs} Hz (Nyquist {nyq:.0f} Hz): peaks above "
                         f"{params.alias_frac * nyq:.0f} Hz are flagged as possible aliases; 192 kHz+ recommended."))
    if rec.n_frames < 0.05 * rec.fs:
        w.append(warning("CRITICAL", "TOO_SHORT", f"only {rec.duration_s * 1e3:.1f} ms of audio"))
    if rec.bit_depth is not None and rec.bit_depth <= 8:
        w.append(warning("WARNING", "LOW_BIT_DEPTH",
                         f"{rec.subtype}: {rec.bit_depth}-bit samples; quantisation noise limits the noise floor"))
    if rec.effective_bits and rec.bit_depth and rec.effective_bits < rec.bit_depth and not rec.is_float:
        w.append(warning("INFO", "EFFECTIVE_BITS",
                         f"{rec.bit_depth}-bit container carries only {rec.effective_bits} significant bits"))
    for c in range(rec.n_channels):
        tag = f"ch{c}: " if rec.n_channels > 1 else ""
        frac = rec.clip_fraction[c]
        if frac > 0:
            n = int(round(frac * rec.n_frames))
            w.append(warning("WARNING", "CLIPPING", f"{tag}{n} samples ({frac:.2e} of the file) at full scale"))
        if abs(rec.dc_offset[c]) > params.dc_max_fs:
            w.append(warning("WARNING", "DC_OFFSET",
                             f"{tag}DC offset {rec.dc_offset[c]:+.4f} FS (removed before analysis)"))
        if rec.peak_dbfs[c] < params.low_level_dbfs:
            w.append(warning("WARNING", "LOW_LEVEL",
                             f"{tag}peak level {rec.peak_dbfs[c]:.1f} dBFS: recording gain is very low"))
    return w


def check_ultrasonic_energy(freqs: np.ndarray, ring_psd: np.ndarray, noise_psd: np.ndarray,
                            fs: float, params: Params) -> tuple[float | None, dict | None]:
    """Strongest ringdown-over-noise excess [dB] between ultra_hz and Nyquist (per-bin SNR,
    3-bin smoothed), and a warning when even that is small: then no ultrasonic content
    reaches the file above the noise floor (mic roll-off or anti-alias filtering).
    A per-bin maximum is used because band-integrated power is dominated by broadband
    noise even when strong narrow modes are present."""
    nyq = fs / 2
    if nyq <= params.ultra_hz:
        return None, warning("WARNING", "NO_ULTRASONIC_BAND",
                             f"Nyquist {nyq:.0f} Hz <= {params.ultra_hz:.0f} Hz: nothing above 20 kHz can be recorded")
    band = (freqs >= params.ultra_hz) & (freqs <= 0.98 * nyq)
    snr = 10 * np.log10(ring_psd[band] / noise_psd[band])
    peak = float(np.max(np.convolve(snr, np.ones(3) / 3, mode="same")))
    if peak < params.ultra_min_db:
        return peak, warning("WARNING", "NO_ULTRASONIC_ENERGY",
                             f"above {params.ultra_hz / 1e3:.0f} kHz the ringdown never rises more than {peak:.1f} dB "
                             "over the noise floor: no ultrasonic content reaches the recording "
                             "(mic roll-off or anti-alias filtering?)")
    return peak, None
