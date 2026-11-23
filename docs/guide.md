# User guide

How to use Map Vietnam once it's running. To get it running, see the [README](../README.md#install).

## Adding old maps

Setup doesn't download any old maps. You add the ones you want, for the places you care about.

Go to a place and click **Add old maps** at the bottom of the screen. Once you have some maps,
the same list is under **Old maps** in the side panel. You get every map that covers what's on
screen, with its size, and outlines on the map showing where each one sits. Click **Add** on the
ones you want. A map takes about a minute and appears on its own when it's ready. You don't have
to wait with the list open, because the bar at the bottom shows progress.

![The Old maps list open beside the map, with outlines on the map showing where each old map lies](images/add-old-maps.jpg)

The switch at the top of the list narrows it to the detailed 1:50,000 sheets, the 1:250,000
overview sheets or the older scans.

A detailed US Army sheet (1:50,000, roughly 27 km on a side) is about 10 MB to download and
about 20 MB on disk, because the download is kept so the map can be rebuilt. All 498 of them come
to about 5 GB of downloads and 11 GB on disk.

## Removing maps

Open **Old maps**, click the map, choose **Remove** and confirm. Its tiles and its download are
deleted. You can add it again later.

## Doing it from a terminal

Run these in the Map Vietnam folder. On Windows, type `py` instead of `python3`.

```sh
python3 start.py maps list --area 105.4,10.2,105.9,10.6   # maps of an area (west,south,east,north)
python3 start.py maps list --region south                 # north, centre or south
python3 start.py maps list --installed                    # the maps you have added
python3 start.py maps add L701461294                      # add one map by its id
python3 start.py maps add --area 105.4,10.2,105.9,10.6    # add every map touching an area
python3 start.py maps remove L701461294                   # remove one map
python3 start.py maps remove --all                        # remove every map you added
```

`maps add` shows the count and download size and asks before it starts.

## Choosing which map is shown

The bar at the bottom names the old map that's showing. Click it to pick another, oldest first,
or pick **Today** to turn old maps off. The slider next to it fades the old map in and out. It
starts at 60%.

![The map picker open, listing the old maps added for this area, from an 1873 French chart to a 1986 US overview sheet](images/choose-an-old-map.jpg)

## Comparing two maps

The button at the right end of the bar splits the screen. Drag the handle in the middle to wipe
between the left map and the right one.

![The screen split down the middle: an 1873 French chart of Cao Lãnh on the left, the 1965 US Army sheet on the right](images/swipe-compare-1873-1965.jpg)

This is Cao Lãnh in 1873 and in 1965. The river didn't move that far. The 1873 survey was just
that rough.

## Searching

The search box takes place names, with or without Vietnamese accents, and grid references copied
from wartime reports, like `WS683427`. A grid reference names a square rather than a point, so a
square is what gets drawn.

![A 100 m grid square marked on the 1965 map, with a card that explains the square and lists nearby records](images/wartime-grid-reference.jpg)

## Records and cases

Click any record to open its card. It shows the coordinate exactly as the source wrote it, where
that falls on today's map, the date, and a link to the original. The ring around the record on
the map is how far off the position could be.

![A record card for Sa Dec Base: the original coordinate, today's position, a 3 km accuracy ring, the date and the source](images/record-card.jpg)

Records you want to keep go into a case, which is a named collection of records, places and
notes. A case exports to GeoJSON, KML or GPX, for a GPS or Google Earth.

## Small windows

In a narrow window the side panel turns into a sheet you drag up from the bottom, the way a phone
map app does it.

<img src="images/phone-layout.jpg" width="260" alt="The phone layout: the 1965 map fills the top, the map bar floats above a sheet that shows a grid-square card">

## Air-strike records

The US Air Force strike records (THOR) are hosted on Kaggle, which wants a free account before it
lets you download anything. Everything else works without one.

1. Sign in or register at [kaggle.com](https://www.kaggle.com).
2. Click your picture at the top right, then **Settings**.
3. Under **API**, click **Create Legacy API Key**. Older versions of the page call it **Create New
   Token**. Your browser downloads a file called `kaggle.json` with a username and a key in it.
4. Run `python3 start.py setup --only thor` and paste the two values in when it asks.

If you'd rather not paste, copy `.env.example` to `.env` and fill in the two lines, or put
`kaggle.json` in a `.kaggle` folder in your home folder. Whichever you pick, the key stays on
your computer. The download is about 320 MB, and only the strikes around Cao Lãnh are loaded
unless you ask for more. [data-sources.md](data-sources.md) says how.

## Helicopter losses

These are off by default. They come from the Vietnam Helicopter Pilots Association website,
which is copyrighted, so Map Vietnam keeps only short facts from it, for personal research. It
also reads the site slowly so as not to be a nuisance: one page every three seconds, about four
hours in all. Turn it on with `python3 start.py setup --only vhpa`.

## Disk space, backups and starting over

Setup needs about 1 GB, plus about 200 MB for its private Python in `.venv/`. The air-strike
records add about 0.5 GB. Each old map adds about 20 MB (a 1:250,000 sheet about 30 MB), and all
of them together come to about 12 GB.

Your cases, sites and notes are kept only in `data/evidence.sqlite`, next to the records. To back
them up, stop the map (Control-C) and copy that file. Export a case as GeoJSON, KML or GPX to keep
it in a form other tools can read.

Everything Map Vietnam downloads or makes stays inside its folder:

| Path | Holds |
| --- | --- |
| `.venv/` | its private Python and add-ons |
| `data/raw/` | the downloaded records and old-map files |
| `data/work/` | intermediate files, setup logs and your first-run answers |
| `data/evidence.sqlite` | the records, and your cases and notes |
| `web/tiles/`, `web/catalog.json` | the old maps you have added |
| `.env` | your Kaggle key, if you saved one |

To ask the first-run questions again, delete `data/work/setup/choices.json`. To uninstall, delete
the folder (and `~/.kaggle/kaggle.json` if you put a Kaggle key there and no longer want it).

## Running without questions

`python3 start.py --yes` sets up without asking anything, leaving the optional parts off.
`--no-browser` doesn't open a browser window, `--port` picks another port, and
`python3 start.py serve` opens the map without checking setup. `python3 start.py --help` lists
everything.

## What goes over the internet

The map is served to your own machine only (`127.0.0.1`), so nobody else on your network can
open it. Your cases and notes live in `data/evidence.sqlite`. They are never sent anywhere, and
neither are your searches.

Setup downloads the records from their publishers, and each old map is downloaded from its
archive when you add it. While you use the map, the page also loads background tiles for today's
map (OpenStreetMap and Esri), the MapLibre map library (unpkg.com), and fonts (Google Fonts, and
Protomaps on GitHub). Those sites see your IP address, as any website you visit does.
