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
import urllib.parse
import warnings
from pathlib import Path

import datums
import numpy as np
import rasterio
import tiling
from rasterio.enums import Resampling
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
        lat_i = math.floor((s + n) / 2.0)
        lon_i = math.floor((w + e) / 2.0)
        cb = [max(w, float(lon_i)), max(s, float(lat_i)), min(e, float(lon_i + 1)), min(n, float(lat_i + 1))]
        if not (cb[0] < cb[2] and cb[1] < cb[3]):
            cb = [w, s, e, n]
        url = spec["url_template"].format(lat=abs(lat_i), lon=abs(lon_i))
        elev, valid, transform, crs = read_cog_window(url, cb, spec["scale_factor"])
    else:
        lat3 = int(math.floor(((s + n) / 2.0) / 3.0) * 3)
        lon3 = int(math.floor(((w + e) / 2.0) / 3.0) * 3)
        cb = [max(w, float(lon3)), max(s, float(lat3)), min(e, float(lon3 + 3)), min(n, float(lat3 + 3))]
        if not (cb[0] < cb[2] and cb[1] < cb[3]):
            cb = [w, s, e, n]
        url = spec["url_template_3deg"].format(lat3=abs(lat3), lon3=abs(lon3))
        elev, valid, transform, crs = read_cog_window(url, cb, spec["scale_factor"])

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
def sample_cog_point(cog_url, lon, lat, scale_factor=1.0):
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
            cached = cache_fn(cell_key, lambda: _probe_gedtm30_remote(lon, lat))
            ground_m, uncertainty_m = cached.get("ground_m"), cached.get("uncertainty_m")
        else:
            res = _probe_gedtm30_remote(lon, lat)
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


def _probe_gedtm30_remote(lon, lat):
    elev = sample_cog_point(GEDTM30_ELEV_COG, lon, lat, scale_factor=0.1)
    std = sample_cog_point(GEDTM30_STD_COG, lon, lat, scale_factor=0.1) if elev is not None else None
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
            lon = rec.get("longitude") or rec.get("lon")
            lat = rec.get("latitude") or rec.get("lat")
            elev = rec.get("elev_lowestmode") or rec.get("elevation_lm")
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
            entry = {
                "name": fname,
                "sha1": sha1,
                "bytes": f.get("size") or 0,
                "preview_url": f"https://api.nakala.fr/iiif/{doi}/{sha1}/full/,900/0/default.jpg",
                "full_url": f"https://api.nakala.fr/data/{doi}/{sha1}",
                "iiif_info": f"https://api.nakala.fr/iiif/{doi}/{sha1}/info.json",
            }
            if "__PLOT" in fname.upper() or "__TAPE" in fname.upper():
                plots.append(entry)
            else:
                frames.append(entry)
        missions.append(
            {
                "doi": doi,
                "title": titles[0] if titles else doi,
                "date": dates[0] if dates else "",
                "squadron": creators[0] if creators else "Armée de l'Air / SHD",
                "description": descs[-1] if descs else "",
                "licence": licences[0] if licences else (item.get("license") or "CC-BY-NC-SA-4.0"),
                "source_url": f"https://nakala.fr/{doi}",
                "frame_count": len(frames),
                "plots": plots,
                "sample_frames": frames[:24],
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
        scenes.append(
            {
                "id": ft.get("id"),
                "datetime": props.get("datetime") or "",
                "platform": props.get("platform") or "landsat-mss",
                "cloud_cover": cloud,
                "bbox": ft.get("bbox"),
                "preview_url": (assets.get("rendered_preview") or assets.get("thumbnail") or {}).get("href") or "",
                "bands": {
                    k: (assets.get(k) or {}).get("href")
                    for k in ("green", "red", "nir08", "nir09")
                    if (assets.get(k) or {}).get("href")
                },
                "licence": "Public domain (USGS / NASA Landsat Collection 2)",
            }
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
        iiif_id = str(res.get("id") or "")
        gcps = []
        for g in it.get("gcps") or []:
            px = g.get("resource") or g.get("image")
            geo = g.get("geo")
            if isinstance(px, list) and isinstance(geo, list) and len(px) >= 2 and len(geo) >= 2:
                gcps.append([float(px[0]), float(px[1]), float(geo[0]), float(geo[1])])
        if iiif_id and len(gcps) >= 3:
            maps.append(
                {
                    "id": it.get("id") or "",
                    "iiif_service": iiif_id,
                    "preview_url": f"{iiif_id.rstrip('/')}/full/,900/0/default.jpg",
                    "full_url": f"{iiif_id.rstrip('/')}/full/max/0/default.jpg",
                    "width": res.get("width"),
                    "height": res.get("height"),
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

    login_req = urllib.request.Request(
        f"{base}/login-token",
        data=json.dumps({"username": username, "token": token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MapVietnam-M2M/0.3"},
        method="POST",
    )
    with urllib.request.urlopen(login_req, timeout=20) as resp:
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
                urllib.request.urlopen(logout, timeout=5).close()
            except Exception:
                pass
