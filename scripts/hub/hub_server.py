"""HTTP server and CLI for the Map Vietnam Intermediary Hub (Docker / Railway + attached Volume).

Listens on `0.0.0.0:${PORT:-8780}` by default so Railway's proxy and `/hub/v1/health` healthcheck
reach it directly. Every route except `GET /hub/v1/health` requires `Authorization: Bearer <key>`.

Usage:
  .venv/bin/python scripts/hub/hub_server.py serve [--host 0.0.0.0] [--port 8780] [--data /data]
  .venv/bin/python scripts/hub/hub_server.py keys create --label "Research team" [--scopes read,request_build,upload] [--group team1]
  .venv/bin/python scripts/hub/hub_server.py keys list
  .venv/bin/python scripts/hub/hub_server.py keys revoke <key_id>
"""

import argparse
import base64
import concurrent.futures
import contextlib
import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import urllib.parse
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import paths  # noqa: E402
import terrain_aerial  # noqa: E402
import tiling  # noqa: E402
from app_log import AppLogger  # noqa: E402

from hub import auto_gcp  # noqa: E402
from hub.auth import (  # noqa: E402
    DEFAULT_CLIENT_SCOPES,
    AuthError,
    authenticate,
    list_keys,
    mint_key,
    revoke_key,
    utc_now,
)
from hub.polite import is_safe_id  # noqa: E402
from hub.storage import MAX_HUB_UPLOAD_BYTES, HubStorage, HubStorageError  # noqa: E402

HUB_VERSION = "0.3.0"
TILE_ROUTE_RE = re.compile(
    r"^/hub/v1/tiles/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})/(\d{1,2})/(\d{1,10})/(\d{1,10})\.(webp|png|jpg)$"
)
PACK_ROUTE_RE = re.compile(r"^/hub/v1/packs/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\.mbtiles$")
LAYER_ROUTE_RE = re.compile(r"^/hub/v1/layers/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})$")
MEDIA_ROUTE_RE = re.compile(r"^/hub/v1/media/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})$")
PROP_ROUTE_RE = re.compile(r"^/hub/v1/admin/proprietary/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})$")
JOB_ROUTE_RE = re.compile(r"^/hub/v1/jobs/([A-Za-z0-9][A-Za-z0-9_.-]{0,95})$")
KEY_ROUTE_RE = re.compile(r"^/hub/v1/admin/keys/([A-Za-z0-9_-]{4,16})$")
UPLOAD_CHUNK = 1 << 20
MAX_JSON_BYTES = 2 << 20
IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"
EMPTY_WEBP = tiling.empty_tile()
EMPTY_TERRARIUM = tiling.empty_terrarium_tile()


def parse_bbox_param(text):
    """Parse `w,s,e,n` query string into `[w, s, e, n]` floats."""
    try:
        vals = [float(v) for v in str(text).split(",")]
    except ValueError:
        raise ValueError("bbox must be four comma-separated numbers: west,south,east,north.") from None
    if len(vals) != 4 or not (vals[0] < vals[2] and vals[1] < vals[3]):
        raise ValueError("bbox needs west < east and south < north.")
    return vals


class HubJobRunner:
    """Bounded single-worker background job runner (`max_workers=1`) so COG/tile builds never
    exhaust Railway container RAM, with single-flight job deduplication and clean SIGTERM drain."""

    def __init__(self, storage: HubStorage, *, app_root=None):
        self.storage = storage
        self.app_root = Path(app_root) if app_root else paths.APP_ROOT
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="hub-build")
        self._lock = threading.Lock()
        self._active_by_target = {}  # target_id -> job_id
        self._stopping = False

    def _make_catalog_library(self, scratch_dir):
        import library

        cat_lib = library.Library(
            root=scratch_dir,
            catalogue_path=self.app_root / "data" / "sources" / "map_library.json",
        )
        cat_lib.app_root = self.app_root
        cat_lib.scans = self.app_root / "data" / "scans"
        return cat_lib

    def stop(self):
        """Stop accepting new jobs and shut down cleanly on SIGTERM."""
        with self._lock:
            self._stopping = True
        self._pool.shutdown(wait=False, cancel_futures=True)

    def submit_dem_pack(self, pack_key, bbox, visibility, principal):
        """Queue or coalesce a regional COG DEM/HAND/canopy build on `/data/tiles/`."""
        principal.require("request_build")
        if pack_key not in terrain_aerial.BUILDABLE_COG_PACKS:
            raise HubStorageError(f"Unknown terrain pack {pack_key!r}.")
        w, s, e, n = terrain_aerial.check_dem_bbox(bbox)
        bbox_slug = f"{w:.2f}_{s:.2f}_{e:.2f}_{n:.2f}".replace("-", "m").replace(".", "p")
        vis = visibility if visibility in ("private", "group", "hub") else "hub"
        if vis == "private":
            layer_id = f"dem_{principal.key_id}_{pack_key}_{bbox_slug}"
        elif vis == "group":
            if not principal.group_tag:
                raise HubStorageError("This API key has no group assigned for group-scoped DEM builds.")
            grp_slug = re.sub(r"[^A-Za-z0-9_-]", "_", principal.group_tag)[:24]
            layer_id = f"dem_grp_{grp_slug}_{pack_key}_{bbox_slug}"
        else:
            layer_id = f"dem_{pack_key}_{bbox_slug}"

        # Already in the infinite cache? Return ready immediately with zero work.
        if self.storage.tiles_path(layer_id).is_file():
            row = self.storage.get_layer_row(layer_id, principal)
            if row is not None:
                return {"layer_id": layer_id, "state": "done", "progress": 1.0}

        with self._lock:
            if self._stopping:
                raise HubStorageError("The Hub server is restarting; try again in a moment.")
            existing_job = self._active_by_target.get(layer_id)
            if existing_job:
                return self.get_job(existing_job)
            job_id = f"job_{hashlib.sha256(f'{layer_id}:{utc_now()}'.encode()).hexdigest()[:12]}"
            self._active_by_target[layer_id] = job_id
            now = utc_now()
            with closing(self.storage.connect()) as db, db:
                db.execute(
                    """
                    INSERT INTO hub_jobs (id, target_id, job_kind, state, progress, status_text, requested_by, created_at, updated_at)
                    VALUES (?, ?, 'dem_pack', 'queued', 0.0, 'Queued on Hub', ?, ?, ?)
                    """,
                    (job_id, layer_id, principal.key_id, now, now),
                )
            self._pool.submit(self._run_dem_job, job_id, layer_id, pack_key, [w, s, e, n], vis, principal)
        return self.get_job(job_id)

    def _update_job(self, job_id, *, state=None, progress=None, status_text=None, error=None):
        sets = ["updated_at = ?"]
        vals = [utc_now()]
        if state is not None:
            sets.append("state = ?")
            vals.append(state)
        if progress is not None:
            sets.append("progress = ?")
            vals.append(round(float(progress), 3))
        if status_text is not None:
            sets.append("status_text = ?")
            vals.append(status_text[:240])
        if error is not None:
            sets.append("error = ?")
            vals.append(self.storage.vault.scrub(error)[:500])
        vals.append(job_id)
        with closing(self.storage.connect()) as db, db:
            db.execute(f"UPDATE hub_jobs SET {', '.join(sets)} WHERE id = ?", vals)

    def _run_dem_job(self, job_id, layer_id, pack_key, bbox, visibility, principal):
        tmp = self.storage.work_dir / f"{layer_id}.{job_id}.mbtiles"
        try:
            self._update_job(job_id, state="running", progress=0.05, status_text=f"Streaming {pack_key} COG window")
            built = terrain_aerial.build_cog_dem_pack(
                pack_key,
                bbox,
                tmp,
                progress=lambda f: self._update_job(
                    job_id, progress=f, status_text=f"Tiling {pack_key} ({int(f * 100)}%)"
                ),
            )
            # Store in infinite Hub cache; hub-wide public COG packs are system-owned so no user key locks them.
            from hub.auth import Principal

            uploader = Principal(
                key_id="system" if visibility == "hub" else principal.key_id,
                label="Hub System" if visibility == "hub" else principal.label,
                scopes=frozenset({"admin", "read", "request_build", "upload"})
                if visibility == "hub"
                else frozenset({*principal.scopes, "upload"}),
                group_tag=principal.group_tag,
            )
            self.storage.ingest_pushed_pack(
                tmp,
                layer_id,
                {
                    "title": f"{built['title']} ({bbox[0]:.2f}–{bbox[2]:.2f}E, {bbox[1]:.2f}–{bbox[3]:.2f}N)",
                    "series": built["series"],
                    "series_id": pack_key,
                    "kind": "dem",
                    "attribution": built["attribution"],
                    "licence_policy": built["licence_policy"],
                    "visibility": visibility if visibility in ("private", "group", "hub") else "hub",
                },
                uploader,
            )
            self._update_job(job_id, state="done", progress=1.0, status_text="Ready in infinite Hub cache")
        except BaseException as e:
            tmp.unlink(missing_ok=True)
            self._update_job(job_id, state="failed", status_text="Build failed", error=str(e))
        finally:
            with self._lock:
                self._active_by_target.pop(layer_id, None)

    def submit_catalog_map(self, map_id, visibility, principal):
        """Download and build one standard catalogue map (`data/sources/map_library.json`) directly
        onto the Hub's `/data/tiles/` infinite cache."""
        principal.require("request_build")
        if not is_safe_id(map_id):
            raise HubStorageError("Invalid map id.")

        cat_lib = self._make_catalog_library(self.storage.work_dir / "cat_build")
        entries = cat_lib.catalogue_entries()
        if map_id not in entries:
            raise HubStorageError(f"Unknown standard catalogue map {map_id!r}.")
        if self.storage.tiles_path(map_id).is_file():
            row = self.storage.get_layer_row(map_id, principal)
            if row is not None:
                return {"layer_id": map_id, "state": "done", "progress": 1.0}

        vis = visibility if visibility in ("private", "group", "hub") else "hub"
        with self._lock:
            if self._stopping:
                raise HubStorageError("The Hub server is restarting; try again in a moment.")
            existing_job = self._active_by_target.get(map_id)
            if existing_job:
                return self.get_job(existing_job)
            job_id = f"job_{hashlib.sha256(f'{map_id}:{utc_now()}'.encode()).hexdigest()[:12]}"
            self._active_by_target[map_id] = job_id
            now = utc_now()
            with closing(self.storage.connect()) as db, db:
                db.execute(
                    """
                    INSERT INTO hub_jobs (id, target_id, job_kind, state, progress, status_text, requested_by, created_at, updated_at)
                    VALUES (?, ?, 'catalog_map', 'queued', 0.0, 'Queued on Hub', ?, ?, ?)
                    """,
                    (job_id, map_id, principal.key_id, now, now),
                )
            self._pool.submit(self._run_catalog_map_job, job_id, map_id, vis, principal)
        return self.get_job(job_id)

    def _run_catalog_map_job(self, job_id, map_id, visibility, principal):
        from files import delete

        from hub.auth import Principal

        scratch = self.storage.work_dir / f"cat_{map_id}_{job_id}"
        try:
            self._update_job(job_id, state="running", progress=0.05, status_text=f"Downloading & building {map_id}")
            cat_lib = self._make_catalog_library(scratch)
            entry = cat_lib.entry(map_id)
            cat_lib.add([map_id])

            def _report(j):
                p = j.progress if j.progress is not None else 0.1
                frac = 0.05 + 0.45 * p if j.state == "downloading" else 0.5 + 0.45 * p
                self._update_job(job_id, progress=frac, status_text=f"{j.state.capitalize()} {map_id}")

            cat_lib.run_pending(report=_report)
            built_mbt = cat_lib.tiles_path(map_id)
            if not built_mbt.is_file():
                j = cat_lib._jobs.get(map_id)
                raise HubStorageError(j.error if j and j.error else f"Failed building {map_id} on Hub.")

            tmeta = tiling.read_metadata(built_mbt)
            series_id = entry.get("series") or "hub"
            uploader = Principal(
                key_id="system" if visibility == "hub" else principal.key_id,
                label="Hub System" if visibility == "hub" else principal.label,
                scopes=frozenset({"admin", "read", "request_build", "upload"})
                if visibility == "hub"
                else frozenset({*principal.scopes, "upload"}),
                group_tag=principal.group_tag,
            )
            self.storage.ingest_pushed_pack(
                built_mbt,
                map_id,
                {
                    "title": entry.get("title") or tmeta.get("name") or map_id,
                    "series": cat_lib.catalogue().get("series", {}).get(series_id, {}).get("name", "Historical maps"),
                    "series_id": series_id,
                    "kind": "map",
                    "date": str(entry.get("date") or entry.get("year") or tmeta.get("date") or ""),
                    "date_note": str(tmeta.get("date_note") or ""),
                    "scale": str(entry.get("scale") or ""),
                    "source": str(entry.get("page") or (entry.get("urls") or [""])[0] or ""),
                    "attribution": str(entry.get("credit") or "Cached on Map Vietnam Hub"),
                    "licence_policy": "noncommercial" if series_id == "sgi100k" else "open",
                    "visibility": visibility,
                },
                uploader,
            )
            self._update_job(job_id, state="done", progress=1.0, status_text="Ready in infinite Hub cache")
        except BaseException as e:
            self._update_job(job_id, state="failed", status_text="Build failed", error=str(e))
        finally:
            delete(scratch)
            with self._lock:
                self._active_by_target.pop(map_id, None)

    def get_job(self, job_id):
        with closing(self.storage.connect()) as db:
            row = db.execute("SELECT * FROM hub_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["id"],
            "layer_id": row["target_id"],
            "kind": row["job_kind"],
            "state": row["state"],
            "progress": row["progress"],
            "status_text": row["status_text"],
            "error": row["error"],
            "updated_at": row["updated_at"],
        }


class HubHandler(BaseHTTPRequestHandler):
    """HTTP request handler for `/hub/v1/*`."""

    storage: HubStorage = None
    jobs: HubJobRunner = None
    hub_log: AppLogger = None

    def log_message(self, fmt, *args):
        return  # Keep stdout clean and never log URLs or headers carrying tokens.

    def send_json(self, obj, status=HTTPStatus.OK, extra_headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def read_json_body(self, max_bytes=MAX_JSON_BYTES):
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if length < 0 or length > max_bytes:
            raise ValueError("JSON request body is missing or too large.")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def require_principal(self, scope="read"):
        with closing(self.storage.connect()) as db:
            principal = authenticate(db, self.headers.get("Authorization"))
        principal.require(scope)
        return principal

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path == "/hub/v1/health":
                return self.send_json({"ok": True, "version": HUB_VERSION, "service": "map-vietnam-hub"})

            m_tile = TILE_ROUTE_RE.fullmatch(path)
            if method == "GET" and m_tile:
                principal = self.require_principal("read")
                return self.serve_tile(
                    principal,
                    m_tile.group(1),
                    int(m_tile.group(2)),
                    int(m_tile.group(3)),
                    int(m_tile.group(4)),
                    m_tile.group(5),
                )

            m_pack = PACK_ROUTE_RE.fullmatch(path)
            if method == "GET" and m_pack:
                principal = self.require_principal("read")
                return self.serve_pack(principal, m_pack.group(1))

            if method == "GET" and path == "/hub/v1/me":
                p = self.require_principal("read")
                return self.send_json(
                    {
                        "key_id": p.key_id,
                        "label": p.label,
                        "scopes": sorted(p.scopes),
                        "group_tag": p.group_tag,
                        "version": HUB_VERSION,
                        "capabilities": {**self.storage.vault.status(), "auto_gcp": True, "match_pair": True},
                        "stats": self.storage.stats(),
                    }
                )

            if method == "GET" and path == "/hub/v1/catalog":
                p = self.require_principal("read")
                return self.send_json(
                    {
                        "layers": self.storage.list_layers(p),
                        "streaming_terrain": list(terrain_aerial.STREAMING_TERRAIN_SOURCES),
                        "buildable_packs": {
                            k: {"title": v["title"], "series": v["series"], "attribution": v["attribution"]}
                            for k, v in terrain_aerial.BUILDABLE_COG_PACKS.items()
                        },
                    }
                )

            if method == "POST" and path == "/hub/v1/archives/push-pack":
                p = self.require_principal("upload")
                return self.receive_pushed_pack(p, q)

            m_layer = LAYER_ROUTE_RE.fullmatch(path)
            if m_layer:
                p = self.require_principal("upload")
                if method == "PATCH":
                    updated = self.storage.update_layer_meta(m_layer.group(1), self.read_json_body(), p)
                    if updated is None:
                        return self.send_json({"error": "Layer not found."}, HTTPStatus.NOT_FOUND)
                    return self.send_json({"layer": updated})
                if method == "DELETE":
                    deleted = self.storage.delete_layer(m_layer.group(1), p)
                    if not deleted:
                        return self.send_json({"error": "Layer not found."}, HTTPStatus.NOT_FOUND)
                    return self.send_json({"deleted": m_layer.group(1)})

            if method == "POST" and path == "/hub/v1/jobs/build-dem":
                p = self.require_principal("request_build")
                b = self.read_json_body()
                job = self.jobs.submit_dem_pack(
                    str(b.get("pack") or "gedtm30"),
                    b.get("bbox"),
                    str(b.get("visibility") or "hub"),
                    p,
                )
                return self.send_json(job, HTTPStatus.ACCEPTED if job["state"] != "done" else HTTPStatus.OK)

            if method == "POST" and path == "/hub/v1/jobs/build-map":
                p = self.require_principal("request_build")
                b = self.read_json_body()
                job = self.jobs.submit_catalog_map(
                    str(b.get("id") or "").strip(),
                    str(b.get("visibility") or "hub"),
                    p,
                )
                return self.send_json(job, HTTPStatus.ACCEPTED if job["state"] != "done" else HTTPStatus.OK)

            m_job = JOB_ROUTE_RE.fullmatch(path)
            if method == "GET" and m_job:
                self.require_principal("read")
                job = self.jobs.get_job(m_job.group(1))
                if job is None:
                    return self.send_json({"error": "No such job."}, HTTPStatus.NOT_FOUND)
                return self.send_json(job)

            if method == "GET" and path == "/hub/v1/elevation":
                p = self.require_principal("read")
                return self.serve_elevation(p, q)

            if method == "POST" and path == "/hub/v1/transect":
                p = self.require_principal("read")
                return self.serve_transect(p, self.read_json_body())

            if method == "POST" and path == "/hub/v1/auto-gcp":
                self.require_principal("read")
                b = self.read_json_body(max_bytes=12 << 20)
                scan_bytes = auto_gcp.decode_image_b64(b.get("scan_b64"))
                ref_bytes = auto_gcp.decode_image_b64(b.get("ref_b64"))
                res = auto_gcp.match_and_score(
                    scan_bytes,
                    ref_bytes,
                    b["ref_bounds"],
                    confidence_threshold=b.get("confidence_threshold", auto_gcp.DEFAULT_CONFIDENCE_THRESHOLD),
                    max_points=b.get("max_points", 12),
                    scan_target_size=b.get("scan_target_size"),
                )
                return self.send_json(res)

            if method == "POST" and path == "/hub/v1/match-pair":
                self.require_principal("read")
                b = self.read_json_body(max_bytes=12 << 20)
                res = auto_gcp.match_pair(
                    auto_gcp.decode_image_b64(b.get("a_b64")),
                    auto_gcp.decode_image_b64(b.get("b_b64")),
                    a_size=b.get("a_size"),
                    b_size=b.get("b_size"),
                )
                return self.send_json(res)

            if method == "GET" and path == "/hub/v1/lidar":
                self.require_principal("read")
                bbox = parse_bbox_param(q["bbox"])
                fc = terrain_aerial.query_sliderule_gedi(bbox, self.storage.cached_fetch)
                return self.send_json(fc)

            if method == "GET" and path.startswith("/hub/v1/aerial/"):
                self.require_principal("read")
                return self.serve_aerial(path.removeprefix("/hub/v1/aerial/"), q)

            if path == "/hub/v1/media":
                if method == "GET":
                    p = self.require_principal("read")
                    bbox = parse_bbox_param(q["bbox"]) if q.get("bbox") else None
                    return self.send_json(self.storage.query_media(p, bbox))
                if method == "POST":
                    p = self.require_principal("upload")
                    rec = self.storage.add_media(self.read_json_body(), p)
                    return self.send_json(rec, HTTPStatus.CREATED)

            m_media = MEDIA_ROUTE_RE.fullmatch(path)
            if method == "DELETE" and m_media:
                p = self.require_principal("upload")
                ok = self.storage.delete_media(m_media.group(1), p)
                if not ok:
                    return self.send_json({"error": "Media item not found."}, HTTPStatus.NOT_FOUND)
                return self.send_json({"deleted": m_media.group(1)})

            if method == "GET" and path == "/hub/v1/admin/overview":
                self.require_principal("admin")
                with closing(self.storage.connect()) as db:
                    keys = list_keys(db)
                return self.send_json(
                    {
                        "vault": self.storage.vault.admin_summary(),
                        "keys": keys,
                        "stats": self.storage.stats(),
                    }
                )

            if path == "/hub/v1/admin/proprietary":
                self.require_principal("admin")
                if method == "POST":
                    summary = self.storage.vault.upsert_proprietary_source(self.read_json_body())
                    return self.send_json({"vault": summary, "capabilities": self.storage.vault.status()})

            m_prop = PROP_ROUTE_RE.fullmatch(path)
            if method == "DELETE" and m_prop:
                self.require_principal("admin")
                summary = self.storage.vault.delete_proprietary_source(m_prop.group(1))
                return self.send_json({"vault": summary, "capabilities": self.storage.vault.status()})

            if path == "/hub/v1/admin/keys":
                p = self.require_principal("admin")
                with closing(self.storage.connect()) as db:
                    if method == "GET":
                        return self.send_json({"keys": list_keys(db)})
                    if method == "POST":
                        b = self.read_json_body()
                        created = mint_key(
                            db,
                            b.get("label", "Client key"),
                            b.get("scopes", DEFAULT_CLIENT_SCOPES),
                            b.get("group_tag", ""),
                        )
                        return self.send_json(created, HTTPStatus.CREATED)

            m_key = KEY_ROUTE_RE.fullmatch(path)
            if method == "DELETE" and m_key:
                self.require_principal("admin")
                kid = m_key.group(1)
                if kid == "adminenv":
                    raise HubStorageError(
                        "The server password comes from HUB_ADMIN_PASSWORD and cannot be revoked here."
                    )
                with closing(self.storage.connect()) as db:
                    ok = revoke_key(db, kid)
                if not ok:
                    return self.send_json({"error": "Key not found or already revoked."}, HTTPStatus.NOT_FOUND)
                return self.send_json({"revoked": kid})

            if method == "POST" and path == "/hub/v1/admin/secrets":
                self.require_principal("admin")
                b = self.read_json_body()
                self.storage.vault.set_many(b)
                return self.send_json(
                    {"capabilities": self.storage.vault.status(), "vault": self.storage.vault.admin_summary()}
                )

            if method == "GET" and path == "/hub/v1/admin/logs":
                self.require_principal("admin")
                if self.hub_log is None:
                    return self.send_json({"path": "", "lines": []})
                return self.send_json({"path": str(self.hub_log.path), "lines": self.hub_log.tail(120)})

            self.send_json({"error": "Route not found."}, HTTPStatus.NOT_FOUND)
        except AuthError as e:
            if e.status >= 403 and self.hub_log is not None:
                self.hub_log.log("WARN", f"{method} {path}", self.storage.vault.scrub(str(e)))
            self.send_json({"error": str(e)}, e.status)
        except (HubStorageError, ValueError, KeyError) as e:
            msg = f"{e.args[0]} is missing" if isinstance(e, KeyError) else str(e)
            clean_msg = self.storage.vault.scrub(msg)
            if self.hub_log is not None:
                self.hub_log.log("WARN", f"{method} {path}", clean_msg)
            self.send_json({"error": clean_msg}, HTTPStatus.BAD_REQUEST)
        except ConnectionError:
            return
        except Exception as e:
            clean_msg = self.storage.vault.scrub(f"Hub error: {e}")
            if self.hub_log is not None:
                self.hub_log.log("ERROR", f"{method} {path}", clean_msg, exc=e)
            self.send_json(
                {"error": clean_msg},
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )

    def serve_tile(self, principal, layer_id, z, x, y, ext):
        if not (0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            return self.send_json({"error": "Tile coordinates out of range."}, HTTPStatus.BAD_REQUEST)

        if layer_id.startswith("prop_"):
            body, ctype = self.storage.get_proprietary_tile(layer_id, z, x, y)
            if body is None:
                return self.send_json({"error": "Unknown proprietary source."}, HTTPStatus.NOT_FOUND)
            etag = f'"{hashlib.sha256(body).hexdigest()[:16]}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", IMMUTABLE_CACHE_CONTROL)
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)
            return

        row = self.storage.get_layer_row(layer_id, principal)
        if row is None:
            return self.send_json({"error": "Layer not found."}, HTTPStatus.NOT_FOUND)
        etag = f'"{layer_id}-r{row["revision"]}-{z}-{x}-{y}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", IMMUTABLE_CACHE_CONTROL)
            self.end_headers()
            return

        mbt_path = self.storage.tiles_path(layer_id)
        data = None
        if mbt_path.is_file():
            with contextlib.suppress(sqlite3.Error), closing(tiling.connect_read_only(mbt_path)) as db:
                data = tiling.read_tile(db, z, x, y)
        is_empty = data is None
        if is_empty:
            data = EMPTY_TERRARIUM if row["encoding"] == "terrarium" else EMPTY_WEBP
            ctype = "image/webp"
        else:
            fmt = row["format"] or ext
            ctype = "image/png" if fmt == "png" else "image/jpeg" if fmt in ("jpg", "jpeg") else "image/webp"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", IMMUTABLE_CACHE_CONTROL)
        self.send_header("ETag", etag)
        if is_empty:
            self.send_header("X-MapVietnam-Empty", "1")
        self.end_headers()
        self.wfile.write(data)

    def serve_pack(self, principal, layer_id):
        row = self.storage.get_layer_row(layer_id, principal)
        if row is None:
            return self.send_json({"error": "Layer not found."}, HTTPStatus.NOT_FOUND)
        if row["licence_policy"] == "stream_only":
            raise AuthError("This layer's licence allows streaming only, not full offline pack downloads.", status=403)
        path = self.storage.tiles_path(layer_id)
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            return self.send_json({"error": "Tile archive is missing on disk."}, HTTPStatus.NOT_FOUND)
        with f:
            total = os.fstat(f.fileno()).st_size
            if total == 0:
                return self.send_json({"error": "Tile archive is empty."}, HTTPStatus.NOT_FOUND)
            start, end = 0, total - 1
            status = HTTPStatus.OK
            range_hdr = (self.headers.get("Range") or "").strip()
            if range_hdr.startswith("bytes="):
                m = re.fullmatch(r"bytes=(\d+)-(\d*)", range_hdr)
                if not m:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                start = int(m.group(1))
                end = min(total - 1, int(m.group(2))) if m.group(2) else total - 1
                if not (0 <= start <= end < total):
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{total}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "application/vnd.sqlite3")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", IMMUTABLE_CACHE_CONTROL)
            self.send_header("ETag", f'"{layer_id}-r{row["revision"]}"')
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.end_headers()
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(UPLOAD_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def receive_pushed_pack(self, principal, q):
        """Receive a locally built `.mbtiles` pack pushed from a desktop client and cache it forever."""
        try:
            size = int(self.headers.get("Content-Length") or "")
        except ValueError:
            size = -1
        if size <= 0:
            self.close_connection = True
            raise HubStorageError("Send the MBTiles archive body with a valid Content-Length.")
        if size > MAX_HUB_UPLOAD_BYTES:
            self.close_connection = True
            return self.send_json(
                {"error": "Archive exceeds 2 GiB Hub upload limit."}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )

        meta_b64 = self.headers.get("X-MapVietnam-Meta") or ""
        info = {}
        if meta_b64:
            info = json.loads(base64.b64decode(meta_b64).decode("utf-8"))
        layer_id = str(q.get("id") or info.get("id") or "").strip()
        if not is_safe_id(layer_id):
            self.close_connection = True
            raise HubStorageError("Specify a valid map layer id (letters, digits, '_', '-' or '.', no '..').")
        if layer_id.startswith("dem_") and not principal.has_scope("admin"):
            self.close_connection = True
            raise AuthError("Layer ids starting with 'dem_' are reserved for Hub DEM builds.", status=403)
        if q.get("visibility"):
            info["visibility"] = q["visibility"]
        if q.get("title"):
            info["title"] = q["title"]

        part = self.storage.work_dir / f"push-{layer_id}-{os.getpid()}-{threading.get_ident()}.partial"
        try:
            with open(part, "wb") as out:
                left = size
                while left > 0:
                    block = self.rfile.read(min(UPLOAD_CHUNK, left))
                    if not block:
                        raise HubStorageError("Upload ended before Content-Length bytes arrived.")
                    out.write(block)
                    left -= len(block)
            layer = self.storage.ingest_pushed_pack(part, layer_id, info, principal)
        except BaseException:
            part.unlink(missing_ok=True)
            self.close_connection = True
            raise
        return self.send_json({"cached": True, "layer": layer}, HTTPStatus.CREATED)

    def _visible_dem_paths(self, principal):
        dem_paths = []
        hand_path = None
        canopy_path = None
        for lyr in self.storage.list_layers(principal):
            if lyr.get("encoding") != "terrarium":
                continue
            p = self.storage.tiles_path(lyr["id"])
            if not p.is_file():
                continue
            sid = lyr.get("seriesId") or ""
            if sid == "glo30_hand" and hand_path is None:
                hand_path = p
            elif "canopy" in sid and canopy_path is None:
                canopy_path = p
            else:
                dem_paths.append((lyr["title"], p))
        return dem_paths, hand_path, canopy_path

    def serve_elevation(self, principal, q):
        lon, lat = float(q["lon"]), float(q["lat"])
        remote = q.get("remote", "0") in ("1", "true")
        dem_paths, hand_path, canopy_path = self._visible_dem_paths(principal)

        def _cache_pt(cell_key, compute_fn):
            hit, _ = self.storage.get_cached_raw(cell_key)
            if hit is not None:
                return json.loads(hit.decode("utf-8"))
            val = compute_fn()
            self.storage.put_cached_raw(cell_key, GEDTM_URI_TAG, json.dumps(val).encode("utf-8"), "application/json")
            return val

        GEDTM_URI_TAG = terrain_aerial.GEDTM30_ELEV_COG
        res = terrain_aerial.sample_point_terrain(
            lon,
            lat,
            dem_paths=dem_paths,
            hand_path=hand_path,
            canopy_path=canopy_path,
            remote_fallback=remote,
            cache_fn=_cache_pt,
        )
        return self.send_json(res)

    def serve_transect(self, principal, body):
        dem_paths, _, canopy_path = self._visible_dem_paths(principal)
        start, end = body["start"], body["end"]
        dem_id = body.get("dem_id")
        chosen_dem = None
        if dem_id and is_safe_id(dem_id):
            row = self.storage.get_layer_row(str(dem_id), principal)
            if row and row["encoding"] == "terrarium":
                chosen_dem = self.storage.tiles_path(row["id"])
        if chosen_dem is None:
            for _, cand in dem_paths:
                b = tiling.read_bounds(cand)
                if tiling.bounds_cover(b, start[0], start[1]) and tiling.bounds_cover(b, end[0], end[1]):
                    chosen_dem = cand
                    break
        res = terrain_aerial.compute_transect(
            start,
            end,
            samples=body.get("samples", 64),
            observer_height_m=body.get("observer_height_m", 1.7),
            dem_path=chosen_dem,
            canopy_path=canopy_path,
            remote_cog=bool(body.get("remote_cog", False) and chosen_dem is None),
        )
        return self.send_json(res)

    def serve_aerial(self, provider, q):
        if provider == "nakala":
            return self.send_json(
                terrain_aerial.search_nakala_aerials(
                    q.get("q", ""),
                    int(q.get("page", 1)),
                    int(q.get("size", 12)),
                    fetch_fn=self.storage.cached_fetch,
                )
            )
        if provider == "oam":
            return self.send_json(
                terrain_aerial.search_openaerialmap(
                    parse_bbox_param(q["bbox"]),
                    int(q.get("limit", 12)),
                    fetch_fn=self.storage.cached_fetch,
                )
            )
        if provider == "landsat":
            return self.send_json(
                terrain_aerial.search_landsat_mss(
                    parse_bbox_param(q["bbox"]),
                    q.get("from", "1972-07-01"),
                    q.get("to", "1975-12-31"),
                    float(q.get("max_cloud", 35)),
                    fetch_fn=self.storage.cached_fetch,
                )
            )
        if provider == "allmaps":
            return self.send_json(
                terrain_aerial.search_allmaps_iiif(
                    parse_bbox_param(q["bbox"]),
                    fetch_fn=self.storage.cached_fetch,
                )
            )
        if provider == "usgs":
            return self.send_json(
                terrain_aerial.search_usgs_declass(
                    parse_bbox_param(q["bbox"]),
                    q.get("dataset", "corona"),
                    q.get("from", "1960-01-01"),
                    q.get("to", "1984-12-31"),
                    username=self.storage.vault.get("USGS_USERNAME"),
                    token=self.storage.vault.get("USGS_M2M_TOKEN"),
                    fetch_fn=self.storage.cached_fetch,
                )
            )
        return self.send_json({"error": f"Unknown aerial archive provider {provider!r}."}, HTTPStatus.NOT_FOUND)


class HubHTTPServer(ThreadingHTTPServer):
    """Threaded Hub HTTP server with graceful SIGTERM worker drain and SQLite WAL checkpoint."""

    daemon_threads = True

    def __init__(self, addr, handler_cls, storage: HubStorage, jobs: HubJobRunner):
        super().__init__(addr, handler_cls)
        self.storage = storage
        self.jobs = jobs
        self._stopping = False
        self._lock = threading.Lock()

    def handle_error(self, request, client_address):
        if not isinstance(sys.exception(), ConnectionError):
            super().handle_error(request, client_address)

    def stop_gracefully(self):
        with self._lock:
            if self._stopping:
                return
            self._stopping = True

        def _drain():
            try:
                self.jobs.stop()
                self.storage.clean_work()
                self.storage.checkpoint()
            finally:
                self.shutdown()

        threading.Thread(target=_drain, name="hub-stop", daemon=True).start()


def make_hub_server(
    host="0.0.0.0", port=8780, data_root=None, env=None, pacer=None, *, allow_loopback=False, app_root=None
):
    """Construct a bound `HubHTTPServer` ready for `serve_forever()`."""
    storage = HubStorage(root=data_root, env=env, pacer=pacer, allow_loopback=allow_loopback)
    jobs = HubJobRunner(storage, app_root=app_root)
    hub_log = AppLogger(storage.root / "logs" / "hub.log", echo_stderr=True)
    handler = type("BoundHubHandler", (HubHandler,), {"storage": storage, "jobs": jobs, "hub_log": hub_log})
    return HubHTTPServer((host, int(port)), handler, storage, jobs)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Map Vietnam Intermediary Hub Server and API-key CLI.")
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser("serve", help="Run the Hub HTTP server")
    p_serve.add_argument("--host", default=os.environ.get("HUB_HOST", "0.0.0.0"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", os.environ.get("HUB_PORT", "8780"))))
    p_serve.add_argument(
        "--data", default=None, help="Persistent volume path (default: $HUB_DATA_DIR or $RAILWAY_VOLUME_MOUNT_PATH)"
    )

    p_keys = sub.add_parser("keys", help="Manage client API keys on the Hub volume")
    p_keys.add_argument("--data", default=None, help="Persistent volume path")
    ksub = p_keys.add_subparsers(dest="kcmd", required=True)

    kc = ksub.add_parser("create", help="Mint a new Hub API key")
    kc.add_argument("--label", default="Client key")
    kc.add_argument("--scopes", default="read,request_build,upload")
    kc.add_argument("--group", default="")

    ksub.add_parser("list", help="List Hub API keys")

    kr = ksub.add_parser("revoke", help="Revoke a Hub API key by 8-char key_id")
    kr.add_argument("key_id")

    args = ap.parse_args(argv)
    if args.cmd in (None, "serve"):
        host = getattr(args, "host", os.environ.get("HUB_HOST", "0.0.0.0"))
        port = getattr(args, "port", int(os.environ.get("PORT", os.environ.get("HUB_PORT", "8780"))))
        data_dir = getattr(args, "data", None)
        srv = make_hub_server(host=host, port=port, data_root=data_dir)
        for sig_name in ("SIGTERM", "SIGINT", "SIGHUP"):
            if hasattr(signal, sig_name):
                signal.signal(getattr(signal, sig_name), lambda s, f: srv.stop_gracefully())
        actual_port = srv.server_address[1]
        print(f"Map Vietnam Hub listening on http://{host}:{actual_port} (volume: {srv.storage.root})", flush=True)
        srv.serve_forever()
        srv.server_close()
        return 0

    storage = HubStorage(root=args.data)
    with closing(storage.connect()) as db:
        if args.kcmd == "create":
            created = mint_key(db, args.label, args.scopes, args.group)
            print(json.dumps(created, indent=2), flush=True)
        elif args.kcmd == "list":
            print(json.dumps(list_keys(db), indent=2), flush=True)
        elif args.kcmd == "revoke":
            ok = revoke_key(db, args.key_id)
            if not ok:
                raise SystemExit(f"Key {args.key_id!r} not found or already revoked.")
            print(json.dumps({"revoked": args.key_id}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
