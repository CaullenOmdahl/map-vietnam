"""Helpers shared by every ingester: polite cached downloads, local settings and the area of interest.

Every ingester is a module scripts/ingest/<source>.py with a main() that:
  1. fetches raw data with cached_get (files land in data/raw/<source>/, never re-fetched
     unless --refresh), respecting the source's terms and a minimum delay between requests;
  2. turns it into store.Evidence records (original coordinate + datum kept, uncertainty set);
  3. calls store.upsert_source once and store.sync_source / store.upsert_many with the records.
Run one with:  .venv/bin/python scripts/ingest/<source>.py   (scripts/records.py runs one when a
person adds its source, in the viewer or with python3 start.py setup).

Local settings come from the environment, or from a .env file at the repository root
(see .env.example); a variable already set in the environment wins over the file.

Progress: when the environment variable MAPVIETNAM_PROGRESS is set (scripts/records.py sets it for
the ingesters it runs), progress() prints lines of the form

    progress 0.42 Downloading 134 MB of 320 MB
    progress - Saving 26,551 air strikes

that is, the word "progress", how far along the current piece of work is (0 to 1, or "-" when
that cannot be known), then a short note in plain words for the person waiting. cached_get
reports every download bigger than one block this way; an ingester adds its own lines where it
knows how much work there is. Run by hand, without the variable, nothing extra is printed.
"""

import hashlib
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths  # noqa: E402

RAW = paths.RAW
ENV_FILE = paths.ENV_FILE
# The project page lets an archive see what is calling it (Wikimedia, for one, throttles clients that
# give no way to find out); it says nothing about the person running the copy.
BASE_USER_AGENT = "MapVietnam/0.1 (+https://github.com/CaullenOmdahl/map-vietnam; historical map research; low-volume)"
# Area the project started with (around Cao Lanh, Dong Thap); ingesters for national sources
# still load everything they can, so the map can widen later without re-ingesting.
AREA_OF_INTEREST = (105.35, 10.15, 105.95, 10.75)
PROGRESS_ENV = "MAPVIETNAM_PROGRESS"  # set by whoever wants progress lines on stdout (see the docstring)
DOWNLOAD_BLOCK = 1 << 20  # bytes read at a time; a download is reported once it is past its first block
RETRIES = 3  # attempts per download
RETRY_WAIT = 10.0  # seconds to wait after the first failure; after the second, twice that
RETRY_STATUS = {429, 500, 502, 503, 504}  # HTTP errors worth asking again about
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")
_last_request = {}


def load_env(path=None):
    """Copy KEY=VALUE lines from a .env file (default: ENV_FILE, the repository's .env) into
    os.environ, without overriding variables that are already set. Blank values, comments and
    malformed lines are ignored; one pair of matching quotes around a value is removed."""
    path = ENV_FILE if path is None else path
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _ENV_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        key, value = m[1], m[2]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value and key not in os.environ:
            os.environ[key] = value


def user_agent():
    """The User-Agent sent with every request: a generic product string, plus the optional
    MAPVIETNAM_CONTACT (an e-mail or URL) so a site operator can reach whoever runs the copy."""
    contact = os.environ.get("MAPVIETNAM_CONTACT", "").strip()
    return f"{BASE_USER_AGENT[:-1]}; {contact})" if contact else BASE_USER_AGENT


def _tls_context():
    """System trust store plus certifi's bundle. The python.org macOS installer ships no CA
    certificates until its "Install Certificates" step is run, so without certifi every HTTPS
    download would fail there with CERTIFICATE_VERIFY_FAILED."""
    ctx = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return ctx
    ctx.load_verify_locations(certifi.where())
    return ctx


def progress(fraction=None, text=""):
    """Tell whoever started this ingester how far along it is (see the module docstring).

    fraction: 0..1 of the current piece of work, or None when the total is not known.
    text: a few plain words, e.g. "Reading page 812 of 4,012". Silent unless MAPVIETNAM_PROGRESS is set."""
    if not os.environ.get(PROGRESS_ENV):
        return
    amount = "-" if fraction is None else f"{min(max(fraction, 0.0), 1.0):.4f}"
    print(f"progress {amount} {text}".rstrip(), flush=True)


def megabytes(n):
    """'7.6 MB', '134 MB': a byte count as the person waiting reads it (decimal megabytes)."""
    return f"{n / 1e6:.0f} MB" if n >= 1e7 else f"{n / 1e6:.1f} MB"


load_env()
USER_AGENT = user_agent()
_TLS = _tls_context()


def _download(req, path, timeout):
    """Stream one response to <path>.part and rename it to path once complete."""
    with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as r:
        tmp = path.with_suffix(path.suffix + ".part")
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(tmp, "wb") as f:
            while chunk := r.read(DOWNLOAD_BLOCK):
                f.write(chunk)
                done += len(chunk)
                if total > DOWNLOAD_BLOCK:
                    progress(done / total, f"Downloading {megabytes(done)} of {megabytes(total)}")
                elif done > DOWNLOAD_BLOCK:
                    progress(None, f"Downloading {megabytes(done)}")
        tmp.replace(path)


def cached_get(url, source, *, name=None, min_interval=2.0, refresh=False, headers=None, data=None, timeout=120):
    """Fetch url once and keep it under data/raw/<source>/ (source may hold a sub-folder,
    e.g. "scans/koch_1889_f2"). Returns the local Path.

    name: file name to save as (default: a hash of url + POST data, so distinct queries to the
    same endpoint get distinct files). min_interval: seconds to leave between requests to the
    same host. timeout: seconds to wait on any single network read, not the whole download.
    The body is streamed to <name>.part and renamed only when complete, so an interrupted
    download is never mistaken for a finished one. A body bigger than one block is reported
    with progress() as it arrives, by bytes when the server says how long it is.
    """
    folder = RAW / source
    folder.mkdir(parents=True, exist_ok=True)
    key = url + (data.decode() if isinstance(data, bytes) else (data or ""))
    path = folder / (name or hashlib.sha1(key.encode()).hexdigest()[:16])
    if path.exists() and not refresh:
        return path
    host = url.split("/")[2]
    wait = min_interval - (time.monotonic() - _last_request.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    req = urllib.request.Request(
        url,
        data=data.encode() if isinstance(data, str) else data,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    try:
        for attempt in range(1, RETRIES + 1):
            try:
                _download(req, path, timeout)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                # Public endpoints (Overpass, WDQS) answer 502/504 under load: wait and ask again
                # rather than fail a whole setup. Anything else (404, 403) is final.
                code = getattr(e, "code", None)
                if attempt == RETRIES or (code is not None and code not in RETRY_STATUS):
                    raise
                time.sleep(RETRY_WAIT * attempt)
    finally:
        # Count failed requests too: a retry straight after an error is still a request.
        _last_request[host] = time.monotonic()
    return path


def in_bbox(lon, lat, bbox=AREA_OF_INTEREST):
    """True when (lon, lat) lies inside bbox = (west, south, east, north), edges included."""
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n
