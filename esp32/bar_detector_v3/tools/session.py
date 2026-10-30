"""Log one measurement session of bar_detector_v3 over serial.

Opens the board's port (which resets it), waits for READY, sends the setup commands, then writes
every line the board sends, prefixed with the host time in seconds since READY, to
measurements/<date>/<label>.log. Timed commands (--at 30:"spec now") are sent during the session.

Run it with PlatformIO's Python, which has pyserial:
    ~/.local/share/uv/tools/platformio/bin/python tools/session.py --label S1_A_damped --seconds 60
"""

import argparse
import datetime
import pathlib
import sys
import time

import serial

HERE = pathlib.Path(__file__).resolve().parent.parent


def parse_at(spec: str) -> tuple[float, str]:
    t, _, cmd = spec.partition(":")
    return float(t), cmd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", required=True, help="session name, e.g. S1_A_damped")
    ap.add_argument("--seconds", type=float, required=True, help="session length after READY")
    ap.add_argument("--cmd", action="append", default=[], help="extra setup command (repeatable)")
    ap.add_argument("--at", action="append", default=[], type=parse_at, help="T:command, sent T s after READY")
    ap.add_argument("--lvl", action="store_true", help="also stream LVL lines (~20/s)")
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    args = ap.parse_args()

    out_dir = HERE / "measurements" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{args.label}.log"
    if out_path.exists():
        print(f"{out_path} exists: pick another label", file=sys.stderr)
        return 2

    s = serial.Serial()
    s.port, s.baudrate, s.timeout = args.port, args.baud, 0.1
    s.dtr = s.rts = False
    s.open()
    s.rts = True
    time.sleep(0.1)
    s.rts = False  # pulse EN: a clean boot, whatever the driver did on open

    with out_path.open("w") as log:
        log.write(f"# session {args.label} {datetime.datetime.now().isoformat(timespec='seconds')} "
                  f"seconds={args.seconds} cmds={args.cmd} at={args.at} lvl={args.lvl}\n")
        buf = b""
        t0 = None
        deadline = time.time() + 15
        pending = sorted(args.at)
        stats = {"EVT": 0, "SPEC": 0}
        last_rx = time.time()
        while True:
            now = time.time()
            if t0 is None and now > deadline:
                print("no READY within 15 s", file=sys.stderr)
                log.write("# HOST no READY\n")
                return 1
            if t0 is not None and now - t0 > args.seconds:
                break
            if t0 is not None and pending and now - t0 >= pending[0][0]:
                s.write((pending.pop(0)[1] + "\n").encode())
            if t0 is not None and now - last_rx > 20:
                log.write(f"{now - t0:9.3f} # HOST no data for 20 s (USB/IP link dropped?)\n")
                print("no data for 20 s: link dropped?", file=sys.stderr)
                last_rx = now
            chunk = s.read(4096)
            if chunk:
                last_rx = now
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").rstrip("\r")
                if t0 is None:
                    if line.strip() == "READY":
                        t0 = time.time()
                        cmds = ["info", "watch", "spec on"] + args.cmd + (["lvl on"] if args.lvl else [])
                        for c in cmds:
                            s.write((c + "\n").encode())
                            time.sleep(0.05)
                        print(f"READY: logging {args.seconds:.0f} s to {out_path}", flush=True)
                    log.write(f"{0.0:9.3f} {line}\n")
                    continue
                stamp = time.time() - t0
                log.write(f"{stamp:9.3f} {line}\n")
                key = line.split(" ", 1)[0]
                if key in stats:
                    stats[key] += 1
                if not line.startswith(("LVL ", "SPEC_PEAK", "SPEC_LINE")):
                    print(f"{stamp:7.2f} {line}", flush=True)
        s.write(b"lvl off\ninfo\n")
        end = time.time() + 0.5
        while time.time() < end:
            buf += s.read(4096)
        for raw in buf.split(b"\n"):
            line = raw.decode(errors="replace").rstrip("\r")
            if line:
                log.write(f"{time.time() - t0:9.3f} {line}\n")
                if line.startswith("INFO"):
                    print(line, flush=True)
    s.close()
    print(f"done: {stats['EVT']} EVT, {stats['SPEC']} SPEC lines in {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
