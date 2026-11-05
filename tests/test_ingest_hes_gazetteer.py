"""Parsing tests for the HES gazetteer ingester on tiny real excerpts of the NARA files."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "ingest"))
import hes_gazetteer as h  # noqa: E402
import store  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "hes_gazetteer"


@pytest.fixture(scope="module")
def by_usid():
    return h.load_all(FIX)


@pytest.fixture(scope="module")
def built(by_usid):
    records, stats, located = h.build(by_usid)
    return {e.source_record_id: e for e in records}, stats, located


def test_fixed_width_records_split_on_length_not_newlines():
    recs = h.read_records(FIX / "RG349.NPIASS.GZSF.txt", 162)
    assert all(len(r) == 160 for r in recs)
    headers = [r for r in recs if r[0] in "01"]  # region/district header records are not hamlets
    assert headers and all(h.parse_vci(r) is None for r in headers)
    hvgz = h.read_records(FIX / "RG349.NPIASS.HVGZ.txt", 162)
    assert hvgz[0].startswith("0") and h.parse_vci(hvgz[0]) is None  # carries stray binary bytes


def test_gzsf_hamlet_fields(by_usid):
    r = by_usid["432010901"]["GZSF"]
    assert r["utm"] == "WS621625" and r["population"] == "02257" and r["rating"] == "B"
    assert (r["gvn_province"], r["gvn_district"], r["gvn_hamlet"]) == ("KIEN PHONG", "CAO LANH", "PHONG DINH")
    assert (r["vc_province"], r["vc_district"], r["vc_village"]) == ("CAO LANH", "CAO LANH NORTH", "PHONG MY")


def test_hgaz_hamlet_fields_and_village_records_skipped(by_usid):
    r = by_usid["432010901"]["HGAZ"]
    assert r["utm"] == "WS621625" and r["gvn_village"] == "PHONG MY" and r["admin_id"] == "432010900"
    assert "432010900" not in by_usid  # village record
    assert "000000000" not in by_usid  # date record
    assert h.hgaz_date(h.read_records(FIX / "RG349.NPIASS.HGAZ", 152)) == "1973-01"


def test_merged_evidence_keeps_original_grid_and_both_name_schemes(built):
    recs, _, _ = built
    e = recs["432010901"]
    assert e.original_coord == "WS621625" and e.original_datum == "indian1960"
    assert e.kind == "placename" and e.uncertainty_m == 500  # calibration resolves only ~0.5-1 km
    assert "Indian 1960" in e.description and "assumed" not in e.description
    assert e.lon == pytest.approx(105.564, abs=0.002) and e.lat == pytest.approx(10.519, abs=0.002)
    assert "village PHONG MY" in e.description and "CAO LANH NORTH" in e.description
    assert (e.date_start, e.date_end) == ("1972", "1973-01")
    assert e.raw["zone_choice"]["zone"] == "48P"
    assert sorted(e.raw["files"]) == ["GZSF", "HGAZ", "HVGZ"]


def test_files_disagreeing_on_grid_give_one_record_per_grid(built):
    recs, _, _ = built
    ids = sorted(k for k in recs if k.startswith("101011001"))
    assert ids == ["101011001:YD462404", "101011001:YD464410"]
    assert recs["101011001:YD462404"].raw["grid_files"] == ["HGAZ"]


def test_band_ambiguity_is_resolved_and_recorded(built):
    recs, _, _ = built
    e = recs["101011001:YD464410"]  # Quang Tri, near 16N: fits 48Q only after band check
    assert 16 < e.lat < 17 and e.raw["zone_choice"]["zone"].startswith("48")


def test_hamlet_named_tomboi_is_not_mistaken_for_a_burial(built):
    recs, stats, _ = built
    assert "206021705" in recs and not stats.get("dropped: formal burial name")
    assert h.BURIAL_RE.search("NGHIA TRANG LIET SI") and not h.BURIAL_RE.search("DAK TOMBOI")


def test_province_summary(built):
    _, _, located = built
    out = h.provinces(located)
    kp = out["provinces"]["432"]
    assert kp["names"] == ["KIEN PHONG"] and kp["hamlet_count"] == 2
    assert kp["radius_m"] >= 1000 and 105.5 < kp["lon"] < 105.7
    assert "CAO LANH" in out["vc_provinces"]


def test_province_radius_covers_every_hamlet_plus_buffer(built):
    """Upland hamlets (A Shau, Khe Sanh) must fall inside their province circle: the radius is the
    farthest hamlet + 10 km, not a percentile that the coastal clusters dominate."""
    _, _, located = built
    out = h.provinces(located)
    for code, p in out["provinces"].items():
        pts = [(lon, lat) for u, lon, lat, _ in located if u[:3] == code]
        far = max(h.dist_m(p["lon"], p["lat"], *q) for q in pts)
        assert p["radius_m"] == round(far + h.AREA_BUFFER_M), code
        assert p["radius_m"] >= p["radius_p90_m"] + h.AREA_BUFFER_M
        for d in p["districts"].values():
            assert d["radius_m"] >= d["max_hamlet_m"] + h.AREA_BUFFER_M - 1


def test_sync_twice_is_a_no_op(built, tmp_path):
    recs, _, _ = built
    db = store.connect(tmp_path / "e.sqlite")
    assert h.save(db, list(recs.values())) == (len(recs), 0, 0)

    def dump():
        return db.execute("select * from evidence order by source_record_id").fetchall()

    first = dump()
    assert h.save(db, list(recs.values())) == (len(recs), 0, 0)
    assert dump() == first


def test_province_extent_uses_gns_places_of_the_modern_province(built, monkeypatch):
    """Uplands without 1972-73 hamlets (Khe Sanh, Lang Vei for this tiny fixture) are still inside
    the province circle: GNS places of the province's dominant modern province count too.
    Offshore islands (Con Son's Ben Dam, VN-43) and places north of the Ben Hai (Vinh Linh) do not."""
    _, _, located = built
    places = h.gns_places(FIX / "gns_subset.json")
    assert not any(name == "Vĩnh Linh" for *_, name in places)  # north of the DMZ line
    monkeypatch.setattr(h, "ISLAND_KM", 100.0)  # the fixture has 6 hamlets, not 16,000
    out = h.provinces(located, places)["provinces"]
    qt = out["101"]
    for lon, lat in [(106.73884, 16.628248), (106.683333, 16.6)]:  # Khe Sanh, Lang Vei
        assert h.dist_m(qt["lon"], qt["lat"], lon, lat) + h.AREA_BUFFER_M <= qt["radius_m"] + 1
    assert qt["max_gns_place_m"] > qt["max_hamlet_m"] and "VN-25" in qt["farthest_gns_place"]
    kp = out["432"]  # Kien Phong: Hong Ngu / Cao Lanh (VN-45) count, Ben Dam (VN-43 island) not
    assert "VN-43" not in (kp.get("farthest_gns_place") or "")
    assert all("Bến Đầm" not in (p.get("farthest_gns_place") or "") for p in out.values())
    # Kham Duc (VN-27) belongs to no fixture province's modern province, so it is not used.
    assert all("Khâm Đức" not in (p.get("farthest_gns_place") or "") for p in out.values())
    # Without GNS the radius falls back to the farthest hamlet + buffer.
    assert h.provinces(located)["provinces"]["101"]["radius_m"] == qt["max_hamlet_m"] + h.AREA_BUFFER_M
