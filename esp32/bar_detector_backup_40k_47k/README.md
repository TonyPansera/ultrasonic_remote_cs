# bar_detector: which bar rang, 40 kHz or 47 kHz?

> **Backup, 2026-10-16.** Copy of `../bar_detector` taken just before band A moved from the 40.7 kHz bar to the ~38 kHz bar that replaced it. Band A = 40 741.9 Hz, band B = 42 782 Hz. To flash it again: `pio run -t upload` in this folder.

Firmware for the LilyGO T-Display. It listens to GPIO32 at ~249 kS/s and recognises the two bars of
the ultrasonic remote by their ringing, then shows a different screen for each:

| Strike | Screen |
|---|---|
| ~40 kHz bar (40.74 kHz) | **blue** tile, `40 kHz`, measured frequency |
| ~47 kHz bar (detected by its 42.78 kHz line) | **orange** tile, `47 kHz`, measured frequency |
| both bands within 10 dB | **grey** tile, `40+47?` (ambiguous; never forced) |
| nothing | `LISTENING`, with one live SNR meter per band |

Where the band centres come from:
- **Band A, 40 741.9 Hz:** barlab's analysis of `data/raw/rec5.wav`. See
  [`results/reports/2026-10-09_batch-summary.md`](../../results/reports/2026-10-09_batch-summary.md).
- **Band B, 42 782 Hz:** measured on the board on 2026-10-16. That day the ~51 kHz bar (rec4) was
  replaced by a bar that the bat detector shows at ~43 and ~47 kHz. The GPIO32 receiver sees the
  43 kHz line much better than the 47 kHz one (`band b` scan, 30 s of strikes each):

  | Band B centre | Strikes detected | Measured frequency | Peak SNR |
  |---|---|---|---|
  | 42 760 Hz | 9 of 11 | 42 747-42 802 Hz (median 42 782) | 60-68 dB |
  | 47 000 Hz | 1 of 5 | 46 735-46 802 Hz | 34-53 dB, guards close behind |

**Status (2026-10-16):**
- It runs on the board: `load_pct` ~43, no watchdog resets.
- The 40.7 kHz bar is detected as A, and the ~47 kHz bar as B, through its 42.78 kHz line.
- Every strike of either bar drives the receiver past both ADC limits (`CLIP`). The detection
  still works.

## How it detects

The detector is in `lib/tone_detector/` (plain C++, also compiled on the PC for the tests).
- **Blocks:** every 2048 samples (8.2 ms), it removes the DC, applies a Hann window and measures
  generalised Goertzel bins (122 Hz apart):
  - across each band (centre ± 500 Hz);
  - on guard bands 1-2 kHz either side.
- **Noise baselines:** every bin has its own noise baseline. Steady lines get absorbed instead of
  detected:
  - environment lines;
  - the board's own ~41 kHz supply line;
  - a constant tone.
- **Trigger:** a band triggers when its best bin is ≥ 15 dB above its baseline for 2 blocks, and
  stands ≥ 6 dB clear of its guard bins. That rejects:
  - the broadband click of the impact;
  - the 38/42 kHz FSK remote next to the 40 kHz band.
- **Confirmation:** the strike only counts once the line **rings down like a bar**. That means a
  4 dB drop from the peak, with the last 5 blocks falling on a straight line in dB.
  - A tone that is switched on or off is not reported.
  - Neither is the FSK remote. Its 42 kHz tone sits 0.8 kHz below band B, and its clipped
    harmonics alias across the spectrum, but its bursts never decay like a bar.
- **Decision:** the band with the higher peak SNR wins if it leads by ≥ 10 dB.
- **The bands are only 2 kHz apart,** so each band's guard bins hold the other bar's line:
  - a strike of one bar makes the other band "unclean", so the 40 kHz bar never shows as 47 or the
    reverse;
  - if both bars ring at once, neither band is clean and nothing is reported (no `40+47?` tile).

Typical delay from strike to screen: 50-100 ms.

On the PC, with simulated ADC noise, it is reliable when the ringing starts **≥ ~30 dB** above the
noise in one bin. At ~22 dB it catches about 85 % of strikes. For reference, the bat-detector
recordings reach 67 dB (rec4) and 51 dB (rec5).

## Wiring and sensor

The analog signal goes into **GPIO32**, the same pin and ADC path as `ultrasonic_probe`.

1. **Now: the receiver already on GPIO32.** It favours 40-43 kHz:
   - the ~47 kHz bar's 46.8 kHz line arrives ~13 dB weaker than its 42.8 kHz line, which is why band
     B tracks 42.8 kHz;
   - its output idles at ~2.17 V and is driven (the internal pulls do not move it).

   If `DC` drops to 0.14 V with a permanent `CLIP`, the receiver has lost its supply: check its
   VCC/GND wires. This happened twice on 2026-10-16, when the wires got bumped during strikes.
2. **Later: a broadband ultrasonic mic** (MEMS or electret, flat to > 60 kHz) with a preamp.

For both:
- **Keep the signal inside the ADC range,** 0.14-3.15 V. The pin must stay within 0-3.3 V (it is not
  5 V tolerant).
- **AC-couple to mid-rail:** 100 nF in series, then 100 kΩ to 3.3 V and 100 kΩ to GND on the
  GPIO32 side. The `LISTENING` screen shows `DC` (aim for ~1.65 V) and `Vpp`.
  - Orange `CLIP` means the signal hits a rail: lower the gain.
  - Clipping does not break the detection, but its harmonics fold back into the band.
- **Low-pass at ~100 kHz** in front of the ADC (an RC is enough for a mic preamp). The ESP32
  samples at 249 kS/s with no anti-alias filter, so anything between 124.5 and 249 kHz folds back
  (198 kHz would land on 51 kHz).

## Screens

- **LISTENING:**
  - **Status:** `LEARN` (first second after boot or a reset: keep quiet), then `READY`, or `CLIP`.
  - **Meters:** one per band; each shows the SNR of the best bin in dB. The white tick is the
    threshold, and the bar turns green above it.
  - **Bottom lines:**
    - hit counters (`40k`, `47k`, `?` = ambiguous);
    - input DC level, Vpp over the last 100 ms, and the measured sample rate.
- **Detection tile:**
  - **Content:** the colour and big name of the bar, its measured frequency, the peak SNR, `ring`
    (how long it rang above the release level) and the hit number.
  - **Timing:** it appears when the strike is confirmed and is redrawn when the ringing ends. It
    disappears 2 s later.

**Right button (GPIO35):** reset the baselines and counters, then learn for 1 s. Use it after moving
the sensor or changing the gain.

## First run

These steps need the board attached.

```bash
cd ~/signal_analysis/esp32/bar_detector
pio run -t upload                 # flash (board on /dev/ttyACM0, see ../CLAUDE.md for usbipd)
```

Then, over serial at 921600 baud (`pio device monitor`, or the Python snippet in `../CLAUDE.md`;
opening the port resets the board, so wait for `READY`):

1. **Check the setup:** `info` prints:
   - `rate_meas`: expect ~249000;
   - `dc_mv`: expect ~1650 when AC-coupled;
   - `bad_words`: expect 0;
   - `load_pct`: the detector's CPU time per DMA buffer, averaged. It must stay well under 100
     (~43 on the board); above 100 the task watchdog resets the board every ~12 s.
   - `load_max_pct`: the worst buffer. A 2048-sample block is processed every second 1024-sample
     buffer, so up to ~200 is fine (~93 on the board).
2. **Calibrate:** `lvl on` streams the per-band SNR. Strike each bar a few times and note the peak
   `snr_a` / `snr_b`:
   - the struck band should peak well above 15 dB;
   - the other band should stay ≥ 10 dB lower.
3. **Adjust if needed:**
   - `thr <dB>` sets the threshold;
   - `band a <Hz>` / `band b <Hz>` move a band if your bars ring elsewhere. The `EVT` lines print
     the measured `f_a` / `f_b`.

Then make the change permanent in `Config` (`lib/tone_detector/ToneDetector.h`) and run the tests.

## Serial protocol (921600 baud, one command per line)

| Command | Reply |
|---|---|
| `info` | `INFO rate_meas=.. block=.. bin_hz=.. band_a=.. band_b=.. tol_hz=.. thr_db=.. hits_a=.. hits_b=.. hits_both=.. dc_mv=.. bad_words=.. load_pct=.. load_max_pct=..` |
| `lvl <on\|off>` | `LVL on=..`, then ~20 lines/s: `LVL snr_a=.. snr_b=.. guard_a=.. guard_b=.. level_a=.. level_b=.. dc_mv=.. learning=.. active=.. clip=..` |
| `thr <dB>` | `THR db=..` (6-40 dB; relearns) |
| `band <a\|b> <Hz>` | `BAND a=.. b=..` (relearns) |
| `reset` | `RESET ok=1` (same as the right button) |

Unsolicited lines:
- `READY` after boot.
- Every recognised strike: `EVT phase=detect band=A|B|BOTH f_a=.. f_b=.. snr_a=.. snr_b=..
  margin_db=.. dur_ms=.. clip=..`.
- When its ringing stops: `EVT phase=end ... confirmed=1 steady=0`.
- Onsets that never rang down like a bar: an `EVT phase=end ... confirmed=0` line only, nothing on
  screen. `steady=1` means a tone that stayed on for 3 s; the baselines are then relearned with it.

## Tests (on the PC, no board)

```bash
pio test -e native
```

`test/test_detector/test_main.cpp` covers:
- **Synthetic strikes at 249 kS/s:**
  - 40.7 kHz → A, 42.78 kHz → B, also when clipped at both ADC limits as on the board;
  - a weak strike, and two strikes in a row;
  - both bars at once → neither A nor B.
- **Things that must not be reported:**
  - noise;
  - clicks alone;
  - a steady tone;
  - a tone switched on, or switched on and off;
  - the clipped 38/42 kHz FSK remote.
- **The real recordings:**
  - `rec5.wav` → A, with the default bands;
  - `rec4.wav` → B, with band B moved back to 51 014.2 Hz. That bar has been replaced, but it is
    still the only real ringdown recording for band B.

  Both are:
  - read at their native 384 kHz, without resampling;
  - skipped when `data/raw/` is missing.

  On 2026-10-09 the detector found:
  - **rec4:** 51 017.5 Hz at 67 dB, ringing for 0.96 s;
  - **rec5:** 40 745.3 Hz at 51 dB, with the 51 kHz band 30 dB lower.

  On 2026-10-16, with band B at 42.78 kHz, rec5 still gives A only (40 745.3 Hz at 50.9 dB).

Every change to `lib/tone_detector/` needs these tests to pass, plus a new test for the behaviour it
changes.
