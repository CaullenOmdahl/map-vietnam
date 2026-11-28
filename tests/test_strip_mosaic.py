"""Tests for composite strip mosaic building (`scripts/strip_mosaic.py`)."""

import io
import json
from contextlib import closing

import library
import numpy as np
import pytest
import strip_mosaic
import tiling
from PIL import Image, ImageDraw


def _jpg_bytes(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _make_frame_image(width, height, fill_rgb, border=24, caption_bottom=44):
    """Synthetic aerial frame with a bright interior, dark film border, and white-on-black caption bar."""
    img = Image.new("RGB", (width, height), (6, 6, 6))
    draw = ImageDraw.Draw(img)
    # Bright textured interior inside the dark border and above the caption bar.
    draw.rectangle(
        [border, border, width - border - 1, height - caption_bottom - 1],
        fill=fill_rgb,
    )
    # Add a white text-like strip inside the bottom black caption bar (like EROM80.HV741...).
    text_y = height - caption_bottom // 2
    for x in range(border + 10, width - border - 20, 18):
        draw.rectangle([x, text_y - 4, x + 6, text_y + 4], fill=(235, 235, 235))
    return img


def test_trim_film_border_mask_strips_dark_borders_and_caption_bar():
    img = _make_frame_image(400, 400, (170, 185, 160), border=28, caption_bottom=52)
    left, top, right, bottom = strip_mosaic.trim_film_border_mask(img)
    assert left == 28
    assert top == 28
    assert right == 28
    assert bottom == 52

    # Also works on NumPy arrays and leaves borderless bright images untrimmed.
    clean = np.full((300, 300, 3), 180, dtype=np.uint8)
    assert strip_mosaic.trim_film_border_mask(clean) == (0, 0, 0, 0)


def _ingest_placed_frame(lib, frame_num, img, corners_lonlat, mission="HV741", year=1952, warp_mode="affine"):
    part = lib.custom.incoming_path()
    part.write_bytes(_jpg_bytes(img))
    title = f"Mission {mission} SHDAI__AI_75_E_192_{mission}__{frame_num:04d}__V.jpg"
    entry = lib.custom.ingest(part, f"f{frame_num}.jpg", title, str(year))
    w, h = entry["image"]["width"], entry["image"]["height"]
    nw, ne, se, sw = corners_lonlat
    points = [
        [0.0, 0.0, nw[0], nw[1]],
        [float(w), 0.0, ne[0], ne[1]],
        [float(w), float(h), se[0], se[1]],
        [0.0, float(h), sw[0], sw[1]],
    ]
    return lib.custom.save_points(entry["id"], points, title, str(year), warp_mode=warp_mode)


def _sample_tile_rgb(mbtiles_path, lon, lat):
    with closing(tiling.connect_read_only(mbtiles_path)) as db:
        meta = dict(db.execute("select name, value from metadata"))
        max_z = int(meta["maxzoom"])
        tx, ty, px, py = tiling.lonlat_to_tile_pixel(lon, lat, max_z)
        raw = tiling.read_tile(db, max_z, tx, ty)
        assert raw is not None, f"No tile at z={max_z} ({tx}, {ty}) for ({lon}, {lat})"
        with Image.open(io.BytesIO(raw)) as im:
            rgba = np.asarray(im.convert("RGBA"))
        return rgba[py, px]


def test_build_strip_mosaic_stitches_trims_and_updates_without_duplicating(tmp_path):
    lib = library.Library(root=tmp_path)

    # Three overlapping 300x300 frames stepping north-to-south by 0.006 deg (each spans 0.010 deg -> 40% overlap).
    # Each frame has dark 24px film borders and a 40px black caption bar at the bottom.
    lon_w, lon_e = 105.800, 105.810
    specs = [
        (1, (190, 185, 175), 21.020, 21.030),
        (2, (185, 190, 180), 21.014, 21.024),
        (3, (180, 185, 190), 21.008, 21.018),
    ]
    placed = []
    for n, color, lat_s, lat_n in specs:
        img = _make_frame_image(300, 300, color, border=24, caption_bottom=40)
        corners = [(lon_w, lat_n), (lon_e, lat_n), (lon_e, lat_s), (lon_w, lat_s)]
        placed.append(_ingest_placed_frame(lib, n, img, corners, mission="HV741", year=1952))

    # Also add an unplaced 4th frame of the same mission: it should be ignored cleanly.
    part = lib.custom.incoming_path()
    part.write_bytes(_jpg_bytes(_make_frame_image(300, 300, (170, 170, 170))))
    lib.custom.ingest(part, "f4.jpg", "Mission HV741 SHDAI__AI_75_E_192_HV741__0004__V.jpg", "1952")

    progress_values = []
    mosaic = strip_mosaic.build_strip_mosaic(lib, placed[0]["id"], progress=progress_values.append)

    assert mosaic["kind"] == "mosaic"
    assert mosaic["custom"] is True
    assert mosaic["mosaic_mission"] == "SHDAI__AI_75_E_192_HV741"
    assert [f["frame"] for f in mosaic["mosaic_frames"]] == [1, 2, 3]
    assert progress_values and progress_values[-1] == pytest.approx(1.0)

    # Combined union bounds cover all 3 placed frames.
    w, s, e, n = mosaic["bounds"]
    assert w == pytest.approx(lon_w, abs=1e-4)
    assert e == pytest.approx(lon_e, abs=1e-4)
    assert s == pytest.approx(21.008, abs=1e-4)
    assert n == pytest.approx(21.030, abs=1e-4)

    # Valid MBTiles written and catalog.json updated.
    mbtiles = lib.tiles_path(mosaic["id"])
    assert mbtiles.is_file()
    meta = tiling.read_metadata(mbtiles)
    assert meta["format"] == "webp"
    assert tiling.read_bounds(mbtiles) is not None
    catalog_data = json.loads((tmp_path / "web" / "catalog.json").read_text(encoding="utf-8"))
    assert any(layer["id"] == mosaic["id"] for layer in catalog_data["layers"])

    # Overlap seam check: frame 1's bottom black caption bar lies at lat ~ 21.020..21.0213,
    # which overlaps frame 2's bright interior (21.014..21.024). Because the dark border/caption
    # bar was trimmed, the mosaic at (105.805, 21.0206) must be bright (~185), NOT dark!
    seam_px = _sample_tile_rgb(mbtiles, 105.805, 21.0206)
    assert seam_px[3] == 255
    assert int(seam_px[:3].mean()) > 140, f"Dark caption bar bled into overlap seam: {seam_px}"

    # Frame 2's top dark border lies at lat ~ 21.0232..21.024, overlapping frame 1's interior.
    seam_px_2 = _sample_tile_rgb(mbtiles, 105.805, 21.0235)
    assert seam_px_2[3] == 255
    assert int(seam_px_2[:3].mean()) > 140, f"Dark top border bled into overlap seam: {seam_px_2}"

    # Re-running updates the same mosaic entry in place without creating duplicates.
    mosaic_rebuilt = strip_mosaic.build_strip_mosaic(lib, placed[2]["id"])
    assert mosaic_rebuilt["id"] == mosaic["id"]
    mosaics = [e for e in lib.custom.entries() if e.get("mosaic_mission") == "SHDAI__AI_75_E_192_HV741"]
    assert len(mosaics) == 1


def test_build_strip_mosaic_requires_at_least_two_placed_frames(tmp_path):
    lib = library.Library(root=tmp_path)
    img = _make_frame_image(200, 200, (180, 180, 180), border=16, caption_bottom=24)
    corners = [(105.80, 21.02), (105.81, 21.02), (105.81, 21.01), (105.80, 21.01)]
    only = _ingest_placed_frame(lib, 1, img, corners, mission="HV888", year=1953)

    with pytest.raises(ValueError, match="at least 2 placed frames"):
        strip_mosaic.build_strip_mosaic(lib, only["id"])


def test_build_strip_mosaic_handles_homography_and_rotated_frames(tmp_path):
    lib = library.Library(root=tmp_path)
    lon_w, lon_e = 106.100, 106.110
    img1 = _make_frame_image(260, 260, (195, 175, 165), border=20, caption_bottom=32)
    img2 = _make_frame_image(260, 260, (165, 185, 195), border=20, caption_bottom=32)
    # Frame 1 upright with homography; frame 2 rotated 180 degrees with affine.
    c1 = [(lon_w, 20.520), (lon_e, 20.520), (lon_e, 20.510), (lon_w, 20.510)]
    c2_rot180 = [(lon_e, 20.504), (lon_w, 20.504), (lon_w, 20.514), (lon_e, 20.514)]
    f1 = _ingest_placed_frame(lib, 1, img1, c1, mission="HV742", year=1954, warp_mode="homography")
    _ingest_placed_frame(lib, 2, img2, c2_rot180, mission="HV742", year=1954, warp_mode="affine")

    mosaic = strip_mosaic.build_strip_mosaic(lib, f1["id"])
    mbtiles = lib.tiles_path(mosaic["id"])
    assert mbtiles.is_file()
    # Both the overlap zone and frame 2's rotated interior render brightly with full opacity.
    for lat in (20.512, 20.507):
        px = _sample_tile_rgb(mbtiles, 106.105, lat)
        assert px[3] == 255
        assert int(px[:3].mean()) > 140
