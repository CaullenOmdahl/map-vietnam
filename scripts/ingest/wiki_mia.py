"""Wikipedia "List of United States servicemembers and civilians missing in action during the
Vietnam War" (5 period pages) -> loss_incident / aircraft_crash evidence.

Fetch: the raw wikitext of each page via index.php?action=raw (MediaWiki allows this with a
descriptive User-Agent; we use common.USER_AGENT, >= 2 s between requests, cached under
data/raw/wiki_mia/). Licence CC BY-SA 4.0: every record keeps the page URL.

Each table row is one person: date, name, rank, service, unit, operation, location text,
circumstances, recovery status. Cells use rowspan for shared values (service, unit, location
...), so the parser expands rowspans into a full 9-column grid.

Kept: rows whose recovery status says the person is still unaccounted for (killed in action /
body not recovered, missing in action, presumptive finding of death, died in captivity with
remains not returned). Dropped and counted: rows whose status says remains were identified,
recovered, returned or accounted for (these include known formal burials), and released POWs.

Placement: the pages hold no coordinates or grids, only location text such as
"South Vietnam, Kien Phong Province" or "South Vietnam, Lang Vei". Every placement's radius
covers the place the text names; the method is recorded in raw["placement"] and the description:
  * "<X> Province", or a bare name that is also a province (Pleiku, Kontum): the province's
    centroid and covering radius from data/work/hes_provinces.json (farthest hamlet + 10 km);
  * "<X> District": the wartime HES district's centroid and covering radius; else the modern
    GNS district (ADM2) with 15 km; else the province;
  * a named place (village, base, town, valley, river, battle area: NAMED_PLACES): geocoded from
    the GNS name index data/work/gns_index.json, keeping only features inside the expected
    province's circle and of a class that suits the place, with a radius for that kind of place
    (village/base 3 km, town 5 km, city 8 km, battle area 12 km, valley/river: covering its GNS
    anchors + a buffer), never smaller than the GNS feature's own extent. Homonyms of the same
    class in the province are all covered. No match -> the province centroid and full radius;
  * a few rows whose own cited source names a different place (CITATION_CONFLICTS) get a circle
    covering both places, and the conflict is written into the description.
Rows in North Vietnam, Laos, Cambodia, at sea, or with no identifiable province are skipped and
counted. The pages cite no DPAA REFNOs (checked), so none are stored.

Record ids are "<year>-<normalised name>"; only when two rows share that, the date is added, then
a short hash of the row's content (never the row's position).

Inputs: the cached pages, data/work/hes_provinces.json (hes_gazetteer.py) and
data/work/gns_index.json (gns.py). Outputs: evidence rows (source "wiki_mia") in
data/evidence.sqlite and a run summary in data/work/wiki_mia_summary.json.

Run:  .venv/bin/python scripts/ingest/wiki_mia.py [--refresh]
"""

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
import urllib.parse
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import common  # noqa: E402
import store  # noqa: E402

SOURCE = "wiki_mia"
TITLE = "List of United States servicemembers and civilians missing in action during the Vietnam War ({})"
PERIODS = ("1961–1965", "1966–67", "1968–69", "1970–71", "1972–1975")
WORK = store.ROOT / "data" / "work"
HES_PROVINCES = WORK / "hes_provinces.json"
GNS_INDEX = WORK / "gns_index.json"
COLUMNS = ("date", "name", "rank", "service", "unit", "operation", "location", "circumstances", "status")
MONTHS = {
    m: i
    for i, m in enumerate(
        ("january february march april may june july august september october november december").split(), 1
    )
}


def page_url(period):
    """Human-readable article URL of one period page (stored on every record)."""
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(TITLE.format(period).replace(" ", "_"))


def raw_url(period):
    """URL of one period page's raw wikitext (action=raw)."""
    return (
        "https://en.wikipedia.org/w/index.php?title="
        + urllib.parse.quote(TITLE.format(period).replace(" ", "_"))
        + "&action=raw"
    )


def fetch_pages(refresh=False):
    """{period: wikitext} for every period page, fetched through the cache."""
    pages = {}
    for p in PERIODS:
        path = common.cached_get(
            raw_url(p), SOURCE, name=f"page_{p.replace('–', '-')}.wikitext", min_interval=2.0, refresh=refresh
        )
        pages[p] = path.read_text(encoding="utf-8")
    return pages


# ---------------------------------------------------------------- wikitext parsing
# Self-closing refs first, and allowing "/" inside the name ("DPMO-MIA/KIA-BNR1"): otherwise a
# self-closing ref is read as an opening tag and everything up to the next </ref> is lost, e.g.
# "... On 30 August 2012 the DPMO announced that his remains had been identified".
REF_RE = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*>.*?</ref>", re.S | re.I)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
ATTR_RE = re.compile(r'^\s*(?:[a-z-]+\s*=\s*"?[^"|]*"?\s*)+$', re.I)


def _split_top(s, sep):
    """Split s on sep, ignoring separators inside {{...}} and [[...]]."""
    out, depth_t, depth_l, i, start = [], 0, 0, 0, 0
    while i < len(s):
        two = s[i : i + 2]
        if two == "{{":
            depth_t += 1
            i += 2
            continue
        if two == "}}" and depth_t:
            depth_t -= 1
            i += 2
            continue
        if two == "[[":
            depth_l += 1
            i += 2
            continue
        if two == "]]" and depth_l:
            depth_l -= 1
            i += 2
            continue
        if not depth_t and not depth_l and s.startswith(sep, i):
            out.append(s[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    out.append(s[start:])
    return out


def _cell_attrs(cell):
    """'rowspan="3" | USAF' -> ({'rowspan': 3}, 'USAF')."""
    parts = _split_top(cell, "|")
    if len(parts) >= 2 and ATTR_RE.match(parts[0]):
        attrs = {k.lower(): v for k, v in re.findall(r'([a-z-]+)\s*=\s*"?([^"\s|]*)"?', parts[0], re.I)}
        return attrs, "|".join(parts[1:])
    return {}, cell


def clean(text):
    """Wikitext cell -> plain text."""
    t = REF_RE.sub("", text)
    t = re.sub(
        r"\{\{USS\|([^|}]+)(?:\|([^|}]+))?[^}]*\}\}",
        lambda m: f"USS {m.group(1)}" + (f" ({m.group(2)})" if m.group(2) else ""),
        t,
    )
    t = re.sub(r"\{\{convert\|([\d.]+)\|([a-z]+)[^}]*\}\}", r"\1 \2", t)
    for _ in range(3):
        t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", t)
    t = re.sub(r"\[https?://\S+ ([^\]]*)\]", r"\1", t)
    t = re.sub(r"<br\s*/?>", " ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"'''?", "", t)
    return re.sub(r"\s+", " ", t).strip()


def ref_urls(text):
    """The url= values of the citation templates in a raw cell."""
    return re.findall(r"\|\s*url\s*=\s*(\S+?)\s*(?=\||\}\})", text)


def parse_tables(wikitext):
    """Yield (year_section, row_index, {column: raw cell text}) for every data row."""
    wikitext = COMMENT_RE.sub("", wikitext)
    section = None
    for m in re.finditer(r"^==\s*([^=]+?)\s*==\s*$|^\{\|(.*?)^\|\}", wikitext, re.M | re.S):
        if m.group(1):
            section = m.group(1)
            continue
        body = m.group(2)
        spans = {}  # column index -> [remaining rows, raw value]
        rows = re.split(r"^\|-.*$", body, flags=re.M)
        n = 0
        for row in rows[1:]:
            # MediaWiki trims each table line, so " || cell" also starts new cells.
            lines = [ln.strip() for ln in row.strip("\n").split("\n")]
            if not lines or lines[0].startswith("!"):
                continue
            cells, cur = [], None
            for ln in lines:
                if ln.startswith("|") and not ln.startswith("|}"):
                    if cur is not None:
                        cells.append(cur)
                    parts = _split_top(ln[2:] if ln.startswith("||") else ln[1:], "||")
                    cells.extend(parts[:-1])
                    cur = parts[-1]
                elif cur is not None:
                    cur += "\n" + ln
            if cur is not None:
                cells.append(cur)
            if not cells:
                continue
            # Lay cells into the 9 columns, skipping columns held by a rowspan from above.
            values, it = {}, iter(cells)
            for ci in range(len(COLUMNS)):
                if spans.get(ci, [0])[0] > 0:
                    spans[ci][0] -= 1
                    values[COLUMNS[ci]] = spans[ci][1]
                    continue
                try:
                    attrs, val = _cell_attrs(next(it))
                except StopIteration:
                    values[COLUMNS[ci]] = ""
                    continue
                values[COLUMNS[ci]] = val
                rs = int(attrs.get("rowspan", "1") or 1)
                if rs > 1:
                    spans[ci] = [rs - 1, val]
            n += 1
            yield section, n, values


# ---------------------------------------------------------------- interpretation
def norm(s):
    """Accent-stripped, lowercase, punctuation-free form: 'Kiên Phong' -> 'kien phong'."""
    s = unicodedata.normalize("NFD", s.replace("đ", "d").replace("Đ", "D"))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def parse_date(text, year):
    """'April 9' / '9 April' / 'April' in the year section `year` -> ISO date, year-month, or the
    bare year; None when the section is not a year."""
    t = norm(text)
    m = re.match(r"([a-z]+)\s+(\d{1,2})\b", t)
    if m and m.group(1) in MONTHS:
        return f"{year}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    m = re.match(r"(\d{1,2})\s+([a-z]+)", t)
    if m and m.group(2) in MONTHS:
        return f"{year}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
    m = re.match(r"([a-z]+)", t)
    if m and m.group(1) in MONTHS:
        return f"{year}-{MONTHS[m.group(1)]:02d}"
    return year if re.fullmatch(r"\d{4}", year or "") else None


# Status wording seen in the pages (checked on all 1,664 rows): "Killed in action, body not
# recovered", "Presumptive finding of death", "Died in captivity, remains not returned",
# "Unaccounted for", "Missing", "Prisoner" -> still unaccounted for. Resolved cases read
# "... until <date> when his remains were identified", "... when he was accounted for",
# "remains recovered in 1999 and identified in 2003", "Recovered, September 2012",
# "On 6 September 2012 the DPMO announced that his remains had been identified".
# Resolved cases (remains identified, recovered or accounted for) are out of scope for this source.
RECOVERED_RE = re.compile(
    r"\buntil\b.*\bwhen\b|"
    r"remains (?:were |was |had been |have been |has been )?(?:subsequently |later )?"
    r"(?:recovered|identified|returned|repatriated)\b|"
    r"^\s*recovered\b|(?<!un)accounted for|\bidentified (?:in|on)\b",
    re.I,
)
UNRECOVERED_RE = re.compile(
    r"body not recovered|missing|presumptive finding of death|presumed dead|died in captivity|"
    r"declared dead|killed in action|remains not (?:recovered|returned)|unaccounted|prisoner",
    re.I,
)


def status_class(status):
    """'unrecovered', 'recovered' (identified / accounted for) or 'unknown' (empty, unclear)."""
    if RECOVERED_RE.search(status):
        return "recovered"
    if UNRECOVERED_RE.search(status):
        return "unrecovered"
    return "unknown"


BURIAL_RE = re.compile(r"cemetery|cemetary|buried|burial|interred|grave|tomb|martyrs", re.I)
AIR_RE = re.compile(
    r"aircraft|helicopter|\bplane\b|airplane|shot down|crash|ejected|ejection|bail(?:ed)? out|"
    r"parachut|pilot|navigator|co-?pilot|crew ?chief|door gunner|gunship|skyraider|phantom|"
    r"\bUH-|\bCH-|\bOH-|\bAH-|\bHH-|\bF-\d|\bA-\d|\bB-\d|\bC-\d|\bO-\d|\bRF-|\bEC-|\bAC-|\bKC-|"
    r"skyhawk|huey|chinook|thunderchief|intruder|vigilante|hercules|crusader|corsair|sea knight|"
    r"stratofortress|bird dog|bronco|caribou|provider|spooky|loach",
    re.I,
)


def load_provinces(path=HES_PROVINCES):
    """HES province circles (read-only, written by hes_gazetteer.py): code -> province record."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return d["provinces"]


def load_gns(path=GNS_INDEX):
    """GNS name index (read-only): folded name -> [{lon, lat, fc, dsg, adm1, name, nt, ufi, unc}]."""
    return json.loads(Path(path).read_text(encoding="utf-8"))["names"]


# Location-text names (normalised) that map to a wartime GVN province code in hes_provinces.
# Covers wartime spellings, modern names Wikipedia links to, provincial capitals, districts
# and places that occur in the pages. A place that is a city with its own HES code maps to it.
PLACE_TO_CODE = {
    # I Corps
    "quang tri": "101",
    "khe sanh": "101",
    "dong ha": "101",
    "con thien": "101",
    "gio linh": "101",
    "cam lo": "101",
    "huong hoa": "101",
    "lang vei": "101",
    "camp carroll": "101",
    "rockpile": "101",
    "a shau": "102",
    "a shau valley": "102",
    "thua thien": "102",
    "phu bai": "102",
    "phong dien": "102",
    "hue": "191",
    "da nang": "192",
    "danang": "192",
    "quang nam": "103",
    "hoi an": "103",
    "dien ban": "103",
    "hieu duc": "103",
    "dai loc": "103",
    "duy xuyen": "103",
    "que son": "103",
    "thuong duc": "103",
    "marble mountain": "192",
    "quang tin": "104",
    "tam ky": "104",
    "tien phuoc": "104",
    "hiep duc": "104",
    "chu lai": "104",
    "kham duc": "104",
    "hau duc": "104",
    "quang ngai": "105",
    "duc pho": "105",
    "mo duc": "105",
    "son tinh": "105",
    "binh son": "105",
    "nghia hanh": "105",
    "ba to": "105",
    "tu nghia": "105",
    "minh long": "105",
    # II Corps
    "kontum": "206",
    "kon tum": "206",
    "dak to": "206",
    "dak seang": "206",
    "ben het": "206",
    "dak pek": "206",
    "polei kleng": "206",
    "binh dinh": "207",
    "an khe": "207",
    "bong son": "207",
    "an lao": "207",
    "hoai nhon": "207",
    "phu cat": "207",
    "tay son": "207",
    "hoai an": "207",
    "phu my": "207",
    "qui nhon": "292",
    "quy nhon": "292",
    "pleiku": "208",
    "duc co": "208",
    "plei me": "208",
    "ia drang": "208",
    "ia drang valley": "208",
    "phu bon": "209",
    "cheo reo": "209",
    "phu yen": "210",
    "tuy hoa": "210",
    "darlac": "211",
    "dak lak": "211",
    "dac lac": "211",
    "ban me thuot": "211",
    "buon ma thuot": "211",
    "khanh hoa": "212",
    "nha trang": "291",
    "cam ranh": "293",
    "cam ranh bay": "293",
    "ninh thuan": "213",
    "phan rang": "213",
    "tuyen duc": "214",
    "dalat": "294",
    "da lat": "294",
    "quang duc": "215",
    "gia nghia": "215",
    "lam dong": "216",
    "bao loc": "216",
    "binh thuan": "217",
    "phan thiet": "217",
    # III Corps
    "binh tuy": "318",
    "ham tan": "318",
    "long khanh": "319",
    "xuan loc": "319",
    "phuoc long": "321",
    "song be": "321",
    "bu dop": "321",
    "binh long": "322",
    "an loc": "322",
    "loc ninh": "322",
    "quan loi": "322",
    "binh duong": "323",
    "phu loi": "323",
    "lai khe": "323",
    "ben cat": "323",
    "iron triangle": "323",
    "dau tieng": "323",
    "tay ninh": "324",
    "nui ba den": "324",
    "black virgin mountain": "324",
    "trang bang": "325",
    "hau nghia": "325",
    "cu chi": "325",
    "duc hoa": "325",
    "bien hoa": "326",
    "long binh": "326",
    "phuoc tuy": "327",
    "ba ria": "327",
    "long an": "328",
    "tan an": "328",
    "gia dinh": "329",
    "rung sat": "329",
    "rung sat special zone": "329",
    "nha be": "329",
    "vung tau": "395",
    "saigon": "396",
    "tan son nhut": "396",
    "tan son nhat": "396",
    "ho chi minh city": "396",
    "cholon": "396",
    # IV Corps
    "go cong": "430",
    "kien tuong": "431",
    "moc hoa": "431",
    "kien phong": "432",
    "cao lanh": "432",
    "dong thap": "432",
    "hong ngu": "432",
    "dinh tuong": "433",
    "cai lay": "433",
    "kien hoa": "434",
    "ben tre": "434",
    "truc giang": "434",
    "vinh binh": "435",
    "tra vinh": "435",
    "vinh long": "436",
    "an giang": "437",
    "long xuyen": "437",
    "kien giang": "438",
    "ha tien": "438",
    "chuong thien": "439",
    "vi thanh": "439",
    "phong dinh": "440",
    "ba xuyen": "441",
    "soc trang": "441",
    "khanh hung": "441",
    "an xuyen": "442",
    "ca mau": "442",
    "quan long": "442",
    "u minh": "442",
    "bac lieu": "443",
    "chau doc": "444",
    "tinh bien": "444",
    "seven mountains": "444",
    "that son": "444",
    "sa dec": "446",
    "rach gia": "491",
    "my tho": "492",
    "can tho": "493",
    "binh thuy": "493",
    # Misspellings and modern names that occur in the location column. Modern provinces that
    # straddle several wartime ones (e.g. Dong Nai) are deliberately not mapped.
    "binh doung": "323",
    "thus thien": "102",
    "gi dinh": "329",
    "ngha be": "329",
    "a luoi": "102",
    "a luoi camp": "102",
    "cua viet": "101",
    "cua viet river": "101",
    "chi lang": "444",
    "thu duc": "329",
    "phuoc binh": "321",
    "gia lai": "208",
    "plei djereng": "208",
    "dak nong": "215",
}

# Places that are not provinces, geocoded from GNS: place -> (kind, GNS keys used as anchors).
# Every name in PLACE_TO_CODE that is NOT here means its whole province (or HES city): wartime
# province names, their misspellings and modern names, HES cities (Da Nang, Nha Trang, Qui Nhon,
# Saigon ...), and names that are both a province and its capital (Pleiku, Kontum, Quang Tri,
# Tay Ninh, Bien Hoa, Vinh Long, Soc Trang, Chau Doc), where the text does not say which.
# Kinds: village/base 3 km (a hamlet, firebase, special forces camp, airstrip), town 5 km
# (district town), city 8 km (provincial capital), area 12 km (a battle named after a base or
# town, fought on the hills around it: Khe Sanh, Dak To, An Loc ...), valley / river: the circle
# covering the anchors + 10 km / 5 km.
NAMED_PLACES = {
    "khe sanh": ("area", ["khe sanh"]),
    "dong ha": ("town", ["dong ha"]),
    "con thien": ("base", ["con thien"]),
    "gio linh": ("town", ["gio linh"]),
    "cam lo": ("town", ["cam lo"]),
    "huong hoa": ("town", ["huong hoa"]),
    "lang vei": ("village", ["lang vei"]),
    "camp carroll": ("base", ["camp carroll"]),
    "rockpile": ("base", ["rockpile"]),
    # The A Shau valley runs ~25 km from the A Shau camp (south) to A Luoi (north).
    "a shau": ("valley", ["a shau", "a luoi"]),
    "a shau valley": ("valley", ["a shau", "a luoi"]),
    "a luoi": ("town", ["a luoi"]),
    "a luoi camp": ("town", ["a luoi"]),
    "phu bai": ("base", ["phu bai"]),
    "phong dien": ("town", ["phong dien"]),
    # The Cua Viet river runs ~15 km from Dong Ha to its mouth.
    "cua viet": ("river", ["cua viet", "dong ha"]),
    "cua viet river": ("river", ["cua viet", "dong ha"]),
    "hoi an": ("city", ["hoi an"]),
    "dien ban": ("town", ["dien ban"]),
    "hieu duc": ("town", ["hieu duc"]),
    "dai loc": ("town", ["dai loc"]),
    "duy xuyen": ("town", ["duy xuyen"]),
    "que son": ("town", ["que son"]),
    "thuong duc": ("base", ["thuong duc"]),
    "marble mountain": ("base", ["marble mountain"]),
    "tam ky": ("city", ["tam ky"]),
    "tien phuoc": ("town", ["tien phuoc"]),
    "hiep duc": ("town", ["hiep duc"]),
    "chu lai": ("area", ["chu lai"]),
    "kham duc": ("area", ["kham duc"]),
    "hau duc": ("town", ["hau duc"]),
    "duc pho": ("town", ["duc pho"]),
    "mo duc": ("town", ["mo duc"]),
    "son tinh": ("town", ["son tinh"]),
    "binh son": ("town", ["binh son"]),
    "nghia hanh": ("town", ["nghia hanh"]),
    "ba to": ("town", ["ba to"]),
    "tu nghia": ("town", ["tu nghia"]),
    "minh long": ("town", ["minh long"]),
    "dak to": ("area", ["dak to"]),
    "dak seang": ("base", ["dak seang"]),
    "ben het": ("base", ["ben het"]),
    "dak pek": ("base", ["dak pek"]),
    "polei kleng": ("base", ["polei kleng"]),
    "an khe": ("town", ["an khe"]),
    "bong son": ("town", ["bong son"]),
    "an lao": ("town", ["an lao"]),
    "hoai nhon": ("town", ["hoai nhon"]),
    "phu cat": ("town", ["phu cat"]),
    "tay son": ("town", ["tay son"]),
    "hoai an": ("town", ["hoai an"]),
    "phu my": ("town", ["phu my"]),
    "duc co": ("base", ["duc co"]),
    "plei me": ("base", ["plei me"]),
    "plei djereng": ("base", ["plei djereng"]),
    # GNS has the Ia Drang stream and the modern Ia Drang commune; the 1965 battle lies between.
    "ia drang": ("valley", ["ia drang"]),
    "ia drang valley": ("valley", ["ia drang"]),
    "cheo reo": ("town", ["cheo reo"]),
    "tuy hoa": ("city", ["tuy hoa"]),
    "ban me thuot": ("city", ["ban me thuot"]),
    "buon ma thuot": ("city", ["buon ma thuot"]),
    "phan rang": ("city", ["phan rang"]),
    "gia nghia": ("town", ["gia nghia"]),
    "bao loc": ("town", ["bao loc"]),
    "phan thiet": ("city", ["phan thiet"]),
    "ham tan": ("town", ["ham tan"]),
    "xuan loc": ("town", ["xuan loc"]),
    "bu dop": ("base", ["bu dop"]),
    "phuoc binh": ("town", ["phuoc binh"]),
    "an loc": ("area", ["an loc"]),
    "loc ninh": ("town", ["loc ninh"]),
    "quan loi": ("base", ["quan loi"]),
    "phu loi": ("base", ["phu loi"]),
    "lai khe": ("base", ["lai khe"]),
    "ben cat": ("town", ["ben cat"]),
    "iron triangle": ("area", ["iron triangle"]),
    "dau tieng": ("town", ["dau tieng"]),
    "nui ba den": ("area", ["nui ba den"]),
    "black virgin mountain": ("area", ["nui ba den"]),
    "trang bang": ("town", ["trang bang"]),
    "cu chi": ("town", ["cu chi"]),
    "duc hoa": ("town", ["duc hoa"]),
    "long binh": ("area", ["long binh"]),
    "ba ria": ("town", ["ba ria"]),
    "tan an": ("town", ["tan an"]),
    "rung sat": ("area", ["rung sat"]),
    "rung sat special zone": ("area", ["rung sat"]),
    "nha be": ("town", ["nha be"]),
    "ngha be": ("town", ["nha be"]),
    "tan son nhut": ("base", ["tan son nhut", "tan son nhat"]),
    "tan son nhat": ("base", ["tan son nhut", "tan son nhat"]),
    "cholon": ("town", ["cholon"]),
    "thu duc": ("town", ["thu duc"]),
    "moc hoa": ("town", ["moc hoa"]),
    "cao lanh": ("town", ["cao lanh"]),
    "hong ngu": ("town", ["hong ngu"]),
    "cai lay": ("town", ["cai lay"]),
    "ben tre": ("city", ["ben tre"]),
    "truc giang": ("city", ["truc giang"]),
    "tra vinh": ("city", ["tra vinh"]),
    "long xuyen": ("city", ["long xuyen"]),
    "ha tien": ("town", ["ha tien"]),
    "vi thanh": ("town", ["vi thanh"]),
    "khanh hung": ("city", ["khanh hung"]),
    "ca mau": ("city", ["ca mau", "quan long"]),
    "quan long": ("city", ["quan long", "ca mau"]),
    "u minh": ("area", ["u minh"]),
    "tinh bien": ("town", ["tinh bien"]),
    "seven mountains": ("area", ["that son"]),
    "that son": ("area", ["that son"]),
    "binh thuy": ("base", ["binh thuy"]),
    "chi lang": ("base", ["chi lang"]),
}
KIND_RADIUS_M = {
    "village": 3000.0,
    "base": 3000.0,
    "town": 5000.0,
    "city": 8000.0,
    "area": 12000.0,
    "valley": 10000.0,
    "river": 5000.0,
}  # valley/river: buffer around the anchors
CORROBORATE_M = 10_000.0
GNS_DISTRICT_RADIUS_M = 15000.0  # a modern district (ADM2) whose wartime extent is unknown
# GNS feature classes that can stand for each kind of place, best first.
KIND_CLASSES = {
    "village": "PSA",
    "base": "SPA",
    "town": "PAS",
    "city": "PAS",
    "area": "PSAT",
    "valley": "THPA",
    "river": "HPA",
}
DSG_RANK = (
    "PPLA",
    "PPLC",
    "PPLA2",
    "PPLA3",
    "PPL",
    "PPLL",
    "PPLX",
    "PPLQ",
    "PPLW",
    "PPLF",
    "AIRF",
    "AIRB",
    "MILB",
    "AIRQ",
    "AIRP",
    "ADM3",
    "ADM2",
    "VAL",
    "STMM",
    "STM",
)
# A GNS feature's own extent: the radius never shrinks below it (a district centroid is not a camp),
# nor below the feature's GNS uncertainty ("unc": extent + minute rounding of the GNS point).
FEATURE_EXTENT_M = {
    "PPLA": 8000.0,
    "PPLC": 8000.0,
    "PPLA2": 4000.0,
    "ADM2": 15000.0,
    "ADM3": 4000.0,
    "STM": 15000.0,
    "VAL": 10000.0,
    "STMM": 3000.0,
}

# Rows whose own cited source places the loss somewhere other than the location column:
# record id -> (cited page, what it says, place key naming the cited location). The cited page
# is cached under data/raw/wiki_mia/. The placement covers both places; the description says so.
CITATION_CONFLICTS = {
    "1967-schworer_ronald_p": (
        "https://www.virtualwall.org/ds/SchworerRP01a.htm",
        'location "Gia Dinh Province, South Vietnam"; lost on patrol "in the swamps of the Rung Sat"',
        "rung sat",
    ),
}


def dist_m(lon1, lat1, lon2, lat2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def cover(circles):
    """[(lon, lat, r)] -> (lon, lat, r): a circle containing every input circle. Centre = mean of
    centres for 3+, the exact enclosing circle's centre for 2; the radius is then measured, so the
    result always covers the inputs."""
    if len(circles) == 2:
        (x1, y1, r1), (x2, y2, r2) = circles
        d = dist_m(x1, y1, x2, y2)
        if d + r2 <= r1:
            return circles[0]
        if d + r1 <= r2:
            return circles[1]
        t = ((d + r2 - r1) / 2) / d  # fraction of the way from circle 1 to circle 2
        lon, lat = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
    else:
        lon = sum(c[0] for c in circles) / len(circles)
        lat = sum(c[1] for c in circles) / len(circles)
    r = max(dist_m(lon, lat, x, y) + rr for x, y, rr in circles)
    return round(lon, 6), round(lat, 6), round(r)


def gns_candidates(key, gns, prov, classes, every=False):
    """GNS features named `key` inside the province circle, of the best class in `classes`
    (then the best designation). Several left = homonyms we cannot tell apart: all returned.
    every=True returns every feature of any allowed class (for extended features: a valley's
    stream, villages and commune are all points along it)."""
    inside = [
        f
        for f in gns.get(key, ())
        if f["fc"] in classes and dist_m(prov["lon"], prov["lat"], f["lon"], f["lat"]) <= prov["radius_m"]
    ]
    if not inside or every:
        return inside

    def rank(f):
        """Class order first, then designation order (unlisted designations last)."""
        return (classes.index(f["fc"]), DSG_RANK.index(f["dsg"]) if f["dsg"] in DSG_RANK else len(DSG_RANK))

    best = min(rank(f) for f in inside)
    top = [f for f in inside if rank(f) == best]
    if len(top) > 1:
        # Homonyms: prefer those corroborated by another feature of the same name within
        # CORROBORATE_M (a town with its district / airfield / commune of that name), e.g. the
        # An Khe on Route 19 over a hamlet called An Khe elsewhere in the circle.
        backed = [
            f
            for f in top
            if any(
                g["ufi"] != f["ufi"] and dist_m(f["lon"], f["lat"], g["lon"], g["lat"]) <= CORROBORATE_M
                for g in gns.get(key, ())
            )
        ]
        if backed:
            top = backed
    return top


def hes_district(place, prov):
    """(district code, district) of the province's HES district whose name is or starts with
    `place`, else (None, None)."""
    for dcode, d in (prov.get("districts") or {}).items():
        if any(norm(n) == place or norm(n).startswith(place + " ") for n in d["names"]):
            return dcode, d
    return None, None


def geocode(place, text, prov, gns):
    """Place a named non-province place. Returns a placement dict, or None (use the province)."""
    if f" {place} district " in text:
        dcode, d = hes_district(place, prov)
        if d:
            return {
                "lon": d["lon"],
                "lat": d["lat"],
                "radius_m": d["radius_m"],
                "placement": "hes_district",
                "datum": "hes-district-centroid",
                "how": f"HES wartime district {dcode} {d['names'][0]} centroid, radius covering its hamlets + 10 km",
                "gns": [],
            }
        fs = [f for f in gns_candidates(place, gns, prov, "A", every=True) if f["dsg"] == "ADM2"]
        if fs:
            lon, lat, r = cover([(f["lon"], f["lat"], GNS_DISTRICT_RADIUS_M) for f in fs])
            return {
                "lon": lon,
                "lat": lat,
                "radius_m": r,
                "placement": "gns_district",
                "datum": "gns-place-geocode",
                "gns": fs,
                "how": f"GNS modern district '{fs[0]['name']}' (ADM2) centre, {r / 1000:.0f} km "
                f"(wartime district not in HES)",
            }
        return None  # a district we cannot find: the whole province, not a town-sized circle
    if place not in NAMED_PLACES:
        return None
    kind, keys = NAMED_PLACES[place]
    classes = KIND_CLASSES[kind]
    anchors = []
    for k in keys:
        fs = gns_candidates(k, gns, prov, classes, every=kind in ("valley", "river"))
        if fs:
            anchors.extend(fs)
            if kind not in ("valley", "river"):
                break  # first key that matches wins (later keys are alternative spellings)
    if not anchors:
        return None
    if kind in ("valley", "river"):
        lon, lat, r = cover([(f["lon"], f["lat"], float(f.get("unc") or 0.0)) for f in anchors])
        r = round(r + KIND_RADIUS_M[kind])
        how = (
            f"{kind} spanning GNS {', '.join(f['name'] + ' (' + f['dsg'] + ')' for f in anchors)}; "
            f"radius covers them + {KIND_RADIUS_M[kind] / 1000:.0f} km"
        )
    else:
        lon, lat, r = cover(
            [
                (
                    f["lon"],
                    f["lat"],
                    max(KIND_RADIUS_M[kind], FEATURE_EXTENT_M.get(f["dsg"], 0.0), float(f.get("unc") or 0.0)),
                )
                for f in anchors
            ]
        )
        how = (
            f"GNS '{anchors[0]['name']}' ({anchors[0]['dsg']}, ufi {anchors[0]['ufi']})"
            + (f" and {len(anchors) - 1} homonym(s) in the province" if len(anchors) > 1 else "")
            + f"; {kind}, radius {r / 1000:.0f} km"
        )
    return {
        "lon": lon,
        "lat": lat,
        "radius_m": r,
        "placement": "gns_place",
        "datum": "gns-place-geocode",
        "how": how,
        "gns": anchors,
        "kind": kind,
    }


# Location text that means the loss was outside RVN provinces we can place.
OUTSIDE = (
    ("gulf of tonkin", "at_sea"),
    ("gulf of thailand", "at_sea"),
    ("south china sea", "at_sea"),
    ("at sea", "at_sea"),
    ("over water", "at_sea"),
    ("offshore", "at_sea"),
    ("north vietnam", "north_vietnam"),
    ("laos", "laos"),
    ("cambodia", "cambodia"),
    ("thailand", "thailand"),
    ("china", "china"),
    ("demilitarized zone", "dmz"),
    ("dmz", "dmz"),
)


# Post-1975 provinces that merge several wartime ones: place at a circle covering all of them.
MODERN_MULTI = {
    "dong thap": ("432", "446"),  # Kien Phong + Sa Dec
    "song be": ("321", "322", "323"),  # Phuoc Long + Binh Long + Binh Duong ("Song Be Province")
    "dong nai": ("326", "319"),  # Bien Hoa + Long Khanh
    "gia lai": ("208", "209"),  # Pleiku + Phu Bon
}


def merged_province(codes, provinces):
    """Centroid (mean of centres) and a radius covering every member province's circle."""
    lon, lat, r = cover([(provinces[c]["lon"], provinces[c]["lat"], provinces[c]["radius_m"]) for c in codes])
    return {"names": ["+".join(provinces[c]["names"][0] for c in codes)], "lon": lon, "lat": lat, "radius_m": r}


def locate(location, provinces):
    """Return (province_record, matched_place, reason). province None means skip, with reason.

    Several names can match ("Bac Lieu Province, Soc Trang"): a name written as "<X> Province"
    wins, then the longest name."""
    n = " " + norm(location) + " "
    for key, why in OUTSIDE:
        # "dmz" only skips when no province or place is named (checked below).
        if f" {key} " in n and why != "dmz":
            return None, None, why
    for place, codes in MODERN_MULTI.items():
        if f" {place} province " in n:
            p = merged_province(codes, provinces)
            return dict(p, code="+".join(codes)), place, "modern_province"
    best = None
    for place, code in PLACE_TO_CODE.items():
        if f" {place} " in n:
            score = (f" {place} province " in n, len(place))
            if best is None or score > best[0]:
                best = (score, place, code)
    if best:
        return dict(provinces[best[2]], code=best[2]), best[1], "province"
    if " dmz " in n or " demilitarized zone " in n:
        return None, None, "dmz"
    if " south vietnam " in n or " vietnam " in n:
        return None, None, "no_province"
    return None, None, "unknown_location" if n.strip() else "no_location"


def place_row(location, provinces, gns):
    """Location text -> placement dict {lon, lat, radius_m, placement, datum, how, code, province,
    place, gns}, or (None, reason) to skip."""
    p, place, reason = locate(location, provinces)
    if p is None:
        return None, reason
    base = {"code": p["code"], "province": p["names"][0], "place": place, "gns": [], "province_radius_m": p["radius_m"]}
    n = " " + norm(location) + " "
    if reason == "province" and f" {place} province " not in n:
        g = geocode(place, n, p, gns)
        if g:
            return dict(base, **g), None
        if place in NAMED_PLACES or f" {place} district " in n:
            return dict(
                base,
                lon=p["lon"],
                lat=p["lat"],
                radius_m=p["radius_m"],
                placement="province_fallback",
                datum="province-centroid",
                how=f"'{place}' not found in GNS/HES inside {p['names'][0]}: whole-province "
                f"circle (centroid, farthest hamlet + 10 km)",
            ), None
    how = (
        f"{p['names'][0]} province centroid, radius covering the province (farthest HES hamlet + 10 km)"
        if reason == "province"
        else f"circle covering the wartime provinces {p['names'][0]}"
    )
    return dict(
        base, lon=p["lon"], lat=p["lat"], radius_m=p["radius_m"], placement=reason, datum="province-centroid", how=how
    ), None


def apply_conflict(rid, pl, provinces, gns):
    """Widen a placement to also cover the place the row's own citation names."""
    url, says, cited = CITATION_CONFLICTS[rid]
    other, _ = place_row(f"South Vietnam, {cited}", provinces, gns)
    lon, lat, r = cover([(pl["lon"], pl["lat"], pl["radius_m"]), (other["lon"], other["lat"], other["radius_m"])])
    sep = dist_m(pl["lon"], pl["lat"], other["lon"], other["lat"])
    note = (
        f"CONFLICT: the row says {pl['province']} but its cited source ({url}) gives {says}; "
        f"the two placements are {sep / 1000:.0f} km apart, so the circle covers both "
        f"({pl['how']}; and {other['how']})."
    )
    return dict(
        pl,
        lon=lon,
        lat=lat,
        radius_m=r,
        placement="citation_conflict",
        datum="conflict-cover",
        how=note,
        conflict={
            "cited_url": url,
            "cited_says": says,
            "row_placement": pl["placement"],
            "cited_placement": other["placement"],
            "cited_province": other["province"],
            "separation_m": round(sep),
        },
    )


def record_ids(rows):
    """[(year, cells)] -> ids derived from content, never row order: "<year>-<name>"; when rows
    share that, "-<date>" is added; when they still collide, "-<hash of unit/location/text>"."""

    def name_id(y, c):
        return f"{y}-{norm(c['name']).replace(' ', '_')}"

    def date_id(y, c):
        return f"{name_id(y, c)}-{parse_date(c['date'], y) or 'nodate'}"

    def hash_id(y, c):
        h = hashlib.sha1(norm(" ".join(c[k] for k in ("unit", "location", "circumstances"))).encode()).hexdigest()[:6]
        return f"{date_id(y, c)}-{h}"

    ids = [name_id(y, c) for y, c in rows]
    for step in (date_id, hash_id):
        dup = {i for i, k in Counter(ids).items() if k > 1}
        ids = [step(y, c) if i in dup else i for i, (y, c) in zip(ids, rows, strict=True)]
    return ids


def records(pages, provinces, gns, stats=None):
    """{period: wikitext} -> (evidence records, stats Counter).

    Only rows in a year section with a name and an unrecovered status are kept; burials and
    rows that cannot be placed in South Vietnam are counted in stats."""
    stats = stats if stats is not None else Counter()
    rows = []  # (period, year, row index, raw cells, clean cells)
    for period, text in pages.items():
        for section, idx, cells in parse_tables(text):
            if not re.fullmatch(r"\d{4}", section or ""):
                continue
            stats["rows"] += 1
            c = {k: clean(v) for k, v in cells.items()}
            if not c["name"]:
                stats["skip_no_name"] += 1
                continue
            rows.append((period, section, idx, cells, c))
    ids = record_ids([(y, c) for _, y, _, _, c in rows])
    assert len(set(ids)) == len(ids), "record ids are not unique"
    out = []
    for rid, (period, section, idx, cells, c) in zip(ids, rows, strict=True):
        cls = status_class(c["status"])
        stats[f"status_{cls}"] += 1
        if cls != "unrecovered":
            continue
        if BURIAL_RE.search(c["status"]):
            stats["dropped_burial"] += 1
            continue
        pl, reason = place_row(c["location"], provinces, gns)
        if pl is None:
            stats[f"skip_{reason}"] += 1
            continue
        if rid in CITATION_CONFLICTS:
            pl = apply_conflict(rid, pl, provinces, gns)
        stats[f"placement_{pl['placement']}"] += 1
        date = parse_date(c["date"], section)
        kind = "aircraft_crash" if AIR_RE.search(c["circumstances"]) else "loss_incident"
        raw = dict(
            c,
            period=period,
            year=section,
            row=idx,
            province_code=pl["code"],
            province=pl["province"],
            matched_place=pl["place"],
            status_class=cls,
            ref_urls=ref_urls(cells["circumstances"] + cells["status"] + cells["name"]),
            placement=pl["placement"],
            placement_note=pl["how"],
            hes_province_radius_m=pl["province_radius_m"],
            gns_features=[{k: f[k] for k in ("name", "dsg", "fc", "ufi", "lon", "lat")} for f in pl["gns"]],
            licence="CC BY-SA 4.0",
        )
        if "conflict" in pl:
            raw["citation_conflict"] = pl["conflict"]
        out.append(
            store.Evidence(
                source=SOURCE,
                source_record_id=rid,
                kind=kind,
                lon=pl["lon"],
                lat=pl["lat"],
                uncertainty_m=float(pl["radius_m"]),
                original_coord=c["location"],
                original_datum=pl["datum"],
                title=f"{c['rank']} {c['name']} ({c['service']}) — {c['status'][:60]}".strip(),
                date_start=date,
                date_end=date,
                description=f"{c['unit']}. {c['circumstances']} Status: {c['status']}. "
                f"Placement ({pl['placement']}, matched '{pl['place']}'): {pl['how']}.",
                url=page_url(period),
                raw=raw,
            )
        )
        stats["kept"] += 1
    return out, stats


def save(db, recs):
    """Register the source and make the store hold exactly `recs` for it (store.sync_source):
    rows that are now recovered/identified, or no longer listed, are removed (or withdrawn when an
    analyst has them in a case). Returns (kept, deleted, withdrawn)."""
    store.upsert_source(
        db,
        SOURCE,
        name="Wikipedia: List of US servicemembers and civilians MIA during the Vietnam War",
        url=page_url(PERIODS[0]),
        licence="CC BY-SA 4.0",
        notes="5 period pages, raw wikitext. Only rows still unaccounted for (identified/recovered "
        "remains are known burials and excluded). No coordinates in the source: named places "
        "geocoded from GNS, districts from HES, else HES province circles covering the whole "
        "province. Out-of-RVN and province-less rows skipped.",
    )
    return store.sync_source(db, SOURCE, recs, reason="no longer an unaccounted-for row on the page")


def main(argv=None):
    """Fetch (or reuse the cache), ingest, sync the store and write the run summary."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--refresh", action="store_true", help="re-download the Wikipedia pages")
    args = ap.parse_args(argv)
    pages = fetch_pages(refresh=args.refresh)
    recs, stats = records(pages, load_provinces(), load_gns())
    n, deleted, withdrawn = save(store.connect(), recs)
    in_aoi = sum(common.in_bbox(e.lon, e.lat) for e in recs)
    summary = {
        "ingested": n,
        "deleted": deleted,
        "withdrawn": withdrawn,
        "in_area_of_interest": in_aoi,
        "kinds": dict(Counter(e.kind for e in recs)),
        "by_placement": dict(Counter(e.raw["placement"] for e in recs)),
        "by_province": dict(Counter(e.raw["province"] for e in recs).most_common()),
        "stats": dict(stats),
    }
    (WORK / "wiki_mia_summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    return summary


if __name__ == "__main__":
    main()
