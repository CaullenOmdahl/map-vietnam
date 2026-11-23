"""Tests for the VHPA ingester (scripts/ingest/vhpa.py): parsing, location checks, kind of loss, crawl and sync.

The pages in tests/fixtures/vhpa/ are synthetic stand-ins that copy the markup and the coded facts the parser
reads (dates, tails, grids, status codes, page ids); names are fictional and all free text is newly written to
trigger the same parser behaviour (see the README there).

The scope checks (is a loss inside Vietnam, how far from its border) read committed excerpts instead of the
locally built files: tests/fixtures/vhpa/gns_places_excerpt.json for data/work/gns_index.json and
tests/fixtures/wikidata_osm/vn_boundary.json for data/raw/osm/vn_boundary.json. The optional HES province disks
(data/work/hes_provinces.json) are switched off, so results do not depend on what the machine has built.

Run:  .venv/bin/python -m pytest -q tests/test_ingest_vhpa.py
"""

import json
import re
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import store  # noqa: E402
import vhpa  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "vhpa"


@pytest.fixture(autouse=True)
def committed_scope_data(monkeypatch, tmp_path):
    """Point vhpa at the committed GNS and boundary excerpts, with no HES disks, and clear its caches."""
    monkeypatch.setattr(vhpa, "GNS_INDEX", FIX / "gns_places_excerpt.json")
    monkeypatch.setattr(vhpa, "VN_BOUNDARY", FIX.parent / "wikidata_osm" / "vn_boundary.json")
    monkeypatch.setattr(vhpa, "HES_PROVINCES", tmp_path / "no_hes_provinces.json")
    for cache, empty in (("_GNS", None), ("_VNB", None), ("_HES", None), ("_LAND_SEGS", {})):
        monkeypatch.setattr(vhpa, cache, empty)


def page(name):
    """The text of one fixture page (the site serves cp1252)."""
    return (FIX / name).read_text(encoding="cp1252")


def test_panel_links():
    inc, ppl = vhpa.parse_panel(page("PANEL28E_excerpt.HTM"))
    assert inc == ["671019101ACD", "67102025KIA", "671022071ACD"]
    assert ppl == ["K10168", "K11903", "K11942"]


def test_incident_fields_and_bnr():
    d = vhpa.parse_incident(page("07838ACD.HTM"))
    assert (d["aircraft_type"], d["tail"]) == ("CH-37B", "55-00627")
    assert d["date"] == "1963-12-12"
    assert d["refno"] == "0026"
    assert d["unit"] == "61 TR CO"
    assert d["country"] == "South Vietnam"
    assert d["grid"] == "WS910400" and d["grid_zone_hint"] == "48P"
    status = {c["name"]: c["status"] for c in d["crew"]}
    assert status["PAPATON QUEBECTON ROMEOTON"] == "BNR"
    assert status["INDIATON JULIETTTON KILOTON"] == "KIA"
    assert status["SIERRATON TANGOTON"] == ""  # survivor: no status code
    assert d["crew"][0]["person_page"] == "K12180"


def test_incident_record_in_aoi():
    inc = vhpa.parse_incident(page("07838ACD.HTM"))
    e, why, info = vhpa.build_record("07838ACD", "u", inc, [])
    assert why is None and info["bnr"]
    assert e.kind == "aircraft_crash"
    assert e.original_coord == "WS910400"
    assert e.original_datum == "indian1960"
    assert "confirmed by calibration" in e.description
    assert common.in_bbox(e.lon, e.lat)
    assert e.lat == pytest.approx(10.315, abs=0.01) and e.lon == pytest.approx(105.83, abs=0.01)
    assert 500 <= e.uncertainty_m < 1000  # calibration resolves the datum only to ~0.5-1 km
    assert e.title.startswith("[BNR x1, REFNO 0026]")
    assert "PAPATON" in e.description and "killed" in e.description
    assert "SYNOPSIS" not in e.description  # no narrative text copied


def test_kia_incident_summary_kept_short():
    inc = vhpa.parse_incident(page("68051777KIA.HTM"))
    assert inc["incident_number"] == "68051777.KIA"
    assert inc["summary"].startswith("Aircraft shot down")
    e, _, _ = vhpa.build_record("68051777KIA", "u", inc, [])
    assert "4 killed (crew: 4 KIA)" in e.description and "BNR" not in e.title


def test_person_page_province_and_burial_flag():
    p = vhpa.parse_person(page("K00001.HTM"))
    assert p["location"].startswith("Gia Dinh Province")
    assert vhpa.norm_province(p["location"]) == "gia dinh"
    assert p["country"] == "South Vietnam"
    assert p["burial_recorded"] is True
    assert p["date"] == "1969-09-13"


def _person(name, rid):
    p = vhpa.parse_person(page(name))
    p["_id"] = rid
    return p


def _person_only(p):
    stand_in = {
        "aircraft_type": p["aircraft_type"],
        "tail": p["tail"],
        "date": p["date"],
        "unit": p["unit"],
        "summary": p["short_summary"],
        "crew": [],
        "grid": p["grid"],
        "country": p["country"],
        "_person_only": True,
    }
    return vhpa.build_record(p["_id"], "u", stand_in, [p])


def _covers_province(e, prov):
    """The circle reaches every point of the named province's disk."""
    lon, lat, r_km = vhpa.province_geometry(prov)
    return vhpa.km(e.lon, e.lat, lon, lat) + r_km <= e.uncertainty_m / 1000


def test_grid_province_mismatch_flagged():
    """K12228: Vinh Long province, but grid YD761215 is in I Corps."""
    p = _person("K12228.HTM", "K12228")
    assert p["grid"] == "YD761215" and vhpa.norm_province(p["location"]) == "vinh long"
    e, why, info = _person_only(p)
    assert why is None and info["check"] == "province_mismatch"
    assert "grid/province mismatch" in e.description
    assert e.lat > 16  # stored where the grid says, flagged rather than moved
    assert _covers_province(e, "vinh long")
    assert "grid and province disagree by" in e.description


@pytest.mark.parametrize(
    "make, prov, min_km",
    [
        # 70012626KIA: grid WS948515 vs Long Khanh (pilot page K11346), ~120 km apart.
        (
            lambda: vhpa.build_record(
                "70012626KIA", "u", vhpa.parse_incident(page("70012626KIA.HTM")), [_person("K11346.HTM", "K11346")]
            ),
            "long khanh",
            100,
        ),
        # K11427: ground casualty, Pleiku province, grid YT990405 ~220 km away.
        (lambda: _person_only(_person("K11427.HTM", "K11427")), "pleiku", 200),
    ],
)
def test_far_mismatch_radius_covers_grid_and_province(make, prov, min_km):
    e, why, info = make()
    assert why is None and info["check"] == "province_mismatch"
    lon, lat, r_km = vhpa.province_geometry(prov)
    d = vhpa.km(e.lon, e.lat, lon, lat)
    assert d > min_km
    assert e.uncertainty_m / 1000 >= d + r_km  # not capped at 10 km any more
    assert _covers_province(e, prov)
    assert f"grid and province disagree by {d:.0f} km" in e.description
    assert e.original_datum == "indian1960"


def test_latlon_fallback_labelled_honestly():
    """691124241ACD has no UTM grid line; its only position is the 'Loss Coordinates' line of the
    REFNO synopsis quoted on the page, which gives DMS lat/lon plus a grid in brackets."""
    inc = vhpa.parse_incident(page("691124241ACD.HTM"))
    assert inc["grid"] is None and inc["loss_coordinates"] == "111445N 1060714E (XT223433)"
    e, why, info = vhpa.build_record("691124241ACD", "u", inc, [])
    assert why is None
    assert e.original_coord == "111445N 1060714E (XT223433)"
    assert e.original_datum.startswith("unknown")
    assert "REFNO synopsis" in e.description and "POW-Network" not in e.description
    assert "XT223433" in e.description and "agree" in e.description
    assert e.lat == pytest.approx(11.2458, abs=1e-3) and e.lon == pytest.approx(106.1206, abs=1e-3)
    assert e.uncertainty_m >= 2000
    assert e.raw["grid_from"].startswith("REFNO synopsis")


def test_every_named_province_is_covered_not_just_one():
    """680426151ACD: grid ZA186533 fits Pleiku (K10466) but K10246 names Phuoc Tuy, ~350 km
    away. Passing on 'fits any province' hid that; the radius must cover both."""
    inc = vhpa.parse_incident(page("680426151ACD.HTM"))
    ps = [_person("K10246.HTM", "K10246"), _person("K10466.HTM", "K10466")]
    assert {vhpa.norm_province(p["location"]) for p in ps} == {"pleiku", "phuoc tuy"}
    e, why, info = vhpa.build_record("680426151ACD", "u", inc, ps)
    assert why is None and info["check"] == "province_mismatch"
    assert _covers_province(e, "phuoc tuy") and _covers_province(e, "pleiku")
    lon, lat, _ = vhpa.province_geometry("phuoc tuy")
    d = vhpa.km(e.lon, e.lat, lon, lat)
    assert d > 250
    assert f"grid and province disagree by {d:.0f} km (Phuoc Tuy" in e.description
    assert "grid/province mismatch" in e.raw["flags"]


def test_other_grids_from_person_pages_are_covered():
    """71033143KIA: incident grid YB028185, pilot page K11647 gives YB935188 (~90 km east).
    The radius must reach the other grid the source names."""
    inc = vhpa.parse_incident(page("71033143KIA.HTM"))
    p = _person("K11647.HTM", "K11647")
    assert inc["grid"] == "YB028185" and p["grid"] == "YB935188"
    e, why, info = vhpa.build_record("71033143KIA", "u", inc, [p])
    assert why is None
    assert e.original_coord == "YB028185"
    other = vhpa.locate("YB935188", ["kontum"], "South Vietnam")
    d = vhpa.km(e.lon, e.lat, other["lon"], other["lat"])
    assert d > 50
    assert e.uncertainty_m >= d * 1000 + other["square_r"]
    assert "grid/grid disagreement" in e.raw["flags"]
    assert f"YB935188 ({d:.0f} km away" in e.description
    assert e.raw["other_grids"][0]["grid"] == "YB935188"


def test_agreeing_person_grid_does_not_widen():
    inc = vhpa.parse_incident(page("71033143KIA.HTM"))
    p = _person("K11647.HTM", "K11647")
    p["grid"] = "YB028186"  # 100 m off: inside the circle already
    e, _, _ = vhpa.build_record("71033143KIA", "u", inc, [p])
    assert "grid/grid disagreement" not in e.raw["flags"]
    # YB028185 lies outside Kontum, so the province already widens the circle; the agreeing
    # grid adds nothing beyond reaching its own square.
    alone, _, _ = vhpa.build_record("71033143KIA", "u", inc, [dict(p, grid=None)])
    assert alone.uncertainty_m <= e.uncertainty_m <= alone.uncertainty_m + 700
    assert "agree within ~1 km" in e.description


def test_person_page_of_another_incident_is_not_used():
    """71033143KIA links a survivor to K11035, which is the page of a different man
    lost in 67090113KIA (Vinh Long, grid XR430600). Its province/grid say nothing about this
    crash, so it is not used for location, and the description says why."""
    inc_pages = {"71033143KIA": page("71033143KIA.HTM")}
    person_pages = {k: page(k + ".HTM") for k in ("K11647", "K11035")}
    records, _, _ = vhpa.build_all(inc_pages, person_pages)
    by_id = {e.source_record_id: e for e in records}
    e = by_id["71033143KIA"]
    assert e.raw["person_pages"] == ["K11647"]
    assert e.raw["foreign_person_pages"] == {"K11035": "67090113KIA"}
    assert "XR430600" not in e.raw["person_grids"] and "Vinh Long" not in e.description
    assert "K11035" in e.description and "67090113KIA" in e.description
    # K11035 still gets its own (person-page-only) record for its own loss, under the id of the
    # incident it links (67090113KIA, not in this test's pages).
    assert by_id["67090113KIA"].original_coord == "XR430600"
    assert by_id["67090113KIA"].raw["person_pages"] == ["K11035"]


def test_refno_wording_does_not_claim_unresolved():
    inc = vhpa.parse_incident(page("691124241ACD.HTM"))
    e, _, _ = vhpa.build_record("691124241ACD", "u", inc, [])
    assert "unresolved" not in e.description
    assert "MIA-POW file reference number" in e.description
    assert "remains recovered" in e.description  # crew code RR is surfaced


def test_no_grid_is_skipped():
    inc = vhpa.parse_incident(page("68051777KIA.HTM"))
    inc["grid"] = None
    e, why, _ = vhpa.build_record("x", "u", inc, [{"_id": "K1", "location": "Sa Dec Province"}])
    assert e is None and why == "no_grid_province_only"


def test_candidate_choice_uses_province():
    loc = vhpa.locate("WS910400", province="dinh tuong")
    assert loc["zone"] == "48P" and loc["check"] == "province_ok"
    # Several named provinces: the candidate is chosen by the closest one, but EVERY named
    # province is checked - one far away is a mismatch even if another fits.
    loc = vhpa.locate("YD761215", province=["vinh long", "thua thien"])
    assert loc["check"] == "province_mismatch" and loc["checked_province"] == "thua thien"
    assert loc["mismatched_provinces"] == ["vinh long"]


def test_loss_coordinates_parse():
    lon, lat = vhpa.parse_loss_coordinates("161349N 1074301E")
    assert lat == pytest.approx(16.2303, abs=1e-3) and lon == pytest.approx(107.7169, abs=1e-3)
    assert vhpa.parse_loss_coordinates("093515E 1061812E") is None


# ---------------------------------------------------------------- crawl / cache / sync
class FakeCache:
    """Stands in for common.cached_get: serves files from a folder, 404 for anything missing."""

    def __init__(self, folder):
        self.folder, self.calls = folder, []

    def __call__(self, url, source, *, name=None, **kw):
        path = self.folder / name
        if path.exists():
            return path
        self.calls.append(url)
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


@pytest.fixture
def raw_dir(tmp_path, monkeypatch):
    folder = tmp_path / "vhpa"
    folder.mkdir()
    fake = FakeCache(folder)
    monkeypatch.setattr(vhpa, "RAW", folder)
    monkeypatch.setattr(common, "cached_get", fake)
    vhpa.FETCH_ERRORS.clear()
    return folder, fake


def test_404_is_remembered_and_not_refetched(raw_dir):
    folder, fake = raw_dir
    assert vhpa._get(vhpa.BASE + "incident/NOPE.HTM", "incident_NOPE.HTM") is None
    assert len(fake.calls) == 1
    failures = json.loads((folder / vhpa.FAILURES_NAME).read_text(encoding="utf-8"))
    assert failures["incident_NOPE.HTM"]["status"] == 404
    # A re-run (fresh process state) makes no request for it.
    vhpa._FAILURES = None
    assert vhpa._get(vhpa.BASE + "incident/NOPE.HTM", "incident_NOPE.HTM") is None
    assert len(fake.calls) == 1
    assert vhpa.FETCH_ERRORS["http_404_cached"] == 1


def test_cached_pages_not_linked_are_still_used(raw_dir):
    """K12228 is linked from no panel or incident page, but it is in the cache: it must be built."""
    folder, fake = raw_dir
    (folder / "panelind.htm").write_text('<a href="PANEL28E.HTM">', encoding="cp1252")
    (folder / "PANEL28E.HTM").write_text(page("PANEL28E_excerpt.HTM"), encoding="cp1252")
    for name in ("K12228.HTM", "K00001.HTM"):
        (folder / name).write_text(page(name), encoding="cp1252")
    (folder / "incident_07838ACD.HTM").write_text(page("07838ACD.HTM"), encoding="cp1252")
    panels, incidents, inc_pages, person_pages = vhpa.crawl()
    assert "K12228" in person_pages and "07838ACD" in inc_pages
    assert "K12228" not in vhpa.parse_panel(page("PANEL28E_excerpt.HTM"))[1]
    records, _, _ = vhpa.build_all(inc_pages, person_pages)
    ids = {e.source_record_id for e in records}
    # K12228 links incident 70111021KIA, which is not cached: its record carries that id.
    assert {"70111021KIA", "07838ACD"} <= ids
    # Linked pages that 404 were tried once and are now remembered.
    n = len(fake.calls)
    vhpa._FAILURES = None
    again = vhpa.crawl()
    assert len(fake.calls) == n
    assert sorted(again[3]) == sorted(person_pages) and sorted(again[2]) == sorted(inc_pages)


def _fixture_records():
    recs = []
    for name in ("07838ACD.HTM", "68051777KIA.HTM", "691124241ACD.HTM", "70012626KIA.HTM"):
        persons = [_person("K11346.HTM", "K11346")] if name.startswith("7001") else []
        e, _, _ = vhpa.build_record(name[:-4], "u", vhpa.parse_incident(page(name)), persons)
        recs.append(e)
    for name in ("K12228.HTM", "K11427.HTM"):
        recs.append(_person_only(_person(name, name[:-4]))[0])
    recs.append(
        vhpa.build_record(
            "680426151ACD",
            "u",
            vhpa.parse_incident(page("680426151ACD.HTM")),
            [_person("K10246.HTM", "K10246"), _person("K10466.HTM", "K10466")],
        )[0]
    )
    recs.append(
        vhpa.build_record(
            "71033143KIA", "u", vhpa.parse_incident(page("71033143KIA.HTM")), [_person("K11647.HTM", "K11647")]
        )[0]
    )
    return recs


def test_sync_twice_is_a_noop(tmp_path):
    db = store.connect(tmp_path / "e.sqlite")
    store.upsert_source(db, vhpa.SOURCE, name="VHPA")
    recs = _fixture_records()
    assert store.sync_source(db, vhpa.SOURCE, recs) == (len(recs), 0, 0)
    q = (
        "select source_record_id, kind, lon, lat, uncertainty_m, original_coord, original_datum, "
        "title, description, raw_json, withdrawn_at from evidence order by source_record_id"
    )
    first = db.execute(q).fetchall()
    assert store.sync_source(db, vhpa.SOURCE, _fixture_records()) == (len(recs), 0, 0)
    assert db.execute(q).fetchall() == first
    # A record the source stops giving is removed.
    assert store.sync_source(db, vhpa.SOURCE, recs[1:]) == (len(recs) - 1, 1, 0)


# ---------------------------------------------------------------- burials, aircraft class, ground casualties, grouping
def _build(inc_names=(), person_names=()):
    inc_pages = {n: page(n + ".HTM") for n in inc_names}
    person_pages = {n: page(n + ".HTM") for n in person_names}
    records, _, _ = vhpa.build_all(inc_pages, person_pages)
    return {e.source_record_id: e for e in records}


def test_imo_marker_is_not_a_recovered_burial():
    """K10772 (OV-1C, BNR): a burial line marked '(IMO)' is an in-memoriam marker for a
    man whose body was never recovered. It must not be read as a burial of recovered remains."""
    p = _person("K10772.HTM", "K10772")
    assert p["burial_recorded"] is False and p["burial_imo"] is True
    e = _person_only(p)[0]
    assert "recovered)" not in e.description and "formal burial" not in e.description
    assert "remains recovered" not in e.description
    assert "(IMO)" in e.description and "NOT recovered" in e.description
    assert e.raw["pilot_pages_with_burial"] == [] and e.raw["pilot_pages_in_memoriam"] == ["K10772"]


def test_real_burial_line_does_not_claim_recovery_either():
    """A non-IMO burial line is a burial place, not a statement about where remains came from."""
    p = _person("K11427.HTM", "K11427")
    assert p["burial_recorded"] is True and p["burial_imo"] is False
    e = _person_only(p)[0]
    assert "give a burial place" in e.description
    assert "remains recovered" not in e.description and "ANYTOWN" not in e.description


@pytest.mark.parametrize("name, acft", [("K10772", "OV-1C 61-02679"), ("K10860", "O-1G 57-06027")])
def test_person_only_bnr_counts_killed_and_fixed_wing_is_not_helicopter(name, acft):
    e = _person_only(_person(name + ".HTM", name))[0]
    assert e.title == f"[BNR x1] {acft} crash, 1 killed (body not recovered)"
    assert e.description.startswith("Fixed-wing aircraft loss")
    assert "Helicopter" not in e.description and e.kind == "aircraft_crash"


def test_helicopter_incident_still_called_helicopter():
    e, _, _ = vhpa.build_record("68051777KIA", "u", vhpa.parse_incident(page("68051777KIA.HTM")), [])
    assert e.description.startswith("Helicopter loss") and e.kind == "aircraft_crash"


@pytest.mark.parametrize("name", ["K10923", "K11528", "K11427"])
def test_ground_casualty_person_pages_are_not_crashes(name):
    """K10923: a ground casualty (an automobile accident); K11528: killed in a rocket attack
    standing near his Huey; K11427: 'Primary cause: Ground Casualty'."""
    e = _person_only(_person(name + ".HTM", name))[0]
    assert e.kind == "loss_incident"
    assert "crash" not in e.title and "ground casualty" in e.title
    assert "not an aircraft crash" in e.description
    assert "Crash site" not in e.description


def test_ground_casualty_incident_page_is_not_a_crash():
    """71051616KIA: three men killed by a rocket as they worked on the helicopter on the ground."""
    inc = vhpa.parse_incident(page("71051616KIA.HTM"))
    e, _, _ = vhpa.build_record("71051616KIA", "u", inc, [])
    assert e.kind == "loss_incident" and "crash" not in e.title
    assert e.title.startswith("ground casualty, 3 killed")


def test_shot_down_then_killed_on_ground_stays_a_crash():
    assert (
        vhpa.ground_reason({"summary": "Survived the shoot down, then was killed on the ground by enemy fire."}, [])
        is None
    )
    assert vhpa.ground_reason({"summary": "Ground casualty."}, []) is not None


def test_grid_outside_named_province_is_flagged_without_slack():
    """70012020KIA: grid XU923234 is ~32 km outside Quang Duc (K11792) even on the generous HES
    geometry; the old 40 km slack passed it at 500 m. The radius must now cover the province."""
    inc = vhpa.parse_incident(page("70012020KIA.HTM"))
    p = _person("K11792.HTM", "K11792")
    assert vhpa.norm_province(p["location"]) == "quang duc"
    e, why, info = vhpa.build_record("70012020KIA", "u", inc, [p])
    assert info["check"] == "province_mismatch"
    assert _covers_province(e, "quang duc")
    assert e.uncertainty_m > 50_000
    # check and widening use the same geometry
    lon, lat, r = vhpa.province_geometry("quang duc")
    assert e.raw["decode"]["province_excess_km"]["quang duc"] == pytest.approx(
        vhpa.km(e.lon, e.lat, lon, lat) - r, abs=0.1
    )


def test_grid_inside_province_is_ok():
    loc = vhpa.locate("YD368655", province="quang tri")
    assert loc["check"] == "province_ok"


def test_pilot_pages_of_one_missing_incident_make_one_record():
    """K10811 and K11282 are the two pilots of 72042810KIA (no incident page cached): one crash,
    one record, id = the incident id so it does not change when the incident page is fetched."""
    recs = _build(person_names=("K10811", "K11282"))
    assert list(recs) == ["72042810KIA"]
    e = recs["72042810KIA"]
    assert e.raw["person_pages"] == ["K10811", "K11282"]
    assert "2 killed" in e.title


def test_person_only_id_does_not_change_when_incident_page_arrives():
    before = _build(person_names=("K11035",))
    after = _build(inc_names=("67090113KIA",), person_names=("K11035",))
    assert list(before) == list(after) == ["67090113KIA"]


def test_unlinked_person_page_keeps_its_own_id():
    assert list(_build(person_names=("K11427",))) == ["K11427"]


def test_same_day_nearby_loss_is_named():
    """K11427 (ground casualty, 07/08/1970) is 0.9 km from 70070811KIA of the same date."""
    recs = _build(inc_names=("70070811KIA",), person_names=("K11427",))
    e = recs["K11427"]
    assert "70070811KIA" in e.description and e.raw["same_day_nearby"][0]["id"] == "70070811KIA"
    assert e.raw["same_day_nearby"][0]["km"] == pytest.approx(0.9, abs=0.3)


def test_incident_linked_only_from_person_page_is_fetched(raw_dir):
    folder, fake = raw_dir
    (folder / "panelind.htm").write_text('<a href="PANEL28E.HTM">', encoding="cp1252")
    (folder / "PANEL28E.HTM").write_text("", encoding="cp1252")
    (folder / "K11792.HTM").write_text(page("K11792.HTM"), encoding="cp1252")
    vhpa.crawl()
    assert vhpa.BASE + "incident/70012020KIA.HTM" in fake.calls


def test_transient_fetch_error_aborts(raw_dir, monkeypatch):
    def boom(url, source, **kw):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(common, "cached_get", boom)
    with pytest.raises(RuntimeError):
        vhpa._get(vhpa.BASE + "panel/PANEL01E.HTM", "PANEL01E.HTM")


def test_person_page_without_a_recorded_crash_is_not_a_crash():
    """K11779: died of wounds received while flying, reason 'gunshot or small arms fire': the
    source does not say the aircraft crashed."""
    e = _person_only(_person("K11779.HTM", "K11779"))[0]
    assert e.kind == "loss_incident" and "crash," not in e.title
    assert "Not recorded as an aircraft crash" in e.description


def test_aircraft_type_without_tail_is_read():
    """K11881: 'Aircraft: UH-1H' with no tail number is still a helicopter."""
    p = _person("K11881.HTM", "K11881")
    assert p["aircraft_type"] == "UH-1H" and p["tail"] is None
    assert _person_only(p)[0].title.startswith("UH-1H crash")


def test_passengers_killed_are_counted():
    """701124121ACD: the pilot was injured, two passengers KIA - not '0 killed'."""
    inc = vhpa.parse_incident(page("701124121ACD.HTM"))
    assert [x["status"] for x in inc["passengers"]] == ["KIA", "KIA"]
    e, _, _ = vhpa.build_record("701124121ACD", "u", inc, [])
    assert e.title == "OH-6A 68-17177 crash, 3 killed"  # the accident line says 3
    assert "passengers: 2 KIA" in e.description and "accident line says 3 killed" in e.description


def test_incident_without_crew_codes_quotes_casualty_line():
    """69021616KIA lists no crew status codes; its casualty line says only 'YES' - not '0 killed'."""
    e, _, _ = vhpa.build_record("69021616KIA", "u", vhpa.parse_incident(page("69021616KIA.HTM")), [])
    # Its war story tells of a crash, but not in a sentence that names this aircraft (war stories
    # often tell of other aircraft): the page itself does not say.
    assert "0 killed" not in e.title
    assert e.title.endswith("loss, crash not stated, casualties YES")
    assert "casualty line: YES" in e.description


def test_cached_404_body_is_not_an_incident_page():
    """incident_69040303KIA.HTM in the cache is a stored '404 Not Found' body, not a page."""
    assert vhpa.is_error_page(page("69040303KIA.HTM"))
    recs, skips, _ = vhpa.build_all({"69040303KIA": page("69040303KIA.HTM")}, {})
    assert recs == []


def test_did_not_crash_is_not_a_ground_casualty_or_crash():
    """K11396: hit by hostile fire in flight; 'the helicopter ... did not crash'."""
    e = _person_only(_person("K11396.HTM", "K11396"))[0]
    assert e.kind == "loss_incident" and e.title.startswith("loss with no crash recorded")
    assert "ground casualty" not in e.title


def test_remains_returned_status_counts_and_is_shown():
    """K10595: 'Remains were returned on 08/07/03 ...' - killed, remains recovered later."""
    p = _person("K10595.HTM", "K10595")
    assert vhpa.person_code(p["status"]) == "RR"
    e = _person_only(p)[0]
    assert "1 killed" in e.title and "1 RR" in e.description


# ---------------------------------------------------------------- province geometry, foreign pages, scope, kind of loss
def _inc(name, *persons):
    return vhpa.build_record(
        name, "u", vhpa.parse_incident(page(name + ".HTM")), [_person(k + ".HTM", k) for k in persons]
    )


def _covers_every_disk(e, prov):
    return all(
        vhpa.km(e.lon, e.lat, x, y) + r <= e.uncertainty_m / 1000 + 1e-6 for x, y, r in vhpa.province_disks(prov)
    )


def test_province_check_uses_the_tight_hand_disk_not_the_hes_disk():
    """The HES disk (farthest hamlet + 10 km) is far bigger than the province (Saigon 77 km vs
    15 km); a grid 30 km from Saigon's centre is not in Saigon."""
    assert vhpa.province_geometry("saigon")[2] == 15
    x, y, _ = vhpa.province_geometry("saigon")
    assert vhpa._excess(x + 0.28, y, "saigon") > 10  # ~30 km east of the centre
    assert vhpa.locate("XS470910", province="saigon")["check"] == "province_mismatch"


@pytest.mark.parametrize(
    "make, prov",
    [
        # XS470910 is west of Saigon; both pilot pages name Bien Hoa (east of Saigon).
        (lambda: _inc("71092020KIA", "K10437", "K10089"), "bien hoa"),
        # BT158360 is in Quang Nam/Quang Tin; both pilot pages name Quang Ngai.
        (lambda: _inc("700317151ACD", "K11456", "K12117"), "quang ngai"),
        # 69021398BAT (person page only): grid BT192143 in Quang Tin, page names Kontum.
        (lambda: _person_only(_person("K10092.HTM", "K10092")), "kontum"),
    ],
)
def test_grid_outside_the_named_province_is_no_longer_province_ok(make, prov):
    e, why, info = make()
    assert why is None and info["check"] == "province_mismatch"
    assert e.uncertainty_m > 10_000
    assert _covers_every_disk(e, prov)
    assert "grid/province mismatch" in e.raw["flags"]


def test_crew_page_about_a_loss_years_apart_is_foreign():
    """67111455KIA (11/1967) links K10853 as its AC, but K10853 is about a loss on 08/29/1971
    (Quang Tin, with its own grid): not used for this record's place."""
    inc_pages = {"67111455KIA": page("67111455KIA.HTM")}
    person_pages = {k: page(k + ".HTM") for k in ("K11707", "K10853")}
    recs, skips, _ = vhpa.build_all(inc_pages, person_pages)
    recs = {e.source_record_id: e for e in recs}
    # Its own pages give no grid: it was placed at K10853's 1971 grid before; now skipped.
    assert "67111455KIA" not in recs and skips["no_grid_province_only"] == 1
    assert recs["K10853"].raw["person_pages"] == ["K10853"]  # its own record
    raw = page("67111455KIA.HTM").replace('<A HREF="../K11707.HTM">', "")
    e, _, _ = vhpa.build_record(
        "67111455KIA",
        "u",
        vhpa.parse_incident(raw),
        [_person("K11707.HTM", "K11707")],
        foreign={"K10853": "1971-08-29"},
    )
    assert e is None or "a loss on 1971-08-29" in e.description


@pytest.mark.parametrize(
    "make",
    [
        # Pilot and gunner wounded; the helicopter not seriously damaged.
        lambda: _inc("71040101KIA", "K11540"),
        # C&C hit by 37mm; the other pilot flew it back to Firebase Veghel.
        lambda: _inc("71041033KIA", "K10796"),
        # Automatic weapons fire hit him in the chest: no crash described.
        lambda: _person_only(_person("K10092.HTM", "K10092")),
        # Hit by AK-47 fire, died days later at the 95th Evac.
        lambda: _person_only(_person("K10270.HTM", "K10270")),
        # Hit by .51 cal at 2,000 ft and 80 knots, DOA at the 91st Evac.
        lambda: _person_only(_person("K10853.HTM", "K10853")),
        # Struck by the main rotor at LZ Lolo.
        lambda: _person_only(_person("K10035.HTM", "K10035")),
    ],
)
def test_no_crash_described_is_not_an_aircraft_crash(make):
    e, why, _ = make()
    assert why is None
    assert e.kind == "loss_incident"
    assert " crash," not in e.title and "Crash site" not in e.description
    assert e.raw["crash_basis"] is None


def test_claymore_mine_is_a_ground_casualty():
    """K12187: set off a claymore mine crossing the perimeter wire."""
    e = _person_only(_person("K12187.HTM", "K12187"))[0]
    assert e.kind == "loss_incident" and e.title.startswith("ground casualty")


def test_described_crash_stays_a_crash_and_says_why():
    """K11913: '... the aircraft flipped over backwards into a canal.'"""
    e, _, _ = _inc("721028031ACD", "K11913")
    assert e.kind == "aircraft_crash" and "flipped" in e.raw["crash_basis"]
    e, _, _ = _inc("68051777KIA")
    assert e.kind == "aircraft_crash" and "shot down" in e.raw["crash_basis"]


def test_crash_wording_about_another_aircraft_or_negated_is_not_a_crash():
    assert vhpa.crash_wording("It took heavy damage but did not crash.") is None
    assert vhpa.crash_wording("Survived the crash.")  # a crash happened
    assert vhpa.crash_wording("Small arms fire hit the aircraft, wounding the pilot.") is None


@pytest.mark.parametrize(
    "make, where",
    [
        # HH-43B destroyed by the blast of a crashed B-52 at U-Tapao, Thailand.
        (lambda: _inc("69071919KIA", "K10499"), "Thailand"),
        # CH-3C shot down in Thailand by insurgents.
        (lambda: _inc("70081321KIA", "K10370", "K10893"), "Thailand"),
        # CH-53C lost in northern Laos (102.75E 19.2N), ~120 km from Vietnam.
        (lambda: _inc("71030122KIA", "K11632", "K11996"), "Laos"),
    ],
)
def test_losses_outside_vietnam_and_its_border_areas_are_out_of_scope(make, where):
    e, why, _ = make()
    assert e is None and why.startswith("out_of_scope")


def test_border_area_loss_outside_vietnam_is_kept_and_says_so():
    """K10035 (Lam Son 719, country Laos, ~25 km from the nearest Vietnamese place)."""
    e = _person_only(_person("K10035.HTM", "K10035"))[0]
    assert e is not None and "Laos" in e.description
    assert "OUTSIDE VIETNAM" in e.description and "border area" in e.description
    assert e.raw["vn_gazetteer_km"] > 10


def test_source_country_label_is_kept_with_the_province():
    """68030666TXT: incident page says Laos, pilot page says Thua Thien province."""
    e, _, _ = _inc("68030666TXT", "K11807")
    assert "Thua Thien province" in e.description and "Laos" in e.description


def test_pilot_page_killed_overrides_crew_injured_code():
    """721028031ACD: crew code INJ, but pilot page K11913 says Killed In Action, and the page's
    accident line says 'Number killed in accident = 1'."""
    inc = vhpa.parse_incident(page("721028031ACD.HTM"))
    assert inc["accident_killed"] == 1
    e, _, _ = vhpa.build_record("721028031ACD", "u", inc, [_person("K11913.HTM", "K11913")])
    assert "1 killed" in e.title and "no deaths" not in e.title
    assert "K11913" in e.description and "INJ" in e.description
    assert "; ." not in e.description
    assert "country per source: South Vietnam" in e.description  # from the pilot page
    p = dict(_person("K11913.HTM", "K11913"), status="")  # the accident line alone
    e, _, _ = vhpa.build_record("721028031ACD", "u", inc, [p])
    assert "1 killed" in e.title and "accident line says 1 killed" in e.description


def test_pilot_page_remains_returned_overrides_crew_bnr():
    """68030666TXT: crew code BNR, but pilot page K11807 says 'Remains Returned'."""
    e, _, _ = _inc("68030666TXT", "K11807")
    bnr = e.description.split("BODY NOT RECOVERED: ")[1].split(".")[0]
    assert "DELTADALE" not in bnr and "ECHODALE" in bnr  # ECHODALE: passenger BNR, no pilot page
    assert "K11807" in e.description and "Remains Returned" in e.description
    assert e.title.startswith("[BNR x1,")


def test_coded_outcome_line_decides_the_kind():
    """07838ACD: 'The helicopter Crashed. Aircraft Destroyed.'"""
    inc = vhpa.parse_incident(page("07838ACD.HTM"))
    assert inc["fate"] == "The helicopter Crashed. Aircraft Destroyed."
    assert vhpa.crash_evidence(inc, [])[0].startswith("incident page outcome line")
    # 'Continued Flight' / a landing alone: no crash recorded ...
    assert vhpa.crash_evidence({"fate": "The helicopter Continued Flight."}, [])[1]
    assert (
        vhpa.crash_evidence(
            {"fate": "The helicopter made a Forced Landing. Aircraft was capable of one time flight."}, []
        )[0]
        is None
    )
    # ... but it is not a shortcut: a pilot page summary saying it crashed later still counts.
    p = {"_id": "K10593", "short_summary": "Out of fuel, it crashed and burned on the way back."}
    basis, no = vhpa.crash_evidence({"fate": "The helicopter Continued Flight. Aircraft Destroyed."}, [p])
    assert no is None and "K10593" in basis


def test_negated_crash_in_a_longer_account_is_not_a_crash():
    """Pilot page notes that deny a crash ('was not lost or crashed') do not describe one."""
    assert (
        vhpa.crash_wording("The aircraft was not lost or crashed; small arms fire hit it and wounded the pilot.")
        is None
    )
    p = {
        "_id": "K10476",
        "short_summary": "Among the last Army helicopter pilots killed.",
        "_narratives": [("notes", "The aircraft was not lost or crashed; small arms fire hit it.")],
    }
    assert vhpa.crash_evidence(
        {"summary": "Small arms fire hit the aircraft; the wounded pilot died the next day."}, [p]
    ) == (None, 'incident page summary: "Small arms fire hit the aircraft; the wounded pilot died the next day."')


# ---------------------------------------------------------------- coded outcome lines, longer accounts, countries
def _all_fixture_records():
    inc_pages = {f.stem: page(f.name) for f in FIX.glob("*.HTM") if not f.stem.startswith(("K", "PANEL"))}
    person_pages = {f.stem: page(f.name) for f in FIX.glob("K*.HTM")}
    return vhpa.build_all(inc_pages, person_pages)


@pytest.mark.parametrize(
    "name, persons",
    [
        # 'The helicopter Continued Flight.' then 'Ultimate crash and burn'; 5 KIA.
        ("69041810KIA", ("K10498", "K10593")),
        # 'made an Emergency Landing' then 'Ultimate crash and burn' (BNR x2, REFNO 1135).
        ("68041901TXT", ()),
        # 'made an Emergency Landing' then 'Ultimate crash and burn' (REFNO 0203).
        ("65120310KIA", ("K11037", "K11676")),
    ],
)
def test_ultimate_crash_and_burn_line_is_a_crash(name, persons):
    e, why, _ = _inc(name, *persons)
    assert why is None and e.kind == "aircraft_crash"
    assert "Ultimate crash and burn" in e.raw["crash_basis"]
    assert "no crash recorded" not in e.title and "Not recorded as an aircraft crash" not in e.description
    assert "Crash site, not a burial" in e.description


def test_forced_landing_with_crash_in_refno_synopsis_is_a_crash():
    """69081317KIA: coded 'made a Forced Landing. Airframe Destroyed.', but its REFNO synopsis says
    the helicopter was hit by small arms fire and crashed (the pilot BNR)."""
    e, why, _ = _inc("69081317KIA", "K10839")
    assert why is None and e.kind == "aircraft_crash"
    assert e.raw["crash_basis"] == "the REFNO synopsis on the incident page describes a crash"
    assert "during a hoist rescue" not in e.description


@pytest.mark.parametrize(
    "name, persons, ground_words",
    [
        # Pilot pages: 'Killed on ground' by a land mine; REFNO synopsis: hit, spun and crashed.
        ("69051329TXT", ("K11931", "K12172"), "Killed on ground"),
        # Pilot page cause 'Mortar Attack', but 'Hit on short final by a 60 mm mortar round' and the
        # pilot page notes say the Chinook was shot down and burned: hit in flight, no ground
        # reading at all.
        ("72052410KIA", ("K10090", "K11982"), None),
    ],
)
def test_ground_words_do_not_override_a_described_crash(name, persons, ground_words):
    e, why, _ = _inc(name, *persons)
    assert why is None and e.kind == "aircraft_crash"
    assert "ground casualty" not in e.title.lower()
    assert "not an aircraft crash" not in e.description
    assert e.raw["ground_casualty"] is None
    # The conflicting ground wording is reported, not hidden.
    if ground_words:
        assert ground_words in e.description and "disagrees with itself" in e.description


def test_mortar_hit_on_short_final_is_not_a_ground_cause():
    p = {
        "_id": "K11982",
        "short_summary": "Hit on short final by a 60 mm mortar round.",
        "primary_cause": "Mortar Attack",
        "reason": "aircraft lost or crashed",
    }
    assert vhpa.ground_reason({}, [p]) is None
    p = dict(p, short_summary="Killed in a mortar attack on the base camp.")
    assert vhpa.ground_reason({}, [p]) is not None


def test_no_narrative_text_is_stored():
    """Accident reports, REFNO synopses and pilot page notes are read for the kind, never quoted."""
    records, _, _ = _all_fixture_records()
    pages = {
        **{
            f.stem: vhpa.parse_incident(page(f.name))
            for f in FIX.glob("*.HTM")
            if not f.stem.startswith(("K", "PANEL"))
        },
        **{f.stem: vhpa.parse_person(page(f.name)) for f in FIX.glob("K*.HTM")},
    }
    narr = [t for d in pages.values() for _, t in d["_narratives"]]
    checked = 0
    for e in records:
        assert "longer account" not in e.description
        for t in narr:
            for i in range(0, max(1, len(t) - 40), 20):
                window = t[i : i + 40]
                if len(window) == 40:
                    checked += 1
                    assert window not in e.description, (e.source_record_id, window)
    assert checked > 1000


def test_crash_of_another_aircraft_is_not_this_aircraft_crash():
    """680625052ACD (66-16206): the page summary names the mid-air of two OTHER aircraft; the pilot
    pages list a three-aircraft mid-air that includes 66-16206."""
    assert (
        vhpa.crash_wording(
            "damaged while passing through debris of the mid-air between BLUE 1 "
            "(66-16592) and the command ship (66-16601).",
            own_tail="66-16206",
        )
        is None
    )
    assert vhpa.crash_wording(
        "Three-ship mid-air (66-16601, 66-16592, & 66-16206) in poor weather.", own_tail="66-16206"
    )
    e, _, _ = _inc("680625052ACD", "K10933", "K11706")
    assert e.kind == "aircraft_crash" and "66-16592" not in e.raw["crash_basis"].split(":")[0]
    assert e.raw["crash_basis"].startswith("pilot page K10933 summary")


@pytest.mark.parametrize(
    "name, persons, country",
    [
        ("69042323KIA", ("K10100",), "Laos"),  # grid in Quang Tri, source says Laos
        ("70031515KIA", ("K12213",), "Cambodia"),  # grid in Phuoc Long, source says Cambodia
    ],
)
def test_radius_reaches_the_foreign_country_the_source_names(name, persons, country):
    e, why, _ = _inc(name, *persons)
    assert why is None and vhpa.in_vietnam(e.lon, e.lat)
    reach = vhpa.country_border_km(e.lon, e.lat, country)
    assert 1 < reach < 150
    assert e.uncertainty_m / 1000 >= reach
    assert e.uncertainty_m > 500
    assert "grid/country disagreement" in e.raw["flags"]
    assert f"reaches {country}" in e.description


def test_radius_reaches_vietnam_when_the_grid_lies_outside_it():
    """67033120KIA: incident page says South Vietnam (no province), the grid lies outside
    Vietnam ~24 km from any Vietnamese place; its pilot page K10084 says Laos."""
    e, why, _ = _inc("67033120KIA", "K10084")
    assert why is None and not vhpa.in_vietnam(e.lon, e.lat)
    reach = vhpa.vietnam_border_km(e.lon, e.lat)
    assert reach > 1 and e.uncertainty_m / 1000 >= reach
    assert "grid/country disagreement" in e.raw["flags"]
    assert "Laos" in e.description and "K10084" in e.description


def test_country_named_only_in_the_summary_is_used():
    """69092727KIA has no coded country line; its summary says '... mission in Laos.'"""
    e, why, _ = _inc("69092727KIA")
    assert why is None
    assert "the source names no province or country" not in e.description
    assert "OUTSIDE VIETNAM" in e.description and "Laos" in e.description
    assert e.raw["countries"] == {"Laos": ["incident page summary"]}


def test_grid_source_is_labelled_truthfully():
    """69021616KIA: grid and everything else come from the incident page; it has no crew codes
    and no pilot pages."""
    e, _, _ = _inc("69021616KIA")
    assert "(incident page)" in e.description and "(person page)" not in e.description
    assert "pilot page status" not in e.description
    assert e.raw["grid_from"] == "incident page"
    e = _person_only(_person("K10092.HTM", "K10092"))[0]
    assert e.raw["grid_from"] == "person page"


def test_loss_with_no_death_recorded_is_skipped():
    """68011061BAT: a UH-1D damaged in an attack on Kontum airfield; accident line 0 killed, no
    crew codes, no pilot pages: nothing about anyone's remains."""
    e, why, _ = _inc("68011061BAT")
    assert e is None and why == "no_loss_of_life"


def test_description_does_not_depend_on_page_order():
    """71022555KIA: both pilot pages say it crashed; the one quoted must not depend on the
    order the pages were crawled in."""
    inc = {"71022555KIA": page("71022555KIA.HTM")}
    a = {k: page(k + ".HTM") for k in ("K10269", "K10625")}
    b = {k: page(k + ".HTM") for k in ("K10625", "K10269")}
    ra, _, _ = vhpa.build_all(inc, a)
    rb, _, _ = vhpa.build_all(inc, b)
    assert [e.description for e in ra] == [e.description for e in rb]
    assert "K10269" in ra[0].raw["crash_basis"]


def test_summary_line_inside_a_refno_synopsis_is_not_the_page_summary():
    """680212211ACD has no Summary line of its own; the 'Summary:' inside its quoted REFNO
    synopsis (quoted MIA file text) is narrative and must not be stored."""
    raw = page("680212211ACD.HTM")
    assert "Summary:  On February 12, 1968" in raw
    inc = vhpa.parse_incident(raw)
    assert inc["summary"] is None
    e, _, _ = _inc("680212211ACD", "K10804", "K11696")
    assert "On February 12, 1968" not in e.description
    assert e.kind == "aircraft_crash"
    assert "the REFNO synopsis on the incident page describes a crash" in e.description


# ---------------------------------------------------------------- continued flight, war stories, in-memoriam, at sea
def _overlap(desc, text, n=35):
    """Any n-character run of `text` (whitespace-normalised) that appears in `desc`, leaving out
    the page's own short Summary field (a war story can repeat it)."""
    desc = re.sub(r"Summary: [^.]*\.", "", desc)
    t = " ".join(text.split())
    return [t[i : i + n] for i in range(0, max(0, len(t) - n), 5) if t[i : i + n] in desc]


def _war_story(name):
    return dict(vhpa._narratives(page(name + ".HTM"), (("war story", r"War Story:"),)))["war story"]


@pytest.mark.parametrize(
    "name, persons, words",
    [
        # 16 hits, REPAIRED IN THEATER, 'Continued Flight ... accomplished all mission objectives';
        # the pilot page's crash words are about two Navy Seawolf UH-1Bs (call signs, no tails).
        ("69042820KIA", ("K10863",), "Seawolf 320"),
        # OH-58A: 'Continued Flight. The aircraft was diverted', only PERSONNEL damaged; the pilot
        # page summary says 'Shot down'.
        ("71021530KIA", ("K10509",), "Shot down"),
    ],
)
def test_continued_flight_line_outweighs_a_summary_crash_alone(name, persons, words):
    e, _, _ = _inc(name, *persons)
    assert e.kind == "loss_incident" and "crash," not in e.title
    assert "Continued Flight" in e.description and words in e.description
    assert "disagrees with itself" in e.description
    assert "Crash site" not in e.description


def test_continued_flight_crash_corroborated_by_war_story_states_the_conflict():
    """69121414KIA: summary and war story say Seawolf 313 crashed in the channel; the coded line
    says 'Continued Flight ... accomplished all mission objectives'."""
    e, _, _ = _inc("69121414KIA")
    assert e.kind == "aircraft_crash"
    assert "disagrees with itself" in e.description and "Continued Flight" in e.description
    assert not _overlap(e.description, _war_story("69121414KIA"))


@pytest.mark.parametrize(
    "name, persons",
    [
        ("0B765ACD", ()),  # '63-08631 crashed just after takeoff'
        ("71061027KIA", ()),  # 'UH-1C 66-00597 the day we crashed in the jungle'
        ("70011111KIA", ()),  # 'Crashed while lifting off from the III MAF pad'
        ("68092613KIA", ()),  # '... this LOH was shot down'
        ("66071515KIA", ()),  # 'The CH-46 took hits and caught fire, then crashed'
        ("69120657KIA", ()),  # 'Aircraft 699 ... crashed, killing everyone aboard'
        ("70032202KIA", ("K10870",)),  # coded 'was parked', war story 'crashed and burned'
    ],
)
def test_war_story_saying_this_aircraft_crashed_is_read(name, persons):
    e, _, _ = _inc(name, *persons)
    assert e.kind == "aircraft_crash", e.description
    assert "the war story on the incident page describes a crash" in e.raw["crash_basis"]
    assert "does not say whether the aircraft crashed" not in e.description
    assert not _overlap(e.description, _war_story(name))  # read, never quoted


@pytest.mark.parametrize(
    "name, persons",
    [
        ("70051011KIA", ("K11389", "K11854")),  # 'another huey in the Delta ... crashed'
        ("69020606KIA", ("K10861",)),  # 'The lead ship was hit and crashed in the LZ; ours ...'
        ("66111101KIA", ()),  # 'in danger of crashing' / 'get out before we crash'
    ],
)
def test_war_story_crash_words_not_about_this_aircraft_are_not_a_crash(name, persons):
    e, _, _ = _inc(name, *persons)
    assert e.kind == "loss_incident"
    assert "The source does not say" not in e.description
    assert "war story on the incident page uses crash wording" in e.description
    assert not _overlap(e.description, _war_story(name))


def test_in_memoriam_marker_gives_way_to_remains_returned():
    """65120310KIA: K11037 has '(IMO)' but his status is 'Remains were returned on 2016'."""
    e, _, _ = _inc("65120310KIA", "K11037", "K11676")
    assert "NOT recovered for K11037" not in e.description
    assert "K11037" in e.description and "remains returned" in e.description.lower()
    assert e.raw["pilot_pages_in_memoriam"] == []


def test_over_water_with_north_vietnam_named_says_how_it_is_read():
    """66041555KIA: incident page 'Over Water', pilot page K12220 'North Vietnam'; the grid lies
    ~60 km outside Vietnam's boundary."""
    e, _, _ = _inc("66041555KIA", "K12220")
    assert not vhpa.in_vietnam(e.lon, e.lat)
    assert "North Vietnam" in e.description and "Over Water" in e.description
    assert "taken to mean the waters off North Vietnam" in e.description


def test_passenger_status_codes_count_as_per_man_status():
    """68020122KIA: no crew list, one passenger coded KIA."""
    e, _, _ = _inc("68020122KIA")
    assert "passengers: 1 KIA" in e.description
    assert "gives no per-man status" not in e.description
    assert "remains status per status codes above" in e.description


def test_country_named_in_the_war_story_crash_sentence_is_reached():
    """71011625KIA: grid in South Vietnam; the war story sentence read for the kind says 'We
    were shot down inside Cambodia.'"""
    e, _, _ = _inc("71011625KIA")
    assert e.kind == "aircraft_crash"
    assert e.raw["countries"]["Cambodia"] == ["war story on the incident page"]
    if vhpa.in_vietnam(e.lon, e.lat):
        assert e.uncertainty_m / 1000 >= vhpa.country_border_km(e.lon, e.lat, "Cambodia")
    assert not _overlap(e.description, _war_story("71011625KIA"))


# ---------------------------------------------------------------- war story sentences and places
@pytest.mark.parametrize(
    "name, persons, tail",
    [
        # 'The second ship shot down was OH-6A 67-16268; ... hit the ground ...' ('second' is
        # this aircraft's own place in the order of losses, not another aircraft).
        ("71120935KIA", ("K12071",), "67-16268"),
        # '... the helicopter shot down was a 174th AHC ship, tail #67-17191.'
        ("71041131KIA", (), "67-17191"),
    ],
)
def test_war_story_downed_or_shot_down_naming_own_tail_is_a_crash(name, persons, tail):
    inc = vhpa.parse_incident(page(name + ".HTM"))
    assert tail in (vhpa.war_story_crash(inc) or "")
    e, _, _ = _inc(name, *persons)
    assert e.kind == "aircraft_crash", e.description
    assert e.raw["crash_basis"] == vhpa.WAR_STORY_BASIS
    assert "crash not stated" not in e.title and "Not a known crash site" not in e.description
    assert not _overlap(e.description, _war_story(name))


def test_concessive_rumour_clause_does_not_hide_the_main_clause():
    """71120935KIA: 'Although there were rumors the crew had been captured, eyewitnesses confirm
    both men died in the downing of OH-6A 67-16268.'"""
    inc = vhpa.parse_incident(page("71120935KIA.HTM"))
    inc["_war_story"] = " ".join(s for s in vhpa.WS_SENTENCE.split(inc["_war_story"]) if "second ship" not in s)
    assert "downing of OH-6A 67-16268" in (vhpa.war_story_crash(inc) or "")


def test_we_sentence_does_not_count_when_the_story_doubts_which_aircraft():
    """68013144KIA (UH-1B 63-12915, coded 'Forced Landing. Airframe Destroyed.'): 'We got shot
    down early that morning', but the narrator has no idea which tail he was flying."""
    inc = vhpa.parse_incident(page("68013144KIA.HTM"))
    assert vhpa.war_story_crash(inc) is None
    e, _, _ = _inc("68013144KIA", "K11192")
    assert e.kind == "loss_incident", e.description
    assert vhpa.WAR_STORY_BASIS not in e.description
    assert "Forced Landing" in e.description
    assert "questions which aircraft" in e.description
    assert not _overlap(e.description, _war_story("68013144KIA"))


def _reaches(e, lon, lat, r_m=0.0):
    return vhpa.km(e.lon, e.lat, lon, lat) * 1000 + r_m <= e.uncertainty_m + 1e-6


def test_war_story_crash_location_of_this_aircraft_is_reached():
    """67120414KIA: page grid YB772055; the war story says 66-16587 went down at 48P YB775105,
    lat/long 14 deg 33 min 10 sec N, 107 deg 33 min 40 sec E (~5 km away)."""
    e, _, _ = _inc("67120414KIA", "K10029")
    g = vhpa.locate("YB775105", [], "South Vietnam", "48P")
    assert _reaches(e, g["lon"], g["lat"], g["square_r"])
    assert _reaches(e, 107 + 33 / 60 + 40 / 3600, 14 + 33 / 60 + 10 / 3600)
    assert "YB775105" in e.description and "14 deg 33 min 10 sec" in e.description
    assert "grid/war story disagreement" in e.raw["flags"]
    places = {p["place"]: p for p in e.raw["war_story_places"]}
    assert places["YB775105"]["names_this_aircraft"] is True
    assert not _overlap(e.description, _war_story("67120414KIA"))


@pytest.mark.parametrize(
    "name, persons, grid",
    [
        # 'a UH-1H of A/101 AHB near BT265125 ... crashed' (page grid BT266225, 10 km).
        ("69060444KIA", ("K10710", "K11830"), "BT265125"),
        # 'a brigade LOH recon team near BT986942 ... crashed' (page grid AS993993).
        ("69070444KIA", ("K11360",), "BT986942"),
    ],
)
def test_war_story_crash_grid_without_the_tail_is_reached_and_flagged(name, persons, grid):
    e, _, _ = _inc(name, *persons)
    g = vhpa.locate(grid, [], "South Vietnam")
    assert _reaches(e, g["lon"], g["lat"], g["square_r"])
    assert grid in e.description and "does not name this aircraft's tail" in e.description
    assert "grid/war story disagreement" in e.raw["flags"]
    # The page's own short summary (quoted as the crash basis) may repeat the war story.
    summary = vhpa.parse_incident(page(name + ".HTM"))["summary"] or "\0"
    assert not _overlap(e.description.replace(summary, ""), _war_story(name))


@pytest.mark.parametrize("name, war_story", [("65120862KIA", False), ("05976ACD", True), ("69021616KIA", True)])
def test_crash_not_stated_names_only_the_sources_the_record_has(name, war_story):
    e, _, _ = _inc(name)
    assert e.kind == "loss_incident" and "crash not stated" in e.title
    d = e.description
    assert "pilot page" not in d.lower()
    assert "aircraft lost or crashed" not in d
    assert "accident report" not in d and "REFNO synopsis" not in d
    assert ("no sentence this parser recognises" in d) is war_story
    assert ("war story" in d) is war_story


def test_document_number_in_a_war_story_is_not_a_grid():
    """68050310KIA: '(TT 1201031016.pdf p27)' is a Texas Tech archive file, not a grid."""
    inc = vhpa.parse_incident(page("68050310KIA.HTM"))
    assert not [w for w in vhpa.war_story_places(inc) if w["place"].startswith("TT")]
