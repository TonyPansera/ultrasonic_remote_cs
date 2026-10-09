# ultrasonic_probe: GPIO32 ultrasonic remote signal checker

Firmware and a host-side analysis script that check the ultrasonic remote's signal on GPIO32 of the T-Display. The remote sends two tones, **38 kHz and 42 kHz (FSK)**, in bursts.

- **Latest result:** [results/REPORT.md](results/REPORT.md), with graphs in `results/` and raw captures in `results/data/`.
- **2026-10-02 (after the GND fix): PASS with warnings.**
  - 38 kHz measured at 38 003.7 Hz and 42 kHz at 41 993.5 Hz, both within ±6 Hz of nominal and about 55 dB above the noise.
  - Each tone is sent as 50 bursts/s: ~19 ms on, ~1 ms off.
  - The receiver output idles at ~2.18 V. Its bursts are larger than the ADC range, so the peaks clip at both ends; the frequencies are still exact. See the report's range advice.
- **Earlier run (before the GND fix): FAIL.** GPIO32 was pinned above full scale.

## How it works

**Firmware** (`src/main.cpp`):
- Samples GPIO32 (ADC1 channel 4) through the ESP32's I2S built-in-ADC DMA path, configured at 250 kHz by default. The true conversion rate is about **249 kS/s**, or 6.2 samples per 40 kHz period. `analogRead()` would manage only ~20 kS/s.
- At boot, before the ADC takes over the pin, it records two independent references:
  - a hardware edge count (PCNT) on GPIO32: four 250 ms gates. With bursts this gives edges per second, close to the tone frequency, and 0 for gates that fall in a silence;
  - an `analogRead()` average.
- The screen shows GPIO32 live, refreshed about every 200 ms (see [Screen](#screen)).

**Host script** (`tools/analyze_signal.py`) drives the firmware over serial and runs five steps:
1. **Connection test:** GPIO32 with no pull, the internal pull-down, then the internal pull-up. A driven line barely moves; a floating pin follows the pull.
2. **Sampler sweep:** finds the real ADC rate. The DMA stream repeats each conversion 1×/2×/4×/8× depending on the configured I2S rate.
3. **Long captures** (164 ms each): as wired, and with the pull-down if "as wired" is pinned to a rail.
4. **Trend:** 40 short captures over about 26 s. This shows which tone plays when.
5. **Interference controls:** GPIO33 (nothing connected, held at mid-rail) and GPIO32, each with the display awake and asleep. These separate board or supply noise from the real signal.

It then:
- groups the tones it finds and checks each against 38 / 42 kHz ±500 Hz;
- measures burst on/off timing per tone;
- reports where the signal leaves the ADC range.

It writes `results/`:
- figures `01`–`09` (`09_fsk.png` shows the tone over time and the burst pattern)
- `REPORT.md`, with the verdict and hardware checklist
- `summary.json`
- `data/*.npz`

Verdict rules (as wired):
- **PASS** needs both 38 kHz and 42 kHz found within ±500 Hz, each ≥ 15 dB above the local noise floor and ≥ 10 mV.
- **Warning** if more than 0.1 % of samples sit at an ADC limit.
- A strongest component outside both bands is reported only if it repeats; a single capture taken across a tone change is ignored.

## Screen

The top line shows the pin and a status:
- `OK 38k` / `OK 42k`: that tone is present (within 37.5–42.5 kHz).
- `38k CLIP` / `42k CLIP` (orange): tone present, but part of the signal leaves the ADC range.
- `NO SIGNAL`: under 150 mVpp, e.g. silence between transmissions (the line idles with ~50–70 mVpp of noise).
- `TOO HIGH` / `TOO LOW`: the input is stuck at or beyond the ADC's top or bottom limit.
- `OFF BAND`, `NO TONE`, `ADC ERR`.

The frequency is averaged over whole periods only, so the gaps between bursts don't bias it.

Below that: the measured frequency, the DC level (`DC >3.14V` means at or above full scale) and Vpp.

The scope always draws the real voltage. Red dashed lines mark the ADC limits (~0.14 V and ~3.15 V). **The right button (GPIO35) cycles the view:**

| View | Shows |
|---|---|
| `0-3.3V 240us` (default) | triggered waveform on a fixed 0–3.3 V scale, ~10 periods of 40 kHz; grid = 25 µs |
| `ZOOM 240us` | same waveform, auto-scaled to the signal (min 40 mV window), voltage labels top/bottom |
| `0-3.3V 16ms` | min/max envelope of the whole 16 ms capture, fixed scale; grid = 1 ms. Shows bursts (pings) and level changes |

A pin stuck at a rail appears as a flat line on the red "ADC max"/"ADC min" line, not at 0 V.

## Usage

```bash
cd ~/signal_analysis/esp32/ultrasonic_probe
pio run -t upload                      # flash the firmware (board attached as /dev/ttyACM0)
uv run tools/analyze_signal.py         # ~3 min: measure, plot, write results/
uv run tools/analyze_signal.py --trend 5 --captures 1   # quicker run
```

`uv` installs numpy, scipy, matplotlib and pyserial automatically; the dependencies are declared inside the script. Opening the serial port resets the board, so the script waits for `READY` first.

For a quick look without the script, use `pio device monitor` (921600 baud). The firmware prints one `LIVE f=... vpp_mv=... dc_mv=... clip=...` line per second.

## Serial protocol (921600 baud, one command per line)

| Command | Reply |
|---|---|
| `info` | `INFO rate_cfg=.. rate_meas=.. pcnt_hz=.. pcnt_counts=.. oneshot_mean=.. cal=.. pull=.. src=..`, then a `CAL code:mV ...` table |
| `rate <Hz>` | `RATE ok=.. cfg=.. meas=..` (restarts the sampler; `meas` is the DMA rate, see the repeat factor above) |
| `cap <n>` | `CAP n=.. rate=.. ch=..`, then n raw 16-bit words as hex (64 per line), then `END sum=..` |
| `pull <none\|up\|down\|both>` | `PULL mode=..` (internal ~45 kΩ pulls on the sampled pin; `both` = mid-rail) |
| `src <32\|33>` | `SRC gpio=.. ch=..` (switch to the GPIO33 control pin and back) |
| `disp <on\|off>` | `DISP on=..` (sleeps the ST7789 + backlight to rule it out as a noise source) |
| `view <0\|1\|2>` | `VIEW v=.. name=..` (same as the right button) |
| `shot` | `SHOT w=240 h=87`, then the scope image as RGB565 hex rows, then `END` (to check the rendering remotely) |

Raw word layout: bits 15–12 hold the ADC channel, bits 11–0 the 12-bit sample. The I2S FIFO swaps each pair of 16-bit samples, so un-swap them (`buf[i ^ 1]`) before use.

## Limits of the measurement

- **Input range:** 12 dB attenuation covers about 0.14–3.15 V. Anything above reads 4095. A 40 kHz signal must swing inside that range, ideally centred around 1.6 V.
- **Rail ripple:** board and USB noise produces a faint ~41 kHz / 83 kHz line, around 0.3 mV, even on an unconnected pin. Treat a tone that weak as interference, not signal.
- **Accuracy:** the ESP32 ADC is not an oscilloscope (~10 mV RMS noise at 249 kS/s). Use a scope to look at waveform shape.
