#!/usr/bin/env python3
"""Map Vietnam in one command: set up whatever is missing, then open the map in a web browser.

    python3 start.py              set up, then open the map (on macOS: double-click "Start Map Vietnam.command")
    python3 start.py setup        set up only: the app and the record database
    python3 start.py serve        open the map only, no downloads
    python3 start.py maps ...     list, add or remove old maps (python3 start.py maps --help)
    python3 start.py --help       all options, in plain words

What "set up" does, in order, each step only if it has not finished before:
  1. makes a private Python environment in .venv/ and installs requirements.txt into it
     (again only when requirements.txt changes: its SHA-256 is kept in .venv/requirements.sha256);
  2. downloads each record source and loads it into data/evidence.sqlite with that source's own
     ingester (scripts/ingest/<source>.py), in dependency order.
Every step runs in .venv's Python; its output is shown and kept in data/work/setup/logs/<step>.log.
A finished step leaves data/work/setup/<step>.done, so a rerun skips it; a failed step is simply
tried again next time. One source failing never stops the others, and the summary at the end says
what worked, what was skipped and how to retry. Answers to the first-run questions are kept in
data/work/setup/choices.json.

Setup never downloads old maps. The user adds each map they want, from "Add old maps" in the
viewer (the "Old maps" card) or with "python3 start.py maps add", and can remove it again (scripts/library.py does the
work for both). Map folders made by older versions (web/tiles/<id>/ of PNG tiles) are converted
once to the current format, one file per map, without downloading anything.

Only THOR (air strikes, from Kaggle) needs a key: KAGGLE_USERNAME / KAGGLE_KEY in .env, or Kaggle's
own ~/.kaggle/kaggle.json. The first run offers to save one; .env is written readable by you only.

This file uses only the Python standard library, because it runs before .venv exists. Keep it
that way, and keep its syntax readable by old Python 3 versions, so a too-old Python gets the
plain message below instead of a syntax error.
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
import getpass  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
import venv  # noqa: E402
import webbrowser  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
REQUIREMENTS_STAMP = VENV / "requirements.sha256"
STATE = ROOT / "data" / "work" / "setup"
LOGS = STATE / "logs"
CHOICES = STATE / "choices.json"
DB = ROOT / "data" / "evidence.sqlite"
TILES = ROOT / "web" / "tiles"
ENV_FILE = ROOT / ".env"
THOR_ZIP = ROOT / "data" / "raw" / "thor" / "THOR_Vietnam_Bombing_Operations.csv.zip"
DEFAULT_PORT = 8765
PORTS_TO_TRY = 50  # if the default port is taken, try the next ones
SERVER_START_TIMEOUT_S = 60
PYTHON_ADDONS_MB = 50  # pip download of requirements.txt (macOS arm64 wheels; ~200 MB once installed)
DISK_GB = 1  # the app, the downloads it keeps and the record database, before any old maps
MAP_LIBRARY = ROOT / "data" / "sources" / "map_library.json"


class Step:
    """One unit of setup: a few commands run in .venv's Python, plus how to tell it is done.

    name: what --only/--skip accept. title/size_mb/time_text: the first-run explanation.
    needs: steps whose output this one reads; it is skipped while any of them is unfinished.
    produces: files that must exist (besides the stamp) for the step to count as done.
    opt_in: only run when the user said yes to it, or named it with --only.
    """

    def __init__(self, name, title, commands, size_mb, time_text, needs=(), produces=(), opt_in=False, note=""):
        self.name, self.title, self.commands = name, title, commands
        self.size_mb, self.time_text, self.note = size_mb, time_text, note
        self.needs, self.produces, self.opt_in = list(needs), list(produces), opt_in

    @property
    def stamp(self):
        """The file that marks the step as finished."""
        return STATE / (self.name + ".done")

    def is_done(self):
        """True when this step finished on an earlier run and its results are still there."""
        return self.stamp.exists() and DB.exists() and all((ROOT / p).exists() for p in self.produces)


def ingest(source, *args):
    """Command line for one ingester, scripts/ingest/<source>.py."""
    return [f"scripts/ingest/{source}.py", *args]


# Sizes are what each step downloads; times are for a typical home connection and a recent laptop.
STEPS = [
    Step(
        "gns",
        "Place names of Vietnam (US NGA gazetteer)",
        [ingest("gns")],
        size_mb=8,
        time_text="about 1 minute",
        produces=["data/work/gns_index.json"],
    ),
    Step(
        "hes_gazetteer",
        "Wartime hamlet list with grid references (US National Archives)",
        [ingest("hes_gazetteer")],
        size_mb=10,
        time_text="about 1 minute",
        produces=["data/work/hes_provinces.json"],
    ),
    Step(
        "datum_check",
        "Check of which map datum the wartime grid references use (no download)",
        [ingest("datum_check")],
        size_mb=0,
        time_text="under a minute",
        needs=["gns", "hes_gazetteer"],
    ),
    Step(
        "caccf",
        "US casualty records, body not recovered (US National Archives)",
        [ingest("caccf")],
        size_mb=13,
        time_text="about 1 minute",
        needs=["hes_gazetteer"],
    ),
    Step(
        "wiki_mia",
        "List of US personnel missing in action (Wikipedia)",
        [ingest("wiki_mia")],
        size_mb=1,
        time_text="about 1 minute",
        needs=["gns", "hes_gazetteer"],
    ),
    Step(
        "vn_relics",
        "Vietnam's listed historic sites, battles and memorials (Wikipedia)",
        [ingest("vn_relics")],
        size_mb=30,
        time_text="about 5 minutes",
        needs=["gns"],
    ),
    Step(
        "wikidata_osm",
        "Battles, bases, crash sites and memorials (Wikidata and OpenStreetMap)",
        [ingest("wikidata_osm")],
        size_mb=10,
        time_text="about 5 minutes",
        needs=["gns"],
        produces=["data/raw/osm/vn_boundary.json"],
    ),
    Step(
        "vhpa",
        "Helicopter losses (Vietnam Helicopter Pilots Association website)",
        [ingest("vhpa")],
        size_mb=20,
        time_text="3 to 4 hours (one page every 3 seconds, about 4,000 pages)",
        needs=["gns", "hes_gazetteer", "wikidata_osm"],
        opt_in=True,
        note="copyrighted website: for personal research use only; slow on purpose so as not to burden it",
    ),
    Step(
        "thor",
        "US air strike records, 1965-1973 (THOR, from Kaggle; needs a free key)",
        [ingest("thor")],
        size_mb=320,
        time_text="about 10 minutes",
    ),
]
BY_NAME = {step.name: step for step in STEPS}


# ---- talking to the user ------------------------------------------------------------------------
def say(text=""):
    """Print a line at once (output is often piped through a log)."""
    print(text, flush=True)


def ask_yes_no(question, default):
    """Ask on the terminal; Return alone gives the default. Without a terminal, the default."""
    hint = "[Y/n]" if default else "[y/N]"
    if not sys.stdin.isatty():
        return default
    while True:
        answer = input(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        say("Please type y for yes or n for no.")


def load_choices():
    """Answers to the first-run questions: {"agreed": bool, "vhpa": bool}; {} before the first run."""
    try:
        return json.loads(CHOICES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_choices(choices):
    """Keep the first-run answers for later runs."""
    STATE.mkdir(parents=True, exist_ok=True)
    CHOICES.write_text(json.dumps(choices, indent=1), encoding="utf-8")


def explain_first_run(steps, has_key):
    """The plain-words list of what setup will download, how big and how long (`has_key`: a Kaggle
    key is already available, so THOR will be downloaded too)."""
    say("Welcome to Map Vietnam.")
    say()
    say("The first time, it needs to download public records and prepare them.")
    say("Here is what it will fetch:")
    say()
    # THOR is not fetched without a key, so it is listed apart rather than counted in the total.
    keyless = [s for s in steps if s.name == "thor" and not has_key]
    rows = [("Python add-ons (map and image tools)", PYTHON_ADDONS_MB, "a few minutes")]
    rows += [(s.title, s.size_mb, s.time_text) for s in steps if not s.opt_in and s not in keyless]
    for title, size, when in rows:
        amount = f"{size} MB" if size else "nothing to download"
        say(f"  - {title}\n      {amount}, {when}")
    total = sum(size for _, size, _ in rows)
    say()
    say(f"In all: about {total} MB to download, and about {DISK_GB} GB of disk space. It usually takes")
    say("20 to 40 minutes; you can leave it running. Nothing about you is sent anywhere: it only")
    say("downloads from public archives. If you stop it (Control-C) or something fails, just run it")
    say("again: it carries on where it left off.")
    for step in keyless:
        say(f"Not counted: {step.title}, {step.size_mb} MB more once you add a key.")
    say()
    say('Old maps are not downloaded now. Once the map is open, tap "Add old maps" at the bottom')
    say("to add the ones you want (or use: python3 start.py maps add). Any map can be removed again.")
    sizes = sheet_sizes()
    if sizes:
        say("A 1:50,000 sheet is about {:.0f} MB to download and {:.0f} MB on disk;".format(*sizes))
        say(f"all {sizes[2]} of them together are about {sizes[3]:.0f} GB on disk.")
    say()


def sheet_sizes():
    """(MB to download, MB on disk) for an average 1:50,000 sheet, how many there are, and GB on
    disk for all of them: from the map library catalogue, so the text never drifts from it.
    None if the catalogue cannot be read."""
    try:
        catalogue = json.loads(MAP_LIBRARY.read_text(encoding="utf-8"))
        sheets = [m["bytes"] for m in catalogue["maps"] if m["series"] == "l7014"]
        ratio = catalogue["series"]["l7014"]["disk_per_download_byte"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not sheets:
        return None
    mean = sum(sheets) / len(sheets)
    return mean / 1e6, mean * ratio / 1e6, len(sheets), sum(sheets) * ratio / 1e9


def offer_vhpa(step):
    """Describe the optional VHPA download and ask whether to include it (default no)."""
    say(f"Optional: {step.title}.")
    say(f"  This is a {step.note}. It takes {step.time_text}.")
    return ask_yes_no("Download it too?", default=False)


# ---- Kaggle key (THOR) --------------------------------------------------------------------------
def read_env_file():
    """KEY=VALUE pairs from .env (the same rules as scripts/ingest/common.load_env, which the
    ingesters use; duplicated because this file must not need anything outside the stdlib)."""
    values = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip().removeprefix("export ").strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key] = value
    return values


def kaggle_key_available():
    """True if THOR can be fetched: its download is already cached, or a Kaggle username and key
    are in the environment, .env or kaggle.json (the places scripts/ingest/thor.py looks)."""
    if THOR_ZIP.exists():
        return True
    env = {**read_env_file(), **os.environ}
    if env.get("KAGGLE_USERNAME") and env.get("KAGGLE_KEY"):
        return True
    config = Path(env.get("KAGGLE_CONFIG_DIR") or Path.home() / ".kaggle") / "kaggle.json"
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(cfg, dict) and bool(cfg.get("username") and cfg.get("key"))


def write_env(updates):
    """Set keys in .env, keeping its other lines, and make the file readable by this user only."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip().removeprefix("export ").strip() not in updates]
    kept += [f"{k}={v}" for k, v in updates.items()]
    # Create with 0600 from the start so the key is never readable by others, even briefly.
    fd = os.open(ENV_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    os.chmod(ENV_FILE, 0o600)


def offer_kaggle_key():
    """Explain how to get a Kaggle key and offer to save one. True if a key was saved."""
    say("The air strike records (THOR) come from Kaggle, which needs a free account and key:")
    say("  1. Go to https://www.kaggle.com and sign in (or register, it is free).")
    say("  2. Click your picture (top right), then Settings.")
    say("  3. In the 'API' section click 'Create Legacy API Key' (older pages: 'Create New Token').")
    say("     Your browser downloads kaggle.json.")
    say('  4. Open kaggle.json with TextEdit: it shows your "username" and "key".')
    say("You can also skip this now and add the air strike records later with:  python3 start.py setup --only thor")
    if not ask_yes_no("Do you have the username and key ready to paste?", default=False):
        return False
    user = input("Kaggle username: ").strip()
    key = getpass.getpass("Kaggle key (it will not show as you paste; press Return after): ").strip()
    if not (user and key):
        say("Nothing saved: the username or key was empty.")
        return False
    write_env({"KAGGLE_USERNAME": user, "KAGGLE_KEY": key})
    say("Saved in .env (only you can read that file).")
    return True


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
    have = REQUIREMENTS_STAMP.read_text(encoding="utf-8").strip() if REQUIREMENTS_STAMP.exists() else ""
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
    REQUIREMENTS_STAMP.write_text(wanted + "\n", encoding="utf-8")
    return py


# ---- running setup steps ------------------------------------------------------------------------
def script_env():
    """Environment for running a project script in .venv's Python: the same import path the tests
    use (pytest.ini), scripts/ and scripts/ingest/, so a script finds store, common, gridref...
    whatever order its own sys.path setup and imports are in; unbuffered output; and Python's UTF-8
    mode, so Vietnamese text is read, written and printed as UTF-8 on Windows too (whose default
    text encoding, cp1252, cannot hold it)."""
    paths = [str(ROOT / "scripts"), str(ROOT / "scripts" / "ingest"), os.environ.get("PYTHONPATH", "")]
    return {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
        "PYTHONUTF8": "1",
        "PYTHONPATH": os.pathsep.join(p for p in paths if p),
    }


def run_logged(py, step):
    """Run a step's commands in order, echoing and logging their output. Returns (ok, last line)."""
    LOGS.mkdir(parents=True, exist_ok=True)
    env = script_env()
    last = ""
    with open(LOGS / (step.name + ".log"), "w", encoding="utf-8") as log:
        log.write(f"{step.name} started {datetime.now().isoformat(timespec='seconds')}\n")
        for cmd in step.commands:
            proc = subprocess.Popen(
                [str(py), *cmd],
                cwd=ROOT,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                encoding="utf-8",
            )
            for line in proc.stdout:
                sys.stdout.write("  " + line)
                sys.stdout.flush()
                log.write(line)
                if line.strip():
                    last = line.strip()
            if proc.wait() != 0:
                log.write(f"exit status {proc.returncode}\n")
                return False, last
    return True, last


def plan(args, choices, has_key):
    """Steps to run this time, and the ones left out with the reason, in setup order."""
    chosen = set(args.only or [])
    # --only also pulls in unfinished steps it depends on.
    pending = list(chosen)
    while pending:
        for need in BY_NAME[pending.pop()].needs:
            if need not in chosen and not BY_NAME[need].is_done():
                chosen.add(need)
                pending.append(need)
    run, left_out = [], []
    for step in STEPS:
        if chosen and step.name not in chosen:
            continue
        if step.name in (args.skip or []):
            left_out.append((step, "skipped as asked (--skip)"))
        elif step.opt_in and step.name not in chosen and not choices.get(step.name):
            left_out.append((step, f"optional, not chosen (add it with: python3 start.py setup --only {step.name})"))
        elif step.name == "thor" and not has_key:
            left_out.append((step, "no Kaggle key (see .env.example, or run: python3 start.py setup --only thor)"))
        else:
            run.append(step)
    return run, left_out


def setup(args):
    """Everything but serving. Returns True when nothing failed."""
    choices = load_choices()
    first_run = not choices.get("agreed")
    has_key = kaggle_key_available()
    if first_run:
        explain_first_run(STEPS, has_key)
        if not args.yes and not ask_yes_no("Go ahead?", default=True):
            say("Stopped; nothing was downloaded. Run this again when you are ready.")
            sys.exit(0)
        choices["agreed"] = True
        if not args.yes and sys.stdin.isatty():
            choices["vhpa"] = offer_vhpa(BY_NAME["vhpa"])
        save_choices(choices)
    wants_thor = "thor" in (args.only or []) or (first_run and not args.only)
    if not has_key and wants_thor and "thor" not in (args.skip or []) and not args.yes and sys.stdin.isatty():
        has_key = offer_kaggle_key()
    say()

    py = ensure_venv()
    convert_old_maps(py)
    run, left_out = plan(args, choices, has_key)
    worked, already, failed, skipped = [], [], [], list(left_out)
    for step in run:
        if step.is_done():
            already.append(step)
            continue
        missing = [n for n in step.needs if not BY_NAME[n].is_done()]
        if missing:
            skipped.append((step, f"needs {', '.join(missing)} to finish first"))
            continue
        say(f"== {step.title} ({step.name}) ==")
        started = time.monotonic()
        ok, last = run_logged(py, step)
        minutes = (time.monotonic() - started) / 60
        if ok:
            STATE.mkdir(parents=True, exist_ok=True)
            step.stamp.write_text(datetime.now().isoformat(timespec="seconds") + "\n", encoding="utf-8")
        if ok and step.is_done():
            worked.append((step, minutes))
        else:
            failed.append((step, last or "no output"))
    summarise(worked, already, failed, skipped)
    return not failed


def summarise(worked, already, failed, skipped):
    """Print what worked, what was already there, what was skipped (and why), what failed (and
    where its log is), then how to retry."""
    say()
    say("Summary")
    say("-------")
    for step, minutes in worked:
        took = f"{minutes:.0f} min" if minutes >= 1 else "under a minute"
        say(f"  done      {step.title}  ({took})")
    for step in already:
        say(f"  ready     {step.title}  (done before)")
    for step, why in skipped:
        say(f"  skipped   {step.title}: {why}")
    for step, last in failed:
        say(f"  FAILED    {step.title}")
        say(f"            last message: {last[:200]}")
        log = LOGS / (step.name + ".log")
        say(f"            full log: {log.relative_to(ROOT) if log.is_relative_to(ROOT) else log}")
    if failed:
        say()
        say("To retry what failed, run python3 start.py again (finished parts are not redone),")
        say(f"or retry one part with, for example:  python3 start.py setup --only {failed[0][0].name}")


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
    """Run scripts/server.py on 127.0.0.1 until Control-C (or the window closes)."""
    py = ensure_venv()
    convert_old_maps(py)
    port = free_port(args.port)
    if port != args.port:
        say(f"Port {args.port} is in use; using {port} instead.")
    url = f"http://127.0.0.1:{port}/app/"
    # Own process group: Control-C reaches only this script, which then stops the server
    # quietly instead of both printing tracebacks.
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen([str(py), "scripts/server.py", "--port", str(port)], cwd=ROOT, env=script_env(), **group)
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
        say("Leave this window open while you use it. To stop, press Control-C here (or close the window).")
        if not args.no_browser:
            webbrowser.open(url)
        # Poll rather than wait(): on Windows a bare wait() blocks Control-C until the server exits.
        while proc.poll() is None:
            time.sleep(0.5)
        return proc.returncode == 0
    except KeyboardInterrupt:
        say()
        say("Stopping the map server ...")
        return True
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
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
    py = ensure_venv()
    proc = subprocess.Popen([str(py), "scripts/library.py", *(argv or ["--help"])], cwd=ROOT, env=script_env())
    try:
        return proc.wait()
    except KeyboardInterrupt:
        # Control-C reached the library too; let it clean up and say what was kept.
        return proc.wait()


# ---- command line -------------------------------------------------------------------------------
HELP = """\
Sets up Map Vietnam (old maps of Vietnam over today's map, plus war-era records you
can search, filter, pin and export) and opens it in your web browser.

The first run explains what it will download and asks before starting. Later runs
only do what is still missing, then open the map. Stop the map with Control-C.

Old maps are never downloaded on their own: add the ones you want with "Add old
maps" in the viewer, or with "python3 start.py maps".
"""

EPILOG = """\
parts you can name with --only / --skip:
{}

old maps:
  python3 start.py maps list --region south      the maps you can add, with their sizes
  python3 start.py maps add L701461292           download and prepare one map (asks first)
  python3 start.py maps add --area 105.4,10.2,105.9,10.6   every map touching that box
  python3 start.py maps remove L701461292        delete it again (python3 start.py maps remove --all)

examples:
  python3 start.py                     set up what is missing, then open the map
  python3 start.py --yes --no-browser  same, without questions or opening a browser
  python3 start.py setup --only thor   just the air strike records (asks for the Kaggle key)
  python3 start.py setup --skip vhpa   everything except the helicopter losses
  python3 start.py serve --port 9000   just open the map, on port 9000
""".format("\n".join(f"  {s.name:14s}{s.title}" for s in STEPS))


def step_names(text):
    """argparse type for --only/--skip: comma-separated step names, checked."""
    names = [n.strip() for n in text.split(",") if n.strip()]
    unknown = [n for n in names if n not in BY_NAME]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown part {', '.join(unknown)}; choose from: {', '.join(BY_NAME)}")
    return names


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
        choices=["setup", "serve", "maps"],
        help="setup = prepare everything but do not open the map; serve = only open the map; "
        "maps = list, add or remove old maps",
    )
    ap.add_argument("--yes", action="store_true", help="do not ask questions (optional parts stay off)")
    ap.add_argument("--no-browser", action="store_true", help="do not open a browser window")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="port for the map (default %(default)s)")
    ap.add_argument("--only", type=step_names, action="extend", metavar="PART", help="set up only these parts")
    ap.add_argument("--skip", type=step_names, action="extend", metavar="PART", help="leave these parts out")
    return ap.parse_args(argv)


def main(argv=None):
    """Set up and/or serve, or run a maps command. Returns the exit status: 0 all well,
    1 something failed, 130 stopped."""
    argv = sys.argv[1:] if argv is None else argv
    # A console or log that cannot show a character (a cp1252 pipe on Windows) gets "?" instead of a crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    if argv[:1] == ["maps"]:
        # Everything after "maps" belongs to the map library's own command line.
        return maps_command(argv[1:])
    args = parse_args(argv)
    try:
        ok = True
        if args.command in (None, "setup"):
            ok = setup(args)
        if args.command in (None, "serve"):
            ok = serve(args) and ok
    except KeyboardInterrupt:
        say()
        say("Stopped. Nothing is lost: run python3 start.py again to carry on where it left off.")
        return 130
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
