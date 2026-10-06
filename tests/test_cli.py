import json

import pytest
import soundfile as sf

from barlab.cli import main
from barlab.io import sha256_file
from barlab.store import read_index
from barlab.synth import Scenario, make_signal
from conftest import make_project

PNGS = ["waveform.png", "spectrogram.png", "psd.png", "decay.png", "hit_zoom.png"]


@pytest.fixture(scope="module")
def cli_project(tmp_path_factory):
    proj = make_project(tmp_path_factory.mktemp("cli"))
    raw = proj.raw / "rec_test.wav"  # a "real" recording: read-only, like data/raw/
    x, _ = make_signal(Scenario(snr_db=25.0, seed=99))
    sf.write(raw, x, 192_000, subtype="PCM_16")
    raw.chmod(0o444)
    return proj


def _run(proj, *args):
    return main(["--root", str(proj.root), *args])


def test_analyze_end_to_end_and_idempotent(cli_project):
    proj = cli_project
    raw = proj.raw / "rec_test.wav"
    before = sha256_file(raw)
    listing = sorted(p.name for p in proj.raw.iterdir())

    assert _run(proj, "analyze", str(raw)) == 0
    out = proj.results / "rec_test"
    res = json.loads((out / "result.json").read_text())
    for png in PNGS:
        assert (out / png).is_file(), png
    assert (out / "result.json").stat().st_size < 30_000
    assert res["file"]["source"] == "raw" and res["file"]["sha256"] == before
    n_rows = len(read_index(proj.index))
    assert n_rows == max(1, len(res["peaks"]))

    assert _run(proj, "analyze", str(raw), "--no-plots") == 0
    assert len(read_index(proj.index)) == n_rows  # re-run: no duplicates
    assert sha256_file(raw) == before  # input untouched
    assert sorted(p.name for p in proj.raw.iterdir()) == listing


def test_analyze_glob_then_table(cli_project, capsys):
    proj = cli_project
    assert _run(proj, "analyze", str(proj.synthetic / "synth_fs192k_*.wav"), "--no-plots") == 0
    for s in ("synth_fs192k_snr40", "synth_fs192k_snr20", "synth_fs192k_snr10"):
        assert (proj.results / s / "result.json").is_file()
    capsys.readouterr()
    assert _run(proj, "table", "synth_fs192k_snr40", "--md") == 0
    out = capsys.readouterr().out
    assert out.lstrip().startswith("|") and "f (Hz)" in out and "bar_mode_candidate" in out


def test_compare_history_and_batch(cli_project, capsys):
    proj = cli_project
    _run(proj, "analyze", str(proj.synthetic / "synth_fs192k_*.wav"), "--no-plots")
    capsys.readouterr()
    assert _run(proj, "compare", "synth_fs192k_snr20", "--md") == 0
    out = capsys.readouterr().out
    assert "synth_fs192k_snr40" in out and "ppm" in out
    head, sep = out.splitlines()[:2]
    assert head.count("|") == sep.count("|"), "markdown header must not contain stray pipes"
    assert "rec_test" not in out  # real recordings are never compared with synthetic ones
    assert _run(proj, "compare", "synth_fs192k_snr40", "synth_fs192k_snr20", "--md") == 0
    out = capsys.readouterr().out
    assert "synth_fs192k_snr40" in out and "synth_fs192k_snr20" in out


def test_pending_lists_only_unanalysed_raw_files(cli_project, capsys):
    proj = cli_project
    x, _ = make_signal(Scenario(snr_db=15.0, seed=98))
    sf.write(proj.raw / "rec_new.wav", x, 192_000, subtype="PCM_16")
    capsys.readouterr()
    assert _run(proj, "pending") == 0
    out = capsys.readouterr().out.split()
    assert any(o.endswith("rec_new.wav") for o in out)
    assert not any(o.endswith("rec_test.wav") for o in out)


def test_refuses_to_write_into_raw(cli_project):
    proj = cli_project
    assert _run(proj, "demo", "--out", str(proj.raw / "x")) != 0
    assert not (proj.raw / "x").exists()
    assert _run(proj, "analyze", str(proj.raw / "rec_test.wav"), "--results", str(proj.raw / "res")) != 0
    assert not (proj.raw / "res").exists()


def test_compare_significance_is_symmetric(cli_project, capsys):
    proj = cli_project
    _run(proj, "analyze", str(proj.synthetic / "synth_fs192k_*.wav"), "--no-plots")

    def signif(target, other):
        capsys.readouterr()
        _run(proj, "compare", target, "--md")
        rows = [r for r in capsys.readouterr().out.splitlines() if other in r and r.startswith("|")]
        return sorted(float(r.split("|")[7]) for r in rows)

    assert signif("synth_fs192k_snr40", "synth_fs192k_snr20") == signif("synth_fs192k_snr20", "synth_fs192k_snr40")
