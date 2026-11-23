"""download_maps: files are fetched only from https catalogue URLs, checked, kept under data/raw, and a
scan's working image is rebuilt on exactly the pixel grid its control points need."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import download_maps as dm
import numpy as np
import pytest
from fakeweb import FakeArchive
from PIL import Image, JpegImagePlugin

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


@pytest.fixture
def archive(monkeypatch):
    a = FakeArchive()
    monkeypatch.setattr(dm, "ALLOWED_URL_PREFIXES", ("https://", a.base))
    monkeypatch.setattr(dm, "RETRY_WAIT_S", 0)
    monkeypatch.setattr(dm, "MIN_INTERVAL_S", 0)
    yield a
    a.close()


def gradient(w, h):
    x = np.linspace(0, 255, w)[None, :, None]
    y = np.linspace(0, 255, h)[:, None, None]
    return np.broadcast_to(np.concatenate([x + 0 * y, y + 0 * x, (x + y) / 2], axis=2), (h, w, 3)).astype(np.uint8)


def test_raw_paths_keep_the_layout_older_versions_used():
    sheet = {"id": "L701461292", "series": "l7014", "kind": "geopdf"}
    assert dm.raw_paths(sheet)[0] == dm.RAW / "l7014" / "L701461292_geo.pdf"
    scan = {"id": "koch_1889_f2", "series": "scans", "kind": "scan"}
    assert dm.raw_paths(scan) == (dm.RAW / "scans" / "koch_1889_f2" / "work.jpg", [dm.RAW / "scans" / "koch_1889_f2"])


def test_scan_control_files_read_the_image_the_library_downloads():
    root = dm.ROOT
    for control in (root / "data" / "scans").glob("*.json"):
        image = json.loads(control.read_text(encoding="utf-8"))["image"]
        entry = {"id": control.stem, "series": "scans", "kind": "scan"}
        assert root / image == dm.raw_paths(entry)[0]


def test_jp2_scan_is_reduced_resized_and_saved_444(tmp_path):
    src = tmp_path / "src.jp2"
    Image.fromarray(gradient(1001, 601)).save(src, format="JPEG2000", irreversible=False)
    dst = tmp_path / "work.jpg"
    dm.derive_work_image(src, dst, {"jp2_reduce": 1, "width": 300, "jpeg_quality": 90})
    out = Image.open(dst)
    # Reduce 1 halves (rounding up) to 501 x 301; then 300 wide keeps the aspect: 301 * 300 / 501 = 180.2.
    assert out.size == (300, 180)
    assert JpegImagePlugin.get_sampling(out) == 0  # 4:4:4, no chroma subsampling
    assert not list(tmp_path.glob("*.part"))


def test_full_resolution_jp2_keeps_its_size(tmp_path):
    src = tmp_path / "src.jp2"
    Image.fromarray(gradient(120, 80)).save(src, format="JPEG2000")
    dm.derive_work_image(src, tmp_path / "work.jpg", {"jpeg_quality": 90})
    assert Image.open(tmp_path / "work.jpg").size == (120, 80)


def test_a_derived_scan_keeps_only_its_working_image(tmp_path, archive):
    src = tmp_path / "upload.jp2"
    Image.fromarray(gradient(120, 80)).save(src, format="JPEG2000")
    archive.files["/s.jp2"] = src.read_bytes()
    entry = {
        "id": "s1",
        "series": "scans",
        "kind": "scan",
        "urls": [f"{archive.base}/s.jp2"],
        "source_file": "src.jp2",
        "derive": {"jpeg_quality": 90},
        "work_size": [120, 80],
    }
    path, notes = dm.fetch_map(entry, tmp_path / "raw")
    assert path == tmp_path / "raw" / "scans" / "s1" / "work.jpg" and notes == []
    assert [p.name for p in path.parent.iterdir()] == ["work.jpg"]


def test_a_file_already_downloaded_is_reused_without_the_network(tmp_path, archive):
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": [f"{archive.base}/L1.pdf"]}
    pdf = tmp_path / "l7014" / "L1_geo.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.7 already here\n%%EOF\n")
    assert dm.fetch_map(entry, tmp_path) == (pdf, [])
    assert archive.requests == []


def test_progress_is_reported_in_bytes(tmp_path, archive):
    archive.files["/big.pdf"] = b"%PDF-" + b"x" * (3 << 20) + b"%%EOF"
    entry = {"id": "big", "series": "l7014", "kind": "geopdf", "urls": [f"{archive.base}/big.pdf"]}
    seen = []
    dm.fetch_map(entry, tmp_path, progress=lambda done, total: seen.append((done, total)))
    assert seen[-1] == ((3 << 20) + 10, (3 << 20) + 10) and len(seen) >= 3


def test_a_body_shorter_than_announced_is_not_kept(tmp_path, monkeypatch):
    class Short(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"%PDF-only part of it")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Short)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(dm, "ALLOWED_URL_PREFIXES", (base,))
    monkeypatch.setattr(dm, "RETRY_WAIT_S", 0)
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": [f"{base}/L1.pdf"]}
    try:
        with pytest.raises(dm.MapFetchError, match="Could not download it"):
            dm.fetch_map(entry, tmp_path)
    finally:
        srv.shutdown()
    assert not list(tmp_path.rglob("*.pdf*"))


def test_cancelling_stops_the_download_and_removes_the_part_file(tmp_path, archive):
    archive.files["/c.pdf"] = b"%PDF-" + b"x" * (3 << 20) + b"%%EOF"
    entry = {"id": "c", "series": "l7014", "kind": "geopdf", "urls": [f"{archive.base}/c.pdf"]}
    with pytest.raises(dm.Cancelled):
        dm.fetch_map(entry, tmp_path, cancelled=lambda: True)
    assert not list(tmp_path.rglob("*.pdf*"))


def test_requests_to_one_host_are_spaced_out(monkeypatch):
    clock = {"now": 100.0}
    slept = []
    monkeypatch.setattr(dm.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(dm.time, "sleep", slept.append)
    monkeypatch.setattr(dm, "_last_request", {})
    dm._wait_turn("archive.example")
    dm._wait_turn("archive.example")
    dm._wait_turn("other.example")
    assert slept == [pytest.approx(dm.MIN_INTERVAL_S)]


def serve(handler_class):
    """A local server answering with handler_class; returns (server, base URL)."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_a_damaged_copy_moves_on_to_the_mirror_at_once(tmp_path, archive):
    # As on the USGS Store: the start of the PDF, then zero bytes up to the full size.
    archive.files["/a/L1.pdf"] = b"%PDF-1.6 start of it" + bytes(5000)
    archive.files["/b/L1.pdf"] = b"%PDF-1.6 all of it\n%%EOF\n"
    urls = [f"{archive.base}/a/L1.pdf", f"{archive.base}/b/L1.pdf"]
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": urls}
    path, _ = dm.fetch_map(entry, tmp_path)
    assert path.read_bytes().endswith(b"%%EOF\n")
    assert archive.requests == ["/a/L1.pdf", "/b/L1.pdf"]  # the damaged copy was not asked for again
    # With no good copy anywhere, the reason says so, and nothing is kept.
    path.unlink()
    archive.files["/b/L1.pdf"] = archive.files["/a/L1.pdf"]
    with pytest.raises(dm.MapFetchError, match="damaged"):
        dm.fetch_map(entry, tmp_path)
    assert not list(tmp_path.rglob("*.pdf*"))


def test_an_unusable_earlier_download_is_deleted_so_trying_again_fetches_it_afresh(tmp_path, archive):
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": [f"{archive.base}/L1.pdf"]}
    pdf = tmp_path / "l7014" / "L1_geo.pdf"
    pdf.parent.mkdir()
    pdf.write_bytes(b"%PDF-1.7 cut")
    with pytest.raises(dm.MapFetchError, match="Try again"):
        dm.fetch_map(entry, tmp_path)
    assert not pdf.exists() and archive.requests == []


def test_without_a_length_from_the_server_a_body_shorter_than_the_catalogue_says_is_not_kept(tmp_path, monkeypatch):
    class NoLength(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # the body ends when the connection closes

        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"%PDF-cut off\n%%EOF\n")

    srv, base = serve(NoLength)
    monkeypatch.setattr(dm, "ALLOWED_URL_PREFIXES", (base,))
    monkeypatch.setattr(dm, "RETRY_WAIT_S", 0)
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": [f"{base}/L1.pdf"], "bytes": 5000}
    try:
        with pytest.raises(dm.MapFetchError, match="stopped early"):
            dm.fetch_map(entry, tmp_path)
        entry["bytes"] = 19  # the size the catalogue knows: then the same body is whole
        assert dm.fetch_map(entry, tmp_path)[0].exists()
    finally:
        srv.shutdown()


def test_a_redirect_to_plain_http_is_refused(tmp_path, monkeypatch, archive):
    archive.files["/L1.pdf"] = b"%PDF-1.7\n%%EOF\n"

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"{archive.base}/L1.pdf")
            self.end_headers()

    srv, base = serve(Redirect)
    # The redirecting host is allowed; the address it sends us to is not.
    monkeypatch.setattr(dm, "ALLOWED_URL_PREFIXES", (base,))
    entry = {"id": "L1", "series": "l7014", "kind": "geopdf", "urls": [f"{base}/L1.pdf"]}
    try:
        with pytest.raises(dm.MapFetchError, match="not an https address"):
            dm.fetch_map(entry, tmp_path)
    finally:
        srv.shutdown()
    assert archive.requests == [] and not list(tmp_path.rglob("*.pdf*"))
    # An allowed target is followed.
    srv, base2 = serve(Redirect)
    monkeypatch.setattr(dm, "ALLOWED_URL_PREFIXES", (base2, archive.base))
    try:
        entry["urls"] = [f"{base2}/L1.pdf"]
        assert dm.fetch_map(entry, tmp_path)[0].exists()
    finally:
        srv.shutdown()


def test_removing_a_map_stops_a_wait_to_retry_at_once(tmp_path, archive, monkeypatch):
    archive.status["/busy.pdf"] = 503
    monkeypatch.setattr(dm, "RETRY_WAIT_S", 60)
    entry = {"id": "b", "series": "l7014", "kind": "geopdf", "urls": [f"{archive.base}/busy.pdf"]}
    stop = threading.Timer(0.3, lambda: None)
    stop.start()
    t0 = dm.time.monotonic()
    with pytest.raises(dm.Cancelled):
        dm.fetch_map(entry, tmp_path, cancelled=lambda: not stop.is_alive())
    assert dm.time.monotonic() - t0 < 5 and archive.requests == ["/busy.pdf"]


def test_the_derive_step_runs_as_a_command_too(tmp_path):
    src = tmp_path / "src.jp2"
    Image.fromarray(gradient(120, 80)).save(src, format="JPEG2000")
    guard = Image.MAX_IMAGE_PIXELS
    assert dm.main(["--derive", str(src), str(tmp_path / "work.jpg"), json.dumps({"jpeg_quality": 90})]) == 0
    assert Image.open(tmp_path / "work.jpg").size == (120, 80)
    # Opening a huge trusted scan lifts Pillow's guard only for that moment.
    assert Image.MAX_IMAGE_PIXELS == guard


def test_importing_the_downloader_leaves_pillows_guard_alone():
    # The viewer's server imports it; only the builders, in their own processes, lift the guard.
    code = "import download_maps, PIL.Image; print(PIL.Image.MAX_IMAGE_PIXELS)"
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=SCRIPTS, encoding="utf-8"
    )
    assert out.stdout.strip() not in ("", "None")
