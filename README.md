# barlab: ringing frequencies of struck bars from ultrasonic recordings

This repository measures the ringing frequency (and Q) of the aluminium bars inside a mechanical
ultrasonic remote control (Zenith Space Command style). Two bars strike each other and ring, a
bat-detector microphone records the ultrasound, and barlab turns those WAV files into measured
frequencies, decay rates and an honest verdict. The modes are expected around 30-50 kHz. The bars
are solid cylinders, d = 16 mm, of unknown length.

The repository has two layers:

- **`barlab`** is a deterministic Python package and command-line tool that does all the signal
  processing. The same file always gives the same numbers. Nothing is resampled and no model is
  in the loop.
- **`/analyze-bars`** is a [Claude Code](https://claude.com/claude-code) skill. It runs barlab,
  checks the numbers against the plots and writes one report per recording, keeping measurements
  apart from interpretation.

## Contents

- [Quick start](#quick-start)
- [Analysing a new recording](#analysing-a-new-recording)
- [Reading the results](#reading-the-results)
- [Command reference](#command-reference)
- [How the analysis works](#how-the-analysis-works)
- [Repository map](#repository-map)
- [Development](#development)

## Quick start

**Requirements:**
- [uv](https://docs.astral.sh/uv/).
- Linux, macOS or WSL. The results index is locked with `fcntl`, which native Windows lacks.

uv installs Python 3.13 (from `.python-version`; any 3.11+ works) together with numpy, scipy,
soundfile and matplotlib.

```bash
uv sync                                       # create .venv
uv run pytest -q                              # 91 tests on synthetic ground truth, ~40 s
uv run barlab analyze "data/synthetic/*.wav"  # analyse the synthetic test recordings
uv run barlab table synth_fs192k_snr20 --md   # peaks table of one of them
```

`analyze` does three things:
- prints one summary line per file;
- writes `results/<stem>/` (a `result.json` and five plots);
- updates `results/index.csv`.

The synthetic recordings have a known answer: 38.0 kHz (Q 500) and 76.0 kHz (Q 800). That makes
them the quickest way to see what a good result looks like. Their report is
[`results/reports/2026-10-06_batch-summary.md`](results/reports/2026-10-06_batch-summary.md).

## Analysing a new recording

### 1. Record

These settings make a recording able to answer the question:

- **Sample rate of 192 kHz or more** (384 kHz is ideal). Below 96 kHz a file cannot answer the
  question: the bar modes are filtered out or folded back as aliases, and barlab marks the file
  CRITICAL.
- **Gain** set so the hits peak at about −6 dBFS or below. Clipping destroys the strongest part
  of the ringdown.
- **A quiet lead-in** of at least 0.5 s, which barlab uses as the noise reference.
- **No other ultrasonic sources** in the room: switch-mode power supplies, displays, ultrasonic
  devices.
- **At least 5–8 single, clean strikes** per file, each left to ring out before the next.
- **The mic at a fixed distance**, about 5–20 cm.

### 2. Put the file in `data/raw/`

Copy the WAV into `data/raw/` and never change it afterwards. Results are keyed by the file's
SHA-256, so every number can be traced back to the original bytes. barlab never writes there, and
`.claude/settings.json` denies Claude Code write access. You can also make the file read-only
(`chmod a-w`).

`data/raw/` is **not in git**. Bat-detector WAVs carry GUANO metadata that includes GPS
coordinates, so back the folder up separately. barlab never copies location data into its results.

### 3. Describe the setup (optional, but it makes the theory useful)

Write what you know into `data/meta/<stem>.toml`, where `<stem>` is the file name without `.wav`.
Alternatively, tell `/analyze-bars` in your prompt and it writes the file for you. The template is
`.claude/skills/analyze-bars/references/meta_template.toml`:

```toml
[recording]
mic = "PYG Bat_Detector_USB_384kHz"
distance_m = 0.10
temperature_c = 21.0
striking = "bar A struck against bar B, hand-held"
support = "bars resting on foam at the middle"

[[bar]]            # one block per bar that can ring
name = "A"
L = "66.5mm"       # "66.5mm", "6.65cm" or plain metres
bc = "free-free"   # "clamped-free" for a clamped bar
```

The metadata is merged in three layers, each overriding the one before:
1. `data/meta/_defaults.toml` (d = 16 mm, aluminium, free-free);
2. the per-recording sidecar;
3. command-line flags.

Leave out what you don't know. Without a bar length, barlab lists the length each mode hypothesis
would need instead of guessing.

### 4. Analyse

**With Claude Code:**

```text
/analyze-bars                        # every file in data/raw/ that has not been analysed yet
/analyze-bars data/raw/rec4.wav      # specific files, or a quoted glob
```

Put what you know in the same message, for example "bars are 66.5 mm long, resting on foam, mic
10 cm away, 21 °C". The skill writes `results/reports/YYYY-MM-DD_<stem>.md` for each file, plus
`YYYY-MM-DD_batch-summary.md` when there are several.

**Without Claude Code**, the CLI gives the same numbers:

```bash
uv run barlab pending                      # raw files that have no results yet
uv run barlab analyze data/raw/rec4.wav    # writes results/rec4/ and updates the index
uv run barlab table rec4 --md              # the peaks table
uv run barlab compare rec4 --md            # the same modes in earlier recordings
```

## Reading the results

### The report

Every report in `results/reports/` has the same sections:

| section | what it tells you |
|---|---|
| Verdict | Either "Ringing frequency: **f ± σ Hz** (spread over n hits; standard error …), Q ≈ …" for each bar-mode candidate, or **Not determinable** with the reason. A CRITICAL warning always comes first. |
| Recording quality | sample rate, warnings, hits, clipping, noise reference, spectral resolution, ultrasonic content |
| Detected peaks | the table from `barlab table` |
| Interpretation | three parts: *Measured* (frequencies, Q, spreads, SNR), *Inferred* (what each peak is, the mode family, the theory match) and *Caveats* |
| Comparison with previous recordings | the table from `barlab compare`: drift in Hz and ppm, and how significant it is |
| Recommendations | what to change in the next session, based on what this file showed |
| Figures | the five plots |

### Peak classes

Every spectral peak gets exactly one class. The rules are checked in this order, and the first one
that applies wins:

| class | meaning | is it the answer? |
|---|---|---|
| `possible_alias` | The sample rate is below 96 kHz, or the peak is close to Nyquist. The line may be folded down from a higher frequency. | never |
| `noise_or_environment` | Less than 6 dB stronger after hits than in the noise window, so it is there without hits too (electronics, other ultrasonic sources). | no |
| `unresolved` (steady) | No measurable decay at all, so it is not a struck bar. | no |
| `broadband_resonance` | Decays fast (Q < 100): a hand, support or mic resonance. | no |
| `bar_mode_candidate` | High Q (≥ 100), the same frequency (spread < 1 %) in at least 2 hits, and clearly stronger after hits than in the noise. | yes; it is a bar mode by inference |
| `unresolved` | Anything else, such as a single hit, inconsistent hits or failed fits. `reasons` says what is missing. | not yet |

Flags add detail:

| flag | meaning |
|---|---|
| `steady` | no decay |
| `q_lower_bound` | the decay was too slow to measure in the window, so only "Q > x" is known |
| `q_from_stack` | Q comes from the hit-averaged envelope, because fewer than 3 hits gave a clean fit |
| `line_in_noise` | the line is also present in the noise window |
| `resolution_limited` | the peak is as narrow as the spectral resolution allows |
| `matches_theory` | an unambiguous match with a predicted mode |

### The numbers

- **f ± σ:** the median of the per-hit frequencies and their spread between hits. Per-hit
  frequencies come from phase fits, so they are much finer than the spectral bin spacing.
- **Standard error** (`f_sem_hz`): σ/√n, the uncertainty of the mean frequency.
- **Q = π f τ**, where τ is the amplitude decay time. "Q > x" is a lower bound. "(stack)" marks a
  hit-averaged fit. For a free aluminium bar expect 10³–10⁵, for a hand-held or clamped bar
  10²–10³. Below 100 it is not the bar ringing.
- **SNR:** the ringdown spectrum against the noise window at the peak. "Above floor" compares
  against the smoothed noise floor instead.
- **Significance in `compare`:** the frequency difference divided by the combined standard error.
  Above about 3 it is a real change. Two causes to rule out:
  - temperature: aluminium shifts by about −2.4 × 10⁻⁴ per kelvin, about −10 Hz/K at 40 kHz;
  - sample clocks: different recorders differ by ±20–100 ppm.

### The plots (`results/<stem>/`)

- **`psd.png`**
  - The main panel shows the average ringdown spectrum (black) against the noise window (grey)
    and the smoothed noise floor. Peaks are marked by class.
  - Grey vertical lines are theory predictions when the bar length is known: L longitudinal,
    F flexural, T torsional, numbered by mode.
  - Lower left: the per-bin SNR. Lower right: a zoom on the strongest candidate, with single
    hits drawn as thin lines.
- **`hit_zoom.png`**: a spectrogram of one hit.
  - The vertical stripe at t = 0 is the impact click. It is broadband, which is why a raw
    spectrum looks "everything covered".
  - Bar modes are horizontal lines that start at the click and fade.
  - Lines that are already there before the hit are environmental.
  - The bars at the top mark the attack window (excluded) and the ringdown window (analysed).
- **`spectrogram.png`**: the whole file on a linear frequency axis up to Nyquist. The ticks on top
  mark the hits.
- **`decay.png`**
  - For the main peaks, each hit's band envelope (in dB) against time, with the fitted decay lines
    (black accepted, grey rejected) and the band's noise level.
  - A straight slope means a clean exponential decay. Wiggles suggest two close modes beating,
    for example two nearly identical bars.
- **`waveform.png`**
  - The waveform with hit numbers and the attack, ringdown and noise windows.
  - Below it, the detector trace (the rise over the trailing 20 ms level) against the detection
    threshold.

### `result.json` and `index.csv`

- **`results/<stem>/result.json`** holds everything about one file:
  - warnings;
  - the hits and their windows;
  - the spectral settings;
  - every peak, with its per-hit fits, class, flags, reasons and theory match;
  - a `summary` (`determinable`, `ringing_hz`, `q`, …);
  - every parameter used.

  Each field is documented in [`docs/result_schema.md`](docs/result_schema.md).
- **`results/index.csv`** has one row per analysed file and peak, keyed by the file's SHA-256.
  - Re-analysing a file replaces its rows.
  - It is the history behind `pending` and `compare`, and it opens in any spreadsheet.
  - Its `source` column keeps real recordings apart from synthetic ones.

## Command reference

Run every command as `uv run barlab <command>`. `--root DIR` points at another project root.

| command | what it does |
|---|---|
| `analyze FILES…` | Analyses WAV files or quoted globs. See the flags below. |
| `pending` | Lists raw files whose content has no row in the index yet. |
| `table STEM --md` | Prints the peaks table of one analysed file. |
| `compare STEM --md` | Lists earlier recordings of the same source with a matching mode: Δf in Hz and ppm, significance, Q ratio. `--tol` (default 0.02) sets how close counts as the same mode. |
| `compare STEM STEM… --md` | Prints a batch table: one row per file, then the modes across files. |
| `theory --L 66.5mm --d 16mm` | Lists the expected modes of every family below `--fs`/2 or `--fmax`, with validity notes. |
| `theory --d 16mm --implied-from F` | Lists the bar length each mode hypothesis would need to ring at F Hz. |
| `demo` | Rewrites the synthetic test recordings in `data/synthetic/` (never into `data/raw/`). |

Flags for `analyze`:
- `--L 66.5mm` (repeat it for several bars), `--d`, `--bc free-free|clamped-free`, and
  `--material` or `--E`/`--rho`/`--nu` override the metadata.
- `--set KEY=VALUE` overrides any analysis parameter in `barlab/params.py`, for example
  `--set k_mad=8`.
- `--no-plots` skips the PNGs.
- `--results DIR` writes the results and the index somewhere else, which is handy for trial runs.

Example: once you have a measured frequency, ask which bar length each explanation needs, then
measure the bars:

```text
$ uv run barlab theory --d 16mm --implied-from 38000 --md
material aluminium: E=69.0 GPa, rho=2700 kg/m3, nu=0.330; c=5055 m/s, c_T=3100 m/s; d=16.00 mm; bc=free-free
| f (Hz) | family | n | model | L (mm) | L simple formula (mm) |
|---|---|---|---|---|---|
| 38 000.0 | longitudinal | 1 | rayleigh-love | 66.26 | 66.52 |
| 38 000.0 | longitudinal | 2 | rayleigh-love | 132.51 | 133.03 |
| 38 000.0 | longitudinal | 3 | rayleigh-love | 198.77 | 199.55 |
| 38 000.0 | flexural | 1 | timoshenko | 37.22 | 43.53 |
| 38 000.0 | flexural | 2 | timoshenko | 61.02 | 72.27 |
| 38 000.0 | flexural | 3 | timoshenko | 85.61 | 101.19 |
| 38 000.0 | torsional | 1 | exact-circular | 40.78 | 40.78 |
| 38 000.0 | torsional | 2 | exact-circular | 81.57 | 81.57 |
```

How to read the result: each row is one explanation, and your bar's measured length picks it.
- A 66 mm bar points to the first longitudinal mode.
- A 61 mm bar points to the second flexural mode.
- A length that matches no row rules out every explanation in the table.

## How the analysis works

1. **Load and check** (`io.py`). The file is read at its native sample rate. fs < 96 kHz is
   CRITICAL.
2. **Find the hits** (`segment.py`) and cut each one into:
   - the attack, the first 1 ms, which holds the click and is excluded;
   - the ringdown, which lasts until the ringing sinks into the noise or the next hit begins;
   - quiet noise windows.

   Ringdowns that clip are restarted after the last clipped sample.
3. **Compare spectra** (`spectra.py`). The average ringdown spectrum is compared with the noise
   spectrum, and peaks are found.
4. **Fit the decays** (`decay.py`). For each peak and hit: band-pass, take the envelope, fit the
   decay (τ, Q) and get the frequency from the phase.
5. **Compare with bar theory** (`theory.py`), when the length is known.
   - Families: longitudinal with the Rayleigh–Love correction; flexural with Euler–Bernoulli and
     Timoshenko (needed for stubby bars, L/d < 10); torsional.
   - Boundary conditions: free-free or clamped-free.
6. **Classify and record** (`classify.py`). Every peak is classified and the file summarised.
   Then `result.json`, the plots and the index rows are written.

`barlab/pipeline.py` connects these steps and is the best place to start reading the code. All
thresholds live in `barlab/params.py`. The physics used for interpretation is in
`.claude/skills/analyze-bars/references/interpretation.md`: plausible Q values, mode families and
their ratios, aliasing, drift.

## Repository map

```text
barlab/                        the analysis package (pipeline.py connects the steps)
tests/                         pytest suite on synthetic signals with known answers
data/raw/                      your recordings: read-only, not in git
data/synthetic/                synthetic test recordings + *.truth.json ground truth
data/meta/                     _defaults.toml and per-recording <stem>.toml details
results/<stem>/                result.json + five plots per analysed file
results/index.csv              one row per file and peak: the analysis history
results/reports/               reports written by /analyze-bars
docs/result_schema.md          every field of result.json
.claude/skills/analyze-bars/   the skill: SKILL.md, report template, physics notes, evals
.claude/settings.json          denies Claude Code write access to data/raw/
evals/                         benchmarks and viewers of the skill evaluations
CLAUDE.md                      instructions for Claude Code working in this repo
```

## Development

**Tests.** `uv run pytest -q` must pass.
- Run one file with `uv run pytest tests/test_decay.py -q`, or one test with `-k <name>`.
- The tests build a temporary project from the synthetic scenarios in `barlab/synth.py` and never
  touch `data/` or `results/`.

**Rules the code relies on:**
- Never resample. Frequency axes are linear up to Nyquist. No librosa, no mel scales.
- All signal processing lives in `barlab/`, and every change to it comes with a test.
- When `result.json` changes, update `docs/result_schema.md` (a test checks that every key is
  documented). For incompatible changes, also bump `SCHEMA_VERSION` in `barlab/schema.py`.

**Skill evaluations** live in `.claude/skills/analyze-bars/evals/`. A round works like this:
1. `setup_sandboxes.py` builds one project copy per run under
   `evals/analyze-bars-workspace/iteration-N/`.
2. Claude Code subagents carry out the runs.
3. `grade.py` checks the reports against the expectations in `evals.json`.
4. The benchmark and the viewer come from Claude Code's skill-creator plugin.

Each iteration's `benchmark.md` and `review.html` are committed. The sandboxes themselves are
not.
