"""results/index.csv: one row per (file SHA-256, peak index).

Upserting a file replaces *all* of its rows (no duplicates, no stale peaks from an
earlier run). A file without peaks gets one sentinel row (peak_idx = -1, class = none),
so it is not reported as pending forever. Writes are locked (fcntl) and atomic.
"""

from __future__ import annotations

import csv
import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from barlab.io import max_severity, sha256_file
from barlab.paths import Project

INDEX_COLUMNS = [
    "sha256", "peak_idx", "stem", "path", "source", "recorded_at", "analyzed_at", "fs_hz", "channel",
    "n_hits", "max_warning", "determinable", "f_hz", "f_std_hz", "f_sem_hz", "f_range_rel", "q", "q_std", "tau_ms",
    "snr_db", "above_floor_db", "width_hz", "n_fitted", "class", "flags", "theory_family", "theory_n",
    "theory_f_hz", "theory_rel_err", "barlab_version", "schema_version",
]
AUDIO_SUFFIXES = {".wav", ".wave", ".flac", ".aif", ".aiff"}


def _s(v) -> str:
    return "" if v is None else str(v)


def rows_from_result(result: dict) -> list[dict]:
    f = result["file"]
    common = {
        "sha256": f["sha256"], "stem": f["stem"], "path": f["path"], "source": f.get("source", "other"),
        "recorded_at": _s(f.get("recorded_at")), "analyzed_at": result["analyzed_at"],
        "fs_hz": _s(f["fs_hz"]), "channel": _s(f.get("channel_used")),
        "n_hits": _s(result.get("segmentation", {}).get("n_hits")),
        "max_warning": _s(max_severity(result.get("warnings", []))),
        "determinable": _s(result.get("summary", {}).get("determinable")),
        "barlab_version": _s(result.get("barlab_version")), "schema_version": _s(result.get("schema_version")),
    }
    rows = []
    for p in result.get("peaks", []):
        d = p.get("decay") or {}
        nm = (p.get("theory") or {}).get("nearest") or {}
        rows.append({**common, "peak_idx": str(p["i"]), "f_hz": _s(p["f_hz"]), "f_std_hz": _s(p.get("f_std_hz")),
                     "f_sem_hz": _s(p.get("f_sem_hz")),
                     "f_range_rel": _s(p.get("f_range_rel")), "q": _s(d.get("q")), "q_std": _s(d.get("q_std")),
                     "tau_ms": _s(d.get("tau_ms")), "snr_db": _s(p.get("snr_db")),
                     "above_floor_db": _s(p.get("above_floor_db")), "width_hz": _s(p.get("width_hz")),
                     "n_fitted": _s(d.get("n_fitted")), "class": p["class"], "flags": ";".join(p.get("flags", [])),
                     "theory_family": _s(nm.get("family")), "theory_n": _s(nm.get("n")),
                     "theory_f_hz": _s(nm.get("f_hz")), "theory_rel_err": _s(nm.get("rel_err"))})
    if not rows:
        empty = {k: "" for k in INDEX_COLUMNS}
        rows.append({**empty, **common, "peak_idx": "-1", "class": "none"})
    return [{k: r.get(k, "") for k in INDEX_COLUMNS} for r in rows]


@contextmanager
def _locked(index_path: Path):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path.with_name(index_path.name + ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def read_index(index_path: Path) -> list[dict]:
    index_path = Path(index_path)
    if not index_path.is_file():
        return []
    with open(index_path, newline="") as f:
        return list(csv.DictReader(f))


def upsert(index_path: Path, rows: list[dict]) -> None:
    """Replace every row of the file(s) in `rows` (by sha256), then add `rows`."""
    index_path = Path(index_path)
    shas = {r["sha256"] for r in rows}
    with _locked(index_path):
        keep = [r for r in read_index(index_path) if r.get("sha256") not in shas]
        merged = keep + [{k: r.get(k, "") for k in INDEX_COLUMNS} for r in rows]
        merged.sort(key=lambda r: (r.get("analyzed_at", ""), r.get("stem", ""), int(r.get("peak_idx") or 0)))
        fd, tmp = tempfile.mkstemp(dir=index_path.parent, prefix=".index.", suffix=".csv")
        try:
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
                w.writeheader()
                w.writerows(merged)
            os.replace(tmp, index_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def pending(project: Project) -> list[Path]:
    """Audio files under data/raw/ whose content hash has no row in the index yet."""
    if not project.raw.is_dir():
        return []
    done = {r["sha256"] for r in read_index(project.index)}
    files = sorted(p for p in project.raw.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
    return [p for p in files if sha256_file(p) not in done]
