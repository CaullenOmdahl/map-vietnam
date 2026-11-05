"""VHPA (Vietnam Helicopter Pilots Association) KIA pages -> aircraft_crash / loss_incident evidence.

What it reads
  The VHPA casualty site, crawled politely (no robots.txt; >= 3 s between requests) and cached in
  data/raw/vhpa/:
    KIA/panel/panelind.htm -> 140 panel pages PANEL{01-70}{E|W}.HTM
      -> incident pages KIA/incident/<id>.HTM (one per lost aircraft)
      -> person pages KIA/K#####.HTM (pilots only; they carry the casualty PROVINCE, which the
         incident pages lack, and sometimes a grid the incident page does not have).
  Also data/work/gns_index.json (gns.py), data/work/hes_provinces.json (hes_gazetteer.py, optional)
  and data/raw/osm/vn_boundary.json (wikidata_osm.py).

What it writes
  One store.Evidence per loss into the evidence database (store.sync_source: records no longer
  produced are withdrawn), and a run summary in data/work/vhpa_summary.json. Only facts and short
  fields are stored (the site is copyrighted): no accident report, REFNO synopsis, pilot page
  notes or war-story text - those are read to decide the kind and are never stored or quoted.

Run
  .venv/bin/python scripts/ingest/vhpa.py [--refresh-index] [--retry-failed]

Position
  Grids are written zoneless ("UTM grid coordinates: WS705532"). They are decoded with
  gridref.candidates on Indian 1960 (VHPA does not state a datum; the wartime L7014 sheets are on
  Indian 1960, and scripts/ingest/datum_check.py confirmed it by calibrating wartime grids against
  known-position features - that calibration resolves only ~0.5-1 km, so no grid position gets a
  radius under 500 m). The candidate zone is picked with the province / country and the result is
  checked against every province the person pages name.

  The radius is widened, never the position moved, whenever the source contradicts itself, because
  we cannot tell which statement is wrong:
  - a grid outside a named province (K12228: Vinh Long province, grid YD761215; K11427: Pleiku,
    grid ~220 km away): the radius covers the grid and the whole of every named province;
  - other grids the person pages give for the same loss (often a letter typo): the radius reaches
    them;
  - grids or lat/longs a war story gives for a crash: the radius reaches them;
  - a country the source names that the position is not in (a Quang Tri grid for a loss put in
    Laos; a 'South Vietnam' grid outside Vietnam's OSM boundary): the radius reaches that country.
  The province check uses the hand-made disks in PROVINCES (drawn to cover each province and no
  more: Saigon 15 km). The HES hamlet disk (farthest hamlet + 10 km: Saigon 77 km) is far bigger,
  so it never passes a grid; it only widens a radius that must cover a province.

  Crew links to a person page about a different incident (71033143KIA -> K11035 of 67090113KIA)
  or dated more than a month away (67111455KIA of 11/1967 -> K10853 of 08/1971) are not used for
  location and are named in the description.

Scope
  Losses in Thailand (e.g. U-Tapao air base) are dropped. Losses the source places in Laos /
  Cambodia (or nowhere) are kept only within 40 km of the nearest place in the GNS Vietnam
  gazetteer (border areas: Lam Son 719, the 1970 Cambodian incursion); at sea, within 150 km. The
  description always shows the source's country labels: the incident page's coded line, each pilot
  page's Country line, and a country a summary puts the loss in ('... extraction mission in Laos.').

Kind (aircraft_crash vs loss_incident)
  aircraft_crash only when the source says the aircraft came down: the incident page's coded
  outcome lines ('The helicopter Crashed.', or 'Ultimate crash and burn' after 'Continued Flight' /
  a forced or emergency landing), crash words in a page or pilot page summary (not the crash of
  another aircraft named by tail number), a crash described in a longer account (accident report,
  REFNO synopsis, pilot page notes, or a war story sentence that plainly says THIS aircraft
  crashed), or a primary cause that is itself a crash (mid-air, wire strike). A ground-casualty
  reading of the short fields gives way to a crash the coded lines or a longer account describe,
  and the conflict is stated; a mortar / rocket hit while the aircraft was in the air (on short
  final, lifting off) is not a ground casualty. Everything else is loss_incident and the title
  says which: a ground casualty, a loss where the text says the aircraft did not come down, or
  one where the source does not say. 'aircraft lost or crashed' (the DCAS reason on nearly every
  pilot page) is a casualty category, not a description. Helicopter vs fixed-wing comes from the
  type designation.

Status per man
  A crew code is corrected by the man's own pilot page where they disagree (INJ on the crew line,
  Killed In Action on his page; BNR vs Remains Returned), and the page's 'Number killed in
  accident' line is shown when it differs. A burial line marked '(IMO)' (in memoriam) means remains
  NOT recovered; no burial line is read as a statement that remains were recovered.

Determinism
  Every page in the cache is used, linked or not (K12228 is linked from no panel), so the output
  is a function of the cache. Incident pages linked only from a person page are fetched too. Pages
  that 404 are remembered in data/raw/vhpa/_fetch_failures.json and not requested again
  (--retry-failed clears that); a stored HTTP error body is not treated as a page. Any other fetch
  failure aborts the run before anything is written. Person pages with no incident page make
  person-only records: pages linking the same incident are ONE record whose id is that incident id
  (stable if the incident page appears later); unlinked pages are grouped by date + tail and take
  the lowest K id.

Skipped and counted: records with no usable grid (province-level sources cover those), burial
places, losses outside scope, and losses where the source records no death at all (accident line
0 killed, no status codes, no pilot pages). Other VHPA losses the same day within 10 km are named.
"""

import argparse
import datetime
import html
import http.client
import itertools
import json
import math
import re
import sys
import urllib.error
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import gridref  # noqa: E402
import store  # noqa: E402

# ---------------------------------------------------------------- configuration
SOURCE = "vhpa"
BASE = "https://www.vhpa.org/KIA/"
MIN_INTERVAL = 3.0  # seconds between requests to the site
WORK = store.ROOT / "data" / "work"
GNS_INDEX = WORK / "gns_index.json"  # built by gns.py; required (scope checks)
HES_PROVINCES = WORK / "hes_provinces.json"  # built by hes_gazetteer.py; optional (wider province disks)

RAW = common.RAW / SOURCE  # page cache; read at call time so tests can point it elsewhere
FAILURES_NAME = "_fetch_failures.json"
PERMANENT_HTTP = {404, 410}  # remembered and not requested again; any other failure aborts
_FAILURES = None  # name -> {"url", "status", "at"}; loaded lazily from RAW / FAILURES_NAME
FETCH_ERRORS = Counter()  # per-run fetch error counts, reported in the summary

# Error budget on top of the 100 m square: plotting/reporting error of wartime loss grids
# (spec: "often several hundred metres to 1-2 km").
REPORT_ERROR_M = 400.0
# Datum: Indian 1960, confirmed by datum_check.py (GNS places centre on the Indian 1960 reading);
# the calibration only resolves ~0.5-1 km, so no grid-derived position is tighter than this.
GRID_DATUM = "indian1960"
GRID_ZONES = (47, 48, 49)  # UTM zones that cover Vietnam, Laos and Cambodia
GRID_MIN_UNC_M = 500.0
DATUM_NOTE = (
    "datum Indian 1960 (not stated by VHPA; confirmed by calibration of wartime grids "
    "against known-position features, which resolves ~0.5-1 km)"
)
# A lat/long with no datum stated (WGS84 vs Indian 1960 readings differ by ~0.5 km).
LATLON_DATUM_M = 500.0
GRID_AGREE_KM = 1.0  # two grids for one loss this close agree (100 m squares + plotting error)

# Crew / passenger status codes. KIA killed in action, BNR killed and body not recovered, DOI died
# of injuries, RR remains recovered later, RES rescued, WIA wounded, INJ injured, POW prisoner of
# war, REL released POW, MIA missing in action; KIC, DIC, KNF and RTD also appear on the pages.
KILLED = {"KIA", "BNR", "DOI", "KIC", "DIC", "KNF"}
STATUS_CODES = KILLED | {"RES", "RR", "WIA", "INJ", "POW", "REL", "MIA", "RTD"}
# The incident page's coded country line takes exactly one of these.
COUNTRIES = (
    "South Vietnam",
    "North Vietnam",
    "Laos",
    "Cambodia",
    "Over Water",
    "Thailand",
    "Vietnam unspecified",
    "Unknown",
    "Classified",
)
# Generous (west, south, east, north) lon/lat boxes for a country-level sanity check when no
# province is known, and for picking a grid zone inside the named country.
COUNTRY_BBOX = {
    "South Vietnam": (104.3, 8.3, 109.7, 17.3),
    "North Vietnam": (102.0, 16.8, 108.2, 23.5),
    "Laos": (100.0, 13.8, 107.8, 22.6),
    "Cambodia": (102.2, 10.3, 107.8, 14.8),
    "Thailand": (97.3, 5.5, 105.7, 20.5),
}

# Wartime RVN provinces: approximate centre (lon, lat) and a radius (km) covering the province.
# Hand-made from the 1967-72 province map; good to ~10-20 km, which is enough to catch grids
# that land in the wrong part of the country. Not used to place points.
PROVINCES = {
    "quang tri": (107.05, 16.75, 45),
    "thua thien": (107.55, 16.35, 55),
    "quang nam": (108.0, 15.75, 65),
    "quang tin": (108.3, 15.45, 50),
    "quang ngai": (108.7, 15.0, 55),
    "kontum": (107.95, 14.6, 90),
    "binh dinh": (108.95, 14.15, 65),
    "pleiku": (107.95, 13.85, 70),
    "phu bon": (108.45, 13.35, 45),
    "phu yen": (109.1, 13.1, 50),
    "darlac": (108.15, 12.75, 75),
    "khanh hoa": (109.0, 12.3, 55),
    "quang duc": (107.6, 12.2, 60),
    "tuyen duc": (108.4, 11.9, 45),
    "ninh thuan": (108.9, 11.6, 45),
    "lam dong": (107.8, 11.6, 50),
    "binh thuan": (108.1, 11.1, 70),
    "phuoc long": (107.0, 11.8, 60),
    "binh long": (106.6, 11.7, 40),
    "tay ninh": (106.1, 11.35, 50),
    "binh duong": (106.6, 11.2, 40),
    "bien hoa": (106.95, 11.0, 45),
    "long khanh": (107.3, 10.95, 45),
    "binh tuy": (107.6, 10.8, 45),
    "phuoc tuy": (107.25, 10.55, 40),
    "hau nghia": (106.4, 10.95, 35),
    "gia dinh": (106.7, 10.85, 35),
    "saigon": (106.7, 10.78, 15),
    "long an": (106.4, 10.6, 45),
    "kien tuong": (105.95, 10.75, 45),
    "kien phong": (105.6, 10.55, 45),
    "chau doc": (105.05, 10.65, 50),
    "an giang": (105.35, 10.4, 40),
    "sa dec": (105.75, 10.3, 30),
    "dinh tuong": (106.15, 10.4, 45),
    "go cong": (106.65, 10.35, 25),
    "kien hoa": (106.45, 10.15, 45),
    "vinh long": (106.0, 10.15, 40),
    "vinh binh": (106.3, 9.85, 45),
    "phong dinh": (105.7, 10.0, 40),
    "chuong thien": (105.4, 9.75, 40),
    "ba xuyen": (105.95, 9.6, 50),
    "bac lieu": (105.6, 9.3, 50),
    "an xuyen": (105.0, 9.05, 70),
    "kien giang": (104.9, 10.0, 90),
}
# Spellings and typos seen on the person pages' Location lines -> PROVINCES key (identity entries
# are harmless and keep the table a complete list of what has been seen).
PROVINCE_ALIASES = {
    "thua tien": "thua thien",
    "thua tin": "thua thien",
    "dac lac": "darlac",
    "darlac": "darlac",
    "dak lak": "darlac",
    "ba xugen": "ba xuyen",
    "an xugen": "an xuyen",
    "ba xuven": "ba xuyen",
    "kon tum": "kontum",
    "quang tin": "quang tin",
    "binh buong": "binh duong",
    "kien hua": "kien hoa",
    "phuoc thanh": "binh duong",
    "bin dinh": "binh dinh",
    "qung tri": "quang tri",
    "pleiku": "pleiku",
    "plei ku": "pleiku",
    "gia dinh": "gia dinh",
    "capital military district": "saigon",
    "saigon": "saigon",
    "cholon": "saigon",
    "bien hoa": "bien hoa",
    "ba xuyen": "ba xuyen",
    "phong dinh": "phong dinh",
    "an xuyen": "an xuyen",
    "vinh binh": "vinh binh",
    "kien phong": "kien phong",
}
# A person page location or page summary naming a burial place: the record would map a grave,
# which this source is not about, so it is skipped and counted.
BURIAL_RE = re.compile(r"cemetery|cemetary|graveyard|martyrs|nghia trang|tomb", re.I)


# ---------------------------------------------------------------- text helpers
def _text(fragment):
    """HTML fragment -> plain text: tags dropped, entities decoded, whitespace collapsed."""
    return re.sub(r"[ \t\r\n]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _field(raw, label, stop=r"<br>|<p>|\n"):
    """Plain text of a 'Label: value' line (from the label regex to the first `stop`), or None."""
    m = re.search(label + r"\s*(.*?)(?:" + stop + ")", raw, re.I | re.S | re.M)
    return _text(m.group(1)) if m else None


def _mdy(s):
    """First 'M/D/YYYY' in s -> ISO date; an impossible day keeps 'YYYY-MM', an impossible month
    keeps 'YYYY' (the pages have both kinds of typo). None if there is no date."""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    if not m:
        return None
    mo, d, y = int(m.group(1)), int(m.group(2)), m.group(3)
    if not 1 <= mo <= 12:
        return y
    if not 1 <= d <= 31:
        return f"{y}-{mo:02d}"
    return f"{y}-{mo:02d}-{d:02d}"


def km(lon1, lat1, lon2, lat2):
    """Great-circle distance in km (haversine on a 6371 km sphere; ample for km-scale radii)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- provinces
_HES = None  # cache of _hes_provinces()


def norm_province(s):
    """'Vinh Long Province' -> 'vinh long' (a PROVINCES key), or None."""
    if not s:
        return None
    k = s.lower()
    if "province" in k:
        k = k.split("province")[0]
    k = re.sub(r"[^a-z ]", " ", k)
    k = re.sub(r"\b(province|prov|south|vietnam|rvn|svn)\b", " ", k)
    k = re.sub(r"\s+", " ", k).strip()
    k = PROVINCE_ALIASES.get(k, k)
    if k in PROVINCES:
        return k
    nospace = k.replace(" ", "")
    for p in PROVINCES:
        if p.replace(" ", "") == nospace:
            return p
    return None


def _named_provinces(province):
    """A PROVINCES key or an iterable of them -> the known ones, de-duplicated, order kept."""
    provs = [province] if isinstance(province, str) else list(province or [])
    return [p for p in dict.fromkeys(provs) if p in PROVINCES]


def _hes_provinces():
    """PROVINCES key -> (lon, lat, radius_km) from the HES hamlet gazetteer, if built."""
    global _HES
    if _HES is None:
        _HES = {}
        if HES_PROVINCES.exists():
            for v in json.loads(HES_PROVINCES.read_text())["provinces"].values():
                for n in v["names"]:
                    k = norm_province(n.replace("-", " "))
                    if k and k not in _HES:
                        _HES[k] = (v["lon"], v["lat"], v["radius_m"] / 1000)
    return _HES


def province_geometry(prov):
    """(lon, lat, radius_km) of the hand-table disk of a named province: drawn to cover the whole
    province and no more (Saigon 15 km, Kontum 90 km). This is the disk a grid must fall in for
    the province check. The HES disk (farthest hamlet + 10 km) is far bigger than the province
    (Saigon 77 km, Hau Nghia 109 km), so it is never used to pass a grid; it only widens the
    circle that has to cover a province (province_disks)."""
    return PROVINCES[prov]


def province_disks(prov):
    """Every disk we have for a province: the hand table and, if built, the HES one. The province
    check requires a position inside ALL of them (_excess); a radius that must cover the province
    covers ALL of them (province_reach_km)."""
    disks = [PROVINCES[prov]]
    if prov in _hes_provinces():
        disks.append(_hes_provinces()[prov])
    return disks


def province_reach_km(lon, lat, prov):
    """Radius (km) from a position that covers every disk of the named province."""
    return max(km(lon, lat, cx, cy) + r for cx, cy, r in province_disks(prov))


def _excess(lon, lat, prov):
    """km by which a position lies outside the named province (negative = inside): outside ANY of
    its disks counts, so the tight hand-table disk decides (the HES disk is far larger)."""
    return max(km(lon, lat, cx, cy) - r for cx, cy, r in province_disks(prov))


# ---------------------------------------------------------------- parsers
def parse_panel(raw):
    """Incident page ids and person page ids linked from one panel page."""
    inc = re.findall(r"KIA/incident/([A-Za-z0-9]+)\.HTM", raw, re.I)
    ppl = re.findall(r"KIA/(K\d+)\.HTM", raw, re.I)
    return list(dict.fromkeys(i.upper() for i in inc)), list(dict.fromkeys(p.upper() for p in ppl))


def parse_crew(raw):
    """[{duty, rank, name, status, person_page}] from the 'Crew Members:' block, one per line
    ('P CW2 SMITH JOHN KIA'); status is '' when the last token is not a known code."""
    m = re.search(
        r"Crew Members:(.*?)(?:<br>\s*<br>|REFNO Synopsis|Accident Summary|War Story"
        r"|This record was last)",
        raw,
        re.I | re.S,
    )
    crew = []
    if not m:
        return crew
    for line in re.split(r"<br\s*/?>", m.group(1), flags=re.I):
        person = re.findall(r"\.\./(K\d+)\.HTM", line, re.I)
        s = _text(line)
        if not s:
            continue
        toks = s.split()
        status = toks[-1] if toks[-1] in STATUS_CODES else ""
        body = toks[:-1] if status else toks
        duty = body[0] if body else ""
        rank = body[1] if len(body) > 1 else ""
        crew.append(
            {
                "duty": duty,
                "rank": rank,
                "name": " ".join(body[2:]),
                "status": status,
                "person_page": person[0].upper() if person else None,
            }
        )
    return crew


def parse_passengers(raw):
    """'Passengers and/or other participants:' lines ('SP4 SURNAME GIVEN MIDDLE, AR, PX, KIA': name first,
    status code last)."""
    m = re.search(
        r"Passengers and/or other participants:(.*?)(?:<p>|<br>\s*<br>|REFNO Synopsis"
        r"|Accident Summary|War Story|This record was last)",
        raw,
        re.I | re.S,
    )
    out = []
    for line in re.split(r"<br\s*/?>", m.group(1), flags=re.I) if m else []:
        s = _text(line)
        if not s:
            continue
        last = s.split(",")[-1].strip()
        out.append({"name": s.split(",")[0].strip(), "status": last if last in STATUS_CODES else ""})
    return out


def is_error_page(raw):
    """A stored HTTP error body (the cache holds two '404 Not Found' pages), not a VHPA page."""
    return (
        bool(re.search(r"<title>\s*\d{3} [^<]*</title>", raw or "", re.I))
        and "Crew Members" not in raw
        and "Status:" not in raw
    )


NARRATIVE_END = (
    r"Accident Summary:|REFNO Synopsis|War Story:|This record was last|"
    r"\n[^\n]*Reason:|Crew Members:|Passengers and/or"
)


def _narratives(raw, sections):
    """[(label, text)] of the longer free-text sections of a page, in the order given."""
    out = []
    for label, start in sections:
        m = re.search(start + r"(.*?)(?=" + NARRATIVE_END + r"|$)", raw, re.I | re.S)
        if m and _text(m.group(1)):
            out.append((label, _text(m.group(1))))
    return out


def parse_incident(raw):
    """Facts from one incident page (no narrative text)."""
    h1 = re.search(r"<h1>(.*?)</h1>", raw, re.I | re.S)
    title = _text(h1.group(1)) if h1 else None
    m = re.match(r"Helicopter\s+(\S+)\s+(\S+)", title or "")
    country = None
    for c in COUNTRIES:
        if re.search(r"(?:<br>|<p>)\s*" + re.escape(c) + r"\s*<br>", raw, re.I):
            country = c
            break
    grid = re.search(r"UTM grid coordinates:\s*([A-Z]{2}\s?\d{2,10})", raw)
    hint = re.search(r"Grid Reference\s*(\d{1,2}[C-X])([A-Z]{2}\d{2,10})", raw)
    loss = re.search(r"Loss Coordinates:\s*(\d{6}[NS]?\s+\d{7}[EW]?(?:\s*\([A-Z]{2}\d{4,10}\))?)", raw)
    refno = re.search(r"MIA-POW file reference number:\s*(\w+)", raw)
    # The page's own short Summary line comes before the longer accounts; a 'Summary:' inside a
    # quoted REFNO synopsis (680212211ACD: Senate Select Committee text) is narrative, not it.
    head = raw[
        : min(
            [m.start() for m in [re.search(r"Accident Summary:|REFNO Synopsis|War Story:", raw, re.I)] if m]
            or [len(raw)]
        )
    ]
    summary = _field(head, r"(?<!Accident )Summary:")
    acc = re.search(r"Number killed in accident = (\d+)", raw)
    fate = re.search(
        r"The helicopter (Crashed|Continued Flight|made an? (?:Forced|Emergency) "
        r"Landing|was parked)\.[ \t]*([A-Z][^<\n]{0,80})?",
        raw,
    )
    return {
        "heading": title,
        "aircraft_type": m.group(1) if m else None,
        "tail": m.group(2) if m else None,
        "date": _mdy(_field(raw, r"Date:", stop=r"<br>|MIA-POW|\n")),
        "incident_number": _field(raw, r"Incident number:", stop=r"<br>|\s{3,}|\n"),
        "refno": refno.group(1) if refno else None,
        "unit": _field(raw, r"Unit:"),
        "country": country,
        "grid": grid.group(1).replace(" ", "") if grid else None,
        "grid_zone_hint": hint.group(1) if hint else None,
        "loss_coordinates": loss.group(1) if loss else None,
        "casualties": _field(raw, r"Casualties =", stop=r"\.\s\.|<br>|\n"),
        "summary": summary[:200] if summary else None,
        "crew": parse_crew(raw),
        "passengers": parse_passengers(raw),
        "accident_killed": int(acc.group(1)) if acc else None,
        # The page's coded outcome line ('The helicopter Crashed. Aircraft Destroyed.').
        "fate": _text(fate.group(0)) if fate else None,
        # A later coded line can say how it ended: 'Ultimate crash and burn' (the aircraft stayed
        # airborne for a while, then crashed and burned), after 'Continued Flight' or a forced /
        # emergency landing.
        "ultimate_crash": bool(re.search(r"Ultimate crash and burn", raw)),
        # Longer accounts: read to tell whether a crash is described, never stored ('_' keys).
        # War stories are read separately (_war_story below): they often tell of other aircraft
        # ('heard that another huey ... crashed').
        "_narratives": _narratives(
            raw, (("accident report", r"Accident Summary:"), ("REFNO synopsis", r"REFNO Synopsis:?"))
        ),
        # The war story is read only for sentences that say THIS aircraft crashed and for the
        # places crash sentences give (war_story_crash, war_story_places); never stored or quoted.
        "_war_story": next((t for _, t in _narratives(raw, (("war story", r"War Story:"),))), None),
    }


def parse_person(raw):
    """Facts from a pilot's person page K#####.HTM."""
    inc = re.search(r"incident/([A-Za-z0-9]+)\.HTM", raw, re.I)
    status = _field(raw, r"Status:")
    grid = re.search(r"Military grid coordinates of event:\s*([A-Z]{2}\s?\d{2,10})", raw)
    acft = re.search(r"Aircraft:\s*([A-Z]{1,3}-\d+[A-Z]?)\b(?:\s+tail number\s+(\d\S*))?", _text(raw))
    short = _field(raw, r"Short Summary:")
    burial = _field(raw, r"Burial information:")
    imo = bool(burial and re.search(r"\(IMO\)", burial, re.I))
    return {
        "name": _field(raw, r"Name:"),
        "status": status,
        "date": _mdy(status),
        "unit": _field(raw, r"Unit:"),
        "service": _field(raw, r"Service:"),
        "location": _field(raw, r"(?:^|>)[ \t]*Location:"),
        "country": _field(raw, r"(?:^|>)[ \t]*Country:"),
        # "(IMO)" = in memoriam: a memorial marker for a man whose remains were NOT recovered.
        "burial_recorded": bool(burial) and not imo,
        "burial_imo": imo,
        "primary_cause": _field(raw, r"Primary cause:"),
        "reason": _field(raw, r"(?:^|>)[ \t]*Reason:"),
        "vehicle": _field(raw, r"Vehicle involved:"),
        "grid": grid.group(1).replace(" ", "") if grid else None,
        "aircraft_type": acft.group(1) if acft else None,
        "tail": acft.group(2) if acft else None,
        "short_summary": short[:200] if short else None,
        "incident": inc.group(1).upper() if inc else None,
        "_narratives": _narratives(raw, (("notes", r"Additional information about this casualty:"),)),
    }


# ---------------------------------------------------------------- location: grids and the province check
def parse_loss_coordinates(s):
    """'161349N 1074301E' (DDMMSS / DDDMMSS) -> (lon, lat) or None."""
    m = re.match(r"(\d{2})(\d{2})(\d{2})N?\s+(\d{3})(\d{2})(\d{2})E?(?:\s*\(\w+\))?$", s or "")
    if not m:
        return None
    a = [int(x) for x in m.groups()]
    lat = a[0] + a[1] / 60 + a[2] / 3600
    lon = a[3] + a[4] / 60 + a[5] / 3600
    if a[1] >= 60 or a[2] >= 60 or a[4] >= 60 or a[5] >= 60:
        return None
    return lon, lat


def province_check(lon, lat, province=None, country=None, tol_km=0.0):
    """Does a position fit EVERY named province, or (none named) the country?

    Crew person pages can name different provinces. checked_province is the one that fits
    best (used for the label); the check fails if ANY named province lies further outside than
    tol_km, the position's own uncertainty (the circle must at least touch the province), since
    the source names that place too. No extra slack: a grid outside its province gets a radius
    that covers the province."""
    provs = _named_provinces(province)
    out = {"check": "none", "mismatch_km": None}
    if provs:
        excess = {p: round(_excess(lon, lat, p), 1) for p in provs}
        best = min(provs, key=lambda p: excess[p])
        bad = sorted(p for p in provs if excess[p] > tol_km)
        out["checked_province"] = best
        out["province_excess_km"] = excess
        out["mismatched_provinces"] = bad
        if bad:
            out["check"] = "province_mismatch"
            out["mismatch_km"] = max(excess[p] for p in bad)
        else:
            out["check"] = "province_ok"
    elif country in COUNTRY_BBOX:
        # ~30 km slack: border areas (DMZ, Lao/Cambodian border) are often attributed loosely.
        w, s_, e_, n = COUNTRY_BBOX[country]
        slack = (w - 0.3, s_ - 0.3, e_ + 0.3, n + 0.3)
        out["check"] = "country_ok" if common.in_bbox(lon, lat, slack) else "country_mismatch"
    return out


def locate(grid, province=None, country=None, zone_hint=None):
    """Decode a zoneless grid and check it against the province(s) / country.

    province: a PROVINCES key or a list of them (crew pages can disagree; the candidate closest
    to any of them is chosen, and every one of them is then checked). Without a province the
    candidate in VHPA's own zone hint wins, else the first inside the named country's box.
    Returns dict(lon, lat, square_r, zone, choice, candidates, check, mismatch_km, ...) with the
    province_check() keys, or None if no candidate decodes."""
    try:
        cands = gridref.candidates(grid, datum=GRID_DATUM, zones=GRID_ZONES)
    except ValueError:
        cands = []
    if not cands:
        return None
    provs = _named_provinces(province)

    def excess(c, p):
        return _excess(c[1], c[2], p)

    reason = "only candidate"
    chosen = cands[0]
    if len(cands) > 1:
        if provs:
            chosen = min(cands, key=lambda c: min(excess(c, p) for p in provs))
            reason = f"closest to {'/'.join(provs)} province"
        elif zone_hint and any(c[0] == zone_hint for c in cands):
            chosen = next(c for c in cands if c[0] == zone_hint)
            reason = "matches VHPA's own zone hint"
        elif country in COUNTRY_BBOX:
            inside = [c for c in cands if common.in_bbox(c[1], c[2], COUNTRY_BBOX[country])]
            if inside:
                chosen = inside[0]
                reason = f"inside {country}"
    zone, lon, lat, r = chosen
    out = {
        "lon": lon,
        "lat": lat,
        "square_r": r,
        "zone": zone,
        "choice": reason,
        "candidates": [c[0] for c in cands],
        "check": "none",
        "mismatch_km": None,
    }
    # The check tolerates the position's own error (the circle must at least touch the province).
    tol_km = max(GRID_MIN_UNC_M, r + REPORT_ERROR_M) / 1000
    out.update(province_check(lon, lat, provs, country, tol_km))
    if zone_hint and zone_hint != zone:
        out["zone_hint_disagrees"] = zone_hint
    return out


# ---------------------------------------------------------------- kind: crash and ground-casualty wording
# A loss the source describes as a ground casualty is not an aircraft crash (no wreck to look
# for), unless the same text also describes a crash/shoot-down.
GROUND_TEXT = re.compile(
    r"ground casualty|on the ground|rocket attack|mortar attack|mortar hit|ammo dump|automobile"
    r"|chow line|mess hall|in his hootch|heart attack|claymore|clamore|land ?mine"
    r"|stepped on a mine|perimeter wire|sapper",
    re.I,
)
# Words that describe THIS aircraft coming down. 'aircraft lost or crashed' (the DCAS casualty
# reason on almost every pilot page) is a casualty category, not a description, and is not used.
CRASH_TEXT = re.compile(
    r"crash|shot down|shoot ?down|(?:was|were|been) downed|went down|came down|mid-?air|collid"
    r"|explod|blew up"
    r"|burst into flames|flipped|rolled over|inverted|spun in|went in\b|impacted|ditched|wreck"
    r"|(?:into|struck|hit) (?:the |a )?(?:side of (?:a |the )?)?(?:ground|trees?|jungle|water|sea"
    r"|river|canal|mountain|hill|ridge|dike|wires?)\b|flew into|broke up|disintegrat"
    r"|fell (?:to|into)",
    re.I,
)
# 'did not crash', 'without going down': blanked out before crash words are looked for.
NEGATED_CRASH = re.compile(
    r"\b(?:did not|didn.t|does not|without|no|not|never)\s+(?:\w+\s+){0,3}"
    r"(?:crash\w*|go down|went down)",
    re.I,
)
# The aircraft did not come down: these settle it against a crash read only from a longer
# account or a cause.
STRONG_NO_CRASH = re.compile(
    r"not (?:seriously |badly )?damaged|flew (?:the )?(?:a/c |aircraft |helicopter |ship )?back"
    r"|returned to (?:base|the)|landed safely|running landing|forced to land|precautionary landing"
    r"|did not crash",
    re.I,
)
# The man was hit/died without the text saying the aircraft came down.
WEAK_NO_CRASH = re.compile(
    r"\bDOA\b|died (?:of|from) (?:his )?wounds|died [\w ]{0,20}later|died the next day"
    r"|died .{0,40}(?:hospital|evac)|rott?or (?:blade )?(?:hit|struck)|wounded|wounding"
    r"|\bdamaged\b",
    re.I,
)
# Pilot page primary causes that themselves say the aircraft came down.
CRASH_CAUSES = re.compile(
    r"mid-?air|wire strike|shot down|explo|lost main rotor|main rotor loss"
    r"|lost tail boom|crash",
    re.I,
)
# Pilot page 'Primary cause' / 'Reason' values (lower-cased, exact) that put the death on the
# ground or outside any aircraft loss.
GROUND_CAUSES = {
    "ground casualty",
    "ground accident",
    "rocket attack",
    "heart attack",
    "malaria",
    "hepatitis",
    "aneurism",
    "homicide",
    "drowned",
    "exposure",
    "claymore mine",
    "land mine",
    "mortar attack",
}
GROUND_REASONS = {"heart attack", "illness or disease", "vehicle lost or crashed", "malaria", "hepatitis", "suicide"}


# A US Army tail number ('66-16592': fiscal year + serial).
TAIL_RE = re.compile(r"\b\d{2}-\d{5}\b")


# Crash words that leave no doubt an aircraft came down. A longer account must use one of these
# to outweigh a coded 'Continued Flight' / landing line or a ground-casualty reading ('crew went
# in', 'the truck exploded', 'autogyrated into the river' do not).
STRONG_CRASH = re.compile(
    r"crash|shot down|shoot ?down|spun in|went down|came down|impacted"
    r"|mid-?air|collid|burst into flames|broke up|disintegrat",
    re.I,
)
# A crash word followed shortly by 'of / between / with / involving <tail>': the crash of the
# aircraft named there.
OTHER_TAIL_AFTER = re.compile(r"\s*(?:of|between|with|involving)\b[^.]{0,40}?\b(\d{2}-\d{5})\b")


def _about_other_aircraft(t, end, own_tail):
    """Is the crash word ending at `end` the crash OF another aircraft, named by tail number
    ('the mid-air between BLUE 1 (66-16592) and the command ship (66-16601)' on the page of
    66-16206)?"""
    m = OTHER_TAIL_AFTER.match(t, end)
    if not (own_tail and m):
        return False
    return own_tail not in TAIL_RE.findall(t[end : t.find(".", end) if "." in t[end:] else len(t)])


def crash_wording(text, own_tail=None, strong=False):
    """The words of `text` that describe a crash (not negated, not the crash of another aircraft
    named by tail number), shortened, or None. 'crash' and 'shot down' are quoted in preference to
    the weaker words; strong=True accepts only STRONG_CRASH words."""
    if not text:
        return None
    t = NEGATED_CRASH.sub(" ", text)
    for pat in (
        re.compile(r"crash", re.I),
        re.compile(r"shot down|shoot ?down", re.I),
        STRONG_CRASH if strong else CRASH_TEXT,
    ):
        for m in pat.finditer(t):
            if _about_other_aircraft(t, m.end(), own_tail):
                continue
            a, b = max(0, m.start() - 60), min(len(t), m.end() + 60)
            return ("..." if a else "") + t[a:b].strip() + ("..." if b < len(t) else "")
    return None


def _primary_texts(inc, persons):
    """[(label, text)] of the short summaries (incident page, pilot pages): quotable short fields."""
    return ([("incident page summary", inc.get("summary"))] if inc.get("summary") else []) + [
        (f"pilot page {x.get('_id')} summary", x["short_summary"]) for x in persons if x.get("short_summary")
    ]


# The text puts the aircraft in the air when it was hit: a mortar / rocket hitting it then is
# not a ground casualty ('Hit by 60 mm mortar while on short final').
IN_FLIGHT = re.compile(
    r"short final|on final|on approach|in flight|lift-?off|lifting off|"
    r"take-?off|taking off|hover|\bat \d[\d,]* (?:ft|feet)\b|knots",
    re.I,
)


def ground_reason(inc, persons):
    """Why the source's short fields say this was not an aircraft crash (a ground casualty), or
    None. (build_record still lets a crash described elsewhere on the pages win; see classify.)"""
    texts = [t for _, t in _primary_texts(inc, persons)]
    if any(crash_wording(t) or re.search(r"did not crash", t, re.I) for t in texts):
        return None
    if any(IN_FLIGHT.search(t) for t in texts):
        return None
    for t in texts:
        if GROUND_TEXT.search(t):
            return f'"{t}"'
    for x in persons:
        cause = (x.get("primary_cause") or "").strip().lower()
        reason = (x.get("reason") or "").strip().lower()
        if cause in GROUND_CAUSES:
            return f"pilot page {x.get('_id')} primary cause: {x['primary_cause']}"
        if reason in GROUND_REASONS:
            return f"pilot page {x.get('_id')} reason: {x['reason']}"
    return None


def _narrative_texts(inc, persons):
    """[(label, text)] of the longer accounts (accident report, REFNO synopsis, pilot page
    notes): read to decide the kind, NEVER stored or quoted."""
    return [(f"the {label} on the incident page", t) for label, t in inc.get("_narratives") or []] + [
        (f"the notes on pilot page {x.get('_id')}", t) for x in persons for _, t in x.get("_narratives", [])
    ]


def _describes(label):
    """'the notes on pilot page K1' -> '... describe a crash' (plural noun), else '... describes'."""
    return f"{label} {'describe' if label.startswith('the notes') else 'describes'} a crash"


# ---------------------------------------------------------------- kind: war stories
# War stories often tell of other aircraft ('heard that another huey ... crashed', 'the lead
# ship ... crashed in the LZ while another (our tail) was destroyed'), of crashes that nearly
# happened ('in danger of crashing') or of guesses ('may have had a mid-air'). Only a sentence
# that plainly says THIS aircraft crashed counts: it names the aircraft's own tail number (or
# its last three digits: 'Aircraft 699'), or 'the/this/our aircraft|helicopter|ship|LOH|<type>'
# is the one that crashed / was shot down, or 'we crashed / were shot down', and nothing in the
# sentence points at another aircraft or at doubt.
WS_SENTENCE = re.compile(r"(?<=[.!?])\s+")
WS_NOT_OURS = re.compile(
    r"\b(?:another|other|others|lead|second|third|wing ?man|chase|trail|heard|rumou?r|danger"
    r"|almost|nearly|would|could|may have|might|probably|apparently|appears|seemed|if|before"
    r"|helicopters|aircraft were|ships)\b",
    re.I,
)
WS_CRASH_VERB = r"(?:crashed|(?:was|were|been|got) shot down|went down|came down|spun in)"


def _own_refs(inc):
    """Regexes that name this aircraft in a war story: its full tail number, then (for a standard
    'NN-NNNNN' tail) the last three digits after a word that introduces an aircraft ('ship 699')."""
    tail = inc.get("tail") or ""
    refs = [re.escape(tail)] if tail else []
    if re.fullmatch(r"\d{2}-\d{5}", tail):
        refs.append(r"(?:aircraft|ship|helicopter|huey|tail(?: number)?|chalk|on)\s+#?" + tail[-3:] + r"\b")
    return refs


# A story that doubts which aircraft its narrator was in ('no idea what the tail number of the
# ship we were flying was', '... in 915 or was that my ship??'): only a sentence naming this
# aircraft's full tail number then speaks for it ('we', 'our ship' and 'Aircraft 915' do not).
WS_TAIL_DOUBT = re.compile(
    r"\b(?:no idea|not sure|unsure|not certain|(?:do not|don'?t|can'?t|cannot) (?:know|remember|"
    r"recall))\b[^.?!]{0,40}\btail\b|\bor was that my (?:ship|aircraft|helicopter)\b",
    re.I,
)
# A concessive opening clause concedes what the main clause then denies ('Although there were
# rumors the crew had been captured, ... died in the downing of <type> <tail>'): its doubt words
# are not about the main clause.
WS_CONCESSIVE = re.compile(r"^(?:Although|Though|Even though)\b[^,;]*,\s*", re.I)
# With this aircraft's own full tail named, more verbs count ('the second ship shot down was
# <type> <tail>', 'the helicopter shot down was ... tail #<tail>').
WS_OWN_TAIL_VERB = WS_CRASH_VERB[:-1] + r"|\bdowned\b|\bdowning\b|\bshot down\b)"


def war_story_doubts_tail(inc):
    """Does the war story question which aircraft its narrator was in?"""
    return bool(WS_TAIL_DOUBT.search(inc.get("_war_story") or ""))


def _ws_sentences(inc):
    """The war story's sentences, negated crash words blanked, a stray opening control
    character and a concessive opening clause removed."""
    t = inc.get("_war_story")
    if not t:
        return []
    out = []
    for sent in WS_SENTENCE.split(NEGATED_CRASH.sub(" ", t)):
        sent = re.sub(r"^[\W_]+", "", sent)  # a stray control character opens some stories
        out.append(WS_CONCESSIVE.sub("", sent))
    return out


def _ws_says_own_crash(sent, inc, doubt):
    """Does this war story sentence plainly say THIS aircraft crashed?"""
    tail = inc.get("tail") or ""
    acft = inc.get("aircraft_type") or ""
    checked = sent
    if tail:
        # 'The second ship shot down was <type> <tail>': the ordinal is this aircraft's own
        # place in the order of losses, not a reference to another aircraft.
        checked = re.sub(
            r"\b(?:first|second|third|fourth|last)\s+(?:aircraft|helicopter|ship|LOH"
            r"|huey)\s+(?:downed|shot down|lost)\s+was\s+(?:an?\s+)?(?:[A-Z]{1,3}-\d+"
            r"[A-Z]?\s+)?#?(?=" + re.escape(tail) + r")",
            " ",
            checked,
            flags=re.I,
        )
    if WS_NOT_OURS.search(checked):
        return False
    if any(x != tail for x in TAIL_RE.findall(sent)):
        return False  # another aircraft named by tail number
    if tail and tail in sent and re.search(WS_OWN_TAIL_VERB, sent, re.I):
        return True
    if doubt:
        return False
    if any(re.search(r, sent, re.I) for r in _own_refs(inc)[1:]) and re.search(WS_CRASH_VERB, sent, re.I):
        return True
    types = [re.escape(acft), re.escape(re.sub(r"[A-Z]$", "", acft))] if acft else []
    subject = (
        r"\b(?:the|this|our|my)\s+(?:aircraft|helicopter|ship|LOH|huey|gunship"
        + "".join("|" + x for x in types)
        + r")\b"
    )
    if re.search(subject + r"[^.]{0,60}?\b(?:crashed|(?:was|were) shot down)\b", sent, re.I):
        return True
    if re.search(r"\bwe\s+(?:crashed|were shot down)\b", sent, re.I):
        return True
    return bool(re.match(r"(?:Aircraft|Crashed)\b[^.;]{0,60}?\bcrashed|Crashed\b", sent))


def war_story_crash(inc):
    """The war story sentence that says, in so many words, that this aircraft crashed, or None
    (read for the kind, the country and the places it names; never stored)."""
    doubt = war_story_doubts_tail(inc)
    return next((s for s in _ws_sentences(inc) if _ws_says_own_crash(s, inc, doubt)), None)


# A grid in a war story sentence ('went down at Grid Zone Designator 48P YB775105', 'vic
# BT265125'): an optional zone, a 100 km square (MGRS letters, no I or O), 4, 6 or 8 digits, never
# an archive file number ('TT 1201031016.pdf').
WS_GRID = re.compile(
    r"(?<![A-Za-z0-9])(?:(\d{1,2}[C-X])\s+)?([A-HJ-NP-Z]{2})\s?"
    r"(\d{8}|\d{6}|\d{4})(?![\d.]*\.pdf)(?!\d)"
)
# A lat/long given as '14 deg, 33 min, 10 sec / 107 deg, 33 min, 40 sec' (datum not stated).
WS_LATLON = re.compile(
    r"(\d{1,2})\s*deg,?\s*(\d{1,2})\s*min,?\s*(\d{1,2}(?:\.\d+)?)\s*sec\s*/\s*"
    r"(\d{2,3})\s*deg,?\s*(\d{1,2})\s*min,?\s*(\d{1,2}(?:\.\d+)?)\s*sec",
    re.I,
)


def war_story_places(inc):
    """[{place, zone_hint | lon/lat, names_this_aircraft}] for every grid or lat/long the war
    story gives in a sentence about a crash: one that says this aircraft crashed
    (names_this_aircraft), or one that tells of a crash without naming another aircraft's tail
    or pointing at another aircraft or at doubt ('a UH-1H from A/101 AHB vic BT265125 ...
    crashed'), which may be this loss. Read for the radius; the sentences are never stored."""
    doubt = war_story_doubts_tail(inc)
    tail = inc.get("tail") or ""
    out, seen = [], set()
    for sent in _ws_sentences(inc):
        grids = WS_GRID.findall(sent)
        lls = WS_LATLON.findall(sent)
        if not (grids or lls):
            continue
        own = _ws_says_own_crash(sent, inc, doubt)
        if not own and (
            WS_NOT_OURS.search(sent)
            or any(x != tail for x in TAIL_RE.findall(sent))
            or not (STRONG_CRASH.search(sent) or re.search(r"\bdown(?:ed|ing)\b", sent, re.I))
        ):
            continue
        for zone, sq, digits in grids:
            place = sq + digits
            if place not in seen:
                seen.add(place)
                out.append({"place": place, "zone_hint": zone or None, "names_this_aircraft": own})
        for a, b, c, d, e_, f in lls:
            lat = int(a) + int(b) / 60 + float(c) / 3600
            lon = int(d) + int(e_) / 60 + float(f) / 3600
            place = f"{a} deg {b} min {c} sec / {d} deg {e_} min {f} sec"
            # Mainland Southeast Asia only: anything else is a misread number, not a place.
            if 8 <= lat <= 24 and 100 <= lon <= 112 and place not in seen:
                seen.add(place)
                out.append({"place": place, "lon": lon, "lat": lat, "names_this_aircraft": own})
    return out


def war_story_crash_wording(inc):
    """Crash words in the war story that war_story_crash did not read as this aircraft's crash."""
    t = inc.get("_war_story")
    return bool(t) and war_story_crash(inc) is None and bool(CRASH_TEXT.search(NEGATED_CRASH.sub(" ", t)))


# ---------------------------------------------------------------- kind: decision
WAR_STORY_BASIS = "the war story on the incident page describes a crash of this aircraft"


def _coded_crash(inc):
    """The crash basis from the incident page's coded outcome line(s), or None."""
    fate = inc.get("fate") or ""
    if fate.startswith("The helicopter Crashed"):
        return f'incident page outcome line: "{fate}"'
    if inc.get("ultimate_crash"):
        return f'incident page outcome lines: "{fate or "(no first outcome line)"}" then "Ultimate crash and burn"'
    return None


def _continued(fate):
    """A coded 'Continued Flight' line that does not also say the aircraft was destroyed."""
    return fate.startswith("The helicopter Continued Flight") and "Destroyed" not in fate


def _summary_crash(inc, persons):
    """(label, words) of the first page / pilot page summary that uses crash words, or None."""
    for label, t in _primary_texts(inc, persons):
        w = crash_wording(t, inc.get("tail"))
        if w:
            return label, w
    return None


def _long_crash(inc, persons, strong, airframe_only=False):
    """The basis from a longer account (accident report, REFNO synopsis, pilot page notes, or a
    war story sentence about this aircraft) that describes a crash, or None. airframe_only: only
    the incident page's own accounts (a pilot page's notes are about the man, who may have been
    on another aircraft: K10863's notes tell of Seawolf 320, not of the 175 AHC UH-1D on whose
    rescue he was killed)."""
    for label, t in _narrative_texts(inc, [] if airframe_only else persons):
        if crash_wording(t, inc.get("tail"), strong=strong):
            return _describes(label)
    return WAR_STORY_BASIS if war_story_crash(inc) is not None else None


def crash_evidence(inc, persons):
    """(crash_basis, no_crash_reason): exactly one is set, or neither (the source does not say).

    In order: the coded outcome lines ('Crashed', or 'Ultimate crash and burn' after continued
    flight / a landing); 'was parked' or 'Continued Flight' (not destroyed), which only a longer
    account or a war story sentence about this aircraft outweighs (a summary's crash words can be
    about another aircraft named by call sign: 69042820KIA); crash words in the page summary /
    pilot page summaries (quoted: short fields); text there that says the aircraft did not come
    down (flew back, landed, not damaged); crash words in the longer accounts (accident report,
    REFNO synopsis, pilot page notes, war story sentences about this aircraft: read, never quoted
    - the basis only says which account describes a crash); a pilot page primary cause that is
    itself a crash (mid-air, wire strike, ...); a coded forced or emergency landing line with
    nothing above; text that says only that the man was hit / died later; pilot page reasons that
    are all something other than an aircraft loss."""
    primary = _primary_texts(inc, persons)
    fate = inc.get("fate") or ""
    coded = _coded_crash(inc)
    if coded:
        return coded, None
    if fate.startswith("The helicopter was parked") or _continued(fate):
        long_ = _long_crash(inc, persons, strong=True, airframe_only=True)
        if long_:
            return long_, None
        for label, t in primary:
            if _continued(fate) and STRONG_NO_CRASH.search(t):
                return None, f'{label}: "{t}"'
        return None, f'incident page outcome line: "{fate}"'
    sc = _summary_crash(inc, persons)
    if sc:
        return f'{sc[0]}: "{sc[1]}"', None
    for label, t in primary:
        if STRONG_NO_CRASH.search(t):
            return None, f'{label}: "{t}"'
    # Against a coded forced / emergency landing line only an unambiguous crash word counts.
    long_ = _long_crash(inc, persons, strong=bool(fate))
    if long_:
        return long_, None
    for x in persons:
        cause = x.get("primary_cause") or ""
        if CRASH_CAUSES.search(cause):
            return f"pilot page {x.get('_id')} primary cause: {cause}", None
    if fate:  # a forced or emergency landing, and nothing says it crashed
        return None, f'incident page outcome line: "{fate}"'
    for label, t in primary:
        if WEAK_NO_CRASH.search(t):
            return None, f'{label}: "{t}"'
    reasons = [(x.get("reason") or "").strip() for x in persons]
    if (
        persons
        and not inc.get("crew")
        and all(r and not r.lower().startswith("aircraft lost or crashed") for r in reasons)
    ):
        return None, "pilot page reason: " + "; ".join(sorted(set(reasons)))
    return None, None


def _outcome_conflict(inc, persons, crash_basis, no_crash):
    """The sentence stating that a coded 'Continued Flight' / 'was parked' line and the text
    used (or not used) for the kind disagree, or None."""
    fate = inc.get("fate") or ""
    if not (
        fate.startswith("The helicopter was parked") or fate.startswith("The helicopter Continued Flight")
    ) or _coded_crash(inc):
        return None
    if crash_basis:
        return (
            f'The source disagrees with itself: the incident page outcome line says "{fate}", '
            f"but {crash_basis}; kept as a crash."
        )
    sc = _summary_crash(inc, persons)
    if no_crash and sc:
        return (
            f'The source disagrees with itself: the incident page outcome line says "{fate}", '
            f'but the {sc[0]} uses crash words ("{sc[1]}"), which may be about another '
            "aircraft or a later event; neither the incident page's accident report, REFNO "
            "synopsis nor war story says this aircraft crashed, so it is not mapped as a "
            "crash site - check it."
        )
    return None


def classify(inc, persons):
    """(ground, crash_basis, no_crash, conflict_note). A ground-casualty reading of the short
    fields gives way to a crash the coded outcome lines or a longer account describe (69051329TXT:
    pilot pages 'Killed on ground after hiting land mine', REFNO synopsis: hit, spun and
    crashed); the conflict is then stated. So is a coded 'Continued Flight' / 'was parked' line
    that the kind goes against or that a summary's crash words go against."""
    ground = ground_reason(inc, persons)
    if not ground:
        basis, no = crash_evidence(inc, persons)
        return None, basis, no, _outcome_conflict(inc, persons, basis, no)
    basis = _coded_crash(inc) or _long_crash(inc, persons, strong=True)
    if basis:
        return (
            None,
            basis,
            None,
            (
                f"The source disagrees with itself: {ground} reads as a ground "
                "casualty, but "
                + basis.replace("incident page outcome", "the incident page outcome")
                + "; kept as a crash."
            ),
        )
    return ground, None, None, None


# US designations: H for helicopter, after up to two mission letters (UH-1H, AH-1G, CH-47A, OH-6A).
HELICOPTER_TYPE = re.compile(r"^[A-Z]{0,2}H-\d")


def aircraft_class(acft_type, persons=()):
    """'helicopter', 'fixed-wing aircraft' or 'aircraft' (unknown). The type designation decides
    (VHPA files a few fixed-wing losses, e.g. OV-1C, O-1G, U-6A, under 'Helicopter')."""
    if acft_type:
        return "helicopter" if HELICOPTER_TYPE.match(acft_type) else "fixed-wing aircraft"
    vehicles = {(x.get("vehicle") or "").lower() for x in persons}
    if "fixed wing aircraft" in vehicles:
        return "fixed-wing aircraft"
    if "helicopter" in vehicles:
        return "helicopter"
    return "aircraft"


# ---------------------------------------------------------------- status per man
# Person page "Status:" -> crew code; first match wins, so the more specific phrases come first
# ('Killed in action, body not recovered' is BNR, not KIA).
PERSON_STATUS = (
    (r"body not recovered", "BNR"),
    (r"remains (?:were )?returned", "RR"),
    (r"killed in action", "KIA"),
    (r"\bdied\b", "DOI"),
    (r"missing", "MIA"),
)


def person_code(status):
    """A person page 'Status:' line -> the crew code it amounts to (first PERSON_STATUS match), or ''."""
    s = (status or "").lower()
    return next((code for pat, code in PERSON_STATUS if re.search(pat, s)), "")


NAME_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}  # skipped when taking a surname


def _surname(person_name):
    """'CW2 Given Middle Surname (posthumously promoted)' -> 'surname': the rank and any
    parenthesised note are dropped, then the last word that is not a suffix (Jr, III) is taken."""
    toks = re.sub(r"\(.*?\)", " ", person_name or "").replace(",", " ").split()[1:]
    toks = [re.sub(r"[^a-z]", "", t.lower()) for t in toks]
    toks = [t for t in toks if t and t not in NAME_SUFFIX]
    return toks[-1] if toks else None


def _match(name, persons, used):
    """The not-yet-matched pilot page whose surname (>= 3 letters, to avoid chance hits) occurs in
    a crew / passenger name, or None."""
    compact = re.sub(r"[^a-z]", "", (name or "").lower())
    for x in persons:
        s = _surname(x.get("name"))
        if x["_id"] not in used and s and len(s) >= 3 and s in compact:
            return x
    return None


def losses(inc, persons):
    """Status code per man, from the crew/passenger codes, corrected by the man's own pilot page
    where they disagree (VHPA crew codes are sometimes stale: INJ for a pilot whose own page says
    Killed In Action, BNR for one whose remains were returned). Pilot pages that link this loss
    but match no listed man are added. Returns (rows, notes): rows = (who, group, code)."""
    rows, notes, used = [], [], set()
    by_id = {x["_id"]: x for x in persons}
    for group, men in (("crew", inc.get("crew") or []), ("passengers", inc.get("passengers") or [])):
        for c in men:
            who = f"{c.get('duty', '')} {c.get('rank', '')} {c['name']}".strip() if group == "crew" else c["name"]
            x = by_id.get(c.get("person_page")) or _match(c["name"], persons, used)
            code = c["status"]
            if x:
                used.add(x["_id"])
                pc = person_code(x.get("status"))
                if pc and pc != code:
                    use = (
                        pc == "RR" or (pc in KILLED and code not in KILLED | {"RR"}) or (pc == "BNR" and code in KILLED)
                    )
                    notes.append(
                        f"{group} code {code or '(none)'} for {who}, but pilot page "
                        f"{x['_id']} says '{x['status'].split(' from ')[0]}'"
                        + ("; the pilot page is used" if use else "; the code is kept")
                    )
                    code = pc if use else code
            rows.append((who, group, code))
    for x in persons:
        if x["_id"] not in used:
            rows.append((x.get("name") or x["_id"], "pilot pages", person_code(x.get("status"))))
    return rows, notes


# ---------------------------------------------------------------- scope (inside Vietnam?)
# A loss outside Vietnam is kept only in the Lao/Cambodian border area (Lam Son 719, the 1970
# Cambodian incursion, the A Shau/Khe Sanh border), measured as the distance to the nearest
# place in the GNS Vietnam gazetteer (data/work/gns_index.json, built by gns.py); at sea, only
# off Vietnam's coast. Thailand (e.g. the U-Tapao air base) is never in scope.
VN_BORDER_KM = 40.0
VN_SEA_KM = 150.0
_GNS = None
_GNS_CELL = 0.2


def _gns_cells():
    """{(cell x, cell y): {(lon, lat)}} of GNS Vietnam places on a _GNS_CELL-degree grid (cached)."""
    global _GNS
    if _GNS is None:
        if not GNS_INDEX.exists():
            raise RuntimeError(
                f"{GNS_INDEX} missing (run scripts/ingest/gns.py): cannot tell which "
                "losses are in Vietnam; aborting, nothing written"
            )
        cells = {}
        for feats in json.loads(GNS_INDEX.read_text())["names"].values():
            for f in feats:
                if f.get("fc") in ("P", "A", "T", "L", "S"):  # places, admin, terrain, islands
                    cells.setdefault((math.floor(f["lon"] / _GNS_CELL), math.floor(f["lat"] / _GNS_CELL)), set()).add(
                        (f["lon"], f["lat"])
                    )
        _GNS = cells
    return _GNS


def vn_gazetteer_km(lon, lat):
    """km to the nearest place in the GNS Vietnam gazetteer (0 inside populated Vietnam)."""
    cells = _gns_cells()
    cx, cy = math.floor(lon / _GNS_CELL), math.floor(lat / _GNS_CELL)
    best = float("inf")
    for r in range(0, 200):
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1) if abs(i - cx) == r else (cy - r, cy + r):
                for x, y in cells.get((i, j), ()):
                    best = min(best, km(lon, lat, x, y))
        if best <= r * _GNS_CELL * 100:  # every cell within this distance has been searched
            break
    return best


# Vietnam's national boundary (OSM relation 49915, fetched by wikidata_osm.py): tells whether a
# position is inside Vietnam, and how far it is to Vietnam / to the Lao or Cambodian border when
# the source names a country the position is not in. The relation's outer ring runs offshore at
# sea (territorial sea), so the land border with Laos / Cambodia is taken as the boundary
# segments within LAND_SEG_KM of a place in the GNS Vietnam gazetteer (the sea boundary lies
# >= 12 nm off any land) inside each country's stretch of the border.
VN_BOUNDARY = common.RAW / "osm" / "vn_boundary.json"
LAND_SEG_KM = 10.0
BORDER_STRETCH = {  # (lon, lat) of a segment midpoint -> that stretch of the land border
    "Laos": lambda x, y: 14.6 <= y <= 22.45 and x <= 107.75 and (y <= 21.0 or x <= 103.3),
    "Cambodia": lambda x, y: 10.3 <= y <= 14.75 and x <= 107.65,
}
VIETNAM_LABELS = ("South Vietnam", "North Vietnam", "Vietnam unspecified")
REACH_MARGIN_M = 1000.0  # a circle that must reach a country goes 1 km past its border
_VNB = None
_LAND_SEGS = {}


def _vn_boundary():
    """(segments, bins) of Vietnam's OSM boundary, cached: every ring segment as (x1, y1, x2, y2)
    in lon/lat, and the same segments binned by 0.1-degree latitude band for the point-in-polygon
    test. Raises RuntimeError if the boundary file is missing or malformed."""
    global _VNB
    if _VNB is None:
        if not VN_BOUNDARY.exists():
            raise RuntimeError(
                f"{VN_BOUNDARY} missing (run scripts/ingest/wikidata_osm.py): cannot "
                "tell which positions are inside Vietnam; aborting, nothing written"
            )
        el = json.loads(VN_BOUNDARY.read_text())["elements"]
        if len(el) != 1 or el[0].get("type") != "relation":
            raise RuntimeError(f"{VN_BOUNDARY}: expected one boundary relation; aborting")
        segs = [
            (a["lon"], a["lat"], b["lon"], b["lat"])
            for m in el[0]["members"]
            if m["type"] == "way" and m.get("role") in ("outer", "inner")
            for a, b in itertools.pairwise(m.get("geometry") or [])
            if a and b
        ]
        if not segs:
            raise RuntimeError(f"{VN_BOUNDARY}: no boundary geometry; aborting")
        bins = {}
        for sg in segs:
            for k in range(math.floor(min(sg[1], sg[3]) * 10), math.floor(max(sg[1], sg[3]) * 10) + 1):
                bins.setdefault(k, []).append(sg)
        _VNB = (segs, bins)
    return _VNB


def in_vietnam(lon, lat):
    """Inside Vietnam's national boundary (even-odd rule over every ring segment)."""
    inside = False
    for x1, y1, x2, y2 in _vn_boundary()[1].get(math.floor(lat * 10), ()):
        if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _segs_km(lon, lat, segs):
    """km from a position to the nearest of `segs`, on a local equirectangular projection
    (km per degree at this latitude; accurate to well under 1% at border distances)."""
    kx = 111.320 * math.cos(math.radians(lat))
    best = math.inf
    for x1, y1, x2, y2 in segs:
        ax, ay, bx, by = (x1 - lon) * kx, (y1 - lat) * 110.574, (x2 - lon) * kx, (y2 - lat) * 110.574
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def vietnam_border_km(lon, lat):
    """km from a position to Vietnam's boundary (land or sea)."""
    return _segs_km(lon, lat, _vn_boundary()[0])


def country_border_km(lon, lat, country):
    """km from a position (in Vietnam) to the land border with Laos or Cambodia."""
    if country not in _LAND_SEGS:
        stretch = BORDER_STRETCH[country]
        _LAND_SEGS[country] = [
            sg
            for sg in _vn_boundary()[0]
            if stretch((sg[0] + sg[2]) / 2, (sg[1] + sg[3]) / 2)
            and vn_gazetteer_km((sg[0] + sg[2]) / 2, (sg[1] + sg[3]) / 2) <= LAND_SEG_KM
        ]
    return _segs_km(lon, lat, _LAND_SEGS[country])


COUNTRY_IN_TEXT = re.compile(r"\b(?:in|over|into|inside)\s+(Laos|Cambodia|North Vietnam|Thailand)\b")


def _by(sources, verb="put"):
    """['incident page'] -> 'the incident page puts'; two or more -> '... and ... put'."""
    names = [f"the {x}" for x in sources]
    joined = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{joined} {verb}{'s' if len(names) == 1 else ''}"


def named_countries(inc, persons):
    """{country: [where the source names it]}: the incident page's coded country line, each pilot
    page's Country line, and a country the page / pilot page summaries put the loss in ('...
    extraction mission in Laos.')."""
    out = {}
    if inc.get("country"):
        out.setdefault(inc["country"], []).append("incident page")
    for x in persons:
        if x.get("country"):
            out.setdefault(x["country"], []).append(f"pilot page {x.get('_id')}")
    for label, t in _primary_texts(inc, persons) + [("war story on the incident page", war_story_crash(inc) or "")]:
        for c in COUNTRY_IN_TEXT.findall(t):
            out.setdefault(c, []).append(label)
    return out


def scope(lon, lat, country, named_provinces):
    """(out_of_scope_reason or None, km to the nearest Vietnamese place)."""
    d = vn_gazetteer_km(lon, lat)
    if country == "Thailand":
        return "out_of_scope_thailand", d
    vietnam_named = bool(named_provinces) or country in VIETNAM_LABELS
    limit = VN_SEA_KM if country == "Over Water" else VN_BORDER_KM
    if not vietnam_named and d > limit:
        return "out_of_scope_outside_vietnam", d
    return None, d


# ---------------------------------------------------------------- record assembly
def _not_stated_sentence(inc, persons):
    """What was read for a loss whose kind no text decides, naming only what the pages have."""
    read = []
    if inc.get("summary"):
        read.append("the page summary")
    if any(x.get("short_summary") for x in persons):
        read.append("the pilot page summaries")
    read += [f"the {label}" for label, _ in inc.get("_narratives") or []]
    if any(x.get("_narratives") for x in persons):
        read.append("the pilot page notes")
    ws = bool(inc.get("_war_story"))
    out = "The incident page has no coded outcome line"
    if read:
        out += (
            f"; {', '.join(read[:-1]) + ' and ' + read[-1] if len(read) > 1 else read[0]} "
            f"{'do' if len(read) > 1 else 'does'} not say whether it crashed"
        )
    else:
        out += ", and nothing else on the pages says whether it crashed"
    if ws:
        out += (
            "; the war story on the incident page has no sentence this parser recognises as "
            "saying this aircraft crashed"
        )
    out += "."
    if any((x.get("reason") or "").lower().startswith("aircraft lost or crashed") for x in persons):
        out += " The pilot page reason 'aircraft lost or crashed' is a casualty category, not a description."
    return out


def _ws_places_note(ws_places, disagree, unc):
    """The description sentence naming the places a war story gives for a crash."""

    def one(w):
        if w.get("undecodable"):
            return f"{w['place']} (undecodable)"
        if "zone" in w:
            return f"{w['place']} ({w['distance_km']:.1f} km away, zone {w['zone']})"
        return f"lat/long {w['place']} (datum not stated, read as WGS84; {w['distance_km']:.1f} km away)"

    groups = []
    own = [one(w) for w in ws_places if w["names_this_aircraft"]]
    other = [one(w) for w in ws_places if not w["names_this_aircraft"]]
    if own:
        groups.append("in a sentence saying this aircraft went down: " + ", ".join(own))
    if other:
        groups.append(
            "in a sentence about a crash that does not name this aircraft's tail (it "
            "may be this loss or another aircraft): " + ", ".join(other)
        )
    return (
        ("FLAG grid/war story disagreement: " if disagree else "")
        + "The war story on the incident page gives a place for a crash, "
        + "; ".join(groups)
        + (
            f". The position is kept; the radius ({unc / 1000:.0f} km) is widened to cover "
            "every place the source gives."
            if disagree
            else "; it agrees with the position within ~1 km, and the radius covers it."
        )
    )


def _decode_position(inc, persons, provinces, country):
    """Where the source puts the loss: (position, None) or (None, "grid_undecodable" | "no_grid").

    The incident page's grid is preferred, else the lowest person page grid, else (seen only on
    691124241ACD) a 'Loss Coordinates' lat/long quoted from a REFNO synopsis. position is a dict:
    loc (the locate() result), original_coord, datum, unc (radius in m), coord_note, grid_from,
    and from_grid (False for the lat/long)."""
    grid, grid_from = inc.get("grid"), ("person page" if inc.get("_person_only") else "incident page")
    person_grids = sorted({x["grid"] for x in persons if x.get("grid")})
    if not grid and person_grids:
        grid, grid_from = person_grids[0], "person page"
    if grid:
        loc = locate(grid, provinces, country, inc.get("grid_zone_hint"))
        if loc is None:
            return None, "grid_undecodable"
        return {
            "loc": loc,
            "original_coord": grid,
            "datum": GRID_DATUM,
            "unc": max(GRID_MIN_UNC_M, loc["square_r"] + REPORT_ERROR_M),
            "coord_note": f"Grid {grid} ({grid_from}) decoded as zone {loc['zone']}; {DATUM_NOTE}",
            "grid_from": grid_from,
            "from_grid": True,
        }, None
    if not (inc.get("loss_coordinates") and parse_loss_coordinates(inc["loss_coordinates"])):
        return None, "no_grid"
    # The incident page has no grid line, but the REFNO synopsis it quotes (a POW/MIA case
    # summary) has "Loss Coordinates: 111445N 1060714E (XT223433)".
    lon, lat = parse_loss_coordinates(inc["loss_coordinates"])
    loc = {
        "lon": lon,
        "lat": lat,
        "square_r": 30.0,  # a DMS second is ~30 m
        "zone": None,
        "choice": "lat/lon",
        "candidates": [],
        "check": "none",
        "mismatch_km": None,
    }
    loc.update(province_check(lon, lat, provinces, country, 2.0))
    original_coord = inc["loss_coordinates"]
    grid_from = "REFNO synopsis 'Loss Coordinates' line quoted on the incident page"
    # Datum unknown: WGS84 vs Indian 1960 readings differ by ~0.5 km, and the DMS may have been
    # converted from a grid; 2 km covers both.
    unc = 2000.0
    coord_note = f"Position {original_coord} from the {grid_from}; datum not stated, read as WGS84"
    bracket = re.search(r"\(([A-Z]{2}\d{4,10})\)", original_coord)
    if bracket:
        g = locate(bracket.group(1), provinces, country)
        if g:
            d_m = km(lon, lat, g["lon"], g["lat"]) * 1000
            loc["bracket_grid"] = {
                "grid": bracket.group(1),
                "zone": g["zone"],
                "lon": g["lon"],
                "lat": g["lat"],
                "distance_m": round(d_m),
            }
            unc = max(unc, d_m + g["square_r"] + REPORT_ERROR_M)
            coord_note += (
                f"; the grid it gives in brackets, {bracket.group(1)}, decodes "
                f"(zone {g['zone']}, Indian 1960) {d_m / 1000:.1f} km away"
                + (" - the two agree" if d_m <= 2000 else " - they DISAGREE")
            )
    return {
        "loc": loc,
        "original_coord": original_coord,
        "datum": "unknown (DMS lat/lon from REFNO synopsis, datum not stated; read as wgs84)",
        "unc": unc,
        "coord_note": coord_note,
        "grid_from": grid_from,
        "from_grid": False,
    }, None


def _reach_other_grids(loc, unc, grids, provinces, country, zone_hint):
    """Other grids the person pages give for this loss: decode each and widen the radius to reach
    it (we cannot tell which grid is the typo). Returns (other_grids, unc)."""
    other_grids = []
    for g in grids:
        o = locate(g, provinces, country, zone_hint)
        if o is None:
            other_grids.append({"grid": g, "undecodable": True})
            continue
        d_km = km(loc["lon"], loc["lat"], o["lon"], o["lat"])
        need = d_km * 1000 + o["square_r"] + REPORT_ERROR_M
        other_grids.append(
            {
                "grid": g,
                "zone": o["zone"],
                "lon": o["lon"],
                "lat": o["lat"],
                "distance_km": round(d_km, 3),
                "widened": need > unc,
            }
        )
        unc = max(unc, need)
    return other_grids, unc


def _reach_war_story_places(inc, loc, unc, original_coord, provinces, country):
    """Places the war story gives for a crash (war_story_places): widen the radius to reach each
    (67120414KIA: page grid YB772055, war story '66-16587 went down at ... YB775105', 5 km away).
    Returns (ws_places, unc)."""
    ws_places = []
    for wp in war_story_places(inc):
        if wp["place"] == original_coord:
            continue
        if "lon" in wp:
            d_km = km(loc["lon"], loc["lat"], wp["lon"], wp["lat"])
            need = d_km * 1000 + LATLON_DATUM_M + REPORT_ERROR_M
        else:
            o = locate(wp["place"], provinces, country, wp["zone_hint"] or inc.get("grid_zone_hint"))
            if o is None:
                ws_places.append({**wp, "undecodable": True})
                continue
            wp = {**wp, "zone": o["zone"], "lon": o["lon"], "lat": o["lat"]}
            d_km = km(loc["lon"], loc["lat"], o["lon"], o["lat"])
            need = d_km * 1000 + o["square_r"] + REPORT_ERROR_M
        ws_places.append({**wp, "distance_km": round(d_km, 3), "widened": need > unc})
        unc = max(unc, need)
    return ws_places, unc


def _check_mismatch(loc, named, country, unc):
    """A failed province / country check: keep the position, widen the radius to cover the whole of
    every named province (or the whole country box). Returns (flag or None, flag_note, unc); the
    note quotes the radius and is re-stamped by the caller if the radius grows later."""
    if loc["check"] == "province_mismatch":
        centre = {p: km(loc["lon"], loc["lat"], *province_geometry(p)[:2]) for p in named}
        need_km = max(province_reach_km(loc["lon"], loc["lat"], p) for p in named)
        unc = max(unc, need_km * 1000 + loc["square_r"] + REPORT_ERROR_M)
        bad = loc["mismatched_provinces"]
        loc["mismatch_centre_km"] = {p: round(centre[p], 1) for p in bad}
        which = "; ".join(
            f"grid and province disagree by {centre[p]:.0f} km ({p.title()} province: the "
            f"position is {centre[p]:.0f} km from its centre, ~{loc['province_excess_km'][p]:.0f}"
            " km outside it)"
            for p in bad
        )
        fits = [p.title() for p in named if p not in bad]
        note = (
            f"FLAG grid/province mismatch: {which}"
            + (f"; the grid does fit {'/'.join(fits)} province, also named by the source" if fits else "")
            + ". One of them is wrong. Kept at the grid position; radius "
            f"{unc / 1000:.0f} km covers the grid and the whole of every named province "
            f"({'/'.join(p.title() for p in named)})."
        )
        return "grid/province mismatch", note, unc
    if loc["check"] == "country_mismatch":
        w, s_, e_, n = COUNTRY_BBOX[country]
        far = max(km(loc["lon"], loc["lat"], x, y) for x in (w, e_) for y in (s_, n))
        unc = max(unc, far * 1000 + loc["square_r"] + REPORT_ERROR_M)
        note = (
            f"FLAG grid/country mismatch: the position is outside {country}; one of "
            f"them is wrong. Kept at the position; radius {unc / 1000:.0f} km covers "
            f"both it and the whole of {country}."
        )
        return "grid/country mismatch", note, unc
    return None, None, unc


def _other_grids_note(other_grids, disagree, unc):
    """The description sentence naming the other grids the person pages give."""
    desc = [
        f"{o['grid']} (undecodable)"
        if o.get("undecodable")
        else f"{o['grid']} ({o['distance_km']:.0f} km away, zone {o['zone']})"
        for o in other_grids
    ]
    return (
        ("FLAG grid/grid disagreement: " if disagree else "")
        + f"Person pages give other grid(s): {', '.join(desc)}"
        + (
            "; one of the grids is wrong (often a letter typo), so the radius "
            f"({unc / 1000:.0f} km) is widened to cover every grid the source gives."
            if disagree
            else "; they agree within ~1 km, and the radius covers them."
        )
    )


def _reach_named_countries(loc, countries, country, unc, inside_vn):
    """A country the source names that the position is not in (a Quang Tri grid for a loss put in
    Laos; a 'South Vietnam' grid that lies in Laos): widen the radius to reach it, REACH_MARGIN_M
    past the border. 'Over Water' with a Vietnam label means the waters off Vietnam, so Vietnam's
    land is not reached for. Returns ({country: {named_by, border_km}} widened for, unc)."""
    country_reach = {}
    for c, srcs in sorted(countries.items()):
        if c in ("Laos", "Cambodia") and inside_vn:
            d = country_border_km(loc["lon"], loc["lat"], c)
        elif c in VIETNAM_LABELS and not inside_vn and country != "Over Water":
            d = vietnam_border_km(loc["lon"], loc["lat"])
        else:
            continue
        need = d * 1000 + REACH_MARGIN_M
        if need <= unc:
            continue
        unc = need
        country_reach[c] = {"named_by": srcs, "border_km": round(d, 1)}
    return country_reach, unc


def _country_reach_note(country_reach, unc):
    """The FLAG sentence for the countries _reach_named_countries widened the radius to reach."""
    return (
        "FLAG grid/country disagreement: "
        + "; ".join(
            (
                f"the position is inside Vietnam, but {_by(v['named_by'])} the loss in "
                f"{c}; the nearest point of the {c} border is ~{v['border_km']:.0f} km away"
                if c in ("Laos", "Cambodia")
                else f"the position lies outside Vietnam's boundary, but {_by(v['named_by'], 'say')} "
                f"{c}; Vietnam's boundary is ~{v['border_km']:.0f} km away"
            )
            for c, v in sorted(country_reach.items())
        )
        + (
            f". One of them is wrong. Kept at the grid position; the radius "
            f"({unc / 1000:.0f} km) reaches "
            + " and ".join("Vietnam" if c in VIETNAM_LABELS else c for c in sorted(country_reach))
            + "."
        )
    )


def _over_water_note(loc, countries, country, inside_vn):
    """Explains an 'Over Water' loss also labelled Vietnam whose position is offshore, or None."""
    vn_named = sorted(c for c in countries if c in VIETNAM_LABELS)
    if not (country == "Over Water" and vn_named and not inside_vn):
        return None
    return (
        f"The incident page says Over Water and {_by(countries[vn_named[0]], 'say')} "
        f"{vn_named[0]}: '{vn_named[0]}' is taken to mean the waters off "
        f"{vn_named[0]}, not its land; the position lies ~"
        f"{vietnam_border_km(loc['lon'], loc['lat']):.0f} km outside Vietnam's "
        "boundary, and the radius is not widened to reach it."
    )


def _scope_note(loc, country, named, inside_vn, vn_km):
    """Where the source puts the loss, and whether that is inside Vietnam, or None."""
    if country in ("Laos", "Cambodia"):
        return (
            f"OUTSIDE VIETNAM per the source ({country})"
            + (
                f", though the pilot page(s) name {'/'.join(p.title() for p in named)} province, "
                "South Vietnam - the source disagrees with itself"
                if named
                else ""
            )
            + (
                "; but the position lies inside Vietnam's national boundary"
                if inside_vn
                else f"; the position lies outside Vietnam's national boundary, ~{vn_km:.0f} km from the "
                f"nearest place in the Vietnam gazetteer ({country} border area; kept as a Vietnam "
                "War loss near Vietnam)"
            )
            + "."
        )
    if country == "Over Water":
        return (
            f"At sea per the source; the position is ~{vn_km:.0f} km from the nearest "
            "Vietnamese place (land or island)."
        )
    if not inside_vn:
        return (
            f"The position lies outside Vietnam's national boundary (~"
            f"{vietnam_border_km(loc['lon'], loc['lat']):.0f} km from it; ~{vn_km:.0f} km "
            "from the nearest place in the Vietnam gazetteer): Lao/Cambodian border area "
            "or offshore."
        )
    return None


def _where_note(province, country, countries):
    """'Quang Tri province (pilot page); country per source: ...' for the position sentence. A
    single country named only by coded lines is shown bare; otherwise each one with its sources."""
    coded_only = len(countries) == 1 and all(not s.endswith("summary") for s in next(iter(countries.values())))
    return (
        "; ".join(
            ([f"{province.title()} province (pilot page)"] if province else [])
            + (
                [f"country per source: {country}"]
                if country and coded_only
                else ["country per source: " + "; ".join(f"{c} ({', '.join(v)})" for c, v in countries.items())]
                if countries
                else []
            )
        )
        or "the source names no province or country"
    )


def _loss_of_life(inc, crew, rows, killed, status_notes):
    """(toll for the title, loss-of-life sentence) from the per-man rows, the page's 'Number killed
    in accident' line and its casualty line, in that order of preference."""
    st = Counter(c for _, g, c in rows if c and g != "passengers")
    pax = Counter(c for _, g, c in rows if c and g == "passengers")
    acc_killed = inc.get("accident_killed")
    # Two counts that disagree: the higher is shown in the title (no death is dropped).
    shown = max(killed, acc_killed or 0)
    if st or pax:
        toll = f"{shown} killed" if shown else "no deaths in the status codes listed"
        detail = "; ".join(
            f"{who}: " + ", ".join(f"{n} {s}" for s, n in sorted(c.items()))
            for who, c in (("crew" if crew else "pilot pages", st), ("passengers", pax))
            if c
        )
        loss_line = f"Loss of life: {killed} killed ({detail})."
        if acc_killed is not None and acc_killed != killed:
            loss_line += (
                f" The page's accident line says {acc_killed} killed in the accident; "
                "the source disagrees with itself" + ("; the higher count is shown." if acc_killed > killed else ".")
            )
    elif acc_killed is not None:
        toll = f"{acc_killed} killed" if acc_killed else "no deaths in the accident line"
        loss_line = f"No status codes listed; the page's accident line: {acc_killed} killed."
    elif inc.get("casualties"):
        toll = f"casualties {inc['casualties']}"
        loss_line = f"No crew status codes listed; the page's casualty line: {inc['casualties']}."
    else:
        toll = "casualties not listed"
        loss_line = "No crew status codes or casualty line on the page."
    if status_notes:
        loss_line += " Status codes and pilot pages disagree: " + "; ".join(status_notes) + "."
    return toll, loss_line


def _kind_lead(inc, persons, cls, acft, toll, prefix, bnr_note, ground, no_crash, crash_basis):
    """(title, opening description sentence) for the kind the evidence supports."""
    date = inc.get("date") or "date unknown"
    unit = f", unit {inc['unit']}" if inc.get("unit") else ""
    named_acft = f" ({acft})" if acft != cls else ""
    aircraft_named = f"; aircraft named: {acft}" if acft != cls else ""
    loss_of = f"{cls[0].upper() + cls[1:]} loss {date}: {acft}{unit}."
    if ground:
        title = f"{prefix}ground casualty, {toll}{bnr_note}{named_acft}"
        return title, f"Ground casualty, not an aircraft crash ({ground}), {date}{aircraft_named}{unit}."
    if no_crash:
        title = f"{prefix}loss with no crash recorded, {toll}{bnr_note}{named_acft}"
        return title, f"Not recorded as an aircraft crash ({no_crash}), {date}{aircraft_named}{unit}."
    if crash_basis:
        # A basis read from a longer account names only the account (it is not stored or quoted).
        why = (
            f"Crash: {crash_basis} (the account itself is not stored)."
            if crash_basis.endswith(" a crash") or crash_basis == WAR_STORY_BASIS
            else f"Crash per {crash_basis}."
        )
        return f"{prefix}{acft} crash, {toll}{bnr_note}", f"{loss_of} {why}"
    return f"{prefix}{acft} loss, crash not stated, {toll}{bnr_note}", f"{loss_of} " + _not_stated_sentence(
        inc, persons
    )


def build_record(rec_id, url, inc, persons, *, kind_note="", foreign=None):
    """One loss -> (Evidence or None, skip_reason or None, info dict for the run summary).

    inc: parsed incident (or a person-only stand-in); persons: parsed person pages about this
    loss; foreign: {person id: incident id or loss date} for crew links to pages about another
    loss (not used for location). The radius starts from the position's own error and only ever
    grows, once for each other place the source gives (other grids, war story places, the whole
    of a mismatched province or country, a named country's border); each FLAG note that quotes
    the radius is re-stamped when it grows later, so every note quotes the final radius."""
    foreign = foreign or {}
    provinces = [p for p in (norm_province(x.get("location")) for x in persons) if p]
    province = Counter(provinces).most_common(1)[0][0] if provinces else None
    locations = sorted({x["location"] for x in persons if x.get("location")})
    text_blob = " ".join(locations + [inc.get("summary") or ""])
    if BURIAL_RE.search(text_blob):
        return None, "burial", {}
    # The incident page's country, else the pilot pages' (721028031ACD has none of its own), else
    # one the summaries name (69092727KIA: '... extraction mission in Laos.').
    countries = named_countries(inc, persons)
    country = (
        inc.get("country")
        or next((x["country"] for x in persons if x.get("country")), None)
        or next(iter(countries), None)
    )
    pos, no_position = _decode_position(inc, persons, provinces, country)
    if no_position == "grid_undecodable":
        return None, "grid_undecodable", {"province": province}
    if no_position:
        return None, ("no_grid_province_only" if (province or locations) else "no_grid"), {"province": province}
    loc, original_coord, unc = pos["loc"], pos["original_coord"], pos["unc"]
    if loc.get("checked_province"):
        province = loc["checked_province"]
    named = sorted({p for p in provinces if p in PROVINCES})
    out_of_scope, vn_km = scope(loc["lon"], loc["lat"], country, named)
    if out_of_scope:
        return None, out_of_scope, {"province": province, "country": country, "vn_gazetteer_km": round(vn_km, 1)}

    person_grids = sorted({x["grid"] for x in persons if x.get("grid")})
    other_grids, unc = _reach_other_grids(
        loc,
        unc,
        [x for x in person_grids if x != original_coord] if pos["from_grid"] else [],
        provinces,
        country,
        inc.get("grid_zone_hint"),
    )
    ws_places, unc = _reach_war_story_places(inc, loc, unc, original_coord, provinces, country)

    crew = inc.get("crew") or []
    rows, status_notes = losses(inc, persons)
    pax = [c for _, g, c in rows if c and g == "passengers"]
    killed = sum(1 for _, _, c in rows if c in KILLED or c == "RR")
    # Body not recovered: BNR codes, and MIA on a person-only record (no crew list to say more).
    bnr = [who for who, g, c in rows if c == "BNR" or (c == "MIA" and g == "pilot pages" and not crew)]
    buried = [x["_id"] for x in persons if x.get("burial_recorded")]
    # A man's own status 'Remains were returned' outranks an '(IMO)' marker set before his
    # remains came home (65120310KIA: K11037, IMO, 'Remains were returned on 2016').
    in_memoriam = [x["_id"] for x in persons if x.get("burial_imo") and person_code(x.get("status")) != "RR"]
    imo_returned = [x["_id"] for x in persons if x.get("burial_imo") and person_code(x.get("status")) == "RR"]
    if not persons and killed == 0 and inc.get("accident_killed") == 0:
        # 68011061BAT: an aircraft damaged in an airfield attack; accident line 0 killed, no
        # status codes, no pilot pages: no one's remains are involved.
        return None, "no_loss_of_life", {"province": province}
    ground, crash_basis, no_crash, kind_conflict = classify(inc, persons)

    flags = []
    mismatch_flag, flag_note, unc = _check_mismatch(loc, named, country, unc)
    if mismatch_flag:
        flags.append(mismatch_flag)
    # Grids within ~1 km of each other agree (reporting error); the circle still covers them.
    grids_disagree = any(o.get("distance_km", 0) > GRID_AGREE_KM or o.get("undecodable") for o in other_grids)
    if grids_disagree:
        flags.append("grid/grid disagreement")
    grid_flag_note = None
    if other_grids:
        grid_flag_note = _other_grids_note(other_grids, grids_disagree, unc)
        if flag_note:  # the province note quotes the radius; keep it in step
            flag_note = re.sub(r"radius \d+ km", f"radius {unc / 1000:.0f} km", flag_note)
    ws_disagree = any(w.get("undecodable") or w["distance_km"] > GRID_AGREE_KM for w in ws_places)
    if ws_disagree:
        flags.append("grid/war story disagreement")
    inside_vn = in_vietnam(loc["lon"], loc["lat"])
    country_reach, unc = _reach_named_countries(loc, countries, country, unc, inside_vn)
    country_note = None
    water_note = _over_water_note(loc, countries, country, inside_vn)
    if country_reach:
        flags.append("grid/country disagreement")
        country_note = _country_reach_note(country_reach, unc)
        if flag_note:
            flag_note = re.sub(r"radius \d+ km", f"radius {unc / 1000:.0f} km", flag_note)
        if grid_flag_note:
            grid_flag_note = re.sub(r"radius \(\d+ km\)", f"radius ({unc / 1000:.0f} km)", grid_flag_note)
    if country in ("Laos", "Cambodia") and named:
        flags.append("country/province disagreement")
    scope_note = _scope_note(loc, country, named, inside_vn, vn_km)

    # An incident page with a crew list but no type designation is still a VHPA helicopter page;
    # otherwise the type (or, for person-only records, the Vehicle line) decides.
    cls = aircraft_class(inc.get("aircraft_type"), persons) if inc.get("aircraft_type") or not crew else "helicopter"
    acft = " ".join(x for x in (inc.get("aircraft_type"), inc.get("tail")) if x) or cls
    head = []
    if bnr:
        head.append(f"BNR x{len(bnr)}")
    if inc.get("refno"):
        head.append(f"REFNO {inc['refno']}")
    prefix = ("[" + ", ".join(head) + "] ") if head else ""
    bnr_note = " (body not recovered)" if bnr else ""
    toll, loss_line = _loss_of_life(inc, crew, rows, killed, status_notes)
    title, lead = _kind_lead(inc, persons, cls, acft, toll, prefix, bnr_note, ground, no_crash, crash_basis)
    parts = [lead]
    ws_unread = not crash_basis and war_story_crash_wording(inc)
    if ws_unread and war_story_doubts_tail(inc):
        parts.append(
            "The war story on the incident page uses crash wording, but its narrator "
            "questions which aircraft he was in, so only a sentence naming this "
            "aircraft's tail number would count, and none says it crashed; it is not "
            "used for the kind - check it."
        )
    elif ws_unread:
        parts.append(
            "The war story on the incident page uses crash wording, but not in a "
            "sentence that plainly says this aircraft crashed (war stories often tell of "
            "other aircraft), so it is not used for the kind - check it."
        )
    if kind_conflict:
        parts.append(kind_conflict)
    parts.append(loss_line)
    if bnr:
        parts.append("BODY NOT RECOVERED: " + "; ".join(bnr) + ".")
    if inc.get("refno"):
        rr = sum(1 for _, _, c in rows if c == "RR")
        parts.append(
            f"MIA-POW file reference number {inc['refno']} (a wartime missing/POW case "
            "file; the number alone does not say whether remains are still missing"
            + (f"; status codes show {rr} remains recovered later" if rr else "; see status codes")
            + ")."
        )
    if inc.get("summary"):
        parts.append(f"Summary: {inc['summary']}")
    parts.append(f"{pos['coord_note']}; {_where_note(province, country, countries)}.")
    parts += [note for note in (scope_note, flag_note, grid_flag_note) if note]
    if ws_places:
        parts.append(_ws_places_note(ws_places, ws_disagree, unc))
    parts += [note for note in (country_note, water_note) if note]
    if foreign:
        parts.append(
            "Crew link(s) to pilot page(s) about a different loss, not used for location: "
            + ", ".join(f"{k} ({'a loss on' if v[4:5] == '-' else 'incident'} {v})" for k, v in sorted(foreign.items()))
            + "."
        )
    if buried:
        parts.append(f"{len(buried)} of {len(persons)} linked pilot page(s) give a burial place (not stored).")
    if in_memoriam:
        parts.append(
            f"{len(in_memoriam)} of {len(persons)} linked pilot page(s) give only an "
            "in-memoriam marker '(IMO)': a memorial, not a grave: remains "
            f"NOT recovered for {', '.join(in_memoriam)}."
        )
    if imo_returned:
        parts.append(
            f"{', '.join(imo_returned)}: in-memoriam marker '(IMO)', but the pilot page "
            "status says remains returned (the marker predates the recovery)."
        )
    status_from = (
        "remains status per status codes above"
        if crew or pax
        else "remains status per pilot page status above"
        if persons
        else "the page gives no per-man status, so it does not say whether remains were recovered"
    )
    parts.append(
        (
            "Ground casualty: not an aircraft crash site and not a burial"
            if ground
            else "Crash site, not a burial"
            if crash_basis
            else "Not a known crash site (see the war story) and not a burial"
            if ws_unread
            else "Not a known crash site and not a burial"
        )
        + f"; {status_from}."
    )

    raw = {k: v for k, v in inc.items() if v not in (None, [], "") and not k.startswith("_")}
    raw.update(
        {
            "record": rec_id,
            "province": province,
            "person_locations": locations,
            "person_pages": [x.get("_id") for x in persons],
            "person_grids": person_grids,
            "grid_from": pos["grid_from"],
            "decode": loc,
            "other_grids": other_grids,
            "war_story_places": ws_places,
            "foreign_person_pages": foreign,
            "pilot_pages_with_burial": buried,
            "pilot_pages_in_memoriam": in_memoriam,
            "pilot_pages_in_memoriam_remains_returned": imo_returned,
            "flags": flags,
            "kind_note": kind_note,
            "aircraft_class": cls,
            "ground_casualty": ground,
            "crash_basis": crash_basis,
            "kind_conflict": kind_conflict,
            "countries": countries,
            "country_reach": country_reach,
            "no_crash_recorded": no_crash,
            "status_corrections": status_notes,
            "killed": killed,
            "vn_gazetteer_km": round(vn_km, 1),
        }
    )
    e = store.Evidence(
        source=SOURCE,
        source_record_id=rec_id,
        kind="aircraft_crash" if crash_basis else "loss_incident",
        lon=round(loc["lon"], 6),
        lat=round(loc["lat"], 6),
        uncertainty_m=round(unc, 1),
        original_coord=original_coord,
        original_datum=pos["datum"],
        title=title,
        date_start=inc.get("date"),
        date_end=inc.get("date"),
        description=" ".join(parts),
        url=url,
        raw=raw,
    )
    return e, None, {"bnr": bool(bnr), "check": loc["check"], "province": province, "kind": e.kind}


# ---------------------------------------------------------------- crawl
def _failures():
    """The remembered permanent fetch failures (loaded once from RAW / FAILURES_NAME)."""
    global _FAILURES
    if _FAILURES is None:
        try:
            _FAILURES = json.loads((RAW / FAILURES_NAME).read_text())
        except (FileNotFoundError, ValueError):
            _FAILURES = {}
    return _FAILURES


def _save_failures():
    """Write the remembered failures atomically (temp file, then rename)."""
    RAW.mkdir(parents=True, exist_ok=True)
    tmp = RAW / (FAILURES_NAME + ".part")
    tmp.write_text(json.dumps(_failures(), indent=1, sort_keys=True))
    tmp.replace(RAW / FAILURES_NAME)


def _get(url, name, refresh=False):
    """Cached page text, or None. A permanent HTTP failure (404/410) is remembered in
    RAW/_fetch_failures.json and not requested again unless refresh=True (--retry-failed)."""
    known = _failures().get(name)
    if known and not refresh:
        FETCH_ERRORS[f"http_{known['status']}_cached"] += 1
        return None
    try:
        p = common.cached_get(url, SOURCE, name=name, min_interval=MIN_INTERVAL, refresh=refresh)
    except urllib.error.HTTPError as err:
        print(f"  {err.code} {url}", file=sys.stderr)
        FETCH_ERRORS[f"http_{err.code}"] += 1
        if err.code in PERMANENT_HTTP:
            _failures()[name] = {"url": url, "status": err.code, "at": store.now()}
            _save_failures()
            return None
        raise RuntimeError(f"VHPA fetch failed ({err.code} {url}); aborting, nothing written") from err
    except (urllib.error.URLError, http.client.HTTPException, OSError) as err:
        # Transient (server dropped the connection, timeout): abort rather than write records
        # built from a partial crawl (which would withdraw evidence); a re-run resumes from cache.
        raise RuntimeError(f"VHPA fetch failed ({type(err).__name__} {url}); aborting, nothing written") from err
    if known:  # refreshed and now present
        _failures().pop(name, None)
        _save_failures()
    return p.read_text(encoding="cp1252", errors="replace")


def _read(path):
    """A cached page's text (the site is Windows-1252; undecodable bytes become U+FFFD)."""
    return path.read_text(encoding="cp1252", errors="replace")


def crawl(refresh_index=False, retry_failed=False):
    """Fetch (or read from cache) the index, panels, linked incident and person pages, then add
    every incident/person page already in the cache that nothing links (e.g. K12228), so the
    result depends only on the cache contents. Returns (panel names, linked incident ids,
    {incident id: html}, {K id: html})."""
    if retry_failed:
        _failures().clear()
        _save_failures()
    idx = _get(BASE + "panel/panelind.htm", "panelind.htm", refresh=refresh_index) or ""
    panels = sorted({p.upper() for p in re.findall(r"(PANEL\d\d[EW])\.HTM", idx, re.I)})
    if not panels:
        panels = [f"PANEL{n:02d}{s}" for n in range(1, 71) for s in "EW"]
    incidents, people = {}, {}
    for p in panels:
        raw = _get(f"{BASE}panel/{p}.HTM", f"{p}.HTM")
        if raw:
            i, k = parse_panel(raw)
            incidents.update(dict.fromkeys(i))
            people.update(dict.fromkeys(k))
    inc_pages = {}
    for i in incidents:
        raw = _get(f"{BASE}incident/{i}.HTM", f"incident_{i}.HTM")
        if raw:
            inc_pages[i] = raw
            people.update(dict.fromkeys(k.upper() for k in re.findall(r"\.\./(K\d+)\.HTM", raw, re.I)))
    person_pages = {}
    for k in people:
        raw = _get(f"{BASE}{k}.HTM", f"{k}.HTM")
        if raw:
            person_pages[k] = raw
    # Cached but unlinked pages (a page linked once and since dropped from the panels, or one
    # only reachable from elsewhere): use them too.
    for path in sorted(RAW.glob("incident_*.HTM")):
        i = path.stem[len("incident_") :].upper()
        if i not in inc_pages:
            inc_pages[i] = _read(path)
    for path in sorted(RAW.glob("K*.HTM")):
        if re.fullmatch(r"K\d+", path.stem.upper()) and path.stem.upper() not in person_pages:
            person_pages[path.stem.upper()] = _read(path)
    # Incident pages linked only from a person page (not from any panel), and their crew pages.
    for _ in range(2):
        linked = sorted({parse_person(r)["incident"] for r in person_pages.values()} - {None} - set(inc_pages))
        for i in linked:
            raw = _get(f"{BASE}incident/{i}.HTM", f"incident_{i}.HTM")
            if raw:
                inc_pages[i] = raw
                for k in re.findall(r"\.\./(K\d+)\.HTM", raw, re.I):
                    k = k.upper()
                    if k not in person_pages:
                        r2 = _get(f"{BASE}{k}.HTM", f"{k}.HTM")
                        if r2:
                            person_pages[k] = r2
    return panels, list(incidents), inc_pages, person_pages


# ---------------------------------------------------------------- build all records
# A pilot page dated within a month of the incident is about it (VHPA dates of one loss differ by
# days: incident vs death date, day/month swaps).
SAME_LOSS_DAYS = 31


def _days_apart(a, b):
    """Days between two ISO dates; 0 when either is missing or partial (no evidence of a gap)."""
    try:
        return abs((datetime.date.fromisoformat(a) - datetime.date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return 0


def _first(pages, field):
    """The first non-empty `field` among parsed person pages (in page id order), or None."""
    return next((d[field] for d in pages if d.get(field)), None)


def build_all(inc_pages, person_pages):
    """{incident id: html}, {K id: html} -> (records, skip counts, per-record info).

    One record per incident page, with the person pages its crew links or that link it (pages
    about another loss excluded), then one per group of person pages with no incident page;
    finally the same-day-nearby notes. Pure: reads nothing but the pages and the reference files."""
    persons = {}
    for k, raw in person_pages.items():
        d = parse_person(raw)
        d["_id"] = k
        persons[k] = d
    by_incident = {}
    for d in persons.values():
        if d["incident"]:
            by_incident.setdefault(d["incident"], []).append(d)
    out, skips, infos = [], Counter(), []
    # A stored HTTP error body is not an incident page: its pilots get a person-only record.
    skips["incident_error_page"] = sum(is_error_page(r) for r in inc_pages.values())
    inc_pages = {i: r for i, r in inc_pages.items() if not is_error_page(r)}
    person_pages = {k: r for k, r in person_pages.items() if not is_error_page(r)}
    for i, raw in sorted(inc_pages.items()):
        inc = parse_incident(raw)
        linked = {c["person_page"] for c in inc["crew"] if c["person_page"]}
        # A crew link can point to a page about another loss (e.g. a survivor here who died in a
        # later crash, or a wrong link): that page's province/grid are not about this crash.
        foreign = {
            k: persons[k]["incident"]
            for k in sorted(linked)
            if k in persons and persons[k]["incident"] and persons[k]["incident"] != i
        }
        # ... or a page with no incident link whose loss date is far from this one's (67111455KIA,
        # 11/1967, links K10853 about a loss on 08/29/1971).
        foreign.update(
            {
                k: persons[k]["date"]
                for k in sorted(linked)
                if k in persons and k not in foreign and _days_apart(persons[k]["date"], inc["date"]) > SAME_LOSS_DAYS
            }
        )
        ps = [persons[k] for k in sorted(linked) if k in persons and k not in foreign]
        ps += [d for d in by_incident.get(i, []) if d["_id"] not in linked]
        # Crawl order must not matter (which pilot page is quoted first): sort by page id.
        ps.sort(key=lambda d: d["_id"])
        e, why, info = build_record(i, f"{BASE}incident/{i}.HTM", inc, ps, foreign=foreign)
        if e:
            out.append(e)
            infos.append(info)
        else:
            skips[why] += 1
    # Person pages with no incident page of their own (e.g. many VNAF/USMC losses). Pages about
    # one loss make ONE record: grouped by the incident they link (id = that incident id, so the
    # id does not change if the incident page is fetched later), else by date + tail number.
    groups = {}
    for k, d in sorted(persons.items()):
        if d["incident"] and d["incident"] in inc_pages:
            continue
        if d["incident"]:
            key = d["incident"]
        elif d["date"] and d["tail"]:
            key = ("tail", d["date"], d["tail"])
        else:
            key = k
        groups.setdefault(key, []).append(d)
    for key, ds in groups.items():
        rec_id = key if isinstance(key, str) else ds[0]["_id"]
        stand_in = {
            "aircraft_type": _first(ds, "aircraft_type"),
            "tail": _first(ds, "tail"),
            "date": _first(ds, "date"),
            "unit": _first(ds, "unit"),
            "summary": _first(ds, "short_summary"),
            "crew": [],
            "country": _first(ds, "country"),
            "grid": _first(ds, "grid"),
            "person_status": [d["status"] for d in ds],
            "person_name": [d["name"] for d in ds],
            "service": _first(ds, "service"),
            "_person_only": True,
        }
        if rec_id in inc_pages or (rec_id != ds[0]["_id"] and rec_id in persons):
            rec_id = ds[0]["_id"]
        e, why, info = build_record(
            rec_id,
            f"{BASE}{ds[0]['_id']}.HTM",
            stand_in,
            ds,
            kind_note="person page(s) only (no incident page"
            + (
                f"; they link incident {key}, not in the cache)"
                if isinstance(key, str) and key != ds[0]["_id"]
                else ")"
            ),
        )
        if e:
            out.append(e)
            infos.append(info)
        else:
            skips["person_" + why] += len(ds)
    _note_same_day_nearby(out)
    return out, skips, infos


SAME_DAY_KM = 10.0  # same-day losses this close are named in each other's description


def _note_same_day_nearby(records):
    """Name other VHPA losses of the same day within SAME_DAY_KM (facts from the source; often
    one action, sometimes a sign that one record's province or grid is off)."""
    by_date = {}
    for e in records:
        if e.date_start and len(e.date_start) == 10:
            by_date.setdefault(e.date_start, []).append(e)
    for es in by_date.values():
        for e in es:
            near = sorted(
                ({"id": o.source_record_id, "km": round(km(e.lon, e.lat, o.lon, o.lat), 1)} for o in es if o is not e),
                key=lambda x: (x["km"], x["id"]),
            )
            near = [x for x in near if x["km"] <= SAME_DAY_KM]
            if near:
                e.raw["same_day_nearby"] = near
                e.description += (
                    " Other VHPA loss(es) the same day within "
                    f"{SAME_DAY_KM:.0f} km of this position: "
                    + ", ".join(f"{x['id']} ({x['km']:.1f} km)" for x in near)
                    + "."
                )


# ---------------------------------------------------------------- command line
def main(argv=None):
    """Crawl (or read the cache), build every record, sync them into the store and write
    data/work/vhpa_summary.json. Flags: --refresh-index re-fetches the panel index;
    --retry-failed forgets remembered 404s. Returns the summary dict."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh-index", action="store_true", help="re-fetch the panel index page")
    ap.add_argument("--retry-failed", action="store_true", help="forget remembered 404s and request them again")
    args = ap.parse_args(argv)
    panels, incidents, inc_pages, person_pages = crawl(refresh_index=args.refresh_index, retry_failed=args.retry_failed)
    records, skips, infos = build_all(inc_pages, person_pages)
    db = store.connect()
    store.upsert_source(
        db,
        SOURCE,
        name="Vietnam Helicopter Pilots Association KIA incident pages",
        url="https://www.vhpa.org/KIA/panel/panelind.htm",
        licence="Copyright VHPA 1998-2026; only facts and short coded fields are stored, no narrative text",
        notes="Zoneless 6-digit grids decoded on Indian 1960 (confirmed by datum calibration, "
        "~0.5-1 km; grid positions >= 500 m). Province from pilot person pages; "
        "grid/province and grid/grid disagreements flagged, kept at the grid, radius "
        "covers the grid, every named province and every other grid the source gives.",
    )
    n, deleted, withdrawn = store.sync_source(db, SOURCE, records, reason="no longer produced from the VHPA pages")
    aoi = [e for e in records if common.in_bbox(e.lon, e.lat)]
    checks = Counter(i["check"] for i in infos)
    summary = {
        "panels": len(panels),
        "incident_links": len(incidents),
        "incident_pages": len(inc_pages),
        "person_pages": len(person_pages),
        "fetch_errors": dict(FETCH_ERRORS),
        "known_failures": len(_failures()),
        "ingested": n,
        "deleted": deleted,
        "withdrawn": withdrawn,
        "in_aoi": len(aoi),
        "bnr_records": sum(i["bnr"] for i in infos),
        "checks": dict(checks),
        "kinds": dict(Counter(i["kind"] for i in infos)),
        "skipped": dict(skips),
        "burials_dropped": skips.get("burial", 0) + skips.get("person_burial", 0),
        "aoi_records": [
            {
                "id": e.source_record_id,
                "title": e.title,
                "coord": e.original_coord,
                "lon": e.lon,
                "lat": e.lat,
                "unc_m": e.uncertainty_m,
            }
            for e in aoi
        ],
    }
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "vhpa_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps({k: v for k, v in summary.items() if k != "aoi_records"}, indent=1))
    return summary


if __name__ == "__main__":
    main()
