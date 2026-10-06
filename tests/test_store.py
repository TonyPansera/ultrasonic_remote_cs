import shutil
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import soundfile as sf

from barlab.io import sha256_file
from barlab.paths import Project
from barlab.store import INDEX_COLUMNS, pending, read_index, rows_from_result, upsert


def fake_result(sha, n_peaks, stem="a", source="raw"):
    peaks = [{
        "i": i, "f_hz": 38_000.0 + i, "f_std_hz": 1.0, "f_range_rel": 1e-4, "snr_db": 30.0,
        "above_floor_db": 40.0, "width_hz": 90.0, "class": "bar_mode_candidate", "flags": ["high_q"],
        "decay": {"q": 500.0, "q_std": 10.0, "tau_ms": 4.2, "n_fitted": 8},
        "theory": {"nearest": None, "matches": False},
    } for i in range(n_peaks)]
    return {
        "schema_version": 1, "barlab_version": "0.1.0", "analyzed_at": "2026-10-06T10:00:00+02:00",
        "file": {"sha256": sha, "stem": stem, "path": f"data/raw/{stem}.wav", "source": source,
                 "fs_hz": 384_000, "channel_used": 0, "recorded_at": None},
        "segmentation": {"n_hits": 8},
        "warnings": [{"severity": "WARNING", "code": "X", "message": "m"}],
        "peaks": peaks,
    }


def test_upsert_is_idempotent(tmp_path):
    idx = tmp_path / "index.csv"
    rows = rows_from_result(fake_result("aa" * 32, 3))
    upsert(idx, rows)
    upsert(idx, rows)
    got = read_index(idx)
    assert len(got) == 3
    assert list(got[0].keys()) == INDEX_COLUMNS
    assert {(r["sha256"], r["peak_idx"]) for r in got} == {("aa" * 32, str(i)) for i in range(3)}


def test_rerun_replaces_all_rows_of_that_file(tmp_path):
    idx = tmp_path / "index.csv"
    upsert(idx, rows_from_result(fake_result("aa" * 32, 3)))
    upsert(idx, rows_from_result(fake_result("bb" * 32, 2, stem="b")))
    upsert(idx, rows_from_result(fake_result("aa" * 32, 1)))
    got = read_index(idx)
    assert sum(r["sha256"] == "aa" * 32 for r in got) == 1
    assert sum(r["sha256"] == "bb" * 32 for r in got) == 2


def test_file_without_peaks_gets_sentinel_row(tmp_path):
    idx = tmp_path / "index.csv"
    upsert(idx, rows_from_result(fake_result("cc" * 32, 0, stem="c")))
    got = read_index(idx)
    assert len(got) == 1 and got[0]["peak_idx"] == "-1" and got[0]["class"] == "none"


def test_pending_is_by_hash(tmp_path):
    proj = Project.at(tmp_path)
    proj.raw.mkdir(parents=True)
    rng = np.random.default_rng(0)
    for name in ("a", "b"):
        sf.write(proj.raw / f"{name}.wav", 0.01 * rng.standard_normal(1000), 192_000)
    shutil.copy(proj.raw / "a.wav", proj.raw / "a_copy.wav")  # same content, new name
    upsert(proj.index, rows_from_result(fake_result(sha256_file(proj.raw / "a.wav"), 1)))
    assert [p.name for p in pending(proj)] == ["b.wav"]


def test_concurrent_upserts_lose_nothing(tmp_path):
    idx = tmp_path / "index.csv"
    shas = [f"{i:02d}" * 32 for i in range(16)]
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda s: upsert(idx, rows_from_result(fake_result(s, 2, stem=s[:4]))), shas))
    got = read_index(idx)
    assert len(got) == 32 and {r["sha256"] for r in got} == set(shas)
