# Troubleshooting

## Windows says "Windows protected your PC" when I run the installer

Map Vietnam's installer is not code-signed (a certificate costs money every year), so Windows
SmartScreen does not know it. Click **More info**, then **Run anyway**. You only see this for the
installer. If your browser says the file "isn't commonly downloaded", choose **Keep** first. The
installer, the program and its source are all in the open at
[github.com/CaullenOmdahl/map-vietnam](https://github.com/CaullenOmdahl/map-vietnam).

## macOS won't open the downloaded Map Vietnam app

The app is not signed with an Apple developer account, so macOS blocks it the first time.
Open the `.dmg`, drag **Map Vietnam** onto the Applications folder beside it, then right-click (or Control-click) it and choose
**Open**, then **Open** again. If the dialog only offers to move it to the Bin, close it, open
**System Settings > Privacy & Security**, scroll down, click **Open Anyway** next to Map Vietnam
and confirm. You only do this once. If macOS says the app is damaged, run
`xattr -dr com.apple.quarantine "/Applications/Map Vietnam.app"` in Terminal and open it again.

## The Linux app does not open a window

Unpack the `.tar.gz` and run `Map Vietnam/Map Vietnam`. It is built on Ubuntu 22.04, so it needs
glibc 2.35 or newer (Ubuntu 22.04, Debian 12 and later; older systems will not start it). It needs a
desktop session and the usual graphics libraries (on Debian or Ubuntu:
`sudo apt install libxcb-cursor0 libnss3 libxkbcommon-x11-0`). Without a window it opens the map in your browser
instead (see [below](#the-map-opens-in-my-browser-instead-of-its-own-window)).

## "Port 8765 is in use"

The installed app never needs a port from you: it picks any free one and shows no address, because
the window opens it for you. From source, Map Vietnam takes the next free port after 8765 and
prints the address to open. To choose one yourself, run `python3 start.py serve --port 9000`.

## "Map Vietnam needs Python … or newer"

This is for running from source; the installed app carries its own Python. Install the latest Python
from [python.org/downloads](https://www.python.org/downloads/) and start again. To see which version
you have, type `python3 --version` in a terminal. On a Mac, the `.command` file uses the newest
Python it finds, and on Windows the `.bat` file looks for the `py` launcher first, then `python`.

## "Installing the Python add-ons failed"

From source only; the installed app has its add-ons built in. Check your internet connection and
start again; what was already downloaded is kept. Behind a proxy, set the `HTTPS_PROXY` environment
variable (for example `https://proxy.example:8080`) before starting. On a Mac older than macOS 15
the map tools won't install, so update macOS first.

## macOS won't open the .command file

From source only. It came from the internet, so macOS wants you to say it's all right. When it says
"Apple could not verify...", click **Done**, open **System Settings > Privacy & Security**, scroll
down and click **Open Anyway** next to the file's name, then confirm. You only have to do this once.
(On macOS 14 and older, right-clicking the file and choosing **Open** did the same, but the map
tools need macOS 15.) Or skip the file: open Terminal in the folder and run `python3 start.py`.

## "Could not make the Python environment" (Linux)

From source only. Debian and Ubuntu ship Python without its environment maker. Run
`sudo apt install python3-venv` (or `python3.12-venv` if you installed Python 3.12 separately) and start again.

## "The Python environment in .venv was not finished"

From source only. An earlier start was stopped part-way. The half-made folder has been removed;
start again and it is made afresh.

## Windows: the `.bat` file says Python was not found, or the window closes

From source only; the installed app needs no Python. Double-click **Start Map Vietnam.bat**. If it
says no Python 3.12 or newer was found, install it from
[python.org/downloads](https://www.python.org/downloads/) and tick **Add python.exe to PATH**, then
double-click it again. Typing `python` in a terminal can open the Microsoft Store instead of running
Python; the `.bat` file avoids that by using the `py` launcher that the installer adds
(`py start.py` in a terminal). If Windows says "Windows protected your PC", click **More info**, then
**Run anyway**. The window stays open when something goes wrong, so you can read the message.

## The map opens in my browser instead of its own window

This happens when no window can be opened: there is no window toolkit (mostly Linux without the
Qt or GTK web view libraries, or a session with no display). Windows and macOS have one built in.
The installed Linux app carries Qt (PySide6); if it still fails, install the libraries listed in
[the Linux entry](#the-linux-app-does-not-open-a-window). The app opens your browser at its own
address, and everything works the same there. There is no window to close, so click **Quit Map
Vietnam** in the side panel when you are done.

From source, the Qt toolkit (PySide6) that `start.py` installs on x86-64, or GTK's, is used.
`start.py` prints a one-line explanation, and the address if the browser could not be opened
(normally `http://127.0.0.1:8765/app/`, or the next free port).

## Some records could not be added

The archives are sometimes busy, OpenStreetMap's Overpass servers most of all. Open **Records**
in the side panel. The source that failed is at the top with the reason, and **Retry** tries it
again. What was already downloaded is kept. The full log for each source is in
`data/work/setup/logs/` inside the data folder (see
[the user guide](guide.md#disk-space-backups-and-starting-over) for where that is; from source it is
the checkout itself).

## The air strikes say the area holds too many

An area can hold up to 150,000 strikes, so that the map stays quick. Zoom in and add them for a
smaller area. See the [user guide](guide.md#air-strike-records).

## Kaggle asks for a key, or says the key is wrong

Open **Records**, then **Air strikes**. The row shows how to get a free key and where to choose
the `kaggle.json` file Kaggle gives you. If Kaggle refused a key you saved earlier, make a new
one the same way and choose the new file.

## The map says "Map Vietnam has stopped"

The program behind the page isn't running any more, because you quit it. Open Map Vietnam again
from the Start menu or Applications. If the old page was in your browser, close it and use the
tab the app opens, because the address may have changed.

From source, this also happens when you close the terminal window. Start it again (double-click
**Start Map Vietnam.bat** or **.command**, or run `python3 start.py`).

## The map is blank

In the installed app, quit and open it again. If it is in your browser, use the tab the app
opened. From source, open the address `start.py` prints (normally `http://127.0.0.1:8765/app/`),
not the HTML file itself.

## An old map I added doesn't show

Check that it's picked in the bar at the bottom, and that the slider isn't at zero. If the bar
says the map is off screen, the map covers somewhere else. Click **Old maps** to see where.
