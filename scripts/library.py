"""The map library: the old maps a user can add, the ones they have added, and the queue that adds them.

No map is ever downloaded unless the user asks for it by name: from the viewer's Old maps card
(scripts/server.py, /api/library) or from the command line (python3 start.py maps ...). Every map
added can be removed again, which deletes its tiles and its download.

Inputs and outputs:
  data/sources/map_library.json   the catalogue (tracked; scripts/map_library_catalogue.py makes it):
                                  {"series": {id: {name, scale, ...}}, "maps": [{id, series, title,
                                  bounds, region, urls, bytes, sha256, kind}, ...]}. Only maps listed
                                  there can be added, and only from the URLs listed there.
  data/raw/...                    each map's download (download_maps.raw_paths), kept for rebuilds
  web/tiles/<id>.mbtiles          an installed map (tiling.py); its presence is what "installed" means
  web/catalog.json                the viewer's layer list (catalog.py), rewritten after every change
  data/work/maps/logs/<id>.log    what the last build of a map printed (deleted with the map)
  data/work/maps/worker.lock      held while a map is being added (see below)
  data/work/maps/current.json     {"id", "pid", "state"} of the map the lock holder is adding
  data/work/maps/cancel/<id>      "stop adding this map": a removal asked for by another process

A map's state is one of: available, queued, downloading, building, installed, failed. Installed
comes from disk, so it survives restarts; the others live in memory for as long as the process
(normally the viewer's server) runs, so they survive viewer reloads but not a server restart.
A map that another process is adding is shown with that process's state, from current.json.

Adding runs one map at a time: building needs up to about 2 GB of memory. The build itself runs
in a child process (build_tiles.py or georef_scan.py --progress), so the server stays responsive,
its memory is returned when it ends, and cancelling is simply stopping it. Decoding a JPEG 2000
scan into its working image also runs in a child process, for the same reason.

The viewer's server and `start.py maps add` may run at the same time; an exclusive lock on
worker.lock makes them take turns. Whoever holds the lock deletes leftovers of interrupted work
(*.partial, *.part) before starting a map, which is safe exactly because nobody else can be
working then; the server also does so when it starts, and reports those maps as interrupted.
Removing a map that the other process is adding leaves a cancel request, which that process
honours within a second or so: it stops, then deletes the map's files itself.

Nothing is kept from a failed attempt: a download that fails or a build that fails deletes the
map's files, so trying again always starts from a fresh download.

Maps made by versions before the library (web/tiles/<id>/ folders of PNG tiles) still show, but
take several times the space. The listing reports them as "legacy" (how many, how big), and they
are converted to the current format, without downloading anything, when the person asks: the
viewer's button (POST /api/library/migrate, in the background) or `start.py maps migrate`.

Usage: .venv/bin/python scripts/library.py list [--area W,S,E,N | --region north|centre|south] [--installed]
       .venv/bin/python scripts/library.py add ID ... | --area W,S,E,N | --region R  [--yes]
       .venv/bin/python scripts/library.py remove ID ... | --all  [--yes]
       .venv/bin/python scripts/library.py migrate    (convert map folders made by older versions)
"""

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import catalog
import custom_maps
import download_maps
import l7014_meta
import morecantile
import numpy as np
import paths
import tiling
from datums import INDIAN_1960
from files import StillInUse, delete, lock_file, path_bytes, read_json, unlock_file, write_json
from PIL import Image
from pyproj import Transformer

REGIONS = ("north", "centre", "south")
STATES = ("available", "queued", "downloading", "building", "installed", "failed", "needs_points")
# Render resolution for each GeoPDF series (see build_tiles.py); others use its default.
SERIES_DPI = {"jog1501": 300}
# Disk space a map takes once installed, per byte downloaded, when its series in the catalogue
# does not say (the catalogue's "disk_per_download_byte"). Only used for "about N MB" estimates.
DEFAULT_DISK_PER_DOWNLOAD_BYTE = 2.4
# Folders under data/work that older versions filled with renders, GeoTIFFs and bounds files.
LEGACY_WORK = ("l7014", "scans")
# The datum of a series' catalogue corners when it is not each sheet's own: the 1:50,000 sheet
# grid is drawn on Indian 1960, and so are the frames of the few sheets reissued on WGS84.
FRAME_DATUM = {"l7014": "ING-A"}
# A build that runs longer than this is stuck (the slowest sheet measured takes about 3 minutes).
BUILD_TIMEOUT_S = 30 * 60
# How often a process adding a map looks for a cancel request from another process.
CANCEL_POLL_S = 0.5
# The catalogue gives a GeoPDF sheet's corners on its own datum, Indian 1960 for all but three
# 1:250,000 sheets. The viewer draws outlines on WGS84, which is a few hundred metres away, so
# listing() shifts them (for an installed map it gives the map's own extent instead).
TO_WGS84 = Transformer.from_crs(f"+proj=longlat {INDIAN_1960} +no_defs", "EPSG:4326", always_xy=True)


class LibraryError(ValueError):
    """A request the library cannot honour (unknown map ids, bad area); str() is plain English."""


class BuildError(Exception):
    """Building a map's tiles failed; str() is the builder's last message."""


@dataclass
class Job:
    """One map on its way in. `progress` is 0..1 within the current state (bytes while
    downloading, work done while building). `remove` is set when the user removed the map
    while it was being worked on: the worker then deletes its files once it has stopped."""

    entry: dict
    state: str = "queued"
    progress: float | None = None
    error: str | None = None
    notes: list = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None
    remove: bool = False
    rebuild: bool = False  # build although tiles exist (a custom map placed again)


def human_bytes(n):
    """'7.6 MB', '1.2 GB': sizes as the user reads them (decimal units, like file browsers)."""
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.1f} MB" if n >= 1e7 else f"{n / 1e6:.2g} MB"


def estimated_disk_bytes(entries, series=None):
    """About how much disk space `entries` will take once installed. series: the catalogue's
    series descriptions, whose disk_per_download_byte gives the ratio for each."""
    series = series or {}
    return sum(
        (m.get("bytes") or 0)
        * (series.get(m["series"], {}).get("disk_per_download_byte") or DEFAULT_DISK_PER_DOWNLOAD_BYTE)
        for m in entries
    )


def parse_area(text):
    """[west, south, east, north] from "W,S,E,N" in degrees; LibraryError if it is not one."""
    try:
        w, s, e, n = (float(v) for v in text.split(","))
    except ValueError:
        raise LibraryError(f"an area is four numbers west,south,east,north in degrees, not {text!r}") from None
    if not (w < e and s < n):
        raise LibraryError("an area needs west < east and south < north")
    return [w, s, e, n]


def overlaps(a, b):
    """True when two [west, south, east, north] boxes share some area."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def prune_empty(folder, stop):
    """Delete `folder` and then its parents while they are empty, stopping before `stop`."""
    folder, stop = Path(folder), Path(stop)
    while folder != stop and stop in folder.parents:
        try:
            folder.rmdir()
        except OSError:  # not empty, or already gone
            if folder.exists():
                return
        folder = folder.parent


def progress_of(line):
    """The fraction in a builder's "progress 0.42" line, or None for any other line."""
    parts = line.split()
    if len(parts) == 2 and parts[0] == "progress":
        with contextlib.suppress(ValueError):
            return min(max(float(parts[1]), 0.0), 1.0)
    return None


class Library:
    """The catalogue, the installed maps under `root`, and the queue of maps being added.

    Thread-safe: the server calls listing/add/remove from request threads while one worker
    thread (start_worker) or the caller itself (run_pending) works through the queue.
    """

    def __init__(self, root=None, catalogue_path=None, python=None):
        """`root` is a folder holding both the program's files and the data (what the tests
        use); by default they are apart: shipped files under paths.APP_ROOT, what is written
        under paths.DATA_ROOT. `python` names an interpreter to run the child scripts with
        (the tests pass a fake one); by default paths.script_command() decides."""
        self.root = Path(root) if root else paths.DATA_ROOT  # where everything written goes
        self.app_root = Path(root) if root else paths.APP_ROOT  # shipped, read only
        self.catalogue_path = (
            Path(catalogue_path) if catalogue_path else self.app_root / "data" / "sources" / "map_library.json"
        )
        self.tiles = self.root / "web" / "tiles"
        self.raw = self.root / "data" / "raw"
        self.work = self.root / "data" / "work" / "maps"
        self.scans = self.app_root / "data" / "scans"
        self.custom = custom_maps.CustomMaps(self.root)  # the user's own maps
        self.alignments = self.root / "data" / "alignments"
        self.python = python or sys.executable
        self._python_given = python is not None
        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._jobs = {}  # id -> Job, for maps queued, being added, or failed
        self._queue = deque()  # ids waiting, in the order they were asked for
        self._current = None  # id of the job the worker has taken, if any
        self._worker = None
        self._catalogue = (None, {"series": {}, "maps": []})  # (file mtime, parsed catalogue)
        self._outlines = (None, {})  # (catalogue mtime, {id: WGS84 outline}) for maps not installed
        self._extents = {}  # id -> (MBTiles mtime, bounds) for installed maps
        self._catalog_lock = threading.Lock()  # one catalog.json rewrite at a time in this process
        # The conversion of old map folders started from the viewer: state is idle, converting or failed.
        self._migration = {"state": "idle", "error": None, "converted": 0}
        self._legacy_sizes = {}  # folder name -> (folder mtime, bytes)

    # ---- the catalogue ---------------------------------------------------------------------
    def catalogue(self):
        """The parsed catalogue, re-read when the file changes; empty if there is none."""
        try:
            mtime = self.catalogue_path.stat().st_mtime_ns
        except FileNotFoundError:
            return {"series": {}, "maps": []}
        if self._catalogue[0] != mtime:
            try:
                parsed = json.loads(self.catalogue_path.read_text(encoding="utf-8"))
            except ValueError:  # includes JSONDecodeError and UnicodeDecodeError
                raise LibraryError(catalog.damaged_file_message(self.catalogue_path)) from None
            self._catalogue = (mtime, parsed)
        return self._catalogue[1]

    def catalogue_entries(self):
        """{id: entry} for the maps in the catalogue only."""
        return {m["id"]: m for m in self.catalogue().get("maps", [])}

    def entries(self):
        """{id: entry} for every map that can be listed: the catalogue's, then the user's own
        (custom_maps.py), whose entries carry "custom": true."""
        return {**self.catalogue_entries(), **{m["id"]: m for m in self.custom.entries()}}

    def entries_for(self, ids):
        """Catalogue entries for `ids`, in order; LibraryError naming any that are not listed."""
        entries = self.entries()
        unknown = [i for i in ids if i not in entries]
        if unknown:
            raise LibraryError(f"not in the map list: {', '.join(map(str, unknown))}")
        return [entries[i] for i in dict.fromkeys(ids)]

    def entry(self, map_id):
        """One catalogue or custom map entry; LibraryError if not listed."""
        return self.entries_for([map_id])[0]

    def select(self, area=None, region=None):
        """Catalogue entries overlapping `area` ([w, s, e, n]) and/or in `region`."""
        if region is not None and region not in REGIONS:
            raise LibraryError(f"region must be one of {', '.join(REGIONS)}")
        return [
            m
            for m in self.catalogue_entries().values()
            if (area is None or overlaps(m["bounds"], area)) and (region is None or m.get("region") == region)
        ]

    # ---- what is on disk -------------------------------------------------------------------
    def tiles_path(self, map_id):
        """The installed file of a map (it may not exist)."""
        return self.tiles / f"{map_id}{catalog.MBTILES}"

    def alignment_dir(self, map_id):
        """Folder holding a user's alignment override (preview, work image, points, control) for `map_id`."""
        return self.alignments / map_id

    def alignment_control_path(self, map_id):
        """The control file written when the user saves an adjusted alignment for `map_id`."""
        return self.alignment_dir(map_id) / "control.json"

    def alignment_state_path(self, map_id):
        """The saved point pairs, image dimensions and bounds for re-opening alignment on `map_id`."""
        return self.alignment_dir(map_id) / "alignment.json"

    def alignment_preview_path(self, map_id):
        """The shrunk JPEG preview served to the Placement screen when adjusting `map_id`."""
        return self.alignment_dir(map_id) / "preview.jpg"

    def alignment_work_path(self, map_id):
        """The full-resolution working image that user alignment pixel coordinates refer to."""
        return self.alignment_dir(map_id) / "work.jpg"

    def has_alignment(self, map_id):
        """True when a saved user alignment override exists for `map_id`."""
        return self.alignment_control_path(map_id).exists()

    def installed_ids(self):
        """Ids of every installed map, listed in the catalogue or not."""
        return catalog.installed_ids(self.tiles)

    def raw_paths(self, entry):
        """(the file the builder reads, [paths to delete when the map is removed]) for an entry:
        a download of the catalogue's, or the folder of the user's own map."""
        if entry.get("custom"):
            return self.custom.build_input(entry), [self.custom.folder(entry["id"])]
        return download_maps.raw_paths(entry, self.raw)

    def disk_bytes(self, map_id, entry=None):
        """Bytes a map takes on disk (tiles, download and alignment override), or None when zero."""
        n = path_bytes(self.tiles_path(map_id)) + path_bytes(self.alignment_dir(map_id))
        if entry:
            n += sum(path_bytes(p) for p in self.raw_paths(entry)[1])
        return n or None

    def disk_usage(self, records_db=None):
        """{"maps", "records", "downloads", "total"} in bytes: installed maps (tiles, the user's
        own uploads and alignment overrides), the record database and what the record sources
        downloaded, and the old maps' downloads kept for rebuilding."""
        folders = {m["series"] for m in self.catalogue().get("maps", [])} | {"scans"}
        downloads = sum(path_bytes(self.raw / name) for name in folders)
        maps = path_bytes(self.tiles) + self.custom.disk_bytes() + path_bytes(self.alignments)
        records = path_bytes(records_db) + max(path_bytes(self.raw) - downloads, 0) if records_db else 0
        return {"maps": maps, "records": records, "downloads": downloads, "total": maps + records + downloads}

    def log_path(self, map_id):
        """Where the last build of a map wrote what it printed."""
        return self.work / "logs" / f"{map_id}.log"

    def delete_files(self, map_id, entry=None, keep_log=False):
        """Delete a map's tiles (finished or partial), its build log unless keep_log, and, if
        its entry is known, its download; then any download folder left empty."""
        tiles = self.tiles_path(map_id)
        stuck = []  # what a program still holds open: everything else is deleted regardless

        def attempt(path):
            try:
                delete(path)
            except OSError as e:
                stuck.append(e)

        attempt(tiles)
        attempt(tiles.with_name(tiles.name + tiling.PARTIAL_SUFFIX))
        attempt(self.alignment_dir(map_id))
        if not keep_log:
            attempt(self.log_path(map_id))
        if entry:
            for p in self.raw_paths(entry)[1]:
                attempt(p)
                attempt(p.with_name(p.name + ".part"))
                if not entry.get("custom"):
                    prune_empty(p.parent, self.raw)
        if stuck:
            raise StillInUse() from stuck[0]

    def delete_files_or_fail(self, map_id, entry):
        """delete_files for a removal. When something is still in use, leave the map in a state
        the viewer shows and the user can retry (Remove again): a failed job carrying the plain
        message if the map is in the catalogue (a map whose tiles are still there stays listed
        as installed). Returns that message, or None when everything was deleted."""
        try:
            self.delete_files(map_id, entry)
        except StillInUse as e:
            if entry:
                job = Job(entry)
                job.state, job.error = "failed", str(e)
                with self._lock:
                    self._jobs.setdefault(map_id, job)  # unless the map was added again meanwhile
            return str(e)
        return None

    def write_catalog(self):
        """Rewrite web/catalog.json from what is installed now.

        Listing the installed maps and replacing the file happen under one lock, shared with
        any other process (the command line), so the last file written is always built from the
        latest state: a writer that listed the maps before a removal cannot replace the file
        after the removal's own rewrite."""
        self.work.mkdir(parents=True, exist_ok=True)
        with self._catalog_lock, open(self.work / "catalog.lock", "a+", encoding="utf-8") as f:
            lock_file(f)
            try:
                catalog.write_catalog(
                    self.root / "web" / "catalog.json",
                    tiles_dir=self.tiles,
                    catalogue=self.catalogue(),
                    scans_dir=self.scans,
                    custom_dir=self.custom.root,
                    corrections=catalog.read_json(self.app_root / "data" / "maps.json", {}),
                )
            finally:
                unlock_file(f)

    # ---- state -----------------------------------------------------------------------------
    def scan_facts(self, map_id):
        """The date, scale and series name a scan's control file gives ({} if none). The
        catalogue has no per-map date, and the viewer shows a scan's year and maker before it is
        added (series_name: "US Army Map Service L509 ...", "Service Géographique de l'Indochine ...")."""
        meta = catalog.read_json(self.scans / f"{map_id}.json", {}).get("meta", {})
        facts = {k: meta[k] for k in ("date", "scale") if meta.get(k)}
        if meta.get("series"):
            facts["series_name"] = meta["series"]
        return facts

    def outline(self, map_id, entry):
        """[w, s, e, n] in WGS84 degrees of where a map lies, for drawing its outline: an
        installed map's own extent, else its catalogue corners moved onto WGS84 (see TO_WGS84)."""
        path = self.tiles_path(map_id)
        with contextlib.suppress(OSError):
            mtime = path.stat().st_mtime_ns
            if self._extents.get(map_id, (None,))[0] != mtime:
                self._extents[map_id] = (mtime, catalog.parse_bounds(tiling.read_metadata(path).get("bounds")))
            if self._extents[map_id][1]:
                return self._extents[map_id][1]
        if entry.get("custom") or entry.get("kind") != "geopdf" or not entry.get("bounds"):
            return entry.get("bounds")  # scan and own-map bounds are on WGS84 already
        mtime = self._catalogue[0]
        if self._outlines[0] != mtime:
            self._outlines = (mtime, {})
        if map_id not in self._outlines[1]:
            w, s, e, n = entry["bounds"]
            lons, lats = TO_WGS84.transform([w, e, e, w], [s, s, n, n])
            self._outlines[1][map_id] = [round(v, 6) for v in (min(lons), min(lats), max(lons), max(lats))]
        return self._outlines[1][map_id]

    def listing(self):
        """What GET /api/library answers: every map with its state, plus the bytes still to
        download for the maps in the queue, and the catalogue's series (names and scales) so
        the viewer can describe each map in words. Each map also carries its outline on WGS84
        (outline()) and the disk space it would take once added (disk_estimate). "legacy" tells
        of map folders made by older versions that can be converted (legacy_status())."""
        elsewhere = self.other_worker()
        with self._lock:
            entries = self.entries()
            series = self.catalogue().get("series", {})
            installed = set(self.installed_ids())
            maps, queue_bytes = [], 0
            for map_id, entry in entries.items():
                job = self._jobs.get(map_id)
                if job:
                    state, progress, error = job.state, job.progress, job.error
                    if entry.get("custom"):
                        pass  # uploaded already: nothing to download
                    elif state == "queued":
                        queue_bytes += entry.get("bytes") or 0
                    elif state == "downloading":
                        queue_bytes += round((entry.get("bytes") or 0) * (1 - (progress or 0)))
                elif elsewhere and elsewhere.get("id") == map_id and elsewhere.get("state") in STATES:
                    state, progress, error = elsewhere["state"], None, None  # added from the command line
                else:
                    state, progress, error = ("installed" if map_id in installed else "available"), None, None
                    if entry.get("custom") and map_id not in installed:
                        state, error = self.idle_custom_state(entry)
                maps.append(
                    {
                        **entry,
                        # Catalogues built since the finder fields were added carry a scan's facts already.
                        **(self.scan_facts(map_id) if entry.get("kind") == "scan" and "year" not in entry else {}),
                        "outline": self.outline(map_id, entry),
                        "state": state,
                        "progress": progress,
                        "error": error,
                        "disk_bytes": self.disk_bytes(map_id, entry),
                        "disk_estimate": round(estimated_disk_bytes([entry], series)),
                        "adjusted": self.has_alignment(map_id),
                    }
                )
            # Maps installed by hand or by an older version, no longer in the catalogue: listed so
            # they can still be removed, never re-added.
            for map_id in sorted(installed - set(entries)):
                meta = tiling.read_metadata(self.tiles_path(map_id))
                maps.append(
                    {
                        "id": map_id,
                        "title": meta.get("name", map_id),
                        "series": "",
                        "bounds": catalog.parse_bounds(meta.get("bounds")),
                        "outline": catalog.parse_bounds(meta.get("bounds")),
                        "region": "",
                        "urls": [],
                        "bytes": None,
                        "sha256": None,
                        "kind": "",
                        "state": "installed",
                        "progress": None,
                        "error": None,
                        "disk_bytes": self.disk_bytes(map_id),
                        "disk_estimate": None,
                        "adjusted": self.has_alignment(map_id),
                    }
                )
            return {
                "maps": maps,
                "queue_bytes": queue_bytes,
                "series": {**self.catalogue().get("series", {}), custom_maps.SERIES: custom_maps.SERIES_INFO},
                "legacy": self.legacy_status(),
            }

    def idle_custom_state(self, entry):
        """(state, error) of one of the user's own maps that is not installed and has no job: a
        scan waits for its points; a map whose build never finished (the program was stopped
        meanwhile) is shown as failed, so it can be tried again or removed."""
        if not self.custom.buildable(entry):
            return "needs_points", None
        return "failed", "Making this map did not finish. Try again, or remove it and add it once more."

    def state_of(self, map_id):
        """The state name of one map."""
        with self._lock:
            job = self._jobs.get(map_id)
            if job:
                return job.state
            if self.tiles_path(map_id).exists():
                return "installed"
            entry = self.custom.entry_path(map_id).exists() and self.custom.get(map_id)
            return self.idle_custom_state(entry)[0] if entry else "available"

    # ---- changing it -----------------------------------------------------------------------
    def add(self, ids):
        """Queue the maps `ids` (catalogue ids only). Maps already installed, queued or being
        added are left as they are; failed ones are tried again. Returns the ids queued."""
        entries = self.entries_for(ids)
        queued = []
        with self._lock:
            for entry in entries:
                map_id = entry["id"]
                if entry.get("custom") and not self.custom.buildable(entry):
                    continue  # a scan that still needs its points
                # Asking for a map again overrules an earlier removal of it.
                self.cancel_path(map_id).unlink(missing_ok=True)
                job = self._jobs.get(map_id)
                if (job and job.state != "failed") or self.tiles_path(map_id).exists():
                    continue
                self._jobs[map_id] = Job(entry)
                self._queue.append(map_id)
                queued.append(map_id)
            self._wake.notify_all()
        return queued

    def place_custom(self, map_id, points, title, year):
        """Save the control points of one of the user's scans (custom_maps.save_points) and queue
        its build, even if it is built already (placing it again rebuilds it). LibraryError if it
        is being made right now; nothing is saved then."""
        with self._lock:
            job = self._jobs.get(map_id)
            if job and job.state != "failed":
                raise LibraryError("This map is being made right now. Wait until it is ready, then place it again.")
            entry = self.custom.save_points(map_id, points, title, year)
            self.cancel_path(map_id).unlink(missing_ok=True)
            self._jobs[map_id] = Job(entry, rebuild=True)
            self._queue.append(map_id)
            self._wake.notify_all()

    def _alignment_image_size(self, work_path):
        with Image.open(work_path) as img:
            return {"width": img.width, "height": img.height}

    def prepare_alignment(self, map_id):
        """Prepare work/preview images for adjusting `map_id` and return its Placement payload."""
        entry = self.entry(map_id)
        if entry.get("custom") and entry.get("kind") == "scan":
            raise LibraryError("Use Place on map to adjust this custom image.")
        if not self.tiles_path(map_id).exists():
            raise LibraryError("Install this map before adjusting its alignment.")
        src, _ = self.raw_paths(entry)
        if not src.exists():
            raise LibraryError("The original map file is missing. Re-add the map to download it again.")
        folder = self.alignment_dir(map_id)
        work = self.alignment_work_path(map_id)
        preview = self.alignment_preview_path(map_id)
        if not (work.exists() and preview.exists()):
            created = not folder.exists()
            folder.mkdir(parents=True, exist_ok=True)
            try:
                image_dims = self.custom.make_work_image(src, folder)
            except Exception:
                if created:
                    with contextlib.suppress(OSError):
                        delete(folder)
                raise
        else:
            image_dims = None
        state = read_json(self.alignment_state_path(map_id))
        if isinstance(state, dict) and isinstance(state.get("image"), dict):
            image_dims = state["image"]
            points = state.get("points") or []
            bounds = state.get("bounds")
        else:
            if image_dims is None:
                image_dims = self._alignment_image_size(work)
            points, bounds = [], None
        return {
            "id": map_id,
            "title": entry.get("title") or map_id,
            "year": str(entry.get("year") or entry.get("date") or ""),
            "image": image_dims,
            "points": points,
            "bounds": bounds or entry.get("bounds"),
            "original_bounds": entry.get("bounds"),
            "adjusted": self.has_alignment(map_id),
            "preview_url": f"/api/library/{map_id}/align/preview",
        }

    def save_alignment(self, map_id, points):
        """Save user alignment control points for `map_id` and queue a rebuild from `work.jpg`."""
        entry = self.entry(map_id)
        if entry.get("custom") and entry.get("kind") == "scan":
            raise LibraryError("Use Place on map to adjust this custom image.")
        with self._lock:
            job = self._jobs.get(map_id)
            if job and job.state != "failed":
                raise LibraryError("This map is being made right now. Wait until it is ready, then adjust it again.")
            work = self.alignment_work_path(map_id)
            if not work.exists():
                raise LibraryError("Prepare this map's alignment image before saving points.")
            image_dims = self._alignment_image_size(work)
            pts = custom_maps.clean_points(points, image_dims)
            bounds = custom_maps.placed_bounds(pts, image_dims["width"], image_dims["height"])
            max_zoom = (
                min(custom_maps.native_zoom_for(bounds, image_dims["width"]), custom_maps.MAX_ZOOM)
                if bounds
                else custom_maps.MAX_ZOOM
            )
            scan_meta = {}
            if entry.get("kind") == "scan":
                scan_file = self.scans / f"{map_id}.json"
                if scan_file.exists():
                    scan_meta = (read_json(scan_file) or {}).get("meta", {})
            meta = {
                "title": entry.get("title") or map_id,
                "date": str(entry.get("year") or entry.get("date") or ""),
                "dateNote": entry.get("date_note") or entry.get("dateNote") or "",
                **scan_meta,
            }
            control = {
                "image": str(work),
                "crs": "EPSG:4326",
                "gcps": pts,
                "max_zoom": max_zoom,
                "meta": meta,
            }
            if len(pts) < custom_maps.POLYNOMIAL_POINTS:
                control["order"] = 1
            write_json(self.alignment_control_path(map_id), control)
            write_json(self.alignment_state_path(map_id), {"image": image_dims, "points": pts, "bounds": bounds})
            self.cancel_path(map_id).unlink(missing_ok=True)
            self._jobs[map_id] = Job(entry, rebuild=True)
            self._queue.append(map_id)
            self._wake.notify_all()
        return {"ok": True, "id": map_id, "adjusted": True, "bounds": bounds}

    def reset_alignment(self, map_id):
        """Delete user alignment overrides for `map_id` and queue a rebuild from original sources."""
        entry = self.entry(map_id)
        with self._lock:
            job = self._jobs.get(map_id)
            if job and job.state != "failed":
                raise LibraryError("This map is being made right now. Wait until it is ready, then adjust it again.")
            try:
                delete(self.alignment_dir(map_id))
            except StillInUse as e:
                raise LibraryError(str(e)) from e
            self.cancel_path(map_id).unlink(missing_ok=True)
            self._jobs[map_id] = Job(entry, rebuild=True)
            self._queue.append(map_id)
            self._wake.notify_all()
        return {"ok": True, "id": map_id, "adjusted": False}

    def remove(self, ids):
        """Remove maps: stop them if queued or being added, delete their tiles and downloads,
        and rewrite the viewer's catalogue. Returns the ids that had anything to remove.

        A map this process is adding is stopped here, and the worker deletes its files once
        stopped. When another process (the command line) is adding maps, a cancel request is
        left for each map removed: that process stops the one it is on, deletes its files
        itself, and skips the others if they are in its queue."""
        entries = self.entries()
        installed = set(self.installed_ids())
        unknown = [i for i in ids if i not in entries and i not in installed]
        if unknown:
            raise LibraryError(f"not in the map list: {', '.join(map(str, unknown))}")
        elsewhere = self.other_worker()
        removed, problem = [], None
        with self._lock:
            for map_id in dict.fromkeys(ids):
                entry = entries.get(map_id)
                job = self._jobs.pop(map_id, None)
                with contextlib.suppress(ValueError):
                    self._queue.remove(map_id)
                busy_elsewhere = elsewhere is not None and elsewhere.get("id") == map_id
                if elsewhere is not None:
                    self.request_cancel(map_id)
                if job and map_id == self._current:
                    # The worker owns its files until it has stopped; it deletes them then.
                    job.remove = True
                    job.cancel.set()
                    proc = job.proc  # read once: the worker may clear it at any moment
                    if proc:
                        proc.terminate()
                elif not busy_elsewhere:
                    problem = self.delete_files_or_fail(map_id, entry) or problem
                if job or busy_elsewhere or map_id in installed or (entry and self.disk_bytes(map_id, entry)):
                    removed.append(map_id)
        self.write_catalog()
        if problem:  # the rest were removed; this one can be tried again
            raise LibraryError(problem)
        return removed

    # ---- coordination with another process adding maps ------------------------------------
    def cancel_path(self, map_id):
        """The file that asks whichever process is adding `map_id` to stop and not add it."""
        return self.work / "cancel" / map_id

    def request_cancel(self, map_id):
        """Leave a cancel request for `map_id` (see remove)."""
        path = self.cancel_path(map_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def cancel_requested(self, map_id):
        """True if another process asked for `map_id` to be removed while it was being added."""
        return self.cancel_path(map_id).exists()

    def _mark_current(self, map_id=None, state=None):
        """Record in current.json the map this process is adding (only the lock holder writes
        it), or delete the record when map_id is None."""
        marker = self.work / "current.json"
        if map_id is None:
            marker.unlink(missing_ok=True)
        else:
            write_json(marker, {"id": map_id, "pid": os.getpid(), "state": state})

    def other_worker(self):
        """{"id", "state", "pid"} of the map another process is adding right now, or None.
        A record left by a process that died is ignored: then nobody holds the lock."""
        try:
            current = json.loads((self.work / "current.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(current, dict) or current.get("pid") == os.getpid():
            return None
        with self.try_exclusive() as free:
            return None if free else current

    # ---- working through the queue ---------------------------------------------------------
    def start_worker(self):
        """Start the background worker thread (once); it works whenever something is queued."""
        with self._lock:
            if self._worker is None:
                self._worker = threading.Thread(target=self._work_forever, name="map-library", daemon=True)
                self._worker.start()

    def _work_forever(self):
        while True:
            with self._lock:
                while not self._queue:
                    self._wake.wait()
            try:
                self.run_pending()
            except Exception as e:  # e.g. a broken catalogue file: say so, and keep serving
                print(f"map library: {e}", file=sys.stderr, flush=True)

    def run_pending(self, report=None):
        """Add every queued map, one after another, in this thread. `report(job)` is called on
        every change of state or progress (the command line prints from it)."""
        while True:
            with self._lock:
                if not self._queue:
                    return
                map_id = self._queue.popleft()
                job = self._jobs[map_id]
                self._current = map_id
            try:
                self._run(job, report or (lambda job: None))
            finally:
                if job.remove:
                    # A map removed while it was built: its files go now that the worker let go.
                    # Still marked as current (so a new add waits) but outside the lock, because
                    # a locked file is retried for seconds and listings must not wait for that.
                    self.delete_files_or_fail(map_id, job.entry)
                with self._lock:
                    self._current = None
                    self._wake.notify_all()
                self.write_catalog()

    def wait_idle(self, timeout=None):
        """Block until nothing is queued or being added (for tests and the command line)."""
        with self._lock:
            return self._wake.wait_for(lambda: not self._queue and self._current is None, timeout)

    def stop(self, timeout=10):
        """Stop working, for quitting: forget the queue, end the download or build under way,
        and wait (up to `timeout` seconds) for the worker to let go. What the stopped map left
        behind is cleaned up, and the map shown as interrupted, when the server next starts
        (recover_interrupted)."""
        with self._lock:
            for map_id in self._queue:
                self._jobs.pop(map_id, None)
            self._queue.clear()
            job = self._jobs.get(self._current) if self._current else None
            if job:
                job.cancel.set()
                proc = job.proc  # read once: the worker may clear it at any moment
                if proc:
                    proc.terminate()
            return self._wake.wait_for(lambda: self._current is None, timeout)

    def _set(self, job, report, state=None, progress=None):
        with self._lock:
            if state:
                job.state = state
            job.progress = progress
            self._wake.notify_all()
        if state:
            self._mark_current(job.entry["id"], state)
        report(job)

    def _watch(self, job, finished):
        """Until `finished` is set, honour a cancel request from another process: stop the job
        as remove() would (the worker then deletes the map's files)."""
        while not finished.wait(CANCEL_POLL_S):
            if not job.cancel.is_set() and self.cancel_requested(job.entry["id"]):
                with self._lock:
                    job.remove = True
                    job.cancel.set()
                    proc = job.proc
                if proc:
                    proc.terminate()

    def _run(self, job, report):
        """Download and build one map, recording failure in the job instead of raising."""
        entry = job.entry
        map_id = entry["id"]
        try:
            with self.exclusive():
                if self.cancel_requested(map_id):  # removed by another process while queued here
                    job.remove = True
                    job.cancel.set()
                if job.cancel.is_set():
                    raise download_maps.Cancelled()
                if job.rebuild or not self.tiles_path(map_id).exists():  # another process may have added it meanwhile
                    self.clean_leftovers()
                    finished = threading.Event()
                    threading.Thread(target=self._watch, args=(job, finished), daemon=True).start()
                    try:
                        self._download_and_build(job, report)
                    except Exception:
                        if not job.cancel.is_set():
                            # Keep nothing but the log: trying again starts from a fresh download.
                            # The user's own upload cannot be fetched again, so it stays.
                            with contextlib.suppress(StillInUse):  # the build's own error is the news
                                if entry.get("custom") or job.rebuild:
                                    tiles = self.tiles_path(map_id)
                                    delete(tiles.with_name(tiles.name + tiling.PARTIAL_SUFFIX))
                                else:
                                    self.delete_files(map_id, entry, keep_log=True)
                        raise
                    finally:
                        finished.set()
                        self._mark_current(None)
                    if entry.get("custom"):
                        self.custom.record_built(
                            map_id, catalog.parse_bounds(tiling.read_metadata(self.tiles_path(map_id)).get("bounds"))
                        )
                    # The viewer reloads catalog.json as soon as it sees "installed": list the map first.
                    self.write_catalog()
            with self._lock:
                if self._jobs.get(map_id) is job:
                    del self._jobs[map_id]
                job.state, job.progress = "installed", None
            report(job)
        except download_maps.Cancelled:
            self._stopped(job, report)
        except Exception as e:  # any failure belongs to this map only: the worker must go on
            if job.cancel.is_set():
                self._stopped(job, report)  # stopped on purpose: the error is a consequence, not news
                return
            with self._lock:
                job.state, job.progress, job.error = "failed", None, str(e)
            report(job)
        finally:
            with self._lock:
                job.proc = None

    def _stopped(self, job, report):
        """A job ended because its map was removed. A removal by another process is reported
        (the command line says so) and its request is used up."""
        map_id = job.entry["id"]
        if self.cancel_requested(map_id):
            self.cancel_path(map_id).unlink(missing_ok=True)
            with self._lock:
                if self._jobs.get(map_id) is job:
                    del self._jobs[map_id]
                job.state, job.progress = "removed", None
            report(job)

    def _download_and_build(self, job, report):
        """The two steps of adding a map; call only while holding exclusive()."""
        entry = job.entry
        if self.has_alignment(entry["id"]):
            self._set(job, report, "building", 0.0)
            return self._build(job, self.alignment_work_path(entry["id"]), report)
        if entry.get("custom"):  # uploaded already: only the build is left
            self._set(job, report, "building", 0.0)
            return self._build(job, self.custom.build_input(entry), report)
        self._set(job, report, "downloading", 0.0)
        path, job.notes = download_maps.fetch_map(
            entry,
            self.raw,
            progress=lambda done, total: self._set(job, report, progress=min(done / total, 1) if total else None),
            cancelled=job.cancel.is_set,
            derive=self.derive_in_child,
        )
        self._set(job, report, "building", 0.0)
        self._build(job, path, report)

    def script_command(self, script, *args):
        """The command line of one of the project's scripts: through paths.script_command, or
        with the interpreter this library was given."""
        if self._python_given:
            return [self.python, str(paths.SCRIPTS / script), *args]
        return paths.script_command(script, *args)

    def derive_in_child(self, src, dst, derive):
        """download_maps.derive_work_image in a child process: decoding a large JPEG 2000 takes
        gigabytes of memory, which a long-running server would not give back."""
        cmd = self.script_command("download_maps.py", "--derive", str(src), str(dst), json.dumps(derive))
        done = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, errors="replace", encoding="utf-8")
        if done.returncode != 0:
            message = (done.stderr.strip().splitlines() or ["no message"])[-1]
            raise OSError(message)

    def build_command(self, entry, path, out):
        """The child process that builds `entry`'s tiles from its download `path` into `out`."""
        if self.has_alignment(entry["id"]):
            return self.script_command(
                "georef_scan.py",
                str(self.alignment_control_path(entry["id"])),
                str(out),
                "--image",
                str(self.alignment_work_path(entry["id"])),
            )
        if entry.get("custom"):
            return self.custom_build_command(entry, path, out)
        if entry["kind"] == "scan":
            control = self.scans / f"{entry['id']}.json"
            if not control.exists():
                raise BuildError(f"this scan has no control points (data/scans/{entry['id']}.json is missing)")
            return self.script_command("georef_scan.py", str(control), str(out), "--image", str(path))
        cmd = self.script_command("build_tiles.py", str(path), str(out))
        if entry.get("bounds"):
            # For a PDF that records no corners of its own, the catalogue's cut off the margin its
            # neatline lets through (see build_tiles.py).
            cmd += ["--frame", ",".join(str(v) for v in entry["bounds"])]
            if entry["series"] in FRAME_DATUM:
                cmd += ["--frame-datum", FRAME_DATUM[entry["series"]]]
        dpi = SERIES_DPI.get(entry["series"])
        return cmd + (["--dpi", str(dpi)] if dpi else [])

    def custom_build_command(self, entry, path, out):
        """The child process that builds one of the user's own maps: a GeoTIFF with
        build_geotiff.py, a GeoPDF with build_tiles.py, a placed scan with georef_scan.py."""
        if entry["kind"] == "geotiff":
            return self.script_command(
                "build_geotiff.py", str(path), str(out), "--title", entry["title"], "--date", entry.get("date", "")
            )
        if entry["kind"] == "geopdf":
            return self.script_command("build_tiles.py", str(path), str(out))
        control = self.custom.control_path(entry["id"])
        if not control.exists():
            raise BuildError("This picture has not been placed on the map yet")
        return self.script_command("georef_scan.py", str(control), str(out), "--image", str(path))

    def _build(self, job, path, report):
        """Run the builder for a job, following its progress lines; raise BuildError on failure.
        A builder still running after BUILD_TIMEOUT_S is stopped and the build failed."""
        out = self.tiles_path(job.entry["id"])
        cmd = self.build_command(job.entry, path, out) + ["--progress"]
        log_path = self.log_path(job.entry["id"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        last = ""
        timed_out = threading.Event()
        with open(log_path, "w", encoding="utf-8") as log:
            with self._lock:
                if job.cancel.is_set():
                    raise download_maps.Cancelled()
                proc = job.proc = subprocess.Popen(
                    cmd,
                    cwd=self.root,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    encoding="utf-8",
                )
            timer = threading.Timer(BUILD_TIMEOUT_S, lambda: (timed_out.set(), proc.kill()))
            timer.daemon = True
            timer.start()
            try:
                for line in proc.stdout:
                    log.write(line)
                    fraction = progress_of(line)
                    if fraction is not None:
                        self._set(job, report, progress=fraction)
                    elif line.strip():
                        last = line.strip()
                proc.wait()
            finally:
                # Whatever went wrong here (a full disk, say), never leave the builder running.
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stdout.close()
        if job.cancel.is_set():
            # Stopped by remove(): a killed builder cannot clean up after itself.
            delete(out.with_name(out.name + tiling.PARTIAL_SUFFIX))
            raise download_maps.Cancelled()
        details = log_path.relative_to(self.root) if self.root in log_path.parents else log_path
        if timed_out.is_set():
            raise BuildError(f"Making the map tiles took over {BUILD_TIMEOUT_S // 60} minutes and was stopped")
        if proc.returncode != 0 or not out.exists():
            raise BuildError(f"Making the map tiles failed: {last or 'no message'} (details: {details})")

    @contextlib.contextmanager
    def exclusive(self, on_wait=None):
        """Hold data/work/maps/worker.lock: only one process adds maps at a time. on_wait() is
        called once if another process holds it (the command line says so before waiting)."""
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / "worker.lock", "a+", encoding="utf-8") as f:
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
        self.work.mkdir(parents=True, exist_ok=True)
        with open(self.work / "worker.lock", "a+", encoding="utf-8") as f:
            free = lock_file(f, blocking=False)
            try:
                yield free
            finally:
                if free:
                    unlock_file(f)

    def clean_leftovers(self):
        """Delete what interrupted work left behind; returns the ids of the maps it was for.
        Call only while holding the lock."""
        ids = set()
        if self.tiles.is_dir():
            for p in self.tiles.glob(f"*{catalog.MBTILES}{tiling.PARTIAL_SUFFIX}"):
                ids.add(p.name.removesuffix(f"{catalog.MBTILES}{tiling.PARTIAL_SUFFIX}"))
                delete(p)
        folders = {m["series"] for m in self.catalogue().get("maps", [])} | {"scans"}
        for folder in folders:
            for p in (self.raw / folder).glob("**/*.part"):
                # data/raw/<series>/<id>_geo.pdf.part, or data/raw/scans/<id>/<file>.part
                ids.add(p.parent.name if folder == "scans" else p.name.split("_geo.")[0])
                delete(p)
                prune_empty(p.parent, self.raw)
        self._mark_current(None)  # whoever wrote it is not working any more
        return ids

    def recover_interrupted(self):
        """Called when the server starts: if no process is adding maps, delete what was cut
        short when one stopped, and show those maps as failed with a reason, so the user sees
        that adding them did not finish. Returns their ids."""
        with self.try_exclusive() as free:
            if not free:
                return []
            ids = self.clean_leftovers()
            self.custom.clean_incoming()
        entries = self.entries()
        interrupted = sorted(i for i in ids if i in entries and not self.tiles_path(i).exists())
        with self._lock:
            for map_id in interrupted:
                if map_id not in self._jobs:
                    error = "Adding this map was interrupted when Map Vietnam stopped. Try again."
                    self._jobs[map_id] = Job(entries[map_id], state="failed", error=error)
        return interrupted

    # ---- maps made by older versions -------------------------------------------------------
    def legacy_folders(self):
        """Tile folders web/tiles/<id>/ of PNG tiles, as versions before the map library made:
        folders holding at least one zoom-level folder (a number). Other folders are not ours."""

        def has_zoom_folder(folder):
            try:
                return any(z.is_dir() and z.name.isdigit() for z in folder.iterdir())
            except OSError:  # deleted while the viewer lists the maps, by a conversion finishing
                return False

        if not self.tiles.is_dir():
            return []
        return sorted(
            p
            for p in self.tiles.iterdir()
            if p.is_dir() and not p.name.endswith(tiling.PARTIAL_SUFFIX) and has_zoom_folder(p)
        )

    def legacy_status(self):
        """{"count", "bytes", "state", "error", "converted"}: how many map folders made by older
        versions wait to be converted and how much disk they take, and how the conversion
        started with start_migration is going: state "idle", "converting" or "failed" (then
        "error" says why, in plain words); "converted" counts the maps the last run converted."""
        total = 0
        folders = self.legacy_folders()
        for folder in folders:
            # Thousands of small files each: measured once, and again only if the folder changes.
            try:
                mtime = folder.stat().st_mtime_ns
            except OSError:  # converted and deleted since the folders were listed
                continue
            if self._legacy_sizes.get(folder.name, (None,))[0] != mtime:
                self._legacy_sizes[folder.name] = (mtime, path_bytes(folder))
            total += self._legacy_sizes[folder.name][1]
        with self._lock:
            return {"count": len(folders), "bytes": total, **self._migration}

    def start_migration(self):
        """Convert the old map folders in a background thread (one conversion at a time).
        Returns at once; legacy_status() tells how it is going."""
        with self._lock:
            if self._migration["state"] == "converting":
                return
            self._migration = {"state": "converting", "error": None, "converted": 0}
        threading.Thread(target=self._migrate, name="map-migration", daemon=True).start()

    def _migrate(self):
        problems = []
        try:
            done = self.migrate_legacy(say=lambda text: None, problems=problems)
        except Exception as e:  # e.g. a full disk: say so in the listing, and keep serving
            done, problems = [], [("the old maps", str(e))]
        error = None
        if problems:
            names = ", ".join(f"{map_id} ({why})" for map_id, why in problems)
            error = f"Could not convert {names}. Nothing was lost: the old folders are still there."
        with self._lock:
            self._migration = {"state": "failed" if error else "idle", "error": error, "converted": len(done)}

    def migrate_legacy(self, say=print, problems=None):
        """Convert each old PNG tile folder into a WebP MBTiles file (no download, no rebuild:
        the tiles are re-encoded), then delete the folder and the old build intermediates.
        A folder that cannot be converted is reported (and added to `problems` as (id, why)) and
        left as it was; the others go on. Returns the ids converted."""
        done = []
        with self.exclusive():
            for folder in self.legacy_folders():
                map_id = folder.name
                say(f"  converting {map_id} to the new compact format ...")
                try:
                    convert_png_folder(folder, self.tiles_path(map_id), self._legacy_meta(map_id))
                except (BuildError, OSError, ValueError) as e:
                    say(f"  could not convert {map_id} ({e}); its folder is left as it was")
                    if problems is not None:
                        problems.append((map_id, str(e)))
                    continue
                shutil.rmtree(folder)
                for sub in LEGACY_WORK:
                    for p in (self.root / "data" / "work" / sub).glob(f"{map_id}.*"):
                        delete(p)
                done.append(map_id)
            for stale in self.tiles.glob(f"*{tiling.PARTIAL_SUFFIX}") if self.tiles.is_dir() else []:
                delete(stale)
        if done:
            self.write_catalog()
        return done

    def _legacy_meta(self, map_id):
        """Facts for a converted map: extent and zoom from the old bounds file, dates from the
        downloaded PDF or the scan's control file."""
        extent = {}
        for sub in LEGACY_WORK:
            f = self.root / "data" / "work" / sub / f"{map_id}.bounds.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                extent = data if isinstance(data, dict) else {"bounds": data}
        meta = {"bounds": extent.get("bounds"), "name": map_id}
        entry = self.entries().get(map_id)
        if entry and entry["kind"] == "geopdf":
            pdf = download_maps.raw_paths(entry, self.raw)[0]
            if pdf.exists():
                f = l7014_meta.facts(pdf)
                meta.update(name=f["title"] or map_id, date=f["date"], edition=f["edition"], date_note=f["date_note"])
        control = self.scans / f"{map_id}.json"
        if control.exists():
            m = json.loads(control.read_text(encoding="utf-8")).get("meta", {})
            meta.update(name=m.get("title", map_id), date=m.get("date", ""), date_note=m.get("dateNote", ""))
        return meta


def convert_png_folder(folder, out, meta):
    """Re-encode a <folder>/<z>/<x>/<y>.png tile tree as the WebP MBTiles file `out`.
    meta: {"name", "bounds" (or None: taken from the tiles), plus extra metadata keys}."""
    zooms = sorted(int(p.name) for p in folder.iterdir() if p.is_dir() and p.name.isdigit())
    if not zooms:
        raise BuildError(f"{folder.name} holds no tiles")
    bounds = meta.pop("bounds", None)
    if not bounds:
        # The extent of the deepest zoom's tiles: a little larger than the map, never smaller.
        z = zooms[-1]
        tiles = [(int(p.parent.name), int(p.stem)) for p in folder.glob(f"{z}/*/*.png")]
        if not tiles:
            raise BuildError(f"{folder.name} has no tiles at zoom {z} to take its extent from")
        west = tiling.TMS.bounds(morecantile.Tile(min(x for x, _ in tiles), max(y for _, y in tiles), z))
        east = tiling.TMS.bounds(morecantile.Tile(max(x for x, _ in tiles), min(y for _, y in tiles), z))
        bounds = [west.left, west.bottom, east.right, east.top]
    with tiling.MBTilesWriter(out) as w:
        for png in folder.glob("*/*/*.png"):
            with Image.open(png) as img:  # closed before the folder is deleted (Windows refuses otherwise)
                rgba = np.asarray(img.convert("RGBA"))
            if rgba[..., 3].any():
                w.put(int(png.parent.parent.name), int(png.parent.name), int(png.stem), tiling.encode_webp(rgba))
        w.metadata = tiling.standard_metadata(meta.pop("name"), bounds, zooms[0], zooms[-1], **meta)


# ---- command line ----------------------------------------------------------------------------
def ask_yes_no(question, default=True):
    """Ask on the terminal; Return alone gives the default. Without a terminal nobody can
    answer, so the answer is no (pass --yes to a command to go ahead without asking)."""
    if not sys.stdin.isatty():
        print(f"{question} No terminal to ask on, so no (add --yes to go ahead).")
        return False
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = input(f"{question} {hint} ").strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes", "n", "no"):
            return answer.startswith("y")
        print("Please type y for yes or n for no.")


def chosen(lib, args, *, need_choice=False):
    """The catalogue entries a list/add command line names: ids, an area, a region. With none
    of these, every map; unless need_choice, when that is an error (add never means "all")."""
    if getattr(args, "ids", None):
        entries = lib.entries_for(args.ids)
        if args.area or args.region:
            raise LibraryError("name maps by id, or by --area / --region, not both")
        return entries
    if need_choice and not (args.area or args.region):
        raise LibraryError("name the maps to add: their ids, --area W,S,E,N or --region (see: maps list)")
    area = parse_area(args.area) if args.area else None
    return lib.select(area, args.region)


def cmd_list(lib, args):
    """Print the maps matching the options, one per line, then a total."""
    entries = chosen(lib, args)
    installed = set(lib.installed_ids())
    if args.installed:
        entries = [m for m in entries if m["id"] in installed]
    for m in sorted(entries, key=lambda m: (m["series"], m["id"])):
        mark = "installed" if m["id"] in installed else ""
        print(f"{m['id']:<24} {m['title'][:46]:<46} {human_bytes(m.get('bytes') or 0):>9}  {mark}")
    total = sum(m.get("bytes") or 0 for m in entries)
    print(
        f"{len(entries)} maps, {human_bytes(total)} to download; {len(installed & {m['id'] for m in entries})} installed"
    )
    if not lib.entries():
        print("The map list (data/sources/map_library.json) is missing or empty.")
    return 0


def cmd_add(lib, args):
    """Add the maps named, after saying how much that is and asking (unless --yes)."""
    entries = [m for m in chosen(lib, args, need_choice=True) if not lib.tiles_path(m["id"]).exists()]
    if not entries:
        print("Nothing to add: those maps are already installed (or none match).")
        return 0
    total = sum(m.get("bytes") or 0 for m in entries)
    print(
        f"{len(entries)} map{'s' if len(entries) != 1 else ''}: {human_bytes(total)} to download, "
        f"about {human_bytes(estimated_disk_bytes(entries, lib.catalogue().get('series')))} of disk space once ready."
    )
    print("Each map takes about a minute to prepare after it downloads.")
    # Return alone says yes to a handful of maps, never to a whole region.
    if not args.yes and not ask_yes_no("Add them?", default=len(entries) <= 20):
        print("Nothing was added.")
        return 0
    lib.add([m["id"] for m in entries])
    order = {m["id"]: i for i, m in enumerate(entries, 1)}
    shown = {}

    def report(job):
        # One line per change of state, so the output reads well in a log as well as a terminal.
        map_id = job.entry["id"]
        if shown.get(map_id) == job.state:
            return
        shown[map_id] = job.state
        where = f"[{order[map_id]}/{len(entries)}] {job.entry['title']}"
        if job.state == "downloading":
            print(f"{where}: downloading {human_bytes(job.entry.get('bytes') or 0)} ...", flush=True)
        elif job.state == "building":
            print(f"{where}: preparing the map ...", flush=True)
        elif job.state == "installed":
            print(f"{where}: ready", flush=True)
            for note in job.notes:
                print(f"  note: {note}", flush=True)
        elif job.state == "failed":
            print(f"{where}: FAILED: {job.error}", flush=True)
        elif job.state == "removed":
            print(f"{where}: removed in the map viewer, so not added", flush=True)

    def waiting():
        print("The map viewer is adding a map right now; waiting for it to finish ...", flush=True)

    try:
        with lib.exclusive(on_wait=waiting):
            pass  # only to say so up front if another process is busy; each map takes the lock again
        lib.run_pending(report)
    except KeyboardInterrupt:
        print("\nStopped. The maps that were ready are kept; run the same command again to add the rest.")
        return 130
    added = sum(state == "installed" for state in shown.values())
    print(f"{added} of {len(entries)} maps added. Open the map viewer to see them.")
    return 1 if any(state == "failed" for state in shown.values()) else 0


def cmd_remove(lib, args):
    """Remove the maps named (or all installed maps with --all), deleting tiles and downloads."""
    if args.all:
        ids = lib.installed_ids()
        if not ids:
            print("No maps are installed.")
            return 0
        size = sum(lib.disk_bytes(i, lib.entries().get(i)) or 0 for i in ids)
        if not args.yes and not ask_yes_no(f"Remove all {len(ids)} maps ({human_bytes(size)})?", default=False):
            print("Nothing was removed.")
            return 0
    elif args.ids:
        ids = args.ids
    else:
        raise LibraryError("name the maps to remove, or use --all")
    removed = lib.remove(ids)
    print(f"Removed {len(removed)} map{'s' if len(removed) != 1 else ''}.")
    return 0


def cmd_migrate(lib, args):
    """Convert map folders made by older versions."""
    done = lib.migrate_legacy()
    print(f"Converted {len(done)} map{'s' if len(done) != 1 else ''}." if done else "Nothing to convert.")
    return 0


def parse_args(argv=None):
    """Command-line options (see the module docstring)."""
    ap = argparse.ArgumentParser(prog="python3 start.py maps", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name, text in (("list", "list the maps you can add"), ("add", "download and prepare maps")):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("ids", nargs="*", metavar="ID", help="map ids (see the list)")
        p.add_argument("--area", metavar="W,S,E,N", help="maps overlapping this box (degrees)")
        p.add_argument("--region", choices=REGIONS, help="maps in this part of Vietnam")
        if name == "list":
            p.add_argument("--installed", action="store_true", help="only maps already added")
        else:
            p.add_argument("--yes", action="store_true", help="do not ask before downloading")
    p = sub.add_parser("remove", help="delete maps (tiles and downloads)", description="delete maps")
    p.add_argument("ids", nargs="*", metavar="ID", help="map ids")
    p.add_argument("--all", action="store_true", help="every installed map")
    p.add_argument("--yes", action="store_true", help="do not ask before removing all")
    sub.add_parser("migrate", help="convert maps made by older versions", description="convert old map folders")
    return ap.parse_args(argv)


def main(argv=None):
    """Run one library command. Returns the exit status."""
    args = parse_args(argv)
    lib = Library()
    command = {"list": cmd_list, "add": cmd_add, "remove": cmd_remove, "migrate": cmd_migrate}[args.command]
    try:
        return command(lib, args)
    except LibraryError as e:
        print(f"{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
