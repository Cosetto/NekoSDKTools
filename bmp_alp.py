#!/usr/bin/env python3
"""
Convert between Waffle/NekoSDK BMP+ALP pairs and normal RGBA images.

ALP files contain one 8-bit alpha value per pixel. Some archives pad each ALP
row to a 4-byte boundary; others store exactly width*height bytes. Both layouts
are accepted on extract. Create writes the compact width*height layout, matching
the supplied WaffleBmp reference.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


IMAGE_EXTENSIONS = {".png", ".tga", ".bmp", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}


class BmpAlpError(Exception):
    pass


def alpha_stride(width: int, height: int, alpha_size: int) -> int:
    compact = width * height
    padded_stride = (width + 3) & ~3
    padded = padded_stride * height
    if alpha_size == padded:
        return padded_stride
    if alpha_size == compact:
        return width
    raise BmpAlpError(
        f"ALP size {alpha_size} does not match {width}x{height} "
        f"(expected {compact} or {padded})"
    )


def combine_bmp_alp(bmp_path: Path, alp_path: Path, output_path: Path) -> None:
    if not bmp_path.is_file():
        raise BmpAlpError(f"BMP file not found: {bmp_path}")
    if not alp_path.is_file():
        raise BmpAlpError(f"ALP file not found: {alp_path}")

    with Image.open(bmp_path) as bmp_image:
        rgba = bmp_image.convert("RGBA")

    width, height = rgba.size
    alpha = alp_path.read_bytes()
    stride = alpha_stride(width, height, len(alpha))

    pixels = bytearray(rgba.tobytes())
    for y in range(height):
        alpha_row = y * stride
        pixel_row = y * width * 4
        for x in range(width):
            pixels[pixel_row + x * 4 + 3] = alpha[alpha_row + x]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_image = Image.frombytes("RGBA", (width, height), bytes(pixels))
    save_image(out_image, output_path)
    print(f"extracted {bmp_path} + {alp_path} -> {output_path}")


def split_image(image_path: Path, bmp_path: Path, alp_path: Path) -> None:
    if not image_path.is_file():
        raise BmpAlpError(f"image file not found: {image_path}")

    with Image.open(image_path) as image:
        rgba = image.convert("RGBA")

    width, height = rgba.size
    bmp_path.parent.mkdir(parents=True, exist_ok=True)
    alp_path.parent.mkdir(parents=True, exist_ok=True)

    rgba.convert("RGB").save(bmp_path, format="BMP")

    alpha = bytearray(width * height)
    alpha_channel = rgba.getchannel("A").tobytes()
    alpha[:] = alpha_channel
    alp_path.write_bytes(alpha)
    print(f"created {image_path} -> {bmp_path} + {alp_path}")


def save_image(image: Image.Image, output_path: Path) -> None:
    suffix = output_path.suffix.lower()
    if suffix == ".tga":
        image.save(output_path, format="TGA", compression=None)
    elif suffix == ".bmp":
        image.save(output_path, format="BMP")
    else:
        image.save(output_path)


def iter_files(root: Path, extensions: set[str]) -> list[Path]:
    files = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions
    ]
    files.sort()
    return files


def extract_folder(input_folder: Path, output_folder: Path) -> None:
    if not input_folder.is_dir():
        raise BmpAlpError(f"input is not a folder: {input_folder}")
    output_folder.mkdir(parents=True, exist_ok=True)
    bmp_files = iter_files(input_folder, {".bmp"})
    if not bmp_files:
        raise BmpAlpError(f"no BMP files found: {input_folder}")
    for bmp_path in bmp_files:
        rel = bmp_path.relative_to(input_folder)
        alp_path = bmp_path.with_suffix(".alp")
        out_path = (output_folder / rel).with_suffix(".png")
        combine_bmp_alp(bmp_path, alp_path, out_path)


def create_folder(image_folder: Path, output_folder: Path) -> None:
    if not image_folder.is_dir():
        raise BmpAlpError(f"input is not a folder: {image_folder}")
    output_folder.mkdir(parents=True, exist_ok=True)
    image_files = iter_files(image_folder, IMAGE_EXTENSIONS)
    if not image_files:
        raise BmpAlpError(f"no image files found: {image_folder}")
    for image_path in image_files:
        rel = image_path.relative_to(image_folder)
        split_image(
            image_path,
            (output_folder / rel).with_suffix(".bmp"),
            (output_folder / rel).with_suffix(".alp"),
        )


def usage() -> str:
    return (
        "Usage:\n"
        "  bmp_alp.py -e in.bmp in.alp out.png\n"
        "  bmp_alp.py -e bmp_alp_folder out_folder\n"
        "  bmp_alp.py -c in.png out.bmp out.alp\n"
        "  bmp_alp.py -c image_folder out_folder\n"
    )


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        if len(argv) == 4 and argv[0] == "-e":
            combine_bmp_alp(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        elif len(argv) == 3 and argv[0] == "-e":
            extract_folder(Path(argv[1]), Path(argv[2]))
        elif len(argv) == 4 and argv[0] == "-c":
            split_image(Path(argv[1]), Path(argv[2]), Path(argv[3]))
        elif len(argv) == 3 and argv[0] == "-c":
            create_folder(Path(argv[1]), Path(argv[2]))
        else:
            print(usage(), file=sys.stderr, end="")
            return 2
    except (OSError, BmpAlpError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
