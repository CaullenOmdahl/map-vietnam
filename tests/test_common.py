"""common: .env loading, the User-Agent, and cached downloads."""

import http.server
import os
import threading

import common
import pytest


def test_load_env_reads_values_but_never_overrides_the_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# a comment\n"
        "MV_TEST_PLAIN=one\n"
        'MV_TEST_QUOTED="two words"\n'
        "export MV_TEST_EXPORTED=three\n"
        "MV_TEST_EMPTY=\n"
        "MV_TEST_SET=from-file\n"
        "not a setting\n",
        encoding="utf-8",
    )
    # A private copy of the environment: load_env writes to os.environ directly.
    monkeypatch.setattr(os, "environ", {"MV_TEST_SET": "from-env"})
    common.load_env(env)
    assert os.environ["MV_TEST_PLAIN"] == "one"
    assert os.environ["MV_TEST_QUOTED"] == "two words"
    assert os.environ["MV_TEST_EXPORTED"] == "three"
    assert "MV_TEST_EMPTY" not in os.environ
    assert os.environ["MV_TEST_SET"] == "from-env"


def test_load_env_without_a_file_does_nothing(tmp_path):
    common.load_env(tmp_path / "missing.env")


def test_user_agent_is_generic_and_appends_the_optional_contact(monkeypatch):
    monkeypatch.delenv("MAPVIETNAM_CONTACT", raising=False)
    assert common.user_agent() == "MapVietnam/0.1 (historical map research; low-volume)"
    assert "@" not in common.BASE_USER_AGENT
    monkeypatch.setenv("MAPVIETNAM_CONTACT", "someone@example.org")
    assert common.user_agent() == "MapVietnam/0.1 (historical map research; low-volume; someone@example.org)"


@pytest.fixture
def http_server():
    """A local server that answers GET /file with a fixed body and records the User-Agent."""
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers["User-Agent"])
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_cached_get_downloads_once_with_the_user_agent(tmp_path, monkeypatch, http_server):
    base, seen = http_server
    monkeypatch.setattr(common, "RAW", tmp_path)
    path = common.cached_get(f"{base}/file", "src/sub", name="f.txt", min_interval=0)
    assert path == tmp_path / "src" / "sub" / "f.txt" and path.read_bytes() == b"hello"
    assert not list(tmp_path.rglob("*.part"))
    assert common.cached_get(f"{base}/file", "src/sub", name="f.txt", min_interval=0) == path
    assert seen == [common.USER_AGENT]


def test_in_bbox_includes_edges():
    w, s, e, n = common.AREA_OF_INTEREST
    assert common.in_bbox(w, s) and common.in_bbox(e, n)
    assert not common.in_bbox(w - 0.01, s)


def test_progress_is_silent_unless_asked_for(monkeypatch, capsys):
    monkeypatch.delenv(common.PROGRESS_ENV, raising=False)
    common.progress(0.5, "Downloading")
    assert capsys.readouterr().out == ""  # run by hand, an ingester prints what it always did
    monkeypatch.setenv(common.PROGRESS_ENV, "1")
    common.progress(0.42, "Downloading 134 MB of 320 MB")
    common.progress(None, "Saving 26,551 air strikes")
    common.progress(7, "past the end")
    common.progress(0.1)
    assert capsys.readouterr().out.splitlines() == [
        "progress 0.4200 Downloading 134 MB of 320 MB",
        "progress - Saving 26,551 air strikes",
        "progress 1.0000 past the end",
        "progress 0.1000",
    ]


def test_megabytes_reads_like_a_file_size():
    assert common.megabytes(7_600_000) == "7.6 MB" and common.megabytes(134_000_000) == "134 MB"


@pytest.fixture
def big_file_server():
    """A local server whose /big is three download blocks and a bit, and whose /unsized is the
    same body sent without a Content-Length."""
    body = b"x" * (3 * common.DOWNLOAD_BLOCK + 1000)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/big":
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", len(body)
    srv.shutdown()


def test_a_big_download_reports_its_progress_by_bytes(tmp_path, monkeypatch, capsys, http_server, big_file_server):
    base, size = big_file_server
    monkeypatch.setattr(common, "RAW", tmp_path)
    monkeypatch.setenv(common.PROGRESS_ENV, "1")
    path = common.cached_get(f"{base}/big", "src", name="big.bin", min_interval=0)
    lines = capsys.readouterr().out.splitlines()
    assert path.stat().st_size == size and len(lines) >= 2
    fractions = [float(line.split()[1]) for line in lines]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert lines[-1] == "progress 1.0000 Downloading 3.1 MB of 3.1 MB"
    assert all(line.startswith("progress ") and "Downloading" in line and line.endswith("of 3.1 MB") for line in lines)

    common.cached_get(f"{base}/unsized", "src", name="unsized.bin", min_interval=0)  # no length: no fraction
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(line.startswith("progress - Downloading ") for line in lines)

    common.cached_get(f"{http_server[0]}/file", "src", name="small.txt", min_interval=0)  # one block: not worth a line
    common.cached_get(f"{base}/big", "src", name="big.bin", min_interval=0)  # cached: nothing fetched
    assert capsys.readouterr().out == ""
