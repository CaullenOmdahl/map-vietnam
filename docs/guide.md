# User guide

How to use Map Vietnam once it's running. To get it running, see the [README](../README.md#install).

## Adding records

Map Vietnam starts empty. The first time it opens, a card in the side panel offers the standard
records: place names, wartime hamlets, bases, battles and casualty records. Click **Add the
standard records**. It's about 70 MB from public archives and takes about 15 minutes. You can
use the map meanwhile, and the records appear on it as each source finishes.

![The Records card listing the sources on this computer with their record counts, and the ones that can still be added](images/add-records.jpg)

**Records** in the side panel lists every source. Click a source to read what it is, how big it
is and its licence. **Add** downloads it, the ring that replaces the button stops it, and
**Retry** tries again if a download failed. What was already downloaded is kept, so a retry
picks up where it left off.

Two sources aren't in the standard set, because each needs a choice from you.
[Air strikes](#air-strike-records) are added for an area you pick, and
[helicopter losses](#helicopter-losses) take hours.

## Removing records

Open **Records**, click the source, choose **Remove** and confirm. Its records leave the map and
its downloads are deleted. Records you've put in a case stay in the case, marked as withdrawn.
**Update**, on the standard sources, downloads one again for corrections its publisher has made
since.

## Adding old maps

No old map is downloaded until you add it. You add the ones you want, for the places you care
about.

Go to a place and click **Add old maps** at the bottom of the screen. Once you have some maps,
the bar shows the one you are viewing, and the same list is under **Old maps** in the side panel
(or **Add old maps of this area** in the bar's map picker). You get every map that covers what's on
screen, with its size, and outlines on the map showing where each one sits. Click **Add** on the
ones you want. A map takes about a minute and appears on its own when it's ready. You don't have
to wait with the list open, because the bar at the bottom shows progress.

![The Old maps list open beside the map, with outlines on the map showing where each old map lies](images/add-old-maps.jpg)

To find a map somewhere else, type a town, a sheet number or a year in the box at the top of the
list ("Huế", "feuille 221", "1952"): it searches every map, not just the ones on screen. The
chips under it narrow the list to one scale, from town plans to 1:250,000, and show how many maps
each has. The list starts with the best match for the place on screen; **Oldest** and **Newest**
put it in date order. When a sheet was printed more than
once, its editions share one row: **Add** takes the newest, and opening the row lists the others.

A detailed US Army sheet (1:50,000, roughly 27 km on a side) is about 10 MB to download and
about 20 MB on disk, because the download is kept so the map can be rebuilt. All 498 of them come
to about 5 GB of downloads and 11 GB on disk. The whole library, 895 maps, is about 9 GB of
downloads and 21 GB on disk. Nobody needs all of it.

## Removing maps

Open **Old maps**, click the map, choose **Remove** and confirm. Its tiles and its download are
deleted. You can add it again later.

## Doing it from a terminal

None of this is needed, but everything above can be scripted. This is for running from source: the
installed app has no `start.py`. Run these in the source folder. On Windows, type `py` instead of
`python3`. `setup` and `maps add` ask before they download; add `--yes` to skip the question.

```sh
python3 start.py setup                                    # add the standard records
python3 start.py setup --only thor --area 105.4,10.2,105.9,10.6   # air strikes for an area
python3 start.py setup --only vhpa                        # helicopter losses
python3 start.py records list                             # the sources and what's added
python3 start.py records remove vhpa                      # remove one source
python3 start.py maps list --area 105.4,10.2,105.9,10.6   # maps of an area (west,south,east,north)
python3 start.py maps list --region south                 # north, centre or south
python3 start.py maps list --installed                    # the maps you have added
python3 start.py maps add L701461294                      # add one map by its id
python3 start.py maps add --area 105.4,10.2,105.9,10.6    # add every map touching an area
python3 start.py maps remove L701461294                   # remove one map
python3 start.py maps remove --all                        # remove every map you added
```

`maps add` shows the count and download size before it starts. The terminal and the map share
everything, so a source or map added one way shows up the other way.

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

The US Air Force strike records (THOR) cover the whole war, about three million strikes, which
is far more than a map can draw. So you add them for one area at a time.

Move and zoom the map until it shows the area you want. Open **Records**, click **Add** next to
**Air strikes**, then **Add strikes for this area**. The first time, it downloads one 320 MB
file, which takes about ten minutes. After that, changing the area takes a minute or two: open
**Air strikes** and click **Change area**. The new area's strikes replace the old ones.

An area can hold up to 150,000 strikes. If yours holds more, nothing is changed and you're asked
to pick a smaller one. Around the former Demilitarized Zone that can mean zooming in to a single
district.

The file comes from Kaggle. At the time of writing Kaggle hands it over without an account. If
it ever asks for a key, the **Air strikes** row says so and shows what to do:

1. Sign in or register at [kaggle.com](https://www.kaggle.com). It's free.
2. Click your picture at the top right, then **Settings**.
3. Under **API**, click **Create Legacy API Key**. Older versions of the page call it **Create New
   Token**. Your browser downloads a file called `kaggle.json`.
4. Back in Map Vietnam, click **Choose kaggle.json** and pick that file.

The key is saved on your computer, in a file called `.env` in Map Vietnam's data folder (see
[Disk space, backups and starting over](#disk-space-backups-and-starting-over)), and is
only ever sent to Kaggle. **Forget the saved key** on the same row deletes it.

## Helicopter losses

These aren't in the standard set. They come from the Vietnam Helicopter Pilots Association
website, which is copyrighted, so Map Vietnam keeps only short facts from it, for personal
research. It also reads the site slowly so as not to be a nuisance: one page every three
seconds, about four hours in all.

Open **Records** and click **Add** next to **Helicopter losses**. It tells you the above and
asks you to confirm. You can stop it and carry on another day. The pages it has read are kept.

## Quitting

Close the window, or click **Quit Map Vietnam** at the bottom of the side panel, then **Quit**.
Anything still being added stops, and what was already downloaded is kept. To start again, open
Map Vietnam from the Start menu (Windows), Applications (macOS) or the program you unpacked
(Linux).

From source, closing the terminal window does the same. To start again, double-click **Start Map
Vietnam.bat** (Windows) or **Start Map Vietnam.command** (macOS), or run `python3 start.py`.

## Disk space, backups and starting over

Setup from source needs about 1 GB, plus about 200 MB for its private Python in `.venv/`.
The air-strike records add about 0.5 GB. Each old map adds
about 20 to 30 MB, and all 895 of them together come to about 21 GB.

### The installed app

Everything Map Vietnam downloads or makes goes in a data folder of your own, apart from the
program:

| Platform | Data folder |
| --- | --- |
| Windows | `%LOCALAPPDATA%\Map Vietnam` |
| macOS | `~/Library/Application Support/Map Vietnam` |
| Linux | `$XDG_DATA_HOME/map-vietnam`, or `~/.local/share/map-vietnam` |

Updating the app leaves it alone. Set `MAPVIETNAM_HOME` to a folder to keep the data there
instead.

Your cases, sites and notes are kept only in `data/evidence.sqlite` in that folder, next to the
records. To back them up, quit Map Vietnam and copy that file. Export a case as GeoJSON, KML or
GPX to keep it in a form other tools can read.

| Path in the data folder | Holds |
| --- | --- |
| `data/raw/` | the downloaded records and old-map files |
| `data/work/` | intermediate files, setup logs and small settings such as the first-run card |
| `data/evidence.sqlite` | the records, and your cases and notes |
| `web/tiles/`, `web/catalog.json` | the old maps you have added |
| `.env` | your Kaggle key, if you saved one |

`data/work/setup/choices.json` remembers that you dismissed the first-run card; delete it, with no
records added, to see the card again.

To uninstall:

- Windows: **Settings > Apps > Map Vietnam > Uninstall**. It asks whether to delete your maps and
  records too (the data folder); answer No to keep them for a later install.
- macOS: delete **Map Vietnam** from Applications, then the data folder.
- Linux: delete the folder you unpacked, then the data folder.

If you saved a Kaggle key with Kaggle's own tools, it is in `~/.kaggle/kaggle.json`, outside the
data folder. Delete it too if you no longer want it.

### Running from source

The same files live inside the checkout, and the program adds `.venv/`, its private Python and
add-ons. Set `MAPVIETNAM_HOME` to keep the data somewhere else.

| Path | Holds |
| --- | --- |
| `.venv/` | its private Python and add-ons |
| `data/raw/`, `data/work/`, `data/evidence.sqlite` | as above |
| `web/tiles/`, `web/catalog.json` | the old maps you have added |
| `.env` | your Kaggle key, if you saved one |

To back up your cases, stop the map (Control-C, or **Quit Map Vietnam**) and copy
`data/evidence.sqlite`. To uninstall, delete the folder (and `~/.kaggle/kaggle.json` if you put a
Kaggle key there and no longer want it).

## Other options (from source)

`python3 start.py` never asks a question. `--no-window` (or `--no-browser`) doesn't open the map's
window: it only serves the map and prints its address, for a browser or a script. `--port` picks
another port. `python3 start.py serve` does the same as no command. `python3 start.py --help` lists
everything. Options for adding records (`--yes`, `--only`, `--skip`, `--area`, `--refresh`) given
without a command run `setup`.

## What goes over the internet

The map is served to your own machine only (`127.0.0.1`), so nobody else on your network can open
it. Your cases and notes live in `data/evidence.sqlite` in your
[data folder](#disk-space-backups-and-starting-over). They are never sent anywhere, and neither
are your searches.

Each record source is downloaded from its publisher when you add it, and each old map from its
archive. While you use the map, the page also loads background tiles for today's
map (OpenStreetMap and Esri), the MapLibre map library (unpkg.com), and fonts (Google Fonts, and
Protomaps on GitHub). Those sites see your IP address, as any website you visit does.
