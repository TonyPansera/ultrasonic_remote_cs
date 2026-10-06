# Bar ringing analysis (barlab)

Goal: measure the ringing frequency (and Q) of the aluminium bars of a mechanical ultrasonic
remote control (Zenith Space Command style), from bat-detector WAV recordings. Modes are expected
around 30-50 kHz. The bars are solid cylinders, d = 16 mm; their length is not known yet.

Physics in brief: a metal-on-metal impact click lasts tens of µs, so it is broadband. That is
why a raw spectrum looks "everything covered". The bar's modes are the narrow lines that keep
ringing after the click. barlab therefore:
- analyses each hit's ringdown (after a 1 ms attack) against a quiet noise window;
- fits each mode's decay (Q = π f τ);
- compares the result with bar theory: longitudinal with the Rayleigh-Love correction, flexural
  Euler-Bernoulli plus Timoshenko (needed for stubby bars, L/d < 10), and torsional.

Record at fs >= 192 kHz. Below 96 kHz a file cannot answer the question, because the modes are
gone or aliased.

## Layout

- `data/raw/`: original recordings. **Read-only**: never write, move or rename (a deny rule in
  `.claude/settings.json` and the file permissions also block it).
- `data/synthetic/`: test signals from `barlab demo`, plus `*.truth.json` ground truth.
- `data/meta/`: `_defaults.toml` (d = 16 mm, aluminium, free-free) and per-recording
  `<stem>.toml` sidecars (bar L, support, mic, temperature). Write only what is known.
- `barlab/`: the DSP package (io, segment, spectra, decay, theory, classify, plots, store, cli).
- `docs/result_schema.md`: every `result.json` field.
- `tests/`: synthetic ground truth.
- `results/<stem>/`: `result.json` and PNGs.
- `results/index.csv`: one row per (file SHA-256, peak).
- `results/reports/`: written by `/analyze-bars`.

## Commands

```bash
uv run pytest -q                                   # must pass
uv run barlab analyze "data/raw/*.wav"              # or files; --L/--d/--bc, --set KEY=VALUE
uv run barlab pending                               # raw files not yet in the index
uv run barlab table <stem> --md                     # peaks table of one result
uv run barlab compare <stem> [<stem> ...] --md      # drift vs earlier recordings / batch table
uv run barlab theory --L 66.5mm --d 16mm            # expected modes below Nyquist
uv run barlab theory --d 16mm --implied-from 38000  # bar length each mode family would need
uv run barlab demo                                  # regenerate data/synthetic/
```

## Rules

- `data/raw/` is read-only.
- Never resample. No librosa, no mel scales. Frequency axes are linear up to Nyquist.
- DSP lives only in `barlab/`, and every DSP change comes with a test in `tests/`. Don't compute
  spectra ad hoc in the chat.
- When `result.json` changes, update `docs/result_schema.md` (`tests/test_schema_doc.py` checks
  it), and bump `SCHEMA_VERSION` if the change is incompatible.
- Keep measured values (frequency, Q, spreads) apart from inferences (class, mode family,
  theory match).

## Analysing recordings

Use `/analyze-bars [files or glob]`. With no argument it takes the raw recordings that have no
index row yet. It runs barlab, checks the numbers against the plots, and writes
`results/reports/YYYY-MM-DD_<stem>.md`, plus a batch summary when there are several files.
Mention bar length, support, mic, distance and temperature in the prompt; the skill records them
in `data/meta/<stem>.toml`. Skill evals live in `.claude/skills/analyze-bars/evals/`
(`setup_sandboxes.py`, `grade.py`), with workspaces under `evals/`.
