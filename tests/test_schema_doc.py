import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FREE_FORM = {"device", "extra"}  # free-form maps (GUANO fields, unknown sidecar keys)


def _collect(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            if k not in FREE_FORM:
                _collect(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, acc)
    return acc


def _documented():
    doc = (ROOT / "docs" / "result_schema.md").read_text()
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", doc))


def test_every_result_key_is_documented(analyzed, demo_project):
    from barlab.pipeline import analyze_file

    keys = set()
    for res, _ in analyzed.values():
        _collect(res, keys)
    with_plots = analyze_file(demo_project.synthetic / "synth_fs192k_snr40.wav", demo_project, make_plots=True)
    _collect(with_plots, keys)
    missing = sorted(keys - _documented())
    assert not missing, f"result.json keys missing from docs/result_schema.md: {missing}"


def test_schema_version_and_compactness(analyzed):
    for stem, (res, _) in analyzed.items():
        assert res["schema_version"] == 1
        assert len(json.dumps(res)) < 30_000, stem
