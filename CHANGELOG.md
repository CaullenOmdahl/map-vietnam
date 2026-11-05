# Changelog

Notable changes to Map Vietnam. Versions follow [semantic versioning](https://semver.org).

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
