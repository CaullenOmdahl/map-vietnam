# Synthetic VHPA pages

These `.HTM` files are test fixtures for `scripts/ingest/vhpa.py` (exercised by
`tests/test_ingest_vhpa.py`). They are **synthetic, structure-matched stand-ins** for pages of the
Vietnam Helicopter Pilots Association (VHPA) KIA database: incident pages (`<incident id>.HTM`),
pilot pages (`K#####.HTM`), one panel page excerpt (`PANEL28E_excerpt.HTM`) and one stored HTTP error
body (`69040303KIA.HTM`).

**Never replace them with real pages.** The VHPA site is copyrighted and its pages name real
casualties. If a test needs a new page, write it by hand in the same form.

## What is kept

Each file uses the markup and labels the parser reads (`Crew Members:`, `UTM grid coordinates:`,
`Grid Reference`, `Casualties =`, `Number killed in accident =`, the coded outcome line
`The helicopter Crashed.`, `Status:`, `Location:`, `Reason:`, ...) and keeps the coded facts the tests
check: page ids and the links between pages, incident numbers, dates, aircraft types and tail numbers,
units, grids and zone hints, country lines, casualty and status codes, outcome lines, REFNO numbers,
loss coordinates, and the pilot-page status, cause, reason, location and vehicle lines. Parsing a
synthetic page gives the same facts as the page it stands in for, apart from the layout changes
listed below, and the ingester builds the same records from them (kind, position, radius, flags,
titles); only names and quoted summaries in the descriptions differ.

## What is not real

- **Names.** Every personal name is a made-up word built from the NATO alphabet (`PAPATON QUEBECTON
  ROMEOTON`). Each real name word maps to the same made-up word on every page, so a crew line still
  matches its man's pilot page. Burial lines read `SAMPLE MEMORIAL PARK, ANYTOWN`; the `(IMO)`
  (in memoriam) marker is kept where the parser needs it.
- **Free text.** Page summaries, pilot-page short summaries and notes, accident reports, REFNO
  synopses and war stories are short sentences written for these tests. They use the same kinds of
  words as the originals (crash words, ground-casualty words, doubt words, other aircraft's tail
  numbers, grids, lat/long, country names), so the parser reaches the same decisions. No sentence is
  copied from the source. `[not reproduced]` marks a section that held no usable text.
- **Layout.** Fields come in an order of our own, and lines the parser ignores are left out (site
  boilerplate, personal details such as birth dates and service numbers). Landing outcome lines read
  `Airframe Destroyed.`, incident number lines carry only the number, unnamed passengers are
  numbered (`X2 UNK #20`), and the error page is a generic `404 Not Found` body.
