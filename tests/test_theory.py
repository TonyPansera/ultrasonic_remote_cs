import numpy as np
import pytest

from barlab.theory import (
    ALUMINIUM,
    Bar,
    beta_roots,
    flexural_eb_modes,
    flexural_timoshenko_modes,
    implied_length_longitudinal,
    implied_lengths,
    longitudinal_modes,
    mode_table,
    nearest_mode,
    timoshenko_fe_frequencies,
    timoshenko_pinned_exact,
    torsional_modes,
    validity,
)

C = np.sqrt(69e9 / 2700.0)
STUBBY = Bar(L=0.0665, d=0.016)


def _by_n(modes, n):
    return next(m for m in modes if m["n"] == n)


def test_longitudinal_free_free_and_rayleigh_love():
    modes = longitudinal_modes(STUBBY, fmax=200e3)
    for n in (1, 2, 3):
        m = _by_n(modes, n)
        assert m["f_simple_hz"] == pytest.approx(n * C / (2 * STUBBY.L), rel=1e-12)
        k = n * np.pi / STUBBY.L
        rp = STUBBY.d / (2 * np.sqrt(2))
        assert m["f_hz"] == pytest.approx(m["f_simple_hz"] / np.sqrt(1 + (0.33 * k * rp) ** 2), rel=1e-12)
        assert m["f_hz"] < m["f_simple_hz"]
    assert 1 - _by_n(modes, 1)["f_hz"] / _by_n(modes, 1)["f_simple_hz"] == pytest.approx(0.0039, abs=3e-4)


def test_eb_roots():
    assert beta_roots("free-free", 3) == pytest.approx([4.730041, 7.853205, 10.995608], abs=1e-5)
    assert beta_roots("clamped-free", 3) == pytest.approx([1.875104, 4.694091, 7.854757], abs=1e-5)


def test_eb_frequency_formula():
    bar = Bar(L=0.2, d=0.016)
    kappa = np.sqrt(69e9 * (np.pi * 0.016**4 / 64) / (2700 * np.pi * 0.016**2 / 4))
    f1 = 4.730041**2 / (2 * np.pi * bar.L**2) * kappa
    assert _by_n(flexural_eb_modes(bar, fmax=100e3), 1)["f_hz"] == pytest.approx(f1, rel=1e-6)


def test_timoshenko_fe_matches_closed_form_pinned_pinned():
    fe = timoshenko_fe_frequencies(STUBBY, bc="pinned-pinned", n_el=400, n_modes=12)
    for n in range(1, 6):
        f_low, _ = timoshenko_pinned_exact(STUBBY, n)
        assert np.min(np.abs(fe - f_low)) / f_low < 0.002, (n, f_low, fe[:8])


def test_timoshenko_converges_to_eb_for_slender_bar():
    bar = Bar(L=1.6, d=0.016)  # L/d = 100
    eb = flexural_eb_modes(bar, fmax=2e3)
    ti = flexural_timoshenko_modes(bar, fmax=2e3)
    for n in (1, 2, 3):
        e, t = _by_n(eb, n)["f_hz"], _by_n(ti, n)["f_hz"]
        assert t < e
        assert (e - t) / e < 0.005


def test_timoshenko_well_below_eb_for_stubby_bar():
    eb = flexural_eb_modes(STUBBY, fmax=150e3)
    ti = flexural_timoshenko_modes(STUBBY, fmax=150e3)
    for n in (1, 2):
        e, t = _by_n(eb, n)["f_hz"], _by_n(ti, n)["f_hz"]
        assert t < e and (e - t) / e > 0.05  # L/d ~ 4: EB is badly off
        assert _by_n(ti, n)["f_simple_hz"] == pytest.approx(e)


def test_clamped_free_and_torsional():
    bar = Bar(L=0.1, d=0.016, bc="clamped-free")
    lon = longitudinal_modes(bar, fmax=100e3)
    assert _by_n(lon, 1)["f_simple_hz"] == pytest.approx(C / (4 * bar.L), rel=1e-12)
    assert _by_n(lon, 2)["f_simple_hz"] == pytest.approx(3 * C / (4 * bar.L), rel=1e-12)
    eb = flexural_eb_modes(bar, fmax=100e3)
    kappa = bar.d / 4 * C
    assert _by_n(eb, 1)["f_hz"] == pytest.approx(1.875104**2 / (2 * np.pi * bar.L**2) * kappa, rel=1e-5)
    G = 69e9 / (2 * 1.33)
    tor = torsional_modes(Bar(L=0.1, d=0.016), fmax=100e3)
    assert _by_n(tor, 1)["f_hz"] == pytest.approx(np.sqrt(G / 2700) / (2 * 0.1), rel=1e-12)


def test_mode_table_below_nyquist_and_labelled():
    table = mode_table([STUBBY], fmax=96_000.0)
    assert table and all(0 < m["f_hz"] < 96_000.0 for m in table)
    assert [m["f_hz"] for m in table] == sorted(m["f_hz"] for m in table)
    assert {m["family"] for m in table} == {"longitudinal", "flexural", "torsional"}
    assert all({"bar", "family", "n", "model", "f_hz", "f_simple_hz"} <= set(m) for m in table)


def test_validity_flags():
    assert any("Euler-Bernoulli" in v for v in validity(STUBBY))
    assert not any("Euler-Bernoulli" in v for v in validity(Bar(L=0.5, d=0.016)))


def test_implied_length_round_trip():
    f_l1 = _by_n(longitudinal_modes(STUBBY, fmax=200e3), 1)["f_hz"]
    assert implied_length_longitudinal(f_l1, 0.016, ALUMINIUM, 1, "free-free") == pytest.approx(STUBBY.L, rel=1e-9)
    f_t2 = _by_n(flexural_timoshenko_modes(STUBBY, fmax=200e3), 2)["f_hz"]
    hyp = implied_lengths(f_t2, d=0.016)
    flex2 = next(h for h in hyp if h["family"] == "flexural" and h["n"] == 2)
    assert flex2["L_mm"] == pytest.approx(STUBBY.L * 1e3, rel=2e-3)
    assert any(h["family"] == "torsional" for h in hyp)


def test_nearest_mode_and_ambiguity():
    modes = [{"bar": "a", "family": "longitudinal", "n": 1, "model": "x", "f_hz": 38_000.0},
             {"bar": "a", "family": "flexural", "n": 2, "model": "x", "f_hz": 40_000.0}]
    m = nearest_mode(38_050.0, modes)
    assert m["family"] == "longitudinal" and m["unambiguous"]
    assert m["rel_err"] == pytest.approx(50 / 38_000.0)
    assert not nearest_mode(39_000.0, modes)["unambiguous"]
    assert nearest_mode(38_000.0, []) is None


def test_theory_cli(capsys):
    from barlab.cli import main

    assert main(["theory", "--L", "66.5mm", "--d", "16mm", "--fmax", "100000"]) == 0
    out = capsys.readouterr().out
    assert "longitudinal" in out and "timoshenko" in out.lower()
    assert main(["theory", "--d", "16mm", "--implied-from", "38000"]) == 0
    assert "L (mm)" in capsys.readouterr().out


def test_identical_twin_bars_do_not_make_matches_ambiguous():
    # two bars with the same geometry predict the same modes; that is not an ambiguity
    a, b = Bar(L=0.0665, d=0.016, name="A"), Bar(L=0.0665, d=0.016, name="B")
    modes = mode_table([a, b], fmax=100e3)
    f1 = _by_n(longitudinal_modes(a, fmax=100e3), 1)["f_hz"]
    m = nearest_mode(f1 * 1.003, modes)
    assert m["family"] == "longitudinal" and m["n"] == 1 and m["unambiguous"]
    assert m["bar"] == "A,B"
