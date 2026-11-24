#!/usr/bin/env python3
"""Draws Map Vietnam's app icon and writes it in the formats each platform wants.

The icon is the app in one picture: the outline of Vietnam, its left half on an old paper map and
its right half on today's map, split by the swipe handle the viewer uses to compare the two. It is
drawn at 1024 px with Pillow (already in requirements.txt), so the icons are reproducible and no
binary has to be edited by hand. The outline is Natural Earth's 1:50m country boundary (public
domain, naturalearthdata.com), simplified to about 100 points,
with the Mekong's river mouths smoothed over (at icon size they only look like stray marks).

    python packaging/make_icons.py

writes into packaging/icons/:
  map-vietnam.png   1024 px: the Linux icon, and the source of the .icns
  map-vietnam.ico   Windows, 16 to 256 px in one file
  map-vietnam.icns  macOS, only if Pillow can write it here (otherwise make it with iconutil on a Mac)

Run it again and commit the result whenever the design changes.
"""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent / "icons"
SIZE = 1024
SCALE = 2  # draw at twice the size and shrink, for smooth edges

# The old map: aged paper, sepia land, brown ink.
PAPER = (240, 226, 190)
PAPER_EDGE = (205, 178, 128)
OLD_LAND = (222, 196, 142)
OLD_INK = (112, 78, 42)
# Today's map: the viewer's dark theme, with its accent green (web/app/app.css, --accent) for land.
SEA = (26, 74, 82)
NEW_LAND = (38, 148, 118)
NEW_EDGE = (190, 238, 220)
HANDLE = (250, 248, 242)
ACCENT = (14, 90, 75)

# Longitude, latitude. Natural Earth 1:50m (public domain), simplified.
MAINLAND = [
    (107.97, 21.51),
    (107.41, 21.28),
    (107.35, 21.06),
    (107.16, 20.95),
    (106.68, 21.00),
    (106.75, 20.74),
    (106.52, 20.29),
    (105.98, 19.94),
    (105.62, 18.97),
    (105.89, 18.50),
    (106.50, 17.95),
    (106.48, 17.72),
    (106.37, 17.75),
    (107.83, 16.32),
    (108.03, 16.33),
    (108.82, 15.38),
    (109.30, 13.86),
    (109.27, 13.28),
    (109.42, 12.96),
    (109.44, 12.60),
    (109.34, 12.75),
    (109.22, 12.65),
    (109.30, 12.39),
    (109.21, 12.42),
    (109.20, 11.72),
    (108.99, 11.34),
    (108.09, 10.90),
    (108.00, 10.72),
    (107.26, 10.40),
    (107.01, 10.66),
    (106.95, 10.40),
    (106.80, 10.40),
    (106.79, 10.12),
    (106.60, 9.86),
    (106.48, 9.56),
    (106.17, 9.40),
    (105.50, 9.09),
    (105.11, 8.63),
    (104.77, 8.60),
    (104.90, 8.75),
    (104.85, 9.61),
    (105.08, 10.00),
    (104.43, 10.41),
    (104.85, 10.53),
    (105.05, 10.70),
    (105.05, 10.91),
    (105.31, 10.85),
    (105.76, 10.99),
    (106.16, 10.79),
    (106.16, 11.04),
    (105.86, 11.29),
    (105.85, 11.64),
    (106.01, 11.76),
    (106.40, 11.69),
    (106.41, 11.95),
    (107.51, 12.36),
    (107.48, 13.03),
    (107.61, 13.44),
    (107.33, 14.13),
    (107.54, 14.65),
    (107.48, 14.98),
    (107.65, 15.26),
    (107.17, 15.80),
    (107.40, 16.04),
    (106.85, 16.52),
    (106.66, 16.49),
    (106.50, 16.95),
    (105.11, 18.41),
    (105.15, 18.65),
    (103.89, 19.30),
    (104.06, 19.48),
    (104.03, 19.68),
    (104.59, 19.62),
    (104.93, 20.02),
    (104.85, 20.20),
    (104.37, 20.44),
    (104.58, 20.65),
    (104.10, 20.95),
    (103.64, 20.70),
    (103.10, 20.89),
    (102.85, 21.27),
    (102.95, 21.68),
    (102.82, 21.81),
    (102.66, 21.68),
    (102.13, 22.38),
    (102.47, 22.75),
    (102.98, 22.45),
    (103.33, 22.77),
    (103.49, 22.59),
    (103.62, 22.78),
    (103.94, 22.54),
    (104.14, 22.80),
    (104.37, 22.70),
    (104.69, 22.82),
    (104.86, 23.14),
    (105.28, 23.35),
    (105.84, 22.92),
    (106.15, 22.97),
    (106.78, 22.78),
    (106.55, 22.50),
    (106.66, 21.98),
    (106.97, 21.92),
    (107.35, 21.61),
    (107.76, 21.66),
]
PHU_QUOC = [
    (104.06, 10.39),
    (104.05, 10.06),
    (104.02, 10.03),
    (103.95, 10.24),
    (103.85, 10.37),
    (103.99, 10.43),
]

LON0, LAT0 = 105.8, 15.95  # the outline's centre, placed at the middle of the tile
PX_PER_DEG = 56.0
COS = math.cos(math.radians(LAT0))  # a plain equirectangular view, true to shape at this latitude
DIVIDER_LON = 106.4  # crosses the country from north to south, through the narrow waist


def to_px(lon, lat):
    """Design pixels (0..1024) for a longitude and latitude."""
    return SIZE / 2 + (lon - LON0) * COS * PX_PER_DEG, SIZE / 2 - (lat - LAT0) * PX_PER_DEG


def draw_icon(size=SIZE):
    """The icon as an RGBA image, size x size pixels. Below 64 px (taskbars, title bars, file lists)
    the grid, the handle and the thin coast lines would only be noise, so the small icons keep the
    two halves, the divider and the country as solid shapes in stronger colours."""
    big = SIZE * SCALE
    small = size < 64

    def p(x, y):
        return (x * SCALE, y * SCALE)

    def shape(points):
        return [p(*to_px(lon, lat)) for lon, lat in points]

    def graticule(image, color, width):
        """Every second degree, drawn on its own layer so its transparency blends."""
        layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        for lon in range(96, 118, 2):
            d.line([p(*to_px(lon, -10)), p(*to_px(lon, 40))], fill=color, width=width * SCALE)
        for lat in range(-10, 40, 2):
            d.line([p(*to_px(90, lat)), p(*to_px(125, lat))], fill=color, width=width * SCALE)
        image.alpha_composite(layer)

    def land(image, fill, coast, width):
        """Filled land with a coast line of even width (polygon outlines in Pillow break at corners)."""
        d = ImageDraw.Draw(image)
        for ring in (MAINLAND, PHU_QUOC):
            points = shape(ring)
            d.polygon(points, fill=fill)
            d.line([*points, points[0], points[1]], fill=coast, width=width * SCALE, joint="curve")

    # The old map: paper darkening towards its edges, a faint graticule, sepia land with an inked coast.
    old = Image.new("RGBA", (big, big), PAPER_EDGE)
    glow = Image.new("L", (big, big), 0)
    ImageDraw.Draw(glow).ellipse([*p(-200, -150), *p(1224, 1174)], fill=255)
    old.paste(Image.new("RGBA", (big, big), PAPER), (0, 0), glow.filter(ImageFilter.GaussianBlur(160 * SCALE)))
    if small:
        land(old, OLD_INK, OLD_INK, 24)
    else:
        graticule(old, (*OLD_INK, 70), 3)
        land(old, OLD_LAND, OLD_INK, 8)

    # Today's map: dark sea, green land with a light coast.
    new = Image.new("RGBA", (big, big), SEA)
    if small:
        land(new, NEW_EDGE, NEW_EDGE, 24)
    else:
        graticule(new, (255, 255, 255, 26), 3)
        land(new, NEW_LAND, NEW_EDGE, 6)

    # Old on the left of the divider, today on the right.
    split = to_px(DIVIDER_LON, 0)[0]
    left = Image.new("L", (big, big), 0)
    ImageDraw.Draw(left).rectangle([0, 0, split * SCALE, big], fill=255)
    art = new.copy()
    art.paste(old, (0, 0), left)

    # The divider and its round handle with two arrows, as in the viewer, with a soft shadow.
    if small:
        ImageDraw.Draw(art).rectangle([*p(split - 22, 0), *p(split + 22, SIZE)], fill=HANDLE)
        return finish(art, size)
    hy = 470
    shadow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rectangle([*p(split - 8, 0), *p(split + 8, SIZE)], fill=(0, 0, 0, 110))
    sd.ellipse([*p(split - 74, hy - 70), *p(split + 74, hy + 78)], fill=(0, 0, 0, 120))
    art.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14 * SCALE)))
    d = ImageDraw.Draw(art)
    d.rectangle([*p(split - 6, 0), *p(split + 6, SIZE)], fill=HANDLE)
    d.ellipse([*p(split - 68, hy - 68), *p(split + 68, hy + 68)], fill=HANDLE)
    for sign in (-1, 1):  # a chevron each way
        tip, back = split + sign * 44, split + sign * 14
        d.line([p(back, hy - 26), p(tip, hy), p(back, hy + 26)], fill=ACCENT, width=14 * SCALE, joint="curve")

    return finish(art, size)


def finish(art, size):
    """Cut the picture to the rounded tile, with a faint light rim so its edge shows on dark
    backgrounds too, and shrink it to size."""
    big = SIZE * SCALE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, big - 1, big - 1], radius=230 * SCALE, fill=255)
    rim = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    ImageDraw.Draw(rim).rounded_rectangle(
        [0, 0, big - 1, big - 1], radius=230 * SCALE, outline=(255, 255, 255, 60), width=6 * SCALE
    )
    art = art.copy()
    art.alpha_composite(rim)
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    img.paste(art, (0, 0), mask)
    return img.resize((size, size), Image.LANCZOS)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    icon = draw_icon(SIZE)
    icon.save(OUT / "map-vietnam.png")
    # Each size drawn for itself, so the small ones get the simpler design.
    sizes = (16, 24, 32, 48, 64, 128, 256)
    icon.save(OUT / "map-vietnam.ico", sizes=[(n, n) for n in sizes], append_images=[draw_icon(n) for n in sizes])
    try:
        icon.save(OUT / "map-vietnam.icns")
    except Exception as error:  # this Pillow cannot write .icns: iconutil on a Mac can
        print(f"no .icns written ({error}); build it from the PNG with iconutil")
    for path in sorted(OUT.iterdir()):
        print(path.name, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
