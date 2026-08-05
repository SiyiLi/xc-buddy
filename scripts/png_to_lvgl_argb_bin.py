#!/usr/bin/env python3
"""Convert transparent PNG icons to LVGL 9 ARGB8888 binary blobs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from PIL import Image


def write_lvgl_argb8888(source: Path, out_path: Path) -> None:
    image = Image.open(source).convert("RGBA")
    data = bytearray()
    for r, g, b, a in image.getdata():
        # LVGL's ARGB8888 byte order is B, G, R, A in memory.
        data.extend((b, g, r, a))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    for source in args.sources:
        stem = source.stem
        stem = re.sub(r"_\d+$", "", stem)
        if stem and stem[0].isdigit() and "_" in stem:
            stem = stem.split("_", 1)[1]
        out_path = args.out_dir / f"cat_{stem}_argb8888.bin"
        write_lvgl_argb8888(source, out_path)
        print(out_path)


if __name__ == "__main__":
    main()
