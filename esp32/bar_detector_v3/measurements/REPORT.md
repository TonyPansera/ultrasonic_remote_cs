# Board measurements, 2026-10-30: the 38 and 47 bars after the prototype change

These sessions were recorded with prototype 2's board (SPH0641 PDM mic, 192 kS/s), running the v3
firmware, while the user struck the bars. The logs are in [`2026-10-30/`](2026-10-30); one line per
board message, prefixed with the host time. The numbers below come from
`uv run python esp32/bar_detector_v3/tools/analyze_sessions.py [--strikes] [--peaks]`. The bat
detector recorded every session in parallel, as `data/raw/2026-10-30_<session>.wav`; that analysis
(barlab) is not in this report yet.

## Sessions

| Session | What the user did | Firmware | Strikes | Detector result |
|---|---|---|---|---|
| S0_background | 2 min quiet, 6 forced captures | v3, first build | 0 | no event |
| S1_A_damped | 38 bar ×15, 47 bar held | first build | 15 | 15 A, but only 9 captures (the click trigger missed soft strikes) |
| S1b_A_damped | 38 bar, 47 bar held | + band-onset trigger | 12 | 12 A |
| S2_B_damped | 47 bar, 38 bar held | + onset restart | ~16 | 14 B, **2 missed**, several 64-144 ms late |
| S3_A_free | 38 bar ×25 | + guard fix, re-centred bands | 25 | 25 A |
| S4_B_free | 47 bar ×25 | same | 24 | 23 B, 1 soft strike missed (onset seen, no clean ringdown) |
| S5_mixed | 20 in a given sequence | same | 20 | 19 right, **1 grey** (#17, a 38: margin 4.9 dB); 1 extra weak A 0.4 s before #12 |
| S6b_both | both bars at once ×5 | same | 5 | B, B, A, B, A at Detect; 2 tiles changed band at End (the second bar came a few ms later) |
| S7_verify | 40 in a given sequence | **final v3** (decision) | 40 | **40 right**, 0 grey, 0 wrong, 0 missed; 1 extra weak A 0.4 s before #30 |
| S8_background | 2 min quiet, 4 forced captures | final v3 | 0 | no event |

On the final firmware, `info` reads `load_pct` 65, `load_max_pct` 293 (the spectrum's FFTs, absorbed by
the 64 ms DMA queue) and `heap_free` 137 576.

The ~38/40 kHz background source the user reported was **off all day**. Every watched background line
(38.0, ~40, 42.9 kHz) sat at the noise floor in S0 and S8.

## Each bar's lines (detected strikes, S1b-S5: 47 of the 38 bar, 41 of the 47 bar)

| Bar | Line | Frequency (median [min..max]) | Level re fundamental | Ratio to fundamental |
|---|---|---|---|---|
| 38 | A0, fundamental (band A) | 38 418 Hz [38 332..38 855] | 0 | 1 |
| 38 | A1, overtone | 33 917 Hz [33 577..34 249] | -0.5 dB [-23..+21] | **0.88313**, 5-95 % [0.87972..0.88449]; 41/47 within ±0.3 % |
| 47 | B0, fundamental (band B) | 47 110 Hz [47 045..47 305] | 0 | 1 |
| 47 | B1, overtone | 45 386 Hz [45 190..45 501] | -5.2 dB [-28..+10] | **0.96331**, 5-95 % [0.96026..0.96454]; 38/41 within ±0.3 % |

- **The 38 bar moved.** It now rings ~200 Hz lower than on 2026-10-23 (38 596 Hz then).
- **The 47 bar's overtone got much louder.** On 2026-10-23 it was 15-30 dB under the fundamental;
  now it is level with it, or up to 10-14 dB above.
- **The 38 bar's overtone is often louder than its fundamental.**
- **The ratio is the bar's signature.** It depends only on the bar's geometry and material, so
  temperature, which shifts both lines together, leaves it alone. When the *other* bar is struck, these
  lines pass the ratio check in only 3 of 88 strikes (they rose and stood over the floor in those 3).
- **No 2f harmonic was seen:**
  - The ~1.97f windows (76.3 and 92.8 kHz) stayed at the mic's own noise (-74 / -70 dBFS) during
    strikes. That fits a centre support, which sits on the antinode of the even longitudinal modes.
  - The 3f fold-back window (A3x, ~79.5 kHz) shows nothing significant either.
  - 3f itself (~112 and ~137 kHz) is above the board's 96 kHz Nyquist: the bat recordings can check it.
- **Other lines** were found by the peak search but are not watched:
  - 38 bar: 33.10 kHz (strong in S1), 23.2-23.9 kHz and 27.0-27.3 kHz;
  - 47 bar: 20.3 kHz and 24.4 kHz;
  - both bars: 22.0-22.1 kHz, perhaps the support rod.

  They are not used.

### Against theory (uniform 16 mm aluminium cylinders, `barlab theory --L 63mm|50mm --d 16mm`)

| Bar | Theory long1 | Theory flex2 | Theory flex2/long1 | Measured | Measured ratio |
|---|---|---|---|---|---|
| 38, 63 mm | 39 948 Hz | 36 179 Hz | 0.906 | 38 418 / 33 917 Hz (-3.8 % / -6.3 %) | 0.883 |
| 47, 50 mm | 50 208 Hz | 50 921 Hz | 1.014 | 47 110 / 45 386 Hz (-6.2 % / -10.9 %) | 0.963 |

- **Bar 38:** its pair matches long1 + flex2.
- **Bar 47:** long1 and flex2 are nearly degenerate in theory. Its measured pair is 3.7 % apart, and
  which line is which mode is not settled.
- **Why the lines are lower:** the centre hole and the support rod lower the lines by 4-11 % against a
  plain cylinder. Theory therefore names the families but cannot fix the frequencies or the ratios.
- **What the firmware checks:** the **measured** ratios.

## What tells the struck bar from the one ringing in sympathy

Every strike also rings the other bar. The candidate features, taken on the same 88 labelled strikes
(struck minus other):

| Feature | Lowest | 5 % | Median | Struck ≤ other |
|---|---|---|---|---|
| Detector SNR margin (legacy decision) | 4.9 dB | 29.1 | 46.9 | 0 (1 under the 10 dB margin: the grey tile) |
| Early (2-31 ms) fundamental level | -2.8 dB | 4.0 | 24.1 | 2 |
| Late (40-61 ms) fundamental level | 9.0 dB* | 20.3 | 39.6 | 0* |
| **Late level of the bar's counted lines** (fundamental + overtone at the bar's ratio) | **19.1 dB*** | 22.0 | 41.1 | **0** |

\* Not counting one S1b capture that started 40 ms before its strike. The fix was flashed the same
day; the firmware no longer pairs such a capture.

- **The struck bar keeps ringing.** Its fundamental loses a median of 3.4 dB over the 40 ms between
  the windows (90 %: 13.8 dB).
- **The sympathetic bar collapses.** Its fundamental loses a median of 18.7 dB.
- **What the overtone adds:** counting it lifts the worst case from 9 to 19 dB.

The grey strike (S5 #17) was a 38 strike with a detector margin of 4.9 dB:

| Bar | Fundamental | Overtone | Late evidence |
|---|---|---|---|
| 38 | rings on (drop 0 dB) | present, at the ratio | -59.8 dBFS |
| 47 | collapses by 33 dB | collapses by 30 dB | -92.1 dBFS |

## Detection fixes made during the sessions (all in `lib/device_config`, `lib/strike_spectrum`)

1. **Band B's guards held the 47 bar's own overtone.** At 45.37 kHz it sat 1.73 kHz below band B,
   inside the 1-2 kHz guards. As loud as the fundamental now, it made strikes "not narrowband":
   2 of 16 were missed in S2 and others came 64-144 ms late.
   - **Fix:** guards at 1.0-1.375 kHz.
   - **Test:** `test_chain_47k_strike_with_louder_second_line_gives_B`. It fails with the old guards.
2. **Band A re-centred** from 38 618 Hz to 38 500 Hz, at ±200 Hz (bins at 38 250-38 750 Hz).
   - It holds the strikes (38 332-38 563 Hz from the detector).
   - It keeps the bottom bin 250 Hz off the 38.00 kHz background source. At 38 420 Hz, the
     reverberant-remote test failed.
3. **Band B re-centred** from 47 088 Hz to 47 110 Hz.
4. **The strike spectrum missed soft clicks** (6 of 15 in S1): the mic's noise near Nyquist dominates
   its second-difference trigger. A band rising over the detection threshold now also starts the
   capture.
5. **A knock just before a strike** (handling, the mechanism) took the strike's capture. The band onset
   now restarts a capture that is two or more chunks old, and cuts through the 300 ms holdoff.

## Collision check (watch windows and detector bands against everything else known)

| Window | Range | Nearest other line | Clearance |
|---|---|---|---|
| Band A bins | 38 250-38 750 Hz | background 38.00 kHz | 250 Hz (= Hann main lobe half-width) |
| A0 watch | 38 200-38 800 Hz | background 38.00 kHz | 200 Hz; such a line is steady, so it fails the rise test |
| Band A upper guard | 39 500-39 875 Hz | the reported ~40 kHz background | **~125 Hz if it is at 40.0 kHz: not verified** (the source was off) |
| A1 watch | 33 400-34 400 Hz | 38 bar's own 33.10 kHz line | 300 Hz (same bar) |
| B1 watch / B lower guard | 44 970-45 770 / 45 735-46 110 Hz | B1 45.39 kHz vs guard | 345 Hz |
| B0 watch | 46 810-47 410 Hz | B1 45.39 kHz | 1.4 kHz |
| 2f windows | 74.8-77.8 / 91.3-94.3 kHz | 2 × 38.00 = 76.0 kHz (background harmonic) | inside A2: A2 is not used by the decision |

No bar line falls within 1.3 kHz of the other bar's lines.

## Open points

- **The ~40 kHz background was never on.** If it can be switched on, check two things:
  1. `lvl on`: `guard_a` should not rise with it. If it does, band A misses strikes, and the fix is to
     move A's guards (DeviceConfig).
  2. A few strikes should still give A or B.
- **Extra weak 38 detections, twice** (S5 and S7): an A at 26-27 dB SNR **0.4 s before** a real strike,
  shown as a short blue tile before the right one. It looks like something in the key mechanism
  touching the 38 bar before the hammer lands.
- **The bat recordings** (`data/raw/2026-10-30_*.wav`) are to be analysed with barlab:
  - the lines above 96 kHz (3f), for the mode families;
  - a replay test of the decision at 384 kS/s.
