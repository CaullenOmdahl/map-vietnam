"""Share survey-strip frames with the Hub and chain over the whole team's frames of a mission.

The desktop keeps working on its own: everything here is best-effort and the callers fall back to local
chaining when the Hub cannot be reached. What it adds when a Hub is connected:

  * `push_frame` shares one imported frame (placed or not) with its mission and frame number, so teammates see
    it; with "share automatically" on, the server does this on import, on placing and on chaining.
  * `sync_mission` shares this computer's frames of a mission the Hub lacks, and places local frames that a
    teammate already placed on the Hub.
  * `hub_plan` runs chaining on the Hub over every visible frame of the mission and maps the answer back to
    this computer's maps; frames only teammates have stay as Hub proposals (`remote`), placed on the Hub.
  * `pull_mission` downloads teammates' frames of a mission this computer does not have yet.

Control points move between the Hub copy's pixels and the local working image with `scale_points`.
"""

import contextlib

import hub_client
import strip_chain
from hub.scans import scale_points, scan_id_for


def frame_key(entry):
    """(mission, frame) of a strip frame, or None."""
    return strip_chain.strip_key(entry)


def hub_id(entry):
    """The Hub id this local frame has (or will get) when shared."""
    if entry.get("hub_scan_id"):
        return entry["hub_scan_id"]
    key = frame_key(entry)
    if key is None:
        return None
    return scan_id_for(key[0], key[1], entry.get("filename") or entry.get("original_title") or entry.get("title"))


def _size(entry):
    return (entry["image"]["width"], entry["image"]["height"])


def push_frame(lib, hub, entry, *, visibility="hub"):
    """Share one local strip frame (picture, mission, frame, points) on the Hub; returns the Hub scan."""
    key = frame_key(entry)
    if key is None or entry.get("kind") != "scan":
        return None
    meta = {
        "id": hub_id(entry),
        "mission": key[0],
        "frame": key[1],
        "filename": entry.get("filename") or "",
        "title": entry.get("original_title") or entry.get("title") or "",
        "year": str(entry.get("year") or ""),
        "subject": entry.get("subject") or "photo",
        "visibility": visibility,
    }
    if len(entry.get("points") or []) >= 3:
        meta["points"] = entry["points"]
        meta["points_size"] = list(_size(entry))
    res = hub.push_scan(meta, lib.custom.work_image(entry["id"]))
    scan = res.get("scan") or {}
    if scan.get("id") and entry.get("hub_scan_id") != scan["id"]:
        lib.custom.update(entry["id"], hub_scan_id=scan["id"])
    return scan


def share_points(lib, hub, entry, *, chain=None):
    """Send a placed local frame's points (and where they came from) to its Hub copy, sharing it first if the
    Hub does not have it yet."""
    if frame_key(entry) is None:
        return None
    body = {"points": entry.get("points") or [], "points_size": list(_size(entry))}
    if chain:
        body["chain"] = chain
    try:
        return hub.update_scan(hub_id(entry), body)
    except hub_client.HubClientError as e:  # not on the Hub yet: share the whole frame, then add the history
        if "not found" not in str(e).lower():
            raise
    scan = push_frame(lib, hub, entry)
    if chain and scan:
        hub.update_scan(scan["id"], {"chain": chain})
    return scan


def mission_frames(lib, anchor):
    """(mission key, [local strip frames of that mission])."""
    key = frame_key(anchor)
    if key is None:
        return None, []
    return key[0], [
        e for e in lib.custom.entries() if e.get("kind") == "scan" and (frame_key(e) or (None,))[0] == key[0]
    ]


def sync_mission(lib, hub, anchor, *, push=True):
    """Share local frames the Hub lacks (when `push`) and place local frames a teammate placed on the Hub.

    Returns `{"remote": [hub scans], "pushed", "placed_from_hub", "local_by_hub_id"}`."""
    mission, local = mission_frames(lib, anchor)
    if mission is None:
        return {"remote": [], "pushed": 0, "placed_from_hub": 0, "local_by_hub_id": {}}
    remote = {s["id"]: s for s in hub.list_scans(mission)}
    pushed = placed = 0
    by_hub = {}
    for e in local:
        hid = hub_id(e)
        by_hub[hid] = e
        r = remote.get(hid)
        if r is None:
            if push:
                scan = push_frame(lib, hub, e)
                if scan:
                    remote[scan["id"]] = scan
                    pushed += 1
            continue
        local_placed = len(e.get("points") or []) >= 3
        if push and local_placed and not r["points"]:
            with contextlib.suppress(hub_client.HubClientError):
                remote[hid] = share_points(lib, hub, e) or r
        elif not local_placed and len(r["points"]) >= 3:
            pts = scale_points(r["points"], (r["image"]["width"], r["image"]["height"]), _size(e))
            with contextlib.suppress(ValueError):  # being built right now, or the points no longer fit
                lib.place_custom(e["id"], pts, e["title"], e.get("year"), warp_mode="affine")
                lib.custom.update(e["id"], chain=(r.get("chain") or [])[-1:] or None, placed_by=r.get("placed_by"))
                placed += 1
        if e.get("hub_scan_id") != hid:
            lib.custom.update(e["id"], hub_scan_id=hid)
    return {"remote": list(remote.values()), "pushed": pushed, "placed_from_hub": placed, "local_by_hub_id": by_hub}


def hub_plan(lib, hub, anchor, sync, threshold):
    """Chain on the Hub from `anchor` and return the plan in local terms.

    Proposals for frames this computer has get the local id and points in local pixels; the rest are marked
    `remote` (placed on the Hub for the team). Stops keep the Hub id when the frame is not local."""
    by_hub = sync["local_by_hub_id"]
    titles = {s["id"]: s["title"] for s in sync["remote"]}
    plan = hub.chain_scans(hub_id(anchor), {"confidence_threshold": threshold})
    out = []
    for p in plan.get("proposals") or []:
        local = by_hub.get(p["id"])
        remote = next((s for s in sync["remote"] if s["id"] == p["id"]), None)
        hub_size = (remote["image"]["width"], remote["image"]["height"]) if remote else None
        q = {**p, "hub_id": p["id"], "from_hub_id": p.get("from_id")}
        if local is not None and hub_size:
            q["id"] = local["id"]
            q["points"] = scale_points(p["points"], hub_size, _size(local))
            q["remote"] = False
        else:
            q["remote"] = True
            q["title"] = titles.get(p["id"], p["id"])
        from_local = by_hub.get(p.get("from_id"))
        if from_local is not None:
            q["from_id"] = from_local["id"]
        out.append(q)
    stops = []
    for st in plan.get("stops") or []:
        local = by_hub.get(st["id"])
        stops.append({**st, "id": local["id"] if local else st["id"], "remote": local is None})
    return {
        "mission": plan.get("mission"),
        "anchor": {"id": anchor["id"], "frame": (plan.get("anchor") or {}).get("frame")},
        "proposals": out,
        "stops": stops,
        "threshold": threshold,
        "hub": True,
        "team_frames": sum(1 for s in sync["remote"] if s["id"] not in by_hub),
        "pushed": sync["pushed"],
        "placed_from_hub": sync["placed_from_hub"],
    }


def pull_mission(lib, hub, anchor):
    """Download the mission's frames shared by teammates that this computer lacks; placed ones are placed (and
    built) here too. Returns the new local entries."""
    mission, local = mission_frames(lib, anchor)
    if mission is None:
        return []
    have = {hub_id(e) for e in local}
    added = []
    for s in hub.list_scans(mission):
        if s["id"] in have:
            continue
        part = lib.custom.incoming_path()
        try:
            part.write_bytes(hub.scan_image(s["id"], "work"))
            entry = lib.custom.ingest(part, s["filename"] or f"{s['id']}.jpg", s["title"], s.get("year") or "")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        entry = lib.custom.update(entry["id"], hub_scan_id=s["id"], subject=s.get("subject") or "photo")
        if frame_key(entry) is None:  # keep the strip identity even if the shared title lost it
            entry = lib.custom.update(entry["id"], original_title=f"{s['mission']}__{s['frame']:04d}__")
        if len(s["points"]) >= 3:
            pts = scale_points(s["points"], (s["image"]["width"], s["image"]["height"]), _size(entry))
            lib.place_custom(entry["id"], pts, entry["title"], entry.get("year"), warp_mode="affine")
            entry = lib.custom.update(entry["id"], chain=(s.get("chain") or [])[-1:] or None, placed_by=s["placed_by"])
        added.append(entry)
    return added
