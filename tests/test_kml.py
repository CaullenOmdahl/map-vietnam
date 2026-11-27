"""scripts/kml.py: Google Earth Pro KML/KMZ export, import, safety guards, and geometry helpers."""

import io
import math
import xml.etree.ElementTree as ET
import zipfile

import kml
import pytest
from PIL import Image


def _png_bytes(color=(12, 34, 56), size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_hex_and_kml_color_round_trip():
    assert kml.hex_to_kml_color("#ea580c", "ff") == "ff0c58ea"
    assert kml.kml_color_to_hex("ff0c58ea") == "#ea580c"
    assert kml.kml_color_to_hex("not-hex") is None


def test_geodesic_circle_is_closed_and_matches_radius():
    ring = kml.geodesic_circle(105.63, 10.46, 1000.0, steps=36)
    assert len(ring) == 37
    assert ring[0] == ring[-1]
    # North point (step 0) should be ~1000 m north (~0.009 degrees latitude).
    dlat = ring[0][1] - 10.46
    assert math.isclose(dlat * 111_195, 1000.0, rel_tol=0.02)


def test_latlonbox_to_corners_handles_rotation():
    # Unrotated box: NW, NE, SE, SW
    nw, ne, se, sw = kml.latlonbox_to_corners(11.0, 10.0, 106.0, 105.0, rotation=0.0)
    assert nw == pytest.approx((105.0, 11.0))
    assert ne == pytest.approx((106.0, 11.0))
    assert se == pytest.approx((106.0, 10.0))
    assert sw == pytest.approx((105.0, 10.0))

    # 90-degree CCW rotation swaps half-extents around centre (105.5, 10.5)
    nw_r, _, se_r, _ = kml.latlonbox_to_corners(11.0, 10.0, 106.0, 105.0, rotation=90.0)
    assert nw_r == pytest.approx((105.0, 10.0))
    assert se_r == pytest.approx((106.0, 11.0))


def test_export_kml_includes_folders_styles_rings_time_and_extended_data():
    case = {
        "name": "REFNO 1718",
        "summary": "Cobra loss",
        "notes": [{"body": "Witness interview 1971"}],
        "sites": [
            {"name": "Ridge", "lon": 106.71, "lat": 16.62, "uncertainty_m": 250, "rationale": "SAR bearing"},
        ],
        "items": [
            {
                "title": "Loss of Hummel",
                "kind": "aircraft_crash",
                "source": "vhpa",
                "source_record_id": "123",
                "date_start": "1971-03-06",
                "date_end": "1971-03-06",
                "lon": 106.72,
                "lat": 16.63,
                "uncertainty_m": 500,
                "original_coord": "XD834392",
                "original_datum": "indian1960",
                "description": "AH-1G",
                "url": "https://example.com/123",
                "case_note": "primary ref",
            }
        ],
        "shapes": [
            {
                "name": "Ingress leg",
                "kind": "line",
                "geometry": {"type": "LineString", "coordinates": [[106.7, 16.6], [106.72, 16.63]]},
                "description": "Last radar track",
                "folder": "Flight Path",
                "color": "#ff00aa",
                "date_start": "1971-03-06",
                "date_end": "1971-03-07",
            }
        ],
    }
    text = kml.export_kml(case, hazard="Test warning")
    root = ET.fromstring(text)
    assert root.tag == f"{{{kml.KML_NS}}}kml"
    folders = [el.find(f"{{{kml.KML_NS}}}name").text for el in root.findall(f".//{{{kml.KML_NS}}}Folder")]
    assert folders == ["Candidate Sites", "Pinned Evidence", "Paths & Areas", "Uncertainty Radii"]
    assert "<TimeStamp><when>1971-03-06</when></TimeStamp>" in text
    assert "<TimeSpan><begin>1971-03-06</begin><end>1971-03-07</end></TimeSpan>" in text
    assert "SITE: Ridge" in text
    assert "Witness interview 1971" in text
    assert "https://example.com/123" in text
    assert "eeaa00ff" in text  # #ff00aa converted to KML aabbggrr (alpha ee)
    assert "map_vietnam_type" in text


def test_export_kmz_round_trips_sites_shapes_and_overlays(tmp_path):
    png = _png_bytes()
    case = {
        "name": "RoundTrip",
        "summary": "Case summary",
        "notes": [],
        "sites": [{"name": "LZ Alpha", "lon": 105.63, "lat": 10.46, "uncertainty_m": 150, "rationale": "Clearing"}],
        "items": [
            {
                "title": "Pinned strike",
                "kind": "air_strike",
                "source": "thor",
                "source_record_id": "9",
                "date_start": "1968",
                "date_end": "1968",
                "lon": 105.64,
                "lat": 10.47,
                "uncertainty_m": 80,
                "original_coord": "WS683427",
                "original_datum": "indian1960",
                "description": "",
                "url": "",
            }
        ],
        "shapes": [
            {
                "name": "Search box",
                "kind": "polygon",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[105.6, 10.4], [105.7, 10.4], [105.7, 10.5], [105.6, 10.5], [105.6, 10.4]]],
                },
                "description": "Grid sector",
                "folder": "SAR",
                "color": "#16a34a",
                "date_start": "1968-05",
                "date_end": "1968-05",
            }
        ],
    }
    overlays = [
        {
            "name": "Recon frame 42",
            "href": "files/recon.png",
            "image_bytes": png,
            "corners_lonlat": [(105.6, 10.5), (105.7, 10.5), (105.7, 10.4), (105.6, 10.4)],
            "bounds": [105.6, 10.4, 105.7, 10.5],
        }
    ]
    kmz_bytes = kml.export_kmz(case, hazard="UXO warning", overlays=overlays)
    kmz_path = tmp_path / "case.kmz"
    kmz_path.write_bytes(kmz_bytes)

    parsed = kml.parse_kml_or_kmz(kmz_path, "case.kmz")
    # Uncertainty ring polygons are tagged map_vietnam_type=uncertainty_ring and skipped on re-import.
    assert [s["name"] for s in parsed["sites"]] == ["LZ Alpha", "Pinned strike"]
    assert parsed["sites"][0]["uncertainty_m"] == 150.0
    assert parsed["sites"][1]["uncertainty_m"] == 80.0
    assert len(parsed["shapes"]) == 1
    assert parsed["shapes"][0]["name"] == "Search box"
    assert parsed["shapes"][0]["color"] == "#16a34a"
    assert len(parsed["overlays"]) == 1
    assert parsed["overlays"][0]["name"] == "Recon frame 42"
    assert parsed["overlays"][0]["image_bytes"] == png
    assert parsed["overlays"][0]["corners_lonlat"][0] == pytest.approx((105.6, 10.5))


def test_parse_kml_handles_gx_track_multigeometry_stylemap_and_warnings(tmp_path):
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <kml xmlns="http://www.opengis.net/kml/2.2" xmlns:gx="http://www.google.com/kml/ext/2.2">
      <Document>
        <name>GE Pro Export</name>
        <description><![CDATA[<p>Notes line 1<br/>line 2</p>]]></description>
        <Style id="flightNormal"><LineStyle><color>ff0000ff</color><width>3</width></LineStyle></Style>
        <StyleMap id="flightMap"><Pair><key>normal</key><styleUrl>#flightNormal</styleUrl></Pair></StyleMap>
        <NetworkLink><name>Remote</name><Link><href>https://example.com/live.kml</href></Link></NetworkLink>
        <GroundOverlay><name>Remote image</name><Icon><href>https://example.com/img.png</href></Icon>
          <LatLonBox><north>11</north><south>10</south><east>106</east><west>105</west></LatLonBox>
        </GroundOverlay>
        <Folder>
          <name>Sortie</name>
          <Placemark>
            <name>Flight track</name>
            <styleUrl>#flightMap</styleUrl>
            <TimeStamp><when>1969-03-10</when></TimeStamp>
            <gx:Track>
              <gx:coord>105.5 10.4 300</gx:coord>
              <gx:coord>105.6 10.5 250</gx:coord>
            </gx:Track>
          </Placemark>
          <Placemark>
            <name>Combined</name>
            <MultiGeometry>
              <Point><coordinates>105.63,10.46,0</coordinates></Point>
              <Polygon><outerBoundaryIs><LinearRing><coordinates>105.6,10.4 105.7,10.4 105.7,10.5 105.6,10.4</coordinates></LinearRing></outerBoundaryIs></Polygon>
            </MultiGeometry>
          </Placemark>
        </Folder>
      </Document>
    </kml>"""
    p = tmp_path / "ge.kml"
    p.write_text(xml, encoding="utf-8")
    out = kml.parse_kml_or_kmz(p, "ge.kml")
    assert out["name"] == "GE Pro Export"
    assert out["description"] == "Notes line 1\nline 2"
    assert len(out["sites"]) == 1 and out["sites"][0]["folder"] == "Sortie"
    assert len(out["shapes"]) == 2
    assert out["shapes"][0]["kind"] == "line"
    assert out["shapes"][0]["color"] == "#ff0000"
    assert out["shapes"][0]["date_start"] == "1969-03-10"
    assert out["shapes"][1]["kind"] == "polygon"
    assert len(out["warnings"]) == 2


def test_parse_kmz_rejects_traversal_and_invalid_inputs(tmp_path):
    empty = tmp_path / "empty.kml"
    empty.write_bytes(b"")
    with pytest.raises(kml.KmlError, match="empty"):
        kml.parse_kml_or_kmz(empty, "empty.kml")

    bad_xml = tmp_path / "bad.kml"
    bad_xml.write_text("<kml><unclosed>", encoding="utf-8")
    with pytest.raises(kml.KmlError, match="not valid KML"):
        kml.parse_kml_or_kmz(bad_xml, "bad.kml")

    traversal_kmz = tmp_path / "trav.kmz"
    with zipfile.ZipFile(traversal_kmz, "w") as zf:
        zf.writestr(
            "doc.kml",
            """<kml xmlns="http://www.opengis.net/kml/2.2"><Document><GroundOverlay>
            <name>Escape</name><Icon><href>../secret.png</href></Icon>
            <LatLonBox><north>11</north><south>10</south><east>106</east><west>105</west></LatLonBox>
            </GroundOverlay></Document></kml>""",
        )
        zf.writestr("secret.png", _png_bytes())
    with pytest.raises(kml.KmlError, match="not found inside KMZ"):
        kml.parse_kml_or_kmz(traversal_kmz, "trav.kmz")
