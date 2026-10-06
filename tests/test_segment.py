import numpy as np
import pytest

from barlab.segment import segment
from barlab.synth import DEMO, Mode, Scenario, make_signal


@pytest.mark.parametrize("stem", list(DEMO))
def test_hits_detected_at_true_onsets(signals, params, stem):
    x, truth = signals[stem]
    fs = truth["fs_hz"]
    seg = segment(x, fs, params)
    onsets = np.array([h.onset for h in seg.hits]) / fs
    assert len(onsets) == len(truth["hit_times_s"])
    assert np.max(np.abs(onsets - np.array(truth["hit_times_s"]))) < 0.5e-3


def test_windows_are_disjoint_and_ordered(signals, params):
    x, truth = signals["synth_fs192k_snr20"]
    fs = truth["fs_hz"]
    seg = segment(x, fs, params)
    guard = int(params.pre_guard_ms * 1e-3 * fs)
    for i, h in enumerate(seg.hits):
        assert h.attack[0] == h.onset
        assert h.attack[1] - h.attack[0] == int(round(params.attack_ms * 1e-3 * fs))
        assert h.ringdown[0] == h.attack[1]
        assert h.ringdown[1] > h.ringdown[0]
        if i + 1 < len(seg.hits):
            assert h.ringdown[1] <= seg.hits[i + 1].onset - guard
    for a, b in seg.noise:
        for h in seg.hits:
            assert b <= h.onset - guard or a >= h.ringdown[1], "noise overlaps a hit"


def test_noise_window_prefers_quiet_lead_in(signals, params):
    x, truth = signals["synth_fs192k_snr20"]
    fs = truth["fs_hz"]
    seg = segment(x, fs, params)
    assert not seg.noise_fallback
    first = min(a for a, _ in seg.noise)
    assert first < truth["hit_times_s"][0] * fs
    total = sum(b - a for a, b in seg.noise) / fs
    assert total >= params.min_noise_ms * 1e-3


def test_ringdown_ends_in_noise_not_at_cap(signals, params):
    x, truth = signals["synth_fs192k_snr40"]
    seg = segment(x, truth["fs_hz"], params)
    # Q500 @ 38 kHz has tau = 4.2 ms: ringdown into the noise takes tens of ms, not 0.3 s
    lengths = [(h.ringdown[1] - h.ringdown[0]) / truth["fs_hz"] for h in seg.hits]
    assert all(0.01 < L < 0.12 for L in lengths), lengths
    assert all(h.end_reason == "noise" for h in seg.hits)


def test_bounce_is_merged(params):
    sc = Scenario(seed=7, extra_hits_s=(0.6 + 0.003,))  # second contact 3 ms after the first hit
    x, truth = make_signal(sc)
    seg = segment(x, sc.fs, params)
    assert len(seg.hits) == sc.n_hits


def test_no_quiet_segment_falls_back_to_pre_hit(params):
    sc = Scenario(lead_s=0.008, spacing_s=0.06, jitter_s=0.004, n_hits=30, tail_s=0.01,
                  modes=(Mode(38_000.0, 5000.0, 1.0),), lowq=(), env_tones=(), seed=8)
    x, _ = make_signal(sc)
    seg = segment(x, sc.fs, params)
    assert seg.noise_fallback
    assert any(w["code"] == "NOISE_FALLBACK" for w in seg.warnings)
    assert seg.noise, "fallback must still provide pre-hit samples"
