"""Build web/catalog.json, the viewer's list of map layers: one entry per installed map.

A map is installed when web/tiles/<id>.mbtiles exists (unfinished builds are named .partial and
are never listed). The facts shown for it come from, later sources winning:
  1. the map library catalogue, data/sources/map_library.json: title, series name, scale, region;
  2. the MBTiles metadata written when the map was built: extent, zoom range, and for GeoPDF
     sheets the edition date and datum printed in the PDF;
  3. for scans, the hand-written "meta" block of data/scans/<id>.json;
  4. hand corrections in data/maps.json: {"<id>": {"overrides": {"date": "1966", ...}}}.
Layers are sorted oldest first, undated last. scripts/library.py rewrites the file after every
install and removal; run this by hand only after editing one of the inputs.

Usage: .venv/bin/python scripts/catalog.py
"""

import argparse
import json
from pathlib import Path

from tiling import read_metadata

ROOT = Path(__file__).resolve().parent.parent
TILES = ROOT / "web" / "tiles"
OUT = ROOT / "web" / "catalog.json"
CATALOGUE = ROOT / "data" / "sources" / "map_library.json"
SCANS = ROOT / "data" / "scans"
CORRECTIONS = ROOT / "data" / "maps.json"
MBTILES = ".mbtiles"
DEFAULT_MAXZOOM = 15

# The credit the viewer shows on the map while an old map is on screen (MapLibre attribution, so
# plain text or simple links only). Short forms of the credit lines in NOTICE; some licences (CC BY,
# CC BY-NC-SA) require the credit wherever the map is shown, screenshots included.
SERIES_CREDITS = {
    "l7014": "Old map: US Army Map Service, public domain",
    "jog1501": "Old map: US Defense Mapping Agency, public domain",
    "l909": "Old map: US Army Map Service, public domain",
    "l509": "Old map: US Army Map Service, courtesy of the University of Texas Libraries",
    "warper": "Old map: Admiralty chart, via Wikimedia Commons and Wikimaps Warper",
    "sgi100k": "Old map: Service Géographique de l'Indochine, via CNRS/NAKALA",
}
SCAN_CREDITS = {
    "ams_l509_nc48-6_1954": "Old map: US Army Map Service, via ANU Asia Pacific Map Collection",
    "ams_l509_nc48-6_1962": "Old map: US Army Map Service, courtesy of the University of Texas Libraries, "
    "The University of Texas at Austin",
    "atlas_1909_sadec": "Old map: Humazur digital library, Université Côte d'Azur",
    "bigrel_1873_f10": "Old map: David Rumsey Map Collection, David Rumsey Map Center, Stanford Libraries "
    "(CC BY-NC-SA 3.0)",
    "fr_sgi_100k_228_longxuyen": "Old map: Centre IST REGARDS (CNRS), NAKALA (CC BY-NC-SA 4.0)",
    "koch_1889_f2": "Old map: Centre IST REGARDS (CNRS), NAKALA (CC BY 4.0)",
    "routiere_1951_f20_phnompenh": "Old map: ANU Asia Pacific Map Collection",
}


def read_json(path, default):
    """Parsed JSON from path, or `default` when the file does not exist."""
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def installed_ids(tiles_dir):
    """Ids of the finished maps in tiles_dir, sorted; none if nothing is installed."""
    if not tiles_dir.is_dir():
        return []
    return sorted(p.name.removesuffix(MBTILES) for p in tiles_dir.glob(f"*{MBTILES}"))


def parse_bounds(text):
    """[west, south, east, north] from MBTiles "w,s,e,n" metadata, or None."""
    try:
        bounds = [float(v) for v in text.split(",")]
    except (AttributeError, ValueError):
        return None
    return bounds if len(bounds) == 4 else None


def layer(layer_id, meta, entry, series, scan_meta, corrections):
    """One catalog.json layer from the sources listed in the module docstring."""
    out = {
        "id": layer_id,
        "title": entry.get("title") or meta.get("name") or layer_id,
        "series": series.get("name", ""),
        "seriesId": entry.get("series", ""),  # the catalogue's series key: "l7014", "jog1501", "scans"
        "scale": series.get("scale", ""),
        "date": meta.get("date", ""),  # ISO-ish: "1966" or "1966-03"
        "dateNote": meta.get("date_note", ""),  # what the date means (edition, survey, print)
        "source": entry.get("page") or (entry.get("urls") or [""])[0],
        "attribution": entry.get("credit") or SCAN_CREDITS.get(layer_id) or SERIES_CREDITS.get(entry.get("series"), ""),
        "region": entry.get("region", ""),
        "tiles": f"tiles/{layer_id}/{{z}}/{{x}}/{{y}}.webp",
        "bounds": parse_bounds(meta.get("bounds")),
        "minzoom": int(meta.get("minzoom", 0)),
        "maxzoom": int(meta.get("maxzoom", DEFAULT_MAXZOOM)),
    }
    out.update({k: v for k, v in scan_meta.items() if k in out and v})
    out.update(corrections.get(layer_id, {}).get("overrides", {}))
    return out


def build_catalog(tiles_dir=None, catalogue=None, scans_dir=None, corrections=None):
    """{"layers": [...]} for every installed map. `catalogue` is the parsed map library file."""
    tiles_dir = tiles_dir or TILES
    catalogue = catalogue if catalogue is not None else read_json(CATALOGUE, {})
    scans_dir = scans_dir or SCANS
    corrections = corrections if corrections is not None else read_json(CORRECTIONS, {})
    entries = {m["id"]: m for m in catalogue.get("maps", [])}
    series = catalogue.get("series", {})
    layers = []
    for layer_id in installed_ids(tiles_dir):
        entry = entries.get(layer_id, {})
        scan_meta = read_json(scans_dir / f"{layer_id}.json", {}).get("meta", {})
        meta = read_metadata(tiles_dir / f"{layer_id}{MBTILES}")
        layers.append(layer(layer_id, meta, entry, series.get(entry.get("series"), {}), scan_meta, corrections))
    layers.sort(key=lambda layer: (layer["date"] == "", layer["date"], layer["id"]))
    return {"layers": layers}


def write_catalog(out=None, **sources):
    """Write catalog.json (atomically, so the viewer never reads half a file). Returns the layer count."""
    out = out or OUT
    data = build_catalog(**sources)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(out)
    return len(data["layers"])


def main():
    """Write web/catalog.json and print how many layers it lists."""
    n = write_catalog()
    print(f"catalog: {n} layers -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__.splitlines()[0]).parse_args()
    main()
