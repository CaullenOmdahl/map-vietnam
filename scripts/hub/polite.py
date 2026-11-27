"""Polite upstream fetching: per-host rate limiting, Retry-After backoff, and single-flight coalescing.

Government and academic archives (NAKALA, USGS EROS, NARA, NASA SlideRule, Overpass, VHPA) must
never be hammered by concurrent client requests. `SingleFlight` ensures that simultaneous requests
for the same resource share a single upstream execution, and `HostPacer` enforces respectful
minimum intervals between requests to any given host.
"""

import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Minimum delay (seconds) between consecutive requests to the same upstream hostname.
HOST_INTERVALS_S = {
    "api.nakala.fr": 1.2,
    "nakala.fr": 1.2,
    "m2m.cr.usgs.gov": 1.0,
    "earthexplorer.usgs.gov": 1.0,
    "store.usgs.gov": 0.8,
    "catalog.archives.gov": 1.0,
    "vhpa.org": 3.0,
    "www.vhpa.org": 3.0,
    "overpass-api.de": 5.0,
    "slideruleearth.io": 0.5,
    "planetarycomputer.microsoft.com": 0.4,
    "api.imagery.hotosm.org": 0.4,
    "annotations.allmaps.org": 0.5,
    "s3.opengeohub.org": 0.15,
    "opentopography.s3.sdsc.edu": 0.15,
}
DEFAULT_HOST_INTERVAL_S = 0.35
MAX_RETRIES = 3


def user_agent(contact="", role="Hub"):
    """Polite, descriptive User-Agent header naming the project and optional operator contact."""
    extra = f"; {contact.strip()}" if contact and contact.strip() else ""
    return (
        f"MapVietnam-{role}/0.3 "
        f"(+https://github.com/CaullenOmdahl/map-vietnam; historical research infinite cache{extra})"
    )


class HostPacer:
    """Thread-safe per-hostname request pacer with per-host serializing locks."""

    def __init__(
        self, intervals=None, default_interval=DEFAULT_HOST_INTERVAL_S, clock=time.monotonic, sleep=time.sleep
    ):
        self._intervals = dict(HOST_INTERVALS_S)
        if intervals:
            self._intervals.update(intervals)
        self._default = float(default_interval)
        self._clock = clock
        self._sleep = sleep
        self._guard = threading.Lock()
        self._hosts = {}  # host -> (threading.Lock, next_allowed_monotonic)

    def wait(self, url):
        """Wait until `url`'s host is ready to accept the next polite request."""
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        interval = self._intervals.get(host, self._default)
        if interval <= 0:
            return
        with self._guard:
            host_lock, _ = self._hosts.setdefault(host, (threading.Lock(), 0.0))
        with host_lock:
            with self._guard:
                _, next_at = self._hosts[host]
            now = self._clock()
            if now < next_at:
                self._sleep(next_at - now)
                now = self._clock()
            with self._guard:
                self._hosts[host] = (host_lock, now + interval)

    def penalize(self, url, retry_after_s):
        """Push back `url`'s host schedule when an archive responds with 429 / Retry-After."""
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        delay = max(1.0, min(float(retry_after_s), 120.0))
        with self._guard:
            host_lock, next_at = self._hosts.setdefault(host, (threading.Lock(), 0.0))
            self._hosts[host] = (host_lock, max(next_at, self._clock() + delay))


class SingleFlight:
    """Coalesce concurrent calls with the same `key` so `fn()` runs once and all callers share its result."""

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}  # key -> _Call

    def do(self, key, fn):
        """Execute `fn()` once across all concurrent callers sharing `key`."""
        with self._lock:
            call = self._inflight.get(key)
            if call is None:
                call = _Call()
                self._inflight[key] = call
                leader = True
            else:
                leader = False

        if not leader:
            call.done.wait()
            if call.exc is not None:
                raise call.exc
            return call.result

        try:
            call.result = fn()
            return call.result
        except BaseException as exc:
            call.exc = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            call.done.set()


class _Call:
    def __init__(self):
        self.done = threading.Event()
        self.result = None
        self.exc = None


def polite_fetch_bytes(url, *, pacer=None, flight=None, headers=None, data=None, method=None, contact="", timeout=30):
    """Fetch `(body_bytes, content_type)` from `url` politely with single-flight deduplication and 429 retry."""
    req_headers = {"User-Agent": user_agent(contact), **(headers or {})}

    def _run():
        last_err = None
        for attempt in range(MAX_RETRIES):
            if pacer is not None:
                pacer.wait(url)
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                    return resp.read(), ctype
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code in (429, 503) and attempt + 1 < MAX_RETRIES:
                    retry_hdr = e.headers.get("Retry-After") if e.headers else None
                    try:
                        wait_s = float(retry_hdr) if retry_hdr else (2.0 ** (attempt + 1))
                    except ValueError:
                        wait_s = 2.0 ** (attempt + 1)
                    if pacer is not None:
                        pacer.penalize(url, wait_s)
                    else:
                        time.sleep(min(wait_s, 15.0))
                    continue
                raise
            except urllib.error.URLError as e:
                last_err = e
                if attempt + 1 < MAX_RETRIES:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
        raise last_err  # pragma: no cover

    key = ("fetch", method or ("POST" if data is not None else "GET"), url, data)
    return flight.do(key, _run) if flight is not None else _run()
