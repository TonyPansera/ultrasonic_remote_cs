# Batch summary: 2026-10-09 (2 recordings)

## Verdicts

- `rec4`: the ringing frequency is **51 014.2 ± 103.00 Hz**, Q ≈ 23 200 (stack). This is the
  "~50 kHz" bar, strongest line above 20 kHz and 60.0 dB above the floor, from one full strike.
  Weaker candidates: 48 814.5 and 79 111.0 Hz. ([report](2026-10-09_rec4.md))
- `rec5`: barlab's strongest candidate is 24 870.2 ± 8.30 Hz, Q ≈ 13 800 (stack).
  - The "~40 kHz" line is there at **40 741.9 Hz** (Q ≈ 8620 stack, τ 67.3 ms, SNR 39.4 dB). It
    stays `unresolved` because it comes from a single strike.
  - 34 354.2 Hz is stronger (SNR 56.0 dB), also single-strike.

  ([report](2026-10-09_rec5.md))

## Comparison table

| file | fs (kHz) | hits | verdict | ringing f ± σ (Hz) | Q | nearest theory | peak classes | worst warning |
|---|---|---|---|---|---|---|---|---|
| rec4 | 384 | 11 | determinable | 51 014.2 ± 103.00 | 23200.0 | — | bar_mode_candidate 3, broadband_resonance 3, noise_or_environment 1, unresolved 13 | WARNING |
| rec5 | 384 | 18 | determinable | 24 870.2 ± 8.30 | 13800.0 | — | bar_mode_candidate 3, broadband_resonance 2, noise_or_environment 2, unresolved 13 | WARNING |

Bar-mode candidates across files (same mode = within ±2.0%):

| mode ≈ f (Hz) | rec4 | rec5 | spread (ppm) |
|---|---|---|---|
| 14 499.9 | — | 14 499.9 | — |
| 24 870.2 | — | 24 870.2 | — |
| 48 794.4 | 48 814.5 | 48 774.3 | 824 |
| 51 014.2 | 51 014.2 | — | — |
| 79 111.0 | 79 111.0 | — | — |

## Cross-file observations

- **The two strikes ring at different frequencies:** rec4 at 51.01 kHz, rec5 at 40.74 kHz (plus
  34.35 kHz). These lines tell the two bars apart.
  - rec4 has no 40.74 kHz peak.
  - rec5 has 51 013.9 Hz only weakly (18.3 dB, unfitted), probably the 51 kHz bar ringing in
    sympathy.

  So the expected ~40 / ~50 kHz pair is present, as 40.74 / 51.01 kHz.
- **48.8 kHz is in both files** (824 ppm apart, 0.4 σ). It is common to both strikes, so it is
  useless for telling them apart.
- **Each file has one full strike;** the other 10 and 17 "hits" are handling transients. That
  makes every Q a stacked estimate. It also leaves 40.74 and 34.35 kHz `unresolved` (single hit),
  and caps per-hit consistency at 2-3 hits.
- **Both main strikes clip** (38-40 ms of ringdown trimmed).
- **The 27.34 kHz environment line appears in both files.**
- **No theory match:** the bar lengths are unknown.

## Recommendations

- Record **one bar per file, ≥ 5 clean strikes, ≥ 1 s apart**, with hands off the mechanism.
  This turns the single-strike lines into proper candidates with spreads.
- **Lower the gain or strike softer:** both main strikes clip.
- **Measure both bar lengths** and note which button/bar each file strikes in `data/meta/`.
  - For 51.01 kHz, longitudinal n = 1 needs 49.2 mm.
  - For 40.74 kHz, longitudinal n = 1 needs 61.8 mm.
- For a detector (e.g. the ESP32), use 40.74 kHz and 51.01 kHz as the two bands, not 48.8 kHz.
  Re-check 40.74 kHz once a multi-strike recording confirms it.
