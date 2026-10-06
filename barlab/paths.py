"""Project layout and the data/raw write guard.

data/raw/ is read-only input: nothing in barlab ever writes below it. Every writer
calls `ensure_not_raw` on its target first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class RawWriteError(RuntimeError):
    """Raised when something tries to write below data/raw/."""


def find_root(start: Path | None = None) -> Path:
    """Walk up from `start` (default: cwd) to the directory whose pyproject.toml
    declares the barlab project. Falls back to `start` itself."""
    here = Path(start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        pp = d / "pyproject.toml"
        if pp.is_file() and 'name = "barlab"' in pp.read_text(errors="ignore"):
            return d
    return here


def is_within(path: Path, parent: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Project:
    root: Path
    results: Path

    @classmethod
    def at(cls, root: Path | None = None, results: Path | None = None) -> "Project":
        r = Path(root).resolve() if root else find_root()
        res = Path(results).resolve() if results else r / "results"
        return cls(root=r, results=res)

    @property
    def raw(self) -> Path:
        return self.root / "data" / "raw"

    @property
    def synthetic(self) -> Path:
        return self.root / "data" / "synthetic"

    @property
    def meta(self) -> Path:
        return self.root / "data" / "meta"

    @property
    def index(self) -> Path:
        return self.results / "index.csv"

    @property
    def reports(self) -> Path:
        return self.results / "reports"

    def ensure_not_raw(self, target: Path) -> Path:
        if is_within(target, self.raw):
            raise RawWriteError(f"refusing to write below data/raw/: {target}")
        return Path(target)

    def source_of(self, path: Path) -> str:
        if is_within(path, self.raw):
            return "raw"
        if is_within(path, self.synthetic):
            return "synthetic"
        return "other"

    def rel(self, path: Path) -> str:
        """Path relative to the project root when possible (for compact JSON)."""
        p = Path(path).resolve()
        try:
            return str(p.relative_to(self.root))
        except ValueError:
            return str(p)
