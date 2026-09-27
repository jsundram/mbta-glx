"""Draw web/icon-180.png -- the home-screen icon -- from code, not from a design tool.

The tab favicon is an inline SVG data URI in index.html, because a board opened as
file:// cannot fetch a sibling file and a sixth published asset for sixteen pixels is
not worth it. iOS is the reason a PNG exists at all: Safari will not take an SVG for
`apple-touch-icon`, and without one an "Add to Home Screen" shortcut gets a
screenshot of the page, which at icon size is a grey smear.

Full-bleed and opaque on purpose. iOS masks the icon to its own squircle, so artwork
that rounds its own corners ends up as a rounded square inside a rounded square.

Generated rather than committed blind: a binary asset nobody can regenerate is a
binary asset nobody can change. `tests/test_publish.py` decodes the committed file
and compares the pixels to what this writes, which is the same guard web/model.js
gets -- the check is on the decoded image, not the compressed bytes, because zlib's
output is not promised to be identical across versions.

    uv run python src/make_icon.py
"""
import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "icon-180.png"
SIZE = 180
GREEN = (0x00, 0x84, 0x3D)       # the MBTA's green, same as --go in the light theme
WHITE = (0xFF, 0xFF, 0xFF)

# An "E", as four rectangles on the 180-unit grid the SVG in index.html uses, so the
# tab icon and the home-screen icon are the same drawing. The Green Line branch that
# serves Magoun, which is what a rider would recognise on a badge.
BARS = [
    (56, 44, 76, 136),     # stem
    (56, 44, 124, 64),     # top
    (56, 80, 114, 100),    # middle, shorter, as an E is drawn
    (56, 116, 124, 136),   # bottom
]


def pixels() -> bytes:
    """The image as raw 8-bit RGB scanlines, no filter bytes."""
    rows = []
    for y in range(SIZE):
        row = bytearray()
        for x in range(SIZE):
            ink = any(x0 <= x < x1 and y0 <= y < y1 for x0, y0, x1, y1 in BARS)
            row += bytes(WHITE if ink else GREEN)
        rows.append(bytes(row))
    return b"".join(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def png(raw: bytes) -> bytes:
    """Minimal 8-bit truecolour PNG. No dependency worth taking for four rectangles."""
    ihdr = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0)
    stride = SIZE * 3
    # Filter type 0 per scanline: this is flat colour, so nothing else pays for itself.
    scanlines = b"".join(b"\x00" + raw[i:i + stride]
                         for i in range(0, len(raw), stride))
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(scanlines, 9)) + _chunk(b"IEND", b""))


def main() -> None:
    OUT.write_bytes(png(pixels()))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {SIZE}x{SIZE})")


if __name__ == "__main__":
    main()
