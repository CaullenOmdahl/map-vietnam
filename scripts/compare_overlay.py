"""Render a georeferenced layer around a point next to references, to judge alignment.

Writes one PNG with three panels at the same zoom and extent:
  [layer] [reference: the verified 1965-70 L7014 sheets, else OSM] [layer blended over OSM]
plus a 500 m scale bar and a crosshair at the point. Use it to measure how far a feature
(a canal junction, a river bend) sits from where modern data puts it.

Reads tiles from web/tiles/<layer_id>.mbtiles and the installed L7014 sheets, and fetches the OSM tiles it needs
(falling back to the L7014 sheets if OSM refuses). Writes data/work/compare_<layer>_<lon>_<lat>.png
unless --out is given, and prints the path.

Usage: .venv/bin/python scripts/compare_overlay.py <layer_id> <lon> <lat> [--zoom 14] [--out file.png]
"""

import argparse
import io
import math
import urllib.request
from contextlib import closing
from pathlib import Path

import morecantile
from PIL import Image, ImageDraw
from tiling import connect_read_only, read_tile

ROOT = Path(__file__).resolve().parent.parent
TILES = ROOT / "web" / "tiles"
TMS = morecantile.tms.get("WebMercatorQuad")
TILE_PX = 256
GRID = 3  # tiles per side of each panel, centred on the tile holding the point
PANEL_PX = TILE_PX * GRID
GAP_PX = 10
SCALE_BAR_M = 500
# OSM's tile policy asks for an identifying User-Agent; this tool fetches only 9 tiles a run.
OSM_HEADERS = {"User-Agent": "map-vietnam-compare-overlay/0.1 (research tool, low volume)"}


def stitch(layers, z, cx, cy, osm=False):
    """A GRID x GRID mosaic of tiles around tile (cx, cy) at zoom z.

    Tiles from each of `layers` (MBTiles files) are composited in order, later over earlier; missing tiles
    stay transparent. With osm=True the tiles come from the OpenStreetMap tile server instead
    (raises OSError if it cannot be reached or refuses).
    """
    out = Image.new("RGBA", (PANEL_PX, PANEL_PX), (255, 255, 255, 0))
    for dx in range(GRID):
        for dy in range(GRID):
            x, y = cx - GRID // 2 + dx, cy - GRID // 2 + dy
            at = (dx * TILE_PX, dy * TILE_PX)
            if osm:
                req = urllib.request.Request(f"https://tile.openstreetmap.org/{z}/{x}/{y}.png", headers=OSM_HEADERS)
                with urllib.request.urlopen(req, timeout=20) as r:
                    out.alpha_composite(Image.open(io.BytesIO(r.read())).convert("RGBA"), at)
                continue
            for path in layers:
                with closing(connect_read_only(path)) as db:
                    data = read_tile(db, z, x, y)
                if data:
                    out.alpha_composite(Image.open(io.BytesIO(data)).convert("RGBA"), at)
    return out


def main():
    """Stitch the layer, the L7014 reference sheets and OSM around one point and write the comparison PNG."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("layer", help="map id: web/tiles/<id>.mbtiles")
    ap.add_argument("lon", type=float, help="WGS84 longitude of the point to check")
    ap.add_argument("lat", type=float, help="WGS84 latitude of the point to check")
    ap.add_argument("--zoom", type=int, default=14)
    ap.add_argument("--out", help="output PNG path")
    args = ap.parse_args()

    centre = TMS.tile(args.lon, args.lat, args.zoom)
    nw = TMS.xy_bounds(morecantile.Tile(centre.x - GRID // 2, centre.y - GRID // 2, args.zoom))
    res = (nw.right - nw.left) / TILE_PX  # Web Mercator metres per pixel
    px, py = TMS.xy(args.lon, args.lat)
    cross_x, cross_y = (px - nw.left) / res, (nw.top - py) / res

    layer = stitch([TILES / f"{args.layer}.mbtiles"], args.zoom, centre.x, centre.y)
    ref = stitch(sorted(TILES.glob("L701*.mbtiles")), args.zoom, centre.x, centre.y)
    try:
        osm = stitch([], args.zoom, centre.x, centre.y, osm=True)
    except OSError as e:  # OSM tile servers sometimes refuse scripted requests; fall back to the sheets
        print(f"OSM unavailable ({e}); blending over the reference sheets instead")
        osm = Image.alpha_composite(Image.new("RGBA", ref.size, "white"), ref)
    if not ref.getbbox():  # no L7014 sheet covers the point
        ref = osm
    see_through_layer = Image.blend(Image.new("RGBA", layer.size, (0, 0, 0, 0)), layer, 0.6)
    blend = Image.alpha_composite(osm, see_through_layer)

    sheet = Image.new("RGB", (PANEL_PX * 3 + GAP_PX * 2, PANEL_PX + 30), "white")
    # Web Mercator stretches distances by 1/cos(lat); undo that for a true ground scale bar.
    ground_m_per_px = res * math.cos(math.radians(args.lat))
    bar_px = SCALE_BAR_M / ground_m_per_px
    for i, panel in enumerate([layer, ref, blend]):
        bg = Image.new("RGBA", panel.size, "white")
        bg.alpha_composite(panel)
        d = ImageDraw.Draw(bg)
        d.line([(cross_x - 12, cross_y), (cross_x + 12, cross_y)], fill="red", width=2)
        d.line([(cross_x, cross_y - 12), (cross_x, cross_y + 12)], fill="red", width=2)
        sheet.paste(bg.convert("RGB"), (i * (PANEL_PX + GAP_PX), 0))
    d = ImageDraw.Draw(sheet)
    d.line([(10, PANEL_PX + 15), (10 + bar_px, PANEL_PX + 15)], fill="black", width=3)
    caption = f"{SCALE_BAR_M} m   z{args.zoom}   panels: {args.layer} | reference | blend over OSM"
    d.text((14 + bar_px, PANEL_PX + 8), caption, fill="black")
    out = Path(args.out or ROOT / "data" / "work" / f"compare_{args.layer}_{args.lon:.3f}_{args.lat:.3f}.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)
    print(out)


if __name__ == "__main__":
    main()
