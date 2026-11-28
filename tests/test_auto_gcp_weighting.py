"""Tests for road/bridge-aware structure weighting in `hub.auto_gcp`."""

import io

import numpy as np
import pytest
from hub import auto_gcp
from PIL import Image, ImageDraw


def _synthetic_delta_scene(size=256):
    """Build a synthetic delta aerial with four distinct terrain regions:

    - Top-left: dark river water with faint ripples
    - Top-right: uniform open rice paddy with gentle texture
    - Bottom-left: bright cloud glare with soft variation
    - Bottom-right: crisp road intersection + bridge/village block corners
    """
    rng = np.random.default_rng(123)
    img = np.full((size, size), 130.0, dtype=np.float32)
    half = size // 2

    # Top-left: dark water with faint ripples
    img[:half, :half] = 14.0 + rng.normal(0.0, 2.0, (half, half))

    # Top-right: uniform rice paddy with mild noise
    img[:half, half:] = 135.0 + rng.normal(0.0, 2.5, (half, half))

    # Bottom-left: bright cloud glare
    img[half:, :half] = 246.0 + rng.normal(0.0, 2.0, (half, half))

    # Bottom-right: crisp man-made road intersection & village blocks
    pil = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), mode="L")
    draw = ImageDraw.Draw(pil)
    cx, cy = half + half // 2, half + half // 2
    # Crossing dark roads with bright shoulders
    draw.line([(half + 12, cy), (size - 12, cy)], fill=25, width=5)
    draw.line([(cx, half + 12), (cx, size - 12)], fill=25, width=5)
    # Adjacent village blocks around the intersection
    for dx, dy in ((-28, -28), (12, -28), (-28, 12), (12, 12)):
        draw.rectangle([cx + dx, cy + dy, cx + dx + 14, cy + dy + 14], fill=215, outline=30, width=2)
    return np.asarray(pil, dtype=np.uint8), (cx, cy)


def test_structure_weight_map_favors_road_intersections_over_water_paddies_and_clouds():
    scene_u8, (ix, iy) = _synthetic_delta_scene(256)
    w_u8 = auto_gcp._structure_weight_map(scene_u8)
    w_f32 = auto_gcp._structure_weight_map(scene_u8.astype(np.float32) / 255.0)

    assert w_u8.shape == scene_u8.shape
    assert w_u8.dtype == np.float32
    assert float(w_u8.min()) >= 0.25 - 1e-6
    assert float(w_u8.max()) <= 1.0 + 1e-6
    assert np.allclose(w_u8, w_f32, atol=1e-5)

    water_mean = float(w_u8[24:104, 24:104].mean())
    paddy_mean = float(w_u8[24:104, 152:232].mean())
    cloud_mean = float(w_u8[152:232, 24:104].mean())
    intersection_peak = float(w_u8[iy - 6 : iy + 7, ix - 6 : ix + 7].max())
    intersection_mean = float(w_u8[iy - 4 : iy + 5, ix - 4 : ix + 5].mean())

    assert water_mean < 0.32, f"dark water weight too high: {water_mean}"
    assert paddy_mean < 0.35, f"uniform paddy weight too high: {paddy_mean}"
    assert cloud_mean < 0.32, f"bright cloud glare weight too high: {cloud_mean}"
    assert intersection_peak > 0.85, f"intersection peak too low: {intersection_peak}"
    assert intersection_mean > max(water_mean, paddy_mean, cloud_mean) + 0.35

    # Completely uniform image stays at the floor weight
    flat = np.full((64, 64), 128, dtype=np.uint8)
    w_flat = auto_gcp._structure_weight_map(flat)
    assert np.allclose(w_flat, 0.25, atol=1e-5)


def test_sample_weights_interpolates_and_clamps():
    wmap = np.array([[0.25, 0.75], [0.50, 1.00]], dtype=np.float32)
    pts = np.array([[0.0, 0.0], [1.0, 1.0], [0.5, 0.5], [-5.0, 99.0]], dtype=np.float64)
    sampled = auto_gcp._sample_weights(wmap, pts)
    assert sampled == pytest.approx([0.25, 1.00, 0.625, 0.50], abs=1e-5)
    assert len(auto_gcp._sample_weights(wmap, np.zeros((0, 2)))) == 0


def test_weighted_ransac_prefers_high_structure_true_model_over_more_low_weight_spurious_matches():
    """When spurious flat-region matches outnumber true landmarks, structure weights steer RANSAC to the true model."""
    w, h = 400, 400
    # True model: slight shift + scale (x' = 0.95x + 15, y' = 0.95y + 12)
    true_src = np.array(
        [
            [50.0, 60.0],
            [330.0, 55.0],
            [340.0, 330.0],
            [60.0, 340.0],
            [190.0, 80.0],
            [210.0, 310.0],
            [85.0, 200.0],
            [315.0, 210.0],
            [150.0, 160.0],
            [260.0, 250.0],
        ],
        dtype=np.float64,
    )
    true_dst = np.column_stack([0.95 * true_src[:, 0] + 15.0, 0.95 * true_src[:, 1] + 12.0])

    # Competing spurious model: 14 low-weight matches (e.g., repeating paddy grid shifted by +85px)
    spurious_src = np.array(
        [
            [40.0, 40.0],
            [120.0, 45.0],
            [200.0, 50.0],
            [280.0, 45.0],
            [45.0, 130.0],
            [130.0, 135.0],
            [220.0, 140.0],
            [290.0, 130.0],
            [50.0, 230.0],
            [140.0, 235.0],
            [230.0, 240.0],
            [295.0, 230.0],
            [100.0, 300.0],
            [250.0, 295.0],
        ],
        dtype=np.float64,
    )
    spurious_dst = np.column_stack([0.92 * spurious_src[:, 0] + 85.0, 0.92 * spurious_src[:, 1] + 65.0])

    pts_s = np.vstack([true_src, spurious_src])
    pts_r = np.vstack([true_dst, spurious_dst])
    weights = np.array([0.95] * len(true_src) + [0.25] * len(spurious_src), dtype=np.float64)

    # Unweighted RANSAC picks the larger 14-point spurious consensus
    H_unw, mask_unw, _ = auto_gcp._ransac_homography(pts_s, pts_r, w, h, w, h, thresh_px=5.0)
    assert H_unw is not None
    assert int(mask_unw[len(true_src) :].sum()) >= 12

    # Structure-weighted RANSAC locks onto the 10 high-structure landmarks instead
    H_w, mask_w, errs_w = auto_gcp._ransac_homography(pts_s, pts_r, w, h, w, h, thresh_px=5.0, weights=weights)
    assert H_w is not None
    assert int(mask_w[: len(true_src)].sum()) == len(true_src)
    assert int(mask_w[len(true_src) :].sum()) == 0
    assert float(errs_w[: len(true_src)].max()) < 1.0


def test_match_and_score_reports_structure_weighted_and_prioritizes_landmarks(monkeypatch):
    """`match_and_score` sets `structure_weighted=True` and ranks high-structure intersections ahead of paddies."""
    scene_u8, (ix, iy) = _synthetic_delta_scene(256)
    buf = io.BytesIO()
    Image.fromarray(scene_u8, mode="L").convert("RGB").save(buf, format="JPEG", quality=95)
    img_bytes = buf.getvalue()

    # Place 4 identical-score matches in flat regions + 4 on the high-structure intersection/blocks
    pts = np.array(
        [
            [60.0, 60.0],  # Q0 dark water
            [190.0, 60.0],  # Q1 uniform paddy
            [60.0, 190.0],  # Q2 bright cloud
            [150.0, 150.0],  # Q3 flat margin near blocks
            [float(ix), float(iy)],  # Q3 road intersection center
            [float(ix - 28), float(iy - 28)],  # Q3 village block corner
            [float(ix + 12), float(iy + 12)],  # Q3 village block corner
            [float(ix + 12), float(iy - 28)],  # Q3 village block corner
        ],
        dtype=np.float64,
    )
    scores = np.full(len(pts), 0.85, dtype=np.float64)

    monkeypatch.setattr(auto_gcp, "_onnx_session", lambda: None)
    monkeypatch.setattr(auto_gcp, "_match_numpy", lambda _s, _r: (pts.copy(), pts.copy(), scores.copy()))

    res = auto_gcp.match_and_score(
        img_bytes, img_bytes, [105.5, 10.4, 105.6, 10.5], confidence_threshold=0.5, max_points=6, kind="map"
    )
    assert res["structure_weighted"] is True
    assert len(res["points"]) >= 4
    assert all("structure_weight" in p for p in res["points"])
    # Points remain sorted by confidence descending, and the top point is a high-structure landmark
    confs = [p["confidence"] for p in res["points"]]
    assert confs == sorted(confs, reverse=True)
    assert res["points"][0]["structure_weight"] > 0.70
    assert res["points"][0]["structure_weight"] > res["points"][-1]["structure_weight"]
