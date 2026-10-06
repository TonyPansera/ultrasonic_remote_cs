# Batch summary: 2026-10-06 (4 recordings)

All four files are synthetic test signals from `barlab demo` (`data/synthetic/`), so they test the
pipeline and the reporting, not a real bar. Each contains:
- the same simulated bar, with modes injected at 38.0 kHz (Q 500) and 76.0 kHz (Q 800);
- a 15 kHz, Q 60 mount resonance;
- a continuous 30 kHz environment line.

Three files differ only in noise level (SNR 40, 20 and 10 dB). The fourth uses the wrong sample
rate.

## Verdicts

- `synth_fs192k_snr40`: ringing at **38 000.1 ± 0.21 Hz**, Q ≈ 502; second mode 76 000.3 ± 0.49 Hz,
  Q ≈ 816; no warnings ([report](2026-10-06_synth_fs192k_snr40.md))
- `synth_fs192k_snr20`: ringing at **38 000.5 ± 0.63 Hz**, Q ≈ 504; second mode 76 001.9 ± 4.56 Hz,
  Q ≈ 788 ([report](2026-10-06_synth_fs192k_snr20.md))
- `synth_fs192k_snr10`: ringing at **37 999.9 ± 5.71 Hz**, Q ≈ 489; second mode 76 009.4 ± 38.20 Hz,
  Q ≈ 763 (stacked fit, weak) ([report](2026-10-06_synth_fs192k_snr10.md))
- `synth_fs48k_snr40`: **Not determinable**: CRITICAL `FS_BELOW_96K`. It was recorded at 48 kHz, so
  all four peaks are possible aliases ([report](2026-10-06_synth_fs48k_snr40.md))

## Comparison table

| file | fs (kHz) | hits | verdict | ringing f ± σ (Hz) | Q | nearest theory | peak classes | worst warning |
|---|---|---|---|---|---|---|---|---|
| synth_fs192k_snr40 | 192 | 8 | determinable | 38 000.1 ± 0.21 | 502.0 | longitudinal n=1 (-0.00%) | bar_mode_candidate 2, broadband_resonance 1, noise_or_environment 1 | — |
| synth_fs192k_snr20 | 192 | 8 | determinable | 38 000.5 ± 0.63 | 504.0 | longitudinal n=1 (+0.00%) | bar_mode_candidate 2, broadband_resonance 1, noise_or_environment 1 | INFO |
| synth_fs192k_snr10 | 192 | 8 | determinable | 37 999.9 ± 5.71 | 489.0 | longitudinal n=1 (-0.00%) | bar_mode_candidate 2, broadband_resonance 1, noise_or_environment 1 | INFO |
| synth_fs48k_snr40 | 48 | 8 | not determinable (fs = 48000 Hz < 96 kHz) | — | — | — | possible_alias 4 | CRITICAL |

Bar-mode candidates across files (same mode = within ±2.0%):

| mode ≈ f (Hz) | synth_fs192k_snr40 | synth_fs192k_snr20 | synth_fs192k_snr10 | synth_fs48k_snr40 | spread (ppm) |
|---|---|---|---|---|---|
| 38 000.2 | 38 000.1 | 38 000.5 | 37 999.9 | — | 16 |
| 76 003.9 | 76 000.3 | 76 001.9 | 76 009.4 | — | 120 |

## Cross-file observations

- **The same modes appear wherever they can be seen.**
  - The 38 kHz and 76 kHz candidates are found in all three 192 kHz files.
  - Across files they spread by 16 ppm (38 kHz) and 120 ppm (76 kHz).
  - Every pairwise difference is ≤ 1.3σ (see the comparison sections of the per-file reports).
    That is what one unchanged bar should give, and it shows that the per-file uncertainties are
    realistic.
- **The ground truth is recovered.**
  - In every 192 kHz file the injected 38 000.0 and 76 000.0 Hz lie within the stated per-hit
    spreads.
  - Q comes out at 489–504 for the 38 kHz mode (injected 500) and 763–816 for the 76 kHz mode
    (injected 800).
  - The 15 kHz resonance is always `broadband_resonance` and the 30 kHz line always
    `noise_or_environment`. The impact click is never reported as a mode.
  - One limit: the spread of Q between hits measures precision, not accuracy. At SNR 40 the
    38 kHz Q is 502 ± 0.492 against an injected 500.
- **Noise sets the precision.** From SNR 40 to SNR 10:
  - the median usable ringdown shrinks from 22.7 to 13.3 to 9.3 ms;
  - the PSD resolution coarsens from 46.9 to 75.0 to 107.1 Hz (`INFO RESOLUTION_COARSE`);
  - the 38 kHz spread grows from 0.21 to 0.63 to 5.71 Hz.

  At SNR 10 the weak 76 kHz mode gives a Q only through the hit-averaged stack.
- **The 48 kHz file cannot answer.**
  - Its 10.0 and 20.0 kHz lines decay like good modes and agree across hits, yet according to its
    truth file they are the 38 and 76 kHz modes folded down.
  - 18.0 kHz is the folded 30 kHz line.
  - Only the CRITICAL sample-rate check catches this. The report says "not determinable" and names
    no ringing frequency.
- **The theory matches are circular.**
  - `barlab demo` set the sidecar L = 66.257 mm so that longitudinal n = 1 falls at 38.0 kHz, so
    the −0.00 % matches only test the code.
  - The family of the 76 kHz mode stays ambiguous: longitudinal n = 2 sits 1.16–1.17 % below it,
    and flexural n = 4 is about as close.
- **No other warnings.** No file shows clipping, a noise-window fallback or a channel problem.

## Recommendations

- **Sample rate first.** Record at 192 kHz or more, 384 kHz preferred, and check the setting before
  each session. This alone decides whether a file can answer at all.
- **SNR next.** Keep the mic close (5–20 cm), set the gain just below clipping (hits at or below
  about −6 dBFS), and record in a quiet room with no ultrasonic sources. In this batch, SNR alone
  moved the 38 kHz spread from 0.21 to 5.71 Hz.
- **Each file needs:**
  - a quiet lead-in of at least 0.5 s;
  - at least 8 single, clean strikes;
  - each strike left to ring into the noise before the next.
- **For the real bars**, write these into `data/meta/<stem>.toml`:
  - L, measured to ±0.1 mm;
  - the support and the strike direction;
  - the mic and its distance;
  - the temperature.

  With those, a theory match becomes evidence instead of a circular check.
