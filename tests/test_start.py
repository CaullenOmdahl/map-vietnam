"""start.py: which steps run, the Kaggle key, .env, and the setup loop's skip / retry rules."""

import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import start  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    """start.py's state, database, tiles, .env and Kaggle config all under tmp_path."""
    monkeypatch.setattr(start, "STATE", tmp_path / "setup")
    monkeypatch.setattr(start, "LOGS", tmp_path / "setup" / "logs")
    monkeypatch.setattr(start, "CHOICES", tmp_path / "setup" / "choices.json")
    monkeypatch.setattr(start, "DB", tmp_path / "evidence.sqlite")
    monkeypatch.setattr(start, "TILES", tmp_path / "tiles")
    monkeypatch.setattr(start, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(start, "THOR_ZIP", tmp_path / "thor.zip")
    monkeypatch.setattr(os, "environ", {"KAGGLE_CONFIG_DIR": str(tmp_path / "kaggle")})
    return tmp_path


def names(steps):
    return [s.name if isinstance(s, start.Step) else s[0].name for s in steps]


def test_default_plan_leaves_out_optional_vhpa_and_thor_without_a_key(home):
    run, left_out = start.plan(start.parse_args([]), choices={}, has_key=False)
    assert "vhpa" not in names(run) and "thor" not in names(run)
    assert dict((s.name, why) for s, why in left_out).keys() == {"vhpa", "thor"}
    assert names(run)[:2] == ["gns", "hes_gazetteer"]
    run, _ = start.plan(start.parse_args([]), choices={"vhpa": True}, has_key=True)
    assert names(run)[-2:] == ["vhpa", "thor"]


def test_only_pulls_in_unfinished_dependencies_in_setup_order(home):
    run, _ = start.plan(start.parse_args(["--only", "wiki_mia"]), choices={}, has_key=False)
    assert names(run) == ["gns", "hes_gazetteer", "wiki_mia"]
    run, _ = start.plan(start.parse_args(["--only", "vhpa"]), choices={}, has_key=False)
    assert names(run) == ["gns", "hes_gazetteer", "wikidata_osm", "vhpa"]  # named, so opt-in is satisfied


def test_skip_and_unknown_names(home, capsys):
    run, left_out = start.plan(start.parse_args(["--skip", "vn_relics,thor"]), choices={}, has_key=True)
    assert "vn_relics" not in names(run) and {"vn_relics", "thor"} <= set(names(left_out))
    with pytest.raises(SystemExit):
        start.parse_args(["--only", "nope"])
    assert "unknown part nope" in capsys.readouterr().err


def test_setup_never_downloads_maps(home):
    """Old maps are added by the user only: no setup step fetches or builds one."""
    for step in start.STEPS:
        for cmd in step.commands:
            assert not any(word in " ".join(cmd) for word in ("download_maps", "build_tiles", "georef_scan", "library"))
    assert not {"l7014", "scans", "maps"} & set(start.BY_NAME)


def test_first_run_says_maps_are_added_with_the_viewers_add_old_maps_button(capsys):
    start.explain_first_run(start.STEPS, has_key=True)
    out = capsys.readouterr().out
    assert "Old maps are not downloaded now" in out and '"Add old maps"' in out and "start.py maps add" in out


def test_maps_command_hands_its_arguments_to_the_library(home, monkeypatch):
    ran = []

    class Proc:
        def __init__(self, cmd, **kw):
            ran.append((cmd, kw["env"]["PYTHONPATH"]))

        def wait(self):
            return 3

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("/venv/python"))
    monkeypatch.setattr(start.subprocess, "Popen", Proc)
    assert start.main(["maps", "add", "L701461292", "--yes"]) == 3
    assert ran[0][0] == ["/venv/python", "scripts/library.py", "add", "L701461292", "--yes"]
    assert str(start.ROOT / "scripts") in ran[0][1]
    start.main(["maps"])
    assert ran[1][0][2:] == ["--help"]


def test_old_tile_folders_are_converted_once_and_only_if_present(home, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        start.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or start.subprocess.CompletedProcess(cmd, 0)
    )
    start.convert_old_maps(Path("py"))
    assert calls == []
    (home / "tiles" / "L701461292").mkdir(parents=True)
    (home / "tiles" / "L701461292.mbtiles").write_bytes(b"")  # a file is a converted map, not a folder
    start.convert_old_maps(Path("py"))
    assert calls == [["py", "scripts/library.py", "migrate"]]
    assert "Converting 1 old map" in capsys.readouterr().out


def test_ingest_steps_need_their_stamp_the_database_and_their_outputs(home, monkeypatch):
    monkeypatch.setattr(start, "ROOT", home)
    step = start.Step("src", "Source", [], 0, "", produces=["index.json"])
    home.joinpath("setup").mkdir()
    step.stamp.write_text("x", encoding="utf-8")
    assert not step.is_done()
    (home / "evidence.sqlite").write_text("", encoding="utf-8")
    assert not step.is_done()
    (home / "index.json").write_text("{}", encoding="utf-8")
    assert step.is_done()


def test_first_run_total_counts_thor_only_with_a_key(capsys):
    """Without a key THOR is not downloaded, so the stated total leaves it out and says so."""
    thor = start.BY_NAME["thor"]
    start.explain_first_run(start.STEPS, has_key=True)
    with_key = capsys.readouterr().out
    start.explain_first_run(start.STEPS, has_key=False)
    without_key = capsys.readouterr().out
    total = start.PYTHON_ADDONS_MB + sum(s.size_mb for s in start.STEPS if not s.opt_in)
    assert f"about {total} MB" in with_key and "Not counted" not in with_key
    assert f"about {total - thor.size_mb} MB" in without_key
    assert f"Not counted: {thor.title}, {thor.size_mb} MB" in without_key


def test_kaggle_key_is_found_in_env_file_kaggle_json_or_a_cached_download(home):
    assert not start.kaggle_key_available()
    (home / "kaggle").mkdir()
    (home / "kaggle" / "kaggle.json").write_text(json.dumps({"username": "u", "key": "k"}), encoding="utf-8")
    assert start.kaggle_key_available()
    (home / "kaggle" / "kaggle.json").unlink()
    (home / ".env").write_text('KAGGLE_USERNAME="u"\nexport KAGGLE_KEY=k\n', encoding="utf-8")
    assert start.kaggle_key_available()
    (home / ".env").unlink()
    (home / "thor.zip").write_bytes(b"")
    assert start.kaggle_key_available()


def test_write_env_is_private_and_keeps_other_settings(home):
    (home / ".env").write_text("# my settings\nMAPVIETNAM_CONTACT=me@example.org\nKAGGLE_KEY=old\n", encoding="utf-8")
    start.write_env({"KAGGLE_USERNAME": "u", "KAGGLE_KEY": "new"})
    text = (home / ".env").read_text(encoding="utf-8")
    assert text == "# my settings\nMAPVIETNAM_CONTACT=me@example.org\nKAGGLE_USERNAME=u\nKAGGLE_KEY=new\n"
    if os.name != "nt":  # Windows has no owner-only mode bits; the file sits in the user's own folder
        assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600


def test_free_port_skips_a_port_in_use():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        taken = s.getsockname()[1]
        assert start.free_port(taken) != taken


def test_requirements_are_not_reinstalled_when_unchanged(home, monkeypatch):
    venv = home / ".venv"
    monkeypatch.setattr(start, "VENV", venv)
    monkeypatch.setattr(start, "REQUIREMENTS_STAMP", venv / "requirements.sha256")
    venv.mkdir()
    (venv / "requirements.sha256").write_text(
        start.hashlib.sha256(start.REQUIREMENTS.read_bytes()).hexdigest(), encoding="utf-8"
    )
    monkeypatch.setattr(start, "runs", lambda cmd: True)

    def no_install(*a, **k):
        raise AssertionError("pip install should not run")

    monkeypatch.setattr(start.subprocess, "run", no_install)
    assert start.ensure_venv() == start.venv_python()


def fake_steps(home):
    """Three steps run with this Python: one succeeds, one fails, one needs the failing one."""
    script = home / "step.py"
    script.write_text(
        "import sys\nprint('working on', sys.argv[1])\nsys.exit(sys.argv[1] == 'bad')\n", encoding="utf-8"
    )
    return [
        start.Step("good", "Good source", [[str(script), "good"]], 1, ""),
        start.Step("bad", "Bad source", [[str(script), "bad"]], 1, ""),
        start.Step("after", "Needs bad", [[str(script), "after"]], 1, "", needs=["bad"]),
    ]


def test_setup_carries_on_past_failures_and_retries_only_what_did_not_finish(home, monkeypatch, capsys):
    steps = fake_steps(home)
    monkeypatch.setattr(start, "STEPS", steps)
    monkeypatch.setattr(start, "BY_NAME", {s.name: s for s in steps})
    monkeypatch.setattr(start, "ensure_venv", lambda: Path(sys.executable))
    (home / "evidence.sqlite").write_text("", encoding="utf-8")
    args = start.parse_args(["setup", "--yes"])

    assert start.setup(args) is False
    out = capsys.readouterr().out
    assert "Welcome to Map Vietnam" in out  # first run explains what it will download
    assert "done      Good source" in out
    assert "FAILED    Bad source" in out and "last message: working on bad" in out
    assert "skipped   Needs bad: needs bad to finish first" in out
    assert "working on bad" in (home / "setup" / "logs" / "bad.log").read_text(encoding="utf-8")
    assert steps[0].stamp.exists() and not steps[1].stamp.exists()

    assert start.setup(args) is False
    out = capsys.readouterr().out
    assert "Welcome" not in out and "ready     Good source  (done before)" in out
    assert "working on good" not in out and "working on bad" in out


def test_first_run_map_sizes_come_from_the_catalogue():
    mb, disk_mb, count, all_gb = start.sheet_sizes()
    assert 5 < mb < 15 and disk_mb > mb and count == 498 and 5 < all_gb < 20


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
