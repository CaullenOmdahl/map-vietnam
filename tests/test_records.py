"""records: sources are added only when asked, by their own fixed ingester, one at a time, with
what they need first, and can be stopped or removed again without breaking anyone's case.

Runs against small fake ingesters in a temporary folder (tests/fakerecords.py): no network, and
nothing outside tmp_path is read or written.
"""

import argparse
import json
import os
import stat
import subprocess
import sys
import threading
import time

import common
import files
import pytest
import records
import store
import thor
from fakerecords import KEY, USER, fake_sources, make_home, ran, set_plan, source, wait_for
from records import Records, RecordsError


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A project folder under tmp_path with the fake ingester, and no Kaggle key anywhere."""
    return make_home(tmp_path, monkeypatch)


@pytest.fixture
def rec(home):
    r = Records(home, sources=fake_sources())
    yield r
    r.stop(timeout=10)  # never leave a fake ingester running after a test


def entry(rec, sid):
    return next(s for s in rec.listing()["sources"] if s["id"] == sid)


def states(rec):
    return {s["id"]: s["state"] for s in rec.listing()["sources"]}


def pin_in_a_case(root, source_id, record_id="0"):
    """Put one record of a source in a case, as an analyst would; returns its evidence id."""
    db = store.connect(root / "data" / "evidence.sqlite")
    eid = db.execute(
        "select id from evidence where source = ? and source_record_id = ?", (source_id, record_id)
    ).fetchone()[0]
    cid = db.execute("insert into cases (name, created_at) values ('my case', ?)", (store.now(),)).lastrowid
    db.execute("insert into case_items (case_id, evidence_id) values (?, ?)", (cid, eid))
    db.commit()
    db.close()
    return eid


# ---- the real sources --------------------------------------------------------------------------
def test_every_real_source_names_an_ingester_and_comes_after_what_it_needs():
    seen = set()
    for s in records.SOURCES:
        assert (records.ROOT / s.command[0]).is_file(), s.id
        assert set(s.needs) <= seen, f"{s.id} must come after {s.needs}"
        assert s.title and s.description.endswith(".") and s.time_text
        assert not s.opt_in or s.warning
        seen.add(s.id)
        assert not (s.opt_in and s.standard), "an opt-in source is never added without being named"
    assert [s.id for s in records.SOURCES if s.opt_in] == ["vhpa"]
    assert [s.id for s in records.SOURCES if s.uses_key] == ["thor"]
    # The standard set: what a first run adds in one go. The air strikes (big, and an area to
    # choose) and the opt-in helicopter losses are for the person to add when they want them.
    standard = [s.id for s in records.SOURCES if s.standard and s.listed]
    assert standard == ["gns", "hes_gazetteer", "caccf", "wiki_mia", "vn_relics", "wikidata_osm"]
    assert [s.id for s in records.SOURCES if not s.standard] == ["thor", "vhpa"]
    assert "key" not in next(s for s in records.SOURCES if s.id == "thor").description.lower()
    assert [s.id for s in records.SOURCES if not s.listed] == ["datum_check"]
    assert records.SOURCES[-1].yields, "the slow source runs last"


def test_the_air_strike_constants_match_the_ingester():
    assert records.DEFAULT_AREA == common.AREA_OF_INTEREST
    assert records.KEY_EXIT == thor.KEY_EXIT
    w, s, e, n = thor.VIETNAM_BBOX
    lw, ls, le, ln = records.AREA_LIMITS
    assert lw <= w and ls <= s and e <= le and n <= ln  # the box an area must lie in covers Vietnam


def test_setup_never_downloads_maps():
    """Old maps are added by the user only: no record source fetches or builds one."""
    for s in records.SOURCES:
        assert s.command[0].startswith("scripts/ingest/")
    assert not {"l7014", "scans", "maps"} & {s.id for s in records.SOURCES}


# ---- listing and adding ------------------------------------------------------------------------
def test_nothing_runs_until_asked_and_the_listing_describes_every_listed_source(rec, home):
    listing = rec.listing()
    assert set(states(rec).values()) == {"available"} and ran(home) == []
    assert listing["busy"] is False and listing["first_run"] is True and listing["revision"] == 0
    assert "check" not in states(rec)  # a check that makes no records is for the command line only
    child = entry(rec, "child")
    assert child["title"] == "Source child" and child["needs"] == ["base"]
    assert child["also_adds"] == [{"id": "base", "title": "Source base"}]
    assert child["records"] is None and child["added_at"] is None and child["progress"] is None
    assert entry(rec, "optional")["opt_in"] and "copyrighted" in entry(rec, "optional")["warning"]
    assert not (home / "data").exists()


def test_adding_a_source_adds_what_it_needs_first(rec, home):
    assert rec.add(["grandchild"]) == ["base", "child", "grandchild"]
    assert states(rec)["grandchild"] == "queued" and rec.listing()["busy"]
    assert entry(rec, "grandchild")["status_text"] == "Waiting for its turn"
    seen = []
    rec.run_pending(lambda job: seen.append((job.source.id, job.state, job.progress, job.status_text)))

    assert ran(home) == ["base", "child", "grandchild"]
    assert ("base", "running", 0.25, "Downloading 1.0 MB of 4.0 MB") in seen
    assert ("base", "running", None, "Saving the records") in seen
    assert states(rec) == {**states(rec), "base": "added", "child": "added", "grandchild": "added"}
    base = entry(rec, "base")
    assert base["records"] == 2 and base["disk_bytes"] == 1000 and base["also_adds"] == []
    assert [s["id"] for s in base["needed_by"]] == ["child", "grandchild"]
    assert time.strptime(base["added_at"][:19], "%Y-%m-%dT%H:%M:%S")
    listing = rec.listing()
    assert not listing["busy"] and not listing["first_run"] and listing["revision"] > 0
    assert "progress" not in rec.log_path(rec.by_id["base"]).read_text(
        encoding="utf-8"
    )  # the log keeps what was said, not the ticks


def test_a_source_already_added_or_on_its_way_is_left_alone_unless_refreshed(rec, home):
    rec.add(["child"])
    assert rec.add(["child", "base"]) == []  # both already queued
    rec.run_pending()
    assert rec.add(["child"]) == [] and ran(home) == ["base", "child"]
    assert rec.add(["child"], refresh=True) == ["child"]  # only the one named, not what it needs
    rec.run_pending()
    assert ran(home)[-1] == "child --refresh"
    rec.add(["slow"], refresh=True)
    rec.run_pending()
    assert ran(home)[-1] == "slow --refresh-index"  # each source's own way of downloading again


def test_ids_that_are_not_listed_sources_are_refused(rec, home):
    for bad in (None, [], "base", [1], ["nope"], ["check"], ["../../etc/passwd"], ["base; rm -rf /"]):
        with pytest.raises(RecordsError):
            rec.add(bad)
    assert rec.add(["check"], listed_only=False) == ["base", "check"]  # the command line may
    assert rec._queue and ran(home) == []


def test_the_command_is_always_the_sources_own_ingester(rec, home):
    job = records.Job(rec.by_id["child"])
    assert rec.command(job) == [sys.executable, str(home / "fake_ingest.py"), "child"]
    real = Records(home)  # the real sources, never run here
    for s in real.sources:
        cmd = real.command(records.Job(s, options={"area": [105.0, 10.0, 106.0, 11.0]}, refresh=True))
        assert cmd[:2] == [sys.executable, str(home / "scripts" / "ingest" / f"{s.id}.py")]
        assert all(isinstance(part, str) for part in cmd)
    thor_cmd = real.command(records.Job(real.by_id["thor"], options={"area": [105.0, 10.0, 106.0, 11.0]}))
    assert thor_cmd[2:] == ["--bbox", "105.0,10.0,106.0,11.0", "--max-strikes", str(records.MAX_STRIKES)]
    assert real.command(records.Job(real.by_id["vhpa"], refresh=True))[2:] == ["--refresh-index"]


# ---- failures ----------------------------------------------------------------------------------
def test_a_failure_is_put_in_plain_words_and_only_stops_what_needs_it(rec, home):
    set_plan(home, base={"fail": "The archive is closed for maintenance. Try again tomorrow."})
    rec.add(["child", "optional"], accept=["optional"])
    rec.run_pending()
    base, child = entry(rec, "base"), entry(rec, "child")
    assert base["state"] == "failed" and base["error"] == "The archive is closed for maintenance. Try again tomorrow."
    assert base["status_text"] is None and base["progress"] is None
    assert (
        child["state"] == "failed"
        and child["error"] == "Not added: it needs “Source base” first, and that did not finish."
    )
    assert states(rec)["optional"] == "added"  # one source failing never stops the others
    assert not rec.listing()["busy"]

    set_plan(home, base={"makes": ["data/work/base_index.json"]})
    assert rec.add(["child"]) == ["base", "child"]  # a failed source is simply tried again
    rec.run_pending()
    assert states(rec)["base"] == states(rec)["child"] == "added" and entry(rec, "base")["error"] is None


@pytest.mark.parametrize(
    ("crash", "expected"),
    [
        ("urllib.error.URLError: <urlopen error timed out>", "Could not download “Source base”. Check the internet"),
        ("database is locked", "The record database was busy."),
        ("list index out of range", "Adding “Source base” did not work. Try again; if it keeps failing, the details"),
    ],
)
def test_a_crash_is_explained_without_the_traceback(rec, home, crash, expected):
    set_plan(home, base={"crash": crash})
    rec.add(["base"])
    rec.run_pending()
    base = entry(rec, "base")
    assert base["state"] == "failed" and base["error"].startswith(expected)
    assert "Traceback" not in base["error"] and crash in base["error_detail"]
    assert "Traceback" in rec.log_path(rec.by_id["base"]).read_text(encoding="utf-8")  # the whole story is in the log


def test_a_source_that_finishes_without_its_files_is_not_marked_added(rec, home):
    set_plan(home, base={})  # makes no index file
    rec.add(["base"])
    rec.run_pending()
    assert entry(rec, "base")["state"] == "failed" and "missing" in entry(rec, "base")["error"]
    assert not rec.stamp(rec.by_id["base"]).exists()


def test_progress_lines_are_read_and_anything_else_is_not():
    assert records.parse_progress("progress 0.42 Downloading 134 MB of 320 MB\n") == (
        0.42,
        "Downloading 134 MB of 320 MB",
    )
    assert records.parse_progress("progress - Saving 26,551 air strikes") == (None, "Saving 26,551 air strikes")
    assert records.parse_progress("progress 0.5") == (0.5, "") and records.parse_progress("progress 7 x") == (1.0, "x")
    for other in ("progress", "progress oops", "progressing well", "thor: read 4670416 rows", ""):
        assert records.parse_progress(other) is None


# ---- stopping ----------------------------------------------------------------------------------
def test_a_running_source_can_be_stopped_and_goes_back_to_what_the_disk_says(rec, home):
    set_plan(home, child={"wait": True})
    rec.add(["child", "optional"], accept=["optional"])
    rec.start_worker()
    wait_for((home / "child.started").exists)
    running = entry(rec, "child")
    assert running["state"] == "running" and running["progress"] == 0.25
    assert running["status_text"] == "Downloading 1.0 MB of 4.0 MB"
    proc = rec._jobs["child"].proc

    assert rec.cancel(["child"]) == ["child"]
    assert rec.wait_idle(20)
    assert proc.poll() is not None  # the ingester itself was ended, not just forgotten
    assert states(rec)["child"] == "available" and entry(rec, "child")["error"] is None
    assert states(rec)["base"] == "added" and states(rec)["optional"] == "added"  # the rest went on


def test_stopping_a_queued_or_failed_source_forgets_it(rec, home):
    set_plan(home, base={"wait": True}, optional={"fail": "No."})
    rec.add(["child", "optional"], accept=["optional"])
    rec.start_worker()
    wait_for((home / "base.started").exists)
    assert rec.cancel(["child"]) == ["child"] and states(rec)["child"] == "available"
    (home / "base.go").touch()
    assert rec.wait_idle(20)
    assert ran(home) == ["base", "optional"] and states(rec)["optional"] == "failed"
    assert rec.cancel(["optional"]) == ["optional"] and states(rec)["optional"] == "available"
    assert rec.cancel(["optional"]) == []  # nothing left to stop


def test_stopping_a_source_that_is_being_added_again_keeps_its_records(rec, home):
    rec.add(["base"])
    rec.run_pending()
    set_plan(home, base={"wait": True})
    rec.add(["base"], refresh=True)
    rec.start_worker()
    wait_for((home / "base.started").exists)
    assert entry(rec, "base")["records"] == 2  # still there while the new download runs
    rec.cancel(["base"])
    assert rec.wait_idle(20)
    assert states(rec)["base"] == "added" and entry(rec, "base")["records"] == 2


def test_stop_ends_the_running_ingester_and_empties_the_queue(rec, home):
    set_plan(home, base={"wait": True})
    rec.add(["grandchild"])
    rec.start_worker()
    wait_for((home / "base.started").exists)
    proc = rec._jobs["base"].proc
    assert rec.stop(timeout=20) is True
    assert proc.poll() is not None and ran(home) == ["base"]
    assert set(states(rec).values()) == {"available"} and not rec.listing()["busy"]


def test_a_slow_source_steps_aside_for_others_and_carries_on_after(rec, home):
    set_plan(home, slow={"wait": True})
    rec.add(["slow"])
    rec.start_worker()
    wait_for((home / "slow.started").exists)
    (home / "slow.started").unlink()

    rec.add(["optional"], accept=["optional"])  # arrives while the slow one is running
    wait_for(lambda: states(rec)["optional"] == "added")
    wait_for((home / "slow.started").exists)  # ... and the slow one has started again
    assert ran(home) == ["slow", "optional", "slow"]
    assert states(rec)["slow"] == "running"
    (home / "slow.go").touch()
    assert rec.wait_idle(20) and states(rec)["slow"] == "added"


def test_a_slow_source_waits_its_turn_behind_everything_else(rec, home):
    rec.add(["slow"])
    rec.add(["child"])
    paused = []
    rec.run_pending(lambda job: paused.append(job.status_text))
    assert ran(home) == ["base", "child", "slow"]
    assert not any("Paused" in (text or "") for text in paused)  # it never started, so nothing to pause


# ---- removing ----------------------------------------------------------------------------------
def test_removing_a_source_deletes_its_records_marker_downloads_and_files(rec, home):
    rec.add(["base", "optional"], accept=["optional"])
    rec.run_pending()
    assert (home / "data/raw/base/download.bin").exists() and (home / "data/work/base_summary.json").exists()

    assert rec.remove(["base"]) == ["base"]
    assert entry(rec, "base")["removing"] and entry(rec, "base")["state"] == "queued"
    rec.run_pending()

    assert states(rec)["base"] == "available" and entry(rec, "base")["disk_bytes"] == 0
    for gone in (
        "data/raw/base",
        "data/work/base_index.json",
        "data/work/base_summary.json",
        "data/work/setup/base.done",
    ):
        assert not (home / gone).exists(), gone
    assert not rec.log_path(rec.by_id["base"]).exists()
    db = store.connect(home / "data" / "evidence.sqlite")
    assert db.execute("select count(*) from evidence where source = 'base'").fetchone()[0] == 0
    assert [r[0] for r in db.execute("select id from sources")] == ["optional"]  # only its own source went
    assert states(rec)["optional"] == "added" and entry(rec, "optional")["records"] == 2
    assert rec.listing()["first_run"] is False  # removing everything is not a first run again


def test_a_record_in_a_case_survives_removal_marked_withdrawn(rec, home):
    rec.add(["base"])
    rec.run_pending()
    eid = pin_in_a_case(home, "base")
    rec.remove(["base"])
    rec.run_pending()

    db = store.connect(home / "data" / "evidence.sqlite")
    rows = db.execute("select id, withdrawn_at, withdrawn_reason from evidence where source = 'base'").fetchall()
    assert [r[0] for r in rows] == [eid] and rows[0][1] and "removed from Map Vietnam" in rows[0][2]
    assert db.execute("select count(*) from case_items where evidence_id = ?", (eid,)).fetchone()[0] == 1
    assert db.execute("select count(*) from sources where id = 'base'").fetchone()[0] == 1  # still referred to
    base = entry(rec, "base")
    assert base["state"] == "available" and base["records"] is None and base["held_in_cases"] == 1

    rec.add(["base"])  # adding it again makes the record current again
    rec.run_pending()
    assert db.execute("select withdrawn_at from evidence where id = ?", (eid,)).fetchone()[0] is None
    assert entry(rec, "base")["records"] == 2 and entry(rec, "base")["held_in_cases"] == 0


def test_removing_what_others_need_is_refused_unless_it_cascades(rec, home):
    rec.add(["grandchild"])
    rec.run_pending()
    with pytest.raises(RecordsError) as e:
        rec.remove(["base"])
    assert e.value.status == 409 and "“Source child”, “Source grandchild”" in str(e.value)
    assert e.value.extra["also_removes"] == [
        {"id": "child", "title": "Source child"},
        {"id": "grandchild", "title": "Source grandchild"},
    ]
    assert states(rec)["base"] == "added" and not rec.listing()["busy"]  # refused means nothing changed

    assert rec.remove(["base"], cascade=True) == ["grandchild", "child", "base"]  # what needs it goes first
    rec.run_pending()
    assert {states(rec)[i] for i in ("base", "child", "grandchild")} == {"available"}
    assert not (home / "data" / "raw" / "base").exists()


def test_a_download_another_added_source_reads_is_kept(home):
    """Two sources that keep downloads in one folder: removing one must not take the other's."""
    sources = (source("one", raw_folders=("shared",)), source("two", raw_folders=("two",), reads_raw=("shared",)))
    rec = Records(home, sources=sources)
    rec.add(["one", "two"])
    rec.run_pending()
    (home / "data" / "raw" / "shared").mkdir()
    (home / "data" / "raw" / "shared" / "file.bin").write_bytes(b"both read this")
    rec.remove(["one"])
    rec.run_pending()
    assert (home / "data" / "raw" / "shared" / "file.bin").exists() and rec.state_of("one") == "available"
    rec.remove(["two"])
    rec.add(["one"])
    rec.run_pending()
    rec.remove(["one"])
    rec.run_pending()
    assert not (home / "data" / "raw" / "shared").exists()  # nobody reads it any more


def test_removing_a_source_that_is_running_stops_it_first(rec, home):
    set_plan(home, optional={"wait": True})
    rec.add(["optional"], accept=["optional"])
    rec.start_worker()
    wait_for((home / "optional.started").exists)
    proc = rec._jobs["optional"].proc
    (home / "data" / "raw" / "optional").mkdir(parents=True)
    (home / "data" / "raw" / "optional" / "half.part").write_bytes(b"half a download")

    assert rec.remove(["optional"]) == ["optional"]
    assert rec.wait_removed(["optional"], 20) and rec.wait_idle(20)
    assert proc.poll() is not None and states(rec)["optional"] == "available"
    assert not (home / "data" / "raw" / "optional").exists()


def test_adding_again_overrules_a_removal_that_has_not_started(rec, home):
    rec.add(["base"])
    rec.run_pending()
    rec.remove(["base"])
    assert rec.add(["base"]) == []  # still on disk: the removal is dropped, nothing to run
    rec.run_pending()
    assert states(rec)["base"] == "added" and entry(rec, "base")["records"] == 2 and ran(home) == ["base"]


def test_removing_something_never_added_does_nothing(rec, home):
    assert rec.remove(["base"]) == [] and not rec.listing()["busy"]


# ---- opt-in, the key, the air-strike area ------------------------------------------------------
def test_an_opt_in_source_needs_a_yes(rec, home):
    with pytest.raises(RecordsError) as e:
        rec.add(["base", "optional"])
    assert str(e.value) == "This one is slow and comes from a copyrighted site."
    assert e.value.status == 400 and e.value.extra == {"needs_accept": ["optional"]}
    assert not rec.listing()["busy"]  # nothing at all was queued, not even the others named
    assert rec.add(["base", "optional"], accept=["optional"]) == ["base", "optional"]


def test_a_saved_key_is_found_in_the_env_file_the_environment_or_kaggle_json(rec, home):
    assert rec.key_status() == {"present": False, "source": None}
    (home / "data" / "raw" / "thor").mkdir(parents=True)
    (home / "data" / "raw" / "thor" / thor.ZIP_NAME).write_bytes(b"")
    assert rec.key_status() == {"present": False, "source": None}  # a download kept is not a key
    (home / "kaggle").mkdir()
    (home / "kaggle" / "kaggle.json").write_text(json.dumps({"username": USER, "key": KEY}), encoding="utf-8")
    assert rec.key_status()["source"] == "kaggle.json"
    os.environ.update(KAGGLE_USERNAME=USER, KAGGLE_KEY=KEY)
    assert rec.key_status()["source"] == "environment"
    (home / ".env").write_text(f'KAGGLE_USERNAME="{USER}"\nexport KAGGLE_KEY={KEY}\n', encoding="utf-8")
    assert rec.key_status() == {"present": True, "source": "env file"}
    (home / ".env").write_text(f"KAGGLE_USERNAME={USER}\nKAGGLE_KEY=\n", encoding="utf-8")  # half a key is no key
    del os.environ["KAGGLE_KEY"]
    assert rec.key_status()["source"] == "kaggle.json"
    assert rec.listing()["key"] == {"needed": False, "present": True, "source": "kaggle.json"}


def test_saving_the_key_is_private_and_keeps_other_settings(rec, home):
    (home / ".env").write_text(
        "# my settings\nMAPVIETNAM_CONTACT=me@example.org\nKAGGLE_KEY=old-old-old-old-old\nKAGGLE_USERNAME=\n",
        encoding="utf-8",
    )
    rec.save_key(f"  {USER} ", f"{KEY}\n")
    assert (home / ".env").read_text(encoding="utf-8") == (
        f"# my settings\nMAPVIETNAM_CONTACT=me@example.org\nKAGGLE_USERNAME={USER}\nKAGGLE_KEY={KEY}\n"
    )
    if os.name != "nt":  # Windows has no owner-only mode bits
        assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    assert rec.key_status()["source"] == "env file"
    # The ingesters this process starts get the new key, not one loaded from the old file.
    assert os.environ["KAGGLE_KEY"] == KEY and rec.child_env()["KAGGLE_USERNAME"] == USER


def test_forgetting_the_key_removes_only_its_two_lines(rec, home):
    (home / "kaggle").mkdir()
    (home / "kaggle" / "kaggle.json").write_text(json.dumps({"username": "other", "key": "k" * 32}), encoding="utf-8")
    (home / ".env").write_text("MAPVIETNAM_CONTACT=me@example.org\n", encoding="utf-8")
    rec.save_key(USER, KEY)
    rec.forget_key()
    assert (home / ".env").read_text(encoding="utf-8") == "MAPVIETNAM_CONTACT=me@example.org\n"
    assert "KAGGLE_KEY" not in os.environ and "KAGGLE_USERNAME" not in os.environ
    assert rec.key_status()["source"] == "kaggle.json"  # Kaggle's own file is never touched
    assert json.loads((home / "kaggle" / "kaggle.json").read_text(encoding="utf-8"))["username"] == "other"
    rec.forget_key()  # forgetting twice, or with no .env at all, is fine
    (home / ".env").unlink()
    rec.forget_key()
    assert not (home / ".env").exists()


def test_forgetting_the_key_leaves_one_set_outside_the_env_file(rec, home):
    os.environ.update(KAGGLE_USERNAME="shell-user", KAGGLE_KEY="s" * 32)
    (home / ".env").write_text(f"KAGGLE_USERNAME={USER}\nKAGGLE_KEY={KEY}\n", encoding="utf-8")
    rec.forget_key()
    assert os.environ["KAGGLE_KEY"] == "s" * 32 and rec.key_status()["source"] == "environment"


@pytest.mark.parametrize(
    ("username", "key"),
    [
        ("", KEY),
        (USER, ""),
        (USER, "short"),
        (USER, KEY + "\nMAPVIETNAM_CONTACT=evil"),
        (USER + "\nX=1", KEY),
        (USER, 'abc"def' + KEY),
        ("two words", KEY),
        (USER, KEY + " " + KEY),
        (None, KEY),
        (USER, 12345678901234567890),
    ],
)
def test_a_key_that_could_break_the_env_file_is_refused_without_repeating_it(rec, home, username, key):
    with pytest.raises(RecordsError) as e:
        rec.save_key(username, key)
    for secret in (username, key):
        if isinstance(secret, str) and secret.strip():
            assert secret.strip() not in str(e.value) and secret.strip() not in repr(e.value.extra)
    assert not (home / ".env").exists()


KEY_WANTED = "Kaggle asked for a key before it would hand over the air strike records, and none is saved yet."


def test_adding_the_air_strikes_never_asks_for_a_key_up_front(rec, home):
    assert rec.listing()["key"] == {"needed": False, "present": False, "source": None}
    assert rec.add(["base", "thor"]) == ["base", "thor"]  # no key anywhere: queued all the same
    rec.run_pending()
    assert states(rec)["thor"] == "added" and not (home / ".env").exists()
    assert not any(s["wants_key"] for s in rec.listing()["sources"])
    assert rec.listing()["key"]["needed"] is False


def test_a_key_is_wanted_only_when_kaggle_asks_for_one(rec, home):
    set_plan(home, thor={"wants_key": KEY_WANTED}, base={"fail": "The archive is closed."})
    rec.add(["thor", "base"])
    rec.run_pending()
    failed = entry(rec, "thor")
    assert failed["state"] == "failed" and failed["wants_key"] is True and failed["error"] == KEY_WANTED
    assert entry(rec, "base")["state"] == "failed" and entry(rec, "base")["wants_key"] is False  # any other failure
    assert rec.listing()["key"] == {"needed": True, "present": False, "source": None}
    assert rec.wanting_key() == ["thor"]

    rec.save_key(USER, KEY)  # what the viewer does next: save the key, then add the source again
    assert rec.listing()["key"] == {"needed": True, "present": True, "source": "env file"}
    set_plan(home, thor={})
    assert rec.add(["thor"]) == ["thor"]
    assert entry(rec, "thor")["wants_key"] is False and rec.listing()["key"]["needed"] is False
    rec.run_pending()
    assert states(rec)["thor"] == "added" and rec.wanting_key() == []


def test_the_key_status_means_a_key_only_from_a_source_that_uses_one(rec, home):
    """Exit status 3 from an ingester that has nothing to do with Kaggle is just a failure."""
    set_plan(home, optional={"wants_key": "Something else went wrong."})
    rec.add(["optional"], accept=["optional"])
    rec.run_pending()
    assert entry(rec, "optional")["state"] == "failed" and entry(rec, "optional")["wants_key"] is False
    rec.cancel(["optional"])
    set_plan(home, thor={"wants_key": KEY_WANTED})
    rec.add(["thor"])
    rec.run_pending()
    rec.cancel(["thor"])  # dismissing the failure also stops asking for the key
    assert entry(rec, "thor")["wants_key"] is False and rec.listing()["key"]["needed"] is False


def test_every_source_says_whether_it_is_in_the_standard_set(rec):
    flags = {s["id"]: s["standard"] for s in rec.listing()["sources"]}
    assert flags == {"base": True, "child": True, "grandchild": True, "optional": False, "thor": False, "slow": True}
    assert all("needs_key" not in s and s["wants_key"] is False for s in rec.listing()["sources"])


def test_the_key_never_reaches_a_log_or_an_error(rec, home):
    rec.save_key(USER, KEY)
    set_plan(
        home, thor={"says": [f"debug: Authorization for {KEY}"], "fail": f"Kaggle did not accept this key ({KEY})."}
    )
    rec.add(["thor"])
    rec.run_pending()
    thor_entry = entry(rec, "thor")
    assert thor_entry["state"] == "failed" and thor_entry["error"] == "Kaggle did not accept this key ([hidden])."
    assert KEY not in json.dumps(rec.listing())
    log = rec.log_path(rec.by_id["thor"]).read_text(encoding="utf-8")
    assert KEY not in log and "[hidden]" in log


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "105,10,106,11",
        [105, 10, 106],
        [105, 10, 106, 11, 12],
        ["105", 10, 106, 11],
        [True, 10, 106, 11],
        [106, 10, 105, 11],  # west of east
        [105, 11, 106, 10],  # north of south
        [105, 10, 105, 11],  # no width
        [float("nan"), 10, 106, 11],
        [105, 10, float("inf"), 11],
        [2.2, 48.8, 2.4, 48.9],  # Paris
        [99.9, 10, 106, 11],
        [105, 10, 106, 24.5],
        [-106, 10, -105, 11],
    ],
)
def test_an_area_must_be_a_box_in_or_near_vietnam(bad):
    with pytest.raises(RecordsError):
        records.check_area(bad)


def test_a_good_area_is_rounded_and_described():
    assert records.check_area([105, 10, 106.123456789, 11]) == [105.0, 10.0, 106.12346, 11.0]
    assert records.check_area(list(records.AREA_LIMITS)) == list(records.AREA_LIMITS)
    assert (
        records.area_text(records.DEFAULT_AREA)
        == "Around Cao Lãnh: 105.35 to 105.95° E, 10.15 to 10.75° N (about 66 by 66 km)"
    )
    assert records.area_text([106.0, 16.0, 107.0, 17.0]) == "106 to 107° E, 16 to 17° N (about 107 by 111 km)"


def test_the_air_strike_area_is_checked_stored_and_reported(rec, home):
    rec.save_key(USER, KEY)
    for bad in (
        {"thor": {"area": [1, 2, 3, 4]}},
        {"base": {"area": [105, 10, 106, 11]}},
        {"thor": {"shell": "rm"}},
        [1],
    ):
        with pytest.raises(RecordsError):
            rec.add(["thor"], options=bad)
    with pytest.raises(RecordsError, match="3 million"):
        rec.add(["thor"], options={"thor": {"all_vietnam": True}})
    assert not rec.listing()["busy"]

    rec.add(["thor"])  # no area asked for: the default one
    assert entry(rec, "thor")["options"] == {"area": list(records.DEFAULT_AREA)}
    rec.run_pending()
    assert ran(home)[-1] == f"thor --bbox 105.35,10.15,105.95,10.75 --max-strikes {records.MAX_STRIKES}"
    added = entry(rec, "thor")
    assert added["options"] == {"area": [105.35, 10.15, 105.95, 10.75]} and added["area_recorded"] is True
    assert added["area_text"].startswith("Around Cao Lãnh")
    assert (
        added["area_choice"]["all_vietnam"]["allowed"] is False
        and added["area_choice"]["max_records"] == records.MAX_STRIKES
    )

    assert rec.add(["thor"], options={"thor": {"area": [105.35, 10.15, 105.95, 10.75]}}) == []  # the same area
    assert rec.add(["thor"]) == []
    assert rec.add(["thor"], options={"thor": {"area": [106, 16, 107, 17]}}) == ["thor"]  # another: run again
    assert entry(rec, "thor")["options"] == {"area": [106.0, 16.0, 107.0, 17.0]}  # what is on its way
    rec.run_pending()
    assert ran(home)[-1].startswith("thor --bbox 106.0,16.0,107.0,17.0")
    assert json.loads(rec.stamp(rec.by_id["thor"]).read_text(encoding="utf-8"))["options"] == {
        "area": [106.0, 16.0, 107.0, 17.0]
    }
    assert rec.add(["thor"], refresh=True) == ["thor"] and rec._jobs["thor"].options == {
        "area": [106.0, 16.0, 107.0, 17.0]
    }


def test_a_failed_change_of_area_still_reports_the_area_in_the_store(rec, home):
    rec.save_key(USER, KEY)
    rec.add(["thor"])
    rec.run_pending()
    set_plan(home, thor={"fail": "This area holds more than 150,000 air strikes."})
    rec.add(["thor"], options={"thor": {"area": [106, 16, 107, 17]}})
    rec.run_pending()
    failed = entry(rec, "thor")
    assert failed["state"] == "failed" and failed["error"] == "This area holds more than 150,000 air strikes."
    assert failed["options"] == {"area": list(records.DEFAULT_AREA)} and failed["records"] == 2  # nothing changed
    rec.cancel(["thor"])
    assert states(rec)["thor"] == "added"

    rec.remove(["thor"])
    rec.run_pending()
    rec.add(["thor"], options={"thor": {"area": [106, 16, 107, 17]}})
    rec.run_pending()
    assert entry(rec, "thor")["options"] == {"area": [106.0, 16.0, 107.0, 17.0]}  # never added: the one tried


def test_air_strikes_added_by_an_older_version_report_where_they_lie(rec, home):
    rec.save_key(USER, KEY)
    rec.add(["thor"])
    rec.run_pending()
    rec.stamp(rec.by_id["thor"]).write_text("2026-01-02T03:04:05\n", encoding="utf-8")  # an old marker: just the time
    old = entry(rec, "thor")
    assert old["state"] == "added" and old["added_at"] == "2026-01-02T03:04:05"
    assert old["options"] == {"area": list(records.DEFAULT_AREA)} and old["area_recorded"] is False
    db = store.connect(home / "data" / "evidence.sqlite")
    db.execute("update evidence set lon = 107.123, lat = 16.456 where source = 'thor' and source_record_id = '1'")
    db.commit()
    wide = entry(rec, "thor")  # loaded by hand with another area: the box the strikes lie in
    assert wide["options"] == {"area": [105.5, 10.5, 107.13, 16.46]} and wide["area_recorded"] is False


# ---- two processes -----------------------------------------------------------------------------
class OtherProcess:
    """Another process working on `source_id`: a child that holds the worker lock and has
    written current.json, as `start.py setup` does, until stopped."""

    def __init__(self, rec, source_id, action="add"):
        code = (
            "import fcntl, sys, time; f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX);"
            "print('locked', flush=True); time.sleep(60)"
        )
        rec.state_dir.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, str(rec.state_dir / "worker.lock")],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert self.proc.stdout.readline().strip() == "locked"
        files.write_json(rec.state_dir / "current.json", {"id": source_id, "pid": self.proc.pid, "action": action})

    def stop(self):
        self.proc.kill()
        self.proc.wait()


needs_fcntl = pytest.mark.skipif(files.fcntl is None, reason="the stand-in for another process uses fcntl")


@needs_fcntl
def test_a_source_another_process_is_adding_shows_as_running_and_this_one_waits(rec, home):
    other = OtherProcess(rec, "base")
    try:
        listing = rec.listing()
        assert listing["busy"] and entry(rec, "base")["state"] == "running"
        assert entry(rec, "base")["status_text"] == "Being added in another window"
        rec.add(["optional"], accept=["optional"])
        rec.start_worker()
        wait_for(lambda: entry(rec, "optional")["status_text"] == "Waiting for another Map Vietnam window to finish")
        assert ran(home) == []  # two processes never add records at once
        assert rec.cancel(["base"]) == ["base"] and rec.cancel_path("base").exists()  # asks the other to stop
    finally:
        other.stop()
    assert rec.wait_idle(20) and states(rec)["optional"] == "added"
    assert states(rec)["base"] == "available"  # once that process is gone, its record means nothing
    assert rec.listing()["revision"] > 0


def test_a_stop_asked_for_by_another_process_ends_this_ones_job(rec, home):
    set_plan(home, base={"wait": True})
    rec.add(["base"])
    worker = threading.Thread(target=rec.run_pending)
    worker.start()
    wait_for((home / "base.started").exists)
    assert json.loads((rec.state_dir / "current.json").read_text(encoding="utf-8"))["id"] == "base"
    rec.request_cancel("base")  # what cancel() or remove() in another process does
    worker.join(20)
    assert not worker.is_alive() and states(rec)["base"] == "available"
    assert not rec.cancel_path("base").exists() and not (rec.state_dir / "current.json").exists()


# ---- first run, caches -------------------------------------------------------------------------
def test_first_run_ends_when_the_welcome_is_dismissed_or_something_was_added(rec, home):
    assert rec.first_run()
    rec.set_choices(welcome_dismissed=True)
    assert not rec.first_run() and rec.choices() == {"welcome_dismissed": True}
    rec.set_choices(welcome_dismissed=False)
    assert rec.first_run()
    rec.add(["optional"], accept=["optional"])
    rec.run_pending()
    assert not rec.first_run() and rec.choices()["ever_added"] is True
    (home / "data" / "work" / "setup" / "choices.json").write_text(json.dumps({"agreed": True}), encoding="utf-8")
    rec.stamp(rec.by_id["optional"]).unlink()
    assert not rec.first_run()  # someone who said yes on the terminal, in an older version, is not new


def test_the_server_is_told_whenever_the_records_changed(home):
    calls = []
    rec = Records(home, sources=fake_sources(), on_change=lambda: calls.append(1))
    rec.add(["optional"], accept=["optional"])
    rec.run_pending()
    assert calls and rec.listing()["revision"] == len(calls)
    before = len(calls)
    rec.remove(["optional"])
    rec.run_pending()
    assert len(calls) > before and entry(rec, "optional")["records"] is None


# ---- .env --------------------------------------------------------------------------------------
def test_env_files_are_read_the_way_the_ingesters_read_them(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\n\nA=1\nexport B = 'two'\nC=\nnot a setting\nD=\"x=y\"\n", encoding="utf-8")
    assert records.read_env(path) == {"A": "1", "B": "two", "D": "x=y"}
    assert records.read_env(tmp_path / "missing") == {}
    os.environ.pop("A", None)
    common.load_env(path)
    try:
        assert {k: os.environ[k] for k in ("A", "B", "D")} == records.read_env(path)
    finally:
        for name in ("A", "B", "D"):
            os.environ.pop(name, None)


# ---- the command line --------------------------------------------------------------------------
def test_the_default_setup_is_the_standard_set(rec):
    ids, left_out = records.setup_plan(rec, only=[], skip=[], choices={})
    assert ids == ["base", "child", "grandchild", "check", "slow"]
    reasons = {s.id: why for s, why in left_out}
    assert set(reasons) == {"optional", "thor"}
    assert reasons["optional"].startswith("optional, not chosen")
    assert reasons["thor"] == (
        "not in the standard set (add it in the map viewer, or with: python3 start.py setup --only thor --area W,S,E,N)"
    )
    ids, left_out = records.setup_plan(rec, only=[], skip=[], choices={"optional": True})
    assert "optional" in ids and "thor" not in ids  # a yes to the optional one; the air strikes are still by name
    ids, left_out = records.setup_plan(rec, only=["thor"], skip=[], choices={})
    assert ids == ["thor"] and left_out == []


def test_the_real_default_setup_adds_the_standard_sources_and_the_check(home):
    ids, left_out = records.setup_plan(Records(home), only=[], skip=[], choices={})
    assert ids == ["gns", "hes_gazetteer", "datum_check", "caccf", "wiki_mia", "vn_relics", "wikidata_osm"]
    assert [s.id for s, _ in left_out] == ["thor", "vhpa"]


def test_the_real_command_line_parses_its_options():
    args = records.parse_args(["setup", "--yes", "--only", "thor,gns", "--skip", "vhpa", "--area", "106,16,107,17"])
    assert args.only == ["thor", "gns"] and args.skip == ["vhpa"] and args.yes and args.area == "106,16,107,17"
    args = records.parse_args(["remove", "gns", "thor", "--cascade"])
    assert args.ids == ["gns", "thor"] and args.cascade
    assert records.parse_args(["list"]).command == "list"


def test_only_and_skip_choose_the_sources(rec):
    ids, left_out = records.setup_plan(rec, only=["optional", "child"], skip=[], choices={})
    assert ids == ["child", "optional"] and left_out == []  # named, so the opt-in is satisfied
    ids, left_out = records.setup_plan(rec, only=[], skip=["child", "thor"], choices={})
    assert "child" not in ids and [s.id for s, _ in left_out][:1] == ["child"]
    with pytest.raises(SystemExit):
        records.parse_args(["setup", "--only", "nope"])


def setup_args(only=None, area=None, refresh=False):
    """What `setup --yes [--only ...] [--area ...] [--refresh]` parses to, for the fake sources
    (the real parser only knows the real source ids)."""
    return argparse.Namespace(yes=True, only=only, skip=None, refresh=refresh, area=area)


def test_setup_carries_on_past_failures_and_retries_only_what_did_not_finish(rec, home, capsys):
    set_plan(home, child={"fail": "The child archive is down."}, optional={"says": ["working on optional"]})
    assert records.cmd_setup(rec, setup_args()) == 1
    out = capsys.readouterr().out
    assert "Welcome to Map Vietnam" in out and "Old maps are not downloaded now" in out  # the first run explains
    assert "done      Source base" in out and "== Source base (base) ==" in out
    assert "FAILED    Source child" in out and "The child archive is down." in out
    assert "full log: data/work/setup/logs/child.log" in out
    assert "FAILED    Source grandchild" in out and "needs “Source child” first" in out
    assert "skipped   Source optional: optional, not chosen" in out
    assert "skipped   Source thor: not in the standard set (add it in the map viewer, or with:" in out
    assert "python3 start.py setup --only child" in out

    set_plan(home, child={})
    assert records.cmd_setup(rec, setup_args()) == 0
    out = capsys.readouterr().out
    assert "Welcome" not in out and "ready     Source base  (done before)" in out
    assert "done      Source child" in out and ran(home).count("base") == 1


def test_setup_does_not_welcome_someone_who_added_records_in_the_viewer(rec, home, capsys):
    rec.add(["optional"], accept=["optional"])  # what the viewer does
    rec.run_pending()
    assert records.cmd_setup(rec, setup_args(only=["base"])) == 0
    out = capsys.readouterr().out
    assert "Welcome" not in out and "done      Source base" in out


def test_setup_only_names_the_parts_and_the_air_strike_area(rec, home, capsys):
    rec.save_key(USER, KEY)
    assert records.cmd_setup(rec, setup_args(only=["thor"], area="106,16,107,17")) == 0
    assert ran(home) == [f"thor --bbox 106.0,16.0,107.0,17.0 --max-strikes {records.MAX_STRIKES}"]
    assert records.cmd_setup(rec, setup_args(only=["thor"], area="1,2,3")) == 2
    assert "four numbers" in capsys.readouterr().out
    assert records.cmd_setup(rec, setup_args(only=["optional"], refresh=True)) == 0  # named: no question
    assert ran(home)[-1] == "optional --refresh"


def test_the_command_line_and_the_viewer_share_their_state(rec, home, capsys):
    records.cmd_setup(rec, setup_args(only=["child"]))
    viewer = Records(home, sources=fake_sources())  # what the server would make
    assert viewer.state_of("child") == "added" and viewer.state_of("base") == "added"
    viewer.remove(["child"])
    viewer.run_pending()
    assert rec.state_of("child") == "available"
    assert records.cmd_list(rec, None) == 0
    out = capsys.readouterr().out
    assert "base           added      Source base" in out and "2 records" in out


def test_remove_from_the_command_line_says_what_else_would_go(rec, home, capsys):
    records.cmd_setup(rec, setup_args(only=["child"]))
    capsys.readouterr()
    args = argparse.Namespace(ids=["base"], cascade=False)
    assert records.cmd_remove(rec, args) == 2
    assert "“Source child”" in capsys.readouterr().out and rec.state_of("base") == "added"
    args.cascade = True
    assert records.cmd_remove(rec, args) == 0
    assert "Removed 2 record sources." in capsys.readouterr().out and rec.state_of("base") == "available"


def test_the_first_run_counts_the_standard_set_and_says_how_to_add_the_air_strikes(capsys):
    records.explain_first_run(records.SOURCES)
    out = capsys.readouterr().out
    total = sum(s.size_mb for s in records.SOURCES if s.standard)
    assert f"In all: about {total} MB to download" in out and total < 100
    assert "- Air strikes" not in out and "- Helicopter losses" not in out
    assert "Not included: Air strikes, 320 MB." in out and "python3 start.py setup --only thor --area W,S,E,N" in out
    assert "key" not in out.lower()  # nothing asks for a key up front
    assert '"Add old maps"' in out and "start.py maps add" in out


def test_setup_offers_to_save_a_key_only_after_kaggle_asked_and_then_tries_again(rec, home, capsys, monkeypatch):
    set_plan(home, thor={"wants_key": KEY_WANTED})
    asked = []

    def answer(prompt=""):
        asked.append(prompt)
        set_plan(home, thor={})  # with the key saved, Kaggle hands the file over
        return USER

    # Without a terminal (or with --yes) nobody can be asked: it fails, and says how to give the key.
    assert records.cmd_setup(rec, setup_args(only=["thor"], area="106,16,107,17")) == 1
    out = capsys.readouterr().out
    assert "FAILED    Source thor" in out and KEY_WANTED in out and "enter it in the map viewer" in out
    assert asked == [] and not (home / ".env").exists()

    # On a terminal: the steps, the two questions, the key saved privately, and one more try.
    monkeypatch.setattr(records.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt="": "y" if "ready to paste" in prompt else answer(prompt))
    monkeypatch.setattr(records.getpass, "getpass", lambda prompt="": KEY)
    args = setup_args(only=["thor"], area="106,16,107,17")
    args.yes = False
    assert records.cmd_setup(rec, args) == 0
    out = capsys.readouterr().out
    assert "this time it asked for one" in out and "Saved in .env" in out and "done      Source thor" in out
    assert asked == ["Kaggle username: "] and KEY not in out
    assert rec.key_status()["source"] == "env file" and rec.wanting_key() == []
    assert ran(home)[-1].startswith("thor --bbox 106.0,16.0,107.0,17.0")  # the area asked for survived the retry


def test_first_run_map_sizes_come_from_the_catalogue():
    mb, disk_mb, count, all_gb = records.sheet_sizes()
    assert 5 < mb < 15 and disk_mb > mb and count == 498 and 5 < all_gb < 20


def test_env_files_with_a_bom_or_another_encoding_do_not_break_the_listing(tmp_path):
    path = tmp_path / ".env"
    path.write_bytes(b"\xef\xbb\xbfKAGGLE_USERNAME=sam\r\n")  # Notepad's "UTF-8 with BOM"
    assert records.read_env(path) == {"KAGGLE_USERNAME": "sam"}
    path.write_bytes("KAGGLE_USERNAME=sam\n".encode("utf-16"))
    assert records.read_env(path) == {}
    with pytest.raises(RecordsError, match="not saved as UTF-8"):
        records.write_env(path, {"KAGGLE_KEY": "x"})


def test_counts_are_read_from_a_database_in_an_oddly_named_folder(tmp_path, home):
    path = tmp_path / "my maps #1 ?" / "e.sqlite"
    db = store.connect(path)
    store.upsert_source(db, "thor", name="Thor")
    store.upsert_many(
        db,
        [
            store.Evidence(
                source="thor",
                source_record_id="1",
                kind="air_strike",
                lon=105.6,
                lat=10.4,
                uncertainty_m=50,
                original_coord="x",
                original_datum="wgs84",
                title="strike",
            )
        ],
    )
    db.close()
    rec = Records(home, sources=fake_sources(), db_path=path)
    assert rec.store_facts()["thor"]["records"] == 1
