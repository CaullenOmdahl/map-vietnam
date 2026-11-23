"""Tests for the THOR ingester (scripts/ingest/thor.py): position decoding, scope, sync, and the Kaggle key checks.

Run:  .venv/bin/python -m pytest -q tests/test_ingest_thor.py
"""

import json
import os
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import store  # noqa: E402
import thor  # noqa: E402

# Two real THOR rows near Cao Lanh, as described in the source research (abbreviated).
CACTA = {
    "THOR_DATA_VIET_ID": "405849",
    "MSNDATE": "1967-05-08",
    "MFUNC_DESC_CLASS": "KINETIC",
    "MFUNC_DESC": "AIR INTERDICTION",
    "TGTTYPE": "TROOPS",
    "WEAPONTYPE": "M-39",
    "NUMWEAPONSDELIVERED": "10",
    "VALID_AIRCRAFT_ROOT": "F-100",
    "COUNTRYFLYINGMISSION": "UNITED STATES OF AMERICA",
    "MILSERVICE": "USAF",
    "TGTLATDD_DDD_WGS84": "10.52628822",
    "TGTLONDDD_DDD_WGS84": "105.7191159",
    "TGTORIGCOORDS": "48PWS790636",
    "SOURCERECORD": "CACTA6669",
}
SEADAB = {
    "THOR_DATA_VIET_ID": "5767",
    "MSNDATE": "12/10/1970",
    "MFUNC_DESC_CLASS": "KINETIC",
    "MFUNC_DESC": "CLOSE AIR SUPPORT",
    "TGTLATDD_DDD_WGS84": "10.470555",
    "TGTLONDDD_DDD_WGS84": "105.602222",
    "TGTORIGCOORDS": "102814N1053608E",
    "SOURCERECORD": "SEADAB",
}


def test_mgrs_rows_are_re_derived_on_indian1960():
    e = thor.to_evidence(CACTA)
    assert e.kind == "air_strike" and e.date_start == "1967-05-08"
    assert e.original_coord == "48PWS790636" and e.original_datum.startswith("indian1960")
    assert e.uncertainty_m >= 300
    # Within a few hundred metres of THOR's own (slightly offset) conversion.
    assert abs(e.lon - 105.7191) < 0.006 and abs(e.lat - 10.5263) < 0.003
    assert "unexploded ordnance" in e.description
    assert e.raw["THOR_DATA_VIET_ID"] == "405849"


def test_dms_rows_keep_thor_position_with_wide_uncertainty():
    e = thor.to_evidence(SEADAB)
    assert e.date_start == "1970-12-10"
    assert (e.lon, e.lat) == (105.602222, 10.470555)
    assert e.uncertainty_m == 1000 and e.original_coord == "102814N1053608E"


def test_non_kinetic_and_unplaced_rows_are_dropped():
    assert thor.to_evidence({**CACTA, "MFUNC_DESC_CLASS": "NONKINETIC"}) is None
    assert (
        thor.to_evidence({**SEADAB, "TGTORIGCOORDS": "", "TGTLATDD_DDD_WGS84": "", "TGTLONDDD_DDD_WGS84": ""}) is None
    )


def test_default_bbox_is_the_area_of_interest():
    assert thor.parse_args([]).bbox == ",".join(map(str, common.AREA_OF_INTEREST))


def test_rows_without_a_thor_id_are_skipped():
    assert thor.to_evidence({k: v for k, v in CACTA.items() if k != "THOR_DATA_VIET_ID"}) is None


def test_sync_twice_is_a_no_op_and_only_touches_thor(tmp_path):
    db = store.connect(tmp_path / "e.sqlite")
    store.upsert_source(db, "other", name="other")
    db.execute(
        "insert into evidence (source, source_record_id, kind, lon, lat, uncertainty_m, "
        "original_coord, original_datum, title) values ('other','405849','other',1,1,1,'1,1','wgs84','x')"
    )
    db.commit()
    rows = [CACTA, SEADAB, {**CACTA, "MFUNC_DESC_CLASS": "NONKINETIC", "THOR_DATA_VIET_ID": "1"}]
    bbox = common.AREA_OF_INTEREST
    first = thor.write(db, thor.select(rows, bbox))
    snap = db.execute("select id, source, source_record_id, raw_json from evidence order by id").fetchall()
    second = thor.write(db, thor.select(rows, bbox))
    assert first == second == (2, 0, 0)
    assert db.execute("select id, source, source_record_id, raw_json from evidence order by id").fetchall() == snap
    assert thor.write(db, thor.select([CACTA], bbox)) == (1, 1, 0)
    assert db.execute("select count(*) from evidence where source='other'").fetchone()[0] == 1


# ---- Kaggle key and download ----------------------------------------------------------------
@pytest.fixture
def no_key(tmp_path, monkeypatch):
    """No key anywhere: empty environment, no .env, no kaggle.json, empty raw cache."""
    # A private copy of the environment: load_env writes to os.environ directly.
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for k in ("KAGGLE_USERNAME", "KAGGLE_KEY"):
        os.environ.pop(k, None)
    monkeypatch.setenv("KAGGLE_CONFIG_DIR", str(tmp_path / "kaggle"))
    monkeypatch.setattr(common, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(common, "RAW", tmp_path / "raw")

    def never(url, *a, **k):
        raise AssertionError(f"a test tried to download {url}")

    monkeypatch.setattr(common, "cached_get", never)  # no test ever contacts Kaggle: see fake_kaggle
    return tmp_path


def fake_kaggle(monkeypatch, *answers):
    """Stand in for Kaggle: each download attempt gets the next of `answers`, an HTTP status to
    refuse with or the bytes of the file to hand over. Returns the list of attempts made, each
    the Authorization header sent (None when no key was sent)."""
    attempts, pending = [], list(answers)

    def cached_get(url, source, *, name=None, headers=None, **kw):
        assert url == thor.URL and source == thor.SOURCE and name == thor.ZIP_NAME
        attempts.append((headers or {}).get("Authorization"))
        answer = pending.pop(0)
        if isinstance(answer, int):
            raise urllib.error.HTTPError(url, answer, "refused", {}, None)
        path = common.RAW / source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(answer)
        return path

    monkeypatch.setattr(common, "cached_get", cached_get)
    return attempts


ZIP_BYTES = b"PK\x03\x04 the start of a zip file"
BASIC_U_K = "Basic dTpr"  # base64 of "u:k"


def test_credentials_come_from_env_file_then_kaggle_json(no_key):
    assert thor.kaggle_credentials() is None
    (no_key / "kaggle").mkdir()
    (no_key / "kaggle" / "kaggle.json").write_text(
        json.dumps({"username": "jsonuser", "key": "jsonkey"}), encoding="utf-8"
    )
    assert thor.kaggle_credentials() == ("jsonuser", "jsonkey")
    (no_key / ".env").write_text("KAGGLE_USERNAME=envuser\nKAGGLE_KEY=envkey\n", encoding="utf-8")
    assert thor.kaggle_credentials() == ("envuser", "envkey")


def test_cached_zip_needs_no_key(no_key):
    cached = no_key / "raw" / "thor" / thor.ZIP_NAME
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"zip")
    assert thor.download() == cached


def test_without_a_key_the_file_is_asked_for_without_one(no_key, monkeypatch):
    attempts = fake_kaggle(monkeypatch, ZIP_BYTES)
    path = thor.download()
    assert path == no_key / "raw" / "thor" / thor.ZIP_NAME and path.read_bytes() == ZIP_BYTES
    assert attempts == [None]  # one request, and nothing in it to identify anyone


def test_a_saved_key_is_sent(no_key, monkeypatch):
    (no_key / ".env").write_text("KAGGLE_USERNAME=u\nKAGGLE_KEY=k\n", encoding="utf-8")
    attempts = fake_kaggle(monkeypatch, ZIP_BYTES)
    assert thor.download().read_bytes() == ZIP_BYTES
    assert attempts == [BASIC_U_K]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_does_not_stand_in_the_way_of_a_file_anyone_may_have(no_key, monkeypatch, status):
    (no_key / ".env").write_text("KAGGLE_USERNAME=u\nKAGGLE_KEY=k\n", encoding="utf-8")
    attempts = fake_kaggle(monkeypatch, status, ZIP_BYTES)
    assert thor.download().read_bytes() == ZIP_BYTES
    assert attempts == [BASIC_U_K, None]  # tried once more, without the key


def test_kaggle_asking_for_a_key_when_none_is_saved_ends_with_the_key_status(no_key, monkeypatch, capsys):
    attempts = fake_kaggle(monkeypatch, 401)
    with pytest.raises(SystemExit) as e:
        thor.download()
    assert e.value.code == thor.KEY_EXIT == 3 and attempts == [None]
    said = capsys.readouterr().err
    assert said.startswith(
        "Kaggle asked for a key before it would hand over the air strike records, and none is saved yet."
    )
    assert "Create Legacy API Key" in said and said.count("\n") == 1  # one plain sentence on one line


def test_a_key_refused_and_no_luck_without_it_says_the_key_was_refused(no_key, monkeypatch, capsys):
    (no_key / ".env").write_text("KAGGLE_USERNAME=secret-user\nKAGGLE_KEY=secret-key-value\n", encoding="utf-8")
    attempts = fake_kaggle(monkeypatch, 403, 401)
    with pytest.raises(SystemExit) as e:
        thor.download()
    assert e.value.code == thor.KEY_EXIT and len(attempts) == 2 and attempts[1] is None
    said = capsys.readouterr()
    assert said.err.startswith("Kaggle did not accept this key (it answered 403). Make a new key and enter it again.")
    assert "secret-user" not in said.err + said.out and "secret-key-value" not in said.err + said.out
    assert not (no_key / "raw" / "thor" / thor.ZIP_NAME).exists()


def test_a_sign_in_page_is_never_kept_as_the_file(no_key, monkeypatch, capsys):
    attempts = fake_kaggle(monkeypatch, b"\n<!DOCTYPE html><html>Sign in to Kaggle</html>")
    with pytest.raises(SystemExit) as e:
        thor.download()
    assert e.value.code == thor.KEY_EXIT and attempts == [None]
    assert capsys.readouterr().err.startswith("Kaggle asked for a key")
    assert not (no_key / "raw" / "thor" / thor.ZIP_NAME).exists()  # the next try downloads afresh


def test_other_download_failures_are_not_mistaken_for_a_missing_key(no_key, monkeypatch):
    fake_kaggle(monkeypatch, 503)
    with pytest.raises(urllib.error.HTTPError):
        thor.download()


def test_the_messages_are_one_line_each_and_point_to_the_viewer_first():
    for text in (thor.KEY_WANTED, thor.KEY_REFUSED, thor.TOO_MANY):
        assert "\n" not in text  # the viewer shows an ingester's last line as its error
    assert thor.KEY_HELP.index("where Map Vietnam asks for the key") < thor.KEY_HELP.index(".env")


# ---- progress and the strike limit ----------------------------------------------------------
def write_csv(path, rows):
    """A THOR-shaped CSV file of `rows` (dicts)."""
    import csv

    fields = sorted({k for row in rows for k in row})
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_reading_the_file_reports_rows_and_the_share_read(tmp_path, monkeypatch, capsys):
    import zipfile

    rows = [{**CACTA, "THOR_DATA_VIET_ID": str(i)} for i in range(25)]
    csv_path = write_csv(tmp_path / "thor.csv", rows)
    zip_path = tmp_path / "thor.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.write(csv_path, "THOR_Vietnam_Bombing_Operations.csv")
    monkeypatch.setattr(thor, "ROWS_PER_REPORT", 10)
    monkeypatch.setenv(common.PROGRESS_ENV, "1")
    for path in (csv_path, zip_path):  # zipped or not, the same rows and the same reports
        assert [r["THOR_DATA_VIET_ID"] for r in thor.open_rows(path)] == [str(i) for i in range(25)]
        lines = capsys.readouterr().out.splitlines()
        assert [line.split(" ", 2)[2] for line in lines] == [
            "Reading the air strike file: 10 rows so far",
            "Reading the air strike file: 20 rows so far",
        ]
        assert all(0 < float(line.split()[1]) <= 1 for line in lines)


@pytest.fixture
def thor_home(tmp_path, monkeypatch):
    """A cached THOR file of three strikes near Cao Lanh, and a store, both under tmp_path."""
    real_connect = store.connect
    monkeypatch.setattr(common, "RAW", tmp_path / "raw")
    monkeypatch.setattr(common, "cached_get", lambda url, *a, **k: pytest.fail(f"a test tried to download {url}"))
    monkeypatch.setattr(store, "connect", lambda path=tmp_path / "e.sqlite": real_connect(path))
    # thor.main() syncs the store it opens: make sure that can only ever be the temporary one.
    with store.connect() as db:
        assert Path(db.execute("pragma database_list").fetchone()[2]).parent.samefile(tmp_path)
    (tmp_path / "raw" / "thor").mkdir(parents=True)
    write_csv(tmp_path / "raw" / "thor" / thor.ZIP_NAME, [{**CACTA, "THOR_DATA_VIET_ID": str(i)} for i in range(3)])
    return tmp_path


def test_an_area_with_more_strikes_than_the_limit_changes_nothing(thor_home, capsys):
    assert thor.main(["--max-strikes", "3"]) == (3, 0, 0)  # at the limit is fine
    with pytest.raises(
        SystemExit, match="more than 2 air strikes, too many for the map to stay quick. Nothing was changed"
    ):
        thor.main(["--max-strikes", "2"])
    db = store.connect(thor_home / "e.sqlite")
    assert db.execute("select count(*) from evidence where source = 'thor'").fetchone()[0] == 3
    assert thor.main([]) == (3, 0, 0)  # without the option there is no limit
