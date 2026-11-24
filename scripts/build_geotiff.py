"""Turn a GeoTIFF the user added into a single MBTiles file of web tiles.

A GeoTIFF says where it lies (a coordinate system and a transform), in any projection;
tiling.write_tiles warps it onto the web map grid. This reads the picture as colour (a grey
or palette image is coloured, 16-bit and float data is stretched to 8 bits), makes the file's
empty pixels (no-data, or its alpha band) transparent, and hands the result on. A picture too big
to hold in memory is read at reduced size: the tiles would not show the extra detail anyway.

Usage: .venv/bin/python scripts/build_geotiff.py SRC.tif OUT.mbtiles [--title T] [--date YYYY] [--progress]
"""

import argparse
import json
import warnings
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.transform import Affine, array_bounds
from rasterio.warp import transform_bounds
from tiling import native_zoom, write_tiles

# Pixels read at most; beyond this the picture is shrunk while reading (about 240 MB of RGBA).
MAX_PIXELS = 60_000_000
# The deepest zoom tiled, whatever the file's resolution (drone pictures are centimetres a pixel).
MAX_ZOOM = 17
# Share of the progress spent reading the picture; the rest is tiling.
LOAD_SHARE = 0.15


def to_uint8(band, valid):
    """One band as uint8: 8-bit data as it is, anything else stretched between its 2nd and 98th
    percentile over the valid pixels."""
    if band.dtype == np.uint8:
        return band
    values = band[valid][::7] if valid.any() else band.ravel()[::7]
    lo, hi = np.percentile(values.astype(np.float64), [2, 98]) if values.size else (0.0, 1.0)
    scaled = (band.astype(np.float64) - lo) / ((hi - lo) or 1.0)
    return (np.clip(scaled, 0, 1) * 255).astype(np.uint8)


def read_rgba(ds):
    """The GeoTIFF as (4, h, w) uint8 RGBA, and the transform of that (possibly shrunk) grid."""
    k = max(1.0, (ds.width * ds.height / MAX_PIXELS) ** 0.5)
    w, h = max(1, round(ds.width / k)), max(1, round(ds.height / k))
    transform = ds.transform * Affine.scale(ds.width / w, ds.height / h)
    alpha_band = next((i for i, c in enumerate(ds.colorinterp, 1) if c == ColorInterp.alpha), None)
    colour = [i for i in range(1, ds.count + 1) if i != alpha_band]
    shape = (h, w)
    # The mask covers no-data and the alpha band together (255 = a real pixel).
    valid = ds.dataset_mask(out_shape=shape, resampling=Resampling.nearest) > 0
    if len(colour) >= 3:
        rgb = [to_uint8(ds.read(i, out_shape=shape, resampling=Resampling.average), valid) for i in colour[:3]]
    else:
        band = ds.read(colour[0], out_shape=shape, resampling=Resampling.average)
        try:
            table = ds.colormap(colour[0])
        except ValueError:
            table = None
        if table and band.dtype == np.uint8:
            lut = np.array([table.get(v, (0, 0, 0, 255))[:3] for v in range(256)], np.uint8)
            rgb = list(lut[band].transpose(2, 0, 1))
        else:
            rgb = [to_uint8(band, valid)] * 3
    return np.stack([*rgb, valid.astype(np.uint8) * 255]), transform


def build(src, out, *, title="", date="", progress=None):
    """Tile the GeoTIFF `src` into the MBTiles file `out`. Returns {"tiles", "bounds", "maxzoom"}."""
    report = progress or (lambda fraction: None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        ds = rasterio.open(src)
    with ds:
        if ds.crs is None or ds.transform.is_identity:
            raise ValueError(f"{Path(src).name} has no position on the Earth (no coordinate system)")
        bands, transform = read_rgba(ds)
        crs = ds.crs
    if not bands[3].any():
        raise ValueError(f"{Path(src).name} has no picture in it (every pixel is empty)")
    report(LOAD_SHARE)
    _, h, w = bands.shape
    extent = transform_bounds(crs, "EPSG:4326", *array_bounds(h, w, transform))
    n, bounds, zoom = write_tiles(
        bands,
        crs,
        out,
        transform=transform,
        max_zoom=min(native_zoom(extent, w), MAX_ZOOM),
        name=title or Path(src).stem,
        meta={"date": date},
        progress=lambda fraction: report(LOAD_SHARE + (1 - LOAD_SHARE) * fraction),
    )
    return {"tiles": n, "bounds": bounds, "maxzoom": zoom}


def main(argv=None):
    """Build one GeoTIFF; print a one-line report (and progress lines with --progress)."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path, help="the GeoTIFF")
    ap.add_argument("out", type=Path, help="MBTiles file to write")
    ap.add_argument("--title", default="", help="the map's name")
    ap.add_argument("--date", default="", help="the map's year")
    ap.add_argument("--progress", action="store_true", help='print "progress <0..1>" lines while working')
    args = ap.parse_args(argv)

    def progress(fraction):
        print(f"progress {fraction:.3f}", flush=True)

    result = build(args.src, args.out, title=args.title, date=args.date, progress=progress if args.progress else None)
    print(json.dumps({"done": result}), flush=True)


if __name__ == "__main__":
    main()
