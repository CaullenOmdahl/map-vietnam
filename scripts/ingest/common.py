"""Helpers shared by every ingester: polite cached downloads, local settings and the area of interest.

Every ingester is a module scripts/ingest/<source>.py with a main() that:
  1. fetches raw data with cached_get (files land in data/raw/<source>/, never re-fetched
     unless --refresh), respecting the source's terms and a minimum delay between requests;
  2. turns it into store.Evidence records (original coordinate + datum kept, uncertainty set);
  3. calls store.upsert_source once and store.sync_source / store.upsert_many with the records.
Run one with:  .venv/bin/python scripts/ingest/<source>.py   (start.py runs them all in order).

Local settings come from the environment, or from a .env file at the repository root
(see .env.example); a variable already set in the environment wins over the file.
"""

import hashlib
import os
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store  # noqa: E402

RAW = store.ROOT / "data" / "raw"
ENV_FILE = store.ROOT / ".env"
# The project page lets an archive see what is calling it (Wikimedia, for one, throttles clients that
# give no way to find out); it says nothing about the person running the copy.
BASE_USER_AGENT = "MapVietnam/0.1 (+https://github.com/CaullenOmdahl/map-vietnam; historical map research; low-volume)"
# Area the project started with (around Cao Lanh, Dong Thap); ingesters for national sources
# still load everything they can, so the map can widen later without re-ingesting.
AREA_OF_INTEREST = (105.35, 10.15, 105.95, 10.75)
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


load_env()
USER_AGENT = user_agent()
_TLS = _tls_context()


def cached_get(url, source, *, name=None, min_interval=2.0, refresh=False, headers=None, data=None, timeout=120):
    """Fetch url once and keep it under data/raw/<source>/ (source may hold a sub-folder,
    e.g. "scans/koch_1889_f2"). Returns the local Path.

    name: file name to save as (default: a hash of url + POST data, so distinct queries to the
    same endpoint get distinct files). min_interval: seconds to leave between requests to the
    same host. timeout: seconds to wait on any single network read, not the whole download.
    The body is streamed to <name>.part and renamed only when complete, so an interrupted
    download is never mistaken for a finished one.
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
        with urllib.request.urlopen(req, timeout=timeout, context=_TLS) as r:
            tmp = path.with_suffix(path.suffix + ".part")
            with open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            tmp.replace(path)
    finally:
        # Count failed requests too: a retry straight after an error is still a request.
        _last_request[host] = time.monotonic()
    return path


def in_bbox(lon, lat, bbox=AREA_OF_INTEREST):
    """True when (lon, lat) lies inside bbox = (west, south, east, north), edges included."""
    w, s, e, n = bbox
    return w <= lon <= e and s <= lat <= n
