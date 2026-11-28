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
import math
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


def _hartley_norm(pts):
    """Center `pts` (N, 2) and scale so mean distance from origin is sqrt(2); returns (norm_pts, T_3x3)."""
    mean = pts.mean(axis=0)
    shifted = pts - mean
    dist = np.hypot(shifted[:, 0], shifted[:, 1]).mean()
    s = math.sqrt(2.0) / dist if dist > 1e-12 else 1.0
    T = np.array([[s, 0.0, -s * mean[0]], [0.0, s, -s * mean[1]], [0.0, 0.0, 1.0]], dtype=np.float64)
    hom = np.column_stack([pts, np.ones(len(pts), dtype=np.float64)])
    return (T @ hom.T).T[:, :2], T


def homography_fit(gcps):
    """Normalized DLT projective homography `H` (3x3) mapping `(col, row) -> (x, y)` for 4+ GCPs."""
    if len(gcps) < 4:
        raise ValueError("Projective homography requires at least 4 control points.")
    src = np.array([[g.col, g.row] for g in gcps], dtype=np.float64)
    dst = np.array([[g.x, g.y] for g in gcps], dtype=np.float64)
    src_n, T_s = _hartley_norm(src)
    dst_n, T_d = _hartley_norm(dst)
    rows = []
    for (u, v), (x, y) in zip(src_n, dst_n, strict=True):
        rows.append([-u, -v, -1.0, 0.0, 0.0, 0.0, u * x, v * x, x])
        rows.append([0.0, 0.0, 0.0, -u, -v, -1.0, u * y, v * y, y])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    H_n = vt[-1].reshape(3, 3)
    H = np.linalg.inv(T_d) @ H_n @ T_s
    if abs(H[2, 2]) > 1e-12:
        H = H / H[2, 2]
    return H


def apply_homography(H, col, row):
    """Project one pixel `(col, row)` through 3x3 homography `H` -> `(x, y)`."""
    w = H[2, 0] * col + H[2, 1] * row + H[2, 2]
    if abs(w) < 1e-12:
        raise ValueError("Degenerate homography projection.")
    x = (H[0, 0] * col + H[0, 1] * row + H[0, 2]) / w
    y = (H[1, 0] * col + H[1, 1] * row + H[1, 2]) / w
    return float(x), float(y)


def homography_residuals(gcps):
    """Per-GCP Euclidean residual against a fitted projective homography."""
    H = homography_fit(gcps)
    errs = []
    for g in gcps:
        px, py = apply_homography(H, g.col, g.row)
        errs.append(math.hypot(px - g.x, py - g.y))
    return np.asarray(errs, dtype=np.float64)


def warp_homography_to_grid(bands, gcps):
    """Inverse-map `(4, h, w)` RGBA image through a fitted homography onto an EPSG:4326 grid.

    Used for oblique / angled aerial photographs so projective foreshortening is corrected before
    Web Mercator tiling (`write_tiles(..., transform=aff)`). Returns `(warped_bands, Affine)`.
    """
    _, h, w = bands.shape
    H = homography_fit(gcps)
    H_inv = np.linalg.inv(H)

    xs, ys = [], []
    for col, row in (
        (0.0, 0.0),
        (w, 0.0),
        (w, h),
        (0.0, h),
        (w / 2.0, 0.0),
        (w, h / 2.0),
        (w / 2.0, h),
        (0.0, h / 2.0),
    ):
        px, py = apply_homography(H, col, row)
        xs.append(px)
        ys.append(py)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    if not (max_x > min_x and max_y > min_y) or (max_x - min_x) > 40.0 or (max_y - min_y) > 40.0:
        raise ValueError("Homography corners diverge or span too wide an area; add better spread control points.")

    mid_lat = 0.5 * (min_y + max_y)
    cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
    span_x = (max_x - min_x) * cos_lat
    span_y = max_y - min_y
    target_long = min(max(int(round(max(w, h) * 1.25)), 512), 10000)
    if span_x >= span_y:
        out_w = target_long
        out_h = max(64, int(round(target_long * (span_y / span_x))))
    else:
        out_h = target_long
        out_w = max(64, int(round(target_long * (span_x / span_y))))

    dx = (max_x - min_x) / out_w
    dy = (max_y - min_y) / out_h
    aff = Affine(dx, 0.0, min_x, 0.0, -dy, max_y)

    cols = np.arange(out_w, dtype=np.float64) + 0.5
    rows = np.arange(out_h, dtype=np.float64) + 0.5
    gx = min_x + cols[None, :] * dx
    gy = max_y - rows[:, None] * dy

    denom = H_inv[2, 0] * gx + H_inv[2, 1] * gy + H_inv[2, 2]
    src_x = (H_inv[0, 0] * gx + H_inv[0, 1] * gy + H_inv[0, 2]) / denom
    src_y = (H_inv[1, 0] * gx + H_inv[1, 1] * gy + H_inv[1, 2]) / denom
    # Keep only forward projective rays matching center pixel orientation
    center_sign = (
        1.0
        if (H_inv[2, 0] * ((min_x + max_x) / 2.0) + H_inv[2, 1] * ((min_y + max_y) / 2.0) + H_inv[2, 2]) >= 0
        else -1.0
    )
    valid = (denom * center_sign > 0) & (src_x >= 0.0) & (src_x <= w - 1.0) & (src_y >= 0.0) & (src_y <= h - 1.0)

    warped = np.zeros((bands.shape[0], out_h, out_w), dtype=np.uint8)
    vy, vx = np.nonzero(valid)
    if len(vy):
        fx = src_x[vy, vx]
        fy = src_y[vy, vx]
        x0 = np.floor(fx).astype(np.int32)
        y0 = np.floor(fy).astype(np.int32)
        x1 = np.minimum(x0 + 1, w - 1)
        y1 = np.minimum(y0 + 1, h - 1)
        wx = fx - x0
        wy = fy - y0
        w00 = (1.0 - wx) * (1.0 - wy)
        w10 = wx * (1.0 - wy)
        w01 = (1.0 - wx) * wy
        w11 = wx * wy
        for b_idx in range(bands.shape[0]):
            ch = bands[b_idx]
            val = ch[y0, x0] * w00 + ch[y0, x1] * w10 + ch[y1, x0] * w01 + ch[y1, x1] * w11
            warped[b_idx, vy, vx] = np.clip(np.rint(val), 0, 255).astype(np.uint8)
    return warped, aff


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
    bands = np.dstack([img, alpha]).transpose(2, 0, 1)
    del img
    warp_mode = str(c.get("warp_mode") or "auto").lower()
    if warp_mode == "homography" and len(gcps) >= 4:
        err = homography_residuals(gcps)
        bands, hom_aff = warp_homography_to_grid(bands, gcps)
        warp = {"transform": hom_aff}
    elif warp_mode == "affine" or (warp_mode == "auto" and c.get("order") == 1):
        err = residuals(gcps)
        warp = {"transform": affine_fit(gcps)}
    else:
        err = residuals(gcps)
        warp = {"gcps": gcps}
        if warp_mode == "tps" or (warp_mode == "auto" and c.get("tps")):
            warp["tps"] = True
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
