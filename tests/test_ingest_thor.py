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
    return tmp_path


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


def test_missing_key_explains_how_to_get_one(no_key):
    with pytest.raises(SystemExit, match="Create Legacy API Key"):
        thor.download()


def test_refused_key_is_explained(no_key, monkeypatch):
    (no_key / ".env").write_text("KAGGLE_USERNAME=u\nKAGGLE_KEY=bad\n", encoding="utf-8")

    def refuse(url, *a, headers=None, **k):
        assert headers["Authorization"].startswith("Basic ")
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(common, "cached_get", refuse)
    with pytest.raises(SystemExit, match="refused the key"):
        thor.download()
