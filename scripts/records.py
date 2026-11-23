"""The record sources: the records a person can add, the ones they have added, and the queue that adds them.

This is the records counterpart of library.py (old maps). Nothing is downloaded unless someone
asks for a source by name: from the viewer (scripts/server.py, /api/records) or from the command
line (python3 start.py setup ...), which both end up here, so a source added one way shows as
added the other way. Every source added can be removed again.

Adding a source runs its ingester, scripts/ingest/<id>.py, as a child process: always that fixed
command for a source listed in SOURCES, never anything taken from a request. The ingester prints
"progress ..." lines (scripts/ingest/common.py) that become the progress and the plain-words note
shown to the person. Sources a source needs are added first, automatically.

Inputs and outputs (all under the project folder):
  data/evidence.sqlite              the records (store.py); each ingester writes its own source
  data/raw/<folder>/                each source's downloads, kept so nothing is fetched twice
  data/work/setup/<id>.done         a source's marker: {"added_at", "options"} once it has been
                                    added (markers of older versions hold just the time)
  data/work/setup/logs/<id>.log     what the last run of a source's ingester printed
  data/work/setup/choices.json      {"agreed", "vhpa", "welcome_dismissed", "ever_added"}: answers
                                    to the first-run questions and whether the welcome is done
  data/work/setup/worker.lock       held while a source is being added or removed (see below)
  data/work/setup/current.json      {"id", "pid", "action"} of the source the lock holder is on
  data/work/setup/cancel/<id>       "stop adding this source": asked for by another process
  .env                              KAGGLE_USERNAME / KAGGLE_KEY, saved here (readable by its owner
                                    only) if Kaggle asks for a key; never printed, logged or returned

A source's state is one of: available, queued, running, added, failed. Added comes from disk
(the marker, the database and the files the source makes all exist), so it survives restarts and
is the same for the viewer and the command line. Queued, running and failed live in memory for as
long as the process runs (normally the viewer's server), so they survive viewer reloads but not a
restart: a job cut short by a restart leaves the source as it was (available, or added if it was
being added again), and adding it again carries on from the downloads already kept.

One job at a time. One worker thread (or the command line itself) takes the jobs in turn, and an
exclusive lock on worker.lock makes the viewer's server and the command line take turns too, so
two processes never write records at once. Removing a source is a job as well, so it never
deletes records an ingester is writing. A slow source that can pick up where it stopped (the
helicopter losses: hours of polite page reading, every page cached) steps aside when anything
else is waiting and carries on afterwards, so it never holds up the rest.

Records and old maps do not share a lock: a map being built and a source being added may run at
the same time. Measured on the real downloads, no ingester needs more than 0.4 GB of memory
(the air strikes included, with the largest area allowed, MAX_STRIKES), next to at most about
2 GB for a map build, and they write different files; making either wait for the other would
only mean that hours of helicopter-loss pages kept every map waiting.

The Kaggle key. The air strikes come from Kaggle, which gives the file without a key at the time
of writing, so adding them never asks for one up front. If Kaggle does ask, the ingester exits
with status KEY_EXIT: the source fails with "wants_key" set, the viewer (or the command line, on
a terminal) asks the person for a key, saves it, and the source is added again.

Stopping is always safe: the ingester is ended, a half-written download (<name>.part) is ignored
next time, and each ingester writes its records in one transaction that SQLite undoes if it is
cut short. Removing a source deletes its records the way a sync does (store.remove_source: a
record someone put in a case is kept and marked withdrawn, never silently deleted), then its
marker, its downloads (unless another added source reads them) and the files it made.

Usage: .venv/bin/python scripts/records.py list
       .venv/bin/python scripts/records.py setup [--only ID,...] [--skip ID,...] [--yes] [--refresh] [--area W,S,E,N]
       .venv/bin/python scripts/records.py remove ID ... [--cascade]
       (python3 start.py setup ... and python3 start.py records ... run these in .venv)
"""

import argparse
import contextlib
import getpass
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import store
from files import delete, lock_file, path_bytes, read_json, unlock_file, write_json

ROOT = store.ROOT
STATES = ("available", "queued", "running", "added", "failed")
# How often a process running a job looks for a cancel request from another process.
CANCEL_POLL_S = 0.5
# The air strikes: the area loaded unless another is chosen (scripts/ingest/common.AREA_OF_INTEREST,
# around Cao Lãnh), and the box an area must lie in (Vietnam with its borderlands, where the
# wartime strikes are).
DEFAULT_AREA = (105.35, 10.15, 105.95, 10.75)
AREA_LIMITS = (100.0, 7.0, 111.0, 24.0)
# The exit status with which an ingester says "Kaggle wants a key" (scripts/ingest/thor.KEY_EXIT).
KEY_EXIT = 3
# The most air strikes one area may hold; the ingester stops, changing nothing, as soon as an
# area turns out to hold more (thor.py --max-strikes). Measured on the real file: the whole
# country is 3,002,090 strikes, which is about 7 GB of memory to load (2.2 KB a strike), a 5.6 GB
# database, 12 seconds for every search and three minutes to draw one screen. The viewer draws
# every strike in view: 386,000 in one view took 30 seconds and 105 MB to fetch and once brought
# the page down. 150,000 costs the ingester about 0.4 GB, the database about 260 MB and a
# full view about 40 MB, which stays quick; it is five times the area loaded by default.
MAX_STRIKES = 150_000
ALL_VIETNAM_REFUSED = (
    "All of Vietnam is about 3 million air strikes: a database of over 5 GB, and far more points than "
    f"the map can draw. Choose an area instead; it can hold up to {MAX_STRIKES:,} strikes."
)
KAGGLE_NAMES = ("KAGGLE_USERNAME", "KAGGLE_KEY")
# What a Kaggle user name and key look like. Checked only so that nothing saved in .env can break
# the file (no spaces, quotes, "=" or line breaks); whether Kaggle accepts them shows on download.
KAGGLE_USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
KAGGLE_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")
# Words in an ingester's last message that mean "the download did not work", not a fault here.
NETWORK_WORDS = (
    "URLError",
    "HTTPError",
    "HTTP Error",
    "TimeoutError",
    "timed out",
    "ConnectionError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "RemoteDisconnected",
    "IncompleteRead",
    "gaierror",
    "SSLError",
    "fetch failed",
)
REMOVED_REASON = "its source was removed from Map Vietnam"


@dataclass(frozen=True)
class Source:
    """One record source: what the person is told about it, and how it is added and removed.

    id: the name of its ingester (scripts/ingest/<id>.py) and of its marker; what --only accepts.
    title, description: plain words for the list. size_mb: what it downloads. time_text: how
    long adding it takes on a typical home connection and a recent laptop. licence: one line.
    needs: sources whose output its ingester reads; they are added first, automatically.
    produces: files that must exist (besides the marker) for it to count as added.
    store_ids: its ids in the store's `sources` table (one ingester may write two).
    raw_folders: its download folders under data/raw/. reads_raw: other sources' folders it reads.
    work_files: other files it writes, deleted with it. refresh_args: ingester arguments that
    download the source again instead of using what is kept.
    opt_in: only added when the person says yes to `warning`.
    standard: part of the set a first run adds in one go (the viewer's first-run button, and
    `setup` with no --only): small, quick, and nothing to choose. False for the air strikes
    (320 MB, and an area to pick) and for anything opt-in.
    uses_key: its ingester sends the Kaggle key when one is saved, so what it prints is scrubbed
    of the key, and it may end asking for one (KEY_EXIT).
    listed: False for a check that makes no records (command line only, not shown in the viewer).
    yields: slow, and resumes where it stopped, so it steps aside while other jobs are waiting.
    script, args: the fixed command, relative to the project folder (the tests use small fakes).
    """

    id: str
    title: str
    description: str
    size_mb: int
    time_text: str
    licence: str
    needs: tuple = ()
    produces: tuple = ()
    store_ids: tuple = ()
    raw_folders: tuple = ()
    reads_raw: tuple = ()
    work_files: tuple = ()
    refresh_args: tuple = ("--refresh",)
    opt_in: bool = False
    warning: str = ""
    standard: bool = True
    uses_key: bool = False
    listed: bool = True
    yields: bool = False
    script: str = ""
    args: tuple = ()

    @property
    def command(self):
        """The ingester's script and fixed arguments, relative to the project folder."""
        return (self.script or f"scripts/ingest/{self.id}.py", *self.args)


# In the order they run: a source comes after the ones it needs, and the slow one last.
SOURCES = (
    Source(
        "gns",
        "Place names",
        "Every named place in Vietnam, with the older spellings that wartime reports use. "
        "Other records are placed on the map with its help.",
        size_mb=8,
        time_text="about 1 minute",
        licence="US Government work (National Geospatial-Intelligence Agency), free to use",
        produces=("data/work/gns_index.json",),
        store_ids=("gns",),
        raw_folders=("gns",),
    ),
    Source(
        "hes_gazetteer",
        "Wartime hamlets",
        "The hamlets of South Vietnam in 1972-73, with their wartime province, district and grid reference.",
        size_mb=10,
        time_text="about 1 minute",
        licence="Public domain (US National Archives)",
        produces=("data/work/hes_provinces.json",),
        store_ids=("hes_gazetteer",),
        raw_folders=("hes_gazetteer",),
    ),
    Source(
        "datum_check",
        "Check of the wartime map grid",
        "Measures which map grid the wartime grid references agree with. It downloads nothing and adds no records.",
        size_mb=0,
        time_text="under a minute",
        licence="",
        needs=("gns", "hes_gazetteer"),
        work_files=("data/work/datum_calibration.json",),
        refresh_args=(),
        listed=False,
    ),
    Source(
        "caccf",
        "US casualties, body not recovered",
        "US service members who died in the war and whose body was not recovered, placed by the province "
        "their record names.",
        size_mb=13,
        time_text="about 1 minute",
        licence="Public domain (US National Archives)",
        needs=("hes_gazetteer",),
        store_ids=("caccf",),
        raw_folders=("caccf",),
        work_files=("data/work/caccf_summary.json",),
    ),
    Source(
        "wiki_mia",
        "US missing in action",
        "US personnel still unaccounted for, from Wikipedia's lists, placed by the place each entry names.",
        size_mb=1,
        time_text="about 1 minute",
        licence="CC BY-SA 4.0 (Wikipedia)",
        needs=("gns", "hes_gazetteer"),
        store_ids=("wiki_mia",),
        raw_folders=("wiki_mia",),
        work_files=("data/work/wiki_mia_summary.json",),
    ),
    Source(
        "vn_relics",
        "Vietnam's listed historic sites",
        "War-related national relics from Vietnamese Wikipedia: battles, bases, prisons, tunnels and shoot-down sites.",
        size_mb=30,
        time_text="about 5 minutes",
        licence="CC BY-SA 4.0 (Wikipedia), with CC0 and ODbL data",
        needs=("gns",),
        store_ids=("vn_relics",),
        raw_folders=("vn_relics",),
        work_files=("data/work/vn_relics_report.json",),
    ),
    Source(
        "wikidata_osm",
        "Battles, bases and crash sites",
        "Battles, bases, airfields, crash sites and war memorials from Wikidata and OpenStreetMap.",
        size_mb=10,
        time_text="about 5 minutes",
        licence="CC0 (Wikidata); ODbL 1.0, © OpenStreetMap contributors",
        needs=("gns",),
        produces=("data/raw/osm/vn_boundary.json",),
        store_ids=("wikidata", "osm"),
        raw_folders=("wikidata", "osm"),
        work_files=("data/work/wikidata_osm_report.json",),
    ),
    Source(
        "thor",
        "Air strikes",
        "US air strike records, 1965 to 1973: where each strike mission was aimed, for the area you choose.",
        size_mb=320,
        time_text="about 10 minutes",
        licence="Public domain (US Air Force); the Kaggle copy is marked CC0",
        store_ids=("thor",),
        raw_folders=("thor",),
        refresh_args=(),  # one fixed file: there is nothing newer to fetch
        standard=False,
        uses_key=True,
    ),
    Source(
        "vhpa",
        "Helicopter losses",
        "Helicopters and crews lost in the war, from the Vietnam Helicopter Pilots Association website.",
        size_mb=20,
        time_text="3 to 4 hours",
        licence="Copyrighted website: only short facts are kept, for personal research",
        needs=("gns", "hes_gazetteer", "wikidata_osm"),
        store_ids=("vhpa",),
        raw_folders=("vhpa",),
        reads_raw=("osm",),
        work_files=("data/work/vhpa_summary.json",),
        refresh_args=("--refresh-index",),
        opt_in=True,
        standard=False,
        warning=(
            "The helicopter losses come from a copyrighted website, so Map Vietnam keeps only short facts "
            "from it, for your own research. It reads one page every 3 seconds so as not to burden the "
            "site: about 4,000 pages, 3 to 4 hours. You can stop it and carry on later; the pages "
            "already read are kept."
        ),
        yields=True,
    ),
)


class RecordsError(ValueError):
    """A request that cannot be honoured. str() is plain words for the person; `status` is the
    HTTP status the server answers with, and `extra` goes into its JSON answer beside "error"."""

    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.status, self.extra = status, extra


class Cancelled(Exception):
    """The job was stopped on purpose (cancelled, removed, or stepping aside for other jobs)."""


class Failed(Exception):
    """The job did not work. str() is plain words; `detail` is the ingester's own last message;
    `wants_key` is set when it stopped because Kaggle asked for a key."""

    def __init__(self, message, detail=None, wants_key=False):
        super().__init__(message)
        self.detail, self.wants_key = detail, wants_key


@dataclass
class Job:
    """One source being added (action "add") or removed ("remove").

    `progress` is 0..1 within what the ingester is doing now, or None when it cannot say;
    `status_text` is its plain-words note. `options` are the checked choices for this run (the
    air-strike area). `refresh` asks the ingester to download again. `aside` is set when the job
    is being stopped only to let others go first: it goes back in the queue instead of ending.
    `wants_key` is set on a failed job whose ingester stopped because Kaggle asked for a key."""

    source: Source
    action: str = "add"
    state: str = "queued"
    progress: float | None = None
    status_text: str = "Waiting for its turn"
    error: str | None = None
    error_detail: str | None = None
    wants_key: bool = False
    options: dict = field(default_factory=dict)
    refresh: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    aside: bool = False
    outcome: str | None = None  # how the last run ended: added, removed, cancelled, failed, waiting


# ---- small helpers -----------------------------------------------------------------------------
def parse_progress(line):
    """(fraction or None, note) from an ingester's "progress 0.42 Downloading ..." line (see
    scripts/ingest/common.py), or None for any other line."""
    parts = line.split(None, 2)
    if len(parts) < 2 or parts[0] != "progress":
        return None
    note = parts[2].strip() if len(parts) > 2 else ""
    if parts[1] == "-":
        return None, note
    try:
        return min(max(float(parts[1]), 0.0), 1.0), note
    except ValueError:
        return None


def read_env(path):
    """{name: value} of the KEY=VALUE lines in a .env file ({} if there is none), read the way
    scripts/ingest/common.load_env reads them: comments and blank values are skipped, "export"
    and one pair of quotes are dropped."""
    values = {}
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        name, value = env_line(line)
        if name and value:
            values[name] = value
    return values


def env_line(line):
    """(name, value) of one .env line (value "" for a line like "KAGGLE_KEY="), or (None, None)
    for a comment, a blank line or anything else that is not a setting."""
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None, None
    name, value = text.split("=", 1)
    name, value = name.strip().removeprefix("export ").strip(), value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return name, value


def write_env(path, updates):
    """Set (value) or delete (None) names in a .env file, leaving every other line as it is, and
    make the file readable by its owner only."""
    path = Path(path)
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [ln for ln in lines if env_line(ln)[0] not in updates]
    kept += [f"{name}={value}" for name, value in updates.items() if value is not None]
    # Created private from the start, so the key is never readable by others, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(kept) + "\n" if kept else "")
    os.chmod(path, 0o600)


def check_area(value):
    """[west, south, east, north] from a request's "area", rounded to 5 decimals (about a metre);
    RecordsError in plain words if it is not a box inside AREA_LIMITS."""
    numbers = isinstance(value, (list, tuple)) and len(value) == 4
    if not numbers or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
        raise RecordsError("An area is four numbers: west, south, east, north, in degrees.")
    w, s, e, n = (round(float(v), 5) for v in value)
    if not all(math.isfinite(v) for v in (w, s, e, n)):
        raise RecordsError("An area is four numbers: west, south, east, north, in degrees.")
    if not (w < e and s < n):
        raise RecordsError("An area needs west smaller than east, and south smaller than north.")
    lw, ls, le, ln = AREA_LIMITS
    if not (lw <= w and e <= le and ls <= s and n <= ln):
        raise RecordsError(
            f"The area must lie in or near Vietnam: between {lw:g} and {le:g} degrees east, "
            f"and {ls:g} and {ln:g} degrees north."
        )
    return [w, s, e, n]


def area_text(area):
    """An area in words: its edges and its rough size on the ground."""
    w, s, e, n = (f"{v:.3f}".rstrip("0").rstrip(".") for v in area)  # to about 100 m: enough to read
    # A degree of latitude is about 110.6 km here; a degree of longitude shrinks with latitude.
    wide = (area[2] - area[0]) * 111.3 * math.cos(math.radians((area[1] + area[3]) / 2))
    tall = (area[3] - area[1]) * 110.6
    text = f"{w} to {e}° E, {s} to {n}° N (about {wide:.0f} by {tall:.0f} km)"
    return f"Around Cao Lãnh: {text}" if tuple(area) == DEFAULT_AREA else text


def plain_error(source, lines):
    """(plain-words message, the ingester's own last line) for a run that failed, from its
    output. An ingester that stops on purpose prints one plain sentence, which is passed on as
    it is; a crash (a traceback) is put in words here, with the log to look in."""
    last = next((ln.strip() for ln in reversed(lines) if ln.strip()), "")
    crashed = any(ln.startswith("Traceback (most recent call last)") for ln in lines)
    if last and not crashed:
        return last, None
    text = " ".join(lines[-5:])
    if any(word in text for word in NETWORK_WORDS):
        message = (
            f"Could not download “{source.title}”. Check the internet connection, or try again later: "
            "the archive may be busy. What was already downloaded is kept."
        )
    elif "database is locked" in text:
        message = "The record database was busy. Try again in a minute."
    elif "No space left" in text:
        message = "The disk is full. Free some space, then try again."
    else:
        message = (
            f"Adding “{source.title}” did not work. Try again; if it keeps failing, the details are in "
            f"data/work/setup/logs/{source.id}.log."
        )
    return message, last[:300] or None


def local_now():
    """The time now, with its UTC offset, to the second: what a marker records as added_at."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Records:
    """The sources, what has been added under `root`, and the queue of jobs.

    Thread-safe: the server calls listing/add/remove/cancel from request threads while one worker
    thread (start_worker) or the caller itself (run_pending) works through the queue.
    `on_change()` is called after the stored records changed (the server drops its caches).
    """

    def __init__(self, root=ROOT, sources=SOURCES, python=sys.executable, db_path=None, on_change=None):
        self.root = Path(root)
        self.sources = tuple(sources)
        self.by_id = {s.id: s for s in self.sources}
        self.python = python
        self.db_path = Path(db_path) if db_path else self.root / "data" / "evidence.sqlite"
        self.raw = self.root / "data" / "raw"
        self.state_dir = self.root / "data" / "work" / "setup"
        self.env_file = self.root / ".env"
        self.on_change = on_change
        self.echo = None  # the command line sets this to print what an ingester says
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._jobs = {}  # id -> Job, for sources queued, running or failed
        self._queue = deque()  # ids waiting, in the order they were asked for
        self._current = None  # the Job the worker has taken, if any
        self._worker = None
        self._stopping = False
        self._revision = 0  # goes up whenever the stored records changed
        self._facts = (None, {})  # (database file signature, store_facts result)
        self._raw_sizes = {}  # folder name -> (folder mtime, bytes)
        self._other_busy = False  # another process was on a job when last looked

    # ---- what is on disk -------------------------------------------------------------------
    def stamp(self, source):
        """The file that marks a source as added."""
        return self.state_dir / f"{source.id}.done"

    def log_path(self, source):
        """Where the last run of a source's ingester wrote what it printed."""
        return self.state_dir / "logs" / f"{source.id}.log"

    def is_added(self, source):
        """True when the source was added and its results are still there: its marker, the
        database and every file it makes."""
        return (
            self.stamp(source).exists()
            and self.db_path.exists()
            and all((self.root / p).exists() for p in source.produces)
        )

    def read_stamp(self, source):
        """{"added_at", "options"} from a source's marker; {} when it has none. Markers written
        by older versions hold only the time."""
        try:
            text = self.stamp(source).read_text().strip()
        except OSError:
            return {}
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        return data if isinstance(data, dict) else {"added_at": text or None}

    def choices(self):
        """The answers kept in choices.json ({} before any)."""
        data = read_json(self.state_dir / "choices.json", {})
        return data if isinstance(data, dict) else {}

    def set_choices(self, **updates):
        """Change some of the kept answers."""
        write_json(self.state_dir / "choices.json", {**self.choices(), **updates})

    def first_run_marks(self):
        """How many signs there are that records were added here before: sources with a marker,
        plus one if a source ever finished (it may have been removed since)."""
        return sum(self.stamp(s).exists() for s in self.sources) + bool(self.choices().get("ever_added"))

    def first_run(self):
        """True when nothing has ever been added and the welcome has not been dismissed."""
        choices = self.choices()
        if choices.get("welcome_dismissed") or choices.get("agreed"):
            return False
        return self.first_run_marks() == 0

    def store_facts(self, busy=False):
        """{store source id: {"records", "held", "extent"}} from the database: the records
        shown on the map, the withdrawn ones still held in someone's case, and the box
        [w, s, e, n] the records lie in. Re-read only when the database file changes; an
        unreadable or busy database answers with what was read last.

        Counting reads the whole table, and an ingester saving its records changes the file
        many times a second, so with `busy` (a job is running) what was counted last is good
        enough: the count is taken afresh when the job ends (_changed)."""
        try:
            stat = self.db_path.stat()
        except OSError:
            return {}
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._facts[0] == signature or (busy and self._facts[0] is not None):
            return self._facts[1]
        facts = {}
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=2)) as db:
                rows = db.execute(
                    "select source, count(*), count(withdrawn_at), min(lon), min(lat), max(lon), max(lat) "
                    "from evidence group by source"
                ).fetchall()
        except sqlite3.Error:
            return self._facts[1]
        for sid, total, held, w, s, e, n in rows:
            facts[sid] = {"records": total - held, "held": held, "extent": [w, s, e, n]}
        self._facts = (signature, facts)
        return facts

    def raw_bytes(self, source):
        """Bytes of a source's downloads kept on disk. A folder is measured again only when a
        file is added to it or removed (its modification time changes)."""
        total = 0
        for name in source.raw_folders:
            folder = self.raw / name
            try:
                mtime = folder.stat().st_mtime_ns
            except OSError:
                continue
            if self._raw_sizes.get(name, (None,))[0] != mtime:
                self._raw_sizes[name] = (mtime, path_bytes(folder))
            total += self._raw_sizes[name][1]
        return total

    # ---- the Kaggle key ------------------------------------------------------------------------
    def kaggle_json(self):
        """Where Kaggle's own tools keep kaggle.json: $KAGGLE_CONFIG_DIR, else ~/.kaggle."""
        return Path(os.environ.get("KAGGLE_CONFIG_DIR") or Path.home() / ".kaggle") / "kaggle.json"

    def key_status(self):
        """{"present", "source"}: whether a Kaggle key is saved, and where: "env file" (.env),
        "environment" (variables set outside it), "kaggle.json" (Kaggle's own file), or None.
        The values themselves never leave this method."""
        file_values = read_env(self.env_file)
        if all(file_values.get(name) for name in KAGGLE_NAMES):
            source = "env file"
        elif all(os.environ.get(name) for name in KAGGLE_NAMES):
            source = "environment"
        elif self._kaggle_json_has_key():
            source = "kaggle.json"
        else:
            source = None
        return {"present": source is not None, "source": source}

    def wanting_key(self):
        """Ids of the sources that failed because Kaggle asked for a key."""
        with self._lock:
            return [i for i, job in self._jobs.items() if job.state == "failed" and job.wants_key]

    def _kaggle_json_has_key(self):
        config = read_json(self.kaggle_json())
        return isinstance(config, dict) and bool(config.get("username") and config.get("key"))

    def save_key(self, username, key):
        """Save a Kaggle user name and key in .env (replacing only those two lines). Only their
        form is checked; Kaggle says whether it accepts them when the air strikes are added again.
        No message from here ever contains either value."""
        if not isinstance(username, str) or not isinstance(key, str):
            raise RecordsError("Send the Kaggle user name and key as text.")
        username, key = username.strip(), key.strip()
        if not KAGGLE_USERNAME_RE.fullmatch(username):
            raise RecordsError(
                "That does not look like a Kaggle user name: letters, digits, dots, dashes and underscores only."
            )
        if not KAGGLE_KEY_RE.fullmatch(key):
            raise RecordsError(
                "That does not look like a Kaggle key: a long run of letters and digits, with no spaces."
            )
        write_env(self.env_file, dict(zip(KAGGLE_NAMES, (username, key), strict=True)))
        # An ingester takes a variable from the environment before .env, and this process may
        # already hold the old key there (scripts/ingest/common.py loads .env on import): the
        # new one must win for the ingesters this process starts.
        os.environ.update(zip(KAGGLE_NAMES, (username, key), strict=True))

    def forget_key(self):
        """Take the Kaggle user name and key out of .env (only those two lines; kaggle.json in
        the home folder is never touched), and out of this process's environment if they came
        from that file."""
        old = read_env(self.env_file)
        if self.env_file.exists():
            write_env(self.env_file, dict.fromkeys(KAGGLE_NAMES))
        for name in KAGGLE_NAMES:
            if old.get(name) and os.environ.get(name) == old[name]:
                del os.environ[name]

    def _secrets(self):
        """Values that must never reach a log or an error message, wherever they came from."""
        values = {read_env(self.env_file).get("KAGGLE_KEY"), os.environ.get("KAGGLE_KEY")}
        config = read_json(self.kaggle_json())
        if isinstance(config, dict) and isinstance(config.get("key"), str):
            values.add(config["key"])
        return [v for v in values if v and len(v) >= 8]

    # ---- state -----------------------------------------------------------------------------
    def needs(self, source):
        """Every source `source` needs, directly or through another, in running order."""
        wanted, pending = set(), list(source.needs)
        while pending:
            name = pending.pop()
            if name not in wanted:
                wanted.add(name)
                pending += self.by_id[name].needs
        return [s for s in self.sources if s.id in wanted]

    def dependents(self, source, among):
        """The sources in `among` (ids) that need `source`, directly or through another."""
        return [s for s in self.sources if s.id in among and source in self.needs(s)]

    def _in_use(self):
        """Ids of sources that are added, or on their way in (call with the lock held)."""
        adding = {i for i, job in self._jobs.items() if job.action == "add" and job.state != "failed"}
        return adding | {s.id for s in self.sources if self.is_added(s)}

    def options_of(self, source, facts=None):
        """The choices a source was added with: from its marker, or, for air strikes added by an
        older version (which recorded none), from where the stored strikes lie. None if it has
        no choices. Returns (options, recorded)."""
        if source.id != "thor":
            return None, True
        options = self.read_stamp(source).get("options")
        if isinstance(options, dict) and options.get("area"):
            return {"area": list(options["area"])}, True
        extent = ((facts or self.store_facts()).get("thor") or {}).get("extent")
        if not extent or None in extent:
            return {"area": list(DEFAULT_AREA)}, False
        w, s, e, n = DEFAULT_AREA
        if w <= extent[0] and s <= extent[1] and extent[2] <= e and extent[3] <= n:
            return {"area": list(DEFAULT_AREA)}, False
        # Loaded by hand with another area: the box the strikes lie in, widened to 0.01 degree.
        box = [math.floor(extent[0] * 100) / 100, math.floor(extent[1] * 100) / 100]
        box += [math.ceil(extent[2] * 100) / 100, math.ceil(extent[3] * 100) / 100]
        return {"area": box}, False

    def other_worker(self):
        """{"id", "pid", "action"} of the job another process is on right now, or None. A record
        left by a process that died is ignored: then nobody holds the lock."""
        current = read_json(self.state_dir / "current.json")
        if not isinstance(current, dict) or current.get("pid") == os.getpid():
            return None
        with self.try_exclusive() as free:
            return None if free else current

    def listing(self):
        """What GET /api/records answers: every listed source with its description and state,
        the key status ("needed" only while a source has failed because Kaggle asked for a key),
        whether anything is queued or running, whether this is the first run, and a revision
        number that changes whenever the stored records changed."""
        elsewhere = self.other_worker()
        key = self.key_status()
        with self._lock:
            if self._other_busy and not elsewhere:
                self._changed()  # the command line just finished a job: the records may differ
            self._other_busy = bool(elsewhere)
            facts = self.store_facts(busy=bool(elsewhere) or self._current is not None)
            added = {s.id for s in self.sources if self.is_added(s)}
            in_use = self._in_use()
            sources = [self._describe(s, added, in_use, facts, elsewhere) for s in self.sources if s.listed]
            busy = bool(elsewhere) or any(job.state != "failed" for job in self._jobs.values())
            return {
                "sources": sources,
                "key": {"needed": any(s["wants_key"] for s in sources), **key},
                "busy": busy,
                "first_run": self.first_run(),
                "revision": self._revision,
            }

    def _describe(self, source, added, in_use, facts, elsewhere):
        """One source as the listing gives it (call with the lock held)."""
        job = self._jobs.get(source.id)
        is_added = source.id in added
        removing = False
        state, progress, status_text, error, detail = ("added" if is_added else "available"), None, None, None, None
        if job:
            state, progress, status_text = job.state, job.progress, job.status_text
            error, detail, removing = job.error, job.error_detail, job.action == "remove"
            if state == "failed":
                status_text = None
        elif elsewhere and elsewhere.get("id") == source.id:
            state, removing = "running", elsewhere.get("action") == "remove"
            status_text = "Being removed in another window" if removing else "Being added in another window"
        stamp = self.read_stamp(source) if is_added else {}
        counts = [facts.get(sid, {}) for sid in source.store_ids]
        entry = {
            "id": source.id,
            "title": source.title,
            "description": source.description,
            "size_mb": source.size_mb,
            "time_text": source.time_text,
            "licence": source.licence,
            "needs": list(source.needs),
            "also_adds": [{"id": s.id, "title": s.title} for s in self.needs(source) if s.id not in added],
            "needed_by": [{"id": s.id, "title": s.title} for s in self.dependents(source, in_use)],
            "opt_in": source.opt_in,
            "warning": source.warning or None,
            "standard": source.standard,
            "wants_key": bool(job and job.state == "failed" and job.wants_key),
            "state": state,
            "removing": removing,
            "progress": progress,
            "status_text": status_text,
            "error": error,
            "error_detail": detail,
            "records": sum(c.get("records", 0) for c in counts) if is_added else None,
            "held_in_cases": sum(c.get("held", 0) for c in counts),
            "added_at": stamp.get("added_at"),
            "disk_bytes": self.raw_bytes(source),
            "options": None,
            "area_text": None,
        }
        if source.id == "thor":
            # The area on its way in while queued or running; otherwise the area whose strikes
            # are in the store (a run that failed changed nothing), or the one that was tried.
            if job and job.action == "add" and job.options and job.state != "failed":
                options, recorded = job.options, True
            elif is_added:
                options, recorded = self.options_of(source, facts)
            else:
                options, recorded = (job.options or None) if job and job.action == "add" else None, True
            entry["options"] = options
            entry["area_text"] = area_text(options["area"]) if options else None
            entry["area_recorded"] = recorded
            entry["area_choice"] = {
                "default": list(DEFAULT_AREA),
                "default_text": area_text(DEFAULT_AREA),
                "within": list(AREA_LIMITS),
                "max_records": MAX_STRIKES,
                "all_vietnam": {"allowed": False, "reason": ALL_VIETNAM_REFUSED},
            }
        return entry

    def state_of(self, source_id):
        """The state name of one source."""
        with self._lock:
            job = self._jobs.get(source_id)
            if job:
                return job.state
            return "added" if self.is_added(self.by_id[source_id]) else "available"

    # ---- changing it -----------------------------------------------------------------------
    def known(self, ids, listed_only=True):
        """The sources named by `ids`, in running order; RecordsError naming any id that is not
        a source (or, with listed_only, not one the viewer lists)."""
        if not isinstance(ids, (list, tuple)) or not ids or not all(isinstance(i, str) for i in ids):
            raise RecordsError('send {"ids": ["<source id>", ...]}')
        unknown = [i for i in ids if i not in self.by_id or (listed_only and not self.by_id[i].listed)]
        if unknown:
            raise RecordsError(f"not a record source: {', '.join(unknown)}")
        return [s for s in self.sources if s.id in ids]

    def check_options(self, options):
        """{source id: checked options} from a request's "options"; RecordsError if a source has
        no such choice. Only the air strikes have one: {"area": [w, s, e, n]}, or
        {"all_vietnam": true}, which is refused with the reason (ALL_VIETNAM_REFUSED)."""
        if not isinstance(options, dict):
            raise RecordsError('"options" must be an object, for example {"thor": {"area": [105, 10, 106, 11]}}.')
        checked = {}
        for sid, value in options.items():
            if sid != "thor" or not isinstance(value, dict) or set(value) - {"area", "all_vietnam"}:
                raise RecordsError(
                    'The only choice is the air-strike area: {"thor": {"area": [west, south, east, north]}}.'
                )
            if value.get("all_vietnam"):
                raise RecordsError(ALL_VIETNAM_REFUSED)
            if value.get("area") is not None:
                checked[sid] = {"area": check_area(value["area"])}
        return checked

    def add(self, ids, options=None, accept=(), refresh=False, listed_only=True):
        """Queue the sources `ids`, each after the sources it needs that are not added yet.

        A source already queued or running is left as it is; a failed one is tried again. One
        already added is left alone unless `refresh` (download it again) or, for the air
        strikes, a different area is asked for: then it is run again, and a sync replaces its
        records. An opt-in source must be named in `accept`, or nothing at all is queued.
        Returns the ids queued, in the order they will run."""
        named = self.known(ids, listed_only)
        options = self.check_options(options or {})
        if not isinstance(accept, (list, tuple)) or not all(isinstance(i, str) for i in accept):
            raise RecordsError('"accept" must be a list of source ids.')
        unaccepted = [s for s in named if s.opt_in and s.id not in accept]
        if unaccepted:
            raise RecordsError(unaccepted[0].warning, needs_accept=[s.id for s in unaccepted])
        with self._lock:
            # A source whose removal has started counts as gone; one whose removal is only
            # waiting counts as it is on disk, and that removal is dropped below: adding wins.
            going = {i for i, job in self._jobs.items() if job.action == "remove" and job is self._current}
            added = {s.id for s in self.sources if self.is_added(s)} - going
            in_flight = {i for i, job in self._jobs.items() if job.action == "add" and job.state != "failed"}
            plan = {}  # id -> the options to run it with
            wanted = set()  # every source this request needs to end up added
            for source in named:
                wanted.add(source.id)
                if source.id in in_flight:
                    continue
                chosen = options.get(source.id) or self._kept_options(source, source.id in added)
                if source.id in added and not refresh and not self._area_differs(source, chosen):
                    continue
                plan[source.id] = chosen
                for need in self.needs(source):
                    wanted.add(need.id)
                    if need.id not in added and need.id not in in_flight:
                        plan.setdefault(need.id, self._kept_options(need, False))
            for sid in wanted:
                job = self._jobs.get(sid)
                if job and job.action == "remove" and job is not self._current:
                    with contextlib.suppress(ValueError):  # a removal that failed is not in the queue
                        self._queue.remove(sid)
                    del self._jobs[sid]
            queued = [s.id for s in self.sources if s.id in plan]
            for sid in queued:
                # Asking for a source again overrules an earlier stop of it.
                self.cancel_path(sid).unlink(missing_ok=True)
                self._jobs[sid] = Job(self.by_id[sid], options=plan[sid] or {}, refresh=refresh and sid in ids)
                self._queue.append(sid)
            self._make_way()
            self._wake.notify_all()
        return queued

    def _kept_options(self, source, is_added):
        """The options to run a source with when the request gives none: the ones it was added
        with, else the defaults."""
        if source.id != "thor":
            return {}
        return self.options_of(source)[0] if is_added else {"area": list(DEFAULT_AREA)}

    def _area_differs(self, source, chosen):
        """True when `chosen` asks for another air-strike area than the one stored."""
        return source.id == "thor" and list(chosen.get("area", [])) != list(self.options_of(source)[0]["area"])

    def _make_way(self):
        """If the running job is a slow one that resumes where it stopped (Source.yields) and
        another job is now waiting, stop it and put it back in the queue: _take gives it its
        turn again once nothing else is waiting. Call with the lock held."""
        job = self._current
        if not job or not job.source.yields or job.action != "add" or job.cancel.is_set():
            return
        if any(not self._jobs[i].source.yields or self._jobs[i].action == "remove" for i in self._queue):
            job.aside = True
            self._end(job)

    def remove(self, ids, cascade=False, listed_only=True):
        """Remove the sources `ids`: stop them if queued or running, then delete their records
        (those in a case are kept, marked withdrawn), markers, downloads and the files they made.

        A source that added sources need is refused (RecordsError, status 409, naming them in
        "also_removes") unless `cascade`, which removes those too, first. The deleting itself is
        a job, so it waits its turn behind whatever is running. Returns the ids being removed."""
        named = self.known(ids, listed_only)
        with self._lock:
            in_use = self._in_use()
            wanted = {s.id for s in named}
            for source in named:
                wanted |= {s.id for s in self.dependents(source, in_use)}
            extra = [s for s in self.sources if s.id in wanted and s not in named]
            if extra and not cascade:
                titles = ", ".join(f"“{s.title}”" for s in extra)
                raise RecordsError(
                    f"Other records were made with these and would have to go too: {titles}. "
                    "Remove them as well, or keep these.",
                    status=409,
                    also_removes=[{"id": s.id, "title": s.title} for s in extra],
                )
            elsewhere = self.other_worker()
            removing = []
            # Sources that need others go first, so nothing is ever left without what it was made from.
            for source in reversed([s for s in self.sources if s.id in wanted]):
                sid = source.id
                job = self._jobs.get(sid)
                if job and job.action == "remove" and job.state != "failed":
                    removing.append(sid)
                    continue
                with contextlib.suppress(ValueError):
                    self._queue.remove(sid)
                if elsewhere is not None:
                    self.request_cancel(sid)  # the other process stops it; this removal then takes its turn
                if job is not None and job is self._current:
                    self._end(job)  # the worker lets go of it first, then reaches the removal below
                elif not self._leaves_anything(source):
                    if self._jobs.pop(sid, None):  # only waiting or failed, with nothing on disk
                        removing.append(sid)
                    continue
                self._jobs[sid] = Job(source, action="remove")
                self._queue.append(sid)
                removing.append(sid)
            self._make_way()
            self._wake.notify_all()
        return removing

    def _leaves_anything(self, source):
        """True when a source has a marker, records, downloads or files to delete."""
        facts = self.store_facts()
        return (
            self.stamp(source).exists()
            or any(facts.get(sid, {}).get("records") for sid in source.store_ids)
            or any((self.raw / name).exists() for name in source.raw_folders)
            or any((self.root / p).exists() for p in (*source.produces, *source.work_files))
        )

    def cancel(self, ids, listed_only=True):
        """Stop adding the sources `ids`: a queued one leaves the queue, a running one is ended
        (what it downloaded is kept), and a failed one forgets its failure. Each goes back to
        what the disk says: available, or added if it was being added again. A removal that has
        not started is cancelled too; one that is running finishes. Returns the ids stopped."""
        named = self.known(ids, listed_only)
        elsewhere = self.other_worker()
        stopped = []
        with self._lock:
            for source in named:
                sid = source.id
                job = self._jobs.get(sid)
                if elsewhere is not None and elsewhere.get("id") == sid and elsewhere.get("action") == "add":
                    self.request_cancel(sid)
                    stopped.append(sid)
                if not job:
                    continue
                if job is self._current:
                    if job.action == "add":
                        self._end(job)
                        stopped.append(sid)
                    continue
                with contextlib.suppress(ValueError):
                    self._queue.remove(sid)
                del self._jobs[sid]
                stopped.append(sid)
            self._wake.notify_all()
        return list(dict.fromkeys(stopped))

    def _end(self, job):
        """Stop a running job's ingester (call with the lock held). The worker sees it end and
        decides what that means: cancelled, or, with job.aside, back in the queue."""
        if not job.aside:
            job.cancel.set()
        proc = job.proc  # read once: the worker may clear it at any moment
        if proc and proc.poll() is None:
            proc.terminate()

    def stop(self, timeout=10):
        """Stop everything, for quitting: forget the queue, end the running ingester, and wait
        (up to `timeout` seconds) for the worker to let go. A removal that is running finishes."""
        with self._lock:
            self._stopping = True
            for sid in list(self._queue):
                del self._jobs[sid]
            self._queue.clear()
            job = self._current
            if job and job.action == "add":
                job.aside = False
                self._end(job)
            self._wake.notify_all()
            return self._wake.wait_for(lambda: self._current is None, timeout)

    # ---- coordination with another process -----------------------------------------------------
    def cancel_path(self, source_id):
        """The file that asks whichever process is adding `source_id` to stop."""
        return self.state_dir / "cancel" / source_id

    def request_cancel(self, source_id):
        """Leave a cancel request for `source_id` (see cancel and remove)."""
        path = self.cancel_path(source_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def _mark_current(self, source_id=None, action=None):
        """Record in current.json the job this process is on (only the lock holder writes it),
        or delete the record when source_id is None."""
        marker = self.state_dir / "current.json"
        if source_id is None:
            marker.unlink(missing_ok=True)
        else:
            write_json(marker, {"id": source_id, "pid": os.getpid(), "action": action})

    @contextlib.contextmanager
    def exclusive(self, on_wait=None):
        """Hold data/work/setup/worker.lock: only one process adds or removes records at a time.
        on_wait() is called once if another process holds it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "worker.lock", "a+") as f:
            if not lock_file(f, blocking=False):
                if on_wait:
                    on_wait()
                lock_file(f)
            try:
                yield
            finally:
                unlock_file(f)

    @contextlib.contextmanager
    def try_exclusive(self):
        """Like exclusive(), but never waits: yields True holding the lock, or False when
        someone else (another process, or this process's worker) holds it."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.state_dir / "worker.lock", "a+") as f:
            free = lock_file(f, blocking=False)
            try:
                yield free
            finally:
                if free:
                    unlock_file(f)

    # ---- working through the queue -------------------------------------------------------------
    def start_worker(self):
        """Start the background worker thread (once); it works whenever something is queued."""
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._work_forever, name="records", daemon=True)
                self._worker.start()

    def _work_forever(self):
        while True:
            with self._lock:
                while not self._queue:
                    self._wake.wait()
            try:
                self.run_pending()
            except Exception as e:  # never let one surprise end the worker: say so, and go on
                print(f"records: {e}", file=sys.stderr, flush=True)

    def _take(self):
        """The next job to run, taken off the queue: the first that is not a slow, resumable
        one, so those only run when nothing else is waiting. None when the queue is empty.
        Call with the lock held."""
        if not self._queue:
            return None
        quick = [i for i in self._queue if not self._jobs[i].source.yields or self._jobs[i].action == "remove"]
        sid = quick[0] if quick else self._queue[0]
        self._queue.remove(sid)
        return self._jobs[sid]

    def run_pending(self, report=None):
        """Run every queued job, one after another, in this thread. `report(job)` is called on
        every change of state or progress (the command line prints from it)."""
        report = report or (lambda job: None)
        while True:
            with self._lock:
                job = self._take()
                if job is None:
                    return
                # Reset here, under the lock that makes it the running job: a request that asks
                # it to step aside from now on is never lost.
                job.aside, job.outcome = False, None
                self._current = job
            try:
                self._run(job, report)
            finally:
                with self._lock:
                    self._current = None
                    self._wake.notify_all()

    def wait_idle(self, timeout=None):
        """Block until nothing is queued or running (for tests and the command line)."""
        with self._lock:
            return self._wake.wait_for(lambda: not self._queue and self._current is None, timeout)

    def wait_stopped(self, ids, timeout):
        """Block (up to `timeout` seconds) until none of `ids` is the running job, so a request
        that stopped one can answer with it already stopped."""
        with self._lock:
            return self._wake.wait_for(lambda: self._current is None or self._current.source.id not in ids, timeout)

    def wait_removed(self, ids, timeout):
        """Block (up to `timeout` seconds) until none of `ids` is still being removed, so a
        request can usually answer with the removal already done."""

        def done():
            return not any(i in self._jobs and self._jobs[i].action == "remove" for i in ids)

        with self._lock:
            return self._wake.wait_for(done, timeout)

    def _set(self, job, report, state=None, progress=None, text=None):
        with self._lock:
            if state:
                job.state = state
            job.progress = progress
            if text is not None:
                job.status_text = text
            self._wake.notify_all()
        report(job)

    def _changed(self):
        """The stored records changed: forget what was counted, tell the server, bump the revision."""
        with self._lock:
            self._facts = (None, {})
            self._revision += 1
        if self.on_change:
            self.on_change()

    def _watch(self, job, finished):
        """Until `finished` is set, honour a cancel request from another process."""
        while not finished.wait(CANCEL_POLL_S):
            if not job.cancel.is_set() and self.cancel_path(job.source.id).exists():
                with self._lock:
                    self._end(job)

    def _run(self, job, report):
        """Do one job, recording how it ended in the job instead of raising."""
        sid = job.source.id
        try:
            waiting = "Waiting for another Map Vietnam window to finish"
            with self.exclusive(on_wait=lambda: self._set(job, report, text=waiting)):
                if self.cancel_path(sid).exists() and job.action == "add":
                    job.cancel.set()  # stopped by another process while it waited here
                if job.cancel.is_set():
                    raise Cancelled()
                self._mark_current(sid, job.action)
                finished = threading.Event()
                if job.action == "add":
                    threading.Thread(target=self._watch, args=(job, finished), daemon=True).start()
                try:
                    if job.action == "remove":
                        self._set(job, report, "running", text="Removing these records")
                        self._remove_now(job.source)
                    else:
                        self._set(job, report, "running", text="Starting")
                        self._ingest(job, report)
                finally:
                    finished.set()
                    self._mark_current(None)
            self._finish(job, "removed" if job.action == "remove" else "added")
        except Cancelled:
            if job.aside and not job.cancel.is_set() and not self._stopping:
                with self._lock:  # back in the queue: _take runs it when nothing else waits
                    job.state, job.progress, job.outcome = "queued", None, "waiting"
                    job.status_text = "Paused while other records are added. It carries on afterwards."
                    self._queue.append(sid)
            else:
                self.cancel_path(sid).unlink(missing_ok=True)
                self._finish(job, "cancelled")
        except Failed as e:
            with self._lock:
                job.state, job.progress, job.error, job.error_detail = "failed", None, str(e), e.detail
                job.outcome, job.wants_key = "failed", e.wants_key
        except Exception as e:  # anything unforeseen belongs to this job only: the worker must go on
            with self._lock:
                job.state, job.progress, job.outcome = "failed", None, "failed"
                job.error = f"Something went wrong inside Map Vietnam while working on “{job.source.title}”."
                job.error_detail = f"{type(e).__name__}: {e}"[:300]
        finally:
            with self._lock:
                job.proc = None
            # Even a run that was stopped or failed may have changed something (an ingester that
            # writes two sources had finished the first): count again, whatever happened.
            self._changed()
            report(job)

    def _finish(self, job, outcome):
        """A job ended without a failure: forget it, so the source's state comes from disk again."""
        with self._lock:
            if self._jobs.get(job.source.id) is job:
                del self._jobs[job.source.id]
            job.outcome, job.progress = outcome, None

    def command(self, job):
        """The child process that adds a job's source: its fixed ingester, plus the arguments
        for a fresh download and, for the air strikes, the checked area and the strike limit."""
        source = job.source
        cmd = [self.python, str(self.root / source.command[0]), *source.command[1:]]
        if job.refresh:
            cmd += source.refresh_args
        if source.id == "thor":
            area = job.options.get("area") or DEFAULT_AREA
            cmd += ["--bbox", ",".join(str(v) for v in area), "--max-strikes", str(MAX_STRIKES)]
        return cmd

    def child_env(self):
        """Environment for an ingester: the import path the tests use (pytest.ini), unbuffered
        output, and the switch that makes it print progress lines."""
        paths = [str(self.root / "scripts"), str(self.root / "scripts" / "ingest"), os.environ.get("PYTHONPATH", "")]
        return {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join(p for p in paths if p),
            "MAPVIETNAM_PROGRESS": "1",
        }

    def _ingest(self, job, report):
        """Run a source's ingester, following its progress lines; then mark the source added.
        Raises Failed (in plain words) or Cancelled. Call only while holding exclusive()."""
        source = job.source
        missing = [s.title for s in self.needs(source) if not self.is_added(s)]
        if missing:
            titles = ", ".join(f"“{t}”" for t in missing)
            raise Failed(f"Not added: it needs {titles} first, and that did not finish.")
        log_path = self.log_path(source)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        secrets = self._secrets() if source.uses_key else []
        lines = []
        with open(log_path, "w") as log:
            log.write(f"{source.id} started {local_now()}\n")
            with self._lock:
                if job.cancel.is_set() or job.aside:
                    raise Cancelled()
                proc = job.proc = subprocess.Popen(
                    self.command(job),
                    cwd=self.root,
                    env=self.child_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                )
            try:
                for line in proc.stdout:
                    for secret in secrets:
                        line = line.replace(secret, "[hidden]")
                    reported = parse_progress(line)
                    if reported is not None:
                        self._set(job, report, progress=reported[0], text=reported[1] or None)
                        continue
                    log.write(line)
                    lines.append(line.rstrip("\n"))
                    del lines[:-50]  # only the end matters for the error message
                    if self.echo:
                        self.echo(line)
                proc.wait()
            finally:
                # Whatever went wrong here (a full disk, say), never leave the ingester running.
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
            if proc.returncode != 0:
                log.write(f"exit status {proc.returncode}\n")
        if job.cancel.is_set() or job.aside:
            raise Cancelled()
        if proc.returncode != 0:
            message, detail = plain_error(source, lines)
            raise Failed(message, detail, wants_key=source.uses_key and proc.returncode == KEY_EXIT)
        missing = [p for p in source.produces if not (self.root / p).exists()]
        if missing or not self.db_path.exists():
            raise Failed(
                f"“{source.title}” finished, but the files it should have made are missing. Try again.",
                "missing: " + ", ".join(missing or [self.db_path.name]),
            )
        write_json(self.stamp(source), {"added_at": local_now(), "options": job.options or None})
        if not self.choices().get("ever_added"):
            self.set_choices(ever_added=True)

    def _remove_now(self, source):
        """Delete a source: marker first (so it reads as not added even if this is cut short),
        then its records, the files it made, its log and its downloads. A download folder that
        another added source uses is kept. Call only while holding exclusive()."""
        self.stamp(source).unlink(missing_ok=True)
        if self.db_path.exists() and source.store_ids:
            with contextlib.closing(store.connect(self.db_path)) as db:
                for sid in source.store_ids:
                    store.remove_source(db, sid, reason=REMOVED_REASON)
        for path in (*source.produces, *source.work_files):
            delete(self.root / path)
        delete(self.log_path(source))
        others = [s for s in self.sources if s is not source and self.is_added(s)]
        still_used = {name for s in others for name in (*s.raw_folders, *s.reads_raw)}
        for name in source.raw_folders:
            if name not in still_used:
                delete(self.raw / name)
                self._raw_sizes.pop(name, None)


# ---- command line ----------------------------------------------------------------------------
MAP_LIBRARY = ROOT / "data" / "sources" / "map_library.json"
DISK_GB = 1  # the downloads kept and the record database, before any old maps
KEY_STEPS = """\
Kaggle usually gives the air strike file without a key, but this time it asked for one.
A free account is enough:
  1. Go to https://www.kaggle.com and sign in (or register, it is free).
  2. Click your picture (top right), then Settings.
  3. In the 'API' section click 'Create Legacy API Key' (older pages: 'Create New Token').
     Your browser downloads kaggle.json.
  4. Open kaggle.json with TextEdit: it shows your "username" and "key".
You can also do this later: in the map viewer, or by running this command again."""


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


def sheet_sizes():
    """(MB to download, MB on disk) for an average 1:50,000 sheet, how many there are, and GB on
    disk for all of them: from the map library catalogue, so the text never drifts from it.
    None if the catalogue cannot be read."""
    try:
        catalogue = json.loads(MAP_LIBRARY.read_text())
        sheets = [m["bytes"] for m in catalogue["maps"] if m["series"] == "l7014"]
        ratio = catalogue["series"]["l7014"]["disk_per_download_byte"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not sheets:
        return None
    mean = sum(sheets) / len(sheets)
    return mean / 1e6, mean * ratio / 1e6, len(sheets), sum(sheets) * ratio / 1e9


def explain_first_run(sources):
    """The plain-words list of what setup will download, how big and how long: the standard
    sources, then what is left for the person to add when they want it."""
    say("Welcome to Map Vietnam.")
    say()
    say("The first time, it needs to download public records and prepare them.")
    say("Here is what it will fetch:")
    say()
    rows = [s for s in sources if s.standard]
    for s in rows:
        amount = f"{s.size_mb} MB" if s.size_mb else "nothing to download"
        say(f"  - {s.title}\n      {amount}, {s.time_text}")
    say()
    say(f"In all: about {sum(s.size_mb for s in rows)} MB to download, and about {DISK_GB} GB of disk space.")
    say("It usually takes 10 to 20 minutes; you can leave it running. Nothing about you is sent")
    say("anywhere: it only downloads from public archives. If you stop it (Control-C) or something")
    say("fails, just run it again: it carries on where it left off.")
    for s in sources:
        if not s.standard and not s.opt_in:
            say(f"Not included: {s.title}, {s.size_mb} MB. Add them in the map viewer, where you choose")
            say(f"the area, or with: python3 start.py setup --only {s.id} --area W,S,E,N")
    say()
    say('Old maps are not downloaded now. Once the map is open, tap "Add old maps" at the bottom')
    say("to add the ones you want (or use: python3 start.py maps add). Any map can be removed again.")
    sizes = sheet_sizes()
    if sizes:
        say("A 1:50,000 sheet is about {:.0f} MB to download and {:.0f} MB on disk;".format(*sizes))
        say(f"all {sizes[2]} of them together are about {sizes[3]:.0f} GB on disk.")
    say()


def offer_kaggle_key(records):
    """Kaggle asked for a key: explain how to get one and offer to save it. True if one was saved."""
    say(KEY_STEPS)
    if not ask_yes_no("Do you have the username and key ready to paste?", default=False):
        return False
    user = input("Kaggle username: ").strip()
    key = getpass.getpass("Kaggle key (it will not show as you paste; press Return after): ").strip()
    try:
        records.save_key(user, key)
    except RecordsError as e:
        say(f"Nothing saved. {e}")
        return False
    say("Saved in .env (only you can read that file).")
    return True


def setup_plan(records, only, skip, choices):
    """The ids to add this time, and the sources left out with the reason, in running order.

    With `only`, just those (add() brings in whatever they still need); otherwise the standard
    sources, plus an optional one the person said yes to, minus the ones skipped."""
    ids, left_out = [], []
    for source in records.sources:
        if only and source.id not in only:
            continue
        if source.id in skip:
            left_out.append((source, "skipped as asked (--skip)"))
        elif source.opt_in and source.id not in only and not choices.get(source.id):
            left_out.append((source, f"optional, not chosen (add it with: python3 start.py setup --only {source.id})"))
        elif not source.standard and not source.opt_in and source.id not in only:
            how = f"python3 start.py setup --only {source.id} --area W,S,E,N"
            left_out.append((source, f"not in the standard set (add it in the map viewer, or with: {how})"))
        else:
            ids.append(source.id)
    return ids, left_out


def cmd_setup(records, args):
    """`python3 start.py setup`: add the sources asked for (default: the standard set),
    showing what each ingester prints, then a summary. Returns the exit status.

    If Kaggle asks for a key for the air strikes, and there is a terminal to ask on, the person
    is offered to save one, and the air strikes are tried once more."""
    only, skip = args.only or [], args.skip or []
    choices = records.choices()
    # New here: never said yes on the terminal, and nothing added from the viewer either.
    first_run = not choices.get("agreed") and records.first_run_marks() == 0
    if first_run:
        explain_first_run(records.sources)
        if not args.yes and not ask_yes_no("Go ahead?", default=True):
            say("Stopped; nothing was downloaded. Run this again when you are ready.")
            return 0
        answers = {"agreed": True}
        if not args.yes and sys.stdin.isatty():
            for source in (s for s in records.sources if s.opt_in):
                say(f"Optional: {source.title}.")
                say(f"  {source.warning}")
                answers[source.id] = ask_yes_no("Download it too?", default=False)
        records.set_choices(**answers)
        choices = records.choices()
    say()

    ids, left_out = setup_plan(records, only, skip, choices)
    before = {s.id for s in records.sources if records.is_added(s)}
    try:
        options = {"thor": {"area": [float(v) for v in args.area.split(",")]}} if args.area else None
        queued = records.add(ids, options, accept=ids, refresh=args.refresh, listed_only=False) if ids else []
    except (RecordsError, ValueError) as e:
        say(str(e))
        return 2
    started, results, shown = {}, {}, {}

    def report(job):
        sid = job.source.id
        if job.state == "running" and sid not in started:
            started[sid] = time.monotonic()
            say(f"== {job.source.title} ({sid}) ==")
        if job.outcome:
            results[sid] = (job.outcome, job.error, time.monotonic() - started.get(sid, time.monotonic()))
        elif job.status_text and time.monotonic() - shown.get(sid, 0) > 5 and job.state == "running":
            shown[sid] = time.monotonic()  # a progress note every few seconds, not every line
            say(f"  {job.status_text}")

    records.echo = lambda line: (sys.stdout.write("  " + line), sys.stdout.flush())
    with records.exclusive(on_wait=lambda: say("The map viewer is adding records right now; waiting for it ...")):
        pass  # only to say so up front if another process is busy; each job takes the lock again
    records.run_pending(report)
    wanting = records.wanting_key()
    if wanting and not args.yes and sys.stdin.isatty() and offer_kaggle_key(records):
        for sid in wanting:
            started.pop(sid, None)
        records.add(wanting, options, accept=wanting, listed_only=False)
        records.run_pending(report)

    say()
    say("Summary")
    say("-------")
    failed = []
    for source in records.sources:
        sid = source.id
        outcome, error, seconds = results.get(sid, (None, None, 0))
        if outcome == "added":
            say(f"  done      {source.title}  ({f'{seconds / 60:.0f} min' if seconds >= 60 else 'under a minute'})")
        elif outcome == "failed":
            failed.append(source)
            say(f"  FAILED    {source.title}")
            say(f"            {error}")
            say(f"            full log: data/work/setup/logs/{sid}.log")
        elif outcome == "cancelled":
            say(f"  stopped   {source.title}: stopped in the map viewer")
        elif sid in ids and sid in before and sid not in queued:
            say(f"  ready     {source.title}  (done before)")
    for source, why in left_out:
        say(f"  skipped   {source.title}: {why}")
    if failed:
        say()
        say("To retry what failed, run python3 start.py setup again (finished parts are not redone),")
        say(f"or retry one part with, for example:  python3 start.py setup --only {failed[0].id}")
    if records.wanting_key():
        say("To give Map Vietnam the Kaggle key it asked for: enter it in the map viewer, or run")
        say("python3 start.py setup --only thor in a terminal, without --yes, and it will ask.")
    return 1 if failed else 0


def cmd_list(records, args):
    """Print every source with its state, one per line."""
    for entry in records.listing()["sources"]:
        count = f"{entry['records']:,} records" if entry["records"] is not None else ""
        say(f"{entry['id']:<14} {entry['state']:<10} {entry['title']:<36} {count}")
    return 0


def cmd_remove(records, args):
    """Remove the sources named (with --cascade, also the ones made with them)."""
    try:
        removing = records.remove(args.ids, cascade=args.cascade, listed_only=False)
    except RecordsError as e:
        say(f"{e}" + (" (add --cascade to remove those too)" if e.status == 409 else ""))
        return 2
    with records.exclusive(on_wait=lambda: say("The map viewer is adding records right now; waiting for it ...")):
        pass
    records.run_pending()
    say(f"Removed {len(removing)} record source{'s' if len(removing) != 1 else ''}.")
    return 0


def source_ids(text):
    """argparse type for --only/--skip: comma-separated source ids, checked."""
    names = [n.strip() for n in text.split(",") if n.strip()]
    known = [s.id for s in SOURCES]
    unknown = [n for n in names if n not in known]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown part {', '.join(unknown)}; choose from: {', '.join(known)}")
    return names


def add_setup_options(parser):
    """The options of `setup`, shared with start.py's own parser so the two never drift."""
    parser.add_argument("--yes", action="store_true", help="do not ask questions (optional parts stay off)")
    parser.add_argument("--only", type=source_ids, action="extend", metavar="PART", help="set up only these parts")
    parser.add_argument("--skip", type=source_ids, action="extend", metavar="PART", help="leave these parts out")
    parser.add_argument("--refresh", action="store_true", help="download the parts named with --only again")
    parser.add_argument("--area", metavar="W,S,E,N", help="the air-strike area, in degrees (with --only thor)")


def parse_args(argv=None):
    """Command-line options (see the module docstring)."""
    ap = argparse.ArgumentParser(prog="python3 start.py records", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list the record sources and their state", description="list the record sources")
    add_setup_options(sub.add_parser("setup", help="download and prepare records", description="add record sources"))
    p = sub.add_parser("remove", help="delete a source's records and downloads", description="remove record sources")
    p.add_argument("ids", nargs="+", metavar="ID", type=lambda t: source_ids(t)[0], help="source ids (see list)")
    p.add_argument("--cascade", action="store_true", help="also remove the sources made with these")
    return ap.parse_args(argv)


def main(argv=None):
    """Run one records command. Returns the exit status: 0 all well, 1 something failed,
    2 a request that cannot be honoured, 130 stopped."""
    args = parse_args(argv)
    records = Records()
    command = {"list": cmd_list, "setup": cmd_setup, "remove": cmd_remove}[args.command]
    try:
        return command(records, args)
    except KeyboardInterrupt:
        records.stop(timeout=5)
        say()
        say("Stopped. Nothing is lost: run the same command again to carry on where it left off.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
