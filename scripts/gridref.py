"""MGRS grid references as written in wartime reports, decoded on Indian 1960 or WGS84.

Reports give references like "XS 683 427" read off L7014 sheets (Indian 1960 datum). A reference
names a square, not a point: 6 digits = 100 m, 8 digits = 10 m. to_wgs84 returns the square's
centre plus a radius covering the whole square, so precision is never overstated.

The 100 km square letters use the standard ("AA") MGRS scheme; the L7014 sheet margins confirm
this (Cao Lanh 6129 IV prints square "WS", which the AA scheme gives on Indian 1960).

Inputs are reference strings (spaces and hyphens ignored, zone optional when a default is given)
and a datum name from datums.BY_NAME; outputs are WGS84 lon/lat in degrees and sizes in metres.
Used by the ingesters and the server; there is nothing to run.
"""

import math
import re
from dataclasses import dataclass
from functools import cache

from datums import BY_NAME
from pyproj import Transformer

# 100 km column letters cycle through three sets by zone; I and O are never used.
COLUMN_SETS = ["ABCDEFGH", "JKLMNPQR", "STUVWXYZ"]
# 100 km row letters repeat every 2,000 km of northing.
ROW_LETTERS = "ABCDEFGHJKLMNPQRSTUV"
# 8° latitude bands from 80°S, C to X.
BANDS = "CDEFGHJKLMNPQRSTUVWX"
REF_RE = re.compile(r"^(?:(\d{1,2})([C-HJ-NP-X]))?([A-HJ-NP-Z])([A-HJ-NP-V])(\d*)$")
ZONE_RE = re.compile(r"^(\d{1,2})([C-HJ-NP-X])$")


@dataclass(frozen=True)
class GridRef:
    """A decoded reference: the UTM square it names, on the datum it was decoded with."""

    zone: int
    band: str
    square: str  # the two 100 km square letters, e.g. "WS"
    easting: float  # full UTM easting of the square's SW corner (m)
    northing: float  # full UTM northing of the square's SW corner (m)
    precision_m: int  # side of the square the digits name (m)


@cache
def _utm(zone, datum):
    """(WGS84 lon/lat -> UTM, UTM -> WGS84 lon/lat) transformers for a zone on a named datum."""
    proj = f"+proj=utm +zone={zone} {BY_NAME[datum]} +units=m +no_defs"
    return (
        Transformer.from_crs("EPSG:4326", proj, always_xy=True),
        Transformer.from_crs(proj, "EPSG:4326", always_xy=True),
    )


def _row_offset(zone):
    """AA-scheme row lettering starts 5 letters later ("F") in even-numbered zones."""
    return 5 if zone % 2 == 0 else 0


def _band_of(lat):
    return BANDS[int((lat + 80) // 8)]


def parse(ref, default_zone=None, datum="indian1960"):
    """Decode `ref` into a GridRef; raise ValueError if it is not a usable grid reference.

    `default_zone` (e.g. "48P") supplies the zone and band when the reference omits them, as
    most wartime reports do. `datum` matters because the northing cycle is fixed from the band.
    """
    s = re.sub(r"[\s-]", "", ref.upper())
    m = REF_RE.match(s)
    if not m:
        raise ValueError(f"not a grid reference: {ref!r}")
    zone, band, col, row, digits = m.groups()
    if zone is None:
        if not default_zone:
            raise ValueError(f"{ref!r} has no grid zone (e.g. 48P) and no default was given")
        dz = ZONE_RE.match(default_zone.upper())
        if not dz:
            raise ValueError(f"not a grid zone: {default_zone!r} (expected e.g. 48P)")
        zone, band = dz.groups()
    zone = int(zone)
    if not 1 <= zone <= 60:
        raise ValueError(f"{ref!r}: UTM zone {zone} does not exist (1-60)")
    if len(digits) % 2 or len(digits) > 10:
        raise ValueError(f"{ref!r}: easting and northing need the same number of digits, 5 at most")
    n = len(digits) // 2
    precision = 10 ** (5 - n)
    e_in = int(digits[:n] or 0) * precision
    n_in = int(digits[n:] or 0) * precision

    col_idx = COLUMN_SETS[(zone - 1) % 3].find(col)
    if col_idx < 0:
        raise ValueError(f"{ref!r}: column letter {col} is not used in zone {zone}")
    easting = (col_idx + 1) * 100_000 + e_in

    # Row letters repeat every 2,000 km; the band letter picks the cycle. Take the first cycle
    # that reaches the band's southern edge (measured on the zone's central meridian), allowing
    # one 100 km row of slack because a band edge does not fall on a row boundary.
    row_100k = (ROW_LETTERS.index(row) - _row_offset(zone)) % 20
    band_south = -80 + 8 * BANDS.index(band)
    fwd, _ = _utm(zone, datum)
    _, base_n = fwd.transform(zone * 6 - 183, band_south)
    northing = row_100k * 100_000 + n_in
    while northing < base_n - 100_000:
        northing += 2_000_000
    return GridRef(zone, band, col + row, easting, northing, precision)


def candidates(ref, datum="indian1960", zones=(48, 49), bands="PQ"):
    """All plausible (zone+band, lon, lat, radius) readings of a reference written WITHOUT
    a grid zone, as in most wartime reports ("WS621625"). In Vietnam the 100 km column letter
    already fixes zone 48 vs 49; the band (P below 16°N, Q above) is kept only if the decoded
    latitude actually falls inside it (with 0.1° of slack for datum shift and rounding).
    Callers pick among several using other evidence (e.g. the province the source names)."""
    out = []
    for z in zones:
        for b in bands:
            try:
                lon, lat, r = to_wgs84(ref, datum=datum, default_zone=f"{z}{b}")
            except ValueError:
                continue
            south = -80 + 8 * BANDS.index(b)
            if south - 0.1 <= lat < south + 8.1:
                out.append((f"{z}{b}", lon, lat, r))
    return out


def to_wgs84(ref, datum="indian1960", default_zone="48P"):
    """(lon, lat, uncertainty_radius_m) of the centre of the square the reference names.

    The radius is half the square's diagonal, so the circle covers the whole square.
    """
    g = parse(ref, default_zone, datum)
    _, inv = _utm(g.zone, datum)
    half = g.precision_m / 2
    lon, lat = inv.transform(g.easting + half, g.northing + half)
    return lon, lat, g.precision_m * math.sqrt(2) / 2


def square(ref, datum="indian1960", default_zone="48P"):
    """WGS84 [lon, lat] corners (closed ring, SW first, anticlockwise) of the square a reference names."""
    g = parse(ref, default_zone, datum)
    _, inv = _utm(g.zone, datum)
    p = g.precision_m
    corners = [(0, 0), (p, 0), (p, p), (0, p), (0, 0)]
    return [list(inv.transform(g.easting + dx, g.northing + dy)) for dx, dy in corners]


def from_lonlat(lon, lat, datum="indian1960", digits=8):
    """MGRS reference (on `datum`) for a WGS84 lon/lat, e.g. '48PWS68304271'.

    `digits` is the total digit count (even, 2-10). Digits are truncated, not rounded, as MGRS
    requires: the reference names the square containing the point. The zone and band come from
    the WGS84 position; the datum shift (a few hundred metres) only matters right at their edges.
    """
    zone = int((lon + 180) // 6) + 1
    fwd, _ = _utm(zone, datum)
    e, n = fwd.transform(lon, lat)
    col = COLUMN_SETS[(zone - 1) % 3][int(e // 100_000) - 1]
    row = ROW_LETTERS[(int(n // 100_000) + _row_offset(zone)) % 20]
    k = digits // 2
    scale = 10 ** (5 - k)
    return f"{zone}{_band_of(lat)}{col}{row}{int(e % 100_000) // scale:0{k}d}{int(n % 100_000) // scale:0{k}d}"
