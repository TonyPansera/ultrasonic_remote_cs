import json

import numpy as np
import pytest
import soundfile as sf

from barlab.paths import Project, RawWriteError
from barlab.synth import DEMO, alias_hz, make_signal, write_demo


def test_deterministic():
    x1, t1 = make_signal(DEMO["synth_fs192k_snr20"])
    x2, t2 = make_signal(DEMO["synth_fs192k_snr20"])
    assert np.array_equal(x1, x2)
    assert t1 == t2


def test_truth_metadata(signals):
    for stem, (x, truth) in signals.items():
        assert np.max(np.abs(x)) == pytest.approx(0.5)
        assert len(truth["hit_times_s"]) == 8
        assert truth["fs_hz"] in (192_000, 48_000)
        assert [m["f_hz"] for m in truth["modes"]] == [38_000.0, 76_000.0]
        assert [m["q"] for m in truth["modes"]] == [500.0, 800.0]


def test_48k_is_the_same_signal_aliased(signals):
    _, t192 = signals["synth_fs192k_snr40"]
    _, t48 = signals["synth_fs48k_snr40"]
    assert t48["hit_times_s"] == t192["hit_times_s"]
    assert [m["observed_f_hz"] for m in t48["modes"]] == pytest.approx([10_000.0, 20_000.0])
    assert t48["expected"]["bar_mode_candidate"] == []
    assert alias_hz(30_000.0, 48_000) == 18_000.0


def test_demo_writes_files_and_refuses_raw(tmp_path):
    proj = Project.at(tmp_path)
    files = write_demo(proj.synthetic, proj.meta, guard=proj.ensure_not_raw)
    assert len(files) == len(DEMO)
    info = sf.info(files[0])
    assert info.samplerate == 192_000 and info.subtype == "PCM_16" and info.channels == 1
    truth = json.loads(files[0].with_suffix(".truth.json").read_text())
    assert truth["bar_L_m"] == pytest.approx(0.0663, rel=0.01)
    assert (proj.meta / f"{files[0].stem}.toml").is_file()
    with pytest.raises(RawWriteError):
        write_demo(proj.raw, None, guard=proj.ensure_not_raw)
    assert not proj.raw.exists()
