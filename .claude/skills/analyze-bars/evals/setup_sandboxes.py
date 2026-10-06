"""Create per-run sandbox copies of the project for /analyze-bars evals.

    uv run python .claude/skills/analyze-bars/evals/setup_sandboxes.py <workspace>/iteration-N \
        [--configs with_skill without_skill] [--evals 1 2 3 4]

Layout (what skill-creator's aggregate_benchmark.py and the eval viewer expect):
    iteration-N/eval-<id>-<name>/eval_metadata.json
    iteration-N/eval-<id>-<name>/<config>/eval_metadata.json      (copy, for the viewer)
    iteration-N/eval-<id>-<name>/<config>/run-1/{project/, outputs/, pre_manifest.json}

Each sandbox gets code + synthetic data + metadata, an empty data/raw and results/, and its
own .venv (uv sync, hardlinked from the uv cache). with_skill sandboxes get SKILL.md and
references/ only - never these evals, so runs cannot see the expected answers. The real
data/raw is never read or written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
ROOT = SKILL.parents[2]
COPY = ["pyproject.toml", "uv.lock", ".python-version", "barlab", "docs", "data/synthetic", "data/meta"]
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def manifest(proj: Path) -> dict:
    files = {}
    for p in proj.rglob("*"):
        rel = p.relative_to(proj)
        if p.is_file() and rel.parts[0] != ".venv":
            files[str(rel)] = p.stat().st_mtime_ns
    raw = {p.name: sha256(p) for p in sorted((proj / "data" / "raw").glob("*")) if p.is_file()}
    return {"raw": raw, "files": files,
            "py_files": sorted(f for f in files if f.endswith(".py"))}


def make_sandbox(run_dir: Path, spec: dict, config: str) -> None:
    proj = run_dir / "project"
    if proj.exists():
        shutil.rmtree(proj)
    proj.mkdir(parents=True)
    for item in COPY:
        src, dst = ROOT / item, proj / item
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=IGNORE)
        else:
            shutil.copy2(src, dst)
    (proj / "data" / "raw").mkdir(parents=True)
    (proj / "results" / "reports").mkdir(parents=True)
    if config == "with_skill":
        dst = proj / ".claude" / "skills" / "analyze-bars"
        dst.mkdir(parents=True)
        shutil.copy2(SKILL / "SKILL.md", dst / "SKILL.md")
        shutil.copytree(SKILL / "references", dst / "references")
    env = {**os.environ, "UV_LINK_MODE": "hardlink"}
    subprocess.run(["uv", "sync", "--frozen", "--quiet"], cwd=proj, check=True, env=env)
    if spec["id"] == 4:  # two "new" raw recordings (fresh noise/hits, same bar); rec_a was analysed earlier
        import soundfile as sf

        from barlab.synth import Scenario, make_signal

        raw = proj / "data" / "raw"
        for name, sc in (("rec_a", Scenario(snr_db=30.0, seed=51)), ("rec_b", Scenario(snr_db=25.0, seed=52))):
            x, _ = make_signal(sc)
            sf.write(raw / f"{name}.wav", x, sc.fs, subtype="PCM_16")
        for p in raw.iterdir():
            p.chmod(0o444)
        subprocess.run(["uv", "run", "barlab", "analyze", "data/raw/rec_a.wav", "--no-plots"],
                       cwd=proj, check=True, env=env, stdout=subprocess.DEVNULL)
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "pre_manifest.json").write_text(json.dumps(manifest(proj), indent=1))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("iteration_dir")
    ap.add_argument("--configs", nargs="+", default=["with_skill", "without_skill"])
    ap.add_argument("--evals", nargs="+", type=int)
    args = ap.parse_args()
    it = Path(args.iteration_dir).resolve()
    specs = json.loads((SKILL / "evals" / "evals.json").read_text())["evals"]
    for spec in specs:
        if args.evals and spec["id"] not in args.evals:
            continue
        eval_dir = it / f"eval-{spec['id']}-{spec['name']}"
        meta = {"eval_id": spec["id"], "eval_name": spec["name"], "prompt": spec["prompt"],
                "assertions": spec["expectations"]}
        eval_dir.mkdir(parents=True, exist_ok=True)
        (eval_dir / "eval_metadata.json").write_text(json.dumps(meta, indent=1))
        for config in args.configs:
            cfg_dir = eval_dir / config
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "eval_metadata.json").write_text(json.dumps(meta, indent=1))
            make_sandbox(cfg_dir / "run-1", spec, config)
            print(f"{eval_dir.name}/{config}/run-1/project")


if __name__ == "__main__":
    main()
