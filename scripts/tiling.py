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
from pathlib import Path

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
# Terrarium RGB elevation formula: height_m = (R * 256 + G + B / 256) - 32768 (1/256 m steps).
TERRARIUM_OFFSET = 32_768.0
TERRARIUM_MIN_M = -32_768.0
TERRARIUM_MAX_M = 32_767.99609375


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


def encode_terrarium_webp(elev_m, valid_mask=None):
    """Lossless Terrarium-encoded WebP bytes for a (256, 256) float elevation grid in metres.

    Lossy WebP chroma subsampling corrupts RGB-packed elevation into 10-50 m steps, so DEM tiles
    are always written with `lossless=True`. No-data pixels are encoded as sea-level zero (with
    alpha=0 when `valid_mask` has missing cells so MapLibre treats missing edges cleanly).
    """
    h, w = elev_m.shape
    mask = np.isfinite(elev_m) if valid_mask is None else (valid_mask & np.isfinite(elev_m))
    clean = np.where(mask, np.clip(elev_m, TERRARIUM_MIN_M, TERRARIUM_MAX_M), 0.0)
    scaled = np.rint((clean + TERRARIUM_OFFSET) * 256.0).astype(np.uint32)
    r = ((scaled >> 16) & 0xFF).astype(np.uint8)
    g = ((scaled >> 8) & 0xFF).astype(np.uint8)
    b = (scaled & 0xFF).astype(np.uint8)
    if mask.all():
        img = Image.fromarray(np.dstack([r, g, b]), "RGB")
    else:
        a = np.where(mask, 255, 0).astype(np.uint8)
        img = Image.fromarray(np.dstack([r, g, b, a]), "RGBA")
    buf = io.BytesIO()
    img.save(buf, "WEBP", lossless=True, method=WEBP_METHOD)
    return buf.getvalue()


def decode_terrarium_webp(tile_bytes):
    """(elev_m float32 array, valid bool mask) decoded from a Terrarium WebP or PNG tile."""
    with Image.open(io.BytesIO(tile_bytes)) as img:
        rgba = np.asarray(img.convert("RGBA"))
    r = rgba[..., 0].astype(np.float32)
    g = rgba[..., 1].astype(np.float32)
    b = rgba[..., 2].astype(np.float32)
    valid = rgba[..., 3] > 0
    elev = (r * 256.0 + g + b / 256.0) - TERRARIUM_OFFSET
    return elev, valid


def empty_terrarium_tile():
    """A 256 px lossless transparent Terrarium tile (0 m, alpha=0) for unstored ocean/empty cells."""
    return encode_terrarium_webp(np.zeros((TILE_PX, TILE_PX), np.float32), np.zeros((TILE_PX, TILE_PX), bool))


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


DEM_CHUNK_TILES = 16  # 16x16 tiles (4096x4096 float32 = 64 MB max RAM per chunk on Railway/desktop)


def write_dem_tiles(
    elev,
    src_crs,
    out_path,
    *,
    transform,
    valid_mask=None,
    min_zoom=MIN_ZOOM,
    max_zoom=None,
    bounds=None,
    name="",
    meta=None,
    progress=None,
):
    """Warp a 2D float elevation grid `elev` (metres) onto Web Mercator and store lossless Terrarium
    WebP tiles in `out_path`. Tiles are warped in bounded 16x16-tile chunks so wide regional or
    country DEM builds use under 100 MB of memory."""
    elev_f = np.asarray(elev, dtype=np.float32)
    h, w = elev_f.shape
    mask = np.isfinite(elev_f) if valid_mask is None else (np.asarray(valid_mask, dtype=bool) & np.isfinite(elev_f))
    src_elev = np.where(mask, elev_f, np.nan).astype(np.float32)
    image_bounds = transform_bounds(src_crs, "EPSG:4326", *array_bounds(h, w, transform))
    bounds = list(bounds or image_bounds)
    max_zoom = max(min_zoom, max_zoom or native_zoom(image_bounds, w))
    zooms = range(min_zoom, max_zoom + 1)
    total_weight = sum(4**z for z in zooms) or 1
    done_weight = 0

    with MBTilesWriter(Path(out_path)) as out:
        for z in zooms:
            tiles = list(TMS.tiles(*bounds, [z]))
            if not tiles:
                done_weight += 4**z
                continue
            x0, y0 = min(t.x for t in tiles), min(t.y for t in tiles)
            nx, ny = max(t.x for t in tiles) - x0 + 1, max(t.y for t in tiles) - y0 + 1
            for cy in range(0, ny, DEM_CHUNK_TILES):
                ch_y = min(DEM_CHUNK_TILES, ny - cy)
                for cx in range(0, nx, DEM_CHUNK_TILES):
                    ch_x = min(DEM_CHUNK_TILES, nx - cx)
                    ul = TMS.xy_bounds(morecantile.Tile(x0 + cx, y0 + cy, z))
                    res = (ul.right - ul.left) / TILE_PX
                    dst = np.full((ch_y * TILE_PX, ch_x * TILE_PX), np.nan, np.float32)
                    reproject(
                        src_elev,
                        dst,
                        src_crs=src_crs,
                        src_transform=transform,
                        src_nodata=np.nan,
                        dst_transform=Affine(res, 0, ul.left, 0, -res, ul.top),
                        dst_crs="EPSG:3857",
                        dst_nodata=np.nan,
                        resampling=Resampling.bilinear if z == max_zoom else Resampling.average,
                    )
                    for ty in range(ch_y):
                        for tx in range(ch_x):
                            block = dst[ty * TILE_PX : (ty + 1) * TILE_PX, tx * TILE_PX : (tx + 1) * TILE_PX]
                            bmask = np.isfinite(block)
                            if bmask.any():
                                out.put(z, x0 + cx + tx, y0 + cy + ty, encode_terrarium_webp(block, bmask))
                if progress:
                    progress((done_weight + 4**z * min(ny, cy + DEM_CHUNK_TILES) / ny) / total_weight)
            done_weight += 4**z
        extra = {"encoding": "terrarium", "type": "dem", **(meta or {})}
        out.metadata = standard_metadata(name or Path(out_path).stem, bounds, min_zoom, max_zoom, **extra)
        written = out.count
    return written, bounds, max_zoom


def lonlat_to_tile_pixel(lon, lat, z):
    """(tile_x, tile_y, px_x, px_y) in XYZ numbering for WGS84 `(lon, lat)` at zoom `z`."""
    n = 1 << z
    lat_c = max(-85.05112878, min(85.05112878, float(lat)))
    rad = math.radians(lat_c)
    fx = ((float(lon) + 180.0) / 360.0) * n
    fy = ((1.0 - math.log(math.tan(rad) + 1.0 / math.cos(rad)) / math.pi) / 2.0) * n
    tx = min(n - 1, max(0, int(math.floor(fx))))
    ty = min(n - 1, max(0, int(math.floor(fy))))
    px = min(TILE_PX - 1, max(0, int((fx - tx) * TILE_PX)))
    py = min(TILE_PX - 1, max(0, int((fy - ty) * TILE_PX)))
    return tx, ty, px, py


def sample_dem_mbtiles(path, lon, lat):
    """Sample elevation (float metres) at WGS84 `(lon, lat)` from the deepest available tile in a
    Terrarium MBTiles file `path`, or return None if not covered or unreadable."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        with closing(connect_read_only(path)) as db:
            meta = dict(db.execute("select name, value from metadata"))
            min_z = int(meta.get("minzoom", 0))
            max_z = int(meta.get("maxzoom", 15))
            for z in range(max_z, min_z - 1, -1):
                tx, ty, px, py = lonlat_to_tile_pixel(lon, lat, z)
                raw = read_tile(db, z, tx, ty)
                if not raw:
                    continue
                elev, valid = decode_terrarium_webp(raw)
                if valid[py, px]:
                    return round(float(elev[py, px]), 2)
    except (sqlite3.Error, ValueError, OSError):
        return None
    return None
