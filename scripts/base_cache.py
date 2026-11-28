"""Offline base-map tile and font-glyph cache for Map Vietnam.

Stores background street (OpenStreetMap) and satellite (Esri World Imagery) XYZ raster tiles plus
MapLibre PBF font glyphs in a local SQLite database under data/work/base_cache.sqlite with LRU
eviction up to a configurable max_bytes cap.

Supports:
  * Cache-as-you-browse (enabled/disabled via base_cache.json): tiles the person looks at are kept,
    so visited places still draw without internet.
  * Pre-caching a bounding box in a cancellable background thread, only for tile sources whose
    usage policy allows it (`"prefetch": True`). Neither public source here does: the OpenStreetMap
    tile usage policy and Esri's World Imagery terms both forbid bulk downloading, so area
    pre-caching is refused for them.
  * Serving cached tiles/glyphs cleanly when offline (returning (None, None) / None on miss).
"""

import json
import math
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from hub.polite import tls_context

BASES = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "ctype": "image/png",
        "maxzoom": 19,
        "est_bytes": 22_000,
        "prefetch": False,  # https://operations.osmfoundation.org/policies/tiles/ forbids bulk downloading
    },
    "sat": {
        "url": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        "ctype": "image/jpeg",
        "maxzoom": 19,
        "est_bytes": 26_000,
        "prefetch": False,  # Esri's terms do not allow bulk or offline export of the public basemap
    },
}
PREFETCH_REFUSED = (
    "The street and satellite map servers' usage policies don't allow downloading areas ahead of time. "
    "Places you view are kept automatically while 'Keep tiles you view' is on."
)
GLYPH_URL = "https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf"
VIETNAM_BOUNDS = (102.0, 8.1, 109.6, 23.45)  # [west, south, east, north]
DEFAULT_MAX_BYTES = 1024 * 1024 * 1024  # 1 GiB
MIN_MAX_BYTES = 50 * 1024 * 1024
MAX_MAX_BYTES = 20 * 1024 * 1024 * 1024
MAX_PRECACHE_TILES = 750_000
PRECACHE_DELAY_S = 0.08
FETCH_TIMEOUT_S = 10
USER_AGENT = "MapVietnam/0.3 (+https://github.com/CaullenOmdahl/map-vietnam)"
_TLS = tls_context()  # certifi CA bundle: the frozen macOS app has no system CA path

FONTSTACK_RE = re.compile(r"^[A-Za-z0-9 _.,-]{1,120}$")
RANGE_RE = re.compile(r"^\d+-\d+$")

SCHEMA = """
create table if not exists tiles (
  base text not null,
  z integer not null,
  x integer not null,
  y integer not null,
  ctype text not null,
  data blob not null,
  size integer not null,
  fetched_at real not null,
  last_used real not null,
  primary key (base, z, x, y)
);
create index if not exists tiles_lru on tiles(last_used);
create table if not exists glyphs (
  fontstack text not null,
  range_name text not null,
  data blob not null,
  fetched_at real not null,
  primary key (fontstack, range_name)
);
"""


def lonlat_to_tile(lon, lat, z):
    """Web Mercator XYZ tile (x, y) containing (lon, lat) at zoom `z`."""
    n = 1 << z
    lat_c = max(-85.05112878, min(85.05112878, float(lat)))
    lon_c = max(-180.0, min(180.0, float(lon)))
    x = int((lon_c + 180.0) / 360.0 * n)
    lat_r = math.radians(lat_c)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def tile_ranges(bounds, min_z, max_z):
    """List of (z, x0, x1, y0, y1) inclusive tile index ranges covering `bounds` across zooms."""
    w, s, e, n = bounds
    out = []
    for z in range(min_z, max_z + 1):
        x0, y1 = lonlat_to_tile(w, s, z)
        x1, y0 = lonlat_to_tile(e, n, z)
        out.append((z, min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)))
    return out


def count_tiles(bounds, min_z, max_z):
    """Total number of tiles covered by `bounds` from `min_z` through `max_z`."""
    return sum((x1 - x0 + 1) * (y1 - y0 + 1) for _, x0, x1, y0, y1 in tile_ranges(bounds, min_z, max_z))


def iter_tiles(bounds, min_z, max_z):
    """Yield (z, x, y) for every tile covered by `bounds` from `min_z` through `max_z`."""
    for z, x0, x1, y0, y1 in tile_ranges(bounds, min_z, max_z):
        for x in range(x0, x1 + 1):
            for y in range(y0, y1 + 1):
                yield z, x, y


def _default_fetch(url):
    """Fetch `url` -> (bytes, content_type) or raise OSError/URLError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S, context=_TLS) as resp:
        if resp.status != 200:
            raise urllib.error.HTTPError(url, resp.status, "unexpected status", resp.headers, None)
        return resp.read(), (resp.headers.get("Content-Type") or "").split(";")[0].strip()


def _normalize_bases(bases):
    if not isinstance(bases, list) or not bases or not all(isinstance(b, str) and b in BASES for b in bases):
        raise ValueError("bases must be a non-empty list of 'osm' and/or 'sat'")
    seen = []
    for b in bases:
        if b not in seen:
            seen.append(b)
    return seen


def _normalize_bounds(bounds):
    if bounds == "vietnam":
        return VIETNAM_BOUNDS, True
    if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
        raise ValueError("bounds must be 'vietnam' or [west, south, east, north]")
    w, s, e, n = (float(v) for v in bounds)
    if not all(math.isfinite(v) for v in (w, s, e, n)):
        raise ValueError("bounds must be finite numbers")
    if not (-180 <= w < e <= 180 and -85.0511 <= s < n <= 85.0511):
        raise ValueError("bounds must have west < east and south < north within map limits")
    return (w, s, e, n), False


class BaseCache:
    """Local SQLite cache for background map tiles and font glyphs."""

    def __init__(self, root):
        self.root = Path(root)
        self.work_dir = self.root / "data" / "work"
        self.db_path = self.work_dir / "base_cache.sqlite"
        self.config_path = self.work_dir / "base_cache.json"
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker = None
        self._job = self._idle_job()
        self._config = self._load_config()
        self._init_db()

    @staticmethod
    def _idle_job():
        return {
            "state": "idle",
            "label": "",
            "bases": [],
            "min_z": 0,
            "max_z": 0,
            "done": 0,
            "total": 0,
            "skipped": 0,
            "failed": 0,
            "bytes_added": 0,
            "error": None,
        }

    def _connect(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.db_path, timeout=30)
        db.execute("pragma journal_mode = wal")
        db.execute("pragma busy_timeout = 10000")
        return db

    def _init_db(self):
        with self._lock, self._connect() as db:
            db.executescript(SCHEMA)

    def _load_config(self):
        cfg = {"enabled": True, "max_bytes": DEFAULT_MAX_BYTES}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                if isinstance(raw.get("enabled"), bool):
                    cfg["enabled"] = raw["enabled"]
                mb = raw.get("max_bytes")
                if isinstance(mb, int) and not isinstance(mb, bool) and MIN_MAX_BYTES <= mb <= MAX_MAX_BYTES:
                    cfg["max_bytes"] = mb
        except (OSError, ValueError):
            pass
        return cfg

    def _save_config(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
        tmp.replace(self.config_path)

    def set_config(self, *, enabled=None, max_bytes=None):
        """Update cache-as-you-browse toggle and/or max cache size in bytes; enforces LRU limit."""
        with self._lock:
            if enabled is not None:
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be true or false")
                self._config["enabled"] = enabled
            if max_bytes is not None:
                if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
                    raise ValueError("max_bytes must be a whole number")
                if not MIN_MAX_BYTES <= max_bytes <= MAX_MAX_BYTES:
                    raise ValueError(f"max_bytes must be between {MIN_MAX_BYTES} and {MAX_MAX_BYTES}")
                self._config["max_bytes"] = max_bytes
            self._save_config()
            if max_bytes is not None:
                with self._connect() as db:
                    self._enforce_limit_locked(db)
        return self.status()

    def _enforce_limit_locked(self, db):
        """Evict oldest last_used tiles until total stored tile bytes <= max_bytes."""
        limit = self._config["max_bytes"]
        (total,) = db.execute("select coalesce(sum(size), 0) from tiles").fetchone()
        while total > limit:
            rows = db.execute("select base, z, x, y, size from tiles order by last_used asc limit 256").fetchall()
            if not rows:
                break
            drop = []
            for base, z, x, y, sz in rows:
                drop.append((base, z, x, y))
                total -= sz
                if total <= limit:
                    break
            db.executemany("delete from tiles where base = ? and z = ? and x = ? and y = ?", drop)
        db.commit()

    def enforce_limit(self):
        """Public wrapper to enforce the configured LRU size cap."""
        with self._lock, self._connect() as db:
            self._enforce_limit_locked(db)

    def has_tile(self, base, z, x, y):
        with self._lock, self._connect() as db:
            return (
                db.execute(
                    "select 1 from tiles where base = ? and z = ? and x = ? and y = ?", (base, z, x, y)
                ).fetchone()
                is not None
            )

    def _store_tile(self, base, z, x, y, ctype, data):
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                "insert into tiles (base, z, x, y, ctype, data, size, fetched_at, last_used) values (?,?,?,?,?,?,?,?,?) "
                "on conflict(base, z, x, y) do update set ctype=excluded.ctype, data=excluded.data, "
                "size=excluded.size, fetched_at=excluded.fetched_at, last_used=excluded.last_used",
                (base, z, x, y, ctype, data, len(data), now, now),
            )
            self._enforce_limit_locked(db)

    def get_tile(self, base, z, x, y, *, fetcher=None):
        """Return (bytes, content_type) from cache or upstream; (None, None) when offline & uncached."""
        info = BASES.get(base)
        if not info or not (0 <= z <= info["maxzoom"] and 0 <= x < (1 << z) and 0 <= y < (1 << z)):
            raise ValueError("tile coordinates out of range")
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute(
                "select data, ctype, last_used from tiles where base = ? and z = ? and x = ? and y = ?",
                (base, z, x, y),
            ).fetchone()
            if row is not None:
                data, ctype, last_used = row
                if now - last_used > 60:
                    db.execute(
                        "update tiles set last_used = ? where base = ? and z = ? and x = ? and y = ?",
                        (now, base, z, x, y),
                    )
                    db.commit()
                return bytes(data), ctype

        url = info["url"].format(z=z, x=x, y=y)
        try:
            data, upstream_ctype = (fetcher or _default_fetch)(url)
        except Exception:
            return None, None
        ctype = upstream_ctype or info["ctype"]
        if data and self._config["enabled"] and self._config["max_bytes"] > 0:
            self._store_tile(base, z, x, y, ctype, data)
        return data, ctype

    def get_glyph(self, fontstack, range_name, *, fetcher=None):
        """Return MapLibre glyph PBF bytes from cache or Protomaps upstream; None if offline & uncached."""
        if not FONTSTACK_RE.fullmatch(fontstack or "") or not RANGE_RE.fullmatch(range_name or ""):
            raise ValueError("invalid font glyph request")
        with self._lock, self._connect() as db:
            row = db.execute(
                "select data from glyphs where fontstack = ? and range_name = ?",
                (fontstack, range_name),
            ).fetchone()
            if row is not None:
                return bytes(row[0])
        url = GLYPH_URL.format(fontstack=urllib.parse.quote(fontstack), range=range_name)
        try:
            data, _ = (fetcher or _default_fetch)(url)
        except Exception:
            return None
        if data:
            with self._lock, self._connect() as db:
                db.execute(
                    "insert into glyphs (fontstack, range_name, data, fetched_at) values (?,?,?,?) "
                    "on conflict(fontstack, range_name) do update set data=excluded.data, fetched_at=excluded.fetched_at",
                    (fontstack, range_name, data, time.time()),
                )
                db.commit()
        return data

    def estimate(self, bases, bounds, min_z, max_z):
        """Estimate tile count and byte size for a pre-cache request."""
        bases = _normalize_bases(bases)
        box, is_vn = _normalize_bounds(bounds)
        if (
            isinstance(min_z, bool)
            or isinstance(max_z, bool)
            or not isinstance(min_z, int)
            or not isinstance(max_z, int)
        ):
            raise ValueError("min_z and max_z must be whole numbers")
        max_allowed = 14 if is_vn else 16
        if not (0 <= min_z <= max_z <= max_allowed):
            raise ValueError(f"zoom levels must be between 0 and {max_allowed}")
        tiles_per_base = count_tiles(box, min_z, max_z)
        total_tiles = tiles_per_base * len(bases)
        est_bytes = sum(tiles_per_base * BASES[b]["est_bytes"] for b in bases)
        return {
            "bases": bases,
            "bounds": list(box),
            "min_z": min_z,
            "max_z": max_z,
            "tiles_per_base": tiles_per_base,
            "total_tiles": total_tiles,
            "estimated_bytes": est_bytes,
            "exceeds_max_cache": est_bytes > self._config["max_bytes"],
            "exceeds_tile_limit": total_tiles > MAX_PRECACHE_TILES,
        }

    def start_prefetch(self, bases, bounds, min_z, max_z, label="", *, fetcher=None):
        """Start a background pre-cache job; rejects if already running or exceeding limits."""
        est = self.estimate(bases, bounds, min_z, max_z)
        if any(not BASES[b].get("prefetch") for b in est["bases"]):
            raise ValueError(PREFETCH_REFUSED)
        if est["exceeds_tile_limit"]:
            raise ValueError(
                f"That asks for {est['total_tiles']:,} tiles (limit {MAX_PRECACHE_TILES:,}). "
                "Lower the zoom level or pick a smaller area."
            )
        if est["exceeds_max_cache"]:
            raise ValueError(
                f"Estimated size (~{est['estimated_bytes'] // (1024 * 1024):,} MB) exceeds your maximum cache size. "
                "Increase Maximum cache size or choose a lower zoom."
            )
        box, _ = _normalize_bounds(bounds)
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                raise ValueError("A pre-cache download is already running.")
            self._cancel.clear()
            self._job = {
                "state": "running",
                "label": str(label or "Offline area")[:80],
                "bases": est["bases"],
                "min_z": min_z,
                "max_z": max_z,
                "done": 0,
                "total": est["total_tiles"],
                "skipped": 0,
                "failed": 0,
                "bytes_added": 0,
                "error": None,
            }
            self._worker = threading.Thread(
                target=self._run_prefetch,
                args=(est["bases"], box, min_z, max_z, fetcher),
                name="base-cache-prefetch",
                daemon=True,
            )
            self._worker.start()
        return self.status()

    def _run_prefetch(self, bases, box, min_z, max_z, fetcher):
        fetch_fn = fetcher or _default_fetch
        try:
            for base in bases:
                info = BASES[base]
                for z, x, y in iter_tiles(box, min_z, max_z):
                    if self._cancel.is_set():
                        with self._lock:
                            self._job["state"] = "cancelled"
                        return
                    if self.has_tile(base, z, x, y):
                        with self._lock:
                            self._job["done"] += 1
                            self._job["skipped"] += 1
                        continue
                    url = info["url"].format(z=z, x=x, y=y)
                    try:
                        data, upstream_ctype = fetch_fn(url)
                        if data:
                            self._store_tile(base, z, x, y, upstream_ctype or info["ctype"], data)
                            with self._lock:
                                self._job["bytes_added"] += len(data)
                        else:
                            with self._lock:
                                self._job["failed"] += 1
                    except Exception:
                        with self._lock:
                            self._job["failed"] += 1
                    with self._lock:
                        self._job["done"] += 1
                    if PRECACHE_DELAY_S > 0 and fetcher is None:
                        self._cancel.wait(PRECACHE_DELAY_S)
            with self._lock:
                self._job["state"] = "done"
        except Exception as e:
            with self._lock:
                self._job["state"] = "failed"
                self._job["error"] = str(e)

    def cancel(self):
        """Request cancellation of any active pre-cache job and return current status."""
        self._cancel.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=5)
        with self._lock:
            if self._job["state"] == "running":
                self._job["state"] = "cancelled"
        return self.status()

    def stop(self):
        """Stop any background worker cleanly on server shutdown."""
        self.cancel()

    def clear(self, base=None):
        """Cancel any active job and remove cached tiles (all or for one base), reclaiming space."""
        if base is not None and base not in BASES:
            raise ValueError("base must be 'osm' or 'sat'")
        self.cancel()
        with self._lock, self._connect() as db:
            if base:
                db.execute("delete from tiles where base = ?", (base,))
            else:
                db.execute("delete from tiles")
            db.commit()
            db.execute("vacuum")
        return self.status()

    def disk_bytes(self):
        """On-disk byte size of base_cache.sqlite and its WAL/SHM sidecars."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            try:
                total += p.stat().st_size
            except FileNotFoundError:
                pass
        return total

    def status(self):
        """Snapshot of cache configuration, stored tile/glyph counts, and active job."""
        with self._lock, self._connect() as db:
            by_base = {b: {"tiles": 0, "bytes": 0} for b in BASES}
            for base, cnt, sz in db.execute(
                "select base, count(*), coalesce(sum(size), 0) from tiles group by base"
            ).fetchall():
                if base in by_base:
                    by_base[base] = {"tiles": cnt, "bytes": sz}
            (glyph_count,) = db.execute("select count(*) from glyphs").fetchone()
            job = dict(self._job)
        used = sum(v["bytes"] for v in by_base.values())
        tile_count = sum(v["tiles"] for v in by_base.values())
        return {
            "enabled": self._config["enabled"],
            "max_bytes": self._config["max_bytes"],
            "used_bytes": used,
            "tile_count": tile_count,
            "by_base": by_base,
            "glyph_count": glyph_count,
            "vietnam_bounds": list(VIETNAM_BOUNDS),
            "job": job,
        }
