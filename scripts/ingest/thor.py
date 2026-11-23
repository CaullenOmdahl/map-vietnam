"""THOR (Theater History of Operations) Vietnam air operations -> air_strike evidence.

Source: Kaggle dataset usaf/vietnam-war-bombing-operations (CC0; the only complete copy
still online). The 1.6 GB CSV is downloaded once (as a ~320 MB zip) into data/raw/thor/ and
streamed; only rows inside --bbox are stored. Kaggle gives the file without a key at the time of
writing. A key is sent when one is saved: KAGGLE_USERNAME / KAGGLE_KEY (environment or .env, see
.env.example) or Kaggle's own kaggle.json (~/.kaggle/, or $KAGGLE_CONFIG_DIR). If Kaggle asks for
a key and none it accepts is saved, this exits with status 3 (KEY_EXIT) and one plain sentence,
so whoever ran it can ask the person for one. Once the zip is cached Kaggle is not contacted.

Positions: THOR's own *_WGS84 columns are off by up to ~300 m in longitude against the
original grids (checked on several Cao Lanh records), so when the original coordinate is an
MGRS string we re-derive the point from it on Indian 1960 — the datum of the maps the
reports were read from — and keep THOR's value in raw. Even so, a THOR point is the
*target*, not the impact: uncertainty is never below 300 m (FAQ: "100 m for the best
coordinates, but can vary to as many as 1,000 m").

These rows are strike records, not loss records: they mark where fighting happened and
where unexploded ordnance may lie. Kinetic missions only; reconnaissance etc. are skipped.

Each run makes the store hold exactly the strikes inside the chosen area for source "thor"
(store.sync_source): strikes outside it, or no longer in the file, are deleted unless an analyst
holds them in a case, in which case they are marked withdrawn. Other sources are never touched.
The default area is common.AREA_OF_INTEREST (around Cao Lanh); running with a wider --bbox or --all-vietnam widens it.

Size: the whole of Vietnam is about 3 million strikes. Every strike kept is held in memory until
it is written (about 2.2 KB each, so some 7 GB for the whole country) and takes about 1.8 KB in the
store, and the viewer draws every strike in view, so a wide area makes everything slow.
--max-strikes N stops the run, before anything is written, as soon as the area turns out to hold
more than N strikes; scripts/records.py always passes it (records.MAX_STRIKES).

Usage: .venv/bin/python scripts/ingest/thor.py [--bbox w,s,e,n] [--all-vietnam] [--max-strikes N]
"""

import argparse
import base64
import csv
import io
import json
import os
import re
import sys
import urllib.error
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import gridref  # noqa: E402
import store  # noqa: E402

SOURCE = "thor"
URL = "https://www.kaggle.com/api/v1/datasets/download/usaf/vietnam-war-bombing-operations/THOR_Vietnam_Bombing_Operations.csv"
ZIP_NAME = "THOR_Vietnam_Bombing_Operations.csv.zip"
VIETNAM_BBOX = (102.1, 8.4, 109.5, 23.4)
MGRS_RE = re.compile(r"^\s*(\d{1,2})([C-HJ-NP-X])([A-HJ-NP-Z]{2})(\d+)\s*$")
# The exit status that tells scripts/records.py (records.KEY_EXIT) that Kaggle wants a key, so the
# viewer can ask the person for one. Any other failure exits with status 1, as Python does.
KEY_EXIT = 3
# How to get a key, in the words both the viewer and the terminal show. No message here may ever
# include the username or the key.
KEY_HELP = (
    "To get a free Kaggle key: sign in at kaggle.com, open Settings, and under 'API' click "
    "'Create Legacy API Key' (older pages: 'Create New Token'), then choose the kaggle.json it downloads "
    "where Map Vietnam asks for the key (or put its username and key in .env as KAGGLE_USERNAME / KAGGLE_KEY)."
)
KEY_WANTED = (
    "Kaggle asked for a key before it would hand over the air strike records, and none is saved yet. " + KEY_HELP
)
KEY_REFUSED = "Kaggle did not accept this key (it answered {code}). Make a new key and enter it again. " + KEY_HELP
TOO_MANY = (
    "This area holds more than {limit:,} air strikes, too many for the map to stay quick. "
    "Nothing was changed. Choose a smaller area."
)
ROWS_PER_REPORT = 50_000  # rows read between two progress lines (about one a second)
csv.field_size_limit(1 << 24)


def kaggle_config_file():
    """Where the Kaggle tools keep kaggle.json: $KAGGLE_CONFIG_DIR, else ~/.kaggle."""
    folder = os.environ.get("KAGGLE_CONFIG_DIR") or Path.home() / ".kaggle"
    return Path(folder) / "kaggle.json"


def kaggle_credentials():
    """(username, key) from the environment / .env, else from kaggle.json; None if neither has both."""
    common.load_env()
    user, key = os.environ.get("KAGGLE_USERNAME"), os.environ.get("KAGGLE_KEY")
    if user and key:
        return user, key
    try:
        cfg = json.loads(kaggle_config_file().read_text())
    except (OSError, ValueError):
        return None
    if isinstance(cfg, dict) and cfg.get("username") and cfg.get("key"):
        return cfg["username"], cfg["key"]
    return None


def stop_for_key(message):
    """Stop with one plain sentence and the exit status that says Kaggle wants a key (KEY_EXIT)."""
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(KEY_EXIT)


def download():
    """Path of the cached THOR zip, downloading it first if it is missing.

    Kaggle gives this file without a key at the time of writing, so a key is used when one is
    saved and otherwise none is sent. If Kaggle refuses the saved key (401 or 403), the download
    is tried once more without it: a stale key must not stand in the way of a file anyone may
    have. Only when Kaggle still refuses, or sends a web page instead of the file, does this
    stop asking for a key (stop_for_key)."""
    cached = common.RAW / SOURCE / ZIP_NAME
    if cached.exists():
        return cached
    creds = kaggle_credentials()
    refusal = None
    for attempt in (creds, None) if creds else (None,):
        headers = None
        if attempt:
            auth = base64.b64encode(f"{attempt[0]}:{attempt[1]}".encode()).decode()
            headers = {"Authorization": f"Basic {auth}"}
        try:
            path = common.cached_get(URL, SOURCE, name=ZIP_NAME, headers=headers, timeout=3600)
        except urllib.error.HTTPError as e:
            if e.code not in (401, 403):
                raise
            refusal = refusal or e.code  # the answer to the key, if one was sent
            continue
        with open(path, "rb") as f:
            if not f.read(64).lstrip().startswith(b"<"):
                return path
        # A sign-in page saved under the file's name: never keep it as if it were the records.
        path.unlink()
    stop_for_key(KEY_REFUSED.format(code=refusal or "with a sign-in page") if creds else KEY_WANTED)


def open_rows(path):
    """Yield CSV rows from the downloaded file, zipped or not, reporting progress (common.progress)
    every ROWS_PER_REPORT rows: the share of the file's bytes read so far, and the row count."""
    if zipfile.is_zipfile(path):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
        raw, total = z.open(name), z.getinfo(name).file_size
    else:
        raw, total = open(path, "rb"), Path(path).stat().st_size
    f = io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline="")
    for n, row in enumerate(csv.DictReader(f), 1):
        if n % ROWS_PER_REPORT == 0:
            # raw.tell() runs a little ahead of the row (the text layer reads in blocks): near enough.
            common.progress(raw.tell() / total if total else None, f"Reading the air strike file: {n:,} rows so far")
        yield row


def num(v):
    """float(v), or None for blanks and non-numbers."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def position(row):
    """(lon, lat, uncertainty_m, original_coord, original_datum) for a row, or None if it has no
    usable position. MGRS originals are re-derived on Indian 1960; otherwise THOR's WGS84 columns
    are used with a 1 km radius."""
    orig = (row.get("TGTORIGCOORDS") or "").strip()
    lat, lon = num(row.get("TGTLATDD_DDD_WGS84")), num(row.get("TGTLONDDD_DDD_WGS84"))
    m = MGRS_RE.match(orig.upper())
    if m and len(m[4]) % 2 == 0 and len(m[4]) >= 4:
        try:
            glon, glat, r = gridref.to_wgs84(orig, datum="indian1960")
            return glon, glat, max(300.0, r + 100), orig, "indian1960 (assumed, MGRS re-derived)"
        except ValueError:
            pass
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return lon, lat, 1000.0, orig or f"{lat},{lon}", "wgs84 (THOR-converted)"


def to_evidence(row):
    """store.Evidence for a kinetic, placeable row with a THOR id; None otherwise."""
    if (row.get("MFUNC_DESC_CLASS") or "").upper() != "KINETIC":
        return None
    rid = (row.get("THOR_DATA_VIET_ID") or "").strip()
    if not rid:  # no stable id -> cannot be kept in step with the source across runs
        return None
    pos = position(row)
    if not pos:
        return None
    lon, lat, r, orig, datum = pos
    date = (row.get("MSNDATE") or "").strip()
    if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", date):
        mo, d, y = date.split("/")
        date = f"{y}-{int(mo):02d}-{int(d):02d}"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        date = None
    tgt = (row.get("TGTTYPE") or "").strip().title()
    weapon = (row.get("WEAPONTYPE") or "").strip()
    aircraft = (row.get("VALID_AIRCRAFT_ROOT") or row.get("AIRCRAFT_ROOT") or "").strip()
    who = " ".join(filter(None, [row.get("COUNTRYFLYINGMISSION", "").strip(), row.get("MILSERVICE", "").strip()]))
    title = f"Air strike: {row.get('MFUNC_DESC', '').strip().title() or 'mission'}" + (f" on {tgt}" if tgt else "")
    desc = "; ".join(
        filter(
            None,
            [
                f"{who} {aircraft}".strip(),
                f"{row.get('NUMWEAPONSDELIVERED', '').strip()} x {weapon}" if weapon else "",
                f"BDA: {row['RESULTSBDA'].strip()}" if (row.get("RESULTSBDA") or "").strip() else "",
                f"operation {row['OPERATIONSUPPORTED'].strip()}"
                if (row.get("OPERATIONSUPPORTED") or "").strip()
                else "",
                f"record {row.get('SOURCERECORD', '').strip()}",
                "possible unexploded ordnance",
            ],
        )
    )
    return store.Evidence(
        source=SOURCE,
        source_record_id=rid,
        kind="air_strike",
        lon=lon,
        lat=lat,
        uncertainty_m=r,
        original_coord=orig,
        original_datum=datum,
        title=title,
        date_start=date,
        description=desc,
        raw={k: v for k, v in row.items() if v not in ("", None)},
    )


def select(rows, bbox, counts=None):
    """Yield evidence for kinetic, placeable rows whose re-derived point lies inside bbox."""
    counts = counts if counts is not None else {}
    counts.setdefault("seen", 0)
    counts.setdefault("skipped", 0)
    # Pre-filter on THOR's own coordinates with a 0.02° margin, then place precisely.
    pad = (bbox[0] - 0.02, bbox[1] - 0.02, bbox[2] + 0.02, bbox[3] + 0.02)
    for row in rows:
        counts["seen"] += 1
        lat, lon = num(row.get("TGTLATDD_DDD_WGS84")), num(row.get("TGTLONDDD_DDD_WGS84"))
        if lat is None or lon is None or not common.in_bbox(lon, lat, pad):
            continue
        e = to_evidence(row)
        if e is None or not common.in_bbox(e.lon, e.lat, bbox):
            counts["skipped"] += 1
            continue
        yield e


def write(db, records):
    """Register the source and sync source "thor" to exactly `records` (an iterable, streamed).
    Returns (kept, deleted, withdrawn)."""
    store.upsert_source(
        db,
        SOURCE,
        name="THOR Vietnam air operations (USAF/Kaggle)",
        url="https://www.kaggle.com/datasets/usaf/vietnam-war-bombing-operations",
        licence="CC0 / US Government public domain",
        notes="Target points of strike sorties 1965-73 (no Nov 1967). Indirect evidence of combat; UXO hazard.",
    )
    return store.sync_source(db, SOURCE, records, reason="no longer in THOR within the ingested area")


def parse_args(argv=None):
    """Command-line options: the area to keep (default common.AREA_OF_INTEREST, around Cao Lanh)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--bbox", default=",".join(map(str, common.AREA_OF_INTEREST)), help="area to keep: west,south,east,north"
    )
    ap.add_argument("--all-vietnam", action="store_true", help="keep every strike in Vietnam's bounding box")
    ap.add_argument(
        "--max-strikes",
        type=int,
        metavar="N",
        help="stop, changing nothing, if the area holds more than N strikes (default: no limit)",
    )
    return ap.parse_args(argv)


def main(argv=None):
    """Download (once), filter and sync. Returns (kept, deleted, withdrawn)."""
    a = parse_args(argv)
    bbox = VIETNAM_BBOX if a.all_vietnam else tuple(map(float, a.bbox.split(",")))
    path = download()
    db = store.connect()
    counts = {}
    # Read everything first: streaming into sync_source would hold the SQLite write lock for the
    # whole multi-minute CSV scan and block other ingesters (and the viewer's own queries).
    records = []
    for e in select(open_rows(path), bbox, counts):
        records.append(e)
        if a.max_strikes is not None and len(records) > a.max_strikes:
            raise SystemExit(TOO_MANY.format(limit=a.max_strikes))
    common.progress(None, f"Saving {len(records):,} air strikes")
    kept, deleted, withdrawn = write(db, records)
    print(
        f"thor: read {counts['seen']} rows, kept {kept} kinetic strikes in {bbox} "
        f"(deleted {deleted}, withdrawn {withdrawn}), skipped {counts['skipped']} (non-kinetic/unplaceable)"
    )
    return kept, deleted, withdrawn


if __name__ == "__main__":
    main()
