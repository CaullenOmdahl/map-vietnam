"""The local API, exercised over HTTP against a temporary store."""

import json
import os
import stat
import threading
import urllib.error
import urllib.request

import library
import pytest
import records
import server
import store
import tiling
from fakerecords import KEY, USER, fake_sources, make_home, ran, set_plan, wait_for
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


# ---- the record sources --------------------------------------------------------------------------
@pytest.fixture
def recs(tmp_path, monkeypatch):
    """A server over a project folder under tmp_path whose record sources are small fakes
    (tests/fakerecords.py) and whose map library is empty. Yields (address, Records, server)."""
    root = make_home(tmp_path / "root", monkeypatch)
    (root / "web").mkdir()
    rec = records.Records(root, sources=fake_sources())
    srv = server.make_server(0, rec.db_path, library.Library(root), web=root / "web", record_sources=rec)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", rec, srv
    rec.stop()
    srv.shutdown()
    thread.join(10)


def delete(url, headers=None):
    """DELETE with chosen headers; returns (status, json)."""
    req = urllib.request.Request(url, method="DELETE", headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def source_of(listing, sid):
    return next(s for s in listing["sources"] if s["id"] == sid)


def test_records_are_listed_added_and_show_on_the_map_without_a_restart(recs):
    base, rec, _ = recs
    listing = call(f"{base}/api/records")
    assert listing["first_run"] is True and listing["busy"] is False
    assert listing["key"] == {"needed": False, "present": False, "source": None}
    assert {s["state"] for s in listing["sources"]} == {"available"}
    wanted = {"id", "title", "description", "size_mb", "time_text", "licence", "needs", "also_adds", "needed_by"}
    wanted |= {"opt_in", "warning", "standard", "wants_key", "state", "removing", "progress", "status_text", "error"}
    wanted |= {"error_detail", "records", "held_in_cases", "added_at", "disk_bytes", "options", "area_text"}
    assert wanted <= set(listing["sources"][0]) and "needs_key" not in listing["sources"][0]
    assert [s["id"] for s in listing["sources"] if not s["standard"]] == ["optional", "thor"]
    assert call(f"{base}/api/search?q=child%20record") == []  # this search is remembered by the server ...

    status, answer = post(f"{base}/api/records/add", {"ids": ["child"]}, JSON)
    assert status == 200 and answer["queued"] == ["base", "child"] and answer["busy"] is True
    assert source_of(answer, "child")["state"] in ("queued", "running")
    assert rec.wait_idle(30)
    listing = call(f"{base}/api/records")
    assert source_of(listing, "child")["state"] == "added" and source_of(listing, "child")["records"] == 2
    assert listing["first_run"] is False and listing["revision"] > 0
    # ... and still the new records are found at once, on the map and by name.
    assert len(call(f"{base}/api/evidence?bbox=105,10,106,11")["features"]) == 4
    assert {h["title"] for h in call(f"{base}/api/search?q=child%20record")} == {"child record 0", "child record 1"}
    assert {s["id"] for s in call(f"{base}/api/sources")} == {"base", "child"}

    status, answer = post(f"{base}/api/records/remove", {"ids": ["child"]}, JSON)
    assert status == 200 and answer["removing"] == ["child"]
    assert source_of(answer, "child")["state"] == "available"  # the answer waited for the removal
    assert call(f"{base}/api/search?q=child%20record") == []
    assert len(call(f"{base}/api/evidence?bbox=105,10,106,11")["features"]) == 2


def test_records_requests_that_cannot_be_honoured_say_why(recs):
    base, rec, _ = recs
    for bad in ({}, {"ids": "base"}, {"ids": []}, {"ids": [1]}, {"ids": ["nope"]}, {"ids": ["check"]}):
        assert post(f"{base}/api/records/add", bad, JSON)[0] == 400
    assert post(f"{base}/api/records/add", {"ids": ["base"], "refresh": "yes"}, JSON)[0] == 400
    assert post(f"{base}/api/records/remove", {"ids": ["base"], "cascade": 1}, JSON)[0] == 400
    assert post(f"{base}/api/records/welcome", {"dismissed": "yes"}, JSON)[0] == 400
    assert post(f"{base}/api/records/nonsense", {}, JSON)[0] == 404

    status, answer = post(f"{base}/api/records/add", {"ids": ["optional"]}, JSON)  # opt-in: needs a yes
    assert status == 400 and answer == {
        "error": "This one is slow and comes from a copyrighted site.",
        "needs_accept": ["optional"],
    }
    for area in ([106, 10, 105, 11], [2.2, 48.8, 2.4, 48.9], "105,10,106,11", [105, 10, 106]):
        status, answer = post(f"{base}/api/records/add", {"ids": ["thor"], "options": {"thor": {"area": area}}}, JSON)
        assert status == 400 and "area" in answer["error"].lower()
    status, answer = post(
        f"{base}/api/records/add", {"ids": ["thor"], "options": {"thor": {"all_vietnam": True}}}, JSON
    )
    assert status == 400 and "3 million" in answer["error"]
    assert not rec.listing()["busy"] and ran(rec.root) == []

    post(f"{base}/api/records/add", {"ids": ["child", "optional"], "accept": ["optional"]}, JSON)
    assert rec.wait_idle(30)
    status, answer = post(f"{base}/api/records/remove", {"ids": ["base"]}, JSON)
    assert status == 409 and answer["also_removes"] == [{"id": "child", "title": "Source child"}]
    assert "“Source child”" in answer["error"] and rec.state_of("base") == "added"
    status, answer = post(f"{base}/api/records/remove", {"ids": ["base"], "cascade": True}, JSON)
    assert status == 200 and answer["removing"] == ["child", "base"]
    assert source_of(answer, "base")["state"] == source_of(answer, "child")["state"] == "available"
    assert source_of(answer, "optional")["state"] == "added"


def test_a_running_source_is_stopped_over_the_api_and_the_welcome_is_dismissed(recs):
    base, rec, _ = recs
    set_plan(rec.root, base={"wait": True})
    post(f"{base}/api/records/add", {"ids": ["base"]}, JSON)
    wait_for((rec.root / "base.started").exists)
    running = source_of(call(f"{base}/api/records"), "base")
    assert running["state"] == "running" and running["progress"] == 0.25
    assert running["status_text"] == "Downloading 1.0 MB of 4.0 MB"
    status, answer = post(f"{base}/api/records/cancel", {"ids": ["base"]}, JSON)
    assert status == 200 and answer["stopped"] == ["base"]
    assert source_of(answer, "base")["state"] == "available" and answer["busy"] is False  # the answer waited for it
    assert rec.wait_idle(20) and source_of(call(f"{base}/api/records"), "base")["state"] == "available"

    assert call(f"{base}/api/records")["first_run"] is True
    status, answer = post(f"{base}/api/records/welcome", {"dismissed": True}, JSON)
    assert status == 200 and answer["first_run"] is False


def test_the_kaggle_key_is_saved_and_forgotten_and_never_comes_back_out(recs, capfd):
    base, rec, _ = recs
    said = []  # every answer the server gives while the key is around

    def ask(method, path, body=None):
        if method == "DELETE":
            status, answer = delete(f"{base}{path}")
        elif method == "POST":
            status, answer = post(f"{base}{path}", body, JSON)
        else:
            status, answer = 200, call(f"{base}{path}")
        said.append(json.dumps(answer))
        return status, answer

    # No key is asked for up front. Kaggle asks: the source fails wanting one, and the listing says so.
    wanted = "Kaggle asked for a key before it would hand over the air strike records, and none is saved yet."
    set_plan(rec.root, thor={"wants_key": wanted})
    status, answer = ask("POST", "/api/records/add", {"ids": ["thor"]})
    assert status == 200 and answer["queued"] == ["thor"]
    assert rec.wait_idle(30)
    _, listing = ask("GET", "/api/records")
    assert source_of(listing, "thor")["wants_key"] is True and source_of(listing, "thor")["error"] == wanted
    assert listing["key"] == {"needed": True, "present": False, "source": None}

    (rec.root / ".env").write_text("MAPVIETNAM_CONTACT=me@example.org\n", encoding="utf-8")
    status, answer = ask("POST", "/api/records/key", {"username": USER, "key": KEY})
    assert status == 200 and answer["key"] == {"needed": True, "present": True, "source": "env file"}
    assert (rec.root / ".env").read_text(
        encoding="utf-8"
    ) == f"MAPVIETNAM_CONTACT=me@example.org\nKAGGLE_USERNAME={USER}\nKAGGLE_KEY={KEY}\n"
    if os.name != "nt":  # Windows has no owner-only mode bits
        assert stat.S_IMODE((rec.root / ".env").stat().st_mode) == 0o600

    # Used: an ingester that is refused says so in plain words, and even one that printed the key is scrubbed.
    set_plan(
        rec.root, thor={"says": [f"sending {KEY}"], "wants_key": "Kaggle did not accept this key (it answered 401)."}
    )
    ask("POST", "/api/records/add", {"ids": ["thor"]})
    assert rec.wait_idle(30)
    _, listing = ask("GET", "/api/records")
    assert source_of(listing, "thor")["error"] == "Kaggle did not accept this key (it answered 401)."
    assert source_of(listing, "thor")["wants_key"] is True and listing["key"]["needed"] is True
    # Refused forms are answered without repeating what was sent.
    for bad in ({"username": USER, "key": KEY + " x"}, {"username": USER + "\nA=1", "key": KEY}, {"key": KEY}, {}):
        assert ask("POST", "/api/records/key", bad)[0] == 400
    assert ask("POST", "/api/records/key", {"username": USER, "key": [KEY]})[0] == 400
    ask("GET", "/api/sources")
    ask("POST", "/api/records/cancel", {"ids": ["thor"]})

    status, answer = ask("DELETE", "/api/records/key")
    assert status == 200 and answer["key"] == {"needed": False, "present": False, "source": None}
    assert (rec.root / ".env").read_text(encoding="utf-8") == "MAPVIETNAM_CONTACT=me@example.org\n"

    printed = capfd.readouterr()
    logs = "".join(
        p.read_text(encoding="utf-8") for p in (rec.root / "data" / "work" / "setup").rglob("*") if p.is_file()
    )
    for where in (*said, printed.out, printed.err, logs):
        assert KEY not in where and USER not in where
    assert "[hidden]" in logs  # the ingester's slip was caught on its way into the log


def new_routes(base):
    """Every route added for the records, quitting and the old-map conversion that changes something."""
    return [
        (f"{base}/api/records/add", {"ids": ["base"]}),
        (f"{base}/api/records/remove", {"ids": ["base"]}),
        (f"{base}/api/records/cancel", {"ids": ["base"]}),
        (f"{base}/api/records/welcome", {"dismissed": True}),
        (f"{base}/api/records/key", {"username": USER, "key": KEY}),
        (f"{base}/api/library/migrate", {}),
        (f"{base}/api/quit", {}),
    ]


def test_the_new_routes_refuse_cross_site_requests(recs):
    base, rec, srv = recs
    port = base.rsplit(":", 1)[1]
    for url, body in new_routes(base):
        assert post(url, body, {**JSON, "Origin": "https://evil.example"})[0] == 403, url
        assert post(url, body, {**JSON, "Origin": "null"})[0] == 403, url
        assert post(url, body, {**JSON, "Sec-Fetch-Site": "cross-site"})[0] == 403, url
        assert post(url, body, {**JSON, "Sec-Fetch-Site": "same-site"})[0] == 403, url
        assert post(url, body, {"Content-Type": "text/plain"})[0] == 415, url  # what a plain form can send
        assert post(url, body, {**JSON, "Host": f"evil.example:{port}"})[0] == 403, url  # DNS rebinding
    assert delete(f"{base}/api/records/key", {"Origin": "https://evil.example"})[0] == 403
    assert delete(f"{base}/api/records/key", {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert delete(f"{base}/api/records/key", {"Host": f"evil.example:{port}"})[0] == 403
    req = urllib.request.Request(f"{base}/api/records", headers={"Host": f"evil.example:{port}"})
    with pytest.raises(urllib.error.HTTPError) as e:  # reads too: the listing says what is installed
        urllib.request.urlopen(req)
    assert e.value.code == 403
    # Nothing happened: no source queued, no key saved, the welcome not dismissed, the server still up.
    assert not rec.listing()["busy"] and ran(rec.root) == [] and not (rec.root / ".env").exists()
    assert rec.listing()["first_run"] is True and call(f"{base}/api/records")["busy"] is False
    # The viewer's own requests pass, under either local name.
    for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
        headers = {**JSON, "Origin": f"http://{host}", "Host": host, "Sec-Fetch-Site": "same-origin"}
        assert post(f"{base}/api/records/welcome", {"dismissed": False}, headers)[0] == 200
        assert delete(f"{base}/api/records/key", {k: v for k, v in headers.items() if k != "Content-Type"})[0] == 200


def test_quit_stops_the_jobs_then_the_server(tmp_path, monkeypatch):
    root = make_home(tmp_path / "root", monkeypatch)
    (root / "web").mkdir()
    rec = records.Records(root, sources=fake_sources())
    srv = server.make_server(0, rec.db_path, library.Library(root), web=root / "web", record_sources=rec)
    thread = threading.Thread(target=srv.serve_forever)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    set_plan(root, base={"wait": True})
    post(f"{base}/api/records/add", {"ids": ["child"]}, JSON)
    wait_for((root / "base.started").exists)
    ingester = rec._jobs["base"].proc

    assert post(f"{base}/api/quit", [], JSON)[0] == 400  # like every POST, the body must be an object
    assert post(f"{base}/api/quit", {}, JSON) == (200, {"quitting": True})  # answered before it stops
    thread.join(20)
    assert not thread.is_alive()  # serve_forever returned: server.py's main() then exits with status 0
    srv.server_close()
    assert ingester.poll() is not None  # no ingester is left running without its server
    assert ran(root) == ["base"] and rec.state_of("base") == "available" and rec.state_of("child") == "available"
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"{base}/api/records", timeout=2)
    srv.quit()  # asking again, after the fact, is harmless


def test_old_format_maps_are_reported_and_converted_from_the_viewer(maps):
    base, lib = maps
    assert call(f"{base}/api/library")["legacy"] == {
        "count": 0,
        "bytes": 0,
        "state": "idle",
        "error": None,
        "converted": 0,
    }
    folder = lib.root / "web" / "tiles" / "L7014A"
    (folder / "8" / "203").mkdir(parents=True)
    from PIL import Image

    Image.new("RGBA", (256, 256), (10, 20, 30, 255)).save(folder / "8" / "203" / "120.png")
    legacy = call(f"{base}/api/library")["legacy"]
    assert legacy["count"] == 1 and legacy["bytes"] == (folder / "8" / "203" / "120.png").stat().st_size

    status, answer = post(f"{base}/api/library/migrate", {}, JSON)
    assert status == 200 and answer["legacy"]["state"] in ("converting", "idle")
    wait_for(lambda: call(f"{base}/api/library")["legacy"]["state"] == "idle")
    listing = call(f"{base}/api/library")
    assert listing["legacy"] == {"count": 0, "bytes": 0, "state": "idle", "error": None, "converted": 1}
    assert next(m for m in listing["maps"] if m["id"] == "L7014A")["state"] == "installed"
    assert not folder.exists() and [layer["id"] for layer in call(f"{base}/catalog.json")["layers"]] == ["L7014A"]


def test_an_old_map_that_cannot_be_converted_is_reported_and_kept(maps):
    base, lib = maps
    folder = lib.root / "web" / "tiles" / "L7014B"
    (folder / "8" / "203").mkdir(parents=True)
    (folder / "8" / "203" / "120.png").write_bytes(b"not a png")
    post(f"{base}/api/library/migrate", {}, JSON)
    wait_for(lambda: call(f"{base}/api/library")["legacy"]["state"] != "converting")
    legacy = call(f"{base}/api/library")["legacy"]
    assert legacy["state"] == "failed" and legacy["count"] == 1 and legacy["converted"] == 0
    assert "L7014B" in legacy["error"] and "Nothing was lost" in legacy["error"] and folder.exists()


def test_slim_evidence_reads_only_what_the_map_draws(base):
    """The slim answer carries the same records as the full one, with fewer fields."""
    full = call(f"{base}/api/evidence?bbox=105,10,107,11")
    slim = call(f"{base}/api/evidence?bbox=105,10,107,11&slim=1")
    assert [f["geometry"] for f in slim["features"]] == [f["geometry"] for f in full["features"]]
    assert [set(f["properties"]) for f in slim["features"]] == [set(server.SLIM_PROPS)] * 3
    for s, f in zip(slim["features"], full["features"], strict=True):
        assert s["properties"] == {k: f["properties"][k] for k in server.SLIM_PROPS}


def test_search_follows_changes_to_the_store_made_behind_its_back(base):
    assert [h["title"] for h in call(f"{base}/api/search?q=saigon")] == ["Saigon"]
    db = store.connect(DB["path"])
    store.sync_source(db, "t", [])  # another process (the command line) removes every record
    db.close()
    assert call(f"{base}/api/search?q=saigon") == []


def status_of(url, method="GET", body=None, headers=None):
    """The HTTP status a request gets, error or not."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})}
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_sites_with_infinite_or_out_of_range_numbers_are_refused(base):
    cid = call(f"{base}/api/cases", "POST", {"name": "c"})["id"]
    site = {"name": "s", "lon": 105.6, "lat": 10.4, "uncertainty_m": 100}
    for bad in ({"uncertainty_m": "inf"}, {"uncertainty_m": "nan"}, {"lon": "nan"}, {"lon": 1e308}, {"lat": 95}):
        assert status_of(f"{base}/api/cases/{cid}/sites", "POST", {**site, **bad}) == 400, bad
    assert call(f"{base}/api/cases/{cid}/sites", "POST", site)["sites"]
    # The case still reads back as valid JSON.
    assert call(f"{base}/api/cases/{cid}")["sites"][0]["uncertainty_m"] == 100


def test_wrong_field_types_and_unknown_records_get_a_400(base):
    assert status_of(f"{base}/api/cases", "POST", {"name": 5}) == 400
    cid = call(f"{base}/api/cases", "POST", {"name": "c"})["id"]
    assert status_of(f"{base}/api/cases/{cid}/items", "POST", {"evidence_id": 999999}) == 400
    assert status_of(f"{base}/api/cases/{cid}/notes", "POST", {"body": ["x"]}) == 400


def test_a_negative_limit_does_not_lift_the_cap(base):
    # SQLite treats a negative LIMIT as none at all; the server clamps it to at least one row.
    for limit in (-1, -2, -1000):
        got = call(f"{base}/api/evidence?limit={limit}")
        assert len(got["features"]) == 1 and got["truncated"], limit
    assert len(call(f"{base}/api/search?q=hamlet&limit=-5")) <= 1


def test_static_files_also_check_the_host(base):
    port = base.rsplit(":", 1)[1]
    assert status_of(f"{base}/app/index.html") in (200, 404)  # the temporary web root may be empty
    assert status_of(f"{base}/app/index.html", headers={"Host": f"evil.example:{port}"}) == 403


def status_and_error(url, method="GET", body=None):
    """(HTTP status, the "error" sentence) of a request the server refuses."""
    with pytest.raises(urllib.error.HTTPError) as e:
        call(url, method, body)
    return e.value.code, json.loads(e.value.read())["error"]


def test_a_case_needs_a_name_and_the_name_must_be_new(base):
    assert status_and_error(f"{base}/api/cases", "POST", {"name": "   "}) == (400, "name must not be empty")
    assert status_and_error(f"{base}/api/cases", "POST", {}) == (400, "name is missing")
    call(f"{base}/api/cases", "POST", {"name": "Hue"})
    assert status_and_error(f"{base}/api/cases", "POST", {"name": "Hue"}) == (
        400,
        "A case with that name already exists.",
    )


def test_blank_notes_and_sites_are_refused(base):
    cid = call(f"{base}/api/cases", "POST", {"name": "Hue"})["id"]
    assert status_and_error(f"{base}/api/cases/{cid}/notes", "POST", {"body": " \n"})[0] == 400
    site = {"name": " ", "lon": 105.6, "lat": 10.4, "uncertainty_m": 100}
    assert status_and_error(f"{base}/api/cases/{cid}/sites", "POST", site) == (400, "name must not be empty")


def test_pinning_needs_a_real_whole_record_number(base):
    cid = call(f"{base}/api/cases", "POST", {"name": "Hue"})["id"]
    url = f"{base}/api/cases/{cid}/items"
    assert status_and_error(url, "POST", {"evidence_id": 9999}) == (400, "That record or case does not exist.")
    for bad in (None, True, 1.5, "1"):
        assert status_and_error(url, "POST", {"evidence_id": bad}) == (400, "evidence_id must be a whole number")
    assert status_and_error(url, "POST", {}) == (400, "evidence_id is missing")


def test_a_huge_number_in_a_path_or_query_is_not_a_server_failure(base):
    assert status_and_error(f"{base}/api/cases/{'9' * 30}")[0] == 404
    assert status_and_error(f"{base}/api/evidence?ids={'9' * 30}") == (400, "a number in the request is too large")


def test_malformed_query_values_get_plain_400s(base):
    assert status_and_error(f"{base}/api/gridref") == (400, "ref is missing")
    assert status_and_error(f"{base}/api/evidence?bbox=1,2,3") == (
        400,
        "bbox must be 4 numbers: west,south,east,north",
    )
    assert status_and_error(f"{base}/api/evidence?bbox=1,2,3,nan")[0] == 400
    assert status_and_error(f"{base}/api/search?q=cao&near=105.6") == (400, "near must be 2 numbers: lon,lat")


def test_a_dropped_connection_is_not_reported_as_a_server_error(base, capsys):
    srv = server.MapServer.__new__(server.MapServer)  # handle_error needs nothing else
    try:
        raise ConnectionResetError("the browser cancelled the tile")
    except ConnectionError:
        srv.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""
    try:
        raise RuntimeError("a real fault")
    except RuntimeError:
        srv.handle_error(None, ("127.0.0.1", 1))
    assert "a real fault" in capsys.readouterr().err
