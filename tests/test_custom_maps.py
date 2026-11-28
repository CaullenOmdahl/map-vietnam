"""custom_maps and the user's own maps through the library and the server: what an upload is
(GeoTIFF, GeoPDF, plain picture, nonsense), placing a picture from control points, and a real
build of each kind. No network; children run the real build scripts on tiny files."""

import http.client
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import custom_maps
import numpy as np
import pytest
import rasterio
import server
import store
import tiling
from fakeweb import FakeArchive, make_library
from geopdf import write_geopdf
from PIL import Image
from rasterio.transform import from_origin

CHILD_TIMEOUT_S = 120


def write_geotiff(path, crs="EPSG:4326", transform=None, size=(48, 32), count=3, dtype="uint8"):
    """A small georeferenced picture of a gradient near Can Tho."""
    transform = transform or from_origin(105.70, 10.10, 0.001, 0.001)
    w, h = size
    data = np.tile(np.linspace(20, 240, w), (count, h, 1)).astype(dtype)
    with rasterio.open(
        path, "w", driver="GTiff", width=w, height=h, count=count, dtype=dtype, crs=crs, transform=transform
    ) as ds:
        ds.write(data)
    return path


def write_jpeg(path, size=(600, 400)):
    arr = np.zeros((size[1], size[0], 3), np.uint8)
    arr[..., 0] = np.linspace(0, 255, size[0])
    arr[::40] = 255
    Image.fromarray(arr).save(path)
    return path


def write_plain_tiff(path, size=(200, 100)):
    Image.fromarray(np.full((size[1], size[0], 3), 90, np.uint8)).save(path)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    archive = FakeArchive()
    yield make_library(tmp_path / "root", archive, monkeypatch)
    archive.close()


def ingest(lib, tmp_path, src, title="", year=""):
    """Add `src` as the upload of a custom map the way the server does (the file is moved)."""
    part = lib.custom.incoming_path()
    part.write_bytes(Path(src).read_bytes())
    return lib.custom.ingest(part, Path(src).name, title, year)


# ---- sniffing -------------------------------------------------------------------------------
def test_a_geotiff_is_recognised_with_its_position(tmp_path):
    kind, bounds = custom_maps.sniff(write_geotiff(tmp_path / "g.tif"))
    assert kind == "geotiff"
    assert bounds == pytest.approx([105.70, 10.068, 105.748, 10.10], abs=1e-6)


def test_a_geotiff_in_a_projected_system_is_recognised(tmp_path):
    path = write_geotiff(tmp_path / "utm.tif", crs="EPSG:32648", transform=from_origin(600_000, 1_150_000, 30, 30))
    kind, bounds = custom_maps.sniff(path)
    assert kind == "geotiff" and 104 < bounds[0] < 108 and 10 < bounds[1] < 11


def test_a_plain_tiff_jpeg_and_png_are_scans(tmp_path):
    write_jpeg(tmp_path / "a.jpg")
    Image.open(tmp_path / "a.jpg").save(tmp_path / "a.png")
    write_plain_tiff(tmp_path / "a.tif")
    for name in ("a.jpg", "a.png", "a.tif"):
        assert custom_maps.sniff(tmp_path / name) == ("scan", None)


def test_a_geopdf_is_recognised_and_a_plain_pdf_is_a_scan(tmp_path):
    assert custom_maps.sniff(write_geopdf(tmp_path / "g.pdf"))[0] == "geopdf"
    from pypdf import PdfWriter

    plain = PdfWriter()
    plain.add_blank_page(300, 200)
    with open(tmp_path / "plain.pdf", "wb") as f:
        plain.write(f)
    assert custom_maps.sniff(tmp_path / "plain.pdf") == ("scan", None)
    plain.add_blank_page(300, 200)
    with open(tmp_path / "two.pdf", "wb") as f:
        plain.write(f)
    with pytest.raises(custom_maps.CustomMapError, match="2 pages"):
        custom_maps.sniff(tmp_path / "two.pdf")


def test_anything_else_is_refused_in_plain_words(lib, tmp_path):
    (tmp_path / "notes.txt").write_text("not a map")
    with pytest.raises(custom_maps.CustomMapError, match="cannot use that file"):
        ingest(lib, tmp_path, tmp_path / "notes.txt")
    # A broken picture with a picture's first bytes, too; and nothing is left behind.
    (tmp_path / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"junk")
    with pytest.raises(custom_maps.CustomMapError):
        ingest(lib, tmp_path, tmp_path / "bad.png")
    assert lib.custom.entries() == [] and not list(lib.custom.root.glob("custom_*"))


# ---- ingesting and placing ----------------------------------------------------------------------
def test_a_scan_is_stored_with_a_working_image_and_preview(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_jpeg(tmp_path / "hue_1968-sheet.jpg", (5000, 3000)), year="1968")
    assert custom_maps.is_custom_id(entry["id"]) and entry["kind"] == "scan" and entry["custom"] is True
    assert (
        entry["title"] == "hue 1968 sheet"
        and entry["year"] == 1968
        and entry["image"] == {"width": 5000, "height": 3000}
    )
    folder = lib.custom.folder(entry["id"])
    assert Image.open(folder / "work.jpg").size == (5000, 3000)
    assert max(Image.open(folder / "preview.jpg").size) == custom_maps.PREVIEW_PX
    assert (folder / "source.jpg").exists() and lib.custom.entries() == [entry]
    listed = next(m for m in lib.listing()["maps"] if m["id"] == entry["id"])
    assert listed["state"] == "needs_points" and listed["series"] == "custom" and listed["points"] == []
    assert lib.listing()["series"]["custom"] == {"name": "Your own maps", "credit": "Added by you", "scale": ""}


def test_points_need_three_matching_pairs_that_fix_a_position(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_jpeg(tmp_path / "s.jpg"))
    image = entry["image"]
    with pytest.raises(custom_maps.CustomMapError, match="at least 3"):
        custom_maps.clean_points([[0, 0, 105, 10], [600, 0, 105.1, 10]], image)
    on_a_line = [[0, 0, 105, 10], [300, 200, 105.05, 10.05], [600, 400, 105.1, 10.1]]
    with pytest.raises(custom_maps.CustomMapError, match="one line"):
        custom_maps.clean_points(on_a_line, image)
    with pytest.raises(custom_maps.CustomMapError, match="outside the picture"):
        custom_maps.clean_points([[0, 0, 105, 10], [9000, 0, 105.1, 10], [0, 400, 105, 9.9]], image)
    with pytest.raises(custom_maps.CustomMapError):
        custom_maps.clean_points([[0, 0, 105, "x"]] * 3, image)


def test_placing_saves_a_control_file_and_queues_the_build(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_jpeg(tmp_path / "s.jpg"))
    points = [[0, 0, 105.0, 10.4], [600, 0, 105.06, 10.4], [600, 400, 105.06, 10.34], [0, 400, 105.0, 10.34]]
    lib.place_custom(entry["id"], points, "Sheet 7", "1954")
    control = json.loads(lib.custom.control_path(entry["id"]).read_text())
    assert control["crs"] == "EPSG:4326" and control["order"] == 1 and len(control["gcps"]) == 4
    assert control["image"] == f"data/custom_maps/{entry['id']}/work.jpg" and control["meta"]["title"] == "Sheet 7"
    saved = lib.custom.get(entry["id"])
    assert saved["title"] == "Sheet 7" and saved["year"] == 1954 and len(saved["points"]) == 4
    assert saved["bounds"] == pytest.approx([105.0, 10.34, 105.06, 10.4])
    assert lib.state_of(entry["id"]) == "queued"
    with pytest.raises(library_error(), match="being made"):
        lib.place_custom(entry["id"], points, "Sheet 7", "1954")


def library_error():
    import library

    return library.LibraryError


# ---- real builds -------------------------------------------------------------------------------
def build_all(lib):
    lib.run_pending()


def metadata(lib, map_id):
    return tiling.read_metadata(lib.tiles_path(map_id))


def tile_count(lib, map_id):
    return _query(lib, map_id, "select count(*) from tiles")[0][0]


def _query(lib, map_id, sql):
    from contextlib import closing

    with closing(tiling.connect_read_only(lib.tiles_path(map_id))) as db:
        return db.execute(sql).fetchall()


def test_a_geotiff_is_built_at_once_and_listed_as_the_users_own_map(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_geotiff(tmp_path / "ct.tif"), title="Can Tho aerial", year="1972")
    assert entry["kind"] == "geotiff" and entry["bounds"]
    assert lib.add([entry["id"]]) == [entry["id"]]
    seen = []
    lib.run_pending(lambda job: seen.append(job.state))
    assert seen[0] == "building" and seen[-1] == "installed"  # nothing to download
    assert tile_count(lib, entry["id"]) > 0 and lib.state_of(entry["id"]) == "installed"
    (layer,) = json.loads((lib.root / "web" / "catalog.json").read_text())["layers"]
    assert layer["id"] == entry["id"] and layer["title"] == "Can Tho aerial" and layer["date"] == "1972"
    assert (
        layer["attribution"] == "Your own map" and layer["series"] == "Your own maps" and layer["seriesId"] == "custom"
    )
    listed = next(m for m in lib.listing()["maps"] if m["id"] == entry["id"])
    assert listed["state"] == "installed" and listed["outline"] == pytest.approx(listed["bounds"], abs=1e-3)
    assert lib.listing()["queue_bytes"] == 0


def test_a_utm_geotiff_is_reprojected(lib, tmp_path):
    src = write_geotiff(tmp_path / "utm.tif", crs="EPSG:32648", transform=from_origin(600_000, 1_150_000, 100, 100))
    entry = ingest(lib, tmp_path, src)
    lib.add([entry["id"]])
    lib.run_pending()
    bounds = [float(v) for v in metadata(lib, entry["id"])["bounds"].split(",")]
    assert 104 < bounds[0] < bounds[2] < 108 and 10 < bounds[1] < bounds[3] < 11
    assert lib.custom.get(entry["id"])["bounds"] == pytest.approx(bounds)


def test_a_single_band_geotiff_with_no_data_is_transparent_there(lib, tmp_path):
    path = tmp_path / "one.tif"
    data = np.full((1, 32, 48), 500, np.uint16)
    data[0, :, :24] = 0
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=48,
        height=32,
        count=1,
        dtype="uint16",
        crs="EPSG:4326",
        transform=from_origin(105.70, 10.10, 0.001, 0.001),
        nodata=0,
    ) as ds:
        ds.write(data)
    entry = ingest(lib, tmp_path, path)
    lib.add([entry["id"]])
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed"


def test_a_geopdf_is_built_at_once(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_geopdf(tmp_path / "sheet.pdf"), title="My sheet", year="1966")
    assert entry["kind"] == "geopdf"
    lib.add([entry["id"]])
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed"
    (layer,) = json.loads((lib.root / "web" / "catalog.json").read_text())["layers"]
    assert layer["title"] == "My sheet" and layer["date"] == "1966" and layer["attribution"] == "Your own map"
    assert lib.custom.get(entry["id"])["bounds"]


def test_a_placed_scan_is_built_and_can_be_placed_again(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_jpeg(tmp_path / "s.jpg"))
    # The scan cannot be built, or added again, before it is placed.
    assert lib.add([entry["id"]]) == [] and lib.state_of(entry["id"]) == "needs_points"
    points = [[0, 0, 105.0, 10.4], [600, 0, 105.06, 10.4], [600, 400, 105.06, 10.34], [0, 400, 105.0, 10.34]]
    lib.place_custom(entry["id"], points, "Sheet 7", "1954")
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed" and tile_count(lib, entry["id"]) > 0
    assert metadata(lib, entry["id"])["bounds"].startswith("105.0")
    first = lib.tiles_path(entry["id"]).stat().st_mtime_ns
    # Placing it again (shifted a little east) rebuilds the installed map.
    moved = [[c, r, lon + 0.02, lat] for c, r, lon, lat in points]
    lib.place_custom(entry["id"], moved, "Sheet 7", "1954")
    assert lib.state_of(entry["id"]) == "queued"
    lib.run_pending()
    assert lib.tiles_path(entry["id"]).stat().st_mtime_ns > first
    assert metadata(lib, entry["id"])["bounds"].startswith("105.02")


def test_a_failed_build_keeps_the_upload_and_removal_deletes_everything(lib, tmp_path, monkeypatch):
    entry = ingest(lib, tmp_path, write_geotiff(tmp_path / "ct.tif"))
    # Damage the stored file after the sniff: the build fails but the user's file is not thrown away.
    stored = lib.custom.build_input(entry)
    stored.write_bytes(stored.read_bytes()[:100])
    lib.add([entry["id"]])
    lib.run_pending()
    listed = next(m for m in lib.listing()["maps"] if m["id"] == entry["id"])
    assert listed["state"] == "failed" and listed["error"] and stored.exists()
    lib.remove([entry["id"]])
    assert not lib.custom.folder(entry["id"]).exists() and lib.custom.entries() == []
    assert all(m["id"] != entry["id"] for m in lib.listing()["maps"])


def test_removing_a_built_map_deletes_tiles_and_files(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_geotiff(tmp_path / "ct.tif"))
    lib.add([entry["id"]])
    lib.run_pending()
    assert lib.remove([entry["id"]]) == [entry["id"]]
    assert not lib.tiles_path(entry["id"]).exists() and not lib.custom.folder(entry["id"]).exists()
    assert json.loads((lib.root / "web" / "catalog.json").read_text())["layers"] == []


def test_editing_changes_the_title_and_year_in_the_catalog(lib, tmp_path):
    entry = ingest(lib, tmp_path, write_geotiff(tmp_path / "ct.tif"), title="Old name")
    lib.add([entry["id"]])
    lib.run_pending()
    lib.custom.edit(entry["id"], "New name", "1969")
    lib.write_catalog()
    (layer,) = json.loads((lib.root / "web" / "catalog.json").read_text())["layers"]
    assert layer["title"] == "New name" and layer["date"] == "1969"
    with pytest.raises(custom_maps.CustomMapError, match="year"):
        lib.custom.edit(entry["id"], "x", "19x")


def test_an_interrupted_upload_is_cleaned_up_when_the_server_starts(lib):
    part = lib.custom.incoming_path()
    part.write_bytes(b"half")
    lib.recover_interrupted()
    assert not part.exists()


def test_the_users_maps_are_counted_in_disk_usage(lib, tmp_path):
    ingest(lib, tmp_path, write_geotiff(tmp_path / "ct.tif"))
    usage = lib.disk_usage(tmp_path / "none.sqlite")
    assert usage["maps"] > 0 and usage["total"] == usage["maps"] + usage["records"] + usage["downloads"]


# ---- over HTTP ------------------------------------------------------------------------------------
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


def request(url, method="GET", data=None, ctype="application/json", headers=None):
    """(status, parsed JSON or raw bytes, headers) of a request."""
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": ctype, **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            body, status, head = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        body, status, head = e.read(), e.code, e.headers
    if head.get_content_type() == "application/json":
        body = json.loads(body)
    return status, body, head


def upload(base, path, **q):
    from urllib.parse import urlencode

    return request(
        f"{base}/api/library/custom?{urlencode({'filename': Path(path).name, **q})}",
        "POST",
        Path(path).read_bytes(),
        "application/octet-stream",
    )


def wait_state(base, map_id, states, timeout=CHILD_TIMEOUT_S):
    import time

    end = time.time() + timeout
    while time.time() < end:
        _, listing, _ = request(f"{base}/api/library")
        state = next(m["state"] for m in listing["maps"] if m["id"] == map_id)
        if state in states:
            return state
        time.sleep(0.2)
    raise AssertionError(f"{map_id} did not reach {states}, still {state}")


def test_uploading_a_geotiff_builds_it_and_the_tiles_are_served(web, tmp_path):
    base, lib = web
    status, body, _ = upload(base, write_geotiff(tmp_path / "ct.tif"), title="Can Tho aerial", year="1972")
    assert (
        status == 200
        and body["map"]["kind"] == "geotiff"
        and body["map"]["state"] in ("queued", "building", "installed")
    )
    map_id = body["map"]["id"]
    assert any(m["id"] == map_id for m in body["library"]["maps"])
    assert wait_state(base, map_id, {"installed", "failed"}) == "installed"
    z, x, tms_y = _query(lib, map_id, "select zoom_level, tile_column, tile_row from tiles limit 1")[0]
    status, data, head = request(f"{base}/tiles/{map_id}/{z}/{x}/{(1 << z) - 1 - tms_y}.webp")
    assert status == 200 and head.get_content_type() == "image/webp" and len(data) > 100


def test_a_picture_is_placed_over_the_api_and_built(web, tmp_path):
    base, lib = web
    status, body, _ = upload(base, write_jpeg(tmp_path / "scan.jpg"), title="", year="")
    entry = body["map"]
    assert status == 200 and entry["state"] == "needs_points" and entry["kind"] == "scan" and entry["title"] == "scan"
    assert entry["image"] == {"width": 600, "height": 400} and entry["points"] == []
    status, preview, head = request(f"{base}/api/library/custom/{entry['id']}/preview")
    assert (
        status == 200
        and head.get_content_type() == "image/jpeg"
        and Image.open(__import__("io").BytesIO(preview)).size == (600, 400)
    )

    url = f"{base}/api/library/custom/{entry['id']}"
    status, body, _ = request(
        f"{url}/points", "POST", {"points": [[0, 0, 105, 10.4], [600, 0, 105.06, 10.4]], "title": "x", "year": None}
    )
    assert status == 400 and "at least 3" in body["error"]
    points = [[0, 0, 105.0, 10.4], [600, 0, 105.06, 10.4], [600, 400, 105.06, 10.34], [0, 400, 105.0, 10.34]]
    status, listing, _ = request(f"{url}/points", "POST", {"points": points, "title": "Hue sheet", "year": 1954})
    assert status == 200
    placed = next(m for m in listing["maps"] if m["id"] == entry["id"])
    assert placed["title"] == "Hue sheet" and placed["year"] == 1954 and placed["points"] == points
    assert wait_state(base, entry["id"], {"installed", "failed"}) == "installed"
    status, listing, _ = request(f"{url}/edit", "POST", {"title": "Hue sheet 2", "year": None})
    assert next(m for m in listing["maps"] if m["id"] == entry["id"])["title"] == "Hue sheet 2"
    status, listing, _ = request(f"{base}/api/library/remove", "POST", {"ids": [entry["id"]]})
    assert status == 200 and all(m["id"] != entry["id"] for m in listing["maps"])
    assert not lib.custom.folder(entry["id"]).exists()


def test_bad_uploads_are_answered_in_plain_words(web, tmp_path):
    base, _ = web
    (tmp_path / "notes.txt").write_text("hello")
    status, body, _ = upload(base, tmp_path / "notes.txt")
    assert status == 400 and "cannot use that file" in body["error"]
    status, body, _ = upload(base, write_geotiff(tmp_path / "g.tif"), year="soon")
    assert status == 400 and "year" in body["error"]
    # No length: refused. Too big: refused before any byte is read.
    req = urllib.request.Request(
        f"{base}/api/library/custom?filename=a.tif",
        method="POST",
        data=b"",
        headers={"Content-Type": "application/octet-stream"},
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
    conn = http.client.HTTPConnection("127.0.0.1", int(base.rsplit(":", 1)[1]))
    conn.putrequest("POST", "/api/library/custom?filename=a.tif")
    conn.putheader("Content-Type", "application/octet-stream")
    conn.putheader("Content-Length", str(custom_maps.MAX_UPLOAD_BYTES + 1))
    conn.endheaders()
    answer = conn.getresponse()
    assert answer.status == 413 and "over 2 GB" in json.loads(answer.read())["error"]


def test_unknown_custom_maps_are_404_and_other_posts_must_still_be_json(web, tmp_path):
    base, _ = web
    status, body, _ = request(f"{base}/api/library/custom/custom_0123456789/preview")
    assert status == 404 and "error" in body
    status, _, _ = request(f"{base}/api/library/custom/custom_0123456789/points", "POST", {"points": []})
    assert status == 404
    status, _, _ = request(f"{base}/api/library/add", "POST", b"{}", "application/octet-stream")
    assert status == 415
    # An upload from another site's page is refused like any other change.
    status, _, _ = request(
        f"{base}/api/library/custom?filename=a.tif",
        "POST",
        b"x",
        "application/octet-stream",
        {"Origin": "http://evil.example"},
    )
    assert status == 403


def test_settings_say_where_the_data_is_and_how_much_space_it_takes(web, tmp_path):
    base, lib = web
    status, body, _ = request(f"{base}/api/settings")
    assert status == 200 and body["data_folder"] == str(lib.root)
    assert set(body["disk"]) == {"maps", "records", "downloads", "base_cache", "total"}
    upload(base, write_geotiff(tmp_path / "g.tif"))
    assert request(f"{base}/api/settings")[1]["disk"]["maps"] > 0


def test_a_float_lidar_dem_geotiff_is_tiled_as_lossless_terrarium_dem(lib, tmp_path):
    path = tmp_path / "lidar_dtm.tif"
    elev = np.full((1, 32, 48), 14.75, np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=48,
        height=32,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(105.70, 10.10, 0.001, 0.001),
        nodata=-9999.0,
    ) as ds:
        ds.write(elev)
    entry = ingest(lib, tmp_path, path, title="Cao Lanh LiDAR DTM")
    assert entry["kind"] == "dem"
    lib.add([entry["id"]])
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed"
    (layer,) = json.loads((lib.root / "web" / "catalog.json").read_text(encoding="utf-8"))["layers"]
    assert layer["encoding"] == "terrarium" and layer["title"] == "Cao Lanh LiDAR DTM"
    assert tiling.sample_dem_mbtiles(lib.tiles_path(entry["id"]), 105.72, 10.085) == pytest.approx(14.75, abs=0.05)


def test_scan_with_eight_plus_gcps_builds_with_thin_plate_spline(lib, tmp_path):
    """8+ control points automatically enable Thin Plate Spline (`tps=True`) and build cleanly."""
    from PIL import Image

    img_path = tmp_path / "shdai_aerial.jpg"
    Image.new("RGB", (128, 128), (155, 140, 120)).save(img_path, format="JPEG")
    entry = ingest(lib, tmp_path, img_path, title="SHDAI 8-GCP Aerial", year="1953")
    assert entry["kind"] == "scan"
    gcps = [
        [0, 0, 105.50, 10.50],
        [127, 0, 105.60, 10.50],
        [127, 127, 105.60, 10.40],
        [0, 127, 105.50, 10.40],
        [64, 64, 105.551, 10.449],
        [32, 96, 105.525, 10.425],
        [96, 32, 105.575, 10.475],
        [32, 32, 105.525, 10.475],
    ]
    lib.place_custom(entry["id"], gcps, "SHDAI 8-GCP Aerial", 1953)
    ctrl = json.loads(lib.custom.control_path(entry["id"]).read_text(encoding="utf-8"))
    assert ctrl.get("tps") is True
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed"
    assert lib.tiles_path(entry["id"]).is_file()


def test_oblique_homography_warp_persists_warp_mode_and_builds(lib, tmp_path):
    """4 keystone GCPs with `warp_mode='homography'` warp oblique aerials projectively and tile."""
    from PIL import Image

    arr = np.zeros((120, 160, 3), np.uint8)
    arr[..., 0] = 180
    arr[20:100, 30:130, 1] = 210
    img_path = tmp_path / "oblique_1969.jpg"
    Image.fromarray(arr).save(img_path, format="JPEG")
    entry = ingest(lib, tmp_path, img_path, title="1969 Oblique Aerial", year="1969")
    # Trapezoid keystone mimicking a camera tilted northward
    gcps = [
        [0, 0, 105.51, 10.50],
        [160, 0, 105.59, 10.50],
        [160, 120, 105.57, 10.42],
        [0, 120, 105.53, 10.42],
    ]
    lib.place_custom(entry["id"], gcps, "1969 Oblique Aerial", 1969, warp_mode="homography")
    ctrl = json.loads(lib.custom.control_path(entry["id"]).read_text(encoding="utf-8"))
    assert ctrl["warp_mode"] == "homography"
    lib.run_pending()
    assert lib.state_of(entry["id"]) == "installed"
    assert lib.tiles_path(entry["id"]).is_file()
