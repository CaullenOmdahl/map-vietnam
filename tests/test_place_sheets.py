"""place_sheets: synthetic sheets with a known graticule are placed on it; sgi_sheets turns NAKALA items into specs."""

import json
import math

import numpy as np
import place_sheets as ps
import pytest
import sheet_lists
from PIL import Image, ImageDraw

W, H = 6400, 4600
# Where the map's edge (the neatline) lies in the image, and the ground it covers.
NEAT = (700.0, 700.0, 5700.0, 3850.0)  # left, top, right, bottom
BOUNDS = [105.15, 10.31, 105.85, 10.77]
GRAT = {"step_deg": 0.18, "origin_lon": 2.337229}


def to_px(lon, lat):
    left, top, right, bottom = NEAT
    w, s, e, n = BOUNDS
    return left + (lon - w) / (e - w) * (right - left), top + (n - lat) / (n - s) * (bottom - top)


def turn(x, y, deg):
    t = math.radians(deg)
    cx, cy = W / 2, H / 2
    return cx + (x - cx) * math.cos(t) - (y - cy) * math.sin(t), cy + (x - cx) * math.sin(t) + (y - cy) * math.cos(t)


def make_sheet(deg=0.0, nodes=True, seed=3):
    """A grey scan: a frame (thick border, band, neatline), graticule crosses, 'roads' (including
    some crossing at right angles, which must not be taken for nodes) and paper noise."""
    im = Image.new("L", (W, H), 232)
    d = ImageDraw.Draw(im)
    left, top, right, bottom = NEAT

    def rect(inset, width):
        pts = [
            turn(x, y, deg)
            for x, y in (
                (left - inset, top - inset),
                (right + inset, top - inset),
                (right + inset, bottom + inset),
                (left - inset, bottom + inset),
            )
        ]
        d.line([*pts, pts[0]], fill=35, width=width)

    for inset, width in ((140, 8), (90, 2), (60, 2), (0, 2)):
        rect(inset, width)
    rng = np.random.default_rng(seed)
    for _ in range(30):
        x, y = rng.uniform(900, 5500), rng.uniform(900, 3600)
        a, length = rng.choice([0, math.pi / 2, rng.uniform(0, math.pi)]), rng.uniform(200, 900)
        d.line([turn(x, y, deg), turn(x + length * math.cos(a), y + length * math.sin(a), deg)], fill=70, width=3)
    if nodes:
        for lon, lat in ps.graticule_nodes(BOUNDS, GRAT):
            x, y = to_px(lon, lat)
            d.line([turn(x - 40, y, deg), turn(x + 40, y, deg)], fill=30, width=3)
            d.line([turn(x, y - 40, deg), turn(x, y + 40, deg)], fill=30, width=3)
    arr = np.asarray(im, np.float32) + rng.normal(0, 4, (H, W)).astype(np.float32)
    return np.clip(arr, 0, 255)


class ArraySheet(ps.Sheet):
    """A Sheet over an in-memory image."""

    def __init__(self, arr, bounds=BOUNDS):
        self.id, self.bounds, self.image, self.arr, self._size, self._file = (
            "test",
            bounds,
            {"url": "x.png"},
            arr,
            None,
            arr,
        )

    def _scaled(self, width, name):
        return ps._resized(self.arr, width), W / width


SPEC = {"graticule": GRAT}


@pytest.mark.parametrize("deg", [0.0, 0.35, -0.3])
def test_a_sheet_is_placed_on_its_graticule(deg):
    points, edge = ps.place(ArraySheet(make_sheet(deg)), SPEC)
    assert len(points) >= 4
    for x, y, lon, lat in points:
        assert math.dist((x, y), turn(*to_px(lon, lat), deg)) < 2.5, (lon, lat)
    left, top, right, bottom = NEAT
    want = [turn(left, top, deg), turn(right, top, deg), turn(right, bottom, deg), turn(left, bottom, deg)]
    for got, corner in zip(edge, want, strict=True):
        assert math.dist(got, corner) < 25  # the edge comes from the archive's extent through the fit


def test_a_rough_extent_is_corrected_by_the_graticule():
    # The archive's extent is about 150 m out; the graticule still places the sheet exactly.
    rough = [b + d for b, d in zip(BOUNDS, (0.0012, -0.0009, 0.0011, 0.0013), strict=True)]
    points, _ = ps.place(ArraySheet(make_sheet(), bounds=rough), SPEC)
    for x, y, lon, lat in points:
        assert math.dist((x, y), to_px(lon, lat)) < 2.5


def test_a_sheet_without_a_graticule_is_refused():
    with pytest.raises(ps.NotPlaced):
        ps.place(ArraySheet(make_sheet(nodes=False)), SPEC)


def test_node_score_prefers_a_cross_to_a_corner_or_a_tee():
    im = Image.new("L", (120, 40), 230)
    d = ImageDraw.Draw(im)
    d.line([(5, 20), (35, 20)], fill=30, width=1)  # cross at (20, 20)
    d.line([(20, 5), (20, 35)], fill=30, width=1)
    d.line([(60, 20), (80, 20)], fill=30, width=1)  # corner at (60, 20)
    d.line([(60, 20), (60, 35)], fill=30, width=1)
    d.line([(90, 20), (115, 20)], fill=30, width=1)  # tee at (100, 20)
    d.line([(100, 20), (100, 35)], fill=30, width=1)
    score = ps.node_score(ps.ink_mask(np.asarray(im, np.float32)))
    cross, corner, tee = score[18:23, 18:23].max(), score[18:23, 58:63].max(), score[18:23, 98:103].max()
    assert cross >= ps.NODE_SCORE_MIN > max(corner, tee)


def test_control_file_carries_the_nodes_offset_and_edge():
    spec = {"offset_deg": [0.0045, 0.0], "meta": {"title": "t"}}
    pts = [(1, 2, 105.297229, 10.44), (3, 4, 105.477229, 10.44), (5, 6, 105.297229, 10.62)]
    control = ps.control_file(spec, pts, [(0, 0), (9, 0), (9, 9), (0, 9)], "data/raw/scans/x/work.jpg")
    assert control["gcps"][0] == [1, 2, 105.297229, 10.44] and control["offset_deg"] == [0.0045, 0.0]
    assert control["neatline"][1] == [9, 0] and control["order"] == 1 and "3 graticule nodes" in control["placed_by"]


ITEM = {
    "identifier": "10.34847/nkl.877775w9",
    "files": [{"name": "x.TIF", "sha1": "c769", "mime_type": "image/tiff"}],
    "metas": [
        {
            "propertyUri": "http://nakala.fr/terms#title",
            "value": "Carte de l'Indochine : Pointe de Cà Mau est : feuille n°244E",
        },
        {"propertyUri": "http://purl.org/dc/terms/created", "value": "1952-07"},
        {"propertyUri": "http://nakala.fr/terms#license", "value": "CC-BY-NC-SA-4.0"},
        {"propertyUri": "http://purl.org/dc/terms/extent", "value": "1:100.000"},
        {"propertyUri": "http://purl.org/dc/terms/spatial", "value": "east=105.08333; north=9.08333;name=Cà Mau"},
        {
            "propertyUri": "http://purl.org/dc/terms/spatial",
            "value": "northlimit=8.964545; southlimit=8.511818; westlimit=105.066061; eastlimit=105.40596",
        },
    ],
}


def test_a_nakala_item_becomes_a_sheet_spec():
    spec = sheet_lists.sgi_spec(ITEM)
    assert spec["id"] == "sgi100k_244e_877775w9" and spec["sheet"] == "244E"
    assert spec["place"] == "Pointe de Cà Mau est" and spec["date"] == "1952-07"
    assert spec["bounds"] == [105.066061, 8.511818, 105.40596, 8.964545]
    assert spec["image"]["iiif"] == "https://api.nakala.fr/iiif/10.34847/nkl.877775w9/c769"
    assert "CC BY-NC-SA 4.0" in spec["licence"] and spec["meta"]["date"] == "1952"
    json.dumps(spec)


def test_items_that_are_not_100k_sheets_are_skipped():
    other = {
        **ITEM,
        "metas": [m if "extent" not in m["propertyUri"] else {**m, "value": "1:400.000"} for m in ITEM["metas"]],
    }
    assert sheet_lists.sgi_spec(other) is None
