"""scripts/paths.py: where the program's files and the person's data are, and how a script is run."""

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import paths
import pytest
import tiling

REPO = Path(__file__).resolve().parent.parent


# ---- where things are ------------------------------------------------------------------------
def test_a_relative_home_is_made_absolute(tmp_path, monkeypatch):
    # Children run in another folder, so a relative value must not stay relative.
    monkeypatch.chdir(tmp_path)
    root = paths.find_data_root(REPO, frozen=False, env={"MAPVIETNAM_HOME": "rel/home"})
    assert root == tmp_path / "rel" / "home"
    assert root.is_absolute()


def test_from_source_the_program_and_its_data_share_the_repository():
    assert paths.find_app_root(frozen=False) == REPO
    assert paths.find_data_root(REPO, frozen=False, env={}) == REPO
    assert paths.DB_PATH == REPO / "data" / "evidence.sqlite"  # where a checkout has always kept it


def test_the_home_variable_moves_the_data_but_not_the_program(tmp_path):
    env = {"MAPVIETNAM_HOME": str(tmp_path / "mine")}
    assert paths.find_data_root(REPO, frozen=False, env=env) == tmp_path / "mine"
    assert paths.find_data_root(Path("/opt/app"), frozen=True, platform="linux", env=env) == tmp_path / "mine"
    assert paths.find_data_root(REPO, frozen=False, env={"MAPVIETNAM_HOME": ""}) == REPO  # empty means unset


def test_a_packaged_app_runs_from_its_bundle_and_writes_to_the_user_folder(tmp_path):
    assert paths.find_app_root(frozen=True, meipass=str(tmp_path / "bundle")) == tmp_path / "bundle"
    home = tmp_path / "home"
    win = {"LOCALAPPDATA": str(tmp_path / "local")}
    assert paths.find_data_root(tmp_path, True, "win32", win, home) == tmp_path / "local" / "Map Vietnam"
    assert paths.find_data_root(tmp_path, True, "win32", {}, home) == home / "AppData" / "Local" / "Map Vietnam"
    mac = home / "Library" / "Application Support" / "Map Vietnam"
    assert paths.find_data_root(tmp_path, True, "darwin", {}, home) == mac
    xdg = {"XDG_DATA_HOME": str(tmp_path / "xdg")}
    assert paths.find_data_root(tmp_path, True, "linux", xdg, home) == tmp_path / "xdg" / "map-vietnam"
    assert paths.find_data_root(tmp_path, True, "linux", {}, home) == home / ".local" / "share" / "map-vietnam"
    relative = {"XDG_DATA_HOME": "relative/dir"}  # the XDG rule: not absolute, so ignored
    assert paths.find_data_root(tmp_path, True, "linux", relative, home) == home / ".local" / "share" / "map-vietnam"


def run_python(code, env_extra):
    """Run `code` in a fresh interpreter with scripts/ importable, so the module-level paths are
    computed under the given environment (a fake packaged app included)."""
    env = {k: v for k, v in os.environ.items() if k != "MAPVIETNAM_HOME"}
    env.update(env_extra, PYTHONPATH=str(REPO / "scripts"), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(done.stdout)


NAMES = "import json, paths; print(json.dumps({k: str(getattr(paths, k)) for k in %r}))"
WRITTEN = ["DATA_DIR", "RAW", "WORK", "DB_PATH", "TILES", "CATALOG_JSON", "ENV_FILE"]
SHIPPED = ["WEB_APP", "SOURCES", "SCANS", "MAPS_JSON", "SCRIPTS"]


def test_named_paths_split_what_is_written_from_what_ships(tmp_path):
    got = run_python(NAMES % (WRITTEN + SHIPPED), {"MAPVIETNAM_HOME": str(tmp_path)})
    assert {k: got[k] for k in WRITTEN} == {
        "DATA_DIR": str(tmp_path / "data"),
        "RAW": str(tmp_path / "data" / "raw"),
        "WORK": str(tmp_path / "data" / "work"),
        "DB_PATH": str(tmp_path / "data" / "evidence.sqlite"),
        "TILES": str(tmp_path / "web" / "tiles"),
        "CATALOG_JSON": str(tmp_path / "web" / "catalog.json"),
        "ENV_FILE": str(tmp_path / ".env"),
    }
    assert {k: got[k] for k in SHIPPED} == {
        "WEB_APP": str(REPO / "web" / "app"),
        "SOURCES": str(REPO / "data" / "sources"),
        "SCANS": str(REPO / "data" / "scans"),
        "MAPS_JSON": str(REPO / "data" / "maps.json"),
        "SCRIPTS": str(REPO / "scripts"),
    }


def test_a_faked_packaged_app_reads_from_the_bundle_and_writes_to_the_user_folder(tmp_path):
    bundle, home = tmp_path / "bundle", tmp_path / "home"
    code = f"import sys; sys.frozen = True; sys._MEIPASS = {str(bundle)!r}\n" + NAMES % (WRITTEN + SHIPPED)
    env = {"HOME": str(home), "XDG_DATA_HOME": "", "LOCALAPPDATA": "", "USERPROFILE": str(home)}
    got = run_python(code, env)
    data = Path(got["DATA_DIR"]).parent
    assert data.name in ("map-vietnam", "Map Vietnam") and home in data.parents
    assert got["WEB_APP"] == str(bundle / "web" / "app") and got["SCANS"] == str(bundle / "data" / "scans")
    assert got["SCRIPTS"] == str(bundle / "scripts") and got["DB_PATH"] == str(data / "data" / "evidence.sqlite")
    assert got["TILES"] == str(data / "web" / "tiles") and got["ENV_FILE"] == str(data / ".env")


# ---- running a project script -----------------------------------------------------------------
def test_from_source_a_script_runs_on_this_interpreter(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert paths.script_command("build_tiles.py", "a.pdf") == [
        sys.executable,
        str(paths.SCRIPTS / "build_tiles.py"),
        "a.pdf",
    ]
    assert paths.script_command("ingest/gns.py")[1].endswith(os.path.join("ingest", "gns.py"))


def test_packaged_a_script_runs_through_the_executable_itself(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert paths.script_command("ingest/gns.py", "--refresh") == [
        sys.executable,
        "--run-script",
        "ingest/gns.py",
        "--refresh",
    ]


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    """A tiny scripts/ folder standing in for the real one."""
    folder = tmp_path / "scripts"
    (folder / "ingest").mkdir(parents=True)
    (folder / "helper.py").write_text("VALUE = 41\n", encoding="utf-8")
    (folder / "ingest" / "echo.py").write_text(
        "import sys, json, helper\n"
        "if __name__ == '__main__':\n"
        "    out = sys.argv[1]\n"
        "    open(out, 'w', encoding='utf-8').write(json.dumps([sys.argv, helper.VALUE + 1, __name__]))\n",
        encoding="utf-8",
    )
    (folder / "quit.py").write_text("import sys\nsys.exit(int(sys.argv[1]))\n", encoding="utf-8")
    (folder / "words.py").write_text("raise SystemExit('could not do it')\n", encoding="utf-8")
    (folder / "plain.py").write_text("pass\n", encoding="utf-8")
    (tmp_path / "outside.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    monkeypatch.setattr(paths, "SCRIPTS", folder)
    return folder


def test_run_script_runs_it_as_main_with_its_own_arguments_and_import_path(scripts, tmp_path):
    out = tmp_path / "out.json"
    argv_before, path_before = list(sys.argv), list(sys.path)
    assert paths.run_script_from_argv(["--run-script", "ingest/echo.py", str(out)]) == 0
    argv, value, name = json.loads(out.read_text(encoding="utf-8"))
    assert argv == [str(scripts / "ingest" / "echo.py"), str(out)] and value == 42 and name == "__main__"
    assert (sys.argv, sys.path) == (argv_before, path_before)  # this process is left as it was
    sys.modules.pop("helper", None)


def test_run_script_returns_the_exit_status(scripts):
    assert paths.run_script_from_argv(["--run-script", "plain.py"]) == 0
    assert paths.run_script_from_argv(["--run-script", "quit.py", "0"]) == 0
    assert paths.run_script_from_argv(["--run-script", "quit.py", "7"]) == 7


def test_run_script_prints_a_message_exit_and_returns_one(scripts, capsys):
    assert paths.run_script_from_argv(["--run-script", "words.py"]) == 1
    assert "could not do it" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [[], ["--run-script"], ["x.py"], ["--run-script", "missing.py"], ["--run-script", "../outside.py"]],
)
def test_run_script_refuses_what_is_not_one_of_the_projects_scripts(scripts, argv, capsys):
    assert paths.run_script_from_argv(argv) == 2
    assert capsys.readouterr().err


# ---- the whole app with its data somewhere else --------------------------------------------------
def snapshot(root):
    """(size, mtime) of every file in the repository that a run could have touched."""
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".claude"}
    found = {}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            p = Path(folder, name)
            st = p.stat()
            found[str(p)] = (st.st_size, st.st_mtime_ns)
    return found


def post_json(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST", headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req)


def test_the_server_keeps_everything_it_writes_in_the_home_folder(tmp_path):
    home = tmp_path / "home"
    (home / "web" / "tiles").mkdir(parents=True)
    with tiling.MBTilesWriter(home / "web" / "tiles" / "M.mbtiles") as w:
        w.put(10, 812, 481, b"RIFF-a-webp-tile")
    before = snapshot(REPO)
    env = {**os.environ, "MAPVIETNAM_HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "scripts" / "server.py"), "--port", "0"],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,  # nothing may depend on the folder it is started from
    )
    try:
        # It prints its address once it listens; --port 0 asks the system for a free port.
        base = proc.stdout.readline().strip()
        assert base.startswith("http://127.0.0.1:")

        def get(path):
            with urllib.request.urlopen(base + path) as r:
                return r.read()

        with post_json(f"{base}/api/cases", {"name": "a case"}) as r:
            assert r.status in (200, 201)
        assert b"app.js" in get("/app/index.html")  # the viewer: the program's own files
        assert [m["id"] for m in json.loads(get("/catalog.json"))["layers"]] == ["M"]  # the home's own maps
        assert get("/tiles/M/10/812/481.webp") == b"RIFF-a-webp-tile"  # a tile from the home's maps
        post_json(f"{base}/api/quit", {}).close()
        assert proc.wait(20) == 0
    finally:
        proc.kill()
        proc.stdout.close()
    assert (home / "data" / "evidence.sqlite").is_file()
    assert (home / "web" / "catalog.json").is_file()
    assert (home / "web" / "tiles" / "M.mbtiles").is_file()
    assert snapshot(REPO) == before  # not a byte written under the repository


def test_a_child_script_works_in_a_relative_home_and_writes_nothing_under_the_repository(tmp_path):
    from PIL import Image

    home = tmp_path / "rel" / "home"
    (home / "data" / "raw").mkdir(parents=True)
    Image.new("RGB", (4, 4), "red").save(home / "data" / "raw" / "in.png")
    code = (
        "import library, paths\n"
        "lib = library.Library()\n"
        "assert paths.DATA_ROOT.is_absolute() and lib.root == paths.DATA_ROOT\n"
        "lib.derive_in_child(lib.raw / 'in.png', lib.raw / 'out.jpg', {'jpeg_quality': 80})\n"
    )
    env = {
        **os.environ,
        "MAPVIETNAM_HOME": "rel/home",  # relative on purpose: the child runs with cwd=home
        "PYTHONPATH": str(REPO / "scripts"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    before = snapshot(REPO)
    done = subprocess.run(
        [sys.executable, "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8"
    )
    assert done.returncode == 0, done.stderr
    assert (home / "data" / "raw" / "out.jpg").is_file()
    assert snapshot(REPO) == before


def test_children_of_a_packaged_app_are_started_through_the_executable(monkeypatch):
    import library
    import records

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = library.Library().build_command({"kind": "geopdf", "series": "none", "id": "X"}, Path("a.pdf"), Path("o"))
    assert cmd[:3] == [sys.executable, "--run-script", "build_tiles.py"]
    rec = records.Records()
    job = type("Job", (), {"source": rec.by_id["gns"], "refresh": False, "options": {}})()
    assert rec.command(job)[:3] == [sys.executable, "--run-script", "ingest/gns.py"]


def test_an_injected_interpreter_still_wins(tmp_path):
    import library

    lib = library.Library(tmp_path, python="fake-python")
    cmd = lib.build_command({"kind": "geopdf", "series": "none", "id": "X"}, Path("a.pdf"), Path("o"))
    assert cmd[0] == "fake-python" and cmd[1] == str(paths.SCRIPTS / "build_tiles.py")
