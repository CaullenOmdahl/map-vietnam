"""Tests for the Railway Intermediary Hub Server, infinite write-once cache, polite single-flight
coalescing, bidirectional Client <-> Hub map/DEM sync, and terrain/aerial providers."""

import json
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
import rasterio
import terrain_aerial
import tiling
from fakeweb import FakeArchive, make_library
from hub.auth import AuthError, authenticate, ensure_bootstrap_admin, init_auth_tables, mint_key, revoke_key
from hub.hub_server import make_hub_server
from hub.polite import HostPacer
from hub.storage import HubStorage
from hub_client import HubClient
from rasterio.crs import CRS
from rasterio.transform import from_bounds, from_origin


@pytest.fixture
def hub_instance(tmp_path):
    """Run a live HubHTTPServer in a background thread backed by `tmp_path / 'volume'`."""
    env = {
        "HUB_BOOTSTRAP_ADMIN_KEY": "bootstrap_super_secret_admin_12345",
        "USGS_USERNAME": "eros_user",
        "USGS_M2M_TOKEN": "top_secret_usgs_m2m_token_xyz",
        "MAPVIETNAM_CONTACT": "research@example.org",
    }
    srv = make_hub_server(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path / "volume",
        env=env,
        pacer=HostPacer(default_interval=0.0, intervals={}),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base_url, srv
    srv.stop_gracefully()
    thread.join(timeout=5)
    srv.server_close()


def make_test_mbtiles(path, *, bounds=(105.5, 10.3, 105.7, 10.5), dem=False):
    if dem:
        elev = np.linspace(1.5, 420.0, 64 * 64, dtype=np.float32).reshape(64, 64)
        tiling.write_dem_tiles(
            elev,
            CRS.from_epsg(4326),
            path,
            transform=from_bounds(*bounds, 64, 64),
            max_zoom=9,
            name="Test Bare Earth DEM",
        )
    else:
        bands = np.full((4, 64, 64), 180, dtype=np.uint8)
        bands[3] = 255
        tiling.write_tiles(
            bands,
            CRS.from_epsg(4326),
            path,
            transform=from_bounds(*bounds, 64, 64),
            max_zoom=9,
            name="Cao Lanh 1965",
            meta={"date": "1965"},
        )
    return path


def test_scoped_keys_scrypt_verification_revocation_and_secret_scrubbing(tmp_path):
    storage = HubStorage(
        root=tmp_path / "vol",
        env={
            "HUB_BOOTSTRAP_ADMIN_KEY": "my_bootstrap_admin_secret_token",
            "USGS_M2M_TOKEN": "secret_m2m_999",
            "PROPRIETARY_SOURCES_JSON": json.dumps(
                [{"id": "maxar", "tiles": "https://tiles.example/v1/{z}/{x}/{y}.webp?key=vendor_secret_abc"}]
            ),
        },
    )
    with storage.connect() as db:
        init_auth_tables(db)
        ensure_bootstrap_admin(db, env=storage.env)
        admin = authenticate(db, "Bearer my_bootstrap_admin_secret_token")
        assert admin.has_scope("admin") and admin.has_scope("upload")

        reader = mint_key(db, "Read only researcher", scopes=["read"], group_tag="mekong")
        p_read = authenticate(db, f"Bearer {reader['token']}")
        assert p_read.has_scope("read") and not p_read.has_scope("upload")
        with pytest.raises(AuthError) as exc:
            p_read.require("upload")
        assert exc.value.status == 403

        # Plaintext token is never stored in SQLite.
        raw_rows = str(db.execute("SELECT key_id, label, scopes FROM api_keys").fetchall())
        assert reader["token"] not in raw_rows

        assert revoke_key(db, reader["key_id"]) is True
        with pytest.raises(AuthError):
            authenticate(db, f"Bearer {reader['token']}")

    scrubbed = storage.vault.scrub(
        f"Failed calling url?key=vendor_secret_abc with USGS=secret_m2m_999 and {reader['token']} and my_bootstrap_admin_secret_token"
    )
    assert "vendor_secret_abc" not in scrubbed
    assert "secret_m2m_999" not in scrubbed
    assert "my_bootstrap_admin_secret_token" not in scrubbed
    assert reader["token"] not in scrubbed


def test_single_flight_deduplicates_concurrent_requests_and_cache_is_infinite(tmp_path):
    hits = {"count": 0}
    barrier = threading.Barrier(6)

    class UpstreamHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            hits["count"] += 1
            time.sleep(0.08)
            body = json.dumps({"datas": [], "totalResults": 42}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{upstream.server_address[1]}/archive.json"

    storage = HubStorage(root=tmp_path / "vol", pacer=HostPacer(default_interval=0.0), allow_loopback=True)
    results = []

    def worker():
        barrier.wait()
        body, _ = storage.cached_fetch(url)
        results.append(json.loads(body.decode("utf-8"))["totalResults"])

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results == [42] * 6
    assert hits["count"] == 1  # 6 concurrent callers -> 1 polite upstream hit!

    # Shut down the upstream archive completely: the infinite write-once cache serves forever.
    upstream.shutdown()
    upstream.server_close()
    cached_body, _ = storage.cached_fetch(url)
    assert json.loads(cached_body.decode("utf-8"))["totalResults"] == 42
    assert hits["count"] == 1


def test_host_pacer_spaces_consecutive_requests():
    clock_now = [100.0]
    slept = []

    def fake_clock():
        return clock_now[0]

    def fake_sleep(dt):
        slept.append(round(dt, 3))
        clock_now[0] += dt

    pacer = HostPacer(intervals={"api.nakala.fr": 1.2}, clock=fake_clock, sleep=fake_sleep)
    pacer.wait("https://api.nakala.fr/datas/1")
    pacer.wait("https://api.nakala.fr/datas/2")
    assert slept == [1.2]


def test_bidirectional_client_push_visibility_immutable_stream_and_offline_pull(hub_instance, tmp_path, monkeypatch):
    hub_url, srv = hub_instance
    archive = FakeArchive()
    lib_a = make_library(tmp_path / "client_a", archive, monkeypatch)
    lib_b = make_library(tmp_path / "client_b", archive, monkeypatch)

    # Admin mints two scoped keys: Author A (team1) and Reader B (public, no group).
    admin_client = HubClient(root=tmp_path / "admin")
    me_admin = admin_client.save_config(hub_url, "bootstrap_super_secret_admin_12345")
    assert me_admin["capabilities"]["usgs_m2m"] is True
    assert stat.S_IMODE((tmp_path / "admin" / ".env").stat().st_mode) == 0o600

    key_a = admin_client.proxy_post(
        "/hub/v1/admin/keys", {"label": "Author A", "scopes": ["read", "request_build", "upload"], "group_tag": "team1"}
    )
    key_b = admin_client.proxy_post(
        "/hub/v1/admin/keys", {"label": "Reader B", "scopes": ["read"], "group_tag": "other"}
    )

    client_a = HubClient(root=lib_a.root)
    client_a.save_config(hub_url, key_a["token"])
    client_b = HubClient(root=lib_b.root)
    client_b.save_config(hub_url, key_b["token"])

    # Author A imports a custom GeoTIFF map AND a custom LiDAR float32 DEM locally, builds them,
    # and caches both back to the Hub (one hub-wide, one private).
    tif_path = tmp_path / "local_map.tif"
    with rasterio.open(
        tif_path,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(105.60, 10.50, 0.002, 0.002),
    ) as ds:
        ds.write(np.full((3, 32, 32), 150, np.uint8))
    part = lib_a.custom.incoming_path()
    part.write_bytes(tif_path.read_bytes())
    entry_pub = lib_a.custom.ingest(part, "cao_lanh_1968.tif", "Cao Lanh Recon", "1968")
    lib_a.add([entry_pub["id"]])
    lib_a.run_pending()

    dem_tif = tmp_path / "local_lidar.tif"
    with rasterio.open(
        dem_tif,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(105.60, 10.50, 0.002, 0.002),
    ) as ds:
        ds.write(np.full((1, 32, 32), 3.25, np.float32))
    part2 = lib_a.custom.incoming_path()
    part2.write_bytes(dem_tif.read_bytes())
    entry_priv = lib_a.custom.ingest(part2, "private_lidar.tif", "Private LiDAR", "2024")
    lib_a.add([entry_priv["id"]])
    lib_a.run_pending()

    pushed_pub = client_a.push_local_layer(entry_pub["id"], lib_a, visibility="hub")
    pushed_priv = client_a.push_local_layer(entry_priv["id"], lib_a, visibility="private")
    assert pushed_pub["layer"]["visibility"] == "hub"
    assert pushed_priv["layer"]["encoding"] == "terrarium"

    # Author A still sees both; Reader B sees only the hub-wide layer, never A's private LiDAR!
    cat_a = client_a.status_and_catalog()
    cat_b = client_b.status_and_catalog()
    assert {x["id"] for x in cat_a["layers"]} == {entry_pub["id"], entry_priv["id"]}
    assert [x["id"] for x in cat_b["layers"]] == [entry_pub["id"]]

    # Reader B streams a tile from Hub -> stored permanently in B's append-only local hub_cache!
    tx, ty, _, _ = tiling.lonlat_to_tile_pixel(105.62, 10.48, 9)
    tile_bytes, ctype = client_b.get_streamed_tile(entry_pub["id"], 9, tx, ty)
    assert tile_bytes and "webp" in ctype

    # Reader B pins the entire map pack offline into lib_b's web/tiles/ in one shot!
    pulled = client_b.pull_pack_offline(entry_pub["id"])
    assert pulled.is_file() and tiling.read_metadata(pulled)["name"] == "Cao Lanh Recon"

    # Even if Hub goes offline AND a fresh HubClient restarts cold, the append-only streamed tile cache still serves!
    srv.stop_gracefully()
    time.sleep(0.1)
    cold_client_b = HubClient(root=lib_b.root)
    offline_hit, _ = cold_client_b.get_streamed_tile(entry_pub["id"], 9, tx, ty)
    assert offline_hit == tile_bytes
    archive.close()


def test_elevation_rg373_and_line_of_sight_intervisibility(tmp_path):
    rg = terrain_aerial.nara_rg373_info(105.63, 10.46)
    assert rg["degree_square"] == "10N105E" and "10N105E" in rg["catalog_url"]

    # Build a synthetic ridge DEM: flat 10 m valley at west & east, 500 m mountain ridge in the middle.
    bounds = (106.0, 16.0, 106.2, 16.2)
    grid = np.full((64, 64), 10.0, dtype=np.float32)
    grid[:, 28:36] = 500.0
    dem_mbt = tmp_path / "ridge.mbtiles"
    tiling.write_dem_tiles(
        grid, CRS.from_epsg(4326), dem_mbt, transform=from_bounds(*bounds, 64, 64), max_zoom=10, name="Ridge DEM"
    )

    pt = terrain_aerial.sample_point_terrain(106.02, 16.1, dem_paths=[("Ridge DEM", dem_mbt)])
    assert pt["ground_m"] == pytest.approx(10.0, abs=0.5)
    assert pt["hon_dau_m"] == pytest.approx(10.0 - 0.18, abs=0.5)

    prof = terrain_aerial.compute_transect(
        [106.02, 16.1], [106.18, 16.1], samples=32, observer_height_m=2.0, dem_path=dem_mbt
    )
    assert prof["max_ground_m"] > 450.0 and prof["min_ground_m"] < 20.0
    # Start is visible, ridge crest is visible, valley floor behind the 500 m ridge is NOT visible!
    assert prof["points"][0]["visible"] is True
    assert any(p["ground_m"] > 400 and p["visible"] is True for p in prof["points"])
    assert prof["points"][-1]["visible"] is False


def test_nakala_oam_and_allmaps_parsers_extract_plots_and_gcps():
    def fake_fetch(url, **kwargs):
        if "nakala.fr" in url:
            payload = {
                "totalResults": 1,
                "datas": [
                    {
                        "identifier": "10.34847/nkl.05674b20",
                        "metas": [
                            {"propertyUri": "http://nakala.fr/terms#title", "value": "Mission HV709A Phu Dien Chau"},
                            {"propertyUri": "http://nakala.fr/terms#created", "value": "1952-07-08"},
                            {"propertyUri": "http://nakala.fr/terms#license", "value": "CC-BY-NC-SA-4.0"},
                        ],
                        "files": [
                            {"name": "SHDAI__0001__V.jpg", "sha1": "aaa111", "size": 380000},
                            {"name": "SHDAI__PLOT1__V.jpg", "sha1": "bbb222", "size": 410000},
                        ],
                    }
                ],
            }
            return json.dumps(payload).encode("utf-8"), "application/json"
        if "hotosm.org" in url:
            payload = {
                "features": [
                    {
                        "id": "oam123",
                        "bbox": [105.7, 20.9, 105.8, 21.0],
                        "properties": {"title": "Red River Drone", "gsd": 0.045, "datetime": "2024-05-01T00:00:00Z"},
                        "assets": {"visual": {"href": "https://example.org/drone.tif"}},
                    }
                ]
            }
            return json.dumps(payload).encode("utf-8"), "application/json"
        payload = [
            {
                "id": "am99",
                "resource": {"id": "https://gallica.bnf.fr/iiif/ark:/12148/btv1/f1", "width": 4000, "height": 3000},
                "gcps": [
                    {"resource": [0, 0], "geo": [105.5, 10.6]},
                    {"resource": [4000, 0], "geo": [105.9, 10.6]},
                    {"resource": [4000, 3000], "geo": [105.9, 10.2]},
                ],
            }
        ]
        return json.dumps(payload).encode("utf-8"), "application/json"

    nk = terrain_aerial.search_nakala_aerials("Phu Dien", fetch_fn=fake_fetch)
    assert nk["total"] == 1 and len(nk["missions"][0]["plots"]) == 1 and nk["missions"][0]["frame_count"] == 1
    assert "iiif/10.34847/nkl.05674b20/bbb222" in nk["missions"][0]["plots"][0]["preview_url"]

    oam = terrain_aerial.search_openaerialmap([105.5, 20.5, 106.0, 21.2], fetch_fn=fake_fetch)
    assert oam["items"][0]["gsd_m"] == 0.045 and "{z}/{x}/{y}" in oam["items"][0]["xyz_tiles"]

    am = terrain_aerial.search_allmaps_iiif([105.0, 10.0, 106.0, 11.0], fetch_fn=fake_fetch)
    assert len(am["maps"]) == 1 and am["maps"][0]["gcp_count"] == 3


def test_local_server_hub_terrain_transect_media_and_aerial_import_http(hub_instance, tmp_path, monkeypatch):
    """End-to-end HTTP test of `scripts/server.py` wired to a live `HubHTTPServer`."""
    import io
    import urllib.request

    import server
    import store
    from PIL import Image

    hub_url, _ = hub_instance
    archive = FakeArchive()

    # Author A pushes a map + DEM onto the Hub first so Client B can stream & pull via server.py HTTP routes.
    lib_a = make_library(tmp_path / "author_a", archive, monkeypatch)
    tif_a = tmp_path / "sheet_a.tif"
    with rasterio.open(
        tif_a,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(105.60, 10.50, 0.002, 0.002),
    ) as ds:
        ds.write(np.full((3, 32, 32), 190, np.uint8))
    part_a = lib_a.custom.incoming_path()
    part_a.write_bytes(tif_a.read_bytes())
    entry_a = lib_a.custom.ingest(part_a, "sheet_a.tif", "Author A Sheet", "1967")
    lib_a.add([entry_a["id"]])
    lib_a.run_pending()

    dem_a = tmp_path / "dem_a.tif"
    with rasterio.open(
        dem_a,
        "w",
        driver="GTiff",
        width=32,
        height=32,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(105.60, 10.50, 0.002, 0.002),
    ) as ds:
        ds.write(np.full((1, 32, 32), 14.5, np.float32))
    part_dem = lib_a.custom.incoming_path()
    part_dem.write_bytes(dem_a.read_bytes())
    entry_dem = lib_a.custom.ingest(part_dem, "dem_a.tif", "Cao Lanh LiDAR", "2024")
    lib_a.add([entry_dem["id"]])
    lib_a.run_pending()

    hc_a = HubClient(root=lib_a.root)
    hc_a.save_config(hub_url, "bootstrap_super_secret_admin_12345")
    hc_a.push_local_layer(entry_a["id"], lib_a, visibility="hub")
    hc_a.push_local_layer(entry_dem["id"], lib_a, visibility="hub")

    # Spin up local viewer server for Client B + an image HTTP server for /api/aerial/import.
    img_buf = io.BytesIO()
    Image.new("RGB", (120, 90), (140, 130, 110)).save(img_buf, format="JPEG")
    jpg_bytes = img_buf.getvalue()

    class ImgHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(jpg_bytes)))
            self.end_headers()
            self.wfile.write(jpg_bytes)

    img_srv = ThreadingHTTPServer(("127.0.0.1", 0), ImgHandler)
    threading.Thread(target=img_srv.serve_forever, daemon=True).start()
    img_url = f"http://127.0.0.1:{img_srv.server_address[1]}/SHDAI__0001__V.jpg"

    lib_b = make_library(tmp_path / "client_b", archive, monkeypatch)
    db_b = tmp_path / "client_b" / "e.sqlite"
    store.connect(db_b).close()
    local_srv = server.make_server(0, db_b, map_library=lib_b, allow_loopback=True)
    threading.Thread(target=local_srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{local_srv.server_address[1]}"

    def api_call(route, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{base}{route}", data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8")), dict(r.headers)

    try:
        # 1. Connect Client B to Hub via /api/hub/config with auto_push enabled.
        st, _ = api_call(
            "/api/hub/config",
            "POST",
            {"url": hub_url, "api_key": "bootstrap_super_secret_admin_12345", "auto_push": True},
        )
        assert st["connected"] is True and st["auto_push"] is True
        assert {x["id"] for x in st["layers"]} == {entry_a["id"], entry_dem["id"]}

        # 2. Terrain sources list includes Hub Terrarium DEMs + streaming sources.
        tsrc, _ = api_call("/api/terrain/sources")
        assert any(s["id"] == "mapterhorn_dsm" for s in tsrc["streaming"])
        assert any(d["id"] == entry_dem["id"] and d["local"] is False for d in tsrc["installed_dems"])

        # 3. Stream a tile of Author A's sheet through Client B's /tiles/<id>/z/x/y.webp: immutable cache header!
        tx, ty, _, _ = tiling.lonlat_to_tile_pixel(105.62, 10.48, 9)
        with urllib.request.urlopen(f"{base}/tiles/{entry_a['id']}/9/{tx}/{ty}.webp?v=1", timeout=10) as tr:
            raw_tile = tr.read()
            cc = tr.headers.get("Cache-Control", "")
        assert len(raw_tile) > 20 and "immutable" in cc

        # 4. Pull the DEM offline so Client B can sample elevation & compute transects locally!
        pulled, _ = api_call("/api/hub/pull", "POST", {"id": entry_dem["id"]})
        assert pulled["pulled"] == entry_dem["id"]

        elev, _ = api_call("/api/elevation?lon=105.62&lat=10.48")
        assert elev["ground_m"] == pytest.approx(14.5, abs=0.2)
        assert elev["nara_rg373"]["degree_square"] == "10N105E"

        lonlat, _ = api_call("/api/lonlat?lon=105.62&lat=10.48")
        assert lonlat["nara_rg373"]["degree_square"] == "10N105E"

        prof, _ = api_call("/api/transect", "POST", {"start": [105.61, 10.49], "end": [105.64, 10.46], "samples": 8})
        assert len(prof["points"]) == 8 and prof["points"][0]["ground_m"] == pytest.approx(14.5, abs=0.2)

        # 5. Geolocated oblique aerial media syncs to local SQLite AND Hub!
        created_m, _ = api_call(
            "/api/media",
            "POST",
            {
                "lon": 105.63,
                "lat": 10.46,
                "uncertainty_m": 150,
                "title": "Oblique sortie over Cao Lanh",
                "date": "1952-07",
                "image_url": img_url,
                "push_to_hub": True,
            },
        )
        assert created_m["id"] >= 1
        media_fc, _ = api_call("/api/media?bbox=105.5,10.3,105.8,10.6")
        assert len(media_fc["features"]) >= 1

        # 6. Import an aerial frame with 3 GCPs via /api/aerial/import -> builds & auto-pushes back to Hub!
        imp, _ = api_call(
            "/api/aerial/import",
            "POST",
            {
                "url": img_url,
                "title": "SHDAI 1952 Frame",
                "year": "1952",
                "source_width": 120,
                "source_height": 90,
                "gcps": [
                    [0, 0, 105.60, 10.50],
                    [120, 0, 105.65, 10.50],
                    [120, 90, 105.65, 10.45],
                ],
            },
        )
        new_id = imp["map"]["id"]
        # Wait briefly for background worker + auto-push hook to finish.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            hub_cat = hc_a.status_and_catalog()
            if any(x["id"] == new_id for x in hub_cat["layers"]):
                break
            time.sleep(0.25)
        assert any(x["id"] == new_id for x in hc_a.status_and_catalog()["layers"])

        # 7. Disconnect forgets credentials cleanly while preserving local files.
        disc, _ = api_call("/api/hub/config", "DELETE")
        assert disc["connected"] is False
    finally:
        local_srv.quit()
        img_srv.shutdown()
        img_srv.server_close()
        archive.close()


def test_security_guards_path_traversal_ssrf_group_spoofing_and_sea_level_lidar(hub_instance, tmp_path):
    """Verify P1/P2 hardening: `..` traversal refusal, SSRF/file:// blocks, secret scrubbing,
    group_tag spoofing prevention, `preserve_visibility`, and `0.0 m` GEDI laser footprints."""
    from hub.polite import is_safe_id, validate_fetch_url
    from hub.storage import HubStorageError
    from hub_client import HubClientError, clean_hub_url

    hub_url, srv = hub_instance

    # 1. Path traversal IDs rejected everywhere; `.part1` layer names survive `clean_work()`.
    for bad in ("", ".", "..", "../etc", "foo/../bar", ".hidden", "a/b"):
        assert is_safe_id(bad) is False
        with pytest.raises(HubStorageError):
            srv.storage.tiles_path(bad)
    assert is_safe_id("sheet.part1") is True
    keep_file = srv.storage.tiles_dir / "sheet.part1.mbtiles"
    scratch_file = srv.storage.tiles_dir / "sheet.mbtiles.partial"
    keep_file.write_bytes(b"valid")
    scratch_file.write_bytes(b"temp")
    srv.storage.clean_work()
    assert keep_file.is_file() and not scratch_file.exists()
    keep_file.unlink()

    # 2. SSRF & cleartext remote Hub URLs rejected; proprietary `file://` / metadata sources dropped & scrubbed.
    for bad_url in (
        "file:///data/db/hub.sqlite",
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/secret",
        "https://10.0.0.5/internal.tif",
    ):
        with pytest.raises(ValueError):
            validate_fetch_url(bad_url, allow_loopback=False)
    with pytest.raises(HubClientError):
        clean_hub_url("http://remote-hub.example.com")
    assert clean_hub_url("https://remote-hub.example.com/hub/v1") == "https://remote-hub.example.com"

    srv.storage.vault.set_many(
        {
            "PROPRIETARY_SOURCES_JSON": json.dumps(
                [
                    {"id": "evil_file", "tiles": "file:///data/db/hub.sqlite"},
                    {"id": "evil_meta", "tiles": "http://169.254.169.254/{z}/{x}/{y}"},
                    {
                        "id": "planet",
                        "tiles": "https://tiles.planet.example/v1/tok99abcXYZ12345678/{z}/{x}/{y}.png?api_key=psecret_777&fmt=png",
                    },
                ]
            )
        }
    )
    assert [s["id"] for s in srv.storage.vault.proprietary_sources()] == ["planet"]
    scrubbed = srv.storage.vault.scrub(
        "Error fetching https://tiles.planet.example/v1/tok99abcXYZ12345678/9/405/240.png?api_key=psecret_777&fmt=png"
    )
    assert "psecret_777" not in scrubbed and "tok99abcXYZ12345678" not in scrubbed

    # 3. Non-admin uploader cannot spoof another team's `group_tag`, and `preserve_visibility` keeps shared visibility.
    admin_hc = HubClient(root=tmp_path / "adm")
    admin_hc.save_config(hub_url, "bootstrap_super_secret_admin_12345")
    team_a_key = admin_hc.proxy_post(
        "/hub/v1/admin/keys", {"label": "Team A", "scopes": ["read", "upload"], "group_tag": "team_a"}
    )
    mbt = make_test_mbtiles(tmp_path / "pack.mbtiles")
    from hub.auth import authenticate

    with srv.storage.connect() as db:
        p_team_a = authenticate(db, f"Bearer {team_a_key['token']}")
    tmp_copy = tmp_path / "copy1.mbtiles"
    tmp_copy.write_bytes(mbt.read_bytes())
    layer = srv.storage.ingest_pushed_pack(
        tmp_copy, "shared_custom_1", {"visibility": "group", "group_tag": "spoofed_other_team"}, p_team_a
    )
    assert layer["group_tag"] == "team_a"  # Spoofed group_tag ignored!

    tmp_copy2 = tmp_path / "copy2.mbtiles"
    tmp_copy2.write_bytes(mbt.read_bytes())
    updated = srv.storage.ingest_pushed_pack(
        tmp_copy2, "shared_custom_1", {"visibility": "private", "preserve_visibility": True}, p_team_a
    )
    assert updated["visibility"] == "group" and updated["revision"] == 2

    # 4. `0.0 m` sea-level NASA GEDI laser shots are preserved, not falsy-dropped.
    gedi = terrain_aerial.query_sliderule_gedi(
        [106.5, 10.3, 106.55, 10.35],
        lambda *a, **kw: (
            json.dumps([{"longitude": 106.52, "latitude": 10.32, "elev_lowestmode": 0.0, "rh98": 4.2}]).encode("utf-8"),
            "application/json",
        ),
    )
    assert len(gedi["features"]) == 1 and gedi["features"][0]["properties"]["ground_m"] == 0.0


def test_zero_touch_railway_password_and_full_desktop_admin_workflow(tmp_path, monkeypatch):
    """Deploy Hub with ONLY `HUB_ADMIN_PASSWORD` set; configure 100% of upstream keys, proprietary
    tile feeds, collaborator keys, and Hub layers directly through the desktop server API."""
    import urllib.error
    import urllib.request

    import server
    import store

    archive = FakeArchive()
    lib = make_library(tmp_path / "desktop", archive, monkeypatch)

    vol = tmp_path / "railway_vol"
    hub_srv = make_hub_server(
        host="127.0.0.1",
        port=0,
        data_root=vol,
        env={"HUB_ADMIN_PASSWORD": "client-master-pass-123"},
        pacer=HostPacer(default_interval=0.0),
        app_root=lib.app_root,
    )
    threading.Thread(target=hub_srv.serve_forever, daemon=True).start()
    bare_ip_port = f"127.0.0.1:{hub_srv.server_address[1]}"  # No http:// prefix!

    db_path = tmp_path / "desktop" / "e.sqlite"
    store.connect(db_path).close()
    desk_srv = server.make_server(0, db_path, map_library=lib)
    threading.Thread(target=desk_srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{desk_srv.server_address[1]}"

    def call(route, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{base}{route}", data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        # 1. Connect using bare IP:port + HUB_ADMIN_PASSWORD -> receives full `admin` overview & disk stats.
        st = call("/api/hub/config", "POST", {"url": bare_ip_port, "api_key": "client-master-pass-123"})
        assert st["connected"] is True
        assert "admin" in st["me"]["scopes"]
        assert st["me"]["stats"]["disk_total_bytes"] > 0
        assert st["admin"]["vault"]["has_usgs_token"] is False

        # 2. Save upstream USGS / Kaggle / NASA secrets via desktop app -> active immediately, raw tokens never echoed.
        st = call(
            "/api/hub/admin/secrets",
            "POST",
            {
                "USGS_USERNAME": "client_eros",
                "USGS_M2M_TOKEN": "m2m_super_secret_token_abc",
                "KAGGLE_USERNAME": "client_kag",
                "KAGGLE_KEY": "kag_super_secret_xyz",
                "EARTHDATA_TOKEN": "ed_bearer_token_999",
                "MAPVIETNAM_CONTACT": "ops@client.org",
                "ALLOW_NC_SOURCES": "true",
            },
        )
        assert st["me"]["capabilities"]["usgs_m2m"] is True
        assert st["me"]["capabilities"]["kaggle"] is True
        assert st["me"]["capabilities"]["earthdata"] is True
        assert st["admin"]["vault"]["usgs_username"] == "client_eros"
        assert st["admin"]["vault"]["has_usgs_token"] is True
        assert "m2m_super_secret_token_abc" not in json.dumps(st)

        # 3. Add & remove a proprietary tile feed via point-and-click form (no raw JSON).
        st = call(
            "/api/hub/admin/proprietary",
            "POST",
            {
                "id": "planet_2025",
                "title": "Planet VN 2025",
                "tiles": "https://tiles.planet.example/v1/{z}/{x}/{y}.png?api_key=planet_secret_key_55555",
                "format": "png",
                "maxzoom": 18,
                "stream_only": True,
            },
        )
        assert any(lyr["id"] == "prop_planet_2025" for lyr in st["layers"])
        masked = st["admin"]["vault"]["proprietary_sources"][0]["tiles_masked"]
        assert "planet_secret_key_55555" not in masked and "••••" in masked
        st = call("/api/hub/admin/proprietary/delete", "POST", {"id": "planet_2025"})
        assert not any(lyr["id"] == "prop_planet_2025" for lyr in st["layers"])

        # 4. Mint a collaborator API key, verify `adminenv` cannot be revoked, then revoke the collaborator key.
        st = call(
            "/api/hub/admin/keys",
            "POST",
            {"label": "Field Researcher", "scopes": ["read", "request_build", "upload"], "group_tag": "delta"},
        )
        collab = st["created_key"]
        assert collab["token"].startswith("mvhub_")
        with pytest.raises(urllib.error.HTTPError) as exc:
            call("/api/hub/admin/keys/revoke", "POST", {"key_id": "adminenv"})
        assert exc.value.code == 400
        st = call("/api/hub/admin/keys/revoke", "POST", {"key_id": collab["key_id"]})
        assert any(k["key_id"] == collab["key_id"] and k["active"] is False for k in st["admin"]["keys"])

        # 5. Push a map, update its visibility & title on the Hub, then delete it from the Hub — all via desktop API!
        tif = tmp_path / "m.tif"
        with rasterio.open(
            tif,
            "w",
            driver="GTiff",
            width=16,
            height=16,
            count=3,
            dtype="uint8",
            crs="EPSG:4326",
            transform=from_origin(105.6, 10.5, 0.005, 0.005),
        ) as ds:
            ds.write(np.full((3, 16, 16), 200, np.uint8))
        part = lib.custom.incoming_path()
        part.write_bytes(tif.read_bytes())
        entry = lib.custom.ingest(part, "m.tif", "Client Sheet", "1969")
        lib.add([entry["id"]])
        lib.run_pending()

        st = call("/api/hub/push", "POST", {"id": entry["id"], "visibility": "private"})
        assert any(lyr["id"] == entry["id"] and lyr["visibility"] == "private" for lyr in st["layers"])
        st = call(
            "/api/hub/layer/update",
            "POST",
            {"id": entry["id"], "visibility": "hub", "title": "Renamed Client Sheet"},
        )
        assert any(
            lyr["id"] == entry["id"] and lyr["visibility"] == "hub" and lyr["title"] == "Renamed Client Sheet"
            for lyr in st["layers"]
        )
        st = call("/api/hub/layer/delete", "POST", {"id": entry["id"]})
        assert not any(lyr["id"] == entry["id"] for lyr in st["layers"])

        # 6. Build a standard catalog map directly onto the Hub via /api/hub/build-map!
        job = call("/api/hub/build-map", "POST", {"id": "scan_test", "visibility": "hub"})
        if job.get("job_id") and job["state"] != "done":
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                polled = call(f"/api/hub/job/{job['job_id']}")
                if polled["state"] in ("done", "failed"):
                    assert polled["state"] == "done", polled.get("error")
                    break
                time.sleep(0.2)
        st = call("/api/hub")
        assert any(lyr["id"] == "scan_test" for lyr in st["layers"])

        # 7. Standalone local archive keys (`/api/local-keys`) save to `.env` (`0600`) without echoing secrets.
        lk = call(
            "/api/local-keys",
            "POST",
            {"USGS_USERNAME": "local_u", "USGS_M2M_TOKEN": "local_secret_999", "EARTHDATA_TOKEN": "ed_loc_888"},
        )
        assert lk["usgs_username"] == "local_u" and lk["has_usgs_token"] is True and lk["has_earthdata_token"] is True
        assert "local_secret_999" not in json.dumps(lk)

        # 8. Rotating HUB_ADMIN_PASSWORD on Hub restart invalidates the old password cleanly.
        hub_srv.stop_gracefully()
        time.sleep(0.1)
        hub_srv.server_close()
        hub_srv2 = make_hub_server(
            host="127.0.0.1",
            port=0,
            data_root=vol,
            env={"HUB_ADMIN_PASSWORD": "rotated-new-password-456"},
            pacer=HostPacer(default_interval=0.0),
        )
        threading.Thread(target=hub_srv2.serve_forever, daemon=True).start()
        try:
            new_ip_port = f"127.0.0.1:{hub_srv2.server_address[1]}"
            with pytest.raises(urllib.error.HTTPError) as exc_old:
                call("/api/hub/config", "POST", {"url": new_ip_port, "api_key": "client-master-pass-123"})
            assert exc_old.value.code == 400
            st_new = call("/api/hub/config", "POST", {"url": new_ip_port, "api_key": "rotated-new-password-456"})
            assert st_new["connected"] is True and st_new["admin"]["vault"]["usgs_username"] == "client_eros"
        finally:
            hub_srv2.stop_gracefully()
            hub_srv2.server_close()
    finally:
        desk_srv.quit()
        hub_srv.stop_gracefully()
        archive.close()


def test_auto_gcp_perspective_match_confidence_gate_and_hub_endpoint(tmp_path, monkeypatch):
    """Verify auto_gcp matches landmarks with confidence gating and runs across `/hub/v1/auto-gcp` and `/api/auto-gcp`."""
    import base64
    import io
    import urllib.request

    import server
    import store
    from fakeweb import FakeArchive, make_library
    from hub import auto_gcp
    from PIL import Image, ImageDraw

    # Build a rich synthetic map scene with distinct road crossings, blocks, and diagonal avenues
    ref_img = Image.new("RGB", (480, 480), (218, 212, 195))
    draw = ImageDraw.Draw(ref_img)
    rng = np.random.default_rng(7)
    for _ in range(95):
        x0 = int(rng.integers(25, 420))
        y0 = int(rng.integers(25, 420))
        w = int(rng.integers(14, 42))
        h = int(rng.integers(14, 42))
        c = int(rng.integers(45, 165))
        draw.rectangle([x0, y0, x0 + w, y0 + h], fill=(c, c + 12, c - 8), outline=(35, 35, 35), width=2)
    for k in range(40, 460, 58):
        draw.line([(0, k), (480, (k + 22) % 480)], fill=(55, 50, 45), width=3)
        draw.line([(k, 0), ((k + 18) % 480, 480)], fill=(65, 60, 55), width=3)
    draw.line([(20, 40), (450, 430)], fill=(25, 25, 25), width=4)
    draw.line([(440, 30), (30, 440)], fill=(40, 40, 40), width=4)

    ref_buf = io.BytesIO()
    ref_img.save(ref_buf, format="JPEG", quality=92)
    ref_bytes = ref_buf.getvalue()

    # Apply a slight perspective keystone + brightness contrast shift simulating an old tilted aerial
    coeffs = (0.94, 0.03, 18.0, -0.02, 0.96, 14.0, -0.00012, 0.00008)
    scan_img = ref_img.transform((440, 440), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BILINEAR)
    scan_gray = scan_img.convert("L").convert("RGB")
    scan_buf = io.BytesIO()
    scan_gray.save(scan_buf, format="JPEG", quality=90)
    scan_bytes = scan_buf.getvalue()

    ref_bounds = [105.60, 10.40, 105.70, 10.50]
    res = auto_gcp.match_and_score(scan_bytes, ref_bytes, ref_bounds, confidence_threshold=0.65, max_points=12)
    assert res["meets_threshold"] is True, res
    assert res["confidence"] >= 0.65
    assert res["inliers"] >= 6
    assert res["quadrants_covered"] >= 3
    assert len(res["points"]) >= 6
    confs = [p["confidence"] for p in res["points"]]
    assert confs == sorted(confs, reverse=True)

    # Random noise scan must fail the confidence gate and trigger human-in-the-loop fallback
    noise_arr = rng.integers(0, 255, (320, 320, 3), dtype=np.uint8)
    noise_buf = io.BytesIO()
    Image.fromarray(noise_arr).save(noise_buf, format="JPEG", quality=85)
    bad = auto_gcp.match_and_score(noise_buf.getvalue(), ref_bytes, ref_bounds, confidence_threshold=0.70)
    assert bad["meets_threshold"] is False
    assert bad["confidence"] < 0.45

    # End-to-end via Hub `/hub/v1/auto-gcp` + Desktop `POST /api/auto-gcp` using stored custom scan preview
    hub_srv = make_hub_server(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path / "vol",
        env={"HUB_ADMIN_PASSWORD": "test-pass-auto-gcp"},
        pacer=HostPacer(default_interval=0.0),
    )
    threading.Thread(target=hub_srv.serve_forever, daemon=True).start()
    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    db_path = tmp_path / "desk" / "e.sqlite"
    store.connect(db_path).close()
    desk_srv = server.make_server(0, db_path, map_library=lib)
    threading.Thread(target=desk_srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{desk_srv.server_address[1]}"

    def call(route, method="GET", body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            f"{base}{route}", data=data, method=method, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    try:
        st = call(
            "/api/hub/config",
            "POST",
            {"url": f"127.0.0.1:{hub_srv.server_address[1]}", "api_key": "test-pass-auto-gcp"},
        )
        assert st["me"]["capabilities"]["auto_gcp"] is True

        part = lib.custom.incoming_path()
        part.write_bytes(scan_bytes)
        entry = lib.custom.ingest(part, "oblique_1969.jpg", "1969 Oblique", "1969")
        matched = call(
            "/api/auto-gcp",
            "POST",
            {
                "id": entry["id"],
                "mode": "custom",
                "ref_b64": base64.b64encode(ref_bytes).decode("ascii"),
                "ref_bounds": ref_bounds,
                "confidence_threshold": 0.65,
                "max_points": 12,
            },
        )
        assert matched["meets_threshold"] is True and matched["inliers"] >= 6
        assert all(
            0 <= pt["col"] <= entry["image"]["width"] and 0 <= pt["row"] <= entry["image"]["height"]
            for pt in matched["points"]
        )
    finally:
        desk_srv.quit()
        hub_srv.stop_gracefully()
        archive.close()
