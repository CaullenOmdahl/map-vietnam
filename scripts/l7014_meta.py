"""Read the facts NGA records inside each GeoPDF it publishes: title, sheet, edition, date and datum.

Both GeoPDF series this project builds (AMS L7014 at 1:50,000 and JOG 1501 at 1:250,000) carry
them twice: in the PDF Info dictionary (pri_date, edition, horizontal_datum, ...) and in an XMP
packet with the same field names. Info is read first and XMP fills any gaps. build_tiles stores
the result in the map's MBTiles metadata, which is how the viewer knows the year of a sheet.

Dates matter more than the printed labels: a library's list may say 1972 where the PDF says it
is the 1986 edition, so the PDF's own pri_date (date of the edition) is what gets shown.

Usage: .venv/bin/python scripts/l7014_meta.py SHEET.pdf ...   (prints each sheet's facts)
"""

import argparse
import re
from pathlib import Path

from pypdf import PdfReader

# Datum codes as they appear in the LGIDict (what the georeferencing uses) and, shorter, in the
# Info/XMP "horizontal_datum" field, in plain words for the map's description.
DATUM_NAMES = {
    "ING-A": "Indian 1960 datum",
    "ING-B": "Indian 1960 datum (Con Son)",
    # Mislabels in the georeferencing of some Vietnam sheets, which are on Indian 1960: see datums.py.
    "IND-I": "Indian 1960 datum",
    "IND-B": "Indian 1960 datum",
    "INF-A": "Indian 1960 datum",
    "ING": "Indian 1960 datum",
    "IND": "Indian 1960 datum",
    "WGE": "WGS84",
    "WE": "WGS84",
}


def xmp(pdf):
    """The <pdfx:...> fields of the document's XMP packet, e.g. {"title": ..., "sheet_id": "61294"}."""
    meta = PdfReader(pdf).trailer["/Root"].get("/Metadata")
    if meta is None:
        return {}
    text = meta.get_object().get_data().decode("utf-8", errors="replace")
    return dict(re.findall(r"<pdfx:(\w+)>([^<]*)</pdfx:\1>", text))


def info(pdf):
    """The PDF Info dictionary as plain strings, keys without the leading slash."""
    return {k.lstrip("/"): str(v) for k, v in (PdfReader(pdf).metadata or {}).items()}


def year_of(text):
    """The year in a pri_date field: "1986-03-07" and "Jan  1 1967 12:00AM" both occur; "" if none."""
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", text)
    return m.group(1) if m else ""


def facts(pdf, datum_code=None):
    """{"title", "sheet_id", "date", "edition", "date_note"} for one GeoPDF; missing facts are "".

    date is the year of the edition (pri_date); datum_code, when given, is the LGIDict datum the
    sheet was georeferenced on, which is the one worth naming in the note.
    """
    fields = {**xmp(pdf), **{k: v for k, v in info(pdf).items() if v.strip()}}
    edition = fields.get("edition", "").lstrip("0")
    datum = datum_code or fields.get("horizontal_datum", "")
    note = ", ".join(part for part in (f"edition {edition}" if edition else "", DATUM_NAMES.get(datum, datum)) if part)
    return {
        "title": fields.get("title", "").strip(),
        "sheet_id": fields.get("sheet_id", ""),
        "date": year_of(fields.get("pri_date", "")),
        "edition": edition,
        "date_note": note,
    }


def main(argv=None):
    """Print the facts of each GeoPDF named on the command line."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdfs", nargs="+", type=Path, help="GeoPDF files")
    for pdf in ap.parse_args(argv).pdfs:
        f = facts(pdf)
        print(f"{pdf.name}: {f['date'] or 'undated'} | {f['title']} | {f['date_note']}")


if __name__ == "__main__":
    main()
