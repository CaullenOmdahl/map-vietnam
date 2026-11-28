"""Composite strip mosaic builder: stitch placed aerial survey frames into one seamless MBTiles layer.

Given any placed scan belonging to a survey strip (`strip_chain.strip_key`), this module finds all
placed frames of that mission, trims dark film borders and caption strips, warps each frame onto a
shared WGS84 grid with smooth distance-to-edge feathering, writes the blended RGBA mosaic to
`web/tiles/<id>.mbtiles`, registers/updates the mosaic entry in `data/custom_maps/<id>/map.json`,
and refreshes `web/catalog.json`.
"""

import math
import secrets
import threading
from pathlib import Path

import custom_maps
import georef_scan
import numpy as np
import strip_chain
import tiling
from files import write_json
from PIL import Image, ImageOps
from rasterio.control import GroundControlPoint
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.warp import Resampling, reproject

# Cap the mosaic raster's longest side so memory stays bounded and tiling remains fast.
MAX_MOSAIC_SIDE = 8192
MIN_MOSAIC_SIDE = 64
# Linear feathering ramp width (source pixels inward from the trimmed frame edge).
FEATHER_PX = 50.0
# Minimum positive weight at the trimmed edge so non-overlapping outer edges stay opaque.
MIN_EDGE_WEIGHT = 1e-3
# Share of total progress allocated to warping/blending frames; the rest is tiling.
WARP_SHARE = 0.5
_MOSAIC_LOCKS_GUARD = threading.Lock()
_MOSAIC_LOCKS = {}


def _mission_lock(mission_key):
    with _MOSAIC_LOCKS_GUARD:
        return _MOSAIC_LOCKS.setdefault(mission_key, threading.Lock())


def trim_film_border_mask(img, *, dark=0.14, max_frac=0.18):
    """Detect dark film borders and black caption strips along each edge of `img`.

    `img` may be a Pillow `Image` or a NumPy array (2D grayscale or 3D RGB/RGBA).
    Returns `(left, top, right, bottom)` crop margins in pixels so dark borders and text bars
    (such as `EROM80.HV741...` along SHDAI frames) do not paint over neighbouring frames.
    """
    if isinstance(img, Image.Image):
        arr = np.asarray(ImageOps.exif_transpose(img).convert("L"), dtype=np.float32) / 255.0
    else:
        raw = np.asarray(img)
        if raw.ndim == 3:
            # Standard ITU-R BT.601 luminance from RGB(A).
            rgb = raw[..., :3].astype(np.float32)
            arr = 0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]
        else:
            arr = raw.astype(np.float32)
        if raw.dtype == np.uint8 or (arr.size and float(arr.max()) > 1.0):
            arr = arr / 255.0

    if arr.ndim != 2 or arr.size == 0:
        return 0, 0, 0, 0

    h, w = arr.shape
    lim_y = int(h * max_frac)
    lim_x = int(w * max_frac)
    row_med = np.median(arr, axis=1)
    col_med = np.median(arr, axis=0)

    top = 0
    while top < lim_y and row_med[top] < dark:
        top += 1

    bottom = 0
    while bottom < lim_y and row_med[h - 1 - bottom] < dark:
        bottom += 1

    left = 0
    while left < lim_x and col_med[left] < dark:
        left += 1

    right = 0
    while right < lim_x and col_med[w - 1 - right] < dark:
        right += 1

    min_interior = 32 if min(h, w) >= 64 else max(4, min(h, w) // 2)
    if (w - left - right) < min_interior or (h - top - bottom) < min_interior:
        return 0, 0, 0, 0
    return int(left), int(top), int(right), int(bottom)


def _frame_gcps(points):
    return [
        GroundControlPoint(row=float(r), col=float(c), x=float(lon), y=float(lat), z=0.0) for c, r, lon, lat in points
    ]


def _fit_frame_model(entry):
    """Fit pixel -> (lon, lat) transform for a placed scan entry.

    Returns `("homography", H_3x3)` when `warp_mode == "homography"` with >= 4 well-conditioned
    points, otherwise `("affine", Affine)`. Raises `ValueError` if points are degenerate.
    """
    points = entry.get("points") or []
    if len(points) < custom_maps.MIN_POINTS:
        raise ValueError(f"Frame {entry.get('id')} has fewer than {custom_maps.MIN_POINTS} control points.")

    wmode = str(entry.get("warp_mode") or "auto").lower()
    gcps = _frame_gcps(points)
    if wmode == "homography" and len(gcps) >= 4:
        try:
            H = georef_scan.homography_fit(gcps)
            H_inv = np.linalg.inv(H)
            if np.isfinite(H).all() and np.isfinite(H_inv).all():
                return "homography", H
        except (ValueError, np.linalg.LinAlgError):
            pass

    coef = custom_maps.affine_from_points(points)
    if coef is None:
        raise ValueError(f"Frame {entry.get('id')} has degenerate control points.")
    return "affine", georef_scan.affine_fit(gcps)


def _project_corners(mode, model, x0, y0, x1, y1):
    """Project rectangle corners `(x0, y0)..(x1, y1)` to WGS84 `(lon, lat)` tuples."""
    pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
    if mode == "homography":
        return [georef_scan.apply_homography(model, float(c), float(r)) for c, r in pts]
    return [tuple(float(v) for v in (model @ (float(c), float(r)))) for c, r in pts]


def _feather_weight(th, tw, feather_px=FEATHER_PX):
    """Smooth linear distance-to-valid-edge weight in `[MIN_EDGE_WEIGHT, 1.0]` for shape `(th, tw)`."""
    ramp = min(float(feather_px), max(1.0, min(th, tw) / 4.0))
    dy = np.minimum(np.arange(th, dtype=np.float32), np.arange(th - 1, -1, -1, dtype=np.float32))
    dx = np.minimum(np.arange(tw, dtype=np.float32), np.arange(tw - 1, -1, -1, dtype=np.float32))
    dist = np.minimum(dy[:, None], dx[None, :])
    return np.clip((dist + 1.0) / ramp, MIN_EDGE_WEIGHT, 1.0).astype(np.float32)


def _warp_affine_into(acc_rgb, acc_w, trimmed_rgb, weight, aff, x0, y0, mosaic_transform, sub_box):
    """Warp one trimmed RGB+weight frame via affine `rasterio.warp.reproject` into `acc_rgb`/`acc_w`."""
    r0, r1, c0, c1 = sub_box
    sub_h, sub_w = r1 - r0, c1 - c0
    premul = np.empty((4, trimmed_rgb.shape[0], trimmed_rgb.shape[1]), dtype=np.float32)
    premul[0] = trimmed_rgb[..., 0] * weight
    premul[1] = trimmed_rgb[..., 1] * weight
    premul[2] = trimmed_rgb[..., 2] * weight
    premul[3] = weight

    trimmed_aff = aff @ Affine.translation(float(x0), float(y0))
    sub_transform = mosaic_transform @ Affine.translation(float(c0), float(r0))
    dst = np.zeros((4, sub_h, sub_w), dtype=np.float32)
    reproject(
        premul,
        dst,
        src_transform=trimmed_aff,
        src_crs="EPSG:4326",
        dst_transform=sub_transform,
        dst_crs="EPSG:4326",
        dst_nodata=0.0,
        resampling=Resampling.bilinear,
    )
    acc_rgb[:, r0:r1, c0:c1] += dst[:3]
    acc_w[r0:r1, c0:c1] += dst[3]


def _warp_homography_into(acc_rgb, acc_w, trimmed_rgb, weight, H, x0, y0, mosaic_transform, sub_box):
    """Inverse-map one trimmed RGB+weight frame through homography `H` into `acc_rgb`/`acc_w`."""
    r0, r1, c0, c1 = sub_box
    th, tw = trimmed_rgb.shape[:2]
    H_inv = np.linalg.inv(H)

    cols = np.arange(c0, c1, dtype=np.float64) + 0.5
    rows = np.arange(r0, r1, dtype=np.float64) + 0.5
    gx = mosaic_transform.c + cols[None, :] * mosaic_transform.a
    gy = mosaic_transform.f + rows[:, None] * mosaic_transform.e

    denom = H_inv[2, 0] * gx + H_inv[2, 1] * gy + H_inv[2, 2]
    # Use the trimmed image center projected through H to determine forward ray sign.
    cx_geo, cy_geo = georef_scan.apply_homography(H, x0 + tw / 2.0, y0 + th / 2.0)
    center_sign = 1.0 if (H_inv[2, 0] * cx_geo + H_inv[2, 1] * cy_geo + H_inv[2, 2]) >= 0 else -1.0

    safe_denom = np.where(np.abs(denom) > 1e-12, denom, 1e-12)
    tx = (H_inv[0, 0] * gx + H_inv[0, 1] * gy + H_inv[0, 2]) / safe_denom - float(x0)
    ty = (H_inv[1, 0] * gx + H_inv[1, 1] * gy + H_inv[1, 2]) / safe_denom - float(y0)
    valid = (denom * center_sign > 0) & (tx >= 0.0) & (tx <= tw - 1.0) & (ty >= 0.0) & (ty <= th - 1.0)

    vy, vx = np.nonzero(valid)
    if not len(vy):
        return

    fx = tx[vy, vx]
    fy = ty[vy, vx]
    ix0 = np.floor(fx).astype(np.int32)
    iy0 = np.floor(fy).astype(np.int32)
    ix1 = np.minimum(ix0 + 1, tw - 1)
    iy1 = np.minimum(iy0 + 1, th - 1)
    wx = (fx - ix0).astype(np.float32)
    wy = (fy - iy0).astype(np.float32)
    w00 = (1.0 - wx) * (1.0 - wy)
    w10 = wx * (1.0 - wy)
    w01 = (1.0 - wx) * wy
    w11 = wx * wy

    wv = weight[iy0, ix0] * w00 + weight[iy0, ix1] * w10 + weight[iy1, ix0] * w01 + weight[iy1, ix1] * w11
    dst_y = vy + r0
    dst_x = vx + c0
    acc_w[dst_y, dst_x] += wv
    for ch in range(3):
        plane = trimmed_rgb[..., ch]
        cv = plane[iy0, ix0] * w00 + plane[iy0, ix1] * w10 + plane[iy1, ix0] * w01 + plane[iy1, ix1] * w11
        acc_rgb[ch, dst_y, dst_x] += cv * wv


def _mission_label(mission_key):
    """Short human-readable mission label from e.g. `SHDAI__AI_75_E_192_HV741` -> `HV741`."""
    tail = mission_key.rsplit("_", 1)[-1]
    return tail or mission_key


def _collect_placed_frames(entries, anchor):
    """Return `(mission_key, [(frame_number, entry), ...])` for placed scans in `anchor`'s strip."""
    key, grouped = strip_chain.strip_frames(entries, anchor)
    if key is None:
        raise ValueError("This map's title does not name a survey strip and frame number.")

    placed = []
    for frame_num in sorted(grouped):
        candidates = [
            e
            for e in grouped[frame_num]
            if not e.get("mosaic_mission") and len(e.get("points") or []) >= custom_maps.MIN_POINTS
        ]
        if not candidates:
            continue
        chosen = next((e for e in candidates if e["id"] == anchor["id"]), candidates[0])
        placed.append((frame_num, chosen))
    return key[0], placed


def build_strip_mosaic(lib, anchor_id, *, progress=None):
    """Build (or rebuild) a feathered composite strip mosaic for `anchor_id`'s mission.

    Finds all placed custom scan frames (`len(points) >= 3`) in the same survey mission as
    `anchor_id` (requiring at least 2 placed frames), trims dark film borders and text strips,
    warps and feathers all frames onto a shared WGS84 grid, writes `web/tiles/<id>.mbtiles`,
    creates or updates the custom map entry (tagged with `mosaic_mission`), updates
    `web/catalog.json`, and returns the mosaic entry dict.
    """
    report = progress or (lambda _fraction: None)
    anchor = lib.custom.get(anchor_id)
    mission_key, _ = _collect_placed_frames([anchor], anchor)
    with _mission_lock(mission_key):
        all_custom = lib.custom.entries()
        mission_key, placed_frames = _collect_placed_frames(all_custom, anchor)
        if len(placed_frames) < 2:
            raise ValueError(
                f"Need at least 2 placed frames in mission {mission_key} to build a strip mosaic "
                f"(found {len(placed_frames)})."
            )

        # Fit per-frame transforms and gather combined WGS84 bounds + native resolution estimate.
        frame_specs = []
        all_lons, all_lats = [], []
        deg_per_px_samples = []
        for frame_num, entry in placed_frames:
            work_path = lib.custom.work_image(entry["id"])
            if not work_path.exists():
                raise FileNotFoundError(f"Working image missing for frame {entry['id']}: {work_path}")
            mode, model = _fit_frame_model(entry)
            img_meta = entry.get("image") or {}
            if "width" in img_meta and "height" in img_meta:
                fw, fh = int(img_meta["width"]), int(img_meta["height"])
            else:
                with Image.open(work_path) as im:
                    fw, fh = im.size
            corners = _project_corners(mode, model, 0, 0, fw, fh)
            lons = [c[0] for c in corners]
            lats = [c[1] for c in corners]
            fb = (min(lons), min(lats), max(lons), max(lats))
            all_lons.extend(lons)
            all_lats.extend(lats)

            mid_lat = 0.5 * (fb[1] + fb[3])
            cos_f = max(0.2, math.cos(math.radians(mid_lat)))
            edge_top = math.hypot((corners[1][0] - corners[0][0]) * cos_f, corners[1][1] - corners[0][1])
            edge_left = math.hypot((corners[3][0] - corners[0][0]) * cos_f, corners[3][1] - corners[0][1])
            if fw > 0 and edge_top > 0:
                deg_per_px_samples.append(edge_top / fw)
            if fh > 0 and edge_left > 0:
                deg_per_px_samples.append(edge_left / fh)

            frame_specs.append((frame_num, entry, work_path, mode, model, fb))

        min_lon, max_lon = min(all_lons), max(all_lons)
        min_lat, max_lat = min(all_lats), max(all_lats)
        if not (max_lon > min_lon and max_lat > min_lat):
            raise ValueError("Combined strip mosaic bounds are empty.")
        mosaic_bounds = [min_lon, min_lat, max_lon, max_lat]

        mid_lat = 0.5 * (min_lat + max_lat)
        cos_lat = max(0.2, math.cos(math.radians(mid_lat)))
        span_x = (max_lon - min_lon) * cos_lat
        span_y = max_lat - min_lat
        native_deg_per_px = float(np.median(deg_per_px_samples)) if deg_per_px_samples else max(span_x, span_y) / 1024.0
        ideal_long = int(round(max(span_x, span_y) / max(native_deg_per_px, 1e-12)))
        target_long = max(MIN_MOSAIC_SIDE, min(MAX_MOSAIC_SIDE, ideal_long))
        if span_x >= span_y:
            out_w = target_long
            out_h = max(MIN_MOSAIC_SIDE, int(round(target_long * (span_y / span_x))))
        else:
            out_h = target_long
            out_w = max(MIN_MOSAIC_SIDE, int(round(target_long * (span_x / span_y))))

        dx = (max_lon - min_lon) / out_w
        dy = (max_lat - min_lat) / out_h
        mosaic_transform = Affine(dx, 0.0, min_lon, 0.0, -dy, max_lat)

        acc_rgb = np.zeros((3, out_h, out_w), dtype=np.float32)
        acc_w = np.zeros((out_h, out_w), dtype=np.float32)

        n_frames = len(frame_specs)
        for idx, (_frame_num, _entry, work_path, mode, model, fb) in enumerate(frame_specs):
            with Image.open(work_path) as raw:
                rgb = np.asarray(ImageOps.exif_transpose(raw).convert("RGB"), dtype=np.float32)
            h, w = rgb.shape[:2]
            left, top, right, bottom = trim_film_border_mask(rgb)
            x0, y0 = left, top
            x1, y1 = w - right, h - bottom
            trimmed_rgb = rgb[y0:y1, x0:x1]
            weight = _feather_weight(y1 - y0, x1 - x0)

            # Restrict warping to the mosaic sub-window covered by this frame.
            c0 = max(0, int(math.floor((fb[0] - min_lon) / dx)) - 1)
            c1 = min(out_w, int(math.ceil((fb[2] - min_lon) / dx)) + 1)
            r0 = max(0, int(math.floor((max_lat - fb[3]) / dy)) - 1)
            r1 = min(out_h, int(math.ceil((max_lat - fb[1]) / dy)) + 1)
            if c1 > c0 and r1 > r0:
                sub_box = (r0, r1, c0, c1)
                if mode == "homography":
                    _warp_homography_into(acc_rgb, acc_w, trimmed_rgb, weight, model, x0, y0, mosaic_transform, sub_box)
                else:
                    _warp_affine_into(acc_rgb, acc_w, trimmed_rgb, weight, model, x0, y0, mosaic_transform, sub_box)

            report(WARP_SHARE * (idx + 1) / n_frames)

        valid = acc_w > 1e-5
        bands = np.zeros((4, out_h, out_w), dtype=np.uint8)
        if np.any(valid):
            inv_w = np.zeros_like(acc_w, dtype=np.float32)
            np.divide(np.float32(1.0), acc_w, out=inv_w, where=valid)
            for ch in range(3):
                acc_rgb[ch] *= inv_w
                np.clip(np.rint(acc_rgb[ch]), 0, 255, out=acc_rgb[ch])
                bands[ch] = acc_rgb[ch].astype(np.uint8)
            bands[3, valid] = 255
            del inv_w
        del acc_rgb, acc_w

        existing = next((e for e in all_custom if e.get("mosaic_mission") == mission_key), None)
        map_id = existing["id"] if existing else f"custom_{secrets.token_hex(5)}"
        folder = lib.custom.folder(map_id)
        folder.mkdir(parents=True, exist_ok=True)

        year = anchor.get("year") or next((e.get("year") for _, e in placed_frames if e.get("year")), None)
        title = f"{_mission_label(mission_key)} strip mosaic ({n_frames} frames)"
        max_zoom = min(custom_maps.MAX_ZOOM, tiling.native_zoom(mosaic_bounds, out_w))
        out_path = Path(lib.tiles_path(map_id))

        _written, tile_bounds, max_zoom = tiling.write_tiles(
            bands,
            CRS.from_epsg(4326),
            out_path,
            transform=mosaic_transform,
            max_zoom=max_zoom,
            bounds=mosaic_bounds,
            name=title,
            meta={"date": str(year) if year else ""},
            progress=lambda frac: report(WARP_SHARE + (1.0 - WARP_SHARE) * frac),
        )

        entry = {
            "id": map_id,
            "title": title,
            "year": year,
            "date": str(year) if year else "",
            "series": custom_maps.SERIES,
            "kind": "mosaic",
            "custom": True,
            "filename": f"{mission_key}_mosaic.mbtiles",
            "bytes": out_path.stat().st_size,
            "bounds": [round(float(v), 6) for v in tile_bounds],
            "region": "",
            "scale": "",
            "urls": [],
            "sha256": None,
            "points": [],
            "mosaic_mission": mission_key,
            "mosaic_frames": [{"id": e["id"], "frame": n} for n, e in placed_frames],
        }
        with lib.custom._lock:
            write_json(lib.custom.entry_path(map_id), entry)
        lib.write_catalog()
        report(1.0)
        return entry
