"""NARA RG 472 NPIASS Hamlet Evaluation System gazetteers -> placename evidence.

Three fixed-width files (US federal records, public domain), each fetched once and cached:
  GZSF  HES Gazetteer Source File         160-byte records + CRLF   (catalog NAID 4628955)
  HVGZ  HES/VCI Gazetteer                 160-byte records + CRLF   (catalog NAID 4626239)
  HGAZ  HES Gazetteer (program R7119P)    150-byte records + CRLF   (catalog NAID 4628241)
plus the documentation PDF 133.2DP.pdf (record layouts). catalog.archives.gov/robots.txt is
"Disallow: /", so only these four known static files are fetched, never the catalog UI.

GZSF/HVGZ layout (NARA-prepared layout "NPIASS II - HES/VCI GAZETTEER", 5-24-78, 1-based):
  1 record type ('3' hamlet with VC names, '2' hamlet without, '1' district, '0' region);
  2-6 reporting GVN area; 7-13 VC id; 14-22 USID (region, province, district, village, hamlet);
  23-30 UTM (8-char, zoneless); 31-35 population; 36 HES rating; 37-56 GVN province;
  57-76 GVN district; 77-96 GVN hamlet; 97-116 VC province; 117-136 VC district; 137-156 VC village.
HGAZ hamlet record (scanned R7119P layout is barely legible; offsets below were checked against
the data): 1-9 USID; 10-29 province; 30-49 district; 50-69 village; 70-89 hamlet; 90-94 ratings;
95-109 population fields; 119-127 admin id; 128-135 UTM; 136 urban/rural; 137-138 ratings.
Village records (USID ending '00') and the date record ('7301' = Jan 1973) are not hamlets.

The grid is a 100 m square written without a zone. It is decoded with gridref.candidates on
Indian 1960, the datum of the wartime L7014 1:50k sheets. The files do not state it; the datum
calibration (scripts/ingest/datum_check.py -> data/work/datum_calibration.json, HES hamlets vs
unrounded GNS points) confirmed Indian 1960 over WGS84. When a
reference fits more than one zone/band, the candidate nearest the hamlet's province median is
chosen. A hamlet more than OUTLIER_KM from its province median is skipped as a bad grid.

Also writes data/work/hes_provinces.json: centroid and a radius covering the whole province per
GVN province (plus districts and VC provinces), for placing province-level casualty records.
The radius is the distance from the centroid to the FARTHEST point known to be in the province,
plus AREA_BUFFER_M. Hamlets cluster on the coast and in the lowlands (a percentile radius left
A Shau and Khe Sanh outside their own province's circle), and the 1972-73 gazetteer has no
hamlets at all in VC-held uplands (Kham Duc, in Quang Tin, is 65 km from the centroid but 26 km
past the farthest Quang Tin hamlet). So the farthest point is taken over the hamlets AND, when
data/work/gns_index.json exists, the GNS populated places of the modern province(s) holding at
least DOMINANT_ADM1_SHARE of the province's hamlets (a wartime province lies mostly inside its
modern successor; a modern province that merged two wartime ones makes the circle cover both,
which over-covers but never under-covers). Only places in hamlet-less areas (more than EMPTY_KM
from every hamlet) are shared that way; a place in a settled area counts only for the province of
its nearest hamlet, so the reshuffled Delta boundaries do not inflate the circles. Cities (codes
x9y), offshore islands (GNS places more than ISLAND_KM from any hamlet, e.g. Con Son) and places
north of the Ben Hai are not used. A few bad grids (within OUTLIER_KM) can make a radius too
large; that errs on the honest side. Districts and VC provinces use the hamlets only (farthest
hamlet + AREA_BUFFER_M).

Outputs: evidence rows (source "hes_gazetteer") in data/evidence.sqlite and
data/work/hes_provinces.json. Run gns.py first so the province extents can use GNS places.

Run:  .venv/bin/python scripts/ingest/hes_gazetteer.py [--refresh]
"""

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import gridref  # noqa: E402
import store  # noqa: E402

SOURCE = "hes_gazetteer"
BASE = "https://catalog.archives.gov/medialz/electronic-records/rg-472/NPIASS/"
FILES = {  # code: (file name, record length incl. CRLF, catalog page, data vintage)
    "GZSF": ("RG349.NPIASS.GZSF.txt", 162, "https://catalog.archives.gov/id/4628955", ("1972", "1973")),
    "HVGZ": ("RG349.NPIASS.HVGZ.txt", 162, "https://catalog.archives.gov/id/4626239", ("1972", "1973")),
    "HGAZ": ("RG349.NPIASS.HGAZ", 152, "https://catalog.archives.gov/id/4628241", ("1973-01", "1973-01")),
}
DOC = "133.2DP.pdf"
PREFERENCE = ("GZSF", "HVGZ", "HGAZ")  # whose grid/names lead when files disagree
DATUM = "indian1960"  # datum the grids are decoded on (gridref)
ORIGINAL_DATUM = "indian1960"  # label stored on the evidence; confirmed by data/work/datum_calibration.json
# 100 m square half-diagonal (71 m) + map-reading/plotting error + the point being the hamlet's
# population centre, not its extent. The datum calibration against GNS resolves the hamlet points
# only to ~0.5-1 km (median residual ~0.8 km), so 500 m is the honest floor; the WGS84 reading is
# still kept in raw.
UNCERTAINTY_M = 500.0
AREA_BUFFER_M = 10_000.0  # added to the farthest hamlet: the province extends past its last hamlet
OUTLIER_KM = 150.0
GRID_RE = re.compile(r"^[A-HJ-NP-Z]{2}\d{6}$")
BURIAL_RE = re.compile(r"\b(NGHIA[ -]?TRANG|NGHIA[ -]?DIA|CEMETER\w*|LIET[ -]?SI|GRAVES?|TOMBS?)\b")
OUT_PROVINCES = store.ROOT / "data" / "work" / "hes_provinces.json"
GNS_INDEX = store.ROOT / "data" / "work" / "gns_index.json"  # read-only, written by gns.py
DOMINANT_ADM1_SHARE = 0.2
EMPTY_KM = 15.0  # a GNS place farther than this from every hamlet lies in a hamlet-less area
ISLAND_KM = 60.0  # ... and farther than this is an offshore island (Con Son, Phu Quy)
BEN_HAI_LAT = 17.05  # DMZ line; GNS places north of it are North Vietnam


def clean(s):
    """Fixed-width text field -> printable ASCII with single spaces (the EBCDIC conversion left
    stray bytes, and padding inside a field: 'NGOAI O     (MOT)' -> 'NGOAI O (MOT)')."""
    return " ".join(re.sub(r"[^\x20-\x7e]", "", s).split())


def read_records(path, reclen):
    """Split a fixed-width file into records by length (reclen includes the CRLF, which is dropped).

    Split on length, not newlines: some records carry stray bytes left by the EBCDIC conversion."""
    data = Path(path).read_bytes()
    return [data[i : i + reclen - 2].decode("latin-1") for i in range(0, len(data), reclen)]


def parse_vci(rec):
    """A GZSF/HVGZ hamlet record (types '2' and '3') -> dict, else None."""
    if rec[:1] not in ("2", "3"):
        return None
    return {
        "type": rec[0],
        "gvn_area": rec[1:6].strip(),
        "vcid": rec[6:13].strip(),
        "usid": rec[13:22],
        "utm": rec[22:30],
        "population": rec[30:35].strip(),
        "rating": rec[35:36].strip(),
        "gvn_province": clean(rec[36:56]),
        "gvn_district": clean(rec[56:76]),
        "gvn_hamlet": clean(rec[76:96]),
        "vc_province": clean(rec[96:116]),
        "vc_district": clean(rec[116:136]),
        "vc_village": clean(rec[136:156]),
    }


def parse_hgaz(rec):
    """An HGAZ hamlet record -> dict; village, date and header records -> None."""
    usid = rec[:9]
    if not usid.isdigit() or usid.endswith("00") or usid == "000000000":
        return None
    return {
        "usid": usid,
        "gvn_province": clean(rec[9:29]),
        "gvn_district": clean(rec[29:49]),
        "gvn_village": clean(rec[49:69]),
        "gvn_hamlet": clean(rec[69:89]),
        "ratings": rec[89:94].strip(),
        "population": rec[94:99].strip(),
        "admin_id": rec[118:127].strip(),
        "utm": rec[127:135],
        "urban_rural": rec[135:136].strip(),
        "ratings2": rec[136:138].strip(),
    }


def hgaz_date(records):
    """Vintage from the HGAZ date record, e.g. '7301' -> '1973-01'."""
    for rec in records:
        if rec.startswith("000000000"):
            m = re.search(r"\b(\d{2})(\d{2})\b", rec[9:])
            if m:
                return f"19{m.group(1)}-{m.group(2)}"
    return None


def load_all(raw_dir):
    """{usid: {file_code: parsed record}} over all three files."""
    by_usid = defaultdict(dict)
    for code, (name, reclen, _, _) in FILES.items():
        recs = read_records(Path(raw_dir) / name, reclen)
        parse = parse_hgaz if code == "HGAZ" else parse_vci
        for rec in recs:
            p = parse(rec)
            if p:
                by_usid[p["usid"]][code] = p
    return by_usid


def dist_m(lon1, lat1, lon2, lat2):
    """Great-circle (haversine) distance in metres on a 6371 km sphere."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(a))


def median(xs):
    """Median of a non-empty sequence."""
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def first(recs, key):
    """The first non-empty value of `key` across one hamlet's file records, in PREFERENCE order."""
    for code in PREFERENCE:
        v = recs.get(code, {}).get(key)
        if v:
            return v
    return ""


def variants(recs, key):
    """Distinct non-empty values of `key` across one hamlet's file records."""
    return sorted({r[key] for r in recs.values() if r.get(key)})


def build(by_usid):
    """Decode every hamlet grid. Returns (evidence list, stats, located hamlets for centroids)."""
    stats = Counter()
    # Pass 1: decode; collect unambiguous points per province for disambiguation/outliers.
    decoded = {}  # (usid, grid) -> candidates
    prov_pts = defaultdict(list)
    for usid, recs in by_usid.items():
        grids = {}
        for code in PREFERENCE:
            g = recs.get(code, {}).get("utm", "")
            if GRID_RE.match(g):
                grids.setdefault(g, []).append(code)
        if not grids:
            stats["skipped: no or malformed grid"] += 1
            continue
        for i, (g, codes) in enumerate(grids.items()):
            c = gridref.candidates(g, datum=DATUM)
            decoded[(usid, g)] = (codes, c, i == 0, len(grids) > 1)
            if len(c) == 1:
                prov_pts[usid[:3]].append(c[0][1:3])
    prov_med = {p: (median([x for x, _ in pts]), median([y for _, y in pts])) for p, pts in prov_pts.items()}

    records, located = [], []
    for (usid, g), (codes, cands, primary, multi) in sorted(decoded.items()):
        recs = by_usid[usid]
        hamlet = first(recs, "gvn_hamlet")
        if BURIAL_RE.search(hamlet.upper()):
            stats["dropped: formal burial name"] += 1
            continue
        if not cands:
            stats["skipped: grid fits no zone/band"] += 1
            continue
        med = prov_med.get(usid[:3])
        if med:
            ranked = sorted(cands, key=lambda c: dist_m(c[1], c[2], *med))
        else:
            ranked = cands
        zone, lon, lat, r = ranked[0]
        far_km = dist_m(lon, lat, *med) / 1000 if med else None
        if far_km is not None and far_km > OUTLIER_KM:
            stats[f"skipped: grid > {OUTLIER_KM:.0f} km from province median"] += 1
            continue
        choice = {
            "zone": zone,
            "n_candidates": len(cands),
            "rule": "only candidate" if len(cands) == 1 else "nearest province median",
            "candidates": [[c[0], round(c[1], 6), round(c[2], 6)] for c in cands],
            "km_from_province_median": far_km and round(far_km, 1),
        }
        alt = gridref.to_wgs84(g, datum="wgs84", default_zone=zone)
        records.append(make_evidence(usid, g, codes, recs, lon, lat, choice, alt, multi=multi))
        stats["ingested"] += 1
        if primary:
            located.append((usid, lon, lat, recs))
    return records, stats, located


def make_evidence(usid, grid, codes, recs, lon, lat, choice, alt, multi):
    """One placename record for a hamlet grid: names from every file, the zone choice and the
    WGS84 reading (alt) kept in raw. multi=True when the files disagree on the grid, so the id
    carries the grid."""
    hamlet = first(recs, "gvn_hamlet")
    village = first(recs, "gvn_village")
    district = first(recs, "gvn_district")
    province = first(recs, "gvn_province")
    vc = {k: first(recs, k) for k in ("vc_province", "vc_district", "vc_village")}
    pop = first(recs, "population").lstrip("0")
    rating = first(recs, "rating")
    starts = [FILES[c][3][0] for c in recs]
    ends = [FILES[c][3][1] for c in recs]
    title = f"{hamlet or 'hamlet ' + usid} (HES hamlet, {district}, {province})"
    gvn = f"GVN: province {province} / district {district} / village {village or '?'} / hamlet {hamlet}"
    vcs = (
        "VC: province {vc_province} / district {vc_district} / village {vc_village}".format(**vc)
        if any(vc.values())
        else "VC: no VC names in the gazetteer"
    )
    alt_names = {k: v for k in ("gvn_hamlet", "gvn_province", "gvn_district") if len(v := variants(recs, k)) > 1}
    desc = (
        f"{gvn}. {vcs}. HES USID {usid}"
        + (f", population {pop}" if pop else "")
        + (f", HES rating {rating}" if rating else "")
        + f". Grid {grid} (8-char, zoneless) read as {choice['zone']} on Indian 1960; "
        f"files {', '.join(sorted(recs))}; grid from {', '.join(codes)}."
        + (f" Spelling variants: {alt_names}." if alt_names else "")
        + (" The files disagree on this hamlet's grid; one record per grid." if multi else "")
    )
    raw = {
        "usid": usid,
        "grid": grid,
        "grid_files": codes,
        "zone_choice": choice,
        "wgs84_reading_if_datum_wrong": [round(alt[0], 6), round(alt[1], 6), round(dist_m(lon, lat, alt[0], alt[1]))],
        "files": recs,
    }
    return store.Evidence(
        source=SOURCE,
        source_record_id=f"{usid}:{grid}" if multi else usid,
        kind="placename",
        lon=lon,
        lat=lat,
        uncertainty_m=UNCERTAINTY_M,
        original_coord=grid,
        original_datum=ORIGINAL_DATUM,
        title=title,
        date_start=min(starts),
        date_end=max(ends),
        description=desc,
        url=FILES[codes[0]][2],
        raw=raw,
    )


def percentile(xs, q):
    """Linearly interpolated q-quantile (0 <= q <= 1); 0.0 for an empty sequence."""
    s = sorted(xs)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarise(points, extent=()):
    """points: [(lon, lat)] hamlets; extent: [(lon, lat, label)] other places known to be inside.
    -> centroid (of the hamlets), covering radius (farthest point + AREA_BUFFER_M), count.

    radius_p90_m (the old, coast-biased statistic) and max_hamlet_m are kept for reference."""
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    ds = [dist_m(lon, lat, *p) for p in points]
    far_extent = max(((dist_m(lon, lat, x, y), label) for x, y, label in extent), default=(0.0, None))
    out = {
        "lon": round(lon, 6),
        "lat": round(lat, 6),
        "radius_m": round(max(max(ds), far_extent[0]) + AREA_BUFFER_M),
        "max_hamlet_m": round(max(ds)),
        "radius_p90_m": round(percentile(ds, 0.9)),
        "hamlet_count": len(points),
    }
    if extent:
        out["max_gns_place_m"] = round(far_extent[0])
        out["farthest_gns_place"] = far_extent[1]
    return out


class _Grid:
    """Points bucketed in 0.1 degree cells for nearest-neighbour search (no scipy here)."""

    def __init__(self, items):  # items: (lon, lat, payload)
        self.cells = defaultdict(list)
        for it in items:
            self.cells[(int(it[0] * 10), int(it[1] * 10))].append(it)

    def nearest(self, lon, lat, max_ring=20):
        """(distance_m, item) of the nearest item within max_ring cells, or None.

        Searches outward ring by ring and stops once no farther ring can hold a closer point."""
        best, cx, cy = None, int(lon * 10), int(lat * 10)
        for ring in range(max_ring + 1):
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    for it in self.cells.get((cx + dx, cy + dy), ()):
                        d = dist_m(lon, lat, it[0], it[1])
                        if best is None or d < best[0]:
                            best = (d, it)
            if best and best[0] < ring * 10_000 * 0.95:  # no closer point can be in a further ring
                break
        return best


def gns_places(path=GNS_INDEX):
    """GNS populated places south of the DMZ with a modern province: [(lon, lat, adm1, name)]."""
    path = Path(path)
    if not path.exists():
        return []
    names = json.loads(path.read_text(encoding="utf-8"))["names"]
    uniq = {f["ufi"]: f for fs in names.values() for f in fs}
    return [
        (f["lon"], f["lat"], f["adm1"], f["name"])
        for f in uniq.values()
        if f["fc"] == "P" and f["lat"] < BEN_HAI_LAT and f["adm1"] != "VN-000"
    ]


def province_extents(located, places):
    """{province code: [(lon, lat, label)]}: GNS places in the province's dominant modern province(s).

    A hamlet's modern province is that of the nearest GNS place. Cities and islands are left out."""
    if not places:
        return {}
    pg = _Grid(places)
    hg = _Grid([(lon, lat, usid[:3]) for usid, lon, lat, _ in located])
    adm = defaultdict(Counter)
    for usid, lon, lat, _ in located:
        b = pg.nearest(lon, lat)
        if b:
            adm[usid[:3]][b[1][2]] += 1
    dominant = {
        code: {a for a, k in c.items() if k >= DOMINANT_ADM1_SHARE * sum(c.values())}
        for code, c in adm.items()
        if code[1] != "9"
    }  # x9y codes are cities
    out = defaultdict(list)
    for lon, lat, a, name in places:
        near = hg.nearest(lon, lat, max_ring=int(ISLAND_KM / 10) + 1)
        if not near or near[0] > ISLAND_KM * 1000:
            continue  # offshore island: not used
        if near[0] <= EMPTY_KM * 1000:
            # Settled area: the place belongs to the province of the nearest hamlet (if that is
            # a province, not a city, whose dominant modern province it lies in).
            code = near[1][2]
            if a in dominant.get(code, ()):
                out[code].append((lon, lat, f"{name} ({a})"))
            continue
        # Hamlet-less area (uplands): every province whose dominant modern province holds it.
        for code, adms in dominant.items():
            if a in adms:
                out[code].append((lon, lat, f"{name} ({a})"))
    return out


def provinces(located, places=()):
    """GVN provinces by USID region+province code, with districts and the VC provinces seen.
    places: gns_places() for the province extents (empty -> hamlets only)."""
    extents = province_extents(located, places)
    prov = defaultdict(
        lambda: {
            "pts": [],
            "names": Counter(),
            "vc": Counter(),
            "districts": defaultdict(lambda: {"pts": [], "names": Counter()}),
        }
    )
    vcprov = defaultdict(lambda: {"pts": [], "gvn": Counter()})
    for usid, lon, lat, recs in located:
        p = prov[usid[:3]]
        p["pts"].append((lon, lat))
        d = p["districts"][usid[:5]]
        d["pts"].append((lon, lat))
        for r in recs.values():
            if r.get("gvn_province"):
                p["names"][r["gvn_province"]] += 1
            if r.get("gvn_district"):
                d["names"][r["gvn_district"]] += 1
            if r.get("vc_province"):
                p["vc"][r["vc_province"]] += 1
        vcname = first(recs, "vc_province")
        if vcname:
            vcprov[vcname]["pts"].append((lon, lat))
            vcprov[vcname]["gvn"][usid[:3]] += 1
    buffer_km = f"{AREA_BUFFER_M / 1000:.0f}"
    out = {
        "_about": (
            "HES gazetteer hamlet points (NARA RG 472 NPIASS GZSF/HVGZ/HGAZ, c.1972-73), grids read on "
            "Indian 1960 (confirmed by data/work/datum_calibration.json). Centroid = mean of hamlet points; "
            f"radius_m = distance to the farthest point known to be in the province + {buffer_km} km, so the "
            "circle covers the whole province including uplands without hamlets. Provinces: farthest of the "
            "hamlets and the GNS populated places of the modern province(s) holding "
            f">= {DOMINANT_ADM1_SHARE:.0%} of its hamlets (max_hamlet_m, max_gns_place_m, farthest_gns_place; "
            f"places more than {ISLAND_KM:.0f} km from every hamlet (offshore islands) and cities excluded). "
            f"Districts and VC provinces: farthest hamlet + {buffer_km} km. radius_p90_m (the old "
            "90th-percentile radius) is kept for reference only. Keys are HES USID codes: province = region "
            "digit + 2-digit province (e.g. 432 Kien Phong), district = 5 digits. vc_provinces are Viet Cong "
            "administrative provinces keyed by name, with the GVN province codes their hamlets fall in."
        ),
        "source": SOURCE,
        "provinces": {},
        "vc_provinces": {},
    }
    for code, p in sorted(prov.items()):
        out["provinces"][code] = {
            "code": code,
            "region": code[0],
            "names": [n for n, _ in p["names"].most_common()],
            **summarise(p["pts"], extents.get(code, ())),
            "vc_province_names": dict(p["vc"].most_common()),
            "districts": {
                dc: {"names": [n for n, _ in d["names"].most_common()], **summarise(d["pts"])}
                for dc, d in sorted(p["districts"].items())
            },
        }
    for name, v in sorted(vcprov.items()):
        out["vc_provinces"][name] = {
            "name": name,
            **summarise(v["pts"]),
            "gvn_province_codes": dict(v["gvn"].most_common()),
        }
    return out


def fetch(refresh=False):
    """Cache the three gazetteer files and the documentation PDF; return {file code: path}."""
    paths = {
        code: common.cached_get(BASE + name, SOURCE, name=name, refresh=refresh, timeout=900)
        for code, (name, *_rest) in FILES.items()
    }
    common.cached_get(BASE + DOC, SOURCE, name=DOC, refresh=refresh, timeout=900)
    return paths


def save(db, records, vintage=None):
    """Register the source and make the store hold exactly `records` for it (store.sync_source).
    Returns (kept, deleted, withdrawn)."""
    store.upsert_source(
        db,
        SOURCE,
        name="NARA RG 472 NPIASS HES gazetteers (GZSF, HVGZ, HGAZ)",
        url="https://catalog.archives.gov/id/4616225",
        licence="US federal records, public domain (catalog access and use: Unrestricted)",
        notes=f"Static files under {BASE} fetched once (catalog robots.txt disallows crawling). "
        f"HGAZ date record {vintage}. 8-char zoneless grids decoded on Indian 1960 (confirmed by "
        f"the datum calibration against GNS). Hamlet population centres, 500 m uncertainty.",
    )
    return store.sync_source(db, SOURCE, records, reason="no longer in the HES gazetteer files")


def main(argv=None):
    """Fetch (or reuse the cache), sync the store and write data/work/hes_provinces.json."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download the gazetteer files")
    args = ap.parse_args(argv)
    paths = fetch(refresh=args.refresh)
    common.progress(None, "Reading the hamlet lists")
    raw_dir = paths["GZSF"].parent
    vintage = hgaz_date(read_records(paths["HGAZ"], FILES["HGAZ"][1]))
    by_usid = load_all(raw_dir)
    records, stats, located = build(by_usid)
    common.progress(None, f"Saving {len(records):,} hamlets")
    n, deleted, withdrawn = save(store.connect(), records, vintage)
    OUT_PROVINCES.parent.mkdir(parents=True, exist_ok=True)
    OUT_PROVINCES.write_text(
        json.dumps(provinces(located, gns_places()), indent=1, ensure_ascii=False), encoding="utf-8"
    )
    aoi = sum(common.in_bbox(e.lon, e.lat) for e in records)
    print(
        f"hamlet USIDs: {len(by_usid)}; synced {n} (deleted {deleted}, withdrawn {withdrawn}); "
        f"in area of interest {aoi}; HGAZ vintage {vintage}"
    )
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v}")
    print(f"wrote {OUT_PROVINCES}")
    return records, stats


if __name__ == "__main__":
    main()
