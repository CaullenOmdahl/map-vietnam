"""Strip chaining: place the neighbouring frames of an aerial survey strip from one placed frame.

Consecutive exposures of a strip overlap by about 60%, and the same-epoch, same-camera match
between them is far more reliable than matching a 1950s frame straight onto modern imagery. So a
person places one frame (with stepping-stone maps if needed) and this module walks outward along
the strip:

  * Frames are grouped by mission and frame number parsed from their title (SHDAI pattern
    ``SHDAI__AI_75_E_192_HV741__0002__V.jpg``).
  * Each unplaced frame is matched (``auto_gcp.match_pair``) onto its nearest placed or already
    chained neighbour; matched pixels go through that neighbour's pixel -> lon/lat placement.
  * Frames the user placed are never overwritten; they become fresh anchors.
  * Each direction stops at the first frame below the confidence threshold: that frame needs a
    human (Placement), after which chaining can be run again.
"""

import math
import re

import numpy as np
from custom_maps import affine_from_points

FRAME_RE = re.compile(r"(SHDAI__[A-Z0-9_]+?)__(\d{3,5})__", re.IGNORECASE)
MAX_STEPS = 60


def strip_key(entry):
    """`(mission_key, frame_number)` for a frame of a survey strip, or None."""
    for text in (entry.get("title") or "", entry.get("original_title") or "", entry.get("filename") or ""):
        m = FRAME_RE.search(text)
        if m:
            return m.group(1).upper(), int(m.group(2))
    return None


def _fit_similarity_metric(points, cos_lat, handedness=None):
    """Fit a 4-DOF similarity transform `(col, row) -> (lon * cos_lat, lat)`.

    Returns `(a, b, tx, ty, s_y)` where `s_y` is `-1` for standard vertical-aerial orientation
    (image row points south/down while latitude points north/up) or `+1` for reflected orientation:
        e = a * c - s_y * b * r + tx
        n = b * c + s_y * a * r + ty
    Returns `None` if fewer than 2 distinct points or degenerate.
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 4 or not np.isfinite(pts[:, :4]).all():
        return None
    if abs(cos_lat) < 1e-9:
        return None

    c = pts[:, 0]
    r = pts[:, 1]
    e = pts[:, 2] * cos_lat
    n = pts[:, 3]

    c_mean, r_mean = float(c.mean()), float(r.mean())
    e_mean, n_mean = float(e.mean()), float(n.mean())
    dc = c - c_mean
    dr = r - r_mean
    de = e - e_mean
    dn = n - n_mean

    denom = float(np.sum(dc * dc + dr * dr))
    if denom < 1e-12:
        return None

    candidates = (handedness,) if handedness in (-1, 1) else ((-1,) if len(pts) == 2 else (-1, 1))
    best = None
    best_sse = math.inf
    for s_y in candidates:
        a = float(np.sum(dc * de + s_y * dr * dn)) / denom
        b = float(np.sum(dc * dn - s_y * dr * de)) / denom
        if math.hypot(a, b) < 1e-15:
            continue
        res_e = de - (a * dc - s_y * b * dr)
        res_n = dn - (b * dc + s_y * a * dr)
        sse = float(np.sum(res_e * res_e + res_n * res_n))
        # Prefer standard aerial handedness (-1) on ties.
        if best is None or sse < best_sse - 1e-18:
            tx = e_mean - (a * c_mean - s_y * b * r_mean)
            ty = n_mean - (b * c_mean + s_y * a * r_mean)
            best = (a, b, tx, ty, s_y)
            best_sse = sse
    return best


def _similarity_coef(a, b, tx, ty, s_y, cos_lat):
    """3x2 `(col, row, 1) -> (lon, lat)` matrix for a metric 4-DOF similarity transform."""
    return np.array(
        [
            [a / cos_lat, b],
            [-s_y * b / cos_lat, s_y * a],
            [tx / cos_lat, ty],
        ],
        dtype=np.float64,
    )


def similarity_from_points(points):
    """Closed-form 4-DOF similarity `(col, row) -> (lon, lat)` fit in metric `(lon*cos_lat, lat)` space.

    Returns a 3x2 matrix `coef` such that `[col, row, 1.0] @ coef == [lon, lat]` (zero shear and
    isotropic ground scale), or `None` if fewer than 2 distinct points or degenerate.
    """
    pts = np.asarray(points, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[0] < 2 or pts.shape[1] < 4 or not np.isfinite(pts[:, :4]).all():
        return None
    mean_lat = float(np.mean(pts[:, 3]))
    cos_lat = math.cos(math.radians(mean_lat))
    fit = _fit_similarity_metric(pts, cos_lat)
    if fit is None:
        return None
    a, b, tx, ty, s_y = fit
    return _similarity_coef(a, b, tx, ty, s_y, cos_lat)


def _placement_fn(points):
    """Pixel -> (lon, lat) 4-DOF similarity transform fitted to a frame's control points.

    Consecutive frames only overlap on one half (~60%): a 4-DOF similarity fit in metric
    `(lon * cos_lat, lat)` space prevents shear and cumulative scale drift across hops,
    falling back to 6-DOF affine only if similarity fitting is degenerate.
    """
    pts = np.asarray(points, dtype=np.float64)
    pts_list = pts.tolist()
    coef = similarity_from_points(pts_list)
    if coef is None:
        coef = affine_from_points(pts_list)
    if coef is None:
        return None
    return lambda c, r: tuple(float(v) for v in np.array([c, r, 1.0]) @ coef)


def bundle_adjust_strip(anchors, unplaced_frames, edges):
    """Global linear least-squares 4-DOF similarity bundle adjustment for unplaced strip frames.

    Holds placed anchor frames fixed at their ground control points while solving simultaneously
    for all `unplaced_frames`' 4-DOF similarity parameters `(a, b, tx, ty)` in metric
    `(lon * cos_lat, lat)` space to minimize tie-point reprojection residuals across `edges`.

    `anchors`: `{frame_number: [[col, row, lon, lat], ...]}` for fixed placed anchor frames.
    `unplaced_frames`: ordered iterable of unplaced frame numbers to solve for.
    `edges`: list of `(frame_b, frame_a, pairs)` where each pair is `(bc, br, ac, ar, ...)`
             matching pixel `(bc, br)` in `frame_b` to pixel `(ac, ar)` in `frame_a`.

    Returns `{frame_number: coef}` (each `coef` is a 3x2 `(col, row, 1) -> (lon, lat)` matrix),
    or `None` if under-constrained or degenerate.
    """
    unplaced = list(dict.fromkeys(unplaced_frames))
    if not anchors or not unplaced or not edges:
        return None

    all_lats = [float(p[3]) for pts in anchors.values() for p in pts]
    all_lons = [float(p[2]) for pts in anchors.values() for p in pts]
    if not all_lats:
        return None
    mean_lat = float(np.mean(all_lats))
    cos_lat = math.cos(math.radians(mean_lat))
    if abs(cos_lat) < 1e-9:
        return None

    # Fit each anchor frame in the common metric frame and determine strip handedness.
    anchor_metric_fn = {}
    handednesses = []
    for fnum, pts in anchors.items():
        fit = _fit_similarity_metric(pts, cos_lat)
        if fit is not None:
            a, b, tx, ty, s_y = fit
            handednesses.append(s_y)
            anchor_metric_fn[fnum] = lambda c, r, a=a, b=b, tx=tx, ty=ty, s_y=s_y: (
                a * c - s_y * b * r + tx,
                b * c + s_y * a * r + ty,
            )
        else:
            coef = affine_from_points(pts)
            if coef is None:
                return None
            anchor_metric_fn[fnum] = lambda c, r, coef=coef, cos_lat=cos_lat: (
                float((c * coef[0, 0] + r * coef[1, 0] + coef[2, 0]) * cos_lat),
                float(c * coef[0, 1] + r * coef[1, 1] + coef[2, 1]),
            )

    s_y = -1 if handednesses.count(-1) >= handednesses.count(1) else 1
    e0 = float(np.mean(all_lons)) * cos_lat
    n0 = mean_lat

    px_scale = 1.0
    for _fb, _fa, pairs in edges:
        for p in pairs:
            px_scale = max(px_scale, abs(float(p[0])), abs(float(p[1])), abs(float(p[2])), abs(float(p[3])))

    idx_of = {fnum: i for i, fnum in enumerate(unplaced)}
    n_vars = 4 * len(unplaced)
    rows = []
    rhs = []

    for fb, fa, pairs in edges:
        b_u = idx_of.get(fb)
        a_u = idx_of.get(fa)
        if b_u is None and a_u is None:
            continue
        if (b_u is None and fb not in anchor_metric_fn) or (a_u is None and fa not in anchor_metric_fn):
            continue

        for p in pairs:
            bc, br, ac, ar = float(p[0]), float(p[1]), float(p[2]), float(p[3])
            w = math.sqrt(max(float(p[4]), 0.1)) if len(p) > 4 else 1.0
            sc_b, sr_b = bc / px_scale, br / px_scale
            sc_a, sr_a = ac / px_scale, ar / px_scale

            row_e = np.zeros(n_vars, dtype=np.float64)
            row_n = np.zeros(n_vars, dtype=np.float64)
            rhs_e = 0.0
            rhs_n = 0.0

            if b_u is not None:
                j = 4 * b_u
                row_e[j : j + 4] += (sc_b, -s_y * sr_b, 1.0, 0.0)
                row_n[j : j + 4] += (s_y * sr_b, sc_b, 0.0, 1.0)
            else:
                eb, nb = anchor_metric_fn[fb](bc, br)
                rhs_e -= eb - e0
                rhs_n -= nb - n0

            if a_u is not None:
                i = 4 * a_u
                row_e[i : i + 4] -= (sc_a, -s_y * sr_a, 1.0, 0.0)
                row_n[i : i + 4] -= (s_y * sr_a, sc_a, 0.0, 1.0)
            else:
                ea, na = anchor_metric_fn[fa](ac, ar)
                rhs_e += ea - e0
                rhs_n += na - n0

            rows.append(w * row_e)
            rhs.append(w * rhs_e)
            rows.append(w * row_n)
            rhs.append(w * rhs_n)

    if len(rows) < n_vars:
        return None

    A = np.asarray(rows, dtype=np.float64)
    b_vec = np.asarray(rhs, dtype=np.float64)
    sol, _, rank, _ = np.linalg.lstsq(A, b_vec, rcond=None)
    if rank < n_vars or not np.isfinite(sol).all():
        return None

    result = {}
    for fnum, k in idx_of.items():
        a = float(sol[4 * k]) / px_scale
        b = float(sol[4 * k + 1]) / px_scale
        tx = float(sol[4 * k + 2]) + e0
        ty = float(sol[4 * k + 3]) + n0
        if math.hypot(a, b) < 1e-15:
            return None
        result[fnum] = _similarity_coef(a, b, tx, ty, s_y, cos_lat)
    return result


def _norm(p):
    mean = p.mean(axis=0)
    d = float(np.hypot(*(p - mean).T).mean()) or 1.0
    s = np.sqrt(2.0) / d
    return np.array([[s, 0.0, -s * mean[0]], [0.0, s, -s * mean[1]], [0.0, 0.0, 1.0]])


def _dlt(src, dst):
    Ts, Td = _norm(src), _norm(dst)
    sh = (Ts @ np.column_stack([src, np.ones(len(src))]).T).T
    dh = (Td @ np.column_stack([dst, np.ones(len(dst))]).T).T
    rows = []
    for (u, v, _), (x, y, _) in zip(sh, dh, strict=True):
        rows.append([-u, -v, -1.0, 0.0, 0.0, 0.0, u * x, v * x, x])
        rows.append([0.0, 0.0, 0.0, -u, -v, -1.0, u * y, v * y, y])
    _, _, vt = np.linalg.svd(np.asarray(rows))
    H = np.linalg.inv(Td) @ vt[-1].reshape(3, 3) @ Ts
    if not np.isfinite(H).all() or abs(H[2, 2]) < 1e-15:
        return None
    return H / H[2, 2]


def _apply(H, c, r):
    w = H[2, 0] * c + H[2, 1] * r + H[2, 2]
    return float((H[0, 0] * c + H[0, 1] * r + H[0, 2]) / w), float((H[1, 0] * c + H[1, 1] * r + H[1, 2]) / w)


def strip_frames(entries, anchor):
    """`{frame_number: [entries]}` of the scans in `anchor`'s strip (duplicates kept together)."""
    key = strip_key(anchor)
    if key is None:
        return None, {}
    frames = {}
    for e in entries:
        if e.get("kind") != "scan":
            continue
        k = strip_key(e)
        if k and k[0] == key[0]:
            frames.setdefault(k[1], []).append(e)
    return key, frames


def _project_pairs(pairs, coef):
    """Project `pairs` (`[bc, br, ...]`) through a 3x2 `coef` matrix into `[bc, br, lon, lat]`."""
    out = []
    for bc, br, *_ in pairs:
        lon, lat = (float(v) for v in np.array([bc, br, 1.0]) @ coef)
        out.append([bc, br, round(lon, 7), round(lat, 7)])
    return out


def plan_chain(entries, anchor_id, match_fn, *, threshold=0.8):
    """Walk out from `anchor_id` in both directions and propose control points for unplaced frames.

    `match_fn(b_entry, a_entry) -> auto_gcp.match_pair result` (b = frame to place, a = placed
    neighbour). Whenever a contiguous run of unplaced frames is bracketed by two placed anchors,
    solves a global 4-DOF similarity bundle adjustment across all tie-points in that segment.
    Returns `{"mission", "anchor", "anchors", "adjusted", "proposals": [...], "stops": [...]}`;
    a proposal is `{id, frame, from_id, from_frame, hops, points, confidence, inliers, rmse_px,
    meets_threshold}`.
    """
    by_id = {e["id"]: e for e in entries}
    anchor = by_id.get(anchor_id)
    if anchor is None:
        raise ValueError("Unknown map.")
    key, frames = strip_frames(entries, anchor)
    if key is None:
        raise ValueError("This picture's title does not name a survey strip and frame number.")
    if len(anchor.get("points") or []) < 3:
        raise ValueError("Place this frame on the map first, then chain its neighbours.")

    all_anchors = sorted(fn for fn, grp in frames.items() if any(len(e.get("points") or []) >= 3 for e in grp))
    proposals, stops = [], []
    adjusted = False
    for step in (1, -1):
        placed = anchor
        placed_pts = anchor["points"]
        hops = 0
        n = key[1]
        lo, hi = min(frames), max(frames)
        seg_start_frame = key[1]
        seg_start_pts = anchor["points"]
        seg_edges = []
        seg_proposals = []
        last_target = None
        last_frame = None
        for _ in range(MAX_STEPS):
            n += step
            if n < lo or n > hi:
                break
            group = frames.get(n)
            if not group:
                continue  # frame not imported; try the next one against the same neighbour
            mine = next((e for e in group if len(e.get("points") or []) >= 3), None)
            if mine is not None:
                if seg_proposals and last_target is not None:
                    close_res = match_fn(last_target, mine)
                    if close_res and close_res.get("ok") and close_res.get("confidence", 0.0) >= threshold:
                        seg_anchors = {seg_start_frame: seg_start_pts, n: mine["points"]}
                        unplaced = [p["frame"] for p, _ in seg_proposals]
                        adj = bundle_adjust_strip(
                            seg_anchors,
                            unplaced,
                            [*seg_edges, (last_frame, n, close_res["pairs"])],
                        )
                        if adj is not None:
                            for prop, pairs in seg_proposals:
                                prop["points"] = _project_pairs(pairs, adj[prop["frame"]])
                            adjusted = True
                placed, placed_pts, hops = mine, mine["points"], 0
                seg_start_frame, seg_start_pts = n, mine["points"]
                seg_edges, seg_proposals, last_target, last_frame = [], [], None, None
                continue
            target = group[0]
            to_lonlat = _placement_fn(placed_pts)
            res = match_fn(target, placed) if to_lonlat else None
            if not res or not res.get("ok"):
                stops.append(
                    {
                        "id": target["id"],
                        "frame": n,
                        "from_frame": strip_key(placed)[1],
                        "confidence": (res or {}).get("confidence", 0.0),
                        "reason": (res or {}).get("reason") or "No reliable match with the neighbouring frame.",
                    }
                )
                break
            pts = []
            for bc, br, ac, ar, _c in res["pairs"]:
                lon, lat = to_lonlat(ac, ar)
                pts.append([bc, br, round(lon, 7), round(lat, 7)])
            ok = res["confidence"] >= threshold
            from_frame = strip_key(placed)[1]
            frame_props = []
            for e in group:
                if e["image"] != target["image"]:
                    continue  # a different scan of the same frame number; leave it to the user
                prop = {
                    "id": e["id"],
                    "frame": n,
                    "from_id": placed["id"],
                    "from_frame": from_frame,
                    "hops": hops + 1,
                    "points": pts,
                    "confidence": res["confidence"],
                    "confidence_pct": res.get("confidence_pct"),
                    "inliers": res["inliers"],
                    "rmse_px": res.get("rmse_px"),
                    "meets_threshold": ok,
                }
                proposals.append(prop)
                frame_props.append((prop, res["pairs"]))
            if not ok:
                stops.append(
                    {
                        "id": target["id"],
                        "frame": n,
                        "from_frame": from_frame,
                        "confidence": res["confidence"],
                        "reason": "Below the confidence threshold: check this frame in Placement.",
                    }
                )
                break
            seg_edges.append((n, from_frame, res["pairs"]))
            seg_proposals.extend(frame_props)
            last_target = target
            last_frame = n
            placed = {**target, "points": pts}
            placed_pts, hops = pts, hops + 1
    proposals.sort(key=lambda p: p["frame"])
    return {
        "mission": key[0],
        "anchor": {"id": anchor_id, "frame": key[1]},
        "anchors": all_anchors,
        "adjusted": adjusted,
        "proposals": proposals,
        "stops": stops,
    }
