# Changelog

Notable changes to Map Vietnam. Versions follow [semantic versioning](https://semver.org).

## Unreleased

- Windows: every text file is now read and written as UTF-8, so setup and the map server no longer
  fail on Vietnamese names; Control-C stops the map; removing a map that is being viewed retries
  instead of failing; CI now runs on Windows too
- The map now credits each old map on screen, and the OpenStreetMap and Esri credits follow their
  providers' wording
- The viewer explains when MapLibre can't be loaded (offline or blocked) instead of showing a blank
  page, and ignores a damaged saved view
- The server refuses non-finite or out-of-range site positions, wrongly typed fields and negative
  limits with a 400, and checks the Host header for every file it serves
- Docs: macOS 15 Gatekeeper steps, `py` on Windows, disk space, backups and uninstalling

## 0.1.0 (2026-11-05)

First public version.

- Viewer with old maps over today's map, a fade slider and a side-by-side swipe
- Map library of 498 US Army 1:50,000 sheets, 32 overview sheets at 1:250,000 and 7 older
  scans, added and removed one at a time from the viewer or with `python3 start.py maps`
- Records from the US National Archives, the NGA place-name server, Wikipedia, Wikidata and
  OpenStreetMap, with optional air strikes (Kaggle) and helicopter losses (VHPA)
- Search by place name or wartime grid reference, on the Indian 1960 or WGS84 datum
- Cases with GeoJSON, KML and GPX export
- One-command setup with `python3 start.py`
