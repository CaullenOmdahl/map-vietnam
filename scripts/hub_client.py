"""Desktop client bridge to a Map Vietnam Intermediary Hub Server.

Keeps `HUB_URL`, `HUB_API_KEY` and `HUB_AUTO_PUSH` in the user's private `.env` file (`0600`
permissions) so the browser never handles raw tokens:
  - Streams remote Hub layers on demand while writing every received tile into a permanent
    append-only local SQLite `.mbtiles` cache (`data/hub_cache/<id>_r<rev>.mbtiles`, infinite TTL,
    never evicted unless the user deletes it).
  - Downloads complete pre-built `.mbtiles` archives from the Hub (`Keep offline`) into
    `web/tiles/<id>.mbtiles` atomically.
  - Pushes locally imported, placed, aligned, or built maps and LiDAR DEMs (`web/tiles/<id>.mbtiles`)
    back up to the Hub (`Cache to Hub`), preserving metadata, bounds, and control points so
    collaborators (or your other computers) have them forever without rebuilding.
"""

import base64
import contextlib
import json
import os
import re
import sqlite3
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from pathlib import Path

import paths
import records
import tiling
from files import delete, path_bytes, read_json
from hub.polite import SingleFlight, user_agent

UPLOAD_BLOCK = 1 << 20
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")


class HubClientError(ValueError):
    """Plain-English error talking to or configuring a Hub Server."""


def clean_hub_url(url):
    """Normalize and validate an HTTP(S) Hub base URL (stripping any trailing `/hub/v1` or `/`)."""
    raw = str(url or "").strip()
    if not raw:
        raise HubClientError("Enter the Hub server's web address (for example https://my-hub.up.railway.app).")
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HubClientError("That does not look like a valid http:// or https:// Hub address.")
    path = parsed.path.rstrip("/")
    if path.endswith("/hub/v1"):
        path = path[: -len("/hub/v1")]
    return f"{parsed.scheme}://{parsed.netloc}{path}"


class HubClient:
    """Thread-safe bridge between the local `127.0.0.1` server and a remote Intermediary Hub."""

    def __init__(self, root=None, env_file=None, env=None):
        self.root = Path(root) if root else paths.DATA_ROOT
        self.env_file = Path(env_file) if env_file else (self.root / ".env")
        self._env = os.environ if env is None else env
        self.cache_dir = self.root / "data" / "hub_cache"
        self.tiles_dir = self.root / "web" / "tiles"
        self.flight = SingleFlight()
        self._lock = threading.Lock()
        self._tile_locks = {}
        self._remote_layers = {}  # id -> layer dict from last /hub/v1/catalog fetch

    # ---- configuration (.env, owner-only 0600, never echoing secrets) ---------------------------
    def _setting(self, key):
        val = (self._env.get(key) or "").strip()
        if val:
            return val, "environment"
        file_vals = records.read_env(self.env_file)
        fval = (file_vals.get(key) or "").strip()
        return (fval, "env file") if fval else ("", None)

    def config(self):
        """Non-sensitive configuration state for Settings UI."""
        url, url_src = self._setting("HUB_URL")
        key, key_src = self._setting("HUB_API_KEY")
        auto_val, _ = self._setting("HUB_AUTO_PUSH")
        return {
            "configured": bool(url and key),
            "url": url,
            "has_key": bool(key),
            "source": key_src or url_src,
            "auto_push": auto_val.lower() in ("1", "true", "yes"),
            "cache_bytes": path_bytes(self.cache_dir) if self.cache_dir.exists() else 0,
        }

    def credentials(self):
        """Return `(hub_url, api_key)` or `("", "")` if not configured."""
        url, _ = self._setting("HUB_URL")
        key, _ = self._setting("HUB_API_KEY")
        return (url, key) if (url and key) else ("", "")

    def save_config(self, url, api_key=None, auto_push=None):
        """Verify and save Hub connection settings in `.env` (`0600` owner-only)."""
        clean_url = clean_hub_url(url)
        existing_key, _ = self._setting("HUB_API_KEY")
        chosen_key = str(api_key).strip() if api_key and str(api_key).strip() else existing_key
        if not chosen_key:
            raise HubClientError("Enter an API key for this Hub server.")
        # Probe `/hub/v1/me` before saving so a typo is caught immediately in plain English.
        me = self._request_json(clean_url, chosen_key, "/hub/v1/me", timeout=12)
        updates = {"HUB_URL": clean_url, "HUB_API_KEY": chosen_key}
        if auto_push is not None:
            updates["HUB_AUTO_PUSH"] = "true" if auto_push else "false"
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        records.write_env(self.env_file, updates)
        return me

    def set_auto_push(self, enabled):
        self.env_file.parent.mkdir(parents=True, exist_ok=True)
        records.write_env(self.env_file, {"HUB_AUTO_PUSH": "true" if enabled else "false"})

    def disconnect(self):
        """Forget saved Hub credentials in `.env` while keeping all locally cached tiles on disk."""
        if self.env_file.exists():
            records.write_env(self.env_file, {"HUB_URL": None, "HUB_API_KEY": None, "HUB_AUTO_PUSH": None})
        with self._lock:
            self._remote_layers.clear()

    # ---- HTTP calls to the Hub ------------------------------------------------------------------
    def _headers(self, api_key, extra=None):
        return {
            "Authorization": f"Bearer {api_key}",
            "User-Agent": user_agent(role="Desktop"),
            **(extra or {}),
        }

    def _request_raw(self, hub_url, api_key, route, *, method="GET", data=None, headers=None, timeout=30):
        full = f"{hub_url.rstrip('/')}{route}"
        req = urllib.request.Request(full, data=data, method=method, headers=self._headers(api_key, headers))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.headers.get("Content-Type") or ""
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(raw).get("error") or f"Hub returned HTTP {e.code}."
            except ValueError:
                msg = f"Hub returned HTTP {e.code}."
            raise HubClientError(msg) from None
        except urllib.error.URLError as e:
            raise HubClientError(f"Could not reach the Hub server ({e.reason}).") from None

    def _request_json(self, hub_url, api_key, route, *, method="GET", body=None, timeout=25):
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        hdrs = {"Content-Type": "application/json"} if payload is not None else None
        raw, _ = self._request_raw(hub_url, api_key, route, method=method, data=payload, headers=hdrs, timeout=timeout)
        return json.loads(raw.decode("utf-8"))

    def status_and_catalog(self):
        """Combined status + remote layer catalog for the viewer (`GET /api/hub`)."""
        cfg = self.config()
        url, key = self.credentials()
        if not (url and key):
            return {**cfg, "connected": False, "me": None, "layers": [], "buildable_packs": {}}
        try:
            me = self._request_json(url, key, "/hub/v1/me", timeout=10)
            cat = self._request_json(url, key, "/hub/v1/catalog", timeout=15)
        except HubClientError as e:
            return {
                **cfg,
                "connected": False,
                "error": str(e),
                "me": None,
                "layers": list(self._remote_layers.values()),
            }
        layers = cat.get("layers") or []
        installed = {p.stem for p in self.tiles_dir.glob("*.mbtiles")} if self.tiles_dir.is_dir() else set()
        for lyr in layers:
            lyr["installed_offline"] = lyr["id"] in installed
            lyr["hub_stream"] = True
        with self._lock:
            self._remote_layers = {lyr["id"]: lyr for lyr in layers}
        return {
            **cfg,
            "connected": True,
            "me": me,
            "layers": layers,
            "streaming_terrain": cat.get("streaming_terrain") or [],
            "buildable_packs": cat.get("buildable_packs") or {},
        }

    def remote_layers(self):
        """Cached dictionary of remote Hub layers `{id: layer_dict}`."""
        with self._lock:
            return dict(self._remote_layers)

    def refresh_remote_layers(self):
        url, key = self.credentials()
        if not (url and key):
            return {}
        with contextlib.suppress(HubClientError):
            cat = self._request_json(url, key, "/hub/v1/catalog", timeout=10)
            with self._lock:
                self._remote_layers = {lyr["id"]: lyr for lyr in (cat.get("layers") or [])}
        return self.remote_layers()

    # ---- Infinite Append-Only Local Tile Cache for Streamed Hub Layers --------------------------
    def _cache_mbtiles_path(self, layer_id, revision=1):
        return self.cache_dir / f"{layer_id}_r{int(revision)}.mbtiles"

    def _tile_lock(self, key):
        with self._lock:
            return self._tile_locks.setdefault(key, threading.Lock())

    def get_streamed_tile(self, layer_id, z, x, y):
        """Return `(tile_bytes, content_type)` for a Hub layer, caching every seen tile locally forever."""
        if not SAFE_ID_RE.fullmatch(str(layer_id)):
            return None, None
        url, key = self.credentials()
        if not (url and key):
            return None, None
        meta = self.remote_layers().get(layer_id)
        if meta is None:
            meta = self.refresh_remote_layers().get(layer_id)
        if meta is None:
            return None, None

        rev = int(meta.get("revision") or 1)
        fmt = str(meta.get("format") or "webp").lower()
        ctype = "image/png" if fmt == "png" else "image/jpeg" if fmt in ("jpg", "jpeg") else "image/webp"
        cache_path = self._cache_mbtiles_path(layer_id, rev)

        if cache_path.is_file():
            with contextlib.suppress(sqlite3.Error), closing(tiling.connect_read_only(cache_path)) as db:
                hit = tiling.read_tile(db, z, x, y)
                if hit is not None:
                    return hit, ctype

        def _fetch_and_append():
            if cache_path.is_file():
                with contextlib.suppress(sqlite3.Error), closing(tiling.connect_read_only(cache_path)) as db:
                    again = tiling.read_tile(db, z, x, y)
                    if again is not None:
                        return again, ctype
            body, got_ct = self._request_raw(
                url, key, f"/hub/v1/tiles/{layer_id}/{z}/{x}/{y}.{fmt}?v={rev}", timeout=20
            )
            self._append_tile(cache_path, z, x, y, body, fmt=fmt)
            return body, (got_ct or ctype)

        return self.flight.do(("hub_tile", layer_id, rev, z, x, y), _fetch_and_append)

    def _append_tile(self, cache_path, z, x, y, tile_bytes, fmt="webp"):
        lock = self._tile_lock(str(cache_path))
        tms_y = (1 << z) - 1 - y
        with lock:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(cache_path, timeout=15.0)) as db, db:
                db.execute("PRAGMA journal_mode = WAL")
                db.execute("PRAGMA synchronous = NORMAL")
                db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)")
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tiles (
                        zoom_level INTEGER,
                        tile_column INTEGER,
                        tile_row INTEGER,
                        tile_data BLOB,
                        PRIMARY KEY (zoom_level, tile_column, tile_row)
                    )
                    """
                )
                db.execute("INSERT OR IGNORE INTO metadata (name, value) VALUES ('format', ?)", (fmt,))
                db.execute(
                    "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) VALUES (?, ?, ?, ?)",
                    (z, x, tms_y, sqlite3.Binary(tile_bytes)),
                )

    # ---- Pull Full Pack Offline & Push Local Map/DEM to Hub -------------------------------------
    def pull_pack_offline(self, layer_id):
        """Download a Hub layer's complete `.mbtiles` into `web/tiles/<layer_id>.mbtiles`."""
        if not SAFE_ID_RE.fullmatch(str(layer_id)):
            raise HubClientError("Invalid layer id.")
        url, key = self.credentials()
        if not (url and key):
            raise HubClientError("Connect to a Hub server in Settings first.")
        self.tiles_dir.mkdir(parents=True, exist_ok=True)
        dest = self.tiles_dir / f"{layer_id}.mbtiles"
        part = self.tiles_dir / f"{layer_id}.mbtiles.partial"
        req = urllib.request.Request(
            f"{url.rstrip('/')}/hub/v1/packs/{layer_id}.mbtiles",
            headers=self._headers(key),
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp, open(part, "wb") as out:
                while True:
                    chunk = resp.read(UPLOAD_BLOCK)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(part, dest)
        except urllib.error.HTTPError as e:
            delete(part)
            raw = e.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(raw).get("error") or f"Hub returned HTTP {e.code}."
            except ValueError:
                msg = f"Hub returned HTTP {e.code}."
            raise HubClientError(msg) from None
        except BaseException as e:
            delete(part)
            if isinstance(e, HubClientError):
                raise
            raise HubClientError(f"Could not download offline pack ({e}).") from None
        return dest

    def push_local_layer(self, layer_id, library_obj, *, visibility="hub"):
        """Push an already-built local `.mbtiles` map or custom LiDAR DEM (plus metadata & GCPs)
        up to the Hub's infinite cache."""
        if not SAFE_ID_RE.fullmatch(str(layer_id)):
            raise HubClientError("Invalid map id.")
        url, key = self.credentials()
        if not (url and key):
            raise HubClientError("Connect to a Hub server in Settings before caching maps to it.")
        mbt_path = library_obj.tiles_path(layer_id)
        if not mbt_path.is_file():
            raise HubClientError("Build or install that map on this computer first.")

        entries = library_obj.entries()
        entry = entries.get(layer_id, {})
        tmeta = tiling.read_metadata(mbt_path)
        control = None
        if layer_id.startswith("custom_"):
            ctrl_path = library_obj.custom.control_path(layer_id)
            if ctrl_path.is_file():
                control = read_json(ctrl_path)
        elif library_obj.has_alignment(layer_id):
            align_ctrl = library_obj.alignment_control_path(layer_id)
            if align_ctrl.is_file():
                control = read_json(align_ctrl)

        series_id = entry.get("series") or "hub"
        licence_policy = "noncommercial" if series_id == "sgi100k" else "open"
        info = {
            "id": layer_id,
            "title": entry.get("title") or tmeta.get("name") or layer_id,
            "series": (
                "Your own maps (synced)"
                if entry.get("custom")
                else library_obj.catalogue().get("series", {}).get(series_id, {}).get("name", "Cached Hub maps")
            ),
            "series_id": series_id,
            "kind": entry.get("kind") or ("dem" if tmeta.get("encoding") == "terrarium" else "map"),
            "date": str(entry.get("date") or entry.get("year") or tmeta.get("date") or ""),
            "date_note": str(tmeta.get("date_note") or ""),
            "scale": str(entry.get("scale") or ""),
            "source": str(entry.get("page") or (entry.get("urls") or [""])[0] or ""),
            "attribution": str(entry.get("credit") or ("Shared custom map" if entry.get("custom") else "")),
            "licence_policy": licence_policy,
            "visibility": visibility if visibility in ("private", "group", "hub") else "hub",
            "control": control,
        }
        meta_b64 = base64.b64encode(json.dumps(info, ensure_ascii=False).encode("utf-8")).decode("ascii")
        total = mbt_path.stat().st_size
        qs = urllib.parse.urlencode({"id": layer_id, "visibility": info["visibility"]})
        full_url = f"{url.rstrip('/')}/hub/v1/archives/push-pack?{qs}"

        # Stream file bytes using http.client so multi-hundred-MB MBTiles archives use ~1 MB RAM.
        parsed = urllib.parse.urlparse(full_url)
        import http.client

        conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = conn_cls(parsed.netloc, timeout=180)
        try:
            conn.putrequest("POST", f"{parsed.path}?{parsed.query}")
            for hk, hv in self._headers(
                key,
                {
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(total),
                    "X-MapVietnam-Meta": meta_b64,
                },
            ).items():
                conn.putheader(hk, hv)
            conn.endheaders()
            with open(mbt_path, "rb") as f:
                while True:
                    chunk = f.read(UPLOAD_BLOCK)
                    if not chunk:
                        break
                    conn.send(chunk)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status not in (200, 201):
                try:
                    msg = json.loads(raw).get("error") or f"Hub returned HTTP {resp.status}."
                except ValueError:
                    msg = f"Hub returned HTTP {resp.status}."
                raise HubClientError(msg)
            result = json.loads(raw)
        except OSError as e:
            raise HubClientError(f"Upload to Hub failed ({e}).") from None
        finally:
            conn.close()

        self.refresh_remote_layers()
        return result

    # ---- Hub-proxied Terrain, LiDAR & Aerial Discovery ------------------------------------------
    def request_hub_dem_build(self, pack_key, bbox, visibility="hub"):
        url, key = self.credentials()
        if not (url and key):
            raise HubClientError("Not connected to a Hub server.")
        return self._request_json(
            url,
            key,
            "/hub/v1/jobs/build-dem",
            method="POST",
            body={"pack": pack_key, "bbox": bbox, "visibility": visibility},
        )

    def poll_hub_job(self, job_id):
        url, key = self.credentials()
        if not (url and key):
            raise HubClientError("Not connected to a Hub server.")
        return self._request_json(url, key, f"/hub/v1/jobs/{urllib.parse.quote(str(job_id))}")

    def proxy_get(self, route, params=None):
        url, key = self.credentials()
        if not (url and key):
            return None
        qs = f"?{urllib.parse.urlencode(params)}" if params else ""
        return self._request_json(url, key, f"{route}{qs}", timeout=30)

    def proxy_post(self, route, body):
        url, key = self.credentials()
        if not (url and key):
            return None
        return self._request_json(url, key, route, method="POST", body=body, timeout=30)
