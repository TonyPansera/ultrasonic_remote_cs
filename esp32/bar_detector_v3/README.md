# bar_detector_v3: which bar rang, 38 kHz or 47 kHz? (prototype 3, overtone-checked)

Prototype 3 runs on prototype 2's hardware. That means the same LilyGO T-Display (MAC
`30:c6:f7:25:ad:f0`) and the same SPH0641 PDM mic on GPIO22/21 at 192 kS/s. Wiring, the PDM-to-PCM
decimation and flashing are described in [`../bar_detector_v2/README.md`](../bar_detector_v2/README.md).
Prototype 2 stays in `../bar_detector_v2/` unchanged.

What v3 adds, after the prototype was changed on 2026-10-30:
- **Re-measured bands and narrower guards.** The 47 bar's own overtone had become as loud as its
  fundamental. It fell in band B's guards and made strikes go missing.
- **A strike spectrum after every strike**, with fixed "watched" lines measured whether or not they stand
  out: both bars' lines, background lines and theory candidates.
- **A bar decision.** When the detector cannot tell (the grey `38+47?` tile), each bar's **overtone**
  settles it, but only if its frequency ratio to the fundamental is that bar's. Without that check,
  the line could come from the other bar or from the background.

The measurements behind every number are in [`measurements/REPORT.md`](measurements/REPORT.md), with the
raw logs in `measurements/2026-10-30/`.

| Strike | Screen |
|---|---|
| ~38 kHz bar | **blue** tile, `38 kHz`, measured frequency |
| ~47 kHz bar | **orange** tile, `47 kHz`, measured frequency |
| the detector says both, and the bars' lines cannot settle it | **grey** tile, `38+47?` (never forced) |
| nothing | `LISTENING`, with one live SNR meter per band |

## Status (2026-10-30)

- **Final firmware on the board:**
  - **Strikes:** 40 of 40 strikes right in a mixed sequence, with no grey tile, no wrong colour and
    no miss.
  - **Background:** 2 min of quiet gave no event.
  - **Load and memory:** `load_pct` 65 and `heap_free` ~138 KB.
- **The bars,** measured with the other bar held:

  | Bar | Fundamental (band) | Overtone | Overtone / fundamental |
  |---|---|---|---|
  | 38 | 38 418 Hz (band A: 38 500 ±250 Hz) | 33 917 Hz | 0.88313 ±0.3 % |
  | 47 | 47 110 Hz (band B: 47 110 ±250 Hz) | 45 386 Hz | 0.96331 ±0.3 % |

  - No 2f harmonic shows on this mic: the centre support damps even longitudinal modes, and the mic's
    own noise rises above 75 kHz.
  - Measured against uniform-cylinder theory with L = 63 / 50 mm, the lines are 4-11 % low, because of
    the centre hole and the rod. The ratios used are therefore the measured ones.
- **What tells the struck bar from the one ringing in sympathy:** what still rings 40-60 ms after the
  click. The struck bar's fundamental loses a median of 3 dB in 40 ms; the other bar's loses 19 dB.
  - With the overtone counted, the struck bar leads by ≥ 19 dB on all 88 labelled strikes (median
    41 dB).
  - The one grey tile of the day (detector margin 4.9 dB) is settled correctly.
- **Not yet verified:**
  - the ~40 kHz background source, which was off all day;
  - two weak extra 38 detections 0.4 s before a strike (the key mechanism?).

  See the report's open points.

## How it decides

1. **The detector** (`lib/tone_detector`, byte-identical to prototype 1's) tells A, B or BOTH from its
   two bands, as before. Its settings are in [`lib/device_config/DeviceConfig.h`](lib/device_config/DeviceConfig.h).
2. **The strike spectrum** (`lib/strike_spectrum`) runs on every buffer. The ultrasonic energy jump of a
   click starts a capture, and so does a detector band rising over its threshold (soft clicks barely
   move the energy). A band onset also restarts a capture that a knock started two or more chunks
   earlier. For each **watched line**, the capture measures:
   - **early:** the 2-31 ms after the click;
   - **late:** 40 ms later;
   - **before:** the 21 ms just ahead of the click. A line that did not rise is background.
3. **The bar decision** (`lib/bar_decision`) pairs each Detect with its capture (the capture started 0-36
   ms before the event):
   - **A or B from the detector:** kept. A colour is never changed.
   - **BOTH:**
     - each bar's evidence is the late level of its counted lines;
     - the fundamental counts if it rose ≥ 10 dB at the strike and stands ≥ 6 dB over the capture's
       floor;
     - the overtone counts too only if, in addition, its frequency over the fundamental's is the bar's
       ratio ±0.3 %;
     - a lead of ≥ 10 dB settles it. Otherwise it stays BOTH, as it does without a capture (after a
       200 ms wait).
   - **The tile:** it appears when the decision is made, ~80 ms after the strike instead of ~50. Its End
     redraw keeps the decided band.

## Serial protocol (921600 baud; opening the port resets the board, wait for `READY`)

As prototype 2 (`info`, `lvl`, `thr`, `band`, `pdm`, `reset`), plus:

| Command | Reply |
|---|---|
| `spec <on\|off>` | `SPEC on=..`. While on, after each capture: `SPEC t=.. trig_db=.. forced=.. ext=.. floor_dbfs=.. peaks=.. lines=..`, the `SPEC_PEAK` lines (20-62 kHz, strongest first), then one `SPEC_LINE name=.. target=.. f=.. early_dbfs=.. late_dbfs=.. drop_db=.. before_dbfs=..` per watched line. The spectrum always runs: this only prints it |
| `spec now` | a capture without a click (background survey), printed even with `spec off` |
| `watch` | `WATCH n=.. name=Hz:tol ...` |
| `watch add <Hz> [tol]`, `watch clear`, `watch default` | change the watched lines (≤ 12). The decision looks its four lines up by frequency, so after `watch clear` it falls back on the detector |

Unsolicited lines:
- `READY`;
- `EVT phase=detect|end band=.. t=.. ...`, the detector, as before plus `t`, the event start in seconds;
- `DEC t=.. band=.. detector=.. why=detector|lines|close|no_spectrum spec=.. ev_a=.. ev_b=.. margin_db=..
  fund_a=.. fund_b=.. ov_a=.. ov_b=.. ratio_a=.. ratio_b=..`, the decision of each strike. The tile and
  the hit counters follow `band`.

## Measuring (board attached)

```bash
cd ~/signal_analysis/esp32/bar_detector_v3
~/.local/share/uv/tools/platformio/bin/python tools/session.py --label S1_A_damped --seconds 75
~/.local/share/uv/tools/platformio/bin/python tools/session.py --label S0_bg --seconds 120 --at "10:spec now"
cd ~/signal_analysis
uv run python esp32/bar_detector_v3/tools/analyze_sessions.py --strikes --peaks
uv run python esp32/bar_detector_v3/tools/analyze_sessions.py --export-vectors esp32/bar_detector_v3/test/test_decision/strikes_2026_10_30.h
```

`session.py`:
- waits for `READY`, then sends `info`, `watch` and `spec on`;
- logs every line with the host time to `measurements/<date>/<label>.log`.

`analyze_sessions.py` pairs each capture with its EVT lines. The labelled sessions it knows (which bar
was struck) are set in its `TRUTH` and `S5_SEQUENCE`. When a bar changes:
1. hold the other bar and strike this one;
2. find its lines (`--peaks`) and their ratio;
3. update `kWatch`, `detectorConfig` and `decisionConfig` in DeviceConfig.h;
4. re-export the vectors and run the tests.

## Tests (on the PC, no board)

```bash
pio test -e native
diff -r ../bar_detector/lib/tone_detector lib/tone_detector   # must print nothing
```

| Suite | What it covers |
|---|---|
| `test/test_decision` | the 88 real strikes of 2026-10-30, each with its true bar: all right, and the lines alone never name the wrong bar. Then the edge rules: a colour is never changed, no capture, an overtone off the ratio, a steady line on a bar's line, `watch clear`, and the pairing order |
| `test/test_spectrum` | watched lines (weak ones, past 62 kHz, absent), the before window against a steady line, forced captures, the band-onset trigger, and its restart past a knock and a holdoff |
| `test/test_pdm` | the decimator and the whole chain (PDM bits to decision). Includes: a 47 strike whose overtone is louder than its fundamental; a 38 strike at its lowest measured frequency; BOTH settled for each bar; a continuous 40 kHz line; a steady line on the 47 overtone |
| `test/test_detector` | prototype 1's detector tests, unchanged |
