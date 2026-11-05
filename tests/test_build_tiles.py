"""build_tiles: GeoPDF pages rendered with pdfium, georeferenced from their LGIDict, tiled into MBTiles."""

import io
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import build_tiles
import numpy as np
import pytest
import tiling
from geopdf import write_geopdf
from PIL import Image
from pypdf import PdfWriter
from pyproj import Transformer

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_tiles.py"


def test_render_size_rounds_up_like_pdftoppm_without_float_noise(tmp_path):
    # 2085.12 pt at 250 dpi is exactly 7240 px; pdfium's single-precision page size gives 7240.0004.
    pdf = write_geopdf(tmp_path / "s_geo.pdf", width_pt=72, height_pt=2085.12)
    assert build_tiles.render_size(pdf) == (250, 7240)
    assert build_tiles.render_page(pdf).shape == (7240, 250, 3)
    assert build_tiles.render_size(write_geopdf(tmp_path / "t_geo.pdf", width_pt=100.1, height_pt=50)) == (348, 174)


def neatline_on_wgs84(pdf):
    """[w, s, e, n] in degrees of the synthetic sheet's neatline: the page inset by 10 %."""
    to_wgs84 = Transformer.from_crs(build_tiles.crs_for(build_tiles.lgi(pdf)), "EPSG:4326", always_xy=True)
    (w, e), (s, n) = to_wgs84.transform([560_000 + 576, 560_000 + 5184], [1_150_000 + 432, 1_150_000 + 3888])
    return [w, s, e, n]


def test_sheet_is_tiled_where_its_registration_points_say(tmp_path):
    pdf = write_geopdf(tmp_path / "L7014TEST_geo.pdf", info={"pri_date": "1969-05-01", "edition": "002"})
    out = tmp_path / "tiles" / "L7014TEST.mbtiles"
    stale = out.with_name(out.name + ".partial")
    stale.parent.mkdir()
    stale.write_bytes(b"left over from an interrupted build")

    report = build_tiles.build(pdf, out)

    assert report["fit_m"] == 0.0 and report["maxzoom"] == 15 and report["id"] == "L7014TEST"
    assert out.exists() and not stale.exists()
    meta = tiling.read_metadata(out)
    # The recorded extent is the neatline (the map frame), not the whole page with its collar.
    assert [float(v) for v in meta["bounds"].split(",")] == pytest.approx(neatline_on_wgs84(pdf), abs=2e-4)
    assert meta["date"] == "1969" and meta["date_note"] == "edition 2, Indian 1960 datum"
    assert meta["datum"] == "ING-A" and meta["format"] == "webp"
    with sqlite3.connect(out) as db:
        z15 = [bytes(t) for (t,) in db.execute("select tile_data from tiles where zoom_level = 15")]
    alphas = [np.asarray(Image.open(io.BytesIO(t)).convert("RGBA"))[..., 3] for t in z15]
    # The collar outside the neatline is transparent, the map inside it opaque.
    assert any(a.min() == 0 for a in alphas) and any(a.min() == 255 for a in alphas)


def test_wgs84_jog_sheet_in_utm_zone_49(tmp_path):
    """A 1:250,000 Joint Operations Graphic on WGS84 ("WGE"), central meridian 111 (UTM 49)."""
    pdf = write_geopdf(
        tmp_path / "1501AND4901_geo.pdf",
        origin=(300_000, 1_700_000),
        m_per_pt=100.0,
        datum="WGE",
        cm=111,
        projection="TC",
        info={"pri_date": "1986-03-07", "edition": "005", "title": "Joint Operations Graphic. Da Nang"},
    )
    out = tmp_path / "jog.mbtiles"
    w, s, _, _ = build_tiles.build(pdf, out, dpi=50)["bounds"]
    to_wgs84 = Transformer.from_crs("EPSG:32649", "EPSG:4326", always_xy=True)
    # The neatline's west edge is a grid line, so in degrees it leans: its westmost point is a corner.
    lons, lats = to_wgs84.transform([302_880, 302_880], [1_702_160, 1_719_440])
    assert (w, s) == pytest.approx((min(lons), min(lats)), abs=1e-4)
    meta = tiling.read_metadata(out)
    assert meta["date"] == "1986" and meta["date_note"] == "edition 5, WGS84"
    assert meta["name"] == "Joint Operations Graphic. Da Nang"


def test_unknown_datum_and_pdfs_without_georeferencing_are_refused(tmp_path):
    with pytest.raises(ValueError, match="unknown datum code 'XYZ'"):
        build_tiles.crs_for({"datum": "XYZ", "cm": 105, "k": 0.9996, "fe": 500000, "fn": 0})
    plain = tmp_path / "plain.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(plain)
    with pytest.raises(ValueError, match="no georeferencing"):
        build_tiles.build(plain, tmp_path / "x.mbtiles")
    assert not list(tmp_path.glob("*.mbtiles*"))


def test_command_line_reports_progress_then_the_result(tmp_path):
    pdf = write_geopdf(tmp_path / "S_geo.pdf")
    out = tmp_path / "S.mbtiles"
    done = subprocess.run(
        [sys.executable, str(SCRIPT), str(pdf), str(out), "--dpi", "100", "--max-zoom", "12", "--progress"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = done.stdout.splitlines()
    fractions = [float(line.split()[1]) for line in lines if line.startswith("progress ")]
    assert fractions == sorted(fractions) and fractions[-1] == pytest.approx(1.0)
    assert json.loads(lines[-1])["done"]["maxzoom"] == 12 and out.exists()


def test_version_2_sheet_with_only_a_ctm_is_placed_like_one_with_registration_points(tmp_path):
    """Some reissued sheets have no /Registration, only the /CTM page -> grid transform."""
    reference = build_tiles.build(write_geopdf(tmp_path / "R_geo.pdf"), tmp_path / "R.mbtiles", dpi=100)
    ctm_only = write_geopdf(tmp_path / "C_geo.pdf", georef="ctm")
    assert build_tiles.lgi(ctm_only)["georef"] == "ctm"
    report = build_tiles.build(ctm_only, tmp_path / "C.mbtiles", dpi=100)
    assert report["bounds"] == pytest.approx(reference["bounds"], abs=1e-9) and report["fit_m"] == 0.0
    assert tiling.read_metadata(tmp_path / "C.mbtiles")["georeferencing"] == "ctm"
    # With both, the registration points win (they are what the sheet was measured with).
    assert build_tiles.lgi(write_geopdf(tmp_path / "B_geo.pdf", georef="both"))["georef"] == "registration"


def test_utm_projection_named_only_by_its_zone(tmp_path):
    g = build_tiles.lgi(write_geopdf(tmp_path / "Z_geo.pdf", projection="UT-zone", cm=111))
    assert (g["cm"], g["k"], g["fe"], g["fn"]) == (111, 0.9996, 500_000, 0)


@pytest.mark.parametrize("code", ["IND-I", "IND-B", "INF-A"])
def test_mislabelled_indian_datum_codes_are_read_as_indian_1960(code):
    """Their margins print Indian datum 1960 (see datums.py); "WE" is WGS84."""
    assert build_tiles.BY_DMA_CODE[code] == build_tiles.BY_DMA_CODE["ING-A"]
    assert build_tiles.BY_DMA_CODE["WE"] == build_tiles.BY_DMA_CODE["WGE"]


def test_frame_cuts_off_what_the_neatline_lets_through(tmp_path):
    """A neatline that is the whole page (legend and all) leaves the cut to the frame."""
    pdf = write_geopdf(tmp_path / "P_geo.pdf", neatline="page")
    g = build_tiles.lgi(pdf)
    to_sheet = Transformer.from_crs(build_tiles.crs_for(g), f"+proj=longlat {build_tiles.datum_of(g)}", always_xy=True)
    # The frame: the middle half of the page, in degrees on the sheet's datum.
    (w, e), (s, n) = to_sheet.transform([560_000 + 1440, 560_000 + 4320], [1_150_000 + 1080, 1_150_000 + 3240])
    out = tmp_path / "P.mbtiles"
    whole = build_tiles.build(pdf, tmp_path / "whole.mbtiles", dpi=100)["bounds"]
    framed = build_tiles.build(pdf, out, dpi=100, frame=[w, s, e, n])["bounds"]
    to_wgs84 = Transformer.from_crs(f"+proj=longlat {build_tiles.datum_of(g)}", "EPSG:4326", always_xy=True)
    (fw, fe), (fs, fn) = to_wgs84.transform([w, e], [s, n])
    assert framed == pytest.approx([fw, fs, fe, fn], abs=2e-4)
    assert framed[2] - framed[0] < 0.6 * (whole[2] - whole[0])
    # A frame that has nothing to do with the sheet (a wrong catalogue entry) cuts nothing.
    far = build_tiles.build(pdf, tmp_path / "x.mbtiles", dpi=100, frame=[0.0, 0.0, 1.0, 1.0])
    assert far["cut"] == [] and far["bounds"] == pytest.approx(whole)


def test_a_map_reaching_past_its_frame_in_part_keeps_that_part(tmp_path):
    """A side is cut only where the neatline overshoots along nearly all of it: margin. A tab of
    map drawn past the frame (as Cam Ranh below the Nha Trang sheet's frame) stays."""
    # The neatline: the whole page, plus nothing; then a page whose lower left quarter is cut
    # away, so that below the frame's bottom only the left part of the map reaches on.
    whole = write_geopdf(tmp_path / "W_geo.pdf", neatline="page")
    g = build_tiles.lgi(whole)
    to_sheet = Transformer.from_crs(build_tiles.crs_for(g), f"+proj=longlat {build_tiles.datum_of(g)}", always_xy=True)
    # Frame: inside the page on the east and the south, past it on the west and the north.
    (w, e), (s, n) = to_sheet.transform([560_000 - 100, 560_000 + 4000], [1_150_000 + 1000, 1_150_000 + 4400])
    assert build_tiles.build(whole, tmp_path / "W.mbtiles", dpi=100, frame=[w, s, e, n])["cut"] == ["east", "south"]
    tab = write_geopdf(tmp_path / "T_geo.pdf", neatline="tab")
    r = build_tiles.build(tab, tmp_path / "T.mbtiles", dpi=100, frame=[w, s, e, n])
    assert r["cut"] == ["east"]
    assert r["bounds"][1] < s  # the tab below the frame is kept


def test_the_sheets_own_corners_win_over_the_catalogues(tmp_path):
    pdf = write_geopdf(tmp_path / "C_geo.pdf", neatline="page")
    g = build_tiles.lgi(pdf)
    to_sheet = Transformer.from_crs(build_tiles.crs_for(g), f"+proj=longlat {build_tiles.datum_of(g)}", always_xy=True)
    (w, e), (s, n) = to_sheet.transform([560_000 + 1440, 560_000 + 4320], [1_150_000 + 1080, 1_150_000 + 3240])
    corners = {"ll_long": w, "ll_lat": s, "ur_long": e, "ur_lat": n}
    pdf = write_geopdf(tmp_path / "C_geo.pdf", neatline="page", info={k: f"{v:+.9f}" for k, v in corners.items()})
    assert build_tiles.own_corners(pdf) == pytest.approx([w, s, e, n])
    # A wrong catalogue frame is ignored: the PDF's own is used.
    r = build_tiles.build(pdf, tmp_path / "C.mbtiles", dpi=100, frame=[0.0, 0.0, 1.0, 1.0])
    assert sorted(r["cut"]) == ["east", "north", "south", "west"]


def test_the_main_map_of_a_sheet_with_an_inset_is_the_one_placed(tmp_path):
    """A sheet with an inset (an island drawn apart) has one LGIDict per map; the inset's may come
    first. The main map, with the largest neatline, is the one placed."""
    from geopdf import numbers
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, NameObject

    pdf = write_geopdf(tmp_path / "M_geo.pdf")
    inset = PdfReader(write_geopdf(tmp_path / "I_geo.pdf", origin=(700_000, 1_900_000), m_per_pt=2.0)).pages[0]
    writer = PdfWriter(clone_from=pdf)
    inset_lgi = inset["/LGIDict"]
    inset_lgi[NameObject("/Neatline")] = numbers([100, 100, 120, 100, 120, 120, 100, 120])
    main_lgi = writer.pages[0]["/LGIDict"]
    writer.pages[0][NameObject("/LGIDict")] = ArrayObject([writer._add_object(inset_lgi), writer._add_object(main_lgi)])
    writer.write(tmp_path / "both_geo.pdf")
    assert build_tiles.lgi(tmp_path / "both_geo.pdf")["reg"][0][2:] == [560_000, 1_150_000]
