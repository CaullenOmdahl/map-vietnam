# Contributing

Thank you for helping. Corrections to records, new maps, new sources and fixes are all welcome.
Please read the [code of conduct](CODE_OF_CONDUCT.md) first. It's short.

## Reporting a problem with a record or a map

Open an issue with the record's source and record id (shown on its card), or the map's name, and
what the source actually says. A link to the source page helps most. Please do not paste long
passages of copyrighted text.

## Working on the code

1. Set up once: `python3 start.py setup` (add `--skip vhpa,thor` for a quicker start), then
   `.venv/bin/python -m pip install ruff`. No old map is needed for the tests; add one with
   `python3 start.py maps add <id>` when you want to see one.
2. Make your change on a branch.
3. Before you open a pull request, run:

   ```sh
   .venv/bin/python -m pytest -q
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   ```

   CI runs the same three checks on Linux and macOS.

Style, in short:

- Python 3.12 or newer, formatted with `ruff format` (line length 120).
- Each module starts with a docstring saying what it is for, what it reads and writes, and how to
  run it. Public functions have docstrings.
- Comments explain intent, units, datums and trade-offs, not what the next line does.
- `start.py` uses only the standard library, because it runs before `.venv` exists.
- Behaviour changes come with a test. Bug fixes come with a test that fails without the fix.

## Where things are

```text
start.py                    one-command setup and server (standard library only)
Start Map Vietnam.command   macOS double-click launcher for start.py
requirements.txt            pinned Python dependencies
scripts/
  library.py                the map library: adds and removes old maps (python3 start.py maps ...)
  map_library_catalogue.py  builds data/sources/map_library.json, every map that can be added
  download_maps.py          fetches one map's file from the addresses in that catalogue
  l7014_meta.py             reads title, date and datum from a US GeoPDF (kept in its map's metadata)
  build_tiles.py            a GeoPDF sheet -> web/tiles/<id>.mbtiles
  georef_scan.py            a scanned map + control points (data/scans/<id>.json) -> web/tiles/<id>.mbtiles
  tiling.py, datums.py      shared tiling (WebP tiles in one MBTiles file per map) and datum code
  catalog.py                web/tiles/ -> web/catalog.json, the viewer's list of installed maps
  gridref.py                wartime MGRS grid references on Indian 1960 or WGS84
  store.py                  the SQLite evidence store, data/evidence.sqlite
  server.py                 local web server and JSON API
  compare_overlay.py        side-by-side image to check how well a map lines up
  ingest/<source>.py        one ingester per record source; common.py holds shared helpers
web/app/                    the viewer: index.html, app.js, app.css (MapLibre GL, no build step)
tests/                      pytest suite; tests/fixtures/ holds small source excerpts
docs/                       how it works, data sources, known limits
```

Downloads go to `data/raw/`, intermediate files to `data/work/`, records to
`data/evidence.sqlite` and the maps a person added to `web/tiles/`. All of these are kept out of
git: `start.py` rebuilds the records, and each map is rebuilt when it is added again.

## Adding a record source

Read [docs/how-it-works.md](docs/how-it-works.md) first. A new ingester:

- lives in `scripts/ingest/<source>.py` and downloads through `common.cached_get`, which caches
  every file in `data/raw/<source>/`, waits between requests and sends a descriptive User-Agent;
- respects the source's terms and `robots.txt`, and stores only what its licence allows;
- keeps every coordinate as the source wrote it, with its datum, and gives every record an honest
  accuracy radius;
- uses the source's own stable key as `source_record_id`;
- writes through `store.sync_source`;
- leaves out formal burials (cemeteries, graves, grave registries);
- has tests that run on small excerpts in `tests/fixtures/<source>/`, with the excerpt's origin
  and licence added to `tests/fixtures/README.md`;
- is added as a step in `start.py` and described in [docs/data-sources.md](docs/data-sources.md).

## Adding a map

Every map that can be added is listed in `data/sources/map_library.json`, which
`scripts/map_library_catalogue.py` generates (do not edit it by hand):

- **US GeoPDF sheets** (1:50,000 and 1:250,000) come from a probe of the USGS store and the PDFs'
  own metadata; see the script's docstring. Each sheet is placed by the georeferencing inside
  its GeoPDF and cut at its frame.
- **A scanned map:** add an entry to the `scans` list in `data/sources/maps.json` (the script copies
  it into the catalogue), write `data/scans/<id>.json` with at least three control points (pixel
  to longitude and latitude) and the map's title, date and source, then regenerate the catalogue.

Then add the map with `python3 start.py maps add <id>`. To rebuild a map that is already added,
remove it and add it again (`python3 start.py maps remove <id>`).
[docs/how-it-works.md](docs/how-it-works.md) explains both pipelines.

Give the holder, the page the file came from and its licence, and add the map to [NOTICE](NOTICE).
Only add maps that their holder lets anyone download.

## What not to commit

Downloads, intermediate files, the record database and added maps (`data/raw/`, `data/work/`,
`data/evidence.sqlite`, `web/tiles/`) are made on each computer (the records by `start.py`, the
maps when someone adds them) and are ignored by git. Never
commit `.env`, API keys, or personal details about anyone.

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), like the rest of the code.
