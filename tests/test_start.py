"""start.py: it opens the map without asking or downloading anything, and hands setup, maps and
records commands to the project's own command lines in .venv's Python."""

import socket
import subprocess
import sys
from pathlib import Path

import pytest
import records

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import start  # noqa: E402


@pytest.fixture
def ran(monkeypatch):
    """Every child process start.py starts, as (command, environment), instead of running it;
    each "exits" with status 0."""
    calls = []

    class Proc:
        returncode = 0

        def __init__(self, cmd, **kw):
            calls.append((cmd, kw.get("env")))

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("/venv/python"))
    monkeypatch.setattr(start.subprocess, "Popen", Proc)
    monkeypatch.setattr(start, "TILES", Path("/nonexistent/tiles"))
    return calls


def test_start_py_needs_nothing_outside_the_standard_library():
    """It runs before .venv exists, so importing it (and the records module it reads the source
    names from) must not pull in any add-on."""
    code = (
        "import sys, start; "
        "heavy = {'numpy', 'PIL', 'rasterio', 'pyproj', 'morecantile', 'pypdf', 'pypdfium2', 'certifi'}; "
        "sys.exit(', '.join(sorted(heavy & set(sys.modules))) or 0)"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=start.ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_with_no_command_it_opens_the_map_and_asks_and_downloads_nothing(ran, monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(start, "free_port", lambda port: port)
    monkeypatch.setattr(start, "wait_until_up", lambda url, proc: True)
    monkeypatch.setattr(start.webbrowser, "open", opened.append)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("start.py must not ask questions"))

    assert start.main([]) == 0
    assert [cmd for cmd, _ in ran] == [["/venv/python", "scripts/server.py", "--port", str(start.DEFAULT_PORT)]]
    assert opened == [f"http://127.0.0.1:{start.DEFAULT_PORT}/app/"]
    out = capsys.readouterr().out
    assert "The map is ready at" in out and '"Quit Map Vietnam"' in out
    # The server stopping by itself is how "Quit Map Vietnam" in the viewer ends this script: status 0.
    assert "Map Vietnam has stopped" in out

    ran.clear()
    assert start.main(["serve", "--no-browser", "--port", "9000"]) == 0
    assert ran[0][0][-1] == "9000" and len(opened) == 1


def test_a_server_that_fails_makes_the_exit_status_1(ran, monkeypatch, capsys):
    monkeypatch.setattr(start, "free_port", lambda port: port)
    monkeypatch.setattr(start, "wait_until_up", lambda url, proc: True)
    monkeypatch.setattr(start.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(start.subprocess.Popen, "returncode", 1)
    assert start.main([]) == 1
    assert "stopped unexpectedly" in capsys.readouterr().out


def test_setup_options_without_setup_are_explained_not_obeyed(ran, monkeypatch, capsys):
    monkeypatch.setattr(start, "free_port", lambda port: port)
    monkeypatch.setattr(start, "wait_until_up", lambda url, proc: True)
    assert start.main(["--yes", "--no-browser"]) == 0
    assert "go with 'python3 start.py setup'" in capsys.readouterr().out
    assert [cmd[1] for cmd, _ in ran] == ["scripts/server.py"]  # the map opens; nothing is set up


def test_setup_goes_through_the_records_module_the_viewer_uses(ran):
    assert start.main(["setup"]) == 0
    assert start.main(["setup", "--yes", "--only", "thor,gns", "--skip", "vhpa", "--area", "106,16,107,17"]) == 0
    assert start.main(["setup", "--only", "gns", "--only", "caccf", "--refresh"]) == 0
    assert [cmd for cmd, _ in ran] == [
        ["/venv/python", "scripts/records.py", "setup"],
        [
            "/venv/python",
            "scripts/records.py",
            "setup",
            "--only",
            "thor,gns",
            "--skip",
            "vhpa",
            "--area",
            "106,16,107,17",
            "--yes",
        ],
        ["/venv/python", "scripts/records.py", "setup", "--only", "gns,caccf", "--refresh"],
    ]
    assert str(start.ROOT / "scripts") in ran[0][1]["PYTHONPATH"]
    # What start.py builds is exactly what the records command line accepts.
    for cmd, _ in ran:
        records.parse_args(cmd[2:])


def test_unknown_parts_are_refused_and_the_help_lists_the_real_ones(capsys):
    with pytest.raises(SystemExit):
        start.parse_args(["setup", "--only", "nope"])
    assert "unknown part nope" in capsys.readouterr().err
    for source in records.SOURCES:
        assert f"  {source.id:14s}{source.title}" in start.EPILOG


def test_maps_and_records_commands_are_handed_to_their_own_command_lines(ran):
    assert start.main(["maps", "add", "L701461292", "--yes"]) == 0
    assert start.main(["maps"]) == 0
    assert start.main(["records", "remove", "vhpa", "--cascade"]) == 0
    assert start.main(["records"]) == 0
    assert [cmd for cmd, _ in ran] == [
        ["/venv/python", "scripts/library.py", "add", "L701461292", "--yes"],
        ["/venv/python", "scripts/library.py", "--help"],
        ["/venv/python", "scripts/records.py", "remove", "vhpa", "--cascade"],
        ["/venv/python", "scripts/records.py", "--help"],
    ]


def test_old_tile_folders_are_converted_by_setup_only_and_only_if_present(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(start, "TILES", tmp_path / "tiles")
    monkeypatch.setattr(
        start.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or start.subprocess.CompletedProcess(cmd, 0)
    )
    start.convert_old_maps(Path("py"))
    assert calls == []
    (tmp_path / "tiles" / "L701461292").mkdir(parents=True)
    (tmp_path / "tiles" / "L701461292.mbtiles").write_bytes(b"")  # a file is a converted map, not a folder
    start.convert_old_maps(Path("py"))
    assert calls == [["py", "scripts/library.py", "migrate"]]
    assert "Converting 1 old map" in capsys.readouterr().out


def test_opening_the_map_does_not_convert_old_maps(ran, tmp_path, monkeypatch):
    """The viewer offers that as a button (the listing's "legacy"); starting must not do it unasked."""
    monkeypatch.setattr(start, "TILES", tmp_path / "tiles")
    (tmp_path / "tiles" / "L701461292").mkdir(parents=True)
    monkeypatch.setattr(start, "free_port", lambda port: port)
    monkeypatch.setattr(start, "wait_until_up", lambda url, proc: True)
    monkeypatch.setattr(start.subprocess, "run", lambda *a, **k: pytest.fail("nothing is converted on start"))
    assert start.main(["--no-browser"]) == 0


def test_free_port_skips_a_port_in_use():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        assert start.free_port(taken) != taken


def test_requirements_are_not_reinstalled_when_unchanged(tmp_path, monkeypatch):
    venv = tmp_path / ".venv"
    monkeypatch.setattr(start, "VENV", venv)
    monkeypatch.setattr(start, "REQUIREMENTS_STAMP", venv / "requirements.sha256")
    venv.mkdir()
    (venv / "requirements.sha256").write_text(start.hashlib.sha256(start.REQUIREMENTS.read_bytes()).hexdigest())
    monkeypatch.setattr(start, "runs", lambda cmd: True)

    def no_install(*a, **k):
        raise AssertionError("pip install should not run")

    monkeypatch.setattr(start.subprocess, "run", no_install)
    assert start.ensure_venv() == start.venv_python()


def test_a_port_whose_last_connections_are_still_closing_is_reused():
    # A server that served a request and stopped leaves its port in TIME_WAIT for a while.
    with socket.socket() as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.listen()
        client = socket.create_connection(("127.0.0.1", port))
        conn, _ = srv.accept()
        conn.close()  # the server side closes first, so the server's port is the one left waiting
        client.close()
    assert start.free_port(port) == port
