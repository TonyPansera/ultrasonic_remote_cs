# bar_detector_v4: the Space Command TV

Prototype 4 turns the remote's bars into a TV remote, as the bars' original remote did:

| Strike | TV |
|---|---|
| 38 kHz bar | power on / off. The picture collapses to a line, then a dot, then standby (a red LED in the corner); power-on opens it again |
| 47 kHz bar | next channel, through a burst of TV snow (ignored while off) |
| both bars at once | an explosion: white flash, fireball, shockwave, sparks, smoke, a shaking "BOOM!". It works even in standby |

**Channels:**

| # | Channel | What it shows |
|---|---|---|
| 1 | TEST CARD | colour bars, a test circle with a sweeping radar beam, the uptime clock |
| 2 | SPACE | a 3D starfield at warp speed past a ringed planet |
| 3 | PLASMA | demoscene plasma (four sine fields, a palette that rotates) |
| 4 | FIREPLACE | demoscene fire burning on two logs |
| 5 | ULTRASONIC | the detector live: both bands' SNR scrolling over 12 s, the detection threshold, each decided strike marked (38 / 47 / !!) and counted |

The hardware is prototype 2's board and mic. Detection is prototype 3's, unchanged:
- **Libraries:** `lib/tone_detector`, `lib/pdm_decimator`, `lib/bar_decision` and `lib/device_config` are
  as in v3.
- **Strike spectrum** (`lib/strike_spectrum`): it computes the same numbers as in v3 and passes the same
  tests, but in less RAM (see Memory).
- **Details:** see [`../bar_detector_v3/README.md`](../bar_detector_v3/README.md).

Prototype 3 stays in `../bar_detector_v3/`, with its diagnostic tiles.

## From strikes to commands (`lib/tv_remote`, host-tested)

Each decided strike waits a little before it acts. These rules come from the 2026-10-30 sessions
(`../bar_detector_v3/measurements/`):
- **Hold, 300 ms.** A person never strikes both bars at exactly the same time, so the detector decides on
  the first bar. If the other band then rings clean above **44 dB** within the hold, the strike becomes an
  explosion. The weaker band reached 48-60 dB on all 5 "both at once" strikes, and at most 41 dB in
  sympathy on 111 single strikes.
- **Weak strikes, under 40 dB, wait 500 ms.** A stronger strike in that time replaces them. Twice, a
  weak 38 came 0.4 s *before* a real strike.
- **Refractory, 800 ms.** A weak strike within 800 ms *after* a command is dropped (once, a weak echo
  0.5 s after a strong 38).

Replayed on the sessions S3, S4, S5, S6b and S7 (116 detections), these rules give exactly the commands
that were struck (`test/test_tv`).

Board check, 2026-10-30: power, channel and the explosion all fired from real strikes, including two
"both at once" strikes (peaks 75/64 and 74/79 dB).

## Buttons and serial

- **Left button (GPIO0):** power. Hold it 1 s for an explosion.
- **Right button (GPIO35):** next channel. Hold it 1 s to make the detector relearn the room's noise
  (keep quiet for 1 s).

Serial at 921600 baud. Everything from v3 (`info`, `lvl`, `thr`, `band`, `pdm`, `spec`, `watch`,
`reset`, and the `EVT` / `DEC` lines), plus:

| Command / line | Meaning |
|---|---|
| `tv`, `tv power`, `tv ch`, `tv boom` | the TV state, after the command if one is given: `TV on=.. ch=.. name=..` |
| `shot [y0 y1]` | the screen as RGB565 hex rows (`SHOT ...`, `SHOT_ROW y hex`, `SHOT end`). This takes ~5 s and paces the rows for the USB/IP link; the loop is blocked meanwhile |
| `TV cmd=power\|channel\|boom on=.. ch=.. name=.. peaks=a/b` | unsolicited, one per command, with the clean peaks that decided it |
| `HEAP stage=mic\|screen\|spectrum ...` | at boot: free RAM and the largest free block after each allocation |

Opening the serial port resets the board. To drive it without losing the TV's state, keep one connection
open:

```bash
cd ~/signal_analysis/esp32/bar_detector_v4
~/.local/share/uv/tools/platformio/bin/python tools/console.py --log /tmp/tv.log --fifo /tmp/tv.cmd &
echo "tv power" > /tmp/tv.cmd
echo "shot" > /tmp/tv.cmd
cd ~/signal_analysis && uv run python esp32/bar_detector_v4/tools/render_shot.py /tmp/tv.log /tmp/shot.png
```

## Memory

The screen is one 240 × 135 16-bit sprite: 64.8 KB in one piece. The ESP32 has no PSRAM, so the
allocation order matters (`setup()`):
1. **The mic's I2S DMA buffers.** When the sprite came first, the I2S driver failed with "I2S0 rx DMA
   buffer malloc failed".
2. **The sprite and the fire buffer.**
3. **The strike spectrum.** To fit, it now keeps only its two FFT windows instead of the whole 71 ms
   span, and keeps its spectra in int16 centi-dB instead of float: about 83 KB instead of 105 KB, with
   the same results, and its tests pass unchanged.

At boot: 181 KB free after the mic, 113 KB after the screen, 29 KB after the spectrum. The `HEAP`
lines show this.

## Tests (on the PC, no board)

```bash
pio test -e native
diff -r ../bar_detector/lib/tone_detector lib/tone_detector   # must print nothing
```

v3's suites (`test_decision`, `test_spectrum`, `test_pdm`, `test_detector`) plus `test/test_tv`:
- the session replays;
- the hold, the weak-strike, echo and explosion rules;
- the TV state: power toggles, channel ignored while off, wrapping, boom.

The screen code (`src/TvScreen.*`) needs the display, so it is checked on the board with `shot`.
