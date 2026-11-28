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


def _placement_fn(points):
    """Pixel -> (lon, lat) function fitted to a frame's control points (homography for 4+)."""
    pts = np.asarray(points, dtype=np.float64)
    if len(pts) >= 4:
        H = _dlt(pts[:, :2], pts[:, 2:4])
        if H is not None:
            return lambda c, r: _apply(H, c, r)
    coef = affine_from_points(pts.tolist())
    if coef is None:
        return None
    return lambda c, r: tuple(float(v) for v in np.array([c, r, 1.0]) @ coef)


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


def plan_chain(entries, anchor_id, match_fn, *, threshold=0.8):
    """Walk out from `anchor_id` in both directions and propose control points for unplaced frames.

    `match_fn(b_entry, a_entry) -> auto_gcp.match_pair result` (b = frame to place, a = placed
    neighbour). Returns `{"mission", "anchor", "proposals": [...], "stops": [...]}`; a proposal is
    `{id, frame, from_id, from_frame, hops, points, confidence, inliers, rmse_px, meets_threshold}`.
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

    proposals, stops = [], []
    for step in (1, -1):
        placed = anchor
        placed_pts = anchor["points"]
        hops = 0
        n = key[1]
        lo, hi = min(frames), max(frames)
        for _ in range(MAX_STEPS):
            n += step
            if n < lo or n > hi:
                break
            group = frames.get(n)
            if not group:
                continue  # frame not imported; try the next one against the same neighbour
            mine = next((e for e in group if len(e.get("points") or []) >= 3), None)
            if mine is not None:
                placed, placed_pts, hops = mine, mine["points"], 0
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
            for e in group:
                if e["image"] != target["image"]:
                    continue  # a different scan of the same frame number; leave it to the user
                proposals.append(
                    {
                        "id": e["id"],
                        "frame": n,
                        "from_id": placed["id"],
                        "from_frame": strip_key(placed)[1],
                        "hops": hops + 1,
                        "points": pts,
                        "confidence": res["confidence"],
                        "confidence_pct": res.get("confidence_pct"),
                        "inliers": res["inliers"],
                        "rmse_px": res.get("rmse_px"),
                        "meets_threshold": ok,
                    }
                )
            if not ok:
                stops.append(
                    {
                        "id": target["id"],
                        "frame": n,
                        "from_frame": strip_key(placed)[1],
                        "confidence": res["confidence"],
                        "reason": "Below the confidence threshold: check this frame in Placement.",
                    }
                )
                break
            placed = {**target, "points": pts}
            placed_pts, hops = pts, hops + 1
    proposals.sort(key=lambda p: p["frame"])
    return {"mission": key[0], "anchor": {"id": anchor_id, "frame": key[1]}, "proposals": proposals, "stops": stops}
