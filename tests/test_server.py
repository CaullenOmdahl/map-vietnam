"""The local API, exercised over HTTP against a temporary store."""

import json
import threading
import urllib.error
import urllib.request

import pytest
import server
import store
import tiling
from fakeweb import FakeArchive, make_library

DB = {}  # the fixture's temporary store, for tests that change it directly


@pytest.fixture
def base(tmp_path):
    db_path = tmp_path / "e.sqlite"
    db = store.connect(db_path)
    store.upsert_source(db, "t", name="Test")
    store.upsert_many(
        db,
        [
            store.Evidence(
                source="t",
                source_record_id="a",
                kind="air_strike",
                lon=105.63,
                lat=10.46,
                uncertainty_m=50,
                original_coord="WS683427",
                original_datum="indian1960",
                title="near Cao Lanh",
                date_start="1968-02-01",
            ),
            store.Evidence(
                source="t",
                source_record_id="b",
                kind="loss_incident",
                lon=106.7,
                lat=10.8,
                uncertainty_m=500,
                original_coord="Gia Dinh province",
                original_datum="n/a",
                title="Saigon",
                date_start="1972",
            ),
            store.Evidence(
                source="t",
                source_record_id="c",
                kind="placename",
                lon=105.60,
                lat=10.40,
                uncertainty_m=100,
                original_coord="WS660370",
                original_datum="indian1960",
                title="undated hamlet",
            ),
        ],
    )
    DB["path"] = db_path
    srv = server.make_server(0, db_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def call(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        text = r.read().decode()
        return json.loads(text) if "json" in r.headers["Content-Type"] else text


def test_binds_localhost_only(base):
    assert base.startswith("http://127.0.0.1:")


def test_evidence_bbox_and_dates(base):
    fc = call(f"{base}/api/evidence?bbox=105.35,10.15,105.95,10.75")
    assert {f["properties"]["title"] for f in fc["features"]} == {"near Cao Lanh", "undated hamlet"}
    fc = call(f"{base}/api/evidence?from=1970&to=1975")
    # Undated records are kept so nothing silently disappears from the map.
    assert {f["properties"]["title"] for f in fc["features"]} == {"Saigon", "undated hamlet"}
    fc = call(f"{base}/api/evidence?kinds=air_strike")
    assert [f["properties"]["original_coord"] for f in fc["features"]] == ["WS683427"]
    assert fc["truncated"] is False
    fc = call(f"{base}/api/evidence?limit=2")
    assert len(fc["features"]) == 2 and fc["truncated"] is True


def test_search_finds_names_without_accents_and_grid_refs(base):
    hits = call(f"{base}/api/search?q=undated%20HAMLET")
    assert hits[0]["type"] == "evidence" and hits[0]["title"] == "undated hamlet"
    hits = call(f"{base}/api/search?q=WS683427")
    assert hits[0]["type"] == "gridref" and len(hits[0]["square"]) == 5
    assert call(f"{base}/api/search?q=zzzz") == []


def test_fold_strips_vietnamese_diacritics():
    assert server.fold("Cao Lãnh, Đồng Tháp") == "cao lanh, dong thap"


def test_gridref_endpoint(base):
    r = call(f"{base}/api/gridref?ref=WS683427&datum=indian1960")
    assert 105.5 < r["lon"] < 105.75 and 10.25 < r["lat"] < 10.5
    assert r["precision_m"] == 100 and r["wgs84_mgrs"].startswith("48PWS")
    assert len(r["square"]) == 5 and r["square"][0] == r["square"][-1]
    ll = call(f"{base}/api/lonlat?lon={r['lon']}&lat={r['lat']}")
    # Converting the square centre back gives the same square on Indian 1960.
    assert ll["mgrs_indian1960"].startswith("48PWS683") and ll["mgrs_indian1960"][10:13] == "427"
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{base}/api/gridref?ref=nonsense")
    assert e.value.code == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        call(f"{base}/api/gridref?ref=WS683427&zone=48")  # zone without its band letter
    assert e.value.code == 400


def test_each_request_closes_its_store_connection(base, monkeypatch):
    class Tracked:
        """Stands in for the request's connection and records whether it was closed."""

        def __init__(self, db):
            self.db, self.closed = db, False

        def __getattr__(self, name):
            return getattr(self.db, name)

        def __enter__(self):
            self.db.__enter__()
            return self

        def __exit__(self, *exc):
            return self.db.__exit__(*exc)

        def close(self):
            self.closed = True
            self.db.close()

    opened = []
    real_db = server.Handler.db

    def tracked_db(handler):
        opened.append(Tracked(real_db(handler)))
        return opened[-1]

    monkeypatch.setattr(server.Handler, "db", tracked_db)
    call(f"{base}/api/sources")
    with pytest.raises(urllib.error.HTTPError):
        call(f"{base}/api/gridref?ref=nonsense")
    assert [t.closed for t in opened] == [True, True]


def test_case_workflow_and_exports(base):
    cid = call(f"{base}/api/cases", "POST", {"name": "Case 1", "summary": "test"})["id"]
    ev_id = call(f"{base}/api/evidence?kinds=air_strike")["features"][0]["properties"]["id"]
    call(f"{base}/api/cases/{cid}/items", "POST", {"evidence_id": ev_id, "note": "key strike"})
    call(
        f"{base}/api/cases/{cid}/sites",
        "POST",
        {"name": "Paddy edge", "lon": 105.631, "lat": 10.461, "uncertainty_m": 30, "rationale": "matches photo"},
    )
    c = call(f"{base}/api/cases/{cid}/notes", "POST", {"body": "talked to farmer"})
    assert len(c["items"]) == 1 and len(c["sites"]) == 1 and len(c["notes"]) == 1

    kml = call(f"{base}/api/cases/{cid}/export.kml")
    gpx = call(f"{base}/api/cases/{cid}/export.gpx")
    gj = call(f"{base}/api/cases/{cid}/export.geojson")
    for text in (kml, gpx, json.dumps(gj)):
        assert "SITE: Paddy edge" in text and "unexploded ordnance" in text
    assert gj["features"][1]["properties"]["uncertainty_m"] == 50

    with pytest.raises(urllib.error.HTTPError):
        call(f"{base}/api/cases/{cid}/sites", "POST", {"name": "x", "lon": 1, "lat": 1, "uncertainty_m": 0})


def test_search_ranks_nearest_first(base):
    assert call(f"{base}/api/search?q=zzz&near=106.7,10.8") == []
    hits = call(f"{base}/api/search?q=on&near=106.7,10.8")
    assert hits[0]["title"] == "Saigon" and hits[0]["distance_km"] == 0.0


def test_slim_evidence_and_case_delete(base):
    fc = call(f"{base}/api/evidence?slim=1")
    assert set(fc["features"][0]["properties"]) == {"id", "kind", "date_start", "date_end", "uncertainty_m", "title"}
    cid = call(f"{base}/api/cases", "POST", {"name": "temp"})["id"]
    assert call(f"{base}/api/cases/{cid}", "DELETE") == {"deleted": cid}
    assert all(c["id"] != cid for c in call(f"{base}/api/cases"))


def test_exports_carry_notes_and_plain_names_and_notes_can_be_deleted(base):
    cid = call(f"{base}/api/cases", "POST", {"name": "N"})["id"]
    ev_id = call(f"{base}/api/evidence?kinds=air_strike")["features"][0]["properties"]["id"]
    call(f"{base}/api/cases/{cid}/items", "POST", {"evidence_id": ev_id})
    c = call(f"{base}/api/cases/{cid}/notes", "POST", {"body": "farmer saw crash 1968"})
    gpx = call(f"{base}/api/cases/{cid}/export.gpx")
    # The waypoint name is the record's title, not the internal kind code.
    assert "<name>near Cao Lanh</name>" in gpx and "air_strike:" not in gpx
    assert "farmer saw crash 1968" in gpx
    assert "farmer saw crash 1968" in call(f"{base}/api/cases/{cid}/export.kml")
    assert call(f"{base}/api/cases/{cid}/export.geojson")["notes"] == ["farmer saw crash 1968"]
    c = call(f"{base}/api/cases/{cid}/notes/{c['notes'][0]['id']}", "DELETE")
    assert c["notes"] == []


def test_withdrawn_records_leave_map_and_search_but_stay_in_cases(base):
    ev = call(f"{base}/api/evidence?kinds=placename")["features"][0]["properties"]
    cid = call(f"{base}/api/cases", "POST", {"name": "W"})["id"]
    call(f"{base}/api/cases/{cid}/items", "POST", {"evidence_id": ev["id"]})
    db = store.connect(DB["path"])
    store.sync_source(db, "t", [], reason="source removed it")
    assert call(f"{base}/api/evidence?kinds=placename")["features"] == []
    assert call(f"{base}/api/search?q=undated") == []
    got = call(f"{base}/api/evidence?ids={ev['id']}")["features"][0]["properties"]
    assert got["withdrawn_reason"] == "source removed it"
    assert call(f"{base}/api/cases/{cid}")["items"][0]["withdrawn_reason"] == "source removed it"


# ---- the map library and tiles ----------------------------------------------------------------
@pytest.fixture
def maps(tmp_path, monkeypatch):
    """A server over an empty store and a map library under tmp_path (fake archive, see fakeweb)."""
    archive = FakeArchive()
    lib = make_library(tmp_path / "root", archive, monkeypatch)
    store.connect(tmp_path / "e.sqlite").close()
    (lib.root / "web").mkdir()
    srv = server.make_server(0, tmp_path / "e.sqlite", lib, web=lib.root / "web")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", lib
    srv.shutdown()
    archive.close()


def post(url, body, headers=None):
    """POST JSON with the headers a browser page on another site could choose; returns (status, json)."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


JSON = {"Content-Type": "application/json"}


def test_library_lists_adds_and_removes_maps(maps):
    base, lib = maps
    listing = call(f"{base}/api/library")
    assert {m["state"] for m in listing["maps"]} == {"available"} and listing["queue_bytes"] == 0
    first = listing["maps"][0]
    assert {"id", "title", "bounds", "region", "urls", "bytes", "kind", "progress", "error", "disk_bytes"} <= set(first)

    status, listing = post(f"{base}/api/library/add", {"ids": ["L7014A"]}, JSON)
    assert status == 200
    assert next(m for m in listing["maps"] if m["id"] == "L7014A")["state"] in ("queued", "downloading", "building")
    assert lib.wait_idle(60)
    assert next(m for m in call(f"{base}/api/library")["maps"] if m["id"] == "L7014A")["state"] == "installed"
    assert [layer["id"] for layer in call(f"{base}/catalog.json")["layers"]] == ["L7014A"]

    status, listing = post(f"{base}/api/library/remove", {"ids": ["L7014A"]}, JSON)
    assert status == 200 and next(m for m in listing["maps"] if m["id"] == "L7014A")["state"] == "available"
    assert call(f"{base}/catalog.json")["layers"] == []


def test_library_refuses_ids_outside_the_catalogue_and_malformed_bodies(maps):
    base, lib = maps
    status, body = post(f"{base}/api/library/add", {"ids": ["https://evil.example/x.pdf"]}, JSON)
    assert status == 400 and "not in the map list" in body["error"]
    for bad in ({}, {"ids": "L7014A"}, {"ids": []}, {"ids": [1]}):
        assert post(f"{base}/api/library/add", bad, JSON)[0] == 400
    assert lib.listing()["queue_bytes"] == 0


def test_cross_site_requests_are_refused(maps):
    base, lib = maps
    port = base.rsplit(":", 1)[1]
    # A page on another site may POST, but its browser names that site in Origin ...
    status, body = post(f"{base}/api/library/add", {"ids": ["L7014A"]}, {**JSON, "Origin": "https://evil.example"})
    assert status == 403 and "other web sites" in body["error"]
    # ... or marks the request cross-site; and a plain form cannot send JSON at all.
    assert post(f"{base}/api/library/add", {"ids": ["L7014A"]}, {**JSON, "Sec-Fetch-Site": "cross-site"})[0] == 403
    assert post(f"{base}/api/library/add", {"ids": ["L7014A"]}, {"Content-Type": "text/plain"})[0] == 415
    assert post(f"{base}/api/library/add", {"ids": ["L7014A"]}, {"Origin": "null", **JSON})[0] == 403
    # A DNS-rebinding page reaches 127.0.0.1 under its own host name: refused, for reads too.
    req = urllib.request.Request(f"{base}/api/library", headers={"Host": f"evil.example:{port}"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert lib.listing()["queue_bytes"] == 0 and lib.state_of("L7014A") == "available"
    # The viewer's own requests pass: same origin, under either local name.
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        headers = {**JSON, "Origin": f"http://{host}", "Host": host, "Sec-Fetch-Site": "same-origin"}
        assert post(f"{base}/api/library/remove", {"ids": ["L7014A"]}, headers)[0] == 200


def test_case_changes_are_protected_the_same_way(base):
    status, _ = post(f"{base}/api/cases", {"name": "x"}, {**JSON, "Origin": "https://evil.example"})
    assert status == 403
    req = urllib.request.Request(f"{base}/api/cases/1", method="DELETE", headers={"Origin": "https://evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_tiles_come_from_the_installed_map_and_missing_ones_are_empty(maps):
    base, lib = maps
    with tiling.MBTilesWriter(lib.tiles_path("M")) as w:
        w.put(10, 812, 481, b"RIFF-a-webp-tile")
    with urllib.request.urlopen(f"{base}/tiles/M/10/812/481.webp") as r:
        assert r.headers["Content-Type"] == "image/webp" and r.read() == b"RIFF-a-webp-tile"
        etag = r.headers["ETag"]
    req = urllib.request.Request(f"{base}/tiles/M/10/812/481.webp", headers={"If-None-Match": etag})
    with pytest.raises(urllib.error.HTTPError) as e:  # urllib reports 304 Not Modified as an error
        urllib.request.urlopen(req)
    assert e.value.code == 304
    for missing in ("M/10/0/0", "not-installed/10/812/481"):
        with urllib.request.urlopen(f"{base}/tiles/{missing}.webp") as r:
            assert r.status == 200 and r.read() == server.EMPTY_TILE
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/tiles/..%2F..%2Fsecret/1/1/1.webp")


def test_tile_addresses_outside_the_grid_are_refused_and_damaged_files_show_nothing(maps):
    base, lib = maps
    with tiling.MBTilesWriter(lib.tiles_path("M")) as w:
        w.put(10, 812, 481, b"RIFF-a-webp-tile")
    for bad in ("M/99/1/1", "M/10/1024/0", "M/3/0/9999999999", "M/64/1/1"):
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"{base}/tiles/{bad}.webp")
        assert e.value.code in (400, 404)  # never a dropped connection
    lib.tiles_path("broken").write_bytes(b"not a database at all, but long enough to be read as a file" * 100)
    with urllib.request.urlopen(f"{base}/tiles/broken/10/812/481.webp") as r:
        assert r.status == 200 and r.read() == server.EMPTY_TILE


def test_bodies_that_are_not_json_objects_get_a_400(maps):
    base, lib = maps
    for body in ([1], "x", 3):
        assert post(f"{base}/api/library/add", body, JSON) == (400, {"error": "send a JSON object"})
    port = base.rsplit(":", 1)[1]
    import http.client

    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
    conn.putrequest("POST", "/api/library/add")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400  # answered at once, not left waiting for a body
    conn.close()


def test_a_failure_inside_the_server_is_answered_not_dropped(maps, monkeypatch):
    base, lib = maps
    monkeypatch.setattr(lib, "remove", lambda ids: (_ for _ in ()).throw(OSError("disk on fire")))
    status, body = post(f"{base}/api/library/remove", {"ids": ["L7014A"]}, JSON)
    assert status == 500 and "disk on fire" in body["error"]
