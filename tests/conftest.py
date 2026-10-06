"""Shared fixtures: synthetic signals with known ground truth and a throwaway
project tree (never the real data/raw)."""

from __future__ import annotations

import json

import pytest

from barlab.params import Params
from barlab.paths import Project
from barlab.synth import DEMO, make_signal, write_demo

STEMS_192K = ["synth_fs192k_snr40", "synth_fs192k_snr20", "synth_fs192k_snr10"]
STEM_48K = "synth_fs48k_snr40"


@pytest.fixture(scope="session")
def params() -> Params:
    return Params()


@pytest.fixture(scope="session")
def signals() -> dict:
    """stem -> (signal, truth) for every demo scenario."""
    return {stem: make_signal(sc) for stem, sc in DEMO.items()}


def make_project(root) -> Project:
    (root / "pyproject.toml").write_text('[project]\nname = "barlab"\n')
    (root / "data" / "raw").mkdir(parents=True, exist_ok=True)
    proj = Project.at(root)
    write_demo(proj.synthetic, proj.meta, guard=proj.ensure_not_raw)
    return proj


@pytest.fixture(scope="session")
def demo_project(tmp_path_factory) -> Project:
    return make_project(tmp_path_factory.mktemp("proj"))


@pytest.fixture(scope="session")
def analyzed(demo_project) -> dict:
    """stem -> (result dict, truth dict), analysed through the full pipeline."""
    from barlab.pipeline import analyze_file

    out = {}
    for stem in DEMO:
        wav = demo_project.synthetic / f"{stem}.wav"
        res = analyze_file(wav, demo_project, make_plots=False)
        truth = json.loads((demo_project.synthetic / f"{stem}.truth.json").read_text())
        out[stem] = (res, truth)
    return out
