"""georef_scan: scans georeferenced from hand-picked control points and tiled into MBTiles."""

import json

import georef_scan
import numpy as np
import pytest
import tiling
from PIL import Image


@pytest.fixture
def scan(tmp_path, monkeypatch):
    monkeypatch.setattr(georef_scan, "ROOT", tmp_path)
    Image.fromarray(np.full((400, 600, 3), 128, np.uint8)).save(tmp_path / "s.jpg")
    # 600x400 px image spanning 105.0-105.6E, 10.0-10.4N (1 px = 0.001 deg).
    gcps = [[c, r, 105.0 + c / 1000, 10.4 - r / 1000] for c in (0, 300, 600) for r in (0, 200, 400)]

    def make(**extra):
        c = {"image": "s.jpg", "crs": "EPSG:4326", "gcps": gcps, "max_zoom": 9, **extra}
        p = tmp_path / "ctl.json"
        p.write_text(json.dumps(c))
        return p

    return make


@pytest.mark.parametrize("extra", [{}, {"order": 1}])
def test_gcp_scan_builds_tiles_in_the_right_place(scan, tmp_path, extra):
    # Regression: GCPs without z reached GDAL as Z="None" and every scan failed to build.
    report = georef_scan.build(scan(**extra), tmp_path / "ctl.mbtiles")
    w, s, e, n = report["bounds"]
    assert w == pytest.approx(105.0, abs=0.01) and e == pytest.approx(105.6, abs=0.01)
    assert s == pytest.approx(10.0, abs=0.01) and n == pytest.approx(10.4, abs=0.01)
    assert report["tiles"] > 0 and report["maxzoom"] == 9


def test_offset_shifts_whole_sheet(scan, tmp_path):
    w, _, _, n = georef_scan.build(scan(offset_deg=[0.05, -0.02]), tmp_path / "o.mbtiles")["bounds"]
    assert w == pytest.approx(105.05, abs=0.01) and n == pytest.approx(10.38, abs=0.01)


def test_meta_block_goes_into_the_tiles_metadata(scan, tmp_path):
    out = tmp_path / "m.mbtiles"
    georef_scan.build(scan(meta={"title": "Feuille 2", "date": "1889", "dateNote": "publication"}), out)
    meta = tiling.read_metadata(out)
    assert meta["name"] == "Feuille 2" and meta["date"] == "1889" and meta["date_note"] == "publication"


def test_image_can_be_given_explicitly(scan, tmp_path):
    other = tmp_path / "elsewhere.jpg"
    (tmp_path / "s.jpg").rename(other)
    assert georef_scan.build(scan(), tmp_path / "e.mbtiles", image=other)["tiles"] > 0


def test_missing_image_says_how_to_get_it(scan, tmp_path):
    (tmp_path / "s.jpg").unlink()
    with pytest.raises(FileNotFoundError, match="add the map again"):
        georef_scan.build(scan(), tmp_path / "x.mbtiles")
    assert not list(tmp_path.glob("*.mbtiles*"))
