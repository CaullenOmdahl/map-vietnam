"""Strip chaining: placing neighbouring survey frames from one placed frame."""

import io
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
import strip_chain
from hub import auto_gcp
from PIL import Image, ImageDraw, ImageOps

STEP = 400  # synthetic frames advance 400 px down the strip


def frame(n, mission="HV999", placed=None, w=1000, h=1000):
    return {
        "id": f"custom_{n:010d}",
        "kind": "scan",
        "title": f"Mission {mission} SHDAI__AI_75_E_1_{mission}__{n:04d}__V.jpg",
        "image": {"width": w, "height": h},
        "points": placed or [],
    }


def anchor_points():
    # lon = 105 + col * 1e-5, lat = 20 - row * 1e-5
    return [[c, r, 105 + c * 1e-5, 20 - r * 1e-5] for c, r in ((0, 0), (1000, 0), (1000, 1000), (0, 1000))]


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
    frames[4]["points"] = [[c, r, lon + 0.01, lat] for c, r, lon, lat in anchor_points()]
    calls = []

    def fake_match(b, a):
        nb, na = strip_chain.strip_key(b)[1], strip_chain.strip_key(a)[1]
        calls.append((nb, na))
        shift = STEP * (nb - na)  # b pixel (c, r) shows the ground of a pixel (c, r + shift)
        pairs = [[c, r, c, r + shift, 0.9] for c, r in ((100, 100), (900, 100), (900, 500), (100, 500), (500, 300))]
        return {"ok": True, "pairs": pairs, "confidence": conf[nb], "confidence_pct": conf[nb] * 100, "inliers": 50}

    plan = strip_chain.plan_chain(frames, frames[1]["id"], fake_match, threshold=0.8)
    got = {p["frame"]: p for p in plan["proposals"]}
    assert sorted(got) == [0, 2, 3, 5, 6]
    assert 4 not in got and 7 not in got  # user's frame kept; nothing past the stop
    assert (got[2]["from_frame"], got[2]["hops"]) == (1, 1)
    assert (got[3]["from_frame"], got[3]["hops"]) == (2, 2)
    assert (got[5]["from_frame"], got[5]["hops"]) == (4, 1)  # chain re-anchors on the user's frame
    assert got[6]["meets_threshold"] is False
    assert [s["frame"] for s in plan["stops"]] == [6]
    # Frame 3 pixel (100, 100) is frame 1 pixel (100, 900) -> lat 20 - 0.009, through two hops.
    c, r, lon, lat = got[3]["points"][0]
    assert (c, r) == (100, 100)
    assert lon == pytest.approx(105.001, abs=1e-6) and lat == pytest.approx(19.991, abs=1e-6)
    # Frame 5 inherits the user's +0.01 lon offset of frame 4, not the anchor's.
    assert got[5]["points"][0][2] == pytest.approx(105.011, abs=1e-6)
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
    pts = [[c * k, r * k, 105 + c * 1e-5, 20 - r * 1e-5] for c, r in ((0, 0), (600, 0), (600, 600), (0, 600))]
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
        assert lon == pytest.approx(105 + (c / k) * 1e-5, abs=4e-5)
        assert lat == pytest.approx(20 - (r / k + 240) * 1e-5, abs=4e-5)

        listing = post(f"/api/library/custom/{ids[0]}/chain", {"apply": [{"id": prop["id"], "points": prop["points"]}]})
        placed = next(m for m in listing["maps"] if m["id"] == ids[1])
        assert len(placed["points"]) == len(prop["points"]) and placed["warp_mode"] == "homography"
        # The renamed anchor kept its strip identity.
        anchor = next(m for m in listing["maps"] if m["id"] == ids[0])
        assert anchor["title"] == "f1" and "SHDAI__AI_75_E_1_HV999__0001" in anchor["original_title"]
    finally:
        srv.quit()
        archive.close()
