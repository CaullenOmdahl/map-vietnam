"""Georeference a plain scanned map from hand-picked control points and tile it into one MBTiles file.

Each scan has a control file data/scans/<layer_id>.json:
{
  "image": "data/raw/scans/<id>/work.jpg",
  "crs": "EPSG:4326",              # CRS the control-point coordinates are in (proj string ok)
  "gcps": [[col, row, x, y], ...], # pixel (col, row) -> coordinate; >= 3, ideally 6+ spread out
  "neatline": [[col, row], ...],   # optional: map frame, everything outside is made transparent
  "max_zoom": 13,                  # optional: otherwise picked from the scan's resolution
  "order": 1,                      # optional: 1 = affine fit (use when points are few or lie in
                                   #   few rows/columns); default lets GDAL pick a polynomial order
  "offset_deg": [0.0045, 0.0],     # optional: whole-sheet shift (lon, lat) added to every point,
                                   #   e.g. an old longitude origin measured against modern maps;
                                   #   the measured points stay untouched in "gcps"
  "meta": {"title", "series", "scale", "date", "dateNote", "source"}
}
Pixel coordinates refer to that exact image: the map library (scripts/library.py) downloads it
and, where the archive serves another format, reproduces it pixel for pixel (download_maps.py).

Output: one MBTiles file (default web/tiles/<layer_id>.mbtiles), written as .partial and renamed
when complete; the control file's "meta" block goes into its metadata.

Usage: .venv/bin/python scripts/georef_scan.py data/scans/<id>.json [OUT.mbtiles] [--image FILE] [--progress]
"""

import argparse
import json
from pathlib import Path

import numpy as np
import paths
from PIL import Image
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import Affine
from tiling import write_tiles

ROOT = paths.DATA_ROOT  # a control file's "image" path (data/raw/...) is relative to this
TILES = paths.TILES
# The scans are up to 10,000 x 10,000 px: far past Pillow's decompression-bomb guard, and trusted.
Image.MAX_IMAGE_PIXELS = None
# Share of a build's time spent reading the scan and masking it; the rest is tiling.
LOAD_SHARE = 0.15


def residuals(gcps):
    """Misfit of each control point (in control-point units, e.g. degrees) against a first-order
    fit to all of them: one point far above the rest is usually a mistyped coordinate."""
    a = np.array([[g.col, g.row, 1.0] for g in gcps])
    b = np.array([[g.x, g.y] for g in gcps])
    coef, *_ = np.linalg.lstsq(a, b, rcond=None)
    return np.hypot(*(a @ coef - b).T)


def affine_fit(gcps):
    """Least-squares pixel -> coordinate affine transform from control points."""
    a = np.array([[g.col, g.row, 1.0] for g in gcps])
    b = np.array([[g.x, g.y] for g in gcps])
    (ax, ay), (bx, by), (cx, cy) = np.linalg.lstsq(a, b, rcond=None)[0]
    return Affine(ax, bx, cx, ay, by, cy)


def build(control_path, out, *, image=None, progress=None):
    """Warp and tile the scan a control file describes into the MBTiles file `out`. `image`
    overrides the control file's image path (which is relative to the data folder, paths.DATA_ROOT).
    Returns a report dict: {"id", "tiles", "bounds", "maxzoom", "misfit"}."""
    report = progress or (lambda fraction: None)
    layer_id = control_path.stem
    c = json.loads(control_path.read_text(encoding="utf-8"))
    image = Path(image) if image else ROOT / c["image"]
    if not image.exists():
        raise FileNotFoundError(f"{image} is missing: add the map again to download it")

    img = np.asarray(Image.open(image).convert("RGB"))
    h, w = img.shape[:2]
    alpha = np.full((h, w), 255, np.uint8)
    if c.get("neatline"):
        ring = [list(p) for p in c["neatline"]] + [list(c["neatline"][0])]
        # Pixel space: identity transform, so geometry coordinates are (col, row).
        inside = ~geometry_mask([{"type": "Polygon", "coordinates": [ring]}], (h, w), Affine.identity())
        alpha = (inside * 255).astype(np.uint8)

    dx, dy = c.get("offset_deg", (0.0, 0.0))
    # z must be a number: rasterio passes z=None to GDAL as "None", and GDAL then drops every point.
    gcps = [GroundControlPoint(row=r, col=col, x=x + dx, y=y + dy, z=0.0) for col, r, x, y in c["gcps"]]
    err = residuals(gcps)
    bands = np.dstack([img, alpha]).transpose(2, 0, 1)
    del img
    warp = {"transform": affine_fit(gcps)} if c.get("order") == 1 else {"gcps": gcps}
    report(LOAD_SHARE)
    meta = c.get("meta", {})
    n, bounds, max_zoom = write_tiles(
        bands,
        CRS.from_user_input(c["crs"]),
        out,
        max_zoom=c.get("max_zoom"),
        name=meta.get("title", layer_id),
        meta={"date": meta.get("date", ""), "date_note": meta.get("dateNote", "")},
        progress=lambda fraction: report(LOAD_SHARE + (1 - LOAD_SHARE) * fraction),
        **warp,
    )
    return {"id": layer_id, "tiles": n, "bounds": bounds, "maxzoom": max_zoom, "misfit": round(float(err.max()), 6)}


def main(argv=None):
    """Build one scan; print a one-line report (and progress lines with --progress)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("control", type=Path, help="the scan's control file, data/scans/<id>.json")
    ap.add_argument("out", type=Path, nargs="?", help="MBTiles file to write (default web/tiles/<id>.mbtiles)")
    ap.add_argument("--image", type=Path, help="the scan image (default: the control file's)")
    ap.add_argument("--progress", action="store_true", help='print "progress <0..1>" lines while working')
    args = ap.parse_args(argv)

    def progress(fraction):
        print(f"progress {fraction:.3f}", flush=True)

    out = args.out or TILES / f"{args.control.stem}.mbtiles"
    result = build(args.control, out, image=args.image, progress=progress if args.progress else None)
    print(json.dumps({"done": result}), flush=True)


if __name__ == "__main__":
    main()
