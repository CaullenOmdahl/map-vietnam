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
  GET  /api/cases/<id>                (items, sites, notes)      DELETE /api/cases/<id>
  POST /api/cases/<id>/items {evidence_id, note}    DELETE /api/cases/<id>/items/<evidence_id>
  POST /api/cases/<id>/sites {name, lon, lat, uncertainty_m, rationale}
  DELETE /api/cases/<id>/sites/<site_id>
  POST /api/cases/<id>/notes {body}           DELETE /api/cases/<id>/notes/<note_id>
  GET  /api/cases/<id>/export.geojson | export.kml | export.gpx

Usage: .venv/bin/python scripts/server.py [--port 8765] [--db data/evidence.sqlite]
"""

import argparse
import json
import math
import re
import signal
import sqlite3
import sys
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

import gridref
import library
import records
import store
import tiling

WEB = store.ROOT / "web"
EMPTY_TILE = tiling.empty_tile()
TILE_PATH = re.compile(r"/tiles/([A-Za-z0-9_.-]+)/(\d{1,2})/(\d{1,10})/(\d{1,10})\.webp")
# Deeper than any map is tiled (z16), and far inside what SQLite's integers and the TMS row flip hold.
MAX_TILE_ZOOM = 24
# Sec-Fetch-Site values of requests made by this server's own pages (or typed by the user).
OWN_FETCH_SITES = {"same-origin", "none"}
MAX_BODY_BYTES = 1 << 20  # the API's JSON bodies are tiny; refuse anything bigger
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
        sql += f" and id in ({','.join('?' * len(ids))})"
        args += ids
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
    """A case with its pinned records (each with its case note), sites and notes; None if absent."""
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
    return {
        **dict(c),
        "items": [dict(r) for r in items],
        "sites": [dict(r) for r in sites],
        "notes": [dict(r) for r in notes],
    }


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


def export(case, fmt):
    """(content type, text) of a case exported as `fmt`: "geojson", "kml" or "gpx".

    Every format carries the hazard warning and the case notes; each point says how far
    (its uncertainty radius) the true position may lie from it.
    """
    pts = export_points(case)
    title = f"{case['name']} ({HAZARD})"
    notes = [n["body"] for n in case["notes"]]
    notes_text = escape("\n".join(notes))
    if fmt == "geojson":
        return "application/geo+json", json.dumps(
            {
                "type": "FeatureCollection",
                "name": case["name"],
                "warning": HAZARD,
                "notes": notes,
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
                        "properties": {"name": p["name"], "uncertainty_m": p["r"], "description": p["desc"]},
                    }
                    for p in pts
                ],
            },
            indent=1,
        )
    if fmt == "kml":
        marks = "".join(
            f"<Placemark><name>{escape(p['name'])}</name><description>{escape(p['desc'])} "
            f"(within {p['r']:.0f} m)</description><Point><coordinates>{p['lon']},{p['lat']}</coordinates>"
            f"</Point></Placemark>"
            for p in pts
        )
        return "application/vnd.google-earth.kml+xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{escape(title)}</name><description>{notes_text}</description>{marks}</Document></kml>"
        )
    if fmt == "gpx":
        # GPS units truncate long waypoint names; keep the start, which carries the place name.
        wpts = "".join(
            f'<wpt lat="{p["lat"]}" lon="{p["lon"]}"><name>{escape(p["name"][:60])}</name>'
            f"<desc>{escape(p['desc'])} (within {p['r']:.0f} m)</desc></wpt>"
            for p in pts
        )
        return "application/gpx+xml", (
            f'<?xml version="1.0" encoding="UTF-8"?><gpx version="1.1" creator="MapVietnam" '
            f'xmlns="http://www.topografix.com/GPX/1/1"><metadata><name>{escape(title)}</name><desc>{notes_text}</desc></metadata>{wpts}</gpx>'
        )
    raise ValueError(f"unknown export format {fmt!r}")


class Handler(SimpleHTTPRequestHandler):
    """Serves web/, the tiles and the /api/ routes listed in the module docstring, one store
    connection per request."""

    db_path = store.DB_PATH
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

    def body(self):
        """The request's JSON body, which must be an object ({} if empty)."""
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 <= n <= MAX_BODY_BYTES:
            raise ValueError("request body too large" if n > 0 else "bad Content-Length")
        data = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("send a JSON object")
        return data

    def send_failure(self, error):
        """Answer 500 for a failure of the server itself, logging the traceback for the console."""
        traceback.print_exception(error, file=sys.stderr)
        self.send_json({"error": f"the server failed: {error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

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
            elif p == "quit" and method == "POST":
                self.body()  # refuse a body that is not a JSON object, like every other POST
                self.send_json({"quitting": True})
                self.server.quit()
            else:
                # closing() releases the connection; the inner `with` rolls back on an error.
                with closing(self.db()) as db, db:
                    self.dispatch(method, p, q, db)
        except records.RecordsError as e:  # plain words, sometimes with facts the viewer acts on
            self.send_json({"error": str(e), **e.extra}, e.status)
        except KeyError as e:  # a field or query value the request left out
            self.send_json({"error": f"{e.args[0]} is missing"}, HTTPStatus.BAD_REQUEST)
        except (ValueError, TypeError) as e:  # LibraryError is a ValueError
            self.send_json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
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
        m = re.fullmatch(r"cases/(\d{1,18})(?:/(items|sites|notes)(?:/(\d{1,18}))?|/export\.(geojson|kml|gpx))?", p)
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
        if not m:
            return self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        cid, sub, sub_id, fmt = int(m[1]), m[2], m[3], m[4]
        case = case_detail(db, cid)
        if case is None:
            return self.send_json({"error": "no such case"}, HTTPStatus.NOT_FOUND)
        if method == "GET" and fmt:
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
        self.send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

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

    def send_tile(self, map_id, z, x, y):
        """One tile of an installed map, or an empty tile. Revalidated with an ETag rather than
        cached blindly: a map removed and added again must not show stale tiles."""
        if not (z <= MAX_TILE_ZOOM and x < (1 << z) and y < (1 << z)):
            self.send_error(HTTPStatus.BAD_REQUEST, "no such tile")
            return
        path = self.library.tiles_path(map_id)
        try:
            etag = f'"{path.stat().st_mtime_ns:x}"'
        except FileNotFoundError:
            data, etag = None, None
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
        self.send_header("Content-Type", "image/webp")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.route("GET"):
            return
        # Static files and tiles get the same Host check as the API, so a DNS-rebinding page
        # cannot read which maps are installed.
        if self.headers.get("Host") not in self.own_hosts():
            return self.send_error(HTTPStatus.FORBIDDEN, "this server only answers requests addressed to itself")
        m = TILE_PATH.fullmatch(urlparse(self.path).path)
        if m:
            return self.send_tile(m[1], int(m[2]), int(m[3]), int(m[4]))
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


def make_server(port=8765, db_path=store.DB_PATH, map_library=None, web=WEB, record_sources=None):
    """A threaded server on 127.0.0.1:`port` (0 picks a free port) over the store at `db_path`,
    serving the files in `web`, the maps of `map_library` and the record sources of
    `record_sources` (default: this repository's)."""
    lib = map_library or library.Library()
    rec = record_sources or records.Records(db_path=db_path)
    rec.on_change = forget_caches
    handler = type("BoundHandler", (Handler,), {"db_path": Path(db_path), "library": lib, "records": rec})
    srv = MapServer(("127.0.0.1", port), partial(handler, directory=str(web)))
    srv.library, srv.records = lib, rec
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
    print(f"http://127.0.0.1:{args.port}", flush=True)
    srv.serve_forever()
    srv.server_close()


if __name__ == "__main__":
    main()
