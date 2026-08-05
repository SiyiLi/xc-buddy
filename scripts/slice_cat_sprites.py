#!/usr/bin/env python3
"""Slice a 3x2 chroma-key cat sprite sheet into LVGL-friendly PNG icons."""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image


STATE_NAMES = (
    "pairing",
    "ready",
    "listening",
    "thinking",
    "resting",
    "error",
)


def chroma_alpha(r: int, g: int, b: int) -> int:
    """Return alpha for a #00ff00-ish generated chroma background."""
    if g > 150 and g > r * 1.45 and g > b * 1.45:
        dominance = g - max(r, b)
        if dominance > 110:
            return 0
        if dominance > 65:
            return int(255 * (110 - dominance) / 45)
    return 255


def remove_green(crop: Image.Image) -> Image.Image:
    crop = crop.convert("RGBA")
    px = crop.load()
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b, a = px[x, y]
            px[x, y] = (r, g, b, min(a, chroma_alpha(r, g, b)))
    return crop


def trim_alpha(image: Image.Image, pad: int) -> Image.Image:
    alpha = image.getchannel("A")
    bbox = alpha.point(lambda p: 255 if p > 8 else 0).getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    return image.crop(
        (
            max(0, left - pad),
            max(0, top - pad),
            min(image.width, right + pad),
            min(image.height, bottom + pad),
        )
    )


def fit_square(image: Image.Image, size: int) -> Image.Image:
    image = trim_alpha(image, pad=10)
    scale = min(size / image.width, size / image.height)
    width = max(1, round(image.width * scale))
    height = max(1, round(image.height * scale))
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(image, ((size - width) // 2, (size - height) // 2))
    return canvas


def write_preview(paths: list[Path], out_path: Path, tile_size: int) -> None:
    preview = Image.new("RGBA", (tile_size * 3, tile_size * 2), (255, 255, 255, 255))
    for y in range(preview.height):
        for x in range(preview.width):
            if ((x // 8) + (y // 8)) % 2:
                preview.putpixel((x, y), (232, 232, 232, 255))
    for index, path in enumerate(paths):
        icon = Image.open(path).convert("RGBA")
        x = (index % 3) * tile_size + (tile_size - icon.width) // 2
        y = (index // 3) * tile_size + (tile_size - icon.height) // 2
        preview.alpha_composite(icon, (x, y))
    preview.convert("RGB").save(out_path, quality=95)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--out-dir", type=Path, default=Path("docs/cat_sprites"))
    parser.add_argument("--size", type=int, default=112)
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    image = Image.open(args.source).convert("RGBA")
    cell_width = image.width // 3
    cell_height = image.height // 2
    outputs: list[Path] = []

    for row in range(2):
        for col in range(3):
            index = row * 3 + col
            box = (
                col * cell_width,
                row * cell_height,
                (col + 1) * cell_width,
                (row + 1) * cell_height,
            )
            transparent = remove_green(image.crop(box))
            raw_path = args.out_dir / f"{index + 1}_{STATE_NAMES[index]}_raw.png"
            icon_path = args.out_dir / f"{index + 1}_{STATE_NAMES[index]}_{args.size}.png"
            trim_alpha(transparent, pad=12).save(raw_path)
            fit_square(transparent, size=args.size).save(icon_path)
            outputs.append(icon_path)

    preview_path = args.out_dir / "preview.jpg"
    write_preview(outputs, preview_path, tile_size=args.size + 32)
    print(f"source={image.width}x{image.height} cell={cell_width}x{cell_height}")
    for path in outputs:
        icon = Image.open(path)
        print(f"{path}: {icon.size}, alpha_bbox={icon.getchannel('A').getbbox()}")
    print(preview_path)


if __name__ == "__main__":
    main()
