# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This folder is part of the barlab repo (`~/signal_analysis`; merged from `~/esp32` on 2026-10-09). The root `CLAUDE.md` covers the Python analysis side.

Workspace for ESP32 projects targeting a **LilyGO T-Display v1.1** (ESP32-D0WDQ6-V3, 4MB flash, no PSRAM, ST7789 135x240 TFT), built and flashed from WSL2 with PlatformIO. Each subfolder is a standalone PlatformIO project (`tdisplay_test`, `ultrasonic_probe`, `bar_detector` (prototype 1, GPIO32 receiver), `bar_detector_v2` (prototype 2, PDM MEMS mic, on a second board), `bar_detector_v3` (prototype 3: prototype 2's board and mic, with re-measured bands and overtone-checked decisions), `bar_detector_v4` (prototype 4, the current one: v3's detection driving a TV UI), and `bar_detector_backup_40k_47k`, a frozen copy of bar_detector for the 40.7 / 42.78 kHz bars). Versions are kept as separate folders: never overwrite an earlier prototype's folder. `tdisplay_test/` is the working reference: copy its `platformio.ini` when starting a new project. See `README.md` for the user-facing guide.

## Toolchain versions (matter for API choice)

- PlatformIO Core 6.2 (`~/.local/bin/pio`, installed with `uv tool install --python 3.13 platformio`).
- Platform `espressif32` 7.1.3, which ships **Arduino-ESP32 core 2.0.17** (ESP-IDF 4.4). Use the 2.x APIs: `ledcSetup()`/`ledcAttachPin()`, not the 3.x `ledcAttach()`; `analogReadMilliVolts()` is available.
- `bodmer/TFT_eSPI@^2.5.43`.

## Commands

Run from inside a project folder (e.g. `~/signal_analysis/esp32/tdisplay_test`):

```bash
pio run                      # build
pio run -t upload            # build + flash (port pinned to /dev/ttyACM0 in platformio.ini)
pio run -t clean
pio pkg exec -p tool-esptoolpy -- esptool.py --port /dev/ttyACM0 flash_id   # check serial link, chip, flash size
```

No linters are configured. Only `bar_detector`, `bar_detector_v2`, `bar_detector_v3` and `bar_detector_v4` have tests: `pio test -e native` (host, no board).

`pio device monitor` needs a TTY, so Claude cannot use it. Read serial without it like this (it resets the board to capture the boot output):

```bash
~/.local/share/uv/tools/platformio/bin/python - <<'EOF'
import serial, time
s = serial.Serial(); s.port = "/dev/ttyACM0"; s.baudrate = 115200; s.timeout = 0.2
s.dtr = False; s.rts = False; s.open()
s.rts = True; time.sleep(0.1); s.rts = False   # pulse EN = reset
end = time.time() + 8; buf = b""
while time.time() < end: buf += s.read(4096)
print(buf.decode(errors="replace"))
EOF
```

## USB connection (WSL2 + usbipd-win)

The board reaches WSL through usbipd-win (BUSID `3-2` on the current USB port, `6-1` on the other one, VID:PID `1a86:55d4`, CH9102 bridge, appears as `/dev/ttyACM0` via `cdc_acm`). Before uploading, check `ls /dev/ttyACM0`. If it is missing, (re)attach it from WSL, running in the background:

```bash
"/mnt/c/Program Files/usbipd-win/usbipd.exe" attach --wsl --busid 3-2 --auto-attach
```

The device is already bound (persisted), so `bind` (admin) is not needed again. If the BUSID changed (different USB port), find it with `usbipd.exe list`. Port permissions come from PlatformIO's udev rules (`/etc/udev/rules.d/99-platformio-udev.rules`, mode 0666) and from `dialout` membership.

`sudo` needs a password, so ask the user to run sudo commands themselves with `! <command>`. A pre-existing half-configured `openssh-server` package makes every `apt-get` call exit 1 even when the install worked, so never chain anything after `apt-get` with `&&`.

## ultrasonic_probe (GPIO32 signal analysis)

- The firmware speaks a line protocol at 921600 baud (`info`, `rate`, `cap`, `pull`, `src`, `disp`, `view`, `shot`); see `ultrasonic_probe/README.md`. `tools/analyze_signal.py` is the host side: `cd ultrasonic_probe && uv run tools/analyze_signal.py` takes about 3 min and regenerates `results/` (figures, `REPORT.md`, `summary.json`, `data/*.npz`).
- I can't see the panel: `shot` dumps the scope sprite (240x87 RGB565 hex) so it can be rendered to a PNG to check UI changes. Rows sometimes arrive corrupted over USB/IP, so validate the row length and hex, then retry.
- **Opening `/dev/ttyACM0` resets the board** (cdc_acm toggles DTR/RTS even with `dtr=False`). Host code must wait for the `READY` line before sending commands, or the first command is lost during boot.
- ESP32 I2S built-in-ADC quirks, all verified on this board:
  - each 16-bit sample pair comes out swapped (un-swap with `i ^ 1`);
  - the true conversion rate is fixed at ~249 kS/s, and a higher I2S rate only repeats each conversion (x2/x4/x8);
  - `i2s_set_adc_mode()`/`i2s_adc_enable()` re-init the pad and clear RTC pulls, so re-apply pulls after them.
- The legacy PCNT driver enables a pull-up on its input pin, which precharges a floating node, so measure with `analogRead()` before configuring PCNT. But `analogRead()` leaves the pad on the RTC/analog mux, which disconnects PCNT's digital input: call `rtc_gpio_deinit()` + `pinMode(INPUT)` before PCNT, or it silently counts 0.
- The signal is an FSK remote: 38 kHz and 42 kHz tones (each ±500 Hz accepted), sent as 50 bursts/s (~19 ms on / ~1 ms off) with silences between transmissions (the line idles at ~2.18 V with ~50-70 mVpp noise). Count a tone as signal only at ≥ 10 mV (`signal_present`); weaker lines are interference.
- A ~0.3 mV line wandering around 41 kHz (plus an 83 kHz harmonic) appears even on an unconnected ADC pin, with the display asleep. It is supply/ground interference: never report a tone that weak as a signal.

## bar_detector (prototype 1: 38 / 47 kHz bar recognition on GPIO32)

- The detector lives in `bar_detector/lib/tone_detector/` as plain C++11, with no Arduino headers. `src/main.cpp` runs it in a task on core 0 that reads every I2S DMA buffer; the display and serial run in `loop()` on core 1.
- `pio test -e native` runs it on the PC, against synthetic signals and against `data/raw/rec5.wav` and `rec4.wav` at 384 kHz; it walks up to the barlab `pyproject.toml` to find them. Both recordings hold bars that have since been replaced, so their tests move the band back: rec5 (40.7 kHz bar) puts band A at 40 741.9 Hz, rec4 (~51 kHz bar) puts band B at 51 014.2 Hz. Any change to the detector must keep these tests green and add a test.
- Band A (38 000 Hz) is the bat detector's reading of the ~38 kHz bar that replaced the 40.7 kHz one on 2026-10-16; it is not measured yet. On the board, 10 of 10 strikes were detected as A at 49-68 dB, but `f_a` sat on the band's top bin (38 608 Hz) in 9 of them, so the line is at or just above ~38.6 kHz: re-centre after a `band a` scan with strikes (`EVT` lines only; `lvl on` tends to drop the USB/IP link). The 40.7 kHz / 42.78 kHz firmware is kept in `bar_detector_backup_40k_47k/`.
- Band B (42 782 Hz) was measured on the board on 2026-10-16. That day the ~51 kHz bar was replaced by one the bat detector shows at ~43 and ~47 kHz.
  - The GPIO32 receiver sees its 43 kHz line far better: 60-68 dB SNR and 9 of 11 strikes detected, against 34-53 dB and 1 of 5 for the 46.77 kHz line.
  - The centre is the median of the 11 strikes.
  - The tile still names that bar "47".
- A and B are 4.8 kHz apart, so neither band's guard bins (1-2 kHz out) reach the other bar's line: both bars at once give BOTH (grey tile) unless one leads by 10 dB.
- The clipped 38/42 kHz FSK remote sits on band A (38 kHz tone), next to band B (42 kHz tone), and aliases across the spectrum. That is why a detection requires a clean exponential ringdown, not just a level.
- First run on the board on 2026-10-16. `info` gave `rate_meas=249031`, `bad_words=0`, `load_pct=43`, `load_max_pct=93`. Thresholds are not calibrated yet: calibrate with `lvl on` and real strikes (see `bar_detector/README.md`).
- CPU budget: one Goertzel bin per pass with `-Os` took ~140 % of real time and the task watchdog reset the board every ~12 s. Four bins per pass, plus `-O2` in `platformio.ini`, brings it to ~47 %. More bins (wider `toleranceHz` or guards) cost time, so recheck `load_pct` after changing them.
- `dc_mv=142` on that first run: GPIO32 sat near ground, not at the ~1650 mV expected with an AC-coupled, biased input. Check the front end before calibrating.

## bar_detector_v2 (prototype 2: SPH0641 PDM MEMS mic)

- **Board and mic:**
  - Its T-Display: MAC `30:c6:f7:25:ad:f0`, COM6, usbipd BUSID `3-2` on the current port, bound on 2026-10-23.
    - It replaced `a0:dd:6c:71:14:04` (COM4), whose screen was faulty.
    - The first board (prototype 1) is COM3, MAC `A0:DD:6C:72:CD:2C`.
    - Each new board needs a one-time `usbipd bind` (UAC prompt).
  - The mic is an Elecrow module with a Syntiant/Knowles SPH0641LU4H-1: CLK on GPIO22, DATA on GPIO21, SEL to GND.
- **Layout:** `lib/tone_detector` and `test/test_detector` are byte-identical copies of `bar_detector`'s. Keep them so (`diff -r`), and put v2-specific settings in `lib/device_config/DeviceConfig.h`, which both `main.cpp` and the tests use.
- **Clock modes and wake-up:** the mic's ultrasonic mode is a 3.072-4.8 MHz clock. The datasheet forbids waking from sleep (clock < 250 kHz, as on every ESP32 reset) straight into it, so `startMic()` runs 2.4 MHz for 100 ms, then `i2s_set_clk()` to 3.072 MHz.
- **No hardware PDM decoder:** the ESP32's own PDM RX decimates by 64/128 only (48 kS/s max at 3.072 MHz), which is useless for the bars. So I2S0 is a standard 32-bit stereo master RX (BCK = mic clock, APLL, `ws`/`mck` = `I2S_PIN_NO_CHANGE`; mck left at 0 would drive GPIO0). The DMA delivers the raw bitstream, first bit at bit 31, and **in time order**: verified on the board, where `pdm swap 1` raises the broadband RMS from -55 to -26 dBFS.
- **Decimator** (`lib/pdm_decimator`): a byte-LUT FIR, 256 taps, decimation by 16, so 192 kS/s. Detector blocks are 1536 samples (8 ms, 125 Hz bins). On the board: `load_pct` ~64, of which `dec_pct` ~31, and `load_max_pct` 68.
- **Bands, measured 2026-10-23 with `spec on`** (`lib/strike_spectrum`: FFTs early and late after each strike's click; lines in 20-90 kHz with their 40 ms drop):
  - **A:** 38 618 Hz, the ~38 kHz bar (38 595.5 Hz struck, 38 606-38 650 Hz in sympathy).
  - **B:** 47 088 Hz, the other bar (11 strikes, 47 081-47 114 Hz, second line 45.37 kHz). Its old 42.78 kHz line was gone.
  - **Tolerance:** ±300 Hz.
- **False detections from background noise:** a clickless 38.00 kHz background source (FSK-remote-like, with a faint 42.7-43.2 kHz line), fading in the room's reverberation, was detected as A every few seconds while A sat at 38.0 kHz ±500 Hz. The user confirmed it is background noise, not strikes. An acoustic mic hears reverberant fades as ringdowns, which prototype 1's wired receiver did not. Regression test: `test_chain_reverberant_remote_is_not_a_bar`.
- **Sympathetic ringing:** every strike of the 47 bar also rings the 38 bar's line, 25-40 dB weaker. B must therefore lead by the 10 dB decision margin.
- **Memory:** `spec` needs ~90 KB of heap. Allocating it in a global constructor threw `std::bad_alloc` and boot-looped the board, so it is allocated in `setup()`. Keep `double` out of per-sample loops: the ESP32 has no double FPU, and one such accumulator cost 16 % of a core.
- **Testing the decimator:** `test_pdm` uses a 2nd-order sigma-delta model. That model itself puts a spur on 42 kHz from a strong (-10 dBFS) 150 kHz tone, so the alias test runs at -30 dBFS (the FIR alone gives -102 dB at 150 kHz).

## bar_detector_v3 (prototype 3: overtone-checked decisions)

- **Hardware:** the same board and mic as prototype 2 (MAC `30:c6:f7:25:ad:f0`, BUSID `3-2`). `lib/tone_detector`, `lib/pdm_decimator` and `test/test_detector` are unchanged copies, and `tone_detector` must stay byte-identical to v1's.
- **Measured on 2026-10-30, after the user changed the prototype** (`measurements/REPORT.md`, logs in `measurements/2026-10-30/`):
  - **38 bar:** 38 418 Hz, with an overtone at 33 917 Hz (ratio 0.88313). It moved down ~200 Hz.
  - **47 bar:** 47 110 Hz, with an overtone at 45 386 Hz (ratio 0.96331). Each ratio holds within ±0.3 %.
  - **Bars:** the 38 bar is 63 mm, the 47 bar is 50 mm, d = 16 mm, both held by a rod through a centre hole. No 2f shows on the board.
  - **Background:** the ~40 kHz / 38.00 kHz background source was off all day.
- **Bands** (DeviceConfig):
  - A is 38 500 Hz and B is 47 110 Hz, ±200 Hz (±2 bins).
  - Guards are 1.0-1.375 kHz. The 47 bar's overtone, now as loud as its fundamental, sat in B's old 1-2 kHz guards: 2 of 16 strikes were missed and others came late.
  - A's bottom bin (38 250 Hz) stays 250 Hz off the 38.00 kHz background. At 38 420 Hz, `test_chain_reverberant_remote_is_not_a_bar` failed.
- **`lib/strike_spectrum`:**
  - **Watched lines:** after each capture it measures watched lines (`kWatch`, up to 12), each with early, late and **before** (the 4096 samples ahead of the click, from a ring) levels.
  - **Triggers:** it runs on every buffer. `main.cpp` also triggers it (`triggerNext()`) when a detector band rises over its threshold, because the second-difference trigger missed 6 of 15 soft clicks: the mic's noise near Nyquist dominates it. That onset also restarts a capture that a knock started two or more chunks earlier, and cuts through the holdoff.
  - **Peak range:** peaks are searched in 20-62 kHz only. The mic's shaped noise rises from -100 dBFS at 35 kHz to -70 at 90 kHz.
- **`lib/bar_decision`:**
  - Pairs each Detect with its capture (the capture started 0-36 ms before the event).
  - Only settles BOTH. The fundamental counts if it rose ≥ 10 dB and stands ≥ 6 dB over the floor; the overtone counts too only if, in addition, it is at the bar's ratio ±0.3 %.
  - The evidence is the late (40-61 ms) power sum of the counted lines, and a ≥ 10 dB lead wins.
  - The struck bar's lines ring on, while the sympathetic bar's collapse (median drop 3 dB against 19 dB in 40 ms).
  - On the 88 labelled strikes: all right, and the lines alone are never wrong (`test/test_decision`, vectors exported by `tools/analyze_sessions.py --export-vectors`).
  - A or B is never changed. The End redraw keeps the decided band.
- **Measuring:**
  - `tools/session.py` (PlatformIO's Python, pyserial) logs a session, with `--at T:cmd` for timed commands such as `spec now`.
  - `uv run python esp32/bar_detector_v3/tools/analyze_sessions.py --strikes --peaks` summarises the logs.
  - The labels (which bar was struck) are `TRUTH` and `S5_SEQUENCE` in that tool.
  - Ask the user to hold the other bar to get a bar's own lines, and give them a written strike sequence for mixed sessions.
  - About 1.6 KB of output per strike was fine over USB/IP.
- **Open points:**
  - weak extra 38 detections 0.4 s before a strike (twice);
  - the band-A upper guard (39.5-39.875 kHz) against a ~40 kHz background, not verified;
  - the bat-detector WAVs (`data/raw/2026-10-30_*.wav`), not analysed yet.

## bar_detector_v4 (prototype 4: the Space Command TV, current)

- **What it is:** v3's detection, with `tone_detector`, `pdm_decimator`, `bar_decision` and `device_config` unchanged, driving a TV on the screen:
  - 38 kHz: power, with CRT-style animations and a red standby LED (backlight PWM on LEDC channel 0);
  - 47 kHz: next of 5 channels (TEST CARD, SPACE, PLASMA, FIREPLACE, ULTRASONIC);
  - both bars: an explosion.
- **`lib/tv_remote`** (host-tested in `test/test_tv`, with replays of v3's sessions S3-S7) turns decided strikes into commands:
  - **Hold 300 ms:** "both" means both bands ring clean ≥ 44 dB within the hold. That is 48-60 dB for real both-strikes, against ≤ 41 dB for sympathetic ringing.
  - **Weak strikes** (< 40 dB) wait 500 ms, and a stronger one replaces them.
  - **Refractory:** a weak strike within 800 ms after a command is dropped.
  - **Strike peaks:** the sampling task tracks each band's clean peak SNR during the detector's event (`Shared.strikePeak`).
- **`src/TvScreen`:**
  - **Frame:** one 240×135 16-bit sprite (64.8 KB), pushed whole at ≤ 33 fps. Its buffer is byte-swapped, so the effects that write it directly use swapped colours.
  - **Allocation order in `setup()`:** mic (I2S DMA) first, then the sprite, then the strike spectrum. With the sprite first, I2S failed to get its DMA buffers. With the old spectrum (105 KB), `std::bad_alloc` boot-looped the board.
  - **Slimmer spectrum:** v4's `strike_spectrum` therefore keeps only its two FFT windows and stores its spectra as int16 centi-dB (~83 KB, same numbers, v3's tests pass).
  - **Boot heap:** the `HEAP` lines print it. About 29 KB of 8-bit heap is left.
- **Checking the screen:**
  - `shot` dumps the frame as hex rows, paced at 25 ms/row.
  - `tools/console.py` keeps one serial connection open, with a FIFO for commands, because opening the port resets the board.
  - `tools/render_shot.py` (barlab's venv has Pillow; PlatformIO's Python does not) renders the dump to a PNG, which can then be viewed.
  - The user may be striking the bars while you test, and their real commands then interleave with yours (check the `TV cmd=` lines).

## Display configuration (non-obvious)

TFT_eSPI is configured **entirely through `build_flags`** in `platformio.ini`: `-DUSER_SETUP_LOADED=1` plus the pins from TFT_eSPI's `Setup25_TTGO_T_Display.h`. Never edit `User_Setup.h`/`User_Setup_Select.h` under `.pio/libdeps/`, because PlatformIO re-downloads that folder. Key flags:
- `CGRAM_OFFSET=1` is required: the 135x240 panel sits at an offset inside the controller's 240x320 RAM. Without it, the image is shifted and shows garbage borders.
- Fonts are compiled in only when their `LOAD_*` flag is set (GLCD, 2, 4, 6, 7, 8, GFXFF, SMOOTH_FONT are all enabled).
- `setRotation(1)` gives 240x135 landscape.

## Board pin facts

| Function | GPIO | Note |
|---|---|---|
| TFT MOSI/SCLK/CS/DC/RST/BL | 19/18/5/16/23/4 | backlight active HIGH |
| Button left (BOOT) | 0 | `INPUT_PULLUP`, LOW = pressed; held at reset = download mode |
| Button right | 35 | input-only, no internal pull-up (external one on board), LOW = pressed |
| Supply voltage ADC | 34 | ×2 divider; set GPIO14 (ADC_EN) HIGH first |

GPIOs 34–39 are input-only. Keep project folders on the WSL ext4 filesystem (`~/signal_analysis/esp32`), not `/mnt/c`, because builds there are much slower.
