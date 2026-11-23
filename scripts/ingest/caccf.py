"""NARA Combat Area Casualties Current File (CACCF), Dec 1998 extract -> loss_incident evidence.

Source: NARA RG 330, catalog id 1965870. One-off static fetches of two /medialz/ files
(catalog.archives.gov robots.txt is "Disallow: /", so we never crawl; just these two URLs, cached):
  CAC.DEC98.CURRENT  fixed-width ASCII, 164 chars + CRLF, 58,193 US casualty records
  153.1DP.pdf        documentation packet (record layout, DoD 7730.22 code tables)

Scope: only body-not-recovered records (positions 120-122 == "BNR"). Recovered bodies are
counted, not stored. The file has no grid or place name: the only location is a South Vietnam
province code (positions 157-158, DoD 7730.22 Table 6) plus the country of casualty
(positions 2-3, Table 2). Placement, from most to least precise:
  * province code in Table 6 -> HES province centroid (data/work/hes_provinces.json),
    uncertainty = that province's radius_m, which covers the whole province (farthest hamlet or
    upland place + 10 km, see hes_gazetteer.py); three Table 6 areas have no HES province
    (DMZ, Con Son, Phu Quoc) and use hand-set centroids, flagged in raw;
  * "Province unknown, MR n" (91-94) -> centroid of the HES provinces in that military region,
    radius covering all of them;
  * province unknown / unlisted code, country South or North Vietnam -> country centroid with
    a radius covering the whole country (several hundred km);
  * offshore codes (81-84, 89), Laos, Cambodia, China -> skipped and counted.

Dates: positions 59-66 hold the date of death (for the missing, often the later date death was
declared). For BNR records positions 113-118 (labelled "date tour in SE Asia began") equal the
death date in 315/323 hostile-killed cases and precede it for died-while-missing cases, i.e. they
carry the incident date; we use them as date_start when valid and not after the death date,
and keep both in raw.

The file has no unit field: titles carry the service instead. Personal names are public record;
service numbers are NOT stored (post-1969 Army service numbers are SSNs): only an 8-hex hash of
the number is used to make the record id unique.
Caveat: remains recovered after Dec 1998 are still flagged BNR here.

Inputs: the two cached NARA files above and data/work/hes_provinces.json (run hes_gazetteer.py
first). Outputs: evidence rows (source "caccf") in data/evidence.sqlite and a run summary in
data/work/caccf_summary.json.

Run:  .venv/bin/python scripts/ingest/caccf.py [--refresh]
"""

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import paths  # noqa: E402
import store  # noqa: E402

SOURCE = "caccf"
BASE = "https://catalog.archives.gov/medialz/electronic-records/rg-330/cacdb/"
DATA_URL = BASE + "CAC.DEC98.CURRENT"
DOC_URL = BASE + "153.1DP.pdf"
CATALOG_URL = "https://catalog.archives.gov/id/1965870"
HES_PROVINCES = paths.WORK / "hes_provinces.json"
SUMMARY = paths.WORK / "caccf_summary.json"
RECORD_LEN = 164

# DoD 7730.22 Encl 8 Table 6 (153.1DP.pdf, NARA reference pages 39-40), read from the page
# images, not the OCR layer (the OCR misreads slashed zeros). code -> (name, military region).
PROVINCES = {
    "01": ("Quang Tri", 1),
    "02": ("Thua Thien", 1),
    "03": ("Quang Nam", 1),
    "04": ("Quang Tin", 1),
    "05": ("Quang Ngai", 1),
    "50": ("DMZ", 1),
    "06": ("Kontum", 2),
    "07": ("Binh Dinh", 2),
    "08": ("Pleiku", 2),
    "09": ("Phu Yen", 2),
    "10": ("Darlac", 2),
    "11": ("Khanh Hoa", 2),
    "12": ("Tuyen Duc", 2),
    "13": ("Quang Duc", 2),
    "15": ("Lam Dong", 2),
    "16": ("Ninh Thuan", 2),
    "17": ("Binh Thuan", 2),
    "41": ("Phu Bon", 2),
    "14": ("Phuoc Long", 3),
    "18": ("Binh Tuy", 3),
    "19": ("Long Khanh", 3),
    "21": ("Binh Long", 3),
    "22": ("Tay Ninh", 3),
    "23": ("Binh Duong", 3),
    "24": ("Gia Dinh", 3),
    "25": ("Bien Hoa", 3),
    "26": ("Phuoc Tuy", 3),
    "27": ("Long An", 3),
    "42": ("Hau Nghia", 3),
    "28": ("Kien Tuong", 4),
    "29": ("Kien Phong", 4),
    "30": ("Dinh Tuong", 4),
    "31": ("An Giang", 4),
    "32": ("Vinh Long", 4),
    "33": ("Kien Hoa", 4),
    "34": ("Vinh Binh", 4),
    "35": ("Phong Dinh", 4),
    "36": ("Kien Giang", 4),
    "37": ("Chuong Thien", 4),
    "38": ("Ba Xuyen", 4),
    "39": ("An Xuyen", 4),
    "40": ("Con Son Island", 4),
    "43": ("Go Cong", 4),
    "44": ("Bac Lieu", 4),
    "45": ("Chau Doc", 4),
    "46": ("Sa Dec", 4),
    "49": ("Phu Quoc Island", 4),
}
OFFSHORE = {
    "81": "Offshore, MR 1",
    "82": "Offshore, MR 2",
    "83": "Offshore, MR 3",
    "84": "Offshore, MR 4",
    "89": "Offshore, province and MR unknown",
}
MR_UNKNOWN = {"91": 1, "92": 2, "93": 3, "94": 4}  # "Province unknown, MR n"
# Table 6 areas with no HES province: hand-set centre and a radius covering the area.
HAND_SET = {
    "50": (106.85, 17.0, 45_000, "hand-set: DMZ strip along the Ben Hai river, ~75 km long"),
    "40": (106.61, 8.69, 10_000, "hand-set: Con Son island group"),
    "49": (103.97, 10.23, 30_000, "hand-set: Phu Quoc island"),
}
# Country-level fallbacks (only Vietnam is placed). South Vietnam is derived from HES; North
# Vietnam's box (102.1-108.1 E, 17.0-23.4 N) is hand-set, centre of the box, radius to corners.
NORTH_VIETNAM = (105.1, 20.2, 480_000)

COUNTRIES = {
    "VS": "South Vietnam",
    "VN": "North Vietnam",
    "LA": "Laos",
    "CB": "Cambodia",
    "CH": "China",
    "TH": "Thailand",
}
SERVICES = {
    "A": "US Army",
    "F": "US Air Force",
    "N": "US Navy",
    "M": "US Marine Corps",
    "P": "US Coast Guard",
    "C": "US Coast Guard",
}
CASUALTY_TYPES = {
    "A1": "hostile, killed",
    "A2": "hostile, died of wounds",
    "A3": "hostile, died while missing",
    "A4": "hostile, died while captured/interned",
    "B5": "hostile, missing, returned",
    "B6": "hostile, current missing",
    "B7": "hostile, captured, returned",
    "B8": "hostile, current captured",
    "C1": "non-hostile, died of other causes",
    "C2": "non-hostile, died of illness/injuries",
    "C3": "non-hostile, died while missing",
    "D5": "non-hostile, missing, returned",
    "D6": "non-hostile, current missing",
}
AIR_CODES = {
    "1": "fixed-wing aircrew, pilot",
    "2": "fixed-wing aircrew, other",
    "3": "fixed-wing, non-aircrew",
    "4": "helicopter, pilot",
    "5": "helicopter, other aircrew",
    "6": "helicopter, non-aircrew",
    "7": "ground casualty",
    "8": "sea casualty",
}
BURIAL_RE = re.compile(r"BURI|CEMET|GRAVE|INTERR", re.I)


def fetch(refresh=False):
    """Cache the data file and its documentation PDF; return the data file's path."""
    data = common.cached_get(DATA_URL, SOURCE, name="CAC.DEC98.CURRENT", refresh=refresh)
    common.cached_get(DOC_URL, SOURCE, name="153.1DP.pdf", refresh=refresh)
    return data


def read_lines(raw: bytes):
    """Split the fixed-width file into 164-char records (latin-1, CRLF)."""
    lines = [ln for ln in raw.decode("latin-1").split("\r\n") if ln.strip()]
    bad = [ln for ln in lines if len(ln) != RECORD_LEN]
    if bad:
        raise ValueError(f"{len(bad)} records are not {RECORD_LEN} chars, e.g. {bad[0]!r}")
    return lines


def _mdy(s):
    """'04/28/69' (MM/DD/YY, all 19xx) -> date, or None when the field is blank or invalid."""
    try:
        m, d, y = s.split("/")
        return date(1900 + int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None


def _yymmdd(s):
    """'690428' (YYMMDD, all 19xx) -> date, or None when the field is blank or invalid."""
    try:
        return date(1900 + int(s[:2]), int(s[2:4]), int(s[4:6]))
    except (ValueError, TypeError):
        return None


def parse_line(ln):
    """One 164-char record -> dict of its fields (1-based positions per the 153.1DP record layout)."""

    def f(a, b):
        return ln[a - 1 : b]

    return {
        "service": f(1, 1),
        "country": f(2, 3),
        "casualty_type": f(4, 5),
        "ref_no": f(6, 10).strip(),
        "name": " ".join(f(12, 39).split()),
        "date_processed": f(40, 43),
        "service_number": f(44, 52).strip(),
        "grade": f(53, 56).strip(),
        "pay_grade": f(57, 58).strip(),
        "death_date": f(59, 66),
        "home_city": f(67, 86).strip(),
        "home_state": f(87, 88).strip(),
        "occupation": f(89, 93).strip(),
        "birth_date": f(94, 101).strip(),
        "reason": f(102, 102),
        "air_code": f(103, 103),
        "tour_began": f(113, 118),
        "last_record": f(119, 119),
        "body": f(120, 122),
        "age": f(123, 124).strip(),
        "component": f(125, 125),
        "comments": f(126, 156).strip(),
        "province": f(157, 158),
        "corps": f(159, 160).strip(),
        "procd": f(161, 162).strip(),
        "flag": f(163, 164).strip(),
    }


def record_id(p):
    """Stable id: service + country + casualty type + ref no, then an 8-hex hash of the service
    number, because the first four alone are not unique across the BNR records (2048/2058)."""
    key = hashlib.sha1(p["service_number"].encode()).hexdigest()[:8]
    return f"{p['service']}{p['country']}{p['casualty_type']}{p['ref_no']:>5}".replace(" ", "_") + "-" + key


def load_places(path=HES_PROVINCES):
    """Placement table from the HES province circles: {code: (lon, lat, radius_m, how)}.

    Keys are the Table 6 province codes, the "province unknown, MR n" codes 91-94, and the
    country codes "VS" / "VN" used as the last fallback."""
    hes = json.loads(Path(path).read_text(encoding="utf-8"))["provinces"]

    def norm(s):
        return re.sub(r"[^A-Z]", "", s.upper())

    by_name = {norm(n): (code, v) for code, v in hes.items() for n in v["names"]}
    places = {}
    for code, (name, _mr) in PROVINCES.items():
        if code in HAND_SET:
            lon, lat, r, how = HAND_SET[code]
            places[code] = (lon, lat, r, how)
            continue
        hcode, v = by_name[norm(name)]  # KeyError here means the HES file changed: fail loud
        places[code] = (v["lon"], v["lat"], v["radius_m"], f"hes_provinces {hcode} {v['names'][0]}")

    def cover(provs, label):
        """Mean of the province centroids, radius reaching the far edge of every province circle."""
        lon = sum(v["lon"] for v in provs) / len(provs)
        lat = sum(v["lat"] for v in provs) / len(provs)
        r = max(_dist_m(lon, lat, v["lon"], v["lat"]) + v["radius_m"] for v in provs)
        return (round(lon, 6), round(lat, 6), round(r, -3), label)

    for code, mr in MR_UNKNOWN.items():
        provs = [v for v in hes.values() if v["region"] == str(mr)]
        places[code] = cover(provs, f"mean of {len(provs)} HES province centroids in MR {mr}")
    places["VS"] = cover(list(hes.values()), "mean of all HES province centroids (South Vietnam)")
    places["VN"] = (*NORTH_VIETNAM, "hand-set: centre of North Vietnam box 102.1-108.1E 17.0-23.4N")
    return places


def _dist_m(lon1, lat1, lon2, lat2):
    """Great-circle (haversine) distance in metres on a 6371 km sphere."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def place(p, places):
    """Return (lon, lat, radius, original_coord, original_datum, how) or (None, reason)."""
    code, country = p["province"], p["country"]
    cname = COUNTRIES.get(country, country)
    # 6 BNR records (revised 1992) say Laos/North Vietnam but kept a South Vietnam province
    # code; the country field is the later correction, so it wins (province kept in raw).
    if code in PROVINCES and country != "VS" and not (code == "50" and country == "VN"):
        code = "conflict"
    if code in PROVINCES:
        lon, lat, r, how = places[code]
        return (lon, lat, r, f"province code {code} ({PROVINCES[code][0]})", "province-centroid", how)
    if code in OFFSHORE:
        return None, f"at sea ({OFFSHORE[code]})"
    if code in MR_UNKNOWN:
        lon, lat, r, how = places[code]
        return (lon, lat, r, f"province code {code} (province unknown, MR {MR_UNKNOWN[code]})", "region-centroid", how)
    if country in ("VS", "VN"):
        lon, lat, r, how = places[country]
        shown = f'"{p["province"]}"'
        why = "conflicts with country, country used" if code == "conflict" else "unknown"
        return (lon, lat, r, f"country {country} ({cname}), province code {shown} ({why})", "country-centroid", how)
    return None, f"outside Vietnam ({cname})"


def build(lines, places):
    """Turn file records into evidence.

    Returns (records, skipped reasons Counter, burials dropped, Counter of placement datums).
    Only BNR records are kept; those whose comment names a burial or that cannot be placed in
    Vietnam are counted, not stored."""
    records, skipped, burials = [], Counter(), 0
    stats = Counter()
    for ln in lines:
        p = parse_line(ln)
        if p["body"] != "BNR":
            skipped["body recovered (not BNR)"] += 1
            continue
        if BURIAL_RE.search(p["comments"]):
            burials += 1
            continue
        res = place(p, places)
        if res[0] is None:
            skipped[res[1]] += 1
            continue
        lon, lat, radius, orig, datum, how = res
        death, tour = _mdy(p["death_date"]), _yymmdd(p["tour_began"])
        if tour and death and tour <= death:
            incident, basis = tour, "positions 113-118 (incident date)"
        elif death:
            incident, basis = death, "positions 59-66 (date of death)"
        else:
            incident, basis = None, "no valid date"
        service = SERVICES.get(p["service"], p["service"])
        when = incident.isoformat() if incident else "date unknown"
        rank = p["grade"] or "?"
        ctype = CASUALTY_TYPES.get(p["casualty_type"], p["casualty_type"])
        air = AIR_CODES.get(p["air_code"], p["air_code"])
        desc = (
            f"US casualty, body not recovered (CACCF Dec 1998). {ctype.capitalize()}; {air}. "
            f"Country of casualty: {COUNTRIES.get(p['country'], p['country'])}. "
            f"Location known only as {orig}; point is a centroid, not a site. "
            f"Incident date {when} ({basis})"
            + (f"; date of death/declared death {death.isoformat()}" if death and death != incident else "")
            + (f". Home of record: {p['home_city'].title()}, {p['home_state']}" if p["home_city"] else "")
            + (f". Comment: {p['comments']}" if p["comments"] else "")
            + "."
        )
        raw = dict(
            p,
            country_name=COUNTRIES.get(p["country"]),
            casualty_type_meaning=ctype,
            air_code_meaning=air,
            service_name=service,
            incident_date_basis=basis,
            placement=how,
        )
        del raw["service_number"]  # an SSN for many records: never stored, only hashed into the id
        records.append(
            store.Evidence(
                source=SOURCE,
                source_record_id=record_id(p),
                kind="loss_incident",
                lon=round(lon, 6),
                lat=round(lat, 6),
                uncertainty_m=float(radius),
                original_coord=orig,
                original_datum=datum,
                title=f"BNR: {rank} {p['name']}, {service}, {when}",
                date_start=incident.isoformat() if incident else None,
                date_end=incident.isoformat() if incident else None,
                description=desc,
                url=CATALOG_URL,
                raw=raw,
            )
        )
        stats[datum] += 1
    return records, skipped, burials, stats


def save(db, records):
    """Register the source and make the store hold exactly `records` for it. Rows this run no
    longer produces are removed by store.sync_source (withdrawn instead when in a case).
    Returns (kept, deleted, withdrawn)."""
    store.upsert_source(
        db,
        SOURCE,
        name="NARA Combat Area Casualties Current File (CACCF), Dec 1998 extract",
        url=CATALOG_URL,
        licence="US federal record, unrestricted (NARA catalog)",
        notes="RG 330, NAID 1965870. BNR records only, placed at province/MR/country centroids "
        "from the province code (DoD 7730.22 Table 6), with radii covering the whole area. "
        "Service numbers not stored. Remains recovered after 1998 still show BNR; check "
        "against DPAA accounted-for lists.",
    )
    return store.sync_source(db, SOURCE, records, reason="no longer produced from the CACCF file")


def main(argv=None):
    """Fetch (or reuse the cache), ingest, sync the store and write the run summary."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download the CACCF file")
    args = ap.parse_args(argv)
    path = fetch(args.refresh)
    common.progress(None, "Reading the casualty records")
    lines = read_lines(path.read_bytes())
    places = load_places()
    records, skipped, burials, stats = build(lines, places)
    ids = [r.source_record_id for r in records]
    assert len(ids) == len(set(ids)), "duplicate source_record_id"
    common.progress(None, f"Saving {len(records):,} casualty records")
    n, deleted, withdrawn = save(store.connect(), records)
    aoi = [r for r in records if common.in_bbox(r.lon, r.lat)]
    summary = {
        "file_records": len(lines),
        "bnr_records": len(lines) - skipped["body recovered (not BNR)"],
        "ingested": n,
        "by_placement": dict(stats),
        "in_area_of_interest": len(aoi),
        "skipped": dict(skipped),
        "burials_dropped": burials,
        "stale_deleted": deleted,
        "stale_withdrawn": withdrawn,
        "by_province_code": dict(Counter(r.raw["province"] for r in records).most_common()),
    }
    SUMMARY.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    main()
