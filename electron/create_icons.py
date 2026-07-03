"""
Generate 256x256 app icons (icon.ico, tray.ico, icon.png) using only the
Python standard library — electron-builder requires icons >= 256x256.

Design: dark blue background (#2B6CB0) with a simple white scales-of-justice
pixel glyph. Visual quality is secondary; correct size is what matters.
"""
import os
import struct
import sys
import zlib

SIZE = 256
BG = (43, 108, 176)   # #2B6CB0
FG = (255, 255, 255)


def _build_pixels():
    """Return a SIZE x SIZE grid of RGB tuples with a simple glyph."""
    px = [[BG] * SIZE for _ in range(SIZE)]

    def rect(x0, y0, x1, y1):
        for y in range(max(0, y0), min(SIZE, y1)):
            for x in range(max(0, x0), min(SIZE, x1)):
                px[y][x] = FG

    # Simple scales-of-justice glyph:
    cx = SIZE // 2
    rect(cx - 6, 48, cx + 6, 196)          # central pole
    rect(cx - 80, 60, cx + 80, 72)         # crossbeam
    rect(cx - 90, 72, cx - 70, 120)        # left chain
    rect(cx + 70, 72, cx + 90, 120)        # right chain
    rect(cx - 110, 120, cx - 50, 134)      # left pan
    rect(cx + 50, 120, cx + 110, 134)      # right pan
    rect(cx - 48, 196, cx + 48, 212)       # base

    return px


def make_png_256() -> bytes:
    """Build a valid 256x256 RGB PNG byte-by-byte."""
    px = _build_pixels()

    def chunk(name: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(name + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", crc)

    raw = b"".join(
        b"\x00" + bytes(c for pixel in row for c in pixel) for row in px
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def make_ico(png_bytes: bytes, output_path: str) -> None:
    """Wrap a 256x256 PNG into a valid .ico (PNG-in-ICO, Vista+ format)."""
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=1 (icon), count=1
    entry = struct.pack(
        "<BBBBHHII",
        0, 0,          # width=256, height=256 (0 means 256 in ICO format)
        0, 0,          # color count, reserved
        1, 32,         # planes, bit count
        len(png_bytes),
        6 + 16,        # offset to image data (ICONDIR + 1 ICONDIRENTRY)
    )
    with open(output_path, "wb") as f:
        f.write(header + entry + png_bytes)


if __name__ == "__main__":
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
    os.makedirs(assets, exist_ok=True)

    png = make_png_256()
    make_ico(png, os.path.join(assets, "icon.ico"))
    make_ico(png, os.path.join(assets, "tray.ico"))
    with open(os.path.join(assets, "icon.png"), "wb") as f:
        f.write(png)

    print("Icons created: 256x256 ->", assets)
    sys.exit(0)
