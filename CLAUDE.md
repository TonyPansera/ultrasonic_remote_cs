# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

barlab measures the ringing frequency (and Q) of the aluminium bars of a mechanical ultrasonic
remote control (Zenith Space Command style) from bat-detector WAV recordings. Modes are expected
around 30-50 kHz. The bars are solid cylinders, d = 16 mm; their length is not known yet.

A metal-on-metal impact click lasts tens of µs, so it is broadband: that is why a raw spectrum
looks "everything covered". The bar's modes are the narrow lines that keep ringing after the
click. barlab therefore:
- analyses each hit's ringdown (after a 1 ms attack) against a quiet noise window;
- fits each mode's decay (Q = π f τ);
- compares the result with bar theory: longitudinal with the Rayleigh-Love correction, flexural
  Euler-Bernoulli plus Timoshenko (needed for stubby bars, L/d < 10), and torsional.

Record at fs >= 192 kHz. Below 96 kHz a file cannot answer the question, because the modes are
gone or aliased.

## Commands

```bash
uv sync                                             # create .venv (Python 3.13 from .python-version)
uv run pytest -q                                    # full suite, ~40 s; must pass
uv run pytest tests/test_decay.py -q                # one file
uv run pytest -q -k click_is_never_a_mode           # one test by name
uv run barlab analyze "data/raw/*.wav"              # or files; --L/--d/--bc, --set KEY=VALUE, --no-plots
uv run barlab analyze x.wav --results <scratch-dir> # trial run with its own index; results/ untouched
uv run barlab pending                               # raw files not yet in the index
uv run barlab table <stem> --md                     # peaks table of one result
uv run barlab compare <stem> [<stem> ...] --md      # drift vs earlier recordings / batch table
uv run barlab theory --L 66.5mm --d 16mm            # expected modes below Nyquist
uv run barlab theory --d 16mm --implied-from 38000  # bar length each mode family would need
uv run barlab demo                                  # regenerate data/synthetic/ and its sidecars
```

No linter or formatter is configured.

Most tests share session fixtures in `tests/conftest.py`. They write the demo files into a
temporary project and run each one through the full pipeline once, without plots, so even a single
test costs about 3 s of setup. Tests never touch the real `data/` or `results/`.

## Architecture

`barlab/pipeline.py::analyze_file` runs one file end to end. The other modules are its stages:

1. **`io.py`**
   - Loads at the native rate; nothing is ever resampled.
   - Checks the recording (sample rate, clipping, DC, bit depth, level) and reports
     `{severity, code, message}` warnings.
   - Reads GUANO metadata, leaving out the location keys.
2. **`segment.py`**
   - Detects hits from the rise of a high-passed envelope over its trailing level.
   - Gives each hit an attack window (excluded from analysis) and a ringdown window. The ringdown
     ends where the narrowband excess over the noise spectrum falls into the noise, at the next
     hit, or at a cap.
   - Takes noise windows from quiet stretches, preferring the pretrigger.
   - Segments every channel; the pipeline analyses the one with the clearest hits, then restarts
     clipped ringdowns after the last clipped sample.
3. **`spectra.py`**
   - Computes Welch PSDs of the ringdowns and the noise windows on one grid.
   - Keeps a peak only if it has enough prominence and height above the median-filtered noise
     floor. Hann sidelobes are suppressed rather than reported as peaks.
4. **`decay.py`**
   - For each peak and hit, a band-pass plus Hilbert envelope gives a weighted log-power fit
     (τ, Q) and a phase-slope frequency.
   - Aggregates are outlier-cleaned medians with spread and standard error.
   - When fewer than 3 hits fit, a hit-averaged "stack" fit stands in.
   - A decay too slow to measure gives a Q lower bound; no decay at all flags the line `steady`.
5. **`meta.py` and `theory.py`**
   - Build the bars from the sidecars and CLI flags.
   - With L known, they produce the mode table below Nyquist; otherwise, the bar length each mode
     hypothesis would need.
6. **`classify.py`**
   - Gives each peak one class. The first match wins:
     1. `possible_alias` (fs < 96 kHz or near Nyquist)
     2. `noise_or_environment` (no excess over the noise window)
     3. `unresolved` (steady)
     4. `broadband_resonance` (Q < 100)
     5. `bar_mode_candidate` (high Q, consistent over at least 2 hits)
     6. `unresolved` (with reasons)
   - `summarize` builds the file-level verdict.
7. **Output**
   - `schema.py` writes `results/<stem>/result.json`.
   - `store.py` upserts `results/index.csv`.
   - `plots.py` draws five PNGs.

Cross-cutting:
- **`params.py`**: every threshold lives in the frozen `Params` dataclass, grouped by module.
  - All of it is written into each result.json and can be overridden with
    `analyze --set KEY=VALUE`.
  - Changing a default changes results, so it needs a test like any DSP change.
- **`paths.py`**: the project root is the nearest ancestor whose `pyproject.toml` declares
  `name = "barlab"` (or `--root`).
  - Commands run inside an eval sandbox therefore act on that sandbox.
  - `Project.ensure_not_raw()` guards every write.
  - If a stem's result directory already holds a different file (another hash), the new result
    goes to `results/<stem>_<sha8>/`.
- **`store.py`**: upserting a file replaces all of its index rows, keyed by SHA-256.
  - A file without peaks gets a sentinel row (`peak_idx = -1`), so it stops showing as pending.
  - Writes are locked (fcntl) and atomic.
  - The `source` column (raw, synthetic or other) keeps `compare` from mixing real and synthetic
    recordings.
  - `table`, `compare` and `pending` never re-run DSP; they read result.json and the index.
- **`synth.py`**: the ground truth shared by the tests and `barlab demo`.
  - Each signal has modes at 38 kHz (Q 500) and 76 kHz (Q 800), a 15 kHz Q 60 resonance, a
    continuous 30 kHz line and a click, at SNR 40, 20 and 10 dB.
  - One extra file is synthesised directly at 48 kHz, so it really aliases.
  - The demo sidecar L is derived from the injected 38 kHz, so theory matches on synthetic files
    are circular.

## Data and results

- **`data/raw/`**: original recordings. **Read-only**: never write, move or rename. A deny rule in
  `.claude/settings.json` and the file permissions also block it. The folder is gitignored: the
  GUANO metadata in the WAVs holds GPS coordinates, which barlab never copies into results.
- **`data/synthetic/`**: `barlab demo` output, plus `*.truth.json` ground truth.
- **`data/meta/`**: recording metadata, merged in three layers, each overriding the one before:
  1. `_defaults.toml` (d = 16 mm, aluminium, free-free);
  2. per-recording `<stem>.toml` sidecars (bar L, support, mic, temperature);
  3. CLI flags.

  Write only what is known. With L absent, barlab reports implied lengths instead of guessing.
- **`docs/result_schema.md`**: every `result.json` field.
- **`results/`**:
  - `results/<stem>/`: result.json and PNGs;
  - `results/index.csv`: one row per file SHA-256 and peak;
  - `results/reports/`: written by `/analyze-bars`.

## Rules

- `data/raw/` is read-only.
- Never resample. No librosa, no mel scales. Frequency axes are linear up to Nyquist.
- DSP lives only in `barlab/`, and every DSP change comes with a test in `tests/`. Don't compute
  spectra ad hoc in the chat.
- When `result.json` changes, update `docs/result_schema.md`; `tests/test_schema_doc.py` checks
  it. Bump `SCHEMA_VERSION` in `schema.py` if the change is incompatible.
- Keep measured values (frequency, Q, spreads) apart from inferences (class, mode family,
  theory match).

## Analysing recordings

Use `/analyze-bars [files or glob]`.
- With no argument it takes the raw recordings that have no index row yet.
- It runs barlab, checks the numbers against the plots, and writes
  `results/reports/YYYY-MM-DD_<stem>.md`, plus a batch summary when there are several files.
- Mention bar length, support, mic, distance and temperature in the prompt; the skill records
  them in `data/meta/<stem>.toml`.

Skill evals live in `.claude/skills/analyze-bars/evals/`:
- `evals.json`: the eval cases;
- `setup_sandboxes.py`: builds one project copy per run under
  `evals/analyze-bars-workspace/iteration-N/`;
- `grade.py`: grades the runs.

Only each iteration's `benchmark.*` and `review.html` are committed. The sandboxes are
gitignored.
