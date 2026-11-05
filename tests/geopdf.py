"""Builds small synthetic NGA-style GeoPDFs (LGIDict georeferencing, XMP metadata) for tests."""

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, StreamObject, TextStringObject

XMP = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:pdfx="http://ns.adobe.com/pdfx/1.3/">
<pdfx:title>{title}</pdfx:title><pdfx:sheet_id>{sheet_id}</pdfx:sheet_id><pdfx:pri_date>{pri_date}</pdfx:pri_date>
<pdfx:edition>{edition}</pdfx:edition><pdfx:horizontal_datum>{datum}</pdfx:horizontal_datum>
</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>"""


def numbers(values):
    return ArrayObject(FloatObject(v) for v in values)


def write_geopdf(
    path,
    *,
    width_pt=288,
    height_pt=216,
    origin=(560_000, 1_150_000),
    m_per_pt=20.0,
    xmp=None,
    info=None,
    datum="ING-A",
    cm=105,
    projection="UT",
    georef="registration",
    neatline="inset",
):
    """A blank page whose LGIDict maps page point (x, y) to transverse Mercator grid metres on
    `datum` with central meridian `cm` (105 = UTM 48, 111 = UTM 49):
    easting = origin[0] + x * m_per_pt, northing = origin[1] + y * m_per_pt. The neatline is
    the page inset by 10 %. xmp: dict of fields for the XMP packet (see XMP), or None;
    info: extra PDF Info dictionary fields such as {"pri_date": "1986-03-07"}, or None.

    The forms seen on real sheets: georef "registration" (corner points), "ctm" (a version 2
    LGIDict with only the /CTM transform) or "both"; projection "UT" or "TC" with an explicit
    central meridian, or "UT-zone" naming only its UTM zone; neatline "inset", "page" (the
    whole page, as on some reissued sheets) or "tab" (the page less its lower right quarter)."""
    writer = PdfWriter()
    page = writer.add_blank_page(width=width_pt, height=height_pt)
    corners = [(0, 0), (width_pt, 0), (width_pt, height_pt), (0, height_pt)]
    reg = [numbers([x, y, origin[0] + x * m_per_pt, origin[1] + y * m_per_pt]) for x, y in corners]
    inset = [(0.1 * width_pt, 0.1 * height_pt), (0.9 * width_pt, 0.1 * height_pt)]
    inset += [(0.9 * width_pt, 0.9 * height_pt), (0.1 * width_pt, 0.9 * height_pt)]
    # "tab": the whole page less its lower right quarter, so only the left half reaches the bottom.
    tab = [(0, 0), (width_pt / 2, 0), (width_pt / 2, height_pt / 4), (width_pt, height_pt / 4)]
    tab += [(width_pt, height_pt), (0, height_pt)]
    frame = {"inset": inset, "page": corners, "tab": tab}[neatline]
    if projection == "UT-zone":
        proj = {"/ProjectionType": "UT", "/Zone": (cm + 183) // 6, "/Hemisphere": "N"}
    else:
        proj = {"/ProjectionType": projection, "/CentralMeridian": cm, "/ScaleFactor": 0.9996}
        proj.update({"/FalseEasting": 500_000, "/FalseNorthing": 0})
    proj["/Datum"] = datum
    projection = DictionaryObject({NameObject("/Type"): NameObject("/Projection")})
    for key, value in proj.items():
        projection[NameObject(key)] = TextStringObject(value) if isinstance(value, str) else FloatObject(value)
    lgi = {
        NameObject("/Type"): NameObject("/LGIDict"),
        NameObject("/Neatline"): numbers([v for p in frame for v in p]),
        NameObject("/Projection"): projection,
    }
    if georef in ("registration", "both"):
        lgi[NameObject("/Registration")] = ArrayObject(reg)
    if georef in ("ctm", "both"):
        lgi[NameObject("/Version")] = FloatObject(2)
        lgi[NameObject("/CTM")] = numbers([m_per_pt, 0, 0, m_per_pt, *origin])
    page[NameObject("/LGIDict")] = DictionaryObject(lgi)
    if xmp is not None:
        stream = StreamObject()
        stream.set_data(XMP.format(**xmp).encode())
        stream[NameObject("/Type")] = NameObject("/Metadata")
        stream[NameObject("/Subtype")] = NameObject("/XML")
        writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)
    if info:
        writer.add_metadata({f"/{k}": v for k, v in info.items()})
    with open(path, "wb") as f:
        writer.write(f)
    return path
