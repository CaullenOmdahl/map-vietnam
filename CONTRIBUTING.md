# Contributing

Thank you for helping. Corrections to records, new maps, new sources and fixes are all welcome.

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

See "Adding a map" in the [README](README.md#adding-a-map). Give the holder, the page the file
came from and its licence, and add the map to [NOTICE](NOTICE). Only add maps that their holder
lets anyone download.

## What not to commit

Downloads, intermediate files, the record database and added maps (`data/raw/`, `data/work/`,
`data/evidence.sqlite`, `web/tiles/`) are made on each computer (the records by `start.py`, the
maps when someone adds them) and are ignored by git. Never
commit `.env`, API keys, or personal details about anyone.

## Licence

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), like the rest of the code.
