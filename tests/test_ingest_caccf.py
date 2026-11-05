"""Parsing tests for the CACCF ingester.

Inputs: tests/fixtures/caccf/CAC.DEC98.excerpt (8 real records from CAC.DEC98.CURRENT, service
numbers replaced by synthetic digits) and tests/fixtures/caccf/hes_provinces.json (a trimmed copy
of the HES province circles), so the suite needs nothing under data/.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import caccf  # noqa: E402
import store  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "caccf" / "CAC.DEC98.excerpt"
HES_FIX = ROOT / "tests" / "fixtures" / "caccf" / "hes_provinces.json"
HES = json.loads(HES_FIX.read_text(encoding="utf-8"))["provinces"]


def _places():
    return caccf.load_places(HES_FIX)


def _build():
    lines = caccf.read_lines(FIX.read_bytes())
    return lines, caccf.build(lines, _places())


def _by_name(records):
    return {r.raw["name"]: r for r in records}


def test_fixed_width_fields():
    lines, _ = _build()
    assert len(lines) == 8 and all(len(ln) == 164 for ln in lines)
    p = caccf.parse_line(lines[0])
    assert p["name"] == "REARDON RICHARD JOHN"
    assert (p["service"], p["country"], p["casualty_type"]) == ("N", "VS", "A1")
    assert (p["grade"], p["death_date"], p["tour_began"]) == ("LTJG", "04/28/69", "690428")
    assert (p["body"], p["province"], p["corps"]) == ("BNR", "28", "04")


def test_only_bnr_ingested_and_skips_counted():
    _, (records, skipped, burials, _) = _build()
    names = set(_by_name(records))
    assert "CRAMER HARRY GRIFFITH" not in names  # body recovered
    assert skipped["body recovered (not BNR)"] == 1
    assert skipped["outside Vietnam (Laos)"] == 1  # WEITKAMP
    assert skipped["at sea (Offshore, MR 1)"] == 1  # MASON
    assert burials == 0
    assert len(records) == 5


def test_province_centroid_from_hes():
    _, (records, *_) = _build()
    r = _by_name(records)["REARDON RICHARD JOHN"]
    assert r.original_coord == "province code 28 (Kien Tuong)"
    assert r.original_datum == "province-centroid"
    assert r.kind == "loss_incident"
    assert r.title == "BNR: LTJG REARDON RICHARD JOHN, US Navy, 1969-04-28"
    assert r.date_start == "1969-04-28"
    assert abs(r.lon - 105.956) < 0.01 and abs(r.lat - 10.732) < 0.01  # HES 431 KIEN TUONG
    # The full covering radius of the province (farthest point + 10 km), not a percentile.
    assert r.uncertainty_m == HES["431"]["radius_m"]
    assert r.uncertainty_m >= HES["431"]["max_hamlet_m"] + 10_000
    s = _by_name(records)["SCHUMANN JOHN ROBERT"]
    assert s.original_coord == "province code 30 (Dinh Tuong)"  # 30, not 38 (Ba Xuyen)


def test_region_and_country_fallbacks():
    _, (records, *_) = _build()
    by = _by_name(records)
    g = by["GORTON THOMAS FREDERICK"]
    assert g.original_datum == "region-centroid" and g.uncertainty_m > 100_000
    a = by["ANSPACH ROBERT ALLEN"]
    assert a.original_coord == 'country VS (South Vietnam), province code "99" (unknown)'
    assert a.original_datum == "country-centroid" and a.uncertainty_m > 400_000
    assert a.date_start == "1967-09-11"


def test_country_overrides_conflicting_province():
    _, (records, *_) = _build()
    h = _by_name(records)["HOLT ROBERT ALAN"]
    assert h.original_datum == "country-centroid"
    assert "North Vietnam" in h.original_coord and "conflicts" in h.original_coord
    assert h.raw["province"] == "01"


def test_incident_date_prefers_tour_field_when_earlier():
    line = (
        "AVSC3 7140 TERRY ORAL RAY              7411999999999SSGTE611/05/74MASCOUTAH"
        "           1461B3001/27/45Z7C72 *SM106805031BNR29Y                         "
        "      30041 X "
    )
    assert len(line) == 164
    (r,), *_ = caccf.build([line], _places())
    assert r.date_start == "1968-05-03"
    assert "1974-11-05" in r.description


def test_province_centroid_radius_covers_upland_places():
    """Quang Tri (01) and Quang Tin (04) centroids must cover Khe Sanh and Kham Duc."""
    places = _places()
    for code, (lon, lat) in (("01", (106.739, 16.628)), ("04", (107.793, 15.445))):
        plon, plat, r, how = places[code]
        assert caccf._dist_m(plon, plat, lon, lat) < r, (code, how)


def test_service_number_not_in_raw():
    """Post-1969 Army service numbers are SSNs: only their hash (in the id) is kept."""
    lines, (records, *_) = _build()
    for r in records:
        assert "service_number" not in r.raw
        sn = caccf.parse_line(next(ln for ln in lines if r.raw["name"] in ln))["service_number"]
        assert sn and sn not in json.dumps(r.raw) and sn not in r.description + r.title
        assert sn not in r.source_record_id


def test_sync_twice_is_a_no_op(tmp_path):
    _, (records, *_) = _build()
    db = store.connect(tmp_path / "e.sqlite")
    assert caccf.save(db, records) == (len(records), 0, 0)

    def dump():
        return db.execute("select * from evidence order by source_record_id").fetchall()

    first = dump()
    assert caccf.save(db, records) == (len(records), 0, 0)
    assert dump() == first
    # A record the file no longer yields is deleted by sync_source, not by the ingester.
    assert caccf.save(db, records[1:]) == (len(records) - 1, 1, 0)


def test_record_ids_unique_and_stable():
    _, (records, *_) = _build()
    ids = [r.source_record_id for r in records]
    assert len(set(ids)) == len(ids)
    assert caccf.record_id(caccf.parse_line(FIX.read_bytes().decode("latin-1").split("\r\n")[0])) == ids[0]
