# Map Vietnam

[![CI](https://github.com/CaullenOmdahl/map-vietnam/actions/workflows/ci.yml/badge.svg)](https://github.com/CaullenOmdahl/map-vietnam/actions/workflows/ci.yml)
[![Licence: Apache 2.0](https://img.shields.io/badge/licence-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

Old maps of Vietnam on top of today's map, with war-era records you can search and pin. It runs
on your own computer and opens in its own window.

![A 1965 US Army map of the area south of Cao Lãnh laid over today's street map, with orange air-strike points and labelled hamlets](docs/images/hero-cao-lanh-1965-map.jpg)

## Features

- Dated old maps, the earliest from 1855, faded over today's street map or satellite view
- About 900 maps covering nearly all of Vietnam: 1960s US Army sheets and city plans, French
  Indochina sheets from 1895 to 1952, and British Admiralty charts of the coast. You add the
  ones you want and nothing else is downloaded
- Find maps by place, sheet number or year, sort them by date, and see every edition of a sheet
- Swipe between any two maps side by side
- Search by place name or by a wartime grid reference such as `WS683427`
- Public war-era records: air strikes, aircraft losses, casualty records, hamlets, bases, battles
- Every record shows the coordinate as its source wrote it, how far off it could be, and a link
  to the source
- Collect records and your own pins into a case and export it as GeoJSON, KML or GPX
- Your searches, pins and notes stay on your computer

## Install

You need Python 3.12 or newer, free from [python.org/downloads](https://www.python.org/downloads/).
On a Mac you also need macOS 15 or newer.

1. Download this project. On GitHub that's the green **Code** button, then **Download ZIP**, then
   unzip it. Or `git clone` it if you use git.
2. On Windows, open the folder and double-click **Start Map Vietnam.bat**. On a Mac,
   double-click **Start Map Vietnam.command**. On Linux, open a terminal in the folder and run:

   ```sh
   python3 start.py
   ```

   (On Windows or a Mac you can also run it from a terminal: `py start.py` on Windows.) When you
   install Python on Windows, tick **Add python.exe to PATH**. On Debian or Ubuntu you may first
   need `sudo apt install python3-venv`.

The first start takes a minute or two while it installs what it needs, then the map opens in
its own window. Everything after that is done in the map: there is nothing more to type. Keep the
terminal window open while you use it. To stop, click **Quit Map Vietnam** at the bottom of the
side panel, or close the map's window. Links to record sources open in your web browser. (On a
Linux computer that has no Qt or GTK web view, the map opens in your browser instead.)

The first time, macOS may refuse to open the `.command` file because it came from the internet
("Apple could not verify..."). Click **Done**, open **System Settings > Privacy & Security**,
scroll down and click **Open Anyway** next to the file's name, then confirm. You only do this
once. Running `python3 start.py` in Terminal works too. On Windows, SmartScreen may say
"Windows protected your PC" about the `.bat` file: click **More info**, then **Run anyway**.

If something goes wrong, see [docs/troubleshooting.md](docs/troubleshooting.md).

## Usage

Nothing is downloaded until you ask for it. The first time, a card in the side panel offers the
standard records: place names, hamlets, bases, battles and casualty records, about 70 MB from
public archives. Click **Add the standard records** and carry on using the map while they arrive,
which takes about 15 minutes. **Records** in the side panel lists every source, with Add and
Remove on each.

Old maps work the same way. There are about 900 of them and you probably want six. Go to the
place you care about and click **Add old maps** at the bottom of the screen (once you have some,
the list is under **Old maps** in the side panel). You get every map that covers what's on
screen, with its size, and outlines showing where each one sits.
Click **Add** on the ones you want. A typical sheet takes about a minute, a 10 MB download and
about 20 MB of disk. To find a map somewhere else, type a town, sheet number or year in the box
at the top of the list.

![The Old maps list open beside the map, with outlines on the map showing where each old map lies](docs/images/add-old-maps.jpg)

The bar at the bottom switches between the maps you've added, and its slider fades the old map in
and out. The button at the right end splits the screen so you can drag between two maps. This is
Cao Lãnh in 1873 and in 1965:

![The screen split down the middle: an 1873 French chart of Cao Lãnh on the left, the 1965 US Army sheet on the right](docs/images/swipe-compare-1873-1965.jpg)

The [user guide](docs/guide.md) has the rest: air strikes for an area you choose, helicopter
losses, removing things, grid references, cases and exports, and doing it all from a terminal if
you prefer.

## Data sources

No maps or record databases are in this repository. Each record source and each old map is
downloaded from its publisher when you add it. The repository holds only the catalogue of where
to fetch them, small source excerpts for the tests, and the screenshots in this README, each
under its own licence.

The maps are US Army Map Service and Defense Mapping Agency sheets and city plans from the USGS
store and the University of Texas Libraries, all public domain; French Indochina 1:100,000
sheets scanned by the CNRS (on NAKALA, mostly CC BY-NC-SA); British Admiralty charts on Wikimedia
Commons; and a few older scans from other map libraries. The records come from the US National
Archives, the NGA place-name server, Wikipedia, Wikidata and OpenStreetMap. Two more sources are
optional: US Air Force strike records from Kaggle, and helicopter losses from the Vietnam
Helicopter Pilots Association website.

[docs/data-sources.md](docs/data-sources.md) goes through each one, and [NOTICE](NOTICE) has the
licences and credits.

## Accuracy and limits

Old maps don't sit perfectly on new ones. The 1960s US sheets are within about 50 m and the
1:250,000 sheets within 100 to 200 m. The older French maps can be out by hundreds of metres.

Wartime grid references were read off maps drawn on the Indian 1960 datum, a few hundred metres
from the grid a GPS uses. Map Vietnam assumes Indian 1960 for any grid reference you type and
shows the GPS equivalent beside it.

Every record carries a ring showing how well its source pins it down, and sources are sometimes
plainly wrong. Read the original before you rely on a record.
[docs/known-limits.md](docs/known-limits.md) lists the problems found so far.

Air-strike sites can still hold unexploded ordnance. Don't visit one without clearance from the
local authorities.

## Privacy

The map is served to your own machine only (`127.0.0.1`). Your cases, notes and searches are
never sent anywhere. The page does load background map tiles, the MapLibre library and fonts
from the internet, so those sites see your IP address, as any website you visit does.
[docs/guide.md](docs/guide.md#what-goes-over-the-internet) names them.

## Documentation

- [User guide](docs/guide.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Data sources](docs/data-sources.md)
- [Known limits](docs/known-limits.md)
- [How it works](docs/how-it-works.md)
- [Changelog](CHANGELOG.md)

## Contributing

Corrections to records, new maps, new sources and fixes are all welcome.
[CONTRIBUTING.md](CONTRIBUTING.md) says how to set up, where things are, and what a change needs
before it's merged. To report a security problem, see [SECURITY.md](SECURITY.md).

## Licence

The code is under the [Apache License 2.0](LICENSE). The maps and records it downloads aren't
part of this repository and keep their own licences. The test excerpts and the map content in the
screenshots keep their sources' licences too; [NOTICE](NOTICE) lists them.
