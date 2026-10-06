"""Per-recording metadata (bar geometry, material, support, mic, conditions).

Merge order (later wins): data/meta/_defaults.toml -> data/meta/<stem>.toml -> CLI flags.
Bars listed in a per-file sidecar inherit any key they omit from the first default bar.
Unknown [recording] keys are kept under recording.extra. Values are never invented:
an unknown length stays unknown (theory then reports implied lengths instead).

    [recording]
    mic = "..."            temperature_c = 21.5     striking = "..."
    support = "..."        notes = "..."
    [[bar]]
    name = "A"   L = "66.5mm"   d = "16mm"   material = "aluminium"   bc = "free-free"
    # optional explicit constants: E = 69e9, rho = 2700, nu = 0.33
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from barlab.theory import ALUMINIUM, BOUNDARY_CONDITIONS, MATERIALS, Bar, Material

RECORDING_KEYS = ("mic", "temperature_c", "striking", "support", "distance_m", "operator", "notes")
_UNITS = {"m": 1.0, "cm": 1e-2, "mm": 1e-3, "um": 1e-6, "in": 0.0254}


def parse_length(v) -> float | None:
    """'66.5mm' | '6.65cm' | '0.0665' | 0.0665 -> metres (plain numbers are metres)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)\s*([a-zA-Z]*)\s*", str(v))
    if not m:
        raise ValueError(f"cannot parse length {v!r} (use e.g. '66.5mm' or 0.0665)")
    unit = (m.group(2) or "m").lower()
    if unit not in _UNITS:
        raise ValueError(f"unknown length unit {unit!r} in {v!r}; use one of {sorted(_UNITS)}")
    return float(m.group(1)) * _UNITS[unit]


def _read(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _material(b: dict) -> Material:
    name = str(b.get("material", "aluminium")).lower()
    base = MATERIALS.get(name)
    if base is None and not all(k in b for k in ("E", "rho", "nu")):
        raise ValueError(f"unknown material {name!r}: give E, rho and nu explicitly")
    base = base or ALUMINIUM
    return Material(name=name, E=float(b.get("E", base.E)), rho=float(b.get("rho", base.rho)),
                    nu=float(b.get("nu", base.nu)))


def load_meta(meta_dir: Path, stem: str, cli: dict | None = None, root: Path | None = None) -> dict:
    meta_dir = Path(meta_dir)
    sources, defaults, per = [], {}, {}
    for p, slot in ((meta_dir / "_defaults.toml", "d"), (meta_dir / f"{stem}.toml", "p")):
        if p.is_file():
            data = _read(p)
            if slot == "d":
                defaults = data
            else:
                per = data
            sources.append(str(p.relative_to(root)) if root and p.is_relative_to(root) else str(p))

    recording = {**defaults.get("recording", {}), **per.get("recording", {})}
    base_bar = dict((defaults.get("bar") or [{}])[0])
    bars = [{**base_bar, **b} for b in (per.get("bar") or defaults.get("bar") or [])]

    cli = {k: v for k, v in (cli or {}).items() if v not in (None, [], "")}
    if cli.get("L"):
        tmpl = bars[0] if bars else base_bar
        Ls = cli.pop("L")
        bars = [{**tmpl, "L": L, "name": (f"bar{i + 1}" if len(Ls) > 1 else tmpl.get("name", "bar"))}
                for i, L in enumerate(Ls)]
    if cli and not bars:
        bars = [{"name": "bar"}]
    for b in bars:
        b.update({k: v for k, v in cli.items() if k in ("d", "bc", "material", "E", "rho", "nu")})

    norm = []
    for b in bars:
        bc = str(b.get("bc", "free-free"))
        if bc not in BOUNDARY_CONDITIONS:
            raise ValueError(f"bc must be one of {BOUNDARY_CONDITIONS}, got {bc!r}")
        mat = _material(b)
        norm.append({"name": str(b.get("name", "bar")), "L_m": parse_length(b.get("L")),
                     "d_m": parse_length(b.get("d")), "material": mat.name, "E": mat.E,
                     "rho": mat.rho, "nu": mat.nu, "bc": bc})
    rec = {k: recording[k] for k in RECORDING_KEYS if k in recording}
    extra = {k: v for k, v in recording.items() if k not in RECORDING_KEYS}
    if extra:
        rec["extra"] = extra
    return {"sources": sources, "recording": rec, "bars": norm}


def bars_from_meta(meta: dict) -> list[Bar]:
    """Bars with a known diameter (needed for any theory)."""
    out = []
    for b in meta["bars"]:
        if b["d_m"] is None:
            continue
        mat = Material(b["material"], b["E"], b["rho"], b["nu"])
        out.append(Bar(L=b["L_m"], d=b["d_m"], material=mat, bc=b["bc"], name=b["name"]))
    return out


def meta_for_json(meta: dict) -> dict:
    bars = [{"name": b["name"], "L_mm": None if b["L_m"] is None else round(b["L_m"] * 1e3, 3),
             "d_mm": None if b["d_m"] is None else round(b["d_m"] * 1e3, 3), "material": b["material"],
             "E_gpa": round(b["E"] / 1e9, 2), "rho_kg_m3": round(b["rho"], 1), "nu": round(b["nu"], 3),
             "bc": b["bc"]} for b in meta["bars"]]
    return {"sources": meta["sources"], "recording": meta["recording"], "bars": bars}
