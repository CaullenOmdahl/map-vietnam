#!/usr/bin/env python3
"""Draws Map Vietnam's app icon and writes it in the formats each platform wants.

A folded old map with a pin, on the viewer's accent green (web/app/app.css, --accent). The design
is drawn at 1024 px with Pillow (already in requirements.txt), so the icons are reproducible and
no binary has to be edited by hand.

    python packaging/make_icons.py

writes into packaging/icons/:
  map-vietnam.png   1024 px: the Linux icon, and the source of the .icns
  map-vietnam.ico   Windows, 16 to 256 px in one file
  map-vietnam.icns  macOS, only if Pillow can write it here (otherwise make it with iconutil on a Mac)

Run it again and commit the result whenever the design changes.
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent / "icons"
SIZE = 1024
SCALE = 2  # draw at twice the size and shrink, for smooth edges

ACCENT = (14, 90, 75)  # --accent
ACCENT_DARK = (9, 62, 52)
PAPER = (244, 232, 200)
PAPER_SHADE = (226, 208, 164)
PAPER_LIGHT = (251, 244, 224)
INK = (120, 96, 56)
RIVER = (110, 160, 190)
PIN = (200, 52, 44)
PIN_DARK = (150, 34, 30)


def draw_icon(size=SIZE):
    """The icon as an RGBA image, size x size pixels."""
    big = SIZE * SCALE
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def p(x, y):  # design coordinates run 0..1024
        return (x * SCALE, y * SCALE)

    def poly(points, fill):
        d.polygon([p(*pt) for pt in points], fill=fill)

    def line(points, fill, width):
        d.line([p(*pt) for pt in points], fill=fill, width=width * SCALE, joint="curve")

    # The tile: accent green with a darker lower band, rounded like an app icon.
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=230 * SCALE, fill=255)
    tile = Image.new("RGBA", (big, big), ACCENT)
    ImageDraw.Draw(tile).rectangle([*p(0, 760), *p(1024, 1024)], fill=ACCENT_DARK)
    img.paste(tile, (0, 0), mask)

    # The map: three panels folded like an accordion, the middle one raised.
    left, right, top, bottom = 150, 874, 250, 780
    x = [left + i * (right - left) / 3 for i in range(4)]
    panels = [
        [(x[0], top + 40), (x[1], top), (x[1], bottom - 40), (x[0], bottom)],
        [(x[1], top), (x[2], top + 40), (x[2], bottom), (x[1], bottom - 40)],
        [(x[2], top + 40), (x[3], top), (x[3], bottom - 40), (x[2], bottom)],
    ]
    for panel in panels:  # a soft shadow under the paper
        poly([(px, py + 26) for px, py in panel], (0, 0, 0, 70))
    for panel, color in zip(panels, (PAPER_LIGHT, PAPER_SHADE, PAPER), strict=True):
        poly(panel, color)

    # A river and a road, kept clear of the pin.
    line([(x[0] + 40, top + 170), (x[1], top + 200), (x[2], top + 270), (x[3] - 40, top + 250)], RIVER, 26)
    line(
        [(x[0] + 30, bottom - 110), (x[1] + 60, bottom - 150), (x[2] - 20, bottom - 120), (x[3] - 30, bottom - 170)],
        INK,
        14,
    )
    line([(x[1], top + 10), (x[1], bottom - 40)], (150, 125, 80), 6)  # the fold lines
    line([(x[2], top + 50), (x[2], bottom)], (150, 125, 80), 6)

    # The pin: a red drop with a pale centre, standing on the map.
    cx, cy, r, tip = 512, 400, 118, 650
    d.ellipse([*p(cx - 90, tip - 14), *p(cx + 90, tip + 14)], fill=(0, 0, 0, 70))  # its shadow
    poly([(cx - r * 0.92, cy + r * 0.45), (cx, tip), (cx + r * 0.92, cy + r * 0.45)], PIN)
    d.ellipse([*p(cx - r, cy - r), *p(cx + r, cy + r)], fill=PIN)
    poly([(cx + 24, cy + r * 0.97), (cx, tip), (cx + r * 0.92, cy + r * 0.45)], PIN_DARK)
    d.ellipse([*p(cx - 48, cy - 48), *p(cx + 48, cy + 48)], fill=PAPER_LIGHT)

    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    icon = draw_icon(SIZE)
    icon.save(OUT / "map-vietnam.png")
    icon.save(OUT / "map-vietnam.ico", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    try:
        icon.save(OUT / "map-vietnam.icns")
    except Exception as error:  # this Pillow cannot write .icns: iconutil on a Mac can
        print(f"no .icns written ({error}); build it from the PNG with iconutil")
    for path in sorted(OUT.iterdir()):
        print(path.name, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
