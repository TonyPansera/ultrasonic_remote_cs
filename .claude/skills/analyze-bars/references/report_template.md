# Report templates

Use these exactly: same headings, same order. Everything in `<...>` is filled in; delete any
remaining guidance in parentheses. Numbers come from result.json or from barlab output pasted
as-is, never from mental arithmetic.

## Per-file report: `results/reports/YYYY-MM-DD_<stem>.md`

```markdown
# <stem>: bar ringing analysis (<YYYY-MM-DD>)

`<file.path>` · sha256 `<first 12 chars>` · <fs/1000> kHz, <bit_depth>-bit, <channels> ch,
<duration_s> s · barlab <barlab_version> · data: [`result.json`](../<stem>/result.json)

## Verdict

<One or two lines. Either:
"Ringing frequency: **<f_hz> ± <f_std_hz> Hz** (spread over <n_freq> hits; standard error
<f_sem_hz> Hz), Q ≈ <q> (<q_source>) - <why we believe it is a bar mode, in a few words>."
listing every bar_mode_candidate, strongest first; or
"**Not determinable**: <reason>." When there is a CRITICAL warning, the verdict starts with it.>

## Recording quality

- **Sample rate / format:** <fs> Hz (Nyquist <fs/2> Hz), <subtype>, <channels> channel(s)
  (analysed: <channel_used>), <duration> s; device: <GUANO make/model or "unknown">.
- **Warnings:** <each as `SEVERITY CODE`: what it means for this file, or "none">.
- **Hits:** <n_hits> detected; ringdowns <typical ringdown_ms> ms; clipping <none / trimmed /
  excluded>; noise reference <noise_total_s> s<, fallback if used>.
- **Spectral resolution:** <resolution_hz> Hz (target ≤ 50 Hz); frequencies come from per-hit
  phase fits, which are much finer than the PSD grid.
- **Ultrasonic content:** strongest excess above 20 kHz <ultrasonic_peak_snr_db> dB<; note
  roll-off if flagged>.

## Detected peaks

<paste the output of `uv run barlab table <stem> --md`>

<1-3 lines of legend as needed: "± σ hits" = spread of per-hit frequencies; "(stack)" = Q from
the hit-averaged envelope; classes: bar_mode_candidate / broadband_resonance (low Q) /
noise_or_environment (also present without hits) / possible_alias / unresolved (see reasons).>

## Interpretation

- **Measured:** <frequencies, Q, decay times, consistency across hits, SNR - with uncertainties>.
- **Inferred:** <which peaks are bar modes and why; what the others are (environment line,
  mount/mic resonance, steady tone, alias); mode family / theory match or implied bar length;
  confidence and what would change it>.
- **Caveats:** <resolution, few hits, clipping, beating/close pairs, unknown geometry,
  uncalibrated mic amplitude - only those that apply>.

## Comparison with previous recordings

<paste `uv run barlab compare <stem> --md`, then 1-3 lines: same mode found again? drift in Hz
and ppm vs its significance; temperature (~ -2e-4 per K) or sample-clock (~ ±20-100 ppm between
devices) explanations where relevant. If there is nothing to compare, say so.>

## Recommendations for the next recording session

- <3-6 concrete items that follow from what this file showed: sample rate, gain/clipping, mic
  position, striking (gentler, single contact, spacing between hits), mounting/support at
  nodes, what to measure and note (bar L, support, temperature).>

## Figures

![Ringdown vs noise PSD](../<stem>/psd.png)
![Single hit: click vs ringdown](../<stem>/hit_zoom.png)
![Spectrogram](../<stem>/spectrogram.png)
![Decay fits](../<stem>/decay.png)
![Waveform and windows](../<stem>/waveform.png)
```

## Batch summary: `results/reports/YYYY-MM-DD_batch-summary.md`

```markdown
# Batch summary: <YYYY-MM-DD> (<n> recordings)

## Verdicts

- `<stem>`: <one-line verdict> ([report](YYYY-MM-DD_<stem>.md))

## Comparison table

<paste `uv run barlab compare <stem1> <stem2> ... --md` (both tables)>

## Cross-file observations

<Is the same mode found in every determinable file? Spread in ppm vs per-file uncertainty;
which files could not answer and why; patterns in warnings (clipping, fs, roll-off).>

## Recommendations

- <the few changes that would most improve the next session, across all files>
```
