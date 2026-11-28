"""Automated Ground Control Point (GCP) matching, RANSAC verification, and confidence gating.

Designed to bridge historical aerial photos / scans (`scan_bytes`) onto either modern imagery or an
earlier already-placed stepping-stone map (`ref_bytes` + `ref_bounds`):

  1. Keypoint matching: learned SuperPoint + LightGlue (ONNX Runtime, CPU or GPU) when
     `AUTO_GCP_MODEL_DIR` holds `superpoint_lightglue_pipeline.onnx`, else a pure-NumPy
     cross-polarity descriptor fallback. Both search the four 90-degree flight headings; the learned
     matcher also searches several scan-to-viewport scales, because survey strips are rarely flown
     north-up and a frame rarely fills the Placement view. Dark film borders and caption strips are
     trimmed before matching.
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


def _trim_film_border(arr, dark=0.12, max_frac=0.15):
    """Crop dark film-frame edges and caption strips (rows/cols whose median is near black).

    Returns `(cropped, x0, y0)` so keypoints found in the crop can be shifted back.
    """
    h, w = arr.shape
    lim_y, lim_x = int(h * max_frac), int(w * max_frac)
    row_med = np.median(arr, axis=1)
    col_med = np.median(arr, axis=0)
    y0 = 0
    while y0 < lim_y and row_med[y0] < dark:
        y0 += 1
    y1 = h
    while h - y1 < lim_y and row_med[y1 - 1] < dark:
        y1 -= 1
    x0 = 0
    while x0 < lim_x and col_med[x0] < dark:
        x0 += 1
    x1 = w
    while w - x1 < lim_x and col_med[x1 - 1] < dark:
        x1 -= 1
    if y1 - y0 < 32 or x1 - x0 < 32:
        return arr, 0, 0
    return arr[y0:y1, x0:x1], x0, y0


def _normalize_contrast(arr):
    mu = float(arr.mean())
    sd = float(arr.std())
    if sd > 1e-5:
        arr = np.clip((arr - mu) / (2.5 * sd) + 0.5, 0.0, 1.0)
    return arr.astype(np.float32)


def _load_work_gray(img_bytes, max_side=MAX_WORK_SIDE, *, trim_border=False):
    """Return `(gray_float32, orig_w, orig_h, (x0, y0))` with `max(work_w, work_h) <= max_side`.

    `(x0, y0)` is the work-pixel offset of the returned array inside the full work image (non-zero
    only when `trim_border` removed a dark film frame or caption strip).
    """
    with Image.open(io.BytesIO(img_bytes)) as raw:
        raw = ImageOps.exif_transpose(raw).convert("L")
        orig_w, orig_h = raw.size
        scale = min(1.0, float(max_side) / max(orig_w, orig_h, 1))
        work_w = max(32, int(round(orig_w * scale)))
        work_h = max(32, int(round(orig_h * scale)))
        if (work_w, work_h) != (orig_w, orig_h):
            raw = raw.resize((work_w, work_h), Image.Resampling.BILINEAR)
        arr = np.asarray(raw, dtype=np.float32) / 255.0
    off = (0, 0)
    if trim_border:
        arr, ox, oy = _trim_film_border(arr)
        off = (ox, oy)
    return _normalize_contrast(arr), orig_w, orig_h, off


def _resize_gray(arr, factor):
    if abs(factor - 1.0) < 1e-6:
        return arr
    h, w = arr.shape
    nw, nh = max(32, int(round(w * factor))), max(32, int(round(h * factor)))
    img = Image.fromarray(np.clip(arr * 255.0, 0, 255).astype(np.uint8))
    return np.asarray(img.resize((nw, nh), Image.Resampling.LANCZOS), dtype=np.float32) / 255.0


def _unrotate_points(pts, k, rot_shape):
    """Map `(x, y)` points found in `np.rot90(img, k)` (shape `rot_shape`) back into `img` pixels."""
    out = np.asarray(pts, dtype=np.float64).copy()
    h, w = rot_shape
    for _ in range(k % 4):
        # np.rot90(a, 1)[i, j] == a[j, n - 1 - i]: undo one CCW quarter turn.
        x, y = out[:, 0].copy(), out[:, 1].copy()
        out[:, 0] = (h - 1) - y
        out[:, 1] = x
        h, w = w, h
    return out


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


ONNX_MODEL_NAME = "superpoint_lightglue_pipeline.onnx"
ONNX_REF_SIDE = 1024
_ONNX_CACHE = {}


def _onnx_session():
    """Return `(session, backend_name)` for `AUTO_GCP_MODEL_DIR/superpoint_lightglue_pipeline.onnx`.

    The model is fabio-sim/LightGlue-ONNX v2.0 (SuperPoint + LightGlue end-to-end, dynamic batch):
    input `images` `(2N, 1, H, W)` in `[0, 1]`, outputs `keypoints` `(2N, K, 2)`, `matches`
    `(M, 3)` as `[pair, idx0, idx1]`, and `mscores` `(M,)`. Returns `None` when unavailable.
    """
    model_dir = os.environ.get("AUTO_GCP_MODEL_DIR", "").strip()
    if not model_dir:
        return None
    path = Path(model_dir) / ONNX_MODEL_NAME
    if not path.is_file():
        return None
    key = str(path)
    if key in _ONNX_CACHE:
        return _ONNX_CACHE[key]
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    avail = ort.get_available_providers()
    providers = [p for p in ("CUDAExecutionProvider", "ROCMExecutionProvider", "CPUExecutionProvider") if p in avail]
    try:
        sess = ort.InferenceSession(key, providers=providers or ["CPUExecutionProvider"])
    except Exception:
        return None
    gpu = any(p != "CPUExecutionProvider" for p in sess.get_providers())
    _ONNX_CACHE[key] = (sess, "onnx_lightglue_gpu" if gpu else "onnx_lightglue_cpu")
    return _ONNX_CACHE[key]


def _match_onnx(sess, scan_gray, ref_gray):
    """Run one SuperPoint+LightGlue pair; both images are zero-padded onto a shared canvas."""
    h = max(scan_gray.shape[0], ref_gray.shape[0])
    w = max(scan_gray.shape[1], ref_gray.shape[1])
    batch = np.zeros((2, 1, h, w), dtype=np.float32)
    batch[0, 0, : scan_gray.shape[0], : scan_gray.shape[1]] = scan_gray
    batch[1, 0, : ref_gray.shape[0], : ref_gray.shape[1]] = ref_gray
    kpts, matches, mscores = sess.run(None, {"images": batch})
    matches = np.asarray(matches)
    if matches.ndim != 2 or len(matches) == 0:
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros((0,))
    pts_s = np.asarray(kpts[0], dtype=np.float64)[matches[:, 1]]
    pts_r = np.asarray(kpts[1], dtype=np.float64)[matches[:, 2]]
    # Drop keypoints that LightGlue placed on the zero padding.
    keep = (
        (pts_s[:, 0] < scan_gray.shape[1])
        & (pts_s[:, 1] < scan_gray.shape[0])
        & (pts_r[:, 0] < ref_gray.shape[1])
        & (pts_r[:, 1] < ref_gray.shape[0])
    )
    return pts_s[keep], pts_r[keep], np.clip(np.asarray(mscores, dtype=np.float64)[keep], 0.05, 0.99)


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


ROTATIONS = (0, 1, 2, 3)
# Scan long side as a fraction of the reference long side. Aerial frames rarely fill the Placement
# viewport, so the learned matcher also tries the frame shrunk to a portion of the view.
ONNX_SCAN_FRACTIONS = (1.0, 0.6, 0.4, 0.25)
NUMPY_SCAN_FRACTIONS = (1.0,)
EARLY_EXIT_INLIERS = 40


def _quadrants(pts, w, h):
    return len({(0 if x < w / 2.0 else 1) + (0 if y < h / 2.0 else 2) for x, y in pts})


def _hull_fraction(pts, w, h):
    """Area of the convex hull of `pts` as a fraction of the w x h picture (0 when fewer than 3 points)."""
    p = sorted({(float(x), float(y)) for x, y in pts})
    if len(p) < 3 or w <= 0 or h <= 0:
        return 0.0

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for q in p:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(p):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    hull = lower[:-1] + upper[:-1]
    area = 0.5 * abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(hull, hull[1:] + hull[:1], strict=True)))
    return min(1.0, area / float(w * h))


def _search_hypotheses(matcher, scan_gray, ref_gray, fractions, *, inlier_px, rotations=ROTATIONS):
    """Try each 90-degree heading x scan scale; return the best RANSAC hypothesis.

    Points are returned in un-rotated, un-scaled scan work pixels so the homography and the
    returned GCPs stay in the scan's own frame regardless of which heading matched.
    """
    sh, sw = scan_gray.shape
    rh, rw = ref_gray.shape
    best = None
    tried = 0
    for frac in fractions:
        factor = min(1.0, frac * max(rw, rh) / float(max(sw, sh)))
        if max(sw, sh) * factor < 96:
            continue
        scaled = _resize_gray(scan_gray, factor)
        fx = sw / float(scaled.shape[1])
        fy = sh / float(scaled.shape[0])
        for k in rotations:
            rot = np.rot90(scaled, k)
            tried += 1
            ps, pr, ms = matcher(np.ascontiguousarray(rot), ref_gray)
            if len(ps) < 4:
                continue
            ps0 = _unrotate_points(ps, k, rot.shape)
            ps0[:, 0] *= fx
            ps0[:, 1] *= fy
            H, mask, errs = _ransac_homography(ps0, pr, sw, sh, rw, rh, thresh_px=inlier_px)
            n = int(mask.sum())
            if H is None:
                continue
            rmse = math.sqrt(float(np.mean(errs[mask] ** 2))) if n else 999.0
            cand = {
                "pts_s": ps0,
                "pts_r": pr,
                "mscores": ms,
                "H": H,
                "mask": mask,
                "errs": errs,
                "n": n,
                "rmse": rmse,
                "rotation_deg": 90 * k,
                "scan_factor": round(factor, 3),
            }
            if best is None or (n, -rmse) > (best["n"], -best["rmse"]):
                best = cand
            if n >= EARLY_EXIT_INLIERS and _quadrants(ps0[mask], sw, sh) >= 3:
                best["tried"] = tried
                return best
    if best is not None:
        best["tried"] = tried
    return best


def _confidence(n_inliers, total_matches, quadrants_covered, rmse_px, inlier_px, hull_frac=None):
    """Score in [0, 1] from inlier count, inlier ratio, quadrant spread and RMSE, cut down when the matched
    points cover little of the picture: points bunched on one corner (or on a printed label) fit any warp there
    and say nothing about the rest, so they must not read as a sure match."""
    s_inliers = min(1.0, n_inliers / 12.0)
    s_ratio = min(1.0, (n_inliers / max(total_matches, 1)) / 0.45)
    s_spread = quadrants_covered / 4.0
    # A 4-point homography has 0 degrees of freedom (RMSE is identically ~0 for any 4 points).
    dof_weight = 0.0 if n_inliers <= 4 else (0.4 if n_inliers == 5 else 1.0)
    s_rmse = max(0.0, 1.0 - (rmse_px / float(inlier_px))) * dof_weight
    raw_conf = 0.32 * s_inliers + 0.18 * s_ratio + 0.28 * s_spread + 0.22 * s_rmse
    if n_inliers < 5:
        raw_conf = min(raw_conf, 0.35)
    if quadrants_covered <= 2:
        raw_conf = min(raw_conf, 0.5)
    if hull_frac is not None:
        # Full weight once the points span 30% of the picture; down to 55% for a single cluster.
        raw_conf *= 0.55 + 0.45 * min(1.0, hull_frac / 0.3)
    return round(max(0.0, min(1.0, raw_conf)), 3)


def _warp_shape_problem(corners, scan_w, scan_h, is_map):
    """Why the matched warp cannot be right, or None. `corners` are the scan's TL, TR, BR, BL in reference pixels.

    Vertical survey frames and printed maps keep their shape: square corners, near-equal opposite sides and the
    picture's own aspect. A handful of chance matches can still fit a homography that folds or stretches the
    frame across the map, so a match that does that is rejected however many points agree with it.
    """
    q = np.asarray(corners, dtype=np.float64)
    edges = np.roll(q, -1, axis=0) - q
    lengths = np.linalg.norm(edges, axis=1)
    if lengths.min() < 1e-6:
        return "folded"
    cross = edges[:, 0] * np.roll(edges, -1, axis=0)[:, 1] - edges[:, 1] * np.roll(edges, -1, axis=0)[:, 0]
    if not (np.all(cross > 0) or np.all(cross < 0)):
        return "folded"
    max_side_ratio, max_angle_off = (1.15, 12.0) if is_map else (1.35, 20.0)
    if max(lengths[0], lengths[2]) / min(lengths[0], lengths[2]) > max_side_ratio:
        return "skewed"
    if max(lengths[1], lengths[3]) / min(lengths[1], lengths[3]) > max_side_ratio:
        return "skewed"
    for i in range(4):
        a, b = -edges[i - 1], edges[i]
        cos = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        if abs(math.degrees(math.acos(max(-1.0, min(1.0, cos)))) - 90.0) > max_angle_off:
            return "sheared"
    aspect = ((lengths[0] + lengths[2]) / 2.0) / ((lengths[1] + lengths[3]) / 2.0)
    want = scan_w / float(max(scan_h, 1))
    if max(aspect / want, want / aspect) > 1.4:
        return "stretched"
    return None


def _work_to_full(off, work_scale):
    """3x3 matrix taking trimmed-work pixels to full-resolution pixels."""
    k = 1.0 / work_scale
    return np.array([[k, 0.0, off[0] * k], [0.0, k, off[1] * k], [0.0, 0.0, 1.0]], dtype=np.float64)


def _spread_pick(pts, scores, w, h, n_max, grid=4):
    """Indices of up to `n_max` points, best-scored first within each cell of a `grid` x `grid` layout."""
    order = np.argsort(-np.asarray(scores))
    seen, picked = set(), []
    for i in order:
        cell = (min(grid - 1, int(pts[i, 0] / w * grid)), min(grid - 1, int(pts[i, 1] / h * grid)))
        if cell in seen:
            continue
        seen.add(cell)
        picked.append(int(i))
        if len(picked) >= n_max:
            break
    return picked


def match_pair(
    a_bytes,
    b_bytes,
    *,
    a_size=None,
    b_size=None,
    max_points=12,
    min_inliers=8,
    inlier_px=4.0,
):
    """Match frame `b` onto overlapping frame `a` (e.g. neighbouring exposures of one survey strip).

    `a_size` / `b_size` = `(W, H)` of the full-resolution pictures the previews stand for (the
    pixels control points are stored in). Returns `H_b_to_a` (3x3 in those full-res pixels),
    up to `max_points` well-spread `pairs` `[b_col, b_row, a_col, a_row, confidence]` with the `a`
    side projected through the homography, and the same `confidence` / `inliers` / `rmse_px`
    fields as `match_and_score`. `ok` is False when too few consistent matches survive.
    """
    onnx = _onnx_session()
    side = ONNX_REF_SIDE if onnx else MAX_WORK_SIDE
    a_gray, a_w0, a_h0, a_off = _load_work_gray(a_bytes, side, trim_border=True)
    b_gray, b_w0, b_h0, b_off = _load_work_gray(b_bytes, side, trim_border=True)
    a_full_w, a_full_h = (int(a_size[0]), int(a_size[1])) if a_size else (a_w0, a_h0)
    b_full_w, b_full_h = (int(b_size[0]), int(b_size[1])) if b_size else (b_w0, b_h0)
    a_scale = min(1.0, float(side) / max(a_w0, a_h0, 1)) * a_w0 / float(a_full_w)
    b_scale = min(1.0, float(side) / max(b_w0, b_h0, 1)) * b_w0 / float(b_full_w)

    if onnx is not None:
        sess, backend = onnx
        best = _search_hypotheses(lambda s_, r_: _match_onnx(sess, s_, r_), b_gray, a_gray, (1.0,), inlier_px=inlier_px)
    else:
        backend = "numpy_local"
        best = _search_hypotheses(_match_numpy, b_gray, a_gray, (1.0,), inlier_px=inlier_px)

    base = {"backend": backend, "ok": False, "pairs": [], "H_b_to_a": None, "confidence": 0.0, "inliers": 0}
    if best is None or best["n"] < min_inliers:
        base.update(
            inliers=0 if best is None else best["n"],
            total_matches=0 if best is None else int(len(best["pts_s"])),
            reason="These frames do not share enough matching ground to chain them.",
        )
        return base

    mask = best["mask"]
    ps, errs = best["pts_s"][mask], best["errs"][mask]
    ms = np.asarray(best["mscores"])[mask]
    bh, bw = b_gray.shape
    quadrants = _quadrants(ps, bw, bh)
    rmse = math.sqrt(float(np.mean(errs**2)))
    confidence = _confidence(
        int(mask.sum()), int(len(best["pts_s"])), quadrants, rmse, inlier_px, _hull_fraction(ps, bw, bh)
    )

    T_a = _work_to_full(a_off, a_scale)
    T_b = _work_to_full(b_off, b_scale)
    H_full = T_a @ best["H"] @ np.linalg.inv(T_b)
    H_full = H_full / H_full[2, 2]

    per_pt = 0.45 * np.clip(ms, 0.0, 1.0) + 0.55 * np.exp(-0.35 * errs**2)
    pick = _spread_pick(ps, per_pt, bw, bh, max(4, min(32, int(max_points))))
    b_full = _reproject(T_b, ps[pick])
    a_full = _reproject(H_full, b_full)
    pairs = []
    for (bc, br), (ac, ar), c in zip(b_full, a_full, per_pt[pick], strict=True):
        if not (0 <= bc <= b_full_w and 0 <= br <= b_full_h):
            continue
        pairs.append(
            [round(float(bc), 1), round(float(br), 1), round(float(ac), 2), round(float(ar), 2), round(float(c), 3)]
        )
    base.update(
        ok=len(pairs) >= 4,
        pairs=pairs,
        H_b_to_a=[[float(v) for v in row] for row in H_full],
        confidence=confidence,
        confidence_pct=round(confidence * 100.0, 1),
        inliers=int(mask.sum()),
        total_matches=int(len(best["pts_s"])),
        quadrants_covered=quadrants,
        rmse_px=round(rmse, 2),
        search={"rotation_deg": best["rotation_deg"], "tried": best.get("tried")},
    )
    return base


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
    kind="photo",
):
    """Detect and score GCP correspondences from `scan_bytes` onto `ref_bytes` (`ref_bounds=[w,s,e,n]`).

    Returns ranked points in full-resolution scan pixels (using `scan_target_size=(W, H)` when
    a downscaled preview was sent) + WGS84 lon/lat (exact Web Mercator inverse from `ref_bounds`),
    overall `confidence` in `[0.0, 1.0]`, and `meets_threshold` for HITL gating.

    `kind="map"` is for printed maps and flight-index cards: they are drawn north-up and flat, so only the
    upright heading is tried and a perspective warp is never recommended. `kind="photo"` (aerial frames) searches
    all four flight headings.
    """
    if len(ref_bounds) != 4 or not (ref_bounds[0] < ref_bounds[2] and ref_bounds[1] < ref_bounds[3]):
        raise ValueError("ref_bounds must be [west, south, east, north] with west < east and south < north.")
    threshold = max(0.25, min(0.99, float(confidence_threshold)))
    max_points = max(4, min(32, int(max_points)))
    is_map = str(kind or "photo").lower() == "map"
    rotations = (0,) if is_map else ROTATIONS

    onnx = _onnx_session()
    work_side = ONNX_REF_SIDE if onnx else MAX_WORK_SIDE
    scan_gray, orig_scan_w, orig_scan_h, (off_x, off_y) = _load_work_gray(scan_bytes, work_side, trim_border=True)
    ref_gray, _, _, _ = _load_work_gray(ref_bytes, work_side)
    work_scan_h, work_scan_w = scan_gray.shape
    work_ref_h, work_ref_w = ref_gray.shape
    # Full (untrimmed) work-image size, used to convert work pixels back to scan pixels.
    full_work_scale = min(1.0, float(work_side) / max(orig_scan_w, orig_scan_h, 1))
    full_work_w = max(32, int(round(orig_scan_w * full_work_scale)))
    full_work_h = max(32, int(round(orig_scan_h * full_work_scale)))
    target_w, target_h = orig_scan_w, orig_scan_h
    if scan_target_size and len(scan_target_size) == 2:
        target_w = max(1, int(scan_target_size[0]))
        target_h = max(1, int(scan_target_size[1]))

    if onnx is not None:
        sess, backend = onnx
        best = _search_hypotheses(
            lambda s, r: _match_onnx(sess, s, r),
            scan_gray,
            ref_gray,
            ONNX_SCAN_FRACTIONS,
            inlier_px=inlier_px,
            rotations=rotations,
        )
    else:
        backend = "numpy_local"
        best = _search_hypotheses(
            lambda s, r: _match_numpy(s, r),
            scan_gray,
            ref_gray,
            NUMPY_SCAN_FRACTIONS,
            inlier_px=inlier_px,
            rotations=rotations,
        )

    if best is None:
        pts_s = np.zeros((0, 2))
        pts_r = np.zeros((0, 2))
        mscores = np.zeros((0,))
        H, inlier_mask, errs = None, np.zeros(0, dtype=bool), np.zeros(0)
        search = {"rotation_deg": None, "scan_factor": None}
    else:
        pts_s, pts_r, mscores = best["pts_s"], best["pts_r"], best["mscores"]
        H, inlier_mask, errs = best["H"], best["mask"], best["errs"]
        search = {"rotation_deg": best["rotation_deg"], "scan_factor": best["scan_factor"], "tried": best["tried"]}

    total_matches = int(len(pts_s))
    inlier_idxs = np.nonzero(inlier_mask)[0]
    n_inliers = int(len(inlier_idxs))

    west, south, east, north = (float(v) for v in ref_bounds)
    ym_n, ym_s = _merc_y(north), _merc_y(south)
    sx_scan = target_w / float(full_work_w)
    sy_scan = target_h / float(full_work_h)

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
            "search": search,
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
                "col": round((sx + off_x) * sx_scan, 1),
                "row": round((sy + off_y) * sy_scan, 1),
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
    hull_frac = _hull_fraction(pts_s[inlier_idxs], work_scan_w, work_scan_h)
    confidence = _confidence(n_inliers, total_matches, quadrants_covered, rmse_px, inlier_px, hull_frac)

    # Check if homography has significant projective perspective skew (ratio of bottom vs top width)
    top_l = _reproject(H, np.array([[0.0, 0.0]]))[0]
    top_r = _reproject(H, np.array([[float(work_scan_w), 0.0]]))[0]
    bot_l = _reproject(H, np.array([[0.0, float(work_scan_h)]]))[0]
    bot_r = _reproject(H, np.array([[float(work_scan_w), float(work_scan_h)]]))[0]
    w_top = float(np.linalg.norm(top_r - top_l))
    w_bot = float(np.linalg.norm(bot_r - bot_l))
    skew_ratio = max(w_top, w_bot) / max(1e-3, min(w_top, w_bot))
    shape_problem = _warp_shape_problem([top_l, top_r, bot_r, bot_l], work_scan_w, work_scan_h, is_map)
    if shape_problem:
        low_conf = round(min(confidence, 0.2), 3)
        return {
            "points": [],
            "confidence": low_conf,
            "confidence_pct": round(low_conf * 100.0, 1),
            "meets_threshold": False,
            "threshold": threshold,
            "inliers": n_inliers,
            "total_matches": total_matches,
            "quadrants_covered": quadrants_covered,
            "coverage": round(hull_frac, 3),
            "kind": "map" if is_map else "photo",
            "rmse_px": round(rmse_px, 2),
            "backend": backend,
            "search": search,
            "recommended_warp": "auto",
            "shape_problem": shape_problem,
            "reason": (
                f"The landmarks that matched would leave your {'map' if is_map else 'photo'} {shape_problem} out of shape, "
                "so they are chance look-alikes. Zoom the reference map to roughly the area the picture covers, "
                "or place a few spots by hand."
            ),
        }
    if is_map:
        recommended_warp = "tps" if len(picked) >= 8 else "auto"
    else:
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
        "coverage": round(hull_frac, 3),
        "kind": "map" if is_map else "photo",
        "rmse_px": round(rmse_px, 2),
        "backend": backend,
        "search": search,
        "recommended_warp": recommended_warp,
    }
