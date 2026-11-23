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
    assert (
        common.user_agent()
        == "MapVietnam/0.1 (+https://github.com/CaullenOmdahl/map-vietnam; historical map research; low-volume)"
    )
    assert "@" not in common.BASE_USER_AGENT
    monkeypatch.setenv("MAPVIETNAM_CONTACT", "someone@example.org")
    assert (
        common.user_agent()
        == "MapVietnam/0.1 (+https://github.com/CaullenOmdahl/map-vietnam; historical map research; low-volume; someone@example.org)"
    )


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
