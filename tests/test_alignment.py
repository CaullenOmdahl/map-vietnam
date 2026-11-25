"""Per-map alignment adjustment (backend Library overrides and /api/library/<id>/align/* endpoints).

No network; children run the real georef_scan / build_tiles scripts on tiny test maps from FakeArchive.
"""

import io
import json
import threading
import time
import urllib.error
import urllib.request

import custom_maps
import library
import pytest
import server
import store
import tiling
from fakeweb import FakeArchive, make_library, path_of
from files import path_bytes
from PIL import Image

CHILD_TIMEOUT_S = 120


@pytest.fixture
def setup_lib(tmp_path, monkeypatch):
    archive = FakeArchive()
    lib = make_library(tmp_path / "root", archive, monkeypatch)
    yield lib, archive
    archive.close()


@pytest.fixture
def web(tmp_path, monkeypatch):
    archive = FakeArchive()
    lib = make_library(tmp_path / "root", archive, monkeypatch)
    store.connect(tmp_path / "e.sqlite").close()
    (lib.root / "web").mkdir()
    srv = server.make_server(0, tmp_path / "e.sqlite", lib, web=lib.root / "web")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", lib
    srv.shutdown()
    archive.close()


def request(url, method="GET", data=None, ctype="application/json"):
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r:
            body, status, head = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        body, status, head = e.read(), e.code, e.headers
    if head.get_content_type() == "application/json":
        body = json.loads(body)
    return status, body, head


def wait_installed(base, map_id, *, adjusted=None, timeout=CHILD_TIMEOUT_S):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        _, listing, _ = request(f"{base}/api/library")
        last = next(m for m in listing["maps"] if m["id"] == map_id)
        if last["state"] == "failed":
            raise AssertionError(f"{map_id} build failed: {last['error']}")
        if last["state"] == "installed" and (adjusted is None or last["adjusted"] is adjusted):
            return last
        time.sleep(0.2)
    raise AssertionError(f"{map_id} did not settle (last={last})")


def test_prepare_alignment_rejects_uninstalled_missing_and_custom_scan(setup_lib):
    lib, _ = setup_lib
    with pytest.raises(library.LibraryError, match="not in the map list"):
        lib.prepare_alignment("no_such_map")
    with pytest.raises(library.LibraryError, match="Install this map"):
        lib.prepare_alignment("scan_test")

    # A custom plain scan has its own Place on map workflow.
    img_path = lib.root / "plain.jpg"
    Image.new("RGB", (120, 80), (100, 120, 140)).save(img_path)
    part = lib.custom.incoming_path()
    part.write_bytes(img_path.read_bytes())
    custom = lib.custom.ingest(part, "plain.jpg", "Plain", "1968")
    with pytest.raises(library.LibraryError, match="Place on map"):
        lib.prepare_alignment(custom["id"])

    # Installed catalogue scan with deleted raw image refuses preparation clearly.
    lib.add(["scan_test"])
    lib.run_pending()
    src, _ = lib.raw_paths(lib.entry("scan_test"))
    src.unlink()
    with pytest.raises(library.LibraryError, match="original map file is missing"):
        lib.prepare_alignment("scan_test")


def test_prepare_alignment_renders_scan_and_geopdf(setup_lib):
    lib, _ = setup_lib
    lib.add(["scan_test", "L7014A"])
    lib.run_pending()

    scan_info = lib.prepare_alignment("scan_test")
    assert scan_info["id"] == "scan_test"
    assert scan_info["image"] == {"width": 600, "height": 400}
    assert scan_info["points"] == []
    assert scan_info["adjusted"] is False
    assert scan_info["preview_url"] == "/api/library/scan_test/align/preview"
    assert lib.alignment_work_path("scan_test").exists()
    assert lib.alignment_preview_path("scan_test").exists()

    pdf_info = lib.prepare_alignment("L7014A")
    assert pdf_info["id"] == "L7014A"
    assert pdf_info["image"]["width"] > 0 and pdf_info["image"]["height"] > 0
    assert pdf_info["points"] == []
    assert pdf_info["adjusted"] is False
    assert pdf_info["preview_url"] == "/api/library/L7014A/align/preview"
    assert lib.alignment_work_path("L7014A").exists()
    assert lib.alignment_preview_path("L7014A").exists()


def test_save_alignment_validates_writes_control_and_rebuilds_without_download(setup_lib):
    lib, archive = setup_lib
    lib.add(["scan_test"])
    lib.run_pending()
    fetches_before = archive.requests.count(path_of(lib, "scan_test"))
    assert fetches_before == 1

    lib.prepare_alignment("scan_test")
    with pytest.raises(custom_maps.CustomMapError, match="at least 3"):
        lib.save_alignment("scan_test", [[0, 0, 105.0, 10.4], [600, 0, 105.6, 10.4]])

    # 4 points (< 6): writes affine order=1 and shifts bounds east to ~105.2..105.8.
    pts4 = [[0, 0, 105.2, 10.4], [600, 0, 105.8, 10.4], [600, 400, 105.8, 10.0], [0, 400, 105.2, 10.0]]
    res = lib.save_alignment("scan_test", pts4)
    assert res["adjusted"] is True
    assert lib.has_alignment("scan_test") is True
    ctrl4 = json.loads(lib.alignment_control_path("scan_test").read_text(encoding="utf-8"))
    assert ctrl4["order"] == 1 and ctrl4["gcps"] == pts4
    assert ctrl4["meta"]["title"] == "A test scan" and ctrl4["meta"]["date"] == "1909"

    listed = next(m for m in lib.listing()["maps"] if m["id"] == "scan_test")
    assert listed["adjusted"] is True and listed["state"] == "queued"

    cmd = lib.build_command(lib.entry("scan_test"), lib.alignment_work_path("scan_test"), lib.tiles_path("scan_test"))
    assert any("georef_scan.py" in part for part in cmd)
    assert str(lib.alignment_control_path("scan_test")) in cmd

    lib.run_pending()
    assert archive.requests.count(path_of(lib, "scan_test")) == fetches_before
    assert lib.state_of("scan_test") == "installed"
    bounds = tiling.read_metadata(lib.tiles_path("scan_test"))["bounds"]
    assert float(bounds.split(",")[0]) == pytest.approx(105.2, abs=0.02)

    # 6 points: polynomial fit ("order" omitted) and prepare returns saved points.
    pts6 = pts4 + [[300, 0, 105.5, 10.4], [300, 400, 105.5, 10.0]]
    lib.save_alignment("scan_test", pts6)
    ctrl6 = json.loads(lib.alignment_control_path("scan_test").read_text(encoding="utf-8"))
    assert "order" not in ctrl6 and len(ctrl6["gcps"]) == 6
    lib.run_pending()
    again = lib.prepare_alignment("scan_test")
    assert again["adjusted"] is True and len(again["points"]) == 6


def test_reset_and_remove_clean_up_alignment_and_disk_usage(setup_lib, tmp_path):
    lib, _ = setup_lib
    lib.add(["scan_test"])
    lib.run_pending()
    orig_west = float(tiling.read_metadata(lib.tiles_path("scan_test"))["bounds"].split(",")[0])

    lib.prepare_alignment("scan_test")
    shifted = [[0, 0, 105.2, 10.4], [600, 0, 105.8, 10.4], [600, 400, 105.8, 10.0], [0, 400, 105.2, 10.0]]
    lib.save_alignment("scan_test", shifted)
    lib.run_pending()

    align_bytes = path_bytes(lib.alignment_dir("scan_test"))
    assert align_bytes > 0
    assert lib.disk_usage(tmp_path / "none.sqlite")["maps"] >= path_bytes(lib.tiles) + align_bytes
    assert lib.disk_bytes("scan_test") >= align_bytes

    res = lib.reset_alignment("scan_test")
    assert res == {"ok": True, "id": "scan_test", "adjusted": False}
    assert not lib.alignment_dir("scan_test").exists()
    lib.run_pending()
    restored_west = float(tiling.read_metadata(lib.tiles_path("scan_test"))["bounds"].split(",")[0])
    assert restored_west == pytest.approx(orig_west, abs=0.02)

    # Removing the map also removes any active alignment directory.
    lib.prepare_alignment("scan_test")
    lib.save_alignment("scan_test", shifted)
    lib.run_pending()
    assert lib.alignment_dir("scan_test").exists()
    lib.remove(["scan_test"])
    assert not lib.alignment_dir("scan_test").exists()
    assert not lib.tiles_path("scan_test").exists()


def test_alignment_http_endpoints(web):
    base, lib = web
    status, _, _ = request(f"{base}/api/library/add", "POST", {"ids": ["scan_test"]})
    assert status == 200
    wait_installed(base, "scan_test")

    status, _, _ = request(f"{base}/api/library/scan_test/align/preview")
    assert status == 404

    status, body, _ = request(f"{base}/api/library/unknown_map/align/prepare", "POST", {})
    assert status == 400 and "error" in body

    status, prep, _ = request(f"{base}/api/library/scan_test/align/prepare", "POST", {})
    assert status == 200
    assert prep["image"] == {"width": 600, "height": 400}
    assert prep["adjusted"] is False

    status, img_bytes, head = request(f"{base}/api/library/scan_test/align/preview")
    assert status == 200 and head.get_content_type() == "image/jpeg"
    assert Image.open(io.BytesIO(img_bytes)).size == (600, 400)

    status, body, _ = request(
        f"{base}/api/library/scan_test/align/points", "POST", {"points": [[0, 0, 105.2, 10.4], [600, 0, 105.8, 10.4]]}
    )
    assert status == 400 and "at least 3" in body["error"]

    pts = [[0, 0, 105.25, 10.4], [600, 0, 105.85, 10.4], [600, 400, 105.85, 10.0], [0, 400, 105.25, 10.0]]
    status, saved, _ = request(f"{base}/api/library/scan_test/align/points", "POST", {"points": pts})
    assert status == 200 and saved["ok"] is True and saved["adjusted"] is True
    wait_installed(base, "scan_test", adjusted=True)

    status, catalog_doc, _ = request(f"{base}/catalog.json")
    assert status == 200 and catalog_doc["layers"][0]["bounds"][0] == pytest.approx(105.25, abs=0.02)

    status, prep2, _ = request(f"{base}/api/library/scan_test/align/prepare", "POST", {})
    assert status == 200 and prep2["adjusted"] is True and prep2["points"] == pts

    status, reset_res, _ = request(f"{base}/api/library/scan_test/align/reset", "POST", {})
    assert status == 200 and reset_res == {"ok": True, "id": "scan_test", "adjusted": False}
    wait_installed(base, "scan_test", adjusted=False)
    assert not lib.alignment_dir("scan_test").exists()
