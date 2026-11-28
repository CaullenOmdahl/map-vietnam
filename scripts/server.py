"""Local server for the viewer: static files from web/, old-map tiles, the map library, the
record sources, and a small JSON API over the evidence store. Binds to 127.0.0.1 only: there is
no authentication, and cases hold the user's own notes and candidate sites, so nothing here is
meant to be reachable from outside.

Because any web page the user visits could try to talk to this server through their browser,
every /api/ request must name this server in its Host header (which defeats DNS rebinding), and
every request that changes something (POST, DELETE) must also come from this server's own pages
(Origin and Sec-Fetch-Site headers, when the browser sends them) and, for POST, carry a JSON body
(Content-Type: application/json, which a plain cross-site form cannot send). Refusals are 403/415.

Map tiles are served from each installed map's MBTiles file (scripts/tiling.py). A tile that is
not stored (fully transparent), or that cannot be read, is answered with an empty WebP, never a
404; a tile address outside the tile grid (z beyond MAX_TILE_ZOOM, x or y beyond 2^z) is a 400.
Bad input to the API gets a 400 with {"error": ...}; unknown routes a 404; a failure of the
server itself a 500 with {"error": ...}, so the viewer can always say what happened.

  GET  /tiles/<map id>/<z>/<x>/<y>.webp                          a tile of an installed map
  GET  /api/library     -> {maps: [catalogue entry + state, progress, error, disk_bytes], queue_bytes, series,
                            legacy: {count, bytes, state, error, converted}}   (legacy: maps made by older versions)
  POST /api/library/add {"ids": [...]}      queue maps from the catalogue (never arbitrary URLs)
  POST /api/library/remove {"ids": [...]}   stop and delete maps (tiles and downloads)
  POST /api/library/migrate {}              convert the legacy maps, in the background

The user's own maps (scripts/custom_maps.py); the first four answer with the same object as
GET /api/library, and GET /api/settings reports where the data is kept and how much space it takes:
  POST /api/library/custom?filename=<name>&title=<title>&year=<YYYY or empty>
        body: the file's raw bytes (application/octet-stream, Content-Length required, at most 2 GiB,
        streamed to disk) -> {"map": <listing entry>, "library": <listing>}. A GeoTIFF or GeoPDF is
        queued to build at once; a picture waits in state "needs_points". Not a map: 400 {"error"}.
  GET  /api/library/custom/<id>/preview     the scan's working image as JPEG, at most 4096 px a side
  POST /api/library/custom/<id>/points {"points": [[col, row, lon, lat], ...], "title", "year"}
        col/row are pixels of the full-size working image; 3 or more points; queues the build
  POST /api/library/custom/<id>/edit {"title", "year"}                    rename / re-date a map
  GET  /api/settings -> {"data_folder", "disk": {"maps", "records", "downloads", "total"}} (bytes)

Record sources (scripts/records.py). Nothing is downloaded unless one of these asks for it, only
the fixed ingester of a listed source is ever run, and no answer or log ever holds the Kaggle key.
No key is needed up front: a source whose download Kaggle refused for want of one fails with
"wants_key" true, and "key.needed" is true while any source does:
  GET  /api/records     -> {sources: [{id, title, description, size_mb, time_text, licence, needs, also_adds,
                            needed_by, opt_in, warning, standard, wants_key, state, removing, progress,
                            status_text, error, error_detail, records, held_in_cases, added_at, disk_bytes,
                            options, area_text}],
                            key: {needed, present, source}, busy, first_run, revision}
  POST /api/records/add {"ids": [...], "options": {"thor": {"area": [w,s,e,n]}}, "accept": [...], "refresh": bool}
                                            queue sources (and what they need); opt-in ones must be in "accept"
  POST /api/records/remove {"ids": [...], "cascade": bool}   delete records, markers and downloads; 409 with
                                            "also_removes" when other added sources need them and cascade is not set
  POST /api/records/cancel {"ids": [...]}   stop adding (a failed source forgets its failure)
  POST /api/records/welcome {"dismissed": true}              the first-run welcome has been seen
  POST /api/records/key {"username", "key"} save a Kaggle key in .env        DELETE /api/records/key  forget it
  (each answers with the same object as GET /api/records)

  POST /api/quit {}     -> {"quitting": true}; then stops the jobs under way and the server itself (exit status 0)

  GET  /api/sources
  GET  /api/evidence?bbox=w,s,e,n&from=YYYY&to=YYYY&kinds=a,b[&slim=1][&ids=1,2][&limit=N] -> GeoJSON
  GET  /api/gridref?ref=WS683427&datum=indian1960&zone=48P       -> square centre, radius, corners
  GET  /api/lonlat?lon=105.63&lat=10.46                          -> MGRS on Indian 1960 and WGS84
  GET  /api/search?q=cao lanh | WS683427 [&near=lon,lat]         -> grid-ref hit and/or matching evidence,
                                                                    nearest first, with province context
  GET  /api/cases                     POST /api/cases {name, summary}
  POST /api/cases/import?filename=<name>[&case_id=<id>][&name=<case name>]
        body: raw .kml or .kmz bytes (application/octet-stream) -> {"case", "imported", "library"}
  GET  /api/cases/<id>                (items, sites, shapes, notes)  DELETE /api/cases/<id>
  POST /api/cases/<id>/items {evidence_id, note}    DELETE /api/cases/<id>/items/<evidence_id>
  POST /api/cases/<id>/sites {name, lon, lat, uncertainty_m, rationale}
  DELETE /api/cases/<id>/sites/<site_id>
  POST /api/cases/<id>/shapes {name, kind, geometry, description, folder, color, date_start, date_end}
  DELETE /api/cases/<id>/shapes/<shape_id>
  POST /api/cases/<id>/notes {body}           DELETE /api/cases/<id>/notes/<note_id>
  GET  /api/cases/<id>/export.geojson | export.kml | export.kmz | export.gpx

Usage: .venv/bin/python scripts/server.py [--port 8765] [--db data/evidence.sqlite]
"""

import argparse
import contextlib
import json
import math
import os
import re
import signal
import sqlite3
import sys
import tempfile
import threading
import traceback
import unicodedata
from contextlib import closing
from functools import partial
from html import escape
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import custom_maps
import gridref
import hub_client
import kml
import library
import paths
import records
import store
import terrain_aerial
import tiling
from hub.polite import HostPacer, SingleFlight, is_safe_id, polite_fetch_bytes

WEB = paths.APP_ROOT / "web"  # the viewer, shipped with the app; the data under web/ is elsewhere (see Handler)
EMPTY_TILE = tiling.empty_tile()
TILE_PATH = re.compile(r"/tiles/([A-Za-z0-9_.-]+)/(\d{1,2})/(\d{1,10})/(\d{1,10})\.(webp|png|jpg)")
# Deeper than any map is tiled (z16), and far inside what SQLite's integers and the TMS row flip hold.
MAX_TILE_ZOOM = 24
# Sec-Fetch-Site values of requests made by this server's own pages (or typed by the user).
OWN_FETCH_SITES = {"same-origin", "none"}
MAX_BODY_BYTES = 1 << 20  # the API's JSON bodies are tiny; refuse anything bigger
UPLOAD_PATH = "/api/library/custom"  # raw-file upload routes (not JSON)
CASE_IMPORT_PATH = "/api/cases/import"
UPLOAD_BLOCK = 1 << 20
CUSTOM_PATH = re.compile(r"library/custom/(custom_[0-9a-f]{10})/(preview|points|edit)")
ALIGN_PATH = re.compile(r"library/([A-Za-z0-9_-]+)/align/(prepare|preview|points|reset)")
# How long a removal request waits for the removal to finish before it answers, so the answer
# usually shows the source already gone; a removal queued behind a running job answers sooner.
REMOVE_WAIT_S = 5
# How long a stop request waits for the ingester to end before it answers (it ends at once).
STOP_WAIT_S = 3
# Carried in the title or metadata of every case export, so the warning travels with the file.
HAZARD = (
    "Casualty, aircraft-loss and air-strike locations; records can be wrong. "
    "Strike areas may contain unexploded ordnance: no digging without UXO clearance from the local authorities."
)


def feature(row, props):
    """GeoJSON Point feature for an evidence row, carrying only the columns named in `props`."""
    lon, lat = row["lon"], row["lat"]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": {k: row[k] for k in props},
    }


# What the map needs to draw and filter every point; the full record is fetched on click.
SLIM_PROPS = ["id", "kind", "date_start", "date_end", "uncertainty_m", "title"]
EVIDENCE_PROPS = [
    "id",
    "source",
    "source_record_id",
    "kind",
    "date_start",
    "date_end",
    "uncertainty_m",
    "original_coord",
    "original_datum",
    "title",
    "description",
    "url",
    "withdrawn_reason",
]


def query_evidence(db, q):
    """GeoJSON FeatureCollection of evidence matching the /api/evidence query parameters `q`.

    `truncated` is true when more records matched than `limit` (default 20,000, capped at 50,000).
    Withdrawn records are left out unless asked for by `ids`, which is how cases show them.
    With `slim`, only the columns the map draws are read: a record's raw_json is over a
    kilobyte, kept on pages of its own, and a dense map cell holds tens of thousands of
    records, so leaving it unread makes such a cell about ten times quicker.
    """
    props = SLIM_PROPS if q.get("slim") else EVIDENCE_PROPS
    columns = ", ".join(["lon", "lat", *props]) if q.get("slim") else "*"
    sql, args = f"select {columns} from evidence where 1=1", []
    if q.get("bbox"):
        w, s, e, n = number_list(q["bbox"], 4, "bbox", "west,south,east,north")
        sql += " and lon between ? and ? and lat between ? and ?"
        args += [w, e, s, n]
    # Date filter on the year prefix; undated records always pass so nothing silently vanishes.
    if q.get("from"):
        sql += " and (coalesce(date_end, date_start) is null or substr(coalesce(date_end, date_start),1,4) >= ?)"
        args.append(q["from"][:4])
    if q.get("to"):
        sql += " and (date_start is null or substr(date_start,1,4) <= ?)"
        args.append(q["to"][:4])
    if q.get("kinds"):
        kinds = q["kinds"].split(",")
        sql += f" and kind in ({','.join('?' * len(kinds))})"
        args += kinds
    if q.get("ids"):
        ids = [int(i) for i in q["ids"].split(",")]
        if any(abs(i) >= 2**63 for i in ids):  # SQLite integers are 64 bits (json_each would not say so)
            raise OverflowError
        # One JSON parameter, not one placeholder per id: SQLite caps placeholders (999 to 32,766).
        sql += " and id in (select value from json_each(?))"
        args.append(json.dumps(ids))
    else:
        sql += " and withdrawn_at is null"  # withdrawn records live on only in the cases that hold them
    limit = bounded_int(q.get("limit"), 20000, 1, 50000)
    # One row past the limit tells us whether anything was cut off.
    rows = db.execute(sql + " limit ?", args + [limit + 1]).fetchall()
    return {
        "type": "FeatureCollection",
        "truncated": len(rows) > limit,
        "features": [feature(r, props) for r in rows[:limit]],
    }


def bounded_int(value, default, low, high):
    """`value` (a query-string number, or None for `default`) as an int clamped to [low, high].
    SQLite reads a negative LIMIT as "no limit", so the clamp is what keeps the caps real."""
    return max(low, min(int(value if value is not None else default), high))


def number_list(text, count, name, shape):
    """`count` finite numbers from the comma-separated query value `text`; ValueError in plain
    words (naming `shape`, e.g. "west,south,east,north") if it is anything else."""
    try:
        numbers = [float(v) for v in text.split(",")]
    except ValueError:
        numbers = []
    if len(numbers) != count or not all(math.isfinite(v) for v in numbers):
        raise ValueError(f"{name} must be {count} numbers: {shape}")
    return numbers


def text_field(body, key, required=True):
    """String field `key` of a JSON body; a missing optional field gives None. A required one
    must not be blank."""
    value = body[key] if required else body.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if required and not value.strip():
        raise ValueError(f"{key} must not be empty")
    return value


def whole_number(body, key):
    """Whole-number field `key` of a JSON body (a bool, a fraction or a string is refused)."""
    value = body[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be a whole number")
    return value


def finite_number(body, key, low=-math.inf, high=math.inf):
    """Number field `key` of a JSON body, finite and within [low, high]. Infinity or NaN would be
    stored and then written back out as invalid JSON, breaking the case page and its exports."""
    value = float(body[key])
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be a finite number between {low} and {high}")
    return value


def fold(s):
    """Lowercase and strip Vietnamese/French diacritics so 'cao lanh' finds 'Cao Lãnh'."""
    # NFD splits most accents into combining marks (dropped below); đ/Đ is a letter of its own.
    s = unicodedata.normalize("NFD", s or "").replace("đ", "d").replace("Đ", "D")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


# ISO 3166-2:VN province codes as used by the GNS gazetteer (pre-2025 provinces).
VN_PROVINCES = {
    "VN-01": "Lai Châu",
    "VN-02": "Lào Cai",
    "VN-03": "Hà Giang",
    "VN-04": "Cao Bằng",
    "VN-05": "Sơn La",
    "VN-06": "Yên Bái",
    "VN-07": "Tuyên Quang",
    "VN-09": "Lạng Sơn",
    "VN-13": "Quảng Ninh",
    "VN-14": "Hòa Bình",
    "VN-18": "Ninh Bình",
    "VN-20": "Thái Bình",
    "VN-21": "Thanh Hóa",
    "VN-22": "Nghệ An",
    "VN-23": "Hà Tĩnh",
    "VN-24": "Quảng Bình",
    "VN-25": "Quảng Trị",
    "VN-26": "Thừa Thiên Huế",
    "VN-27": "Quảng Nam",
    "VN-28": "Kon Tum",
    "VN-29": "Quảng Ngãi",
    "VN-30": "Gia Lai",
    "VN-31": "Bình Định",
    "VN-32": "Phú Yên",
    "VN-33": "Đắk Lắk",
    "VN-34": "Khánh Hòa",
    "VN-35": "Lâm Đồng",
    "VN-36": "Ninh Thuận",
    "VN-37": "Tây Ninh",
    "VN-39": "Đồng Nai",
    "VN-40": "Bình Thuận",
    "VN-41": "Long An",
    "VN-43": "Bà Rịa–Vũng Tàu",
    "VN-44": "An Giang",
    "VN-45": "Đồng Tháp",
    "VN-46": "Tiền Giang",
    "VN-47": "Kiên Giang",
    "VN-49": "Vĩnh Long",
    "VN-50": "Bến Tre",
    "VN-51": "Trà Vinh",
    "VN-52": "Sóc Trăng",
    "VN-53": "Bắc Kạn",
    "VN-54": "Bắc Giang",
    "VN-55": "Bạc Liêu",
    "VN-56": "Bắc Ninh",
    "VN-57": "Bình Dương",
    "VN-58": "Bình Phước",
    "VN-59": "Cà Mau",
    "VN-61": "Hải Dương",
    "VN-63": "Hà Nam",
    "VN-66": "Hưng Yên",
    "VN-67": "Nam Định",
    "VN-68": "Phú Thọ",
    "VN-69": "Thái Nguyên",
    "VN-70": "Vĩnh Phúc",
    "VN-71": "Điện Biên",
    "VN-72": "Đắk Nông",
    "VN-73": "Hậu Giang",
    "VN-CT": "Cần Thơ",
    "VN-DN": "Đà Nẵng",
    "VN-HN": "Hà Nội",
    "VN-HP": "Hải Phòng",
    "VN-SG": "Hồ Chí Minh",
}


def place_context(row):
    """Short 'where is this' line for a search hit, e.g. 'Đồng Tháp' or 'Kien Phong · Cao Lanh (1969)'."""
    raw = json.loads(row["raw_json"] or "{}")
    if row["source"] == "gns":
        return VN_PROVINCES.get(raw.get("adm1"), "")
    if row["source"] == "hes_gazetteer":
        m = re.search(r"province (.+?) / district (.+?) /", row["description"] or "")
        return f"{m[1].title()} · {m[2].title()} (wartime)" if m else ""
    return ""


def km_between(lon1, lat1, lon2, lat2):
    """Approximate distance in km between two WGS84 points (equirectangular, 111.2 km a degree).

    Plenty for ranking search hits by distance; not a geodesic.
    """
    x = (lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    return 111.2 * math.hypot(x, lat2 - lat1)


# (database file signature, cache key, rows) for title_index, replaced in one assignment so
# request threads never see a key paired with rows from another state of the store.
_title_cache = (None, None, [])


def forget_caches():
    """Drop what this module remembers about the records. scripts/records.py calls it after a
    source was added, added again or removed, so a search never answers from the old records."""
    global _title_cache
    _title_cache = (None, None, [])


def title_index(db):
    """(id, folded title) for every current (not withdrawn) record, for substring search.

    Rebuilt when the record count, the highest id, the latest withdrawal or the database file
    changes, which covers adding, deleting and withdrawing records. A title edited in place by a
    re-ingest keeps its old folded form until one of those changes (or forget_caches is called,
    as scripts/records.py does after every change it makes).

    Working out that key reads the whole table, seconds on a store with many air strikes, so it
    is done only when the database file itself has changed (its modification time or size):
    every write to the store changes one of them, and most searches come between writes.
    """
    global _title_cache
    path = str(db.execute("pragma database_list").fetchone()[2])
    try:
        stat = Path(path).stat()
        signature = (path, stat.st_mtime_ns, stat.st_size)
    except OSError:  # an in-memory store: no file to watch, so check the key every time
        signature = None
    cached_signature, cached_key, rows = _title_cache
    if signature is not None and cached_signature == signature:
        return rows
    counts = db.execute("select count(*), max(id), max(withdrawn_at) from evidence").fetchone()
    key = (tuple(counts), path)
    if cached_key != key:
        rows = [(i, fold(t)) for i, t in db.execute("select id, title from evidence where withdrawn_at is null")]
    _title_cache = (signature, key, rows)
    return rows


def search(db, q, limit=20, near=None):
    """One box for everything: a grid reference, or a name among the evidence titles.
    Names are ranked nearest-first to `near` (lon, lat), prefix matches ahead of the rest."""
    q = q.strip()
    out = []
    try:
        lon, lat, r = gridref.to_wgs84(q, datum="indian1960")
        out.append(
            {
                "type": "gridref",
                "label": f"Grid reference {q.upper()} (Indian 1960)",
                "lon": lon,
                "lat": lat,
                "uncertainty_m": r,
                "square": gridref.square(q, "indian1960"),
            }
        )
    except ValueError:
        pass  # not a grid reference: a name search only
    if len(q) >= 2:
        fq = fold(q)
        # At most 2,000 candidates are ranked; ids are fetched 500 at a time to stay well under
        # SQLite's bound-parameter limit.
        ids = [i for i, t in title_index(db) if fq in t][:2000]
        rows = []
        for chunk in range(0, len(ids), 500):
            part = ids[chunk : chunk + 500]
            rows += db.execute(
                "select id, kind, title, lon, lat, uncertainty_m, date_start, source, description, raw_json "
                f"from evidence where id in ({','.join('?' * len(part))})",
                part,
            ).fetchall()
        hits = []
        for r in rows:
            d = km_between(*near, r["lon"], r["lat"]) if near else None
            name = fold(r["title"].split(" (")[0])  # HES titles carry "(HES hamlet, district, province)"
            exact = name == fq or name.endswith(f" {fq}")  # "Ấp Mỹ Hiệp" is an exact hit for "my hiep"
            prefix = name.startswith(fq) or f" {fq}" in name
            hits.append(
                (
                    (not exact, not prefix, d if d is not None else 0, len(r["title"])),
                    {
                        "type": "evidence",
                        **{
                            k: r[k]
                            for k in ("id", "kind", "title", "lon", "lat", "uncertainty_m", "date_start", "source")
                        },
                        "context": place_context(r),
                        "distance_km": round(d, 1) if d is not None else None,
                    },
                )
            )
        out += [h for _, h in sorted(hits, key=lambda t: t[0])[:limit]]
    return out


def case_detail(db, cid):
    """A case with its pinned records (each with its case note), sites, shapes and notes; None if absent."""
    c = db.execute("select * from cases where id = ?", (cid,)).fetchone()
    if not c:
        return None
    items = db.execute(
        "select e.*, ci.note as case_note from case_items ci join evidence e on e.id = ci.evidence_id "
        "where ci.case_id = ? order by e.date_start",
        (cid,),
    ).fetchall()
    sites = db.execute("select * from sites where case_id = ? order by id", (cid,)).fetchall()
    notes = db.execute("select * from notes where case_id = ? order by id", (cid,)).fetchall()
    shapes = []
    for r in db.execute("select * from shapes where case_id = ? order by id", (cid,)):
        d = dict(r)
        d["geometry"] = json.loads(d.pop("geometry_json"))
        shapes.append(d)
    return {
        **dict(c),
        "items": [dict(r) for r in items],
        "sites": [dict(r) for r in sites],
        "shapes": shapes,
        "notes": [dict(r) for r in notes],
    }


def _iter_coords(geom):
    """Yield (lon, lat) pairs from a GeoJSON LineString or Polygon geometry."""
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString":
        for pt in coords:
            yield pt[0], pt[1]
    elif gtype == "Polygon":
        for ring in coords:
            for pt in ring:
                yield pt[0], pt[1]


def validate_shape_geometry(kind, geom):
    """Validate and normalise a LineString or Polygon GeoJSON geometry dict."""
    if kind not in ("line", "polygon"):
        raise ValueError("kind must be 'line' or 'polygon'")
    if not isinstance(geom, dict):
        raise ValueError("geometry must be a GeoJSON object")
    expected_type = "LineString" if kind == "line" else "Polygon"
    if geom.get("type") != expected_type:
        raise ValueError(f"geometry.type must be {expected_type}")
    coords = geom.get("coordinates")
    if not isinstance(coords, list):
        raise ValueError("geometry.coordinates must be a list")

    def clean_ring(raw, min_pts, close=False):
        if not isinstance(raw, list) or len(raw) < min_pts:
            raise ValueError(f"geometry needs at least {min_pts} coordinates")
        out = []
        for pt in raw:
            if not isinstance(pt, (list, tuple)) or len(pt) < 2:
                raise ValueError("each coordinate must be [lon, lat]")
            lon, lat = float(pt[0]), float(pt[1])
            if not (math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError("coordinate out of range")
            out.append([lon, lat])
        if close and out[0] != out[-1]:
            out.append(list(out[0]))
        return out

    if kind == "line":
        return {"type": "LineString", "coordinates": clean_ring(coords, 2)}
    if not coords:
        raise ValueError("polygon needs at least an outer ring")
    rings = [clean_ring(coords[0], 3, close=True)]
    for hole in coords[1:]:
        rings.append(clean_ring(hole, 3, close=True))
    return {"type": "Polygon", "coordinates": rings}


def case_kmz_overlays(case, lib):
    """Collect custom map overlays intersecting `case` for bundling inside export.kmz."""
    if lib is None:
        return []
    lons, lats = [], []
    for s in case.get("sites") or []:
        lons.append(s["lon"])
        lats.append(s["lat"])
    for e in case.get("items") or []:
        lons.append(e["lon"])
        lats.append(e["lat"])
    for sh in case.get("shapes") or []:
        for lon, lat in _iter_coords(sh.get("geometry") or {}):
            lons.append(lon)
            lats.append(lat)
    if not lons:
        return []
    pad = 0.05
    w, s, e, n = min(lons) - pad, min(lats) - pad, max(lons) + pad, max(lats) + pad

    overlays = []
    total_bytes = 0
    for entry in lib.custom.entries():
        b = entry.get("bounds")
        if not b or len(b) != 4:
            continue
        mw, ms, me, mn = b
        if me < w or mw > e or mn < s or ms > n:
            continue
        img_path = lib.custom.preview_path(entry["id"])
        if not img_path.is_file():
            img_path = lib.custom.work_image(entry["id"])
        if not img_path.is_file():
            continue
        data = img_path.read_bytes()
        if total_bytes + len(data) > (50 << 20) or len(overlays) >= kml.MAX_OVERLAYS:
            break
        corners = None
        if entry.get("points") and entry.get("image"):
            corners = custom_maps.placed_corners(entry["points"], entry["image"]["width"], entry["image"]["height"])
        overlays.append(
            {
                "name": entry["title"],
                "href": f"files/{entry['id']}{img_path.suffix.lower()}",
                "image_bytes": data,
                "corners_lonlat": corners,
                "bounds": b,
            }
        )
        total_bytes += len(data)
    return overlays


def export_points(case):
    """A case as plain points (name, lon, lat, radius r in m, description): sites first, then records."""
    pts = [
        {
            "name": f"SITE: {s['name']}",
            "lon": s["lon"],
            "lat": s["lat"],
            "r": s["uncertainty_m"],
            "desc": s["rationale"] or "",
        }
        for s in case["sites"]
    ]
    pts += [
        {
            "name": e["title"],
            "lon": e["lon"],
            "lat": e["lat"],
            "r": e["uncertainty_m"],
            "desc": f"{e['kind'].replace('_', ' ').capitalize()}. "
            f"{e['source']} {e['source_record_id']}, {e['date_start'] or 'undated'}, "
            f"original {e['original_coord']} ({e['original_datum']}). {e['description'] or ''}",
        }
        for e in case["items"]
    ]
    return pts


def export(case, fmt, overlays=()):
    """(content type, text-or-bytes) of a case exported as `fmt`: "geojson", "kml", "kmz" or "gpx".

    Every format carries the hazard warning and the case notes; each point says how far
    (its uncertainty radius) the true position may lie from it.
    """
    pts = export_points(case)
    shapes = case.get("shapes") or []
    title = f"{case['name']} ({HAZARD})"
    notes = [n["body"] for n in case["notes"]]
    notes_text = escape("\n".join(notes))
    if fmt == "geojson":
        features = [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                "properties": {"name": p["name"], "uncertainty_m": p["r"], "description": p["desc"]},
            }
            for p in pts
        ]
        features += [
            {
                "type": "Feature",
                "geometry": sh["geometry"],
                "properties": {
                    "name": sh["name"],
                    "kind": sh["kind"],
                    "description": sh.get("description") or "",
                    "folder": sh.get("folder") or "",
                    "color": sh.get("color") or "",
                },
            }
            for sh in shapes
        ]
        return "application/geo+json", json.dumps(
            {
                "type": "FeatureCollection",
                "name": case["name"],
                "warning": HAZARD,
                "notes": notes,
                "features": features,
            },
            indent=1,
        )
    if fmt == "kml":
        return "application/vnd.google-earth.kml+xml", kml.export_kml(case, hazard=HAZARD)
    if fmt == "kmz":
        return "application/vnd.google-earth.kmz", kml.export_kmz(case, hazard=HAZARD, overlays=overlays)
    if fmt == "gpx":
        # GPS units truncate long waypoint names; keep the start, which carries the place name.
        wpts = "".join(
            f'<wpt lat="{p["lat"]}" lon="{p["lon"]}"><name>{escape(p["name"][:60])}</name>'
            f"<desc>{escape(p['desc'])} (within {p['r']:.0f} m)</desc></wpt>"
            for p in pts
        )
        trks = []
        for sh in shapes:
            geom = sh.get("geometry") or {}
            coords = geom.get("coordinates") or []
            pts_seq = coords[0] if geom.get("type") == "Polygon" and coords else coords
            if len(pts_seq) >= 2:
                trkpts = "".join(f'<trkpt lat="{pt[1]}" lon="{pt[0]}"></trkpt>' for pt in pts_seq)
                trks.append(
                    f"<trk><name>{escape(sh['name'][:60])}</name>"
                    f"<desc>{escape(sh.get('description') or '')}</desc><trkseg>{trkpts}</trkseg></trk>"
                )
        return "application/gpx+xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="MapVietnam" '
            f'xmlns="http://www.topografix.com/GPX/1/1"><metadata><name>{escape(title)}</name>'
            f"<desc>{notes_text}</desc></metadata>{wpts}{''.join(trks)}</gpx>"
        )
    raise ValueError(f"unknown export format {fmt!r}")


class Handler(SimpleHTTPRequestHandler):
    """Serves web/, the tiles and the /api/ routes listed in the module docstring, one store
    connection per request."""

    db_path = store.DB_PATH
    data_web = paths.DATA_ROOT / "web"  # where /catalog.json and /tiles/ live (set by make_server)
    library = None  # the map library (set by make_server)
    records = None  # the record sources (set by make_server)
    # Fixed types for what the viewer loads: on Windows the defaults come from the registry, where
    # .js or .css is sometimes text/plain, and browsers then refuse the file.
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".html": "text/html",
        ".js": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".webp": "image/webp",
        ".png": "image/png",
        ".svg": "image/svg+xml",
    }

    def translate_path(self, path):
        """The file for a URL path. The viewer comes from the program's own folder (self.directory);
        what the app writes, /catalog.json and /tiles/, comes from the data folder. The parent
        class has already refused any climb out of the folder (.. and the like)."""
        found = super().translate_path(path)
        parts = Path(found).relative_to(self.directory).parts
        if parts and parts[0] in ("catalog.json", "tiles"):
            return str(Path(self.data_web, *parts))
        return found

    def log_message(self, *args):
        pass  # keep the console quiet: the viewer fetches hundreds of tiles per pan

    def db(self):
        """A fresh connection to the store, returning rows addressable by column name."""
        db = store.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def send_json(self, obj, status=200, ctype="application/json"):
        """Send `obj` as JSON, or as-is if it is already text (exports), never cached."""
        body = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, status=200, ctype="application/octet-stream", filename=None):
        """Send binary bytes (e.g. a .kmz archive), never cached."""
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if filename:
            safe = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip() or "export.kmz"
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        """The request's JSON body, which must be an object ({} if empty)."""
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= MAX_BODY_BYTES:
            raise ValueError("request body too large" if n > 0 else "bad Content-Length")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("send a JSON object")
        return data

    def _scrub_local(self, text):
        out = str(text or "")
        if not out:
            return out
        env_vals = records.read_env(self.library.root / ".env") if self.library else {}
        for k in ("USGS_M2M_TOKEN", "KAGGLE_KEY", "HUB_API_KEY", "EARTHDATA_TOKEN"):
            for v in (os.environ.get(k), env_vals.get(k)):
                if v and len(v) >= 4:
                    out = out.replace(v, "[REDACTED]")
        return re.sub(r"mvhub_[0-9a-f]{8}_[0-9a-f]{16,96}", "mvhub_[REDACTED]", out)

    def send_failure(self, error):
        """Answer 500 for a failure of the server itself, logging the traceback for the console."""
        traceback.print_exception(error, file=sys.stderr)
        self.send_json({"error": f"the server failed: {self._scrub_local(error)}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def own_hosts(self):
        """The Host header values that name this server: 127.0.0.1, localhost or [::1] on its port."""
        port = self.server.server_address[1]
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def refusal(self, method):
        """(status, reason) if the request must be refused as possibly cross-site, else None.

        Host must name this server on every API call. A request that changes something must not
        come from another site's page: browsers send Origin with such requests (checked against
        our own) and Sec-Fetch-Site (checked when present); a POST must also be JSON.
        """
        hosts = self.own_hosts()
        if self.headers.get("Host") not in hosts:
            return HTTPStatus.FORBIDDEN, "this server only answers requests addressed to itself"
        if method == "GET":
            return None
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in hosts}:
            return HTTPStatus.FORBIDDEN, "requests from other web sites are refused"
        site = self.headers.get("Sec-Fetch-Site")
        if site is not None and site not in OWN_FETCH_SITES:
            return HTTPStatus.FORBIDDEN, "requests from other web sites are refused"
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # A file upload is raw bytes. Like JSON, that type cannot be sent by a plain cross-site
        # form, and a script on another site needs a CORS preflight this server never grants.
        if method == "POST" and urlparse(self.path).path in (UPLOAD_PATH, CASE_IMPORT_PATH) and ctype == "application/octet-stream":
            return None
        if method == "POST" and ctype != "application/json":
            return HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send the request body as application/json"
        return None

    def route(self, method):
        """Handle the request if it is an /api/ call and return True; return False otherwise."""
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return False
        refused = self.refusal(method)
        if refused:
            self.send_json({"error": refused[1]}, refused[0])
            return True
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        p = url.path[len("/api/") :].strip("/")
        try:
            if p == "library" or p.startswith("library/"):
                self.library_api(method, p)
            elif p == "records" or p.startswith("records/"):
                self.records_api(method, p)
            elif p == "hub" or p.startswith("hub/"):
                self.hub_api(method, p, q)
            elif p == "terrain" or p.startswith("terrain/"):
                self.terrain_api(method, p, q)
            elif p == "aerial" or p.startswith("aerial/"):
                self.aerial_api(method, p, q)
            elif p == "elevation" and method == "GET":
                self.elevation_api(q)
            elif p == "transect" and method == "POST":
                self.transect_api()
            elif p == "lidar" and method == "GET":
                self.lidar_api(q)
            elif p == "media" or p == "media/delete":
                with closing(self.db()) as db, db:
                    self.media_api(method, p, q, db)
            elif p == "local-keys":
                self.local_keys_api(method)
            elif p == "settings" and method == "GET":
                self.settings()
            elif p == "quit" and method == "POST":
                self.body()  # refuse a body that is not a JSON object, like every other POST
                self.send_json({"quitting": True})
                self.server.quit()
            else:
                # closing() releases the connection; the inner `with` rolls back on an error.
                with closing(self.db()) as db, db:
                    self.dispatch(method, p, q, db)
        except custom_maps.NoSuchMap as e:
            self.send_json({"error": str(e)}, HTTPStatus.NOT_FOUND)
        except records.RecordsError as e:  # plain words, sometimes with facts the viewer acts on
            self.send_json({"error": str(e), **e.extra}, e.status)
        except KeyError as e:  # a field or query value the request left out
            self.send_json({"error": f"{e.args[0]} is missing"}, HTTPStatus.BAD_REQUEST)
        except (ValueError, TypeError) as e:  # LibraryError is a ValueError
            self.send_json({"error": self._scrub_local(e)}, HTTPStatus.BAD_REQUEST)
        except OverflowError:  # a number too big for SQLite, e.g. an id of 30 digits
            self.send_json({"error": "a number in the request is too large"}, HTTPStatus.BAD_REQUEST)
        except sqlite3.IntegrityError as e:  # a duplicate name, or a record id that does not exist
            unique = "UNIQUE" in str(e)
            reason = "A case with that name already exists." if unique else "That record or case does not exist."
            self.send_json({"error": reason}, HTTPStatus.BAD_REQUEST)
        except ConnectionError:  # the browser went away mid-answer: nobody is left to tell
            raise
        except Exception as e:  # never drop the connection without an answer
            self.send_failure(e)
        return True

    def dispatch(self, method, p, q, db):
        """Answer API path `p` (without the /api/ prefix) with query parameters `q`."""
        m = re.fullmatch(
            r"cases/(\d{1,18})(?:/(items|sites|notes|shapes)(?:/(\d{1,18}))?|/export\.(geojson|kml|kmz|gpx))?",
            p,
        )
        if method == "GET" and p == "sources":
            return self.send_json(
                [
                    dict(r)
                    for r in db.execute(
                        "select s.*, count(e.id) as records from sources s left join evidence e on e.source = s.id group by s.id"
                    )
                ]
            )
        if method == "GET" and p == "evidence":
            return self.send_json(query_evidence(db, q), ctype="application/geo+json")
        if method == "GET" and p == "gridref":
            datum = q.get("datum", "indian1960")
            lon, lat, r = gridref.to_wgs84(q["ref"], datum=datum, default_zone=q.get("zone", "48P"))
            g = gridref.parse(q["ref"], q.get("zone", "48P"), datum)
            return self.send_json(
                {
                    "lon": lon,
                    "lat": lat,
                    "uncertainty_m": r,
                    "precision_m": g.precision_m,
                    "datum": datum,
                    "wgs84_mgrs": gridref.from_lonlat(lon, lat, "wgs84", 10),
                    "square": gridref.square(q["ref"], datum, q.get("zone", "48P")),
                }
            )
        if method == "GET" and p == "search":
            near = number_list(q["near"], 2, "near", "lon,lat") if q.get("near") else None
            return self.send_json(search(db, q.get("q", ""), bounded_int(q.get("limit"), 20, 1, 200), near))
        if method == "GET" and p == "lonlat":
            lon, lat = float(q["lon"]), float(q["lat"])
            return self.send_json(
                {
                    "lon": lon,
                    "lat": lat,
                    "mgrs_indian1960": gridref.from_lonlat(lon, lat, "indian1960", 10),
                    "mgrs_wgs84": gridref.from_lonlat(lon, lat, "wgs84", 10),
                    "nara_rg373": terrain_aerial.nara_rg373_info(lon, lat),
                }
            )
        if p == "cases" and method == "GET":
            return self.send_json([dict(r) for r in db.execute("select * from cases order by id desc")])
        if p == "cases" and method == "POST":
            b = self.body()
            cur = db.execute(
                "insert into cases (name, summary, created_at) values (?,?,?)",
                (text_field(b, "name").strip(), text_field(b, "summary", False) or "", store.now()),
            )
            return self.send_json({"id": cur.lastrowid}, HTTPStatus.CREATED)
        if p == "cases/import" and method == "POST":
            return self.import_case_kml(q, db)
        if not m:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        cid, sub, sub_id, fmt = int(m[1]), m[2], m[3], m[4]
        case = case_detail(db, cid)
        if case is None:
            return self.send_json({"error": "no such case"}, HTTPStatus.NOT_FOUND)
        if method == "GET" and fmt:
            if fmt == "kmz":
                ctype, data = export(case, fmt, overlays=case_kmz_overlays(case, self.library))
                return self.send_bytes(data, ctype=ctype, filename=f"{case['name']}.kmz")
            ctype, text = export(case, fmt)
            return self.send_json(text, ctype=ctype)
        if method == "GET" and not sub:
            return self.send_json(case)
        if method == "DELETE" and not sub and not fmt:
            db.execute("delete from cases where id = ?", (cid,))
            db.commit()
            return self.send_json({"deleted": cid})
        if method == "POST" and sub == "items":
            b = self.body()
            db.execute(
                "insert or replace into case_items (case_id, evidence_id, note) values (?,?,?)",
                (cid, whole_number(b, "evidence_id"), text_field(b, "note", False)),
            )
        elif method == "DELETE" and sub == "items" and sub_id:
            db.execute("delete from case_items where case_id = ? and evidence_id = ?", (cid, int(sub_id)))
        elif method == "POST" and sub == "sites":
            b = self.body()
            r = finite_number(b, "uncertainty_m")
            if r <= 0:
                raise ValueError("uncertainty_m must be > 0")
            lon, lat = finite_number(b, "lon", -180, 180), finite_number(b, "lat", -90, 90)
            db.execute(
                "insert into sites (case_id, name, lon, lat, uncertainty_m, rationale, created_at) "
                "values (?,?,?,?,?,?,?)",
                (cid, text_field(b, "name"), lon, lat, r, text_field(b, "rationale", False), store.now()),
            )
        elif method == "DELETE" and sub == "sites" and sub_id:
            db.execute("delete from sites where case_id = ? and id = ?", (cid, int(sub_id)))
        elif method == "POST" and sub == "shapes":
            b = self.body()
            kind = text_field(b, "kind")
            geom = validate_shape_geometry(kind, b.get("geometry"))
            color = text_field(b, "color", False)
            if color is not None and color != "" and not kml.HEX_COLOR_RE.match(color):
                raise ValueError("color must be a #rrggbb hex string")
            ds = text_field(b, "date_start", False)
            de = text_field(b, "date_end", False)
            for d in (ds, de):
                if d and not store.DATE_RE.match(d):
                    raise ValueError("dates must be YYYY, YYYY-MM or YYYY-MM-DD")
            db.execute(
                "insert into shapes (case_id, name, kind, geometry_json, description, folder, color, date_start, date_end, created_at) "
                "values (?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    text_field(b, "name").strip(),
                    kind,
                    json.dumps(geom),
                    text_field(b, "description", False),
                    text_field(b, "folder", False),
                    color or None,
                    ds or None,
                    de or None,
                    store.now(),
                ),
            )
        elif method == "DELETE" and sub == "shapes" and sub_id:
            db.execute("delete from shapes where case_id = ? and id = ?", (cid, int(sub_id)))
        elif method == "POST" and sub == "notes":
            db.execute(
                "insert into notes (case_id, body, created_at) values (?,?,?)",
                (cid, text_field(self.body(), "body"), store.now()),
            )
        elif method == "DELETE" and sub == "notes" and sub_id:
            db.execute("delete from notes where case_id = ? and id = ?", (cid, int(sub_id)))
        else:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        db.commit()
        self.send_json(case_detail(db, cid))

    def import_case_kml(self, q, db):
        """POST /api/cases/import: parse a Google Earth .kml or .kmz upload into a case."""
        try:
            size = int(self.headers.get("Content-Length") or "")
        except ValueError:
            size = -1
        if size <= 0:
            self.close_connection = True
            return self.send_json({"error": "Send the file's contents with a Content-Length."}, HTTPStatus.BAD_REQUEST)
        if size > kml.MAX_KMZ_BYTES:
            self.close_connection = True
            return self.send_json(
                {"error": "That KML/KMZ file is larger than 200 MB, which is more than Map Vietnam allows."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        fd, tmp_name = tempfile.mkstemp(prefix="mv-kml-", suffix=".tmp")
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            with open(tmp_path, "wb") as f:
                left = size
                while left:
                    block = self.rfile.read(min(UPLOAD_BLOCK, left))
                    if not block:
                        raise kml.KmlError("The upload was cut short. Please try adding the file again.")
                    f.write(block)
                    left -= len(block)
            parsed = kml.parse_kml_or_kmz(tmp_path, q.get("filename", ""))
        except BaseException:
            self.close_connection = True
            raise
        finally:
            tmp_path.unlink(missing_ok=True)

        created_new = False
        if q.get("case_id"):
            cid = int(q["case_id"])
            if case_detail(db, cid) is None:
                return self.send_json({"error": "no such case"}, HTTPStatus.NOT_FOUND)
        else:
            existing = {r[0] for r in db.execute("select name from cases")}
            base_name = (q.get("name") or parsed["name"] or "Imported KML").strip()[:200] or "Imported KML"
            name = base_name
            n = 2
            while name in existing:
                name = f"{base_name[:190]} ({n})"
                n += 1
            summary = parsed["description"].splitlines()[0][:200] if parsed["description"] else ""
            cur = db.execute(
                "insert into cases (name, summary, created_at) values (?,?,?)",
                (name, summary, store.now()),
            )
            cid = cur.lastrowid
            if parsed["description"]:
                db.execute(
                    "insert into notes (case_id, body, created_at) values (?,?,?)",
                    (cid, parsed["description"], store.now()),
                )
            created_new = True

        now_ts = store.now()
        for s in parsed["sites"]:
            db.execute(
                "insert into sites (case_id, name, lon, lat, uncertainty_m, rationale, created_at) "
                "values (?,?,?,?,?,?,?)",
                (cid, s["name"], s["lon"], s["lat"], s["uncertainty_m"], s["rationale"], now_ts),
            )
        for sh in parsed["shapes"]:
            db.execute(
                "insert into shapes (case_id, name, kind, geometry_json, description, folder, color, date_start, date_end, created_at) "
                "values (?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    sh["name"],
                    sh["kind"],
                    json.dumps(sh["geometry"]),
                    sh["description"],
                    sh["folder"],
                    sh["color"],
                    sh["date_start"],
                    sh["date_end"],
                    now_ts,
                ),
            )
        db.commit()

        warnings = list(parsed["warnings"])
        added_ids = []
        if parsed["overlays"] and self.library is not None:
            for ov in parsed["overlays"]:
                part = self.library.custom.incoming_path()
                try:
                    part.write_bytes(ov["image_bytes"])
                    entry = self.library.custom.ingest_placed_overlay(
                        part, ov["filename"], ov["name"], ov["corners_lonlat"], ov["year"]
                    )
                    added_ids.append(entry["id"])
                except (custom_maps.CustomMapError, OSError) as e:
                    part.unlink(missing_ok=True)
                    warnings.append(f"Overlay '{ov['name']}' could not be added: {e}")
            if added_ids:
                self.library.add(added_ids)
                self.library.start_worker()

        return self.send_json(
            {
                "case": case_detail(db, cid),
                "imported": {
                    "sites": len(parsed["sites"]),
                    "shapes": len(parsed["shapes"]),
                    "overlays": len(added_ids),
                    "warnings": warnings,
                },
                "library": self.library.listing() if added_ids else None,
            },
            HTTPStatus.CREATED if created_new else HTTPStatus.OK,
        )

    def library_api(self, method, p):
        """The /api/library routes (see the module docstring)."""
        lib = self.library
        if method == "GET" and p == "library":
            return self.send_json(lib.listing())
        if method == "POST" and p in ("library/add", "library/remove"):
            ids = self.body().get("ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
                raise ValueError('send {"ids": ["<map id>", ...]}')
            if p == "library/add":
                lib.add(ids)
                lib.start_worker()
            else:
                lib.remove(ids)
            return self.send_json(lib.listing())
        if method == "POST" and p == "library/migrate":
            self.body()
            lib.start_migration()
            return self.send_json(lib.listing())
        if method == "POST" and p == "library/custom":
            return self.upload_custom_map()
        m = CUSTOM_PATH.fullmatch(p)
        if m and (method, m[2]) in (("GET", "preview"), ("POST", "points"), ("POST", "edit")):
            return self.custom_map_api(m[1], m[2])
        am = ALIGN_PATH.fullmatch(p)
        if am and ((method == "GET" and am[2] == "preview") or (method == "POST" and am[2] != "preview")):
            return self.align_map_api(am[1], am[2])
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def upload_custom_map(self):
        """POST /api/library/custom: stream the body to disk, then let custom_maps decide what it is."""
        q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        lib = self.library
        try:
            size = int(self.headers.get("Content-Length") or "")
        except ValueError:
            size = -1
        if size <= 0:
            self.close_connection = True  # the body, if any, is left unread
            return self.send_json({"error": "Send the file's contents with a Content-Length."}, HTTPStatus.BAD_REQUEST)
        if size > custom_maps.MAX_UPLOAD_BYTES:
            self.close_connection = True
            return self.send_json(
                {"error": "That file is over 2 GB, which is more than Map Vietnam can add."},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
        part = lib.custom.incoming_path()
        try:
            with open(part, "wb") as f:
                left = size
                while left:
                    block = self.rfile.read(min(UPLOAD_BLOCK, left))
                    if not block:
                        raise custom_maps.CustomMapError("The upload was cut short. Please try adding the file again.")
                    f.write(block)
                    left -= len(block)
            entry = lib.custom.ingest(part, q.get("filename", ""), q.get("title", ""), q.get("year", ""))
        except BaseException:
            part.unlink(missing_ok=True)
            self.close_connection = True
            raise
        if lib.custom.buildable(entry):
            lib.add([entry["id"]])
            lib.start_worker()
        listing = lib.listing()
        self.send_json({"map": next(m for m in listing["maps"] if m["id"] == entry["id"]), "library": listing})

    def send_jpeg(self, path):
        """Send a JPEG file without caching (previews change when re-uploaded or prepared)."""
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def custom_map_api(self, map_id, action):
        """The routes of one of the user's own maps: preview, points, edit."""
        lib = self.library
        entry = lib.custom.get(map_id)  # NoSuchMap -> 404
        if action == "preview":
            path = lib.custom.preview_path(map_id)
            if entry["kind"] != "scan" or not path.exists():
                raise custom_maps.NoSuchMap()
            return self.send_jpeg(path)
        b = self.body()
        if action == "points":
            lib.place_custom(map_id, b.get("points"), b.get("title"), b.get("year"))
            lib.start_worker()
        else:
            lib.custom.edit(map_id, b.get("title"), b.get("year"))
            lib.write_catalog()
        self.send_json(lib.listing())

    def align_map_api(self, map_id, action):
        """The alignment routes of any installed map: prepare, preview, points, reset."""
        lib = self.library
        if action == "preview":
            path = lib.alignment_preview_path(map_id)
            if not path.exists():
                return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return self.send_jpeg(path)
        b = self.body()
        if action == "prepare":
            return self.send_json(lib.prepare_alignment(map_id))
        if action == "points":
            res = lib.save_alignment(map_id, b.get("points"))
        else:
            res = lib.reset_alignment(map_id)
        lib.start_worker()
        self.send_json(res)

    def settings(self):
        """GET /api/settings: where the data is kept and how much space each kind takes."""
        lib = self.library
        self.send_json({"data_folder": str(lib.root), "disk": lib.disk_usage(self.records.db_path)})

    def records_api(self, method, p):
        """The /api/records routes (see the module docstring). Every change answers with the
        new listing. The Kaggle key is taken from the body and handed on; it is never echoed,
        and nothing here prints a request body."""
        rec = self.records
        if method == "GET" and p == "records":
            return self.send_json(rec.listing())
        if method == "DELETE" and p == "records/key":
            rec.forget_key()
            return self.send_json(rec.listing())
        if method != "POST":
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        b = self.body()
        if p == "records/add":
            if not isinstance(b.get("refresh", False), bool):
                raise ValueError('"refresh" must be true or false')
            queued = rec.add(b.get("ids"), b.get("options") or {}, b.get("accept") or [], b.get("refresh", False))
            rec.start_worker()
            return self.send_json({**rec.listing(), "queued": queued})
        if p == "records/remove":
            if not isinstance(b.get("cascade", False), bool):
                raise ValueError('"cascade" must be true or false')
            removing = rec.remove(b.get("ids"), cascade=b.get("cascade", False))
            rec.start_worker()
            rec.wait_removed(removing, REMOVE_WAIT_S)
            return self.send_json({**rec.listing(), "removing": removing})
        if p == "records/cancel":
            stopped = rec.cancel(b.get("ids"))
            rec.wait_stopped(stopped, STOP_WAIT_S)
            return self.send_json({**rec.listing(), "stopped": stopped})
        if p == "records/welcome":
            if not isinstance(b.get("dismissed"), bool):
                raise ValueError('send {"dismissed": true}')
            rec.set_choices(welcome_dismissed=b["dismissed"])
            return self.send_json(rec.listing())
        if p == "records/key":
            rec.save_key(b.get("username"), b.get("key"))
            return self.send_json(rec.listing())
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def send_tile(self, map_id, z, x, y, ext="webp"):
        """One tile of an installed map (or streamed Hub layer, cached infinitely in data/hub_cache),
        or an empty tile. Revalidated with an ETag rather than cached blindly unless ?v= is present."""
        if not (z <= MAX_TILE_ZOOM and x < (1 << z) and y < (1 << z)):
            self.send_error(HTTPStatus.BAD_REQUEST, "no such tile")
            return
        path = self.library.tiles_path(map_id)
        data = None
        etag = None
        ctype = "image/webp"
        has_ver = "v=" in (urlparse(self.path).query or "")
        try:
            etag = f'"{path.stat().st_mtime_ns:x}"'
        except FileNotFoundError:
            if self.hub and self.hub.config()["configured"]:
                try:
                    data, hub_ct = self.hub.get_streamed_tile(map_id, z, x, y)
                    if data is not None:
                        ctype = hub_ct or ctype
                        has_ver = True
                except hub_client.HubClientError:
                    data = None
        else:
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            try:
                with closing(tiling.connect_read_only(path)) as db:
                    data = tiling.read_tile(db, z, x, y)
            except sqlite3.Error as e:  # a damaged file shows as no map, not as a dropped connection
                print(f"tiles: {map_id}: {e}", file=sys.stderr, flush=True)
                data = None
        body = data or EMPTY_TILE
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype if data else "image/webp")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=31536000, immutable" if (has_ver and data) else "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _local_cached_fetch(self, url, *, cache_key=None, headers=None, data=None, method=None, timeout=25):
        """Infinite write-once disk cache (`data/raw/aerial_cache/`) for standalone desktop queries."""
        import hashlib

        cache_root = self.library.raw / "aerial_cache"
        cache_root.mkdir(parents=True, exist_ok=True)
        raw_key = cache_key or f"{method or 'GET'}:{url}:{hashlib.sha256(data or b'').hexdigest()[:12]}"
        digest = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        f_path = cache_root / f"{digest}.bin"
        if f_path.is_file():
            return f_path.read_bytes(), "application/octet-stream"

        def _fetch_and_persist():
            if f_path.is_file():
                return f_path.read_bytes(), "application/octet-stream"
            body, ctype = polite_fetch_bytes(
                url,
                pacer=self.local_pacer,
                headers=headers,
                data=data,
                method=method,
                timeout=timeout,
                allow_loopback=getattr(self, "allow_loopback", False),
            )
            part = cache_root / f"{digest}.{os.getpid()}.{threading.get_ident()}.partial"
            try:
                part.write_bytes(body)
                part.replace(f_path)
            finally:
                part.unlink(missing_ok=True)
            return body, ctype

        return self.local_flight.do(("local_cache", digest), _fetch_and_persist)

    def _local_dem_paths(self):
        dem_paths = []
        hand_path = None
        canopy_path = None
        if self.library.tiles.is_dir():
            for p in sorted(self.library.tiles.glob("*.mbtiles")):
                meta = tiling.read_metadata(p)
                if meta.get("encoding") != "terrarium":
                    continue
                lid = p.stem
                if "glo30_hand" in lid and hand_path is None:
                    hand_path = p
                elif "canopy" in lid and canopy_path is None:
                    canopy_path = p
                else:
                    dem_paths.append((meta.get("name") or lid, p))
        return dem_paths, hand_path, canopy_path

    def hub_api(self, method, p, q):
        """Routes under `/api/hub`: connect/disconnect, pull offline, push local maps to Hub."""
        hub = self.hub
        if method == "GET" and p == "hub":
            return self.send_json(hub.status_and_catalog())
        if method == "DELETE" and p == "hub/config":
            hub.disconnect()
            return self.send_json(hub.status_and_catalog())
        if method == "GET" and p.startswith("hub/job/"):
            return self.send_json(hub.poll_hub_job(p.removeprefix("hub/job/")))
        if method != "POST":
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        b = self.body()
        if p == "hub/config":
            hub.save_config(b.get("url"), b.get("api_key"), b.get("auto_push"))
            return self.send_json(hub.status_and_catalog())
        if p == "hub/auto-push":
            hub.set_auto_push(bool(b.get("auto_push")))
            return self.send_json(hub.status_and_catalog())
        if p == "hub/push":
            vis = str(b.get("visibility") or "hub")
            ids = b.get("ids") if isinstance(b.get("ids"), list) else ([b["id"]] if b.get("id") else [])
            if not ids:
                raise ValueError('Send {"id": "<map_id>"} or {"ids": [...]}.')
            pushed = [hub.push_local_layer(str(mid), self.library, visibility=vis)["layer"] for mid in ids]
            return self.send_json({**hub.status_and_catalog(), "pushed": pushed})
        if p == "hub/pull":
            lid = str(b.get("id") or "").strip()
            if not lid:
                raise ValueError('Send {"id": "<layer_id>"}.')
            hub.pull_pack_offline(lid)
            self.library.write_catalog()
            return self.send_json({**hub.status_and_catalog(), "pulled": lid})
        if p == "hub/build-dem":
            job = hub.request_hub_dem_build(
                str(b.get("pack") or "gedtm30"), b.get("bbox"), str(b.get("visibility") or "hub")
            )
            return self.send_json(job)
        if p == "hub/build-map":
            job = hub.request_hub_map_build(str(b.get("id") or "").strip(), str(b.get("visibility") or "hub"))
            return self.send_json(job)
        if p == "hub/layer/update":
            lid = str(b.get("id") or "").strip()
            updates = {k: b[k] for k in ("title", "date", "visibility", "group_tag", "licence_policy") if k in b}
            res = hub.update_remote_layer(lid, updates)
            return self.send_json({**hub.status_and_catalog(), **res})
        if p == "hub/layer/delete":
            lid = str(b.get("id") or "").strip()
            hub.delete_remote_layer(lid)
            return self.send_json({**hub.status_and_catalog(), "deleted": lid})
        if p == "hub/admin/secrets":
            hub.admin_save_secrets(b)
            return self.send_json(hub.status_and_catalog())
        if p == "hub/admin/proprietary":
            hub.admin_upsert_proprietary(b)
            return self.send_json(hub.status_and_catalog())
        if p == "hub/admin/proprietary/delete":
            hub.admin_delete_proprietary(str(b.get("id") or "").strip())
            return self.send_json(hub.status_and_catalog())
        if p == "hub/admin/keys":
            created = hub.admin_create_key(
                b.get("label", "Client key"),
                b.get("scopes", ["read", "request_build", "upload"]),
                b.get("group_tag", ""),
            )
            return self.send_json({**hub.status_and_catalog(), "created_key": created})
        if p == "hub/admin/keys/revoke":
            kid = str(b.get("key_id") or "").strip()
            hub.admin_revoke_key(kid)
            return self.send_json({**hub.status_and_catalog(), "revoked": kid})
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def local_keys_api(self, method):
        """Manage standalone local `.env` archive credentials (`USGS_USERNAME`, `USGS_M2M_TOKEN`,
        `EARTHDATA_TOKEN`, `MAPVIETNAM_CONTACT`) from the desktop Settings UI without echoing secrets."""
        env_path = self.library.root / ".env"
        if method == "GET":
            fvals = records.read_env(env_path)
            return self.send_json(
                {
                    "usgs_username": os.environ.get("USGS_USERNAME") or fvals.get("USGS_USERNAME") or "",
                    "has_usgs_token": bool(os.environ.get("USGS_M2M_TOKEN") or fvals.get("USGS_M2M_TOKEN")),
                    "has_earthdata_token": bool(os.environ.get("EARTHDATA_TOKEN") or fvals.get("EARTHDATA_TOKEN")),
                    "contact": os.environ.get("MAPVIETNAM_CONTACT") or fvals.get("MAPVIETNAM_CONTACT") or "",
                }
            )
        if method == "POST":
            b = self.body()
            allowed = {"USGS_USERNAME", "USGS_M2M_TOKEN", "EARTHDATA_TOKEN", "MAPVIETNAM_CONTACT"}
            unknown = set(b) - allowed
            if unknown:
                raise ValueError(f"Unsupported key(s): {', '.join(sorted(unknown))}")
            updates = {}
            for k, v in b.items():
                if v is None:
                    continue
                clean = str(v).strip()
                updates[k] = clean if clean else None
            env_path.parent.mkdir(parents=True, exist_ok=True)
            records.write_env(env_path, updates)
            return self.local_keys_api("GET")
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def terrain_api(self, method, p, q):
        """Routes under `/api/terrain`: list available/streaming/Hub DEMs or build a regional COG DEM."""
        if method == "GET" and p == "terrain/sources":
            local_dems = []
            if self.library.tiles.is_dir():
                for f in sorted(self.library.tiles.glob("*.mbtiles")):
                    meta = tiling.read_metadata(f)
                    if meta.get("encoding") == "terrarium":
                        local_dems.append(
                            {
                                "id": f.stem,
                                "title": meta.get("name") or f.stem,
                                "bounds": [float(v) for v in meta["bounds"].split(",")] if meta.get("bounds") else None,
                                "maxzoom": int(meta.get("maxzoom", 12)),
                                "tiles": f"/tiles/{f.stem}/{{z}}/{{x}}/{{y}}.webp",
                                "attribution": meta.get("attribution") or "Local Terrarium DEM",
                                "local": True,
                            }
                        )
            hub_dems = [
                {
                    "id": lyr["id"],
                    "title": lyr["title"],
                    "bounds": lyr["bounds"],
                    "maxzoom": lyr["maxzoom"],
                    "tiles": f"/tiles/{lyr['id']}/{{z}}/{{x}}/{{y}}.{lyr.get('format') or 'webp'}?v={lyr.get('revision', 1)}",
                    "attribution": lyr.get("attribution") or "Hub Terrarium DEM",
                    "local": False,
                }
                for lyr in self.hub.remote_layers().values()
                if lyr.get("encoding") == "terrarium" and not any(x["id"] == lyr["id"] for x in local_dems)
            ]
            return self.send_json(
                {
                    "streaming": list(terrain_aerial.STREAMING_TERRAIN_SOURCES),
                    "installed_dems": local_dems + hub_dems,
                    "buildable_packs": {
                        k: {"title": v["title"], "series": v["series"], "attribution": v["attribution"]}
                        for k, v in terrain_aerial.BUILDABLE_COG_PACKS.items()
                    },
                }
            )
        if method == "POST" and p == "terrain/build":
            b = self.body()
            pack = str(b.get("pack") or "gedtm30")
            bbox = terrain_aerial.check_dem_bbox(b.get("bbox"))
            target = str(b.get("target") or ("hub" if self.hub.config()["configured"] else "local"))
            if target == "hub" and self.hub.config()["configured"]:
                return self.send_json(self.hub.request_hub_dem_build(pack, bbox, str(b.get("visibility") or "hub")))
            slug = f"{bbox[0]:.2f}_{bbox[1]:.2f}_{bbox[2]:.2f}_{bbox[3]:.2f}".replace("-", "m").replace(".", "p")
            lid = f"dem_{pack}_{slug}"
            out_path = self.library.tiles_path(lid)
            if not out_path.is_file():
                terrain_aerial.build_cog_dem_pack(pack, bbox, out_path)
                self.library.write_catalog()
                if self.hub.config()["configured"] and self.hub.config()["auto_push"]:
                    req_vis = str(b.get("visibility") or "hub")

                    def _push_dem():
                        with contextlib.suppress(Exception):
                            self.hub.push_local_layer(lid, self.library, visibility=req_vis)

                    threading.Thread(target=_push_dem, daemon=True).start()
            return self.send_json({"layer_id": lid, "state": "done"})
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def elevation_api(self, q):
        lon, lat = float(q["lon"]), float(q["lat"])
        remote = q.get("remote", "0") in ("1", "true")
        dem_paths, hand_path, canopy_path = self._local_dem_paths()
        local_res = terrain_aerial.sample_point_terrain(
            lon, lat, dem_paths=dem_paths, hand_path=hand_path, canopy_path=canopy_path, remote_fallback=False
        )
        if local_res["ground_m"] is not None:
            return self.send_json(local_res)
        if self.hub.config()["configured"]:
            try:
                return self.send_json(
                    self.hub.proxy_get("/hub/v1/elevation", {"lon": lon, "lat": lat, "remote": "1" if remote else "0"})
                )
            except hub_client.HubClientError:
                pass
        if remote:
            local_res = terrain_aerial.sample_point_terrain(
                lon, lat, dem_paths=(), hand_path=hand_path, canopy_path=canopy_path, remote_fallback=True
            )
        self.send_json(local_res)

    def transect_api(self):
        b = self.body()
        start, end = b["start"], b["end"]
        dem_paths, _, canopy_path = self._local_dem_paths()
        chosen_dem = None
        if b.get("dem_id"):
            did = str(b["dem_id"])
            if not is_safe_id(did):
                raise ValueError("Invalid dem_id.")
            cand = self.library.tiles_path(did)
            if cand.is_file():
                chosen_dem = cand
        if chosen_dem is None:
            for _, cand in dem_paths:
                bounds = tiling.read_bounds(cand)
                if tiling.bounds_cover(bounds, start[0], start[1]) and tiling.bounds_cover(bounds, end[0], end[1]):
                    chosen_dem = cand
                    break
        if chosen_dem is None and self.hub.config()["configured"]:
            try:
                return self.send_json(self.hub.proxy_post("/hub/v1/transect", b))
            except hub_client.HubClientError:
                pass
        res = terrain_aerial.compute_transect(
            start,
            end,
            samples=b.get("samples", 64),
            observer_height_m=b.get("observer_height_m", 1.7),
            dem_path=chosen_dem,
            canopy_path=canopy_path,
            remote_cog=bool(b.get("remote_cog", False) and chosen_dem is None),
        )
        self.send_json(res)

    def lidar_api(self, q):
        bbox = number_list(q["bbox"], 4, "bbox", "w,s,e,n")
        if self.hub.config()["configured"]:
            return self.send_json(self.hub.proxy_get("/hub/v1/lidar", {"bbox": q["bbox"]}))
        self.send_json(terrain_aerial.query_sliderule_gedi(bbox, self._local_cached_fetch))

    def aerial_api(self, method, p, q):
        """Search historical overhead archives (proxying through Hub cache when connected) or import a frame."""
        if method == "POST" and p == "aerial/import":
            b = self.body()
            img_url = str(b.get("url") or "").strip()
            allow_lb = getattr(self, "allow_loopback", False)
            if not img_url.startswith("https://") and not (allow_lb and img_url.startswith("http://127.0.0.1:")):
                raise ValueError("Provide an https:// image URL to import.")
            raw, _ = polite_fetch_bytes(
                img_url,
                pacer=self.local_pacer,
                flight=self.local_flight,
                max_bytes=128 << 20,
                allow_loopback=allow_lb,
            )
            part = self.library.custom.incoming_path()
            try:
                part.write_bytes(raw)
                fname = Path(urlparse(img_url).path).name or "aerial.jpg"
                entry = self.library.custom.ingest(
                    part, fname, b.get("title") or "Historical aerial", b.get("year") or ""
                )
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            gcps = b.get("gcps")
            if entry["kind"] == "scan" and isinstance(gcps, list) and len(gcps) >= 3:
                # Scale Allmaps IIIF pixel coordinates if work.jpg differed from IIIF canvas size.
                src_w, src_h = b.get("source_width"), b.get("source_height")
                dst_w, dst_h = entry["image"]["width"], entry["image"]["height"]
                sx = (dst_w / float(src_w)) if src_w else 1.0
                sy = (dst_h / float(src_h)) if src_h else 1.0
                scaled = [[pt[0] * sx, pt[1] * sy, pt[2], pt[3]] for pt in gcps]
                self.library.place_custom(entry["id"], scaled, entry["title"], entry.get("year"))
                self.library.start_worker()
            elif self.library.custom.buildable(entry):
                self.library.add([entry["id"]])
                self.library.start_worker()
            listing = self.library.listing()
            return self.send_json(
                {"map": next(m for m in listing["maps"] if m["id"] == entry["id"]), "library": listing}
            )

        if method != "GET":
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        provider = p.removeprefix("aerial/")
        if self.hub.config()["configured"] and provider in ("nakala", "oam", "landsat", "allmaps", "usgs"):
            return self.send_json(self.hub.proxy_get(f"/hub/v1/aerial/{provider}", q))
        if provider == "nakala":
            return self.send_json(
                terrain_aerial.search_nakala_aerials(
                    q.get("q", ""), int(q.get("page", 1)), int(q.get("size", 12)), fetch_fn=self._local_cached_fetch
                )
            )
        if provider == "oam":
            return self.send_json(
                terrain_aerial.search_openaerialmap(
                    number_list(q["bbox"], 4, "bbox", "w,s,e,n"), fetch_fn=self._local_cached_fetch
                )
            )
        if provider == "landsat":
            return self.send_json(
                terrain_aerial.search_landsat_mss(
                    number_list(q["bbox"], 4, "bbox", "w,s,e,n"),
                    q.get("from", "1972-07-01"),
                    q.get("to", "1975-12-31"),
                    float(q.get("max_cloud", 35)),
                    fetch_fn=self._local_cached_fetch,
                )
            )
        if provider == "allmaps":
            return self.send_json(
                terrain_aerial.search_allmaps_iiif(
                    number_list(q["bbox"], 4, "bbox", "w,s,e,n"), fetch_fn=self._local_cached_fetch
                )
            )
        if provider == "usgs":
            env_vals = records.read_env(self.library.root / ".env")
            user = os.environ.get("USGS_USERNAME") or env_vals.get("USGS_USERNAME") or ""
            tok = os.environ.get("USGS_M2M_TOKEN") or env_vals.get("USGS_M2M_TOKEN") or ""
            return self.send_json(
                terrain_aerial.search_usgs_declass(
                    number_list(q["bbox"], 4, "bbox", "w,s,e,n"),
                    q.get("dataset", "corona"),
                    q.get("from", "1960-01-01"),
                    q.get("to", "1984-12-31"),
                    username=user,
                    token=tok,
                    fetch_fn=self._local_cached_fetch,
                )
            )
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def media_api(self, method, p, q, db):
        """Geolocated oblique aerials and plot cards in `evidence.sqlite` (`media` table) + Hub sync."""
        if method == "POST" and p == "media/delete":
            b = self.body()
            mid = str(b.get("id") or "").strip()
            if mid.startswith("local_") and mid[6:].isdigit():
                db.execute("delete from media where id = ?", (int(mid[6:]),))
                db.commit()
                return self.send_json({"deleted": mid})
            if self.hub.config()["configured"]:
                self.hub.delete_remote_media(mid)
                return self.send_json({"deleted": mid})
            raise ValueError("Media item not found.")
        if method == "GET":
            bbox = number_list(q["bbox"], 4, "bbox", "w,s,e,n") if q.get("bbox") else None
            sql = "select * from media where lon is not null and lat is not null"
            params = []
            if bbox:
                sql += " and lon between ? and ? and lat between ? and ?"
                params = [bbox[0], bbox[2], bbox[1], bbox[3]]
            rows = db.execute(sql + " order by id desc limit 500", params).fetchall()
            features = []
            for r in rows:
                extra = {}
                cap = r["caption"] or ""
                if cap.startswith("{"):
                    try:
                        extra = json.loads(cap)
                        cap = extra.get("title") or ""
                    except ValueError:
                        pass
                features.append(
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                        "properties": {
                            "id": f"local_{r['id']}",
                            "title": cap or "Aerial photo",
                            "date": r["taken"] or "",
                            "uncertainty_m": r["uncertainty_m"] or 100.0,
                            "azimuth_deg": extra.get("azimuth_deg"),
                            "fov_deg": extra.get("fov_deg"),
                            "image_url": r["path"],
                            "plot_url": extra.get("plot_url") or "",
                            "licence": extra.get("licence") or "",
                        },
                    }
                )
            if self.hub.config()["configured"]:
                try:
                    hub_fc = self.hub.proxy_get("/hub/v1/media", {"bbox": q["bbox"]} if q.get("bbox") else None)
                    if hub_fc and isinstance(hub_fc.get("features"), list):
                        features.extend(hub_fc["features"])
                except hub_client.HubClientError:
                    pass
            return self.send_json({"type": "FeatureCollection", "features": features}, ctype="application/geo+json")
        if method == "POST":
            b = self.body()
            lon, lat = finite_number(b, "lon", -180, 180), finite_number(b, "lat", -90, 90)
            unc = finite_number(b, "uncertainty_m")
            if unc <= 0:
                raise ValueError("uncertainty_m must be > 0")
            img_url = text_field(b, "image_url")
            title = text_field(b, "title", False) or "Oblique aerial photo"
            meta_blob = json.dumps(
                {
                    "title": title,
                    "azimuth_deg": b.get("azimuth_deg"),
                    "fov_deg": b.get("fov_deg"),
                    "plot_url": b.get("plot_url") or "",
                    "licence": b.get("licence") or "",
                },
                ensure_ascii=False,
            )
            cur = db.execute(
                "insert into media (path, kind, taken, lon, lat, uncertainty_m, caption, created_at) values (?,?,?,?,?,?,?,?)",
                (img_url, "oblique_aerial", str(b.get("date") or "")[:32], lon, lat, unc, meta_blob, store.now()),
            )
            db.commit()
            if b.get("push_to_hub") and self.hub.config()["configured"]:
                with contextlib.suppress(hub_client.HubClientError):
                    self.hub.proxy_post("/hub/v1/media", {**b, "title": title})
            return self.send_json({"id": cur.lastrowid}, HTTPStatus.CREATED)
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_GET(self):
        if self.route("GET"):
            return
        # Static files and tiles get the same Host check as the API, so a DNS-rebinding page
        # cannot read which maps are installed.
        if self.headers.get("Host") not in self.own_hosts():
            return self.send_error(HTTPStatus.FORBIDDEN, "this server only answers requests addressed to itself")
        m = TILE_PATH.fullmatch(urlparse(self.path).path)
        if m:
            return self.send_tile(m[1], int(m[2]), int(m[3]), int(m[4]), m[5])
        super().do_GET()

    def do_POST(self):
        if not self.route("POST"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)

    def do_DELETE(self):
        if not self.route("DELETE"):
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)


class MapServer(ThreadingHTTPServer):
    """The viewer's server, which can be told to stop itself: by the viewer (POST /api/quit) or
    by a signal (start.py stopping it, or Control-C when it is run by hand)."""

    library = None  # set by make_server
    records = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._quit_lock = threading.Lock()
        self._quitting = False

    def quit(self):
        """Stop cleanly, from any thread, and return at once: the jobs under way are ended
        first (no ingester or map builder is left running without a server), then
        serve_forever returns. Asking twice does nothing more."""
        with self._quit_lock:
            if self._quitting:
                return
            self._quitting = True
        # shutdown() waits for serve_forever, so it must not run on the thread that serves
        # (a signal handler does); a thread of its own is right from anywhere.
        threading.Thread(target=self._stop, name="quit", daemon=True).start()

    def handle_error(self, request, client_address):
        """A browser that gives up on a request (the viewer cancels tile fetches as the map
        pans) resets the connection; that is not worth a traceback on the console."""
        if not isinstance(sys.exception(), ConnectionError):
            super().handle_error(request, client_address)

    def _stop(self):
        try:
            self.records.stop()
            self.library.stop()
        finally:
            self.shutdown()


def make_server(
    port=8765,
    db_path=store.DB_PATH,
    map_library=None,
    web=WEB,
    record_sources=None,
    hub=None,
    *,
    allow_loopback=False,
):
    """A threaded server on 127.0.0.1:`port` (0 picks a free port) over the store at `db_path`,
    serving the viewer's files in `web`, the maps of `map_library` (whose folder also holds
    catalog.json and tiles/), the record sources of `record_sources`, and an optional `hub` bridge."""
    lib = map_library or library.Library()
    rec = record_sources or records.Records(db_path=db_path)
    hc = hub or hub_client.HubClient(root=lib.root)
    rec.on_change = forget_caches

    def _auto_push_installed(map_id):
        if hc.config().get("configured") and hc.config().get("auto_push"):
            vis = "private" if str(map_id).startswith("custom_") else "hub"

            def _push():
                with contextlib.suppress(Exception):
                    hc.push_local_layer(map_id, lib, visibility=vis, preserve_visibility=True)

            threading.Thread(target=_push, name=f"auto-push-{map_id}", daemon=True).start()

    lib.on_installed = _auto_push_installed
    pacer = HostPacer()
    flight = SingleFlight()
    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "db_path": Path(db_path),
            "data_web": lib.root / "web",
            "library": lib,
            "records": rec,
            "hub": hc,
            "local_pacer": pacer,
            "local_flight": flight,
            "allow_loopback": bool(allow_loopback),
        },
    )
    srv = MapServer(("127.0.0.1", port), partial(handler, directory=str(web)))
    srv.library, srv.records, srv.hub = lib, rec, hc
    return srv


def main():
    """Serve the viewer and its API on 127.0.0.1 until told to stop (POST /api/quit, SIGTERM,
    SIGHUP or Control-C); then exit with status 0."""
    ap = argparse.ArgumentParser(description="Serve the Map Vietnam viewer and its JSON API on 127.0.0.1.")
    ap.add_argument("--port", type=int, default=8765, help="port on 127.0.0.1 (default 8765)")
    ap.add_argument("--db", default=str(store.DB_PATH), help="evidence database (default data/evidence.sqlite)")
    args = ap.parse_args()
    lib = library.Library()
    # Maps whose adding was cut short when the server last stopped: clean up and say so.
    lib.recover_interrupted()
    # The command line may have added or removed maps while no server ran: start from the truth.
    lib.write_catalog()
    srv = make_server(args.port, args.db, lib)
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda signum, frame: srv.quit())
    print(f"http://127.0.0.1:{srv.server_address[1]}", flush=True)  # the real port when --port is 0
    srv.serve_forever()
    srv.server_close()


if __name__ == "__main__":
    main()
