"""Parsing tests for the GNS ingester on a tiny real excerpt of the pinned Vietnam.txt."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import gns  # noqa: E402
import store  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "gns" / "vietnam_excerpt.txt"


def _build():
    rows = gns.read_rows(FIXTURE.read_text(encoding="utf-8"))
    return rows, *gns.build(rows)


def test_duplicate_rows_collapsed():
    rows, *_ = _build()
    assert sum(r["uni"] == "-4962255" for r in rows) == 1


def test_cao_lanh_record():
    _, records, _, _ = _build()
    rec = {r.source_record_id: r for r in records}["-3709962/-4930626"]
    assert rec.title == "Cao Lãnh"
    assert rec.kind == "placename"
    assert rec.original_coord == "10.460171, 105.632944"
    assert rec.original_datum == "wgs84"
    assert (rec.lon, rec.lat) == (105.632944, 10.460171)
    assert 100 <= rec.uncertainty_m <= 400  # second-level PPL
    assert "Đồng Tháp" in rec.description  # adm1 name resolved from the ADM1 row
    assert rec.raw["mgrs"] == "48PWS6926956361"


def test_variant_kept_and_linked_to_approved_name():
    _, records, _, _ = _build()
    rec = {r.source_record_id: r for r in records}["-3702823/-4921201"]
    assert rec.title == "Ap Hung Pe" and rec.raw["nt"] == "V"
    assert "approved name: Ấp Hưng Đễ" in rec.description


def test_minute_rounded_gets_km_uncertainty_and_linear_note():
    _, records, _, _ = _build()
    rec = {r.source_record_id: r for r in records}["-3704554/-4923100"]  # Kinh Ba Càng, 10:12:00
    assert rec.raw["_minute_rounded"] is True
    assert rec.uncertainty_m > 3000
    assert "linear feature" in rec.description


def test_burials_dropped_but_mine_kept():
    _, records, _, stats = _build()
    titles = {r.title for r in records}
    assert "Nghĩa Trang Liệt Sĩ Trường Sơn" not in titles
    assert "Mộ Nhà Thơ Phạm Văn Trị" not in titles  # a named grave filed as a shrine
    assert "Mỏ Than Nông Sơn" in titles  # coal mine: "mỏ", not "mộ"
    assert stats["burials_dropped"] == 2


def test_military_and_undersea():
    _, records, _, stats = _build()
    by_title = {r.title: r for r in records}
    assert by_title["Vinh Long"].kind == "military_site"  # AIRQ abandoned airfield
    assert "Rocher Yusun" not in by_title and stats["skipped_fc"] == 1


def test_index_keys():
    _, _, index, _ = _build()
    assert gns.normalise("Ấp Hưng Đễ") == "ap hung de"
    assert index["cao lanh"][0]["dsg"] == "PPL"
    assert index["hung de"][0]["ufi"] == "-3702823"  # 'Ấp' prefix stripped alias
    assert index["ap hung pe"][0]["adm1"] == "VN-46"
    assert "nghia trang liet si truong son" not in index


def test_burial_name_terms_drop_only_graves():
    """Real rows that merely contain mả/mộ/lăng (village, dam, station, mountain) stay."""
    rows, records, _, stats = _build()
    titles = {r.title for r in records}
    for kept in ("Làng Mả", "Đập Mả Tổ", "Ga Mộ Dức", "Ga Lăng Cô", "Núi Ga Lăng", "Mỏ Than Nông Sơn"):
        assert kept in titles, kept
    # No real non-dropped S/L row in GNS starts with these terms, so check the rule on copies of
    # a real S-class row (the coal mine) renamed the way a grave/cemetery entry would be.
    mine = next(r for r in rows if r["full_name"] == "Mỏ Than Nông Sơn")
    for name in (
        "Nghĩa Địa Bến Tre",
        "Khu Nghĩa Trang Xã",
        "Mả Ông Cả",
        "Mộ Cụ Nguyễn",
        "Lăng Ông Bà Chiểu",
        "Lăng Mộ Họ Trần",
    ):
        assert gns.is_burial({**mine, "full_name": name}), name
    # A locality (L) called "Lăng ..." is a place, not a tomb; a village (P) "Mộ Đức" likewise.
    loc = next(r for r in rows if r["fc"] == "P")
    assert not gns.is_burial({**mine, "fc": "L", "desig_cd": "LCTY", "full_name": "Lăng Cô"})
    assert not gns.is_burial({**loc, "full_name": "Mộ Đức"})
    assert not gns.is_burial({**mine, "full_name": "Đập Mả Tổ"})  # dam named after a place


def test_records_carry_the_pinned_file_url():
    _, records, _, _ = _build()
    assert records and all(r.url == gns.URL for r in records)
    assert gns.URL == (
        "http://web.archive.org/web/20260903061555id_/https://geonames.nga.mil/geonames/GNSData/Vietnam.zip"
    )


def test_sync_twice_is_a_no_op_and_only_touches_gns(tmp_path):
    db = store.connect(tmp_path / "e.sqlite")
    store.upsert_source(db, "other", name="other")
    db.execute(
        "insert into evidence (source, source_record_id, kind, lon, lat, uncertainty_m, "
        "original_coord, original_datum, title) values ('other','x','other',1,1,1,'1,1','wgs84','x')"
    )
    db.commit()
    _, records, _, _ = _build()
    first = gns.write(db, records)
    snap = db.execute("select id, source_record_id, raw_json, url from evidence order by id").fetchall()
    second = gns.write(db, records)
    assert first == second == (len(records), 0, 0)
    assert db.execute("select id, source_record_id, raw_json, url from evidence order by id").fetchall() == snap
    # Dropping a record from the source removes it from gns only.
    assert gns.write(db, records[1:]) == (len(records) - 1, 1, 0)
    assert db.execute("select count(*) from evidence where source='other'").fetchone()[0] == 1
