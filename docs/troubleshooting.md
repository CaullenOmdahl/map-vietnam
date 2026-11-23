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

It came from the internet, so macOS wants you to say it's all right. When it says "Apple could
not verify...", click **Done**, open **System Settings > Privacy & Security**, scroll down and
click **Open Anyway** next to the file's name, then confirm. You only have to do this once. (On
macOS 14 and older, right-clicking the file and choosing **Open** did the same, but the map tools
need macOS 15.) Or skip the file: open Terminal in the folder and run `python3 start.py`.

## Windows says "Python was not found"

Typing `python` on Windows can open the Microsoft Store instead of running Python. Use the
`py` launcher that the python.org installer adds: `py start.py`. If that isn't found either,
run the installer again and tick **Add python.exe to PATH**.

## A download failed

The archives are sometimes busy, OpenStreetMap's Overpass servers most of all. The summary at the
end says what failed, and running `python3 start.py` again later redoes only that. The full log
for each part is in `data/work/setup/logs/`.

## Kaggle says the key is wrong

Make a new key (see the [user guide](guide.md#air-strike-records)), then run
`python3 start.py setup --only thor` again, or correct the two lines in `.env`.

## The map is blank

Open the address `start.py` prints (normally `http://127.0.0.1:8765/app/`), not the HTML file
itself.

## An old map I added doesn't show

Check that it's picked in the bar at the bottom, and that the slider isn't at zero. If the bar
says the map is off screen, the map covers somewhere else. Click **Old maps** to see where.
