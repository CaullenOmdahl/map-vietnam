"""Grid references: parsing, datum handling and agreement with a printed L7014 sheet."""

import math

import pytest
from gridref import from_lonlat, parse, to_wgs84

# Ground truth from the Cao Lanh sheet (L7014 6129 IV): its NW neatline corner is
# 105°30'E 10°30'N on Indian 1960, and NGA's registration gives that corner as
# UTM 48N E 554705 N 1160647 on the same datum. The margin names the 100 km square "WS".
CORNER_UTM = (554705, 1160647)


def test_parse_variants():
    a = parse("48P WS 683 427")
    b = parse("WS683427", default_zone="48P")
    c = parse("48pws683427")
    assert a == b == c
    assert (a.zone, a.band, a.square, a.precision_m) == (48, "P", "WS", 100)


def test_parse_precision():
    assert parse("WS 68 42", default_zone="48P").precision_m == 1000
    assert parse("WS 6834 4271", default_zone="48P").precision_m == 10
    with pytest.raises(ValueError):
        parse("WS 683 42", default_zone="48P")  # odd digit split
    with pytest.raises(ValueError):
        parse("WS683427")  # no zone given anywhere


def test_bad_zones_are_value_errors():
    # A malformed default zone used to escape as AttributeError (a 500 from the server).
    with pytest.raises(ValueError, match="not a grid zone"):
        parse("WS683427", default_zone="48")
    with pytest.raises(ValueError, match="does not exist"):
        parse("0P WS 683 427")
    with pytest.raises(ValueError, match="does not exist"):
        parse("61P WS 683 427")


def test_square_letters_match_printed_sheet():
    # Any point on the Cao Lanh sheet must fall in 100 km square WS on Indian 1960.
    assert from_lonlat(105.63, 10.40, datum="indian1960", digits=6).startswith("48PWS")


def test_sheet_corner_grid_ref_lands_on_sheet_corner():
    # The corner's printed grid position, converted to WGS84, must land where the corner's
    # Indian 1960 graticule position (105°30'E 10°30'N) lands — within the ~20 m sheet misfit.
    from datums import INDIAN_1960
    from pyproj import Transformer

    t = Transformer.from_crs(f"+proj=longlat {INDIAN_1960}", "EPSG:4326", always_xy=True)
    want_lon, want_lat = t.transform(105.5, 10.5)
    lon, lat, _ = to_wgs84(f"48P WS {CORNER_UTM[0] % 100000:05d} {CORNER_UTM[1] % 100000:05d}")
    d = math.hypot((lon - want_lon) * 111_320 * math.cos(math.radians(10.5)), (lat - want_lat) * 110_570)
    assert d < 25


def test_from_lonlat_roundtrip():
    ref = from_lonlat(105.63, 10.40, datum="indian1960", digits=10)
    lon, lat, r = to_wgs84(ref)
    assert r < 1 and abs(lon - 105.63) < 1e-5 and abs(lat - 10.40) < 1e-5


def test_indian1960_vs_wgs84_shift_is_hundreds_of_metres():
    lon_i, lat_i, _ = to_wgs84("48P WS 54705 60647", datum="indian1960")
    lon_w, lat_w, _ = to_wgs84("48P WS 54705 60647", datum="wgs84")
    d = math.hypot((lon_i - lon_w) * 111_320 * math.cos(math.radians(10.5)), (lat_i - lat_w) * 110_570)
    assert 100 < d < 1000


def test_uncertainty_is_half_diagonal_of_square():
    _, _, r = to_wgs84("48P WS 683 427", datum="indian1960")
    assert r == pytest.approx(100 * math.sqrt(2) / 2)


def test_returns_square_centre():
    # 6-digit ref names a 100 m square; the point returned is its middle, not its corner.
    lon_a, lat_a, _ = to_wgs84("48P WS 683 427", datum="wgs84")
    lon_b, lat_b, _ = to_wgs84("48P WS 68350 42750", datum="wgs84")  # 1 m square, centre +0.5 m
    assert lon_a == pytest.approx(lon_b, abs=1e-5) and lat_a == pytest.approx(lat_b, abs=1e-5)


def test_zoneless_candidates_pick_zone_from_column_letter():
    from gridref import candidates

    c = candidates("WS621625")  # HES gazetteer style, Kien Phong area
    assert [z for z, *_ in c] == ["48P"]
    _, lon, lat, r = c[0]
    assert 105.5 < lon < 105.6 and 10.5 < lat < 10.55 and r == pytest.approx(70.7, abs=0.1)
    # Zone 49 squares use column letters A-H (e.g. Hue area "YD" is zone 48 Q; "BS" is zone 49)
    assert {z for z, *_ in candidates("YD761215")} == {"48Q"}
    assert all(z.startswith("49") for z, *_ in candidates("BS555598"))
