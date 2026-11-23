# Troubleshooting

## "Port 8765 is in use"

Map Vietnam takes the next free port and prints the address to open. To choose one yourself, run
`python3 start.py serve --port 9000`.

## "Map Vietnam needs Python … or newer"

Install the latest Python from [python.org/downloads](https://www.python.org/downloads/) and
start again. To see which version you have, type `python3 --version` in a terminal. On a Mac, the
`.command` file uses the newest Python it finds.

## "Installing the Python add-ons failed"

Check your internet connection and start again. On a Mac older than macOS 15 the map tools won't
install, so update macOS first.

## macOS won't open the .command file

It came from the internet, so macOS wants you to say it's all right. Right-click the file, choose
**Open**, then **Open** again. You only have to do this once.

## Some records could not be added

The archives are sometimes busy, OpenStreetMap's Overpass servers most of all. Open **Records**
in the side panel. The source that failed is at the top with the reason, and **Retry** tries it
again. What was already downloaded is kept. The full log for each source is in
`data/work/setup/logs/`.

## The air strikes say the area holds too many

An area can hold up to 150,000 strikes, so that the map stays quick. Zoom in and add them for a
smaller area. See the [user guide](guide.md#air-strike-records).

## Kaggle asks for a key, or says the key is wrong

Open **Records**, then **Air strikes**. The row shows how to get a free key and where to choose
the `kaggle.json` file Kaggle gives you. If Kaggle refused a key you saved earlier, make a new
one the same way and choose the new file.

## The map says "Map Vietnam has stopped"

The program behind the page isn't running any more, because you quit it or closed its terminal
window. Start it again (double-click **Start Map Vietnam.command**, or run `python3 start.py`)
and reload the page.

## The map is blank

Open the address `start.py` prints (normally `http://127.0.0.1:8765/app/`), not the HTML file
itself.

## An old map I added doesn't show

Check that it's picked in the bar at the bottom, and that the slider isn't at zero. If the bar
says the map is off screen, the map covers somewhere else. Click **Old maps** to see where.
