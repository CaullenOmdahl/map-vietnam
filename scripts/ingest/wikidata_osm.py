"""Wikidata + OpenStreetMap ingester: two evidence sources, "wikidata" and "osm", from one module.

Both sources describe the same kinds of place (battles, massacres, air strikes, aircraft losses, wartime
bases, forts, airfields, war memorials) and share the scope rules below, so they are built together.

Wikidata (CC0): SPARQL queries to the Wikidata Query Service for items in Vietnam (P17 = Q881, or a P625
coordinate inside the Vietnam bbox) whose P31 falls under a battle, siege, military operation, massacre,
airstrike, aviation accident/shootdown, military installation, fort or airport class (subclasses
included). Coordinates are article-level: one point somewhere in the event or site, so radii are wide.

OpenStreetMap (ODbL, "© OpenStreetMap contributors"): Overpass queries over Vietnam's admin area for
military=*, landuse=military, historic=battlefield/fort/ruins/bunker/monument/memorial and
aerodromes/airstrips; plus burial grounds (only used to drop memorials standing beside them), the
outlines of military areas and aerodromes, and Vietnam's national boundary.

Scope rules (each is explained where it is defined):
  - Era: Wikidata events are kept only when dated 1858-1989 (the French conquest through the border
    wars); an undated event whose own text gives no year cannot be placed and is dropped. A site is
    kept unless it was gone before 1858 or built after 1989. Pre-1858 sites are dropped in both sources.
  - Place: items outside Vietnam's OSM boundary are kept only within 50 km of it and only when their own
    text ties them to Vietnam's wars. Nothing is filtered by nationality.
  - Burials: formal burials (cemeteries, graves, tombs, ossuaries, memorials over mass graves, martyrs'
    memorials, and memorials within 300 m of a burial ground) are dropped and counted. They mark where
    the dead were laid to rest, which other records already document, not where an event took place.
  - Present-day military: serving installations are out of scope (not historical, and sensitive to
    map). A Wikidata site needs a historical marker in its own data; an OSM military feature needs
    wartime wording and no sign of current use. Anything inside (or within 25 m of) a present-day
    installation's OSM outline is dropped, as is a disused airfield part inside an airport still in
    use; a Wikidata base within 1 km of one is taken to be reused by it; an event or site whose circle
    takes one in says so in its description.
  - Not war events: criminal cases, protests and equipment on display are dropped.
  - Wikidata P276 (location): a named place is reached for by the radius; a country, region, river or
    sea is only noted; a province is reached for only when the point lies clearly outside it. Places an
    event's own label names are checked against the GNS gazetteer (see named_place_check).

Inputs: the WDQS and Overpass APIs (responses cached under data/raw/wikidata/ and data/raw/osm/, so a
re-run never re-downloads unless --refresh is given) and data/work/gns_index.json (written by gns.py).
Outputs: evidence rows in data/evidence.sqlite, written with store.sync_source so records a source stops
yielding are removed (or withdrawn if they are in a case), and a run summary in
data/work/wikidata_osm_report.json.

Run:
    .venv/bin/python scripts/ingest/wikidata_osm.py [--refresh] [--only wikidata|osm]
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
from collections import Counter
from datetime import date
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ingest: common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts: store
import store  # noqa: E402
from common import AREA_OF_INTEREST, cached_get, in_bbox, progress  # noqa: E402

VN_BBOX = (102.1, 8.4, 109.5, 23.4)  # lon/lat (west, south, east, north), WGS84
WORK = store.ROOT / "data" / "work"
GNS_INDEX = WORK / "gns_index.json"
REPORT = WORK / "wikidata_osm_report.json"

# --------------------------------------------------------------------------------------------
# Scope rules shared by both sources
# --------------------------------------------------------------------------------------------
# Evidence window: the French conquest (1858) through the border wars (1989).
YEAR_MIN, YEAR_MAX = 1858, 1989

# Names that mark a formal burial or a martyrs' memorial (usually inside a cemetery), matched on
# norm()-ed Wikidata labels and OSM names alike.
# Martyrs' memorials (liệt sĩ / Tổ quốc ghi công) stand almost always in or beside a martyrs'
# cemetery, "nhà mồ" is a tomb house/ossuary (e.g. Ba Chúc) and "nghĩa trủng" a burial mound.
# "mộ" as a word means grave/tomb, but "Mộ Đức" is a district in Quảng Ngãi, so it is spared.
# "mả" is a grave/burial mound; "hài cốt" remains (a place keeping them is an ossuary).
BURIAL_NAME_RE = re.compile(
    r"nghĩa trang|nghia trang|nghĩa địa|nghia dia|cemetery|graveyard|"
    r"cimetière|cimetiere|\btomb\b|lăng mộ|đài tưởng niệm liệt sĩ|"
    r"dai tuong niem liet si|nhà mồ|nghĩa trủng|(?<!\w)mả(?!\w)|hài cốt|"
    r"mộ tập thể|ossuary|mass grave|(?<!\w)mộ(?!\w)(?!\s+đức)"
)

# Pre-modern sites (gone before the 1858 window): older civilisations, dynasties, archaeology, the Bạch
# Đằng battles (938/1288). PREMODERN_CIVILIZATIONS are OSM historic:civilization values; PREMODERN_RE is
# matched on the norm()-ed text of both sources, so a Wikidata "Thành nhà Hồ" falls as an OSM one does.
PREMODERN_CIVILIZATIONS = {"champa", "cha", "medieval", "ancient", "prehistoric", "khmer", "funan", "oc_eo"}
PREMODERN_RE = re.compile(
    r"(?<!\w)nhà (?:mạc|lý|trần|hồ|lê|đinh|ngô|tiền lê|hậu lê)(?!\w)(?! chí| chủ)|"
    r"bạch đằng|khảo cổ|archaeolog"
)


# --------------------------------------------------------------------------------------------
# Geometry and text helpers
# --------------------------------------------------------------------------------------------
def norm(s):
    """Lowercase NFC text for matching. U+00D0 (Icelandic eth), often typed for Vietnamese Đ, is read as Đ."""
    return unicodedata.normalize("NFC", s or "").replace("\u00d0", "\u0110").lower()


def distance_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in metres between two WGS84 lon/lat points (sphere of the mean Earth radius)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(a))


AREA_EDGE_M = 25  # a point this close to a present-day installation's outline counts as part of it


class Region:
    """An area outlined by OSM ways (`out geom`): a closed way, or a multipolygon relation's member ways.
    Point-in-area is the even-odd rule over every ring segment, so rings need not be assembled and inner
    rings (holes) are honoured."""

    def __init__(self, key, segments, tags=None):
        self.key, self.tags, self.segs = key, tags or {}, segments
        lons = [x for a, b, c, d in segments for x in (a, c)] or [0]
        lats = [y for a, b, c, d in segments for y in (b, d)] or [0]
        self.bbox = (min(lons), min(lats), max(lons), max(lats))
        self.bins = {}  # 0.1-degree latitude bands -> segments crossing them
        for sg in segments:
            for k in range(math.floor(min(sg[1], sg[3]) * 10), math.floor(max(sg[1], sg[3]) * 10) + 1):
                self.bins.setdefault(k, []).append(sg)

    @classmethod
    def from_element(cls, el):
        """Region of an Overpass `out geom` way or relation (its outer/inner member ways), or None when the
        element carries no geometry."""
        lines = (
            [el.get("geometry") or []]
            if el["type"] == "way"
            else [
                m.get("geometry") or []
                for m in el.get("members", [])
                if m["type"] == "way" and m.get("role") in ("outer", "inner", "")
            ]
        )
        segs = [(a["lon"], a["lat"], b["lon"], b["lat"]) for line in lines for a, b in pairwise(line) if a and b]
        return cls(f"{el['type']}/{el['id']}", segs, el.get("tags")) if segs else None

    def contains(self, lon, lat):
        """True when the point lies inside the outline (even-odd rule; a point on an edge may go either way)."""
        w, s, e, n = self.bbox
        if not (w <= lon <= e and s <= lat <= n):
            return False
        inside = False
        for x1, y1, x2, y2 in self.bins.get(math.floor(lat * 10), ()):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    def near(self, lon, lat, tol_m=AREA_EDGE_M):
        """Inside, or within tol_m of the outline (a memorial at a base's gate, OSM mapping precision)."""
        w, s, e, n = self.bbox
        pad = tol_m / 100_000  # metres -> degrees, generously (1 degree >= ~100 km here)
        if not (w - pad <= lon <= e + pad and s - pad <= lat <= n + pad):
            return False
        return self.contains(lon, lat) or self.distance_m(lon, lat) <= tol_m

    def distance_m(self, lon, lat):
        """Metres from a point to the nearest outline segment (local equirectangular projection)."""
        kx = 111_320 * math.cos(math.radians(lat))  # metres per degree of longitude; 110_574 per degree of latitude
        best = math.inf
        for x1, y1, x2, y2 in self.segs:
            ax, ay, bx, by = (x1 - lon) * kx, (y1 - lat) * 110_574, (x2 - lon) * kx, (y2 - lat) * 110_574
            dx, dy = bx - ax, by - ay
            t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
        return best


# --------------------------------------------------------------------------------------------
# Wikidata
# --------------------------------------------------------------------------------------------
WDQS = "https://query.wikidata.org/sparql"

# ---- Wikidata: classes, kinds and base radii -----------------------------------------------------
# class group -> (kind, base uncertainty m, {QID: label}). Order = priority when an item
# matches several groups (a massacre that is also a battle is described as a massacre).
# A battle spreads over several km of positions; a massacre over a cluster of hamlets. A massacre is
# people killed at a place, not combat: loss_incident.
WD_GROUPS = {
    "massacre": ("loss_incident", 3000, {"Q3199915": "massacre"}),
    "aviation": ("aircraft_crash", 2000, {"Q744913": "aviation accident", "Q6539177": "aircraft shootdown"}),
    "airstrike": ("air_strike", 3000, {"Q2380335": "airstrike"}),
    "battle": ("ground_combat", 10_000, {"Q178561": "battle", "Q188055": "siege", "Q645883": "military operation"}),
    "base": (
        "military_site",
        3000,
        {
            "Q18691599": "military installation",
            "Q245016": "military base",
            "Q695850": "airbase",
            "Q1785071": "fort",
            "Q57821": "fortification",
            "Q91830": "fire support base",
            "Q1198413": "military camp",
            "Q1321241": "outpost",
            "Q1070290": "prisoner-of-war camp",
            "Q91122": "bunker",
            "Q1324633": "naval base",
        },
    ),
    "airport": ("military_site", 5000, {"Q1248784": "airport"}),
}
# cemetery, war cemetery, memorial, grave, tomb (and subclasses): burials, never stored (see the module docstring)
WD_BURIAL = {
    "Q39614": "cemetery",
    "Q1707610": "war cemetery",
    "Q5003624": "memorial",
    "Q173387": "grave",
    "Q381885": "tomb",
}
WD_DATE_PROPS = ("P580", "P582", "P585", "P571", "P576")  # start, end, point in time, inception, dissolved

# Operations, campaigns and offensives (and battles lasting a month or more, e.g. Khe Sanh) span
# tens of km; the article coordinate is one point somewhere in that area.
OPERATION_RADIUS = 25_000
LONG_BATTLE_DAYS = 30
OPERATION_RE = re.compile(r"\boperation\b|campaign|offensive|military occupation", re.I)

# Groups that are events (dated, checked against named places); the others are sites (built / closed dates).
EVENT_GROUPS = {"massacre", "aviation", "airstrike", "battle"}

# ---- Wikidata: P276 locations --------------------------------------------------------------------
# Every P276 (location) place in mainland Southeast Asia and its seas is covered by the radius, however
# far: the source says the event happened there. Places outside it (Khe Sanh -> Gapyeong County, Korea)
# are bad values and only reported. Beyond P276_COVER_M the description flags the distance.
P276_COVER_M = 50_000
P276_REGION_BBOX = (97.0, 5.0, 117.0, 24.0)
# P276 values that are whole countries (their coordinate is the capital): never a place to cover.
COUNTRY_QIDS = {"Q881", "Q180573", "Q172640", "Q819", "Q424", "Q148", "Q869", "Q30", "Q142"}
# P276 values that are areas, by their own P31 labels. A region (Tonkin, the Central Highlands) has only a
# representative point, which says nothing about where in it the event was: like a country, it is noted and
# never reached for. A province is reached for only when the item coordinate lies clearly outside it: farther
# from its point than 1.5 x the radius of a disc of its area (P2046), or PROVINCE_DEFAULT_M without one
# (Operation El Paso: point 91 km from Bình Long province, where it was fought). The DMZ, a strip ~10 km
# wide, stays a place to reach (Con Thien lay just south of it).
PROVINCE_TYPE_RE = re.compile(r"\bprovinces?\b", re.I)
REGION_TYPE_RE = re.compile(r"\bregions?\b|historical country|administrative territorial entity|theater command", re.I)
# A river or a sea area (Ia Drang River, Gulf of Tonkin) has one representative point, which marks
# neither the feature (a long line, a sea) nor where along or in it the event was: noted, never reached for.
# Whole P31 labels only ("river island" is a place).
FEATURE_TYPE_RE = re.compile(
    r"river|stream|creek|canal|watercourse|gulf|bay|sea|marginal sea|strait|"
    r"mountain range",
    re.I,
)
SETTLEMENT_TYPE_RE = re.compile(r"city|town|commune|district|municipality|quarter|village|\bward\b", re.I)
PROVINCE_SHAPE = 1.5  # a province's reach = this x the radius of a disc of its area
PROVINCE_DEFAULT_M = 50_000  # reach of a province with no stated area

# Kind by what the source says happened: an aerial attack is an air strike, a naval engagement is not
# a land combat site (both were otherwise filed under "battle" -> ground_combat).
AERIAL_RE = re.compile(r"\baerial\b|air raid|air ?strike|bombing raid", re.I)
NAVAL_RE = re.compile(r"naval battle|naval warfare|naval confrontation|naval engagement|sea battle", re.I)
# A naval engagement Wikidata dates over more than one day (Gulf of Tonkin incident, 2-4 August 1964) has
# one article coordinate and no point for the other days. Wikidata gives no extent, so 25 km is a project
# default, said to be one; encounters on other days may lie beyond it.
NAVAL_RUNNING_RADIUS = 25_000

# ---- Wikidata: places named in an event's labels (GNS) -------------------------------------------
# Places named in an event's own label ("Bù Đốp massacre", "Battle of A Shau", "Trận A Sầu") are looked up
# in the NGA GNS gazetteer index (data/work/gns_index.json, written by gns.py). Whatever the source names
# must lie inside the radius: a namesake within NAMED_PLACE_REACH_M is reached for. Farther than that, a
# namesake that is a district or province (or its seat) means the coordinate contradicts the place the
# source names (Bù Đốp massacre: Point(104.9 10.5) is ~250 km from Bù Đốp district), and the item is not
# stored; a farther hamlet or commune namesake is taken as a homonym (GNS repeats such names) and only counted.
NAMED_PLACE_REACH_M = 50_000
NAMED_PLACE_RES = (
    re.compile(
        r"^(?:first |second |the )?(?:battle|siege|capture|fall|bombardment|skirmish|attack)s? "
        r"(?:of|on|at) (?:the )?(.+)$",
        re.I,
    ),
    re.compile(r"^(?:the )?(.+?) massacre$", re.I),
    re.compile(r"^(?:massacre|bataille|siège|prise) (?:de |du |d'|à )(.+)$", re.I),
    re.compile(r"^(?:thảm sát|trận|cuộc vây hãm|vụ thảm sát) (.+)$", re.I),
)
# GNS designations whose name is a district or province (or its seat): not a repeated hamlet name
GNS_DISTINCT_DSG = {"ADM1", "ADM2", "PPLA", "PPLA2"}
# hydrographic features (streams, canals): GNS gives one representative point of a long line, as Wikidata does
GNS_LINE_FC = {"H"}
GNS_DSG_NAMES = {
    "PPL": "populated place",
    "PPLA": "provincial seat",
    "PPLA2": "district seat",
    "ADM1": "province",
    "ADM2": "district",
    "ADM3": "commune",
    "ISL": "island",
    "MT": "mountain",
    "HLL": "hill",
    "PEN": "peninsula",
    "PASS": "pass",
    "LCTY": "locality",
}
# ---- Wikidata: scope rules -----------------------------------------------------------------------
# Not war events at all: criminal cases (a "massacre" class match on a murder case) and items listed here.
CRIME_RE = re.compile(r"criminal case|(?<!war )\bcrimes?\b|murder case|homicide|vụ án", re.I)  # not "war crime"
WD_NOT_WAR = {"Q5951632": "Huế chemical attacks: tear gas used on Buddhist protesters (1963), not combat"}
# Sites gone before the 1858 window that nothing in their Wikidata rows dates (no P576, no telling label).
WD_PRE1858_SITE = {
    "Q5122391": "Gia Định (Bát Quái) citadel of 1790: razed in 1835 after Lê Văn Khôi's revolt; the citadel the "
    "French took in 1859 is its 1836 successor, Q10825547",
    "Q10825533": "Thành Bát Quái: the same 1790 Gia Định citadel, razed in 1835",
    "Q10841882": "Đồ Bàn: Vijaya, the Champa capital (11th-15th c.), later the Tây Sơn citadel, abandoned by 1802",
}
# P31 labels that place a site before the window (Đại La, Phú Xuân: "ancient city").
WD_PREMODERN_TYPE_RE = re.compile(r"ancient city|archaeological site", re.I)

# Scope: Vietnam. An event or site outside Vietnam is kept only near the border (Laos/Cambodia) and only
# when its own text ties it to the Indochina wars: fighting near the border often spilled across it, and an
# article point is too coarse to say on which side.
BORDER_AREA_M = 50_000
WAR_TIE_RE = re.compile(
    r"vietnam war|indochina war|north vietnam|south vietnam|vietnamese|viet ?minh|"
    r"viet ?cong|\bpavn\b|\bnva\b|\barvn\b|ho chi minh trail|lam son 719|việt",
    re.I,
)
COUNTRY_NAMES = {"Q819": "Laos", "Q424": "Cambodia", "Q869": "Thailand", "Q148": "China"}
VN_STATE_NAMES = {
    "Q881": "Vietnam",
    "Q180573": "South Vietnam",
    "Q172640": "North Vietnam",
    "Q185682": "French Indochina",
}
# A US/RVN handover year recorded as P576 on an air or naval base says the Americans left, not that the
# base closed (Phu Cat Air Base, P576 = 1975, is an active air force base).
HANDOVER_YEARS = range(1973, 1977)

# Items whose victims are known to lie in a formal ossuary / mass grave (a burial: out of scope).
WD_KNOWN_BURIAL = {"Q796398": "Ba Chúc massacre: victims' remains are kept in the Ba Chúc ossuary (nhà mồ)"}
# Items a class match mislabels as combat: QID -> (kind, description note).
WD_KIND_OVERRIDE = {
    "Q2025973": (
        "other",
        "Evacuation airlift of children, not a battle; its C-5 crash near Tan Son Nhut "
        "(4 April 1975) is the separate item Q1993878.",
    ),
}
# ---- Wikidata: military sites --------------------------------------------------------------------
# Military sites (base / airport groups). Present-day installations are outside the historical scope and
# sensitive to map, so a site is kept only when the source itself marks it as historical; an active marker
# always wins, and a site with neither marker is dropped (status unknown).
SITE_ACTIVE_RE = re.compile(
    r"people'?s (?:army|air force|navy)|people'?s liberation army|\bpla\b|royal thai|"
    r"royal cambodian|coast guard|education organi[sz]ation|universit|academy|"
    r"international airport|quân đội nhân dân|học viện",
    re.I,
)
SITE_HISTORIC_RE = re.compile(
    r"\bformer\b|vietnam war|indochina war|\bu\.s\.|\bus\b|united states|american|"
    r"australian|french|korean|\bvnch\b|\barvn\b|republic of vietnam|south vietnam|"
    r"colonial|citadel|fortress|\bfort\b|world heritage|"
    r"base camp|combat base|firebase|fire support base|landing zone|\blz\b|"
    r"^camp |\bcamp$|\bhill \d+|stockade|\bjail\b|prison",
    re.I,
)
# A ground base (not an air or naval base: those are routinely reused) founded during the wars, e.g.
# Cửa Việt Base (1966): US/ARVN ground bases were all given up by 1975.
GROUND_BASE_RE = re.compile(r"\bbase$", re.I)
REUSED_BASE_RE = re.compile(r"air ?base|airfield|airport|naval|sân bay", re.I)
SITE_HISTORIC_CLASSES = {"fort", "fortification", "prisoner-of-war camp", "fire support base"}
HISTORIC_STATES = {"Q180573", "Q172640", "Q185682"}  # South Vietnam, North Vietnam, French Indochina
INDOCHINA = {"Q881", "Q180573", "Q172640", "Q819", "Q424"}  # Vietnam, South/North Vietnam, Laos, Cambodia
# An airport (the "airport" group alone) is kept only when its own words, a closure in the war years or an
# opening by 1975 in Indochina say it was military or in wartime use: see airport_is_military.
AIRPORT_MIL_RE = re.compile(
    r"air ?base|airfield|military|army|navy|naval|air force|camp|"
    r"căn cứ|sân bay quân sự|quân sự",
    re.I,
)
# Footprint radius from the article coordinate (somewhere inside the site), by kind of installation.
# Checked in order; the first match wins. Values are generous on purpose: the point is not the centre.
SITE_RADII = (
    (re.compile(r"base area|\bpost$", re.I), 10_000, "base area / logistics post (10+ km across)"),
    (
        re.compile(r"air ?base|airfield|airport|air facility|sân bay|phi trường|aerodrome|airstrip", re.I),
        5000,
        "air base / airfield (runways, dispersal and cantonment several km across)",
    ),
    (re.compile(r"base camp|^camp ", re.I), 5000, "division/brigade base camp (several km across)"),
    (re.compile(r"citadel|^thành ", re.I), 3000, "citadel (walls up to ~2.5 km a side)"),
    (
        re.compile(
            r"fire support base|firebase|\blz\b|landing zone|\bhill \d+|outpost|\bcamp$|^đồn|"
            r"bunker|lighthouse|station|prison|jail|stockade|hospital|\bfort\b|fortress|barracks",
            re.I,
        ),
        1000,
        "compact post, camp, fort or compound",
    ),
)
SITE_DEFAULT = (3000, "military base / installation")

# Aviation items are kept only with a military link (civil airliner accidents are out of scope).
AVIATION_MIL_RE = re.compile(
    r"air force|usaf|\bmilitary\b|\barmy\b|\bnavy\b|naval|marine corps|"
    r"shoot ?down|shot down|\b(?:c|b|f|ac|hc|kc|rf)-\d+|helicopter|"
    r"quân sự|không quân|operation ",
    re.I,
)


# ---- Wikidata: queries and fetching --------------------------------------------------------------
def wd_subclass_query():
    """Every subclass (P279*) of the wanted and burial root classes, as (?sub, ?cls) rows."""
    wanted = {q for _, _, cl in WD_GROUPS.values() for q in cl} | set(WD_BURIAL)
    values = " ".join(f"wd:{q}" for q in sorted(wanted))
    return f"SELECT ?sub ?cls WHERE {{ VALUES ?cls {{ {values} }} ?sub wdt:P279* ?cls }}"


def wd_match_query(types):
    """Items whose P31 is one of `types` (already subclass-expanded), with a coordinate inside
    the Vietnam bbox or with country = Vietnam. Class-first with a plain VALUES join is cheap;
    P279* inside this query, or the box service over all of Vietnam, timed out on WDQS."""
    w, s, e, n = VN_BBOX
    return f"""SELECT DISTINCT ?item ?t ?coord WHERE {{
  VALUES ?t {{ {" ".join(f"wd:{q}" for q in sorted(types))} }}
  ?item wdt:P31 ?t ; wdt:P625 ?coord .
  BIND(geof:longitude(?coord) AS ?lon) BIND(geof:latitude(?coord) AS ?lat)
  FILTER((?lon >= {w} && ?lon <= {e} && ?lat >= {s} && ?lat <= {n}) || EXISTS {{ ?item wdt:P17 wd:Q881 }})
}}"""


def wd_detail_query(qids):
    """One row per item: en/vi/fr labels, English description, P31 types with labels, the non-deprecated
    date statements (property|value|precision), truthy P625 coordinates, P17 countries and sitelinks.
    Dates bind each statement's own value node (psv:), so qualifiers never leak in as dates."""
    values = " ".join(f"wd:{q}" for q in qids)
    dps = " ".join(f"(p:{p} psv:{p})" for p in WD_DATE_PROPS)
    return f"""SELECT ?item
  (SAMPLE(?en) AS ?label_en) (SAMPLE(?vi) AS ?label_vi) (SAMPLE(?fr) AS ?label_fr) (SAMPLE(?den) AS ?desc_en)
  (GROUP_CONCAT(DISTINCT CONCAT(STR(?t), "|", ?tlabel); separator=";") AS ?types)
  (GROUP_CONCAT(DISTINCT CONCAT(STRAFTER(STR(?dp), "/prop/"), "|", STR(?tv), "|", STR(?tp)); separator=";") AS ?dates)
  (GROUP_CONCAT(DISTINCT STR(?c); separator=";") AS ?coords)
  (GROUP_CONCAT(DISTINCT STR(?country); separator=";") AS ?countries)
  (SAMPLE(?enwiki) AS ?enwiki) (SAMPLE(?viwiki) AS ?viwiki) (SAMPLE(?frwiki) AS ?frwiki)
WHERE {{
  VALUES ?item {{ {values} }}
  OPTIONAL {{ ?item rdfs:label ?en FILTER(LANG(?en) = "en") }}
  OPTIONAL {{ ?item rdfs:label ?vi FILTER(LANG(?vi) = "vi") }}
  OPTIONAL {{ ?item rdfs:label ?fr FILTER(LANG(?fr) = "fr") }}
  OPTIONAL {{ ?item schema:description ?den FILTER(LANG(?den) = "en") }}
  OPTIONAL {{ ?item wdt:P31 ?t . OPTIONAL {{ ?t rdfs:label ?tlabel0 FILTER(LANG(?tlabel0) = "en") }}
             BIND(COALESCE(?tlabel0, "") AS ?tlabel) }}
  OPTIONAL {{ VALUES (?dp ?psv) {{ {dps} }} ?item ?dp ?st . ?st wikibase:rank ?rank
             FILTER(?rank != wikibase:DeprecatedRank)
             ?st ?psv ?tvn . ?tvn wikibase:timeValue ?tv ; wikibase:timePrecision ?tp }}
  OPTIONAL {{ ?item wdt:P625 ?c }}
  OPTIONAL {{ ?item wdt:P17 ?country }}
  OPTIONAL {{ ?enwiki schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> }}
  OPTIONAL {{ ?viwiki schema:about ?item ; schema:isPartOf <https://vi.wikipedia.org/> }}
  OPTIONAL {{ ?frwiki schema:about ?item ; schema:isPartOf <https://fr.wikipedia.org/> }}
}} GROUP BY ?item"""


def wd_statement_query(qids):
    """Every P625 statement with its rank (to pick the best-ranked one, not the first string), and
    every non-deprecated P276 (location) value with its own label and coordinate."""
    values = " ".join(f"wd:{q}" for q in qids)
    return f"""SELECT ?item ?st ?c ?rank ?loc ?loc_en ?loc_vi ?lc WHERE {{
  VALUES ?item {{ {values} }}
  {{ ?item p:P625 ?st . ?st ps:P625 ?c ; wikibase:rank ?rank }}
  UNION
  {{ ?item p:P276 ?lst . ?lst ps:P276 ?loc ; wikibase:rank ?lrank FILTER(?lrank != wikibase:DeprecatedRank)
     OPTIONAL {{ ?loc rdfs:label ?loc_en FILTER(LANG(?loc_en) = "en") }}
     OPTIONAL {{ ?loc rdfs:label ?loc_vi FILTER(LANG(?loc_vi) = "vi") }}
     OPTIONAL {{ ?loc wdt:P625 ?lc }} }}
}}"""


def wd_location_query(qids):
    """P31 types (with English labels) and areas in km² (P2046) of the P276 location values, to tell a
    place (a town, a hill, a river) from an area (a province, a region) that has no single point."""
    values = " ".join(f"wd:{q}" for q in qids)
    return f"""SELECT ?loc ?t ?tl ?a WHERE {{
  VALUES ?loc {{ {values} }}
  OPTIONAL {{ ?loc wdt:P31 ?t OPTIONAL {{ ?t rdfs:label ?tl FILTER(LANG(?tl) = "en") }} }}
  OPTIONAL {{ ?loc p:P2046 ?ast . ?ast wikibase:rank ?ar FILTER(?ar != wikibase:DeprecatedRank)
             ?ast psv:P2046 ?av . ?av wikibase:quantityAmount ?a ; wikibase:quantityUnit wd:Q712226 }}
}}"""


def wdqs(query, name, refresh=False):
    """Result rows of one SPARQL query, cached as data/raw/wikidata/<name>. The cache is keyed by name
    only, so a changed query needs a new name (or --refresh) to be fetched again."""
    path = cached_get(
        WDQS,
        "wikidata",
        name=name,
        refresh=refresh,
        min_interval=3.0,
        timeout=90,
        data=urllib.parse.urlencode({"query": query}),
        headers={"Accept": "application/sparql-results+json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    return json.loads(path.read_text(encoding="utf-8"))["results"]["bindings"]


def wd_value(binding, key):
    """The value of one variable of a SPARQL JSON result row, or None when the row leaves it unbound."""
    return binding[key]["value"] if key in binding else None


def qid(uri):
    """'http://www.wikidata.org/entity/Q881' -> 'Q881'."""
    return uri.rsplit("/", 1)[-1]


def primary_group(groups):
    """The WD_GROUPS key that decides an item's kind: the first, in WD_GROUPS order, of those it matched."""
    return next(g for g in WD_GROUPS if g in groups)


def wd_countries(detail):
    """P17 country QIDs of a detail row, in the order WDQS gave them."""
    return [qid(c) for c in filter(None, (wd_value(detail, "countries") or "").split(";"))]


def wd_type_pairs(detail):
    """P31 values of a detail row as [type URI, English label] pairs (the label may be empty)."""
    return [t.split("|", 1) for t in filter(None, (wd_value(detail, "types") or "").split(";"))]


def wd_text(detail):
    """An item's own words: its en/vi/fr labels and English description, space-joined."""
    labels = [wd_value(detail, f"label_{lang}") for lang in ("en", "vi", "fr")]
    return " ".join(filter(None, labels + [wd_value(detail, "desc_en")]))


# ---- Wikidata: parsing rows ----------------------------------------------------------------------
POINT_RE = re.compile(r"^\s*Point\(\s*([-\d.eE+]+)\s+([-\d.eE+]+)\s*\)\s*$")


def parse_point(wkt):
    """'Point(lon lat)' WKT -> (lon, lat) floats, or None for anything else (missing, non-point)."""
    m = POINT_RE.match(wkt or "")
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def decimals(numstr):
    """Digits written after the decimal point of a number string."""
    return len(numstr.split(".", 1)[1]) if "." in numstr else 0


def point_decimals(wkt):
    """Decimals of the coarser of a WKT point's two coordinates."""
    m = POINT_RE.match(wkt)
    return min(decimals(m.group(1)), decimals(m.group(2)))


def coord_uncertainty(wkt, base):
    """Base radius by kind, widened when the coordinate is written coarsely: a point written to d decimals
    is only known to a 10^-d degree square, whose half-diagonal is ~7.9 km for 1 decimal."""
    return max(base, round(10 ** -point_decimals(wkt) * 111_320 * math.sqrt(2) / 2))


def gns_key(name):
    """Accent-stripped lowercase key, as gns_index.json keys: 'Bù Đốp' -> 'bu dop'."""
    t = (name or "").replace("đ", "d").replace("Đ", "D")
    t = "".join(c for c in unicodedata.normalize("NFD", t) if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", t).split())


def named_places(labels):
    """Place names an event's own labels name ('Battle of A Shau' -> 'A Shau'), in label order, unique."""
    out = []
    for lab in labels:
        for rx in NAMED_PLACE_RES:
            m = rx.match((lab or "").strip())
            name = m and re.sub(r"\s*\(.*\)$|,\s*\d+$", "", m.group(1)).strip()  # "Snuol (1971)", "Bạch Đằng, 938"
            if name and gns_key(name) not in {gns_key(x) for x in out}:
                out.append(name)
    return out


def load_gazetteer(path=GNS_INDEX):
    """The GNS name index; a missing index aborts the run (records would be built without the check)."""
    if not path.exists():
        raise SystemExit(f"{path} is missing: run scripts/ingest/gns.py first")
    return json.loads(path.read_text(encoding="utf-8"))["names"]


def named_place_check(lon, lat, unc, names, gazetteer):
    """-> list of (status, name, feature, metres to reach it) per named place found in GNS, where status is
    "covered", "reach" (namesake within NAMED_PLACE_REACH_M, outside the radius), "contradicts" (every
    namesake farther, one a district/province or its seat) or "homonym" (every namesake farther, none a
    district/province). The reach distance is to the edge of the feature's own GNS extent (its unc)."""
    out = []
    for name in names:
        feats = [f for f in (gazetteer or {}).get(gns_key(name), []) if f.get("fc") not in GNS_LINE_FC]
        if not feats:
            continue
        reach = sorted(
            (max(0.0, distance_m(lon, lat, f["lon"], f["lat"]) - f.get("unc", 0)), f["ufi"], f) for f in feats
        )
        d, _, f = reach[0]
        if d <= unc:
            out.append(("covered", name, f, d))
        elif d <= NAMED_PLACE_REACH_M:
            out.append(("reach", name, f, d))
        elif any(x.get("dsg") in GNS_DISTINCT_DSG for x in feats):
            out.append(("contradicts", name, f, d))
        else:
            out.append(("homonym", name, f, d))
    return out


def gns_what(f):
    """Plain-words name of a GNS feature's designation ('ADM2' -> 'district')."""
    return GNS_DSG_NAMES.get(f.get("dsg"), f.get("dsg") or "feature")


RANK_ORDER = {"PreferredRank": 0, "NormalRank": 1}  # lower is better; DeprecatedRank is never used


def parse_wd_statements(rows):
    """Statement-query rows -> {qid: {"coords": [{wkt, rank, st}], "locations": [{qid, label, wkt}]}}."""
    out = {}
    for r in rows:
        x = out.setdefault(qid(wd_value(r, "item")), {"coords": [], "locations": []})
        if wd_value(r, "st") and parse_point(wd_value(r, "c")):
            c = {"wkt": wd_value(r, "c"), "rank": wd_value(r, "rank").rsplit("#", 1)[-1], "st": wd_value(r, "st")}
            if c not in x["coords"]:
                x["coords"].append(c)
        if wd_value(r, "loc"):
            loc = {
                "qid": qid(wd_value(r, "loc")),
                "label": wd_value(r, "loc_en") or wd_value(r, "loc_vi") or qid(wd_value(r, "loc")),
                "wkt": wd_value(r, "lc") if parse_point(wd_value(r, "lc")) else None,
            }
            if loc not in x["locations"]:
                x["locations"].append(loc)
    for x in out.values():
        x["coords"].sort(key=lambda c: (RANK_ORDER.get(c["rank"], 9), c["wkt"], c["st"]))
        x["locations"].sort(key=lambda loc: (loc["qid"], loc["wkt"] or ""))
    return out


def best_coords(detail, extra):
    """-> (best-rank WKTs, chosen first, rank). Uses the statement ranks when fetched; otherwise the
    detail query's wdt:P625 values, which Wikidata already limits to the best rank."""
    stmts = [c for c in (extra or {}).get("coords", []) if c["rank"] in RANK_ORDER]
    if stmts:
        best = min(RANK_ORDER[c["rank"]] for c in stmts)
        wkts = sorted({c["wkt"] for c in stmts if RANK_ORDER[c["rank"]] == best})
        return wkts, next(r for r, v in RANK_ORDER.items() if v == best)
    return sorted({c for c in (wd_value(detail, "coords") or "").split(";") if parse_point(c)}), "truthy"


def text_years(text):
    """Years written in a label/description: '1077', '(1972)', '15th century' -> 1400."""
    years = [int(y) for y in re.findall(r"(?<![\d.])(\d{3,4})(?![\d.])", text or "") if 500 <= int(y) <= 2100]
    years += [(int(c) - 1) * 100 for c in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)[ -]century", text or "", re.I)]
    return sorted(set(years))


def in_window(start, end):
    """True when the dated span overlaps YEAR_MIN..YEAR_MAX."""
    lo = int(start[:4]) if start else None
    hi = int(end[:4]) if end else None
    if lo is None and hi is None:
        return True
    return (lo if lo is not None else hi) <= YEAR_MAX and (hi if hi is not None else lo) >= YEAR_MIN


def span_days(start, end):
    """Days from start to end when both are full YYYY-MM-DD dates, else 0 (a coarser date gives no span)."""
    if not (start and end and len(start) == len(end) == 10):
        return 0
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def wd_date(value, precision):
    """'1974-05-04T00:00:00Z' + precision (9 year, 10 month, 11 day) -> YYYY[-MM[-DD]]; BCE -> None."""
    m = re.match(r"^(\d{4,})-(\d\d)-(\d\d)T", value or "")
    if not m or len(m.group(1)) != 4:
        return None
    p = int(precision)
    if p >= 11:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    if p == 10:
        return f"{m.group(1)}-{m.group(2)}"
    if p == 9:
        return m.group(1)
    return None  # decade/century precision: too vague to state as a date


def parse_wd_dates(s):
    """The detail query's "P580|value|precision;..." string -> {property: sorted dates as wd_date gives them}."""
    out = {}
    for part in filter(None, (s or "").split(";")):
        prop, value, prec = part.split("|")
        d = wd_date(value, prec)
        if d:
            out.setdefault(prop, []).append(d)
    return {k: sorted(v) for k, v in out.items()}


def wd_dates(detail, site=False):
    """-> (dates by property, start, end). For a site the start/end are only its own built (P571/P580) and
    closed (P576/P582) dates: a P585 point in time on a base says nothing about which event it marks."""
    dates = parse_wd_dates(wd_value(detail, "dates"))
    if site:
        start = (dates.get("P571") or dates.get("P580") or [None])[0]
        end = (dates.get("P576") or dates.get("P582") or [None])[-1]
    else:
        start = (dates.get("P580") or dates.get("P585") or dates.get("P571") or [None])[0]
        end = (dates.get("P582") or dates.get("P585") or dates.get("P576") or [None])[-1]
    if start and end and end < start:
        end = None
    return dates, start, end


SITE_DATE_PROPS = (
    ("P571", "inception (P571)"),
    ("P580", "start time (P580)"),
    ("P576", "dissolved (P576)"),
    ("P582", "end time (P582)"),
    ("P585", "point in time (P585)"),
)


def site_date_note(dates):
    """Says which Wikidata date properties a site has, as what they are."""
    parts = [f"{what} {', '.join(dates[p])}" for p, what in SITE_DATE_PROPS if dates.get(p)]
    if not parts:
        return None
    note = "Wikidata dates for the site: " + "; ".join(parts) + ". These are the site's own dates, not an event date"
    if dates.get("P585"):
        note += "; Wikidata does not say what the point in time marks, so it is not used as a date here"
    return note + "."


def parse_wd_locations(rows):
    """Location-query rows -> {qid: {"types": [P31 labels], "area_km2": smallest stated area or None}}."""
    out = {}
    for r in rows:
        x = out.setdefault(qid(wd_value(r, "loc")), {"types": [], "area_km2": None})
        if wd_value(r, "t"):
            lab = wd_value(r, "tl") or qid(wd_value(r, "t"))
            if lab not in x["types"]:
                x["types"].append(lab)
        if wd_value(r, "a"):
            a = float(wd_value(r, "a"))
            if a > 0 and (x["area_km2"] is None or a < x["area_km2"]):  # smallest: the widest reach
                x["area_km2"] = a
    for x in out.values():
        x["types"].sort()
    return out


def location_class(info):
    """-> ("region", None) | ("province", extent m) | ("feature", P31 label) | ("place", None) for a P276
    value, by its P31 labels."""
    labels = (info or {}).get("types", [])
    types = " ; ".join(labels)
    if PROVINCE_TYPE_RE.search(types):
        a = (info or {}).get("area_km2")
        return "province", (PROVINCE_SHAPE * math.sqrt(a / math.pi) * 1000 if a else PROVINCE_DEFAULT_M)
    feature = next((lab for lab in labels if FEATURE_TYPE_RE.fullmatch(lab.strip())), None)
    if feature:
        return "feature", feature
    if REGION_TYPE_RE.search(types) and not SETTLEMENT_TYPE_RE.search(types):
        return "region", None
    return "place", None


# ---- Wikidata: building one record ---------------------------------------------------------------
def wd_kind(item, kind, group, type_labels, desc_en, desc):
    """-> (kind, naval) after the per-item overrides and what the source says happened: a "battle" class
    match that Wikidata describes as aerial is an air strike, a naval one is filed as "other". Notes the
    change in desc."""
    naval = False
    if item in WD_KIND_OVERRIDE:
        kind, note = WD_KIND_OVERRIDE[item]
        desc.append(note)
    if group == "battle" and kind == "ground_combat":
        what = " ".join(type_labels + [desc_en or ""])
        if AERIAL_RE.search(what):
            kind = "air_strike"
        elif NAVAL_RE.search(what):
            kind, naval = "other", True
            desc.append("Wikidata calls it a naval engagement, so it is not filed as ground combat.")
    return kind, naval


def battle_radius(base, type_labels, title, start, end, naval, desc):
    """Base radius of a battle-group event: an operation, campaign or month-long battle spreads over tens
    of km, a multi-day naval engagement gets the running default; else the group's base. Notes why in desc."""
    # own P31 labels and title only: matched_classes holds the subclass roots, and every battle falls under
    # "military operation" there
    if OPERATION_RE.search(" ".join(type_labels + [title])):
        desc.append("Operation/campaign: spread over tens of km; the coordinate is one point in it.")
        return OPERATION_RADIUS
    if span_days(start, end) >= LONG_BATTLE_DAYS:
        desc.append(f"Fought over {span_days(start, end)} days across a wide area.")
        return OPERATION_RADIUS
    if naval and span_days(start, end) >= 1:
        desc.append(
            f"Wikidata dates this naval engagement {start} to {end} and gives one point for it. "
            f"The radius is {NAVAL_RUNNING_RADIUS / 1000:g} km around that point, a project default, not a "
            "figure from Wikidata, which gives no extent; Wikidata gives no point for the other days, and "
            "encounters on them may lie beyond this radius."
        )
        return NAVAL_RUNNING_RADIUS
    return base


def nearest_locations(lon, lat, extra):
    """{P276 QID: (metres from the item point or None, location)}: one entry per P276 place, using its
    coordinate nearest the item (a place may carry several P625 values)."""
    nearest = {}
    for loc in (extra or {}).get("locations", []):
        d = distance_m(lon, lat, *parse_point(loc["wkt"])) if loc["wkt"] else None
        old = nearest.get(loc["qid"])
        if old is None or (d is not None and (old[0] is None or d < old[0])):
            nearest[loc["qid"]] = (d, loc)
    return nearest


def p276_radius(unc, nearest, locations, desc):
    """Widens the radius to every P276 place the source names in mainland Southeast Asia (a country, region,
    river or sea is only noted; a province only when the point lies clearly outside it) and describes each
    in desc. -> the new radius in metres."""
    for d, loc in nearest.values():
        lclass, inside_m = location_class((locations or {}).get(loc["qid"]))
        if d is None:
            desc.append(f"Location (P276): {loc['label']} (no coordinate).")
        elif loc["qid"] in COUNTRY_QIDS:
            desc.append(f"Location (P276): {loc['label']} (country-level).")
        elif lclass == "region":
            desc.append(
                f"Location (P276): {loc['label']}, a region; Wikidata gives it only a representative point "
                f"({d / 1000:.1f} km from the item coordinate), which does not mark where in the region "
                "this happened, so the radius is not sized to it."
            )
        elif lclass == "feature":
            desc.append(
                f"Location (P276): {loc['label']}, a {inside_m}; Wikidata gives it only one representative "
                f"point ({d / 1000:.1f} km from the item coordinate), which marks neither the whole {inside_m} "
                f"nor where along or in it this happened, so the radius is not sized to it."
            )
        elif lclass == "province" and d <= inside_m:
            desc.append(
                f"Location (P276): {loc['label']}, a province-level area; its representative point is "
                f"{d / 1000:.1f} km from the item coordinate, within its extent (~{inside_m / 1000:.0f} km), "
                "so the item coordinate can lie inside it and the radius is not widened to that point."
            )
        elif lclass == "province" and in_bbox(*parse_point(loc["wkt"]), P276_REGION_BBOX):
            # a province is not a disc and its outline is not fetched: whether the item coordinate lies
            # inside it cannot be told from the distance alone, so the radius reaches the point either way
            unc = max(unc, d)
            if (locations or {}).get(loc["qid"], {}).get("area_km2"):
                size = (
                    f"That is farther than a disc of its stated area would reach (~{inside_m / 1000:.0f} km), "
                    "but a province is not a disc, so the item coordinate may still lie inside it"
                )
            else:
                size = (
                    "Wikidata gives no area for it, so its extent is unknown and whether the item coordinate "
                    "lies inside it cannot be told"
                )
            desc.append(
                f"Location (P276): {loc['label']}, a province-level area whose representative point is "
                f"{d / 1000:.1f} km from the item coordinate. {size}; the radius reaches that point."
            )
        elif in_bbox(*parse_point(loc["wkt"]), P276_REGION_BBOX):
            # every place the source names in the region is covered, however far (Operation El Paso:
            # item point 91 km from Bình Long, where it was fought)
            unc = max(unc, d)
            if d > P276_COVER_M:  # say what the place is, never why it is far: the source does not say
                typ = ", ".join((locations or {}).get(loc["qid"], {}).get("types") or [])
                typ = f" (Wikidata type: {typ})" if typ else ""
                desc.append(
                    f"Location (P276): {loc['label']}{typ}, {d / 1000:.1f} km from the item coordinate; "
                    "the radius is widened to reach it."
                )
            else:
                desc.append(f"Location (P276): {loc['label']}, {d / 1000:.1f} km from the item coordinate.")
        else:
            desc.append(
                f"Location (P276) also names {loc['label']}, {d / 1000:.0f} km away, outside "
                "Indochina (a bad value; not covered by the radius)."
            )
    return unc


def label_place_names(labels, nearest, locations):
    """Place names in an item's own labels, less those that are a P276 river or sea area: a name the source
    itself ties to such a feature (P276 "Ia Drang River" for "Battle of Ia Drang") is that feature, not a
    GNS namesake commune or hamlet."""
    features = [
        gns_key(loc["label"])
        for _, loc in nearest.values()
        if location_class((locations or {}).get(loc["qid"]))[0] == "feature"
    ]
    return [
        n
        for n in named_places([labels["en"], labels["vi"], labels["fr"]])
        if not any(re.search(rf"\b{re.escape(gns_key(n))}\b", k) for k in features)
    ]


def named_place_radius(lon, lat, unc, names, gazetteer, desc):
    """-> (new radius m, checked): checks each named place against GNS (named_place_check), widens the radius
    to a namesake within reach and says so in desc. checked holds one entry per GNS feature for raw."""
    checked = []
    for status, name, f, d in named_place_check(lon, lat, unc, names, gazetteer):
        if f["ufi"] in {c["gns_ufi"] for c in checked}:  # "A Shau" / "A Sầu": one GNS feature
            continue
        checked.append(
            {
                "status": status,
                "name": name,
                "gns_name": f["name"],
                "gns_ufi": f["ufi"],
                "gns_dsg": f.get("dsg"),
                "gns_lonlat": [f["lon"], f["lat"]],
                "reach_m": round(d),
            }
        )
        if status == "reach":
            unc = max(unc, d)
            desc.append(
                f"Its label names {name}; the nearest place of that name in the GNS gazetteer is "
                f"{f['name']}, a {gns_what(f)} at ({f['lon']:.4f}, {f['lat']:.4f}), whose gazetteer "
                f"extent (~{f.get('unc', 0) / 1000:.1f} km) comes within {d / 1000:.1f} km of the item "
                "coordinate, so the radius is widened to reach it."
            )
    return unc, checked


def boundary_note(lon, lat, unc, detail, vn, desc):
    """A point inside Vietnam whose radius reaches past the national boundary (Battle of Bang Bo: ~2 km from
    the China border, no country, no P276) may have happened on the other side: says so in desc. -> metres
    from the point to the boundary when the radius crosses it, else None."""
    if vn is None or not vn.contains(lon, lat):
        return None
    boundary_m = vn.distance_m(lon, lat)
    if boundary_m >= unc:
        return None
    named = ", ".join(
        (COUNTRY_NAMES | VN_STATE_NAMES)[c]
        for c in sorted(wd_countries(detail))
        if c in COUNTRY_NAMES or c in VN_STATE_NAMES
    )
    said = f"Wikidata gives its country as {named}" if named else "Wikidata names no country for it"
    desc.append(
        f"The item coordinate is inside Vietnam's mapped boundary but {boundary_m / 1000:.1f} km "
        f"from it, so the {math.ceil(unc) / 1000:.1f} km radius extends beyond that boundary "
        f"(across a land border or offshore); {said}, and this record does not say on which "
        "side of the boundary it took place."
    )
    return boundary_m


def wd_evidence(detail, groups, matched_classes, extra=None, scope_note=None, locations=None, gazetteer=None, vn=None):
    """Build one Evidence from a detail binding, or None when it has no coordinate.

    groups = set of WD_GROUPS keys it matched; matched_classes = the root class labels it fell under;
    extra = parse_wd_statements() entry (P625 ranks, P276 locations) when fetched; scope_note = what
    scope_check() said about an item outside Vietnam; locations = parse_wd_locations() (P276 value types);
    gazetteer = the GNS name index, for the places an event's own labels name (named_place_check);
    vn = Region of Vietnam's boundary, to flag a radius that crosses it.

    The radius starts from the kind's base, is resized for operations, long battles and site footprints,
    then only ever widened: for a coarsely written coordinate, other equal-rank coordinates, P276 places
    and GNS namesakes within reach. Every widening is explained in the description.
    """
    item = qid(wd_value(detail, "item"))
    coords, rank = best_coords(detail, extra)
    if not coords:
        return None
    wkt = coords[0]  # best rank; ties broken by the WKT string, so the choice is stable
    lon, lat = parse_point(wkt)
    group = primary_group(groups)
    kind, base, _ = WD_GROUPS[group]
    dates, start, end = wd_dates(detail, site=kind == "military_site")
    labels = {lang: wd_value(detail, f"label_{lang}") for lang in ("en", "vi", "fr")}
    title = labels["en"] or labels["vi"] or labels["fr"] or item
    types = wd_type_pairs(detail)
    type_labels = [lab or qid(u) for u, lab in types]
    desc = [wd_value(detail, "desc_en") or ""]
    if group == "massacre":
        desc.insert(0, "Massacre.")
    kind, naval = wd_kind(item, kind, group, type_labels, wd_value(detail, "desc_en"), desc)
    if kind == "military_site":
        desc.append(site_date_note(dates))
    if scope_note:
        desc.append(scope_note)
    if group == "battle":
        base = battle_radius(base, type_labels, title, start, end, naval, desc)
    alt = [f"{lang}: {labels[lang]}" for lang in ("vi", "fr") if labels[lang] and labels[lang] != title]
    if alt:
        desc.append("Also: " + "; ".join(alt) + ".")
    desc.append(f"Wikidata type: {', '.join(type_labels)}.")
    desc.append("Article-level coordinate, not a contact or crash point.")
    if kind == "military_site":
        base, what = site_radius(title, type_labels, matched_classes)
        art = "an" if what[0] in "aeiou" else "a"
        desc.append(f"Radius sized for {art} {what} footprint, {base / 1000:g} km.")
    unc = coord_uncertainty(wkt, base)
    if unc > base:
        places = point_decimals(wkt)
        desc.append(
            f"The coordinate is written to {places} decimal{'s' if places != 1 else ''} ({wkt}), so it is only "
            f"known to a ~{unc / 1000:.1f} km square; the radius is widened to {unc / 1000:.1f} km."
        )
    if len(coords) > 1:  # several coordinates of the same rank: cover them all
        far = max(distance_m(lon, lat, *parse_point(c)) + coord_uncertainty(c, 0) for c in coords[1:])
        unc = max(unc, far)
        desc.append(
            f"Item has {len(coords)} coordinates of equal rank, up to {far / 1000:.1f} km apart; radius covers all."
        )
    nearest = nearest_locations(lon, lat, extra)
    unc = p276_radius(unc, nearest, locations, desc)
    checked = []
    if group in EVENT_GROUPS:
        names = label_place_names(labels, nearest, locations)
        unc, checked = named_place_radius(lon, lat, unc, names, gazetteer, desc)
    boundary_m = boundary_note(lon, lat, unc, detail, vn, desc)
    raw = {
        "qid": item,
        "labels": labels,
        "description_en": wd_value(detail, "desc_en"),
        "types": dict(types),
        "matched_groups": sorted(groups),
        "matched_classes": sorted(matched_classes),
        "dates": dates,
        "coords": coords,
        "coord_rank": rank,
        "named_places": checked,
        "p625_statements": (extra or {}).get("coords", []),
        "locations": (extra or {}).get("locations", []),
        "location_types": {q: (locations or {}).get(q) for q in sorted(nearest)},
        "countries": wd_countries(detail),
        "sitelinks": {k: wd_value(detail, k) for k in ("enwiki", "viwiki", "frwiki") if wd_value(detail, k)},
        "datum_note": "Wikidata P625 globe-coordinate (WGS84); precision is article-level",
    }
    if boundary_m is not None:
        raw["boundary_m"] = round(boundary_m)
    return store.Evidence(
        source="wikidata",
        source_record_id=item,
        kind=kind,
        lon=lon,
        lat=lat,
        uncertainty_m=math.ceil(unc),
        original_coord=wkt,
        original_datum="article-coordinate",
        title=title,
        date_start=start,
        date_end=end,
        description=" ".join(d for d in desc if d),
        url=f"https://www.wikidata.org/wiki/{item}",
        raw=raw,
    )


# ---- Wikidata: scope checks ----------------------------------------------------------------------
def airport_is_military(detail, groups):
    """-> what marks an airport as military or wartime (kept), or None (a civil airport: dropped)."""
    if groups - {"airport"}:
        return "military class"
    text = " ".join(filter(None, [wd_text(detail), wd_value(detail, "types")]))
    if AIRPORT_MIL_RE.search(text):
        return "military wording"
    dates = parse_wd_dates(wd_value(detail, "dates"))
    if any(int(x[:4]) <= YEAR_MAX for x in dates.get("P576", [])):  # closed in the war years, not 2012
        return "dissolved"
    countries = set(wd_countries(detail))
    # opened by 1975 in Indochina = was in use during the wars (e.g. Tan Son Nhut)
    if countries & INDOCHINA and any(d[:4] <= "1975" for d in dates.get("P571", [])):
        return "opened by 1975 in Indochina"
    return None


def site_in_window(dates):
    """A site's P571/P580 (built) and P576/P582 (closed) are not event dates: it is in the window
    unless it was gone before 1858 or built after 1989. A citadel built in 1809 and never closed stood
    through the whole window (Quảng Trị citadel, fought over in 1972)."""
    built = [int(x[:4]) for p in ("P571", "P580") for x in dates.get(p, [])]
    gone = [int(x[:4]) for p in ("P576", "P582") for x in dates.get(p, [])]
    return not ((gone and max(gone) < YEAR_MIN) or (built and min(built) > YEAR_MAX))


def wd_skip_reason(q, d, groups, burial_types):
    """-> why an item is out of scope by what it is and when it was (burial, crime, not a war event, before
    1858, outside the window, undated, civil aviation), or None to keep it. Place rules come later
    (scope_check, site_skip_reason), once the coordinate is known."""
    types = wd_type_pairs(d)
    p31 = {qid(t[0]) for t in types}
    names = wd_text(d)
    if q in WD_KNOWN_BURIAL or p31 & burial_types or BURIAL_NAME_RE.search(norm(names)):
        return "burial_cemetery_memorial_grave"
    if CRIME_RE.search(" ".join([names] + [t[1] for t in types if len(t) > 1])):
        return "criminal_case"
    if q in WD_NOT_WAR:
        return "not_a_war_event"
    dates, start, end = wd_dates(d)
    type_labels = " ".join(t[1] for t in types if len(t) > 1)
    is_event = primary_group(groups) in EVENT_GROUPS
    if not is_event and (
        q in WD_PRE1858_SITE or PREMODERN_RE.search(norm(names)) or WD_PREMODERN_TYPE_RE.search(type_labels)
    ):
        return "pre_1858_site"  # the OSM rule (premodern()), plus Wikidata's own "ancient city" type
    if is_event:
        if start or end:
            if not in_window(start, end):
                return "outside_1858_1989"
        else:  # undated event: its own text must date it inside the window, or it cannot be placed
            years = text_years(names)
            if not years:
                return "undated_event"
            if not any(YEAR_MIN <= y <= YEAR_MAX for y in years):
                return "outside_1858_1989"
    elif not site_in_window(dates):
        return "outside_1858_1989"
    if groups == {"aviation"} and not AVIATION_MIL_RE.search(names):
        return "civil_aviation"
    return None


def scope_check(lon, lat, d, vn):
    """-> (skip reason, None) for an item outside Vietnam, (None, note) for one kept in the border area,
    (None, None) inside Vietnam. vn = Region of Vietnam's boundary."""
    if vn.contains(lon, lat):
        return None, None
    dist = vn.distance_m(lon, lat)
    countries = set(wd_countries(d))
    if dist <= BORDER_AREA_M and WAR_TIE_RE.search(wd_text(d)):
        where = ", ".join(COUNTRY_NAMES[c] for c in sorted(countries) if c in COUNTRY_NAMES)
        if where:
            return None, (
                f"Outside Vietnam's mapped boundary ({where}), {dist / 1000:.0f} km from it; kept because "
                "its own text ties it to Vietnam's wars and it lies within 50 km of the border."
            )
        # no foreign country in the record: the point may be across a border or offshore, or simply imprecise
        own = ", ".join(VN_STATE_NAMES[c] for c in sorted(countries) if c in VN_STATE_NAMES)
        said = f"Wikidata gives its country as {own}" if own else "Wikidata names no country for it"
        return None, (
            f"The item coordinate lies {dist / 1000:.0f} km outside Vietnam's mapped boundary (across a "
            f"land border or offshore); {said}, so this record does not say where it took place beyond "
            "that coordinate. Kept because its own text ties it to Vietnam's wars and the point is within "
            "50 km of the boundary."
        )
    return "outside_vietnam", None


def site_radius(title, type_labels, matched_classes):
    """-> (base radius m, what) for a military site: the larger of what its title and its own P31 labels
    say (Đức Phong Camp: "camp" in the title, typed airbase), else by its matched classes."""

    def match(text):  # first SITE_RADII entry whose pattern the text matches
        return next(((radius, what) for rx, radius, what in SITE_RADII if rx.search(text or "")), None)

    found = [m for m in (match(title), match(" ".join(type_labels))) if m]
    if found:
        return max(found, key=lambda m: m[0])
    return match(" ".join(sorted(matched_classes))) or SITE_DEFAULT


def site_historic_markers(d, matched_classes):
    """-> a skip reason (str) for a site that is out of scope whatever else it says, else the list of
    things in the source that mark it as historical (empty: nothing does)."""
    countries = set(wd_countries(d))
    # P17 = United States marks US bases abroad (Camp Eagle); any other country outside the war area is
    # out of scope (Thai, Chinese bases)
    if countries and not countries & (INDOCHINA | HISTORIC_STATES | {"Q30"}):
        return "outside_indochina"
    types = " ".join(t[-1] for t in wd_type_pairs(d))
    labels = [wd_value(d, f"label_{x}") or "" for x in ("en", "vi", "fr")]
    text = " ".join(labels + [wd_value(d, "desc_en") or "", types])
    if SITE_ACTIVE_RE.search(text):
        return "present_day_military"
    dates = parse_wd_dates(wd_value(d, "dates"))
    title = labels[0] or labels[1] or labels[2]
    reused = REUSED_BASE_RE.search(title + " " + types)
    # on an air or naval base a 1973-76 P576 is the US/RVN handover, not a closure
    dissolved = [x for x in dates.get("P576", []) if not (reused and int(x[:4]) in HANDOVER_YEARS)]
    wartime_ground_base = (
        GROUND_BASE_RE.search(title)
        and not REUSED_BASE_RE.search(title)
        and any(1945 <= int(x[:4]) <= 1975 for x in dates.get("P571", []))
    )
    markers = []
    if any(int(x[:4]) <= YEAR_MAX for x in dissolved):
        markers.append("dissolved")
    if countries & HISTORIC_STATES:
        markers.append("country")
    if wartime_ground_base:
        markers.append("wartime ground base")
    if set(matched_classes) & SITE_HISTORIC_CLASSES:
        markers.append("class")
    if any(SITE_HISTORIC_RE.search(t) for t in labels + [wd_value(d, "desc_en") or "", types]):
        markers.append("wording")
    return markers


def site_skip_reason(d, matched_classes):
    """-> why a military site (base/airport group) is out of scope, or None to keep it."""
    markers = site_historic_markers(d, matched_classes)
    if isinstance(markers, str):
        return markers
    return None if markers else "present_day_military"  # nothing in the source says it is no longer in use


def site_country_only_note(d, matched_classes):
    """A site kept only because its P17 is a former state (Do Son Airfield: French Indochina) says nothing
    about whether it is used today; the analyst is told so."""
    if site_historic_markers(d, matched_classes) != ["country"]:
        return None
    countries = set(wd_countries(d))
    states = ", ".join(VN_STATE_NAMES[c] for c in sorted(countries & HISTORIC_STATES))
    return (
        f"Wikidata's only sign that this site is historical is its country, {states}; it does not say "
        "whether the site is in use today."
    )


def is_nationwide_campaign(d, extra):
    """An offensive/campaign whose location is a whole country (e.g. the Tet Offensive) is an umbrella
    over many separate battles; it has no site, and any radius around one point would be fake."""
    types = " ".join(t[-1] for t in wd_type_pairs(d))
    title = wd_value(d, "label_en") or ""
    return bool(OPERATION_RE.search(types + " " + title)) and any(
        loc["qid"] in COUNTRY_QIDS for loc in (extra or {}).get("locations", [])
    )


# ---- Wikidata: building all records --------------------------------------------------------------
def build_wd_records(
    details, groups_of, classes_of, extra, burial_types, vn, active_areas=(), locations=None, gazetteer=None
):
    """Applies every Wikidata scope rule, in order, to the matched items. -> (records, stats), where stats
    counts each skip reason as skipped_<reason>.

    details = {QID: detail row}; groups_of / classes_of = {QID: matched WD_GROUPS keys / root class labels};
    extra = parse_wd_statements(); burial_types = every P31 type under a WD_BURIAL class; vn = Region of
    Vietnam; active_areas = Regions of present-day military installations (OSM); locations =
    parse_wd_locations() for the P276 values; gazetteer = GNS name index (load_gazetteer).
    """
    stats = Counter(skipped_burial_cemetery_memorial_grave=0)  # always reported, even when zero
    records = []
    for q in sorted(groups_of):
        d = details.get(q)
        if not d:
            stats["skipped_no_detail"] += 1
            continue
        why = wd_skip_reason(q, d, groups_of[q], burial_types)
        if why:
            stats[f"skipped_{why}"] += 1
            continue
        if is_nationwide_campaign(d, extra.get(q)):
            stats["skipped_nationwide_campaign"] += 1
            continue
        coords, _ = best_coords(d, extra.get(q))
        if not coords:
            stats["skipped_no_coordinate"] += 1
            continue
        lon, lat = parse_point(coords[0])
        why, note = scope_check(lon, lat, d, vn)
        if why:
            stats[f"skipped_{why}"] += 1
            continue
        if WD_GROUPS[primary_group(groups_of[q])][0] == "military_site":
            why = site_skip_reason(d, classes_of[q])
            if why:
                stats[f"skipped_{why}"] += 1
                continue
            if any(a.near(lon, lat) for a in active_areas):  # e.g. Camp Alpha inside Tân Sơn Nhất
                stats["skipped_inside_present_day_military"] += 1
                continue
            only = site_country_only_note(d, classes_of[q])
            if only:
                note = f"{note} {only}" if note else only
        classes = set(classes_of[q])
        if groups_of[q] == {"airport"}:
            why = airport_is_military(d, groups_of[q])
            if not why:
                stats["skipped_civil_airport"] += 1
                continue
            classes.add(f"airport kept: {why}")
        e = wd_evidence(
            d, groups_of[q], classes, extra.get(q), scope_note=note, locations=locations, gazetteer=gazetteer, vn=vn
        )
        named = Counter(c["status"] for c in e.raw["named_places"])
        if named["contradicts"]:  # the coordinate is not where the source's own label says
            stats["skipped_coordinate_contradicts_named_place"] += 1
            continue
        if named["homonym"]:
            stats["named_place_far_homonym_not_reached"] += named["homonym"]
        why = present_day_overlap(e, active_areas)
        if why:
            stats[f"skipped_{why}"] += 1
            continue
        records.append(e)
    return records, stats


# A base, camp, airfield or barracks (not a citadel, prison or hospital) whose article point lies this close
# to a serving installation is taken to be reused by it (Cat Lo Naval Base, 362 m from the Coast Guard
# Region 3 command): the article point is somewhere inside the old base, so the two overlap.
SITE_REUSE_M = 1000
REUSABLE_SITE_RE = re.compile(
    r"\bbase\b|\bcamp\b|airfield|air ?base|airport|barracks|naval|sân bay|căn cứ|"
    r"doanh trại|\btrại\b",
    re.I,
)
NOT_REUSED_SITE_RE = re.compile(
    r"prison|jail|prisoner|stockade|nhà tù|hospital|citadel|fortress|\bfort\b|"
    r"^thành ",
    re.I,
)


def present_day_overlap(e, active_areas):
    """Relates a Wikidata record to the present-day installations (OSM Regions) near its point. -> a skip
    reason, or None after adding a note to e.description: a site beside a serving installation is dropped,
    as is an event whose whole circle lies inside one; otherwise the analyst is told which installations
    the point or the circle takes in, since those areas are out of scope."""
    near = []  # (metres from the point, 0 when inside, Region) for installations within the radius
    pad = e.uncertainty_m / 100_000 + 0.01
    for a in active_areas:
        w_, s_, e_, n_ = a.bbox
        if not (w_ - pad <= e.lon <= e_ + pad and s_ - pad <= e.lat <= n_ + pad):
            continue
        d = 0.0 if a.contains(e.lon, e.lat) else a.distance_m(e.lon, e.lat)
        if d <= e.uncertainty_m:
            near.append((d, a.key, a))
    if not near:
        return None
    near.sort(key=lambda x: (x[0], x[1]))
    if e.kind == "military_site":
        what = " ".join([e.title] + list(e.raw.get("types", {}).values()))
        if near[0][0] <= SITE_REUSE_M and REUSABLE_SITE_RE.search(what) and not NOT_REUSED_SITE_RE.search(what):
            return "next_to_present_day_military"
    else:
        inside = [(d, k, a) for d, k, a in near if d == 0 or d <= AREA_EDGE_M]
        if any(d == 0 and a.distance_m(e.lon, e.lat) >= e.uncertainty_m for d, _, a in inside):
            return "inside_present_day_military"  # the whole circle lies inside a serving installation
        if inside:
            d, k, a = inside[0]
            e.description += (
                f" The coordinate falls inside present-day military installation {k} "
                f"({area_name(a)}); that installation is out of scope, search only outside it."
            )
        return None
    d, k, a = near[0]
    more = f" and {len(near) - 1} more" if len(near) > 1 else ""
    e.description += (
        f" This radius takes in present-day military installation {k} ({area_name(a)}, "
        f"{d:.0f} m from the point){more}; such installations are out of scope."
    )
    return None


def area_name(region):
    """A present-day installation's name for a description, or what its tags make it when unnamed."""
    tags = region.tags
    if tags.get("name") or tags.get("name:en"):
        return tags.get("name") or tags.get("name:en")
    if tags.get("landuse") == "military":
        return "unnamed landuse=military"
    if tags.get("military"):
        return f"unnamed military={tags['military']}"
    return "unnamed aerodrome"


def ingest_wikidata(refresh=False):
    """Fetch (or read from cache) every Wikidata query and build the records. -> (records, stats).

    Order: subclass closure of the wanted classes -> items per class group -> details and statements per
    item -> types and areas of their P276 locations. Batch sizes (400 types per match query, 200 items per
    detail/statement query) keep each query small enough for the public WDQS endpoint; they are part of the cache file
    names, so changing them re-downloads.
    """
    stats = Counter()
    vn, active_areas = osm_geometry(refresh)
    group_of_class = {q: g for g, (_, _, cl) in WD_GROUPS.items() for q in cl}
    label_of_class = {q: lab for _, _, cl in WD_GROUPS.values() for q, lab in cl.items()} | WD_BURIAL
    # subclass closure: type QID -> set of wanted/burial root classes it falls under
    roots_of_type = {}
    for r in wdqs(wd_subclass_query(), "subclasses.json", refresh):
        roots_of_type.setdefault(qid(wd_value(r, "sub")), set()).add(qid(wd_value(r, "cls")))
    burial_types = {t for t, roots in roots_of_type.items() if roots & set(WD_BURIAL)}
    groups_of, classes_of = {}, {}
    for group, (_, _, classes) in WD_GROUPS.items():
        types = sorted(t for t, roots in roots_of_type.items() if roots & set(classes))
        for i in range(0, len(types), 400):
            rows = wdqs(wd_match_query(types[i : i + 400]), f"match_{group}_{i // 400}.json", refresh)
            stats[f"match_rows_{group}"] += len(rows)
            for r in rows:
                q, t = qid(wd_value(r, "item")), qid(wd_value(r, "t"))
                groups_of.setdefault(q, set()).add(group)
                classes_of.setdefault(q, set()).update(
                    label_of_class[c] for c in roots_of_type[t] if group_of_class.get(c) == group
                )
    stats["matched_items"] = len(groups_of)
    keep = sorted(groups_of)
    details, stmt_rows = {}, []
    for i in range(0, len(keep), 200):
        chunk = keep[i : i + 200]
        for b in wdqs(wd_detail_query(chunk), f"detail_v2_{i // 200:03d}_{len(chunk)}.json", refresh):
            details[qid(wd_value(b, "item"))] = b
        stmt_rows += wdqs(wd_statement_query(chunk), f"statements_{i // 200:03d}_{len(chunk)}.json", refresh)
    extra = parse_wd_statements(stmt_rows)
    loc_qids = sorted({loc["qid"] for x in extra.values() for loc in x["locations"]})
    loc_rows = []
    if loc_qids:  # one query; the cache name follows the set of locations, so a new set is fetched afresh
        tag = hashlib.sha1(" ".join(loc_qids).encode()).hexdigest()[:10]
        loc_rows = wdqs(wd_location_query(loc_qids), f"locations_{len(loc_qids)}_{tag}.json", refresh)
    records, built = build_wd_records(
        details,
        groups_of,
        classes_of,
        extra,
        burial_types,
        vn,
        active_areas,
        parse_wd_locations(loc_rows),
        gazetteer=load_gazetteer(),
    )
    stats.update(built)
    return records, stats


# --------------------------------------------------------------------------------------------
# OpenStreetMap
# --------------------------------------------------------------------------------------------
# ---- OSM: queries and fetching -------------------------------------------------------------------
OVERPASS = "https://overpass-api.de/api/interpreter"
# Public mirrors of the same API, tried in turn when the main instance is overloaded (the cache file
# name does not depend on the mirror, so a later run reads the same bytes).
OVERPASS_MIRRORS = (
    OVERPASS,
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OSM_AREA = 'area["ISO3166-1"="VN"]["admin_level"="2"]->.vn;'
OSM_QUERIES = {
    "military": '(nwr["military"](area.vn); nwr["landuse"="military"](area.vn););',
    "historic": '(nwr["historic"~"^(battlefield|fort|ruins|bunker|monument|memorial)$"](area.vn););',
    "aeroway": '(nwr["aeroway"~"^(aerodrome|airstrip)$"](area.vn); nwr["disused:aeroway"](area.vn);'
    ' nwr["abandoned:aeroway"](area.vn););',
    # ordinary cemeteries and graves: never stored, only used for the "memorial beside a burial" check
    "burial": '(nwr["amenity"="grave_yard"](area.vn); nwr["landuse"="cemetery"](area.vn);'
    ' nwr["historic"~"^(tomb|grave)$"](area.vn); nwr["cemetery"](area.vn););',
}
# Outlines (not just bboxes) of military areas and aerodromes, to test whether a point lies inside a
# present-day installation; and Vietnam's own boundary, to tell events in Vietnam from those abroad.
OSM_GEOM_QUERIES = {
    "areas_geom": '(way["landuse"="military"](area.vn); relation["landuse"="military"](area.vn);'
    ' way["military"](area.vn); relation["military"](area.vn);'
    ' way["aeroway"="aerodrome"](area.vn); relation["aeroway"="aerodrome"](area.vn););',
    "vn_boundary": 'rel(49915)["ISO3166-1"="VN"]["admin_level"="2"];',  # Vietnam's national boundary
}


def overpass_query(body, out="tags bb"):
    """Full Overpass QL for one query body over Vietnam's admin area. `out tags bb` gives tags plus a bbox per
    way/relation (its midpoint is used as the centre); `out geom` gives full outlines."""
    return f"[out:json][timeout:240];{OSM_AREA}{body}out {out};"


def overpass(name, body, refresh=False, attempts=3, out="tags bb"):
    """Elements of one Overpass query (cached). Any failure raises: a run never writes degraded data."""
    for attempt in range(attempts):
        url = OVERPASS_MIRRORS[attempt % len(OVERPASS_MIRRORS)]
        try:
            path = cached_get(
                url,
                "osm",
                name=f"{name}.json",
                refresh=refresh,
                min_interval=10.0,
                timeout=300,
                data=urllib.parse.urlencode({"data": overpass_query(body, out)}),
            )
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as err:
            # busy public instance: back off politely, then retry on the next mirror
            code = getattr(err, "code", None)
            if (code is not None and code not in (429, 502, 503, 504)) or attempt == attempts - 1:
                raise
            time.sleep(30 * (attempt + 1))
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("remark") and "error" in data["remark"].lower():
        raise RuntimeError(f"overpass {name}: {data['remark']}")
    return data["elements"]


# ---- OSM: scope rules ----------------------------------------------------------------------------
# Martyrs' memorials (liệt sĩ, Tổ quốc ghi công) stand almost always in or beside a martyrs' cemetery, so a
# memorial or monument that names them is taken as a burial; "trận vong" / "tử sĩ" (the fallen) name the
# same kind of altar or memorial to the dead.
MARTYR_RE = re.compile(
    r"liệt sĩ|liệt sỹ|liet si|liet sy|tổ quốc ghi công|to quoc ghi cong|martyr|"
    r"trận vong|tran vong|tử sĩ"
)
# OSM memorials built on a mass grave / ossuary of the victims (a burial, though not tagged as one).
OSM_KNOWN_BURIAL = {
    "node/10723988763": "Hà My massacre memorial, built over the victims' mass grave",
    "way/596624577": "Vĩnh Trinh dam massacre memorial, where the victims' remains are interred",
    # memorial complexes whose dead are buried inside them (the graves are not mapped in OSM)
    "node/4409284591": "Tháp Chuông, Đồng Lộc junction: the ten young volunteers are buried in the complex",
    "node/5353612634": "Hang Tám Cô: the eight volunteers killed in the cave are buried at the site",
}
NEAR_BURIAL_M = 300  # a memorial this close to a cemetery/martyrs' memorial is treated as part of it
BURIAL_TAGS = (
    ("amenity", "grave_yard"),
    ("landuse", "cemetery"),
    ("historic", "tomb"),
    ("historic", "grave"),
    ("memorial", "grave"),
    ("memorial", "tomb"),
)

# Words that tie a memorial or ruin to war. Not "bia" (any stele) or "di tích" (any heritage site), and
# "đồn"/"bót" (post) only as whole words ("đồng" is not "đồn").
WAR_RE = re.compile(
    r"chiến thắng|chien thang|chiến công|trận|tran danh|chiến khu|chien khu|căn cứ|can cu|"
    r"thảm sát|tham sat|chiến tranh|kháng chiến|"
    r"khang chien|(?<!\w)đồn(?!\w)|(?<!\w)bót(?!\w)|lô cốt|lo cot|pháo đài|phao dai|tháp canh|"
    r"\b(?:massacre|battle|bunker|fort|war|military|tunnel)\b|quân đội|quân sự|quân giải phóng|"
    r"quân khu|bộ đội|sư đoàn|trung đoàn|tiểu đoàn|đại đội|địa đạo|dia dao|"
    r"xứ ủy|xứ uỷ|tỉnh ủy|tỉnh uỷ|anh hùng|"
    r"giải phóng|giai phong|pháo|máy bay|b-52|b52"
)
# A present-day military feature (military=*/landuse=military without historic/disused tags) is kept
# only when its own text names the wartime occupant or calls it former/old. A year alone is not enough:
# an academy founded in 1955 is still an active installation.
OSM_WARTIME_RE = re.compile(
    r"hoa kỳ|quân mỹ|lính mỹ|của mỹ|pháp|american|french|\bu\.?s\.? |\busaf\b|"
    r"\barvn\b|vnch|cộng hòa|cộng hoà|ngụy|\bformer\b|\bcũ\b|di tích"
)
# Signs a military feature is in use today: someone runs it, it has opening hours or contacts, access
# is closed, it is an institution, or its name is a serving unit / command.
OSM_ACTIVE_KEYS = ("operator", "opening_hours", "phone", "website", "email", "amenity", "office")
OSM_ACTIVE_NAME_RE = re.compile(
    r"^bch|bchqs|lữ đoàn|sư đoàn|trung đoàn|tiểu đoàn|quân đoàn|binh đoàn|hải đoàn|"
    r"bộ tư lệnh|bộ chỉ huy|ban chỉ huy|quân khu|học viện|viện |thư viện|"
    r"bệnh viện|trường|doanh trại|công an|biên phòng|academy|brigade|regiment|"
    r"military committee|command"
)


def osm_active_reason(tags):
    """-> why a military feature looks in use today (the tag or name that says so), or None."""
    for k in tags:
        if k in OSM_ACTIVE_KEYS or k.startswith("contact:"):
            return k
    if tags.get("access") in ("no", "private", "military"):
        return f"access={tags['access']}"
    m = OSM_ACTIVE_NAME_RE.search(norm(tags.get("name")) + " | " + norm(tags.get("name:en")) + " ")
    return f"name:'{m.group(0).strip()}'" if m else None


MASSACRE_RE = re.compile(r"thảm sát|tham sat|massacre")
FORT_TEXT_RE = re.compile(r"\bfort\b|pháo đài|phao dai")
MILITARY_RUINS = {"fort", "fortification", "military", "bunker", "castle", "watchtower"}  # not "tower": Cham towers
# OSM memorials that are not war events, though tagged war_memorial: element -> why.
OSM_NOT_WAR = {
    "node/3812282642": "Thích Quảng Đức memorial: his 1963 self-immolation in protest (as Wikidata Q5951632, tear "
    "gas on protesters, is not a war event)",
    "node/7368395546": "students' enlistment memorial on a Hanoi campus: marks where students left to enlist, "
    "not a war event or loss",
}
# Memorials whose generic note would mislead: element -> what the source itself says it commemorates.
OSM_MEMORIAL_NOTE = {
    "node/13357233094": "Memorial; its Chinese name (中華民國留越國軍病故官兵眷暨義民紀念碑) says it commemorates "
    "officers and soldiers of the Republic of China army who stayed in Vietnam (留越國軍), their "
    "families and loyal civilians (義民), who died of illness (病故): not a combat site. Where they were "
    "buried is not stated in OSM.",
}
# OSM features whose tags give the wrong kind: element -> (kind, description note).
OSM_KIND_OVERRIDE = {
    "node/10291730841": (
        "military_site",
        "Named as an old artillery position (gun emplacement), tagged "
        "historic=battlefield; kept as a military site, not a combat site.",
    ),
}
# military=* values that name a whole installation (not a bunker, trench or other small object)
FOOTPRINT_MILITARY = {"base", "airfield", "barracks", "naval_base", "camp", "training_area", "range"}
# A gun, tank or aircraft put on display ("Pháo 130 K-59-1", "Máy bay 6144") is an exhibit of a type of
# equipment, not the place of an event; a wreck left where it fell is kept.
EXHIBIT_RE = re.compile(
    r"^(?:pháo|xe tăng|xe thiết giáp|máy bay|trực thăng|tàu|tank|aircraft|helicopter|cannon)"
    r"\s+[\w\-./ ]*\d|"
    # a bare equipment name: "Military Bulldozer", "Xe tăng", "Tank"
    r"^(?:military |army )?(?:bulldozer|tank|truck|jeep|aircraft|helicopter|cannon|"
    r"artillery|xe ủi|xe tăng|xe tải|máy bay|trực thăng|pháo)s?$"
)
WRECK_RE = re.compile(r"xác|wreck|bắn rơi|bị bắn|rơi|crash|shot down|chiến thắng|trận")
CITADEL_RE = re.compile(r"(?:^|di tích )thành (?!phố|viên|lập|công|tâm)")  # ruins of a citadel (thành)


# ---- OSM: classifying one element ----------------------------------------------------------------
def premodern(tags):
    """True when an OSM feature's civilisation tag or text places it before the 1858 window."""
    civ = norm(tags.get("historic:civilization"))
    return civ in PREMODERN_CIVILIZATIONS or bool(PREMODERN_RE.search(text_of(tags)))


def footprint_radius(tags, name):
    """-> (radius m, what) when a node stands for a whole base, camp, airfield or fort, else None: sized
    for the footprint as on the Wikidata side (bunkers, ruins and memorials are small objects)."""
    if not (
        tags.get("historic") == "fort"
        or tags.get("military") in FOOTPRINT_MILITARY
        or tags.get("landuse") == "military"
        or any(tags.get(k) for k in ("aeroway", "disused:aeroway", "abandoned:aeroway"))
    ):
        return None
    labels = [
        (tags.get(k) or "").replace("_", " ")
        for k in ("military", "historic", "aeroway", "disused:aeroway", "abandoned:aeroway", "landuse")
    ]
    return site_radius(name or "", labels, ())


def burial_reason(tags):
    """-> what marks an OSM feature as a burial ("landuse=cemetery", "name:'nghĩa trang'", "martyrs'
    memorial:'liệt sĩ'"), or None. Names, descriptions and inscriptions are all read."""
    for k, v in BURIAL_TAGS:
        if tags.get(k) == v:
            return f"{k}={v}"
    if "cemetery" in tags:
        return "cemetery=*"
    names = " | ".join(
        norm(v)
        for k, v in tags.items()
        if k == "name"
        or k.startswith("name:")
        or k in ("official_name", "alt_name", "old_name", "description", "inscription")
    )
    m = BURIAL_NAME_RE.search(names)
    if m:
        return f"name:'{m.group(0)}'"
    if tags.get("historic") in ("memorial", "monument") or tags.get("memorial"):
        m = MARTYR_RE.search(names)
        if m:
            return f"martyrs' memorial:'{m.group(0)}'"
    return None


def text_of(tags):
    """norm()-ed text of every OSM tag that names or describes a feature, space-joined."""
    return " ".join(
        norm(tags.get(k))
        for k in (
            "name",
            "name:vi",
            "name:en",
            "name:fr",
            "old_name",
            "alt_name",
            "description",
            "inscription",
            "memorial",
            "subject",
            "historic:civilization",
        )
    )


def disused_tags(tags):
    """True when a feature carries a disused:/abandoned:/was: lifecycle prefix or disused/abandoned=yes."""
    return (
        any(k.startswith(("disused:", "abandoned:", "was:")) for k in tags)
        or tags.get("disused") == "yes"
        or tags.get("abandoned") == "yes"
    )


def osm_classify(tags):
    """-> (kind, uncertainty floor in metres, reason) or (None, None, skip reason), by the feature's tags.
    The floor is a node's radius before footprint sizing (footprint_radius); a way's comes from its bbox."""
    t = text_of(tags)
    historic = tags.get("historic")
    if historic and premodern(tags):
        return None, None, "pre_1858_site"
    disused = disused_tags(tags)
    if historic == "battlefield":
        return "ground_combat", 1000, "historic=battlefield"
    if historic in ("fort", "bunker") or tags.get("military") == "bunker":
        return "military_site", 25, f"historic={historic}" if historic else "military=bunker"
    if historic == "ruins":
        if tags.get("ruins") in MILITARY_RUINS or tags.get("military") or WAR_RE.search(t) or CITADEL_RE.search(t):
            return "military_site", 25, "historic=ruins with military context"
        return None, None, "ruins_without_military_context"
    if historic in ("monument", "memorial"):
        if "bia chủ quyền" in t:  # island sovereignty markers
            return None, None, "monument_not_war_related"
        if EXHIBIT_RE.search(norm(tags.get("name"))) and not WRECK_RE.search(t):
            return None, None, "equipment_exhibit"
        if tags.get("memorial") in ("war_memorial", "battlefield") or tags.get("military") or WAR_RE.search(t):
            return "other", 25, f"historic={historic} (war-related)"
        return None, None, "monument_not_war_related"
    aero = tags.get("aeroway") or tags.get("disused:aeroway") or tags.get("abandoned:aeroway")
    if aero and not tags.get("military") and tags.get("landuse") != "military":
        if disused:
            # a disused airfield is military only when its tags say so; one with an airline code or a civil
            # operator was a civil airport (Phu Quoc Old Airport, as its Wikidata twin Q203395)
            if aeroway_military_evidence(tags):
                return "military_site", 100, "disused/abandoned aeroway"
            if civil_aerodrome(tags):
                return None, None, "civil_aerodrome"
            return "other", 100, "disused/abandoned aeroway, era not stated"
        if (
            tags.get("aerodrome:type") == "military"
            or tags.get("aerodrome") == "military"
            or re.search(r"quân sự|air ?base|military|căn cứ", t)
            or re.search(r"army|air force|navy|military|quân đội|quân chủng|không quân", norm(tags.get("operator")))
        ):
            return "military_site", 100, "military aerodrome"
        return None, None, "civil_aerodrome"
    if tags.get("military") == "trench" and tags.get("landuse") != "military":
        # a trench line or tunnel entrance is a field work, not an installation (A1 Hill, Củ Chi)
        return "military_site", 100, "military=trench"  # a node may stand for a whole trench line
    if tags.get("military") or tags.get("landuse") == "military":
        return "military_site", 100, "military=*/landuse=military"
    return None, None, "unmatched_tags"


AERO_MIL_TEXT_RE = re.compile(
    r"quân sự|căn cứ|military|air ?base|\bbase\b|\bcamp\b|task force|\blz\b|"
    r"landing zone|army|navy|marine|air force|không quân"
)


def aeroway_military_evidence(tags):
    """Tags or text that say a (disused) airfield was military or wartime."""
    if any(
        k.startswith(("disused:", "abandoned:", "was:"))
        and k.split(":", 1)[1] in ("military", "landuse")
        and tags[k] in ("military", "airfield", "base", "yes")
        for k in tags
    ):
        return True
    t = text_of(tags)
    return bool(
        AERO_MIL_TEXT_RE.search(t)
        or OSM_WARTIME_RE.search(t)
        or re.search(r"army|air force|navy|military|quân", norm(tags.get("operator")))
    )


def civil_aerodrome(tags):
    """An airline code, a civil operator or an airport name in use: a civil airport."""
    return bool(
        tags.get("iata")
        or tags.get("icao")
        or tags.get("operator:type") in ("private", "civil", "public", "commercial")
        or re.search(
            r"airports? (?:corporation|services)|cảng hàng không|international",
            norm(tags.get("operator")) + " " + text_of(tags),
        )
    )


def is_memorial(tags):
    """True for historic=memorial/monument: it marks a commemoration, not necessarily the event's place."""
    return tags.get("historic") in ("memorial", "monument")


def osm_evidence(el):
    """-> (Evidence, reason it was classified so) for one Overpass element, or (None, skip reason).

    Applies the per-element scope rules: burials, not war events, pre-1858, unnamed memorials and
    present-day installations. Rules that need other elements (inside an installation, beside a burial,
    a trench's wartime context) are applied by osm_records.
    """
    tags = el.get("tags") or {}
    etype, eid = el["type"], el["id"]
    key = f"{etype}/{eid}"
    if key in OSM_KNOWN_BURIAL:
        return None, "burial: known mass grave/ossuary"
    if key in OSM_NOT_WAR:
        return None, "not_a_war_event"
    reason = burial_reason(tags)
    if reason:
        return None, "burial: " + reason.split(":")[0]
    kind, floor, why = osm_classify(tags)
    if not kind:
        return None, why
    named = any(
        k == "name"
        or k.startswith("name:")
        or k in ("inscription", "description", "old_name", "official_name", "alt_name")
        for k in tags
    )
    if is_memorial(tags) and not named:  # nothing says what it commemorates; often a martyrs' plot
        return None, "unnamed_memorial"
    name = tags.get("name") or tags.get("name:vi") or tags.get("name:en")
    desc = []
    if key in OSM_KIND_OVERRIDE:
        kind, note = OSM_KIND_OVERRIDE[key]
        desc.append(note)
    if etype == "node":
        lat, lon = el["lat"], el["lon"]
        original = f"node lat={el['lat']} lon={el['lon']}"
        unc = floor
        fp = footprint_radius(tags, name)
        if fp and fp[0] > unc:
            unc = fp[0]
            desc.append(f"One point for a whole {fp[1]}; radius sized for that footprint, {unc / 1000:g} km.")
    else:
        b = el.get("bounds")
        if not b:
            return None, "no_geometry"
        lat, lon = (b["minlat"] + b["maxlat"]) / 2, (b["minlon"] + b["maxlon"]) / 2
        original = f"{etype} bbox minlat={b['minlat']} minlon={b['minlon']} maxlat={b['maxlat']} maxlon={b['maxlon']}"
        dy = (b["maxlat"] - b["minlat"]) * 111_320  # degrees -> metres
        dx = (b["maxlon"] - b["minlon"]) * 111_320 * math.cos(math.radians(lat))
        # half the bbox diagonal covers the whole feature from its bbox centre; a battlefield keeps its 1 km
        # floor (the fighting spread beyond what is mapped), any other mapped outline is the feature itself
        unc = max(25, math.hypot(dx, dy) / 2, floor if kind == "ground_combat" else 25)
    label = (
        tags.get("historic")
        or tags.get("military")
        or tags.get("landuse")
        or tags.get("aeroway")
        or tags.get("disused:aeroway")
        or tags.get("abandoned:aeroway")
        or "feature"
    )
    title = name or f"{label.replace('_', ' ')} ({etype}/{eid})"
    if tags.get("name:en") and tags.get("name:en") != title:
        desc.append(f"en: {tags['name:en']}.")
    if tags.get("description"):
        desc.append(tags["description"].rstrip(".") + ".")
    if MASSACRE_RE.search(text_of(tags)):
        desc.append("Massacre site memorial.")
    if key in OSM_MEMORIAL_NOTE:
        desc.append(OSM_MEMORIAL_NOTE[key])
    elif kind == "other" and why == "disused/abandoned aeroway, era not stated":
        desc.append("Disused or abandoned airfield part; its tags do not say who built or used it, or when.")
    elif kind == "other":
        desc.append("Memorial/monument; marks a commemorated event, not necessarily the exact site.")
    fort_site = not tags.get("military") and not osm_active_reason(tags) and FORT_TEXT_RE.search(text_of(tags))
    present_day = (
        kind == "military_site"
        and why in ("military=*/landuse=military", "military aerodrome")
        and not tags.get("historic")
        and not fort_site
        and (osm_active_reason(tags) or not OSM_WARTIME_RE.search(text_of(tags)))
    )
    # a bunker or trench (no historic / disused tag) that someone runs or closes off today is part of an
    # installation
    present_day = present_day or (
        why in ("military=bunker", "military=trench")
        and not tags.get("historic")
        and not disused_tags(tags)
        and (tags.get("operator") or tags.get("access") in ("no", "private", "military"))
    )
    if present_day:  # serving installations are outside the historical scope and sensitive to map
        return None, "present_day_military"
    if why == "military=*/landuse=military" and fort_site:
        desc.append("Military area whose tags only name an old fort (no unit, operator or access restriction).")
    elif why == "military=*/landuse=military":
        desc.append("Military feature whose tags refer to the wartime era.")
    elif why == "military=trench":
        desc.append("Trench or other field work (military=trench); its tags give no date.")
    tagline = ", ".join(
        f"{k}={v}"
        for k, v in sorted(tags.items())
        if k
        in (
            "military",
            "landuse",
            "historic",
            "memorial",
            "ruins",
            "aeroway",
            "disused:aeroway",
            "abandoned:aeroway",
            "military_service",
        )
    )
    desc.append(f"OSM tags: {tagline}.")
    start = tags.get("start_date") if store.DATE_RE.match(tags.get("start_date", "")) else None
    end = tags.get("end_date") if store.DATE_RE.match(tags.get("end_date", "")) else None
    if start and end and end < start:
        end = None
    raw = {
        "osm_type": etype,
        "osm_id": eid,
        "tags": tags,
        "bounds": el.get("bounds"),
        "lat": el.get("lat"),
        "lon": el.get("lon"),
        "classified_by": why,
        "present_day": False,  # present-day features are never stored; kept so stored rows say so explicitly
    }
    return store.Evidence(
        source="osm",
        source_record_id=f"{etype}/{eid}",
        kind=kind,
        lon=lon,
        lat=lat,
        uncertainty_m=round(unc, 1),
        original_coord=original,
        original_datum="wgs84",
        title=title,
        date_start=start,
        date_end=end,
        description=" ".join(desc),
        url=f"https://www.openstreetmap.org/{etype}/{eid}",
        raw=raw,
    ), why


# ---- OSM: present-day installations and records --------------------------------------------------
def distance_to_element_m(lon, lat, el):
    """Metres from a point to a burial element: to the node, or to its bbox (0 when inside)."""
    if el["type"] == "node":
        return distance_m(lon, lat, el["lon"], el["lat"])
    b = el["bounds"]
    return distance_m(lon, lat, min(max(lon, b["minlon"]), b["maxlon"]), min(max(lat, b["minlat"]), b["maxlat"]))


def present_day_regions(geom_elements):
    """Outlines of the military areas / aerodromes that osm_evidence judges present-day installations."""
    out = []
    for el in geom_elements:
        tags = el.get("tags") or {}
        if tags.get("military") == "trench" and tags.get("landuse") != "military":
            continue  # a trench line is a field work, not an installation (A1 Hill, Điện Biên Phủ)
        if el["type"] != "node" and osm_evidence(el)[1] == "present_day_military":
            r = Region.from_element(el)
            if r:
                out.append(r)
    return out


def osm_geometry(refresh=False):
    """-> (Region of Vietnam, [Regions of present-day military installations])."""
    vn = [
        Region.from_element(el)
        for el in overpass("vn_boundary", OSM_GEOM_QUERIES["vn_boundary"], refresh, out="geom")
        if el["type"] == "relation"
    ]
    if len(vn) != 1 or vn[0] is None:
        raise RuntimeError("overpass vn_boundary: expected one boundary relation with geometry")
    areas = overpass("areas_geom", OSM_GEOM_QUERIES["areas_geom"], refresh, out="geom")
    return vn[0], present_day_regions(areas)


TRENCH_CONTEXT_M = 1000  # a trench this close to a kept wartime feature is taken as part of that site


def trench_needs_context(e):
    """True for a kept trench whose own tags give no era (no historic tag, no wartime wording): it is kept only
    beside a mapped wartime site (osm_records), since it may be a modern training work."""
    tags = e.raw["tags"]
    return (
        e.raw["classified_by"] == "military=trench"
        and not tags.get("historic")
        and not OSM_WARTIME_RE.search(text_of(tags))
    )


def wartime_marker(e):
    """-> what in a kept OSM record's own tags marks it as a wartime site, or None. Only a battlefield tag,
    or a name/description naming the wartime occupant or calling it former (OSM_WARTIME_RE) counts: a
    memorial marks a commemoration, a disused airfield may be of any era, and historic=yes gives no date."""
    tags = e.raw["tags"]
    if tags.get("historic") == "battlefield":
        return "tagged historic=battlefield"
    m = OSM_WARTIME_RE.search(text_of(tags))
    if m and not is_memorial(tags):
        return f"whose name or description says '{m.group(0).strip()}'"
    return None


def osm_records(elements, active_areas=()):
    """Classify unique elements (osm_evidence), then apply the rules that need other elements: drop disused
    airfield parts inside an airport in use or a closed civil one, anything inside a present-day military
    installation (active_areas: Regions), memorials within NEAR_BURIAL_M of a burial (cemetery, martyrs'
    memorial, ossuary) that was itself filtered out, and undated trenches with no wartime site within
    TRENCH_CONTEXT_M. -> (records, stats)."""
    stats = Counter()
    seen, records, burials, airports, civil = set(), [], [], [], []
    for el in elements:
        key = f"{el['type']}/{el['id']}"
        if key in seen:
            continue
        seen.add(key)
        tags = el.get("tags") or {}
        if (
            tags.get("aeroway") == "aerodrome"
            and el.get("bounds")
            and not any(k.startswith(("disused", "abandoned")) for k in tags)
        ):
            airports.append(el)  # any airport in use today, civil or military
        e, why = osm_evidence(el)
        if why == "civil_aerodrome" and el.get("bounds"):
            civil.append(el)  # a closed civil airport: its runways and the roads laid on them go with it
        if e is None:
            stats[f"skipped_{why}"] += 1
            if why.startswith("burial") and ("lat" in el or el.get("bounds")):
                burials.append(el)
        else:
            records.append(e)
    kept, trenches = [], []
    for e in records:
        # a disused taxiway/runway inside an airport still in use (Tân Sơn Nhất, Nội Bài) is part of a
        # present-day installation, not a wartime site
        if e.raw["classified_by"].startswith("disused/abandoned aeroway") and any(
            distance_to_element_m(e.lon, e.lat, a) == 0 for a in airports
        ):
            stats["skipped_inside_active_aerodrome"] += 1
            continue
        if e.raw["classified_by"].startswith("disused/abandoned aeroway") and any(
            distance_to_element_m(e.lon, e.lat, a) == 0 for a in civil
        ):
            stats["skipped_inside_civil_aerodrome"] += 1
            continue
        inside = [a.key for a in active_areas if a.key != e.source_record_id and a.near(e.lon, e.lat)]
        if inside:  # bunkers of Cam Ranh naval base, exhibits inside Biên Hòa air base
            stats["skipped_inside_present_day_military"] += 1
            continue
        if is_memorial(e.raw["tags"]) and any(distance_to_element_m(e.lon, e.lat, b) <= NEAR_BURIAL_M for b in burials):
            stats["skipped_memorial_near_burial"] += 1
            continue
        if trench_needs_context(e):
            trenches.append(e)
            continue
        kept.append(e)
    # a trench whose tags say nothing of its era is kept only beside a mapped wartime site (the A1 Hill
    # trench lines beside the A1 battlefield); others may be modern training works
    context = [(c, wartime_marker(c)) for c in kept]
    context = [(c, why) for c, why in context if why]
    for e in trenches:
        near = sorted((distance_m(e.lon, e.lat, c.lon, c.lat), c.source_record_id, c, why) for c, why in context)
        near = [x for x in near if x[0] <= TRENCH_CONTEXT_M]
        if not near:
            stats["skipped_trench_no_wartime_context"] += 1
            continue
        d, k, c, why = near[0]
        e.description = e.description.replace(
            "its tags give no date.",
            f"its tags give no date; kept because it lies {d:.0f} m from {c.title} ({k}), {why}.",
        )
        kept.append(e)
    stats["unique_elements"] = len(seen)
    return kept, stats


def ingest_osm(refresh=False):
    """Fetch (or read from cache) every Overpass query and build the records. -> (records, stats)."""
    elements, counts = [], Counter()
    for name, body in OSM_QUERIES.items():
        els = overpass(name, body, refresh)
        counts[f"elements_{name}"] = len(els)
        elements += els
    _, active_areas = osm_geometry(refresh)
    counts["present_day_areas"] = len(active_areas)
    records, stats = osm_records(elements, active_areas)
    stats.update(counts)
    return records, stats


# --------------------------------------------------------------------------------------------
# Run report and command line
# --------------------------------------------------------------------------------------------
def summarise(source, records, stats):
    """Run summary for one source: counts by kind, the records inside the starting area
    (common.AREA_OF_INTEREST) and every stat the ingest counted."""
    aoi = [r for r in records if in_bbox(r.lon, r.lat, AREA_OF_INTEREST)]
    return {
        "source": source,
        "ingested": len(records),
        "in_area_of_interest": len(aoi),
        "by_kind": dict(Counter(r.kind for r in records)),
        "aoi_records": [
            {
                "id": r.source_record_id,
                "title": r.title,
                "kind": r.kind,
                "lon": r.lon,
                "lat": r.lat,
                "uncertainty_m": r.uncertainty_m,
                "original_coord": r.original_coord,
            }
            for r in aoi
        ],
        "stats": dict(stats),
    }


SOURCES = {
    "wikidata": dict(
        name="Wikidata Query Service (battles, operations, massacres, military sites in Vietnam)",
        url="https://query.wikidata.org/",
        licence="CC0 1.0",
        notes="Article-level coordinates; events dated 1858-1989 only; civil air accidents and "
        "cemeteries/memorials/graves/tombs/ossuaries excluded.",
        doing="Asking Wikidata for battles, bases and crash sites",  # shown while it runs (common.progress)
        fn=ingest_wikidata,
    ),
    "osm": dict(
        name="OpenStreetMap via Overpass API (military, historic, aeroway features in Vietnam)",
        url="https://www.openstreetmap.org/",
        licence="ODbL 1.0 (© OpenStreetMap contributors)",
        notes="Wartime/historic military features and war memorials; present-day installations and "
        "burials (cemeteries, graves, ossuaries, martyrs' memorials and memorials beside them) excluded.",
        doing="Asking OpenStreetMap for wartime places and memorials",
        fn=ingest_osm,
    ),
}


def sync(db, source, records):
    """Final write for a source: upsert all, delete what it no longer yields (withdraw if in a case)."""
    return store.sync_source(db, source, records, reason=f"no longer yielded by the {source} ingester")


def main(argv=None):
    """Ingest each source (or only --only), sync it into the evidence store and write the run report."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    ap.add_argument("--only", choices=sorted(SOURCES))
    args = ap.parse_args(argv)
    db = store.connect()
    report = {}
    for sid, cfg in SOURCES.items():
        if args.only and sid != args.only:
            continue
        progress(None, cfg["doing"])
        records, stats = cfg["fn"](refresh=args.refresh)
        progress(None, f"Saving {len(records):,} records")
        store.upsert_source(db, sid, name=cfg["name"], url=cfg["url"], licence=cfg["licence"], notes=cfg["notes"])
        kept, deleted, withdrawn = sync(db, sid, records)
        stats.update(synced_kept=kept, synced_deleted=deleted, synced_withdrawn=withdrawn)
        report[sid] = summarise(sid, records, stats)
        in_aoi = report[sid]["in_area_of_interest"]
        print(f"{sid}: {len(records)} records, {in_aoi} in the area of interest; {dict(stats)}")
    WORK.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


if __name__ == "__main__":
    main()
