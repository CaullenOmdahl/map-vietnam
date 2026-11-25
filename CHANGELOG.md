# Changelog

Notable changes to Map Vietnam. Versions follow [semantic versioning](https://semver.org).

## 0.2.0 (Unreleased)

Settings
- Each control now lives in exactly one place: the base map is switched from the style popover
  (now titled **Map style**), the appearance theme lives in **Settings**, the old-map outlines
  Show/Hide sits in the **Old maps** card, and the Kaggle key is managed in **Settings** (the key
  box still appears in Records when a download asks for one)

Zooming and panning old maps
- While the map sits idle, the tiles just around what is on screen — and the same area one zoom in and
  one out — are quietly fetched ahead from the local server, so the next pan or zoom draws almost at
  once. Only old maps installed on this computer are fetched ahead; the street and satellite base maps
  are never fetched ahead (their providers forbid it), they just keep more of what you have already seen
  in memory

Trackpad navigation & overlay glass
- Multi-touch trackpads now work like Apple Maps across the main map, swipe comparison and custom-map
  placement: two-finger drag pans the map and pinch or stretch zooms smoothly around the pointer.
  Physical notched mouse wheels still zoom automatically, and **Settings › Scrolling** lets you choose
  Automatic, Trackpad or Mouse wheel explicitly
- Floating UI overlay backgrounds (sidebar, map bar, map controls, popovers and map badges) now use
  60% opacity frosted glass in light and dark themes
- Enlarged MapLibre's in-memory tile cache and added idle zoom-out parent tile preloading for local
  old maps so zooming out no longer reloads recently viewed tiles
- Added macOS Force Touch trackpad haptics (alignment ticks on opacity 0%/50%/100%, swipe-compare
  centre snap, compass reset, native scan resolution threshold and custom-map placement) plus Force
  Click shortcuts (X-ray peek through old maps, quick-pinning records and instant spot drops); silent
  no-op on Windows, Linux and web browsers

## 0.1.1 - 2026-11-24

Your own maps
- **Settings › My maps › Add your own map** takes a file from your computer. A GeoTIFF or a
  georeferenced PDF is placed for you; a scan or photo (JPG, PNG, TIFF or PDF) is placed on a
  new screen where you click a spot on your map, then the same spot on today's map. From three
  pairs it previews your map over today's, with how far off the spots are and a see-through slider.
  Your maps show, swipe and fade like the library's, labelled "Your map", and can be renamed,
  placed again or removed. The uploaded file is kept on this computer only

Settings
- A **Settings** panel (the gear button, or Settings at the foot of the side panel): today's map,
  appearance, old map outlines, the Kaggle key, your own maps, and where your data is kept and how
  much space it takes
- Adding maps no longer tints the whole area amber: sheets are outlined, and only the one pointed
  at or chosen is shaded. The outlines can be hidden (**Outlines: Hide** in the Old maps card)
- The Kaggle key can be added or forgotten at any time (Settings, or the air strikes row) instead
  of only after Kaggle refuses a download

Downloads
- The Linux download no longer bundles the system's C++, graphics, glib and font libraries: on
  distributions newer than Ubuntu 22.04 the old copies stopped the graphics driver loading and the
  app closed at start
- The Mac download is a `.dmg`: open it and drag Map Vietnam onto Applications. The README has
  step-by-step first-open instructions for macOS 15 and newer

## 0.1.0 - 2026-11-24

Everything is done from the map
- `python3 start.py` now asks nothing and downloads nothing except its private Python add-ons.
  The first visit offers the standard records in one click, and a new **Records** card in the
  side panel lists every source with Add, Stop, Retry, Update and Remove. Air strikes are added
  for the area on screen, helicopter losses after their warning, and a Kaggle key is taken from
  the `kaggle.json` file if Kaggle ever asks for one. **Quit Map Vietnam** stops the app
- Records and old maps added from a terminal (`start.py setup`, `maps`, `records`) and from the
  map share their state; the terminal route stays for people who script it

Installers
- A Windows installer (per user, no administrator needed), a Linux `.tar.gz` and a Mac `.dmg`,
  all built from one Linux machine with `packaging/build.sh` (Docker; Windows under Wine, the
  installer with NSIS; the Mac app over ssh on a Mac) and smoke-tested, installer included. The installed app keeps what it downloads in a per-user data folder, kept when
  you update and asked about when you uninstall; `MAPVIETNAM_HOME` moves it. Nothing is
  code-signed; docs/troubleshooting.md explains SmartScreen and macOS' first-open step
- A new app icon: Vietnam's outline, half on an old paper map and half on today's map, split by the
  viewer's compare handle (`packaging/make_icons.py`); the small sizes and the favicon are simplified
  so they still read at 16 px, and the Linux window now shows it too

Its own window
- Map Vietnam now opens in its own window instead of a browser tab (`scripts/desktop.py`, using
  pywebview). Links to record sources open in your browser, exports show a Save dialog, and closing
  the window stops the app like **Quit Map Vietnam**. Where no window can open, the browser is used
- `python3 start.py --yes` (and the other setup options) with no command now runs `setup`
- The old map's credit no longer runs under the map bar on a laptop screen (it wraps beside the bar or
  sits above it), panel scrollbars are thin and themed on Linux and Windows, and Linux no longer
  prints a GTK import traceback before opening its Qt window

A bigger map library
- The library grows from 537 to 895 maps: 498 US Army L7014 sheets at 1:50,000, 298 French
  Indochina 1:100,000 sheets and editions (1895–1952, CNRS/NAKALA), 34 Joint Operations Graphics
  at 1:250,000, 32 US Army L509 sheets at 1:250,000 (1950s–1960s, University of Texas), 18 US Army
  city plans at 1:12,500 (1963–1971), 8 British Admiralty charts of the coast (1855–1964) and 7
  older scans
- The French and L509 sheets are placed automatically from their printed graticule
  (`scripts/sheet_lists.py`, `scripts/place_sheets.py`); sheets it cannot place reliably are left
  out rather than shown wrong
- The **Old maps** card has a filter (place, sheet number or year, across every map), scale
  chips with counts, Best here / Oldest / Newest order, and one row per sheet with its editions
  inside
- Downloads name the project's page in their User-Agent, as Wikimedia and others ask

Windows and hardening
- Windows: every text file is read and written as UTF-8, so setup and the map server no longer
  fail on Vietnamese names; Control-C stops the map; removing a map that is being viewed retries
  instead of failing
- The map credits each old map on screen, and the OpenStreetMap and Esri credits follow their
  providers' wording
- The viewer explains when MapLibre can't be loaded (offline or blocked) instead of showing a
  blank page, and ignores a damaged saved view
- The server refuses non-finite or out-of-range site positions, wrongly typed fields and negative
  limits with a 400, and checks the Host header for every file it serves

Docs
- macOS 15 Gatekeeper steps, `py` on Windows, disk space, backups, uninstalling, and a pass to
  make the README, guide and sources pages agree with the code and the catalogue

## 0.1.0 (2026-11-05)

First public version.

- Viewer with old maps over today's map, a fade slider and a side-by-side swipe
- Map library of 498 US Army 1:50,000 sheets, 32 overview sheets at 1:250,000 and 7 older
  scans, added and removed one at a time from the viewer or with `python3 start.py maps`
- Records from the US National Archives, the NGA place-name server, Wikipedia, Wikidata and
  OpenStreetMap, with air strikes for an area you choose (Kaggle) and optional helicopter losses
  (VHPA). Old maps are downloaded only when you add them
- Search by place name or wartime grid reference, on the Indian 1960 or WGS84 datum
- Cases with GeoJSON, KML and GPX export
- One command, `python3 start.py`, to set up and start it
