"""List the scanned map sheets of the regular series that scripts/place_sheets.py can place, as
sheet specs, in data/sources/sheets.json.

Each spec says where a sheet's image is, roughly where the sheet lies, which graticule it prints,
and what the map library should say about it. place_sheets.py turns each into a control file in
data/scans/; scripts/map_library_catalogue.py then lists every placed sheet.

Sources:
  - sgi100k: the French Indochina 1:100,000 sheets (Service Géographique de l'Indochine, Carte de
    l'Indochine; also the Carte provisoire du Tonkin) in the NAKALA collection "Fonds de cartes de
    l'Indochine française du Centre IST REGARDS (CNRS)", read through NAKALA's public API (one page
    of 25 items per request). Each item gives its title, date, licence and an approximate extent;
    NAKALA's IIIF service serves the scan as a JPEG (about 6 MB, where the TIFF is about 150 MB).
    Graticule every 0.2 grad (0.18 degrees) from the Paris meridian.
  - l509: the US Army Map Service series L509, Indochina and Thailand 1:250,000 (1950s-1960s), as
    scanned by the University of Texas Libraries (one JPEG per sheet, 4-7 MB), for the sheets that
    touch Vietnam. Graticule every 15 minutes on the Indian 1960 datum, printed in black over a blue
    UTM grid, so only dark ink is looked at.
  - warper: British Admiralty charts of the coast of Vietnam on Wikimedia Commons that volunteers have
    already georeferenced on the Wikimaps Warper (warper.wmflabs.org). These need no placing: their
    control points are copied, scaled to the image the library downloads (Commons' 3,840 px wide
    rendition; the originals run to 230 megapixels), and the control files written to data/scans/.

Every download goes through common.cached_get (paced, cached), so a rerun fetches nothing new.

Usage: .venv/bin/python scripts/sheet_lists.py [--cache DIR] [--out FILE] [--only sgi100k|l509|warper] [--scans DIR]
"""

import argparse
import html
import json
import re
import sys
import urllib.parse
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import common  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "sources" / "sheets.json"
REQUEST_INTERVAL_S = 1.2

# --- French Indochina 1:100,000 (NAKALA) -----------------------------------------------------

NAKALA_COLLECTION = "10.34847/nkl.16bcmpc6"
NAKALA_API = "https://api.nakala.fr"
NAKALA_CACHE = "map_library/nakala"
PAGE_SIZE = 25
SGI_GRATICULE = {"step_deg": 0.18, "origin_lon": 2.337229}
# The sheets' longitudes sit about 470 m west of the 1960s US sheets wherever it was measured (sheet
# 228 by hand against L7014; sheets 28, 95W, 218 and 232 against today's map): an older value for
# the Paris meridian. Applied to every sheet.
SGI_OFFSET_DEG = [0.0045, 0.0]
SGI_SERIES = "Service Géographique de l'Indochine, Carte de l'Indochine 1:100 000"
NAKALA_LICENCES = {"CC-BY-NC-SA-4.0": "CC BY-NC-SA 4.0", "CC-BY-4.0": "CC BY 4.0"}
# NAKALA items that are the same scan as a hand-placed map already in the library (same image size).
NAKALA_SKIP = {"10.34847/nkl.dedfrtar"}  # sheet 228 Long-Xuyên: fr_sgi_100k_228_longxuyen
SHEET_RE = re.compile(r"feuille\s*n[°o]\s*(\d+\s*(?:bis)?\s*[A-Z]?)\b", re.IGNORECASE)


def nakala_meta(item, key):
    """Every value of one Dublin Core field of a NAKALA item (keys like 'title', 'spatial')."""
    return [m["value"] for m in item["metas"] if m["propertyUri"].rsplit("#", 1)[-1].rsplit("/", 1)[-1] == key]


def nakala_bounds(item):
    """[w, s, e, n] from 'northlimit=..; southlimit=..; westlimit=..; eastlimit=..', or None."""
    for value in nakala_meta(item, "spatial"):
        found = dict(re.findall(r"(\w+limit)=([-\d.]+)", str(value)))
        if len(found) == 4:
            return [round(float(found[k]), 6) for k in ("westlimit", "southlimit", "eastlimit", "northlimit")]
    return None


def place_of(title):
    """'Carte de l'Indochine : Pointe de Cà Mau est : feuille n°244E' -> 'Pointe de Cà Mau est';
    'Tuyen-Quang' -> 'Tuyen-Quang'."""
    parts = [p.strip() for p in title.split(":")]
    return parts[1] if len(parts) >= 2 else title.strip()


def sheet_number(title):
    """'feuille n°244E' -> '244E', 'n°167bisW' -> '167bisW', 'n°10bis' -> '10bis' ('O' for ouest
    is written 'W', as on the sheets); '' when the title has none."""
    m = SHEET_RE.search(title)
    if not m:
        return ""
    raw = re.sub(r"\s+", "", m.group(1))
    num, rest = re.match(r"(\d+)(.*)", raw).groups()
    bis = "bis" if rest.lower().startswith("bis") else ""
    half = rest[len(bis) :].upper().replace("O", "W")
    return num + bis + half


def nakala_licence(item):
    """The item's licence in words, with the holder's credit."""
    codes = [str(v) for v in nakala_meta(item, "license") if not str(v).startswith("http")]
    words = [NAKALA_LICENCES.get(c, c) for c in codes]
    return "Centre IST REGARDS (CNRS) on NAKALA: " + " / ".join(words or ["see the record"]) + "."


def sgi_spec(item):
    """The sheet spec for one NAKALA item, or None when it is not a 1:100,000 sheet with an image
    and an extent."""
    if "1:100.000" not in [str(v) for v in nakala_meta(item, "extent")] or item["identifier"] in NAKALA_SKIP:
        return None
    title = str((nakala_meta(item, "title") or [""])[0])
    bounds = nakala_bounds(item)
    images = [f for f in item.get("files", []) if str(f.get("mime_type", "")).startswith("image/")]
    if not bounds or not images:
        return None
    sheet, place = sheet_number(title), place_of(title)
    doi = item["identifier"]
    date = next((str(d) for d in nakala_meta(item, "created") if d and re.match(r"\d{4}", str(d))), "")
    series = (
        SGI_SERIES if "Indochine" in title or not title.startswith("Carte provisoire") else "Carte provisoire du Tonkin"
    )
    licence = nakala_licence(item)
    return {
        "id": f"sgi100k_{(sheet or 'x').lower()}_{doi.rsplit('.', 1)[-1]}",
        "series": "sgi100k",
        "sheet": sheet,
        "place": place,
        "date": date,
        "bounds": bounds,
        "image": {"iiif": f"{NAKALA_API}/iiif/{doi}/{images[0]['sha1']}"},
        "download": f"{NAKALA_API}/iiif/{doi}/{images[0]['sha1']}/full/max/0/default.jpg",
        "page": f"https://nakala.fr/{doi}",
        "licence": licence,
        "credit": "Old map: Service Géographique de l'Indochine, via CNRS/NAKALA ("
        + licence.split(": ", 1)[-1].rstrip(".")
        + ")",
        "graticule": SGI_GRATICULE,
        "offset_deg": SGI_OFFSET_DEG,
        # A full sheet is 0.72 x 0.45 degrees; special sheets (an island, a coast) run larger.
        "max_extent_deg": [0.9, 0.7],
        "meta": {
            "title": f"{place} (feuille {sheet})" if sheet else place,
            "series": series,
            "scale": "1:100,000",
            "date": date[:4],
            "dateNote": (f"edition dated {date}" if date else "undated print")
            + "; placed automatically from its printed graticule (grads from Paris), shifted 0.0045 degrees "
            "east for the old Paris meridian value",
            "source": f"https://nakala.fr/{doi}",
        },
    }


def nakala_items():
    """Every item of the NAKALA collection, page by page."""
    items, page = [], 1
    while True:
        url = f"{NAKALA_API}/collections/{NAKALA_COLLECTION}/datas?page={page}&limit={PAGE_SIZE}"
        path = common.cached_get(url, NAKALA_CACHE, name=f"page{page:03d}.json", min_interval=REQUEST_INTERVAL_S)
        data = json.loads(path.read_text(encoding="utf-8"))
        items += data["data"]
        if page >= data["lastPage"]:
            return items
        page += 1


def sgi_specs():
    specs = [s for s in map(sgi_spec, nakala_items()) if s]
    return sorted(specs, key=lambda s: (int(re.sub(r"\D", "", s["sheet"]) or 0), s["id"]))


# --- US Army Map Service L509, 1:250,000 (University of Texas Libraries) ---------------------

UT_L509 = "https://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/"
UT_CACHE = "map_library/ut"
INDIAN_1960 = "+proj=longlat +ellps=evrst30 +towgs84=198,881,317,0,0,0,0 +no_defs"
L509_GRATICULE = {"step_deg": 0.25, "origin_lon": 0.0}
UT_CREDIT = (
    "Old map: US Army Map Service, courtesy of the University of Texas Libraries, The University of Texas at Austin"
)
UT_LICENCE = (
    "University of Texas Libraries, Perry-Castañeda Library Map Collection. A work of the US Government; "
    "the library describes such maps as public domain."
)
UT_LINK_RE = re.compile(
    r'href="[^"]*/(txu-oclc-6535632-(n[c-f])(\d\d)-(\d+)(?:-(\d)(?:st|nd|rd|th)-ed)?\.jpg)"[^>]*>([^<]*)', re.IGNORECASE
)
# When each sheet was printed, as "PRINTED BY ARMY MAP SERVICE ... 9-62" in its lower margin (read
# off the scans by eye, 2026-11-24; the University of Texas records no date). Month-year.
L509_PRINTED = {
    "l509_nc48-3_ed3": "9-62", "l509_nc48-4_ed2": "8-62", "l509_nc48-5_ed2": "9-62", "l509_nc48-7_ed3": "9-62",
    "l509_nc48-8_ed3": "8-62", "l509_nc48-10_ed4": "8-62", "l509_nc48-11_ed3": "9-62", "l509_nc48-15_ed3": "8-62",
    "l509_nc49-1": "3-57", "l509_nd48-4_ed2": "8-62", "l509_nd48-8_ed2": "8-62", "l509_nd48-12_ed2": "9-62",
    "l509_nd48-16_ed2": "9-62", "l509_nd49-1_ed2": "9-62", "l509_nd49-5": "6-57", "l509_nd49-9_ed2": "9-62",
    "l509_nd49-13_ed3": "8-62", "l509_ne48-2_ed2": "10-62", "l509_ne48-3_ed2": "10-62", "l509_ne48-7_ed2": "10-62",
    "l509_ne48-11_ed4": "9-62", "l509_ne48-12_ed3": "9-62", "l509_ne48-16_ed2": "8-62", "l509_nf48-5": "1-57",
    "l509_nf48-7": "7-54", "l509_nf48-9": "6-57", "l509_nf48-10": "7-54", "l509_nf48-11": "5-57",
    "l509_nf48-12": "5-57", "l509_nf48-13": "6-57", "l509_nf48-14": "6-54", "l509_nf48-15": "10-57",
    "l509_nf48-16": "6-57",
}  # fmt: skip
# The hand-placed L509 scans already in the library (data/sources/maps.json), not listed again.
HAND_PLACED = {"txu-oclc-6535632-nc48-6-2nd-ed.jpg"}


def l509_cell(band, zone, number):
    """[w, s, e, n] of a 1:250,000 sheet on the regular grid: band letter (C = 8-12 N, D = 12-16 N, ...),
    UTM zone (48 = 102-108 E), number 1-16 in rows of four from the north-west."""
    south = 8 + 4 * "cdef".index(band[-1].lower())
    west = 6 * (zone - 1) - 180
    row, col = (number - 1) // 4, (number - 1) % 4
    return [west + 1.5 * col, south + 3 - row, west + 1.5 * (col + 1), south + 4 - row]


def l509_specs(touching, known_bounds):
    """Specs for the L509 sheets the University of Texas holds whose sheet code is in `touching`
    (the 1:250,000 sheets that cover part of Vietnam). known_bounds: {code: [w, s, e, n]} for sheets
    off the regular grid (the JOG sheets of the same layout), else l509_cell."""
    page = common.cached_get(UT_L509, UT_CACHE, name="l509_index.html", min_interval=REQUEST_INTERVAL_S)
    specs, seen = [], set()
    for m in UT_LINK_RE.finditer(page.read_text(encoding="utf-8", errors="replace")):
        file, band, zone, number, edition, text = m.groups()
        code = f"{band.upper()}{zone}{int(number):02d}"
        if code not in touching or file in HAND_PLACED or file in seen:
            continue
        seen.add(file)
        label = f"{band.upper()} {zone}-{int(number)}"
        place = html.unescape(re.sub(r"^\s*N[C-F]\s*\d+-\d+\s*", "", text))
        place = re.sub(r"\s*\[[^\]]*\]", "", place).strip() or label  # "[with inset maps of ...]"
        bounds = known_bounds.get(code) or l509_cell(band, int(zone), int(number))
        ed = f"edition {edition}" if edition else "edition 1"
        spec_id = f"l509_{band.lower()}{zone}-{int(number)}" + (f"_ed{edition}" if edition else "")
        month, year = (int(v) for v in L509_PRINTED[spec_id].split("-")) if spec_id in L509_PRINTED else (0, 0)
        date = f"19{year:02d}-{month:02d}" if year else ""
        specs.append(
            {
                "id": spec_id,
                "series": "l509",
                "sheet": label,
                "place": place,
                "date": date,
                "bounds": bounds,
                "image": {"url": UT_L509 + file},
                "download": UT_L509 + file,
                "page": UT_L509,
                "licence": UT_LICENCE,
                "credit": UT_CREDIT,
                "graticule": L509_GRATICULE,
                "max_extent_deg": [1.6, 1.5],
                "crs": INDIAN_1960,
                "dark_ink_only": True,
                "meta": {
                    "title": f"{place} ({label})",
                    "series": "US Army Map Service L509, Indochina and Thailand 1:250,000",
                    "scale": "1:250,000",
                    "date": date[:4],
                    "dateNote": (f"{ed}, printed by the Army Map Service {month}/{date[:4]}" if date else ed)
                    + "; placed automatically from its printed 15-minute graticule on the Indian 1960 datum",
                    "source": UT_L509 + file,
                },
            }
        )
    return specs


# --- Admiralty charts georeferenced on the Wikimaps Warper ----------------------------------

WARPER = "https://warper.wmflabs.org"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WARPER_CACHE = "map_library/warper"
# The warped maps on the Wikimaps Warper that cover part of Vietnam at a regional scale, found by
# searching it for Vietnamese and Indochinese place names (2026-11-24): every one an Admiralty chart.
WARPER_IDS = [4317, 5200, 5306, 6140, 6142, 6463, 9667, 9669]
COMMONS_WIDTH = 3840  # the largest standard rendition Commons serves
WARPER_LICENCE = (
    "Admiralty chart (UK Hydrographic Office): Crown copyright expired, public domain. Scan on Wikimedia "
    "Commons; control points by Wikimaps Warper contributors."
)
WARPER_CREDIT = "Old map: Admiralty chart (UK Hydrographic Office), via Wikimedia Commons and Wikimaps Warper"


def _json(url, name, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    path = common.cached_get(url, WARPER_CACHE, name=name, min_interval=REQUEST_INTERVAL_S)
    return json.loads(path.read_text(encoding="utf-8"))


def warper_specs(scans_dir):
    """Specs for the Warper's charts, writing each one's control file into scans_dir."""
    specs = []
    for map_id in WARPER_IDS:
        info = _json(f"{WARPER}/maps/{map_id}.json", f"{map_id}.json")["items"]
        gcps = _json(f"{WARPER}/maps/{map_id}/gcps.json", f"{map_id}_gcps.json")["items"]
        title = info["title"]  # "File:Admiralty Chart No 3875 Haiphong to Cam Pha, Published 1944.jpg"
        pages = _json(
            COMMONS_API,
            f"{map_id}_commons.json",
            {
                "action": "query",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|size",
                "iiurlwidth": COMMONS_WIDTH,
                "format": "json",
            },
        )["query"]["pages"]
        ii = next(iter(pages.values()))["imageinfo"][0]
        url = ii["thumburl"].split("?", 1)[0]
        # Commons may serve a smaller rendition than asked for: measure the one it sends.
        image = common.cached_get(url, WARPER_CACHE, name=f"{map_id}.jpg", min_interval=REQUEST_INTERVAL_S)
        with Image.open(image) as im:
            width, height = im.size
        k = width / info["width"]
        name = re.sub(r"^File:|\.jpe?g$", "", title, flags=re.IGNORECASE)
        year = (re.search(r"Published (\d{4})", name) or [None, ""])[1]
        place = re.sub(r"^Admiralty Chart No \d+\w*\s*|,? Published.*$", "", name).strip()
        chart = (re.search(r"Admiralty Chart No (\d+\w*)", name) or [None, ""])[1]
        spec_id = f"warper_{map_id}"
        control = {
            "image": f"data/raw/scans/{spec_id}/work.jpg",
            "crs": "EPSG:4326",
            # Warper points that fit badly (its own error field) are left out; the rest are scaled.
            "gcps": [
                [round(g["x"] * k, 1), round(g["y"] * k, 1), round(float(g["lon"]), 6), round(float(g["lat"]), 6)]
                for g in gcps
                if (g.get("error") or 0) * k < 20
            ],
            "placed_by": f"Wikimaps Warper map {map_id} ({WARPER}/maps/{map_id}), control points scaled by {k:.4f}",
            "work_size": [width, height],
            "source_bytes": image.stat().st_size,
            "meta": {
                "title": f"{place} (chart {chart})" if chart else place,
                "series": "British Admiralty charts",
                "scale": "",
                "date": year,
                "dateNote": f"published {year}" if year else "",
                "source": "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
            },
        }
        if len(control["gcps"]) < 3:
            continue
        (scans_dir / f"{spec_id}.json").write_text(
            json.dumps(control, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        w, s, e, n = (float(v) for v in info["bbox"].split(","))
        specs.append(
            {
                "id": spec_id,
                "series": "warper",
                "sheet": "",
                "place": place,
                "date": year,
                "bounds": [round(v, 4) for v in (w, s, e, n)],
                "image": {"url": url},
                "download": url,
                "page": control["meta"]["source"],
                "licence": WARPER_LICENCE,
                "credit": WARPER_CREDIT,
                "meta": control["meta"],
            }
        )
    return specs


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--cache", type=Path, help="folder to cache downloads in (default: data/raw)")
    p.add_argument("--out", type=Path, default=OUT)
    p.add_argument(
        "--only", choices=["sgi100k", "l509", "warper"], help="list only this source (the others are kept from OUT)"
    )
    p.add_argument(
        "--scans", type=Path, default=ROOT / "data" / "scans", help="where the warper charts' control files go"
    )
    args = p.parse_args(argv)
    if args.cache:
        common.RAW = args.cache
    import map_library_catalogue as catalogue  # the 1:250,000 sheet list and the JOG sheets' corners

    old = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else []
    specs = [s for s in old if args.only and s["series"] != args.only]
    if args.only in (None, "sgi100k"):
        specs += sgi_specs()
    if args.only in (None, "l509"):
        touching = set(catalogue.JOG_SHEETS) | set(catalogue.JOG_UT_SHEETS)
        lib = json.loads(catalogue.OUT.read_text(encoding="utf-8")) if catalogue.OUT.exists() else {"maps": []}
        known = {m["id"].removeprefix("JOG_"): m["bounds"] for m in lib["maps"] if m["series"] == "jog1501"}
        specs += l509_specs(touching, known)
    if args.only in (None, "warper"):
        specs += warper_specs(args.scans)
    # Only sheets that overlap Vietnam: the French collection also covers Laos, Cambodia and Yunnan.
    w0, s0, e0, n0 = catalogue.VIETNAM_BOX
    specs = [
        x for x in specs if x["bounds"][2] > w0 and x["bounds"][0] < e0 and x["bounds"][3] > s0 and x["bounds"][1] < n0
    ]
    args.out.write_text(json.dumps(specs, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    counts = {}
    for s in specs:
        counts[s["series"]] = counts.get(s["series"], 0) + 1
    print(f"{args.out}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
