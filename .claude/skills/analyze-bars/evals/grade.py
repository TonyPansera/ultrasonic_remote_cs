"""Grade /analyze-bars eval runs deterministically and collect their outputs.

    uv run python .claude/skills/analyze-bars/evals/grade.py <workspace>/iteration-N [--date YYYY-MM-DD]

For every run-1/ under the iteration: copies the reports (and eval 4's sidecar) into
run-1/outputs/ for the viewer, checks each expectation listed in evals.json and writes
run-1/grading.json in skill-creator's format ({text, passed, evidence} + summary).
Checks are text heuristics on the written reports plus file-system facts (data/raw hashes,
new .py files, index.csv duplicates); judgement calls are left to the human/inline review.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
SECTIONS = ["verdict", "recording quality", "detected peaks", "interpretation",
            "comparison with previous recordings", "recommendations", "figures"]
SYNTH = ["synth_fs192k_snr40", "synth_fs192k_snr20", "synth_fs192k_snr10", "synth_fs48k_snr40"]
N = r"\d{1,3}(?:[   ,]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
FREQ = re.compile(rf"({N})(?:\s*(?:±|\+/-|\+-)\s*(?:{N}))?\s*(kHz|Hz)\b", re.I)
BARE = re.compile(rf"(?<![\w.])({N})(?![\w.])")


def _num(s: str) -> float:
    return float(re.sub(r"[   ,]", "", s))


def freqs(text: str, bare: bool = False) -> list[float]:
    """Frequencies in Hz: numbers followed by Hz/kHz (also 'X ± Y Hz'); with bare=True also
    unit-less numbers >= 1000 (table cells)."""
    out = [(_num(m.group(1)) * (1e3 if m.group(2).lower() == "khz" else 1.0)) for m in FREQ.finditer(text)]
    if bare:
        out += [v for v in (_num(m.group(1)) for m in BARE.finditer(text)) if v >= 1000]
    return out


def near(values, target, rel) -> bool:
    return any(abs(v / target - 1) <= rel for v in values)


def sections(md: str) -> list[tuple[str, str]]:
    parts = re.split(r"^##\s+(.+?)\s*$", md, flags=re.M)
    return [(parts[i].strip().lower(), parts[i + 1]) for i in range(1, len(parts), 2)]


def section(md: str, name: str) -> str:
    return next((head + "\n" + body for head, body in sections(md) if head.startswith(name)), "")


def sections_ok(md: str) -> tuple[bool, str]:
    heads = [h for h, _ in sections(md)]
    pos = []
    for s in SECTIONS:
        i = next((k for k, h in enumerate(heads) if h.startswith(s)), None)
        if i is None:
            return False, f"missing '{s}'; headings: {heads}"
        pos.append(i)
    return pos == sorted(pos), f"headings: {heads}"


def links_ok(report: Path) -> tuple[bool, str]:
    links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", report.read_text())
    missing = [ln for ln in links if not (report.parent / ln).resolve().is_file()]
    return (bool(links) and not missing), f"{len(links)} image links, missing: {missing}"


def line_with(text: str, target: float, rel: float, words: list[str]) -> tuple[bool, str]:
    for line in re.split(r"\n|(?<=\.)\s", text):
        if near(freqs(line, bare=True), target, rel) and any(w in line.lower() for w in words):
            return True, line.strip()[:200]
    return False, f"no line with ~{target:.0f} Hz and any of {words}"


class Run:
    def __init__(self, run_dir: Path, date: str):
        self.dir, self.proj, self.date = run_dir, run_dir / "project", date
        self.pre = json.loads((run_dir / "pre_manifest.json").read_text())

    def report(self, stem: str) -> Path:
        return self.proj / "results" / "reports" / f"{self.date}_{stem}.md"

    def md(self, stem: str) -> str:
        p = self.report(stem)
        return p.read_text() if p.is_file() else ""

    def raw_unchanged(self):
        raw = self.proj / "data" / "raw"
        now = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(raw.glob("*")) if p.is_file()}
        return now == self.pre["raw"], f"before {len(self.pre['raw'])} files, after {len(now)}; equal={now == self.pre['raw']}"

    def no_adhoc(self):
        new = [str(p.relative_to(self.proj)) for p in self.proj.rglob("*")
               if p.is_file() and p.suffix in (".py", ".ipynb") and ".venv" not in p.parts
               and str(p.relative_to(self.proj)) not in self.pre["py_files"]]
        return not new, f"new script files: {new}"

    def no_dup_index(self):
        idx = self.proj / "results" / "index.csv"
        if not idx.is_file():
            return False, "no index.csv"
        rows = list(csv.DictReader(open(idx)))
        keys = [(r["sha256"], r["peak_idx"]) for r in rows]
        return len(keys) == len(set(keys)), f"{len(rows)} rows, {len(set(keys))} unique keys"

    def collect(self) -> None:
        out = self.dir / "outputs"
        out.mkdir(exist_ok=True)
        for p in sorted((self.proj / "results" / "reports").glob("*.md")):
            shutil.copy2(p, out / p.name)
        side = self.proj / "data" / "meta" / "rec_b.toml"
        if side.is_file():
            shutil.copy2(side, out / "rec_b.toml")


def checks(run: Run, spec: dict) -> list[tuple[str, bool, str]]:
    eid = spec["id"]
    res: list[tuple[str, bool, str]] = []

    def add(text, ok_ev):
        res.append((text, bool(ok_ev[0]), ok_ev[1]))

    if eid in (1, 2, 4):
        stem = {1: "synth_fs192k_snr20", 2: "synth_fs48k_snr40", 4: "rec_b"}[eid]
        md, rp = run.md(stem), run.report(stem)
        verdict = section(md, "verdict")
        for text in spec["expectations"]:
            t = text.lower()
            if t.startswith("report file"):
                add(text, (rp.is_file(), str(rp.relative_to(run.proj))))
            elif t.startswith("report has the 7"):
                add(text, sections_ok(md) if md else (False, "no report"))
            elif t.startswith("all embedded png"):
                add(text, links_ok(rp) if md else (False, "no report"))
            elif t.startswith("peaks table pasted"):
                add(text, ("± σ hits (Hz)" in md, "barlab table header present" if "± σ hits (Hz)" in md else "header missing"))
            elif t.startswith("verdict gives a ringing frequency within 0.2%"):
                add(text, (near(freqs(verdict), 38_000.0, 0.002), f"verdict: {verdict.strip()[:240]!r}"))
            elif t.startswith("verdict gives an uncertainty"):
                add(text, (("±" in verdict or "+/-" in verdict), verdict.strip()[:200]))
            elif t.startswith("report mentions the second mode"):
                add(text, (near(freqs(md, bare=True), 76_000.0, 0.005), "~76 kHz mentioned" if near(freqs(md, bare=True), 76_000.0, 0.005) else "not found"))
            elif t.startswith("interpretation calls the 30 khz"):
                add(text, line_with(section(md, "interpretation"), 30_000.0, 0.005,
                                    ["environment", "noise window", "external", "stationary", "not the bar", "not a bar", "noise"]))
            elif t.startswith("interpretation calls the 15 khz"):
                add(text, line_with(section(md, "interpretation"), 15_000.0, 0.01,
                                    ["broadband", "low-q", "low q", "mount", "low quality", "q ≈ 6", "q 6", "resonance"]))
            elif t.startswith("verdict says not determinable"):
                ok = re.search(r"not\s+determinable", verdict, re.I)
                add(text, (ok, verdict.strip()[:240]))
            elif t.startswith("verdict explains the sample-rate"):
                v = verdict.lower()
                add(text, (any(w in v for w in ("96 khz", "96khz", "nyquist", "alias")), verdict.strip()[:240]))
            elif t.startswith("critical is stated before"):
                i, j = md.lower().find("critical"), md.lower().find("## detected peaks")
                add(text, (0 <= i < j, f"CRITICAL at {i}, Detected peaks at {j}"))
            elif t.startswith("verdict does not present an aliased"):
                bad = re.search(r"ringing frequency\W{0,6}(is|:|=)?\W{0,6}\d", verdict, re.I)
                add(text, (not bad, "no 'ringing frequency: <number>' in verdict" if not bad else bad.group(0)))
            elif t.startswith("recommendations ask for a higher sample rate"):
                rec = section(md, "recommendations").lower()
                add(text, (any(w in rec for w in ("192", "384")), rec.strip()[:200]))
            elif t.startswith("only rec_b was analysed"):
                key = "results/rec_a/result.json"
                rj = run.proj / key
                untouched = rj.is_file() and run.pre["files"].get(key) == rj.stat().st_mtime_ns
                analysed_b = (run.proj / "results/rec_b/result.json").is_file()
                add(text, (untouched and analysed_b, f"rec_a untouched={untouched}, rec_b analysed={analysed_b}"))
            elif t.startswith("data/meta/rec_b.toml"):
                from barlab.meta import parse_length
                import tomllib

                p = run.proj / "data/meta/rec_b.toml"
                ok, ev = False, "missing"
                if p.is_file():
                    try:
                        d = tomllib.loads(p.read_text())
                        bars = d.get("bar", [])
                        ok = any(abs((parse_length(b.get("L")) or 0) - 0.0665) < 2e-4
                                 and abs((parse_length(b.get("d")) or 0.016) - 0.016) < 2e-4 for b in bars)
                        ev = p.read_text()[:300]
                    except Exception as e:  # noqa: BLE001 - report any parse problem as evidence
                        ev = f"unparseable: {e}"
                add(text, (ok, ev))
            elif t.startswith("report identifies the longitudinal"):
                low = md.lower()
                ok = "longitudinal" in low and bool(re.search(r"n\s*=\s*1|fundamental", low))
                add(text, (ok, "found" if ok else "no 'longitudinal' + 'n=1'/'fundamental'"))
            elif t.startswith("data/raw unchanged"):
                add(text, run.raw_unchanged())
            elif t.startswith("no ad-hoc dsp"):
                add(text, run.no_adhoc())
            elif t.startswith("index.csv has no duplicate"):
                add(text, run.no_dup_index())
            else:
                add(text, (False, "no automatic check for this expectation"))
    else:  # eval 3: batch
        summ_p = run.proj / "results" / "reports" / f"{run.date}_batch-summary.md"
        summ = summ_p.read_text() if summ_p.is_file() else ""
        for text in spec["expectations"]:
            t = text.lower()
            if t.startswith("four per-file reports"):
                have = [s for s in SYNTH if run.report(s).is_file()]
                add(text, (len(have) == 4, f"present: {have}"))
            elif t.startswith("every per-file report has the 7"):
                bad = [s for s in SYNTH if not (run.md(s) and sections_ok(run.md(s))[0])]
                add(text, (not bad, f"failing: {bad}"))
            elif t.startswith("batch summary exists"):
                add(text, (summ_p.is_file(), str(summ_p.relative_to(run.proj))))
            elif t.startswith("batch summary has a comparison table"):
                rows = [ln for ln in summ.splitlines() if ln.strip().startswith("|")]
                missing = [s for s in SYNTH if not any(s in r for r in rows)]
                add(text, (bool(rows) and not missing, f"table rows {len(rows)}, stems missing: {missing}"))
            elif t.startswith("batch summary marks the 48"):
                ok = any("synth_fs48k" in ln and re.search(r"not\s+determinable", ln, re.I) for ln in summ.splitlines())
                add(text, (ok, "found" if ok else "no line with synth_fs48k + not determinable"))
            elif t.startswith("batch summary gives ~38"):
                add(text, (near(freqs(summ, bare=True), 38_000.0, 0.002), "~38 kHz present" if near(freqs(summ, bare=True), 38_000.0, 0.002) else "absent"))
            elif t.startswith("48 khz report verdict"):
                v = section(run.md("synth_fs48k_snr40"), "verdict")
                add(text, (re.search(r"not\s+determinable", v, re.I), v.strip()[:200]))
            elif t.startswith("data/raw unchanged"):
                add(text, run.raw_unchanged())
            elif t.startswith("no ad-hoc dsp"):
                add(text, run.no_adhoc())
            elif t.startswith("index.csv has no duplicate"):
                add(text, run.no_dup_index())
            else:
                add(text, (False, "no automatic check for this expectation"))
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("iteration_dir")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    args = ap.parse_args()
    specs = {s["id"]: s for s in json.loads((SKILL / "evals" / "evals.json").read_text())["evals"]}
    for meta_p in sorted(Path(args.iteration_dir).glob("eval-*/eval_metadata.json")):
        spec = specs[json.loads(meta_p.read_text())["eval_id"]]
        for run_dir in sorted(meta_p.parent.glob("*/run-*")):
            if not (run_dir / "pre_manifest.json").is_file():
                continue
            run = Run(run_dir, args.date)
            run.collect()
            exps = [{"text": t, "passed": ok, "evidence": ev} for t, ok, ev in checks(run, spec)]
            n_pass = sum(e["passed"] for e in exps)
            grading = {"expectations": exps,
                       "summary": {"passed": n_pass, "failed": len(exps) - n_pass, "total": len(exps),
                                   "pass_rate": round(n_pass / len(exps), 3) if exps else 0.0}}
            (run_dir / "grading.json").write_text(json.dumps(grading, indent=1, ensure_ascii=False))
            print(f"{run_dir.parent.parent.name}/{run_dir.parent.name}: {n_pass}/{len(exps)}")
            for e in exps:
                if not e["passed"]:
                    print(f"   FAIL {e['text']}: {e['evidence']}")


if __name__ == "__main__":
    main()
