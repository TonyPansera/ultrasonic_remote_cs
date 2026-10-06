# Interpreting barlab results

Physics and judgement aids for the Interpretation, Verdict and Recommendations sections.
Figures marked "approx." are textbook-level estimates: use them to judge plausibility, never as
measurements.

## Why a raw spectrum looks "broadband, everything covered"

- **The impact click.** Metal-on-metal contact lasts tens of µs, so its spectrum is flat to
  tens or hundreds of kHz, and it dominates a whole-file spectrum. barlab cuts out the first
  `attack_ms` (1 ms) of each hit. `hit_zoom.png` shows the click as a vertical stripe and the
  ringing modes as horizontal lines.
- **Clipping.** A waveform flattened at full scale contains broadband distortion and
  harmonics. barlab restarts a clipped ringdown after the last clipped sample
  (`CLIPPING_RINGDOWN`, `clip_trim_ms`), but heavy clipping still costs the strongest part of
  the ringdown. The fix is lower gain or gentler strikes.
- **Rattles and bounces** (the bars touching again), handling noise, and low-Q resonances of
  the hand, supports or mic housing. These die out within milliseconds and show up as
  `broadband_resonance` or as extra hits.

## Classes, and what to say about them

| class | evidence | in the report |
|---|---|---|
| `bar_mode_candidate` | Q >= 100 (or a lower bound above it); per-hit frequency consistent (spread < 1 %) over >= 2 hits; much stronger after hits than in the noise window | the measured ringing frequency, a bar mode by inference |
| `broadband_resonance` | decays fast (Q < 100), or a wide peak with no measurable Q | a mount, hand or mic resonance; not the bar's ringing |
| `noise_or_environment` | same level in the noise window as after hits | an external source (electronics, other ultrasonic devices, the recorder); not the bar |
| `possible_alias` | fs < 96 kHz, or the peak is close to Nyquist | can't be trusted as a frequency at all |
| `unresolved` | read `reasons`: `steady` (no decay), single hit, inconsistent hits, failed fits | not an answer; say what would resolve it |

Flags worth explaining when present:

- `steady`: no measurable decay over the window. That would need Q > 10^6, which a struck
  metal bar doesn't reach, so it is a constant tone from some source. Check whether it starts
  or stops at the hits in `waveform.png` and `hit_zoom.png`.
- `q_lower_bound`: the decay was too slow to measure in the windows, so only "Q > x" is
  known. Longer, undamped ringdowns give a real Q.
- `q_from_stack`: fewer than 3 hits gave a clean decay fit, so Q comes from the hit-averaged
  envelope. Usually the cause is low SNR, beating, or a mode excited differently by each hit.
- `line_in_noise`, `resolution_limited`, `narrow_band` (a neighbouring peak limited the
  band-pass), `wide`.

## Plausible ranges for aluminium bars (approx.)

- Wave speeds: c = sqrt(E/rho) ≈ 5.05 km/s (E ≈ 69 GPa, rho ≈ 2700 kg/m³; alloys vary by
  about ±2 %). Torsional c_T = sqrt(G/rho) ≈ 3.1 km/s.
- Q:
  - well supported, free bar: 10^3–10^5 (material limit roughly 10^4–10^5; supports and air
    lower it);
  - hand-held or clamped: 10^2–10^3;
  - mount, hand or mic resonances: < 100;
  - no measurable decay over seconds: not a struck bar.
- A longitudinal fundamental at 30–50 kHz needs L ≈ c / 2f ≈ 50–85 mm (free-free). For
  d = 16 mm and L ≈ 65 mm, Rayleigh–Love lowers it by about 0.4 %.

## Mode families (free-free unless noted)

- **Longitudinal:** f_n ≈ n c / 2L, so ratios 1 : 2 : 3 (Rayleigh–Love puts higher n slightly
  below the integers). Node of n = 1 at the middle.
- **Flexural:** Euler–Bernoulli ratios are 1 : 2.757 : 5.404 : 8.933. For stubby bars
  (L/d < 10), shear and rotary inertia lower them a lot. At L/d ≈ 4, Timoshenko is about −12 %
  for n = 1 and −26 % for n = 2, so barlab matches against Timoshenko. Nodes of n = 1 are at
  0.224 L from each end.
- **Torsional:** f_n = n c_T / 2L. Rarely excited by an axial or transverse strike.
- **Clamped-free:** longitudinal (2n − 1) c / 4L, ratios 1 : 3 : 5; flexural βL = 1.875,
  4.694, 7.855.
- **Exact integer multiples of a strong line** (2f, 3f) usually point to nonlinearity
  (clipping, a transducer, electronics) or a periodic source, not to bar modes.
- **Two bars striking each other** give two sets of modes. Nearly identical bars give close
  pairs (Hz to tens of Hz apart), which beat in the envelope. Symptoms are low per-hit R²,
  `q_from_stack` and frequency scatter between hits. Long, undamped ringdowns resolve the
  pairs: barlab's PSD resolution gets finer as the ringdowns get longer.

## Aliased peaks (fs < 96 kHz, or near Nyquist)

- A line at f may really be at |k·fs ± f| for any integer k, so no peak can be named as the
  ringing frequency. barlab gives such peaks no theory match.
- The decay time τ survives aliasing, but Q = π f τ uses the folded f, so the Q of a
  `possible_alias` peak is not the bar's Q.
- Recommend recording at 192 kHz or more. At 96 kHz a 40 kHz mode is visible, but its
  harmonics and higher modes still fold back.

## Theory matching

- **L known:** quote `theory.nearest` (family, n, `rel_err`) and `unambiguous`. An unambiguous
  match within about 1–2 % supports the identification; an ambiguous one is not evidence.
  Mention `theory.validity` notes.
- **Synthetic files** (`data/synthetic/`): `barlab demo` wrote their sidecar L from the injected
  frequency, so their theory matches are circular by construction. Say so, and say that they
  test the pipeline, not a real bar.
- **Two identical bars** predict identical modes. barlab treats these twins as one prediction
  (`bar` reads "A,B"), not as rivals.
- **L unknown:** `implied_L_mm` lists the bar length each hypothesis would need. The
  hypothesis that agrees with the real bar length (within a few %) is the likely family, so
  ask the user to measure L (and say which values would confirm which family).

## Uncertainty and drift

- Frequency is `f_hz` ± `f_std_hz` (spread between hits); `f_sem_hz` = spread / sqrt(n) is the
  uncertainty of the mean. Per-hit frequencies come from phase fits, so the PSD resolution
  does not limit them.
- **Sample clock:** typically ±20–100 ppm for USB recorders, i.e. ±1–4 Hz at 40 kHz. This
  matters when comparing devices, not files from the same device.
- **Temperature (approx.):** for aluminium, df/f ≈ −2.4×10⁻⁴ per K (E softens; thermal
  expansion adds a little), about −10 Hz/K at 40 kHz. A few hundred ppm between sessions can
  be a few kelvin.
- **`barlab compare` |Δf|/σ** uses standard errors. Above about 3 is a significant change: ask
  whether temperature, mounting, striking or the bar itself changed.

## Measured vs inferred: wording

- **Measured:** `f_hz`, `f_std_hz`, `f_sem_hz`, `q`/`q_lower`, `tau_ms`, `snr_db`,
  `above_floor_db`, hit counts, clipping.
- **Inferred:** the class, "this is a bar mode", the mode family, the theory match, the implied
  L, and "steady tone, so an external source".
- Example: "Measured: 38 000.4 ± 0.3 Hz over 8 hits, Q 504 ± 20. Inferred: the longitudinal
  n = 1 mode of the 66 mm bar (theory +0.00 %, unambiguous)."

## Recommendation checklist (pick what this file showed)

- **Sample rate:** fs >= 192 kHz (384 kHz is ideal). 44.1/48 kHz cannot capture ultrasonic
  modes.
- **Gain:** hits should peak at or below about −6 dBFS, with no clipping in the ringdown.
- **Noise reference:** a quiet lead-in of >= 0.5 s. If environment lines appear, find and
  switch off other ultrasonic sources (switch-mode supplies, displays, ultrasonic devices).
- **Striking:** one clean contact in a consistent spot, >= 5 hits per file. Let the bar decay
  (about 5 τ) before the next hit, and don't damp it with a hand.
- **Support:** at the nodes, on soft supports. Otherwise note "clamped" or "hand-held".
- **Mic:** fixed distance (about 5–20 cm) and orientation, model noted. Amplitudes are not
  calibrated.
- **Notes per recording:** bar L (±0.1 mm), d, alloy, support, temperature, mic and distance,
  written into `data/meta/<stem>.toml`.
