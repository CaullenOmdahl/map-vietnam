"""Shared survey-strip scans on the Hub and cross-device strip chaining."""

import io
import json
import threading
import urllib.error
import urllib.request

import numpy as np
import pytest
import scan_sync
import server
import store
from fakeweb import FakeArchive, make_library
from hub.hub_server import make_hub_server
from hub.polite import HostPacer
from hub_client import HubClient, HubClientError
from PIL import Image, ImageDraw


def _scene(size=1400, seed=7):
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


@pytest.fixture
def hub_instance(tmp_path):
    env = {"HUB_BOOTSTRAP_ADMIN_KEY": "bootstrap_scan_admin_secret_123"}
    srv = make_hub_server(
        host="127.0.0.1",
        port=0,
        data_root=tmp_path / "hub_vol",
        env=env,
        pacer=HostPacer(default_interval=0.0, intervals={}),
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", srv
    srv.stop_gracefully()
    thread.join(timeout=5)
    srv.server_close()


def _mint(hub_url, admin_key, label, scopes, group_tag=""):
    c = HubClient(root=None)
    return c._request_json(
        hub_url,
        admin_key,
        "/hub/v1/admin/keys",
        method="POST",
        body={"label": label, "scopes": scopes, "group_tag": group_tag},
    )


def test_hub_scans_push_visibility_dedup_and_delete(hub_instance, tmp_path):
    hub_url, _ = hub_instance
    admin = HubClient(root=tmp_path / "admin")
    me = admin.save_config(hub_url, "bootstrap_scan_admin_secret_123")
    assert me["capabilities"]["shared_scans"] is True

    key_a = _mint(hub_url, "bootstrap_scan_admin_secret_123", "Teammate A", ["read", "upload"], "team1")
    key_b = _mint(hub_url, "bootstrap_scan_admin_secret_123", "Teammate B", ["read", "upload"], "team1")
    key_r = _mint(hub_url, "bootstrap_scan_admin_secret_123", "Reader", ["read"], "other")

    client_a = HubClient(root=tmp_path / "a")
    client_a.save_config(hub_url, key_a["token"])
    client_b = HubClient(root=tmp_path / "b")
    client_b.save_config(hub_url, key_b["token"])
    client_r = HubClient(root=tmp_path / "r")
    client_r.save_config(hub_url, key_r["token"])

    jpg_path = tmp_path / "f1.jpg"
    jpg_path.write_bytes(_jpg(Image.new("RGB", (400, 300), (120, 130, 140))))

    # Read-only key cannot upload.
    with pytest.raises(HubClientError):
        client_r.push_scan({"mission": "SHDAI__HV741", "frame": 1, "filename": "f1.jpg"}, jpg_path)

    # Non-JPEG is rejected.
    png_path = tmp_path / "bad.png"
    Image.new("RGB", (40, 40)).save(png_path, format="PNG")
    with pytest.raises(HubClientError):
        client_a.push_scan({"mission": "SHDAI__HV741", "frame": 1, "filename": "bad.png"}, png_path)

    # A pushes a hub-wide frame without points; B pushes the same frame with points -> dedups & fills points.
    res1 = client_a.push_scan(
        {"mission": "SHDAI__HV741", "frame": 1, "filename": "SHDAI__HV741__0001__V.jpg", "title": "Frame 1"},
        jpg_path,
    )
    assert res1["created"] is True
    scan_id = res1["scan"]["id"]
    assert res1["scan"]["points"] == []

    pts = [[0, 0, 105.0, 20.0], [200, 0, 105.1, 20.0], [200, 150, 105.1, 19.9], [0, 150, 105.0, 19.9]]
    res2 = client_b.push_scan(
        {
            "mission": "SHDAI__HV741",
            "frame": 1,
            "filename": "SHDAI__HV741__0001__V.jpg",
            "points": pts,
            "points_size": [200, 150],
        },
        jpg_path,
    )
    assert res2["created"] is False
    assert res2["scan"]["id"] == scan_id
    # Scaled from 200x150 -> 400x300
    assert res2["scan"]["points"][1][:2] == [400.0, 0.0]
    assert res2["scan"]["placed_by"] == "Teammate B"

    # Both work.jpg and match.jpg are downloadable.
    assert len(client_r.scan_image(scan_id, "work")) > 100
    assert len(client_r.scan_image(scan_id, "match")) > 100

    # Private scan by A is invisible to B and R, visible to A and admin.
    priv = client_a.push_scan(
        {"mission": "SHDAI__HV741", "frame": 2, "filename": "f2.jpg", "visibility": "private"},
        jpg_path,
    )["scan"]
    assert [s["id"] for s in client_b.list_scans("SHDAI__HV741")] == [scan_id]
    assert {s["id"] for s in client_a.list_scans("SHDAI__HV741")} == {scan_id, priv["id"]}

    # B cannot delete A's scan; A can.
    with pytest.raises(HubClientError):
        client_b._request_json(hub_url, key_b["token"], f"/hub/v1/scans/{scan_id}", method="DELETE")
    deleted = client_a._request_json(hub_url, key_a["token"], f"/hub/v1/scans/{scan_id}", method="DELETE")
    assert deleted == {"deleted": scan_id}
    assert client_b.list_scans("SHDAI__HV741") == []


def test_cross_device_hub_chaining_and_pull(hub_instance, tmp_path, monkeypatch):
    """Teammate A has placed frame 1; Teammate B has unplaced frame 2. A chains on the Hub, places B's frame
    on the Hub with chain history, and pulls B's frame locally already placed."""
    hub_url, _ = hub_instance
    key_a = _mint(hub_url, "bootstrap_scan_admin_secret_123", "Teammate A", ["read", "upload"], "team1")
    key_b = _mint(hub_url, "bootstrap_scan_admin_secret_123", "Teammate B", ["read", "upload"], "team1")

    scene = _scene()
    archive = FakeArchive()
    lib_a = make_library(tmp_path / "desk_a", archive, monkeypatch)
    lib_b = make_library(tmp_path / "desk_b", archive, monkeypatch)
    monkeypatch.setattr(lib_a, "start_worker", lambda: None)
    monkeypatch.setattr(lib_b, "start_worker", lambda: None)

    hc_a = HubClient(root=lib_a.root)
    hc_a.save_config(hub_url, key_a["token"], auto_push=True)
    hc_b = HubClient(root=lib_b.root)
    hc_b.save_config(hub_url, key_b["token"], auto_push=True)

    # A imports and places frame 1.
    p1 = lib_a.custom.incoming_path()
    p1.write_bytes(_jpg(scene.crop((0, 0, 600, 600))))
    e1 = lib_a.custom.ingest(
        p1, "SHDAI__AI_75_E_1_HV999__0001__V.jpg", "HV999 SHDAI__AI_75_E_1_HV999__0001__V.jpg", "1952"
    )
    w1 = e1["image"]["width"]
    k = w1 / 600.0
    pts1 = [[c * k, r * k, 105 + c * 1e-5, 20 - r * 1e-5] for c, r in ((0, 0), (600, 0), (600, 600), (0, 600))]
    lib_a.custom.save_points(e1["id"], pts1, e1["title"], 1952)

    # B imports unplaced frame 2 and shares it to the Hub.
    p2 = lib_b.custom.incoming_path()
    p2.write_bytes(_jpg(scene.crop((0, 240, 600, 840))))
    e2 = lib_b.custom.ingest(
        p2, "SHDAI__AI_75_E_1_HV999__0002__V.jpg", "HV999 SHDAI__AI_75_E_1_HV999__0002__V.jpg", "1952"
    )
    scan_sync.push_frame(lib_b, hc_b, e2)

    db_a = tmp_path / "desk_a" / "e.sqlite"
    store.connect(db_a).close()
    srv_a = server.make_server(0, db_a, map_library=lib_a, hub=hc_a)
    threading.Thread(target=srv_a.serve_forever, daemon=True).start()
    base_a = f"http://127.0.0.1:{srv_a.server_address[1]}"

    def post(route, body):
        req = urllib.request.Request(
            f"{base_a}{route}",
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
        # A chains from frame 1: Hub syncs A's frame 1, matches B's frame 2, returns a remote proposal.
        plan = post(f"/api/library/custom/{e1['id']}/chain", {"confidence_threshold": 0.6})
        assert plan["hub"] is True
        assert plan["team_frames"] == 1
        assert plan["pushed"] == 1
        [prop] = plan["proposals"]
        assert prop["remote"] is True
        assert prop["frame"] == 2
        assert prop["meets_threshold"] is True

        # A applies the remote proposal -> Hub stores frame 2's points and chain history.
        res = post(f"/api/library/custom/{e1['id']}/chain", {"apply": [prop]})
        assert res["hub_placed"] == 1
        hub_scans = {s["frame"]: s for s in hc_b.list_scans("SHDAI__AI_75_E_1_HV999")}
        assert len(hub_scans[2]["points"]) >= 4
        assert hub_scans[2]["chain"][-1]["from_frame"] == 1
        assert hub_scans[2]["chain"][-1]["by"] == "Teammate A"

        # A pulls B's frame -> it arrives on A's computer already placed!
        pulled = post(f"/api/library/custom/{e1['id']}/hub-pull", {})
        assert pulled["pulled"] == 1
        f2_local = next(m for m in pulled["library"]["maps"] if "0002" in m["title"])
        assert len(f2_local["points"]) >= 4
        assert f2_local["warp_mode"] == "affine"

        # B syncs the mission -> B's local unplaced frame 2 receives the points from the Hub.
        sync_b = scan_sync.sync_mission(lib_b, hc_b, e2, push=False)
        assert sync_b["placed_from_hub"] == 1
        assert len(lib_b.custom.get(e2["id"])["points"]) >= 4
    finally:
        srv_a.quit()
        archive.close()
