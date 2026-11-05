"""Datum calibration: are the zoneless wartime grids in the HES gazetteer on Indian 1960 or WGS84?

No new source is fetched and nothing is written to the evidence store. The check pairs HES
hamlets (source 'hes_gazetteer', whose `original_coord` is the 8-char zoneless grid and whose
raw `zone_choice.zone` records the zone/band picked at ingest) with GNS populated places
(source 'gns', fc='P', modern WGS84 coordinates) that carry the same normalised name nearby.

For each pair the HES grid is decoded twice with gridref.to_wgs84 (datum='indian1960' and
datum='wgs84', same zone) and the GNS point's offset from each reading is measured in local
east/north metres. The two readings differ by a near-constant ~0.5 km vector, so whichever
reading the GNS points centre on (median offset ~0) is the datum the grids were written on.

Bias control: candidate GNS names are searched within MATCH_KM of the MIDPOINT of the two
readings (so the window favours neither datum), and a pair is kept only when exactly one GNS
feature (ufi) with that name lies in the window. GNS positions have their own errors; many
are rounded to the whole minute (~1.3 km), so these are reported separately and the headline
uses unrounded GNS only, with medians throughout.

Inputs: the evidence store (data/evidence.sqlite) after gns.py and hes_gazetteer.py have run.
Output: data/work/datum_calibration.json.

Run:  .venv/bin/python scripts/ingest/datum_check.py
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import gridref  # noqa: E402
import store  # noqa: E402

OUT = store.ROOT / "data" / "work" / "datum_calibration.json"
MATCH_KM = 3.0  # search radius round the midpoint of the two readings
CELL_DEG = 0.05  # GnsIndex bucket size in degrees (~5.5 km); near() scans enough rings to cover the window
# Generic settlement words that one source writes and the other drops.
PREFIXES = ("ap", "xom", "thon", "lang", "ban", "khom", "phum", "soc", "bon", "buon", "plei")
R_EARTH = 6_371_000.0


def norm_name(name):
    """Comparable form of a place name: no diacritics, no generic prefix, alphanumerics only.
    'TAN-AN' -> 'tanan'; 'Ấp Tân An' -> 'tanan'; 'Đông Hòa 2' -> 'donghoa2'."""
    if not name:
        return ""
    s = name.replace("Đ", "D").replace("đ", "d")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\(.*?\)", " ", s)
    words = re.findall(r"[a-z0-9]+", s)
    while len(words) > 1 and words[0] in PREFIXES:
        words = words[1:]
    return "".join(words)


def enu_offset(lon0, lat0, lon1, lat1):
    """(east_m, north_m) of point 1 relative to point 0 (local equirectangular; fine < 10 km)."""
    e = math.radians(lon1 - lon0) * math.cos(math.radians((lat0 + lat1) / 2)) * R_EARTH
    n = math.radians(lat1 - lat0) * R_EARTH
    return e, n


def quantiles(xs, qs=(0.25, 0.5, 0.75, 0.9)):
    """{'p25': ..., 'p50': ...}: linearly interpolated quantiles to 0.1; {} for an empty input."""
    s = sorted(xs)
    if not s:
        return {}
    out = {}
    for q in qs:
        k = (len(s) - 1) * q
        lo, hi = math.floor(k), math.ceil(k)
        out[f"p{int(q * 100)}"] = round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)
    return out


def median(xs):
    """Median to 0.1, or None for an empty input."""
    return quantiles(xs, (0.5,))["p50"] if xs else None


def bootstrap_median_ci(xs, n_boot=2000, seed=0):
    """95% bootstrap interval of the median (numpy, fixed seed so re-runs are identical).

    None for fewer than 5 values, where the interval would mean nothing."""
    import numpy as np  # only this statistic needs numpy; the rest of the module is stdlib

    a = np.asarray(xs, dtype=float)
    if len(a) < 5:
        return None
    rng = np.random.default_rng(seed)
    meds = np.median(a[rng.integers(0, len(a), (n_boot, len(a)))], axis=1)
    return [round(float(np.percentile(meds, 2.5)), 1), round(float(np.percentile(meds, 97.5)), 1)]


def hes_hamlet_name(raw):
    """(hamlet, province, district) GVN names from a HES record's raw files, in file preference order."""
    for code in ("GZSF", "HVGZ", "HGAZ"):
        f = raw.get("files", {}).get(code)
        if f and f.get("gvn_hamlet"):
            return f["gvn_hamlet"], f.get("gvn_province"), f.get("gvn_district")
    return None, None, None


def decode_both(grid, zone):
    """(lon, lat) of the grid read on Indian 1960 and on WGS84, both in the given zone/band."""
    i = gridref.to_wgs84(grid, datum="indian1960", default_zone=zone)
    w = gridref.to_wgs84(grid, datum="wgs84", default_zone=zone)
    return (i[0], i[1]), (w[0], w[1])


class GnsIndex:
    """GNS populated places bucketed by normalised name and 0.05° cell."""

    def __init__(self):
        self.cells = defaultdict(list)  # (name, cx, cy) -> [(ufi, lon, lat, rounded, title)]

    def add(self, name, ufi, lon, lat, rounded, title):
        """Index one name of a GNS feature; rounded = position rounded to the whole minute."""
        key = (name, int(lon // CELL_DEG), int(lat // CELL_DEG))
        self.cells[key].append((ufi, lon, lat, rounded, title))

    def near(self, name, lon, lat, km):
        """Distinct features (one per ufi) with this normalised name within km of (lon, lat)."""
        cx, cy = int(lon // CELL_DEG), int(lat // CELL_DEG)
        r = math.ceil(km / 111 / CELL_DEG) + 1
        found = {}
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                for rec in self.cells.get((name, cx + dx, cy + dy), ()):
                    e, n = enu_offset(lon, lat, rec[1], rec[2])
                    if math.hypot(e, n) <= km * 1000:
                        found.setdefault(rec[0], rec)
        return list(found.values())


def load_gns(db):
    """GnsIndex of every GNS populated place (fc 'P') in the store, by both its names. -> (index, rows read)."""
    idx = GnsIndex()
    n = 0
    for title, lon, lat, raw_json in db.execute(
        "select title, lon, lat, raw_json from evidence where source='gns' and json_extract(raw_json,'$.fc')='P'"
    ):
        raw = json.loads(raw_json)
        names = {norm_name(raw.get("full_nm_nd") or title), norm_name(title)}
        for nm in names - {""}:
            idx.add(nm, raw.get("ufi"), lon, lat, bool(raw.get("_minute_rounded")), title)
        n += 1
    return idx, n


def match_pairs(hes_rows, idx, km=MATCH_KM):
    """hes_rows: iterable of dicts with name, grid, zone, usid, province. Returns (pairs, stats)."""
    pairs, stats = [], Counter()
    for h in hes_rows:
        nm = norm_name(h["name"])
        if len(nm) < 4:
            stats["skipped: name too short"] += 1
            continue
        try:
            ind, wgs = decode_both(h["grid"], h["zone"])
        except ValueError:
            stats["skipped: grid not decodable"] += 1
            continue
        mid = ((ind[0] + wgs[0]) / 2, (ind[1] + wgs[1]) / 2)
        hits = idx.near(nm, mid[0], mid[1], km)
        if not hits:
            stats["no GNS name match within window"] += 1
            continue
        if len(hits) > 1:
            stats["ambiguous: >1 GNS feature with that name in window"] += 1
            continue
        ufi, glon, glat, rounded, gtitle = hits[0]
        oi = enu_offset(ind[0], ind[1], glon, glat)
        ow = enu_offset(wgs[0], wgs[1], glon, glat)
        shift = enu_offset(ind[0], ind[1], wgs[0], wgs[1])
        pairs.append(
            {
                "usid": h["usid"],
                "hes_name": h["name"],
                "province": h.get("province"),
                "grid": h["grid"],
                "zone": h["zone"],
                "gns_ufi": ufi,
                "gns_name": gtitle,
                "gns_rounded": rounded,
                "gns_lon": glon,
                "gns_lat": glat,
                "ind": ind,
                "wgs": wgs,
                "off_ind": oi,
                "off_wgs": ow,
                "shift_wgs_minus_ind": shift,
                "d_ind": math.hypot(*oi),
                "d_wgs": math.hypot(*ow),
            }
        )
        stats["matched"] += 1
    return pairs, stats


def summarise(pairs):
    """Distance and offset statistics of matched pairs under both readings ({'n': 0} when empty)."""
    if not pairs:
        return {"n": 0}
    d_i = [p["d_ind"] for p in pairs]
    d_w = [p["d_wgs"] for p in pairs]
    me = median([p["off_ind"][0] for p in pairs])
    mn = median([p["off_ind"][1] for p in pairs])
    se = median([p["shift_wgs_minus_ind"][0] for p in pairs])
    sn = median([p["shift_wgs_minus_ind"][1] for p in pairs])
    # Residual scatter once the median offset is removed: what's left is GNS error + HES
    # map-reading error + hamlet-extent ambiguity, i.e. a realistic floor for grid sources.
    resid = [math.hypot(p["off_ind"][0] - me, p["off_ind"][1] - mn) for p in pairs]
    return {
        "n": len(pairs),
        "indian1960_reading": {
            "distance_to_gns_m": quantiles(d_i),
            "median_offset_gns_minus_hes_m": {"east": me, "north": mn},
        },
        "wgs84_reading": {
            "distance_to_gns_m": quantiles(d_w),
            "median_offset_gns_minus_hes_m": {
                "east": median([p["off_wgs"][0] for p in pairs]),
                "north": median([p["off_wgs"][1] for p in pairs]),
            },
        },
        "datum_shift_wgs84_reading_minus_indian1960_reading_m": {
            "east": se,
            "north": sn,
            "length": round(math.hypot(se, sn), 1),
        },
        "median_offset_gns_minus_indian1960_reading_95ci_m": {
            "east": bootstrap_median_ci([p["off_ind"][0] for p in pairs]),
            "north": bootstrap_median_ci([p["off_ind"][1] for p in pairs]),
        },
        "share_closer_on_indian1960": round(sum(a < b for a, b in zip(d_i, d_w, strict=True)) / len(pairs), 3),
        "residual_after_median_offset_m": quantiles(resid, (0.5, 0.68, 0.9, 0.95)),
    }


def load_hes(db):
    """HES hamlet records in the store that have a name and a recorded zone, as match_pairs input."""
    rows = []
    for usid, grid, raw_json in db.execute(
        "select source_record_id, original_coord, raw_json from evidence where source='hes_gazetteer'"
    ):
        raw = json.loads(raw_json)
        name, prov, dist = hes_hamlet_name(raw)
        zone = (raw.get("zone_choice") or {}).get("zone")
        if not name or not zone:
            continue
        rows.append({"usid": usid, "grid": grid, "zone": zone, "name": name, "province": prov, "district": dist})
    return rows


def main(db_path=store.DB_PATH, out=OUT):
    """Pair, summarise overall / by zone / by latitude band, write `out` and print the conclusion."""
    db = sqlite3.connect(db_path)
    idx, n_gns = load_gns(db)
    hes = load_hes(db)
    pairs, stats = match_pairs(hes, idx)
    db.close()

    unrounded = [p for p in pairs if not p["gns_rounded"]]
    rounded = [p for p in pairs if p["gns_rounded"]]
    by_zone = {z: summarise([p for p in unrounded if p["zone"] == z]) for z in sorted({p["zone"] for p in unrounded})}
    lat_bands = {
        "south (<12N)": lambda p: p["ind"][1] < 12,
        "centre (12-15N)": lambda p: 12 <= p["ind"][1] < 15,
        "north (>=15N)": lambda p: p["ind"][1] >= 15,
    }
    by_region = {k: summarise([p for p in unrounded if f(p)]) for k, f in lat_bands.items()}
    aoi = [p for p in unrounded if common.in_bbox(*p["ind"])]

    head = summarise(unrounded)
    ri = head["indian1960_reading"]["distance_to_gns_m"]["p50"]
    rw = head["wgs84_reading"]["distance_to_gns_m"]["p50"]
    off = head["indian1960_reading"]["median_offset_gns_minus_hes_m"]
    shift = head["datum_shift_wgs84_reading_minus_indian1960_reading_m"]
    fits = "indian1960" if ri < rw else "wgs84"
    resid = head["residual_after_median_offset_m"]
    conclusion = (
        f"{head['n']} HES hamlets matched by name to unique unrounded GNS populated places nationwide. "
        f"Median HES->GNS distance is {ri:.0f} m reading the grids on Indian 1960 vs {rw:.0f} m on WGS84; "
        f"{head['share_closer_on_indian1960'] * 100:.0f}% of pairs are closer on Indian 1960. "
        f"The two readings differ by ~{shift['length']:.0f} m (E {shift['east']:.0f}, N {shift['north']:.0f}); "
        f"the median GNS-minus-HES(Indian 1960) vector is E {off['east']:.0f} m, N {off['north']:.0f} m, "
        f"i.e. {'near zero: the grids are on Indian 1960' if math.hypot(off['east'], off['north']) < shift['length'] / 3 else 'not clearly at either reading'}. "
        f"Datum that fits: {fits}. Residual scatter after removing the median offset: "
        f"p50 {resid['p50']:.0f} m, p68 {resid['p68']:.0f} m, p90 {resid['p90']:.0f} m "
        f"(includes GNS error and hamlet extent, so it is an upper bound on grid-reading error)."
    )
    result = {
        "question": "Are zoneless wartime grids in the HES gazetteer (and by extension VHPA) on Indian 1960 or WGS84?",
        "method": __doc__.strip(),
        "match_window_km": MATCH_KM,
        "inputs": {"hes_hamlets": len(hes), "gns_populated_place_records": n_gns},
        "match_stats": dict(stats),
        "headline_unrounded_gns": head,
        "minute_rounded_gns_for_comparison": summarise(rounded),
        "all_pairs": summarise(pairs),
        "by_zone_unrounded": by_zone,
        "by_latitude_unrounded": by_region,
        "area_of_interest_unrounded": summarise(aoi),
        "fits": fits,
        "conclusion": conclusion,
        "military_sites_check": (
            "Not done: GNS military sites and OSM/Wikidata points are both modern "
            "WGS84-ish positions, so comparing them says nothing about the datum of "
            "wartime grids; GNS military_site has only 31 records, mostly minute-rounded."
        ),
        "sample_pairs": [
            {
                k: (round(v, 6) if isinstance(v, float) else v)
                for k, v in p.items()
                if k not in ("ind", "wgs", "off_ind", "off_wgs", "shift_wgs_minus_ind")
            }
            | {"off_ind_m": [round(x) for x in p["off_ind"]], "off_wgs_m": [round(x) for x in p["off_wgs"]]}
            for p in unrounded[:: max(1, len(unrounded) // 15)][:15]
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(json.dumps({k: result[k] for k in ("inputs", "match_stats", "headline_unrounded_gns", "fits")}, indent=2))
    print(conclusion)
    return result


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    main()
