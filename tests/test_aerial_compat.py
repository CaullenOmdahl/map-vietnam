"""French aerial (SHDAI) compatibility: sheet-grid vicinity, built-in places, map-vs-photo matching, spread
penalty, and the Hub-unreachable fallback for archive searches. No network."""

import io
import json
import threading
import urllib.request

import numpy as np
import pytest
import server
import store
import terrain_aerial
from fakeweb import FakeArchive, make_library
from hub import auto_gcp
from PIL import Image

CATALOGUE = server.paths.APP_ROOT / "data" / "sources" / "map_library.json"


# ---- SGI 1:100,000 sheet grid ------------------------------------------------------------------
def test_sheet_grid_predicts_placed_sheets_it_has_not_seen():
    """Leave each placed sheet out and step to it from its neighbours: most land within ~4 km, on the right row."""
    _, _, grid = terrain_aerial._sgi_vicinity_index()
    assert 0.70 < terrain_aerial._sgi_width(grid["width"], 0) < 0.74  # north: 0.8 grad comes out ~0.727 deg
    assert 0.66 < terrain_aerial._sgi_width(grid["width"], 30) < 0.70  # south: narrower in longitude
    errors, wrong_row = [], 0
    for num, (row, west) in grid["cols"].items():
        found = terrain_aerial._sgi_infer(num, grid, exclude=num)
        if found is None:
            continue
        wrong_row += found[0] != row
        errors.append(abs(found[1] - west))
    assert len(errors) > 90 and wrong_row <= 0.05 * len(errors)
    errors.sort()
    # Southern sheets in the catalogue are placed less regularly (numbering jumps across the Cambodian border).
    assert errors[len(errors) // 2] < 0.01 and errors[int(0.75 * len(errors))] < 0.04


def test_missing_sheet_80_is_placed_from_the_grid():
    # Sheet 80 (south-west of Thanh Hoa) is not in the catalogue; its row holds sheets 76..82.
    bounds, inferred = terrain_aerial.sgi_sheet_bounds(80)
    assert inferred
    w, s, e, n = bounds
    assert w == pytest.approx(105.115, abs=0.03) and n == pytest.approx(19.82, abs=0.03)
    assert 0.68 < e - w < 0.75 and n - s == pytest.approx(terrain_aerial.SGI_ROW_H)
    # Beyond the last known row nothing is invented.
    assert terrain_aerial.sgi_sheet_bounds(999) == (None, False)


def test_flight_card_label_and_lettered_sheets_resolve_to_the_right_place():
    v = terrain_aerial.resolve_shdai_vicinity("HV714 flight index", "Feuille N° 80 EW · 1:100.000 · méridien de Paris")
    assert v["source"] == "sgi100k_sheet" and "80" in v["label"]
    assert 105.0 < v["bounds"][0] < 105.2 and 19.3 < v["bounds"][1] < 19.4
    # "feuille 9A" (Hanoi) is not sheet 9 (Cao Bang): sheet 9 stays up by the Chinese border.
    sheets_by_num, _, _ = terrain_aerial._sgi_vicinity_index()
    assert sheets_by_num[9]["bounds"][1] > 22.4


def test_built_in_places_need_no_downloads():
    names = [p["title"] for p in terrain_aerial.builtin_places("tho xuan")]
    assert names[0] == "Thọ Xuân"
    assert any("Bái-Thương" in p["title"] for p in terrain_aerial.builtin_places("bai thuong"))
    assert terrain_aerial.builtin_places("zz") == []


# ---- matcher: spread penalty and maps vs photos --------------------------------------------------
def test_confidence_drops_when_matches_bunch_in_one_corner():
    spread = auto_gcp._confidence(20, 30, 4, 1.0, 6.5, hull_frac=0.5)
    bunched = auto_gcp._confidence(20, 30, 2, 1.0, 6.5, hull_frac=0.03)
    assert spread >= 0.85 and bunched <= 0.5
    assert auto_gcp._hull_fraction([(0, 0), (100, 0), (100, 100), (0, 100)], 100, 100) == pytest.approx(1.0)
    assert auto_gcp._hull_fraction([(10, 10), (12, 10), (11, 12)], 100, 100) < 0.001


def _jpeg(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG")
    return buf.getvalue()


def test_a_match_that_bends_the_frame_out_of_shape_is_rejected():
    shape = auto_gcp._warp_shape_problem
    # A 4:3 frame turned 30 degrees and scaled: still a frame.
    c, s = np.cos(np.radians(30)), np.sin(np.radians(30))
    turned = [(x * c - y * s + 500, x * s + y * c + 500) for x, y in [(0, 0), (400, 0), (400, 300), (0, 300)]]
    assert shape(turned, 800, 600, False) is None
    # Mild tilt is fine for a photo, too much for a printed map.
    tilted = [(0, 0), (400, 0), (440, 300), (-40, 300)]
    assert shape(tilted, 800, 600, False) is None and shape(tilted, 800, 600, True) == "skewed"
    # The reported case: four chance look-alikes smearing one frame into a long thin wedge across the map.
    assert shape([(0, 0), (900, 300), (1000, 420), (60, 160)], 800, 600, False) is not None
    assert shape([(0, 0), (400, 300), (400, 0), (0, 300)], 800, 600, False) == "folded"
    assert shape([(0, 0), (800, 0), (800, 150), (0, 150)], 800, 600, False) == "stretched"


def test_maps_are_matched_upright_and_never_given_a_perspective_warp(monkeypatch):
    calls = []

    def fake_matcher(scan, ref):
        calls.append(scan.shape)
        return np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0)

    monkeypatch.setattr(auto_gcp, "_onnx_session", lambda: None)
    monkeypatch.setattr(auto_gcp, "_match_numpy", fake_matcher)
    img = _jpeg(np.full((200, 300, 3), 128, np.uint8))
    res = auto_gcp.match_and_score(img, img, [105.0, 19.0, 105.5, 19.5], kind="map")
    assert len(calls) == 1 and res["meets_threshold"] is False
    calls.clear()
    auto_gcp.match_and_score(img, img, [105.0, 19.0, 105.5, 19.5], kind="photo")
    assert len(calls) == len(auto_gcp.ROTATIONS)


# ---- server: aerial import remembers what the picture is; Hub outage falls back ------------------
@pytest.fixture
def desk(tmp_path, monkeypatch):
    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    db_path = tmp_path / "desk" / "e.sqlite"
    store.connect(db_path).close()
    srv = server.make_server(0, db_path, map_library=lib, allow_loopback=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, lib, archive
    srv.quit()
    archive.close()


def call(base, route, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"{base}{route}",
        data=data,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def test_imported_flight_card_is_a_map_with_its_mission_area(desk):
    base, lib, archive = desk
    archive.files["/plot.jpg"] = _jpeg(np.full((120, 160, 3), 200, np.uint8))
    res = call(
        base,
        "/api/aerial/import",
        {
            "url": f"{archive.base}/plot.jpg",
            "title": "HV714 FLIGHT INDEX",
            "kind": "map",
            "bounds": [105.115, 19.37, 105.84, 19.82],
        },
    )
    entry = lib.custom.get(res["map"]["id"])
    assert entry["subject"] == "map" and entry["bounds"] == [105.115, 19.37, 105.84, 19.82]
    assert res["map"]["subject"] == "map"


def test_aerial_search_falls_back_to_the_archive_when_the_hub_is_down(desk, monkeypatch):
    base, lib, _ = desk
    (lib.root / ".env").write_text(
        "HUB_URL=http://127.0.0.1:9\nHUB_API_KEY=mvhub_0123abcd_" + "0" * 32 + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        terrain_aerial,
        "search_nakala_aerials",
        lambda q, page, size, fetch_fn: {"total": 1, "page": 1, "missions": [{"title": q}]},
    )
    res = call(base, "/api/aerial/nakala?q=Thanh%20Hoa")
    assert res["missions"] == [{"title": "Thanh Hoa"}]
    logs = call(base, "/api/logs")
    assert any("Hub unreachable" in ln for ln in logs["lines"])


def test_landsat_mss_builds_geotiff_url_and_prefers_l1tp():
    payload = {
        "features": [
            {
                "id": "LM02_L1GS_136053_19751229_02_T2",
                "bbox": [102.1, 9.0, 104.2, 11.0],
                "properties": {"datetime": "1975-12-29T02:42:34Z", "eo:cloud_cover": 1.0, "platform": "landsat-2"},
                "assets": {
                    "rendered_preview": {
                        "href": "https://planetarycomputer.microsoft.com/api/data/v1/item/preview.png?collection=landsat-c2-l1&item=LM02_L1GS_136053_19751229_02_T2"
                    }
                },
            },
            {
                "id": "LM02_L1TP_136053_19751123_02_T2",
                "bbox": [102.2, 9.2, 104.3, 11.2],
                "properties": {"datetime": "1975-11-23T02:42:56Z", "eo:cloud_cover": 5.0, "platform": "landsat-2"},
                "assets": {
                    "rendered_preview": {
                        "href": "https://planetarycomputer.microsoft.com/api/data/v1/item/preview.png?collection=landsat-c2-l1&item=LM02_L1TP_136053_19751123_02_T2"
                    }
                },
            },
        ]
    }
    res = terrain_aerial.search_landsat_mss(
        [103.8, 9.9, 104.2, 10.5], fetch_fn=lambda *a, **k: (json.dumps(payload).encode(), "application/json")
    )
    assert [s["processing_level"] for s in res["scenes"]] == ["L1TP", "L1GS"]
    assert "/item/preview.tif?" in res["scenes"][0]["geotiff_url"]
    assert res["scenes"][0]["geotiff_url"].endswith("&max_size=2048")


def test_aerial_image_proxy_caches_write_once_and_rejects_svg(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, HTTPServer

    hits = {"img": 0}
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (100, 120, 140)).save(buf, format="JPEG")
    jpg = buf.getvalue()

    class FakeUpstream(BaseHTTPRequestHandler):
        def log_message(self, *a):
            return

        def do_GET(self):
            if self.path == "/thumb.jpg":
                hits["img"] += 1
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
            else:
                svg = b"<svg></svg>"
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(svg)))
                self.end_headers()
                self.wfile.write(svg)

    up = HTTPServer(("127.0.0.1", 0), FakeUpstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    up_base = f"http://127.0.0.1:{up.server_address[1]}"

    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    db = tmp_path / "desk" / "e.sqlite"
    store.connect(db).close()
    srv = server.make_server(0, db, map_library=lib, allow_loopback=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        q = urllib.parse.urlencode({"url": f"{up_base}/thumb.jpg"})
        with urllib.request.urlopen(f"{base}/api/aerial/image?{q}", timeout=10) as r:
            assert r.read() == jpg and r.headers["Content-Type"] == "image/jpeg"
        # Second request must be served from local disk cache without hitting upstream again.
        with urllib.request.urlopen(f"{base}/api/aerial/image?{q}", timeout=10) as r:
            assert r.read() == jpg
        assert hits["img"] == 1

        bad_q = urllib.parse.urlencode({"url": f"{up_base}/evil.svg"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"{base}/api/aerial/image?{bad_q}", timeout=10)
        assert exc.value.code == 400
    finally:
        srv.quit()
        up.shutdown()
        up.server_close()
        archive.close()
