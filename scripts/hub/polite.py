"""Polite upstream fetching: per-host rate limiting, Retry-After backoff, and single-flight coalescing.

Government and academic archives (NAKALA, USGS EROS, NARA, NASA SlideRule, Overpass, VHPA) must
never be hammered by concurrent client requests. `SingleFlight` ensures that simultaneous requests
for the same resource share a single upstream execution, and `HostPacer` enforces respectful
minimum intervals between requests to any given host.
"""

import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def tls_context():
    """System trust store plus certifi's CA bundle so frozen macOS .app bundles verify HTTPS."""
    ctx = ssl.create_default_context()
    try:
        import certifi
    except ImportError:
        return ctx
    ctx.load_verify_locations(certifi.where())
    return ctx


_TLS_CTX = tls_context()

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
DEFAULT_MAX_FETCH_BYTES = 256 << 20  # 256 MiB ceiling per upstream HTTP response
FETCH_CHUNK = 1 << 20


def is_safe_id(value):
    """True when `value` is a safe single-segment identifier with no `..` traversal."""
    s = str(value or "")
    return bool(SAFE_ID_RE.fullmatch(s)) and ".." not in s


def _ip_is_allowed(ip_obj, *, allow_loopback=False):
    if ip_obj.is_loopback:
        return bool(allow_loopback)
    return bool(
        ip_obj.is_global
        and not ip_obj.is_private
        and not ip_obj.is_link_local
        and not ip_obj.is_multicast
        and not ip_obj.is_reserved
        and not ip_obj.is_unspecified
    )


def validate_fetch_url(url, *, allow_loopback=False):
    """Validate an upstream HTTP(S) URL against scheme, private-network, and metadata SSRF."""
    parsed = urllib.parse.urlparse(str(url or ""))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("URL is missing a hostname.")
    is_local_name = host in ("127.0.0.1", "localhost", "::1")
    if scheme == "http":
        if not (allow_loopback and is_local_name):
            raise ValueError("Upstream URLs must use https://.")
    elif scheme != "https":
        raise ValueError(f"Unsupported URL scheme {scheme!r}; only https:// is allowed.")

    try:
        ip_obj = ipaddress.ip_address(host)
        if not _ip_is_allowed(ip_obj, allow_loopback=allow_loopback):
            raise ValueError(f"Refusing non-public upstream address {host!r}.")
        return
    except ValueError as e:
        if "Refusing" in str(e):
            raise

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise urllib.error.URLError(f"DNS lookup failed for {host}: {e}") from None
    if not infos:
        raise urllib.error.URLError(f"No DNS records for {host}")
    for info in infos:
        addr = info[4][0]
        ip_obj = ipaddress.ip_address(addr.split("%", 1)[0])
        if not _ip_is_allowed(ip_obj, allow_loopback=allow_loopback):
            raise ValueError(f"Refusing upstream host {host!r} resolving to non-public IP {ip_obj}.")


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


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate every redirect hop against SSRF/downgrade and strip auth headers cross-origin."""

    def __init__(self, *, allow_loopback=False):
        super().__init__()
        self._allow_loopback = allow_loopback

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old_p = urllib.parse.urlparse(req.full_url)
        new_p = urllib.parse.urlparse(newurl)
        if old_p.scheme.lower() == "https" and new_p.scheme.lower() != "https":
            raise urllib.error.HTTPError(newurl, code, "Refusing HTTPS to HTTP downgrade redirect", headers, fp)
        try:
            validate_fetch_url(newurl, allow_loopback=self._allow_loopback)
        except ValueError as e:
            raise urllib.error.HTTPError(newurl, code, str(e), headers, fp) from None
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if (old_p.scheme.lower(), old_p.hostname, old_p.port) != (new_p.scheme.lower(), new_p.hostname, new_p.port):
            for sensitive in ("Authorization", "X-auth-token", "X-Auth-Token", "Cookie"):
                redirected.headers.pop(sensitive, None)
                redirected.unredirected_hdrs.pop(sensitive, None)
        return redirected


def polite_fetch_bytes(
    url,
    *,
    pacer=None,
    flight=None,
    headers=None,
    data=None,
    method=None,
    contact="",
    timeout=30,
    max_bytes=DEFAULT_MAX_FETCH_BYTES,
    allow_loopback=False,
):
    """Fetch `(body_bytes, content_type)` from `url` politely with SSRF validation, safe redirects,
    bounded body reads, single-flight deduplication, and 429 retry."""
    validate_fetch_url(url, allow_loopback=allow_loopback)
    req_headers = {"User-Agent": user_agent(contact), **(headers or {})}
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_TLS_CTX),
        _SafeRedirectHandler(allow_loopback=allow_loopback),
    )

    def _run():
        last_err = None
        for attempt in range(MAX_RETRIES):
            if pacer is not None:
                pacer.wait(url)
            req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
            try:
                with opener.open(req, timeout=timeout) as resp:
                    ctype = resp.headers.get("Content-Type") or "application/octet-stream"
                    cl_raw = resp.headers.get("Content-Length")
                    expected = int(cl_raw) if (cl_raw and cl_raw.isdigit()) else None
                    if expected is not None and expected > max_bytes:
                        raise ValueError(f"Upstream response ({expected} bytes) exceeds safety limit ({max_bytes}).")
                    chunks = []
                    total = 0
                    while True:
                        chunk = resp.read(FETCH_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(f"Upstream response exceeded safety limit ({max_bytes} bytes).")
                        chunks.append(chunk)
                    if expected is not None and total != expected:
                        raise urllib.error.URLError(f"Upstream connection closed early ({total} of {expected} bytes).")
                    return b"".join(chunks), ctype
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
