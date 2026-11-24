# Troubleshooting

## Windows says "Windows protected your PC" when I run the installer

Map Vietnam's installer is not code-signed (a certificate costs money every year), so Windows
SmartScreen does not know it. Click **More info**, then **Run anyway**. You only see this for the
installer. If your browser says the file "isn't commonly downloaded", choose **Keep** first. The
installer, the program and its source are all in the open at
[github.com/CaullenOmdahl/map-vietnam](https://github.com/CaullenOmdahl/map-vietnam).

## macOS won't open the downloaded Map Vietnam app

The app is not signed with an Apple developer account, so macOS blocks it the first time.
Unzip it, drag **Map Vietnam** to Applications, then right-click (or Control-click) it and choose
**Open**, then **Open** again. If the dialog only offers to move it to the Bin, close it, open
**System Settings > Privacy & Security**, scroll down, click **Open Anyway** next to Map Vietnam
and confirm. You only do this once. If macOS says the app is damaged, run
`xattr -dr com.apple.quarantine "/Applications/Map Vietnam.app"` in Terminal and open it again.

## The Linux app does not open a window

Unpack the `.tar.gz` and run `Map Vietnam/Map Vietnam`. It is built on Ubuntu 22.04, so it needs
glibc 2.35 or newer (Ubuntu 22.04, Debian 12 and later; older systems will not start it). It needs a desktop session and the usual
graphics libraries (on Debian or Ubuntu: `sudo apt install libxcb-cursor0 libnss3 libxkbcommon-x11-0`).
Without a window it opens the map in your browser instead.

## "Port 8765 is in use"

Map Vietnam takes the next free port and prints the address to open. To choose one yourself, run
`python3 start.py serve --port 9000`.

## "Map Vietnam needs Python … or newer"

Install the latest Python from [python.org/downloads](https://www.python.org/downloads/) and
start again. To see which version you have, type `python3 --version` in a terminal. On a Mac, the
`.command` file uses the newest Python it finds, and on Windows the `.bat` file looks for the `py`
launcher first, then `python`.

## "Installing the Python add-ons failed"

Check your internet connection and start again; what was already downloaded is kept. Behind a
proxy, set the `HTTPS_PROXY` environment variable (for example `https://proxy.example:8080`)
before starting. On a Mac older than macOS 15 the map tools won't
install, so update macOS first.

## macOS won't open the .command file

It came from the internet, so macOS wants you to say it's all right. When it says "Apple could
not verify...", click **Done**, open **System Settings > Privacy & Security**, scroll down and
click **Open Anyway** next to the file's name, then confirm. You only have to do this once. (On
macOS 14 and older, right-clicking the file and choosing **Open** did the same, but the map tools
need macOS 15.) Or skip the file: open Terminal in the folder and run `python3 start.py`.

## "Could not make the Python environment" (Linux)

Debian and Ubuntu ship Python without its environment maker. Run `sudo apt install python3-venv`
(or `python3.12-venv` if you installed Python 3.12 separately) and start again.

## "The Python environment in .venv was not finished"

An earlier start was stopped part-way. The half-made folder has been removed; start again and it
is made afresh.

## Windows: the `.bat` file says Python was not found, or the window closes

Double-click **Start Map Vietnam.bat**. If it says no Python 3.12 or newer was found, install it
from [python.org/downloads](https://www.python.org/downloads/) and tick **Add python.exe to
PATH**, then double-click it again. Typing `python` in a terminal can open the Microsoft Store
instead of running Python; the `.bat` file avoids that by using the `py` launcher that the
installer adds (`py start.py` in a terminal). If Windows says "Windows protected your PC", click
**More info**, then **Run anyway**. The window stays open when something goes wrong, so you can
read the message.

## The map opens in my browser instead of its own window

The map shows in its own window when the computer has a web view for it (built into Windows and
macOS; on Linux the Qt one (PySide6) that `start.py` installs on x86-64, or GTK's). If it can't open a window it says so and
opens your browser instead; everything works the same there. If neither opens, open the address
`start.py` prints (normally `http://127.0.0.1:8765/app/`) yourself.

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
and, if it opened in a browser, reload the page.

## The map is blank

If you are using the map in a browser, open the address `start.py` prints (normally
`http://127.0.0.1:8765/app/`), not the HTML file itself.

## An old map I added doesn't show

Check that it's picked in the bar at the bottom, and that the slider isn't at zero. If the bar
says the map is off screen, the map covers somewhere else. Click **Old maps** to see where.
