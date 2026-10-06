"""barlab command line.

  barlab analyze <files|globs>    analyse recordings -> results/<stem>/ + results/index.csv
  barlab theory --L .. --d ..     expected bar modes; --implied-from F: bar lengths implied by F
  barlab demo                     write synthetic test recordings to data/synthetic/
  barlab pending                  raw recordings without an index row (by content hash)
  barlab table <stem>             peaks table of one result (--md for markdown)
  barlab compare <stem>           this file's bar-mode candidates vs earlier recordings (drift)
  barlab compare <stem> <stem>..  batch comparison table

Global: --root DIR (project root; default: found from the current directory).
"""

from __future__ import annotations

import argparse
import glob
import math
import sys
from dataclasses import fields
from pathlib import Path

from barlab import __version__
from barlab.io import max_severity, sha256_file
from barlab.meta import parse_length
from barlab.params import Params
from barlab.paths import Project, RawWriteError
from barlab.schema import load_result
from barlab.store import AUDIO_SUFFIXES, pending, read_index

DASH = "—"


# ------------------------------------------------------------------ helpers
def _expand(inputs: list[str], root: Path) -> list[Path]:
    out, seen = [], set()
    for s in inputs:
        if any(ch in s for ch in "*?["):
            hits = sorted(glob.glob(s)) or sorted(glob.glob(str(root / s)))
            paths = [Path(h) for h in hits]
            if not paths:
                print(f"warning: no file matches {s!r}", file=sys.stderr)
        else:
            p = Path(s)
            paths = [p if p.exists() or not (root / s).exists() else root / s]
        for p in paths:
            key = p.resolve()
            if key not in seen:
                seen.add(key)
                out.append(p)
    return out


def _params(args) -> Params:
    kinds = {f.name: f.type for f in fields(Params)}
    over = {}
    for item in args.set or []:
        key, _, val = item.partition("=")
        key = key.strip().replace("-", "_")
        if key not in kinds:
            raise ValueError(f"unknown parameter {key!r}; see barlab/params.py")
        over[key] = int(val) if kinds[key] in ("int", int) else float(val)
    return Params().with_overrides(**over)


def _table(header: list[str], rows: list[list], md: bool) -> str:
    cells = [[DASH if c is None or c == "" else str(c) for c in r] for r in rows]
    if md:
        lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in cells]
        return "\n".join(lines)
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(header)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    return "\n".join([fmt.format(*header), fmt.format(*("-" * w for w in widths))] + [fmt.format(*r) for r in cells])


def _f(v, nd=1):
    return None if v is None else f"{v:,.{nd}f}".replace(",", " ")


def find_result(project: Project, target: str) -> Path:
    p = Path(target)
    if p.suffix == ".json" and p.is_file():
        return p
    if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES:
        sha = sha256_file(p)
        for rj in sorted(project.results.glob("*/result.json")):
            if load_result(rj)["file"]["sha256"] == sha:
                return rj
        raise FileNotFoundError(f"{target} has not been analysed yet (run barlab analyze)")
    direct = project.results / target / "result.json"
    if direct.is_file():
        return direct
    alts = sorted(project.results.glob(f"{target}_*/result.json"))
    if len(alts) == 1:
        return alts[0]
    raise FileNotFoundError(f"no result for {target!r} under {project.results}")


def _theory_cell(p: dict) -> str | None:
    th = p.get("theory") or {}
    nm = th.get("nearest")
    if nm:
        flag = "" if nm["unambiguous"] else ", ambiguous"
        return f"{nm['family']} n={nm['n']} ({nm['rel_err']:+.2%}{flag})"
    imp = th.get("implied_L_mm")
    if imp:
        lon = next((h for h in imp if h["family"] == "longitudinal" and h["n"] == 1), None)
        if lon and lon["L_mm"]:
            return f"L unknown (long. n=1 needs L={lon['L_mm']:.1f} mm)"
    return None


# ------------------------------------------------------------------ commands
def cmd_analyze(args, project: Project) -> int:
    from barlab.pipeline import analyze_file

    project.ensure_not_raw(project.results)
    params = _params(args)
    meta_cli = {"L": args.L, "d": args.d, "bc": args.bc, "material": args.material,
                "E": args.E, "rho": args.rho, "nu": args.nu}
    files = _expand(args.inputs, project.root)
    rc = 0 if files else 1
    for f in files:
        if not f.is_file():
            print(f"error: not a file: {f}", file=sys.stderr)
            rc = 1
            continue
        res = analyze_file(f, project, params, meta_cli, make_plots=not args.no_plots)
        s = res["summary"]
        n_c = s["n_candidates"]
        ring = (f"ringing {_f(s['ringing_hz'])} ± {_f(s['ringing_std_hz'], 2) or '?'} Hz, Q {s['q']}"
                if s["determinable"] else f"NOT DETERMINABLE: {s['reason']}")
        print(f"{res['file']['stem']}: fs={res['file']['fs_hz']} Hz, {res['segmentation']['n_hits']} hits, "
              f"{len(res['peaks'])} peaks ({n_c} bar_mode_candidate); {ring} -> {res['file']['result_dir']}/result.json")
        for w in res["warnings"]:
            if w["severity"] != "INFO":
                print(f"   {w['severity']} {w['code']}: {w['message']}")
    return rc


def _material_from_args(args):
    from barlab.theory import MATERIALS, Material

    base = MATERIALS.get((args.material or "aluminium").lower())
    if base is None:
        raise ValueError(f"unknown material {args.material!r}; known: {sorted(MATERIALS)}")
    return Material(base.name, args.E or base.E, args.rho or base.rho, args.nu or base.nu)


def cmd_theory(args, project: Project) -> int:
    from barlab.theory import Bar, implied_lengths, mode_table, validity

    if not args.d:
        raise ValueError("--d (bar diameter, e.g. 16mm) is required")
    d = parse_length(args.d)
    mat = _material_from_args(args)
    print(f"material {mat.name}: E={mat.E / 1e9:.1f} GPa, rho={mat.rho:.0f} kg/m3, nu={mat.nu:.3f}; "
          f"c={mat.c:.0f} m/s, c_T={mat.c_t:.0f} m/s; d={d * 1e3:.2f} mm; bc={args.bc}")
    if args.implied_from:
        rows = []
        for f in args.implied_from:
            for h in implied_lengths(f, d, mat, args.bc):
                rows.append([_f(f), h["family"], h["n"], h["model"], _f(h["L_mm"], 2), _f(h["L_simple_mm"], 2)])
        print(_table(["f (Hz)", "family", "n", "model", "L (mm)", "L simple formula (mm)"], rows, args.md))
        print("simple formula: n*c/2L (longitudinal), Euler-Bernoulli (flexural). Flexural lengths with L/d < 10 "
              "rely on Timoshenko; compare with your measured bar length.")
        return 0
    if not args.L:
        raise ValueError("give --L (once per bar) or --implied-from FREQ")
    bars = [Bar(L=parse_length(L), d=d, material=mat, bc=args.bc, name=f"bar{i + 1}" if len(args.L) > 1 else "bar")
            for i, L in enumerate(args.L)]
    fmax = args.fmax or (args.fs / 2 if args.fs else 192_000.0)
    for b in bars:
        for v in validity(b):
            print(f"note ({b.name}, L={b.L * 1e3:.2f} mm): {v}")
    rows = [[m["bar"], m["family"], m["n"], m["model"], _f(m["f_hz"]), _f(m["f_simple_hz"]), m["note"]]
            for m in mode_table(bars, fmax)]
    print(_table(["bar", "family", "n", "model", "f (Hz)", "simple formula (Hz)", "note"], rows, args.md))
    print(f"{len(rows)} modes below {fmax:.0f} Hz. simple formula = n*c/2L (longitudinal) or Euler-Bernoulli (flexural).")
    return 0


def cmd_demo(args, project: Project) -> int:
    from barlab.synth import write_demo

    out = Path(args.out) if args.out else project.synthetic
    meta = None if args.no_meta else (Path(args.meta) if args.meta else project.meta)
    for p in write_demo(out, meta, guard=project.ensure_not_raw):
        print(project.rel(p))
    return 0


def cmd_pending(args, project: Project) -> int:
    for p in pending(project):
        print(project.rel(p))
    return 0


def peaks_table(res: dict, md: bool) -> str:
    rows = []
    for p in res["peaks"]:
        d = p["decay"]
        q = None
        if d["q"] is not None:
            q = f"{d['q']:g}" + (f" ± {d['q_std']:g}" if d.get("q_std") is not None else "")
            q += " (stack)" if d.get("q_source") == "stack" else ""
        rows.append([p["i"], _f(p["f_hz"]), _f(p["f_std_hz"], 2), q, d["tau_ms"], p["snr_db"], p["above_floor_db"],
                     f"{d['n_fitted']}/{d['n_tried']}", p["class"], _theory_cell(p)])
    return _table(["#", "f (Hz)", "± σ hits (Hz)", "Q", "τ (ms)", "SNR (dB)", "above floor (dB)",
                   "hits fitted", "class", "nearest theory"], rows, md)


def cmd_table(args, project: Project) -> int:
    res = load_result(find_result(project, args.target))
    print(peaks_table(res, args.md))
    return 0


def _history(res: dict, project: Project, tol: float, md: bool) -> str:
    me = res["file"]
    cands = [p for p in res["peaks"] if p["class"] == "bar_mode_candidate"]
    if not cands:
        return f"{me['stem']}: no bar-mode candidates, nothing to compare."
    others = [r for r in read_index(project.index) if r["sha256"] != me["sha256"]
              and r["source"] == me["source"] and r["class"] == "bar_mode_candidate" and r["f_hz"]]
    rows = []
    for p in cands:
        f, sem = p["f_hz"], p.get("f_sem_hz") or 0.0
        for r in others:
            fo = float(r["f_hz"])
            if abs(fo / f - 1) > tol:
                continue
            if r.get("f_sem_hz"):  # same standard error on both sides of the comparison
                sem_o = float(r["f_sem_hz"])
            else:  # index rows written before f_sem_hz existed
                sem_o = float(r["f_std_hz"]) / math.sqrt(max(int(r["n_fitted"] or 1), 1)) if r["f_std_hz"] else 0.0
            delta = f - fo
            sig = abs(delta) / math.hypot(sem, sem_o) if (sem or sem_o) else float("inf")
            q_ratio = (p["decay"]["q"] / float(r["q"])) if (p["decay"]["q"] and r["q"]) else None
            rows.append([_f(f), r["stem"], r["recorded_at"] or r["analyzed_at"][:10], _f(fo), _f(delta, 2),
                         _f(delta / fo * 1e6, 0), "inf" if math.isinf(sig) else f"{sig:.1f}",
                         None if q_ratio is None else f"{q_ratio:.2f}"])
    if not rows:
        return (f"{me['stem']}: no other {me['source']} recordings with a bar-mode candidate within "
                f"±{tol:.1%} of {', '.join(_f(p['f_hz']) for p in cands)} Hz.")
    head = ["this f (Hz)", "other recording", "recorded", "other f (Hz)", "Δf = this − other (Hz)", "Δf (ppm)",
            "significance (Δf in σ)", "Q this/other"]
    return _table(head, rows, md)


def _batch(results: list[dict], tol: float, md: bool) -> str:
    rows = []
    for r in results:
        s = r["summary"]
        verdict = "determinable" if s["determinable"] else f"not determinable ({s['reason'].split(':')[0]})"
        classes = {}
        for p in r["peaks"]:
            classes[p["class"]] = classes.get(p["class"], 0) + 1
        mix = ", ".join(f"{k} {v}" for k, v in sorted(classes.items()))
        ring = None if s["ringing_hz"] is None else f"{_f(s['ringing_hz'])} ± {_f(s['ringing_std_hz'], 2)}"
        rows.append([r["file"]["stem"], f"{r['file']['fs_hz'] / 1e3:g}", r["segmentation"]["n_hits"], verdict,
                     ring, s["q"], s.get("theory_match"), mix, max_severity(r["warnings"])])
    t1 = _table(["file", "fs (kHz)", "hits", "verdict", "ringing f ± σ (Hz)", "Q", "nearest theory",
                 "peak classes", "worst warning"], rows, md)
    clusters: list[list[tuple[str, float]]] = []
    for r in results:
        for p in r["peaks"]:
            if p["class"] != "bar_mode_candidate":
                continue
            for c in clusters:
                ref = sum(v for _, v in c) / len(c)
                if abs(p["f_hz"] / ref - 1) <= tol:
                    c.append((r["file"]["stem"], p["f_hz"]))
                    break
            else:
                clusters.append([(r["file"]["stem"], p["f_hz"])])
    if not clusters:
        return t1 + "\n\nNo bar-mode candidates in any file: no mode matrix."
    stems = [r["file"]["stem"] for r in results]
    rows2 = []
    for c in sorted(clusters, key=lambda c: c[0][1]):
        vals = [v for _, v in c]
        mean = sum(vals) / len(vals)
        per = {s: v for s, v in c}
        spread = (max(vals) - min(vals)) / mean * 1e6 if len(vals) > 1 else None
        rows2.append([_f(mean)] + [_f(per.get(s)) for s in stems] + [None if spread is None else f"{spread:.0f}"])
    t2 = _table(["mode ≈ f (Hz)"] + stems + ["spread (ppm)"], rows2, md)
    return t1 + "\n\nBar-mode candidates across files (same mode = within ±%.1f%%):\n\n" % (tol * 100) + t2


def cmd_compare(args, project: Project) -> int:
    results = [load_result(find_result(project, t)) for t in args.targets]
    if len(results) == 1:
        print(_history(results[0], project, args.tol, args.md))
    else:
        print(_batch(results, args.tol, args.md))
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="barlab", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--root", help="project root (default: found from the current directory)")
    ap.add_argument("--version", action="version", version=f"barlab {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def bar_args(p, theory=False):
        p.add_argument("--L", action="append", help="bar length, e.g. 66.5mm (repeat for several bars)")
        p.add_argument("--d", help="bar diameter, e.g. 16mm")
        p.add_argument("--bc", choices=["free-free", "clamped-free"], default="free-free" if theory else None)
        p.add_argument("--material", help="aluminium | steel | brass (default aluminium)")
        p.add_argument("--E", type=float, help="Young's modulus [Pa]")
        p.add_argument("--rho", type=float, help="density [kg/m^3]")
        p.add_argument("--nu", type=float, help="Poisson's ratio")

    a = sub.add_parser("analyze", help="analyse recordings")
    a.add_argument("inputs", nargs="+", help="WAV files or glob patterns")
    a.add_argument("--results", help="results root (default <root>/results)")
    a.add_argument("--no-plots", action="store_true")
    a.add_argument("--set", action="append", metavar="KEY=VALUE", help="override an analysis parameter")
    bar_args(a)
    a.set_defaults(func=cmd_analyze)

    t = sub.add_parser("theory", help="expected bar modes, or implied bar lengths")
    bar_args(t, theory=True)
    t.add_argument("--fs", type=float, help="sample rate: list modes below fs/2")
    t.add_argument("--fmax", type=float, help="list modes below this frequency [Hz]")
    t.add_argument("--implied-from", type=float, action="append", metavar="F_HZ",
                   help="instead: bar lengths that would put each mode family at this frequency")
    t.add_argument("--md", action="store_true")
    t.set_defaults(func=cmd_theory)

    d = sub.add_parser("demo", help="write synthetic test recordings (never into data/raw)")
    d.add_argument("--out", help="output directory (default data/synthetic)")
    d.add_argument("--meta", help="metadata directory (default data/meta)")
    d.add_argument("--no-meta", action="store_true", help="do not write metadata sidecars")
    d.set_defaults(func=cmd_demo)

    p = sub.add_parser("pending", help="raw recordings not analysed yet")
    p.add_argument("--results", help="results root (default <root>/results)")
    p.set_defaults(func=cmd_pending)

    tb = sub.add_parser("table", help="peaks table of one analysed file")
    tb.add_argument("target", help="stem, WAV path or result.json path")
    tb.add_argument("--results")
    tb.add_argument("--md", action="store_true")
    tb.set_defaults(func=cmd_table)

    c = sub.add_parser("compare", help="compare bar-mode candidates across recordings")
    c.add_argument("targets", nargs="+", help="one stem: history; several: batch table")
    c.add_argument("--results")
    c.add_argument("--tol", type=float, default=0.02, help="relative tolerance for 'same mode' (default 0.02)")
    c.add_argument("--md", action="store_true")
    c.set_defaults(func=cmd_compare)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        project = Project.at(args.root, getattr(args, "results", None))
        return args.func(args, project)
    except RawWriteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
