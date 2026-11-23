"""map_library_catalogue: the committed catalogue is well formed and complete, and the pieces that
build it (sheet grid, titles, spelling, bounds) give the answers checked against real sheets."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import map_library_catalogue as mlc
import pytest
from pypdf import PdfWriter
from pypdf.generic import NameObject, TextStringObject

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = json.loads(mlc.OUT.read_text(encoding="utf-8"))
LEGACY = json.loads(mlc.LEGACY.read_text(encoding="utf-8"))
MAPS = CATALOGUE["maps"]
# Vietnam's mainland with a little slack, for "this sheet is about Vietnam" checks.
VIETNAM = (102.1, 8.2, 109.6, 23.45)


GEOPDF_SERIES = {"l7014", "jog1501", "l909"}
PLACED_SERIES = {"sgi100k", "l509", "warper"}  # placed by scripts/place_sheets.py or copied from the Warper


def by_series(series):
    return [m for m in MAPS if m["series"] == series]


# --- the committed catalogue ------------------------------------------------------------------


def test_catalogue_shape():
    assert isinstance(CATALOGUE["about"], str) and CATALOGUE["about"]
    assert set(CATALOGUE["series"]) == GEOPDF_SERIES | PLACED_SERIES | {"scans"}
    for meta in CATALOGUE["series"].values():
        assert set(meta) == {"name", "scale", "publisher", "years", "licence", "notes", "disk_per_download_byte"}
        assert all(isinstance(v, str) and v for k, v in meta.items() if k != "disk_per_download_byte")
        assert 1 < meta["disk_per_download_byte"] < 5
    required = {"id", "series", "title", "bounds", "region", "urls", "bytes", "sha256", "kind"}
    # What the viewer's Old maps card sorts, filters and groups by.
    required |= {"year", "scale_n", "place", "sheet_key"}
    for m in MAPS:
        assert required <= set(m), m["id"]
        assert m["series"] in CATALOGUE["series"]
        assert m["kind"] == ("geopdf" if m["series"] in GEOPDF_SERIES else "scan")
        assert m["year"] is None or 1850 <= m["year"] <= 2010
        assert m["place"] and m["sheet_key"]
        assert m["region"] in ("north", "centre", "south")
        assert isinstance(m["title"], str) and m["title"].strip() == m["title"] and m["title"]
        assert isinstance(m["bytes"], int) and m["bytes"] > 100_000
        assert m["sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", m["sha256"])
        assert m["urls"] and all(isinstance(u, str) for u in m["urls"])


def test_counts_per_series():
    # 501 hosted 15' cells (L7014 472, L7015 22, L7016 7), less three that cannot be added
    # (mlc.UNUSABLE: two L7014, one L7015); 32 JOG sheets on the USGS Store and two from the
    # University of Texas; 18 city plans; 7 hand-placed scans.
    assert len(by_series("l7014")) == 498
    assert sum(m["id"].startswith("L7015") for m in MAPS) == 21
    assert sum(m["id"].startswith("L7016") for m in MAPS) == 7
    assert len(by_series("jog1501")) == len(mlc.JOG_SHEETS) + len(mlc.JOG_UT_SHEETS) == 34
    assert len(by_series("l909")) == len(mlc.L909_SHEETS) == 18
    assert len(by_series("scans")) == 7
    # The automatically placed series: as many as passed place_sheets.py's checks.
    assert len(by_series("sgi100k")) > 200 and len(by_series("l509")) > 25 and len(by_series("warper")) >= 6


def test_ids_titles_and_urls_are_unique():
    ids = [m["id"] for m in MAPS]
    assert len(ids) == len(set(ids))
    # The id names the installed file (web/tiles/<id>.mbtiles), so it must be a safe file name.
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", i) and not i.startswith(".") for i in ids)
    # Editions of one scanned sheet share its title (the year is shown beside it); the PDF sheets do not.
    titles = [m["title"] for m in MAPS if m["series"] in GEOPDF_SERIES]
    assert len(titles) == len(set(titles))
    urls = [u for m in MAPS for u in m["urls"]]
    assert len(urls) == len(set(urls))


def test_every_url_is_https_on_an_allowed_host():
    for m in MAPS:
        for url in m["urls"]:
            parts = urlsplit(url)
            assert parts.scheme == "https", url
            assert parts.hostname in mlc.ALLOWED_HOSTS, url
            assert not parts.username and not parts.port, url
        if m["kind"] == "geopdf" and m["id"].removeprefix("JOG_") not in mlc.JOG_UT_SHEETS:
            assert all(u.startswith("https://store.usgs.gov/") for u in m["urls"][:2]), m["id"]


def test_bounds_are_sane_and_touch_vietnam():
    for m in MAPS:
        w, s, e, n = m["bounds"]
        assert -180 <= w < e <= 180 and -90 <= s < n <= 90, m["id"]
        # Sheets are at most 1:250,000 (1.5 x 1 degrees); the Admiralty charts cover a whole gulf.
        limit = 15 if m["series"] == "warper" else 3
        assert e - w <= limit and n - s <= limit, m["id"]
        assert w < VIETNAM[2] and e > VIETNAM[0] and s < VIETNAM[3] and n > VIETNAM[1], m["id"]
        assert m["region"] == mlc.region_for(m["bounds"]), m["id"]


def test_l7014_bounds_follow_the_sheet_grid():
    for m in by_series("l7014"):
        sheet = mlc.parse_l7014_stem(m["id"])
        assert sheet["id"] == m["id"]
        # Except the few sheets whose printed frame is not their cell (they overlap it).
        cell = mlc.l7014_cell_bounds(sheet["sheet"], sheet["quadrant"])
        if m["id"] in mlc.FRAME_OVERRIDES:
            w, s, e, n = m["bounds"]
            assert w < cell[2] and cell[0] < e and s < cell[3] and cell[1] < n
        else:
            assert m["bounds"] == cell
        assert m["title"].endswith(f"({sheet['sheet']} {mlc.QUADRANTS[sheet['quadrant']]})")


def test_the_sheets_the_project_started_with_keep_their_ids_and_checksums():
    entries = {m["id"]: m for m in MAPS}
    for old in LEGACY["l7014"]:
        new = entries[old["id"]]
        assert new["sha256"] == old["sha256"] and new["bytes"] == old["bytes"]
        # The old manifest's address stays one of the ones tried.
        assert old["url"] in new["urls"]


def test_scans_keep_every_field_the_downloader_needs():
    entries = {m["id"]: m for m in by_series("scans")}
    assert set(entries) == {s["id"] for s in LEGACY["scans"]}
    # Every control file belongs to a hand-placed scan or a placed sheet of the catalogue, and back.
    placed = {m["id"] for m in MAPS if m["series"] in PLACED_SERIES}
    assert set(entries) | placed == {p.stem for p in mlc.SCANS.glob("*.json")}
    for old in LEGACY["scans"]:
        new = entries[old["id"]]
        assert new["urls"] == [old["url"]]
        for key, value in old.items():
            if key != "url":
                assert new[key] == value, (old["id"], key)
        assert new["licence"]


def test_jog_sheets_have_checksums_and_three_addresses():
    for m in by_series("jog1501"):
        sheet = m["id"].removeprefix("JOG_")
        if sheet in mlc.JOG_UT_SHEETS:
            assert [urlsplit(u).hostname for u in m["urls"]] == ["maps.lib.utexas.edu"]
            continue
        assert sheet in mlc.JOG_SHEETS
        assert m["sha256"] is not None
        assert [urlsplit(u).hostname for u in m["urls"]] == ["store.usgs.gov", "store.usgs.gov", "maps.lib.utexas.edu"]
        assert m["title"].endswith(f"({mlc.jog_label(sheet)})")


# --- the pieces -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sheet, quadrant, bounds",
    [
        # Neatline corners of real sheets: 6129 I Sa Dec, 6129 II Can Tho, 6029 IV Tri Ton, 5453 III.
        ("6129", 1, [105.75, 10.25, 106.0, 10.5]),
        ("6129", 2, [105.75, 10.0, 106.0, 10.25]),
        ("6029", 4, [105.0, 10.25, 105.25, 10.5]),
        ("5453", 3, [102.0, 22.0, 102.25, 22.25]),
    ],
)
def test_l7014_cell_bounds(sheet, quadrant, bounds):
    assert mlc.l7014_cell_bounds(sheet, quadrant) == bounds


def test_parse_l7014_stem():
    assert mlc.parse_l7014_stem("L701461292_geo") == {
        "id": "L701461292",
        "series": "L7014",
        "sheet": "6129",
        "quadrant": 2,
    }
    assert mlc.parse_l7014_stem("L701461301_002_geo")["id"] == "L701461301_002"
    assert mlc.parse_l7014_stem("L701554533")["series"] == "L7015"
    for bad in ("L701461295_geo", "L70146129_geo", "../L701461292", "JOG_NC4806"):
        with pytest.raises(ValueError):
            mlc.parse_l7014_stem(bad)


def test_region_for():
    assert mlc.region_for([105.75, 10.0, 106.0, 10.25]) == "south"
    assert mlc.region_for([107.0, 11.25, 107.25, 11.5]) == "south"
    assert mlc.region_for([107.0, 11.5, 107.25, 11.75]) == "centre"
    assert mlc.region_for([105.75, 19.75, 106.0, 20.0]) == "centre"
    assert mlc.region_for([105.75, 20.0, 106.0, 20.25]) == "north"


@pytest.mark.parametrize(
    "title, name",
    [
        ("Vietnam 1:50,000. Can Tho, Vietnam. [L7014 61292].", "Can Tho"),
        ("Vietnam 1:50,000. Phong Phu, O Mon, Vietnam. [L7014 61293].", "Phong Phu, O Mon"),
        ("LAOS 1:50,000. FU CHAI, LAOS; VIETMAN. [L7015 54521].", "Fu Chai"),
        ("LAOS 1:50,000. BAN THIN TOC =, LAOS; VIETNAM. [L7015 55511].", "Ban Thin Toc"),
        ("JOINT OPERATIONS GRAPHIC-AIR. LONG XUYEN, VIETNAM; CAMBODIA. [1501A NC4806].", "Long Xuyen"),
        ("Joint Operations Graphic-Air. Saigon, Vietnam; Kampuchea [Cambodia]. [1501A NC4807].", "Saigon"),
        ("Joint Operations Graphic-Air. Cao Bang. China; Vietnam. [1501A NF4807].", "Cao Bang"),
        # Real oddities: countries after a comma, no separator, an abbreviation, a name with "Lao".
        ("Vietnam 1:50,000. Lang Mo, Vietnam, Laos. [L7014 62432].", "Lang Mo"),
        ("Vietnam 1:50,000. Bao Ha Vietnam. [L7014 59532].", "Bao Ha"),
        (
            "Vietnam 1:50,000. Thanh Pho HoHiMinh Ho Chi Minh City, VM. [L7014 63304].",
            "Thanh Pho HoHiMinh Ho Chi Minh City",
        ),
        ("Vietnam 1:50,000. G. Rieng, Vietnam. [L7014 65392].", "G. Rieng"),
        ("Vietnam 1:50,000. An Lao, Vietnam. [L7014 67382].", "An Lao"),
        ("Vietnam 1:50,000. [L7014 66324].", None),
        ("Map Edition", None),
        ("", None),
    ],
)
def test_printed_name(title, name):
    assert mlc.printed_name(title) == name


def test_xmp_title_takes_the_last_update():
    data = b"..<pdfx:title>old</pdfx:title>..<pdfx:title>Vietnam 1:50,000. X, Vietnam. [L7014 1].</pdfx:title>"
    assert mlc.xmp_title(data) == "Vietnam 1:50,000. X, Vietnam. [L7014 1]."
    assert mlc.xmp_title(b"no metadata here") is None


def gns_row(name, lon, lat, fc="P", desig="PPL", nt="N", display="1,2,3"):
    return {
        "full_name": name,
        "long_dd": str(lon),
        "lat_dd": str(lat),
        "fc": fc,
        "desig_cd": desig,
        "nt": nt,
        "display": display,
    }


SHEET = [106.5, 17.0, 108.0, 18.0]  # JOG NE 48-12, Dong Hoi


def test_spelling_prefers_the_notable_place_of_that_name_on_the_sheet():
    g = mlc.Gazetteer(
        [
            gns_row("Động Hỏi", 106.63, 17.5),  # a hamlet near the town
            gns_row("Đồng Hới", 106.62, 17.47, desig="PPLA"),
            gns_row("Đồng Hội", 105.7, 20.2),  # same unaccented name, off the sheet
            gns_row("Dong Hoi", 106.6, 17.4, nt="V"),  # unaccented variant: never used
        ]
    )
    assert g.spell("Dong Hoi", SHEET) == "Đồng Hới"
    assert g.spell("Nowhere", SHEET) == "Nowhere"


def test_spelling_ignores_spaces_hyphens_and_admin_words():
    g = mlc.Gazetteer(
        [
            gns_row("Thành Phố Hà Nội", 105.85, 21.03, desig="PPLC"),
            gns_row("Thanh Hóa", 105.77, 19.8, desig="PPLA"),
            gns_row("Ô Môn", 105.62, 10.11),
        ]
    )
    assert g.spell("Hanoi", [105.0, 21.0, 106.5, 22.0]) == "Hà Nội"
    assert g.spell("Thanh-Hoa", [105.0, 19.0, 106.5, 20.0]) == "Thanh Hóa"
    assert g.spell("Phong Phu, O Mon", [105.5, 10.0, 105.75, 10.25]) == "Phong Phu, Ô Môn"


def test_sheet_without_a_printed_name_is_named_after_its_most_notable_place():
    g = mlc.Gazetteer(
        [
            gns_row("Ấp Một", 105.9, 10.6, display="1,2,3,4,5"),
            gns_row("Mỹ An", 105.8, 10.55, desig="PPLA2"),
            gns_row("Cần Thơ", 105.78, 10.03, desig="PPLA"),
        ]
    )
    assert mlc.sheet_title(None, "6130 II", [105.75, 10.5, 106.0, 10.75], g) == "Mỹ An (6130 II)"
    assert mlc.sheet_title("Can Tho", "6129 II", [105.75, 10.0, 106.0, 10.25], g) == "Cần Thơ (6129 II)"
    # Nothing on a sea sheet: the nearest place, said so.
    assert mlc.sheet_title(None, "6229 I", [106.25, 10.25, 106.5, 10.5], g) == "Near Ấp Một (6229 I)"


def test_l7014_entries_from_a_probe():
    probe = [
        {"id": "L701461292_geo", "url": mlc.USGS_MOD + "L701461292_geo.pdf", "bytes": 6522618, "exists": True},
        {"id": "L701468362_geo", "url": mlc.USGS_YIMAGES + "L701468362_geo.pdf", "bytes": 7778920, "exists": True},
        {"id": "L701461304", "url": mlc.USGS_MOD + "L701461304_geo.pdf", "bytes": None, "exists": False},
    ]
    legacy = [{"id": "L701461292", "url": "u", "bytes": 6522618, "sha256": "ab" * 32}]
    tails = {"L701461292": b"<pdfx:title>Vietnam 1:50,000. Can Tho, Vietnam. [L7014 61292].</pdfx:title>"}
    g = mlc.Gazetteer([gns_row("Cần Thơ", 105.78, 10.03, desig="PPLA")])
    entries = mlc.l7014_entries(probe, legacy, g, tail_for=lambda i, u: tails.get(i, b""))
    assert [e["id"] for e in entries] == ["L701461292", "L701468362"]
    can_tho, qui_nhon_area = entries
    assert can_tho["title"] == "Cần Thơ (6129 II)" and can_tho["sha256"] == "ab" * 32
    assert can_tho["urls"] == [mlc.USGS_MOD + "L701461292_geo.pdf", mlc.USGS_YIMAGES + "L701461292_geo.pdf"]
    # Found only under yimages: no second address, no checksum yet.
    assert qui_nhon_area["urls"] == [mlc.USGS_YIMAGES + "L701468362_geo.pdf"] and qui_nhon_area["sha256"] is None
    assert qui_nhon_area["title"] == "Near Cần Thơ (6836 II)"
    with pytest.raises(SystemExit):
        mlc.l7014_entries(probe[1:], legacy, g, tail_for=lambda i, u: b"")


def test_jog_entry_reads_bounds_and_title_from_the_pdf(tmp_path):
    w = PdfWriter()
    w.add_blank_page(72, 72)
    w.add_metadata(
        {
            NameObject("/title"): TextStringObject(
                "JOINT OPERATIONS GRAPHIC-AIR. LONG XUYEN, VIETNAM; CAMBODIA. [1501A NC4806]."
            ),
            NameObject("/ll_long"): TextStringObject("+104.250000000"),
            NameObject("/ll_lat"): TextStringObject("10.000000000"),
            NameObject("/ur_long"): TextStringObject("+105.750000000"),
            NameObject("/ur_lat"): TextStringObject("+11.000000000"),
        }
    )
    pdf = tmp_path / "1501ANC4806_geo.pdf"
    with open(pdf, "wb") as f:
        w.write(f)
    g = mlc.Gazetteer([gns_row("Long Xuyên", 105.43, 10.38, desig="PPLA")])
    e = mlc.jog_entry("NC4806", pdf, g)
    assert e["id"] == "JOG_NC4806" and e["title"] == "Long Xuyên (NC 48-6)"
    assert e["bounds"] == [104.25, 10.0, 105.75, 11.0] and e["region"] == "south"
    assert e["bytes"] == pdf.stat().st_size and e["sha256"] == mlc.sha256_of(pdf)
    assert e["urls"][-1] == "https://maps.lib.utexas.edu/maps/jog/vietnam/nc-4806_geo.pdf"


def test_scan_bounds_come_from_the_neatline():
    control = {
        "gcps": [[0, 0, 105.0, 11.0], [1000, 0, 106.0, 11.0], [0, 500, 105.0, 10.5], [1000, 500, 106.0, 10.5]],
        "neatline": [[100, 50], [900, 50], [900, 450], [100, 450]],
        "offset_deg": [0.01, 0.0],
    }
    assert mlc.scan_bounds(control) == [105.11, 10.55, 105.91, 10.95]


def test_probe_from_catalogue_round_trips_the_l7014_sheets():
    probe = mlc.probe_from_catalogue(CATALOGUE)
    assert len(probe) == 498
    assert all(p["url"].startswith(mlc.USGS_MOD) for p in probe if "L701558504" in p["id"])
    assert {mlc.parse_l7014_stem(p["id"])["id"] for p in probe} == {m["id"] for m in by_series("l7014")}


def test_sheets_broken_on_the_archive_are_left_out_or_fetched_from_the_mirror_first():
    probe = [
        {"id": "L701467311_geo", "url": mlc.USGS_MOD + "L701467311_geo.pdf", "bytes": 9812257, "exists": True},
        {"id": "L701558504_geo", "url": mlc.USGS_MOD + "L701558504_geo.pdf", "bytes": 9000000, "exists": True},
    ]
    entries = mlc.l7014_entries(probe, [], mlc.Gazetteer([]), tail_for=lambda i, u: b"")
    assert [e["id"] for e in entries] == ["L701558504"]
    assert entries[0]["urls"] == [mlc.USGS_YIMAGES + "L701558504_geo.pdf", mlc.USGS_MOD + "L701558504_geo.pdf"]
    by_id = {m["id"]: m for m in MAPS}
    assert not set(mlc.UNUSABLE) & set(by_id)
    assert "L701467311" not in by_id and by_id["L701558504"]["urls"][0].startswith(mlc.USGS_YIMAGES)
