# Known limits

Each record source was checked by an independent reviewer after every repair pass. The items
below were still open when review stopped (2026-11-03). Each one is a reason to read the source
page before acting on a record, not a reason to distrust a whole source. How each source is read
and placed is in [data-sources.md](data-sources.md).

## Coverage

- No old map is on the computer until you add it. The ones you can add cover almost all of the
  country at 1:50,000 (498 sheets) and nearly all of it at 1:250,000 (32 sheets). The seven older
  scans cover the Mekong Delta.
- About 110 land cells have no 1:50,000 sheet on the USGS Store, mostly along the China, Laos and
  Cambodia borders and on islands (Phú Quốc's south, Thổ Chu, Côn Đảo, Phú Quý, the Quảng Ninh
  islands). The 1:250,000 sheets fill most of them, but neither series covers the far north
  along the China border (Lào Cai, Hà Giang, northern Cao Bằng and Lạng Sơn), the area around
  Sơn La, western Nghệ An on the Laos border, southern Phú Quốc and Phú Quý. The full list is in
  [data-sources.md](data-sources.md#coverage).
- THOR air strikes are loaded for the Cao Lãnh area (105.35–105.95 E, 10.15–10.75 N) unless the
  THOR ingester is run with a wider `--bbox` or `--all-vietnam`.
- The other record sources cover all of Vietnam, and some reach across the borders.

## Old maps

- The 1960s US sheets are fitted to their own registration points (residuals 10–21 m) and sit
  within about 50 m of modern imagery.
- The 1:250,000 Joint Operations Graphics are less exact: expect about 100–200 m between a sheet
  and today's map. They are an overview; use the 1:50,000 sheet where there is one. Many are 1970s
  editions or later reprints, so check the date before reading one as a wartime map.
- The scanned maps are placed from printed graticules on surveys of their day. Errors of hundreds
  of metres are normal, and the 1873 and 1889 maps are off by 1–5 km in places. Each map's date
  note in the viewer gives what was measured.
- Some US sheets' georeferencing names a datum other than the Indian 1960 their margins print:
  `IND-I` and `IND-B` (the Indian datum of India and of Bangladesh) and `INF-A` (Indian 1954).
  Their corners match neighbouring Indian 1960 sheets to about 5 m, and the margins checked say
  Indian 1960, so they are treated as Indian 1960. Read as Indian 1954, an `INF-A` sheet would
  move about 60 m.
- Before a sheet is added, its outline in the Old maps card is its catalogue corners moved from
  Indian 1960 to WGS84. For the three 1:250,000 sheets printed on WGS84 that outline is about
  400 m off; once added, a map's outline is its own extent.
- Three 1:50,000 sheets are not offered: 6731 I, whose copies on the USGS Store are both broken
  (the start of the PDF, then zero bytes); 6836 I, whose georeferencing places it hundreds of
  kilometres away; and the Laos border sheet 5949 IV, whose PDF has no georeferencing.
- A few coastal sheets carry an inset (an island drawn apart, at its own place on the page). The
  main map is placed; the inset stays where it is printed on the sheet, so it does not line up
  with the island on today's map. If the archive changes a file, a download that is cut short or
  damaged is refused and the next address is tried; a file that differs from the catalogue but is
  whole is used, with a note.
- A 1:50,000 sheet takes about a minute and up to about 2 GB of memory to prepare; maps are added
  one at a time. A download is not resumed: a map cut short by a stop starts again from zero.
- Maps waiting to be added are remembered only while the map server runs. If it stops, the map
  being added is shown as interrupted when it starts again, and maps still waiting are dropped.

## Scope calls a user may want to overrule

- Memorials with an on-site mass grave or ossuary are dropped as known burials.
- Wikidata events are kept only if dated 1858–1989. Sites are judged by their wartime use, not
  their founding date.
- Present-day military installations, and anything inside one, are dropped. Wikidata events
  whose point falls inside one are kept with a note.
- VHPA losses in Thailand or deep inside Laos or Cambodia are dropped. Border-area losses are
  kept and marked as being outside Vietnam.
- CACCF service numbers are not stored.

## VHPA (helicopter losses)

The kind (crash or loss) and the position come from coded lines and short summaries. Free-text
war stories are read only for sentences that say this aircraft crashed, and only for a few
grid and lat/long forms. As a result:

- Some war stories give the place of the loss without a crash verb (a sentence that only says the
  loss happened near a given grid). Those places are not reached, so the 500 m circle can be
  1–6 km off. Seen in 69042810KIA, 69081444KIA, 700317161ACD, 71041131KIA, 68042522KIA,
  68042424KIA, 68041989KIA (body not recovered) and 69110777KIA.
- Compact lat/long like `162248N 1070700E` is not decoded. 68041920KIA (3 bodies not
  recovered) has one 2.85 km from its stored grid.
- 05976ACD's war story says the location is unknown and gives Dalat, 189 km away, but the
  record keeps a 500 m circle.
- A crash sentence about another aircraft type can still widen a radius
  (68050522KIA, a Skyraider sentence).
- "Crash not stated" means the parser found no crash wording. It does not mean the source never
  says the aircraft crashed.
- Pages that give a grid only in their war story (for example 0D159ACD, 68060777KIA) are skipped.

## Wikidata / OpenStreetMap

- Border notes do not name the neighbouring country, or say whether the border is on land or
  at sea.
- When Wikidata lists several countries for an event, the note names only the ones it
  recognises. For example, Khe Sanh's note leaves out South Korea.
- The 25 km radius for multi-day naval items (Gulf of Tonkin) is a project default, not a
  figure from Wikidata.

## Vietnamese relic list

- Province and district radii are estimated from gazetteer points, not measured boundaries.
  Province-wide records may fall a few hundred metres short at the far edge.
