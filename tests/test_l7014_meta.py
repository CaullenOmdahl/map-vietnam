"""l7014_meta: a GeoPDF's own facts (edition, date, datum) from its Info dictionary, XMP filling gaps."""

import l7014_meta
from geopdf import write_geopdf

CAO_LANH = {
    "title": "Vietnam 1:50,000. Cao Lanh, Vietnam. [L7014 61294].",
    "sheet_id": "61294",
    "pri_date": "1965-01-01",
    "edition": "001",
    "datum": "IND",
}


def test_xmp_fields_give_the_facts(tmp_path):
    pdf = write_geopdf(tmp_path / "L701461294_geo.pdf", xmp=CAO_LANH)
    assert l7014_meta.facts(pdf) == {
        "title": "Vietnam 1:50,000. Cao Lanh, Vietnam. [L7014 61294].",
        "sheet_id": "61294",
        "date": "1965",
        "edition": "1",
        "date_note": "edition 1, Indian 1960 datum",
    }


def test_info_dictionary_wins_and_the_georeferencing_datum_is_named(tmp_path):
    # As on the JOG sheets: the Info dictionary carries the edition date; the LGIDict datum is
    # what the sheet is actually placed with, so the note names that one.
    pdf = write_geopdf(
        tmp_path / "x_geo.pdf", xmp=CAO_LANH, info={"pri_date": "1986-03-07", "edition": "005", "title": ""}
    )
    f = l7014_meta.facts(pdf, "WGE")
    assert f["date"] == "1986" and f["edition"] == "5" and f["date_note"] == "edition 5, WGS84"
    assert f["title"].startswith("Vietnam 1:50,000")  # an empty Info field does not hide the XMP one


def test_pdf_without_metadata_gives_empty_facts(tmp_path):
    pdf = write_geopdf(tmp_path / "x_geo.pdf")
    assert l7014_meta.xmp(pdf) == {}
    assert l7014_meta.facts(pdf) == {"title": "", "sheet_id": "", "date": "", "edition": "", "date_note": ""}


def test_command_line_prints_one_line_per_sheet(tmp_path, capsys):
    l7014_meta.main([str(write_geopdf(tmp_path / "L701461294_geo.pdf", xmp=CAO_LANH))])
    assert capsys.readouterr().out.startswith("L701461294_geo.pdf: 1965 | Vietnam 1:50,000. Cao Lanh")


def test_year_is_found_in_both_date_forms_nga_uses(tmp_path):
    # Older reissues write pri_date as "Jan  1 1967 12:00AM", not ISO.
    assert l7014_meta.year_of("Jan  1 1967 12:00AM") == "1967"
    assert l7014_meta.year_of("1986-03-07") == "1986" and l7014_meta.year_of("unknown") == ""
    pdf = write_geopdf(tmp_path / "x_geo.pdf", info={"pri_date": "Jan  1 1967 12:00AM"})
    assert l7014_meta.facts(pdf, "INF-A")["date"] == "1967"
    assert l7014_meta.facts(pdf, "INF-A")["date_note"] == "Indian 1960 datum"
