"""Strip chaining: placing neighbouring survey frames from one placed frame."""

import io
import json
import math
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
import strip_chain
from hub import auto_gcp
from PIL import Image, ImageDraw, ImageOps

STEP = 400  # synthetic frames advance 400 px down the strip
COS_20 = math.cos(math.radians(20.0))


def frame(n, mission="HV999", placed=None, w=1000, h=1000):
    return {
        "id": f"custom_{n:010d}",
        "kind": "scan",
        "title": f"Mission {mission} SHDAI__AI_75_E_1_{mission}__{n:04d}__V.jpg",
        "image": {"width": w, "height": h},
        "points": placed or [],
    }


def anchor_points(lon0=105.0, lat0=20.0):
    # Metric square pixels at lat 20 deg: dlon = 1e-5 / cos(20 deg), dlat = -1e-5
    return [[c, r, lon0 + c * 1e-5 / COS_20, lat0 - r * 1e-5] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))]


def test_strip_key_parses_shdai_titles():
    assert strip_chain.strip_key(frame(7)) == ("SHDAI__AI_75_E_1_HV999", 7)
    assert strip_chain.strip_key({"title": "Phnom Penh (feuille 20)"}) is None
    renamed = {**frame(7), "title": "Thọ Xuân 1952", "original_title": frame(7)["title"]}
    assert strip_chain.strip_key(renamed) == ("SHDAI__AI_75_E_1_HV999", 7)


def test_plan_chain_walks_both_ways_keeps_user_frames_and_stops_on_low_confidence():
    """1 placed by the user, 4 placed by the user, 6 too uncertain, 7 beyond the stop, 0 the other way."""
    conf = {0: 0.9, 2: 0.95, 3: 0.92, 5: 0.9, 6: 0.4, 7: 0.99}
    frames = [frame(n) for n in range(0, 8)]
    frames[1]["points"] = anchor_points()
    # Frame 4 is placed 3 * STEP south of frame 1, with a +0.01 lon shift.
    frames[4]["points"] = anchor_points(lon0=105.01, lat0=20.0 - 3 * STEP * 1e-5)
    calls = []

    def fake_match(b, a):
        nb, na = strip_chain.strip_key(b)[1], strip_chain.strip_key(a)[1]
        calls.append((nb, na))
        shift = STEP * (nb - na)  # b pixel (c, r) shows the ground of a pixel (c, r + shift)
        pairs = [[c, r, c, r + shift, 0.9] for c, r in ((100, 100), (900, 100), (900, 500), (100, 500), (500, 300))]
        return {"ok": True, "pairs": pairs, "confidence": conf[nb], "confidence_pct": conf[nb] * 100, "inliers": 50}

    plan = strip_chain.plan_chain(frames, frames[1]["id"], fake_match, threshold=0.8)
    got = {p["frame"]: p for p in plan["proposals"]}
    assert plan["anchors"] == [1, 4]
    assert plan["adjusted"] is True
    assert sorted(got) == [0, 2, 3, 5, 6]
    assert 4 not in got and 7 not in got  # user's frame kept; nothing past the stop
    assert (got[2]["from_frame"], got[2]["hops"]) == (1, 1)
    assert (got[3]["from_frame"], got[3]["hops"]) == (2, 2)
    assert (got[5]["from_frame"], got[5]["hops"]) == (4, 1)  # chain re-anchors on the user's frame
    assert got[6]["meets_threshold"] is False
    assert [s["frame"] for s in plan["stops"]] == [6]
    # Frame 3 is bracketed by anchors 1 and 4: lon is distributed between Frame 1 and Frame 4's +0.01 shift,
    # and lat stays near 19.991 (slight rotation because 4-DOF similarity bridges the lateral offset without shear).
    c, r, lon, lat = got[3]["points"][0]
    assert (c, r) == (100, 100)
    assert lat == pytest.approx(19.991, abs=1e-3)
    assert 105.001 < lon < 105.011
    # Frame 5 inherits the user's +0.01 lon offset of frame 4, not the anchor's.
    assert got[5]["points"][0][2] == pytest.approx(105.01 + 100 * 1e-5 / COS_20, abs=1e-5)
    assert (7, 6) not in calls


def test_plan_chain_needs_a_placed_strip_frame():
    with pytest.raises(ValueError, match="Place this frame"):
        strip_chain.plan_chain([frame(1), frame(2)], frame(1)["id"], lambda b, a: None)
    loose = {**frame(1, placed=anchor_points()), "title": "My scan.jpg"}
    with pytest.raises(ValueError, match="survey strip"):
        strip_chain.plan_chain([loose], loose["id"], lambda b, a: None)


def _scene(size=1400, seed=3):
    img = Image.new("L", (size, size), 140)
    d = ImageDraw.Draw(img)
    rng = np.random.default_rng(seed)
    for _ in range(420):
        x0, y0 = int(rng.integers(0, size - 40)), int(rng.integers(0, size - 40))
        w, h = int(rng.integers(10, 40)), int(rng.integers(10, 40))
        d.rectangle([x0, y0, x0 + w, y0 + h], fill=int(rng.integers(30, 230)), outline=15, width=2)
    d.line([(0, 200), (size, 900)], fill=10, width=6)
    return img


def _jpg(im):
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def test_match_pair_links_overlapping_frames_flown_the_other_way():
    """Frame b overlaps a by 60%, is flown 180 degrees round, and carries a dark film border."""
    scene = _scene()
    a = scene.crop((0, 0, 600, 600))
    b_ground = scene.crop((0, 240, 600, 840))  # b pixel (c, r) == a pixel (c, r + 240)
    b = ImageOps.expand(b_ground.rotate(180), border=20, fill=4)
    res = auto_gcp.match_pair(_jpg(a), _jpg(b), max_points=12)
    assert res["ok"] is True, res
    assert res["search"]["rotation_deg"] == 180
    assert res["inliers"] >= 10 and res["confidence"] >= 0.6
    for bc, br, ac, ar, _c in res["pairs"]:
        # Undo border + 180-degree turn: ground pixel = (599 - (bc - 20), 599 - (br - 20)) in b_ground.
        gc, gr = 599 - (bc - 20), 599 - (br - 20)
        assert ac == pytest.approx(gc, abs=3.0) and ar == pytest.approx(gr + 240, abs=3.0)

    noise = Image.fromarray(np.random.default_rng(1).integers(0, 255, (600, 600), dtype=np.uint8))
    bad = auto_gcp.match_pair(_jpg(a), _jpg(noise))
    assert bad["ok"] is False and bad["pairs"] == []


def test_chain_route_plans_and_places_neighbour(tmp_path, monkeypatch):
    import server
    import store
    from fakeweb import FakeArchive, make_library

    scene = _scene()
    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    monkeypatch.setattr(lib, "start_worker", lambda: None)  # no tile builds in this test
    ids = []
    for n, top in ((1, 0), (2, 240)):
        part = lib.custom.incoming_path()
        part.write_bytes(_jpg(scene.crop((0, top, 600, top + 600))))
        e = lib.custom.ingest(part, f"f{n}.jpg", f"HV999 SHDAI__AI_75_E_1_HV999__{n:04d}__V.jpg", "1952")
        ids.append(e["id"])
    w = lib.custom.get(ids[0])["image"]["width"]
    k = w / 600.0
    pts = [[c * k, r * k, 105 + c * 1e-5 / COS_20, 20 - r * 1e-5] for c, r in ((0, 0), (600, 0), (600, 600), (0, 600))]
    lib.custom.save_points(ids[0], pts, "f1", 1952)

    db = tmp_path / "desk" / "e.sqlite"
    store.connect(db).close()
    srv = server.make_server(0, db, map_library=lib)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def post(route, body):
        req = urllib.request.Request(
            f"{base}{route}",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            raise AssertionError(e.read().decode()) from None

    try:
        plan = post(f"/api/library/custom/{ids[0]}/chain", {"confidence_threshold": 0.6})
        assert plan["anchor"]["frame"] == 1
        [prop] = plan["proposals"]
        assert prop["id"] == ids[1] and prop["meets_threshold"] is True
        c, r, lon, lat = prop["points"][0]
        assert lon == pytest.approx(105 + (c / k) * 1e-5 / COS_20, abs=4e-5)
        assert lat == pytest.approx(20 - (r / k + 240) * 1e-5, abs=4e-5)

        listing = post(f"/api/library/custom/{ids[0]}/chain", {"apply": [{"id": prop["id"], "points": prop["points"]}]})
        placed = next(m for m in listing["maps"] if m["id"] == ids[1])
        assert len(placed["points"]) == len(prop["points"]) and placed["warp_mode"] == "affine"
        # The renamed anchor kept its strip identity.
        anchor = next(m for m in listing["maps"] if m["id"] == ids[0])
        assert anchor["title"] == "f1" and "SHDAI__AI_75_E_1_HV999__0001" in anchor["original_title"]
    finally:
        srv.quit()
        archive.close()


def _synthetic_frame_transform(frame_idx, *, scale=1.2e-5, theta_deg=25.0, lon0=107.0, lat0=16.5):
    """True metric 4-DOF similarity `(c, r) -> (lon, lat)` for frame `frame_idx` along a rotated strip."""
    cos_lat = math.cos(math.radians(lat0))
    th = math.radians(theta_deg)
    a = scale * math.cos(th)
    b = scale * math.sin(th)
    # Strip advances STEP pixels along the frame's +row axis each exposure.
    r_offset = (frame_idx - 1) * STEP
    tx = lon0 * cos_lat + b * r_offset
    ty = lat0 - a * r_offset
    return a, b, tx, ty, cos_lat


def _apply_true(tf, c, r):
    a, b, tx, ty, cos_lat = tf
    e = a * c + b * r + tx
    n = b * c - a * r + ty
    return e / cos_lat, n


def test_similarity_from_points_preserves_aspect_ratio_and_zero_shear():
    """4-DOF similarity fit has exact 1:1 metric aspect ratio and zero shear on rotated/scaled frames."""
    rng = np.random.default_rng(42)
    tf = _synthetic_frame_transform(1, scale=1.4e-5, theta_deg=37.0, lon0=106.5, lat0=16.0)
    true_scale = 1.4e-5
    cos_lat = tf[4]
    pts = []
    for c, r in ((50, 80), (920, 110), (880, 910), (90, 870), (500, 480), (260, 640)):
        lon, lat = _apply_true(tf, c, r)
        # Add realistic ~1.5 px equivalent noise in ground coords.
        lon += float(rng.normal(0.0, 1.5 * true_scale / cos_lat))
        lat += float(rng.normal(0.0, 1.5 * true_scale))
        pts.append([c, r, lon, lat])

    coef = strip_chain.similarity_from_points(pts)
    assert coef is not None
    # Convert 2x2 linear part back into metric (lon * cos_lat, lat) space.
    mean_lat = float(np.mean([p[3] for p in pts]))
    c_lat = math.cos(math.radians(mean_lat))
    col_vec = np.array([coef[0, 0] * c_lat, coef[0, 1]])
    row_vec = np.array([coef[1, 0] * c_lat, coef[1, 1]])
    s_col = float(np.linalg.norm(col_vec))
    s_row = float(np.linalg.norm(row_vec))
    assert s_col == pytest.approx(s_row, rel=1e-12)
    assert float(np.dot(col_vec, row_vec)) == pytest.approx(0.0, abs=1e-18)
    assert s_col == pytest.approx(true_scale, rel=0.01)

    # Degenerate inputs return None.
    assert strip_chain.similarity_from_points([[0, 0, 105.0, 20.0]]) is None
    assert strip_chain.similarity_from_points([[5, 5, 105.0, 20.0], [5, 5, 105.0, 20.0]]) is None


def test_ten_frame_strip_single_anchor_no_scale_drift():
    """Chaining 10 overlapping rotated frames from 1 anchor with match noise preserves scale without drift."""
    rng = np.random.default_rng(7)
    true_scale = 1.2e-5
    frames = [frame(n) for n in range(1, 11)]
    tf1 = _synthetic_frame_transform(1, scale=true_scale, theta_deg=25.0)
    frames[0]["points"] = [[c, r, *_apply_true(tf1, c, r)] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))]
    overlap_pts = [(120, 80), (500, 100), (880, 90), (150, 320), (520, 340), (860, 310), (200, 520), (820, 510)]

    def noisy_match(b, a):
        nb, na = strip_chain.strip_key(b)[1], strip_chain.strip_key(a)[1]
        shift = STEP * (nb - na)
        pairs = []
        for c, r in overlap_pts:
            ac = c + float(rng.normal(0.0, 0.5))
            ar = r + shift + float(rng.normal(0.0, 0.5))
            pairs.append([c, r, ac, ar, 0.92])
        return {"ok": True, "pairs": pairs, "confidence": 0.92, "confidence_pct": 92.0, "inliers": len(pairs)}

    plan = strip_chain.plan_chain(frames, frames[0]["id"], noisy_match, threshold=0.8)
    assert plan["adjusted"] is False
    assert plan["anchors"] == [1]
    assert len(plan["proposals"]) == 9

    for prop in plan["proposals"]:
        coef = strip_chain.similarity_from_points(prop["points"])
        assert coef is not None
        mean_lat = float(np.mean([p[3] for p in prop["points"]]))
        c_lat = math.cos(math.radians(mean_lat))
        col_vec = np.array([coef[0, 0] * c_lat, coef[0, 1]])
        row_vec = np.array([coef[1, 0] * c_lat, coef[1, 1]])
        s_col = float(np.linalg.norm(col_vec))
        s_row = float(np.linalg.norm(row_vec))
        # No shear, 1:1 aspect ratio, and scale stays within 2% of true scale even after 9 hops.
        assert s_col == pytest.approx(s_row, rel=1e-9)
        assert float(np.dot(col_vec, row_vec)) == pytest.approx(0.0, abs=1e-16)
        assert s_col == pytest.approx(true_scale, rel=0.02)


def test_ten_frame_strip_two_anchors_bundle_adjustment_reduces_error():
    """With anchors at Frame 1 and Frame 10, bundle adjustment distributes per-hop drift globally."""
    true_scale = 1.2e-5
    tfs = {n: _synthetic_frame_transform(n, scale=true_scale, theta_deg=20.0) for n in range(1, 11)}
    overlap_pts = [(100, 100), (500, 100), (900, 100), (100, 500), (500, 300), (900, 500)]

    # Inject consistent per-hop bias + random jitter so single-anchor cantilevering drifts noticeably by mid-strip.
    rng = np.random.default_rng(123)
    pair_noise = {}
    for n in range(1, 10):
        # Systematic +4 px col bias, -3 px row bias + 1.5 px jitter per consecutive pair (n+1 <-> n).
        noise = [(4.0 + float(rng.normal(0.0, 1.5)), -3.0 + float(rng.normal(0.0, 1.5))) for _ in overlap_pts]
        pair_noise[(n + 1, n)] = noise

    def biased_match(b, a):
        nb, na = strip_chain.strip_key(b)[1], strip_chain.strip_key(a)[1]
        shift = STEP * (nb - na)
        key = (nb, na) if (nb, na) in pair_noise else (na, nb)
        sign = 1.0 if (nb, na) in pair_noise else -1.0
        pairs = []
        for (c, r), (dc, dr) in zip(overlap_pts, pair_noise[key], strict=True):
            pairs.append([c, r, c + sign * dc, r + shift + sign * dr, 0.95])
        return {"ok": True, "pairs": pairs, "confidence": 0.95, "confidence_pct": 95.0, "inliers": len(pairs)}

    # 1. Single-anchor cantilever from Frame 1 only.
    frames_1 = [frame(n) for n in range(1, 11)]
    frames_1[0]["points"] = [
        [c, r, *_apply_true(tfs[1], c, r)] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))
    ]
    plan_single = strip_chain.plan_chain(frames_1, frames_1[0]["id"], biased_match, threshold=0.8)
    assert plan_single["adjusted"] is False

    # 2. Dual-anchor bundle adjustment with Frame 1 AND Frame 10 placed.
    frames_2 = [frame(n) for n in range(1, 11)]
    frames_2[0]["points"] = [
        [c, r, *_apply_true(tfs[1], c, r)] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))
    ]
    frames_2[9]["points"] = [
        [c, r, *_apply_true(tfs[10], c, r)] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))
    ]
    plan_dual = strip_chain.plan_chain(frames_2, frames_2[0]["id"], biased_match, threshold=0.8)
    assert plan_dual["adjusted"] is True
    assert plan_dual["anchors"] == [1, 10]

    def mid_strip_rmse(plan):
        cos_lat = tfs[1][4]
        sq_errs = []
        for prop in plan["proposals"]:
            fn = prop["frame"]
            if fn not in (4, 5, 6, 7):
                continue
            coef = strip_chain.similarity_from_points(prop["points"])
            lon, lat = (float(v) for v in np.array([500.0, 500.0, 1.0]) @ coef)
            tlon, tlat = _apply_true(tfs[fn], 500.0, 500.0)
            de = (lon - tlon) * cos_lat
            dn = lat - tlat
            sq_errs.append(de * de + dn * dn)
        return math.sqrt(float(np.mean(sq_errs)))

    err_single = mid_strip_rmse(plan_single)
    err_dual = mid_strip_rmse(plan_dual)
    assert err_dual < err_single * 0.25, f"Expected dual-anchor RMSE ({err_dual:.3e}) << single ({err_single:.3e})"
