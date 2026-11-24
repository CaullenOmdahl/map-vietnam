# Contributing

Thank you for helping. Corrections to records, new maps, new sources and fixes are all welcome.
Please read the [code of conduct](CODE_OF_CONDUCT.md) first. It's short.

## Reporting a problem with a record or a map

Open an issue with the record's source and record id (shown on its card), or the map's name, and
what the source actually says. A link to the source page helps most. Please do not paste long
passages of copyrighted text.

## Working on the code

1. Set up once: `python3 start.py setup --yes` (it makes `.venv/` and adds the standard records;
   `python3 start.py` makes `.venv/` alone), then `.venv/bin/python -m pip install ruff`. The
   tests need no records and no old map. Add them from the viewer, or add a map with
   `python3 start.py maps add <id>`, when you want to see them.
2. Make your change on a branch.
3. Before you open a pull request, run:

   ```sh
   .venv/bin/python -m pytest -q
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   ```

   On Windows the venv's programs are in `.venv\Scripts\` instead of `.venv/bin/`
   (`.venv\Scripts\python -m pytest -q`). CI runs the same three checks on Linux, macOS and
   Windows.

Style, in short:

- Python 3.12 or newer, formatted with `ruff format` (line length 120).
- Read and write text files with `encoding="utf-8"`. Windows' default encoding can't hold
  Vietnamese, and the Windows CI job fails without it.
- Each module starts with a docstring saying what it is for, what it reads and writes, and how to
  run it. Public functions have docstrings.
- Comments explain intent, units, datums and trade-offs, not what the next line does.
- `start.py` uses only the standard library, because it runs before `.venv` exists.
- Behaviour changes come with a test. Bug fixes come with a test that fails without the fix.

## Where things are

```text
start.py                    makes .venv, opens the map's window; also setup, maps, records
Start Map Vietnam.bat       Windows double-click launcher for start.py
Start Map Vietnam.command   macOS double-click launcher for start.py
requirements.txt            pinned Python dependencies
requirements-qt.txt         optional Linux window toolkit (installed best-effort; pywebview itself pulls
                            in proxy-tools, a pure-Python sdist, so a wheels-only install cannot work)
scripts/
  records.py                the record sources: adds and removes them (the viewer's Records card,
                            python3 start.py setup / records ...)
  library.py                the map library: adds and removes old maps (python3 start.py maps ...)
  files.py                  the lock and JSON-file helpers those two share
  map_library_catalogue.py  builds data/sources/map_library.json, every map that can be added
  download_maps.py          fetches one map's file from the addresses in that catalogue
  l7014_meta.py             reads title, date and datum from a US GeoPDF (kept in its map's metadata)
  build_tiles.py            a GeoPDF sheet -> web/tiles/<id>.mbtiles
  georef_scan.py            a scanned map + control points (data/scans/<id>.json) -> web/tiles/<id>.mbtiles
  sheet_lists.py            lists the sheets of the regular scanned series (French 1:100,000, US L509,
                            Admiralty charts) -> data/sources/sheets.json
  place_sheets.py           places those sheets from their printed graticule -> data/scans/<id>.json
  tiling.py, datums.py      shared tiling (WebP tiles in one MBTiles file per map) and datum code
  catalog.py                web/tiles/ -> web/catalog.json, the viewer's list of installed maps
  gridref.py                wartime MGRS grid references on Indian 1960 or WGS84
  store.py                  the SQLite evidence store, data/evidence.sqlite
  server.py                 local web server and JSON API
  desktop.py                the server plus the map's own window (pywebview); start.py runs it, and
                            it is the packaged app's entry point
  paths.py                  APP_ROOT and DATA_ROOT, and how child scripts are started (see below)
  compare_overlay.py        side-by-side image to check how well a map lines up
  ingest/<source>.py        one ingester per record source; common.py holds shared helpers
packaging/                  PyInstaller spec, icons and generator, smoke test, Windows installer script
web/app/                    the viewer: index.html, app.js, app.css (MapLibre GL, no build step)
tests/                      pytest suite; tests/fixtures/ holds small source excerpts
docs/                       user guide, troubleshooting, how it works, data sources, known limits
```

`scripts/paths.py` keeps the program's own files (`APP_ROOT`) apart from what the app writes
(`DATA_ROOT`). `DATA_ROOT` is `MAPVIETNAM_HOME` if set; otherwise the per-user data folder in a
packaged app (see [docs/guide.md](docs/guide.md#disk-space-backups-and-starting-over)); otherwise
the checkout. Use these, not paths relative to the working folder.

Downloads go to `data/raw/`, intermediate files to `data/work/`, records to `data/evidence.sqlite`
and the maps a person added to `web/tiles/` (all under `DATA_ROOT`). All of these are kept out of
git: records and maps are downloaded again when someone adds them again.

## Building the desktop app

The installers are built from `packaging/` (PyInstaller spec, icons, Windows installer script).
Locally, on the platform you want to build for, from the repository root:

```sh
.venv/bin/python -m pip install pyinstaller==6.22.3     # Linux: also -r requirements-qt.txt
.venv/bin/pyinstaller packaging/map-vietnam.spec --noconfirm
.venv/bin/python packaging/smoke_test.py "dist/Map Vietnam/Map Vietnam"
```

That makes `dist/Map Vietnam/` (`dist/Map Vietnam.app` on a Mac) and checks it starts, serves the
viewer and the library, runs a child script and quits. On Windows the program is
`dist\Map Vietnam\Map Vietnam.exe` and the installer is then
`iscc /DAppVersion=<version> packaging\windows\map-vietnam.iss` (Inno Setup 6), written to
`dist\installer\`. `build/` and `dist/` are ignored by git. The icon is drawn by
`packaging/make_icons.py`; run it and commit `packaging/icons/` when the design changes.

To release: change `version` in `pyproject.toml` (the one place it is written), merge, then

```sh
git tag v0.1.0 && git push origin v0.1.0
```

`.github/workflows/release.yml` builds Windows, Linux and macOS, smoke-tests each and attaches the
Windows installer, a Linux `.tar.gz` and an unsigned macOS `.zip` to the GitHub Release. The tag
must equal `v` plus the version in `pyproject.toml`. Run the workflow by hand (Actions > Release >
Run workflow) to build without releasing. Nothing is code-signed; users get the steps in
[docs/troubleshooting.md](docs/troubleshooting.md).

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
- is added to `SOURCES` in `scripts/records.py`, which is what lists it in the viewer, and is
  described in [docs/data-sources.md](docs/data-sources.md).

## Adding a map

Every map that can be added is listed in `data/sources/map_library.json`, which
`scripts/map_library_catalogue.py` generates (do not edit it by hand):

- **US GeoPDF sheets** (1:50,000 and 1:250,000 sheets and 1:12,500 city plans) come from a probe
  of the USGS store and the PDFs' own metadata; see the script's docstring. Each sheet is placed
  by the georeferencing inside its GeoPDF and cut at its frame.
- **A regular scanned series** (the French 1:100,000 sheets, the US L509 sheets, the Admiralty
  charts): `scripts/sheet_lists.py` lists the sheets in `data/sources/sheets.json`, and
  `scripts/place_sheets.py` places each from its printed graticule and writes
  `data/scans/<id>.json`. The catalogue includes only sheets that have such a file.
- **A single scanned map:** add an entry to the `scans` list in `data/sources/maps.json` (the script
  copies it into the catalogue), write `data/scans/<id>.json` with at least three control points
  (pixel to longitude and latitude) and the map's title, date and source.

Then regenerate the catalogue with `.venv/bin/python scripts/map_library_catalogue.py` and add the
map with `python3 start.py maps add <id>`. To rebuild a map that is already added,
remove it and add it again (`python3 start.py maps remove <id>`).
[docs/how-it-works.md](docs/how-it-works.md) explains both pipelines.

Give the holder, the page the file came from and its licence, and add the map to [NOTICE](NOTICE).
Only add maps that their holder lets anyone download.

## What not to commit

Downloads, intermediate files, the record database and added maps (`data/raw/`, `data/work/`,
`data/evidence.sqlite`, `web/tiles/`) are made on each computer, when someone adds records or a
map, and are ignored by git. Never commit `.env`, API keys, or personal details about anyone.

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), like the rest of the code.
