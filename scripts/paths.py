"""Where the program's own files are, and where the person's data goes.

A source checkout keeps both in one folder. A packaged desktop app is installed somewhere
read-only, so what the app writes (downloads, tiles, the record database, a saved key) goes to a
per-user data folder instead. Every script asks this module, so the two never get mixed up.

  APP_ROOT   the program's own files: the repository root from source, the unpacked bundle
             (sys._MEIPASS) in a packaged app. Read-only data lives here: data/sources,
             data/scans, data/maps.json, web/app, scripts.
  DATA_ROOT  everything the app writes: $MAPVIETNAM_HOME if set; else, in a packaged app, the
             per-user data folder (Windows %LOCALAPPDATA%\\Map Vietnam, macOS
             ~/Library/Application Support/Map Vietnam, Linux $XDG_DATA_HOME/map-vietnam or
             ~/.local/share/map-vietnam); else the repository root, so a checkout keeps its data
             where it always was.

script_command() builds the command line that runs one of the project's scripts as a child
process, and run_script_from_argv() is the other end of it for a packaged app, whose executable
is not a Python interpreter: it runs the script named by `--run-script`.

Stdlib only: start.py imports this before .venv exists. Nothing here creates a folder.
"""

import os
import runpy
import sys
from pathlib import Path

HOME_ENV = "MAPVIETNAM_HOME"
RUN_SCRIPT_FLAG = "--run-script"
APP_NAME = "Map Vietnam"


def is_frozen():
    """True in a packaged (PyInstaller) app."""
    return bool(getattr(sys, "frozen", False))


def find_app_root(frozen=None, meipass=None, source_root=None):
    """The program's own files: the bundle when packaged, else the repository root."""
    frozen = is_frozen() if frozen is None else frozen
    if frozen:
        return Path(meipass if meipass is not None else sys._MEIPASS)
    return Path(source_root) if source_root else Path(__file__).resolve().parent.parent


def user_data_dir(platform=None, env=None, home=None):
    """The per-user folder an installed app keeps its data in, by the platform's convention."""
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    home = Path.home() if home is None else Path(home)
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / APP_NAME
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    base = env.get("XDG_DATA_HOME")
    # The XDG rule: a relative XDG_DATA_HOME is invalid and ignored.
    return (Path(base) if base and os.path.isabs(base) else home / ".local" / "share") / "map-vietnam"


def find_data_root(app_root, frozen=None, platform=None, env=None, home=None):
    """Where everything the app writes lives (see the module docstring)."""
    env = os.environ if env is None else env
    frozen = is_frozen() if frozen is None else frozen
    if env.get(HOME_ENV):
        # Absolute, because children are started with another working folder and would
        # otherwise read a relative value against that one (rel/home/rel/home).
        return Path(os.path.abspath(Path(env[HOME_ENV]).expanduser()))
    if frozen:
        return user_data_dir(platform, env, home)
    return Path(app_root)


APP_ROOT = find_app_root()
DATA_ROOT = find_data_root(APP_ROOT)

# What the app writes (under DATA_ROOT) ...
DATA_DIR = DATA_ROOT / "data"  # raw/, work/, evidence.sqlite
RAW = DATA_DIR / "raw"
WORK = DATA_DIR / "work"
DB_PATH = DATA_DIR / "evidence.sqlite"
TILES = DATA_ROOT / "web" / "tiles"
CATALOG_JSON = DATA_ROOT / "web" / "catalog.json"
ENV_FILE = DATA_ROOT / ".env"

# ... and what ships with it (under APP_ROOT, never written, except by the maintainer tools that
# regenerate data/sources and data/scans in a checkout).
WEB_APP = APP_ROOT / "web" / "app"
SOURCES = APP_ROOT / "data" / "sources"
SCANS = APP_ROOT / "data" / "scans"
MAPS_JSON = APP_ROOT / "data" / "maps.json"
SCRIPTS = APP_ROOT / "scripts"


def script_command(script, *args):
    """The command line that runs scripts/<script> (e.g. "build_tiles.py", "ingest/gns.py") as a
    child process. From source that is this interpreter on the file; packaged, the executable
    itself with --run-script, which run_script_from_argv() answers."""
    if is_frozen():
        return [sys.executable, RUN_SCRIPT_FLAG, script, *args]
    return [sys.executable, str(SCRIPTS / script), *args]


def _open_std_handle(which):
    """A text stream on one of the process's standard handles (Windows), or None if it has none.
    which is the Win32 id: -11 for standard output, -12 for standard error."""
    import ctypes
    import msvcrt

    kernel = ctypes.windll.kernel32
    kernel.GetStdHandle.restype = ctypes.c_void_p
    handle = kernel.GetStdHandle(which)
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    fd = msvcrt.open_osfhandle(handle, os.O_WRONLY)
    return open(fd, "w", encoding="utf-8", errors="replace", buffering=1, closefd=False)


def attach_standard_streams():
    """Give a windowed Windows app its output back. Such an executable has no console, so Python
    leaves sys.stdout and sys.stderr as None even when the parent connected them to a pipe or a
    file: the child jobs' progress and a `--no-window` address would vanish. Where the process was
    handed real handles, use them. Elsewhere, and where they are already set, nothing changes."""
    if sys.platform != "win32":
        return
    for name, which in (("stdout", -11), ("stderr", -12)):
        if getattr(sys, name) is None:
            setattr(sys, name, _open_std_handle(which))


def run_script_from_argv(argv):
    """Run the script a `--run-script SCRIPT [ARGS...]` command line names, as `python SCRIPT`
    would (same __main__, argv and import path as start.py's script_env()), and return its exit
    status. Used by the packaged executable's entry point."""
    if len(argv) < 2 or argv[0] != RUN_SCRIPT_FLAG:
        print(f"usage: {RUN_SCRIPT_FLAG} SCRIPT [ARGS...]", file=sys.stderr)
        return 2
    script, rest = argv[1], list(argv[2:])
    path = (SCRIPTS / script).resolve()
    # Only the project's own scripts: the name may not climb out of scripts/.
    if SCRIPTS.resolve() not in path.parents or path.suffix != ".py" or not path.is_file():
        print(f"{script}: not one of the project's scripts", file=sys.stderr)
        return 2
    for stream in (sys.stdout, sys.stderr):  # Vietnamese text must survive Windows' default code page
        if is_frozen() and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    saved_argv, saved_path = sys.argv, list(sys.path)
    sys.argv = [str(path), *rest]
    sys.path[:0] = [str(SCRIPTS), str(SCRIPTS / "ingest")]
    try:
        runpy.run_path(str(path), run_name="__main__")
        return 0
    except SystemExit as stop:
        if stop.code is None:
            return 0
        if isinstance(stop.code, int):
            return stop.code
        print(stop.code, file=sys.stderr)  # SystemExit("message") prints it and exits 1
        return 1
    finally:
        sys.argv, sys.path[:] = saved_argv, saved_path
