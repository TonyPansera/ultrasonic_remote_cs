import pytest

from barlab.io import FS_PHRASE
from conftest import STEM_48K, STEMS_192K


def _near(peaks, f, rel):
    return [p for p in peaks if abs(p["f_hz"] / f - 1) < rel]


@pytest.mark.parametrize("stem", STEMS_192K)
def test_bar_modes_recovered_f_and_q(analyzed, stem):
    res, truth = analyzed[stem]
    cands = [p for p in res["peaks"] if p["class"] == "bar_mode_candidate"]
    for m in truth["modes"]:
        hit = _near(cands, m["f_hz"], 0.002)
        assert len(hit) == 1, (stem, m["f_hz"], [(p["f_hz"], p["class"]) for p in res["peaks"]])
        assert hit[0]["decay"]["q"] == pytest.approx(m["q"], rel=0.15)
        assert hit[0]["decay"]["n_freq"] >= 2


@pytest.mark.parametrize("stem", STEMS_192K)
def test_click_is_never_a_mode(analyzed, stem):
    res, truth = analyzed[stem]
    cands = [p["f_hz"] for p in res["peaks"] if p["class"] == "bar_mode_candidate"]
    assert len(cands) == len(truth["modes"]), cands


@pytest.mark.parametrize("stem", STEMS_192K)
def test_line_in_noise_window_is_environment(analyzed, stem):
    res, _ = analyzed[stem]
    env = _near(res["peaks"], 30_000.0, 0.002)
    assert env and env[0]["class"] == "noise_or_environment"


@pytest.mark.parametrize("stem", STEMS_192K)
def test_low_q_resonance_is_broadband(analyzed, stem):
    res, _ = analyzed[stem]
    lowq = _near(res["peaks"], 15_000.0, 0.02)
    assert lowq and lowq[0]["class"] == "broadband_resonance", lowq


def test_48k_critical_and_every_peak_possible_alias(analyzed):
    res, _ = analyzed[STEM_48K]
    crit = [w for w in res["warnings"] if w["severity"] == "CRITICAL"]
    assert any(w["code"] == "FS_BELOW_96K" and FS_PHRASE in w["message"] for w in crit)
    assert res["peaks"], "aliased modes should still be detected and labelled"
    assert all(p["class"] == "possible_alias" for p in res["peaks"])
    assert _near(res["peaks"], 10_000.0, 0.002), "38 kHz aliases to 10 kHz at fs = 48 kHz"
    assert res["summary"]["determinable"] is False
    assert "96 kHz" in res["summary"]["reason"]


def test_theory_match_for_known_geometry(analyzed):
    res, _ = analyzed["synth_fs192k_snr40"]
    p = _near(res["peaks"], 38_000.0, 0.002)[0]
    th = p["theory"]["nearest"]
    assert th["family"] == "longitudinal" and th["n"] == 1
    assert abs(th["rel_err"]) < 0.005
    assert p["theory"]["matches"] is True


@pytest.mark.parametrize("stem", STEMS_192K)
def test_summary_names_the_ringing_frequency(analyzed, stem):
    res, _ = analyzed[stem]
    s = res["summary"]
    assert s["determinable"] is True
    assert s["ringing_hz"] == pytest.approx(38_000.0, rel=0.002)
    assert s["n_candidates"] == 2


def _analyze_signal(tmp_path, x, fs, name="x"):
    import soundfile as sf

    from barlab.paths import Project
    from barlab.pipeline import analyze_file

    p = tmp_path / f"{name}.wav"
    sf.write(p, x, fs, subtype="PCM_16")
    return analyze_file(p, Project.at(tmp_path), make_plots=False)


def test_clipped_ringdowns_are_trimmed_not_discarded(tmp_path, signals):
    import numpy as np

    x, truth = signals["synth_fs192k_snr40"]
    # gain 20 clips ~3.5 ms into the ringdown (like the real strikes in rec2)
    res = _analyze_signal(tmp_path, np.clip(20.0 * x, -1.0, 1.0), 192_000, "clipped")
    assert any(w["code"] == "CLIPPING_RINGDOWN" for w in res["warnings"])
    assert any(h["clip_trim_ms"] > 0 for h in res["hits"])
    assert not any(h["clipped"] for h in res["hits"])
    m1 = _near(res["peaks"], 38_000.0, 0.002)
    assert m1 and m1[0]["class"] == "bar_mode_candidate"
    assert m1[0]["decay"]["q"] == pytest.approx(500.0, rel=0.15)


def test_steady_tone_is_unresolved_not_a_bar_mode(tmp_path):
    from test_decay import steady_tone

    res = _analyze_signal(tmp_path, steady_tone(), 192_000, "tone")
    tone = _near(res["peaks"], 42_000.0, 0.002)
    assert tone and tone[0]["class"] == "unresolved" and "steady" in tone[0]["flags"]
    assert tone[0]["decay"]["steady_s"] > 1.0
    assert res["summary"]["determinable"] is False


def test_very_high_q_mode_is_candidate_with_lower_bound(tmp_path):
    from barlab.synth import Mode, Scenario, make_signal

    sc = Scenario(modes=(Mode(38_000.0, 6e5, 1.0),), lowq=(), env_tones=(), snr_db=40.0, seed=41, click_amp=30.0,
                  n_hits=4, spacing_s=1.0, jitter_s=0.0)
    x, _ = make_signal(sc)
    res = _analyze_signal(tmp_path, x, sc.fs, "highq")
    m = _near(res["peaks"], 38_000.0, 0.002)
    assert m and m[0]["class"] == "bar_mode_candidate" and "q_lower_bound" in m[0]["flags"]
    assert m[0]["decay"]["q"] is None and m[0]["decay"]["q_lower"] > 100
    assert res["summary"]["determinable"] is True and res["summary"]["q_lower"] is not None


def test_aliased_peaks_get_no_theory_match(analyzed):
    res, _ = analyzed[STEM_48K]
    assert all(p["theory"]["nearest"] is None for p in res["peaks"])
