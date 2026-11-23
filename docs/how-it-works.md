# How Map Vietnam works

This page is for anyone who wants to know what happens under the hood: what the parts are, how a
record is stored, and how an old paper map ends up lined up with today's map.

## The idea

Information about a place in wartime Vietnam is scattered. A grid reference sits in a 1968 loss
report, a hamlet name in an archive gazetteer, a strike in an air force database, a battle in an
encyclopedia. Each uses its own coordinate system, spelling and level of detail. Map Vietnam brings
them onto one map, next to the maps people used at the time, so you can see everything known about
a place and judge for yourself how much to trust each piece.

Four rules follow from that:

1. **Never fake precision.** Every point carries an accuracy radius taken from how precise its
   source was. A 6-figure grid reference is a 100 m square; a record that names only a province
   gets a circle covering the whole province.
2. **Never hide the original.** The coordinate is stored exactly as the source wrote it, with its
   datum, beside the converted position.
3. **Keep the source one click away.** Every record links to the page or file it came from, with
   the date it was retrieved.
4. **Leave formal burials out.** Cemeteries, graves and grave registries are documented elsewhere
   and are not stored. Nothing is filtered by nationality: US, Australian, South Vietnamese,
   North Vietnamese and NLF, and French Union (1945–54) sources are all in scope.

## The parts

```text
 data/sources/map_library.json (every map that can be added)
        │  a person adds a map: the viewer's "Add old maps", or python3 start.py maps add
        ▼
 scripts/library.py ──► download_maps.py ──► data/raw/ ──► build_tiles.py / georef_scan.py ──► web/tiles/<id>.mbtiles
                                                                                                  │
                                                                                catalog.py ──► web/catalog.json
 public archives ──► scripts/ingest/<source>.py ──► data/evidence.sqlite
                                                          │
                                   scripts/server.py (127.0.0.1) ──► web/app/ in your browser
```

- **`start.py`** runs everything in order: it makes a Python environment in `.venv/`, runs each
  ingester, then starts the server. Each finished step leaves a marker in `data/work/setup/`, so
  a second run only does what is missing. It never downloads an old map.
- **The map library** (`scripts/library.py`) adds and removes old maps, only when a person asks
  for one by name (below).
- **The map pipeline** turns each old map into one file of map tiles (below).
- **The ingesters** (`scripts/ingest/<source>.py`) turn each record source into records in one
  SQLite file, `data/evidence.sqlite`.
- **The server** (`scripts/server.py`) serves the viewer and a small JSON API over the records:
  search, records in a map area, grid-reference lookup, and cases with their exports. It uses only
  the Python standard library and listens on `127.0.0.1` only, because it has no log-in.
- **The viewer** (`web/app/`) is one HTML page, one script and one style sheet, with no build
  step. It uses MapLibre GL for the map.

## A record

Every record in `data/evidence.sqlite` (table `evidence`) has:

| Field | Meaning |
| --- | --- |
| `source`, `source_record_id` | Which source, and the source's own key for this record. The key must stay the same from one download to the next: never a row number or text the source may reword. |
| `kind` | One of `loss_incident`, `aircraft_crash`, `air_strike`, `ground_combat`, `field_burial` (a reported hasty or temporary burial, never a formal grave), `military_site`, `placename`, `other`. |
| `date_start`, `date_end` | `YYYY`, `YYYY-MM` or `YYYY-MM-DD`. The length of the string is its precision. |
| `original_coord`, `original_datum` | The position exactly as the source gave it (for example `WS683427`, `Indian 1960`). |
| `lon`, `lat` | The converted position, WGS84 degrees. |
| `uncertainty_m` | Accuracy radius in metres; always greater than zero. |
| `title`, `description`, `url` | What the viewer shows, and the link back to the source. |
| `raw_json` | The facts the record was built from, for checking. |
| `withdrawn_at`, `withdrawn_reason` | Set when the source no longer lists a record that someone has put in a case. |

The `sources` table holds each source's name, link, licence, notes and download date. `store.py`
checks the rules above whenever a record is built, and refuses a record that breaks one.

### Keeping in step with the source

Every ingester writes through `store.sync_source(db, source, records)`. After a sync the store
holds exactly what the source gives now:

- a new or changed record is inserted or updated;
- a record the source no longer gives is deleted, **unless** it is in a case: then it is kept,
  marked withdrawn, hidden from the map and search, and still shown in the case;
- if the source lists a withdrawn record again, it becomes current again.

An ingester never deletes records itself, and a sync touches only its own source.

### Writing an ingester

An ingester is a module with a `main()` that:

1. fetches the raw data with `common.cached_get`, which stores each file under
   `data/raw/<source>/` and never fetches it again unless asked to (with `--refresh` in most ingesters; `vhpa.py` has
   `--refresh-index` and `--retry-failed`). It sends a descriptive User-Agent and waits between requests; respect each site's
   terms and `robots.txt`;
2. builds `store.Evidence` records, keeping the original coordinate and datum and setting an
   honest radius;
3. calls `store.upsert_source` once, then `store.sync_source` with all the records.

Some ingesters need another's output: `gns.py` writes a place-name index
(`data/work/gns_index.json`) that the others use to place records that give only a name, and
`hes_gazetteer.py` writes province outlines (`data/work/hes_provinces.json`). `start.py` knows
the order. Add tests with small excerpts of the source under `tests/fixtures/<source>/`.

## Cases

A case is a named collection: records you pin to it (with a note), candidate places you mark on
the map (with a radius and your reasoning), and free-text notes. Cases live in the same SQLite
file (tables `cases`, `case_items`, `sites`, `notes`). The server exports a case as GeoJSON, KML
or GPX: every point carries its accuracy radius, and every export carries the case notes and a
warning that strike sites may hold unexploded ordnance.
The `media` table is reserved for geolocated photos; the viewer does not use it yet.

## Grid references and datums

Wartime reports give positions as MGRS grid references, such as `WS 683 427`, read off the US
L7014 1:50,000 sheets. Those sheets are drawn on the **Indian 1960** datum, so the same reference
on the WGS84 grid that a GPS uses lands a few hundred metres away. `scripts/gridref.py` decodes a
reference on either datum: it returns the centre of the square it names and a radius that covers
the whole square (6 figures = 100 m, 8 figures = 10 m). Many sources write the reference without
its zone; the ingester then tries every zone the reference could be in and keeps the one that
agrees with the province the record names.

`scripts/datums.py` holds the datum shifts (a 3-parameter shift to WGS84, in metres). The map
pipeline and the grid-reference code import the same values, so a reference read off a sheet and
the sheet itself are always converted the same way. `scripts/ingest/datum_check.py` checked the
choice: it pairs wartime hamlet grid references with the same hamlets in a modern gazetteer and
measures which datum they agree with. The answer was Indian 1960.

## The map library

No old map is downloaded unless a person adds it: from the viewer's "Add old maps" card (the
server's `/api/library` routes) or with `python3 start.py maps add`. Only maps listed in
`data/sources/map_library.json` can be added, and only from the https addresses listed there.
Adding a map downloads its file (checked: whole, the right kind, a PDF that ends like one), then
builds its tiles in a child process, one map at a time; removing it deletes its tiles, its
download and its build log. A map is installed when `web/tiles/<id>.mbtiles` exists.

The viewer's server and the command line may run at once. A lock file
(`data/work/maps/worker.lock`) makes them take turns; the one adding a map records which in
`data/work/maps/current.json`, so the other can show its progress, and a removal asked for in one
leaves a cancel request that the other honours within a second. Work cut short by a stop
(`.part` downloads, `.partial` tile files) is cleaned up when the next map is added or the server
starts, and those maps are shown as interrupted so they can be tried again. The server only
answers requests addressed to itself (Host header, against DNS rebinding), and any request that
changes something must come from its own page (Origin and Sec-Fetch-Site) as JSON.

## The map pipeline

Each map is one MBTiles file, `web/tiles/<map>.mbtiles`: an SQLite database of 256-pixel WebP
tiles (rows numbered TMS style, as the MBTiles format asks) plus its metadata. `scripts/tiling.py`
does the last step for every map: it warps the georeferenced image onto the web map grid, writes
the tiles (fully transparent tiles are skipped; the server answers for them with an empty tile),
and records the map's extent, zoom range, date and datum. The file is written as
`<map>.mbtiles.partial` and renamed when complete, so a file named `.mbtiles` is always a finished
map.

**US GeoPDF sheets** (`build_tiles.py`), the 1:50,000 L7014 family and the 1:250,000 Joint
Operations Graphics. The USGS GeoPDFs carry georeferencing that ties positions on the page to UTM
metres on the sheet's datum: registration points, or on some reissued sheets only the transform
itself. The page is rendered with pdfium, an affine transform is fitted to the registration points
(residuals are 10–21 m, from paper and scan distortion), everything outside the printed frame is
made transparent (the PDF's own "neatline" sometimes includes margin, so the sheet is also cut
at its frame corners, but only along sides where the neatline overshoots the whole way: a
peninsula drawn past the frame is kept), and the result is converted from the sheet's datum to
WGS84. Adjacent sheets join cleanly, and overlays sit within about 50 m of modern imagery.
`l7014_meta.py` reads each sheet's title, edition date and datum from the PDF into the map's
metadata; a hand correction goes in `data/maps.json` under the map's `"overrides"` key.

**Scanned maps** (`georef_scan.py`). An older map has no built-in coordinates. For each one,
`data/scans/<id>.json` lists control points, each a pixel position on the scan and the
longitude and latitude it shows (usually where printed graticule lines cross), plus an optional
map frame and the map's title, date, scale and source. The scan is fitted to the control points
with GDAL. Pixel positions refer to one exact image, so `download_maps.py` either uses the
archive's file as it is, or rebuilds the working image from the archive's JPEG 2000 file the same
way every time (the catalogue entry records how, with the file's size and SHA-256).

Old surveys have errors of their own. The fit can only be as good as the map: an 1889 map at
1:400,000 drawn from older surveys may put a river channel kilometres from where it was. Each
map's date note in the viewer says what is known about its accuracy. Use
`scripts/compare_overlay.py <map id> <lon> <lat>` to make a side-by-side image of a map, the 1960s
sheets and today's map at one place, to measure an offset.

`catalog.py` then lists every installed map in `web/catalog.json`, oldest first, and the viewer
reads that list. The library rewrites it after every map added or removed.

## The viewer

The viewer is styled after phone map apps: the map fills the screen, a panel (a side bar on a
wide screen, a sheet you drag on a narrow one) holds search and one card at a time, and a bar at
the bottom picks the old map, sets its opacity and turns on swipe-compare. It asks the server for
records one grid cell at a time as you move, draws each record's accuracy ring at its true ground
size, and filters by kind and by year. Your view, chosen map, opacity and filters are remembered in
the browser's local storage; cases are stored by the server.

## Area covered

Work started around Cao Lãnh (Đồng Tháp province) in the Mekong Delta, and the older scanned maps
and the default THOR area are there. The map library now reaches the whole country: 498 sheets at
1:50,000 and 32 at 1:250,000, with gaps listed in [data-sources.md](data-sources.md#coverage).
Most record sources cover all of Vietnam; THOR air strikes are loaded for the Cao Lãnh area
unless asked for more. Nothing in the design depends on the area: adding maps and widening the
THOR area is all it takes to cover more.

## Not a replacement for

Official case files, the permissions needed to work in the field, or clearance of unexploded
ordnance. Map Vietnam shows what public records say and how sure they are; it does not say what is
on the ground.
