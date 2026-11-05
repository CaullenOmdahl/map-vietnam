"""tiling: WebP tiles in one MBTiles file, written atomically, rows flipped to TMS, empty tiles left out."""

import io
import sqlite3

import numpy as np
import pytest
import tiling
from PIL import Image
from rasterio.crs import CRS
from rasterio.transform import from_bounds


def test_writer_flips_rows_to_tms_and_renames_only_when_complete(tmp_path):
    out = tmp_path / "m.mbtiles"
    with tiling.MBTilesWriter(out) as w:
        w.put(3, 5, 1, b"tile")
        assert w.partial.exists() and not out.exists()
        w.metadata = {"name": "M"}
    assert out.exists() and not w.partial.exists()
    with sqlite3.connect(out) as db:
        # XYZ row 1 at zoom 3 is TMS row 2**3 - 1 - 1 = 6.
        assert db.execute("select zoom_level, tile_column, tile_row from tiles").fetchall() == [(3, 5, 6)]
    db = tiling.connect_read_only(out)
    assert tiling.read_tile(db, 3, 5, 1) == b"tile" and tiling.read_tile(db, 3, 5, 6) is None
    assert tiling.read_metadata(out) == {"name": "M"}


def test_a_failed_write_leaves_nothing(tmp_path):
    out = tmp_path / "m.mbtiles"
    with pytest.raises(RuntimeError), tiling.MBTilesWriter(out) as w:
        w.put(0, 0, 0, b"x")
        raise RuntimeError("build failed")
    assert list(tmp_path.iterdir()) == []


def test_read_only_connection_never_creates_a_file(tmp_path):
    assert tiling.read_metadata(tmp_path / "missing.mbtiles") == {}
    assert not (tmp_path / "missing.mbtiles").exists()


def test_opaque_tiles_drop_alpha_and_edge_tiles_keep_it():
    opaque = np.full((256, 256, 4), 200, np.uint8)
    opaque[..., 3] = 255
    edge = opaque.copy()
    edge[:, :100, 3] = 0
    assert Image.open(io.BytesIO(tiling.encode_webp(opaque))).mode == "RGB"
    decoded = np.asarray(Image.open(io.BytesIO(tiling.encode_webp(edge))).convert("RGBA"))
    assert decoded[:, :100, 3].max() == 0 and decoded[:, 100:, 3].min() == 255
    assert Image.open(io.BytesIO(tiling.empty_tile())).convert("RGBA").getextrema()[3] == (0, 0)


def test_write_tiles_stores_only_tiles_with_map_and_records_its_extent(tmp_path):
    # A 0.2 x 0.2 degree image, opaque only in its west half.
    bands = np.zeros((4, 200, 200), np.uint8)
    bands[:3] = 120
    bands[3, :, :100] = 255
    bounds = (105.0, 10.0, 105.2, 10.2)
    seen = []
    out = tmp_path / "m.mbtiles"
    n, got, zoom = tiling.write_tiles(
        bands,
        CRS.from_epsg(4326),
        out,
        transform=from_bounds(*bounds, 200, 200),
        max_zoom=11,
        name="Test",
        meta={"date": "1969", "empty": ""},
        progress=seen.append,
    )
    assert zoom == 11 and got == pytest.approx(list(bounds))
    assert seen == sorted(seen) and seen[-1] == pytest.approx(1.0)
    meta = tiling.read_metadata(out)
    assert meta["format"] == "webp" and meta["minzoom"] == "8" and meta["maxzoom"] == "11"
    assert meta["date"] == "1969" and "empty" not in meta and meta["name"] == "Test"
    with sqlite3.connect(out) as db:
        assert db.execute("select count(*) from tiles").fetchone()[0] == n
        # Every stored z11 tile lies over the opaque west half (west of 105.1 E).
        cols = [c for (c,) in db.execute("select tile_column from tiles where zoom_level = 11")]
    assert cols and all(tiling.TMS.bounds(tiling.morecantile.Tile(c, 0, 11)).left < 105.1 for c in cols)
