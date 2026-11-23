"""Vietnamese national relic lists (vi.wikipedia) -> war-related sites, geocoded to commune.

Inputs, all cached under data/raw/vn_relics/ on first run (re-fetched only with --refresh):
  - the two relic lists below, as raw wikitext;
  - vi.wikipedia coordinates of the linked relic and commune articles (API prop=coordinates);
  - Wikidata district -> commune membership (one SPARQL query, CC0);
  - OSM pre-2025 province land borders (one Overpass query, ODbL);
and data/work/gns_index.json, the GNS name index written by gns.py (run that first).
Output: store.Evidence records for source 'vn_relics' (the store then holds exactly these;
relics no longer kept are deleted or withdrawn) and a run report in data/work/vn_relics_report.json.

Sources (CC BY-SA 4.0, raw wikitext via index.php?action=raw):
  - 'Danh sách Di tích quốc gia Việt Nam': one table per province (post-2025, 34 provinces),
    columns Di tích | Địa bàn | Phân loại | Năm công nhận | Ghi chú.
  - 'Di tích quốc gia đặc biệt (Việt Nam)': the special-national-relic list, one table per
    ranking round, columns Di tích | (Hình ảnh) | Địa điểm | Hạng mục | Ghi chú; some rows carry
    a {{coord}}. Rows that repeat a main-list relic are merged into it.

Only war-related historical relics are kept (battles, massacres, resistance bases, prisons,
posts, tunnels, shoot-down sites) for 1858-1975. Cemeteries, graves, tombs, mausoleums and
temples/shrines are dropped and counted; nothing is filtered by nationality. Mass-death sites
are kept only with a recorded source check for victims' graves on site (MASS_DEATH_CHECKED).

Why the geocoding is careful: the lists give places as text in the post-2025 administrative
units (34 merged provinces, ~3,300 merged communes, no districts), while the nationwide commune
gazetteer used here (GNS) has the pre-2025 names. A name can therefore match a namesake in
another part of a merged province, or only one of the old communes a new one absorbed. Each
rule below exists to keep the stored radius honest: it must cover every place the list names,
and a match that contradicts the named district is not used.

Geocoding, best first:
  1. a {{coord}} given in the special list for that relic ('article-coordinate');
  2. the coordinate of the relic's own linked vi.wikipedia article, only when it lies near the
     commune/province found below ('article-coordinate');
  3. the commune in 'Địa bàn' matched to a GNS ADM3 feature in that province
     (data/work/gns_index.json, pre-2025 names) -> 'commune-centroid', radius = the commune's
     measured size (see Gazetteer.commune_extent, at least 3 km);
  4. the commune's own vi.wikipedia article coordinate (post-2025 merged communes are often
     missing from GNS) -> 'commune-centroid', radius covering the ~3 old communes merged into it;
  5. the district (GNS ADM2) -> 'district-centroid', radius = the district's estimated size
     (at least 12 km, the GNS ADM2 extent used by gns.py): the farthest of its communes, which
     are those Wikidata places in it (P131) plus those nearer its GNS point than any other
     district's - GNS has no boundaries, so this is an estimate, not a surveyed extent;
  6. the province (mean of its GNS districts) -> 'province-centroid', radius = the farthest edge
     of the province's GNS communes.
A cell naming several places gets one circle around all of them ('enclosing-circle': the middle of
their bounding box, a point in none of them). A commune named with its district must lie in that
district (Wikidata membership); a namesake elsewhere in the merged province is not used.
Every place the list names (all peer communes/districts, communes named in the relic's own name,
the located key points of a road) is covered by the radius; places that cannot be located are
said so in the description. Area relics (war zones, bases, campaigns, battles) get a radius
floor (AREA_FLOOR_M), because their position is only somewhere inside the area. Wikipedia
coordinates are fetched in batches of 50 titles; any failed or truncated answer aborts the run
before the store is written, so records never silently fall back to coarser centroids.

Run:  .venv/bin/python scripts/ingest/vn_relics.py [--refresh]
"""

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ingest: common
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts: store
import store  # noqa: E402
from common import AREA_OF_INTEREST, cached_get, in_bbox  # noqa: E402

SOURCE = "vn_relics"
WORK = store.ROOT / "data" / "work"
GNS_INDEX = WORK / "gns_index.json"
MAIN_TITLE = "Danh sách Di tích quốc gia Việt Nam"
SPECIAL_TITLE = "Di tích quốc gia đặc biệt (Việt Nam)"
WIKI = "https://vi.wikipedia.org"
API = WIKI + "/w/api.php"

# Radii (uncertainty_m, metres) for each kind of position; see the module docstring.
UNC_ARTICLE = 2000.0  # article/list coordinate of a battlefield or base: site-level, approximate
UNC_COMMUNE = 3000.0  # floor for a pre-2025 commune (GNS ADM3 extent); measured size if larger
UNC_COMMUNE_WIKI = 5000.0  # floor for a post-2025 merged commune: ~3x the area of an old one
UNC_DISTRICT = 12000.0  # floor for a district: GNS ADM2 extent (gns.EXTENT_M['ADM2'])
MERGED_PARTS = 3  # 2025 reform: ~10,000 communes -> ~3,300, so ~3 old communes per new one
# A commune's half-size from GNS spacing: in a regular lattice of spacing d each Voronoi cell has
# circumradius d/sqrt(3) = 0.58 d; 0.65 x the median distance to the 6 nearest communes adds margin.
COMMUNE_SPACING_FACTOR = 0.65
ARTICLE_MAX_KM = 25.0  # an article coordinate farther than this from the admin match is ignored
ROUTE_POINT_MIN_M = 500.0  # radius around a located key point of a road (a pass, a ford, a hill)
COMPONENT_MIN_M = 1000.0  # radius around a key point the relic name names (a ferry, a bridge and its banks)
# Geocoding lookups are bucketed on a grid of 1/4-degree cells. A cell is >= 25 km each way in
# Vietnam (27.8 km of latitude; 25.5-27.5 km of longitude between 8 and 23 N), so searching r
# cells around a point sees everything within r x 25 km of it.
GRID_PER_DEG = 4
GRID_CELL_KM = 25.0

WDQS = "https://query.wikidata.org/sparql"
# Old (pre-2025) communes/wards/townships and the districts Wikidata places them in, any rank (the
# 2025 reform abolished districts, so the old P131 statements are often no longer preferred).
DISTRICT_QUERY = """SELECT ?d ?dl ?dc ?c ?cc WHERE {
  VALUES ?ct { wd:Q2389082 wd:Q1070942 wd:Q687188 }
  VALUES ?dt { wd:Q2582669 wd:Q2112349 wd:Q3249005 wd:Q6644510 }
  ?c wdt:P31 ?ct; wdt:P625 ?cc; p:P131/ps:P131 ?d .
  ?d wdt:P31 ?dt .
  OPTIONAL { ?d wdt:P625 ?dc }
  OPTIONAL { ?d rdfs:label ?dl FILTER(LANG(?dl) = "vi") }
}"""
MEMBER_LINK_KM = 15.0  # a district's communes adjoin: a member farther than this from all others is an error
MEMBER_MAX_KM = 80.0  # no district is 160 km across
MEMBER_EVIDENCE_KM = 3.0  # a Wikidata commune this close to a GNS commune centroid is (in) that commune
DISTRICT_MARGIN_KM = 2.0  # another district point must be this much nearer to count as evidence
# A full WDQS answer has ~700 districts and ~20,000 commune rows; an empty or truncated one (a
# timeout that still returns valid JSON) must abort the run, not silently disable the district check.
MIN_WD_DISTRICTS = 600
MIN_WD_MEMBERS = 9000
OVERPASS = "https://overpass-api.de/api/interpreter"
# Pre-2025 province land borders (OSM as of 2025-03-01, before the July 2025 merger), sea
# boundaries (maritime=yes) left out: every vertex of a province's land border, so a province
# radius provably reaches its inland edges. The coast and islands are covered by its GNS communes.
PROVINCE_BORDER_QUERY = (
    '[out:json][timeout:600][date:"2025-03-01T00:00:00Z"];'
    'rel[boundary=administrative][admin_level=4]["ISO3166-2"~"^VN-"]->.p;'
    ".p out body;way(r.p)[maritime!=yes];out tags geom qt;"
)

# Post-2025 province (section heading) -> pre-2025 GNS ADM1 codes it absorbed.
PROVINCE_CODES = {
    "dien bien": ["VN-71"],
    "lai chau": ["VN-01"],
    "lao cai": ["VN-02", "VN-06"],
    "son la": ["VN-05"],
    "cao bang": ["VN-04"],
    "lang son": ["VN-09"],
    "phu tho": ["VN-68", "VN-70", "VN-14"],
    "thai nguyen": ["VN-69", "VN-53"],
    "tuyen quang": ["VN-07", "VN-03"],
    "quang ninh": ["VN-13"],
    "bac ninh": ["VN-56", "VN-54"],
    "hai phong": ["VN-HP", "VN-61"],
    "hung yen": ["VN-66", "VN-20"],
    "ninh binh": ["VN-18", "VN-63", "VN-67"],
    "ha noi": ["VN-HN"],
    "thanh hoa": ["VN-21"],
    "nghe an": ["VN-22"],
    "ha tinh": ["VN-23"],
    "quang tri": ["VN-25", "VN-24"],
    "hue": ["VN-26"],
    "da nang": ["VN-DN", "VN-27"],
    "quang ngai": ["VN-29", "VN-28"],
    "khanh hoa": ["VN-34", "VN-36"],
    "gia lai": ["VN-30", "VN-31"],
    "dak lak": ["VN-33", "VN-32"],
    "lam dong": ["VN-35", "VN-72", "VN-40"],
    "dong nai": ["VN-39", "VN-58"],
    "tay ninh": ["VN-37", "VN-41"],
    "ho chi minh": ["VN-SG", "VN-57", "VN-43"],
    "dong thap": ["VN-45", "VN-46"],
    "an giang": ["VN-44", "VN-47"],
    "vinh long": ["VN-49", "VN-50", "VN-51"],
    "can tho": ["VN-CT", "VN-52", "VN-73"],
    "ca mau": ["VN-59", "VN-55"],
}
# Old province names that may appear in the special list's 'Địa điểm' text.
OLD_PROVINCE_TO_NEW = {
    "ha giang": "tuyen quang",
    "yen bai": "lao cai",
    "bac kan": "thai nguyen",
    "vinh phuc": "phu tho",
    "hoa binh": "phu tho",
    "bac giang": "bac ninh",
    "hai duong": "hai phong",
    "thai binh": "hung yen",
    "ha nam": "ninh binh",
    "nam dinh": "ninh binh",
    "quang binh": "quang tri",
    "thua thien hue": "hue",
    "quang nam": "da nang",
    "kon tum": "quang ngai",
    "binh dinh": "gia lai",
    "phu yen": "dak lak",
    "ninh thuan": "khanh hoa",
    "dak nong": "lam dong",
    "binh thuan": "lam dong",
    "binh phuoc": "dong nai",
    "long an": "tay ninh",
    "binh duong": "ho chi minh",
    "ba ria vung tau": "ho chi minh",
    "tien giang": "dong thap",
    "kien giang": "an giang",
    "ben tre": "vinh long",
    "tra vinh": "vinh long",
    "soc trang": "can tho",
    "hau giang": "can tho",
    "bac lieu": "ca mau",
    "dak lak": "dak lak",
    "dac lak": "dak lak",
}
# Pre-2025 provinces that were merged away -> their GNS ADM1 code (for 'X' naming an old province).
OLD_PROVINCE_CODE = {
    "ha giang": "VN-03",
    "yen bai": "VN-06",
    "bac kan": "VN-53",
    "vinh phuc": "VN-70",
    "hoa binh": "VN-14",
    "bac giang": "VN-54",
    "hai duong": "VN-61",
    "thai binh": "VN-20",
    "ha nam": "VN-63",
    "nam dinh": "VN-67",
    "quang binh": "VN-24",
    "quang nam": "VN-27",
    "kon tum": "VN-28",
    "binh dinh": "VN-31",
    "phu yen": "VN-32",
    "ninh thuan": "VN-36",
    "dak nong": "VN-72",
    "binh thuan": "VN-40",
    "binh phuoc": "VN-58",
    "long an": "VN-41",
    "binh duong": "VN-57",
    "ba ria vung tau": "VN-43",
    "tien giang": "VN-46",
    "kien giang": "VN-47",
    "ben tre": "VN-50",
    "tra vinh": "VN-51",
    "soc trang": "VN-52",
    "hau giang": "VN-73",
    "bac lieu": "VN-55",
}

# ---- text helpers ---------------------------------------------------------------------------
_TONE_FIX = {
    "uỷ": "ủy",
    "uý": "úy",
    "oà": "òa",
    "oá": "óa",
    "oả": "ỏa",
    "oã": "õa",
    "oạ": "ọa",
    "uỳ": "ùy",
    "uỹ": "ũy",
    "uỵ": "ụy",
    "oè": "òe",
    "oé": "óe",
}

_TONE_RE = re.compile("(?:" + "|".join(_TONE_FIX) + r")(?![^\W\d_])")


def vlower(s):
    """NFC lowercase with old-style tone marks moved ('uỷ' -> 'ủy', 'hoà' -> 'hòa')."""
    s = unicodedata.normalize("NFC", s or "").lower()
    # only at a syllable end: 'hoà' -> 'hòa' but 'loại', 'quỳnh' stay as they are
    return _TONE_RE.sub(lambda m: _TONE_FIX[m.group(0)], s)


def normalise(name):
    """Accent-stripped lowercase key, same as gns_index.json keys: 'Cao Lãnh' -> 'cao lanh'."""
    s = (name or "").replace("đ", "d").replace("Đ", "D")
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^0-9a-z]+", " ", s)
    return " ".join(s.split())


# Administrative-level words (and the lists' abbreviations) that open a place name.
ADMIN_PREFIX_RE = re.compile(
    r"^(xã|phường|thị trấn|đặc khu|huyện|quận|thị xã|thành phố|tỉnh|tp\.?|tx\.?|tt\.?|"
    r"h\.|p\.|x\.)\s*",
    re.I,
)
DISTRICT_PREFIX_RE = re.compile(r"^(huyện|quận|thị xã|thành phố|tp\.?|tx\.?|h\.)\s*", re.I)
COMMUNE_PREFIX_RE = re.compile(r"^(xã|phường|thị trấn|đặc khu|tt\.?|p\.|x\.)\s*", re.I)


def admin_key(name):
    """normalise() of a place name without its admin word: 'Huyện Bố Trạch' -> 'bo trach'. The
    prefix is stripped on the accented text, so 'Quận 5' -> '5' but 'Quán Hàu' stays whole."""
    return normalise(ADMIN_PREFIX_RE.sub("", vlower(name)).strip())


def strip_refs(text):
    """Wikitext without comments and <ref> footnotes (which may hold '|' and links of their own)."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"<ref[^>]*/>", "", text)
    return re.sub(r"<ref[^>]*>.*?</ref>", "", text, flags=re.S)


def split_top(s, sep):
    """Split s on sep outside [[...]] and {{...}}."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        two = s[i : i + 2]
        if two in ("[[", "{{"):
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two in ("]]", "}}"):
            depth = max(0, depth - 1)
            cur.append(two)
            i += 2
            continue
        if depth == 0 and s.startswith(sep, i):
            out.append("".join(cur))
            cur = []
            i += len(sep)
            continue
        cur.append(s[i])
        i += 1
    out.append("".join(cur))
    return out


def drop_cell_attrs(cell):
    """A table cell without its leading attributes ('style="..." | text' -> 'text')."""
    parts = split_top(cell, "|")
    if len(parts) > 1 and "=" in parts[0] and "[[" not in parts[0]:
        return "|".join(parts[1:])
    return cell


LINK_RE = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")


def links(cell):
    """[(target, display)] for article links in a cell (files/categories skipped)."""
    out = []
    for target, disp in LINK_RE.findall(cell):
        target = target.strip()
        if re.match(r"(?i)(tập[ _]tin|file|hình|image|thể[ _]loại|category):", target):
            continue
        out.append((target.split("#")[0].strip() or target, (disp or target).strip()))
    return out


def plain(cell):
    """Readable text of a wikitext cell: links as their display text, files, templates, HTML and
    bold/italic markup removed, whitespace collapsed."""
    s = re.sub(r"\[\[(?:tập[ _]tin|file|hình|image):[^\]]*\]\]", "", cell, flags=re.I)
    s = LINK_RE.sub(lambda m: m.group(2) or m.group(1), s)
    s = re.sub(r"\{\{[^{}]*\}\}", "", s)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("'''", "").replace("''", "")
    return " ".join(s.split())


def parse_coord_template(cell):
    """{{coord|21|02|11|N|105|50|35|E|...}} or {{coord|10.4|105.6}} -> (template text, lon, lat)."""
    m = re.search(r"\{\{\s*coord\s*\|([^{}]*)\}\}", cell, flags=re.I)
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).split("|") if p.strip() and "=" not in p]

    def dms(vals):
        v = 0.0
        for k, x in enumerate(vals):
            v += float(x) / (60**k)
        return v

    try:
        hemis = [i for i, p in enumerate(parts) if p.upper() in ("N", "S", "E", "W")]
        if len(hemis) >= 2:
            i, j = hemis[0], hemis[1]
            lat = dms(parts[:i]) * (-1 if parts[i].upper() == "S" else 1)
            lon = dms(parts[i + 1 : j]) * (-1 if parts[j].upper() == "W" else 1)
        else:
            lat, lon = float(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return None
    return m.group(0), round(lon, 6), round(lat, 6)


def coord_extent(template):
    """-> (radius_m, label) declared by a {{coord}}: 'dim:4km' is the size of the thing marked,
    'scale:500000' a map scale (GeoHack convention: dim = scale / 10 m), 'name=' what it marks.
    The radius is half that size; (None, None) when the template declares neither."""
    radius = None
    m = re.search(r"\bdim:\s*([\d.]+)\s*(km|m)?", template, flags=re.I)
    if m:
        radius = float(m.group(1)) * (1000.0 if (m.group(2) or "m").lower() == "km" else 1.0) / 2
    else:
        m = re.search(r"\bscale:\s*(\d+)", template, flags=re.I)
        if m:
            radius = int(m.group(1)) / 10.0 / 2
    n = re.search(r"\|\s*name\s*=\s*([^|}]+)", template)
    return radius, (n.group(1).strip() if n else None)


# ---- wikitext tables ------------------------------------------------------------------------
def parse_tables(text):
    """Yield (section path, headers, [raw cells]) for each row of every wikitable."""
    text = strip_refs(text)
    sections = []
    headers, row, in_table = [], None, False
    for line in text.splitlines():
        m = re.match(r"^(={2,5})\s*(.*?)\s*\1\s*$", line)
        if m and not in_table:
            level = len(m.group(1))
            sections = sections[: level - 2] + [m.group(2)]
            continue
        s = line.strip()
        if s.startswith("{|"):
            in_table, headers, row = True, [], None
            continue
        if not in_table:
            continue
        if s.startswith("|}"):
            if row:
                yield list(sections), headers, row
            in_table, row = False, None
            continue
        if s.startswith("|-"):
            if row:
                yield list(sections), headers, row
            row = None
            continue
        if s.startswith("|+"):
            continue
        if s.startswith("!"):
            headers += [plain(drop_cell_attrs(c)) for c in split_top(s[1:], "!!")]
            continue
        if s.startswith("|"):
            # '|| a || b' continues the row with more cells: the leading '||' is a cell separator,
            # not a '|' followed by a cell that starts with '|'.
            body = s[2:] if s.startswith("||") else s[1:]
            row = (row or []) + [drop_cell_attrs(c).strip() for c in split_top(body, "||")]
            continue
        if row:
            row[-1] = (row[-1] + "\n" + line).strip()


def column(headers, cells, *names):
    """Cell under the first header containing any of names (case-insensitive)."""
    for i, h in enumerate(headers):
        if any(n in vlower(h) for n in names):
            return cells[i] if i < len(cells) else ""
    return ""


def province_key(heading):
    """Key of PROVINCE_CODES for a section heading or place text ('Tỉnh Đắk Lắk' -> 'dak lak')."""
    k = normalise(heading)
    k = re.sub(r"^(thanh pho|tinh|thu do)\s+", "", k)
    return {"dak lak": "dak lak", "dac lak": "dak lak"}.get(k, k)


def main_rows(text):
    """Rows of the main list: dicts with province heading and the five raw cells."""
    out = []
    for sections, headers, cells in parse_tables(text):
        if not headers or "di tích" not in vlower(headers[0]):
            continue
        prov = province_key(sections[-1]) if sections else ""
        if prov not in PROVINCE_CODES:
            continue
        out.append(
            {
                "list": "main",
                "section": sections[-1],
                "province": prov,
                "name_wt": column(headers, cells, "di tích"),
                "place_wt": column(headers, cells, "địa bàn"),
                "category": plain(column(headers, cells, "phân loại")),
                "recognised": plain(column(headers, cells, "năm")),
                "notes": plain(column(headers, cells, "ghi chú")),
            }
        )
    return out


def special_rows(text):
    """Rows of the special-national-relic list: same dicts as main_rows(), plus the image file,
    with the (post-2025) province read from the 'Địa điểm' text, old province names mapped."""
    out = []
    for sections, headers, cells in parse_tables(text):
        if not headers or "di tích" not in vlower(headers[0]) or not any("địa điểm" in vlower(h) for h in headers):
            continue
        # Later rounds drop the image column without changing the header: realign by content.
        img_i = next((i for i, h in enumerate(headers) if "hình" in vlower(h)), None)
        img_cell = cells[img_i] if img_i is not None and img_i < len(cells) else ""
        looks_img = not img_cell.strip() or re.match(r"(?i)\[\[(tập[ _]tin|file|hình|image):", img_cell.strip())
        if img_i is not None and len(cells) < len(headers) and not looks_img:
            hdr = [h for h in headers if "hình" not in vlower(h)]
        else:
            hdr = headers
        place = column(hdr, cells, "địa điểm")
        prov = ""
        for _target, disp in links(place) + [
            (None, x) for x in re.findall(r"(?:tỉnh|thành phố)\s+([^,<\[\]{}]+)", place)
        ]:
            k = province_key(disp)
            k = OLD_PROVINCE_TO_NEW.get(k, k)
            if k in PROVINCE_CODES:
                prov = k
        img = re.search(r"(?i)\[\[(?:tập[ _]tin|file|hình|image):([^|\]]+)", column(hdr, cells, "hình"))
        out.append(
            {
                "list": "special",
                "section": sections[-1] if sections else "",
                "province": prov,
                "image": img.group(1).strip() if img else None,
                "name_wt": column(hdr, cells, "di tích"),
                "place_wt": place,
                "category": plain(column(hdr, cells, "hạng mục", "loại hình")),
                "recognised": "",
                "notes": plain(column(hdr, cells, "ghi chú")),
            }
        )
    return out


# ---- classification -------------------------------------------------------------------------
# 'lăng' (tomb, mausoleum) only as a head noun - at the start or after ',', '&', 'và' ('Đền, Lăng
# Thái sư ...') - so place names ending in 'Lăng' (Chi Lăng, Phù Lăng) are not taken for tombs.
BURIAL_RE = re.compile(
    r"\b(nghĩa trang|nghĩa địa|mộ|mồ|mả|phần mộ|mộ phần|lăng mộ|tháp mộ|nhà mồ|hài cốt|"
    r"mộ tập thể|mộ chung)\b|(?:^|[,&]\s*|\bvà\s+)lăng\b"
)
# Grave words that mean the site itself holds remains, wherever they appear in the name (a
# parenthetical such as '(Mả thằng Tây)' names the site, not a sub-site to ignore).
GRAVE_RE = re.compile(r"\b(mả|nhà mồ|hài cốt|mộ tập thể|mộ chung|nghĩa trang|nghĩa địa|lăng mộ)\b")
# The same on accent-stripped text (names typed without diacritics). Only terms that stay
# unambiguous when folded: 'ma' (ghost/but), 'lang mo' (làng mỏ, mining village), 'nha mo' and
# 'hai cot' (hai cột, two pillars) would match ordinary words, so they are matched accented only.
GRAVE_FOLDED_RE = re.compile(r"\b(nghia trang|nghia dia|mo tap the|quy tap hai cot)\b")
# Relics whose site holds an ossuary or victims' graves although the name does not say so
# (name_key -> what is buried there). Checked against the relics' vi.wikipedia articles.
KNOWN_ON_SITE_BURIAL = {
    "chung tich toi ac cua bon diet chung pon pot": "Ba Chúc (An Giang): the memorial's main structure is an ossuary (nhà mồ) holding victims' remains",
    "chung tich son my": "Sơn Mỹ / My Lai (Quảng Ngãi): mass graves of the massacre victims on site",
    "cuoc noi day cua dong bao stieng": "named 'Mả thằng Tây' (the Westerners' graves)",
    # massacre sites checked 2026-11 (see MASS_DEATH_CHECKED for the ones kept)
    "vu tham sat binh thanh": "Bình Thành (Đồng Tháp), 1954: vi.wikipedia 'Thảm sát Bình Thành 1954 (Đồng Tháp)' - the soldiers "
    "'đào hố chôn chung tại đây' (dug a common pit and buried the victims on the spot); memorial stele "
    "at the original site since 1996",
    "chung tich toi ac my nguy o biet khu hai yen binh hung": "Biệt khu Hải Yến - Bình Hưng (Cà Mau): dulichcamau.net - the site keeps 'các hố chôn người tập thể' "
    "(mass burial pits) and the 'cầu vĩnh biệt' leading prisoners to them",
    "cac chung tich ve cuoc tham sat 286 nguoi vo toi do thuc dan phap tien hanh nam 1947": "Cầu Hòa, Phong Nẫm (Giồng Trôm), 1947: congly.vn (2015) - families still keep common graves of "
    "dozens of victims ('mộ chôn chung hàng chục người', 'một mồ chôn 21 người'); the relic is the "
    "set of 'chứng tích' of the massacre",
}
# Mass-death sites (massacres, crimes, groups killed) kept after a source check for victims' graves
# on site. name_key -> (verdict, evidence). 'no_grave_on_site': the sources describe a memorial and
# no graves; 'unresolved': no source found either way (kept, flagged in the description).
MASS_DEATH_CHECKED = {
    "tham sat my thuy": (
        "no_grave_on_site",
        "hailang.quangtri.gov.vn: a memorial temple (đền tưởng niệm) at the village entrance; no graves "
        "on site mentioned (1948, 526 killed)",
    ),
    "tham sat lang tan minh": (
        "no_grave_on_site",
        "bazantravel.com: a memorial house with a stele listing the victims (1947); no graves on site mentioned",
    ),
    "vu tham sat my trach": (
        "no_grave_on_site",
        "vi/en.wikipedia 'Thảm sát Mỹ Trạch': memorial park with a stele at the foot of Mỹ Trạch bridge; "
        "burial of the victims not described",
    ),
    "tham sat cho giua 1940": (
        "no_grave_on_site",
        "vi.wikipedia 'Thảm sát Chợ Giữa': French troops threw the dead into the bomb craters and kept "
        "families from taking the bodies; the relic is a memorial statue (Tượng đài Căm thù) - no marked "
        "grave found, so remains may be unrecovered near the market",
    ),
    "nga ba giong san": (
        "no_grave_on_site",
        "baotintuc.vn (2014): a memorial stele and park (15,000 m2) and a display house; no graves "
        "mentioned; the stele was later moved to ấp Bến Đình, Phú Đông",
    ),
    "ghi dau toi ac cua thuc dan phap tai lan ang": (
        "no_grave_on_site",
        "dsvh.gov.vn / baolangson.vn: where Hoàng Văn Hán was ambushed and killed (1945); a marker stele, "
        "no grave mentioned",
    ),
    "ghi dau toi ac cua thuc dan phap tai minh dan": (
        "no_grave_on_site",
        "baolangson.vn: villagers killed and 14 houses burned (1940); a stele marking the event, no grave mentioned",
    ),
    "cac thanh nien xung phong dai doi 915 hy sinh tai luu xa": (
        "no_grave_on_site",
        "thainguyen.dcs.vn and others: a memorial house built over the shelter where 60 youth volunteers "
        "died (24/12/1972); no graves on site mentioned, the dead were taken to Dốc Lim cemetery",
    ),
    "tham sat ngan son chi thanh": (
        "no_grave_on_site",
        "vi.wikipedia 'Thảm sát Ngân Sơn - Chí Thạnh': 9 shot in a school yard (1954), the dead were "
        "carried into Chí Thạnh by the protesters; no grave on site",
    ),
    "cuoc tham sat 129 nguoi dan vo toi cua my nguy": (
        "unresolved",
        "no source found on the site or on where the 129 victims (Tân Thanh Tây, Mỏ Cày Bắc) are buried",
    ),
    "nhung di tich lich su chong my cuu nuoc tai thanh pho nam dinh": (
        "unresolved",
        "air-raid sites in Nam Định city (shelters, a command post, Hàng Thao street bombing); not "
        "source-checked for graves",
    ),
}
# Names that record a mass death: these need a MASS_DEATH_CHECKED or KNOWN_ON_SITE_BURIAL entry.
MASS_DEATH_RE = re.compile(r"thảm sát|tàn sát|tội ác|chứng tích|vô tội|\b(?:các|những)\b.*\bhy sinh\b")


def needs_burial_check(name):
    """True when the relic name records a mass death (see MASS_DEATH_RE)."""
    return bool(MASS_DEATH_RE.search(vlower(plain(name))))


def burial_check(name):
    """-> {'verdict', 'evidence'} for a kept mass-death relic, or None for other relics."""
    if not needs_burial_check(name):
        return None
    verdict, why = MASS_DEATH_CHECKED.get(name_key(name), ("unchecked", "no source check recorded"))
    return {"verdict": verdict, "evidence": why}


WORSHIP_RE = re.compile(
    r"^(khu di tích |di tích |cụm di tích )?(đền|đình|chùa|miếu|am|phủ|nhà thờ|"
    r"văn miếu|văn chỉ|từ đường|nhà thờ họ|thánh thất|tịnh xá|quán|điện)\b"
)
AIRCRAFT_RE = re.compile(r"bắn rơi|máy bay|b[- ]?52")
# An air-defence position (missile/AA battery): the relic is the launcher, not where a wreck fell.
AIR_DEFENCE_RE = re.compile(r"trận địa|tên lửa|pháo cao xạ|cao xạ")
AIR_RAID_RE = re.compile(r"ném bom|bắn phá|oanh tạc|không kích|bom mỹ")
MASSACRE_RE = re.compile(r"thảm sát|tàn sát|tội ác|chứng tích")
BATTLE_RE = re.compile(
    r"chiến thắng|\btrận(?! địa)\b|chiến trường|chiến dịch|khởi nghĩa|nổi dậy|"
    r"đồng khởi|giao chiến|chốt chặn|tổng tiến công|chiến đấu|đánh mỹ|đánh pháp|diệt mỹ|"
    r"thành cổ quảng trị|81 ngày đêm"
)
# 'X hy sinh' (X died/was killed here) without a battle word: a loss at a place, not a battle.
DEATH_RE = re.compile(r"\bhy sinh\b")
SITE_RE = re.compile(
    r"căn cứ|chiến khu|an toàn khu|\batk\d*\b|địa đạo|làng hầm|\bhầm\b|nhà tù|nhà lao|"
    r"trại giam|\bngục\b|(?<!ba )(?<!chợ )\bđồn\b|\bbót\b|cứ điểm|pháo đài|chiến lũy|"
    r"trận địa|sân bay|sở chỉ huy|bộ chỉ huy|bộ tư lệnh|\bcăng\b|lô cốt|hàng rào điện tử|"
    r"đường trường sơn|đường hồ chí minh|đường mòn|trung ương cục|xứ ủy|khu ủy|tỉnh ủy|"
    r"kháng chiến|quân giải phóng|quân khu|binh công xưởng|công binh xưởng|"
    r"tuyến phòng thủ|phòng tuyến|ngã ba đồng lộc|bến tàu không số|cơ quan giao bưu|"
    r"cảng quân sự|trọng điểm|đường dây 559|điểm vượt"
)
# The list's 'Ghi chú' names the war relic complex (khu di tích, KDT) a row belongs to, e.g.
# 'KDT Trận chiến Quảng Trị', 'KDT Đường Trường Sơn': such rows are war sites even when their
# own name (a crossroads, a bridge) has no war word.
WAR_COMPLEX_NOTE_RE = re.compile(r"kdt\s+(?:trận chiến quảng trị|đường trường sơn)")
# Relics whose name has no war word but whose war use is documented: name_key -> (kind, reason).
# The reason is shown to the analyst, with its source.
WAR_RELIC_OVERRIDE = {
    "ham rong": (
        "air_strike",
        "air-raid target: the relic complex includes the Hàm Rồng bridge over the Mã river, which US "
        "aircraft attacked repeatedly in 1965-1972 and whose defenders claimed over 100 aircraft "
        "(vi.wikipedia 'Cầu Hàm Rồng'); the complex also holds older non-war sites",
    ),
    # The list says only 'Lịch sử đặc biệt ... đặc biệt 2013'; no article (red link) or other source
    # found. Kept as a lead, kind 'other': nothing says what happened there.
    "nga ba cau ga": (
        "other",
        "possible war site, unconfirmed: a crossroads (ngã ba) that the Quảng Trị table lists between rows "
        "it assigns to the war relic complex 'KDT Trận chiến Quảng Trị'; its own note says only 'đặc biệt "
        "2013' (ranked a special relic in 2013); the list does not say what happened there and no source "
        "was found",
    ),
    # mass-death sites caused by bombing (MASS_DEATH_CHECKED holds the grave check)
    "cac thanh nien xung phong dai doi 915 hy sinh tai luu xa": (
        "air_strike",
        "air raid: 60 youth volunteers of Company 915 died in their shelter on 24/12/1972, during the "
        "US B-52 raids on Thái Nguyên (shelter and date: thainguyen.dcs.vn; a bombing, not a battle)",
    ),
    "nhung di tich lich su chong my cuu nuoc tai thanh pho nam dinh": (
        "air_strike",
        "air-raid sites of the US bombing of Nam Định city (shelters, a command bunker, the Hàng Thao "
        "street bombing), as listed in the relic name",
    ),
}
# Words that contain a war keyword but are not war sites ('Vân Đồn' port, 'đồn điền' plantation).
NOT_WAR_RE = re.compile(r"vân đồn|đồn điền|^nghè\b")
# Undated relics named after pre-1858 wars (Bạch Đằng, Lam Sơn, Tây Sơn ...).
ANCIENT_RE = re.compile(
    r"bạch đằng|chi lăng|xương giang|rạch gầm|xoài mút|đống đa|ngọc hồi|như nguyệt|"
    r"chương dương|hàm tử|tốt động|chúc động|cổ loa|lam sơn|tây sơn|quang trung|"
    r"ngô quyền|trần hưng đạo|lý thường kiệt|hai bà trưng|bà triệu|lê lợi|lý bí|"
    r"phùng hưng|mai thúc loan|nhà trần|nhà lý|nhà lê|nguyên mông|nguyên - mông|"
    r"năm 938|đinh bộ lĩnh|thời hùng|trường lũy|thế kỷ (?:xviii|xvii|xvi|xv|xiv|xiii|xii|xi|x|ix|viii|vii|vi|v|iv|iii|ii|i)\b|thời trần|thời lý|thời lê|lê chân"
)
YEAR_RE = re.compile(r"(?<!\d)(1[0-9]{3})(?!\d)")
DMY_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(1[89]\d\d)(?!\d)")

# Generic words that open a relic name ('Khu di tích', 'Địa điểm') before its head noun.
AREA_PREFIX_RE = re.compile(
    r"^(khu di tích lịch sử|khu di tích|di tích lịch sử|di tích|cụm di tích|địa điểm|"
    r"khu vực|khu|các địa điểm (?:của |thuộc )?|những di tích lịch sử)\s+"
)
# Heads that make a relic one military point site (a post, a prison, a command post, a battery).
# '(?![^\W\d_])' (no letter follows) ends the word, so 'đồn' (post) does not match 'đồng' (field).
POINT_SITE_HEAD_RE = re.compile(
    r"^(sở chỉ huy|chỉ huy sở|nhà tù|nhà lao|ngục|khu trại giam|trại giam|đồn|bót|"
    r"địa đạo|hầm|sân bay|trận địa|cứ điểm|tập đoàn cứ điểm|pháo đài|chiến lũy|"
    r"căng|lô cốt)(?![^\W\d_])"
)


def relic_head(name):
    """The relic's head noun phrase: lowercase, generic prefixes ('Khu di tích', 'Địa điểm') removed."""
    head = vlower(plain(name)).strip()
    for _ in range(3):  # 'Khu di tích Địa điểm ...'
        head = AREA_PREFIX_RE.sub("", head).strip()
    return head


def classify(name, notes=""):
    """-> (decision, kind, reason). decision: keep | burial | worship | not_war | pre1858 | post1975.

    kind is what the relic is: a point site whose head is a post/prison/command post/base is a
    military_site even when the name mentions the campaign it served ('Sở chỉ huy tiền phương
    chiến dịch Hồ Chí Minh'); a launcher position is a military_site, not a crash site; an
    air raid is an air_strike; a massacre or a death at a place is a loss_incident. notes: the
    list's 'Ghi chú' cell (it may name the war complex a row belongs to, or date an ancient war)."""
    n = vlower(name)
    key = name_key(name)
    core = re.sub(r"\([^)]*\)", " ", n).strip()  # parentheticals list sub-sites; judge the main name
    if BURIAL_RE.search(core) or GRAVE_RE.search(n) or GRAVE_FOLDED_RE.search(normalise(name)):
        return "burial", None, "burial term in name"
    if key in KNOWN_ON_SITE_BURIAL:
        return "burial", None, KNOWN_ON_SITE_BURIAL[key]
    if WORSHIP_RE.search(core):
        return "worship", None, "temple/communal house"
    if NOT_WAR_RE.search(n):
        return "not_war", None, "false keyword (port/plantation/shrine)"
    # The first rule that matches decides the kind; the order matters: a curated override, then
    # aircraft (a battery before a shoot-down), air raids, point sites, massacres, battles, other
    # war sites, deaths at a place, and last a war complex the list's note names.
    kind = None
    head = relic_head(name)
    if key in WAR_RELIC_OVERRIDE:
        kind, reason = WAR_RELIC_OVERRIDE[key]
    elif AIRCRAFT_RE.search(n) and AIR_DEFENCE_RE.search(n):
        kind, reason = (
            "military_site",
            ("air-defence position (the relic is the battery that fired; where the aircraft fell is not given)"),
        )
    elif AIRCRAFT_RE.search(n):
        kind, reason = "aircraft_crash", "shoot-down site"
    elif AIR_RAID_RE.search(n):
        kind, reason = "air_strike", "air raid"
    elif POINT_SITE_HEAD_RE.match(head) or re.match(r"(căn cứ|chiến khu|an toàn khu|atk\d*)\b", head):
        kind, reason = "military_site", "base/post/prison"
    elif MASSACRE_RE.search(n):
        kind, reason = "loss_incident", "massacre"
    elif BATTLE_RE.search(n):
        kind, reason = "ground_combat", "battle"
    elif SITE_RE.search(core) or (SITE_RE.search(n) and not re.search(r"\(atk\d+\)", n)):
        kind, reason = "military_site", "base/post/prison"
    elif DEATH_RE.search(n):
        kind, reason = "loss_incident", "place where people died"
    elif WAR_COMPLEX_NOTE_RE.search(vlower(notes)):
        complex_ = WAR_COMPLEX_NOTE_RE.search(vlower(notes)).group(0)
        if "trận chiến" in complex_:
            kind, reason = "ground_combat", f"part of the war relic complex named in the list's note ('{complex_}')"
        else:
            kind, reason = (
                "military_site",
                f"supply-route site, part of the relic complex in the list's note ('{complex_}')",
            )
    if not kind:
        return "not_war", None, "no war keyword"
    years = [int(y) for y in YEAR_RE.findall(n)]
    if years and all(y < 1858 for y in years):
        return "pre1858", None, f"years {years}"
    if years and all(y > 1975 for y in years):
        return "post1975", None, f"years {years}"
    if not any(1858 <= y <= 1975 for y in years) and (ANCIENT_RE.search(n) or ANCIENT_RE.search(vlower(notes))):
        return "pre1858", None, "named after a pre-1858 war"
    return "keep", kind, reason


# Areas named by the relic, smallest radius that honestly covers them (m). The position is a
# commune/article point inside the area, which may be anywhere in it.
AREA_FLOOR_M = {
    "war_zone": 30000.0,  # chiến khu / an toàn khu: tens of km of forest and villages
    "campaign": 25000.0,  # chiến dịch, regional uprisings, multi-week operations
    "base_area": 20000.0,  # căn cứ (resistance base area of a province/region committee)
    "battlefield": 10000.0,  # chiến trường: a whole battlefield (Điện Biên Phủ valley ~ 18 x 6 km)
    "route_section": 20000.0,  # several key points along a supply road
    "network": 15000.0,  # a district's system of village tunnels (làng hầm)
    "battle": 8000.0,  # a named battle/victory: the fighting spread beyond one village
}
NETWORK_RE = re.compile(r"hệ thống (?:làng hầm|địa đạo)")
ROUTE_RE = re.compile(r"trọng điểm trên đường|các điểm vượt đường")
# Leading words that make the relic one building/position/post inside an area: no floor.
POINT_HEAD_RE = re.compile(
    r"^(sở chỉ huy|chỉ huy sở|nhà tù|nhà lao|ngục|khu trại giam|trại giam|đồn|bót|"
    r"địa đạo|hầm|hang|sân bay|trận địa|cứ điểm|tập đoàn cứ điểm|pháo đài|chiến lũy|bến|"
    r"cảng|căng|cơ quan|cơ sở|cơ sỏ|ban|thành lập|nơi|ngã ba|ngã tư|làng|cầu|"
    r"thành cổ|vụ|cuộc thảm sát|chứng tích|địa điểm ghi dấu|thảm sát)(?![^\W\d_])"
)
# 'Bộ chỉ huy chiến dịch' / 'BCH Chiến dịch' is a campaign's command, a base: not the campaign itself.
CAMPAIGN_RE = re.compile(
    r"(?<!chỉ huy )(?<!bch )(?:chiến dịch|khởi nghĩa|tổng tiến công|junction city|"
    r"chiến thắng biên giới|đường 7 - sông bờ|bình giã)"
)


def area_class(name, kind=None):
    """A key of AREA_FLOOR_M, or None for a point site (prison, post, tunnel, command post ...)."""
    n = vlower(plain(name))
    head = relic_head(name)
    if NETWORK_RE.search(n):
        return "network"
    if ROUTE_RE.search(n):
        return "route_section"
    if POINT_HEAD_RE.match(head):
        return None
    if re.search(r"chiến khu|an toàn khu|\batk\d*\b", n):
        return "war_zone"
    if re.match(r"căn cứ\b", head):
        return "base_area"
    if kind in ("air_strike", "loss_incident"):
        return None  # a raid or a death at a place: no battle area
    if CAMPAIGN_RE.search(n):
        return "campaign"
    if re.search(r"\bcăn cứ\b", n):
        return "base_area"
    if re.search(r"chiến trường", n):
        return "battlefield"
    if BATTLE_RE.search(n) and not MASSACRE_RE.search(n):
        return "battle"
    return None


# Relics whose name places the event somewhere other than the list's 'Địa bàn' column:
# name_key -> (article giving the event place, explanation, (date_start, date_end) of the event as
# that article gives them). The record is placed between the two places and its radius covers both.
EVENT_ELSEWHERE = {
    "tran chien dau 81 ngay dem tai quang tri": (
        "Thành cổ Quảng Trị",
        "the 81-day battle (28 June - 16 Sept 1972) was fought at the Quảng Trị citadel and town, but the "
        "list's location column gives Bến Quan, Vĩnh Linh, to the north",
        ("1972-06-28", "1972-09-16"),
    ),
}
MY_RE = re.compile(r"(?<![\d/])(\d{1,2})/(1[89]\d\d)(?!\d)")


def event_dates(name):
    """Event date(s) in the relic name, limited to 1858-1975: (start, end) or (None, None)."""
    n = re.split(r"\bgồm\b", vlower(name))[0]  # dates after 'gồm' belong to component sub-sites
    m = DMY_RE.search(n)
    if m and 1858 <= int(m.group(3)) <= 1975:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            return iso, iso
    m = MY_RE.search(n)  # '(12/1972)': month and year
    if m and 1858 <= int(m.group(2)) <= 1975 and 1 <= int(m.group(1)) <= 12 and len(YEAR_RE.findall(n)) == 1:
        iso = f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
        return iso, iso
    years = sorted({int(y) for y in YEAR_RE.findall(n) if 1858 <= int(y) <= 1975})
    if not years:
        return None, None
    return str(years[0]), str(years[-1])


def id_key(name):
    """name_key of the relic's own name: a component list after ':', '*' or a '+' line (ATK Định
    Hóa's 13 sub-sites, the key points of a road, Tà Thiết's commands) is left out, so rewording
    it keeps the id. A head of one or two words ('Hang động: Xã Hoá Thanh ... Xã Hoá Tiến') does
    not identify the relic, so the whole name (without parentheticals) is used then."""
    head = re.split(r"[:*]|\n\s*\+", name_text(name), maxsplit=1)[0]
    k = name_key(head)
    if len(k.split()) <= 2:
        return name_key(name) or k
    return k


def name_text(name_wt):
    """plain() that keeps line breaks (a '+' component on its own line)."""
    return "\n".join(plain(line) for line in (name_wt or "").splitlines())


def name_key(name):
    """Accent-free key of a relic name for lookups and merging: parentheticals and generic
    prefixes ('Khu di tích', 'Nhà ngục' -> 'nguc') removed. Keys of the curated tables."""
    k = normalise(re.sub(r"\([^)]*\)", " ", plain(name)))
    k = re.sub(r"^(khu di tich|di tich lich su|di tich|dia diem|cum di tich|khu)\s+", "", k)
    return re.sub(r"^nha (nguc|lao|tu)\b", r"\1", k)


def merge_key(key):
    """name_key with the list's abbreviations spelt one way: 'An toàn khu (ATK) Chợ Đồn' and
    'ATK Chợ Đồn', 'An toàn khu II' and 'ATK2', 'Căn cứ BCH ...' and 'Căn cứ Bộ chỉ huy ...'."""
    k = re.sub(r"\ban toan khu\b", "atk", key)
    k = re.sub(r"\batk (ii|2)\b", "atk2", k)
    k = re.sub(r"\batk (i|1)\b", "atk1", k)
    k = re.sub(r"\batk (iii|3)\b", "atk3", k)
    k = re.sub(r"\bbch\b", "bo chi huy", k)
    return " ".join(k.split())


# ---- geocoding ------------------------------------------------------------------------------
def km(a, b):
    """Distance in km between two (lon, lat) points (WGS84 degrees), equirectangular on a 6371 km
    sphere: within about 0.5 % at these latitudes and distances, far below the radii stored."""
    (lo1, la1), (lo2, la2) = a, b
    x = math.radians(lo2 - lo1) * math.cos(math.radians((la1 + la2) / 2))
    y = math.radians(la2 - la1)
    return 6371.0 * math.hypot(x, y)


def ceil100(m):
    """Metres rounded up to 100 m: a rounded radius must never shrink below what it has to cover."""
    return float(math.ceil(m / 100 - 1e-9) * 100)


def centroid(points):
    """-> (lon, lat, spread_m): the mean of (lon, lat) points and the distance to the farthest one."""
    lon = sum(p[0] for p in points) / len(points)
    lat = sum(p[1] for p in points) / len(points)
    return lon, lat, max(km((lon, lat), p) for p in points) * 1000.0


def _wkt_point(s):
    """Wikidata 'Point(lon lat)' WKT literal -> (lon, lat), or None."""
    m = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", s or "")
    return (round(float(m.group(1)), 6), round(float(m.group(2)), 6)) if m else None


def parse_district_members(data):
    """Wikidata SPARQL answer -> {qid: {'label', 'point', 'members': [(lon, lat)]}}: the old
    communes Wikidata places in each district (a commune with two coordinates counts twice)."""
    if not isinstance(data, dict) or "results" not in data:
        raise RuntimeError(f"Wikidata district query failed: {str(data)[:300]!r}; nothing written, re-run later")
    if not data["results"].get("bindings"):
        raise RuntimeError("Wikidata district query returned no rows; nothing written, re-run later")
    out = {}
    for b in data["results"]["bindings"]:
        q = b["d"]["value"].rsplit("/", 1)[-1]
        d = out.setdefault(q, {"label": None, "point": None, "members": []})
        d["label"] = d["label"] or (b.get("dl") or {}).get("value")
        d["point"] = d["point"] or _wkt_point((b.get("dc") or {}).get("value"))
        p = _wkt_point((b.get("cc") or {}).get("value"))
        if p and p not in d["members"]:
            d["members"].append(p)
    return out


def check_district_members(districts):
    """A whole-country answer, not a truncated one: without most districts the commune-in-district
    check and the district sizes silently fall back to GNS spacing alone."""
    n_members = sum(len(d["members"]) for d in districts.values())
    if len(districts) < MIN_WD_DISTRICTS or n_members < MIN_WD_MEMBERS:
        raise RuntimeError(
            f"Wikidata district query: only {len(districts)} districts / {n_members} communes "
            f"(expected >= {MIN_WD_DISTRICTS} / {MIN_WD_MEMBERS}); nothing written, re-run later"
        )
    return districts


def filter_members(d):
    """A district's member points without Wikidata errors (a namesake commune or a wrong
    coordinate hundreds of km away): the points linked to the one nearest the district's own
    coordinate by hops of at most MEMBER_LINK_KM, within MEMBER_MAX_KM of it."""
    pts = d["members"]
    if not pts:
        return []
    ref = d["point"] or (sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2])
    seed = min(pts, key=lambda p: km(p, ref))
    keep, frontier = {seed}, [seed]
    while frontier:
        a = frontier.pop()
        for b in pts:
            if b not in keep and km(a, b) <= MEMBER_LINK_KM and km(b, seed) <= MEMBER_MAX_KM:
                keep.add(b)
                frontier.append(b)
    return [p for p in pts if p in keep]


def fetch_district_members(refresh=False):
    """Wikidata district -> commune membership (one cached SPARQL query); any failure raises."""
    path = cached_get(
        WDQS,
        SOURCE,
        name="wdqs_district_communes.json",
        refresh=refresh,
        min_interval=3.0,
        timeout=120,
        data=urllib.parse.urlencode({"query": DISTRICT_QUERY}),
        headers={"Accept": "application/sparql-results+json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            f"Wikidata district query: unreadable answer ({err}); nothing written, re-run later"
        ) from err
    try:
        return check_district_members(parse_district_members(data))
    except RuntimeError:
        path.unlink(missing_ok=True)
        raise


def parse_province_borders(data):
    """Overpass answer -> {ISO 3166-2 code: [(lon, lat)]}: every vertex of each province's land border."""
    if not isinstance(data, dict) or data.get("remark") or not data.get("elements"):
        raise RuntimeError(f"Overpass province border query failed: {str(data)[:300]!r}; nothing written, re-run later")
    ways = {e["id"]: e for e in data["elements"] if e["type"] == "way"}
    out = {}
    for r in data["elements"]:
        code = r["type"] == "relation" and (r.get("tags") or {}).get("ISO3166-2")
        if not code:
            continue
        pts = out.setdefault(code, [])
        for m in r.get("members", []):
            if m["type"] == "way" and m["ref"] in ways:
                pts += [(g["lon"], g["lat"]) for g in ways[m["ref"]].get("geometry") or []]
    out = {c: p for c, p in out.items() if p}
    if not out:
        raise RuntimeError("Overpass province border query: no province geometry; nothing written, re-run later")
    return out


def check_province_borders(borders):
    """Every pre-2025 province the lists can name must have its land border."""
    need = {c for cs in PROVINCE_CODES.values() for c in cs} | set(OLD_PROVINCE_CODE.values())
    missing = sorted(need - set(borders))
    if missing:
        raise RuntimeError(f"Overpass province borders missing for {', '.join(missing)}; nothing written, re-run later")
    return borders


def fetch_province_borders(refresh=False):
    """Pre-2025 province land borders (one cached Overpass query); any failure raises."""
    path = cached_get(
        OVERPASS,
        SOURCE,
        name="osm_provinces_2025_land.json",
        refresh=refresh,
        min_interval=5.0,
        timeout=600,
        data=urllib.parse.urlencode({"data": PROVINCE_BORDER_QUERY}),
    )
    try:
        return check_province_borders(parse_province_borders(json.loads(path.read_text(encoding="utf-8"))))
    except (ValueError, RuntimeError) as err:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"province borders: {err}") from err


def grid_cell(pt):
    """Grid cell (GRID_PER_DEG cells per degree) holding a (lon, lat) point."""
    return math.floor(pt[0] * GRID_PER_DEG), math.floor(pt[1] * GRID_PER_DEG)


class Gazetteer:
    """GNS admin lookups scoped to a post-2025 province.

    index: gns_index.json ({'names': {normalised name: [GNS feature]}}, pre-2025 names and
    ADM1 codes). districts: parse_district_members() (Wikidata commune membership of the pre-2025
    districts), used to size districts and to check that a commune lies in the district a cell
    names; without it both fall back to GNS spacing. borders: parse_province_borders() (pre-2025
    province land-border vertices): a province radius reaches every one of them.

    provinces / old_provinces: {province key: (lon, lat, radius_m)} for the post-2025 provinces
    and the pre-2025 ones merged away; scope: {province key: (lon, lat, radius_m)}, a tighter
    circle used to decide whether an unassigned feature or an article lies in the province."""

    def __init__(self, index, districts=None, borders=None):
        self.names = index["names"]
        self.provinces, self.old_provinces, self.scope = {}, {}, {}
        adm2 = defaultdict(list)
        for feats in self.names.values():
            for f in feats:
                if f["dsg"] == "ADM2":
                    adm2[f["adm1"]].append((f["lon"], f["lat"]))
        for prov, codes in list(PROVINCE_CODES.items()) + [(k, [c]) for k, c in OLD_PROVINCE_CODE.items()]:
            pts = list({p for c in codes for p in adm2.get(c, [])})
            if not pts:
                continue
            lon, lat, spread = centroid(pts)
            target = self.old_provinces if prov in OLD_PROVINCE_CODE else self.provinces
            target[prov] = (round(lon, 6), round(lat, 6), spread)
            # "is this unassigned (VN-000) GNS feature / this article in the province": a tighter
            # test than the radius below, which reaches into neighbouring provinces
            self.scope[prov] = (round(lon, 6), round(lat, 6), round(spread + 15000.0, -3))
        # ADM3 (communes) and ADM2 (districts), one entry per GNS feature (the index lists a
        # feature once under each of its names); communes also bucketed on the lookup grid
        self.adm3 = list({f["ufi"]: f for fs in self.names.values() for f in fs if f["dsg"] == "ADM3"}.values())
        self.adm2 = list({f["ufi"]: f for fs in self.names.values() for f in fs if f["dsg"] == "ADM2"}.values())
        self.adm2_by_adm1 = defaultdict(dict)
        for f in self.adm2:
            self.adm2_by_adm1[f["adm1"]][f["ufi"]] = f
        self.grid = defaultdict(list)
        for f in self.adm3:
            self.grid[grid_cell((f["lon"], f["lat"]))].append(f)
        # memoised commune_extent / district_extent / wd_district, keyed by GNS ufi
        self._cext, self._dext, self._wd = {}, {}, {}
        # Province radius: the farthest edge of its GNS communes from the centre (not the spread of
        # district points, which leaves out the outer communes).
        by_code = defaultdict(list)
        for f in self.adm3:
            by_code[f["adm1"]].append(f)
        for table, codes_of in (
            (self.provinces, lambda k: PROVINCE_CODES[k]),
            (self.old_provinces, lambda k: [OLD_PROVINCE_CODE[k]]),
        ):
            for prov, (lon, lat, spread) in list(table.items()):
                need = max(
                    [
                        km((lon, lat), (c["lon"], c["lat"])) * 1000 + self.commune_extent(c)
                        for code in codes_of(prov)
                        for c in by_code.get(code, [])
                    ]
                    + [spread + UNC_DISTRICT]
                    + [km((lon, lat), p) * 1000 for code in codes_of(prov) for p in (borders or {}).get(code, ())]
                )
                table[prov] = (lon, lat, ceil100(need))
        # Wikidata districts by name, their cleaned member points, and a grid of all members
        self.wd = {q: dict(d, members=filter_members(d)) for q, d in (districts or {}).items()}
        self.wd_by_name = defaultdict(list)
        self.wd_grid = defaultdict(list)
        for q, d in self.wd.items():
            if d["label"]:
                self.wd_by_name[admin_key(d["label"])].append(q)
            for p in d["members"]:
                self.wd_grid[grid_cell(p)].append((p, q))

    def lookup(self, name, province, dsgs, nearby_km=0.0):
        """Features named `name` (accent-exact preferred) with dsg in dsgs inside the province
        (dsgs None: any designation). nearby_km > 0: when the province has none, features of
        neighbouring provinces within that distance of the province's edge (a relic that spills
        over a provincial border names places on both sides)."""
        codes = set(PROVINCE_CODES.get(province, []))
        # strip an admin prefix on the accented text: 'Quận 5' -> '5' but 'Quán Hàu' stays
        want = ADMIN_PREFIX_RE.sub("", vlower(re.sub(r"\s*\(.*?\)\s*", " ", name)).strip()).strip()
        feats = [f for f in self.names.get(normalise(want), []) if dsgs is None or f["dsg"] in dsgs]
        pc = self.scope.get(province)
        scoped = [
            f
            for f in feats
            if f["adm1"] in codes or (f["adm1"] == "VN-000" and pc and km((f["lon"], f["lat"]), pc[:2]) * 1000 <= pc[2])
        ]
        if not scoped and nearby_km and pc:
            scoped = [
                f
                for f in feats
                if f["adm1"] != "VN-000" and km((f["lon"], f["lat"]), pc[:2]) * 1000 <= pc[2] + nearby_km * 1000
            ]
        # GNS names carry a capitalised admin word, sometimes misspelt 'Huyên'
        exact = [
            f
            for f in scoped
            if vlower(re.sub(r"^(Xã|Phường|Thị Trấn|Huyện|Huyên|Quận|Thị Xã|Thành Phố)\s+", "", f["name"])) == want
        ]
        return exact or scoped

    def nearest_communes(self, pt, k, exclude=None):
        """[(km, feature)] for the k GNS communes nearest pt (exclude: a ufi to leave out)."""
        cx, cy = grid_cell(pt)
        r = 1
        while True:
            cands = sorted(
                (
                    (km(pt, (f["lon"], f["lat"])), f)
                    for dx in range(-r, r + 1)
                    for dy in range(-r, r + 1)
                    for f in self.grid.get((cx + dx, cy + dy), ())
                    if f["ufi"] != exclude
                ),
                key=lambda x: x[0],
            )
            # every commune within r * GRID_CELL_KM has been seen, so the k nearest are final once
            # the k-th lies inside that distance; 12 cells (300 km) bounds the search offshore
            if (len(cands) >= k and cands[k - 1][0] <= r * GRID_CELL_KM) or r >= 12:
                return cands[:k]
            r += 1

    def communes_near(self, pt, radius_km):
        """GNS communes within radius_km of pt."""
        (cx, cy), r = grid_cell(pt), math.ceil(radius_km / GRID_CELL_KM)
        return [
            f
            for dx in range(-r, r + 1)
            for dy in range(-r, r + 1)
            for f in self.grid.get((cx + dx, cy + dy), ())
            if km(pt, (f["lon"], f["lat"])) <= radius_km
        ]

    def commune_extent(self, f):
        """Half-size of a GNS commune (m), from how far apart communes are around it: 3 km in the
        delta, 10+ km for a mountain commune. Never below UNC_COMMUNE."""
        if f["ufi"] not in self._cext:
            # distances to the 6 nearest other communes; one within 300 m is the same place listed
            # twice, not a neighbour
            ds = [d for d, g in self.nearest_communes((f["lon"], f["lat"]), 7, exclude=f["ufi"]) if d > 0.3][:6]
            median = (ds[len(ds) // 2 - 1] + ds[len(ds) // 2]) / 2 if len(ds) >= 2 else (ds[0] if ds else 0)
            self._cext[f["ufi"]] = max(UNC_COMMUNE, round(COMMUNE_SPACING_FACTOR * median * 1000, -2))
        return self._cext[f["ufi"]]

    def merged_extent(self, pt):
        """Radius (m) around pt covering a post-2025 commune that contains pt: the MERGED_PARTS old
        communes nearest pt, each with its own size. Never below UNC_COMMUNE_WIKI."""
        need = max(
            (d * 1000 + self.commune_extent(g) for d, g in self.nearest_communes(pt, MERGED_PARTS)),
            default=UNC_COMMUNE_WIKI,
        )
        return max(UNC_COMMUNE_WIKI, ceil100(need))

    def wd_district(self, f):
        """The Wikidata district matching GNS ADM2 feature f (same name, nearest, within 30 km), or None."""
        if f["ufi"] not in self._wd:
            key = admin_key(f["name"])
            fp = (f["lon"], f["lat"])
            best = None
            for q in self.wd_by_name.get(key, []):
                d = self.wd[q]
                pts = ([d["point"]] if d["point"] else []) + d["members"]
                dist = min((km(fp, p) for p in pts), default=1e9)
                if dist <= 30 and (best is None or dist < best[0]):
                    best = (dist, q)
            self._wd[f["ufi"]] = best[1] if best else None
        return self._wd[f["ufi"]]

    def district_members(self, f):
        """Cleaned Wikidata commune points of GNS district f ([] when Wikidata has no match)."""
        q = self.wd_district(f)
        return self.wd[q]["members"] if q else []

    def district_extent(self, f):
        """Half-size of a GNS district (m), an estimate (GNS has no boundaries): the farthest edge of
        its communes - those Wikidata places in it, and those nearer to its GNS point than to any
        other district point of its province. Never below UNC_DISTRICT."""
        if f["ufi"] not in self._dext:
            fp = (f["lon"], f["lat"])
            peers = list(self.adm2_by_adm1.get(f["adm1"], {}).values()) or [f]
            need = 0.0
            for c in self.communes_near(fp, MEMBER_MAX_KM):
                if c["adm1"] != f["adm1"]:
                    continue
                p = (c["lon"], c["lat"])
                if min(peers, key=lambda g: km(p, (g["lon"], g["lat"])))["ufi"] == f["ufi"]:
                    need = max(need, km(p, fp) * 1000 + self.commune_extent(c))
            for p in self.district_members(f):
                near = self.nearest_communes(p, 1)
                ext = self.commune_extent(near[0][1]) if near else UNC_COMMUNE
                need = max(need, km(p, fp) * 1000 + ext)
            self._dext[f["ufi"]] = max(UNC_DISTRICT, ceil100(need))
        return self._dext[f["ufi"]]

    def in_district(self, pt, f, name=None):
        """Could point pt (a commune) lie in district f (named `name` in the cell)? False only when
        two independent sources agree it lies in another district: (a) another GNS district point is
        nearer to pt than f's by over DISTRICT_MARGIN_KM, and (b) the nearest commune Wikidata places
        in any district (within MEMBER_EVIDENCE_KM of pt) belongs to another district, with no
        commune of the named district within 2 km. Districts renamed or split since (a list may use
        the old name) blur both, hence both are required; a district split off from the named one
        that keeps its name ('Bắc Tân Uyên' from 'Tân Uyên') counts as the named one.
        Also False when (a) holds and pt lies beyond the named district's whole estimated extent
        (a namesake tens of km away with no Wikidata commune near it): the cell then falls back to
        the district's own circle, never to a narrower one."""
        fp = (f["lon"], f["lat"])
        d_named = km(pt, fp)
        if not any(
            km(pt, (g["lon"], g["lat"])) + DISTRICT_MARGIN_KM < d_named
            for g in self.adm2
            if g["ufi"] != f["ufi"] and (g["lon"], g["lat"]) != fp
        ):
            return True
        if d_named * 1000 > self.district_extent(f):
            return False
        key = admin_key(name or f["name"])
        named = set(self.wd_by_name.get(key, [])) | ({self.wd_district(f)} - {None})
        # one cell each way reaches well past MEMBER_EVIDENCE_KM
        cx, cy = grid_cell(pt)
        near = sorted(
            (km(pt, p), q)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for p, q in self.wd_grid.get((cx + dx, cy + dy), ())
        )
        near = [(d, q) for d, q in near if d <= MEMBER_EVIDENCE_KM]
        if not near:
            return True
        if any(q in named and d <= 2.0 for d, q in near):
            return True
        q = near[0][1]
        other = admin_key(self.wd[q]["label"] or "")
        return q in named or other.endswith(" " + key) or other.startswith(key + " ")


def place_parts(place_wt):
    """Split an 'Địa bàn' cell into (name, hint, link target, segment index).

    Commas separate levels (commune, district, province); '&', 'và', ' - ' join peers within a
    level. hint: 'district' / 'commune' from a prefix or the link target's '(huyện)'-style
    disambiguator, 'province' for 'tỉnh X', else None. A cell opening with 'Các xã:' is a list
    of communes, so every segment is a commune.
    """
    s = re.sub(r"\{\{[^{}]*\}\}", " ", place_wt)
    s = re.sub(r"<br\s*/?>", ",", s, flags=re.I).strip().lstrip("|").strip()
    # '[[Mỹ Thủy|Hải An, Hải Lăng]]': a link whose text spans levels -> '[[Mỹ Thủy|Hải An]], Hải Lăng'
    s = re.sub(r"\[\[([^\]|]*)\|([^\],]*),([^\]]*)\]\]", r"[[\1|\2]],\3", s)
    stash = []

    def hide(m):
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    s = LINK_RE.sub(hide, s)  # so commas inside [[Mỹ Lợi, Đồng Tháp|Mỹ Lợi]] do not split
    all_communes = bool(re.match(r"(?i)^\s*các\s+(xã|phường)", s))
    s = re.sub(r"(?i)^\s*các\s+(xã|phường)[^:]*:", "", s)
    s = re.sub(r"\([^)\x00]*\)", " ", s)
    out = []
    for si, seg in enumerate(re.split(r",|;|\.\.\.|\n", s)):
        for peer in re.split(r"&| và | - | – |\+", seg):
            peer = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], peer).strip()
            lk = links(peer)
            target = lk[0][0] if lk else None
            txt = plain(peer).strip(" .:")
            if not txt:
                continue
            low, tlow = vlower(txt), vlower(target or "")
            if re.match(r"^tỉnh\s", low):
                hint = "province"
            elif DISTRICT_PREFIX_RE.match(low) or re.search(r"\((huyện|quận|thị xã|thành phố)\)", tlow):
                hint = "district"
            elif COMMUNE_PREFIX_RE.match(low) or re.search(r"\((xã|phường|thị trấn)\)", tlow) or all_communes:
                hint = "commune"
            else:
                hint = None
            name = ADMIN_PREFIX_RE.sub("", txt).strip(" .")
            if name:
                out.append((name, hint, target, si))
    return out


def enclose(circles):
    """(lon, lat, radius_m) of a circle covering every (lon, lat, r) circle: centred on the middle of
    their bounding box (close to the smallest enclosing circle for a few places)."""
    lon = (min(c[0] for c in circles) + max(c[0] for c in circles)) / 2
    lat = (min(c[1] for c in circles) + max(c[1] for c in circles)) / 2
    return lon, lat, max(km((lon, lat), c[:2]) * 1000 + c[2] for c in circles)


def admin_geocode(gaz, place_wt, province, wiki_coords):
    """-> dict(level, datum, lon, lat, unc, matched, notes, circles) from GNS (+ commune articles),
    or None. circles: [(lon, lat, r)] of every place located; the radius covers all of them.

    Peers ('X & Y', 'X và Y') are all covered, whatever their level; a district or province given
    after the communes (', Bố Trạch') is their parent and only matters when a commune cannot be
    found, in which case the radius covers that parent (the missing commune lies somewhere in it)."""
    pc = gaz.provinces.get(province)
    parts = place_parts(place_wt)
    communes, districts, failed, notes = [], [], [], []

    def wiki_point(target):
        w = wiki_coords.get(target) if target else None
        tkey = normalise(re.sub(r"\(.*?\)", "", target or ""))
        if "(tỉnh)" in vlower(target or "") or tkey in PROVINCE_CODES or tkey in OLD_PROVINCE_TO_NEW:
            return None  # a province article, not a commune
        sc = gaz.scope.get(province)
        if w and sc and km((w["lon"], w["lat"]), sc[:2]) * 1000 <= sc[2]:
            return w
        return None

    n_segs = max((p[3] for p in parts), default=0) + 1
    for name, hint, target, si in parts:
        if hint == "province" or (si > 0 and province_key(name) == province):
            continue  # the province itself ('..., thành phố Đà Nẵng')
        # Which levels to try, in order. 'X, District' -> X is a commune; a lone name is usually a
        # district/town ('Nho Quan', 'Bắc Sơn'); a middle segment may be more peer communes.
        if hint == "commune":
            order = "cd"
        elif hint == "district":
            order = "dc"  # 'Tx. An Thới' is a town GNS has as a commune
        elif si == 0:
            order = "dc" if n_segs == 1 else "cd"
        else:
            order = "dc"  # a district usually; else more communes ('An Điền, An Tây, Phú An')
        found = False
        for level in order:
            if level == "c":
                fs = gaz.lookup(name, province, {"ADM3"})
                w = wiki_point(target)
                if fs or w:
                    communes.append({"name": name, "feats": fs, "wiki": w, "si": si})
                    found = True
                    break
            else:
                d = gaz.lookup(name, province, {"ADM2"})
                if not d:  # a relic spilling into the next province ('Hiệp Hòa, Phổ Yên & Phú Bình')
                    d = gaz.lookup(name, province, {"ADM2"}, nearby_km=30)
                    if d:
                        notes.append(f"district '{name}' is in a neighbouring province (GNS {d[0]['adm1']})")
                if d:
                    districts.append({"name": name, "feats": d, "si": si})
                    found = True
                    break
        if not found and not target and "c" in order:
            # an unlinked commune GNS lacks: its vi.wikipedia title, which for an old commune
            # redirects to the post-2025 commune that absorbed it ('Hải Trạch' -> 'Đông Trạch')
            w = wiki_point(name)
            if w:
                communes.append({"name": name, "feats": [], "wiki": w, "si": si, "by_name": True})
                notes.append(f"'{name}' not in GNS; located by its vi.wikipedia title ('{w['page']}')")
                found = True
        if not found:
            level = "commune" if order[0] == "c" else "district"
            notes.append(f"no GNS {level} '{name}'")
            failed.append({"name": name, "si": si, "level": level})
    # A commune named with its district ('Phường 2, Đông Hà') must lie in that district: a GNS
    # namesake or an article elsewhere in the (merged) province is another place.
    for c in list(communes):
        parents = [(f, d["name"]) for d in districts if d["si"] > c["si"] for f in d["feats"]]
        if not parents:
            continue
        inside = [f for f in c["feats"] if any(gaz.in_district((f["lon"], f["lat"]), d, n) for d, n in parents)]
        for f in c["feats"]:
            if f not in inside:
                notes.append(
                    f"GNS '{f['name']}' (ufi {f['ufi']}) lies in another district, not in "
                    + " / ".join(sorted({f"'{n}'" for _, n in parents}))
                    + "; not used"
                )
        c["feats"] = inside
        if c["wiki"] and not any(gaz.in_district((c["wiki"]["lon"], c["wiki"]["lat"]), d, n) for d, n in parents):
            notes.append(
                f"vi.wikipedia '{c['wiki']['page']}' for '{c['name']}' lies outside the district named; ignored"
            )
            c["wiki"] = None
        if not c["feats"] and not c["wiki"]:
            communes.remove(c)
            failed.append({"name": c["name"], "si": c["si"], "level": "commune"})
    dpoints = [(f["lon"], f["lat"]) for d in districts for f in d["feats"]]
    dcenter = centroid(dpoints)[:2] if dpoints else None
    # A post-2025 cell names the (merged) commune alone, with no district: a GNS match is then the
    # pre-2025 namesake, one part of a commune about MERGED_PARTS times its size.
    post2025 = (
        not districts
        and not any(h == "district" for _, h, _, _ in parts)
        and not any(f["level"] == "district" for f in failed)
        and len({si for _, h, _, si in parts if h != "province"}) == 1
    )

    circles, matched, kinds = [], [], set()
    for c in communes:
        fs = c["feats"]
        # Same-name communes in one province: prefer the one nearest the commune's own
        # article coordinate, else those inside the named district.
        if fs and c["wiki"]:
            wp = (c["wiki"]["lon"], c["wiki"]["lat"])
            best = min(fs, key=lambda f: km((f["lon"], f["lat"]), wp))
            if km((best["lon"], best["lat"]), wp) <= 15:
                if len(fs) > 1:
                    notes.append(f"'{c['name']}' disambiguated by vi.wikipedia '{c['wiki']['page']}'")
                fs = [best]
            else:  # GNS namesake is elsewhere in the (merged) province: trust the linked commune
                notes.append(f"GNS '{c['name']}' >15 km from vi.wikipedia '{c['wiki']['page']}'; used the article")
                fs = []
        if len(fs) > 1 and dcenter:
            fs = [f for f in fs if km((f["lon"], f["lat"]), dcenter) <= 30] or fs
        if fs:
            for f in fs:
                p = (f["lon"], f["lat"])
                if post2025:
                    circles.append((*p, gaz.merged_extent(p)))
                else:
                    circles.append((*p, gaz.commune_extent(f)))
            matched += [f"{f['name']} ({f['adm1']}, ufi {f['ufi']})" for f in fs]
            if post2025:
                notes.append(
                    f"'{c['name']}' is a post-2025 (merged) commune; GNS has its pre-2025 namesake "
                    f"{fs[0]['name']}, so the radius covers the {MERGED_PARTS} old communes nearest it"
                )
            if len(fs) > 1:
                notes.append(f"'{c['name']}' ambiguous: {len(fs)} GNS communes, radius covers all")
            kinds.add("commune")
        else:
            p = (c["wiki"]["lon"], c["wiki"]["lat"])
            circles.append((*p, gaz.merged_extent(p)))
            matched.append(f"vi.wikipedia '{c['wiki']['page']}' (post-2025 commune article)")
            kinds.add("commune")

    # A district after the other places ('Hải Trạch & Thanh Trạch, Bố Trạch') is their parent: the
    # places found lie inside it, and it covers those that could not be found.
    def parent_of(d, items):
        return [x for x in items if x["si"] < d["si"]]

    covered_failed = set()
    for d in districts:
        below = parent_of(d, communes + failed)
        if below:
            lost = parent_of(d, failed)
            if not lost:
                continue  # the communes found lie inside it
            covered_failed |= {id(x) for x in lost}
            if communes:
                notes.append(
                    f"radius covers district '{d['name']}' because "
                    + ", ".join(f"'{x['name']}'" for x in lost)
                    + " could not be located in it"
                )
        elif communes:
            notes.append(f"district '{d['name']}' is named beside the communes: radius covers it")
        circles += [(f["lon"], f["lat"], gaz.district_extent(f)) for f in d["feats"]]
        matched += [f"{f['name']} ({f['adm1']}, ufi {f['ufi']})" for f in d["feats"]]
        kinds.add("district")
    uncovered = [x for x in failed if id(x) not in covered_failed]
    if circles and uncovered and pc:
        # a named place that could not be found, with no district around it: somewhere in the province
        circles.append(pc)
        matched.append(f"province {province} = GNS {'+'.join(PROVINCE_CODES[province])}")
        notes.append(
            "radius covers the province because "
            + ", ".join(f"'{x['name']}'" for x in uncovered)
            + " could not be located"
        )
        kinds.add("province")
    if circles:
        lon, lat, unc = enclose(circles)
        level = (
            "province"
            if "province" in kinds
            else "commune"
            if kinds == {"commune"}
            else "district"
            if kinds == {"district"}
            else "commune+district"
        )
        if len(circles) == 1:
            datum = {"commune": "commune-centroid", "province": "province-centroid"}.get(level, "district-centroid")
        else:
            # the middle of a bounding box around several places: a point in none of them
            datum = "enclosing-circle"
        return {
            "level": level,
            "datum": datum,
            "lon": lon,
            "lat": lat,
            "unc": ceil100(unc),
            "matched": matched,
            "notes": notes,
            "circles": circles,
        }
    # nothing found below province level: a cell naming a pre-2025 province ('Quảng Bình') gets its circle
    for name, _hint, _target, _si in parts:
        op = gaz.old_provinces.get(normalise(name))
        if op:
            return {
                "level": "province",
                "datum": "province-centroid",
                "lon": op[0],
                "lat": op[1],
                "unc": op[2],
                "matched": [f"pre-2025 province {name} = GNS {OLD_PROVINCE_CODE[normalise(name)]}"],
                "notes": notes,
                "circles": [op],
            }
    if not pc:
        return None
    return {
        "level": "province",
        "datum": "province-centroid",
        "lon": pc[0],
        "lat": pc[1],
        "unc": pc[2],
        "matched": [f"province {province} = GNS {'+'.join(PROVINCE_CODES[province])}"],
        "notes": notes,
        "circles": [pc],
    }


# ---- Wikipedia coordinates (batched API) ----------------------------------------------------
def parse_coords(data):
    """API 'prop=coordinates' answer -> {title or redirect/normalised alias: {lon, lat, page}}."""
    out = {}
    query = data.get("query", {})
    alias = {}
    for kind in ("normalized", "redirects"):
        for r in query.get(kind, []):
            alias[r["to"]] = alias.get(r["to"], []) + [r["from"]]
    for p in query.get("pages", []):
        co = (p.get("coordinates") or [None])[0]
        if not co or co.get("globe", "earth") != "earth":
            continue
        rec = {"lon": round(co["lon"], 6), "lat": round(co["lat"], 6), "page": p["title"]}
        names, frontier = {p["title"]}, [p["title"]]
        while frontier:
            for a in alias.get(frontier.pop(), []):
                if a not in names:
                    names.add(a)
                    frontier.append(a)
        for n in names:
            out[n] = rec
    return out


def fetch_coords(titles, refresh=False):
    """{title as linked: {'lon','lat','page'}} for primary coordinates, 50 titles per request."""
    titles = sorted({t for t in titles if t})
    out = {}
    for i in range(0, len(titles), 50):
        batch = titles[i : i + 50]
        q = urllib.parse.urlencode(
            {
                "action": "query",
                "format": "json",
                "formatversion": "2",
                "redirects": "1",
                "prop": "coordinates",
                "coprimary": "primary",
                "colimit": "max",
                "maxlag": "5",
                "titles": "|".join(batch),
            }
        )
        name = "coords_" + hashlib.sha1("|".join(batch).encode()).hexdigest()[:12] + ".json"
        data = json.loads(cached_get(f"{API}?{q}", SOURCE, name=name, refresh=refresh).read_text(encoding="utf-8"))
        if "error" in data or "query" not in data:
            # e.g. maxlag: drop the cache file so a re-run retries, and stop - writing the store
            # without these coordinates would silently move records to coarser centroids
            (store.ROOT / "data" / "raw" / SOURCE / name).unlink(missing_ok=True)
            raise RuntimeError(
                f"vi.wikipedia coordinates API failed for batch {name}: "
                f"{data.get('error', data)!r:.300}; nothing written, re-run later"
            )
        out.update(parse_coords(data))
    # titles are matched as linked; first letter case-insensitive on Wikipedia
    for t in titles:
        if t not in out:
            u = t[:1].upper() + t[1:]
            if u in out:
                out[t] = out[u]
    return out


def wiki_url(title):
    """vi.wikipedia URL of an article title."""
    return f"{WIKI}/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ---- places named in the relic name ---------------------------------------------------------
BIG_CITY_RE = r"(?:thành phố|tp\.?)\s*(?:hải phòng|hồ chí minh|huế|đà nẵng|cần thơ|hà nội)"


def is_multi_province(place_wt):
    """Routes spanning several provinces (Trường Sơn trail, sea trail) have no meaningful point."""
    p = vlower(plain(place_wt))
    return "các tỉnh" in p or len(re.findall(r"\btỉnh\b|" + BIG_CITY_RE, p)) >= 2


TITLE_ADMIN_RE = re.compile(r"(?i)\b(xã|phường|thị trấn|huyện|thị xã|thành phố|quận)\s+")


def title_places(name):
    """[(level, mention, place)] for communes/districts the relic's own name names ('Hang động: – Xã
    Hoá Thanh (...) – Xã Hoá Tiến (...)', '... tại thành phố Nam Định'): capitalised words after
    an admin word."""
    text = plain(name)
    out = []
    for m in TITLE_ADMIN_RE.finditer(text):
        words = []
        for w in text[m.end() :].split():
            core = w.strip("()[],;:.–-")
            if not core or not core[0].isupper() or len(words) >= 4:
                break
            words.append(core)
            if core != w.rstrip(".") and re.search(r"[,;:()\]–]", w):
                break
        if words:
            level = "commune" if vlower(m.group(1)) in ("xã", "phường", "thị trấn") else "district"
            out.append((level, f"{m.group(1)} {' '.join(words)}", " ".join(words)))
    return out


# Generic words opening a road key point ('Ngầm X' ford, 'Đèo X' pass): tried without them when
# the full name is not in GNS.
GENERIC_PART_RE = re.compile(r"(?i)^(ngầm|đèo|đồi|bến|cầu|hang|bãi|dốc|ngã ba|trọng điểm)\s+")


def route_components(name):
    """Named key points of a road relic ('Các trọng điểm trên đường 12A: – Bãi Dinh – Đồi 37,Cha Lo,
    ...'; '... (bao gồm các cầu Khe Xom, Xom Rò, Du Tiên)')."""
    text = plain(name)
    if ":" in text:
        body = text.split(":", 1)[1]
    else:
        m = re.search(r"\(bao gồm (?:các (?:cầu|điểm) )?([^)]*)\)", text, flags=re.I)
        body = m.group(1) if m else ""
    out = []
    for part in re.split(r"[–;,]|\.\.\.|(?:^|\s)-", body):
        p = re.sub(r"(?i)^km\s*[\d.,]+\s*", "", part.strip()).strip()
        p = re.sub(r"(?i)^trọng điểm\s+", "", p).strip(" .")
        if p:
            out.append(p)
    return out


def locate_components(gaz, province, comps):
    """-> ([(lon, lat, r)], [label], [not located]) for road key points, from GNS (any feature
    type) in the province. A name with a number ('Đồi 37', 'hang 8 TNXP') is not a gazetteer name."""
    circles, labels, missing = [], [], []
    for c in comps:
        fs = [] if re.search(r"\d", c) else gaz.lookup(c, province, None)
        if not fs and GENERIC_PART_RE.match(c) and not re.search(r"\d", c):
            fs = gaz.lookup(GENERIC_PART_RE.sub("", c), province, None)
        if not fs:
            missing.append(c)
            continue
        for f in fs:
            circles.append((f["lon"], f["lat"], max(float(f.get("unc") or 0), ROUTE_POINT_MIN_M)))
        labels.append(
            f"'{c}' = GNS "
            + "; ".join(f"{f['name']} ({f['dsg']}, ufi {f['ufi']})" for f in fs)
            + (" - accents differ" if all(vlower(f["name"]) != vlower(c) for f in fs) else "")
        )
    return circles, labels, missing


# The same for places a relic name lists, plus ferries and caves (bến phà, phà, động).
COMPONENT_GENERIC_RE = re.compile(
    r"(?i)^(bến phà|bến đò|phà|ngầm|đèo|đồi|bến|cầu|hang|động|bãi|dốc|ngã ba|"
    r"trọng điểm)\s+"
)


def locate_named_components(gaz, province, name_wt, wiki_coords, near, max_km=ARTICLE_MAX_KM):
    """-> ([(lon, lat, r)], [label], [not located]) for the places a relic name lists ('Khu vực: –
    Bến phà Xuân Sơn – [[Động Phong Nha]]', '... (bao gồm [[Cầu Hiền Lương]], nhà liên hiệp ...)'):
    a linked article's coordinate, else a GNS feature of that name (any type; the full name, e.g.
    'Cầu Hiền Lương', preferred over the bare 'Hiền Lương') within max_km of `near` (the relic is one
    complex: a namesake elsewhere in the merged province is another place). Radius at least
    COMPONENT_MIN_M: a ferry or a bridge with the posts on both banks."""
    linked = {plain(f"[[{t}|{x}]]" if x else f"[[{t}]]").strip(): t for t, x in links(name_wt)}
    circles, labels, missing = [], [], []
    for comp in route_components(name_wt):
        art = wiki_coords.get(linked.get(comp, ""))
        if art and km((art["lon"], art["lat"]), near) <= max_km:
            circles.append((art["lon"], art["lat"], UNC_ARTICLE))
            labels.append(f"'{comp}' = vi.wikipedia '{art['page']}'")
            continue
        if re.search(r"\d", comp):
            missing.append(comp)
            continue
        fs = []
        for cand in (comp, COMPONENT_GENERIC_RE.sub("", comp)):
            found = [f for f in gaz.lookup(cand, province, None) if km((f["lon"], f["lat"]), near) <= max_km]
            full = [f for f in found if vlower(f["name"]) == vlower(comp)]
            fs = full or found
            if fs:
                break
        if not fs:
            missing.append(comp)
            continue
        for f in fs:
            circles.append((f["lon"], f["lat"], max(float(f.get("unc") or 0), COMPONENT_MIN_M)))
        labels.append(f"'{comp}' = GNS " + "; ".join(f"{f['name']} ({f['dsg']}, ufi {f['ufi']})" for f in fs))
    return circles, labels, missing


def locate_title_places(gaz, province, name):
    """-> ([(lon, lat, r)], [label], [not located]) for the communes/districts title_places()
    finds in the relic name, each GNS match sized by its measured commune or district extent."""
    circles, labels, missing = [], [], []
    for level, mention, place in title_places(name):
        fs = gaz.lookup(place, province, {"ADM3"} if level == "commune" else {"ADM2"})
        if not fs:
            missing.append(mention)
            continue
        for f in fs:
            ext = gaz.commune_extent(f) if level == "commune" else gaz.district_extent(f)
            circles.append((f["lon"], f["lat"], ext))
        labels.append(f"{mention} = GNS " + "; ".join(f"{f['name']} (ufi {f['ufi']})" for f in fs))
    return circles, labels, missing


def covered(geo, c):
    """Does the geo circle contain the whole (lon, lat, r) circle c? 1 m slack for rounding."""
    return km((geo["lon"], geo["lat"]), c[:2]) * 1000 + c[2] <= geo["unc"] + 1.0


# ---- build ----------------------------------------------------------------------------------
# Special-list rows still being prepared (not yet listed relics).
PROPOSED_RE = re.compile(r"đang lập hồ sơ|lập hồ sơ")
# Composite relics that include a known grave (out of scope) among their sites: name_key -> note.
COMPOSITE_WITH_GRAVE = {
    "cac dia diem khoi nghia truong dinh": "the relic set includes Trương Định's tomb and temple (the list's image is 'Đền và mộ Trương Định'; "
    "the tomb is the main-list relic 'Lăng Trương Định', Gia Thuận, dropped as a known grave). The "
    "grave is out of scope; this record stands for the uprising's other sites",
}


def position_text(geo):
    """How the stored point was made, for the analyst."""
    n = len(geo.get("circles") or [])
    if geo["datum"] == "enclosing-circle":
        return (
            f"middle of a circle enclosing {n} places ({geo['level']}), a point in none of them in "
            f"particular (enclosing-circle)"
        )
    return f"{geo['level']} ({geo['datum']})"


def main_keys(r):
    """Keys a special-list row may use for this main-list relic: its name, its link, aliases in
    parentheses ('Nhà lao Cây Dừa (Nhà tù Phú Quốc)') and '+' components ('Căn cứ Tà Thiết + Căn cứ
    BCH Quân giải phóng ...')."""
    keys = (
        {name_key(r["name_wt"])}
        | {normalise(t) for t, _ in links(r["name_wt"])}
        | {name_key(a) for a in re.findall(r"\(([^()]*)\)", plain(r["name_wt"]))}
        | {name_key(line.lstrip(" +")) for line in name_text(r["name_wt"]).splitlines() if line.strip().startswith("+")}
    )
    return {merge_key(k) for k in keys if k}


def select_war_relics(main, special, stats):
    """Rows of both lists that classify() keeps, each a copy with its plain 'name', 'kind' and
    'reason' added. Every decision is counted in stats as '<list>:<decision>'; a dropped burial
    whose name also has a war word is counted again, so the analyst can review those drops."""
    kept = []
    for r in main + special:
        name = plain(r["name_wt"])
        if not name:
            stats[f"{r['list']}:empty"] += 1
            continue
        decision, kind, reason = classify(name, r["notes"])
        stats[f"{r['list']}:{decision}"] += 1
        if decision == "burial" and (
            BATTLE_RE.search(vlower(name)) or SITE_RE.search(vlower(name)) or MASSACRE_RE.search(vlower(name))
        ):
            stats[f"{r['list']}:burial_with_war_keyword"] += 1
        if decision != "keep":
            continue
        kept.append(dict(r, name=name, kind=kind, reason=reason))
    return kept


def find_main_match(r, main_by_key):
    """The main-list row that special-list row r repeats, or None. Matched in r's province by
    its name key or its first link, else by a main key followed by a place the main row's
    'Địa bàn' names ('An toàn khu II Hiệp Hòa' = main 'ATK2' at 'Hiệp Hòa, ...'), longest
    main key first."""
    keys = [merge_key(k) for k in [name_key(r["name_wt"])] + [normalise(t) for t, _ in links(r["name_wt"])[:1]] if k]
    m = next((main_by_key[(r["province"], k)] for k in keys if (r["province"], k) in main_by_key), None)
    if m:
        return m
    for (prov, mk), cand in sorted(main_by_key.items(), key=lambda x: -len(x[0][1])):
        if prov != r["province"]:
            continue
        for k in keys:
            rest = k[len(mk) :].strip() if k.startswith(mk + " ") else None
            if rest and f" {rest} " in f" {normalise(plain(cand['place_wt']))} ":
                return cand
    return None


def merge_special_rows(kept, stats):
    """The kept rows, one per relic: a special-list row that repeats a main-list relic is folded
    into that main row (its 'special' summary and 'special_place_wt', the special list's place
    cell, which geocoding must still cover); the other special-list rows stay records of their own."""
    main_by_key = {}
    for r in kept:
        if r["list"] == "main":
            for k in main_keys(r):
                main_by_key.setdefault((r["province"], k), r)
    rows = []
    for r in kept:
        if r["list"] == "special":
            m = find_main_match(r, main_by_key)
            if m:
                m["special_place_wt"] = r["place_wt"]
                m["special"] = {
                    "name": r["name"],
                    "place": plain(r["place_wt"]),
                    "category": r["category"],
                    "round": r["section"],
                    "coord": parse_coord_template(r["place_wt"]),
                    "image": r.get("image"),
                }
                stats["special:merged_into_main"] += 1
                continue
        rows.append(r)
    return rows


def base_position(r, gaz, wiki_coords, stats):
    """-> (geo, admin): the relic's position before the named places are covered, best source
    first: a {{coord}} in either list, then the relic article's coordinate, then admin_geocode()
    of the 'Địa bàn' cell. admin is that admin match (None without a province). A coordinate
    farther from the admin match than ARTICLE_MAX_KM (or than the match's own radius, if larger)
    marks another place and is not used. geo is None when nothing locates the relic."""
    prov = r["province"]
    relic_link = links(r["name_wt"])[:1]
    admin = admin_geocode(gaz, r["place_wt"], prov, wiki_coords) if prov else None
    listed = parse_coord_template(r["place_wt"]) or (r.get("special") or {}).get("coord")
    art = wiki_coords.get(relic_link[0][0]) if relic_link else None
    geo = None
    if listed:
        tmpl, lon, lat = listed
        if not admin or km((lon, lat), (admin["lon"], admin["lat"])) <= max(ARTICLE_MAX_KM, admin["unc"] / 1000):
            geo = {
                "level": "listed-coordinate",
                "datum": "article-coordinate",
                "lon": lon,
                "lat": lat,
                "unc": UNC_ARTICLE,
                "original": tmpl,
                "matched": ["{{coord}} in relic list"],
            }
            extent, label = coord_extent(tmpl)
            if extent and extent > geo["unc"]:
                # the coordinate marks a larger place (an island, a district) the relic spreads over
                geo["unc"] = extent
                geo["extent_note"] = (
                    f"the list's {{{{coord}}}} marks "
                    f"{repr(label) if label else 'a larger area'} (declared map scale/size), "
                    f"not the relic itself; radius {extent / 1000:.0f} km covers it"
                )
    if not geo and art and admin:
        d = km((art["lon"], art["lat"]), (admin["lon"], admin["lat"]))
        if d <= max(ARTICLE_MAX_KM, admin["unc"] / 1000):
            geo = {
                "level": "relic-article",
                "datum": "article-coordinate",
                "lon": art["lon"],
                "lat": art["lat"],
                "unc": UNC_ARTICLE,
                "circles": [(art["lon"], art["lat"], UNC_ARTICLE)],
                "original": f"{art['lat']},{art['lon']} (vi.wikipedia '{art['page']}')",
                "matched": [f"vi.wikipedia '{art['page']}' ({d:.1f} km from {admin['level']} match)"],
            }
        else:
            stats["relic_article_coord_rejected_far"] += 1
    if not geo and admin:
        geo = dict(admin, original=plain(r["place_wt"]) or r["section"])  # verbatim location cell
    return geo, admin


def cover_named_places(r, geo, admin, area, gaz, wiki_coords, notes_out):
    """-> geo with its radius made to cover every place the relic and the lists name.

    Places the relic's own name gives ('... tại xã X') inside the district or province matched
    replace that coarser position; elsewhere they, the located key points of a road, the places a
    name lists after ':' and (for an area or several communes) the list's own communes/districts
    must all lie inside the circle. A position from admin_geocode is redrawn as one circle around
    everything; a coordinate from a list or an article keeps its point and only grows its radius.
    Places that cannot be located are said so in notes_out (appended to)."""
    prov = r["province"]
    dia_ban = plain(r["place_wt"])
    from_admin = geo["datum"] != "article-coordinate"
    required = []  # (circle, why it must be covered)
    tc, tlabels, tmissing = locate_title_places(gaz, prov, r["name_wt"]) if prov else ([], [], [])
    if (
        tc
        and from_admin
        and geo["level"] in ("district", "province")
        and all(km((geo["lon"], geo["lat"]), c[:2]) * 1000 <= geo["unc"] for c in tc)
    ):
        lon, lat, unc = enclose(tc)
        mentions = [m for _, m, _ in title_places(r["name_wt"])]
        all_communes = all(lvl == "commune" for lvl, _, _ in title_places(r["name_wt"]))
        geo = {
            "level": "commune named in relic name" if all_communes else "place named in relic name",
            "datum": "enclosing-circle" if len(tc) > 1 else "commune-centroid" if all_communes else "district-centroid",
            "lon": lon,
            "lat": lat,
            "unc": ceil100(unc),
            "matched": tlabels,
            "circles": list(tc),
            "notes": geo.get("notes", []),
            "original": f"{dia_ban} (relic name: {'; '.join(mentions)})",
        }
        notes_out.append(f"position = the place(s) the relic name gives inside {dia_ban}: {'; '.join(tlabels)}")
        admin = None  # its district contains them: not a separate place to cover
    else:
        required += [(c, "place named in the relic name") for c in tc]
    if tmissing:
        notes_out.append("not located in GNS (radius does not account for them): " + "; ".join(tmissing))
    if area == "route_section" and prov:
        rc, rlabels, rmissing = locate_components(gaz, prov, route_components(r["name_wt"]))
        required += [(c, "key point named in the relic name") for c in rc]
        if rlabels:
            notes_out.append("key points located: " + "; ".join(rlabels))
        if rmissing:
            notes_out.append(
                "key points not located in GNS, so not measured (the radius covers only "
                "the located places and the list's commune): " + "; ".join(rmissing)
            )
    elif prov and route_components(r["name_wt"]):
        # places the relic name lists ('Khu vực: – Bến phà Xuân Sơn – [[Động Phong Nha]]')
        near = (admin["lon"], admin["lat"]) if admin else (geo["lon"], geo["lat"])
        cc, clabels, cmissing = locate_named_components(gaz, prov, r["name_wt"], wiki_coords, near)
        required += [(c, "place named in the relic name") for c in cc]
        if clabels:
            notes_out.append("places the relic name lists, located: " + "; ".join(clabels))
        if cmissing:
            notes_out.append(
                "places the relic name lists, not located (the radius does not account for them): "
                + "; ".join(cmissing)
            )
    # A coordinate from a list/article, or a special-list place, must still cover the communes/
    # districts the lists name when the relic spreads over them (an area, or several places). A
    # point site (a pass, a prison) inside the one district named is already consistent with it.
    special_admin = (
        admin_geocode(gaz, r["special_place_wt"], prov, wiki_coords) if prov and r.get("special_place_wt") else None
    )
    for a in ([admin] if not from_admin else []) + [special_admin]:
        if a and a["level"] != "province" and (area or (a["level"] == "commune" and len(a["matched"]) > 1)):
            required += [(c, f"{a['level']}(s) named in the list ({'; '.join(a['matched'])})") for c in a["circles"]]
    missing_req = [(c, why) for c, why in required if not covered(geo, c)]
    if missing_req:
        if from_admin:
            base = admin["circles"] if admin else [(geo["lon"], geo["lat"], geo["unc"])]
            lon, lat, unc = enclose(base + [c for c, _ in required])
            geo.update(
                lon=lon,
                lat=lat,
                unc=ceil100(unc),
                datum="enclosing-circle",
                circles=list(base) + [c for c, _ in required],
                level=f"circle around the {geo['level']} and the places named",
                original=f"{geo['original']} + places named: " + "; ".join(sorted({why for _, why in missing_req})),
            )
        else:
            for c, _ in missing_req:
                geo["unc"] = max(geo["unc"], km((geo["lon"], geo["lat"]), c[:2]) * 1000 + c[2])
            geo["circles"] = geo["circles"] + [c for c, _ in missing_req]
        notes_out.append("radius widened to cover " + "; ".join(sorted({why for _, why in missing_req})))
    return geo


def place_event_elsewhere(key, geo, wiki_coords, dia_ban, notes_out):
    """-> (event_elsewhere, event dates) for a relic in EVENT_ELSEWHERE, else (None, None).

    The list's location and the event article contradict each other, and neither can be shown
    wrong, so geo (updated in place) becomes the midpoint of the two with a radius covering both;
    event_elsewhere records both points for the analyst. Without a coordinate for the event
    article geo is kept and the contradiction is only noted."""
    if key not in EVENT_ELSEWHERE:
        return None, None
    title, why, ev_dates = EVENT_ELSEWHERE[key]
    ev = wiki_coords.get(title)
    if not ev:
        notes_out.append(f"Contradiction (unresolved, no coordinate for '{title}'): {why}")
        return None, ev_dates
    p1, u1 = (geo["lon"], geo["lat"]), geo["unc"]
    p2, u2 = (ev["lon"], ev["lat"]), UNC_ARTICLE
    lon, lat, unc = enclose([(*p1, u1), (*p2, u2)])
    elsewhere = {
        "listed_point": [round(p1[0], 6), round(p1[1], 6), u1],
        "listed_original": geo["original"],
        "listed_datum": geo["datum"],
        "event_article": title,
        "event_point": [p2[0], p2[1], u2],
        "why": why,
    }
    geo.update(
        lon=lon,
        lat=lat,
        unc=ceil100(unc),
        datum="midpoint-of-two-places",
        circles=[(*p1, u1), (*p2, u2)],
        level=f"midpoint of the list's {geo['level']} and the event article",
        original=f"{geo['original']} (list, {geo['datum']} {p1[1]:.6f},{p1[0]:.6f}) + "
        f"{p2[1]},{p2[0]} (vi.wikipedia '{title}')",
    )
    notes_out.append(
        f"Contradiction: {why} ({km(p1, p2):.0f} km apart); position = midpoint of "
        f"{dia_ban} and vi.wikipedia '{title}' (a point in neither place), radius covers both"
    )
    return elsewhere, ev_dates


BURIAL_CHECK_NOTE = {
    "no_grave_on_site": "Checked for victims' graves on site (out of scope if present): none found - ",
    "unresolved": "Victims' graves on site not ruled out (no source found) - ",
    "unchecked": "Victims' graves on site not checked - ",
}


def grave_notes(r, key, stats, notes_out):
    """-> (burial_check, includes_known_grave note) for a kept relic, appending both to notes_out:
    the mass-death source check (burial_check()) and whether the relic set includes a known grave
    (COMPOSITE_WITH_GRAVE, or the list's image naming one)."""
    check = burial_check(r["name"])
    if check:
        stats[f"mass_death_{check['verdict']}"] += 1
        notes_out.append(BURIAL_CHECK_NOTE[check["verdict"]] + check["evidence"])
    image = r.get("image") or (r.get("special") or {}).get("image")
    grave_note = COMPOSITE_WITH_GRAVE.get(key)
    if not grave_note and image and BURIAL_RE.search(vlower(re.sub(r"[_.]", " ", image))):
        grave_note = (
            f"the list's image for this relic is '{image}', which names a grave: the relic set may "
            f"include a known grave (out of scope); this record stands for its other sites"
        )
    if grave_note:
        stats["composite_with_grave"] += 1
        notes_out.append("Includes a known grave: " + grave_note)
    return check, grave_note


def build(main, special, gaz, wiki_coords, previous=None):
    """-> (records, stats): one store.Evidence per kept war relic, geocoded, and counts of every
    decision. main / special: main_rows() / special_rows(); gaz: a Gazetteer; wiki_coords:
    fetch_coords() of wanted_titles(). previous: {source_record_id: {'title', 'dia_ban'}} already
    stored, so a new same-name relic never renames an existing record (see assign_ids)."""
    stats = Counter()
    rows = merge_special_rows(select_war_relics(main, special, stats), stats)
    records = []
    for r in rows:
        prov = r["province"]
        relic_link = links(r["name_wt"])[:1]
        if is_multi_province(r["place_wt"]):
            stats["skipped_multi_province_route"] += 1
            continue
        geo, admin = base_position(r, gaz, wiki_coords, stats)
        if not geo:
            stats["skipped_no_geocode"] += 1
            continue
        stats[f"geocode:{geo['level']}"] += 1
        dia_ban = plain(r["place_wt"])
        notes_out = [geo.pop("extent_note")] if geo.get("extent_note") else []
        geo["circles"] = list(geo.get("circles") or [(geo["lon"], geo["lat"], geo["unc"])])
        area = area_class(r["name"], r["kind"])
        geo = cover_named_places(r, geo, admin, area, gaz, wiki_coords, notes_out)
        key = name_key(r["name_wt"])
        elsewhere, ev_dates = place_event_elsewhere(key, geo, wiki_coords, dia_ban, notes_out)
        if area and geo["unc"] < AREA_FLOOR_M[area]:
            geo["unc"] = AREA_FLOOR_M[area]
            notes_out.append(
                f"radius {AREA_FLOOR_M[area] / 1000:.0f} km: a {area.replace('_', ' ')} is an area, "
                f"the point is only somewhere in it"
            )
        check, grave_note = grave_notes(r, key, stats, notes_out)
        proposed = r["list"] == "special" and bool(PROPOSED_RE.search(vlower(r["name"] + " " + r["section"])))
        start, end = ev_dates or event_dates(r["name"])
        list_title = MAIN_TITLE if r["list"] == "main" else SPECIAL_TITLE
        url = (
            wiki_url(relic_link[0][0])
            if relic_link
            else wiki_url(list_title) + "#" + urllib.parse.quote(r["section"].replace(" ", "_"))
        )
        status = (
            "Proposed special national relic (dossier in preparation, not yet listed)"
            if proposed
            else f"Vietnamese {'special ' if r['list'] == 'special' or r.get('special') else ''}national relic"
        )
        desc = (
            f"{status} ({r['reason']}): {r['name']}. Location: {dia_ban}"
            f"{', ' + r['section'] if r['list'] == 'main' else ''}. Category: {r['category'] or '-'}. "
            f"Position = {position_text(geo)}; a lead for Vietnamese-language research, "
            f"not a site position." + "".join(f" {n}." for n in geo.get("notes", []) + notes_out)
        )
        raw = {
            "list": list_title,
            "section": r["section"],
            "province": prov,
            "name": r["name"],
            "dia_ban": dia_ban,
            "phan_loai": r["category"],
            "nam_cong_nhan": r["recognised"],
            "ghi_chu": r["notes"],
            "relic_link": relic_link[0][0] if relic_link else None,
            "classified_as": r["reason"],
            "geocode_level": geo["level"],
            "geocode_matched": geo["matched"],
            "geocode_circles": [[round(c[0], 6), round(c[1], 6), ceil100(c[2])] for c in geo["circles"]],
            "geocode_notes": geo.get("notes", []) + notes_out,
            "special": r.get("special"),
            "area_class": area,
            "event_elsewhere": elsewhere,
            "burial_check": check,
            "status": "proposed" if proposed else "listed",
            "includes_known_grave": grave_note,
        }
        records.append(
            store.Evidence(
                source=SOURCE,
                source_record_id=f"{r['list']}:{prov or 'unknown'}:{id_key(r['name_wt'])}",
                kind=r["kind"],
                lon=round(geo["lon"], 6),
                lat=round(geo["lat"], 6),
                uncertainty_m=ceil100(geo["unc"]),
                original_coord=geo["original"],
                original_datum=geo["datum"],
                title=r["name"],
                date_start=start,
                date_end=end,
                description=desc,
                url=url,
                raw=raw,
            )
        )
    return assign_ids(records, previous), stats


def assign_ids(records, previous=None):
    """Ids are 'list:province:name-slug'. Relics sharing a slug in one province (rare) are told
    apart without row order. A record already in the store keeps its id (matched by title, then by
    location cell), so a new same-name row never renames an existing record and later edits of the
    location cell do not either; a new one gets its location cell's slug appended, then its full
    name's if that is still not unique."""
    previous = previous or {}
    groups = defaultdict(list)
    for e in records:
        groups[e.source_record_id].append(e)
    for rid, es in groups.items():
        stored = {k: v for k, v in previous.items() if k == rid or k.startswith(rid + "@")}
        if len(es) == 1 and (rid in stored or not stored):
            continue
        fixed = {}  # id(record) -> stored id it keeps
        for field in ("dia_ban", "title"):  # same-name twins share a title: the location cell first
            for e in es:
                if id(e) in fixed:
                    continue
                mine = e.title if field == "title" else e.raw["dia_ban"]
                hits = [k for k, v in stored.items() if v.get(field) == mine and k not in fixed.values()]
                rivals = [
                    x for x in es if id(x) not in fixed and (x.title if field == "title" else x.raw["dia_ban"]) == mine
                ]
                if len(hits) == 1 and len(rivals) == 1:
                    fixed[id(e)] = hits[0]
        for e in es:
            e.source_record_id = fixed.get(id(e)) or (rid if len(es) == 1 else f"{rid}@{normalise(e.raw['dia_ban'])}")
    groups = defaultdict(list)
    for e in records:
        groups[e.source_record_id].append(e)
    for rid, es in groups.items():
        if len(es) > 1:  # same name and same location cell
            for e in es:
                e.source_record_id = f"{rid}@{normalise(e.title)}"
    dup = [k for k, c in Counter(e.source_record_id for e in records).items() if c > 1]
    if dup:
        raise ValueError(f"duplicate relic ids {dup}")
    return records


def write(db, records):
    """Make the store hold exactly these relics (dropped ones deleted, or withdrawn if in a case)."""
    store.upsert_source(
        db,
        SOURCE,
        name="vi.wikipedia national relic lists (war-related relics)",
        url=wiki_url(MAIN_TITLE),
        licence="CC BY-SA 4.0",
        notes=(
            "'Danh sách Di tích quốc gia Việt Nam' + 'Di tích quốc gia đặc biệt (Việt Nam)'. "
            "War-related relics 1858-1975 only; cemeteries, graves, ossuaries, tombs and "
            "temples dropped. Positions are relic-article coordinates or "
            "commune/district/province centroids (GNS, pre-2025 names), with radii sized to "
            "the war zone/base/battle the relic names, so treat as leads."
        ),
    )
    return store.sync_source(db, SOURCE, records, reason="no longer a war-related relic in the vi.wikipedia lists")


def stored_ids(db):
    """{source_record_id: {'title', 'dia_ban'}} of this source's records already in the store."""
    out = {}
    for rid, title, raw in db.execute(
        "select source_record_id, title, raw_json from evidence where source = ?", (SOURCE,)
    ):
        out[rid] = {"title": title, "dia_ban": (json.loads(raw or "{}") or {}).get("dia_ban")}
    return out


def load(refresh=False):
    """-> (main_rows, special_rows) of the two lists, from the cached raw wikitext."""
    main_p = cached_get(
        f"{WIKI}/w/index.php?title=" + urllib.parse.quote(MAIN_TITLE.replace(" ", "_")) + "&action=raw",
        SOURCE,
        name="list_national.wikitext",
        refresh=refresh,
    )
    spec_p = cached_get(
        f"{WIKI}/w/index.php?title=" + urllib.parse.quote(SPECIAL_TITLE.replace(" ", "_")) + "&action=raw",
        SOURCE,
        name="list_special.wikitext",
        refresh=refresh,
    )
    return main_rows(main_p.read_text(encoding="utf-8")), special_rows(spec_p.read_text(encoding="utf-8"))


def wanted_titles(main_r, special_r):
    """vi.wikipedia titles whose coordinates the build may use: linked relic articles, linked
    communes, unlinked commune names (fallback when GNS lacks them) and the event articles."""
    titles = set()
    for r in main_r + special_r:
        if classify(plain(r["name_wt"]), r["notes"])[0] == "keep":
            titles |= {t for t, _ in links(r["name_wt"])[:1]} | {t for t, _ in links(r["place_wt"])}
            titles |= {
                n
                for n, hint, target, _ in place_parts(r["place_wt"])
                if not target and hint != "province" and re.search(r"[^\W\d_]{2}", n)
            }
    return titles | {v[0] for v in EVENT_ELSEWHERE.values()}


def main(argv=None):
    """Fetch (or reuse the cache), build, write the store and the run report; returns the report."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download instead of using the cache")
    args = ap.parse_args(argv)
    main_r, special_r = load(args.refresh)
    gaz = Gazetteer(
        json.loads(GNS_INDEX.read_text(encoding="utf-8")),
        districts=fetch_district_members(refresh=args.refresh),
        borders=fetch_province_borders(refresh=args.refresh),
    )
    wiki_coords = fetch_coords(wanted_titles(main_r, special_r), refresh=args.refresh)
    db = store.connect()
    records, stats = build(main_r, special_r, gaz, wiki_coords, previous=stored_ids(db))
    kept, deleted, withdrawn = write(db, records)
    aoi = [e for e in records if in_bbox(e.lon, e.lat, AREA_OF_INTEREST)]
    report = {
        "records": kept,
        "in_area_of_interest": len(aoi),
        "stale_deleted": deleted,
        "stale_withdrawn": withdrawn,
        "main_rows": len(main_r),
        "special_rows": len(special_r),
        "by_kind": dict(Counter(e.kind for e in records)),
        "stats": dict(sorted(stats.items())),
        "aoi_titles": [(e.title, e.lon, e.lat, e.uncertainty_m, e.original_datum) for e in aoi],
    }
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "vn_relics_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "aoi_titles"}, ensure_ascii=False, indent=1))
    print(f"{len(records)} records, {len(aoi)} in the area of interest")
    return report


if __name__ == "__main__":
    main()
