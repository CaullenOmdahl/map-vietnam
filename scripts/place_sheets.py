"""Place scanned map sheets automatically from their printed graticule, and write the control files
georef_scan.py tiles them with.

This is a maintainer tool. It runs once per sheet, offline. Its output, data/scans/<id>.json, is
committed, so nobody who adds a map ever repeats the work: the map library downloads the image and
georef_scan.py places it from those control points.

It suits a sheet series that prints a regular graticule: the French Indochina 1:100,000 sheets
draw one every 0.2 grad (0.18 degrees), counted from the Paris meridian, as small crosses on the
older editions and as full lines on the later ones. Either way each graticule node is a "+": a
short horizontal and a short vertical stroke meeting, with blank (or at least different) paper in
the four quarters around them.

How a sheet is placed:
  1. A medium-resolution copy (WORK_WIDTH px wide) is scored for "+" shapes everywhere: for each
     pixel, how much of each of the four short arms leaving it is ink (the weakest arm counts, so
     a canal's corner or T-junction scores low), less how much ink fills the four quarters beside
     it (which rejects blobs, colour fills and lettering).
  2. The sheet's rough extent (from the archive) and its printed frame (on an overview) predict
     the graticule: where each node lies, to within a few per cent. The predicted lattice is then
     moved, scaled and turned as a whole to where the most nodes land on a clear "+" at once: every
     node is evidence together, which a stray "+" (a canal crossing) cannot outvote. Between
     equally good placings, the one nearest the prediction wins. The best few are then refined
     locally with the two directions scaled apart (the frame's band, and the sheet's projection,
     make the prediction's width and height err differently). Each node is then looked for near
     its place in the best lattice.
  3. Each matched node is measured again on a full-resolution crop (refined to sub-pixel by fitting
     its two strokes); a node that does not fit the others (CROSS_MAX_RESIDUAL) is dropped.
  4. The neatline (the map's edge, outside which everything is made transparent) is the sheet's
     extent put through that fit, moved onto the innermost printed rule near each side.

Checks, all of which must pass or the sheet is listed for review instead of written: at least
MIN_NODES nodes on two meridians and two parallels (a half sheet may have only four in all, one
often lost under an inset or a label); the fit's scale within SCALE_TOLERANCE of the prediction,
its rotation under MAX_ROTATION_DEG and its shear under MAX_SHEAR_DEG; residuals under
CROSS_MAX_RESIDUAL px; and the placed sheet's middle within MAX_CENTRE_SHIFT_KM of the archive's
extent (a lattice paired one node off would move the sheet a whole graticule step, about 20 km).

Images come from an IIIF image service (NAKALA) or a plain image file. Either way the whole image
is downloaded once, at full size, and the overview, the medium copy and the crops are made from it
here: NAKALA makes each IIIF image on request, which takes several seconds however small, so one
large request is quicker than many small ones. Every download goes through common.cached_get, so
a rerun fetches nothing new. --part i/n places every n-th sheet starting at the i-th, so a long
run can be shared between processes.

Usage:
  .venv/bin/python scripts/place_sheets.py SHEETS.json [--only ID ...] [--part I/N] [--review OUT.json] [--cache DIR]
SHEETS.json: [{"id", "image": {"iiif": URL} or {"url": URL}, "bounds": [w, s, e, n],
               "graticule": {"step_deg", "origin_lon"}, "crs" (of the graticule, default WGS84),
               "dark_ink_only" (ignore coloured ink, such as a blue UTM grid), "offset_deg",
               "max_zoom", "max_extent_deg": [width, height] (an archive extent larger than this is
               not trusted), "meta": {...}}, ...]   (scripts/sheet_lists.py writes one)
The control file also records the image's pixel size and byte count (work_size, source_bytes),
which the map library catalogue copies.
"""

import argparse
import json
import math
import sys
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import common  # noqa: E402
import paths  # noqa: E402

ROOT = paths.APP_ROOT  # maintainer tool: its outputs go to the checkout's data/sources and data/scans
SCANS = ROOT / "data" / "scans"
CACHE = "map_library/placing"  # sub-folder of data/raw for the images fetched
REQUEST_INTERVAL_S = 1.2

OVERVIEW_WIDTH = 1300
WORK_WIDTH = 3200  # the medium copy searched for graticule nodes
INK_CONTRAST = 0.86  # ink is darker than this fraction of the paper around it (local, so colour fills don't count)
BACKGROUND_PX = 31  # size of the neighbourhood that "the paper around it" is measured over (work px)
ARM_PX = 7  # length of each arm looked for (work px), starting ARM_GAP px out from the node
ARM_GAP = 2
NODE_SCORE_MIN = 0.5  # a node's "+" scores at least this (weakest arm's ink less quarter ink, 0..1)
POOL_PX = 3  # a node may lie this far (work px) from where the lattice puts it, for the search
SEARCH_SHIFT = 0.35  # the lattice search moves the prediction up to this share of the spacing
# An archive's extent can be 10% out (NAKALA's, for some half sheets), so the first search scales widely.
SEARCH_SCALES = tuple(round(0.85 + 0.01 * i, 2) for i in range(31))
SEARCH_TURNS_DEG = (-0.6, -0.3, 0.0, 0.3, 0.6)
TOP_PLACINGS = 5  # placings of the first search refined in the second
FINE_SHIFT = 0.06  # the second search moves each placing up to this share of the spacing
FINE_SCALES = tuple(round(0.95 + 0.005 * i, 3) for i in range(21))
NODE_LOOK_PX = 10  # around its place in the best lattice, a node is looked for this far (work px)
FRAME_BAND = 0.02  # the frame's band (graduations, labels) takes about this share of each side
SCALE_TOLERANCE = 0.18  # the fitted scale may differ this much from the predicted one
MIN_NODES = 3
MAX_ROTATION_DEG = 2.0
MAX_SHEAR_DEG = 0.6
MAX_CENTRE_SHIFT_KM = 8.0
CROSS_WINDOW = 60  # half-size of the full-resolution window a node is measured in, px
CROSS_MAX_RESIDUAL = 4.0  # px at full resolution: a node further than this from the fit to the others is dropped
NEATLINE_SEARCH = 15  # how far (work px, about 250 m) from the predicted edge the printed neatline is looked for


class NotPlaced(Exception):
    """The sheet could not be placed reliably; str() says why."""


# --- image helpers --------------------------------------------------------------------------


def box_mean(a, h, w):
    """Mean of `a` over an h x w box centred on each pixel (edges padded), via an integral image."""
    ph, pw = h // 2, w // 2
    p = np.pad(a.astype(np.float32), ((ph + 1, ph), (pw + 1, pw)), mode="edge")
    s = p.cumsum(0).cumsum(1)
    out = s[h:, w:] - s[:-h, w:] - s[h:, :-w] + s[:-h, :-w]
    return out[: a.shape[0], : a.shape[1]] / (h * w)


def ink_mask(gray):
    """True where a pixel is ink: clearly darker than the paper (or colour fill) around it."""
    return gray < box_mean(gray, BACKGROUND_PX, BACKGROUND_PX) * INK_CONTRAST


def shift(a, dy, dx):
    """`a` moved by (dy, dx), edges filled with 0."""
    out = np.zeros_like(a)
    h, w = a.shape
    out[max(0, dy) : h + min(0, dy), max(0, dx) : w + min(0, dx)] = a[
        max(0, -dy) : h + min(0, -dy), max(0, -dx) : w + min(0, -dx)
    ]
    return out


def node_score(mask):
    """How much each pixel looks like a graticule node, from -1 to 1: the ink along the weakest of
    the four short arms leaving it, less the ink in the fullest of the four quarters beside it."""
    m = mask.astype(np.float32)
    horiz, vert = box_mean(m, 1, ARM_PX), box_mean(m, ARM_PX, 1)
    # An arm may run a pixel off the node's own row or column (rotation, line width): best of three.
    horiz = np.max([shift(horiz, d, 0) for d in (-1, 0, 1)], axis=0)
    vert = np.max([shift(vert, 0, d) for d in (-1, 0, 1)], axis=0)
    reach = ARM_GAP + ARM_PX // 2 + 1
    arms = [shift(horiz, 0, reach), shift(horiz, 0, -reach), shift(vert, reach, 0), shift(vert, -reach, 0)]
    q = ARM_PX // 2 + 1
    quarter = box_mean(m, q, q)
    off = q // 2 + 2
    quarters = np.max([shift(quarter, sy * off, sx * off) for sy in (-1, 1) for sx in (-1, 1)], axis=0)
    return np.min(arms, axis=0) - quarters


def max_pool(a, r):
    """Each pixel's largest value within r pixels along rows and columns (a square neighbourhood)."""
    out = a
    for axis_shift in ((1, 0), (0, 1)):
        acc = out.copy()
        for d in range(1, r + 1):
            acc = np.maximum(acc, shift(out, d * axis_shift[0], d * axis_shift[1]))
            acc = np.maximum(acc, shift(out, -d * axis_shift[0], -d * axis_shift[1]))
        out = acc
    return out


def outer_frame(overview):
    """(left, top, right, bottom) of the outermost long straight rules on an overview, in its pixels."""
    mask = overview < np.percentile(overview, 90) * 0.83
    sides = []
    for m, n in ((mask, mask.shape[1]), (mask.T, mask.shape[0])):
        # Merge a few rows so a slightly rotated rule still fills one row along its length.
        k = max(3, n // 120)
        thick = m.copy()
        for i in range(1, k + 1):
            thick[i:] |= m[:-i]
            thick[:-i] |= m[i:]
        rows = np.flatnonzero(thick.mean(axis=1) >= 0.6)
        if rows.size < 2:
            raise NotPlaced("no printed frame on the overview")
        sides.append((int(rows.min()), int(rows.max())))
    (top, bottom), (left, right) = sides
    return left, top, right, bottom


# --- fitting --------------------------------------------------------------------------------


def affine(points):
    """Least-squares affine from [(x, y, lon, lat)]: (to_lonlat, to_pixel, coefficients to pixel)."""
    pts = np.asarray(points, float)
    a = np.column_stack([pts[:, 0], pts[:, 1], np.ones(len(pts))])
    fwd, *_ = np.linalg.lstsq(a, pts[:, 2:4], rcond=None)
    b = np.column_stack([pts[:, 2], pts[:, 3], np.ones(len(pts))])
    inv, *_ = np.linalg.lstsq(b, pts[:, 0:2], rcond=None)
    return (
        (lambda x, y: tuple(np.array([x, y, 1.0]) @ fwd)),
        (lambda lon, lat: tuple(np.array([lon, lat, 1.0]) @ inv)),
        inv,
    )


def graticule_nodes(bounds, grat):
    """(lon, lat) of every graticule node inside bounds."""
    step, origin = grat["step_deg"], grat.get("origin_lon", 0.0)
    w, s, e, n = bounds
    lons = [origin + k * step for k in range(math.ceil((w - origin) / step), math.floor((e - origin) / step) + 1)]
    lats = [k * step for k in range(math.ceil(s / step), math.floor(n / step) + 1)]
    return [(round(lon, 6), round(lat, 6)) for lon in lons for lat in lats]


def similarity(src, dst):
    """The move, uniform scale and turn that best takes points src onto dst (Umeyama): a function."""
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    ms, md = src.mean(0), dst.mean(0)
    a, b = src - ms, dst - md
    u, sig, vt = np.linalg.svd(b.T @ a)
    d = np.diag([1.0, np.sign(np.linalg.det(u @ vt))])
    rot = u @ d @ vt
    scale = (sig * np.diag(d)).sum() / max((a**2).sum(), 1e-9)
    return lambda pts: (np.asarray(pts, float) - ms) @ (scale * rot).T + md


def _place_lattice(hits_map, base, shifts, pred, period, stray_extra):
    """For lattice positions `base` (N x 2) moved by every (dx, dy) in shifts x shifts: the best
    objective (nodes on a clear "+", less half the stray from the prediction) and its positions."""
    h, w = hits_map.shape
    xs = np.rint(base[None, None, :, 0] + shifts[None, :, None]).astype(int)
    ys = np.rint(base[None, None, :, 1] + shifts[:, None, None]).astype(int)
    inside = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
    hits = (inside & hits_map[np.clip(ys, 0, h - 1), np.clip(xs, 0, w - 1)]).sum(axis=2)
    moved = base.mean(axis=0) - pred.mean(axis=0)
    # How far a placing strays from the prediction: well under 1 for any plausible one, so it only
    # decides between placings that land as many nodes.
    stray = np.hypot(moved[0] + shifts[None, :], moved[1] + shifts[:, None]) / period + stray_extra
    total = hits - 0.5 * stray
    iy, ix = np.unravel_index(int(total.argmax()), total.shape)
    return float(total[iy, ix]), base + (shifts[ix], shifts[iy])


def search_lattice(score, pred, period):
    """Move, scale and turn the predicted node positions `pred` (N x 2, work px) as a whole to
    where the most of them land on a clear "+" (score >= NODE_SCORE_MIN); ties go to the placing
    nearest the prediction. Returns the best positions (N x 2)."""
    hits_map = max_pool(score, POOL_PX) >= NODE_SCORE_MIN
    centre = pred.mean(axis=0)
    rel = pred - centre
    step = max(2, POOL_PX)
    shifts = np.arange(-SEARCH_SHIFT * period, SEARCH_SHIFT * period + 1, step)
    coarse = []
    for scale in SEARCH_SCALES:
        for turn in SEARCH_TURNS_DEG:
            t = math.radians(turn)
            rot = scale * np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
            extra = abs(scale - 1) * 5 + abs(turn) / 2
            total, placed = _place_lattice(hits_map, centre + rel @ rot.T, shifts, pred, period, extra)
            coarse.append((total, turn, placed))
    coarse.sort(key=lambda c: -c[0])
    best_total, best = -1e9, pred
    fine_shifts = np.arange(-FINE_SHIFT * period, FINE_SHIFT * period + 1, 2)
    for _, turn, placed in coarse[:TOP_PLACINGS]:
        c = placed.mean(axis=0)
        r = placed - c
        for sx in FINE_SCALES:
            for sy in FINE_SCALES:
                extra = (abs(sx - 1) + abs(sy - 1)) * 2.5 + abs(turn) / 2
                total, moved = _place_lattice(hits_map, c + r * (sx, sy), fine_shifts, pred, period, extra)
                if total > best_total:
                    best_total, best = total, moved
    return best


def find_nodes(score, lattice, nodes):
    """[(x, y, lon, lat)] in work px of the nodes found near their place in the lattice."""
    h, w = score.shape
    out = []
    for (x, y), (lon, lat) in zip(lattice, nodes, strict=True):
        x0, y0 = int(x) - NODE_LOOK_PX, int(y) - NODE_LOOK_PX
        if x0 < 0 or y0 < 0 or x0 + 2 * NODE_LOOK_PX >= w or y0 + 2 * NODE_LOOK_PX >= h:
            continue
        win = score[y0 : y0 + 2 * NODE_LOOK_PX + 1, x0 : x0 + 2 * NODE_LOOK_PX + 1]
        iy, ix = np.unravel_index(int(win.argmax()), win.shape)
        if win[iy, ix] >= NODE_SCORE_MIN:
            out.append((x0 + ix, y0 + iy, lon, lat))
    return out


# --- full-resolution refinement -------------------------------------------------------------


def fit_stroke(mask, axis):
    """The stroke through the middle of a window along axis "h" or "v" as across = a * along + b,
    fitted to the middle of the ink in each column near the centre row; None if too little ink."""
    m = mask if axis == "h" else mask.T
    n = m.shape[0] // 2
    lo, hi = n - 6, n + 7
    xs, ys = [], []
    for x in range(m.shape[1]):
        ink = np.flatnonzero(m[lo:hi, x])
        if ink.size and abs(x - n) > 4:  # skip the other stroke where they cross
            xs.append(x)
            ys.append(lo + ink.mean())
    if len(xs) < m.shape[1] // 4:
        return None
    xs, ys = np.array(xs, float), np.array(ys, float)
    for _ in range(3):
        a, b = np.polyfit(xs, ys, 1)
        r = np.abs(ys - (a * xs + b))
        keep = r < max(1.5, 2.5 * float(np.median(r)))
        if keep.sum() < 6:
            return None
        xs, ys = xs[keep], ys[keep]
    return tuple(np.polyfit(xs, ys, 1))


def refine_node(gray):
    """Sub-pixel (x, y) of the "+" at the middle of a full-resolution window, or None."""
    mask = ink_mask(gray)
    # Only the ink near the centre lines: a cross's arms are short, and nearby lettering is not part of it.
    h, v = fit_stroke(mask, "h"), fit_stroke(mask, "v")
    if h is None or v is None:
        return None
    (a, b), (c, d) = h, v
    y = (a * d + b) / (1 - a * c)
    x = c * y + d
    n = mask.shape[0] // 2
    return (x, y) if abs(x - n) < n / 2 and abs(y - n) < n / 2 else None


# --- one sheet ------------------------------------------------------------------------------


def place(sheet, spec):
    """(control points [(x, y, lon, lat)], neatline [(x, y)] x4) in full-resolution pixels."""
    w, s, e, n = sheet.bounds
    limit = spec.get("max_extent_deg")
    # The graticule's nodes are named from the archive's extent: one that cannot be this series'
    # sheet would name them wrongly, and nothing later could tell.
    if not (w < e and s < n) or (limit and (e - w > limit[0] or n - s > limit[1])):
        raise NotPlaced(f"the archive's extent ({e - w:.2f} x {n - s:.2f} degrees) is not one of this series' sheets")
    full_w, full_h = sheet.size()
    ov, ov_scale = sheet.overview()
    left, top, right, bottom = (v * ov_scale for v in outer_frame(ov))
    # The map lies inside the frame's band. Its scale comes from the frame's width alone: the rules
    # at the sides are the sheet's own, whereas below the map a legend panel often draws rules as
    # long as the frame. Scans have square pixels, so the height follows from the width.
    bw, bh = (right - left) * FRAME_BAND, (right - left) * FRAME_BAND
    w, s, e, n = sheet.bounds
    per_lon = (right - left - 2 * bw) / (e - w)  # px per degree of longitude
    per_lat = per_lon * 110_574 / (111_320 * math.cos(math.radians((s + n) / 2)))
    x0, y0 = left + bw, top + bh
    rough = [(x0, y0, w, n), (x0 + per_lon * (e - w), y0, e, n), (x0, y0 + per_lat * (n - s), w, s)]
    _, rough_px, rough_coef = affine(rough)

    work, k = sheet.work_image()  # k: full-resolution pixels per work pixel
    step = spec["graticule"]["step_deg"]
    period = min(abs(rough_coef[0, 0]), abs(rough_coef[1, 1])) * step / k
    nodes = graticule_nodes(sheet.bounds, spec["graticule"])
    if len(nodes) < MIN_NODES:
        raise NotPlaced(f"only {len(nodes)} graticule nodes fall on this sheet")
    score = node_score(ink_mask(work))
    pred = np.array([[v / k for v in rough_px(lon, lat)] for lon, lat in nodes])
    matched = find_nodes(score, search_lattice(score, pred, period), nodes)
    if len(matched) < MIN_NODES:
        raise NotPlaced(f"found {len(matched)} of {len(nodes)} graticule nodes")

    points = []
    for x, y, lon, lat in matched:
        cx, cy = int(x * k), int(y * k)
        hit = refine_node(sheet.region(cx - CROSS_WINDOW, cy - CROSS_WINDOW, 2 * CROSS_WINDOW, 2 * CROSS_WINDOW))
        if hit:
            points.append((cx - CROSS_WINDOW + hit[0], cy - CROSS_WINDOW + hit[1], lon, lat))
    while len(points) > MIN_NODES:
        _, to_px, _ = affine(points)
        resid = [math.dist((x, y), to_px(lon, lat)) for x, y, lon, lat in points]
        worst = int(np.argmax(resid))
        if resid[worst] <= CROSS_MAX_RESIDUAL:
            break
        points.pop(worst)
    if len(points) < MIN_NODES or len({p[2] for p in points}) < 2 or len({p[3] for p in points}) < 2:
        raise NotPlaced(f"only {len(points)} graticule nodes could be measured")
    _, to_px, coef = affine(points)
    resid = max(math.dist((x, y), to_px(lon, lat)) for x, y, lon, lat in points)
    if resid > CROSS_MAX_RESIDUAL:
        raise NotPlaced(f"the graticule nodes do not fit one another ({resid:.1f} px)")
    check_fit(coef, rough_coef)
    to_ll, _, _ = affine(points)
    w, s, e, n = sheet.bounds
    lon, lat = to_ll(*rough_px((w + e) / 2, (s + n) / 2))  # where the fit puts the extent's middle pixel
    shift_km = math.hypot((lon - (w + e) / 2) * 111.32 * math.cos(math.radians(lat)), (lat - (s + n) / 2) * 110.57)
    if shift_km > MAX_CENTRE_SHIFT_KM:
        raise NotPlaced(f"the graticule found puts the sheet {shift_km:.0f} km from where the archive has it")
    return points, neatline(work, k, to_px, sheet.bounds)


def check_fit(coef, rough_coef):
    """Raise NotPlaced unless the fitted lon/lat -> pixel map is a plausible scan of the sheet: the
    rough prediction's scale, little rotation, and meridians square to parallels."""
    east, north = coef[0], coef[1]  # pixel direction of one degree east, and of one degree north
    for got, want in (
        (np.linalg.norm(east), np.linalg.norm(rough_coef[0])),
        (np.linalg.norm(north), np.linalg.norm(rough_coef[1])),
    ):
        if abs(got / want - 1) > SCALE_TOLERANCE:
            raise NotPlaced("the graticule found is not at the sheet's scale")
    rotation = math.degrees(math.atan2(east[1], east[0]))
    if abs(rotation) > MAX_ROTATION_DEG:
        raise NotPlaced(f"the graticule found is turned {rotation:.1f} degrees")
    # Image y runs down, so the two directions turn the "wrong" way: compare to the square angle either way.
    shear = abs(
        math.degrees(math.atan2(east[0] * north[0] + east[1] * north[1], abs(east[0] * north[1] - east[1] * north[0])))
    )
    if shear > MAX_SHEAR_DEG:
        raise NotPlaced(f"the graticule found is skewed {shear:.1f} degrees")


def cross2(a, b):
    """The z part of the cross product of two 2-D vectors."""
    return a[0] * b[1] - a[1] * b[0]


def neatline(work, k, to_px, bounds):
    """The map's edge in full-resolution pixels: the sheet's extent through the fit, each side moved
    onto the innermost printed rule within NEATLINE_SEARCH work px (kept where none is found)."""
    w, s, e, n = bounds
    corner = {
        c: np.array(to_px(lon, lat)) / k
        for c, (lon, lat) in {"nw": (w, n), "ne": (e, n), "se": (e, s), "sw": (w, s)}.items()
    }
    mask = ink_mask(work)
    hgt, wid = mask.shape
    centre = (corner["nw"] + corner["se"]) / 2

    def ink_along(p, q):
        t = np.linspace(0.1, 0.9, 200)
        xs = np.clip((p[0] + t * (q[0] - p[0])).round().astype(int), 0, wid - 1)
        ys = np.clip((p[1] + t * (q[1] - p[1])).round().astype(int), 0, hgt - 1)
        return mask[ys, xs].mean()

    moved = {}
    for a, b in (("nw", "ne"), ("ne", "se"), ("se", "sw"), ("sw", "nw")):
        p, q = corner[a], corner[b]
        normal = np.array([q[1] - p[1], p[0] - q[0]])
        normal /= np.linalg.norm(normal)
        if np.dot(centre - (p + q) / 2, normal) < 0:
            normal = -normal  # point inwards
        best = None
        for off in range(-NEATLINE_SEARCH, NEATLINE_SEARCH + 1):
            if ink_along(p + off * normal, q + off * normal) >= 0.6:
                best = off  # keep going: the innermost rule wins
        moved[(a, b)] = (p + (best or 0) * normal, q + (best or 0) * normal)

    def meet(l1, l2):
        (p1, p2), (p3, p4) = l1, l2
        d1, d2 = p2 - p1, p4 - p3
        t = cross2(p3 - p1, d2) / cross2(d1, d2)
        return (p1 + t * d1) * k

    sides = list(moved.values())
    return [tuple(meet(sides[i - 1], sides[i])) for i in range(4)]  # nw, ne, se, sw


def control_file(spec, points, edge, image_path, work_size=None, source_bytes=None):
    """The georef_scan.py control file for a placed sheet."""
    out = {
        "image": image_path,
        "crs": spec.get("crs", "EPSG:4326"),
        "gcps": [[round(x, 1), round(y, 1), lon, lat] for x, y, lon, lat in points],
        "neatline": [[round(x, 1), round(y, 1)] for x, y in edge],
        "order": 1,
        "placed_by": f"scripts/place_sheets.py from {len(points)} graticule nodes",
    }
    if spec.get("offset_deg"):
        out["offset_deg"] = spec["offset_deg"]
    if spec.get("max_zoom"):
        out["max_zoom"] = spec["max_zoom"]
    if work_size:
        out["work_size"] = list(work_size)
    if source_bytes:
        out["source_bytes"] = source_bytes
    out["meta"] = spec.get("meta", {})
    return out


# --- where the image comes from -------------------------------------------------------------


DARK_INK_MAX_CHROMA = 70  # with dark_ink_only, a pixel whose colour channels spread more than this is not ink


def _gray(data, dark_ink_only=False):
    """The image as grey levels. With dark_ink_only, strongly coloured pixels (a blue grid, green
    woodland) are turned to paper, so only black and grey ink is left."""
    Image.MAX_IMAGE_PIXELS = None  # trusted archive scans, up to about 100 Mpx
    with Image.open(BytesIO(data)) as im:
        if not dark_ink_only:
            return np.asarray(im.convert("L"), dtype=np.float32)
        rgb = np.asarray(im.convert("RGB"), dtype=np.int16)
    gray = (rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114).astype(np.float32)
    gray[(rgb.max(axis=2) - rgb.min(axis=2)) > DARK_INK_MAX_CHROMA] = float(np.percentile(gray, 90))
    return gray


def _resized(gray, width):
    with Image.fromarray(gray.astype(np.uint8)) as im:
        return np.asarray(im.resize((width, round(im.height * width / im.width)), Image.Resampling.LANCZOS), np.float32)


class Sheet:
    """One sheet's image, from an IIIF image service or a plain image file."""

    def __init__(self, spec):
        self.id, self.bounds, self.image = spec["id"], spec["bounds"], spec["image"]
        self.dark_ink_only = bool(spec.get("dark_ink_only"))
        self._size = self._file = None
        self.source_bytes = None

    def _get(self, url, name):
        return common.cached_get(url, f"{CACHE}/{self.id}", name=name, min_interval=REQUEST_INTERVAL_S).read_bytes()

    def _whole(self):
        if self._file is None:
            if "iiif" in self.image:
                data = self._get(self.image["iiif"] + "/full/max/0/default.jpg", "full.jpg")
            else:
                data = self._get(self.image["url"], "image" + Path(self.image["url"]).suffix[:5])
            self.source_bytes = len(data)
            self._file = _gray(data, self.dark_ink_only)
        return self._file

    def size(self):
        """(width, height) of the full-resolution image."""
        if self._size is None:
            h, w = self._whole().shape
            self._size = (w, h)
        return self._size

    def _scaled(self, width, name):
        full_w, _ = self.size()
        gray = _resized(self._whole(), min(width, full_w))
        return gray, full_w / gray.shape[1]

    def overview(self):
        """(grey overview, full-resolution pixels per overview pixel)."""
        return self._scaled(OVERVIEW_WIDTH, "overview.jpg")

    def work_image(self):
        """(grey medium copy, full-resolution pixels per work pixel)."""
        return self._scaled(WORK_WIDTH, "work.jpg")

    def region(self, x, y, w, h):
        """Grey full-resolution pixels of the w x h rectangle at (x, y), padded with paper off the image."""
        full_w, full_h = self.size()
        x0, y0, x1, y1 = max(0, x), max(0, y), min(full_w, x + w), min(full_h, y + h)
        part = self._whole()[y0:y1, x0:x1]
        out = np.full((h, w), float(np.percentile(part, 90)), np.float32)
        out[y0 - y : y0 - y + part.shape[0], x0 - x : x0 - x + part.shape[1]] = part
        return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("sheets", type=Path, help="JSON list of sheets (see the module docstring)")
    p.add_argument("--only", nargs="*", help="only these sheet ids")
    p.add_argument("--part", help="I/N: only every N-th sheet, starting at the I-th (1-based)")
    p.add_argument("--out", type=Path, default=SCANS, help="folder for the control files (default data/scans)")
    p.add_argument("--review", type=Path, help="write the sheets that could not be placed, with the reason, here")
    p.add_argument("--cache", type=Path, help="folder to cache downloads in (default: data/raw)")
    p.add_argument("--image-dir", default="data/raw/scans", help="where the map library keeps each scan's work.jpg")
    args = p.parse_args(argv)
    if args.cache:
        common.RAW = args.cache
    specs = json.loads(args.sheets.read_text(encoding="utf-8"))
    if args.only:
        specs = [s for s in specs if s["id"] in set(args.only)]
    if args.part:
        i, n = (int(v) for v in args.part.split("/"))
        specs = specs[i - 1 :: n]
    args.out.mkdir(parents=True, exist_ok=True)
    failed = []
    for i, spec in enumerate(specs, 1):
        try:
            sheet = Sheet(spec)
            points, edge = place(sheet, spec)
        except (NotPlaced, OSError, ValueError, KeyError, np.linalg.LinAlgError) as e:
            failed.append({"id": spec["id"], "why": str(e)})
            print(f"[{i}/{len(specs)}] {spec['id']}: NOT PLACED, {e}", flush=True)
            continue
        control = control_file(
            spec, points, edge, f"{args.image_dir}/{spec['id']}/work.jpg", sheet.size(), sheet.source_bytes
        )
        (args.out / f"{spec['id']}.json").write_text(
            json.dumps(control, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"[{i}/{len(specs)}] {spec['id']}: placed from {len(points)} nodes", flush=True)
    if args.review:
        args.review.write_text(json.dumps(failed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(specs) - len(failed)} of {len(specs)} sheets placed; {len(failed)} need review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
