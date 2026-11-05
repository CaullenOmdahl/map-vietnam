"""NGA GEOnet Names Server (GNS) country file for Vietnam -> placename evidence + name index.

GNS is the modern US Board on Geographic Names gazetteer: every approved (N), variant (V),
conventional (C) and other name of each feature, in WGS84, with decimal degrees, DMS and MGRS.
Variant names matter here: many are the 1960s AMS/BGN spellings without diacritics that wartime
reports use, so every name row becomes its own evidence record.

geonames.nga.mil failed DNSSEC validation when this was written (2026-11-03), so we pin the only
Wayback Machine capture of the country zip (raw "id_" form, byte-identical to the original).

Outputs:
  - evidence rows (source "gns") in data/evidence.sqlite;
  - data/work/gns_index.json: normalised name -> [{lon, lat, fc, dsg, adm1, ...}] for other
    ingesters to geocode hamlet/commune names.

Known formal burials (CMTY cemeteries, TMB tombs, shrines that are named graves) are dropped,
together with every other name of the same feature.

Run:  .venv/bin/python scripts/ingest/gns.py [--refresh]
"""

import argparse
import csv
import io
import json
import math
import re
import sys
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402
import store  # noqa: E402

SOURCE = "gns"
WAYBACK_TS = "20260903061555"  # the only capture in the CDX index (checked 2026-11-03)
ORIGINAL_URL = "https://geonames.nga.mil/geonames/GNSData/Vietnam.zip"
URL = f"http://web.archive.org/web/{WAYBACK_TS}id_/{ORIGINAL_URL}"
ZIP_NAME = f"Vietnam_{WAYBACK_TS}.zip"
INDEX_PATH = store.ROOT / "data" / "work" / "gns_index.json"

# Formal burials: never stored (rule 4 in docs/how-it-works.md).
BURIAL_DESIGS = {"CMTY", "TMB", "GRVE", "BUR", "MSLM"}
# Matched on the lowercased NFC name WITH diacritics: "mộ" (grave) must not catch "mỏ" (mine).
# Only applied to spot (S) and area (L) features: villages called "Nghĩa Trang", "Làng Mả" or
# "Mộ Đức" are places, not graves. "Nghĩa trang/địa" (cemetery) and "lăng mộ" (tomb) denote a
# burial wherever they appear; the short words only when they head the name, because names such
# as "Đập Mả Tổ" (a dam), "Ga Mộ Đức" (a station) or "Núi Ga Lăng" (a hill) merely borrow a
# place name. Checked against the whole 2026-09 file: outside the CMTY/TMB/SHRN rows below, no
# S/L name heads with these words, and every name containing them elsewhere is a settlement,
# station, dam, bridge or hill.
BURIAL_NAME_RE = re.compile(
    r"\b(nghĩa trang|nghĩa địa|lăng mộ)\b"
    r"|^(mộ|mả|mồ mả) "
    r"|cemetery|cimeti[eè]re|\bgraves?\b|\btombs?\b|mausoleum"
)
# "Lăng X" (royal or notable's tomb) is a burial only for spot features; L-class "Lăng ..."
# localities are ordinary place names.
TOMB_SPOT_RE = re.compile(r"^lăng ")
# Military designations. AIRF/AIRQ/AIRS in Vietnam are wartime military airfields/seaplane bases
# (Bien Hoa, Phan Rang, Soc Trang, Dak To 2, abandoned Vinh Long...); AIRP (civil airport) is not.
MILITARY_DESIGS = {"MILB", "INSM", "AIRB", "NVB", "FT", "AIRF", "AIRQ", "AIRS", "CMPMN", "TRANT"}
# Feature classes we do not store: U = undersea (offshore banks/shoals), A/PCLI = the whole country.
SKIP_FC = {"U"}
SKIP_DESIGS = {"PCLI"}

# Approximate half-size (m) of the named thing, i.e. how far a report "at X" can be from GNS's point.
EXTENT_M = {
    "PPL": 250,
    "PPLL": 250,
    "PPLX": 300,
    "PPLQ": 250,
    "PPLW": 250,
    "PPLF": 250,
    "PPLA": 3000,
    "PPLA2": 1500,
    "PPLA3": 1000,
    "PPLC": 6000,
    "ADM1": 40000,
    "ADM2": 12000,
    "ADM3": 3000,
    "ADM4": 1000,
    "ADMD": 3000,
    "CNL": 3000,
    "CNLX": 3000,
    "STM": 5000,
    "STMI": 3000,
    "STMX": 3000,
    "STMD": 3000,
    "STMA": 3000,
    "STMM": 1000,
    "STMB": 2000,
    "STMQ": 3000,
    "CRKT": 2000,
    "CHNM": 1000,
    "LK": 1000,
    "LKS": 1500,
    "LGN": 2000,
    "BAY": 3000,
    "MRSH": 2000,
    "SWMP": 2000,
    "MT": 500,
    "MTS": 5000,
    "HLL": 400,
    "HLLS": 1500,
    "RDGE": 2000,
    "PK": 300,
    "ISL": 1000,
    "ISLS": 3000,
    "PLAT": 3000,
    "PLN": 3000,
    "DLTA": 10000,
    "VAL": 2000,
    "PEN": 3000,
    "LCTY": 1000,
    "AREA": 3000,
    "RGN": 20000,
    "INDS": 1000,
    "RESF": 5000,
    "RESN": 5000,
    "AIRF": 800,
    "AIRQ": 800,
    "AIRP": 1000,
    "FRM": 500,
    "EST": 1000,
}
DEFAULT_EXTENT_M = 250
LINEAR_DESIGS = {"STM", "STMI", "STMX", "STMD", "STMA", "STMB", "STMQ", "CNL", "CNLX", "CRKT", "RDGE", "MTS"}

# Prefixes stripped to make alias keys in the index ("Xã Mỹ Tân" -> "my tan").
ADMIN_PREFIXES = ("thanh pho", "thi tran", "thi xa", "xa", "phuong", "huyen", "quan", "tinh", "ap")

NT_LABEL = {
    "N": "approved",
    "V": "variant",
    "D": "unverified",
    "C": "conventional",
    "NS": "approved, non-Roman script",
    "VA": "variant, non-Roman script",
}


def normalise(name):
    """Accent-stripped, lowercase, punctuation-free key: 'Cao Lãnh' -> 'cao lanh'."""
    s = (name or "").replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


def read_rows(text):
    """Parse a GNS tab-delimited country file; exact duplicate (ufi, uni) rows are dropped."""
    rows, seen = [], set()
    for row in csv.DictReader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE):
        key = (row["ufi"], row["uni"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def load_zip(path):
    """Rows of Vietnam.txt inside the downloaded country zip."""
    with zipfile.ZipFile(path) as z:
        return read_rows(z.read("Vietnam.txt").decode("utf-8"))


def is_burial(row):
    """True for a formal burial: a burial designation, or a spot/area name that says grave/cemetery."""
    if row["desig_cd"] in BURIAL_DESIGS:
        return True
    name = unicodedata.normalize("NFC", row["full_name"]).lower() + " "
    if row["fc"] == "S" and TOMB_SPOT_RE.search(name):
        return True
    return row["fc"] in ("S", "L") and bool(BURIAL_NAME_RE.search(name))


def kind_for(row):
    """Evidence kind: 'military_site' for military designations, else 'placename'."""
    return "military_site" if row["desig_cd"] in MILITARY_DESIGS else "placename"


def minute_rounded(row):
    """True when both DMS seconds are 00: an old record read off a map to the whole minute."""
    try:
        return row["lat_dms"].split(":")[2] == "00" and row["long_dms"].split(":")[2] == "00"
    except IndexError:
        return True


def precision_m(row):
    """Position error (m) of the GNS point itself.

    Rounded to the minute: up to 0.5' (~925 m) off in each axis, ~1300 m worst case.
    Second-level records: digitising error, ~100 m."""
    return 1300 if minute_rounded(row) else 100


def uncertainty_for(row):
    """Evidence radius (m, to 10 m): point error and the feature's half-size, added in quadrature."""
    extent = EXTENT_M.get(row["desig_cd"], DEFAULT_EXTENT_M)
    return float(round(math.hypot(precision_m(row), extent), -1))


def build(rows):
    """Rows -> (evidence records, name index, stats).

    One record per name row; burials (every name of a burial feature), undersea features and
    rows without coordinates are counted in stats, not stored. The index maps each normalised
    name, plus aliases with the generic/admin prefix removed, to the features carrying it."""
    stats = {
        "rows": len(rows),
        "burials_dropped": 0,
        "burial_names": [],
        "skipped_fc": 0,
        "skipped_bad_coord": 0,
        "ingested": 0,
        "in_aoi": 0,
        "kinds": {},
    }
    burial_ufis = {r["ufi"] for r in rows if is_burial(r)}
    approved, adm1_names = {}, {}
    for r in rows:
        if r["nt"] in ("N", "NS") and r["ufi"] not in approved:
            approved[r["ufi"]] = r["full_name"]
        if r["desig_cd"] == "ADM1" and r["nt"] == "N" and not r["term_dt_f"] and r["adm1"] not in adm1_names:
            adm1_names[r["adm1"]] = r["full_name"]
    names_of = {}
    for r in rows:
        names_of.setdefault(r["ufi"], []).append(r["full_name"])

    records, index = [], {}
    for r in rows:
        if r["ufi"] in burial_ufis:
            stats["burials_dropped"] += 1
            stats["burial_names"].append(f"{r['desig_cd']} {r['full_name']}")
            continue
        if r["fc"] in SKIP_FC or r["desig_cd"] in SKIP_DESIGS:
            stats["skipped_fc"] += 1
            continue
        try:
            lat, lon = float(r["lat_dd"]), float(r["long_dd"])
        except ValueError:
            stats["skipped_bad_coord"] += 1
            continue
        kind = kind_for(r)
        unc = uncertainty_for(r)
        dsg = r["desig_cd"]
        main = approved.get(r["ufi"])
        others = sorted({n for n in names_of[r["ufi"]] if n != r["full_name"]})
        parts = [f"GNS {NT_LABEL.get(r['nt'], r['nt'])} name, feature {dsg} (class {r['fc']})"]
        if r["nt"] not in ("N", "NS") and main:
            parts.append(f"approved name: {main}")
        if others:
            parts.append("other names: " + "; ".join(others[:12]))
        parts.append(f"province code {r['adm1']}" + (f" ({adm1_names[r['adm1']]})" if r["adm1"] in adm1_names else ""))
        parts.append(f"MGRS {r['mgrs']}; DMS {r['lat_dms']} {r['long_dms']}")
        if minute_rounded(r):
            parts.append("position rounded to the whole minute")
        if dsg in LINEAR_DESIGS:
            parts.append("linear feature: the point is one representative spot on it")
        if r["term_dt_f"] or r["term_dt_n"]:
            parts.append(f"GNS marks this name/feature terminated {(r['term_dt_n'] or r['term_dt_f'])[:10]}")
        parts.append("modern gazetteer (no event date)")
        records.append(
            store.Evidence(
                source=SOURCE,
                source_record_id=f"{r['ufi']}/{r['uni']}",
                kind=kind,
                lon=lon,
                lat=lat,
                uncertainty_m=unc,
                original_coord=f"{r['lat_dd']}, {r['long_dd']}",
                original_datum="wgs84",
                title=r["full_name"],
                description=". ".join(parts) + ".",
                url=URL,  # the exact file the record was read from
                raw={
                    **{k: v for k, v in r.items() if v != ""},
                    "_wayback_ts": WAYBACK_TS,
                    "_minute_rounded": minute_rounded(r),
                },
            )
        )
        stats["ingested"] += 1
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
        if common.in_bbox(lon, lat):
            stats["in_aoi"] += 1

        entry = {
            "lon": lon,
            "lat": lat,
            "fc": r["fc"],
            "dsg": dsg,
            "adm1": r["adm1"],
            "name": r["full_name"],
            "nt": r["nt"],
            "ufi": r["ufi"],
            "unc": unc,
        }
        keys = {normalise(r["full_name"]), normalise(r["full_nm_nd"])}
        generic = normalise(r["generic"])
        for k in list(keys):
            for p in ((generic,) if generic else ()) + ADMIN_PREFIXES:
                if p and k.startswith(p + " ") and len(k) > len(p) + 1:
                    keys.add(k[len(p) + 1 :])
        for k in keys - {""}:
            lst = index.setdefault(k, [])
            if not any(e["ufi"] == r["ufi"] for e in lst):
                lst.append(entry)
    stats["burial_names"] = sorted(set(stats["burial_names"]))
    return records, index, stats


def write(db, records):
    """Register the source and make the store hold exactly `records` for it (store.sync_source:
    names GNS no longer lists are deleted, or withdrawn if an analyst holds them in a case).
    Returns (kept, deleted, withdrawn)."""
    store.upsert_source(
        db,
        SOURCE,
        name="NGA GEOnet Names Server (GNS): Vietnam country file",
        url=URL,
        licence="US Government work; NGA says foreign names data is freely available. Cite: 'Toponymic "
        "information is based on the Geographic Names Database ... maintained by the National "
        "Geospatial-Intelligence Agency'. NGA name/seal protected (10 USC 425).",
        notes=(
            f"Pinned Wayback capture {WAYBACK_TS[:4]}-{WAYBACK_TS[4:6]}-{WAYBACK_TS[6:8]} "
            f"{WAYBACK_TS[8:10]}:{WAYBACK_TS[10:12]}:{WAYBACK_TS[12:]} UTC of {ORIGINAL_URL}; "
            "geonames.nga.mil failed DNSSEC on 2026-11-03. Latest edit in file 2026-08-19. "
            "One record per name (approved, variant, ...), WGS84; minute-rounded positions get "
            "~1.3 km uncertainty. Cemeteries/tombs dropped. Index: data/work/gns_index.json."
        ),
    )
    return store.sync_source(db, SOURCE, records, reason="no longer in the GNS Vietnam file")


def main(argv=None):
    """Fetch (or reuse the cached zip), write data/work/gns_index.json and sync the store."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download the pinned zip")
    args = ap.parse_args(argv)
    path = common.cached_get(URL, SOURCE, name=ZIP_NAME, refresh=args.refresh, timeout=600)
    rows = load_zip(path)
    records, index, stats = build(rows)

    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.part")
    tmp.write_text(
        json.dumps(
            {
                "_about": (
                    "GNS Vietnam name index: key = accent-stripped lowercase name (also with generic/"
                    "admin prefixes like 'xa', 'ap', 'thi tran' removed); value = features. WGS84. "
                    f"Source: {URL}. unc = evidence uncertainty_m. Burials excluded."
                ),
                "names": index,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    tmp.replace(INDEX_PATH)

    db = store.connect()
    stats["kept"], stats["deleted"], stats["withdrawn"] = write(db, records)
    stats["index_keys"] = len(index)
    print(json.dumps({k: v for k, v in stats.items() if k != "burial_names"}, indent=1))
    print("burials dropped:", stats["burial_names"])
    return stats


if __name__ == "__main__":
    main()
