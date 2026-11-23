#!/usr/bin/env python3
"""Map Vietnam in one command: make its private Python environment if needed, then open the map in a web browser.

    python3 start.py              open the map (on macOS: double-click "Start Map Vietnam.command")
    python3 start.py setup        add the records from the terminal instead (python3 start.py setup --help)
    python3 start.py serve        the same as no command at all
    python3 start.py maps ...     list, add or remove old maps (python3 start.py maps --help)
    python3 start.py records ...  list or remove record sources (python3 start.py records --help)
    python3 start.py --help       all options, in plain words

With no command it does two things and asks nothing:
  1. makes a private Python environment in .venv/ and installs requirements.txt into it
     (again only when requirements.txt changes: its SHA-256 is kept in .venv/requirements.sha256);
  2. starts the local server (scripts/server.py) and opens the map in the browser.
Everything else is done from the map itself: the first visit explains what the records are and
adds the ones the person picks, with the air-strike area, the optional helicopter losses, and a
Kaggle key if Kaggle ever asks for one; old maps are added the same way; and "Quit Map Vietnam"
there stops the server, which makes this script exit with status 0.

"setup" is for people who would rather script it. It runs scripts/records.py in .venv's Python:
the very module the server uses for the viewer, so a source added here shows as added there and
the other way round. Each source's output is kept in data/work/setup/logs/<source>.log, a
finished one leaves data/work/setup/<source>.done so a rerun skips it, one failing never stops
the others, and the summary says what worked, what was skipped and how to retry.

Nothing here downloads an old map. The person adds each map they want, from "Add old maps" in
the viewer or with "python3 start.py maps add" (scripts/library.py does the work for both).

This file uses only the Python standard library, because it runs before .venv exists; the one
project module it imports, scripts/records.py (for the names of the record sources), keeps to
the standard library at import time for the same reason. Keep it that way, and keep this file's
syntax readable by old Python 3 versions, so a too-old Python gets the plain message below
instead of a syntax error.
"""

import sys

MIN_PYTHON = (3, 12)
if sys.version_info < MIN_PYTHON:
    sys.exit(
        "Map Vietnam needs Python {}.{} or newer, but this is Python {}.{}.\n"
        "Download the latest Python from https://www.python.org/downloads/ , install it, then run this again "
        "(on a Mac: double-click 'Start Map Vietnam.command').".format(*MIN_PYTHON, *sys.version_info[:2])
    )

import argparse  # noqa: E402
import hashlib  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import venv  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import records  # noqa: E402  (standard library only at import time; see the module docstring)

VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_STAMP = VENV / "requirements.sha256"
TILES = ROOT / "web" / "tiles"
DEFAULT_PORT = 8765
PORTS_TO_TRY = 50  # if the default port is taken, try the next ones
SERVER_START_TIMEOUT_S = 60


def say(text=""):
    """Print a line at once (output is often piped through a log)."""
    print(text, flush=True)


# ---- the Python environment ---------------------------------------------------------------------
def venv_python():
    """Path of the Python interpreter inside .venv."""
    sub = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    return VENV.joinpath(*sub)


def runs(cmd):
    """True if cmd runs and exits 0 (its output is discarded)."""
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    except OSError:
        return False


def ensure_venv():
    """Make .venv if needed and install requirements.txt when it changed. Exits with a plain
    message if that is impossible."""
    py = venv_python()
    if VENV.exists() and not runs([str(py), "-c", "pass"]):
        say("The Python environment in .venv is broken (Python was probably updated); making a new one.")
        shutil.rmtree(VENV)
    if not VENV.exists():
        say("Making a private Python environment in .venv ...")
        try:
            venv.EnvBuilder(with_pip=True).create(VENV)
        except (OSError, subprocess.CalledProcessError) as e:
            shutil.rmtree(VENV, ignore_errors=True)
            sys.exit(
                f"Could not make the Python environment ({e}).\n"
                "On Linux, install the venv package first (for example: sudo apt install python3-venv)."
            )
    if not runs([str(py), "-m", "pip", "--version"]) and not runs([str(py), "-m", "ensurepip", "--upgrade"]):
        sys.exit("The Python environment in .venv has no pip. Delete the .venv folder and run this again.")
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    have = REQUIREMENTS_STAMP.read_text().strip() if REQUIREMENTS_STAMP.exists() else ""
    if have == wanted:
        return py
    say("Installing the Python add-ons (a few minutes the first time) ...")
    result = subprocess.run(
        [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)], cwd=ROOT
    )
    if result.returncode != 0:
        sys.exit(
            "Installing the Python add-ons failed (see the messages above). Check the internet connection "
            "and run this again."
        )
    REQUIREMENTS_STAMP.write_text(wanted + "\n")
    return py


# ---- running the project's own command lines ---------------------------------------------------
def script_env():
    """Environment for running a project script in .venv's Python: the same import path the tests
    use (pytest.ini), scripts/ and scripts/ingest/, so a script finds store, common, gridref...
    whatever order its own sys.path setup and imports are in; and unbuffered output."""
    paths = [str(ROOT / "scripts"), str(ROOT / "scripts" / "ingest"), os.environ.get("PYTHONPATH", "")]
    return {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": os.pathsep.join(p for p in paths if p)}


def run_script(script, argv):
    """Run scripts/<script> with `argv` in .venv's Python, on this terminal. Returns its exit
    status. Control-C reaches the script too, which cleans up and says what was kept."""
    py = ensure_venv()
    proc = subprocess.Popen([str(py), "scripts/" + script, *argv], cwd=ROOT, env=script_env())
    try:
        return proc.wait()
    except KeyboardInterrupt:
        return proc.wait()


def setup(args):
    """`python3 start.py setup`: add record sources from the terminal, through scripts/records.py
    (the module the viewer's server uses, so both see the same state). Returns its exit status."""
    convert_old_maps(ensure_venv())
    argv = ["setup"]
    for flag, value in (("--only", args.only), ("--skip", args.skip)):
        if value:
            argv += [flag, ",".join(value)]
    if args.area:
        argv += ["--area", args.area]
    argv += [flag for flag, on in (("--yes", args.yes), ("--refresh", args.refresh)) if on]
    return run_script("records.py", argv)


# ---- serving the map ----------------------------------------------------------------------------
def free_port(start):
    """The first port from `start` on that nothing on this computer is listening to.

    The probe binds the way the server will (http.server sets SO_REUSEADDR on macOS and Linux),
    so a port whose last connections are still closing (TIME_WAIT, for a minute or two after the
    map was stopped) counts as free: the map comes back on the same address, and the browser
    keeps its saved view, which is stored per address."""
    for port in range(start, start + PORTS_TO_TRY):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if os.name != "nt":  # on Windows the option would let two programs share a port
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    sys.exit(f"No free port between {start} and {port}; close other programs or pick one with --port.")


def wait_until_up(url, proc):
    """Poll url until it answers (True), the server exits, or the timeout passes (False)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            with opener.open(url, timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _stop_on_signal(signum, frame):
    """Signal handler: unwind like Control-C so the server gets stopped."""
    raise KeyboardInterrupt


def serve(args):
    """Run scripts/server.py on 127.0.0.1 until it is quit from the viewer, Control-C, or the
    window closes. True when the server started and stopped without trouble."""
    py = ensure_venv()
    port = free_port(args.port)
    if port != args.port:
        say(f"Port {args.port} is in use; using {port} instead.")
    url = f"http://127.0.0.1:{port}/app/"
    # Own process group: Control-C reaches only this script, which then stops the server
    # quietly instead of both printing tracebacks.
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([str(py), "scripts/server.py", "--port", str(port)], cwd=ROOT, **group)
    # Closing the Terminal window sends SIGHUP; treat it (and SIGTERM) like Control-C, so the
    # server, which is in its own session and would not get the signal, is not left running.
    # SIGINT is reset too: a shell that starts this in the background has it ignored.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    for name in ("SIGHUP", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop_on_signal)
    try:
        if not wait_until_up(url, proc):
            say("The map server did not start (see the messages above).")
            return False
        say()
        say(f"The map is ready at  {url}")
        say("Leave this window open while you use it. Records and old maps are added from the map itself.")
        say('To stop, choose "Quit Map Vietnam" in the map, or press Control-C here (or close the window).')
        if not args.no_browser:
            webbrowser.open(url)
        if proc.wait() != 0:
            say("The map server stopped unexpectedly (see the messages above).")
            return False
        say()
        say("Map Vietnam has stopped. Run python3 start.py to open it again.")
        return True
    except KeyboardInterrupt:
        say()
        say("Stopping the map server ...")
        return True
    finally:
        if proc.poll() is None:
            # The server ends its jobs first (no download is left running without it), then exits.
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---- old maps -----------------------------------------------------------------------------------
def old_map_folders():
    """Tile folders made by versions before the map library (web/tiles/<id>/ holding PNG tiles)."""
    if not TILES.is_dir():
        return []
    return [p for p in TILES.iterdir() if p.is_dir() and not p.name.endswith(".partial")]


def convert_old_maps(py):
    """Convert old tile folders once into one compact file per map (no download, no questions:
    the maps stay exactly the ones the user had). Says what it does; a failure is reported and
    left for the next run."""
    folders = old_map_folders()
    if not folders:
        return
    say(f"Converting {len(folders)} old map{'s' if len(folders) != 1 else ''} to the new compact format")
    say("(once only; it takes a minute and frees most of their disk space) ...")
    if subprocess.run([str(py), "scripts/library.py", "migrate"], cwd=ROOT, env=script_env()).returncode != 0:
        say("Converting the old maps failed (see above). The map works without them; it will try again next time.")


def maps_command(argv):
    """`python3 start.py maps ...`: hand the arguments to scripts/library.py in .venv's Python.
    Returns its exit status."""
    return run_script("library.py", argv or ["--help"])


def records_command(argv):
    """`python3 start.py records ...`: hand the arguments to scripts/records.py in .venv's Python.
    Returns its exit status."""
    return run_script("records.py", argv or ["--help"])


# ---- command line -------------------------------------------------------------------------------
HELP = """\
Opens Map Vietnam (old maps of Vietnam over today's map, plus war-era records you
can search, filter, pin and export) in your web browser, setting up its private
Python environment first if that is missing.

Nothing else is downloaded until you ask for it in the map: the first visit lists
the records you can add and what each download is, and "Add old maps" lists the
maps. "Quit Map Vietnam" in the map stops it; so does Control-C here.

"python3 start.py setup" adds the records from this terminal instead, for people
who script it. Both ways share their state.
"""

EPILOG = """\
record sources you can name with setup --only / --skip:
{}

old maps:
  python3 start.py maps list --region south      the maps you can add, with their sizes
  python3 start.py maps add L701461292           download and prepare one map (asks first)
  python3 start.py maps add --area 105.4,10.2,105.9,10.6   every map touching that box
  python3 start.py maps remove L701461292        delete it again (python3 start.py maps remove --all)

examples:
  python3 start.py                           open the map
  python3 start.py --no-browser --port 9000  the same, on port 9000, without opening a browser
  python3 start.py setup                     add the standard record sources (asks first)
  python3 start.py setup --yes --skip caccf  the same without questions, leaving one out
  python3 start.py setup --only thor --area 106,10.9,106.6,11.4   the air strikes, for another area
  python3 start.py setup --only vhpa         the optional helicopter losses (3 to 4 hours)
  python3 start.py records list              what has been added
  python3 start.py records remove vhpa       delete a source's records and downloads again
""".format("\n".join(f"  {s.id:14s}{s.title}" for s in records.SOURCES))


def parse_args(argv=None):
    """Command-line options (see HELP and EPILOG for the user-facing text)."""
    ap = argparse.ArgumentParser(
        prog="python3 start.py",
        description=HELP,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "command",
        nargs="?",
        choices=["setup", "serve", "maps", "records"],
        help="none or serve = open the map; setup = add records from the terminal; "
        "maps, records = their own command lines (see --help after either)",
    )
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for the map (default %(default)s)")
    # --yes, --only, --skip, --refresh, --area: defined once, beside the code that acts on them.
    records.add_setup_options(ap.add_argument_group("with setup"))
    return ap.parse_args(argv)


def main(argv=None):
    """Open the map, or run the setup, maps or records command. Returns the exit status:
    0 all well, 1 something failed, 130 stopped."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["maps"]:
        # Everything after "maps" belongs to the map library's own command line.
        return maps_command(argv[1:])
    if argv[:1] == ["records"]:
        return records_command(argv[1:])
    args = parse_args(argv)
    try:
        if args.command == "setup":
            return setup(args)
        if args.only or args.skip or args.yes or args.refresh or args.area:
            say("Note: --yes, --only, --skip, --refresh and --area go with 'python3 start.py setup'.")
            say("Opening the map: records are added there now.")
        return 0 if serve(args) else 1
    except KeyboardInterrupt:
        say()
        say("Stopped. Nothing is lost: run python3 start.py again to carry on where it left off.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
