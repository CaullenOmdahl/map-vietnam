"""Terrain, spaceborne LiDAR, bathymetry, and historical overhead imagery providers.

Shared by both the standalone local server (`scripts/server.py`) and the Railway Intermediary Hub
(`scripts/hub/hub_server.py`) so every capability works locally and benefits from the Hub's
infinite write-once cache and vaulted credentials when connected:

  - Streaming and windowed-COG bare-earth elevation (`GEDTM30` + `std` uncertainty, `GLO-30 HAND`,
    `Mapterhorn` DSM, `AWS Terrarium`, `ETH 10m` & `Meta 1m` LiDAR-calibrated canopy height)
  - Point elevation + vertical uncertainty + Vietnam Hòn Dấu MSL datum note + NARA RG 373 1°x1°
    aerial reconnaissance overlay index lookup
  - Great-circle elevation profile and Earth-curvature-adjusted line-of-sight intervisibility
  - Spaceborne LiDAR footprint queries (`NASA GEDI` & `ICESat-2`) via SlideRule Earth
  - Historical overhead discovery & import:
      * French SHD / IAO First Indochina War aerials on NAKALA (`SHDAI`, 1,255 missions + plot cards)
      * USGS Declassified Spy Satellites (`CORONA`, `KH-7`, `KH-9` Mapping & Panoramic) via M2M API
      * Wartime `Landsat 1-3 MSS` (1972-1983) via Planetary Computer STAC + NIR defoliation composites
      * `OpenAerialMap` community UAV orthomosaics (STAC + TiTiler XYZ)
      * `Allmaps` community-georeferenced IIIF historical maps (Gallica, Rumsey, LoC, Gaihozu)
"""

import json
import math
import re
import statistics
import unicodedata
import urllib.parse
import warnings
from pathlib import Path

import datums
import numpy as np
import paths
import rasterio
import tiling
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.warp import reproject
from rasterio.windows import from_bounds as window_from_bounds

EARTH_RADIUS_M = 6_371_008.8
# Standard atmospheric refraction coefficient k=0.13 reduces effective curvature drop slightly.
EFFECTIVE_EARTH_RADIUS_M = EARTH_RADIUS_M / (1.0 - 0.13)

GEDTM30_ELEV_COG = (
    "https://s3.opengeohub.org/global/edtm/gedtm_rf_m_30m_s_20060101_20151231_go_epsg.4326.3855_v20250611.tif"
)
GEDTM30_STD_COG = (
    "https://s3.opengeohub.org/global/edtm/gedtm_rf_std_30m_s_20060101_20151231_go_epsg.4326.3855_v20250611.tif"
)
VIETNAM_BOUNDS = [102.0, 8.0, 110.5, 23.6]
MAX_DEM_BOX_DEG = 2.5  # up to ~275 x 275 km per regional on-demand COG crop (~25 MB)

# Built-in streaming terrain and overhead sources requiring zero local disk space.
STREAMING_TERRAIN_SOURCES = (
    {
        "id": "mapterhorn_dsm",
        "title": "Mapterhorn Global Terrain (Surface DSM)",
        "note": "Zero-download streaming surface model (Copernicus GLO-30 over Vietnam; includes tree canopy).",
        "tiles": ["https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"],
        "tileSize": 512,
        "maxzoom": 15,
        "encoding": "terrarium",
        "attribution": (
            "Terrain: Mapterhorn · © DLR e.V. 2010–2014 and © Airbus Defence and Space GmbH 2014–2018 "
            "provided under COPERNICUS by the EU and ESA"
        ),
    },
    {
        "id": "aws_terrarium_dsm",
        "title": "AWS Open Terrain (SRTM Surface DSM)",
        "note": "Zero-download streaming SRTM radar surface model (includes tree canopy).",
        "tiles": ["https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"],
        "tileSize": 256,
        "maxzoom": 14,
        "encoding": "terrarium",
        "attribution": "Terrain: Mapzen / AWS Open Data (NASA SRTM, public domain)",
    },
)

# Regional bare-earth & hydrological COG packs that can be built on demand for any bounding box.
BUILDABLE_COG_PACKS = {
    "gedtm30": {
        "title": "GEDTM30 Bare-Earth Terrain (30 m, Canopy Removed)",
        "series": "OpenGeoHub GEDTM30 (GEDI + ICESat-2 LiDAR Fusion)",
        "url": GEDTM30_ELEV_COG,
        "scale_factor": 0.1,
        "attribution": "Bare-earth DEM: GEDTM30 (Ho, Hengl et al., CC BY 4.0; calibrated with NASA GEDI & ICESat-2)",
        "licence_policy": "open",
        "max_zoom": 12,
    },
    "glo30_hand": {
        "title": "Height Above Nearest Drainage (GLO-30 HAND, 30 m)",
        "series": "Alaska Satellite Facility GLO-30 HAND",
        "url_template": (
            "https://glo-30-hand.s3.us-west-2.amazonaws.com/v1/2021/"
            "Copernicus_DSM_COG_10_N{lat:02d}_00_E{lon:03d}_00_HAND.tif"
        ),
        "scale_factor": 1.0,
        "attribution": "HAND: © Alaska Satellite Facility (CC0 1.0), from Copernicus GLO-30 DEM",
        "licence_policy": "open",
        "max_zoom": 12,
    },
    "eth_canopy_10m": {
        "title": "ETH Tree Canopy Height (10 m, GEDI Calibrated)",
        "series": "ETH Zürich Global Canopy Height 2020",
        "url_template_3deg": (
            "https://share.phys.ethz.ch/~pf/nlangdata/ETH_GlobalCanopyHeight_10m_2020_version1/"
            "3deg_cogs/ETH_GlobalCanopyHeight_10m_2020_N{lat3:02d}E{lon3:03d}_Map.tif"
        ),
        "scale_factor": 1.0,
        "attribution": "Canopy height: Lang et al., ETH Zürich (CC BY 4.0, calibrated with NASA GEDI LiDAR)",
        "licence_policy": "open",
        "max_zoom": 13,
    },
}


# ---- NARA Record Group 373 1°x1° Aerial Sortie Overlay Index -----------------------------------
def nara_rg373_info(lon, lat):
    """Return the NARA RG 373 1°x1° Degree Square identifier and catalog search URL for `(lon, lat)`.

    Foreign aerial reconnaissance overlay indexes at the US National Archives (Record Group 373,
    1935-1971) are filed by southwest degree-square corner, e.g. `10N105E` for Cao Lãnh or
    `16N107E` for Huế / A Shau Valley.
    """
    lat_i = math.floor(float(lat))
    lon_i = math.floor(float(lon))
    ns = "N" if lat_i >= 0 else "S"
    ew = "E" if lon_i >= 0 else "W"
    code = f"{abs(lat_i):02d}{ns}{abs(lon_i):03d}{ew}"
    q = urllib.parse.quote(f'"Overlay Indexes" "{code}"')
    return {
        "degree_square": code,
        "bounds": [float(lon_i), float(lat_i), float(lon_i + 1), float(lat_i + 1)],
        "catalog_url": f"https://catalog.archives.gov/search?q={q}&recordGroupNumber=373",
        "series": "NARA RG 373: Overlay Indexes for Aerial Photography of the Defense Intelligence Agency, 1935–1971",
    }


def lonlat_to_quadkey(lon, lat, zoom=9):
    """Bing/Maxar quadkey string at `zoom` (used by Meta/WRI 1m Canopy Height COG tiles on AWS S3)."""
    tx, ty, _, _ = tiling.lonlat_to_tile_pixel(lon, lat, zoom)
    digits = []
    for i in range(zoom, 0, -1):
        digit = 0
        mask = 1 << (i - 1)
        if tx & mask:
            digit += 1
        if ty & mask:
            digit += 2
        digits.append(str(digit))
    return "".join(digits)


def meta_canopy_cog_url(lon, lat):
    """Direct public AWS S3 COG URL for Meta/WRI 1-metre Canopy Height covering `(lon, lat)`."""
    qk = lonlat_to_quadkey(lon, lat, zoom=9)
    return f"https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float/chm/{qk}.tif"


# ---- Windowed COG DEM / HAND / Canopy Pack Builder ----------------------------------------------
def check_dem_bbox(bbox):
    """Validate `[w, s, e, n]` for an on-demand regional COG pack."""
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise ValueError("Provide an area as [west, south, east, north] in degrees.")
    w, s, e, n = (round(float(v), 4) for v in bbox)
    if not (w < e and s < n):
        raise ValueError("Area needs west < east and south < north.")
    if (e - w) > MAX_DEM_BOX_DEG or (n - s) > MAX_DEM_BOX_DEG:
        raise ValueError(
            f"Choose an area up to {MAX_DEM_BOX_DEG:g}° across (zoom in slightly) so the terrain pack stays compact."
        )
    return [w, s, e, n]


def read_cog_window(cog_url, bbox, scale_factor=1.0):
    """Read `(elev_float32, valid_bool, transform, crs)` for `bbox` (`[w, s, e, n]`) from a remote or local COG."""
    w, s, e, n = bbox
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with rasterio.open(cog_url) as ds:
            win = window_from_bounds(w, s, e, n, ds.transform)
            win = win.round_offsets().round_lengths()
            col_off = max(0, int(win.col_off))
            row_off = max(0, int(win.row_off))
            width = max(1, min(int(ds.width - col_off), int(win.width)))
            height = max(1, min(int(ds.height - row_off), int(win.height)))
            clipped = rasterio.windows.Window(col_off, row_off, width, height)
            raw = ds.read(1, window=clipped, resampling=Resampling.bilinear)
            mask = ds.read_masks(1, window=clipped) > 0
            transform = ds.window_transform(clipped)
            nodata = ds.nodata
            crs = ds.crs or rasterio.crs.CRS.from_epsg(4326)
    arr = raw.astype(np.float32)
    if nodata is not None and np.isfinite(nodata):
        mask &= arr != float(nodata)
    arr = arr * float(scale_factor)
    mask &= np.isfinite(arr) & (arr > -12_000.0) & (arr < 12_000.0)
    return arr, mask, transform, crs


def _mosaic_tiled_cog_windows(tile_specs, bbox, scale_factor):
    """Read and mosaic one or more `(url, sub_bbox)` COG windows onto `bbox` (`[w, s, e, n]`)."""
    w, s, e, n = bbox
    pieces = []
    for url, cb in tile_specs:
        try:
            sub_arr, sub_mask, sub_tr, sub_crs = read_cog_window(url, cb, scale_factor)
        except Exception:
            continue
        if sub_mask.any():
            pieces.append((sub_arr, sub_mask, sub_tr, sub_crs))
    if not pieces:
        raise ValueError("No land elevation pixels were found in that area.")
    if len(pieces) == 1:
        return pieces[0]
    min_dx = min(abs(p[2].a) for p in pieces if p[2].a)
    min_dy = min(abs(p[2].e) for p in pieces if p[2].e)
    out_w = max(1, min(16_384, int(round((e - w) / min_dx))))
    out_h = max(1, min(16_384, int(round((n - s) / min_dy))))
    dst_transform = from_bounds(w, s, e, n, out_w, out_h)
    dst_crs = rasterio.crs.CRS.from_epsg(4326)
    dst = np.full((out_h, out_w), np.nan, dtype=np.float32)
    for sub_arr, sub_mask, sub_tr, sub_crs in pieces:
        src = np.where(sub_mask, sub_arr, np.nan).astype(np.float32)
        reproject(
            src,
            dst,
            src_transform=sub_tr,
            src_crs=sub_crs,
            src_nodata=np.nan,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            dst_nodata=np.nan,
            init_dest_nodata=False,
            resampling=Resampling.bilinear,
        )
    valid = np.isfinite(dst) & (dst > -12_000.0) & (dst < 12_000.0)
    return np.where(valid, dst, 0.0).astype(np.float32), valid, dst_transform, dst_crs


def build_cog_dem_pack(pack_key, bbox, out_path, *, progress=None):
    """Stream `bbox` from a public COG (`gedtm30`, `glo30_hand`, `eth_canopy_10m`) and write lossless
    Terrarium WebP tiles to `out_path`. Returns metadata dict."""
    if pack_key not in BUILDABLE_COG_PACKS:
        raise ValueError(f"Unknown terrain pack {pack_key!r}.")
    spec = BUILDABLE_COG_PACKS[pack_key]
    w, s, e, n = check_dem_bbox(bbox)
    report = progress or (lambda f: None)

    if "url" in spec:
        elev, valid, transform, crs = read_cog_window(spec["url"], [w, s, e, n], spec["scale_factor"])
    elif "url_template" in spec:
        tile_specs = []
        for lat_i in range(math.floor(s), math.ceil(n)):
            for lon_i in range(math.floor(w), math.ceil(e)):
                cb = [max(w, float(lon_i)), max(s, float(lat_i)), min(e, float(lon_i + 1)), min(n, float(lat_i + 1))]
                if cb[0] < cb[2] and cb[1] < cb[3]:
                    tile_specs.append((spec["url_template"].format(lat=abs(lat_i), lon=abs(lon_i)), cb))
        elev, valid, transform, crs = _mosaic_tiled_cog_windows(tile_specs, [w, s, e, n], spec["scale_factor"])
    else:
        tile_specs = []
        lat_start, lat_end = int(math.floor(s / 3.0) * 3), int(math.ceil(n / 3.0) * 3)
        lon_start, lon_end = int(math.floor(w / 3.0) * 3), int(math.ceil(e / 3.0) * 3)
        for lat3 in range(lat_start, lat_end, 3):
            for lon3 in range(lon_start, lon_end, 3):
                cb = [max(w, float(lon3)), max(s, float(lat3)), min(e, float(lon3 + 3)), min(n, float(lat3 + 3))]
                if cb[0] < cb[2] and cb[1] < cb[3]:
                    tile_specs.append((spec["url_template_3deg"].format(lat3=abs(lat3), lon3=abs(lon3)), cb))
        elev, valid, transform, crs = _mosaic_tiled_cog_windows(tile_specs, [w, s, e, n], spec["scale_factor"])

    if not valid.any():
        raise ValueError("No land elevation pixels were found in that area.")
    report(0.25)
    written, actual_bounds, max_z = tiling.write_dem_tiles(
        elev,
        crs,
        out_path,
        transform=transform,
        valid_mask=valid,
        max_zoom=spec["max_zoom"],
        name=spec["title"],
        meta={"series": spec["series"], "attribution": spec["attribution"]},
        progress=lambda f: report(0.25 + 0.75 * f),
    )
    return {
        "tiles": written,
        "bounds": actual_bounds,
        "maxzoom": max_z,
        "title": spec["title"],
        "series": spec["series"],
        "attribution": spec["attribution"],
        "licence_policy": spec["licence_policy"],
        "encoding": "terrarium",
    }


# ---- Point Elevation, Uncertainty & Line-of-Sight Transects -------------------------------------
def sample_cog_point(cog_url, lon, lat, scale_factor=1.0, *, raise_on_error=False):
    """Sample a single float value from `cog_url` at `(lon, lat)`, or return None."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with rasterio.open(cog_url) as ds:
                vals = list(ds.sample([(float(lon), float(lat))]))
                if not vals:
                    return None
                raw = float(vals[0][0])
                if ds.nodata is not None and np.isfinite(ds.nodata) and raw == float(ds.nodata):
                    return None
                val = raw * float(scale_factor)
                return round(val, 2) if (-12_000.0 < val < 12_000.0) else None
    except Exception:
        if raise_on_error:
            raise
        return None


def sample_point_terrain(
    lon, lat, *, dem_paths=(), hand_path=None, canopy_path=None, remote_fallback=False, cache_fn=None
):
    """Collect bare-earth elevation, uncertainty, Hòn Dấu MSL equivalent, HAND, canopy height, and
    NARA RG 373 aerial index metadata at WGS84 `(lon, lat)`."""
    lon, lat = float(lon), float(lat)
    ground_m = None
    ground_source = None
    uncertainty_m = None

    for source_label, p in dem_paths:
        val = tiling.sample_dem_mbtiles(p, lon, lat)
        if val is not None:
            ground_m = val
            ground_source = source_label
            break

    if ground_m is None and remote_fallback:
        # Round key to ~1 arc-second (0.0003°) so repeat clicks on the same 30 m pixel hit infinite cache.
        cell_key = f"gedtm30_pt:{round(lon, 4):.4f},{round(lat, 4):.4f}"
        if cache_fn is not None:
            try:
                cached = cache_fn(cell_key, lambda: _probe_gedtm30_remote(lon, lat, raise_on_error=True))
                ground_m, uncertainty_m = cached.get("ground_m"), cached.get("uncertainty_m")
            except Exception:
                ground_m, uncertainty_m = None, None
        else:
            res = _probe_gedtm30_remote(lon, lat, raise_on_error=False)
            ground_m, uncertainty_m = res.get("ground_m"), res.get("uncertainty_m")
        if ground_m is not None:
            ground_source = "GEDTM30 (30 m bare-earth COG)"

    hand_m = tiling.sample_dem_mbtiles(hand_path, lon, lat) if hand_path else None
    canopy_m = tiling.sample_dem_mbtiles(canopy_path, lon, lat) if canopy_path else None
    hon_dau_m = round(ground_m - datums.HON_DAU_OFFSET_M, 2) if ground_m is not None and ground_m < 25.0 else None

    return {
        "lon": lon,
        "lat": lat,
        "ground_m": ground_m,
        "uncertainty_m": uncertainty_m,
        "ground_source": ground_source,
        "hon_dau_m": hon_dau_m,
        "hand_m": hand_m,
        "canopy_m": canopy_m,
        "nara_rg373": nara_rg373_info(lon, lat),
    }


def _probe_gedtm30_remote(lon, lat, *, raise_on_error=False):
    elev = sample_cog_point(GEDTM30_ELEV_COG, lon, lat, scale_factor=0.1, raise_on_error=raise_on_error)
    std = (
        sample_cog_point(GEDTM30_STD_COG, lon, lat, scale_factor=0.1, raise_on_error=raise_on_error)
        if elev is not None
        else None
    )
    return {"ground_m": elev, "uncertainty_m": max(0.1, std) if std is not None else None}


def haversine_m(lon1, lat1, lon2, lat2):
    """Great-circle distance in metres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def compute_transect(
    start, end, *, samples=64, observer_height_m=1.7, dem_path=None, canopy_path=None, remote_cog=False
):
    """Compute an elevation and line-of-sight intervisibility profile from `start` to `end`.

    `start` and `end` are `[lon, lat]`. Each returned sample carries `distance_m`, `lon`, `lat`,
    `ground_m`, optional `canopy_m`, and `visible` (whether an observer `observer_height_m` above
    `start` has an unobstructed geometric sightline over bare-earth terrain, accounting for Earth
    curvature and standard refraction).
    """
    samples = max(4, min(256, int(samples)))
    lon1, lat1 = float(start[0]), float(start[1])
    lon2, lat2 = float(end[0]), float(end[1])
    total_m = haversine_m(lon1, lat1, lon2, lat2)

    lons = np.linspace(lon1, lon2, samples)
    lats = np.linspace(lat1, lat2, samples)
    elevs = [None] * samples
    canopies = [None] * samples

    if dem_path and Path(dem_path).is_file():
        for i in range(samples):
            elevs[i] = tiling.sample_dem_mbtiles(dem_path, float(lons[i]), float(lats[i]))
    elif remote_cog:
        w, e = min(lon1, lon2) - 0.002, max(lon1, lon2) + 0.002
        s, n = min(lat1, lat2) - 0.002, max(lat1, lat2) + 0.002
        if (e - w) <= MAX_DEM_BOX_DEG and (n - s) <= MAX_DEM_BOX_DEG:
            arr, mask, transform, _ = read_cog_window(GEDTM30_ELEV_COG, [w, s, e, n], scale_factor=0.1)
            inv = ~transform
            h, width = arr.shape
            for i in range(samples):
                col_f, row_f = inv * (float(lons[i]), float(lats[i]))
                ci, ri = int(round(col_f)), int(round(row_f))
                if 0 <= ri < h and 0 <= ci < width and mask[ri, ci]:
                    elevs[i] = round(float(arr[ri, ci]), 2)

    if canopy_path and Path(canopy_path).is_file():
        for i in range(samples):
            canopies[i] = tiling.sample_dem_mbtiles(canopy_path, float(lons[i]), float(lats[i]))

    # Line-of-sight horizon angle sweep from index 0.
    origin_h = (elevs[0] + float(observer_height_m)) if elevs[0] is not None else None
    max_slope = -math.inf
    points = []
    for i in range(samples):
        dist = total_m * (i / (samples - 1)) if samples > 1 else 0.0
        g = elevs[i]
        vis = None
        if origin_h is not None and g is not None:
            if i == 0 or dist < 1e-3:
                vis = True
            else:
                curvature_drop = (dist * dist) / (2.0 * EFFECTIVE_EARTH_RADIUS_M)
                effective_target_h = g - curvature_drop
                slope = (effective_target_h - origin_h) / dist
                vis = slope >= max_slope - 1e-9
                if slope > max_slope:
                    max_slope = slope
        points.append(
            {
                "distance_m": round(dist, 1),
                "lon": round(float(lons[i]), 6),
                "lat": round(float(lats[i]), 6),
                "ground_m": g,
                "canopy_m": canopies[i],
                "visible": vis,
            }
        )

    valid_g = [p["ground_m"] for p in points if p["ground_m"] is not None]
    return {
        "start": [lon1, lat1],
        "end": [lon2, lat2],
        "total_distance_m": round(total_m, 1),
        "observer_height_m": float(observer_height_m),
        "min_ground_m": min(valid_g) if valid_g else None,
        "max_ground_m": max(valid_g) if valid_g else None,
        "points": points,
    }


# ---- Spaceborne LiDAR (NASA GEDI & ICESat-2 via SlideRule Earth) --------------------------------
def query_sliderule_gedi(bbox, fetch_fn):
    """Query NASA GEDI L2A bare-earth and canopy footprints inside a small `bbox` (`<= 0.1°`) via SlideRule."""
    w, s, e, n = (round(float(v), 4) for v in bbox)
    if not (w < e and s < n) or (e - w) > 0.12 or (n - s) > 0.12:
        raise ValueError("Zoom in to an area under ~12 km across before querying individual LiDAR laser shots.")
    poly = [
        {"lon": w, "lat": s},
        {"lon": e, "lat": s},
        {"lon": e, "lat": n},
        {"lon": w, "lat": n},
        {"lon": w, "lat": s},
    ]
    payload = json.dumps({"parms": {"poly": poly, "degrade_filter": True, "l2_quality_filter": True}}).encode("utf-8")
    raw, _ = fetch_fn(
        "https://slideruleearth.io/source/gedi02ap",
        cache_key=f"sliderule_gedi02a:{w},{s},{e},{n}",
        headers={"Content-Type": "application/json"},
        data=payload,
        method="POST",
    )
    features = []
    # SlideRule streaming/JSON responses may be NDJSON or a JSON array depending on endpoint gateway.
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line[0] not in "{[":
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        rows = parsed if isinstance(parsed, list) else parsed.get("records", [parsed])
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            lon = rec.get("longitude") if rec.get("longitude") is not None else rec.get("lon")
            lat = rec.get("latitude") if rec.get("latitude") is not None else rec.get("lat")
            elev = rec.get("elev_lowestmode") if rec.get("elev_lowestmode") is not None else rec.get("elevation_lm")
            if lon is None or lat is None or elev is None:
                continue
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]},
                    "properties": {
                        "ground_m": round(float(elev), 2),
                        "canopy_rh98_m": round(float(rec["rh98"]), 2) if rec.get("rh98") is not None else None,
                        "sensitivity": round(float(rec["sensitivity"]), 3)
                        if rec.get("sensitivity") is not None
                        else None,
                        "beam": rec.get("beam"),
                        "source": "NASA GEDI L2A (SlideRule)",
                    },
                }
            )
            if len(features) >= 500:
                break
    return {"type": "FeatureCollection", "features": features}


# ---- Historical Overhead & Satellite Catalogs ---------------------------------------------------
_FIRST_INDOCHINA_PLACES = {
    "dien bien phu": ([102.92, 21.25, 103.18, 21.52], "Điện Biên Phủ"),
    "na san": ([103.85, 21.05, 104.22, 21.35], "Nà Sản (Sơn La)"),
    "cao bang": ([106.10, 22.50, 106.45, 22.80], "Cao Bằng"),
    "dong khe": ([106.30, 22.30, 106.60, 22.55], "Đông Khê (RC 4)"),
    "that khe": ([106.35, 22.15, 106.62, 22.40], "Thất Khê (RC 4)"),
    "lang son": ([106.62, 21.72, 106.90, 21.98], "Lạng Sơn"),
    "hoa binh": ([105.20, 20.68, 105.50, 20.96], "Hòa Bình"),
    "ha noi": ([105.72, 20.92, 105.98, 21.14], "Hà Nội"),
    "hanoi": ([105.72, 20.92, 105.98, 21.14], "Hà Nội"),
    "hai phong": ([106.56, 20.74, 106.82, 20.96], "Hải Phòng"),
    "haiphong": ([106.56, 20.74, 106.82, 20.96], "Hải Phòng"),
    "nam dinh": ([106.05, 20.30, 106.30, 20.54], "Nam Định"),
    "thai binh": ([106.22, 20.32, 106.48, 20.58], "Thái Bình"),
    "ninh binh": ([105.84, 20.14, 106.10, 20.38], "Ninh Bình"),
    "phat diem": ([105.98, 20.02, 106.32, 20.28], "Phát Diệm / Bùi Chu"),
    "bui chu": ([105.98, 20.02, 106.32, 20.28], "Phát Diệm / Bùi Chu"),
    "phu ly": ([105.80, 20.44, 106.05, 20.66], "Phủ Lý"),
    "hai duong": ([106.20, 20.82, 106.46, 21.06], "Hải Dương"),
    "son tay": ([105.38, 21.02, 105.64, 21.26], "Sơn Tây"),
    "vinh yen": ([105.48, 21.20, 105.74, 21.44], "Vĩnh Yên"),
    "bac ninh": ([105.96, 21.08, 106.20, 21.30], "Bắc Ninh"),
    "viet tri": ([105.30, 21.22, 105.54, 21.44], "Việt Trì"),
    "tuyen quang": ([105.12, 21.70, 105.36, 21.94], "Tuyên Quang"),
    "yen bai": ([104.76, 21.60, 105.02, 21.84], "Yên Bái"),
    "lao cai": ([103.86, 22.38, 104.12, 22.62], "Lào Cai"),
    "hue": ([107.46, 16.36, 107.72, 16.58], "Huế"),
    "tourane": ([108.08, 15.96, 108.34, 16.18], "Đà Nẵng (Tourane)"),
    "da nang": ([108.08, 15.96, 108.34, 16.18], "Đà Nẵng (Tourane)"),
    "quang tri": ([107.06, 16.64, 107.32, 16.88], "Quảng Trị"),
    "dong ha": ([106.96, 16.72, 107.22, 16.94], "Đông Hà"),
    "dong hoi": ([106.48, 17.36, 106.74, 17.60], "Đồng Hới"),
    "vinh": ([105.56, 18.56, 105.82, 18.80], "Vinh"),
    "thanh hoa": ([105.66, 19.68, 105.92, 19.92], "Thanh Hóa"),
    "tho xuan": ([105.35, 19.82, 105.60, 20.02], "Thọ Xuân"),
    "qui nhon": ([109.10, 13.66, 109.34, 13.90], "Quy Nhơn"),
    "quy nhon": ([109.10, 13.66, 109.34, 13.90], "Quy Nhơn"),
    "nha trang": ([109.08, 12.14, 109.32, 12.36], "Nha Trang"),
    "da lat": ([108.32, 11.82, 108.58, 12.06], "Đà Lạt"),
    "dalat": ([108.32, 11.82, 108.58, 12.06], "Đà Lạt"),
    "pleiku": ([107.88, 13.86, 108.14, 14.12], "Pleiku"),
    "kontum": ([107.88, 14.24, 108.14, 14.48], "Kon Tum"),
    "kon tum": ([107.88, 14.24, 108.14, 14.48], "Kon Tum"),
    "ankhe": ([108.54, 13.84, 108.80, 14.08], "An Khê"),
    "an khe": ([108.54, 13.84, 108.80, 14.08], "An Khê"),
    "ban me thuot": ([107.92, 12.56, 108.18, 12.80], "Buôn Ma Thuột"),
    "buon ma thuot": ([107.92, 12.56, 108.18, 12.80], "Buôn Ma Thuột"),
    "saigon": ([106.54, 10.66, 106.82, 10.90], "Sài Gòn"),
    "ho chi minh": ([106.54, 10.66, 106.82, 10.90], "Sài Gòn"),
    "cho lon": ([106.54, 10.66, 106.82, 10.90], "Sài Gòn"),
    "bien hoa": ([106.72, 10.86, 106.98, 11.08], "Biên Hòa"),
    "tay ninh": ([106.00, 11.20, 106.26, 11.44], "Tây Ninh"),
    "vung tau": ([107.00, 10.28, 107.22, 10.50], "Vũng Tàu"),
    "cap saint jacques": ([107.00, 10.28, 107.22, 10.50], "Vũng Tàu (Cap Saint-Jacques)"),
    "my tho": ([106.24, 10.26, 106.48, 10.48], "Mỹ Tho"),
    "ben tre": ([106.26, 10.12, 106.50, 10.34], "Bến Tre"),
    "can tho": ([105.66, 9.94, 105.90, 10.16], "Cần Thơ"),
    "soc trang": ([105.86, 9.50, 106.10, 9.72], "Sóc Trăng"),
    "rach gia": ([104.98, 9.90, 105.22, 10.12], "Rạch Giá"),
    "ca mau": ([105.04, 9.06, 105.28, 9.28], "Cà Mau"),
    "long xuyen": ([105.32, 10.26, 105.56, 10.50], "Long Xuyên"),
    "chau doc": ([105.00, 10.60, 105.24, 10.82], "Châu Đốc"),
    "cao lanh": ([105.52, 10.36, 105.74, 10.56], "Cao Lãnh"),
}
_SHDAI_REGIONS = (
    (
        re.compile(
            r"\b(?:tonkin|fleuve\s+rouge|delta\s+du\s+fleuve|riviere\s+noire|riviere\s+claire|rc\s*[46]|hv\d{2,4})\b"
        ),
        [104.80, 19.90, 107.10, 22.10],
        "Tonkin / Red River Delta",
    ),
    (
        re.compile(r"\b(?:annam|centre\s+vietnam|demilitarized)\b"),
        [106.40, 14.50, 109.40, 18.00],
        "Annam / Central Vietnam",
    ),
    (
        re.compile(r"\b(?:cochinchine|mekong|plaine\s+des\s+joncs)\b"),
        [104.80, 9.00, 107.30, 11.40],
        "Cochinchina / Mekong Delta",
    ),
)
_SGI_INDEX_CACHE = {}
# The SGI 1:100,000 series is a regular grid: 0.8 grad (0.7262 deg as placed) wide, 0.5 grad (0.4505 deg) tall,
# numbered west to east along each row and row by row southwards, each row starting where Indochina does.
# Columns and rows below are measured from the placed catalogue sheets (scripts/place_sheets.py).
SGI_LON0, SGI_COL_W, SGI_NORTH0, SGI_ROW_H = 101.484, 0.7262, 23.422, 0.4505
_SHEET_SUFFIXES = ("", "E", "W", "EW")


def _fold_place(text):
    s = unicodedata.normalize("NFD", str(text or "")).replace("đ", "d").replace("Đ", "D")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _sgi_vicinity_index(catalogue_path=None):
    """(sheets_by_num, sheet_places, grid) from the catalogue's `sgi100k` sheets.

    `grid` (see `_sgi_row_model`) places sheets the catalogue lacks (`sgi_sheet_bounds`).
    """
    cat_file = Path(catalogue_path) if catalogue_path else (paths.APP_ROOT / "data" / "sources" / "map_library.json")
    key = str(cat_file)
    if key in _SGI_INDEX_CACHE:
        return _SGI_INDEX_CACHE[key]
    sheets_by_num = {}
    sheet_places = []
    offsets = {}
    if cat_file.is_file():
        data = json.loads(cat_file.read_text(encoding="utf-8"))
        for entry in data.get("maps", []):
            if entry.get("series") != "sgi100k":
                continue
            b = entry.get("bounds")
            if not (isinstance(b, list) and len(b) == 4):
                continue
            title = str(entry.get("title") or "")
            # "feuille 9A" (Hanoi) is a different sheet from "feuille 9" (Cao Bang): only plain numbers and their
            # east / west halves count as sheet N.
            m = re.search(r"feuille\s+(\d+)\s*([A-Za-z]*)\)", title, re.I)
            if m and m.group(2).upper() in _SHEET_SUFFIXES:
                num = int(m.group(1))
                cur = sheets_by_num.get(num)
                if cur is None:
                    sheets_by_num[num] = {"bounds": list(b), "label": title, "ids": [entry["id"]]}
                else:
                    cb = cur["bounds"]
                    cur["bounds"] = [min(cb[0], b[0]), min(cb[1], b[1]), max(cb[2], b[2]), max(cb[3], b[3])]
                    cur["ids"].append(entry["id"])
                if b[3] - b[1] > 0.3:  # full-height sheets only: coastal fragments would skew the rows
                    # Half sheets (a few are titled without their E / W) sit half a column east or on the line.
                    half = 0.25 if b[2] - b[0] < 0.5 else 0.0
                    col_f = (b[0] - SGI_LON0) / SGI_COL_W - half
                    col = round(col_f)
                    row = round((SGI_NORTH0 - b[3]) / SGI_ROW_H)
                    east_half = half and col_f - col > 0.1
                    offsets.setdefault(row, []).append((num - col, num, b[0], bool(east_half)))
            clean_name = re.sub(r"\([^)]*\)", "", title)
            folded = _fold_place(clean_name)
            for suf in (" est", " ouest", " nord", " sud"):
                if folded.endswith(suf):
                    folded = folded[: -len(suf)].strip()
            if len(folded) >= 5:
                sheet_places.append((folded, list(b), clean_name.strip() or title, entry["id"]))
    row_offsets = _sgi_row_model(offsets)
    _SGI_INDEX_CACHE[key] = (sheets_by_num, sheet_places, row_offsets)
    return _SGI_INDEX_CACHE[key]


def _sgi_row_model(samples):
    """{"width": line coefficients of column width by row, "cols": {sheet number: (row, column west)}}.

    Sheets are a fixed angle wide on the old projection, which comes out a little narrower in longitude in the
    south (0.68 deg) than in the north (0.73 deg); the width is fitted once across well-sampled rows.
    """
    rows = {}
    for row, items in samples.items():
        pts = {}
        for _o, num, west, east_half in items:
            pts.setdefault(num, []).append((west, east_half))
        # West edge of each numbered column (an east half starts half a column in), whole columns only for fitting.
        wests = {n: statistics.median(w for w, e in v if not e) for n, v in pts.items() if any(not e for _w, e in v)}
        rows[row] = {"pts": pts, "wests": wests}

    def robust_slope(wests):
        """Median of pairwise slopes (Theil-Sen): one mislabelled sheet cannot tilt the row."""
        nums = sorted(wests)
        slopes = [(wests[b] - wests[a]) / (b - a) for i, a in enumerate(nums) for b in nums[i + 1 :] if b - a >= 1]
        slopes = [s for s in slopes if 0.6 < s < 0.8]
        return statistics.median(slopes) if len(slopes) >= 2 else None

    # Width varies smoothly with latitude, so fit it once across all well-sampled rows (>= 3 columns) and
    # read each row's width off that line instead of trusting sparse rows.
    fit = [(row, robust_slope(r["wests"])) for row, r in rows.items() if len(r["wests"]) >= 3]
    fit = [(row, s) for row, s in fit if s is not None]
    if len(fit) >= 3:
        xs = np.array([f[0] for f in fit], dtype=float)
        ys = np.array([f[1] for f in fit])
        coef = np.polyfit(xs, ys, 1)
        keep = np.abs(np.polyval(coef, xs) - ys) < 0.02
        if keep.sum() >= 3:
            coef = np.polyfit(xs[keep], ys[keep], 1)
    else:
        coef = np.array([0.0, SGI_COL_W])
    cols = {}
    for row, r in rows.items():
        w = _sgi_width(coef, row)
        for n, v in r["pts"].items():
            cols[n] = (row, float(statistics.median(west - (w / 2 if e else 0.0) for west, e in v)))
    return {"width": [float(c) for c in coef], "cols": cols}


def _sgi_width(coef, row):
    """Column width (degrees of longitude) of SGI row `row` from the fitted width line, clamped to sane values."""
    return float(min(0.76, max(0.66, np.polyval(coef, row))))


def _sgi_infer(num, grid, exclude=None):
    """(row, west) of sheet `num` stepped from its nearest numbered neighbour, or None when unsure.

    Numbering runs west to east along a row and jumps at sea or borders, so a sheet is stepped from the closest
    known number rather than from a row start. When the neighbours sit on different rows, the sheet is stepped
    from each and kept only if exactly one lands in Indochina without covering a known sheet (sheet 80: from 76
    it lands south-west of Thanh Hoa, from 86 it would be in Burma).
    """
    cols = {n: v for n, v in grid["cols"].items() if n != exclude}
    lower = max((n for n in cols if n < num), default=None)
    higher = min((n for n in cols if n > num), default=None)
    if lower is None or higher is None:
        return None

    def step_from(base):
        row, west0 = cols[base]
        w = _sgi_width(grid["width"], row)
        west = west0 + (num - base) * w
        if not 100.5 <= west <= 109.6 or any(r == row and abs(x - west) < 0.5 * w for r, x in cols.values()):
            return None
        return row, west

    if cols[lower][0] == cols[higher][0]:
        return step_from(lower if num - lower <= higher - num else higher)
    found = [f for f in (step_from(lower), step_from(higher)) if f is not None]
    return found[0] if len(found) == 1 else None


def sgi_sheet_bounds(num, *, catalogue_path=None):
    """[w, s, e, n] of SGI 1:100,000 sheet `num`: the catalogue's own, or worked out from the sheet grid.

    Returns (bounds, inferred) or (None, False).
    """
    sheets_by_num, _, grid = _sgi_vicinity_index(catalogue_path)
    if num in sheets_by_num:
        return sheets_by_num[num]["bounds"], False
    found = _sgi_infer(num, grid)
    if found is None:
        return None, False
    row, west = found
    width = _sgi_width(grid["width"], row)
    north = SGI_NORTH0 - row * SGI_ROW_H
    return [round(west, 4), round(north - SGI_ROW_H, 4), round(west + width, 4), round(north, 4)], True


def builtin_places(query, limit=8):
    """Places the app knows without any downloaded records: wartime towns and the SGI sheet names.

    Each is {"title", "lon", "lat", "bounds", "source"}; matched on accent-folded words, so "tho xuan" or
    "dien bien" work on a new profile."""
    fq = _fold_place(query)
    if len(fq) < 3:
        return []
    out, seen = [], set()
    for name, (bounds, label) in _FIRST_INDOCHINA_PLACES.items():
        if fq in name and label not in seen:
            seen.add(label)
            out.append({"title": label, "bounds": bounds, "source": "town"})
    _, sheet_places, _ = _sgi_vicinity_index()
    for pname, bounds, label, _sid in sheet_places:
        if fq in pname and label not in seen:
            seen.add(label)
            out.append({"title": f"{label} (French 1:100,000 sheet)", "bounds": bounds, "source": "sgi100k"})
    for p in out:
        w, s, e, n = p["bounds"]
        p["lon"], p["lat"] = round((w + e) / 2, 5), round((s + n) / 2, 5)
    out.sort(key=lambda p: (not _fold_place(p["title"]).startswith(fq), len(p["title"])))
    return out[:limit]


_SHEET_REF_RE = re.compile(
    r"\b(?:feuilles?|carte|sgi|sheet)\s*(?:n\s*[°ºo]?\.?\s*)?[:#]?\s*(\d{1,3})(?:\s*(?:bis|[EW]{1,2}))?\b", re.I
)


def resolve_shdai_vicinity(title, description="", *, catalogue_path=None):
    """Best-effort geographic vicinity resolver for un-georeferenced NAKALA `SHDAI` missions."""
    combined = f"{title or ''} {description or ''}"
    sheets_by_num, sheet_places, _ = _sgi_vicinity_index(catalogue_path)

    # 1. Explicit SGI 1:100k sheet references ("feuille 80", "Feuille N° 80 EW", "carte 71"), including sheets
    #    the catalogue lacks, placed from the sheet grid.
    found = []
    for m in _SHEET_REF_RE.finditer(combined):
        num = int(m.group(1))
        bounds, inferred = sgi_sheet_bounds(num, catalogue_path=catalogue_path)
        if bounds:
            found.append((num, bounds, inferred))
    if found:
        bs = [b for _, b, _ in found]
        merged = [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)]
        labels, ids = [], []
        for num, _, inferred in dict((f[0], f) for f in found).values():
            if inferred:
                labels.append(f"SGI sheet {num} (from the sheet grid)")
            else:
                labels.append(sheets_by_num[num]["label"])
                ids += sheets_by_num[num]["ids"]
        return {
            "bounds": [round(v, 4) for v in merged],
            "label": ", ".join(labels),
            "source": "sgi100k_sheet",
            "sheet_ids": ids,
        }

    # 2. Boundary-matched town / province placenames
    folded = f" {_fold_place(combined)} "
    for name in sorted(_FIRST_INDOCHINA_PLACES, key=len, reverse=True):
        if f" {name} " in folded:
            bounds, label = _FIRST_INDOCHINA_PLACES[name]
            return {"bounds": bounds, "label": label, "source": "placename", "sheet_ids": []}
    for pname, bounds, label, sid in sorted(sheet_places, key=lambda x: len(x[0]), reverse=True):
        if f" {pname} " in folded:
            return {"bounds": bounds, "label": label, "source": "placename", "sheet_ids": [sid]}

    # 3. Regional / operational mission-code fallback (e.g. Tonkin / HV-series / Cochinchina)
    for pat, bounds, label in _SHDAI_REGIONS:
        if pat.search(folded):
            return {"bounds": bounds, "label": label, "source": "region", "sheet_ids": []}
    return None


def _nakala_meta_values(item, prop_name):
    out = []
    for m in item.get("metas", []):
        uri = str(m.get("propertyUri") or "")
        key = uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        if key == prop_name and m.get("value") is not None:
            out.append(str(m["value"]))
    return out


def search_nakala_aerials(query="", page=1, size=15, *, fetch_fn):
    """Search NAKALA's digitized French SHD Indochina aerial photography missions (`SHDAI`)."""
    page = max(1, int(page))
    size = max(1, min(25, int(size)))
    terms = f"SHDAI {query}".strip() if query else "SHDAI"
    params = urllib.parse.urlencode({"q": terms, "page": page, "size": size})
    url = f"https://api.nakala.fr/search?{params}"
    raw, _ = fetch_fn(url, cache_key=f"nakala_shdai:{terms}:p{page}:s{size}", headers={"Accept": "application/json"})
    data = json.loads(raw.decode("utf-8"))
    missions = []
    for item in data.get("datas", []):
        doi = item.get("identifier") or ""
        titles = _nakala_meta_values(item, "title")
        dates = _nakala_meta_values(item, "created")
        creators = _nakala_meta_values(item, "creator")
        descs = _nakala_meta_values(item, "description")
        licences = _nakala_meta_values(item, "license")
        files = item.get("files") or []
        plots = []
        frames = []
        for f in files:
            fname = str(f.get("name") or "")
            sha1 = f.get("sha1") or ""
            if not sha1 or not fname.lower().endswith((".jpg", ".jpeg", ".tif", ".tiff", ".png")):
                continue
            size = int(f.get("size") or 0)
            full_url = f"https://api.nakala.fr/data/{doi}/{sha1}"
            is_plot = "__PLOT" in fname.upper() or "__TAPE" in fname.upper()
            frame_no = re.search(r"__(\d{3,5})__", fname)
            entry = {
                "name": fname,
                "label": (
                    ("Flight index" if "__PLOT" in fname.upper() else "Film label")
                    if is_plot
                    else (f"Frame {int(frame_no.group(1))}" if frame_no else fname.rsplit(".", 1)[0][-16:])
                ),
                "sha1": sha1,
                "bytes": size,
                # NAKALA's IIIF server fails (HTTP 500) on the flight-index cards; they are small JPEGs, so the
                # card itself is the preview.
                "preview_url": (
                    full_url
                    if is_plot and 0 < size <= 4_000_000
                    else f"https://api.nakala.fr/iiif/{doi}/{sha1}/full/,900/0/default.jpg"
                ),
                "full_url": full_url,
            }
            if is_plot:
                plots.append(entry)
            else:
                frames.append(entry)
        frames.sort(key=lambda e: e["name"])
        title = titles[0] if titles else doi
        desc = descs[-1] if descs else ""
        vicinity = resolve_shdai_vicinity(title, desc)
        missions.append(
            {
                "doi": doi,
                "title": title,
                "date": dates[0] if dates else "",
                "squadron": creators[0] if creators else "Armée de l'Air / SHD",
                "description": desc,
                "licence": licences[0] if licences else (item.get("license") or "CC-BY-NC-SA-4.0"),
                "source_url": f"https://nakala.fr/{doi}",
                "frame_count": len(frames),
                "vicinity": vicinity,
                "bounds": vicinity["bounds"] if vicinity else None,
                "plots": plots,
                "sample_frames": frames[:24],
                "frames": frames,
            }
        )
    return {"total": data.get("totalResults", len(missions)), "page": page, "missions": missions}


def search_openaerialmap(bbox, limit=12, *, fetch_fn):
    """Search OpenAerialMap's STAC catalog (`CC BY 4.0`) for UAV/drone orthomosaics overlapping `bbox`."""
    w, s, e, n = (round(float(v), 4) for v in bbox)
    limit = max(1, min(30, int(limit)))
    body = json.dumps({"collections": ["openaerialmap"], "bbox": [w, s, e, n], "limit": limit}).encode("utf-8")
    raw, _ = fetch_fn(
        "https://api.imagery.hotosm.org/stac/search",
        cache_key=f"oam_stac:{w},{s},{e},{n}:{limit}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=body,
        method="POST",
    )
    parsed = json.loads(raw.decode("utf-8"))
    items = []
    for ft in parsed.get("features", []):
        iid = ft.get("id") or ""
        props = ft.get("properties") or {}
        assets = ft.get("assets") or {}
        visual = (assets.get("visual") or assets.get("image") or {}).get("href") or ""
        thumb = (assets.get("thumbnail") or {}).get("href") or ""
        items.append(
            {
                "id": iid,
                "title": props.get("title") or f"UAV Orthomosaic {iid[:8]}",
                "datetime": props.get("datetime") or props.get("start_datetime") or "",
                "gsd_m": round(float(props["gsd"]), 4) if props.get("gsd") else None,
                "platform": props.get("platform") or "UAV",
                "bbox": ft.get("bbox"),
                "cog_url": visual,
                "thumbnail_url": thumb,
                "xyz_tiles": (
                    f"https://api.imagery.hotosm.org/raster/collections/openaerialmap/items/"
                    f"{urllib.parse.quote(iid)}/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}?assets=visual"
                ),
                "licence": "CC BY 4.0 (OpenAerialMap / Open Imagery Network)",
            }
        )
    return {"items": items}


def search_landsat_mss(bbox, date_from="1972-07-01", date_to="1975-12-31", max_cloud=35, limit=12, *, fetch_fn):
    """Search wartime/early post-war Landsat 1-3 MSS scenes (`1972-1983`) via Planetary Computer STAC."""
    w, s, e, n = (round(float(v), 3) for v in bbox)
    limit = max(1, min(25, int(limit)))
    clean_from = re.sub(r"[^0-9-]", "", str(date_from or "1972-07-01"))[:10]
    clean_to = re.sub(r"[^0-9-]", "", str(date_to or "1975-12-31"))[:10]
    body = json.dumps(
        {
            "collections": ["landsat-c2-l1"],
            "bbox": [w, s, e, n],
            "datetime": f"{clean_from}T00:00:00Z/{clean_to}T23:59:59Z",
            "limit": limit,
        }
    ).encode("utf-8")
    raw, _ = fetch_fn(
        "https://planetarycomputer.microsoft.com/api/stac/v1/search",
        cache_key=f"landsat_mss:{w},{s},{e},{n}:{clean_from}:{clean_to}:{limit}",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=body,
        method="POST",
    )
    parsed = json.loads(raw.decode("utf-8"))
    scenes = []
    for ft in parsed.get("features", []):
        props = ft.get("properties") or {}
        cloud = props.get("eo:cloud_cover")
        if cloud is not None and float(cloud) > float(max_cloud):
            continue
        assets = ft.get("assets") or {}
        preview_url = (assets.get("rendered_preview") or assets.get("thumbnail") or {}).get("href") or ""
        # Planetary Computer's TiTiler serves the same false-colour composite as a real UTM-georeferenced
        # RGBA GeoTIFF (`/preview.tif`) so Map Vietnam can reproject it accurately instead of hand-placing a PNG.
        geotiff_url = ""
        if "/item/preview.png?" in preview_url:
            geotiff_url = preview_url.replace("/item/preview.png?", "/item/preview.tif?", 1) + "&max_size=2048"
        scene_id = str(ft.get("id") or "")
        level = str(props.get("landsat:correction") or "")
        if not level:
            m_lvl = re.search(r"_(L1[A-Z]{2})_", scene_id)
            level = m_lvl.group(1) if m_lvl else ""
        scenes.append(
            {
                "id": scene_id,
                "datetime": props.get("datetime") or "",
                "platform": props.get("platform") or "landsat-mss",
                "processing_level": level,
                "cloud_cover": cloud,
                "bbox": ft.get("bbox"),
                "preview_url": preview_url,
                "geotiff_url": geotiff_url,
                "bands": {
                    k: (assets.get(k) or {}).get("href")
                    for k in ("green", "red", "nir08", "nir09")
                    if (assets.get(k) or {}).get("href")
                },
                "licence": "Public domain (USGS / NASA Landsat Collection 2)",
            }
        )
    # Prefer L1TP (precision terrain-corrected) scenes over L1GS (systematic/ephemeris-only), then least cloud.
    scenes.sort(
        key=lambda s: (
            0 if s["processing_level"] == "L1TP" else 1,
            s["cloud_cover"] if s["cloud_cover"] is not None else 999,
        )
    )
    return {"scenes": scenes}


def search_allmaps_iiif(bbox, limit=15, *, fetch_fn):
    """Find community-georeferenced IIIF historical maps (Gallica, Rumsey, LoC, Gaihozu) inside `bbox`."""
    w, s, e, n = (round(float(v), 3) for v in bbox)
    limit = max(1, min(30, int(limit)))
    url = f"https://annotations.allmaps.org/maps?bbox={w},{s},{e},{n}&limit={limit}"
    raw, _ = fetch_fn(url, cache_key=f"allmaps:{w},{s},{e},{n}:{limit}", headers={"Accept": "application/json"})
    parsed = json.loads(raw.decode("utf-8"))
    items = parsed if isinstance(parsed, list) else parsed.get("items", [])
    maps = []
    for it in items:
        if not isinstance(it, dict):
            continue
        res = it.get("resource") or {}
        iiif_id = str(res.get("id") or "").strip()
        if not iiif_id.startswith("https://"):
            continue
        raw_w, raw_h = res.get("width"), res.get("height")
        width = int(raw_w) if isinstance(raw_w, (int, float)) and raw_w > 0 else None
        height = int(raw_h) if isinstance(raw_h, (int, float)) and raw_h > 0 else None
        gcps = []
        for g in it.get("gcps") or []:
            px = g.get("resource") or g.get("image")
            geo = g.get("geo")
            if isinstance(px, list) and isinstance(geo, list) and len(px) >= 2 and len(geo) >= 2:
                gcps.append([float(px[0]), float(px[1]), float(geo[0]), float(geo[1])])
        if len(gcps) >= 3:
            maps.append(
                {
                    "id": str(it.get("id") or ""),
                    "iiif_service": iiif_id,
                    "preview_url": f"{iiif_id.rstrip('/')}/full/,900/0/default.jpg",
                    "full_url": f"{iiif_id.rstrip('/')}/full/max/0/default.jpg",
                    "width": width,
                    "height": height,
                    "gcp_count": len(gcps),
                    "gcps": gcps,
                    "licence": "IIIF source archive + CC0 Allmaps control points",
                }
            )
    return {"maps": maps}


USGS_DATASETS = {
    "corona": ("corona2", "Declass 1: CORONA / ARGON / LANYARD (1960–1972, ~1.8 m)"),
    "gambit_kh9mc": ("declassii", "Declass 2: GAMBIT KH-7 (0.6 m) & HEXAGON KH-9 Mapping (6–9 m)"),
    "hexagon_kh9pc": ("declassiii", "Declass 3: HEXAGON KH-9 Panoramic (1971–1984, 0.6 m)"),
}


def search_usgs_declass(
    bbox, dataset_key="corona", date_from="1960-01-01", date_to="1984-12-31", *, username, token, fetch_fn
):
    """Search USGS M2M API for declassified reconnaissance satellite scenes over `bbox`.

    Never orders paid NARA scans: each scene carries `download_available` so the viewer clearly
    distinguishes free pre-scanned downloads from browse-only scenes.
    """
    if not (username and token):
        return {
            "needs_credentials": True,
            "message": (
                "USGS EarthExplorer M2M credentials are not configured yet. Add them once on the Hub "
                "(USGS_USERNAME and USGS_M2M_TOKEN) or in local .env to search CORONA, KH-7 and KH-9 scenes."
            ),
            "scenes": [],
        }
    ds_name, ds_label = USGS_DATASETS.get(dataset_key, USGS_DATASETS["corona"])
    w, s, e, n = (round(float(v), 3) for v in bbox)
    base = "https://m2m.cr.usgs.gov/api/api/json/stable"

    # Login tokens are ephemeral session keys, so login itself bypasses the permanent raw cache.
    import urllib.request

    from hub.polite import tls_context

    tls = tls_context()
    login_req = urllib.request.Request(
        f"{base}/login-token",
        data=json.dumps({"username": username, "token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MapVietnam-M2M/0.3"},
        method="POST",
    )
    with urllib.request.urlopen(login_req, timeout=20, context=tls) as resp:
        login_data = json.loads(resp.read().decode("utf-8"))
    api_key = login_data.get("data")
    if not api_key:
        raise ValueError(login_data.get("errorMessage") or "USGS M2M login was rejected.")

    try:
        search_payload = json.dumps(
            {
                "datasetName": ds_name,
                "maxResults": 25,
                "sceneFilter": {
                    "spatialFilter": {
                        "filterType": "mbr",
                        "lowerLeft": {"latitude": s, "longitude": w},
                        "upperRight": {"latitude": n, "longitude": e},
                    },
                    "acquisitionFilter": {"start": str(date_from)[:10], "end": str(date_to)[:10]},
                },
            }
        ).encode("utf-8")
        raw, _ = fetch_fn(
            f"{base}/scene-search",
            cache_key=f"usgs_m2m:{ds_name}:{w},{s},{e},{n}:{str(date_from)[:10]}:{str(date_to)[:10]}",
            headers={"Content-Type": "application/json", "X-Auth-Token": api_key},
            data=search_payload,
            method="POST",
        )
        res = json.loads(raw.decode("utf-8")).get("data") or {}
        scenes = []
        for r in res.get("results") or []:
            browse = r.get("browse") or []
            thumb = browse[0].get("browsePath") if browse and isinstance(browse[0], dict) else ""
            temporal = r.get("temporalCoverage") or {}
            spatial = (r.get("spatialBounds") or {}).get("coordinates")
            opts = r.get("options") or {}
            scenes.append(
                {
                    "entity_id": r.get("entityId") or "",
                    "display_id": r.get("displayId") or r.get("entityId") or "",
                    "dataset": ds_name,
                    "dataset_label": ds_label,
                    "date": (temporal.get("startDate") or "")[:10],
                    "browse_url": thumb or "",
                    "download_available": bool(opts.get("bulk") or opts.get("standard")),
                    "footprint": spatial,
                    "licence": "Public domain (US Government declassified intelligence imagery, USGS/NARA)",
                }
            )
        return {"needs_credentials": False, "dataset": ds_name, "dataset_label": ds_label, "scenes": scenes}
    finally:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                logout = urllib.request.Request(f"{base}/logout", headers={"X-Auth-Token": api_key}, method="POST")
                urllib.request.urlopen(logout, timeout=5, context=tls).close()
            except Exception:
                pass
