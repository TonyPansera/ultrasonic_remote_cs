"""Render the last screenshot in a console log (SHOT_ROW lines, RGB565 hex) to a PNG.

    uv run python esp32/bar_detector_v4/tools/render_shot.py /tmp/tv.log shot.png [--scale 3]

Rows can come from several `shot y0 y1` requests (the USB/IP link sometimes corrupts one): for each row,
the last well-formed copy after the last full `SHOT ... y0=0` header wins. Missing rows are drawn magenta
and listed, so they can be asked for again.
"""

import argparse
import pathlib
import re
import sys

import numpy as np
from PIL import Image

ROW = re.compile(r"SHOT_ROW (\d+) ([0-9a-f]+)$")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=pathlib.Path)
    ap.add_argument("png", type=pathlib.Path)
    ap.add_argument("--scale", type=int, default=3)
    args = ap.parse_args()

    lines = args.log.read_text(errors="replace").splitlines()
    starts = [i for i, l in enumerate(lines) if re.search(r"SHOT w=\d+ h=\d+ y0=0 ", l)]
    if not starts:
        print("no full shot in the log", file=sys.stderr)
        return 1
    w, h = 240, 135
    rows: dict[int, np.ndarray] = {}
    for line in lines[starts[-1]:]:
        m = ROW.search(line)
        if m and len(m.group(2)) == 4 * w:
            y = int(m.group(1))
            if 0 <= y < h:
                rows[y] = np.array([int(m.group(2)[4 * x:4 * x + 4], 16) for x in range(w)], dtype=np.uint16)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    img[:, :] = (255, 0, 255)
    for y, v in rows.items():
        img[y, :, 0] = ((v >> 11) & 0x1F) * 255 // 31
        img[y, :, 1] = ((v >> 5) & 0x3F) * 255 // 63
        img[y, :, 2] = (v & 0x1F) * 255 // 31
    missing = [y for y in range(h) if y not in rows]
    Image.fromarray(img).resize((w * args.scale, h * args.scale), Image.NEAREST).save(args.png)
    print(f"{args.png}: {len(rows)}/{h} rows" + (f", missing {missing}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
