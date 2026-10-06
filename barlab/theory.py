"""Expected vibration modes of a solid cylindrical bar.

Families (each free-free or clamped-free):
  longitudinal  f = c k / 2pi, c = sqrt(E/rho)            (f_simple: n c / 2L)
                Rayleigh-Love lateral-inertia correction:  f = f_simple / sqrt(1 + (nu k r_p)^2)
  flexural      Euler-Bernoulli: f = (beta L)^2 / (2 pi L^2) * sqrt(E I / rho A)   (f_simple)
                Timoshenko (shear + rotary inertia), 1D finite elements          (f_hz)
  torsional     f = c_T k / 2pi, c_T = sqrt(G/rho) (exact for a circular section)

k = n pi / L (free-free) or (2n-1) pi / 2L (clamped-free). For stubby bars (L/d < 10)
Euler-Bernoulli overestimates flexural frequencies badly; matching uses the best model
per family (f_hz). `implied_lengths` solves the inverse problem when L is unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import linalg, optimize


@dataclass(frozen=True)
class Material:
    name: str
    E: float  # Young's modulus [Pa]
    rho: float  # density [kg/m^3]
    nu: float  # Poisson's ratio

    @property
    def G(self) -> float:
        return self.E / (2.0 * (1.0 + self.nu))

    @property
    def c(self) -> float:
        """Bar (longitudinal) wave speed sqrt(E/rho)."""
        return float(np.sqrt(self.E / self.rho))

    @property
    def c_t(self) -> float:
        """Torsional wave speed sqrt(G/rho)."""
        return float(np.sqrt(self.G / self.rho))


ALUMINIUM = Material("aluminium", 69e9, 2700.0, 0.33)
MATERIALS = {
    "aluminium": ALUMINIUM,
    "aluminum": ALUMINIUM,
    "steel": Material("steel", 200e9, 7850.0, 0.29),
    "brass": Material("brass", 100e9, 8500.0, 0.34),
}

BOUNDARY_CONDITIONS = ("free-free", "clamped-free")


@dataclass(frozen=True)
class Bar:
    L: float | None  # length [m]; None = unknown
    d: float  # diameter [m]
    material: Material = ALUMINIUM
    bc: str = "free-free"
    name: str = "bar"

    def __post_init__(self):
        if self.bc not in BOUNDARY_CONDITIONS:
            raise ValueError(f"bc must be one of {BOUNDARY_CONDITIONS}, got {self.bc!r}")

    @property
    def A(self) -> float:
        return np.pi * self.d**2 / 4.0

    @property
    def I(self) -> float:  # noqa: E743 - second moment of area
        return np.pi * self.d**4 / 64.0

    @property
    def r_g(self) -> float:
        """Radius of gyration sqrt(I/A) = d/4."""
        return self.d / 4.0

    @property
    def r_p(self) -> float:
        """Polar radius of gyration sqrt(J/A) = d / (2 sqrt 2)."""
        return self.d / (2.0 * np.sqrt(2.0))

    @property
    def kappa_shear(self) -> float:
        """Timoshenko shear coefficient of a solid circle (Cowper)."""
        nu = self.material.nu
        return 6.0 * (1.0 + nu) / (7.0 + 6.0 * nu)


def _mode(bar: Bar, family: str, n: int, model: str, f: float, f_simple: float | None, note=None) -> dict:
    return {"bar": bar.name, "family": family, "n": n, "model": model, "f_hz": float(f),
            "f_simple_hz": None if f_simple is None else float(f_simple), "note": note}


def _k_axial(L: float, bc: str, n: int) -> float:
    return n * np.pi / L if bc == "free-free" else (2 * n - 1) * np.pi / (2 * L)


# ---------------------------------------------------------------- longitudinal
def longitudinal_modes(bar: Bar, fmax: float, n_max: int = 400) -> list[dict]:
    m = bar.material
    out = []
    for n in range(1, n_max + 1):
        k = _k_axial(bar.L, bar.bc, n)
        f0 = m.c * k / (2 * np.pi)
        x = m.nu * k * bar.r_p
        f = f0 / np.sqrt(1.0 + x**2)
        if f > fmax or f0 > 50 * fmax:
            break
        note = "Rayleigh-Love approximate (nu k r_p > 0.5)" if x > 0.5 else None
        out.append(_mode(bar, "longitudinal", n, "rayleigh-love", f, f0, note))
    return out


def implied_length_longitudinal(f: float, d: float, material: Material = ALUMINIUM, n: int = 1,
                                bc: str = "free-free") -> float:
    """Length whose Rayleigh-Love longitudinal mode n sits at f (closed form)."""
    n_eff = n if bc == "free-free" else (2 * n - 1) / 2.0
    a = material.nu * n_eff * np.pi * d / (2.0 * np.sqrt(2.0))
    den = (material.c * n_eff / 2.0) ** 2 - (f * a) ** 2
    if den <= 0:
        return float("nan")
    return float(np.sqrt(den) / f)


# ------------------------------------------------------------ flexural (EB)
def beta_roots(bc: str, n: int) -> np.ndarray:
    """First n roots beta*L of cos(x)cosh(x) = 1 (free-free) or -1 (clamped-free)."""
    sign = -1.0 if bc == "free-free" else 1.0
    out = []
    for k in range(1, n + 1):
        x0 = (k + 0.5) * np.pi if bc == "free-free" else (k - 0.5) * np.pi
        g = lambda x: np.cos(x) + sign / np.cosh(x)  # noqa: E731 - well-conditioned form
        out.append(optimize.brentq(g, x0 - 0.4, x0 + 0.4, xtol=1e-14))
    return np.array(out)


def _eb_freq(bar: Bar, beta_l: float) -> float:
    kap = np.sqrt(bar.material.E * bar.I / (bar.material.rho * bar.A))
    return beta_l**2 / (2 * np.pi * bar.L**2) * kap


def flexural_eb_modes(bar: Bar, fmax: float) -> list[dict]:
    kap = np.sqrt(bar.material.E * bar.I / (bar.material.rho * bar.A))
    bl_max = np.sqrt(fmax * 2 * np.pi * bar.L**2 / kap)
    n_need = int(bl_max / np.pi) + 2
    out = []
    for n, bl in enumerate(beta_roots(bar.bc, n_need), start=1):
        f = _eb_freq(bar, bl)
        if f > fmax:
            break
        out.append(_mode(bar, "flexural", n, "euler-bernoulli", f, f))
    return out


# ----------------------------------------------------- flexural (Timoshenko)
def shear_cutoff_hz(bar: Bar) -> float:
    """Second-spectrum cutoff sqrt(kappa G A / rho I) / 2pi (independent of L)."""
    m = bar.material
    return float(np.sqrt(bar.kappa_shear * m.G * bar.A / (m.rho * bar.I)) / (2 * np.pi))


def timoshenko_fe_frequencies(bar: Bar, bc: str | None = None, n_el: int = 200,
                              n_modes: int | None = None, fmax: float | None = None) -> np.ndarray:
    """Elastic flexural frequencies [Hz] from a Timoshenko beam FE model.

    Two-node elements, linear w and psi, one-point (reduced) shear integration
    (no shear locking), consistent translational + rotary mass.
    bc: 'free-free' | 'clamped-free' | 'pinned-pinned' (default: bar.bc).
    Rigid-body modes are dropped.
    """
    bc = bc or bar.bc
    m = bar.material
    EI, kGA = m.E * bar.I, bar.kappa_shear * m.G * bar.A
    rA, rI = m.rho * bar.A, m.rho * bar.I
    h = bar.L / n_el
    bs = np.array([-1.0 / h, -0.5, 1.0 / h, -0.5])
    ke = kGA * h * np.outer(bs, bs)
    ke[np.ix_([1, 3], [1, 3])] += EI / h * np.array([[1.0, -1.0], [-1.0, 1.0]])
    me = np.zeros((4, 4))
    lump = np.array([[2.0, 1.0], [1.0, 2.0]]) * h / 6.0
    me[np.ix_([0, 2], [0, 2])] = rA * lump
    me[np.ix_([1, 3], [1, 3])] = rI * lump
    ndof = 2 * (n_el + 1)
    K = np.zeros((ndof, ndof))
    M = np.zeros((ndof, ndof))
    for e in range(n_el):
        idx = np.arange(2 * e, 2 * e + 4)
        K[np.ix_(idx, idx)] += ke
        M[np.ix_(idx, idx)] += me
    fixed = {"free-free": [], "clamped-free": [0, 1], "pinned-pinned": [0, ndof - 2]}[bc]
    keep = np.setdiff1d(np.arange(ndof), fixed)
    K, M = K[np.ix_(keep, keep)], M[np.ix_(keep, keep)]
    n_rigid = 2 if bc == "free-free" else 0
    if fmax is not None:
        w2 = linalg.eigh(K, M, eigvals_only=True, subset_by_value=(-np.inf, (2 * np.pi * fmax) ** 2))
    else:
        want = min((n_modes or 10) + n_rigid, len(keep)) - 1
        w2 = linalg.eigh(K, M, eigvals_only=True, subset_by_index=(0, want))
    f = np.sqrt(np.clip(w2, 0.0, None)) / (2 * np.pi)
    f = np.sort(f)
    f_scale = m.c / bar.L
    return f[f > 1e-4 * f_scale]


def flexural_timoshenko_modes(bar: Bar, fmax: float, n_el: int = 200) -> list[dict]:
    freqs = timoshenko_fe_frequencies(bar, n_el=n_el, fmax=fmax)
    cutoff = shear_cutoff_hz(bar)
    betas = beta_roots(bar.bc, len(freqs)) if len(freqs) else []
    out = []
    for n, (f, bl) in enumerate(zip(freqs, betas), start=1):
        above = f > cutoff
        note = "above shear cutoff (second spectrum); mode numbering approximate" if above else None
        out.append(_mode(bar, "flexural", n, "timoshenko", f, None if above else _eb_freq(bar, bl), note))
    return out


def timoshenko_pinned_exact(bar: Bar, n: int) -> tuple[float, float]:
    """Closed-form Timoshenko frequencies (lower, upper branch) of a pinned-pinned bar."""
    m = bar.material
    EI, kGA = m.E * bar.I, bar.kappa_shear * m.G * bar.A
    rA, rI = m.rho * bar.A, m.rho * bar.I
    k = n * np.pi / bar.L
    a = rA * rI
    b = -(rI * kGA * k**2 + rA * (EI * k**2 + kGA))
    c = kGA * EI * k**4
    disc = np.sqrt(b * b - 4 * a * c)
    lo, hi = (-b - disc) / (2 * a), (-b + disc) / (2 * a)
    return float(np.sqrt(lo) / (2 * np.pi)), float(np.sqrt(hi) / (2 * np.pi))


# ----------------------------------------------------------------- torsional
def torsional_modes(bar: Bar, fmax: float, n_max: int = 400) -> list[dict]:
    out = []
    for n in range(1, n_max + 1):
        f = bar.material.c_t * _k_axial(bar.L, bar.bc, n) / (2 * np.pi)
        if f > fmax:
            break
        out.append(_mode(bar, "torsional", n, "exact-circular", f, f))
    return out


# --------------------------------------------------------------------- table
def mode_table(bars: list[Bar], fmax: float) -> list[dict]:
    """All modes below fmax for every bar of known length, best model per family."""
    out = []
    for bar in bars:
        if bar.L is None:
            continue
        out += longitudinal_modes(bar, fmax)
        out += flexural_timoshenko_modes(bar, fmax)
        out += torsional_modes(bar, fmax)
    return sorted(out, key=lambda m: m["f_hz"])


def validity(bar: Bar) -> list[str]:
    if bar.L is None:
        return []
    s = bar.L / bar.d
    out = []
    if s < 10:
        out.append(f"L/d = {s:.1f} < 10: Euler-Bernoulli flexural theory unreliable "
                   "(shear and rotary inertia); Timoshenko values used for matching")
    if s < 3:
        out.append(f"L/d = {s:.1f} < 3: even Timoshenko is approximate (3D effects)")
    return out


def nearest_mode(f: float, modes: list[dict], unambiguous_ratio: float = 3.0) -> dict | None:
    """Nearest theoretical mode, its relative error, and whether the match is
    unambiguous (next-nearest mode at least `unambiguous_ratio` times farther)."""
    if not modes:
        return None
    fm = np.array([m["f_hz"] for m in modes])
    dist = np.abs(fm - f)
    order = np.argsort(dist)
    best = modes[order[0]]
    # identical bars predict identical modes: those twins are the same prediction, not rivals
    same = [j for j in order if abs(fm[j] - best["f_hz"]) <= 1e-9 * best["f_hz"]
            and modes[j]["family"] == best["family"] and modes[j]["n"] == best["n"]]
    rivals = [j for j in order if j not in same]
    nxt = modes[rivals[0]] if rivals else None
    unamb = nxt is None or dist[rivals[0]] >= unambiguous_ratio * dist[order[0]]
    bars = sorted({modes[j]["bar"] for j in same})
    return {
        "bar": ",".join(bars), "family": best["family"], "n": best["n"], "model": best["model"],
        "f_hz": best["f_hz"], "rel_err": float((f - best["f_hz"]) / best["f_hz"]),
        "unambiguous": bool(unamb),
        "next_family": nxt["family"] if nxt else None, "next_n": nxt["n"] if nxt else None,
        "next_f_hz": nxt["f_hz"] if nxt else None,
    }


# ------------------------------------------------------------ inverse problem
def implied_lengths(f: float, d: float, material: Material = ALUMINIUM, bc: str = "free-free",
                    n_long: int = 3, n_flex: int = 3, n_tors: int = 2) -> list[dict]:
    """Bar length L that would put mode (family, n) at the measured frequency f."""
    out = []
    for n in range(1, n_long + 1):
        n_eff = n if bc == "free-free" else (2 * n - 1) / 2.0
        L = implied_length_longitudinal(f, d, material, n, bc)
        out.append({"family": "longitudinal", "n": n, "model": "rayleigh-love",
                    "L_mm": L * 1e3, "L_simple_mm": n_eff * material.c / (2 * f) * 1e3})
    probe = Bar(L=1.0, d=d, material=material, bc=bc)
    kap = np.sqrt(material.E * probe.I / (material.rho * probe.A))
    for n, bl in enumerate(beta_roots(bc, n_flex), start=1):
        L_eb = bl * np.sqrt(kap / (2 * np.pi * f))

        def g(L, n=n):
            fr = timoshenko_fe_frequencies(Bar(L=L, d=d, material=material, bc=bc), n_el=120, n_modes=n)
            return fr[n - 1] - f

        try:
            L_t = optimize.brentq(g, 0.15 * L_eb, 1.0001 * L_eb, xtol=1e-7, rtol=1e-6)
        except (ValueError, IndexError):
            L_t = float("nan")
        out.append({"family": "flexural", "n": n, "model": "timoshenko",
                    "L_mm": L_t * 1e3, "L_simple_mm": L_eb * 1e3})
    for n in range(1, n_tors + 1):
        n_eff = n if bc == "free-free" else (2 * n - 1) / 2.0
        L = n_eff * material.c_t / (2 * f)
        out.append({"family": "torsional", "n": n, "model": "exact-circular",
                    "L_mm": L * 1e3, "L_simple_mm": L * 1e3})
    return out
