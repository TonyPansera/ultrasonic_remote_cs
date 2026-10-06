import numpy as np
import pytest

from barlab.decay import fit_peaks
from barlab.segment import segment
from barlab.spectra import compute_spectra, find_peaks
from barlab.synth import Mode, Scenario, make_signal


def _decay_at(x, fs, params, f0):
    seg = segment(x, fs, params)
    spec = compute_spectra(x, fs, seg, params)
    peaks = find_peaks(spec, fs, params)
    decays = fit_peaks(x, fs, seg, spec, peaks, params)
    k = int(np.argmin([abs(p.f_hz - f0) for p in peaks]))
    return peaks[k], decays[k], seg


@pytest.mark.parametrize("q", [200.0, 500.0, 5000.0])
def test_single_mode_q_tau_f(params, q):
    f0 = 38_000.0
    tau = q / (np.pi * f0)
    sc = Scenario(modes=(Mode(f0, q, 1.0),), lowq=(), env_tones=(), snr_db=30.0, seed=21,
                  spacing_s=max(0.3, 14 * tau), jitter_s=0.01)
    x, _ = make_signal(sc)
    peak, d, seg = _decay_at(x, sc.fs, params, f0)
    assert d is not None
    assert d.n_fitted >= len(seg.hits) - 1
    assert d.q == pytest.approx(q, rel=0.15)
    assert d.tau_s == pytest.approx(tau, rel=0.15)
    # aggregates are medians of per-hit values, so Q ~ pi f tau only to within hit scatter
    assert d.q == pytest.approx(np.pi * d.f_hz * d.tau_s, rel=1e-3)
    assert d.r2 > 0.9
    assert d.f_hz == pytest.approx(f0, rel=2e-4)
    assert d.f_range_rel < 0.001


@pytest.mark.parametrize("stem,snr", [("synth_fs192k_snr40", 40), ("synth_fs192k_snr20", 20),
                                      ("synth_fs192k_snr10", 10)])
def test_demo_modes_q_at_several_snrs(signals, params, stem, snr):
    x, truth = signals[stem]
    for m in truth["modes"]:
        _, d, seg = _decay_at(x, truth["fs_hz"], params, m["f_hz"])
        assert d.n_freq >= 2 and d.q is not None, (stem, m)
        assert d.q == pytest.approx(m["q"], rel=0.15), (stem, m["f_hz"], d.q, d.q_source)
        assert d.f_hz == pytest.approx(m["f_hz"], rel=0.002)


def test_stacked_fit_agrees_with_per_hit_fits(signals, params):
    x, truth = signals["synth_fs192k_snr40"]
    _, d, _ = _decay_at(x, truth["fs_hz"], params, 38_000.0)
    assert d.q_source == "hits" and d.tau_stack_s is not None
    assert d.tau_stack_s == pytest.approx(d.tau_s, rel=0.02)


def test_lowq_resonance_fits_low_q(signals, params):
    x, truth = signals["synth_fs192k_snr20"]
    _, d, _ = _decay_at(x, truth["fs_hz"], params, 15_000.0)
    assert d.n_fitted >= 2
    assert d.q < params.q_min


def test_slow_decay_gives_q_lower_bound(params):
    # Q = 6e5 at 38 kHz: tau = 5 s, but hits every 1 s -> < 6 dB of decay per window
    sc = Scenario(modes=(Mode(38_000.0, 6e5, 1.0),), lowq=(), env_tones=(), snr_db=40.0, seed=41, click_amp=30.0,
                  n_hits=4, spacing_s=1.0, jitter_s=0.0)
    x, _ = make_signal(sc)
    _, d, _ = _decay_at(x, sc.fs, params, 38_000.0)
    assert d.q is None and d.steady_s is None
    assert d.q_lower is not None and params.q_min < d.q_lower < 6e5  # a true, conservative bound
    assert d.f_hz == pytest.approx(38_000.0, rel=1e-5) and d.n_freq >= 3


def steady_tone(fs=192_000, f0=42_000.0, on=(1.0, 3.0), dur=4.0, amp=0.3, sigma=1e-3, seed=5):
    """A constant tone switched on and off abruptly: no impact, no decay."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(dur * fs)) / fs
    x = sigma * rng.standard_normal(len(t))
    gate = (t >= on[0]) & (t < on[1])
    x[gate] += amp * np.sin(2 * np.pi * f0 * t[gate])
    return x


def test_steady_tone_is_not_a_ringdown(params):
    fs = 192_000
    x = steady_tone(fs)
    _, d, seg = _decay_at(x, fs, params, 42_000.0)
    assert len(seg.hits) >= 1  # the switch-on looks like an onset
    assert d.q is None and d.steady_s is not None and d.steady_s > 1.0


def test_frequency_standard_error_is_honest(params):
    """Across independent noise realisations, the quoted standard error must describe the
    real scatter of the estimate (a too-small error bar is a wrong claim)."""
    f0, z = 38_000.0, []
    for seed in range(100, 112):
        sc = Scenario(snr_db=20.0, seed=seed)  # the full demo mixture: two modes, low-Q mode, env line
        x, _ = make_signal(sc)
        _, d, _ = _decay_at(x, sc.fs, params, f0)
        z.append((d.f_hz - f0) / d.f_sem_hz)
    z = np.abs(np.array(z))
    assert np.median(z) < 1.5, z  # ~0.67 for an honest Gaussian error bar
    assert np.mean(z > 3.0) <= 0.2, z
