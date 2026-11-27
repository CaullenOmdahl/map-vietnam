"""Infinite write-once storage and metadata database for the Map Vietnam Hub.

Everything written by the Hub lives under one directory (`HUB_DATA_DIR`, falling back to Railway's
`RAILWAY_VOLUME_MOUNT_PATH`, or `data/hub_server` locally):
  db/hub.sqlite            WAL-mode SQLite database (keys, vault, cache index, layers, media, jobs)
  cache/raw/<sha256>       content-addressed upstream responses — write-once, infinite TTL, no eviction
  tiles/<id>.mbtiles       finished map, aerial, canopy and lossless Terrarium DEM tile archives
  archives/<id>/           uploaded raw files, working images and control-point JSONs
  work/                    temporary `.partial` files cleaned up on startup and SIGTERM

Old maps, historical aerial photographs, declassified satellite strips and bare-earth elevation
models do not change: once cached or pushed to the Hub, they remain on disk permanently unless an
administrator explicitly deletes an item.
"""

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import paths
import tiling
from files import delete

from hub.auth import AuthError, UpstreamVault, ensure_bootstrap_admin, init_auth_tables, utc_now
from hub.polite import HostPacer, SingleFlight, polite_fetch_bytes

LAYER_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
VISIBILITIES = frozenset({"private", "group", "hub"})
LICENCE_POLICIES = frozenset({"open", "noncommercial", "private_vault", "stream_only"})
MAX_HUB_UPLOAD_BYTES = 2 << 30  # 2 GiB


class HubStorageError(ValueError):
    """Invalid upload, damaged MBTiles pack, or disallowed layer operation; str() is plain English."""


def resolve_hub_root(root=None, env=None):
    """Resolve the persistent volume directory for the Hub."""
    if root is not None:
        return Path(root).resolve()
    env = os.environ if env is None else env
    configured = (env.get("HUB_DATA_DIR") or env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (paths.DATA_ROOT / "data" / "hub_server").resolve()


class HubStorage:
    """Thread-safe manager for the Hub's persistent volume, infinite cache, and layer store."""

    def __init__(self, root=None, env=None, pacer=None):
        self.env = os.environ if env is None else env
        self.root = resolve_hub_root(root, self.env)
        self.db_dir = self.root / "db"
        self.raw_dir = self.root / "cache" / "raw"
        self.tiles_dir = self.root / "tiles"
        self.archives_dir = self.root / "archives"
        self.work_dir = self.root / "work"
        for folder in (self.db_dir, self.raw_dir, self.tiles_dir, self.archives_dir, self.work_dir):
            folder.mkdir(parents=True, exist_ok=True)

        self.db_path = self.db_dir / "hub.sqlite"
        self.pacer = pacer or HostPacer()
        self.flight = SingleFlight()
        self._tile_write_locks = {}
        self._guard = threading.Lock()
        self._init_db()
        self.vault = UpstreamVault(self.connect, env=self.env)
        self.clean_work()

    def connect(self):
        """Open a WAL-mode SQLite connection to `hub.sqlite` with busy timeout."""
        db = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 10000")
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _init_db(self):
        with closing(self.connect()) as db, db:
            init_auth_tables(db)
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_cache (
                    cache_key TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_layers (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    series TEXT NOT NULL DEFAULT '',
                    series_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'map',
                    encoding TEXT NOT NULL DEFAULT '',
                    format TEXT NOT NULL DEFAULT 'webp',
                    bounds_json TEXT NOT NULL,
                    minzoom INTEGER NOT NULL DEFAULT 0,
                    maxzoom INTEGER NOT NULL DEFAULT 15,
                    date TEXT NOT NULL DEFAULT '',
                    date_note TEXT NOT NULL DEFAULT '',
                    scale TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    attribution TEXT NOT NULL DEFAULT '',
                    licence_policy TEXT NOT NULL DEFAULT 'open',
                    visibility TEXT NOT NULL DEFAULT 'hub',
                    owner_key_id TEXT NOT NULL DEFAULT '',
                    group_tag TEXT NOT NULL DEFAULT '',
                    revision INTEGER NOT NULL DEFAULT 1,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    control_json TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_media (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    source_record_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    date TEXT NOT NULL DEFAULT '',
                    lon REAL NOT NULL,
                    lat REAL NOT NULL,
                    uncertainty_m REAL NOT NULL,
                    azimuth_deg REAL,
                    fov_deg REAL,
                    image_url TEXT NOT NULL,
                    plot_url TEXT NOT NULL DEFAULT '',
                    licence TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'hub',
                    owner_key_id TEXT NOT NULL DEFAULT '',
                    group_tag TEXT NOT NULL DEFAULT '',
                    meta_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS hub_jobs (
                    id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    job_kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    progress REAL,
                    status_text TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    requested_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            ensure_bootstrap_admin(db, env=self.env)

    def clean_work(self):
        """Remove any leftover `.partial` or `.part` scratch files from an interrupted build or deploy."""
        for folder in (self.work_dir, self.tiles_dir, self.raw_dir):
            if folder.is_dir():
                for p in folder.glob("*.part*"):
                    delete(p)

    def checkpoint(self):
        """Flush SQLite WAL cleanly during SIGTERM shutdown before Railway detaches the volume."""
        with contextlib.suppress(sqlite3.Error), closing(self.connect()) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def allow_noncommercial(self):
        """True unless the operator explicitly set `ALLOW_NC_SOURCES=false`."""
        return (self.env.get("ALLOW_NC_SOURCES") or "true").strip().lower() not in ("0", "false", "no")

    # ---- infinite write-once raw upstream cache -------------------------------------------------
    def raw_blob_path(self, sha256_hex):
        return self.raw_dir / sha256_hex[:2] / sha256_hex

    def get_cached_raw(self, cache_key):
        """Return `(bytes, mime_type)` if `cache_key` is already in the infinite cache, else `(None, None)`."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT sha256, mime_type FROM raw_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return None, None
        blob = self.raw_blob_path(row["sha256"])
        if not blob.is_file():
            return None, None
        return blob.read_bytes(), row["mime_type"]

    def put_cached_raw(self, cache_key, url, body_bytes, mime_type="application/octet-stream"):
        """Store `body_bytes` permanently by SHA-256 and bind `cache_key` to it."""
        sha = hashlib.sha256(body_bytes).hexdigest()
        dest = self.raw_blob_path(sha)
        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            part = self.work_dir / f"raw-{sha}-{os.getpid()}-{threading.get_ident()}.partial"
            try:
                part.write_bytes(body_bytes)
                os.replace(part, dest)
            finally:
                part.unlink(missing_ok=True)
        with closing(self.connect()) as db, db:
            db.execute(
                """
                INSERT INTO raw_cache (cache_key, url, sha256, mime_type, size_bytes, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO NOTHING
                """,
                (cache_key, url, sha, mime_type, len(body_bytes), utc_now()),
            )
        return sha

    def cached_fetch(self, url, *, cache_key=None, headers=None, data=None, method=None, timeout=30):
        """Return `(bytes, mime_type)` from the infinite cache, fetching politely at most once ever."""
        if cache_key is None:
            body_hash = hashlib.sha256(data).hexdigest()[:16] if data else ""
            cache_key = f"{method or ('POST' if data else 'GET')}:{url}:{body_hash}"
        hit, mime = self.get_cached_raw(cache_key)
        if hit is not None:
            return hit, mime

        def _fetch_and_store():
            again, again_mime = self.get_cached_raw(cache_key)
            if again is not None:
                return again, again_mime
            body, ctype = polite_fetch_bytes(
                url,
                pacer=self.pacer,
                headers=headers,
                data=data,
                method=method,
                contact=self.vault.get("MAPVIETNAM_CONTACT"),
                timeout=timeout,
            )
            self.put_cached_raw(cache_key, url, body, ctype)
            return body, ctype

        return self.flight.do(("raw", cache_key), _fetch_and_store)

    # ---- visibility & permissions ---------------------------------------------------------------
    def can_view(self, row, principal):
        """True when `principal` is allowed to see and read `row` (`hub_layers` or `hub_media`)."""
        policy = row["licence_policy"] if "licence_policy" in row.keys() else "open"
        if policy == "noncommercial" and not self.allow_noncommercial():
            return False
        if principal.has_scope("admin"):
            return True
        vis = row["visibility"]
        if vis == "hub":
            return True
        if row["owner_key_id"] and row["owner_key_id"] == principal.key_id:
            return True
        return bool(vis == "group" and principal.group_tag and principal.group_tag == row["group_tag"])

    def can_write_layer(self, existing_row, principal, visibility):
        """Check whether `principal` may create or update a layer on the Hub."""
        principal.require("upload")
        if existing_row is None:
            return True
        if principal.has_scope("admin"):
            return True
        if existing_row["owner_key_id"] and existing_row["owner_key_id"] == principal.key_id:
            return True
        # Allow any uploader to refresh a shared hub-wide layer without a private owner lock only if admin;
        # otherwise require ownership so one client cannot overwrite another client's map.
        raise AuthError(
            f"Layer '{existing_row['id']}' already exists on this Hub and belongs to another key.",
            status=403,
        )

    # ---- MBTiles layer storage & bidirectional Client -> Hub cache push -------------------------
    def tiles_path(self, layer_id):
        if not LAYER_ID_RE.fullmatch(str(layer_id)):
            raise HubStorageError("Invalid layer id.")
        return self.tiles_dir / f"{layer_id}.mbtiles"

    def inspect_mbtiles(self, path):
        """Validate an MBTiles SQLite file and return its normalized metadata dict."""
        path = Path(path)
        if not path.is_file() or path.stat().st_size < 512:
            raise HubStorageError("The tile pack is empty or missing.")
        try:
            with closing(tiling.connect_read_only(path)) as db:
                meta = dict(db.execute("SELECT name, value FROM metadata").fetchall())
                count_row = db.execute("SELECT COUNT(*) FROM tiles").fetchone()
                tile_count = int(count_row[0]) if count_row else 0
                sample = db.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()
        except sqlite3.Error as e:
            raise HubStorageError(f"Not a valid MBTiles SQLite archive ({e}).") from None
        if tile_count < 1 or not sample or not sample[0]:
            raise HubStorageError("The MBTiles file contains no map tiles.")
        try:
            bounds = [float(v) for v in str(meta.get("bounds", "")).split(",")]
        except ValueError:
            bounds = []
        if len(bounds) != 4 or not (bounds[0] < bounds[2] and bounds[1] < bounds[3]):
            raise HubStorageError("The MBTiles metadata is missing valid west,south,east,north bounds.")
        minzoom = max(0, min(24, int(meta.get("minzoom", 0))))
        maxzoom = max(minzoom, min(24, int(meta.get("maxzoom", 15))))
        fmt = str(meta.get("format") or "webp").lower()
        if fmt not in ("webp", "png", "jpg", "jpeg"):
            raise HubStorageError(f"Unsupported tile format {fmt!r} in MBTiles.")
        return {
            "meta": meta,
            "tile_count": tile_count,
            "bounds": bounds,
            "minzoom": minzoom,
            "maxzoom": maxzoom,
            "format": "jpg" if fmt == "jpeg" else fmt,
            "encoding": str(meta.get("encoding") or ""),
        }

    def ingest_pushed_pack(self, temp_file, layer_id, info, principal):
        """Validate `temp_file` as an MBTiles archive and store it permanently in `/data/tiles/`."""
        if not LAYER_ID_RE.fullmatch(str(layer_id)):
            raise HubStorageError("Layer id may contain only letters, numbers, '_', '-' and '.'.")
        visibility = str(info.get("visibility") or "hub").lower()
        if visibility not in VISIBILITIES:
            raise HubStorageError(f"Visibility must be one of {', '.join(sorted(VISIBILITIES))}.")
        licence_policy = str(info.get("licence_policy") or "open").lower()
        if licence_policy not in LICENCE_POLICIES:
            licence_policy = "open"

        inspected = self.inspect_mbtiles(temp_file)
        mmeta = inspected["meta"]
        size_bytes = Path(temp_file).stat().st_size
        dest = self.tiles_path(layer_id)

        with closing(self.connect()) as db:
            existing = db.execute("SELECT * FROM hub_layers WHERE id = ?", (layer_id,)).fetchone()
            self.can_write_layer(existing, principal, visibility)
            revision = (int(existing["revision"]) + 1) if existing else 1
            owner = existing["owner_key_id"] if existing else principal.key_id
            group_tag = str(info.get("group_tag") or principal.group_tag or "")
            title = " ".join(str(info.get("title") or mmeta.get("name") or layer_id).split())[:200]
            series = str(info.get("series") or "Shared Hub maps")[:160]
            series_id = str(info.get("series_id") or info.get("seriesId") or "hub")[:64]
            kind = str(info.get("kind") or ("dem" if inspected["encoding"] == "terrarium" else "map"))[:32]
            date = str(info.get("date") or mmeta.get("date") or "")[:32]
            date_note = str(info.get("date_note") or info.get("dateNote") or mmeta.get("date_note") or "")[:500]
            scale = str(info.get("scale") or "")[:40]
            source_url = str(info.get("source") or info.get("source_url") or "")[:500]
            attribution = str(info.get("attribution") or "Cached on Map Vietnam Hub")[:400]
            control_json = json.dumps(info["control"]) if isinstance(info.get("control"), (dict, list)) else ""
            extra_meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}

            os.replace(temp_file, dest)
            now = utc_now()
            with db:
                db.execute(
                    """
                    INSERT INTO hub_layers (
                        id, title, series, series_id, kind, encoding, format, bounds_json,
                        minzoom, maxzoom, date, date_note, scale, source_url, attribution,
                        licence_policy, visibility, owner_key_id, group_tag, revision,
                        size_bytes, control_json, meta_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        series = excluded.series,
                        series_id = excluded.series_id,
                        kind = excluded.kind,
                        encoding = excluded.encoding,
                        format = excluded.format,
                        bounds_json = excluded.bounds_json,
                        minzoom = excluded.minzoom,
                        maxzoom = excluded.maxzoom,
                        date = excluded.date,
                        date_note = excluded.date_note,
                        scale = excluded.scale,
                        source_url = excluded.source_url,
                        attribution = excluded.attribution,
                        licence_policy = excluded.licence_policy,
                        visibility = excluded.visibility,
                        group_tag = excluded.group_tag,
                        revision = excluded.revision,
                        size_bytes = excluded.size_bytes,
                        control_json = excluded.control_json,
                        meta_json = excluded.meta_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        layer_id,
                        title,
                        series,
                        series_id,
                        kind,
                        inspected["encoding"],
                        inspected["format"],
                        json.dumps(inspected["bounds"]),
                        inspected["minzoom"],
                        inspected["maxzoom"],
                        date,
                        date_note,
                        scale,
                        source_url,
                        attribution,
                        licence_policy,
                        visibility,
                        owner,
                        group_tag,
                        revision,
                        size_bytes,
                        control_json,
                        json.dumps(extra_meta),
                        now,
                        now,
                    ),
                )
            row = db.execute("SELECT * FROM hub_layers WHERE id = ?", (layer_id,)).fetchone()
        return self.layer_to_dict(row)

    def layer_to_dict(self, row):
        """Serialize one `hub_layers` row for `/hub/v1/catalog`."""
        ext = row["format"] or "webp"
        return {
            "id": row["id"],
            "title": row["title"],
            "series": row["series"],
            "seriesId": row["series_id"],
            "kind": row["kind"],
            "encoding": row["encoding"] or None,
            "format": ext,
            "bounds": json.loads(row["bounds_json"]),
            "minzoom": row["minzoom"],
            "maxzoom": row["maxzoom"],
            "date": row["date"],
            "dateNote": row["date_note"],
            "scale": row["scale"],
            "source": row["source_url"],
            "attribution": row["attribution"],
            "licence_policy": row["licence_policy"],
            "stream_only": row["licence_policy"] == "stream_only",
            "visibility": row["visibility"],
            "owner_key_id": row["owner_key_id"],
            "group_tag": row["group_tag"],
            "revision": row["revision"],
            "size_bytes": row["size_bytes"],
            "tiles": f"/hub/v1/tiles/{row['id']}/{{z}}/{{x}}/{{y}}.{ext}?v={row['revision']}",
            "updated_at": row["updated_at"],
        }

    def list_layers(self, principal):
        """All layers visible to `principal` (cached/uploaded MBTiles + configured proprietary feeds)."""
        with closing(self.connect()) as db:
            rows = db.execute("SELECT * FROM hub_layers ORDER BY date = '', date, id").fetchall()
        layers = [
            self.layer_to_dict(r) for r in rows if self.can_view(r, principal) and self.tiles_path(r["id"]).is_file()
        ]
        for prop in self.vault.proprietary_sources():
            pid = f"prop_{re.sub(r'[^A-Za-z0-9_.-]', '_', str(prop['id']))}"
            fmt = str(prop.get("format") or "webp").lower()
            layers.append(
                {
                    "id": pid,
                    "title": str(prop.get("title") or pid),
                    "series": str(prop.get("series") or "Hub Proprietary Sources"),
                    "seriesId": "proprietary",
                    "kind": "dem" if prop.get("encoding") == "terrarium" else "map",
                    "encoding": prop.get("encoding") or None,
                    "format": fmt,
                    "bounds": prop.get("bounds") or [102.0, 8.0, 110.5, 23.6],
                    "minzoom": int(prop.get("minzoom", 0)),
                    "maxzoom": int(prop.get("maxzoom", 18)),
                    "date": str(prop.get("date") or ""),
                    "dateNote": str(prop.get("dateNote") or ""),
                    "scale": "",
                    "source": "",
                    "attribution": str(prop.get("attribution") or "Proprietary Hub Source"),
                    "licence_policy": "stream_only" if prop.get("stream_only", True) else "private_vault",
                    "stream_only": bool(prop.get("stream_only", True)),
                    "visibility": "hub",
                    "owner_key_id": "vault",
                    "group_tag": "",
                    "revision": 1,
                    "size_bytes": 0,
                    "tiles": f"/hub/v1/tiles/{pid}/{{z}}/{{x}}/{{y}}.{fmt}?v=1",
                    "updated_at": "",
                }
            )
        return layers

    def get_layer_row(self, layer_id, principal):
        """Return the `hub_layers` row for `layer_id` if visible to `principal`, else None."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM hub_layers WHERE id = ?", (layer_id,)).fetchone()
        if row is None:
            return None
        if not self.can_view(row, principal):
            raise AuthError("This API key does not have access to that layer.", status=403)
        return row

    def delete_layer(self, layer_id, principal):
        """Remove a layer from the Hub (allowed only for its owner or an `admin` key)."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM hub_layers WHERE id = ?", (layer_id,)).fetchone()
            if row is None:
                return False
            if not (principal.has_scope("admin") or (row["owner_key_id"] and row["owner_key_id"] == principal.key_id)):
                raise AuthError("Only the key that uploaded this layer (or an admin) can delete it.", status=403)
            with db:
                db.execute("DELETE FROM hub_layers WHERE id = ?", (layer_id,))
        delete(self.tiles_path(layer_id))
        delete(self.archives_dir / layer_id)
        return True

    # ---- infinite append-only tile cache for proprietary / proxied tile sources ----------------
    def _tile_lock(self, layer_id):
        with self._guard:
            return self._tile_write_locks.setdefault(layer_id, threading.Lock())

    def append_cached_tile(self, cache_path, z, x, y, tile_bytes, fmt="webp"):
        """Append one tile permanently to an SQLite `.mbtiles` cache file (never evicting)."""
        cache_path = Path(cache_path)
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

    def get_proprietary_tile(self, prop_layer_id, z, x, y):
        """Read or fetch-once-and-cache-forever a tile from an operator-configured proprietary feed."""
        raw_id = prop_layer_id.removeprefix("prop_")
        spec = next(
            (s for s in self.vault.proprietary_sources() if re.sub(r"[^A-Za-z0-9_.-]", "_", str(s["id"])) == raw_id),
            None,
        )
        if spec is None:
            return None, None
        fmt = str(spec.get("format") or "webp").lower()
        ctype = "image/png" if fmt == "png" else "image/jpeg" if fmt in ("jpg", "jpeg") else "image/webp"
        cache_mbtiles = self.tiles_dir / f"{prop_layer_id}_cache.mbtiles"
        if cache_mbtiles.is_file():
            with contextlib.suppress(sqlite3.Error), closing(tiling.connect_read_only(cache_mbtiles)) as db:
                hit = tiling.read_tile(db, z, x, y)
                if hit is not None:
                    return hit, ctype

        url = str(spec["tiles"]).replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))

        def _fetch():
            if cache_mbtiles.is_file():
                with contextlib.suppress(sqlite3.Error), closing(tiling.connect_read_only(cache_mbtiles)) as db:
                    again = tiling.read_tile(db, z, x, y)
                    if again is not None:
                        return again, ctype
            body, _ = polite_fetch_bytes(
                url,
                pacer=self.pacer,
                contact=self.vault.get("MAPVIETNAM_CONTACT"),
                timeout=20,
            )
            self.append_cached_tile(cache_mbtiles, z, x, y, body, fmt=fmt)
            return body, ctype

        return self.flight.do(("prop_tile", prop_layer_id, z, x, y), _fetch)

    # ---- geolocated oblique media & flight-plot cards ------------------------------------------
    def add_media(self, item, principal):
        """Insert or update a geolocated photo/oblique aerial item in `hub_media`."""
        principal.require("upload")
        media_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(item.get("id") or ""))[:96]
        if not media_id:
            media_id = f"media_{hashlib.sha256(os.urandom(16)).hexdigest()[:12]}"
        lon, lat = float(item["lon"]), float(item["lat"])
        unc = float(item.get("uncertainty_m", 100.0))
        if unc <= 0 or not (-180 <= lon <= 180 and -85 <= lat <= 85):
            raise HubStorageError("Media coordinates must lie on the map and carry uncertainty_m > 0.")
        visibility = str(item.get("visibility") or "hub").lower()
        if visibility not in VISIBILITIES:
            visibility = "hub"
        az = float(item["azimuth_deg"]) if item.get("azimuth_deg") is not None else None
        fov = float(item["fov_deg"]) if item.get("fov_deg") is not None else None
        now = utc_now()
        with closing(self.connect()) as db, db:
            existing = db.execute("SELECT owner_key_id FROM hub_media WHERE id = ?", (media_id,)).fetchone()
            if existing and existing["owner_key_id"] != principal.key_id and not principal.has_scope("admin"):
                raise AuthError("Another key owns that media record.", status=403)
            db.execute(
                """
                INSERT INTO hub_media (
                    id, source, source_record_id, title, date, lon, lat, uncertainty_m,
                    azimuth_deg, fov_deg, image_url, plot_url, licence, visibility,
                    owner_key_id, group_tag, meta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    date = excluded.date,
                    lon = excluded.lon,
                    lat = excluded.lat,
                    uncertainty_m = excluded.uncertainty_m,
                    azimuth_deg = excluded.azimuth_deg,
                    fov_deg = excluded.fov_deg,
                    image_url = excluded.image_url,
                    plot_url = excluded.plot_url,
                    licence = excluded.licence,
                    visibility = excluded.visibility
                """,
                (
                    media_id,
                    str(item.get("source") or "user_archive")[:64],
                    str(item.get("source_record_id") or media_id)[:120],
                    str(item.get("title") or "Aerial photo")[:200],
                    str(item.get("date") or "")[:32],
                    lon,
                    lat,
                    unc,
                    az,
                    fov,
                    str(item["image_url"])[:800],
                    str(item.get("plot_url") or "")[:800],
                    str(item.get("licence") or "")[:200],
                    visibility,
                    principal.key_id,
                    principal.group_tag,
                    json.dumps(item.get("meta") or {}),
                    now,
                ),
            )
            row = db.execute("SELECT * FROM hub_media WHERE id = ?", (media_id,)).fetchone()
        return dict(row)

    def query_media(self, principal, bbox=None):
        """GeoJSON FeatureCollection of visible `hub_media` items inside optional `[w, s, e, n]`."""
        sql = "SELECT * FROM hub_media"
        params = []
        if bbox is not None:
            w, s, e, n = bbox
            sql += " WHERE lon BETWEEN ? AND ? AND lat BETWEEN ? AND ?"
            params = [w, e, s, n]
        sql += " ORDER BY date DESC, id LIMIT 1000"
        with closing(self.connect()) as db:
            rows = db.execute(sql, params).fetchall()
        features = []
        for r in rows:
            if not self.can_view(r, principal):
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                    "properties": {
                        "id": r["id"],
                        "source": r["source"],
                        "title": r["title"],
                        "date": r["date"],
                        "uncertainty_m": r["uncertainty_m"],
                        "azimuth_deg": r["azimuth_deg"],
                        "fov_deg": r["fov_deg"],
                        "image_url": r["image_url"],
                        "plot_url": r["plot_url"],
                        "licence": r["licence"],
                        "visibility": r["visibility"],
                    },
                }
            )
        return {"type": "FeatureCollection", "features": features}

    def stats(self):
        """Disk and object counts on the Hub's persistent volume."""
        with closing(self.connect()) as db:
            raw_count, raw_bytes = db.execute("SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM raw_cache").fetchone()
            layer_count, layer_bytes = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(size_bytes), 0) FROM hub_layers"
            ).fetchone()
            media_count = db.execute("SELECT COUNT(*) FROM hub_media").fetchone()[0]
        return {
            "volume_path": str(self.root),
            "cache_policy": "infinite_write_once",
            "raw_blobs": int(raw_count),
            "raw_bytes": int(raw_bytes),
            "layers": int(layer_count),
            "layer_bytes": int(layer_bytes),
            "media_items": int(media_count),
        }
