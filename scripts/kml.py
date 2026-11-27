"""Google Earth Pro (.kml and .kmz) export and import for Map Vietnam cases.

Pure Python standard library (xml.etree.ElementTree + zipfile + math). Nothing here opens the
network: <NetworkLink> and remote http(s):// GroundOverlay icons are skipped with plain-English
warnings rather than fetched.

Export:
  export_kml(case, *, hazard, overlays=()) -> str
    KML 2.2 with:
      * structured <Folder>s (Candidate Sites, Pinned Evidence, Paths & Areas, Uncertainty Radii,
        Map Overlays);
      * distinct <Style> icons/colours per evidence kind and custom hex colours for shapes;
      * geodesic <Polygon> circles for every point's uncertainty radius (toggleable in Google Earth);
      * <TimeStamp> / <TimeSpan> so Google Earth Pro's historical time slider works;
      * <ExtendedData> carrying original_coord, original_datum, source, uncertainty_m, and url.
  export_kmz(case, *, hazard, overlays=()) -> bytes
    ZIP archive containing doc.kml and any local overlay image files referenced by <GroundOverlay>.

Import:
  parse_kml_or_kmz(path, filename="") -> dict
    Reads a .kml file or .kmz archive with strict size, zip-bomb and path-traversal guards.
    Returns:
      {
        "name": str,
        "description": str,
        "sites": [{"name", "lon", "lat", "uncertainty_m", "rationale", "folder", "date_start", "date_end"}],
        "shapes": [{"name", "kind", "geometry", "description", "folder", "color", "date_start", "date_end"}],
        "overlays": [{"name", "filename", "image_bytes", "corners_lonlat", "year", "description"}],
        "warnings": [str],
      }
    corners_lonlat is [NW, NE, SE, SW] as (lon, lat) pairs ready for custom_maps.ingest_placed_overlay().
"""

import io
import math
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from html import escape, unescape
from pathlib import Path

KML_NS = "http://www.opengis.net/kml/2.2"
GX_NS = "http://www.google.com/kml/ext/2.2"

MAX_KML_BYTES = 50 << 20
MAX_KMZ_BYTES = 200 << 20
MAX_KMZ_UNCOMPRESSED_BYTES = 500 << 20
MAX_KMZ_ENTRIES = 500
MAX_FEATURES = 5000
MAX_OVERLAYS = 20
DEFAULT_SITE_UNCERTAINTY_M = 100.0
EARTH_RADIUS_M = 6_371_008.8

# Matches web/app/app.js KINDS colours so pins look familiar in Google Earth Pro.
KIND_HEX = {
    "site": "#c0267a",
    "air_strike": "#ea580c",
    "loss_incident": "#dc2626",
    "field_burial": "#a21caf",
    "aircraft_crash": "#0891b2",
    "ground_combat": "#ca8a04",
    "military_site": "#2563eb",
    "placename": "#57534e",
    "other": "#64748b",
}
DEFAULT_SHAPE_HEX = "#0284c7"

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
KML_COLOR_RE = re.compile(r"^[0-9a-fA-F]{8}$")
ISO_DATE_RE = re.compile(r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)")
WITHIN_M_RE = re.compile(r"\(within\s+(\d+(?:\.\d+)?)\s*m\)", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
BR_TAG_RE = re.compile(r"<\s*(?:br|/?p|/?div|/?li|/?tr)\b[^>]*>", re.IGNORECASE)


class KmlError(ValueError):
    """A KML/KMZ file that cannot be imported; str() is plain English for the user."""


# ---- colour & geometry helpers -------------------------------------------------------------------
def hex_to_kml_color(hex_rgb, alpha="ff"):
    """'#rrggbb' + two-hex-digit alpha -> KML's 'aabbggrr'."""
    h = (hex_rgb or DEFAULT_SHAPE_HEX).lstrip("#")
    if len(h) != 6 or not all(c in "0123456789abcdefABCDEF" for c in h):
        h = DEFAULT_SHAPE_HEX.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"{alpha}{b}{g}{r}".lower()


def kml_color_to_hex(kml_color):
    """KML 'aabbggrr' -> '#rrggbb', or None if not a valid 8-hex KML colour."""
    raw = (kml_color or "").strip()
    if not KML_COLOR_RE.match(raw):
        return None
    b, g, r = raw[2:4], raw[4:6], raw[6:8]
    return f"#{r}{g}{b}".lower()


def geodesic_circle(lon, lat, radius_m, steps=48):
    """Closed WGS84 ring [(lon, lat), ...] of a circle of `radius_m` metres around (lon, lat)."""
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    ang = max(1.0, float(radius_m)) / EARTH_RADIUS_M
    sin_lat1, cos_lat1 = math.sin(lat1), math.cos(lat1)
    sin_ang, cos_ang = math.sin(ang), math.cos(ang)
    ring = []
    for i in range(steps + 1):
        bearing = 2.0 * math.pi * (i % steps) / steps
        lat2 = math.asin(max(-1.0, min(1.0, sin_lat1 * cos_ang + cos_lat1 * sin_ang * math.cos(bearing))))
        lon2 = lon1 + math.atan2(
            math.sin(bearing) * sin_ang * cos_lat1,
            cos_ang - sin_lat1 * math.sin(lat2),
        )
        ring.append((round(math.degrees(lon2), 6), round(math.degrees(lat2), 6)))
    return ring


def latlonbox_to_corners(north, south, east, west, rotation=0.0):
    """KML <LatLonBox> (rotation in CCW degrees about box centre) -> [NW, NE, SE, SW] (lon, lat)."""
    cx = (west + east) / 2.0
    cy = (south + north) / 2.0
    dx = (east - west) / 2.0
    dy = (north - south) / 2.0
    if not (dx > 0 and dy > 0):
        raise KmlError("A GroundOverlay has an empty LatLonBox.")
    # KML spec: positive <rotation> rotates the overlay counter-clockwise about its centre.
    rad = math.radians(rotation)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    local = [(-dx, dy), (dx, dy), (dx, -dy), (-dx, -dy)]  # NW, NE, SE, SW
    return [(cx + x * cos_r - y * sin_r, cy + x * sin_r + y * cos_r) for x, y in local]


def _tag(el):
    """Local XML element name without namespace."""
    t = el.tag
    return t.split("}", 1)[1] if "}" in t else t


def _child(el, name):
    for c in el:
        if _tag(c) == name:
            return c
    return None


def _children(el, name):
    return [c for c in el if _tag(c) == name]


def _text(el, name, default=""):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else default


def _clean_html(raw):
    """Strip KML HTML balloon markup into readable plain text."""
    if not raw:
        return ""
    s = BR_TAG_RE.sub("\n", raw)
    s = HTML_TAG_RE.sub("", s)
    s = unescape(s)
    lines = [" ".join(line.split()) for line in s.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _parse_iso_date(raw):
    if not raw:
        return None
    m = ISO_DATE_RE.search(str(raw).strip())
    return m.group(1) if m else None


def _parse_time_primitive(el):
    """Return (date_start, date_end) from a KML <TimeStamp> or <TimeSpan> child."""
    ts = _child(el, "TimeStamp")
    if ts is not None:
        d = _parse_iso_date(_text(ts, "when"))
        return d, d
    span = _child(el, "TimeSpan")
    if span is not None:
        a = _parse_iso_date(_text(span, "begin"))
        b = _parse_iso_date(_text(span, "end"))
        return a or b, b or a
    return None, None


def _parse_extended_data(el):
    """Extract key -> value string pairs from <ExtendedData> (<Data> and <SimpleData>)."""
    out = {}
    ed = _child(el, "ExtendedData")
    if ed is None:
        return out
    for node in ed.iter():
        t = _tag(node)
        if t == "Data":
            k = node.attrib.get("name", "").strip()
            if k:
                out[k] = _text(node, "value")
        elif t == "SimpleData":
            k = node.attrib.get("name", "").strip()
            if k:
                out[k] = (node.text or "").strip()
    return out


def _parse_coords(text):
    """Parse KML coordinate tuples ('lon,lat[,alt] ...') -> list of [lon, lat]."""
    pts = []
    for token in (text or "").replace("\t", " ").replace("\r", " ").replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            pts.append([lon, lat])
    return pts


def _parse_gx_track(el):
    """Parse <gx:Track> (<gx:coord>lon lat [alt]</gx:coord>) -> list of [lon, lat]."""
    pts = []
    for c in el:
        if _tag(c) != "coord" or not c.text:
            continue
        parts = c.text.strip().split()
        if len(parts) < 2:
            continue
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        if math.isfinite(lon) and math.isfinite(lat) and -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            pts.append([lon, lat])
    return pts


def _extract_style_color(style_el):
    """Pick the most informative #rrggbb colour from a KML <Style> element."""
    for sub in ("LineStyle", "PolyStyle", "IconStyle"):
        s = _child(style_el, sub)
        if s is not None:
            hx = kml_color_to_hex(_text(s, "color"))
            if hx:
                return hx
    return None


def _collect_styles(root):
    """Resolve KML <Style> and <StyleMap> IDs -> '#rrggbb' colour."""
    styles = {}
    maps = {}
    for el in root.iter():
        t = _tag(el)
        sid = el.attrib.get("id", "").strip()
        if t == "Style" and sid:
            hx = _extract_style_color(el)
            if hx:
                styles[sid] = hx
        elif t == "StyleMap" and sid:
            for pair in _children(el, "Pair"):
                if _text(pair, "key") == "normal":
                    ref = _text(pair, "styleUrl").lstrip("#")
                    if ref:
                        maps[sid] = ref
                    else:
                        inline = _child(pair, "Style")
                        if inline is not None:
                            hx = _extract_style_color(inline)
                            if hx:
                                styles[sid] = hx
    for sid, ref in maps.items():
        if ref in styles and sid not in styles:
            styles[sid] = styles[ref]
    return styles


# ---- export --------------------------------------------------------------------------------------
def _coords_str(points):
    return " ".join(f"{lon:.6f},{lat:.6f},0" for lon, lat in points)


def _time_xml(date_start, date_end=None):
    if not date_start:
        return ""
    if date_end and date_end != date_start:
        return f"<TimeSpan><begin>{escape(date_start)}</begin><end>{escape(date_end)}</end></TimeSpan>"
    return f"<TimeStamp><when>{escape(date_start)}</when></TimeStamp>"


def _extended_xml(fields):
    parts = [
        f'<Data name="{escape(str(k))}"><value>{escape(str(v))}</value></Data>'
        for k, v in fields.items()
        if v is not None and v != ""
    ]
    return f"<ExtendedData>{''.join(parts)}</ExtendedData>" if parts else ""


def _styles_xml():
    out = []
    for key, hx in KIND_HEX.items():
        col = hex_to_kml_color(hx, "ff")
        scale = "1.1" if key == "site" else "0.95"
        out.append(
            f'<Style id="mv-{key}"><IconStyle><color>{col}</color><scale>{scale}</scale>'
            f"<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>"
            f"</IconStyle><LabelStyle><scale>0.85</scale></LabelStyle></Style>"
        )
    out.append(
        f'<Style id="mv-ring-site"><LineStyle><color>{hex_to_kml_color("#c0267a", "cc")}</color><width>1.8</width></LineStyle>'
        f"<PolyStyle><color>{hex_to_kml_color('#c0267a', '22')}</color></PolyStyle></Style>"
    )
    out.append(
        f'<Style id="mv-ring-ev"><LineStyle><color>{hex_to_kml_color("#ea580c", "bb")}</color><width>1.5</width></LineStyle>'
        f"<PolyStyle><color>{hex_to_kml_color('#ea580c', '1c')}</color></PolyStyle></Style>"
    )
    out.append(
        f'<Style id="mv-shape-line"><LineStyle><color>{hex_to_kml_color(DEFAULT_SHAPE_HEX, "ee")}</color><width>3</width></LineStyle></Style>'
    )
    out.append(
        f'<Style id="mv-shape-poly"><LineStyle><color>{hex_to_kml_color(DEFAULT_SHAPE_HEX, "ee")}</color><width>2</width></LineStyle>'
        f"<PolyStyle><color>{hex_to_kml_color(DEFAULT_SHAPE_HEX, '33')}</color></PolyStyle></Style>"
    )
    return "".join(out)


def _shape_style_xml(kind, color):
    if not color or not HEX_COLOR_RE.match(color):
        return f"<styleUrl>#mv-shape-{'poly' if kind == 'polygon' else 'line'}</styleUrl>"
    line = f"<LineStyle><color>{hex_to_kml_color(color, 'ee')}</color><width>{'2' if kind == 'polygon' else '3'}</width></LineStyle>"
    poly = f"<PolyStyle><color>{hex_to_kml_color(color, '33')}</color></PolyStyle>" if kind == "polygon" else ""
    return f"<Style>{line}{poly}</Style>"


def _shape_geometry_xml(geom):
    gtype = geom.get("type")
    coords = geom.get("coordinates") or []
    if gtype == "LineString" and len(coords) >= 2:
        return f"<LineString><tessellate>1</tessellate><coordinates>{_coords_str(coords)}</coordinates></LineString>"
    if gtype == "Polygon" and coords and len(coords[0]) >= 3:
        outer = coords[0]
        if outer[0] != outer[-1]:
            outer = [*outer, outer[0]]
        inner_xml = ""
        for hole in coords[1:]:
            if len(hole) >= 3:
                h = hole if hole[0] == hole[-1] else [*hole, hole[0]]
                inner_xml += f"<innerBoundaryIs><LinearRing><coordinates>{_coords_str(h)}</coordinates></LinearRing></innerBoundaryIs>"
        return (
            f"<Polygon><tessellate>1</tessellate>"
            f"<outerBoundaryIs><LinearRing><coordinates>{_coords_str(outer)}</coordinates></LinearRing></outerBoundaryIs>"
            f"{inner_xml}</Polygon>"
        )
    return ""


def _overlay_xml(ov):
    """Build one <GroundOverlay> element for a KMZ overlay dict."""
    href = escape(ov["href"])
    name = escape(ov["name"])
    color = "ccffffff"
    if ov.get("corners_lonlat"):
        nw, ne, se, sw = ov["corners_lonlat"]
        # KML <gx:LatLonQuad> lists corners counter-clockwise starting at lower-left: SW, SE, NE, NW.
        quad = f"{sw[0]:.6f},{sw[1]:.6f} {se[0]:.6f},{se[1]:.6f} {ne[0]:.6f},{ne[1]:.6f} {nw[0]:.6f},{nw[1]:.6f}"
        pos = f"<gx:LatLonQuad><coordinates>{quad}</coordinates></gx:LatLonQuad>"
    else:
        w, s, e, n = ov["bounds"]
        pos = f"<LatLonBox><north>{n:.6f}</north><south>{s:.6f}</south><east>{e:.6f}</east><west>{w:.6f}</west></LatLonBox>"
    return (
        f"<GroundOverlay><name>{name}</name><color>{color}</color><Icon><href>{href}</href></Icon>{pos}</GroundOverlay>"
    )


def export_kml(case, *, hazard, overlays=()):
    """Return a Google Earth Pro compatible KML 2.2 string for `case`."""
    title = f"{case['name']} ({hazard})"
    notes = [n["body"] for n in case.get("notes") or []]
    summary_parts = [p for p in (case.get("summary") or "", "\n".join(notes)) if p]
    doc_desc = escape("\n\n".join(summary_parts))

    site_marks = []
    ring_marks = []
    for s in case.get("sites") or []:
        r = float(s["uncertainty_m"])
        desc = f"{s.get('rationale') or ''} (within {r:.0f} m)".strip()
        ext = _extended_xml(
            {
                "map_vietnam_type": "site",
                "uncertainty_m": f"{r:g}",
                "rationale": s.get("rationale") or "",
            }
        )
        site_marks.append(
            f"<Placemark><name>{escape('SITE: ' + s['name'])}</name>"
            f"<description>{escape(desc)}</description>"
            f"<styleUrl>#mv-site</styleUrl>{ext}"
            f"<Point><coordinates>{s['lon']},{s['lat']},0</coordinates></Point></Placemark>"
        )
        ring = geodesic_circle(s["lon"], s["lat"], r)
        ring_ext = _extended_xml({"map_vietnam_type": "uncertainty_ring", "uncertainty_m": f"{r:g}"})
        ring_marks.append(
            f"<Placemark><name>{escape(s['name'])} (±{r:.0f} m)</name>"
            f"<styleUrl>#mv-ring-site</styleUrl>{ring_ext}"
            f"<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>"
            f"<coordinates>{_coords_str(ring)}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
        )

    ev_marks = []
    for e in case.get("items") or []:
        r = float(e["uncertainty_m"])
        kind = e.get("kind") if e.get("kind") in KIND_HEX else "other"
        plain = (
            f"{e['kind'].replace('_', ' ').capitalize()}. "
            f"{e['source']} {e['source_record_id']}, {e.get('date_start') or 'undated'}, "
            f"original {e['original_coord']} ({e['original_datum']}). {e.get('description') or ''}"
        ).strip()
        if e.get("case_note"):
            plain += f" Case note: {e['case_note']}"
        if e.get("url"):
            plain += f" Source: {e['url']}"
        plain += f" (within {r:.0f} m)"
        ext = _extended_xml(
            {
                "map_vietnam_type": "evidence",
                "kind": e["kind"],
                "source": e["source"],
                "source_record_id": e["source_record_id"],
                "date_start": e.get("date_start") or "",
                "date_end": e.get("date_end") or "",
                "original_coord": e["original_coord"],
                "original_datum": e["original_datum"],
                "uncertainty_m": f"{r:g}",
                "url": e.get("url") or "",
            }
        )
        ev_marks.append(
            f"<Placemark><name>{escape(e['title'])}</name>"
            f"<description>{escape(plain)}</description>"
            f"<styleUrl>#mv-{kind}</styleUrl>{_time_xml(e.get('date_start'), e.get('date_end'))}{ext}"
            f"<Point><coordinates>{e['lon']},{e['lat']},0</coordinates></Point></Placemark>"
        )
        ring = geodesic_circle(e["lon"], e["lat"], r)
        ring_ext = _extended_xml({"map_vietnam_type": "uncertainty_ring", "uncertainty_m": f"{r:g}"})
        ring_marks.append(
            f"<Placemark><name>{escape(e['title'])} (±{r:.0f} m)</name>"
            f"<styleUrl>#mv-ring-ev</styleUrl>{ring_ext}"
            f"<Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing>"
            f"<coordinates>{_coords_str(ring)}</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"
        )

    shape_marks = []
    for sh in case.get("shapes") or []:
        geom = sh.get("geometry")
        if not isinstance(geom, dict):
            continue
        gxml = _shape_geometry_xml(geom)
        if not gxml:
            continue
        ext = _extended_xml(
            {
                "map_vietnam_type": "shape",
                "folder": sh.get("folder") or "",
                "color": sh.get("color") or "",
            }
        )
        shape_marks.append(
            f"<Placemark><name>{escape(sh['name'])}</name>"
            f"<description>{escape(sh.get('description') or '')}</description>"
            f"{_shape_style_xml(sh.get('kind'), sh.get('color'))}"
            f"{_time_xml(sh.get('date_start'), sh.get('date_end'))}{ext}{gxml}</Placemark>"
        )

    folders = []
    if site_marks:
        folders.append(f"<Folder><name>Candidate Sites</name>{''.join(site_marks)}</Folder>")
    if ev_marks:
        folders.append(f"<Folder><name>Pinned Evidence</name>{''.join(ev_marks)}</Folder>")
    if shape_marks:
        folders.append(f"<Folder><name>Paths &amp; Areas</name>{''.join(shape_marks)}</Folder>")
    if ring_marks:
        folders.append(f"<Folder><name>Uncertainty Radii</name>{''.join(ring_marks)}</Folder>")
    if overlays:
        folders.append(f"<Folder><name>Map Overlays</name>{''.join(_overlay_xml(o) for o in overlays)}</Folder>")

    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<kml xmlns="{KML_NS}" xmlns:gx="{GX_NS}">'
        f"<Document><name>{escape(title)}</name><description>{doc_desc}</description>"
        f"{_styles_xml()}{''.join(folders)}</Document></kml>"
    )


def export_kmz(case, *, hazard, overlays=()):
    """Return a Google Earth Pro .kmz (ZIP bytes) with doc.kml and any overlay images."""
    kml_text = export_kml(case, hazard=hazard, overlays=overlays)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("doc.kml", kml_text.encode("utf-8"))
        for ov in overlays:
            zf.writestr(ov["href"], ov["image_bytes"], compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


# ---- import --------------------------------------------------------------------------------------
def _safe_zip_member(base_dir, href):
    """Resolve an archive-relative href safely, refusing traversal or remote URLs."""
    raw = (href or "").strip().replace("\\", "/")
    if not raw:
        return None
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) or raw.startswith("//"):
        return None
    joined = posixpath.normpath(posixpath.join(base_dir, raw.lstrip("/")))
    if joined.startswith("..") or joined == ".":
        return None
    return joined


def _read_kmz(path):
    """Return (kml_bytes, kml_dir, images_map) from a .kmz archive with size/count limits."""
    try:
        zf = zipfile.ZipFile(path, "r")
    except zipfile.BadZipFile:
        raise KmlError("That KMZ file is damaged or not a valid ZIP archive.") from None
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_KMZ_ENTRIES:
            raise KmlError(f"That KMZ archive contains too many entries ({len(infos)}; limit {MAX_KMZ_ENTRIES}).")
        total = 0
        for info in infos:
            if info.file_size < 0:
                raise KmlError("That KMZ archive has an invalid entry size.")
            total += info.file_size
            if total > MAX_KMZ_UNCOMPRESSED_BYTES:
                raise KmlError("That KMZ archive unpacks to more than 500 MB, which is larger than Map Vietnam allows.")
        kml_entries = [i for i in infos if not i.is_dir() and i.filename.lower().endswith(".kml")]
        if not kml_entries:
            raise KmlError("That KMZ archive does not contain a .kml file.")
        # Google Earth Pro convention: doc.kml at root, else first top-level .kml, else first .kml.
        kml_entries.sort(key=lambda i: (i.filename.lower() != "doc.kml", "/" in i.filename, i.filename.lower()))
        main = kml_entries[0]
        if main.file_size > MAX_KML_BYTES:
            raise KmlError("The KML inside that KMZ is larger than 50 MB.")
        kml_bytes = zf.read(main)
        kml_dir = posixpath.dirname(main.filename)
        loaded = {}
        for info in infos:
            if info.is_dir():
                continue
            norm = posixpath.normpath(info.filename.replace("\\", "/"))
            if norm.startswith("..") or norm.startswith("/"):
                continue
            ext = Path(norm).suffix.lower()
            if ext in (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".gif"):
                loaded[norm] = zf.read(info)
        return kml_bytes, kml_dir, loaded


def _extract_geometries(el):
    """Yield ('point', [lon, lat]), ('line', [[lon, lat], ...]), or ('polygon', [[[lon, lat], ...], ...])."""
    t = _tag(el)
    if t == "Point":
        pts = _parse_coords(_text(el, "coordinates"))
        if pts:
            yield "point", pts[0]
    elif t == "LineString":
        pts = _parse_coords(_text(el, "coordinates"))
        if len(pts) >= 2:
            yield "line", pts
    elif t == "Track":
        pts = _parse_gx_track(el)
        if len(pts) >= 2:
            yield "line", pts
    elif t == "Polygon":
        outer_el = _child(el, "outerBoundaryIs")
        ring_el = _child(outer_el, "LinearRing") if outer_el is not None else None
        outer = _parse_coords(_text(ring_el, "coordinates")) if ring_el is not None else []
        if len(outer) >= 3:
            if outer[0] != outer[-1]:
                outer.append(outer[0])
            rings = [outer]
            for inner_el in _children(el, "innerBoundaryIs"):
                iring_el = _child(inner_el, "LinearRing")
                hole = _parse_coords(_text(iring_el, "coordinates")) if iring_el is not None else []
                if len(hole) >= 3:
                    if hole[0] != hole[-1]:
                        hole.append(hole[0])
                    rings.append(hole)
            yield "polygon", rings
    elif t in ("MultiGeometry", "MultiTrack"):
        for child in el:
            yield from _extract_geometries(child)


def _uncertainty_from(ext, desc):
    raw = ext.get("uncertainty_m")
    if raw:
        try:
            v = float(raw)
            if math.isfinite(v) and 1.0 <= v <= 500_000.0:
                return v
        except ValueError:
            pass
    m = WITHIN_M_RE.search(desc or "")
    if m:
        try:
            v = float(m.group(1))
            if math.isfinite(v) and 1.0 <= v <= 500_000.0:
                return v
        except ValueError:
            pass
    return DEFAULT_SITE_UNCERTAINTY_M


def parse_kml_or_kmz(path, filename=""):
    """Parse a Google Earth Pro .kml or .kmz file at `path` into sites, shapes and overlays."""
    path = Path(path)
    size = path.stat().st_size
    if size <= 0:
        raise KmlError("That file is empty.")
    with open(path, "rb") as f:
        head = f.read(8)
    is_kmz = head.startswith(b"PK\x03\x04") or Path(filename).suffix.lower() == ".kmz"
    if is_kmz:
        if size > MAX_KMZ_BYTES:
            raise KmlError("That KMZ file is larger than 200 MB.")
        kml_bytes, kml_dir, kmz_images = _read_kmz(path)
    else:
        if size > MAX_KML_BYTES:
            raise KmlError("That KML file is larger than 50 MB.")
        kml_bytes, kml_dir, kmz_images = path.read_bytes(), "", {}

    try:
        root = ET.fromstring(kml_bytes)
    except ET.ParseError as e:
        raise KmlError(f"That file is not valid KML XML ({e}).") from None

    if _tag(root) != "kml" and _child(root, "Document") is None and _tag(root) != "Document":
        raise KmlError("That XML file is not a Google Earth KML document.")

    styles = _collect_styles(root)
    doc = _child(root, "Document")
    if doc is None:
        doc = root
    doc_name = _text(doc, "name") or Path(filename or "Imported KML").stem
    doc_desc = _clean_html(_text(doc, "description"))

    sites = []
    shapes = []
    overlays = []
    warnings = []
    network_links = 0
    remote_overlays = 0
    truncated = False

    def walk(container, folder_stack):
        nonlocal network_links, remote_overlays, truncated
        for el in container:
            t = _tag(el)
            if t == "Document":
                walk(el, folder_stack)
            elif t == "Folder":
                fname = _text(el, "name")
                walk(el, [*folder_stack, fname] if fname else folder_stack)
            elif t == "NetworkLink":
                network_links += 1
            elif t == "Placemark":
                if len(sites) + len(shapes) >= MAX_FEATURES:
                    truncated = True
                    continue
                ext = _parse_extended_data(el)
                if ext.get("map_vietnam_type") == "uncertainty_ring":
                    continue
                name = _text(el, "name") or "Unnamed placemark"
                if name.startswith("SITE: "):
                    name = name[6:].strip() or name
                desc = _clean_html(_text(el, "description"))
                date_start, date_end = _parse_time_primitive(el)
                sref = _text(el, "styleUrl").lstrip("#")
                inline_style = _child(el, "Style")
                color = (
                    (ext.get("color") if HEX_COLOR_RE.match(ext.get("color") or "") else None)
                    or (_extract_style_color(inline_style) if inline_style is not None else None)
                    or styles.get(sref)
                )
                folder_label = " / ".join(folder_stack)
                for child in el:
                    for gkind, data in _extract_geometries(child):
                        if len(sites) + len(shapes) >= MAX_FEATURES:
                            truncated = True
                            break
                        if gkind == "point":
                            lon, lat = data
                            rationale = ext.get("rationale") or desc
                            if folder_label and not rationale.startswith(f"[{folder_label}]"):
                                rationale = f"[{folder_label}] {rationale}".strip() if rationale else folder_label
                            sites.append(
                                {
                                    "name": name[:200],
                                    "lon": lon,
                                    "lat": lat,
                                    "uncertainty_m": _uncertainty_from(ext, desc),
                                    "rationale": rationale[:2000],
                                    "folder": folder_label,
                                    "date_start": date_start,
                                    "date_end": date_end,
                                }
                            )
                        elif gkind == "line":
                            shapes.append(
                                {
                                    "name": name[:200],
                                    "kind": "line",
                                    "geometry": {"type": "LineString", "coordinates": data},
                                    "description": desc[:2000],
                                    "folder": folder_label,
                                    "color": color,
                                    "date_start": date_start,
                                    "date_end": date_end,
                                }
                            )
                        elif gkind == "polygon":
                            shapes.append(
                                {
                                    "name": name[:200],
                                    "kind": "polygon",
                                    "geometry": {"type": "Polygon", "coordinates": data},
                                    "description": desc[:2000],
                                    "folder": folder_label,
                                    "color": color,
                                    "date_start": date_start,
                                    "date_end": date_end,
                                }
                            )
            elif t == "GroundOverlay":
                if len(overlays) >= MAX_OVERLAYS:
                    truncated = True
                    continue
                icon_el = _child(el, "Icon")
                href = _text(icon_el, "href") if icon_el is not None else ""
                if not href:
                    continue
                if re.match(r"^https?://", href, re.IGNORECASE):
                    remote_overlays += 1
                    continue
                member = _safe_zip_member(kml_dir, href)
                if not member or member not in kmz_images:
                    warnings.append(f"Overlay '{_text(el, 'name') or href}' skipped (image not found inside KMZ).")
                    continue
                corners = None
                quad_el = _child(el, "LatLonQuad")
                box_el = _child(el, "LatLonBox")
                if quad_el is not None:
                    qpts = _parse_coords(_text(quad_el, "coordinates"))
                    if len(qpts) == 4:
                        # KML gx:LatLonQuad order is SW, SE, NE, NW -> convert to NW, NE, SE, SW.
                        sw, se, ne, nw = qpts
                        corners = [(nw[0], nw[1]), (ne[0], ne[1]), (se[0], se[1]), (sw[0], sw[1])]
                elif box_el is not None:
                    try:
                        n = float(_text(box_el, "north"))
                        s = float(_text(box_el, "south"))
                        e = float(_text(box_el, "east"))
                        w = float(_text(box_el, "west"))
                        rot = float(_text(box_el, "rotation") or "0")
                        if all(math.isfinite(v) for v in (n, s, e, w, rot)) and n > s and e > w:
                            corners = latlonbox_to_corners(n, s, e, w, rot)
                    except (ValueError, KmlError):
                        corners = None
                if not corners:
                    warnings.append(
                        f"Overlay '{_text(el, 'name') or href}' skipped (missing valid LatLonBox/LatLonQuad)."
                    )
                    continue
                ov_name = _text(el, "name") or Path(member).stem
                ds, _ = _parse_time_primitive(el)
                year = ds[:4] if ds and ds[:4].isdigit() else ""
                overlays.append(
                    {
                        "name": ov_name[:200],
                        "filename": Path(member).name,
                        "image_bytes": kmz_images[member],
                        "corners_lonlat": corners,
                        "year": year,
                        "description": _clean_html(_text(el, "description"))[:2000],
                    }
                )

    walk(doc, [])

    if network_links:
        warnings.append(
            f"Skipped {network_links} external NetworkLink{'s' if network_links != 1 else ''} "
            "(Map Vietnam does not fetch remote KML links)."
        )
    if remote_overlays:
        warnings.append(
            f"Skipped {remote_overlays} remote image overlay{'s' if remote_overlays != 1 else ''} "
            "(only images bundled inside a KMZ are imported)."
        )
    if truncated:
        warnings.append(
            f"Import hit the safety limit ({MAX_FEATURES} features / {MAX_OVERLAYS} overlays); remaining items were skipped."
        )

    if not sites and not shapes and not overlays:
        extra = f" {' '.join(warnings)}" if warnings else ""
        raise KmlError(f"No points, paths, polygons or bundled overlays were found in that file.{extra}")

    return {
        "name": doc_name[:200],
        "description": doc_desc[:4000],
        "sites": sites,
        "shapes": shapes,
        "overlays": overlays,
        "warnings": warnings,
    }
