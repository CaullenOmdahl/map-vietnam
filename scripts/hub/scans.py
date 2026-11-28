"""Survey-strip scans shared on the Hub, so any team member's device can see and chain a mission's frames.

A desktop pushes each aerial frame it imports (placed or not) with its mission and frame number; the Hub keeps
the picture forever under `<volume>/scans/<id>/` (write-once, like every other Hub cache) and the frame's control
points and chaining history in `hub_scans`. Chaining (`strip_chain.plan_chain`) can then run on the Hub over
every teammate's frames of a mission, and the points it places are recorded with where they came from.

Scan ids are derived from mission, frame number and the archive file name (`scan_id_for`), so two people who
import the same frame from NAKALA land on one Hub record instead of two.

Control points are stored in the pixel space of the Hub's copy (`width` x `height`); clients scale them to and
from their own working image.
"""

import hashlib
import io
import json
import os
import threading
from contextlib import closing
from pathlib import Path

from PIL import Image

from hub.auth import AuthError, utc_now
from hub.polite import is_safe_id

VISIBILITIES = ("private", "group", "hub")
MAX_SCAN_BYTES = 256 << 20  # one frame's working image; far below the 2 GiB pack limit
MATCH_SIDE = 2048  # side of the copy kept for matching
MAX_CHAIN_HISTORY = 20


class ScanError(ValueError):
    """A request about a shared scan that cannot be carried out (bad id, bad image, bad points)."""


def scan_id_for(mission, frame, filename):
    """Stable Hub id of a survey frame: the same archive file of the same frame gets the same id anywhere."""
    key = f"{str(mission).upper()}|{int(frame)}|{Path(str(filename or '')).name.lower()}"
    return f"scan_{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}"


def scale_points(points, from_size, to_size):
    """Control points `[col, row, lon, lat]` moved from one pixel size of a picture to another."""
    fw, fh = (float(v) for v in from_size)
    tw, th = (float(v) for v in to_size)
    if fw <= 0 or fh <= 0:
        raise ScanError("The picture size is unknown.")
    sx, sy = tw / fw, th / fh
    return [[round(c * sx, 2), round(r * sy, 2), float(lon), float(lat)] for c, r, lon, lat in points]


def clean_hub_points(points, width, height):
    """Validated `[col, row, lon, lat]` list inside a `width` x `height` picture (empty means unplaced)."""
    if points in (None, []):
        return []
    if not isinstance(points, list) or len(points) < 3 or len(points) > 200:
        raise ScanError("Send between 3 and 200 control points.")
    out = []
    for p in points:
        try:
            c, r, lon, lat = (float(v) for v in p[:4])
        except (TypeError, ValueError):
            raise ScanError("Each control point is [column, row, longitude, latitude].") from None
        if not (-1 <= c <= width + 1 and -1 <= r <= height + 1 and -180 <= lon <= 180 and -85 <= lat <= 85):
            raise ScanError("A control point lies outside the picture or off the map.")
        out.append([round(c, 2), round(r, 2), round(lon, 7), round(lat, 7)])
    return out


class HubScans:
    """`hub_scans` rows plus the pictures under `<volume>/scans/<id>/`."""

    def __init__(self, storage):
        self.storage = storage
        self.root = storage.root / "scans"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(storage.connect()) as db, db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS hub_scans (
                    id TEXT PRIMARY KEY,
                    mission TEXT NOT NULL,
                    frame INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    filename TEXT NOT NULL DEFAULT '',
                    year TEXT NOT NULL DEFAULT '',
                    subject TEXT NOT NULL DEFAULT 'photo',
                    source_url TEXT NOT NULL DEFAULT '',
                    width INTEGER NOT NULL,
                    height INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    points_json TEXT NOT NULL DEFAULT '[]',
                    placed_by TEXT NOT NULL DEFAULT '',
                    chain_json TEXT NOT NULL DEFAULT '[]',
                    visibility TEXT NOT NULL DEFAULT 'hub',
                    owner_key_id TEXT NOT NULL DEFAULT '',
                    group_tag TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS hub_scans_mission ON hub_scans (mission, frame);
                """
            )

    # ---- files -----------------------------------------------------------------------------------
    def folder(self, scan_id):
        if not is_safe_id(scan_id):
            raise ScanError("Invalid scan id.")
        return self.root / scan_id

    def image_path(self, scan_id, which="work"):
        """`work.jpg` (as pushed) or `match.jpg` (a smaller copy for matching)."""
        return self.folder(scan_id) / ("match.jpg" if which == "match" else "work.jpg")

    # ---- rows ------------------------------------------------------------------------------------
    def _row(self, db, scan_id):
        return db.execute("SELECT * FROM hub_scans WHERE id = ?", (scan_id,)).fetchone()

    def _visible(self, row, principal):
        return row is not None and self.storage.can_view(row, principal)

    def _can_edit(self, row, principal):
        """Owners and admins always; anyone allowed to upload may add points to a frame they can see that is
        shared with the Hub or their group, which is the point of sharing a mission."""
        if principal.has_scope("admin") or (row["owner_key_id"] and row["owner_key_id"] == principal.key_id):
            return True
        return principal.has_scope("upload") and row["visibility"] in ("hub", "group") and self._visible(row, principal)

    def to_dict(self, row):
        return {
            "id": row["id"],
            "mission": row["mission"],
            "frame": row["frame"],
            "title": row["title"],
            "filename": row["filename"],
            "year": row["year"],
            "subject": row["subject"],
            "source_url": row["source_url"],
            "image": {"width": row["width"], "height": row["height"], "sha256": row["sha256"]},
            "size_bytes": row["size_bytes"],
            "points": json.loads(row["points_json"] or "[]"),
            "placed_by": row["placed_by"],
            "chain": json.loads(row["chain_json"] or "[]"),
            "visibility": row["visibility"],
            "owner_key_id": row["owner_key_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get(self, scan_id, principal):
        if not is_safe_id(scan_id):
            raise ScanError("Invalid scan id.")
        with closing(self.storage.connect()) as db:
            row = self._row(db, scan_id)
        return self.to_dict(row) if self._visible(row, principal) else None

    def list(self, principal, mission=None):
        """Visible scans, by mission and frame; one mission when `mission` is given."""
        sql, args = "SELECT * FROM hub_scans", []
        if mission:
            sql += " WHERE mission = ?"
            args.append(str(mission).upper())
        sql += " ORDER BY mission, frame, id LIMIT 5000"
        with closing(self.storage.connect()) as db:
            rows = db.execute(sql, args).fetchall()
        return [self.to_dict(r) for r in rows if self._visible(r, principal)]

    def put(self, meta, image_bytes, principal):
        """Store a pushed frame. The picture is written once; pushing it again (by anyone) only fills in control
        points the Hub does not have yet. Returns `(scan_dict, created)`."""
        principal.require("upload")
        try:
            mission = str(meta["mission"]).strip().upper()
            frame = int(meta["frame"])
        except (KeyError, TypeError, ValueError):
            raise ScanError("Say which mission and frame number this picture is.") from None
        if not mission or len(mission) > 120 or frame < 0:
            raise ScanError("Say which mission and frame number this picture is.")
        filename = Path(str(meta.get("filename") or "")).name[:200]
        scan_id = str(meta.get("id") or "") or scan_id_for(mission, frame, filename)
        if not is_safe_id(scan_id):
            raise ScanError("Invalid scan id.")
        if not image_bytes or len(image_bytes) > MAX_SCAN_BYTES:
            raise ScanError("Send the frame's picture (at most 256 MB).")
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                width, height = img.size
                if img.format != "JPEG":
                    raise ScanError("Send the frame as a JPEG.")
                small = img.convert("L")
                small.thumbnail((MATCH_SIDE, MATCH_SIDE))
        except ScanError:
            raise
        except (OSError, ValueError, Image.DecompressionBombError):
            raise ScanError("That is not a readable JPEG picture.") from None
        visibility = str(meta.get("visibility") or "hub").lower()
        visibility = visibility if visibility in VISIBILITIES else "hub"
        now = utc_now()
        with self._lock, closing(self.storage.connect()) as db, db:
            row = self._row(db, scan_id)
            if row is not None:
                if not self._visible(row, principal):
                    raise AuthError("Another key owns that scan.", status=403)
                incoming = meta.get("points") or []
                if incoming and not json.loads(row["points_json"] or "[]") and self._can_edit(row, principal):
                    pts = clean_hub_points(
                        scale_points(
                            incoming, meta.get("points_size") or (width, height), (row["width"], row["height"])
                        ),
                        row["width"],
                        row["height"],
                    )
                    db.execute(
                        "UPDATE hub_scans SET points_json = ?, placed_by = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(pts), principal.label[:80], now, scan_id),
                    )
                return self.to_dict(self._row(db, scan_id)), False
            folder = self.folder(scan_id)
            folder.mkdir(parents=True, exist_ok=True)
            part = folder / f".work.{os.getpid()}.{threading.get_ident()}.part"
            part.write_bytes(image_bytes)
            os.replace(part, folder / "work.jpg")
            buf = io.BytesIO()
            small.save(buf, "JPEG", quality=88)
            (folder / "match.jpg").write_bytes(buf.getvalue())
            pts = clean_hub_points(
                scale_points(meta["points"], meta.get("points_size") or (width, height), (width, height))
                if meta.get("points")
                else [],
                width,
                height,
            )
            db.execute(
                """
                INSERT INTO hub_scans (
                    id, mission, frame, title, filename, year, subject, source_url, width, height, sha256,
                    size_bytes, points_json, placed_by, chain_json, visibility, owner_key_id, group_tag,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    mission,
                    frame,
                    str(meta.get("title") or filename or scan_id)[:300],
                    filename,
                    str(meta.get("year") or "")[:16],
                    "map" if meta.get("subject") == "map" else "photo",
                    str(meta.get("source_url") or "")[:800],
                    width,
                    height,
                    hashlib.sha256(image_bytes).hexdigest(),
                    len(image_bytes),
                    json.dumps(pts),
                    principal.label[:80] if pts else "",
                    visibility,
                    principal.key_id,
                    principal.group_tag,
                    now,
                    now,
                ),
            )
            return self.to_dict(self._row(db, scan_id)), True

    def update(self, scan_id, body, principal, *, chain_entry=None):
        """Set a frame's control points (in the Hub copy's pixels, or `points_size` pixels) and optionally append
        to its chaining history. Returns the scan, or None when it does not exist / cannot be seen."""
        principal.require("upload")
        if not is_safe_id(scan_id):
            raise ScanError("Invalid scan id.")
        now = utc_now()
        with self._lock, closing(self.storage.connect()) as db, db:
            row = self._row(db, scan_id)
            if not self._visible(row, principal):
                return None
            if not self._can_edit(row, principal):
                raise AuthError("Only the key that shared this frame (or an admin) can change it.", status=403)
            sets, vals = ["updated_at = ?"], [now]
            if "points" in body:
                pts = body.get("points") or []
                if pts and body.get("points_size"):
                    pts = scale_points(pts, body["points_size"], (row["width"], row["height"]))
                sets += ["points_json = ?", "placed_by = ?"]
                vals += [json.dumps(clean_hub_points(pts, row["width"], row["height"])), principal.label[:80]]
            entry = chain_entry or body.get("chain")
            if isinstance(entry, dict):
                history = json.loads(row["chain_json"] or "[]")
                history.append(
                    {
                        "from_id": str(entry.get("from_id") or "")[:64],
                        "from_frame": entry.get("from_frame"),
                        "confidence": entry.get("confidence"),
                        "inliers": entry.get("inliers"),
                        "by": principal.label[:80],
                        "at": now,
                    }
                )
                sets.append("chain_json = ?")
                vals.append(json.dumps(history[-MAX_CHAIN_HISTORY:]))
            if "title" in body:
                sets.append("title = ?")
                vals.append(str(body["title"] or row["title"])[:300])
            vals.append(scan_id)
            db.execute(f"UPDATE hub_scans SET {', '.join(sets)} WHERE id = ?", vals)
            return self.to_dict(self._row(db, scan_id))

    def delete(self, scan_id, principal):
        if not is_safe_id(scan_id):
            raise ScanError("Invalid scan id.")
        with self._lock, closing(self.storage.connect()) as db:
            row = self._row(db, scan_id)
            if row is None:
                return False
            if not (principal.has_scope("admin") or (row["owner_key_id"] and row["owner_key_id"] == principal.key_id)):
                raise AuthError("Only the key that shared this frame (or an admin) can remove it.", status=403)
            with db:
                db.execute("DELETE FROM hub_scans WHERE id = ?", (scan_id,))
        folder = self.folder(scan_id)
        if folder.is_dir():
            for f in folder.iterdir():
                f.unlink(missing_ok=True)
            folder.rmdir()
        return True

    # ---- chaining on the Hub ---------------------------------------------------------------------
    def _as_entry(self, scan):
        """A scan in the shape `strip_chain` reads (it finds mission and frame in the file name)."""
        return {
            "id": scan["id"],
            "kind": "scan",
            "title": f"{scan['mission']}__{scan['frame']:04d}__",
            "filename": scan["filename"],
            "image": scan["image"],
            "points": scan["points"],
        }

    def chain_plan(self, scan_id, principal, *, threshold, match_pair):
        """`strip_chain.plan_chain` over every visible frame of the anchor's mission, matched on the Hub."""
        import strip_chain

        anchor = self.get(scan_id, principal)
        if anchor is None:
            return None
        entries = [self._as_entry(s) for s in self.list(principal, anchor["mission"])]
        if strip_chain.strip_key(self._as_entry(anchor)) is None:
            raise ScanError("This frame's file name does not name a survey strip and frame number.")

        def match(b_entry, a_entry):
            a_bytes = self.image_path(a_entry["id"], "match").read_bytes()
            b_bytes = self.image_path(b_entry["id"], "match").read_bytes()
            a_size = (a_entry["image"]["width"], a_entry["image"]["height"])
            b_size = (b_entry["image"]["width"], b_entry["image"]["height"])
            return match_pair(a_bytes, b_bytes, a_size=a_size, b_size=b_size)

        plan = strip_chain.plan_chain(entries, scan_id, match, threshold=threshold)
        plan["threshold"] = threshold
        plan["mission"] = anchor["mission"]
        return plan

    def chain_apply(self, scan_id, items, principal):
        """Place the given frames of the anchor's mission (never over points someone already set) and record where
        each placement came from. Returns the updated scans."""
        anchor = self.get(scan_id, principal)
        if anchor is None:
            return None
        if not isinstance(items, list) or not items:
            raise ScanError("Send the frames to place.")
        placed = []
        for item in items:
            target = self.get(str(item.get("id") or ""), principal)
            if target is None or target["mission"] != anchor["mission"]:
                raise ScanError("Only frames of the same survey strip can be chained.")
            if target["points"]:
                continue
            placed.append(
                self.update(
                    target["id"],
                    {"points": item.get("points")},
                    principal,
                    chain_entry={
                        "from_id": item.get("from_id") or scan_id,
                        "from_frame": item.get("from_frame"),
                        "confidence": item.get("confidence"),
                        "inliers": item.get("inliers"),
                    },
                )
            )
        return placed
