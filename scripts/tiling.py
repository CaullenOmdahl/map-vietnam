"""Warp a georeferenced RGBA image onto the Web Mercator tile grid and store it as one MBTiles file.

The shared last step of the map pipeline: build_tiles (GeoPDF sheets with an affine transform)
and georef_scan (scans placed by ground control points) both hand their image to write_tiles.

One map is one file, web/tiles/<id>.mbtiles (MBTiles 1.3: an SQLite database of 256 px tiles plus
a metadata table, https://github.com/mapbox/mbtiles-spec). Adding or removing a map is then a
single file operation, and the server answers /tiles/<id>/{z}/{x}/{y}.webp with one indexed query.

Invariants every reader can rely on:
  - tiles are WebP (quality 85, with alpha where the tile has transparent pixels); a 1:50,000
    sheet is about 9 MB instead of about 70 MB as PNG, at about 39 dB PSNR;
  - rows are numbered TMS style (y counted from the bottom), as the MBTiles spec requires;
    read_tile takes the XYZ row the web map asks for and flips it;
  - fully transparent tiles are not stored: a missing row means "nothing here";
  - the file is written as <id>.mbtiles.partial and renamed only once complete, so a file
    named .mbtiles is always a finished map.

Import the functions; there is nothing to run.
"""

import io
import math
import os
import sqlite3
from contextlib import closing

import morecantile
import numpy as np
from PIL import Image
from rasterio.transform import Affine, array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject, transform_bounds

TMS = morecantile.tms.get("WebMercatorQuad")
TILE_PX = 256
# Below z8 a 1:50,000 sheet (about 28 km across) covers only a few dozen pixels: not worth tiling.
MIN_ZOOM = 8
# Ground size of one pixel at zoom 0 on the equator for 256 px Web Mercator tiles (m/px).
Z0_METRES_PER_PX = 156_543.03
# Length of one degree of longitude on the equator (m); scaled by cos(lat) elsewhere.
METRES_PER_DEG_LON = 111_320
# WebP settings measured on a real sheet: q85 keeps small map lettering legible; method 4 is
# Pillow's default speed/size trade-off.
WEBP_QUALITY = 85
WEBP_METHOD = 4
PARTIAL_SUFFIX = ".partial"


def native_zoom(bounds, width_px):
    """Lowest zoom whose pixels are at least as fine as the source image's.

    `bounds` is (west, south, east, north) in degrees and `width_px` the image width across it.
    Tiling deeper would only upsample, so this is the default deepest zoom (never below MIN_ZOOM).
    """
    lon0, lat0, lon1, lat1 = bounds
    metres = (lon1 - lon0) * METRES_PER_DEG_LON * math.cos(math.radians((lat0 + lat1) / 2))
    src_res = metres / width_px
    return max(MIN_ZOOM, math.ceil(math.log2(Z0_METRES_PER_PX * math.cos(math.radians(lat0)) / src_res)))


def encode_webp(rgba):
    """WebP bytes for one (256, 256, 4) uint8 tile. A fully opaque tile is stored without an
    alpha channel, which makes it smaller and looks the same."""
    img = Image.fromarray(rgba, "RGBA")
    if rgba[..., 3].min() == 255:
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
    return buf.getvalue()


def empty_tile():
    """A fully transparent 256 px WebP: what the server sends for a tile that is not stored."""
    buf = io.BytesIO()
    Image.new("RGBA", (TILE_PX, TILE_PX), (0, 0, 0, 0)).save(buf, "WEBP", lossless=True)
    return buf.getvalue()


class MBTilesWriter:
    """Write one MBTiles file atomically: `with MBTilesWriter(path) as w: w.put(z, x, y, data)`.

    Tiles go into <path>.partial; leaving the block normally writes `w.metadata` and renames the
    file to `path`; leaving it with an exception deletes it. `path` itself is never half-written.
    """

    def __init__(self, path):
        self.path = path
        self.partial = path.with_name(path.name + PARTIAL_SUFFIX)
        self.metadata = {}
        self.count = 0

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.partial.unlink(missing_ok=True)  # left over from an interrupted build
        self.db = sqlite3.connect(self.partial)
        # The partial file is thrown away on any failure, so durability is not needed while
        # writing: no journal and no fsync make the build several times faster.
        self.db.executescript(
            """
            pragma journal_mode = off;
            pragma synchronous = off;
            create table metadata (name text, value text);
            create table tiles (zoom_level integer, tile_column integer, tile_row integer, tile_data blob);
            create unique index tile_index on tiles (zoom_level, tile_column, tile_row);
            """
        )
        return self

    def put(self, z, x, y, data):
        """Store tile (z, x, y), y in XYZ numbering (from the top), as encoded bytes."""
        self.db.execute(
            "insert or replace into tiles values (?, ?, ?, ?)", (z, x, (1 << z) - 1 - y, sqlite3.Binary(data))
        )
        self.count += 1

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                rows = [(k, str(v)) for k, v in self.metadata.items()]
                self.db.executemany("insert into metadata values (?, ?)", rows)
                self.db.commit()
        finally:
            self.db.close()
        if exc_type is None:
            os.replace(self.partial, self.path)
        else:
            self.partial.unlink(missing_ok=True)
        return False


def standard_metadata(name, bounds, min_zoom, max_zoom, **extra):
    """The MBTiles 1.3 metadata rows for an overlay of WebP tiles, plus any `extra` keys
    (this project adds e.g. "date" and "edition" read from the source map). Empty extras are
    left out."""
    w, s, e, n = bounds
    meta = {
        "name": name,
        "format": "webp",
        "type": "overlay",
        "version": "1",
        "bounds": f"{w:.6f},{s:.6f},{e:.6f},{n:.6f}",
        "center": f"{(w + e) / 2:.6f},{(s + n) / 2:.6f},{min_zoom}",
        "minzoom": str(min_zoom),
        "maxzoom": str(max_zoom),
    }
    meta.update({k: str(v) for k, v in extra.items() if v not in (None, "")})
    return meta


def write_tiles(
    bands,
    src_crs,
    out_path,
    *,
    transform=None,
    gcps=None,
    max_zoom=None,
    bounds=None,
    name="",
    meta=None,
    progress=None,
):
    """Tile `bands` from MIN_ZOOM to `max_zoom` into the MBTiles file `out_path`.

    `bands` is a uint8 array (4, h, w) in RGBA order whose alpha marks the map area; it is placed
    in `src_crs` by either an affine `transform` or a list of rasterio `gcps`. `max_zoom` defaults
    to native_zoom. `bounds` ([west, south, east, north] in degrees) limits the tiles to the map
    frame and is what the file records as its extent; it defaults to the whole image.
    `meta` adds metadata keys. `progress(fraction)` is called as the work gets done.
    Returns (tiles written, [west, south, east, north], max_zoom).
    """
    _, h, w = bands.shape
    if transform is not None:
        image_bounds = transform_bounds(src_crs, "EPSG:4326", *array_bounds(h, w, transform))
        src = {"src_transform": transform}
    else:
        geo_transform, geo_w, geo_h = calculate_default_transform(src_crs, "EPSG:4326", w, h, gcps=gcps)
        image_bounds = array_bounds(geo_h, geo_w, geo_transform)
        src = {"gcps": gcps}
    bounds = list(bounds or image_bounds)
    max_zoom = max_zoom or native_zoom(image_bounds, w)
    zooms = range(MIN_ZOOM, max_zoom + 1)
    # Each zoom has four times the pixels of the one before, so weight progress by 4^z.
    total_weight = sum(4**z for z in zooms)
    done_weight = 0

    with MBTilesWriter(out_path) as out:
        for z in zooms:
            # Warp the whole extent into one mosaic per zoom, aligned to the tile grid, then cut it
            # up: one reproject call per zoom is far faster than one per tile and leaves no seams.
            tiles = list(TMS.tiles(*bounds, [z]))
            x0, y0 = min(t.x for t in tiles), min(t.y for t in tiles)
            nx, ny = max(t.x for t in tiles) - x0 + 1, max(t.y for t in tiles) - y0 + 1
            ul = TMS.xy_bounds(morecantile.Tile(x0, y0, z))
            res = (ul.right - ul.left) / TILE_PX
            mosaic = np.zeros((4, ny * TILE_PX, nx * TILE_PX), np.uint8)
            reproject(
                bands,
                mosaic,
                src_crs=src_crs,
                **src,
                dst_transform=Affine(res, 0, ul.left, 0, -res, ul.top),
                dst_crs="EPSG:3857",
                # Bilinear keeps line work crisp at full detail; averaging avoids aliasing when shrinking.
                resampling=Resampling.bilinear if z == max_zoom else Resampling.average,
            )
            for ty in range(ny):
                for tx in range(nx):
                    block = mosaic[:, ty * TILE_PX : (ty + 1) * TILE_PX, tx * TILE_PX : (tx + 1) * TILE_PX]
                    if block[3].any():
                        out.put(z, x0 + tx, y0 + ty, encode_webp(np.ascontiguousarray(block.transpose(1, 2, 0))))
                if progress:
                    progress((done_weight + 4**z * (ty + 1) / ny) / total_weight)
            done_weight += 4**z
        out.metadata = standard_metadata(name or out_path.stem, bounds, MIN_ZOOM, max_zoom, **(meta or {}))
        written = out.count
    return written, bounds, max_zoom


def connect_read_only(path):
    """A read-only SQLite connection to an MBTiles file: it never creates or changes the file."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)


def read_tile(db, z, x, y):
    """Tile bytes for XYZ tile (z, x, y) from an open MBTiles connection, or None if not stored."""
    row = db.execute(
        "select tile_data from tiles where zoom_level = ? and tile_column = ? and tile_row = ?",
        (z, x, (1 << z) - 1 - y),
    ).fetchone()
    return bytes(row[0]) if row else None


def read_metadata(path):
    """The metadata table of an MBTiles file as a dict of strings ({} if it cannot be read)."""
    try:
        with closing(connect_read_only(path)) as db:
            return dict(db.execute("select name, value from metadata"))
    except sqlite3.Error:
        return {}
