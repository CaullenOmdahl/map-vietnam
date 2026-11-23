"""The evidence store: validation rules, idempotent upserts, source sync and schema migration."""

import pytest
import store
from store import KINDS, Evidence, connect, upsert_evidence, upsert_source


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "e.sqlite")


def ev(**kw):
    base = dict(
        source="thor",
        source_record_id="1",
        kind="air_strike",
        lon=105.63,
        lat=10.46,
        uncertainty_m=50,
        original_coord="10.46,105.63",
        original_datum="wgs84",
        date_start="1968-02-01",
        title="strike",
    )
    base.update(kw)
    return Evidence(**base)


def test_known_burials_are_not_a_kind():
    assert not {"cemetery", "grave", "burial", "known_burial"} & set(KINDS)
    with pytest.raises(ValueError):
        ev(kind="cemetery")


def test_uncertainty_and_original_are_required():
    with pytest.raises(ValueError):
        ev(uncertainty_m=None)
    with pytest.raises(ValueError):
        ev(uncertainty_m=0)
    with pytest.raises(ValueError):
        ev(original_coord="")


def test_rejects_bad_coordinates_and_dates():
    with pytest.raises(ValueError):
        ev(lat=95)
    with pytest.raises(ValueError):
        ev(date_start="Feb 1968")


def test_upsert_is_idempotent_per_source_record(db):
    upsert_source(db, "thor", name="THOR", url="https://example.org", licence="public domain")
    upsert_evidence(db, ev())
    upsert_evidence(db, ev(title="strike (updated)"))
    rows = db.execute("select title from evidence").fetchall()
    assert rows == [("strike (updated)",)]


def test_evidence_needs_a_known_source(db):
    with pytest.raises(ValueError):
        upsert_evidence(db, ev(source="nope"))


def _ev(rid, **kw):
    return store.Evidence(
        source="s",
        source_record_id=rid,
        kind="placename",
        lon=105.6,
        lat=10.4,
        uncertainty_m=100,
        original_coord="x",
        original_datum="wgs84",
        title=rid,
        **kw,
    )


def test_sync_source_deletes_dropped_records_but_withdraws_ones_in_cases(tmp_path):
    db = store.connect(tmp_path / "e.sqlite")
    store.upsert_source(db, "s", name="S")
    store.sync_source(db, "s", [_ev("a"), _ev("b"), _ev("c")])
    b_id = db.execute("select id from evidence where source_record_id='b'").fetchone()[0]
    cid = db.execute("insert into cases (name, created_at) values ('k', 'now')").lastrowid
    db.execute("insert into case_items (case_id, evidence_id) values (?, ?)", (cid, b_id))
    db.commit()

    kept, deleted, withdrawn = store.sync_source(db, "s", [_ev("a")], reason="no longer listed")
    assert (kept, deleted, withdrawn) == (1, 1, 1)
    rows = dict(db.execute("select source_record_id, withdrawn_reason from evidence").fetchall())
    assert rows == {"a": None, "b": "no longer listed"}
    # The analyst's case link survives.
    assert db.execute("select count(*) from case_items").fetchone()[0] == 1

    # Running the same input twice changes nothing; a record that comes back is un-withdrawn.
    assert store.sync_source(db, "s", [_ev("a"), _ev("b")]) == (2, 0, 0)
    assert db.execute("select withdrawn_at from evidence where source_record_id='b'").fetchone()[0] is None


def test_connect_migrates_an_old_store_without_withdrawn_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.executescript(store.SCHEMA.replace("  withdrawn_at text, withdrawn_reason text,\n", ""))
    old.close()
    cols = {r[1] for r in store.connect(path).execute("pragma table_info(evidence)")}
    assert {"withdrawn_at", "withdrawn_reason"} <= cols


def test_remove_source_takes_everything_out_but_what_is_in_a_case(tmp_path):
    db = store.connect(tmp_path / "e.sqlite")
    for source in ("s", "other"):
        store.upsert_source(db, source, name=source.upper())
    store.sync_source(db, "s", [_ev("a"), _ev("b")])
    db.execute(
        "insert into evidence (source, source_record_id, kind, lon, lat, uncertainty_m, original_coord, "
        "original_datum, title) values ('other', 'a', 'other', 1, 1, 1, '1,1', 'wgs84', 'x')"
    )
    b_id = db.execute("select id from evidence where source = 's' and source_record_id = 'b'").fetchone()[0]
    cid = db.execute("insert into cases (name, created_at) values ('k', 'now')").lastrowid
    db.execute("insert into case_items (case_id, evidence_id) values (?, ?)", (cid, b_id))
    db.commit()

    assert store.remove_source(db, "s", reason="removed by its owner") == (1, 1)
    rows = db.execute("select source, source_record_id, withdrawn_reason from evidence order by source").fetchall()
    assert rows == [("other", "a", None), ("s", "b", "removed by its owner")]  # other sources are never touched
    assert db.execute("select count(*) from case_items").fetchone()[0] == 1  # the analyst's case is whole
    # The source's entry stays while a withdrawn record still refers to it ...
    assert [r[0] for r in db.execute("select id from sources order by id")] == ["other", "s"]
    # ... and goes once nothing does: removing again after the case let go of the record.
    db.execute("delete from case_items")
    db.execute("delete from evidence where id = ?", (b_id,))
    db.commit()
    assert store.remove_source(db, "s") == (0, 0)
    assert [r[0] for r in db.execute("select id from sources")] == ["other"]
    assert store.remove_source(db, "never-there") == (0, 0)
