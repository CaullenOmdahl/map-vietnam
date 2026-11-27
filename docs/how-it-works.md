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
        │  a person adds a map: the viewer's Old maps card, or python3 start.py maps add
        ▼
 scripts/library.py ──► download_maps.py ──► data/raw/ ──► build_tiles.py / georef_scan.py ──► web/tiles/<id>.mbtiles
                                                                                                  │
                                                                                catalog.py ──► web/catalog.json
 a person adds a record source: the viewer's list of records, or python3 start.py setup
        ▼
 scripts/records.py ──► scripts/ingest/<source>.py ──► data/raw/ ──► data/evidence.sqlite
                                                                          │
                                   scripts/server.py (127.0.0.1) ──► web/app/ in its window
```

- **`start.py`** makes a Python environment in `.venv/`, runs `scripts/desktop.py`, which starts the server and shows the map in its own window (pywebview; the default browser if no window can open).
  It asks nothing and downloads nothing else: records and old maps are added from the viewer.
  `python3 start.py setup` adds the records from a terminal instead, through the same module.
- **The record sources** (`scripts/records.py`) add and remove record sources, only when a
  person asks for one (below).
- **The map library** (`scripts/library.py`) adds and removes old maps, only when a person asks
  for one by name (below).
- **The map pipeline** turns each old map into one file of map tiles (below).
- **The ingesters** (`scripts/ingest/<source>.py`) turn each record source into records in one
  SQLite file, `data/evidence.sqlite`.
- **The server** (`scripts/server.py`) serves the viewer and a small JSON API: search, records in
  a map area, grid-reference lookup, cases with their exports, the record sources, the map
  library, and stopping the app. It uses only the Python standard library and listens on
  `127.0.0.1` only, because it has no log-in.
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
`hes_gazetteer.py` writes province outlines (`data/work/hes_provinces.json`). The list of sources
in `scripts/records.py` (`SOURCES`) says what each one needs, and that is the order they run in.
Add tests with small excerpts of the source under `tests/fixtures/<source>/`.

An ingester can say how far along it is with `common.progress(fraction, "a few plain words")`.
When `records.py` runs it (it sets `MAPVIETNAM_PROGRESS`), that prints a line such as
`progress 0.42 Downloading 134 MB of 320 MB`, or `progress - Saving 26,551 air strikes` when the
total is not known, and the viewer shows the bar and the words. `cached_get` reports every large
download by bytes; the helicopter-loss ingester reports pages, the air-strike ingester rows. An
ingester that has to give up on purpose exits with one plain sentence (`raise SystemExit("...")`),
which is shown to the person as it is; a crash is put in plain words by `records.py`, with the
full output in `data/work/setup/logs/<source>.log`.

## The record sources

No records are downloaded unless a person adds a source: from the viewer (the server's
`/api/records` routes) or with `python3 start.py setup`. Both go through `scripts/records.py`,
which lists the sources (`SOURCES`: title, description, size, time, licence, what each needs),
knows which are added, and runs the queue. Adding a source runs its ingester as a child process,
one at a time, after the sources it needs; the command is always the fixed one for a listed
source, never anything taken from a request. A source is added when its marker
(`data/work/setup/<source>.done`), the database and the files it makes all exist, so the viewer
and the command line always agree.

- **Stopping** ends the ingester. Nothing is lost: downloads already finished are kept, a
  half-written one is ignored, and SQLite undoes a half-written save. The helicopter losses
  (hours of slow page reading, every page cached) carry on from where they stopped, and step
  aside by themselves whenever something else is waiting.
- **Removing** a source is a job in the same queue. It deletes the source's records (one that is
  in a case is kept and marked withdrawn, as in a sync), its marker, its downloads under
  `data/raw/` unless another added source reads them, and the files it made. Removing a source
  that other added sources were made with is refused unless they are removed too.
- **The air strikes** are loaded for one area. The default is around Cao Lãnh; the viewer (or
  `setup --only thor --area W,S,E,N`) can choose another, and a new area replaces the old strikes.
  An area may hold at most 150,000 strikes. The whole country is 3 million: measured, that is a
  database of 5.6 GB, 12 seconds for every search and three minutes to draw one screen, so it is
  not offered. The ingester stops, changing nothing, if an area turns out to hold too many.
- **The Kaggle key** is optional. The air-strike download is tried without one, which Kaggle
  allows at the time of writing. If Kaggle refuses, the ingester ends with exit status 3, the
  source is listed with `wants_key`, and the viewer shows where to choose the `kaggle.json` file.
  The key is saved in `.env` (readable by its owner only) and can be forgotten again. The server
  never returns, prints or logs it, and scrubs it from anything an ingester prints.
- **Two processes.** The viewer's server and `start.py setup` may run at once. A lock file
  (`data/work/setup/worker.lock`) makes them take turns, `current.json` beside it lets each show
  what the other is doing, and a stop asked for in one is honoured by the other within a second.
  Records and old maps have separate locks, so a map can be built while records are added: no
  ingester needs more than 0.4 GB of memory, next to about 2 GB for a map build.

**Quitting.** `POST /api/quit` (the viewer's "Quit Map Vietnam"), Control-C and closing the
terminal window all end the same way: the server stops the ingester and the map build under way,
so nothing is left running, then exits, and `start.py` exits with it.

## Cases

A case is a named collection: records you pin to it (with a note), candidate places you mark on
the map (with a radius and your reasoning), and free-text notes. Cases live in the same SQLite
file (tables `cases`, `case_items`, `sites`, `notes`). The server exports a case as GeoJSON, KML
or GPX: every point carries its accuracy radius, and every export carries the case notes and a
warning that strike sites may hold unexploded ordnance.
The `media` table stores geolocated oblique aerial photographs and flight-index references, and can
optionally sync entries with a connected Hub server.

## Intermediary Hub Server (Docker + Railway Volume)

Map Vietnam works completely standalone on `127.0.0.1`, and can also connect (`Settings ->
Intermediary Hub server`) to a self-hosted **Hub Server** (`scripts/hub/hub_server.py`, packaged in
`Dockerfile` + `railway.json` for Railway with an attached persistent volume at `/data`):

1. **Vaulted server-side keys.** An operator sets upstream credentials once on the Hub
   (`USGS_USERNAME`, `USGS_M2M_TOKEN`, `KAGGLE_KEY`, `EARTHDATA_TOKEN`, `PROPRIETARY_SOURCES_JSON`).
   Connected desktop clients authenticate with scoped Bearer keys (`mvhub_<id>_<secret>`, stored as
   salted `scrypt` hashes in `/data/hub.sqlite`) and never see upstream secrets.
2. **Polite upstream coordinator.** Concurrent identical upstream requests coalesce through
   `SingleFlight`, and per-host intervals (`HostPacer`) keep archive requests gentle.
3. **Infinite write-once cache.** Historical maps, aerial frames, LiDAR shots and built `.mbtiles`
   packs never change: `/data/raw_cache/`, `/data/tiles/*.mbtiles`, HTTP tile responses
   (`Cache-Control: private, max-age=31536000, immutable`), and the desktop client's local
   `data/hub_cache/` `.mbtiles` files have **zero TTL and zero automatic eviction**.
4. **Bidirectional archive sync.** Any map or LiDAR DEM imported, placed, or aligned locally on a
   desktop client can be pushed back to the Hub (`Cache to Hub` or `HUB_AUTO_PUSH=true`) with
   `private`, `group`, or `hub` visibility so collaborators or your other computers can stream it
   immediately or save the full `.mbtiles` pack offline (`Keep offline`).

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

No old map is downloaded unless a person adds it: from the viewer's Old maps card (the
server's `/api/library` routes) or with `python3 start.py maps add`. Maps made by versions before
the library (folders of PNG tiles) are reported by the same listing and converted, without
downloading anything, when the person asks (`POST /api/library/migrate`, or
`python3 start.py maps migrate`). Only maps listed in
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

**Placing whole series automatically** (`sheet_lists.py`, `place_sheets.py`). Picking control
points by hand takes an hour a map, which is fine for seven scans and not for hundreds. The
French 1:100,000 sheets and the US L509 1:250,000 sheets print a regular graticule: a "+" (or two
crossing lines) every 0.2 grad from the Paris meridian on the French sheets, every 15′ on the
Indian 1960 datum on the US ones. `sheet_lists.py` lists each sheet with where its image is and
roughly where it lies (from the archive's own record, or the sheet grid). `place_sheets.py` then,
once per sheet, offline:

1. scores every pixel of a medium-size copy for looking like a "+": ink along all four short arms
   leaving it, and none in the quarters between them (so a canal corner, a letter or a colour fill
   scores low). On sheets with a coloured grid over a black graticule, only dark ink counts;
2. predicts where the graticule's nodes lie from the sheet's printed frame and rough extent, then
   moves, scales and turns that lattice as a whole to where the most nodes land on a "+" (one stray
   "+" cannot outvote the lattice), refining the two directions' scales apart;
3. measures each node on the full-size image, drops any that do not fit the others, and fits the
   sheet to the rest;
4. refuses the sheet, rather than placing it badly, unless at least three nodes on two meridians
   and two parallels fit within 4 px, the scale and squareness are plausible, and the sheet lands
   within 8 km of where its archive says it is (a lattice paired one node off would move it a
   whole graticule step, about 20 km).

It writes the usual control file, so from there on a placed sheet is like any scan. On sheets
already placed by hand, the automatic placement agrees within about 20–60 m. The sheets it
refuses are listed with the reason (`--review`), for hand placing later. The British Admiralty
charts come already georeferenced from the Wikimaps Warper, whose volunteers' control points are
copied and scaled to the image the library downloads.

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

Everything a person can do is done here. The Records card lists the sources from `/api/records`
with Add, Stop, Update and Remove, the first-run card adds the standard set, and the air strikes
are added for the area on screen. The Old maps card does the same for maps. The viewer polls
only while something is being added, and fetches the evidence on screen again when the server's
`revision` number changes. "Quit Map Vietnam" stops the server.

## Area covered

Work started around Cao Lãnh (Đồng Tháp province) in the Mekong Delta, and the older scanned maps
and the default THOR area are there. The map library now reaches the whole country: 895 maps,
among them 498 sheets at 1:50,000 and 66 at 1:250,000, with gaps listed in
[data-sources.md](data-sources.md#coverage).
Most record sources cover all of Vietnam; THOR air strikes are loaded for one chosen area at a
time. Nothing in the design depends on the area: adding maps and choosing
the air-strike area is all it takes to look somewhere else.

## Not a replacement for

Official case files, the permissions needed to work in the field, or clearance of unexploded
ordnance. Map Vietnam shows what public records say and how sure they are; it does not say what is
on the ground.
