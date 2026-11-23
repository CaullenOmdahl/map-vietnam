"""Build data/sources/map_library.json: every map sheet a person can add from the viewer's Old maps card.

The catalogue is generated here but committed, so nobody running Map Vietnam ever has to probe
a map server to find out what exists. Nothing in it is downloaded until the person adds it.

The series:
  - l7014: US Army Map Service 1:50,000 GeoPDFs from the USGS Store, one entry per hosted
    15-minute cell touching Vietnam (series L7014, plus the L7015/L7016 sheets that cover the
    Laos and Cambodia borders on the same grid).
  - jog1501: Joint Operations Graphic (Air) 1:250,000 GeoPDFs, series 1501A, from the USGS Store
    (two sheets the Store lacks come from the University of Texas Libraries).
  - l909: US Army Map Service city plans, mostly 1:12,500, GeoPDFs from the USGS Store.
  - sgi100k and l509: the French Indochina 1:100,000 sheets (Service Géographique de l'Indochine,
    on NAKALA) and the US Army Map Service L509 1:250,000 sheets (University of Texas Libraries),
    listed by scripts/sheet_lists.py (data/sources/sheets.json) and placed automatically by
    scripts/place_sheets.py; only sheets with a control file in data/scans/ are catalogued. An image
    served by IIIF is made on request, so its size is only approximately known (bytes_estimated).
  - scans: older scanned maps, copied unchanged from data/sources/maps.json; each has its own
    hand-made control file in data/scans/<id>.json.

Where each field comes from:
  - L7014 ids, URLs and sizes: a one-off HEAD probe of the USGS Store (--probe, a JSON list of
    {id, url, bytes, exists}, where id is the PDF name without .pdf). Without --probe the sheets
    already in the catalogue are kept, so titles can be rebuilt without probing again.
  - L7014 bounds: the sheet grid itself (l7014_cell_bounds), on the sheets' own datum.
  - Titles: the place name NGA printed in each PDF's metadata (the last 64 KB of an L7014 PDF,
    fetched with one range request; the Info dictionary of a JOG PDF), given its Vietnamese
    spelling from the NGA GEOnet Names Server (GNS) when a place of that name lies on the sheet.
    A sheet with no printed name is named after the most notable GNS populated place on it.
  - JOG bounds, sizes and sha256: the downloaded PDFs (their ll_/ur_ corner fields).
  - Scan bounds: an affine fit of each scan's control points, applied to its neatline.

Every download goes through common.cached_get (one request at a time, a pause between requests
to the same host, cached under data/raw/map_library/), so a rerun fetches nothing new.

Usage:
  .venv/bin/python scripts/map_library_catalogue.py [--probe PROBE.json] [--gns Vietnam.zip]
"""

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

import numpy as np
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import common  # noqa: E402
import gns  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sources" / "map_library.json"
LEGACY = ROOT / "data" / "sources" / "maps.json"
SHEETS = ROOT / "data" / "sources" / "sheets.json"
SCANS = ROOT / "data" / "scans"
CACHE = "map_library"  # sub-folder of data/raw for everything this script downloads

USGS_MOD = "https://store.usgs.gov/assets/MOD/StoreFiles/NGA/"
USGS_YIMAGES = "https://store.usgs.gov/assets/yimages/NGA/"
PCL_JOG = "https://maps.lib.utexas.edu/maps/jog/vietnam/"
# Hosts a catalogue URL may point at: the USGS Store, and the archives the scans come from.
# The server only ever downloads catalogue URLs, so this list is the whole of what it can reach.
ALLOWED_HOSTS = {
    "store.usgs.gov",
    "maps.lib.utexas.edu",
    "openresearch-repository.anu.edu.au",
    "humazur.univ-cotedazur.fr",
    "www.davidrumsey.com",
    "api.nakala.fr",
    "thumb.wikimedia.org",
    "upload.wikimedia.org",
}
# NAKALA's IIIF JPEG of a sheet runs about this many bytes per pixel (measured on several sheets);
# the catalogue size of an sgi100k map is an estimate from its pixel count.
IIIF_JPEG_BYTES_PER_PX = 0.153
# Vietnam with a little slack: an automatically listed sheet must overlap it to be catalogued (the
# French collection also holds sheets of Yunnan and Kouang-Tchéou-Wan, in China).
VIETNAM_BOX = (102.1, 8.2, 109.6, 23.45)
# The PDF metadata the titles come from sits in the file's last incremental update, which in
# every L7014-family sheet checked lies within the final 64 KB.
TAIL_BYTES = 65536
REQUEST_INTERVAL_S = 1.2

# JOG 1501A sheets touching Vietnam that the USGS Store hosts (HEAD-probed 2026-11-05).
JOG_SHEETS = (
    "NC4803 NC4804 NC4806 NC4807 NC4808 NC4810 NC4811 NC4815 NC4901 "
    "ND4804 ND4808 ND4812 ND4816 ND4901 ND4905 ND4909 ND4913 "
    "NE4802 NE4803 NE4807 NE4811 NE4812 NE4816 "
    "NF4805 NF4807 NF4809 NF4811 NF4812 NF4813 NF4814 NF4815 NF4816"
).split()
# JOG sheets the USGS Store lacks, which the University of Texas Libraries host under their own
# names (checked 2026-11-24). NF 48-10's PDF records no corners, so its cell is given here: the
# 1:250,000 grid north of 12 N is regular (1 x 1.5 degrees), as its neighbours NF 48-9/11 show.
JOG_UT_SHEETS = {
    "NC4805": "nc-48-5-kampot-cambodia-vietnam.pdf",
    "NF4810": "nf-48-10-yen_bay-vietnam.pdf",
}
JOG_BOUNDS = {"NF4810": [103.5, 21.0, 105.0, 22.0]}
JOG_TITLES = {"NF4810": "Yên Bái (NF 48-10)"}  # its PDF has no title (the University of Texas names it)
# L909 city plans the USGS Store hosts, by their NGA record name (the PDF is <name>_geo.pdf).
# Found by HEAD-probing the Store for Vietnamese towns (2026-11-24); NGA truncates long names
# (VINHBENT = Vinh and Ben Thuy, PHULANGT = Phu Lang Thuong, QUANGNGA = Quang Ngai).
# Towns whose NGA name runs words together or is the old one, so the gazetteer cannot spell it.
L909_TOWNS = {
    "L909XHONGAY__001": "Hòn Gai",
    "L909XLACGIAO": "Buôn Ma Thuột (Lạc Giao)",
    "L909XPHULANGT": "Bắc Giang (Phủ Lạng Thương)",
    "L909XQUINHON": "Quy Nhơn",
    "L909XVINHBENT": "Vinh and Bến Thủy",
}
L909_SHEETS = (
    "L909XBIENHOA L909XCANTHO L909XDALAT L909XDANANG L909XDONGHOI L909XHAIPHONG L909XHANOI "
    "L909XHONGAY__001 L909XHUE L909XLACGIAO L909XMYTHO L909XNHATRANG L909XPHULANGT L909XQUANGNGA "
    "L909XQUINHON L909XTUYHOA L909XVINHBENT L909XVINHLONG"
).split()

# disk_per_download_byte: disk space a map takes once added, per byte downloaded (the download is
# kept, plus its WebP tiles: measured at about 1.4x the PDF for a 1:50,000 sheet and 2.1x for a
# 1:250,000 one). The library, the viewer and setup's first-run text all estimate from it.
SERIES = {
    "l7014": {
        "name": "US Army Map Service series L7014, with border sheets L7015 and L7016",
        "scale": "1:50,000",
        "publisher": "US Army Map Service; GeoPDFs prepared by the US National Geospatial-Intelligence Agency",
        "years": "mostly 1965-1970 editions",
        "licence": "Work of the US Government: public domain in the United States. NGA makes the GeoPDFs "
        "freely downloadable from the USGS Store.",
        "disk_per_download_byte": 2.4,
        "notes": "One sheet per 15-minute cell. Bounds are the sheet's printed frame (its cell, but for three "
        "border and coastal sheets) on the map's own datum (Indian 1960), a few hundred metres from "
        "WGS84; the tiles are placed from each PDF's own georeferencing. The PDF metadata carries release codes 'DS' or 'OO', the same as the "
        "sheets this project has always used; check the printed margin before redistributing tiles.",
    },
    "jog1501": {
        "name": "Joint Operations Graphic (Air), series 1501A",
        "scale": "1:250,000",
        "publisher": "US Defense Mapping Agency; GeoPDFs prepared by the US National Geospatial-Intelligence Agency",
        "years": "editions about 1966-2000, most from the 1970s",
        "licence": "Work of the US Government: public domain in the United States. NGA makes the GeoPDFs "
        "freely downloadable from the USGS Store; the University of Texas Libraries host the same files. "
        "Credit 'University of Texas Libraries' when a file comes from there.",
        "disk_per_download_byte": 3.1,
        "notes": "An overview layer for the whole country, covering most places the 1:50,000 sheets miss. "
        "Bounds are each PDF's own corner fields, because the sheet layout is irregular. Datums vary "
        "by sheet (Indian 1960, or WGS84 on three later reprints). Some PDFs carry release code 'DS'; check the "
        "printed margin before redistributing tiles.",
    },
    "l909": {
        "name": "US Army Map Service city plans, series L909",
        "scale": "1:12,500",
        "publisher": "US Army Map Service; GeoPDFs prepared by the US National Geospatial-Intelligence Agency",
        "years": "mostly 1965-1970 editions",
        "licence": "Work of the US Government: public domain in the United States. NGA makes the GeoPDFs "
        "freely downloadable from the USGS Store.",
        "disk_per_download_byte": 2.4,
        "notes": "Street-level plans of the main towns, each on its own sheet. The PDFs carry their own "
        "georeferencing (Indian 1960). Release code 'ZZ/DS'; check the printed margin before "
        "redistributing tiles.",
    },
    "sgi100k": {
        "name": "Service Géographique de l'Indochine, Carte de l'Indochine 1:100 000",
        "scale": "1:100,000",
        "publisher": "Service Géographique de l'Indochine; scans by the Centre IST REGARDS (CNRS) on NAKALA",
        "years": "1910s-1952 editions",
        "licence": "Each sheet has its own licence on NAKALA, given with it: most are CC BY-NC-SA 4.0 "
        "(non-commercial use only, with attribution, adaptations shared alike), some CC BY 4.0.",
        "disk_per_download_byte": 2.4,
        "notes": "Placed automatically from each sheet's printed graticule (every 0.2 grad from the Paris "
        "meridian) by scripts/place_sheets.py, then shifted 0.0045 degrees east for the old Paris meridian "
        "value. Expect positions within a few hundred metres.",
    },
    "l509": {
        "name": "US Army Map Service series L509, Indochina and Thailand",
        "scale": "1:250,000",
        "publisher": "US Army Map Service; scans by the University of Texas Libraries",
        "years": "1954-1967 editions",
        "licence": "Work of the US Government: public domain in the United States. Credit: Courtesy of the "
        "University of Texas Libraries, The University of Texas at Austin.",
        "disk_per_download_byte": 3.0,
        "notes": "Placed automatically from each sheet's printed 15-minute graticule on the Indian 1960 datum "
        "by scripts/place_sheets.py. Expect positions within a few hundred metres.",
    },
    "warper": {
        "name": "British Admiralty charts",
        "scale": "various",
        "publisher": "UK Hydrographic Office; scans on Wikimedia Commons, georeferenced on the Wikimaps Warper",
        "years": "1855-1964 editions",
        "licence": "Crown copyright expired: public domain. Control points by Wikimaps Warper contributors.",
        "disk_per_download_byte": 3.0,
        "notes": "Nautical charts of the coast: soundings, harbours, lights and the shore as surveyed. Placed "
        "from the control points volunteers set on the Wikimaps Warper; expect a few hundred metres.",
    },
    "scans": {
        "name": "Older scanned maps",
        "scale": "various, 1:100,000 to 1:400,000",
        "publisher": "various; see each map",
        "years": "1873-1962",
        "licence": "Each map has its own terms, given with it. Some are CC BY-NC-SA: non-commercial use only, "
        "with attribution, and adaptations shared alike.",
        "disk_per_download_byte": 2.4,
        "notes": "Placed by hand from control points in data/scans/<id>.json, so the downloaded image must be "
        "reproduced exactly (see 'derive').",
    },
}

# Short licence lines for the scans, condensed from NOTICE (which has the full credit lines).
SCAN_LICENCES = {
    "ams_l509_nc48-6_1954": "Australian National University, Asia Pacific Map Collection: 'Copyright expired'. "
    "A work of the US Government.",
    "ams_l509_nc48-6_1962": "University of Texas Libraries, Perry-Castaneda Library Map Collection. A work of the "
    "US Government; the library describes such maps as public domain.",
    "atlas_1909_sadec": "Humazur digital library, Universite Cote d'Azur. Published 1909; no licence statement "
    "found, so check the holder's terms before reusing it.",
    "bigrel_1873_f10": "David Rumsey Map Collection, David Rumsey Map Center, Stanford Libraries: CC BY-NC-SA 3.0.",
    "fr_sgi_100k_228_longxuyen": "Centre IST REGARDS (CNRS) on NAKALA: CC BY-NC-SA 4.0.",
    "koch_1889_f2": "Centre IST REGARDS (CNRS) on NAKALA: CC BY 4.0; the map itself is public domain.",
    "routiere_1951_f20_phnompenh": "Australian National University, Asia Pacific Map Collection: 'Copyright expired'.",
}

# L7014-family sheets that cannot be added as the USGS Store serves them (checked 2026-11-05: the
# head of every sheet by range request, and these whole). They are left out of the catalogue,
# since nobody could add them.
UNUSABLE = {
    "L701467311": "both copies are broken: the first part of the PDF, then zero bytes to the full size",
    "L701559494_001": "the PDF has no georeferencing at all (no LGIDict), so it cannot be placed",
    "L701468361": "its georeferencing puts the sheet 2.25 degrees south and 6.75 west of its frame",
}
# Sheets whose printed frame is not their 15-minute cell (a border sheet moved to cover land, a
# sheet extended to take in a coast): their corners as the PDF's own ll_/ur_ metadata gives them.
FRAME_OVERRIDES = {
    "L701457291_001": [103.833333, 10.25, 104.1, 10.5],
    "L701459262": [104.666667, 8.533333, 105.0, 8.75],
    "L701461462": [105.75, 18.5, 106.0, 18.808333],
}
# Sheets whose copy at the first address is broken the same way, but whose mirror is whole: the
# mirror is listed first.
MIRROR_FIRST = {"L701558504"}

# Hand corrections, only where NGA's metadata garbled the printed sheet name beyond what the
# spelling lookup can repair.
TITLE_OVERRIDES = {
    "L701463304": "Thành phố Hồ Chí Minh (6330 IV)",  # metadata: "Thanh Pho HoHiMinh Ho Chi Minh City"
    "L701467364": "An Túc, An Khê (6736 IV)",  # metadata: "An uc An Khe" (An Túc is An Khê's old name)
}
QUADRANTS = {1: "I", 2: "II", 3: "III", 4: "IV"}  # 1 = NE, 2 = SE, 3 = SW, 4 = NW of the sheet
# Region split by a sheet's centre latitude: north from about Ninh Binh up, south from about
# Phan Thiet down. It only groups sheets for "add a region" and the panel's sections.
NORTH_FROM_LAT = 20.0
SOUTH_BELOW_LAT = 11.5
# GNS populated-place designations, most notable first (capital, province seat, district seat...).
PLACE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5}
OTHER_PLACE_RANK = 6  # PPLX (section of a town), PPLQ (abandoned), PPLW (destroyed), PPLL...
# Administrative words GNS puts in front of some approved names ("Thành Phố Cần Thơ").
ADMIN_PREFIX_RE = re.compile(r"^(thành phố|thị xã|thị trấn|xã|phường|huyện|quận|tỉnh) ", re.IGNORECASE)
# The countries list that ends a printed title, misspellings included ('Vietman', 'Vietnma', 'Loas').
_COUNTRY = r"(?:viet\w*|lao\w*|loas|cambodia|kampuchea(?:\s*\[[^\]]*\])?|china|thailand|vm)"
_COUNTRIES = rf"{_COUNTRY}(?:\s*[;,]\s*{_COUNTRY})*\s*$"
COUNTRIES_RE = re.compile(rf"[,.]\s*{_COUNTRIES}", re.IGNORECASE)
# A few titles leave out the comma ('Bao Ha Vietnam') or abbreviate ('..., VM'); tried only
# when the usual form is absent, so a name such as 'Ban Lao, Laos' keeps its last word.
COUNTRIES_BARE_RE = re.compile(rf"\s+{_COUNTRIES}", re.IGNORECASE)
L7014_STEM_RE = re.compile(r"^L(701[456])(\d{4})([1-4])(_00\d)?(?:_geo)?$")


# --- grid and geometry ----------------------------------------------------------------------


def l7014_cell_bounds(sheet, quadrant):
    """[west, south, east, north] of one 15' cell of the L7014 grid.

    A sheet number "CCRR" names a 30' square: column CC counts half-degrees east from 75°E, row
    RR half-degrees north from 4.5°S (sheet 6129 is 105.5-106°E, 10-10.5°N). The quadrant digit
    picks a quarter: 1 = NE, 2 = SE, 3 = SW, 4 = NW."""
    west = 75 + int(sheet[:2]) * 0.5
    south = -4.5 + int(sheet[2:]) * 0.5
    east_half = quadrant in (1, 2)
    north_half = quadrant in (1, 4)
    w = west + (0.25 if east_half else 0)
    s = south + (0.25 if north_half else 0)
    return [w, s, w + 0.25, s + 0.25]


def parse_l7014_stem(stem):
    """'L701461301_002_geo' -> {"id": "L701461301_002", "series": "L7014", "sheet": "6130", "quadrant": 1}.

    The id is the PDF's name without "_geo": the eight sheets the project started with keep the
    ids they always had (L701461292), and the installed tiles file is named after it."""
    m = L7014_STEM_RE.match(stem)
    if not m:
        raise ValueError(f"not an L7014-family sheet name: {stem}")
    series, number, quadrant, variant = m.groups()
    return {
        "id": f"L{series}{number}{quadrant}{variant or ''}",
        "series": f"L{series}",
        "sheet": number,
        "quadrant": int(quadrant),
    }


def region_for(bounds):
    """'north', 'centre' or 'south' by the latitude of the middle of bounds."""
    mid = (bounds[1] + bounds[3]) / 2
    if mid >= NORTH_FROM_LAT:
        return "north"
    return "south" if mid < SOUTH_BELOW_LAT else "centre"


def jog_label(sheet):
    """'NC4806' -> 'NC 48-6', the way the sheet number is printed."""
    return f"{sheet[:2]} {sheet[2:4]}-{int(sheet[4:])}"


def scan_bounds(control):
    """[west, south, east, north] of a scan: its neatline (or the control points, when it has no
    neatline) pushed through a least-squares affine fit of the control points, plus the
    control file's own offset. Approximate, which is all the catalogue needs."""
    gcps = np.array(control["gcps"], dtype=float)
    a = np.column_stack([gcps[:, 0], gcps[:, 1], np.ones(len(gcps))])
    coef, *_ = np.linalg.lstsq(a, gcps[:, 2:4], rcond=None)
    ring = np.array(control.get("neatline") or gcps[:, :2], dtype=float)
    lonlat = np.column_stack([ring, np.ones(len(ring))]) @ coef
    dx, dy = control.get("offset_deg", (0.0, 0.0))
    lon, lat = lonlat[:, 0] + dx, lonlat[:, 1] + dy
    return [round(float(v), 4) for v in (lon.min(), lat.min(), lon.max(), lat.max())]


# --- names ------------------------------------------------------------------------------------


def printed_name(title):
    """The place name in an NGA PDF title, or None.

    'Vietnam 1:50,000. Phong Phu, O Mon, Vietnam. [L7014 61293].' -> 'Phong Phu, O Mon'
    'JOINT OPERATIONS GRAPHIC-AIR. LONG XUYEN, VIETNAM; CAMBODIA. [1501A NC4806].' -> 'Long Xuyen'
    The text between the series part and the bracketed sheet number ends with the countries the
    sheet covers ('Vietnam; Laos', 'Vietnam, Laos', sometimes misspelled, once after a full stop
    instead of a comma), which is dropped, as is stray punctuation. Abbreviations such as
    'G. Rieng' are kept. All-capitals names are put in title case."""
    m = re.search(r"^[^.]*\.\s*(.+?)\.?\s*\[[^\]]*\]\.?\s*$", title or "")
    if not m:
        return None
    countries = COUNTRIES_RE.search(m.group(1)) or COUNTRIES_BARE_RE.search(m.group(1))
    if not countries:
        return None
    pieces = [p.strip(" =;:-") for p in m.group(1)[: countries.start()].split(",")]
    name = ", ".join(p for p in pieces if p)
    if not name:
        return None
    return name.title() if name.isupper() else name


def _approved_name(row):
    """A GNS name without the administrative word some approved names start with."""
    return ADMIN_PREFIX_RE.sub("", row["full_name"]).strip()


def _key(name):
    """Matching key that ignores accents, case, punctuation and spaces: the printed 'Hanoi' and
    'Thanh-Hoa' match GNS's 'Hà Nội' and 'Thanh Hóa'."""
    return gns.normalise(name).replace(" ", "")


def _place_rank(row):
    return PLACE_RANK.get(row["desig_cd"], OTHER_PLACE_RANK)


def _distance2(row, lon, lat):
    """Squared distance in degrees, with longitude shrunk by latitude: only used for ranking."""
    dx = (float(row["long_dd"]) - lon) * math.cos(math.radians(lat))
    return dx * dx + (float(row["lat_dd"]) - lat) ** 2


class Gazetteer:
    """The GNS rows needed to spell and choose sheet names."""

    def __init__(self, rows):
        # Only approved (N) and conventional (C) names have the Vietnamese spelling; variants are
        # mostly the unaccented 1960s forms the printed titles already use.
        rows = [r for r in rows if r["nt"] in ("N", "C") and r["lat_dd"] and r["long_dd"]]
        self.places = [r for r in rows if r["fc"] == "P"]
        # Unaccented key -> [(row, spelling)], so spelling a name is one lookup, not a scan. A name
        # with an administrative word is filed with and without it: the printed 'Xa Cam My' finds
        # 'Xã Cẩm Mỹ', the printed 'Can Tho' finds 'Thành Phố Cần Thơ' as 'Cần Thơ'.
        self.by_key = {}
        for r in rows:
            for spelled in {r["full_name"], _approved_name(r)}:
                self.by_key.setdefault(_key(spelled), []).append((r, spelled))

    @staticmethod
    def _inside(row, bounds, margin=0.0):
        w, s, e, n = bounds
        lon, lat = float(row["long_dd"]), float(row["lat_dd"])
        return w - margin <= lon <= e + margin and s - margin <= lat <= n + margin

    def spell(self, name, bounds, margin=0.05):
        """name with Vietnamese diacritics, when a GNS feature of the same unaccented name lies on
        (or just beyond the edge of) the sheet; otherwise name unchanged. Populated places win
        over other features, then the more notable designation (Đồng Hới the province seat, not the
        hamlet Động Hỏi), approved names over conventional ones, then the one nearest the middle."""
        pieces = [p.strip() for p in name.split(",")]
        return ", ".join(self._spell_piece(p, bounds, margin) for p in pieces)

    def _spell_piece(self, name, bounds, margin):
        """One comma-separated piece. NGA's metadata often runs a place and its other name together,
        having lost the brackets of the printed title ('Khanh Hung Soc Trang' for 'Khanh Hung (Soc
        Trang)'). When the whole piece is unknown, it is split where both halves are places on the
        sheet; two spellings of one place ('Ha Noi Hanoi') collapse into one."""
        whole = self._lookup(name, bounds, margin)
        if whole:
            return whole
        words = name.split()
        for i in range(1, len(words)):
            left = self._lookup(" ".join(words[:i]), bounds, margin)
            right = self._lookup(" ".join(words[i:]), bounds, margin)
            if left and right:
                return left if _key(left) == _key(right) else f"{left}, {right}"
        return name

    def _lookup(self, name, bounds, margin):
        """The best-placed GNS spelling of name on the sheet, or None."""
        lon, lat = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
        found = [
            (r["fc"] != "P", _place_rank(r), r["nt"] != "N", _distance2(r, lon, lat), spelled)
            for r, spelled in self.by_key.get(_key(name), [])
            if self._inside(r, bounds, margin)
        ]
        return min(found)[-1] if found else None

    def notable_place(self, bounds):
        """(name, on_sheet) for the most notable populated place on the sheet, or the nearest one
        to its middle when none is on it (on_sheet False). Notability: the GNS designation (capital,
        province seat, district seat, village), then how many map scales GNS shows the name at,
        then nearness to the sheet's middle."""
        lon, lat = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2

        def key(r):
            return (_place_rank(r), -len((r["display"] or "").split(",")), _distance2(r, lon, lat))

        inside = [r for r in self.places if self._inside(r, bounds)]
        if inside:
            return _approved_name(min(inside, key=key)), True
        if not self.places:
            return None, False
        return _approved_name(min(self.places, key=lambda r: _distance2(r, lon, lat))), False


def city_name(title):
    """The town in an L909 PDF title: 'CITY MAP:  VIETNAM. HUE, VIETNAM L909 HUE. [L909 HUE].' -> 'Hue'."""
    m = re.search(r"\.\s*([^.]+?),\s*VIET", title or "", re.IGNORECASE)
    if not m:
        return None
    name = " ".join(m.group(1).split())
    return name.title() if name.isupper() else name


def sheet_title(printed, label, bounds, gazetteer):
    """'Cần Thơ (6129 II)': the printed place name in Vietnamese spelling, then the sheet number.
    Falls back to GNS's most notable place on the sheet, or 'Near X' for a sheet with none."""
    if printed:
        return f"{gazetteer.spell(printed, bounds)} ({label})"
    name, on_sheet = gazetteer.notable_place(bounds)
    if name is None:
        return label
    return f"{name} ({label})" if on_sheet else f"Near {name} ({label})"


# --- downloads (cached, polite) ---------------------------------------------------------------


def fetch_tail(map_id, url):
    """The last TAIL_BYTES of a remote PDF, fetched once with a range request and cached."""
    path = common.cached_get(
        url,
        f"{CACHE}/l7014_tails",
        name=f"{map_id}.tail",
        headers={"Range": f"bytes=-{TAIL_BYTES}"},
        min_interval=REQUEST_INTERVAL_S,
    )
    return path.read_bytes()


def xmp_title(data):
    """The last <pdfx:title> in a PDF's bytes (the final incremental update wins), or None."""
    found = re.findall(rb"<pdfx:title>([^<]*)</pdfx:title>", data)
    return found[-1].decode("utf-8", errors="replace").strip() if found else None


def fetch_jog(sheet):
    """Local path of the JOG GeoPDF for sheet, downloaded once (about 10 MB each)."""
    if sheet in JOG_UT_SHEETS:
        name = JOG_UT_SHEETS[sheet]
        return common.cached_get(PCL_JOG + name, f"{CACHE}/jog", name=name, min_interval=REQUEST_INTERVAL_S)
    url = f"{USGS_MOD}1501A{sheet}_geo.pdf"
    return common.cached_get(url, f"{CACHE}/jog", name=f"1501A{sheet}_geo.pdf", min_interval=REQUEST_INTERVAL_S)


def fetch_l909(name):
    """Local path of an L909 city plan GeoPDF, downloaded once (5 to 17 MB each)."""
    url = f"{USGS_MOD}{name}_geo.pdf"
    return common.cached_get(url, f"{CACHE}/l909", name=f"{name}_geo.pdf", min_interval=REQUEST_INTERVAL_S)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# --- the three series -------------------------------------------------------------------------


def l7014_entries(probe, legacy, gazetteer, tail_for=fetch_tail):
    """Catalogue entries for every hosted L7014-family cell in probe.

    probe: [{id: PDF name without .pdf, url, bytes, exists}]; legacy: data/sources/maps.json's
    l7014 list, whose ids and sha256 are kept. tail_for(id, url) -> bytes gives the PDF's end."""
    legacy_by_id = {e["id"]: e for e in legacy}
    entries = []
    for cell in probe:
        if not cell.get("exists", True):
            continue
        sheet = parse_l7014_stem(cell["id"])
        if sheet["id"] in UNUSABLE:
            continue
        bounds = FRAME_OVERRIDES.get(sheet["id"]) or l7014_cell_bounds(sheet["sheet"], sheet["quadrant"])
        # Both USGS paths serve the same files. The probe tried MOD/StoreFiles first, so a sheet it
        # found only under yimages has no second address.
        urls = [cell["url"]]
        if cell["url"].startswith(USGS_MOD):
            urls.append(USGS_YIMAGES + cell["url"].removeprefix(USGS_MOD))
        if sheet["id"] in MIRROR_FIRST:
            urls.reverse()
        label = f"{sheet['sheet']} {QUADRANTS[sheet['quadrant']]}"
        old = legacy_by_id.get(sheet["id"])
        entries.append(
            {
                "id": sheet["id"],
                "series": "l7014",
                "title": TITLE_OVERRIDES.get(sheet["id"])
                or sheet_title(printed_name(xmp_title(tail_for(sheet["id"], urls[0]))), label, bounds, gazetteer),
                "bounds": bounds,
                "region": region_for(bounds),
                "urls": urls,
                "bytes": int(cell["bytes"]),
                "sha256": old["sha256"] if old else None,
                "kind": "geopdf",
            }
        )
    missing = set(legacy_by_id) - {e["id"] for e in entries}
    if missing:
        raise SystemExit(f"the probe lost sheets the project already uses: {sorted(missing)}")
    return sorted(entries, key=lambda e: e["id"])


def pdf_year(info):
    """The edition year NGA recorded in a GeoPDF's metadata (pri_date "1968-01-01"), or ""."""
    m = re.match(r"\s*(1[89]\d\d|20\d\d)", str(info.get("/pri_date", "")))
    return m.group(1) if m else ""


def jog_entry(sheet, pdf, gazetteer):
    """The catalogue entry for one JOG sheet from its downloaded PDF."""
    info = PdfReader(pdf).metadata or {}
    if sheet in JOG_BOUNDS:
        bounds = JOG_BOUNDS[sheet]
    else:
        bounds = [round(float(info[k]), 6) for k in ("/ll_long", "/ll_lat", "/ur_long", "/ur_lat")]
    name = f"1501A{sheet}_geo.pdf"
    sheet_lower = f"{sheet[:2].lower()}-{sheet[2:]}"
    if sheet in JOG_UT_SHEETS:
        urls = [PCL_JOG + JOG_UT_SHEETS[sheet]]
    else:
        urls = [USGS_MOD + name, USGS_YIMAGES + name, f"{PCL_JOG}{sheet_lower}_geo.pdf"]
    return {
        "id": f"JOG_{sheet}",
        "series": "jog1501",
        "title": JOG_TITLES.get(sheet)
        or sheet_title(printed_name(str(info.get("/title", ""))), jog_label(sheet), bounds, gazetteer),
        "bounds": bounds,
        "region": region_for(bounds),
        "urls": urls,
        "date": pdf_year(info),
        "bytes": Path(pdf).stat().st_size,
        "sha256": sha256_of(pdf),
        "kind": "geopdf",
    }


def l909_entry(name, pdf, gazetteer):
    """The catalogue entry for one L909 city plan from its downloaded PDF."""
    info = PdfReader(pdf).metadata
    bounds = [round(float(info[k]), 6) for k in ("/ll_long", "/ll_lat", "/ur_long", "/ur_lat")]
    town = city_name(str(info.get("/title", ""))) or str(info.get("/sheet_id", name)).title()
    file = f"{name}_geo.pdf"
    scale = int(str(info.get("/scale") or "12500"))
    return {
        "id": name,
        "series": "l909",
        "title": f"{L909_TOWNS.get(name) or gazetteer.spell(town, bounds)} (city plan)",
        "bounds": bounds,
        "region": region_for(bounds),
        "urls": [USGS_MOD + file, USGS_YIMAGES + file],
        "bytes": Path(pdf).stat().st_size,
        "sha256": sha256_of(pdf),
        "kind": "geopdf",
        "date": pdf_year(info),
        **({"scale": f"1:{scale:,}"} if scale != 12500 else {}),
    }


def scan_entries(scans, scans_dir=SCANS):
    """data/sources/maps.json's scans, every field kept, as catalogue entries."""
    entries = []
    for scan in scans:
        control = json.loads((scans_dir / f"{scan['id']}.json").read_text(encoding="utf-8"))
        bounds = scan_bounds(control)
        extra = {k: v for k, v in scan.items() if k not in ("id", "title", "url", "bytes", "sha256")}
        entries.append(
            {
                "id": scan["id"],
                "series": "scans",
                "title": scan["title"],
                "bounds": bounds,
                "region": region_for(bounds),
                "urls": [scan["url"]],
                "bytes": scan["bytes"],
                "sha256": scan["sha256"],
                "kind": "scan",
                "licence": SCAN_LICENCES[scan["id"]],
                **extra,
            }
        )
    return entries


ABOUT = (
    "Every map sheet that can be added from the viewer's Old maps card (or python3 start.py maps add). Nothing here is "
    "downloaded until someone adds it. Generated by scripts/map_library_catalogue.py; do not edit by hand. "
    "bounds: [west, south, east, north] in degrees, approximate (the map's own datum). urls: tried in order. "
    "bytes and sha256: the file as first downloaded (sha256 null where nobody has downloaded it yet; the "
    "size is then the server's). Scan entries also carry what the downloader needs to reproduce "
    "the image their control file in data/scans/ was made on: 'source_file', 'derive', 'work_size'."
)


def sheet_entries(sheets, scans_dir=SCANS):
    """Catalogue entries for the listed sheets (data/sources/sheets.json) that have been placed: those
    with a control file in scans_dir, which also records the image's pixel size and byte count."""
    entries = []
    for sheet in sheets:
        control_path = scans_dir / f"{sheet['id']}.json"
        if not control_path.exists():
            continue
        control = json.loads(control_path.read_text(encoding="utf-8"))
        if not control.get("work_size"):
            continue
        bounds = scan_bounds(control)
        w0, s0, e0, n0 = VIETNAM_BOX
        if bounds[2] < w0 or bounds[0] > e0 or bounds[3] < s0 or bounds[1] > n0:
            continue
        w, h = control["work_size"]
        iiif = "iiif" in sheet["image"]
        entries.append(
            {
                "id": sheet["id"],
                "series": sheet["series"],
                "title": sheet["meta"]["title"],
                "bounds": bounds,
                "region": region_for(bounds),
                "urls": [sheet["download"]],
                "bytes": control.get("source_bytes") or round(w * h * IIIF_JPEG_BYTES_PER_PX),
                # An IIIF image is made on request: the next one may differ by a few bytes.
                **({"bytes_estimated": True} if iiif else {}),
                "sha256": None,
                "kind": "scan",
                "licence": sheet["licence"],
                "credit": sheet["credit"],
                "page": sheet["page"],
                "work_size": [w, h],
                "sheet": sheet["sheet"],
                "place": sheet["place"],
                "date": sheet["date"][:4],
            }
        )
    return entries


SCALE_RE = re.compile(r"1\s*:\s*([\d\s.,]+)")


def scale_number(text):
    """1:100 000, 1:100,000 or 1:100.000 -> 100000; None if there is no scale."""
    m = SCALE_RE.search(text or "")
    digits = re.sub(r"\D", "", m.group(1)) if m else ""
    return int(digits) if digits else None


def finder_fields(entry, series, scans_dir=SCANS):
    """What the viewer's Old maps card sorts, filters and groups by, added to an entry:
    year (int or None), scale_n (the scale's denominator), place (the name without the sheet
    number) and sheet_key (the editions of one sheet share it). A scan's facts come from its
    control file's meta block, read here once rather than on every request."""
    out = dict(entry)
    meta = {}
    if entry["kind"] == "scan" and (scans_dir / f"{entry['id']}.json").exists():
        meta = json.loads((scans_dir / f"{entry['id']}.json").read_text(encoding="utf-8")).get("meta", {})
        for key, name in (("date", "date"), ("scale", "scale"), ("series", "series_name")):
            if meta.get(key) and not out.get(name):
                out[name] = meta[key]
    year = re.match(r"\d{4}", str(out.get("date") or ""))
    out["year"] = int(year.group(0)) if year else None
    out["scale_n"] = scale_number(out.get("scale") or series.get(entry["series"], {}).get("scale"))
    out["place"] = out.get("place") or re.sub(r"\s*\([^)]*\)\s*$", "", entry["title"])
    # A hand-placed scan of a sheet that a series also lists gives its sheet_key in data/sources/maps.json.
    out["sheet_key"] = out.get("sheet_key") or (
        f"{entry['series']}:{out['sheet']}" if out.get("sheet") else entry["id"]
    )
    return out


def build(probe, legacy, jog_pdfs, gazetteer, tail_for=fetch_tail, scans_dir=SCANS, l909_pdfs=None, sheets=()):
    """The whole catalogue. jog_pdfs, l909_pdfs: {sheet: local PDF path}; sheets: the listed sheets
    of the automatically placed series (data/sources/sheets.json)."""
    maps = l7014_entries(probe, legacy["l7014"], gazetteer, tail_for)
    maps += [jog_entry(s, jog_pdfs[s], gazetteer) for s in sorted(jog_pdfs)]
    maps += [l909_entry(n, path, gazetteer) for n, path in sorted((l909_pdfs or {}).items())]
    maps += scan_entries(legacy["scans"], scans_dir)
    maps += sheet_entries(sheets, scans_dir)
    return {"about": ABOUT, "series": SERIES, "maps": [finder_fields(m, SERIES, scans_dir) for m in maps]}


def probe_from_catalogue(catalogue):
    """The probe list behind an existing catalogue's L7014 entries, so a rebuild needs no new probe."""
    return [
        {"id": url.rsplit("/", 1)[1].removesuffix(".pdf"), "url": url, "bytes": e["bytes"]}
        for e in catalogue["maps"]
        if e["series"] == "l7014"
        # The address the probe found: the MOD/StoreFiles one, even where the mirror is listed first.
        for url in [next((u for u in e["urls"] if u.startswith(USGS_MOD)), e["urls"][0])]
    ]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--probe", type=Path, help="JSON list of L7014 HEAD-probe results (default: the current catalogue)")
    p.add_argument("--gns", type=Path, help=f"GNS Vietnam country zip (default: data/raw/gns/{gns.ZIP_NAME})")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument("--cache", type=Path, help="folder to cache downloads in (default: data/raw)")
    args = p.parse_args(argv)
    if args.cache:
        common.RAW = args.cache

    if args.probe:
        probe = json.loads(args.probe.read_text(encoding="utf-8"))
    elif OUT.exists():
        probe = probe_from_catalogue(json.loads(OUT.read_text(encoding="utf-8")))
    else:
        raise SystemExit("no catalogue yet: pass --probe")
    gns_zip = args.gns or common.cached_get(gns.URL, gns.SOURCE, name=gns.ZIP_NAME, timeout=600)
    gazetteer = Gazetteer(gns.load_zip(gns_zip))
    legacy = json.loads(LEGACY.read_text(encoding="utf-8"))
    jog_pdfs = {s: fetch_jog(s) for s in [*JOG_SHEETS, *JOG_UT_SHEETS]}
    l909_pdfs = {n: fetch_l909(n) for n in L909_SHEETS}

    sheets = json.loads(SHEETS.read_text(encoding="utf-8")) if SHEETS.exists() else []
    catalogue = build(probe, legacy, jog_pdfs, gazetteer, l909_pdfs=l909_pdfs, sheets=sheets)
    args.out.write_text(json.dumps(catalogue, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for e in catalogue["maps"]:
        counts[e["series"]] = counts.get(e["series"], 0) + 1
    total = sum(e["bytes"] for e in catalogue["maps"])
    print(f"{args.out}: {counts}, {total / 1e9:.2f} GB in all")


if __name__ == "__main__":
    main()
