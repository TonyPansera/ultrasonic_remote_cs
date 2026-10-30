"""Turn bar_detector_v3 session logs into one row per strike, and summarise them.

Reads measurements/<date>/*.log (written by tools/session.py), pairs each strike's SPEC capture with
its EVT detect/end lines (by board time), and prints per-session tables of the watched lines: level
(dBFS), level relative to the struck bar's fundamental, drop between the early and late windows, and
frequency. No DSP here: every number comes from the board.

    uv run python esp32/bar_detector_v3/tools/analyze_sessions.py [--date 2026-10-30] [--csv out.csv]
"""

import argparse
import csv
import pathlib
import re
import statistics
import sys

HERE = pathlib.Path(__file__).resolve().parent.parent
KV = re.compile(r"(\w+)=(\S+)")


def kv(line: str) -> dict:
    return {k: v for k, v in KV.findall(line)}


def num(v: str) -> float | str:
    try:
        return float(v)
    except ValueError:
        return v


def parse(path: pathlib.Path) -> tuple[list[dict], list[dict]]:
    """Returns (captures, events). A capture holds its SPEC fields, peaks and lines by name."""
    captures, events = [], []
    cap = None
    for raw in path.read_text(errors="replace").splitlines():
        if raw.startswith("#"):
            continue
        parts = raw.split(None, 1)
        if len(parts) < 2:
            continue
        line = parts[1]
        if line.startswith("SPEC t="):
            cap = {k: num(v) for k, v in kv(line).items()}
            cap["peaks"], cap["lines"] = [], {}
            captures.append(cap)
        elif line.startswith("SPEC_PEAK") and cap is not None:
            cap["peaks"].append({k: num(v) for k, v in kv(line).items()})
        elif line.startswith("SPEC_LINE") and cap is not None:
            d = {k: num(v) for k, v in kv(line).items()}
            cap["lines"][d["name"]] = d
        elif line.startswith("EVT "):
            events.append({k: num(v) for k, v in kv(line).items()})
    return captures, events


def strikes(captures: list[dict], events: list[dict]) -> list[dict]:
    """One row per click-triggered capture, with the detector's decision for the same strike."""
    rows = []
    for c in captures:
        if c.get("forced") == 1:
            continue
        row = {"t": c["t"], "trig_db": c["trig_db"], "ext": c.get("ext", 0), "floor": c["floor_dbfs"]}
        for name, l in c["lines"].items():
            row[f"{name}_f"] = l["f"]
            row[f"{name}_db"] = l["early_dbfs"]
            row[f"{name}_drop"] = l["drop_db"]
        # The detector's event that started within -16..+64 ms of the capture's chunk: the band-onset
        # trigger starts the capture at the first block over the threshold, the event at its first
        # clean one (the click block is not clean), up to a few blocks later.
        det = [e for e in events if e["phase"] == "detect" and -0.016 <= e["t"] - c["t"] <= 0.064]
        end = [e for e in events if e["phase"] == "end" and -0.016 <= e["t"] - c["t"] <= 0.064]
        if det:
            e = det[0]
            row.update(band=e["band"], f_a=e["f_a"], f_b=e["f_b"], snr_a=e["snr_a"], snr_b=e["snr_b"],
                       margin=e["margin_db"], event_t=e["t"])
        elif end:
            row.update(band=f"none({'conf' if end[0].get('confirmed') == 1 else 'unconf'})")
        else:
            row.update(band="none")
        rows.append(row)
    return rows


def med(xs: list[float]) -> str:
    xs = [x for x in xs if isinstance(x, float)]
    if not xs:
        return "-"
    return f"{statistics.median(xs):.1f} [{min(xs):.1f}..{max(xs):.1f}]"


def summarise(label: str, rows: list[dict], names: list[str]) -> None:
    det = [r for r in rows if r["band"] in ("A", "B", "BOTH")]
    bands = {b: sum(r["band"] == b for r in rows) for b in ("A", "B", "BOTH")}
    print(f"\n## {label}: {len(rows)} captures, detect A={bands['A']} B={bands['B']} BOTH={bands['BOTH']}, "
          f"no detect={len(rows) - len(det)} (ext-triggered {sum(r['ext'] == 1 for r in rows)})")
    if not rows:
        return
    ref = "A0" if bands["A"] >= bands["B"] else "B0"
    print(f"line   level dBFS (median [min..max])   re {ref} dB              drop dB            f Hz")
    rows = det or rows  # the line statistics of the strikes, not of knocks and handling noise
    for n in names:
        lv = [r.get(f"{n}_db") for r in rows]
        rel = [r[f"{n}_db"] - r[f"{ref}_db"] for r in rows if f"{n}_db" in r and f"{ref}_db" in r]
        dr = [r.get(f"{n}_drop") for r in rows]
        fr = [r.get(f"{n}_f") for r in rows]
        print(f"{n:6} {med(lv):32} {med(rel):24} {med(dr):18} {med(fr)}")
    margins = [r["margin"] for r in det]
    if margins:
        print(f"legacy margin dB: {med(margins)}; f_a {med([r['f_a'] for r in det if r['f_a'] > 0])}; "
              f"f_b {med([r['f_b'] for r in det if r['f_b'] > 0])}")


def peak_census(captures: list[dict], watched_hz: list[float], tol: float = 150.0) -> list[tuple[float, int, float]]:
    """Clusters the SPEC_PEAK frequencies of click captures that no watched line covers."""
    fs = []
    for c in captures:
        if c.get("forced") == 1:
            continue
        top = max((l["early_dbfs"] for l in c["lines"].values()), default=-200)
        for p in c["peaks"]:
            if all(abs(p["f"] - w) > 300 for w in watched_hz):
                fs.append((p["f"], p["early_dbfs"] - top))
    fs.sort()
    clusters: list[list[tuple[float, float]]] = []
    for f, rel in fs:
        if clusters and f - clusters[-1][-1][0] <= tol:
            clusters[-1].append((f, rel))
        else:
            clusters.append([(f, rel)])
    return [(statistics.median(x[0] for x in c), len(c), statistics.median(x[1] for x in c)) for c in clusters]


def strike_table(rows: list[dict], all_captures: bool = False) -> None:
    """One line per strike: the detector's decision and the bars' watched lines (level @ f, drop)."""
    def cell(r: dict, n: str) -> str:
        if f"{n}_db" not in r:
            return f"{n}=   ?  "
        return f"{n}={r[f'{n}_db']:6.1f} d{r[f'{n}_drop']:5.1f}"

    for r in rows:
        if not all_captures and r["band"] not in ("A", "B", "BOTH"):
            continue
        m = r.get("margin", float("nan"))
        print(f"  t={r['t']:7.3f} {r['band']:5} m={m:5.1f} snr={r.get('snr_a', 0):4.1f}/{r.get('snr_b', 0):4.1f} "
              f"fa={r.get('f_a', 0):7.1f} fb={r.get('f_b', 0):7.1f} | {cell(r, 'A0')} {cell(r, 'A1')} | "
              f"{cell(r, 'B0')} {cell(r, 'B1')} | floor={r['floor']:6.1f}")


# Which bar was struck, per session (the user's protocol on 2026-10-30). S5 followed a given sequence;
# its strike 12 was detected twice, 0.4 s apart (a bounce): the extra one is dropped.
TRUTH = {"S1b_A_damped": "A", "S2_B_damped": "B", "S3_A_free": "A", "S4_B_free": "B"}
S5_SEQUENCE = "38 47 47 38 47 38 38 47 38 47 47 38 38 47 38 47 38 38 47 47".split()
S5_BOUNCE_T = 40.672


def labelled(date: str) -> list[dict]:
    """The detected strikes of the single-bar sessions, each with its true bar in r["truth"]."""
    rows = []
    for name in [*TRUTH, "S5_mixed"]:
        path = HERE / "measurements" / date / f"{name}.log"
        if not path.exists():
            continue
        captures, events = parse(path)
        det = [r for r in strikes(captures, events) if r["band"] in ("A", "B", "BOTH") and "A0_db" in r]
        if name == "S5_mixed":
            det = [r for r in det if abs(r["t"] - S5_BOUNCE_T) > 0.01]
            if len(det) != len(S5_SEQUENCE):
                raise SystemExit(f"S5: {len(det)} strikes for a {len(S5_SEQUENCE)}-strike sequence")
            for r, s in zip(det, S5_SEQUENCE):
                r["truth"] = "A" if s == "38" else "B"
        else:
            for r in det:
                r["truth"] = TRUTH[name]
        for r in det:
            r["session"] = name
        rows += det
    return rows


def background(date: str) -> dict[str, float]:
    """Median early level of each watched line in S0's forced captures: the quiet-room level."""
    captures, _ = parse(HERE / "measurements" / date / "S0_background.log")
    forced = [c for c in captures if c.get("forced") == 1]
    return {n: statistics.median(c["lines"][n]["early_dbfs"] for c in forced) for n in forced[0]["lines"]}


def export_vectors(date: str, out: pathlib.Path) -> None:
    """Writes the labelled strikes as a C++ table for test/test_decision.

    These logs predate the "before" window, so each line's before level is S0's quiet-room level.
    """
    rows = labelled(date)
    bg = background(date)
    lines = [
        f"// Generated by tools/analyze_sessions.py --export-vectors from measurements/{date}/: every detected",
        "// strike of the single-bar sessions (S1b, S2, S3, S4, S5), with the bar that was struck. Levels in dBFS;",
        "// the logs predate the before window, so `before` is S0's quiet-room level of each line.",
        "#pragma once",
        "",
        "struct LineVec {",
        "  float f, early, late, before;",
        "};",
        "struct StrikeVec {",
        "  const char *session;",
        "  float t;        // the capture's trigger chunk, s",
        "  float eventT;   // the detector's event start, s",
        "  char truth;     // 'A' or 'B'",
        "  char detector;  // the ToneDetector's band: 'A', 'B' or 'X' (BOTH)",
        "  float floor;",
        "  LineVec a0, a1, b0, b1;",
        "};",
        "",
        "const StrikeVec kStrikes[] = {",
    ]

    def lv(r: dict, n: str) -> str:
        return (f"{{{r[f'{n}_f']:.1f}f, {r[f'{n}_db']:.1f}f, {r[f'{n}_db'] - r[f'{n}_drop']:.1f}f, "
                f"{bg[n]:.1f}f}}")

    for r in rows:
        det = {"A": "A", "B": "B", "BOTH": "X"}[r["band"]]
        lines.append(f"    {{\"{r['session']}\", {r['t']:.3f}f, {r['event_t']:.3f}f, '{r['truth']}', '{det}', {r['floor']:.1f}f, "
                     f"{lv(r, 'A0')}, {lv(r, 'A1')}, {lv(r, 'B0')}, {lv(r, 'B1')}}},")
    lines += ["};", "const int kStrikeCount = sizeof(kStrikes) / sizeof(kStrikes[0]);", ""]
    out.write_text("\n".join(lines))
    print(f"{len(rows)} strikes written to {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default="2026-10-30")
    ap.add_argument("--only", help="regex on the session label")
    ap.add_argument("--csv", help="write every strike row here")
    ap.add_argument("--peaks", action="store_true", help="list unwatched peak clusters per session")
    ap.add_argument("--strikes", action="store_true", help="one line per detected strike")
    ap.add_argument("--export-vectors", type=pathlib.Path, help="write the labelled strikes as a C++ header")
    args = ap.parse_args()
    if args.export_vectors:
        export_vectors(args.date, args.export_vectors)
        return 0
    logs = sorted((HERE / "measurements" / args.date).glob("S*.log"))
    if args.only:
        logs = [p for p in logs if re.search(args.only, p.stem)]
    all_rows = []
    for path in logs:
        captures, events = parse(path)
        rows = strikes(captures, events)
        names = list(captures[0]["lines"]) if captures else []
        summarise(path.stem, rows, names)
        if args.strikes:
            strike_table(rows)
        if args.peaks:
            watched = [l["target"] for l in captures[0]["lines"].values()] if captures else []
            n = sum(c.get("forced") != 1 for c in captures)
            for f, count, rel in peak_census(captures, watched):
                if count >= max(2, n // 3):
                    print(f"    peak {f:9.1f} Hz in {count}/{n} captures, {rel:+.1f} dB re strongest watched line")
        for r in rows:
            r["session"] = path.stem
        all_rows += rows
    if args.csv:
        keys = sorted({k for r in all_rows for k in r})
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, keys)
            w.writeheader()
            w.writerows(all_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
