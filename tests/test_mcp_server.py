"""Tests for the live viewer RemoteBridge (`/api/remote/*`) and the zero-dependency MCP server."""

import base64
import io
import json
import threading
import urllib.request

import mcp_server
import server
import store
from fakeweb import FakeArchive, make_library
from PIL import Image


def _call(name, arguments=None, req_id=1):
    return mcp_server.handle_rpc(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


def _call_json(name, arguments=None, req_id=1):
    resp = _call(name, arguments, req_id=req_id)
    assert resp["result"]["isError"] is False, resp["result"]["content"]
    return json.loads(resp["result"]["content"][0]["text"])


def _jpg_bytes(color=(120, 140, 160), size=(64, 48)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def test_mcp_server_queries_live_bridge_and_views_images(tmp_path, monkeypatch):
    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    monkeypatch.setattr(lib, "start_worker", lambda: None)
    part = lib.custom.incoming_path()
    part.write_bytes(_jpg_bytes((80, 110, 90), (120, 90)))
    entry = lib.custom.ingest(part, "sample.jpg", "Sample Aerial", "1952")

    db = tmp_path / "desk" / "e.sqlite"
    store.connect(db).close()
    srv = server.make_server(0, db, map_library=lib)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("MAP_VIETNAM_URL", base)

    # Simulate an open browser viewer tab connected to the RemoteBridge.
    stop_viewer = threading.Event()
    viewer_ready = threading.Event()
    shot_b64 = "data:image/jpeg;base64," + base64.b64encode(_jpg_bytes((200, 180, 150), (100, 80))).decode("ascii")
    viewer_state = {"center": [105.63, 10.46], "zoom": 11.0, "base": "sat", "era": None}

    def viewer_loop():
        req = urllib.request.Request(
            f"{base}/api/remote/state",
            data=json.dumps(viewer_state).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
        viewer_ready.set()
        while not stop_viewer.is_set():
            try:
                with urllib.request.urlopen(f"{base}/api/remote/poll", timeout=5) as r:
                    cmd = json.load(r).get("cmd")
            except Exception:
                continue
            if not cmd:
                continue
            if cmd["action"] == "navigate":
                viewer_state["center"] = [cmd["params"]["lon"], cmd["params"]["lat"]]
                viewer_state["zoom"] = cmd["params"].get("zoom", 12)
                result = {"ok": True}
            elif cmd["action"] == "screenshot":
                result = {"image_b64": shot_b64}
            else:
                result = {"ok": True}
            rep = urllib.request.Request(
                f"{base}/api/remote/reply",
                data=json.dumps({"id": cmd["id"], "result": result, "state": viewer_state}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(rep, timeout=5).read()

    vt = threading.Thread(target=viewer_loop, daemon=True)
    vt.start()
    assert viewer_ready.wait(timeout=5)

    try:
        # 1. MCP initialize & tools/list
        init = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert init["result"]["serverInfo"]["name"] == "map-vietnam"
        listed = mcp_server.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {t["name"] for t in listed["result"]["tools"]}
        assert {
            "map_status",
            "map_navigate",
            "map_screenshot",
            "view_image",
            "search_places",
            "search_aerials",
            "import_local_files",
            "batch_import_aerials",
            "manage_maps",
            "wait_for_builds",
            "auto_match_map",
            "build_strip_mosaic",
            "push_to_hub",
        } <= tool_names

        # 2. Search places & convert MGRS gridref
        sr = mcp_server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_places", "arguments": {"q": "tho xuan"}},
            }
        )
        assert sr["result"]["isError"] is False
        assert "Thọ Xuân" in sr["result"]["content"][0]["text"]

        gr = mcp_server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "convert_gridref", "arguments": {"ref": "WS683427"}},
            }
        )
        assert gr["result"]["isError"] is False
        assert "lon" in json.loads(gr["result"]["content"][0]["text"])

        # 3. View a custom scan preview as a real MCP image block
        vi = mcp_server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "view_image", "arguments": {"map_id": entry["id"]}},
            }
        )
        assert vi["result"]["isError"] is False
        assert vi["result"]["content"][0]["type"] == "image"
        assert vi["result"]["content"][0]["mimeType"] == "image/jpeg"
        assert len(base64.b64decode(vi["result"]["content"][0]["data"])) > 100

        # 4. Navigate & screenshot the live viewer over RemoteBridge
        nav = mcp_server.handle_rpc(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "map_navigate", "arguments": {"lon": 103.98, "lat": 10.22, "zoom": 13}},
            }
        )
        assert nav["result"]["isError"] is False
        assert json.loads(nav["result"]["content"][0]["text"])["state"]["center"] == [103.98, 10.22]

        shot = mcp_server.handle_rpc(
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "map_screenshot", "arguments": {}}}
        )
        assert shot["result"]["isError"] is False
        assert shot["result"]["content"][0]["type"] == "image"
        assert len(base64.b64decode(shot["result"]["content"][0]["data"])) > 100
    finally:
        stop_viewer.set()
        srv.quit()
        vt.join(timeout=3)
        archive.close()


def _start_test_server(tmp_path, monkeypatch, *, allow_loopback=False):
    archive = FakeArchive()
    lib = make_library(tmp_path / "desk", archive, monkeypatch)
    monkeypatch.setattr(lib, "start_worker", lambda: None)
    db = tmp_path / "desk" / "e.sqlite"
    store.connect(db).close()
    srv = server.make_server(0, db, map_library=lib, allow_loopback=allow_loopback)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("MAP_VIETNAM_URL", base)
    return archive, lib, srv, base


def test_import_local_files_streams_directory_and_handles_options(tmp_path, monkeypatch):
    archive, _lib, srv, _base = _start_test_server(tmp_path, monkeypatch)
    try:
        scans = tmp_path / "scans"
        scans.mkdir()
        (scans / "frame_01.jpg").write_bytes(_jpg_bytes((90, 120, 140), (100, 80)))
        (scans / "frame_02.JPG").write_bytes(_jpg_bytes((110, 100, 90), (120, 90)))
        (scans / "notes.txt").write_text("ignore me", encoding="utf-8")
        (scans / "._hidden.jpg").write_bytes(_jpg_bytes((50, 50, 50), (64, 64)))
        nested = scans / "sub"
        nested.mkdir()
        (nested / "frame_03.jpeg").write_bytes(_jpg_bytes((70, 130, 80), (96, 72)))

        # Non-recursive directory scan with prefix and year
        res = _call_json(
            "import_local_files",
            {"paths": [str(scans)], "year": "1952", "title_prefix": "SHD"},
        )
        assert res["errors"] == []
        assert len(res["imported"]) == 2
        titles = [m["title"] for m in res["imported"]]
        assert titles == ["SHD frame_01", "SHD frame_02"]
        for item in res["imported"]:
            assert item["id"].startswith("custom_")
            assert item["kind"] == "scan"
            assert item["state"] == "needs_points"
            assert item["image"]["width"] > 0

        # Recursive scan with max_files=2 and a missing path recorded in errors
        res2 = _call_json(
            "import_local_files",
            {"paths": [str(scans), str(tmp_path / "does_not_exist.jpg")], "recursive": True, "max_files": 2},
        )
        assert len(res2["imported"]) == 2
        assert len(res2["errors"]) == 1
        assert "does not exist" in res2["errors"][0]["error"].lower()
    finally:
        srv.quit()
        archive.close()


def test_manage_maps_add_by_bbox_edit_and_remove(tmp_path, monkeypatch):
    archive, lib, srv, _base = _start_test_server(tmp_path, monkeypatch)
    try:
        # Pick an existing catalogue map from the test library and queue it via bbox + series
        listing = lib.listing()
        cat_map = next(m for m in listing["maps"] if not m.get("custom") and m.get("state") == "available")
        bounds = cat_map.get("outline") or cat_map["bounds"]
        cx = (bounds[0] + bounds[2]) / 2
        cy = (bounds[1] + bounds[3]) / 2
        small_bbox = [cx - 0.01, cy - 0.01, cx + 0.01, cy + 0.01]

        add_res = _call_json(
            "manage_maps",
            {"action": "add", "bbox": small_bbox, "series": cat_map["series"]},
        )
        assert add_res["ok"] is True
        assert cat_map["id"] in add_res["queued_ids"]
        assert any(m["id"] == cat_map["id"] and m["state"] == "queued" for m in add_res["maps"])

        # Ingest a custom scan, edit its title/year, then remove it
        part = lib.custom.incoming_path()
        part.write_bytes(_jpg_bytes((100, 120, 140), (80, 60)))
        entry = lib.custom.ingest(part, "strip_a.jpg", "Original Title", "1951")

        edit_res = _call_json(
            "manage_maps",
            {"action": "edit", "map_id": entry["id"], "title": "Renamed Strip", "year": "1954"},
        )
        assert edit_res["ok"] is True
        assert edit_res["map"]["title"] == "Renamed Strip"
        assert str(edit_res["map"]["year"]) == "1954"

        rem_res = _call_json("manage_maps", {"action": "remove", "ids": [entry["id"], cat_map["id"]]})
        assert rem_res["ok"] is True
        assert rem_res["removed_ids"] == [entry["id"], cat_map["id"]]
        remaining_ids = {m["id"] for m in lib.listing()["maps"]}
        assert entry["id"] not in remaining_ids
    finally:
        srv.quit()
        archive.close()


def test_wait_for_builds_polls_until_done_and_times_out(monkeypatch):
    monkeypatch.setattr(mcp_server, "POLL_INTERVAL_S", 0.02)
    polls = {"n": 0}

    def fake_api_json(method, route, *, body=None, params=None, timeout=30):
        assert method == "GET" and route == "/api/library"
        polls["n"] += 1
        if polls["n"] == 1:
            return {
                "maps": [
                    {"id": "m1", "title": "Map 1", "state": "building", "progress": 0.5, "bounds": None, "error": None},
                    {"id": "m2", "title": "Map 2", "state": "queued", "progress": None, "bounds": None, "error": None},
                ]
            }
        return {
            "maps": [
                {
                    "id": "m1",
                    "title": "Map 1",
                    "state": "installed",
                    "progress": 1.0,
                    "bounds": [105.0, 10.0, 106.0, 11.0],
                    "error": None,
                },
                {
                    "id": "m2",
                    "title": "Map 2",
                    "state": "available",
                    "progress": None,
                    "bounds": None,
                    "error": "Download failed",
                },
            ]
        }

    monkeypatch.setattr(mcp_server, "_api_json", fake_api_json)
    res = _call_json("wait_for_builds", {"ids": ["m1", "m2"], "timeout_s": 5})
    assert polls["n"] == 2
    assert res["completed"] is True
    assert res["timed_out"] is False
    by_id = {m["id"]: m for m in res["maps"]}
    assert by_id["m1"]["state"] == "installed"
    assert by_id["m1"]["bounds"] == [105.0, 10.0, 106.0, 11.0]
    assert len(res["errors"]) == 1
    assert res["errors"][0]["id"] == "m2"

    # Timeout case
    monkeypatch.setattr(
        mcp_server,
        "_api_json",
        lambda *a, **kw: {"maps": [{"id": "m3", "title": "Stuck", "state": "building", "progress": 0.1}]},
    )
    res_to = _call_json("wait_for_builds", {"ids": ["m3"], "timeout_s": 0.05})
    assert res_to["completed"] is False
    assert res_to["timed_out"] is True


def test_auto_match_map_captures_live_viewer_and_auto_places(tmp_path, monkeypatch):
    archive, lib, srv, base = _start_test_server(tmp_path, monkeypatch)
    part = lib.custom.incoming_path()
    part.write_bytes(_jpg_bytes((90, 110, 100), (200, 160)))
    entry = lib.custom.ingest(part, "aerial_01.jpg", "Aerial 01", "1953")

    stop_viewer = threading.Event()
    viewer_ready = threading.Event()
    shot_b64 = "data:image/jpeg;base64," + base64.b64encode(_jpg_bytes((190, 175, 160), (200, 160))).decode("ascii")
    viewer_state = {
        "center": [105.63, 10.46],
        "zoom": 13.0,
        "bounds": [105.60, 10.43, 105.66, 10.49],
        "base": "sat",
        "era": None,
    }

    def viewer_loop():
        req = urllib.request.Request(
            f"{base}/api/remote/state",
            data=json.dumps(viewer_state).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
        viewer_ready.set()
        while not stop_viewer.is_set():
            try:
                with urllib.request.urlopen(f"{base}/api/remote/poll", timeout=5) as r:
                    cmd = json.load(r).get("cmd")
            except Exception:
                continue
            if not cmd:
                continue
            result = {"image_b64": shot_b64} if cmd["action"] == "screenshot" else {"ok": True}
            rep = urllib.request.Request(
                f"{base}/api/remote/reply",
                data=json.dumps({"id": cmd["id"], "result": result, "state": viewer_state}).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(rep, timeout=5).read()

    vt = threading.Thread(target=viewer_loop, daemon=True)
    vt.start()
    assert viewer_ready.wait(timeout=5)

    sample_points = [
        [10.0, 10.0, 105.61, 10.48],
        [190.0, 10.0, 105.65, 10.48],
        [190.0, 150.0, 105.65, 10.44],
        [10.0, 150.0, 105.61, 10.44],
    ]
    seen_bounds = []

    def fake_match_and_score(scan_bytes, ref_bytes, ref_bounds, **kwargs):
        seen_bounds.append(ref_bounds)
        return {
            "ok": True,
            "confidence": 0.91,
            "meets_threshold": True,
            "points": sample_points,
            "inliers": 28,
        }

    monkeypatch.setattr(server.auto_gcp, "match_and_score", fake_match_and_score)

    try:
        # Preview only (auto_place=False) using auto-captured viewer bounds
        m1 = _call_json("auto_match_map", {"map_id": entry["id"], "confidence_threshold": 0.75})
        assert m1["meets_threshold"] is True
        assert m1["auto_placed"] is False
        assert seen_bounds[-1] == viewer_state["bounds"]
        assert lib.custom.get(entry["id"]).get("points") in (None, [])

        # Auto-place when meets_threshold is True
        m2 = _call_json("auto_match_map", {"map_id": entry["id"], "auto_place": True})
        assert m2["auto_placed"] is True
        assert m2["map"]["id"] == entry["id"]
        assert len(lib.custom.get(entry["id"])["points"]) == 4
    finally:
        stop_viewer.set()
        srv.quit()
        vt.join(timeout=3)
        archive.close()


def test_batch_import_aerials_imports_valid_and_reports_errors(tmp_path, monkeypatch):
    archive, _lib, srv, _base = _start_test_server(tmp_path, monkeypatch, allow_loopback=True)
    try:
        archive.files["/aerial_1.jpg"] = _jpg_bytes((120, 130, 140), (100, 80))
        archive.files["/aerial_2.jpg"] = _jpg_bytes((140, 120, 110), (110, 90))
        res = _call_json(
            "batch_import_aerials",
            {
                "items": [
                    {"url": f"{archive.base}/aerial_1.jpg", "title": "Strip Frame 1", "year": "1952"},
                    {"url": "http://invalid-scheme.example/bad.jpg", "title": "Bad Scheme"},
                    {"url": f"{archive.base}/aerial_2.jpg", "title": "Strip Frame 2", "year": "1952"},
                ]
            },
        )
        assert len(res["imported"]) == 2
        assert [m["title"] for m in res["imported"]] == ["Strip Frame 1", "Strip Frame 2"]
        assert len(res["errors"]) == 1
        assert res["errors"][0]["title"] == "Bad Scheme"
    finally:
        srv.quit()
        archive.close()


def test_build_strip_mosaic_and_push_to_hub(monkeypatch):
    calls = []

    def fake_api_json(method, route, *, body=None, params=None, timeout=30):
        calls.append((method, route, body))
        if route.endswith("/mosaic"):
            return {"ok": True, "map": {"id": "custom_mosaic1234", "state": "queued"}}
        if route == "/api/hub/push":
            if body["id"] == "bad_map":
                raise RuntimeError("Map Vietnam API error (400): not installed")
            return {"pushed": [{"id": body["id"], "visibility": body["visibility"]}]}
        raise AssertionError(f"Unexpected call: {method} {route}")

    monkeypatch.setattr(mcp_server, "_api_json", fake_api_json)

    mosaic_res = _call_json("build_strip_mosaic", {"map_id": "custom_abc1234567", "title": "1952 Strip"})
    assert mosaic_res["ok"] is True
    assert calls[0] == ("POST", "/api/library/custom/custom_abc1234567/mosaic", {"title": "1952 Strip"})

    push_res = _call_json("push_to_hub", {"ids": ["custom_1", "bad_map", "custom_2"], "visibility": "group"})
    assert len(push_res["pushed"]) == 2
    assert push_res["pushed"][0] == {"id": "custom_1", "visibility": "group"}
    assert len(push_res["errors"]) == 1
    assert push_res["errors"][0]["id"] == "bad_map"
