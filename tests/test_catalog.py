"""catalog: one layer per installed MBTiles file, facts from the library, the tiles and hand-written notes."""

import json

import catalog
import tiling


def install(tiles_dir, map_id, **meta):
    """A stand-in installed map: an MBTiles file with metadata and no tiles."""
    with tiling.MBTilesWriter(tiles_dir / f"{map_id}.mbtiles") as w:
        w.metadata = tiling.standard_metadata(map_id, [105, 10, 106, 11], 8, meta.pop("maxzoom", 15), **meta)


def test_no_maps_yet_gives_an_empty_catalogue(tmp_path):
    out = tmp_path / "catalog.json"
    assert catalog.write_catalog(out, tiles_dir=tmp_path / "none", catalogue={}, scans_dir=tmp_path) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == {"layers": []}


def test_layers_merge_their_sources_and_sort_by_date(tmp_path):
    tiles = tmp_path / "tiles"
    install(tiles, "sheet", date="1969", date_note="edition 2, Indian 1960 datum")
    install(tiles, "scan", maxzoom=13)
    install(tiles, "undated")
    (tiles / "unfinished.mbtiles.partial").write_bytes(b"")
    library = {
        "series": {"l7014": {"name": "AMS L7014", "scale": "1:50,000"}},
        "maps": [
            {
                "id": "sheet",
                "series": "l7014",
                "title": "6129 II, near Cần Thơ",
                "region": "south",
                "urls": ["https://x/s.pdf"],
            }
        ],
    }
    (tmp_path / "scan.json").write_text(
        json.dumps({"meta": {"title": "A scan", "date": "1889", "scale": "1:400,000"}}), encoding="utf-8"
    )
    corrections = {"undated": {"overrides": {"title": "Fixed by hand"}}}

    layers = catalog.build_catalog(tiles, library, tmp_path, corrections)["layers"]

    assert [layer["id"] for layer in layers] == ["scan", "sheet", "undated"]
    scan, sheet, undated = layers
    assert sheet["title"] == "6129 II, near Cần Thơ" and sheet["series"] == "AMS L7014"
    assert sheet["date"] == "1969" and sheet["dateNote"].startswith("edition 2") and sheet["region"] == "south"
    assert sheet["source"] == "https://x/s.pdf" and sheet["tiles"] == "tiles/sheet/{z}/{x}/{y}.webp"
    assert sheet["bounds"] == [105, 10, 106, 11] and sheet["minzoom"] == 8 and sheet["maxzoom"] == 15
    assert scan["title"] == "A scan" and scan["scale"] == "1:400,000" and scan["maxzoom"] == 13
    assert undated["title"] == "Fixed by hand" and undated["date"] == ""


def test_every_map_carries_the_credit_shown_on_the_map(tmp_path):
    tiles = tmp_path / "tiles"
    install(tiles, "sheet")
    install(tiles, "bigrel_1873_f10")
    library = {"maps": [{"id": "sheet", "series": "l7014"}, {"id": "bigrel_1873_f10", "series": "scans"}]}
    layers = {layer["id"]: layer for layer in catalog.build_catalog(tiles, library, tmp_path, {})["layers"]}
    assert "US Army Map Service" in layers["sheet"]["attribution"]
    assert "David Rumsey" in layers["bigrel_1873_f10"]["attribution"]
    assert "CC BY-NC-SA" in layers["bigrel_1873_f10"]["attribution"]


def test_every_scan_in_the_library_has_a_credit():
    scans = {
        m["id"] for m in json.loads(catalog.CATALOGUE.read_text(encoding="utf-8"))["maps"] if m["series"] == "scans"
    }
    assert scans == set(catalog.SCAN_CREDITS)
