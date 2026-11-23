"""sheet_lists: sheet numbers and grids of the automatically placed series; the catalogue entries and
viewer fields built from them; downloads of images whose size is only estimated."""

import json

import download_maps
import map_library_catalogue as mlc
import pytest
import sheet_lists


@pytest.mark.parametrize(
    ("title", "sheet"),
    [
        ("Carte de l'Indochine : Pointe de Cà Mau est : feuille n°244E", "244E"),
        ("Carte de l'Indochine : Long-Xuyên : feuille n°228", "228"),
        ("Carte de l'Indochine : Pailin ouest : feuille n°174bisW", "174bisW"),
        ("Carte de l'Indochine : Ha-Lang Sud : feuille n°10bis", "10bis"),
        ("Carte de l'Indochine : Vinh ouest : feuille n° 95 O", "95W"),
        ("Tuyen-Quang", ""),
    ],
)
def test_sheet_numbers(title, sheet):
    assert sheet_lists.sheet_number(title) == sheet


def test_place_names():
    assert (
        sheet_lists.place_of("Carte de l'Indochine : Pointe de Cà Mau est : feuille n°244E") == "Pointe de Cà Mau est"
    )
    assert sheet_lists.place_of("Bac-Kan") == "Bac-Kan"


@pytest.mark.parametrize(
    ("band", "zone", "number", "bounds"),
    [
        ("nf", 48, 10, [103.5, 21, 105.0, 22]),  # Yen Bai, as the JOG grid has it
        ("nf", 48, 11, [105.0, 21, 106.5, 22]),  # Hanoi
        ("nc", 48, 3, [105.0, 11, 106.5, 12]),  # Prey Veng
        ("ne", 49, 13, [108.0, 16, 109.5, 17]),
    ],
)
def test_l509_cells_follow_the_250k_grid(band, zone, number, bounds):
    assert sheet_lists.l509_cell(band, zone, number) == bounds


INDEX = """
<a href="http://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/txu-oclc-6535632-nf48-11.jpg">NF 48-11 Hanoi</a>
<a href="http://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/txu-oclc-6535632-nf48-11a.jpg">NF 48-11 [verso] Glossary</a>
<a href="http://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/txu-oclc-6535632-nc48-8-3rd-ed.jpg">NC 48-8 Phan Thiet [with inset maps of Poulo Cecir de Mer]</a>
<a href="http://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/txu-oclc-6535632-nc48-6-2nd-ed.jpg">NC 48-6 Long Xuyen</a>
<a href="http://maps.lib.utexas.edu/maps/ams/indochina_and_thailand/txu-oclc-6535632-nd47-4.jpg">ND 47-4 Nakhon Ratchasima</a>
"""


def test_l509_specs_keep_sheets_of_vietnam_and_skip_backs_and_hand_placed_ones(tmp_path, monkeypatch):
    page = tmp_path / "index.html"
    page.write_text(INDEX, encoding="utf-8")
    monkeypatch.setattr(sheet_lists.common, "cached_get", lambda *a, **k: page)
    specs = sheet_lists.l509_specs({"NF4811", "NC4808", "NC4806"}, {"NC4808": [107.25, 10.0, 108.75, 11.0]})
    assert [s["id"] for s in specs] == ["l509_nf48-11", "l509_nc48-8_ed3"]
    hanoi, phan_thiet = specs
    assert hanoi["place"] == "Hanoi" and hanoi["bounds"] == [105.0, 21, 106.5, 22] and hanoi["dark_ink_only"]
    assert phan_thiet["place"] == "Phan Thiet" and phan_thiet["bounds"] == [107.25, 10.0, 108.75, 11.0]
    assert phan_thiet["meta"]["title"] == "Phan Thiet (NC 48-8)" and "edition 3" in phan_thiet["meta"]["dateNote"]


def placed(tmp_path, spec_id, gcps, **extra):
    control = {"gcps": gcps, "neatline": [[0, 0], [100, 0], [100, 100], [0, 100]], "work_size": [100, 100], **extra}
    (tmp_path / f"{spec_id}.json").write_text(json.dumps(control), encoding="utf-8")


SPEC = {
    "id": "sgi100k_228_x",
    "series": "sgi100k",
    "sheet": "228",
    "place": "Long-Xuyên",
    "date": "1936-04",
    "image": {"iiif": "https://api.nakala.fr/iiif/a/b"},
    "download": "https://api.nakala.fr/iiif/a/b/full/max/0/default.jpg",
    "page": "https://nakala.fr/a",
    "licence": "Centre IST REGARDS (CNRS) on NAKALA: CC BY-NC-SA 4.0.",
    "credit": "Old map: SGI (CC BY-NC-SA 4.0)",
    "meta": {"title": "Long-Xuyên (feuille 228)", "scale": "1:100,000"},
}
GCPS = [[0, 0, 105.15, 10.77], [100, 0, 105.84, 10.77], [0, 100, 105.15, 10.32]]


def test_only_placed_sheets_in_or_near_vietnam_are_catalogued(tmp_path):
    placed(tmp_path, "sgi100k_228_x", GCPS, source_bytes=5_700_000)
    placed(tmp_path, "sgi100k_china_x", [[0, 0, 110.2, 21.4], [100, 0, 110.9, 21.4], [0, 100, 110.2, 21.0]])
    china = {**SPEC, "id": "sgi100k_china_x"}
    unplaced = {**SPEC, "id": "sgi100k_229_x"}
    entries = mlc.sheet_entries([SPEC, china, unplaced], tmp_path)
    assert [e["id"] for e in entries] == ["sgi100k_228_x"]
    e = entries[0]
    assert e["kind"] == "scan" and e["urls"] == [SPEC["download"]] and e["work_size"] == [100, 100]
    assert e["bytes"] == 5_700_000 and e["bytes_estimated"] and e["date"] == "1936" and e["credit"] == SPEC["credit"]


def test_finder_fields(tmp_path):
    placed(tmp_path, "sgi100k_228_x", GCPS)
    (entry,) = mlc.sheet_entries([SPEC], tmp_path)
    out = mlc.finder_fields(entry, mlc.SERIES, tmp_path)
    assert out["year"] == 1936 and out["scale_n"] == 100000 and out["sheet_key"] == "sgi100k:228"
    assert out["place"] == "Long-Xuyên"
    sheet = mlc.finder_fields(
        {"id": "L1", "series": "l7014", "title": "Cao Lãnh (6129 IV)", "kind": "geopdf"}, mlc.SERIES
    )
    assert (
        sheet["year"] is None
        and sheet["scale_n"] == 50000
        and sheet["place"] == "Cao Lãnh"
        and sheet["sheet_key"] == "L1"
    )
    hand = mlc.finder_fields(
        {"id": "h", "series": "scans", "title": "t", "kind": "scan", "sheet_key": "l509:NC 48-6"}, mlc.SERIES, tmp_path
    )
    assert hand["sheet_key"] == "l509:NC 48-6"


@pytest.mark.parametrize(
    ("text", "n"), [("1:100 000", 100000), ("1:250,000", 250000), ("1:100.000", 100000), ("", None)]
)
def test_scale_number(text, n):
    assert mlc.scale_number(text) == n


def test_an_estimated_size_neither_cuts_a_download_short_nor_raises_a_note(tmp_path, monkeypatch):
    seen = {}

    def fetch(url, dest, *, progress=None, cancelled=None, expected_bytes=None):
        seen["expected"] = expected_bytes
        dest.write_bytes(b"x" * 10)
        return "digest"

    monkeypatch.setattr(download_maps, "fetch_url", fetch)
    entry = {"id": "m", "urls": ["https://api.nakala.fr/x.jpg"], "bytes": 99, "bytes_estimated": True, "sha256": None}
    assert download_maps.fetch_any(entry, tmp_path / "work.jpg") == []
    assert seen["expected"] is None
