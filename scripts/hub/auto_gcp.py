"""Automated Ground Control Point (GCP) matching, RANSAC verification, and confidence gating.

Designed to bridge historical aerial photos / scans (`scan_bytes`) onto either modern imagery or an
earlier already-placed stepping-stone map (`ref_bytes` + `ref_bounds`):

  1. Keypoint detection + cross-polarity multi-scale descriptor matching (pure NumPy fallback built
     in so zero extra weights are required, plus optional ONNX Runtime GPU/CPU model acceleration
     when `AUTO_GCP_MODEL_DIR` holds `aliked.onnx`/`superpoint.onnx` + `lightglue.onnx`).
  2. Normalized 4-point DLT RANSAC projective homography verification (tolerating oblique camera
     tilt while rejecting mirror flips, collapsed quads, and outliers).
  3. 4-factor confidence scoring (inlier count, inlier ratio, 4-quadrant spatial spread, RMSE) and
     per-point ranking for human-in-the-loop review in the Placement screen.
"""

import base64
import io
import math
import os
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MAX_WORK_SIDE = 640
DEFAULT_CONFIDENCE_THRESHOLD = 0.80


def decode_image_b64(text):
    """Decode base64 or `data:image/...;base64,...` image payload into raw bytes."""
    s = str(text or "").strip()
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    if not s:
        raise ValueError("Missing image payload.")
    return base64.b64decode(s)


def _merc_y(lat_deg):
    lat_c = max(-85.05112878, min(85.05112878, float(lat_deg)))
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat_c) / 2.0))


def _unmerc_y(ym):
    return math.degrees(2.0 * math.atan(math.exp(ym)) - math.pi / 2.0)


def _load_work_gray(img_bytes, max_side=MAX_WORK_SIDE):
    """Return `(gray_float32, orig_w, orig_h)` with `max(work_w, work_h) <= max_side`."""
    with Image.open(io.BytesIO(img_bytes)) as raw:
        raw = ImageOps.exif_transpose(raw).convert("L")
        orig_w, orig_h = raw.size
        scale = min(1.0, float(max_side) / max(orig_w, orig_h, 1))
        work_w = max(32, int(round(orig_w * scale)))
        work_h = max(32, int(round(orig_h * scale)))
        if (work_w, work_h) != (orig_w, orig_h):
            raw = raw.resize((work_w, work_h), Image.Resampling.BILINEAR)
        arr = np.asarray(raw, dtype=np.float32) / 255.0
    mu = float(arr.mean())
    sd = float(arr.std())
    if sd > 1e-5:
        arr = np.clip((arr - mu) / (2.5 * sd) + 0.5, 0.0, 1.0)
    return arr, orig_w, orig_h


def _sobel(gray):
    p = np.pad(gray, 1, mode="edge")
    gx = ((p[:-2, 2:] + 2.0 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2.0 * p[1:-1, :-2] + p[2:, :-2])) * 0.25
    gy = ((p[2:, :-2] + 2.0 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2.0 * p[:-2, 1:-1] + p[:-2, 2:])) * 0.25
    return gx, gy


def _box_sum(arr, radius=2):
    k = 2 * radius + 1
    p = np.pad(arr.astype(np.float64), ((radius + 1, radius), (radius + 1, radius)), mode="edge")
    p[:1, :] = 0.0
    p[:, :1] = 0.0
    cs = p.cumsum(axis=0).cumsum(axis=1)
    return cs[k:, k:] - cs[:-k, k:] - cs[k:, :-k] + cs[:-k, :-k]


def _extract_patch_desc(gx, gy, xs, ys):
    """Build 96-dim contrast-polarity-tolerant multi-scale unsigned-gradient descriptors."""
    h, w = gx.shape
    mag = np.hypot(gx, gy)
    theta = np.arctan2(gy, gx) % math.pi  # unsigned orientation [0, pi) handles dark/light reversal
    b_idx = np.clip(np.floor(theta * (4.0 / math.pi)).astype(np.int32), 0, 3)

    # Accumulate 4 soft orientation channels across two spatial scales:
    # coarse 4x4 grid of 6px cells (24px total) + fine 4x4 grid of 3px cells (12px total) -> 128 dims
    chans = np.zeros((4, h, w), dtype=np.float32)
    for b in range(4):
        chans[b] = np.where(b_idx == b, mag, 0.0)

    descs = []
    # Keep keys away from extreme borders (half-patch 14px)
    offsets_fine = (-4.5, -1.5, 1.5, 4.5)
    offsets_coarse = (-9.0, -3.0, 3.0, 9.0)
    int_ch = np.pad(chans.astype(np.float64), ((0, 0), (1, 0), (1, 0)), mode="constant").cumsum(axis=1).cumsum(axis=2)

    def _sample_grid(cx, cy, offsets, half_cell):
        vec = []
        r = max(1, int(round(half_cell)))
        for oy in offsets:
            y0 = max(0, min(h - 1, int(round(cy + oy - r))))
            y1 = max(y0 + 1, min(h, int(round(cy + oy + r + 1))))
            for ox in offsets:
                x0 = max(0, min(w - 1, int(round(cx + ox - r))))
                x1 = max(x0 + 1, min(w, int(round(cx + ox + r + 1))))
                for b in range(4):
                    s = int_ch[b, y1, x1] - int_ch[b, y0, x1] - int_ch[b, y1, x0] + int_ch[b, y0, x0]
                    vec.append(float(s))
        v = np.asarray(vec, dtype=np.float32)
        nrm = float(np.linalg.norm(v))
        if nrm > 1e-8:
            v /= nrm
        return np.clip(v, 0.0, 0.3)

    keep_pts = []
    for cx, cy in zip(xs, ys, strict=True):
        if cx < 13 or cx >= w - 13 or cy < 13 or cy >= h - 13:
            continue
        vf = _sample_grid(cx, cy, offsets_fine, 1.5)
        vc = _sample_grid(cx, cy, offsets_coarse, 3.0)
        v = np.concatenate([vf, vc])
        nrm = float(np.linalg.norm(v))
        if nrm > 1e-8:
            v /= nrm
            descs.append(v)
            keep_pts.append((float(cx), float(cy)))
    if not descs:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 128), dtype=np.float32)
    return np.asarray(keep_pts, dtype=np.float64), np.vstack(descs)


def _detect_keypoints_and_desc(gray, *, max_kp=260, min_dist=9):
    """Detect spatially balanced Shi-Tomasi corners and extract multi-scale gradient descriptors."""
    h, w = gray.shape
    gx, gy = _sobel(gray)
    ixx = _box_sum(gx * gx, 2)
    iyy = _box_sum(gy * gy, 2)
    ixy = _box_sum(gx * gy, 2)
    tr = ixx + iyy
    disc = np.sqrt(np.maximum(0.0, (ixx - iyy) ** 2 + 4.0 * ixy * ixy))
    score = 0.5 * (tr - disc)

    margin = 14
    if h <= 2 * margin or w <= 2 * margin:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 128), dtype=np.float32)

    score[:margin, :] = 0.0
    score[-margin:, :] = 0.0
    score[:, :margin] = 0.0
    score[:, -margin:] = 0.0

    # Strict 3x3 local maxima
    loc_max = score.copy()
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            shifted = np.roll(np.roll(score, dy, axis=0), dx, axis=1)
            loc_max = np.where(score >= shifted, loc_max, 0.0)

    pos_y, pos_x = np.nonzero(loc_max > 0)
    if len(pos_x) == 0:
        return np.zeros((0, 2), dtype=np.float64), np.zeros((0, 128), dtype=np.float32)
    vals = loc_max[pos_y, pos_x]
    thresh = float(np.percentile(vals, 40)) * 0.15
    keep = vals > max(thresh, 1e-6)
    pos_x, pos_y, vals = pos_x[keep], pos_y[keep], vals[keep]

    # Spatial bucket quota across a 4x4 grid so keypoints span the full image
    per_cell = max(12, max_kp // 12)
    candidates = []
    cw = max(1.0, w / 4.0)
    ch = max(1.0, h / 4.0)
    cells = {(r, c): [] for r in range(4) for c in range(4)}
    for idx in np.argsort(-vals):
        x, y, v = int(pos_x[idx]), int(pos_y[idx]), float(vals[idx])
        key = (min(3, int(y // ch)), min(3, int(x // cw)))
        if len(cells[key]) < per_cell * 2:
            cells[key].append((v, x, y))
    for bucket in cells.values():
        candidates.extend(bucket)
    candidates.sort(reverse=True)

    selected_x, selected_y = [], []
    min_d2 = min_dist * min_dist
    for _, x, y in candidates:
        if any((x - sx) ** 2 + (y - sy) ** 2 < min_d2 for sx, sy in zip(selected_x, selected_y, strict=True)):
            continue
        selected_x.append(x)
        selected_y.append(y)
        if len(selected_x) >= max_kp:
            break

    return _extract_patch_desc(gx, gy, selected_x, selected_y)


def _match_numpy(scan_gray, ref_gray):
    """Return `(pts_scan, pts_ref, scores)` via mutual-nearest-neighbor multi-scale descriptors."""
    kp_s, desc_s = _detect_keypoints_and_desc(scan_gray)
    kp_r, desc_r = _detect_keypoints_and_desc(ref_gray)
    if len(kp_s) < 4 or len(kp_r) < 4:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0,))

    sim = desc_s @ desc_r.T  # cosine similarity in [0, 1]
    best_r_for_s = np.argmax(sim, axis=1)
    best_s_for_r = np.argmax(sim, axis=0)

    pts_s, pts_r, mscores = [], [], []
    for i, j in enumerate(best_r_for_s):
        if best_s_for_r[j] != i:
            continue
        s1 = float(sim[i, j])
        if s1 < 0.72:
            continue
        row_sorted = np.partition(sim[i], -2) if sim.shape[1] >= 2 else np.array([0.0, s1])
        s2 = float(row_sorted[-2]) if sim.shape[1] >= 2 else 0.0
        # Lowe distance ratio on normalized cosine distance
        d1 = max(1e-6, 1.0 - s1)
        d2 = max(1e-6, 1.0 - s2)
        if s2 > 0.0 and (d1 / d2) > 0.88:
            continue
        pts_s.append(kp_s[i])
        pts_r.append(kp_r[j])
        margin = max(0.0, min(1.0, (s1 - 0.68) / 0.28 * 0.7 + (1.0 - d1 / max(d2, 1e-6)) * 0.3))
        mscores.append(margin)

    if not pts_s:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0,))
    order = np.argsort(-np.asarray(mscores))[:120]
    return np.asarray(pts_s)[order], np.asarray(pts_r)[order], np.asarray(mscores)[order]


def _try_onnx_match(scan_gray, ref_gray):
    """Optional `AUTO_GCP_MODEL_DIR` (`aliked.onnx`/`superpoint.onnx` + `lightglue.onnx`) hook."""
    model_dir = os.environ.get("AUTO_GCP_MODEL_DIR", "").strip()
    if not model_dir:
        return None
    mdir = Path(model_dir)
    if not mdir.is_dir():
        return None
    try:
        import onnxruntime as ort
    except ImportError:
        return None

    avail = ort.get_available_providers()
    providers = [p for p in ("CUDAExecutionProvider", "ROCMExecutionProvider", "CPUExecutionProvider") if p in avail]
    extractor_path = next((p for p in (mdir / "aliked.onnx", mdir / "superpoint.onnx") if p.is_file()), None)
    matcher_path = mdir / "lightglue.onnx"
    if extractor_path is None or not matcher_path.is_file():
        return None

    using_gpu = any(p != "CPUExecutionProvider" for p in providers)
    try:
        ext_sess = ort.InferenceSession(str(extractor_path), providers=providers or ["CPUExecutionProvider"])
        mat_sess = ort.InferenceSession(str(matcher_path), providers=providers or ["CPUExecutionProvider"])
        s_in = scan_gray[None, None, :, :].astype(np.float32)
        r_in = ref_gray[None, None, :, :].astype(np.float32)
        k0, s0, d0 = ext_sess.run(None, {ext_sess.get_inputs()[0].name: s_in})[:3]
        k1, s1, d1 = ext_sess.run(None, {ext_sess.get_inputs()[0].name: r_in})[:3]
        m_inputs = {mat_sess.get_inputs()[0].name: k0, mat_sess.get_inputs()[1].name: k1}
        if len(mat_sess.get_inputs()) >= 4:
            m_inputs[mat_sess.get_inputs()[2].name] = d0
            m_inputs[mat_sess.get_inputs()[3].name] = d1
        out = mat_sess.run(None, m_inputs)
        matches = np.asarray(out[0])
        mscores = np.asarray(out[1]).reshape(-1) if len(out) > 1 else np.full(len(matches), 0.85)
        if matches.ndim == 2 and matches.shape[1] >= 2 and len(matches) >= 4:
            pts_s = np.asarray(k0)[0, matches[:, 0], :2]
            pts_r = np.asarray(k1)[0, matches[:, 1], :2]
            return ("onnx_gpu" if using_gpu else "onnx_cpu"), pts_s, pts_r, np.clip(mscores, 0.1, 0.99)
    except Exception:
        return None
    return None


def _hartley_norm_2d(pts):
    mean = pts.mean(axis=0)
    shifted = pts - mean
    dist = float(np.hypot(shifted[:, 0], shifted[:, 1]).mean())
    s = math.sqrt(2.0) / dist if dist > 1e-12 else 1.0
    T = np.array([[s, 0.0, -s * mean[0]], [0.0, s, -s * mean[1]], [0.0, 0.0, 1.0]], dtype=np.float64)
    hom = np.column_stack([pts, np.ones(len(pts), dtype=np.float64)])
    return (T @ hom.T).T[:, :2], T


def _fit_homography_dlt(src, dst):
    src_n, T_s = _hartley_norm_2d(src)
    dst_n, T_d = _hartley_norm_2d(dst)
    rows = []
    for (u, v), (x, y) in zip(src_n, dst_n, strict=True):
        rows.append([-u, -v, -1.0, 0.0, 0.0, 0.0, u * x, v * x, x])
        rows.append([0.0, 0.0, 0.0, -u, -v, -1.0, u * y, v * y, y])
    _, _, vt = np.linalg.svd(np.asarray(rows, dtype=np.float64))
    H_n = vt[-1].reshape(3, 3)
    H = np.linalg.inv(T_d) @ H_n @ T_s
    if abs(H[2, 2]) < 1e-12:
        return None
    return H / H[2, 2]


def _homography_valid(H, scan_w, scan_h, ref_w, ref_h):
    """Reject mirror reflections, extreme scale distortion, and non-convex corner warps."""
    if H is None or not np.isfinite(H).all():
        return False
    corners = np.array([[0.0, 0.0, 1.0], [scan_w, 0.0, 1.0], [scan_w, scan_h, 1.0], [0.0, scan_h, 1.0]])
    proj = (H @ corners.T).T
    if np.any(np.abs(proj[:, 2]) < 1e-6):
        return False
    center = H @ np.array([scan_w / 2.0, scan_h / 2.0, 1.0])
    if center[2] == 0:
        return False
    c_sign = 1.0 if center[2] > 0 else -1.0
    if np.any(proj[:, 2] * c_sign <= 0):
        return False
    xy = proj[:, :2] / proj[:, 2:3]
    # Check bounding sanity relative to reference image size
    span_x = float(xy[:, 0].max() - xy[:, 0].min())
    span_y = float(xy[:, 1].max() - xy[:, 1].min())
    if span_x < 16 or span_y < 16 or span_x > ref_w * 8.0 or span_y > ref_h * 8.0:
        return False
    # Convex orientation check (shoelace + consistent cross products, positive orientation in screen coords)
    crosses = []
    for k in range(4):
        p0, p1, p2 = xy[k], xy[(k + 1) % 4], xy[(k + 2) % 4]
        e1, e2 = p1 - p0, p2 - p1
        crosses.append(float(e1[0] * e2[1] - e1[1] * e2[0]))
    if not (all(c > 0 for c in crosses) or all(c < 0 for c in crosses)):
        return False
    # Forbid mirror flips: pixel->ref jacobian determinant at scan center must be positive
    # (both image coordinates increase right and down)
    cx, cy = scan_w / 2.0, scan_h / 2.0
    denom = H[2, 0] * cx + H[2, 1] * cy + H[2, 2]
    du_dx = ((H[0, 0] * denom) - (H[0, 0] * cx + H[0, 1] * cy + H[0, 2]) * H[2, 0]) / (denom * denom)
    du_dy = ((H[0, 1] * denom) - (H[0, 0] * cx + H[0, 1] * cy + H[0, 2]) * H[2, 1]) / (denom * denom)
    dv_dx = ((H[1, 0] * denom) - (H[1, 0] * cx + H[1, 1] * cy + H[1, 2]) * H[2, 0]) / (denom * denom)
    dv_dy = ((H[1, 1] * denom) - (H[1, 0] * cx + H[1, 1] * cy + H[1, 2]) * H[2, 1]) / (denom * denom)
    jac = du_dx * dv_dy - du_dy * dv_dx
    if jac <= 0.005 or jac > 200.0:
        return False
    return True


def _reproject(H, pts):
    hom = np.column_stack([pts, np.ones(len(pts), dtype=np.float64)])
    proj = (H @ hom.T).T
    w = np.where(np.abs(proj[:, 2]) > 1e-8, proj[:, 2], 1e-8)
    return proj[:, :2] / w[:, None]


def _ransac_homography(pts_s, pts_r, scan_w, scan_h, ref_w, ref_h, *, iters=450, thresh_px=6.5):
    n = len(pts_s)
    if n < 4:
        return None, np.zeros(n, dtype=bool), np.full(n, 999.0)
    rng = random.Random(42)
    best_H = None
    best_score = -1.0
    best_mask = np.zeros(n, dtype=bool)
    thresh2 = thresh_px * thresh_px
    idxs = list(range(n))

    for _ in range(min(iters, max(40, n * 18))):
        sample = rng.sample(idxs, 4)
        s_sub = pts_s[sample]
        # Skip nearly collinear 3-of-4 subsets
        p0, p1, p2 = s_sub[0], s_sub[1], s_sub[2]
        if abs((p1[0] - p0[0]) * (p2[1] - p0[1]) - (p1[1] - p0[1]) * (p2[0] - p0[0])) < 30.0:
            continue
        try:
            H = _fit_homography_dlt(s_sub, pts_r[sample])
        except np.linalg.LinAlgError:
            continue
        if not _homography_valid(H, scan_w, scan_h, ref_w, ref_h):
            continue
        pred = _reproject(H, pts_s)
        d2 = np.sum((pred - pts_r) ** 2, axis=1)
        inliers = d2 <= thresh2
        cnt = int(inliers.sum())
        if cnt < 4:
            continue
        msac = float(np.sum(np.where(inliers, 1.0 - 0.5 * (d2 / thresh2), 0.0)))
        if msac > best_score:
            best_score = msac
            best_H = H
            best_mask = inliers

    if best_H is None or int(best_mask.sum()) < 4:
        return None, np.zeros(n, dtype=bool), np.full(n, 999.0)

    # Polish homography on all inliers
    try:
        polished = _fit_homography_dlt(pts_s[best_mask], pts_r[best_mask])
        if _homography_valid(polished, scan_w, scan_h, ref_w, ref_h):
            pred = _reproject(polished, pts_s)
            errs = np.hypot(pred[:, 0] - pts_r[:, 0], pred[:, 1] - pts_r[:, 1])
            mask2 = errs <= thresh_px
            if int(mask2.sum()) >= int(best_mask.sum()):
                best_H, best_mask = polished, mask2
    except np.linalg.LinAlgError:
        pass

    pred = _reproject(best_H, pts_s)
    errs = np.hypot(pred[:, 0] - pts_r[:, 0], pred[:, 1] - pts_r[:, 1])
    return best_H, best_mask, errs


def match_and_score(
    scan_bytes,
    ref_bytes,
    ref_bounds,
    *,
    confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
    max_points=12,
    min_inliers=5,
    inlier_px=6.5,
    scan_target_size=None,
):
    """Detect and score GCP correspondences from `scan_bytes` onto `ref_bytes` (`ref_bounds=[w,s,e,n]`).

    Returns ranked points in full-resolution scan pixels (using `scan_target_size=(W, H)` when
    a downscaled preview was sent) + WGS84 lon/lat (exact Web Mercator inverse from `ref_bounds`),
    overall `confidence` in `[0.0, 1.0]`, and `meets_threshold` for HITL gating.
    """
    if len(ref_bounds) != 4 or not (ref_bounds[0] < ref_bounds[2] and ref_bounds[1] < ref_bounds[3]):
        raise ValueError("ref_bounds must be [west, south, east, north] with west < east and south < north.")
    threshold = max(0.25, min(0.99, float(confidence_threshold)))
    max_points = max(4, min(32, int(max_points)))

    scan_gray, orig_scan_w, orig_scan_h = _load_work_gray(scan_bytes)
    ref_gray, _, _ = _load_work_gray(ref_bytes)
    work_scan_h, work_scan_w = scan_gray.shape
    work_ref_h, work_ref_w = ref_gray.shape
    target_w, target_h = orig_scan_w, orig_scan_h
    if scan_target_size and len(scan_target_size) == 2:
        target_w = max(1, int(scan_target_size[0]))
        target_h = max(1, int(scan_target_size[1]))

    onnx_hit = _try_onnx_match(scan_gray, ref_gray)
    if onnx_hit is not None:
        backend, pts_s, pts_r, mscores = onnx_hit
    else:
        backend = "numpy_local"
        pts_s, pts_r, mscores = _match_numpy(scan_gray, ref_gray)

    total_matches = int(len(pts_s))
    H, inlier_mask, errs = _ransac_homography(
        pts_s, pts_r, work_scan_w, work_scan_h, work_ref_w, work_ref_h, thresh_px=inlier_px
    )
    inlier_idxs = np.nonzero(inlier_mask)[0]
    n_inliers = int(len(inlier_idxs))

    west, south, east, north = (float(v) for v in ref_bounds)
    ym_n, ym_s = _merc_y(north), _merc_y(south)
    sx_scan = target_w / float(work_scan_w)
    sy_scan = target_h / float(work_scan_h)

    if H is None or n_inliers < min_inliers:
        low_conf = round(min(0.25, 0.03 * total_matches), 3)
        return {
            "points": [],
            "confidence": low_conf,
            "confidence_pct": round(low_conf * 100.0, 1),
            "meets_threshold": False,
            "threshold": threshold,
            "inliers": n_inliers,
            "total_matches": total_matches,
            "quadrants_covered": 0,
            "rmse_px": None,
            "backend": backend,
            "recommended_warp": "auto",
            "reason": "Too few geometrically consistent landmarks matched; zoom closer or align via an intermediate map.",
        }

    # Build candidate records with per-point confidence
    candidates = []
    for i in inlier_idxs:
        sx, sy = float(pts_s[i, 0]), float(pts_s[i, 1])
        rx, ry = float(pts_r[i, 0]), float(pts_r[i, 1])
        err_i = float(errs[i])
        m_conf = float(mscores[i])
        pt_conf = round(max(0.1, min(0.99, 0.45 * m_conf + 0.55 * math.exp(-0.35 * (err_i * err_i)))), 3)
        q_id = (0 if sx < work_scan_w / 2.0 else 1) + (0 if sy < work_scan_h / 2.0 else 2)
        lon = west + (rx / float(work_ref_w)) * (east - west)
        lat = _unmerc_y(ym_n - (ry / float(work_ref_h)) * (ym_n - ym_s))
        candidates.append(
            {
                "col": round(sx * sx_scan, 1),
                "row": round(sy * sy_scan, 1),
                "lon": round(lon, 7),
                "lat": round(lat, 7),
                "confidence": pt_conf,
                "error_px": round(err_i, 2),
                "quadrant": q_id,
                "_wx": sx,
                "_wy": sy,
            }
        )

    candidates.sort(key=lambda c: (-c["confidence"], c["error_px"]))

    # Quadrant-aware selection with minimum spacing so matched GCPs don't bunch up
    picked = []
    picked_coords = []
    min_gap2 = 18.0 * 18.0
    for q in range(4):
        for cand in candidates:
            if cand["quadrant"] == q and all(
                (cand["_wx"] - px) ** 2 + (cand["_wy"] - py) ** 2 >= min_gap2 for px, py in picked_coords
            ):
                picked.append(cand)
                picked_coords.append((cand["_wx"], cand["_wy"]))
                break
    for cand in candidates:
        if len(picked) >= max_points:
            break
        if cand in picked:
            continue
        if all((cand["_wx"] - px) ** 2 + (cand["_wy"] - py) ** 2 >= min_gap2 for px, py in picked_coords):
            picked.append(cand)
            picked_coords.append((cand["_wx"], cand["_wy"]))

    if len(picked) < min_inliers:
        for cand in candidates:
            if len(picked) >= min(max_points, len(candidates)):
                break
            if cand not in picked:
                picked.append(cand)

    picked.sort(key=lambda c: (-c["confidence"], c["error_px"]))
    for c in picked:
        c.pop("_wx", None)
        c.pop("_wy", None)

    quadrants_covered = len({c["quadrant"] for c in candidates})
    rmse_px = math.sqrt(float(np.mean(errs[inlier_idxs] ** 2)))
    s_inliers = min(1.0, n_inliers / 12.0)
    s_ratio = min(1.0, (n_inliers / max(total_matches, 1)) / 0.45)
    s_spread = quadrants_covered / 4.0
    # A 4-point homography has 0 degrees of freedom (RMSE is identically ~0 for any 4 points).
    dof_weight = 0.0 if n_inliers <= 4 else (0.4 if n_inliers == 5 else 1.0)
    s_rmse = max(0.0, 1.0 - (rmse_px / float(inlier_px))) * dof_weight
    raw_conf = 0.32 * s_inliers + 0.18 * s_ratio + 0.28 * s_spread + 0.22 * s_rmse
    if n_inliers < 5:
        raw_conf = min(raw_conf, 0.35)
    confidence = round(max(0.0, min(1.0, raw_conf)), 3)

    # Check if homography has significant projective perspective skew (ratio of bottom vs top width)
    top_l = _reproject(H, np.array([[0.0, 0.0]]))[0]
    top_r = _reproject(H, np.array([[float(work_scan_w), 0.0]]))[0]
    bot_l = _reproject(H, np.array([[0.0, float(work_scan_h)]]))[0]
    bot_r = _reproject(H, np.array([[float(work_scan_w), float(work_scan_h)]]))[0]
    w_top = float(np.linalg.norm(top_r - top_l))
    w_bot = float(np.linalg.norm(bot_r - bot_l))
    skew_ratio = max(w_top, w_bot) / max(1e-3, min(w_top, w_bot))
    recommended_warp = "homography" if skew_ratio > 1.08 else ("tps" if len(picked) >= 8 else "auto")

    return {
        "points": picked,
        "confidence": confidence,
        "confidence_pct": round(confidence * 100.0, 1),
        "meets_threshold": confidence >= threshold and n_inliers >= 5 and len(picked) >= 4,
        "threshold": threshold,
        "inliers": n_inliers,
        "total_matches": total_matches,
        "quadrants_covered": quadrants_covered,
        "rmse_px": round(rmse_px, 2),
        "backend": backend,
        "recommended_warp": recommended_warp,
    }
