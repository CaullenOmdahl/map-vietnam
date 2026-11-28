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
