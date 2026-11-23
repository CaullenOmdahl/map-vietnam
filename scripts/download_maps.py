"""Download the file behind one map of the map library, checked, and rebuild a scan's working image.

scripts/library.py calls fetch_map when a map is added (from the viewer's Old maps card or
`python3 start.py maps add`); this file can also be run by hand to fetch files without building.
Nothing is ever downloaded unless a map was asked for by id.

Where the downloads go (raw_paths), kept so a map can be rebuilt without downloading it again:
  GeoPDF sheets  data/raw/<series>/<id>_geo.pdf
  scans          data/raw/scans/<id>/work.jpg, the image whose pixels data/scans/<id>.json's
                 control points refer to

The rules:
  - Only the URLs in the map's catalogue entry are fetched, and only over https, redirects
    included (a redirect to plain http is refused). They are tried in order, so a mirror listed
    second is the fallback when the first host fails.
  - At most one request a second to a host, with the project's User-Agent (scripts/ingest/common.py).
  - Network errors and busy servers (HTTP 5xx, 429) are retried ATTEMPTS times; any other HTTP
    error (404 and the like) moves on to the next URL at once.
  - The body is streamed to <name>.part and renamed only when complete: every byte the server
    announced arrived (or, when it announced no length, at least the catalogue's size), and
    the file is what it should be (a whole PDF, from %PDF- to %%EOF; an image of the right size).
    A file that is whole but not usable (an error page, a broken copy on the archive) moves on
    to the next URL at once.
  - Size and SHA-256 are compared with the catalogue. A difference is reported as a note, not
    a failure: archives re-encode files now and then, a GeoPDF carries its own georeferencing,
    and a scan's pixel grid is checked exactly (check_work_image) because control points need it.
  - `cancelled()` is polled between blocks, before each attempt and while waiting to retry;
    when it turns true the download stops, the .part file is removed and Cancelled is raised.

Why scans may be derived: control points are pixel positions, so work.jpg must have the pixel grid
of the image they were picked on. Two archives serve JPEG 2000 only; their work.jpg is the decoded
image (one at half resolution, then Lanczos-resized to 10,000 px wide) saved as JPEG quality 90
without chroma subsampling, which reproduces the original to within JPEG noise. The archive's
file is deleted once work.jpg is made and checked, since only work.jpg is needed to rebuild.

Usage: .venv/bin/python scripts/download_maps.py ID [ID ...]   (ids from data/sources/map_library.json)
       .venv/bin/python scripts/download_maps.py --derive SRC.jp2 DST.jpg '{"jpeg_quality": 90, ...}'
       (the second form is how scripts/library.py runs derive_work_image in a child process)
"""

import argparse
import contextlib
import hashlib
import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent / "ingest"))
import common  # noqa: E402
import paths  # noqa: E402

RAW = paths.RAW
WORK_IMAGE = "work.jpg"
ATTEMPTS = 3  # tries per URL on network errors and busy servers (HTTP 5xx, 429)
RETRY_WAIT_S = 10
MIN_INTERVAL_S = 1.0  # at most one request a second to any one host
TIMEOUT_S = 60  # per network read, not for the whole download; also bounds how long a stop waits
BLOCK = 1 << 20
CANCEL_POLL_S = 0.2  # how often a wait to retry checks whether the map was removed
# A whole PDF ends with %%EOF, perhaps followed by a line break or a little padding.
PDF_TAIL_BYTES = 1024
# Only these URL prefixes are ever fetched; the catalogue lists https addresses only.
ALLOWED_URL_PREFIXES = ("https://",)

_last_request = {}  # host -> time.monotonic() of the last request to it
_rate_lock = threading.Lock()
_pixels_lock = threading.Lock()


class MapFetchError(Exception):
    """A map file could not be fetched or is not usable; str() is a plain-English sentence."""


class Cancelled(Exception):
    """The download was stopped because the map was removed."""


def raw_paths(entry, raw_root=None):
    """(the file the builder reads, [paths to delete when the map is removed]) for an entry."""
    raw_root = raw_root or RAW
    if entry["kind"] == "scan":
        folder = raw_root / "scans" / entry["id"]
        return folder / WORK_IMAGE, [folder]
    pdf = raw_root / entry["series"] / f"{entry['id']}_geo.pdf"
    return pdf, [pdf]


def sha256_of(path):
    """Hex SHA-256 of a file, read in 1 MiB blocks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(BLOCK):
            h.update(block)
    return h.hexdigest()


def _wait_turn(host):
    """Sleep until a request to `host` keeps to MIN_INTERVAL_S, then claim the slot."""
    with _rate_lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_request.get(host, -MIN_INTERVAL_S))
        _last_request[host] = time.monotonic() + max(wait, 0)
    if wait > 0:
        time.sleep(wait)


@contextlib.contextmanager
def trusted_large_images():
    """Let Pillow open the catalogue's scans, up to 25,690 x 18,049 px: far past its
    decompression-bomb guard. Only for files the catalogue names; the guard is restored after."""
    with _pixels_lock:
        limit, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, None
        try:
            yield
        finally:
            Image.MAX_IMAGE_PIXELS = limit


def host_of(url):
    """'store.usgs.gov' from 'https://store.usgs.gov/...'."""
    return url.split("/")[2]


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to an allowed (https) address, keeping to the per-host pace."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith(ALLOWED_URL_PREFIXES):
            # Damaged, not a plain MapFetchError: asking again gets the same redirect, so no retries.
            raise Damaged(f"the archive redirected to {newurl.split('?')[0]}, which is not an https address")
        _wait_turn(host_of(newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener():
    """A urllib opener with the project's TLS settings and https-only redirects."""
    return urllib.request.build_opener(urllib.request.HTTPSHandler(context=common._TLS), HttpsOnlyRedirects)


def fetch_url(url, dest, *, progress=None, cancelled=None, expected_bytes=None):
    """Stream `url` into `dest` (via dest.part). Returns the SHA-256 of what was written.

    progress(done_bytes, total_bytes) is called after each block; total is the server's
    Content-Length, else expected_bytes, else None. Raises Cancelled, urllib/OS errors, or
    MapFetchError when the body is shorter than announced (or, when the server announced no
    length, shorter than expected_bytes: a connection cut early looks just like a whole file).
    """
    if cancelled and cancelled():
        raise Cancelled()
    _wait_turn(host_of(url))
    req = urllib.request.Request(url, headers={"User-Agent": common.USER_AGENT})
    tmp = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest, done = hashlib.sha256(), 0
    try:
        with opener().open(req, timeout=TIMEOUT_S) as r, open(tmp, "wb") as f:
            announced = int(r.headers["Content-Length"]) if r.headers.get("Content-Length") else None
            total = announced or expected_bytes
            while block := r.read(BLOCK):
                if cancelled and cancelled():
                    raise Cancelled()
                f.write(block)
                digest.update(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if announced is not None and done != announced:
            raise MapFetchError(f"the download stopped early ({done:,} of {announced:,} bytes)")
        if announced is None and expected_bytes and done < expected_bytes:
            raise MapFetchError(f"the download stopped early ({done:,} of about {expected_bytes:,} bytes)")
        tmp.replace(dest)
    finally:
        tmp.unlink(missing_ok=True)
    return digest.hexdigest()


def wait_or_cancel(seconds, cancelled):
    """Sleep `seconds`, waking up early with Cancelled if cancelled() turns true."""
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if cancelled and cancelled():
            raise Cancelled()
        time.sleep(min(left, CANCEL_POLL_S))


def fetch_any(entry, dest, *, progress=None, cancelled=None, check=None):
    """Download the entry's file to `dest`, trying each of its URLs in turn. Returns a list of
    notes (differences from the catalogue). Raises MapFetchError with the last reason.
    check(dest), if given, inspects each download: a Damaged file moves on to the next URL
    at once (the same address would send the same file again)."""
    reason = "it has no download address"
    for url in entry["urls"]:
        if not url.startswith(ALLOWED_URL_PREFIXES):
            reason = f"{url} is not an https address"
            continue
        for attempt in range(1, ATTEMPTS + 1):
            try:
                # An estimated size (an IIIF image, made on request) is only a guide for progress.
                expected = None if entry.get("bytes_estimated") else entry.get("bytes")
                digest = fetch_url(url, dest, progress=progress, cancelled=cancelled, expected_bytes=expected)
                if check:
                    check(dest)
            except Damaged as e:
                reason = str(e)
                break
            except urllib.error.HTTPError as e:
                reason = f"the archive answered HTTP {e.code} {e.reason}"
                if e.code < 500 and e.code != 429:  # missing or refused: asking again will not help
                    break
            except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
                reason = f"network error: {getattr(e, 'reason', e)}"
            except MapFetchError as e:
                reason = str(e)
            else:
                return catalogue_notes(entry, dest, digest)
            if attempt < ATTEMPTS:
                wait_or_cancel(RETRY_WAIT_S, cancelled)
    raise MapFetchError(f"Could not download it ({reason}). Check the internet connection and try again.")


class Damaged(MapFetchError):
    """The archive sent a whole file, but not a usable one (an error page, a broken copy)."""


def catalogue_notes(entry, path, digest):
    """Differences between a downloaded file and its catalogue entry, as notes (see the docstring)."""
    size = path.stat().st_size
    # An IIIF image is made on request: its size was only estimated, and no checksum is kept.
    size_ok = entry.get("bytes_estimated") or entry.get("bytes") in (None, size)
    if size_ok and (entry.get("sha256") in (None, digest)):
        return []
    return [f"the archive's file differs from the one this project was made with ({size:,} bytes); using it anyway"]


def check_pdf(path):
    """Remove `path` and raise Damaged unless it is a whole PDF: it starts like one (servers
    sometimes send an HTML error page with status 200) and ends like one. On the USGS Store
    some copies are broken this way: the first few hundred KB of the PDF, then megabytes of
    zero bytes up to the full size, which no PDF reader opens."""
    with open(path, "rb") as f:
        head = f.read(5)
        f.seek(max(path.stat().st_size - PDF_TAIL_BYTES, 0))
        tail = f.read()
    if head != b"%PDF-":
        path.unlink()
        raise Damaged("the archive sent something that is not a PDF, perhaps an error page")
    if b"%%EOF" not in tail:
        path.unlink()
        raise Damaged("the archive's copy of this map is damaged: it is cut short")


def derive_work_image(src, dst, derive):
    """Rebuild work.jpg from a JPEG 2000 archive file.

    derive: {"jp2_reduce": n (drop n resolution levels, halving the size each time; default 0),
             "width": w (Lanczos resize to w px wide, height rounded to keep the aspect; optional),
             "jpeg_quality": q}. Saved without chroma subsampling (4:4:4), as the originals were.
    """
    with trusted_large_images(), Image.open(src) as opened:  # closed at once: fetch_map deletes src next
        # Decoding at a lower resolution level is how the original was made (and needs a quarter
        # of the memory); Pillow's JPEG 2000 reader takes it as the `reduce` attribute, set before load.
        opened.reduce = derive.get("jp2_reduce", 0)
        opened.load()
        img = opened.convert("RGB")
    if derive.get("width") and img.width != derive["width"]:
        height = round(img.height * derive["width"] / img.width)
        img = img.resize((derive["width"], height), Image.Resampling.LANCZOS)
    tmp = dst.with_name(dst.name + ".part")
    try:
        img.save(tmp, format="JPEG", quality=derive["jpeg_quality"], subsampling=0)
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def check_work_image(path, entry):
    """Raise MapFetchError (removing the file) unless the working image opens and has the
    catalogue's pixel size: control points on a different grid would misplace the map."""
    try:
        with trusted_large_images(), Image.open(path) as img:  # reads the header only
            size = list(img.size)
    except OSError as e:
        path.unlink()
        raise MapFetchError(f"The downloaded image could not be read ({e}). Try again later.") from None
    want = entry.get("work_size")
    if want and size != list(want):
        path.unlink()
        raise MapFetchError(
            f"The archive's image is {size[0]} x {size[1]} px, but this map's control points were measured "
            f"on a {want[0]} x {want[1]} px image, so it would be misplaced. The archive may have changed its copy."
        )


def fetch_map(entry, raw_root=None, *, progress=None, cancelled=None, derive=derive_work_image):
    """Make sure the file the builder needs for `entry` is on disk. Returns (path, notes).
    A file already there (an earlier download) is reused without asking the network.
    derive(src, dst, settings) makes a scan's work.jpg; scripts/library.py passes one that
    runs derive_work_image in a child process."""
    path, _ = raw_paths(entry, raw_root)
    if entry["kind"] != "scan":
        if not path.exists():
            return path, fetch_any(entry, path, progress=progress, cancelled=cancelled, check=check_pdf)
        try:
            check_pdf(path)
        except Damaged as e:
            raise MapFetchError(
                f"The earlier download was not usable ({e}). Try again to download it afresh."
            ) from None
        return path, []
    notes = []
    if not path.exists():
        if entry.get("derive"):
            src = path.with_name(entry.get("source_file", "src.jp2"))
            if not src.exists():
                notes = fetch_any(entry, src, progress=progress, cancelled=cancelled)
            try:
                derive(src, path, entry["derive"])
            except OSError as e:
                src.unlink()
                raise MapFetchError(f"The archive's image could not be decoded ({e}). Try again later.") from None
            src.unlink()  # only work.jpg is needed from here on
        else:
            notes = fetch_any(entry, path, progress=progress, cancelled=cancelled)
    check_work_image(path, entry)
    return path, notes


def main(argv=None):
    """Download the files of the maps named on the command line (no building). Returns the
    number of failures."""
    import library  # here, not at the top: library imports this module

    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--derive"]:
        if len(argv) != 4:
            sys.exit("usage: download_maps.py --derive SRC DST SETTINGS_JSON")
        derive_work_image(Path(argv[1]), Path(argv[2]), json.loads(argv[3]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ids", nargs="+", metavar="ID", help="map ids from data/sources/map_library.json")
    args = ap.parse_args(argv)
    lib = library.Library()
    try:
        entries = lib.entries_for(args.ids)
    except library.LibraryError as e:
        sys.exit(str(e))  # a plain sentence, not a traceback
    failures = 0

    def waiting():
        print("Another process is adding maps; waiting for it to finish ...", flush=True)

    # The same lock as the map library: whoever adds a map deletes unfinished downloads first.
    with lib.exclusive(on_wait=waiting):
        for entry in entries:
            try:
                path, notes = fetch_map(entry)
            except MapFetchError as e:
                failures += 1
                print(f"{entry['id']}: FAILED: {e}", flush=True)
                continue
            print(f"{entry['id']}: {path}", flush=True)
            for note in notes:
                print(f"  note: {note}", flush=True)
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
