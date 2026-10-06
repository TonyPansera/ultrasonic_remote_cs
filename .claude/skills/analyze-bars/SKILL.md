---
name: analyze-bars
description: Analyse bar-impact / ringdown WAV recordings with this repo's barlab pipeline and write an honest report of the ringing frequency (or frequencies), Q and uncertainty, compared with earlier recordings and bar theory. Use it whenever the user wants recordings in data/raw/ or data/synthetic/ analysed or re-analysed, says they added new recordings, asks what frequency the bars ring at, wants a report or a comparison between recording sessions, or types /analyze-bars, even if they never say "barlab".
argument-hint: "[files or glob]"
allowed-tools: Bash(uv run barlab *) Bash(date *) Read Glob Write
---

# /analyze-bars

Turn bar-impact recordings into measured ringing frequencies and a written verdict that a
physicist would sign. The numbers all come from `barlab`, the deterministic DSP package in
this repo. Your job is to run it, check its output against the plots, and explain what is
measured, what is inferred, and how sure we are.

## Ground rules, and why

- `data/raw/` is the archive of real recordings and is read-only. Never write, move, rename or
  "fix" anything there: a re-analysis must always be able to start from the original bytes.
  Reports go to `results/reports/`; recording details go to `data/meta/`.
- Don't do DSP in the chat: no ad-hoc numpy/scipy, no resampling, no librosa or mel scales.
  Every number has to be reproducible by `uv run barlab ...`. If barlab can't do something the
  analysis needs, stop and propose a change to `barlab/` plus a test in `tests/`.
- Copy numbers rather than retyping them: paste `barlab table` / `barlab compare` output.
- Keep "measured" and "inferred" apart. Frequencies, Q, decay times, spreads and SNR in
  result.json are measurements. Classes, flags, theory matches and implied bar lengths are
  inferences from them. Say which is which.

## 1. Targets

Targets given with the command: `$ARGUMENTS`

That line is empty when no arguments were given; if you are reading this file directly it
shows a literal placeholder. In both cases take the targets from the user's message. With no
targets at all, analyse every raw recording that has no row in `results/index.csv` yet:

    uv run barlab pending

If nothing is pending, say so and ask whether to re-analyse or compare existing results.

## 2. Recording details (only what the user actually said)

If the user gives bar length, diameter, material, support ("clamped" means
`bc = "clamped-free"`), mic, distance, temperature or how the bars were struck, write
`data/meta/<stem>.toml` before analysing; the format is in `references/meta_template.toml`.
Write only what was stated. An unknown length stays absent: barlab then reports which bar
length each mode hypothesis would need, which is more useful than a guess.
`data/meta/_defaults.toml` already holds the project-wide d = 16 mm and aluminium.

## 3. Run barlab

    uv run barlab analyze <files or "quoted globs">

This writes `results/<stem>/result.json` plus five PNGs and updates `results/index.csv`
(re-running replaces a file's rows, so it never duplicates them). Read the summary lines it
prints: CRITICAL and WARNING lines appear there.

## 4. Read the evidence for each file

1. `results/<stem>/result.json`. Field meanings are in `docs/result_schema.md`; read the parts
   you need. Start from `warnings`, `summary`, `spectra` and `peaks`.
2. Look at `psd.png`, `hit_zoom.png` and `spectrogram.png`, plus `decay.png` for the main
   peaks. They are how you sanity-check the numbers. A bar mode is a horizontal line that
   starts at the impact click and fades. A line that is already there before the hit,
   continues unchanged, or never fades is something else.
3. `uv run barlab table <stem> --md` gives the peaks table for the report.
4. `uv run barlab compare <stem> --md` gives earlier recordings of the same kind (raw vs
   synthetic are never mixed) with a matching mode, the drift in Hz and ppm, and its
   significance.

## 5. Decide the verdict

Read `references/interpretation.md` before interpreting peaks; it has the physics, the
plausible ranges and the meaning of each class and flag. The verdict follows these rules:

- **CRITICAL first.** With `FS_BELOW_96K` the file cannot answer the question. Content above
  fs/2 never made it into the file, or folded back as aliases, so every peak is labelled
  `possible_alias`. The verdict is "Not determinable" and says why, up front. Don't present any
  peak as the ringing frequency, not even as "probably".
- Otherwise start from `summary` (`determinable`, `primary_peak`, `ringing_hz`). Then check it
  against the plots and flags, and if you disagree, say so and why.
- **Ringing frequency** = `f_hz` of the `bar_mode_candidate` peak(s), ± `f_std_hz` (spread
  between hits; standard error `f_sem_hz`, n = `decay.n_freq`). Quote **Q** from `decay.q`,
  or as "Q > `q_lower`" when the decay was too slow to measure; say when Q came from the
  stacked fit (`q_source: stack`).
- Several candidates: report all of them and name the strongest. Don't squeeze them into one
  number.
- No candidate: say "not determinable" and give the reasons (from `summary.reason` and each
  peak's `reasons`). Don't promote an `unresolved`, `steady`, environment or broadband peak to
  the answer.
- Theory: when L is known, quote the nearest mode (family, n, relative error) and whether the
  match is unambiguous. When L is unknown, use `implied_L_mm` to say which bar length each
  hypothesis would need, and invite the user to measure the bars.

## 6. Write the reports

- One report per file: `results/reports/YYYY-MM-DD_<stem>.md` (today's date: `date +%F`).
- For more than one file, also write `results/reports/YYYY-MM-DD_batch-summary.md` built
  around `uv run barlab compare <stem1> <stem2> ... --md`.
- Use the fixed templates in `references/report_template.md`, with the same sections in the
  same order: Verdict, Recording quality, Detected peaks, Interpretation, Comparison with
  previous recordings, Recommendations for the next recording session, Figures. Figures are
  embedded as `../<stem>/<name>.png`.
- Recommendations should follow from what this file showed (e.g. clipping means lower the
  gain; short ringdowns mean leave more time between strikes; fs < 192 kHz means record at
  192/384 kHz), not be generic advice.

## 7. Reply in the chat

Per file: one or two lines with the verdict and the report path. For a batch, also the summary
path. Mention anything the user must act on (CRITICAL warnings, a missing bar length). Don't
paste whole reports unless asked.

## Reference files

- `references/report_template.md`: the exact per-file and batch templates. Read it before
  writing a report.
- `references/interpretation.md`: physics, plausible Q and frequency ranges, how to read
  classes, flags and drift, and the recommendation checklist.
- `references/meta_template.toml`: format of `data/meta/<stem>.toml`.
- `docs/result_schema.md` (in the repo): every result.json field.
