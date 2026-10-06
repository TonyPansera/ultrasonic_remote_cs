import struct

import numpy as np
import pytest
import soundfile as sf
from scipy import signal

from barlab.io import FS_PHRASE, check_recording, load, read_guano
from barlab.synth import Scenario, make_signal


def _write(path, x, fs, subtype="PCM_16"):
    sf.write(path, x, fs, subtype=subtype)
    return path


def _codes(warnings):
    return {w["code"]: w for w in warnings}


def test_fs_below_96k_is_critical(tmp_path, signals, params):
    x, _ = signals["synth_fs48k_snr40"]
    rec = load(_write(tmp_path / "lo.wav", x, 48_000))
    w = _codes(check_recording(rec, params))
    assert w["FS_BELOW_96K"]["severity"] == "CRITICAL"
    assert FS_PHRASE in w["FS_BELOW_96K"]["message"]


def test_fs_192k_has_no_critical(tmp_path, signals, params):
    x, _ = signals["synth_fs192k_snr40"]
    rec = load(_write(tmp_path / "hi.wav", x, 192_000))
    assert rec.fs == 192_000 and rec.n_channels == 1 and rec.bit_depth == 16
    assert not [w for w in check_recording(rec, params) if w["severity"] == "CRITICAL"]


def test_clipping_fraction(tmp_path, params):
    fs = 192_000
    x = 0.3 * np.sin(2 * np.pi * 1000 * np.arange(fs) / fs)
    x[1000:1100] = 1.0
    rec = load(_write(tmp_path / "clip.wav", x, fs))
    w = _codes(check_recording(rec, params))
    assert "CLIPPING" in w
    assert rec.clip_fraction[0] == pytest.approx(100 / fs, rel=0.05)


def test_dc_offset(tmp_path, params):
    fs = 192_000
    rng = np.random.default_rng(0)
    x = 0.05 + 0.01 * rng.standard_normal(fs)
    rec = load(_write(tmp_path / "dc.wav", x, fs))
    assert "DC_OFFSET" in _codes(check_recording(rec, params))
    assert rec.dc_offset[0] == pytest.approx(0.05, abs=0.002)


def test_bit_depth_low_and_padded(tmp_path, params):
    fs = 192_000
    rng = np.random.default_rng(1)
    x = 0.1 * rng.standard_normal(fs // 4)
    rec8 = load(_write(tmp_path / "u8.wav", x, fs, subtype="PCM_U8"))
    assert rec8.bit_depth == 8
    assert _codes(check_recording(rec8, params))["LOW_BIT_DEPTH"]["severity"] == "WARNING"
    x16 = np.round(x * 32767) / 32768  # 16-bit values stored in a 24-bit container
    rec24 = load(_write(tmp_path / "p24.wav", x16, fs, subtype="PCM_24"))
    assert rec24.bit_depth == 24 and rec24.effective_bits == 16
    assert "EFFECTIVE_BITS" in _codes(check_recording(rec24, params))


def _append_guano(path, text):
    data = bytearray(path.read_bytes())
    payload = text.encode()
    if len(payload) % 2:
        payload += b"\x00"
    data += b"guan" + struct.pack("<I", len(payload)) + payload
    struct.pack_into("<I", data, 4, len(data) - 8)
    path.write_bytes(bytes(data))


def test_guano_parsed_without_location(tmp_path):
    p = _write(tmp_path / "g.wav", np.zeros(4800), 192_000)
    _append_guano(p, "GUANO|Version: 1.0\nMake: TestMaker\nModel: Mic_384k\n"
                     "Timestamp: 2026-01-01 12:00:00+01:00\nLoc Position: 0.0 0.0\n")
    g = read_guano(p)
    assert g["Make"] == "TestMaker" and g["Model"] == "Mic_384k"
    assert not any(k.startswith("Loc") for k in g)
    rec = load(p)  # soundfile must still read the file
    assert rec.device["Model"] == "Mic_384k" and rec.n_frames == 4800


def test_multichannel_uses_channel_with_hits(tmp_path, demo_project, signals):
    from barlab.pipeline import analyze_file

    x, _ = signals["synth_fs192k_snr40"]
    rng = np.random.default_rng(3)
    stereo = np.stack([0.003 * rng.standard_normal(len(x)), x], axis=1)
    p = _write(tmp_path / "stereo.wav", stereo, 192_000)
    res = analyze_file(p, demo_project.__class__.at(tmp_path), make_plots=False)
    assert res["file"]["channels"] == 2
    assert res["file"]["channel_used"] == 1
    assert len(res["file"]["channel_summary"]) == 2


def test_no_ultrasonic_energy_flagged(tmp_path, demo_project):
    from barlab.pipeline import analyze_file

    fs = 192_000
    x, _ = make_signal(Scenario(snr_db=60.0, env_tones=(), seed=5))
    sos = signal.butter(10, 15_000, "lowpass", fs=fs, output="sos")
    x = signal.sosfiltfilt(sos, x)  # mic roll-off simulation: nothing above ~15 kHz
    x = 0.5 * x / np.max(np.abs(x)) + 3e-4 * np.random.default_rng(6).standard_normal(len(x))
    p = _write(tmp_path / "rolloff.wav", x, fs)
    res = analyze_file(p, demo_project.__class__.at(tmp_path), make_plots=False)
    assert "NO_ULTRASONIC_ENERGY" in _codes(res["warnings"])
