"""Keep one serial connection to the board open, log it, and take commands from a FIFO.

Opening the port resets the board, so tools that each open it (session.py) lose the TV's state. This one
opens it once and runs until stopped:
  - every line from the board goes to the log, prefixed with the host time since READY;
  - every line written to the FIFO goes to the board.

    ~/.local/share/uv/tools/platformio/bin/python tools/console.py --log /tmp/tv.log --fifo /tmp/tv.cmd &
    echo "tv power" > /tmp/tv.cmd
    echo "shot" > /tmp/tv.cmd          # then: uv run python tools/render_shot.py /tmp/tv.log shot.png
"""

import argparse
import os
import pathlib
import select
import sys
import time

import serial


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", required=True, type=pathlib.Path)
    ap.add_argument("--fifo", required=True, type=pathlib.Path)
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--seconds", type=float, default=3600)
    args = ap.parse_args()

    if not args.fifo.exists():
        os.mkfifo(args.fifo)
    fifo = os.open(args.fifo, os.O_RDONLY | os.O_NONBLOCK)
    keep = os.open(args.fifo, os.O_WRONLY)  # so the FIFO never reports EOF between writers

    s = serial.Serial()
    s.port, s.baudrate, s.timeout = args.port, args.baud, 0.05
    s.dtr = s.rts = False
    s.open()
    s.rts = True
    time.sleep(0.1)
    s.rts = False

    start = time.time()
    t0 = None
    buf, cmd_buf = b"", b""
    with args.log.open("a") as log:
        log.write(f"# console {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        while time.time() - start < args.seconds:
            r, _, _ = select.select([fifo], [], [], 0)
            if r:
                cmd_buf += os.read(fifo, 4096)
                while b"\n" in cmd_buf:
                    line, cmd_buf = cmd_buf.split(b"\n", 1)
                    s.write(line.strip() + b"\n")
                    stamp = time.time() - (t0 or start)
                    log.write(f"{stamp:9.3f} > {line.decode(errors='replace').strip()}\n")
                    log.flush()
            buf += s.read(8192)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode(errors="replace").rstrip("\r")
                if t0 is None and line.strip() == "READY":
                    t0 = time.time()
                stamp = time.time() - (t0 or start)
                log.write(f"{stamp:9.3f} {line}\n")
                log.flush()
    os.close(keep)
    os.close(fifo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
