"""A fake map archive for the tests: a local HTTP server, plus a map library rooted in a temporary
folder whose catalogue (tests/fixtures/library/map_library.json) points at that server.

No test touches the network: the catalogue's https://archive.example addresses are rewritten to
the fake server, which the library is allowed to fetch from over plain http for the test only.
"""

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import download_maps
import library
import numpy as np
from geopdf import write_geopdf
from PIL import Image

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "library" / "map_library.json"
ARCHIVE = "https://archive.example"


class FakeArchive:
    """Serves `files` {path: bytes}; `status` {path: HTTP status} answers errors instead;
    `slow` {path: seconds per 64 KiB block} drips a file out so a test can stop it midway.
    `requests` records every path asked for."""

    def __init__(self):
        self.files, self.status, self.slow, self.requests = {}, {}, {}, []
        archive = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                archive.requests.append(self.path)
                if self.path in archive.status:
                    self.send_error(archive.status[self.path])
                    return
                body = archive.files.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                delay = archive.slow.get(self.path)
                for i in range(0, len(body), 1 << 16):
                    if delay:
                        time.sleep(delay)
                    try:
                        self.wfile.write(body[i : i + (1 << 16)])
                    except (BrokenPipeError, ConnectionResetError):
                        return  # the client stopped reading: that is what cancelling does

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()


def sheet_pdf(tmp, name, **kw):
    """Bytes of a small synthetic GeoPDF (see geopdf.write_geopdf)."""
    return write_geopdf(tmp / name, **kw).read_bytes()


def scan_jpeg(tmp, size=(600, 400)):
    """Bytes of a plain grey JPEG of `size` pixels."""
    path = tmp / "scan.jpg"
    Image.fromarray(np.full((size[1], size[0], 3), 128, np.uint8)).save(path)
    return path.read_bytes()


def make_library(root, archive, monkeypatch, *, fill=True):
    """A Library under `root` with the fixture catalogue, its URLs pointing at `archive`.

    With fill=True the archive serves a file for every map (each at its catalogue's second URL
    too) and the catalogue records their true size and SHA-256; the scan gets its control file.
    """
    monkeypatch.setattr(download_maps, "ALLOWED_URL_PREFIXES", ("https://", archive.base))
    monkeypatch.setattr(download_maps, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(download_maps, "MIN_INTERVAL_S", 0)
    cat = json.loads(FIXTURE.read_text())
    tmp = root / "src"
    tmp.mkdir(parents=True, exist_ok=True)
    bodies = {
        "L7014A": sheet_pdf(tmp, "a.pdf", info={"pri_date": "1969-01-01", "edition": "002"}),
        "L7014B": sheet_pdf(tmp, "b.pdf", origin=(560_000, 1_160_000)),
        "JOG_NF4811": sheet_pdf(tmp, "j.pdf", origin=(500_000, 2_330_000), m_per_pt=100.0, datum="WGE"),
        "scan_test": scan_jpeg(tmp),
    }
    for entry in cat["maps"]:
        entry["urls"] = [u.replace(ARCHIVE, archive.base) for u in entry["urls"]]
        if fill:
            body = bodies[entry["id"]]
            entry["bytes"], entry["sha256"] = len(body), hashlib.sha256(body).hexdigest()
            for url in entry["urls"]:
                archive.files[url.removeprefix(archive.base)] = body
    path = root / "data" / "sources" / "map_library.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cat, ensure_ascii=False))
    gcps = [[c, r, 105.0 + c / 1000, 10.4 - r / 1000] for c in (0, 300, 600) for r in (0, 200, 400)]
    control = {"image": "data/raw/scans/scan_test/work.jpg", "crs": "EPSG:4326", "gcps": gcps, "max_zoom": 9}
    control["meta"] = {"title": "A test scan", "date": "1909", "dateNote": "publication"}
    (root / "data" / "scans").mkdir(parents=True, exist_ok=True)
    (root / "data" / "scans" / "scan_test.json").write_text(json.dumps(control))
    return library.Library(root)


def path_of(lib, map_id, which=0):
    """The fake archive path of a map's `which`-th URL."""
    url = lib.entries()[map_id]["urls"][which]
    return "/" + url.split("/", 3)[3]
