import numpy as np
import pytest

from barlab.segment import segment
from barlab.spectra import compute_spectra, find_peaks
from barlab.synth import DEMO, Scenario, make_signal


def _spectra(x, fs, params):
    seg = segment(x, fs, params)
    return seg, compute_spectra(x, fs, seg, params)


def test_linear_axis_to_nyquist(signals, params):
    for stem in DEMO:
        x, truth = signals[stem]
        fs = truth["fs_hz"]
        _, spec = _spectra(x, fs, params)
        df = np.diff(spec.freqs)
        assert spec.freqs[0] == 0.0 and spec.freqs[-1] == pytest.approx(fs / 2)
        assert np.allclose(df, df[0])
        assert spec.bin_hz <= spec.resolution_hz


def test_resolution_meets_target_when_windows_allow(signals, params):
    x, truth = signals["synth_fs192k_snr40"]
    _, spec = _spectra(x, truth["fs_hz"], params)
    assert spec.resolution_hz <= params.target_res_hz
    assert spec.nperseg_target == 4096


def test_resolution_reported_when_windows_are_short(signals, params):
    x, truth = signals["synth_fs192k_snr10"]
    _, spec = _spectra(x, truth["fs_hz"], params)
    # short ringdowns at low SNR: resolution is coarser than the target, and says so
    assert spec.resolution_hz == pytest.approx(truth["fs_hz"] / np.median(spec.nperseg_hits))


@pytest.mark.parametrize("stem", ["synth_fs192k_snr40", "synth_fs192k_snr20", "synth_fs192k_snr10"])
def test_peaks_near_truth(signals, params, stem):
    x, truth = signals[stem]
    _, spec = _spectra(x, truth["fs_hz"], params)
    peaks = find_peaks(spec, truth["fs_hz"], params)
    fs_found = np.array([p.f_hz for p in peaks])
    for f in (38_000.0, 76_000.0, 30_000.0):
        assert np.min(np.abs(fs_found / f - 1)) < 0.002, (f, fs_found)
    env = min(peaks, key=lambda p: abs(p.f_hz - 30_000.0))
    assert env.snr_db < params.env_snr_db  # same level in noise and ringdown
    assert env.noise_line_db > params.noise_line_db
    m1 = min(peaks, key=lambda p: abs(p.f_hz - 38_000.0))
    assert m1.above_floor_db >= params.min_above_floor_db and m1.snr_db > 10
    assert m1.width_hz > 0


def test_click_only_gives_no_peak(params):
    sc = Scenario(modes=(), lowq=(), env_tones=(), seed=11, snr_db=40.0)
    x, _ = make_signal(sc)
    _, spec = _spectra(x, sc.fs, params)
    assert find_peaks(spec, sc.fs, params) == []


def test_snr_per_bin_is_ratio_of_psds(signals, params):
    x, truth = signals["synth_fs192k_snr20"]
    _, spec = _spectra(x, truth["fs_hz"], params)
    expect = 10 * np.log10(spec.ring_psd / spec.noise_psd)
    assert np.allclose(spec.snr_db, expect)


def test_long_ringdowns_resolve_a_close_doublet(params):
    from barlab.synth import Mode

    # two similar bars: 8400 and 8440 Hz, Q 1000 (tau ~ 38 ms), 40 Hz apart
    sc = Scenario(modes=(Mode(8_400.0, 1000.0, 1.0), Mode(8_440.0, 1000.0, 0.8)), lowq=(), env_tones=(),
                  snr_db=40.0, seed=12, spacing_s=0.6, jitter_s=0.02)
    x, _ = make_signal(sc)
    _, spec = _spectra(x, sc.fs, params)
    assert spec.resolution_hz < params.target_res_hz  # finer than the 50 Hz target: the windows allow it
    found = np.array([p.f_hz for p in find_peaks(spec, sc.fs, params)])
    for f in (8_400.0, 8_440.0):
        assert np.min(np.abs(found - f)) < 5.0, found


def test_strong_steady_line_gives_one_peak_not_sidelobes(params):
    from test_decay import steady_tone

    x = steady_tone()
    _, spec = _spectra(x, 192_000, params)
    near = [p.f_hz for p in find_peaks(spec, 192_000, params) if abs(p.f_hz - 42_000.0) < 500.0]
    assert len(near) == 1 and abs(near[0] - 42_000.0) < 2.0, near


@pytest.mark.parametrize("stem", ["synth_fs192k_snr20", "synth_fs192k_snr10"])
def test_stationary_line_has_no_snr_spikes(signals, params, stem):
    """Noise and ringdown PSDs use matched segment lengths, so a line present in both (the
    30 kHz environment tone) gives ~0 dB per-bin SNR on and around it, not spurious spikes."""
    x, truth = signals[stem]
    _, spec = _spectra(x, truth["fs_hz"], params)
    near_line = np.abs(spec.freqs - 30_000.0) <= 6 * spec.bin_hz
    assert spec.snr_db[near_line].max() < params.env_snr_db, spec.snr_db[near_line]
