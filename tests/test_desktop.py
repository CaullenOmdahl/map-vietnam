"""scripts/desktop.py: the window's decisions (which links leave the app, which are exports, what a
saved file is called), the command-line dispatch, serving without a window, and the fallback to the
browser when no window can be opened. None of it needs a GUI toolkit."""

import json
import sys
import threading
import urllib.request

import desktop
import paths
import pytest

OWN = "http://127.0.0.1:8765/app/"


# ---- pure decisions ---------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url, external",
    [
        ("https://example.com/record/1", True),
        ("http://example.com", True),
        ("http://127.0.0.1:9999/app/", True),  # another port is another site
        ("http://localhost:8765/app/", True),  # another host name, not our origin
        ("http://127.0.0.1:8765/api/search?q=x", False),
        ("/app/", False),
        ("javascript:alert(1)", False),
        ("mailto:a@b.c", False),
        ("blob:http://127.0.0.1:8765/abc", False),
        ("", False),
        ("http://[bad", False),
    ],
)
def test_only_other_websites_count_as_external(url, external):
    assert desktop.is_external(url, OWN) is external


def test_default_ports_are_filled_in_when_comparing_origins():
    assert desktop.origin_of("http://Example.com:80/x") == desktop.origin_of("http://example.com")
    assert desktop.origin_of("https://example.com") == ("https", "example.com", 443)


@pytest.mark.parametrize(
    "url, fmt",
    [
        ("http://127.0.0.1:8765/api/cases/abc123/export.gpx", "gpx"),
        ("http://127.0.0.1:8765/api/cases/abc123/export.kml", "kml"),
        ("http://127.0.0.1:8765/api/cases/7/export.geojson", "geojson"),
        ("http://127.0.0.1:8765/api/cases/abc/export.gpx?x=1", "gpx"),
        ("http://127.0.0.1:8765/api/cases/abc/export.exe", None),
        ("http://127.0.0.1:8765/api/cases/abc/items", None),
        ("http://127.0.0.1:8765/api/cases/a/b/export.gpx", None),
        ("http://127.0.0.1:8765/other/export.gpx", None),
        ("https://example.com/api/cases/abc/export.gpx", None),  # someone else's file is not ours to fetch
        ("http://127.0.0.1:1/api/cases/abc/export.gpx", None),
    ],
)
def test_export_links_are_recognised_only_on_our_own_server(url, fmt):
    assert desktop.export_format(url, OWN) == fmt


@pytest.mark.parametrize(
    "name, fmt, expected",
    [
        ("Cao Lãnh 1965", "gpx", "Cao Lãnh 1965.gpx"),
        ("Cao Lãnh.gpx", "gpx", "Cao Lãnh.gpx"),
        ("a/b\\c:d", "kml", "a_b_c_d.kml"),
        ('what? "now"', "kml", "what_ _now_.kml"),
        ("", "geojson", "map-vietnam-case.geojson"),
        (None, "geojson", "map-vietnam-case.geojson"),
        ("..", "gpx", "map-vietnam-case.gpx"),
    ],
)
def test_a_saved_export_gets_a_safe_name(name, fmt, expected):
    assert desktop.export_filename(name, fmt) == expected


# ---- what the page may ask for ----------------------------------------------------------------------
def test_open_external_only_opens_other_websites():
    opened = []
    api = desktop.WindowApi(OWN, opener=opened.append)
    assert api.open_external("https://example.com/a") is True
    assert api.open_external("file:///etc/passwd") is False
    assert api.open_external("http://127.0.0.1:8765/app/") is False
    assert api.open_external(None) is False
    assert opened == ["https://example.com/a"]


def test_save_export_fetches_from_the_local_server_and_writes_where_asked(tmp_path, monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"<gpx/>"

    seen = []

    class Opener:
        def open(self, url, timeout=None):
            seen.append(url)
            return Reply()

    monkeypatch.setattr(desktop.urllib.request, "build_opener", lambda *handlers: Opener())
    asked = []

    def dialog(filename):
        asked.append(filename)
        return str(tmp_path / filename)

    api = desktop.WindowApi(OWN, save_dialog=dialog)
    url = "http://127.0.0.1:8765/api/cases/abc/export.gpx"
    result = api.save_export(url, "My case.gpx")
    assert result == {"saved": str(tmp_path / "My case.gpx")}
    assert (tmp_path / "My case.gpx").read_bytes() == b"<gpx/>"
    assert asked == ["My case.gpx"] and seen == [url]


def test_save_export_says_so_when_cancelled_or_refused(tmp_path):
    api = desktop.WindowApi(OWN, save_dialog=lambda name: None)
    assert api.save_export("https://example.com/api/cases/a/export.gpx", "x") == {
        "error": "That is not an export from this map."
    }
    # Nothing is listening on this port, so the fetch fails in plain words; no dialog is shown.
    api = desktop.WindowApi("http://127.0.0.1:1/app/", save_dialog=lambda name: pytest.fail("no dialog"))
    assert "Could not make the export" in api.save_export("http://127.0.0.1:1/api/cases/a/export.gpx", "x")["error"]


def test_save_export_cancelled_dialog_writes_nothing(tmp_path, monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"x"

    monkeypatch.setattr(
        desktop.urllib.request, "build_opener", lambda *h: type("O", (), {"open": lambda s, u, timeout=None: Reply()})()
    )
    api = desktop.WindowApi(OWN, save_dialog=lambda name: None)
    assert api.save_export("http://127.0.0.1:8765/api/cases/a/export.kml", "n") == {"cancelled": True}


def test_save_export_reports_a_file_that_cannot_be_written(tmp_path, monkeypatch):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"x"

    monkeypatch.setattr(
        desktop.urllib.request, "build_opener", lambda *h: type("O", (), {"open": lambda s, u, timeout=None: Reply()})()
    )
    api = desktop.WindowApi(OWN, save_dialog=lambda name: str(tmp_path / "missing-folder" / name))
    result = api.save_export("http://127.0.0.1:8765/api/cases/a/export.kml", "n")
    assert "Could not save the file there" in result["error"]


def test_the_injected_script_covers_exports_and_outside_links():
    """The shim is JavaScript we cannot run here; pin the pieces its behaviour rests on."""
    assert "save_export" in desktop.SHIM and "open_external" in desktop.SHIM
    assert "export\\.(gpx|kml|geojson)" in desktop.SHIM
    assert "window.open" in desktop.SHIM


# ---- dispatch and serving ---------------------------------------------------------------------------
def test_run_script_flag_is_dispatched_before_anything_heavy(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop.paths, "run_script_from_argv", lambda argv: calls.append(argv) or 7)
    monkeypatch.setattr(desktop, "run", lambda args: pytest.fail("no window or server for a child script"))
    assert desktop.main([paths.RUN_SCRIPT_FLAG, "records.py", "list"]) == 7
    assert calls == [[paths.RUN_SCRIPT_FLAG, "records.py", "list"]]


def test_importing_desktop_does_not_import_the_server():
    import subprocess

    code = "import sys, desktop; sys.exit(int('server' in sys.modules or 'webview' in sys.modules))"
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=paths.APP_ROOT,
        env={"PYTHONPATH": str(paths.SCRIPTS), "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_arguments():
    assert desktop.parse_args([]).port == 0 and desktop.parse_args([]).no_window is False
    args = desktop.parse_args(["--no-window", "--port", "9000"])
    assert args.no_window and args.port == 9000
    with pytest.raises(SystemExit):
        desktop.parse_args(["--port", "70000"])


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Keep what a server writes (the map catalogue) out of the checkout."""
    import library
    import server
    import store

    root = tmp_path / "home"
    (root / "web").mkdir(parents=True)
    lib = library.Library(root=root)
    real = server.make_server
    monkeypatch.setattr(library, "Library", lambda: lib)
    monkeypatch.setattr(
        server, "make_server", lambda port, map_library=None: real(port, tmp_path / "evidence.sqlite", map_library)
    )
    assert store  # the server module's own import of it is what makes the database
    return root


def get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=10) as reply:
        return reply.status, reply.read()


def test_no_window_serves_the_viewer_prints_the_address_and_quits_cleanly(home, capsys):
    done = []
    thread = threading.Thread(target=lambda: done.append(desktop.main(["--no-window"])))
    thread.start()
    url = None
    for _ in range(100):
        out = capsys.readouterr().out.strip()
        if out:
            url = out.splitlines()[-1]
            break
        thread.join(0.1)
    assert url and url.startswith("http://127.0.0.1:") and url.endswith("/app/")
    status, body = get(url)
    assert status == 200 and b"Map Vietnam" in body
    request = urllib.request.Request(
        url.replace("/app/", "/api/quit"), data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    assert json.load(opener.open(request, timeout=10)) == {"quitting": True}
    thread.join(30)
    assert not thread.is_alive() and done == [0]
    with pytest.raises(OSError):
        get(url)  # the server is gone


# ---- the window, and falling back --------------------------------------------------------------------
class FakeServer:
    server_address = ("127.0.0.1", 4321)

    def __init__(self):
        self.stopped = False

    def quit(self):
        self.stopped = True


def patch_server(monkeypatch, alive_for=0):
    """Replace the real server with a stub whose thread ends when quit() is called."""
    srv = FakeServer()
    ended = threading.Event()
    thread = threading.Thread(target=ended.wait, daemon=True)
    thread.start()
    srv.quit = lambda: (setattr(srv, "stopped", True), ended.set())[0]
    monkeypatch.setattr(desktop, "start_server", lambda port: (srv, thread))
    monkeypatch.setattr(desktop, "stop_on_signals", lambda s: None)
    return srv, thread


def test_a_window_that_cannot_open_falls_back_to_the_browser(monkeypatch, capsys):
    srv, thread = patch_server(monkeypatch)
    opened = []

    def no_window(url, s, t):
        raise RuntimeError("You must have either QT or GTK with Python extensions installed")

    monkeypatch.setattr(desktop, "open_window", no_window)
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url) or True)
    # With the server's thread alive, the fallback waits for it: end it as "Quit" in the viewer would.
    threading.Timer(0.5, srv.quit).start()
    assert desktop.main([]) == 0
    assert opened == ["http://127.0.0.1:4321/app/"]
    out = capsys.readouterr().out
    assert "could not open its own window" in out and "web browser" in out
    assert srv.stopped


def test_a_missing_webview_module_falls_back_too(monkeypatch, capsys):
    srv, thread = patch_server(monkeypatch)
    monkeypatch.setitem(sys.modules, "webview", None)  # `import webview` raises ImportError
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url) or True)
    threading.Timer(0.5, srv.quit).start()
    assert desktop.main([]) == 0
    assert opened == ["http://127.0.0.1:4321/app/"]


def test_closing_the_window_stops_the_server(monkeypatch):
    srv, thread = patch_server(monkeypatch)
    monkeypatch.setattr(desktop, "open_window", lambda url, s, t: True)  # shown, then closed at once
    assert desktop.main([]) == 0
    assert srv.stopped and not thread.is_alive()


def test_the_server_stopping_closes_the_window(monkeypatch):
    """Quit in the viewer ends the server thread; the window is destroyed from there."""
    pytest.importorskip("webview")
    import webview

    srv, thread = patch_server(monkeypatch)
    destroyed = threading.Event()

    class Window:
        def __init__(self):
            self.events = type("E", (), {"loaded": type("L", (), {"__iadd__": lambda s, f: s})()})()

        def destroy(self):
            destroyed.set()

    window = Window()
    monkeypatch.setattr(webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(webview, "start", lambda *a, **k: destroyed.wait(10))
    threading.Timer(0.3, srv.quit).start()
    assert desktop.open_window("http://127.0.0.1:4321/app/", srv, thread) is True
    assert destroyed.is_set()


def test_window_is_titled_and_sized_for_phones_and_desktops(monkeypatch):
    pytest.importorskip("webview")
    import webview

    srv, thread = patch_server(monkeypatch)
    seen = {}

    class Window:
        events = type("E", (), {"loaded": type("L", (), {"__iadd__": lambda s, f: s})()})()

        def destroy(self):
            pass

    def create(title, url, **kw):
        seen.update(title=title, url=url, **kw)
        return Window()

    monkeypatch.setattr(webview, "create_window", create)
    monkeypatch.setattr(webview, "start", lambda *a, **k: srv.quit())
    desktop.open_window("http://127.0.0.1:4321/app/", srv, thread)
    assert seen["title"] == "Map Vietnam" and seen["url"] == "http://127.0.0.1:4321/app/"
    assert (seen["width"], seen["height"]) == desktop.WINDOW_SIZE
    assert seen["min_size"][0] <= 400 and isinstance(seen["js_api"], desktop.WindowApi)
