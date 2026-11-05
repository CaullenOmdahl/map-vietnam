"""Tests for the Wikidata + OSM ingester (scripts/ingest/wikidata_osm.py).

They run on small real excerpts of the WDQS, Overpass and GNS data in tests/fixtures/wikidata_osm/ (no
network) and cover parsing, kinds, radii and every scope rule, plus the store sync. Comments name the real
item or element each case is taken from.

Run: .venv/bin/python -m pytest -q tests/test_ingest_wikidata_osm.py
"""

import json
from pathlib import Path

import pytest
import store
from ingest import wikidata_osm as w

FIX = Path(__file__).parent / "fixtures" / "wikidata_osm"


def load(name):
    return json.loads((FIX / name).read_text())


@pytest.fixture(scope="module")
def wd_details():
    return {w.qid(b["item"]["value"]): b for b in load("wd_detail.json")["results"]["bindings"]}


@pytest.fixture(scope="module")
def wd_extra():
    return w.parse_wd_statements(load("wd_statements.json")["results"]["bindings"])


def groups_from_types(detail):
    """Fixture stand-in for the match queries: the WD_GROUPS each item's P31 falls under."""
    p31 = {w.qid(t.split("|", 1)[0]) for t in detail["types"]["value"].split(";")}
    out, classes = set(), set()
    for g, (_, _, cl) in w.WD_GROUPS.items():
        for q in p31:
            if q in cl:
                out.add(g)
                classes.add(cl[q])
    return out, classes


# Items whose own P31 is a subclass of a wanted class (the real run expands subclasses via WDQS).
SUBCLASS_GROUPS = {
    "Q43896632": ({"base"}, {"military installation"}),  # military medical university
    "Q877185": ({"base"}, {"fortification", "military installation"}),  # citadel
    "Q110967638": ({"base"}, {"military installation"}),  # military hospital
    "Q10825609": ({"base"}, {"fortification"}),  # citadel (Quảng Trị)
    "Q5122391": ({"base"}, {"fortification"}),  # citadel (Saigon, 1790)
    "Q10825533": ({"base"}, {"fortification"}),  # citadel (Bát Quái, 1790)
    "Q10825547": ({"base"}, {"fortification"}),  # citadel (Saigon, 1836)
    "Q190060": ({"base"}, {"fortification"}),  # citadel (Hồ dynasty)
    "Q10841882": ({"base"}, {"fortification"}),  # citadel (Đồ Bàn)
    "Q8079948": ({"base"}, {"fortification"}),  # fortress / ancient city
    "Q4240280": ({"base"}, {"fortification"}),  # fortress (Sơn Tây)
    "Q23134570": ({"base"}, {"fortification"}),
    "Q63045308": ({"base"}, {"military installation"}),  # observation tower
    "Q110974606": ({"base"}, {"military camp"}),
    "Q18737191": ({"base"}, {"military base"}),
    "Q106963887": ({"base"}, {"fire support base"}),  # firebase (Hill 100)
    "Q2627940": ({"airstrike"}, {"airstrike"}),  # bombardment
    "Q12009712": ({"battle"}, {"military operation"}),  # military occupation
}


@pytest.fixture(scope="module")
def vn():
    """Vietnam's OSM boundary (relation 49915), member ways thinned for the fixture."""
    return w.Region.from_element(load("vn_boundary.json")["elements"][0])


@pytest.fixture(scope="module")
def active_areas():
    """Present-day installations among the real outlines in osm_areas_geom.json."""
    return w.present_day_regions(load("osm_areas_geom.json")["elements"])


@pytest.fixture(scope="module")
def wd_locations():
    """P31 types and P2046 areas of the P276 values (real WDQS rows for the fixture items)."""
    return w.parse_wd_locations(load("wd_locations.json")["results"]["bindings"])


@pytest.fixture(scope="module")
def gazetteer():
    """GNS name-index entries (real rows of data/work/gns_index.json) for the names the fixture labels name."""
    return load("gns_names.json")["names"]


@pytest.fixture(scope="module")
def wd_built(wd_details, wd_extra, vn, active_areas, wd_locations, gazetteer):
    groups_of, classes_of = {}, {}
    for q, d in wd_details.items():
        groups_of[q], classes_of[q] = SUBCLASS_GROUPS.get(q) or groups_from_types(d)
        if not groups_of[q]:  # e.g. naval battle / siege subclasses: treat as battle
            groups_of[q], classes_of[q] = {"battle"}, {"battle"}
    records, stats = w.build_wd_records(
        wd_details,
        groups_of,
        classes_of,
        wd_extra,
        burial_types=set(),
        vn=vn,
        active_areas=active_areas,
        locations=wd_locations,
        gazetteer=gazetteer,
    )
    return {r.source_record_id: r for r in records}, stats


@pytest.fixture(scope="module")
def osm():
    return {f"{e['type']}/{e['id']}": e for e in load("osm_elements.json")["elements"]}


# ---- Wikidata: parsing and single records --------------------------------------------------------
def test_wd_dates_follow_precision():
    assert w.wd_date("1974-05-04T00:00:00Z", "11") == "1974-05-04"
    assert w.wd_date("1966-01-01T00:00:00Z", "9") == "1966"
    assert w.wd_date("1966-03-01T00:00:00Z", "10") == "1966-03"
    assert w.wd_date("1900-01-01T00:00:00Z", "7") is None  # century: too vague
    assert w.wd_date("-0938-01-01T00:00:00Z", "9") is None  # BCE not representable


def test_coordinate_uncertainty_widens_for_coarse_points():
    assert w.coord_uncertainty("Point(105.942 10.535)", 3000) == 3000
    assert w.coord_uncertainty("Point(105.5 10.9)", 500) == 7872  # 1 decimal ~ 0.1 deg square


def test_battle_record(wd_details):
    e = w.wd_evidence(wd_details["Q60524462"], {"battle"}, {"battle"})
    assert (e.source, e.source_record_id, e.kind) == ("wikidata", "Q60524462", "ground_combat")
    assert e.original_coord == "Point(105.942 10.535)" and (e.lon, e.lat) == (105.942, 10.535)
    assert e.original_datum == "article-coordinate" and e.uncertainty_m == 10_000  # a battle, one day
    assert e.date_start == e.date_end == "1974-05-04"
    assert e.url == "https://www.wikidata.org/wiki/Q60524462"
    assert w.in_bbox(e.lon, e.lat)


def test_base_record_keeps_other_labels(wd_details):
    e = w.wd_evidence(wd_details["Q60785495"], {"base"}, {"military base"})
    assert e.kind == "military_site" and e.uncertainty_m == 3000 and e.date_start == "1966"  # base footprint
    assert "Căn cứ Sa Đéc" in e.description and e.raw["countries"] == ["Q180573"]


def test_massacre_is_loss_incident_described_as_massacre(wd_details):
    e = w.wd_evidence(wd_details["Q183421"], {"massacre"}, {"massacre"})
    assert e.kind == "loss_incident" and e.description.startswith("Massacre.")  # people killed, not combat
    assert e.date_start == "1968-03-16" and e.uncertainty_m == 3000  # hamlet cluster (My Lai 4, My Khe 4)


def test_airport_kept_only_when_military_or_wartime(wd_details):
    assert w.airport_is_military(wd_details["Q478595"], {"airport"}) == "opened by 1975 in Indochina"
    civil = dict(wd_details["Q478595"], dates={"value": "P571|2004-01-01T00:00:00Z|9"})
    assert w.airport_is_military(civil, {"airport"}) is None


def test_match_rows_parse():
    rows = load("wd_match.json")["results"]["bindings"]
    assert {w.qid(r["item"]["value"]) for r in rows} >= {"Q60524462", "Q60785495"}


# ---- OSM: single elements ------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["node/1400908663", "node/1722516371", "way/344918979"])
def test_burials_and_martyr_memorials_dropped(osm, key):
    e, why = w.osm_evidence(osm[key])
    assert e is None and why.startswith("burial")


def test_burial_name_rules():
    assert w.burial_reason({"name": "Mộ ông Huyện Bình", "historic": "monument"})
    assert w.burial_reason({"name": "Ðài tưởng niệm liệt sĩ"})  # U+00D0 typed for Đ
    assert w.burial_reason({"cemetery": "war_cemetery"}) == "cemetery=*"
    assert w.burial_reason({"name": "Căn cứ Mộ Đức", "military": "base"}) is None  # district name


def test_wartime_naval_base_node(osm):
    e, why = w.osm_evidence(osm["node/5560875621"])
    assert (e.source, e.source_record_id, e.kind) == ("osm", "node/5560875621", "military_site")
    assert e.original_coord == "node lat=10.297546 lon=105.7669422" and e.original_datum == "wgs84"
    assert (e.lon, e.lat) == (105.7669422, 10.297546) and e.uncertainty_m == 3000  # one node for a whole base
    assert e.raw["present_day"] is False and "Hoa Kỳ" in e.description
    assert e.url == "https://www.openstreetmap.org/node/5560875621"


def test_present_day_military_skipped(osm):
    # present-day installations are outside the historical scope and sensitive to map
    assert w.osm_evidence(osm["way/371124467"]) == (None, "present_day_military")


def test_way_uses_bbox(osm):
    e, _ = w.osm_evidence(osm["way/478473546"])  # Di tích thành Sơn Phòng (citadel ruins)
    b = osm["way/478473546"]["bounds"]
    assert e.original_coord.startswith(f"way bbox minlat={b['minlat']} minlon={b['minlon']}")
    assert e.lat == pytest.approx((b["minlat"] + b["maxlat"]) / 2)
    assert e.lon == pytest.approx((b["minlon"] + b["maxlon"]) / 2)
    assert 170 < e.uncertainty_m < 190  # half the ~360 m bbox diagonal


def test_war_memorial_is_other(osm):
    e, _ = w.osm_evidence(osm["node/10784770501"])
    assert e.kind == "other" and e.uncertainty_m == 25 and "Memorial" in e.description


def test_battlefield_and_disused_airfield(osm):
    b, _ = w.osm_evidence(osm["node/369522525"])
    assert b.kind == "ground_combat" and b.uncertainty_m == 1000
    a, why = w.osm_evidence(osm["node/6952947810"])
    assert a.kind == "military_site" and why == "disused/abandoned aeroway"


def test_military_aerodrome_ruins_and_markers_skipped(osm):
    # present-day military aerodrome (Trường Sa, Spratlys; operator People's Army of Vietnam): skipped
    assert w.osm_evidence(osm["node/2647317689"]) == (None, "present_day_military")
    assert w.osm_evidence(osm["node/357476290"]) == (None, "ruins_without_military_context")
    assert w.osm_evidence(osm["node/2645906877"]) == (None, "monument_not_war_related")


# ---- Wikidata: radii, P276 locations, dates, aviation, burials, coordinate ranks -----------------
def test_operations_and_campaigns_get_at_least_25km(wd_built):
    recs, _ = wd_built
    for q in ("Q42886486", "Q20984292"):  # Lincoln, Oregon
        assert recs[q].uncertainty_m >= 25_000, q
    assert recs["Q247253"].uncertainty_m >= 25_000  # Khe Sanh: 170-day battle


def test_p276_location_noted_and_covered(wd_built):
    recs, _ = wd_built
    ks = recs["Q247253"]
    assert "Location (P276): Khe Sanh" in ks.description
    assert "Gapyeong County" in ks.description and "not covered" in ks.description  # bad far value flagged
    oregon = recs["Q20984292"]  # P276 Huế (Q36399, a province-level city of ~4,947 km²) is 30.6 km
    assert "Huế" in oregon.description and "within its extent" in oregon.description  # from the point: inside it
    assert oregon.uncertainty_m == 25_000
    lincoln = recs["Q42886486"]
    assert "Location (P276): Pleiku" in lincoln.description


def test_date_window_1858_to_1989(wd_built):
    recs, stats = wd_built
    for q in ("Q2890760", "Q109570042", "Q9172651", "Q10829781", "Q18510562", "Q111234364"):
        assert q not in recs, q
    for q in ("Q175767", "Q107610998"):  # 1988; undated battle with a 1972 in its text
        assert q in recs, q
    assert stats["skipped_outside_1858_1989"] >= 4


def test_undated_text_year():
    assert w.text_years("Battle of Như Nguyệt River (1077) Song dynasty-Vietnamese battle") == [1077]
    assert w.text_years("15th century Vietnamese and Chinese battle") == [1400]
    assert w.text_years("part of the Vietnam War (1972)") == [1972]
    assert w.text_years("US military operation") == []


def test_civil_aviation_dropped_military_kept(wd_built):
    recs, stats = wd_built
    for q in ("Q18510562", "Q4698253", "Q1059245", "Q24039782"):
        assert q not in recs, q
    assert recs["Q15402066"].kind == recs["Q1993878"].kind == "aircraft_crash"
    assert stats["skipped_civil_aviation"] >= 3


def test_babylift_is_not_ground_combat(wd_built):
    recs, _ = wd_built
    assert recs["Q2025973"].kind == "other" and "airlift" in recs["Q2025973"].description.lower()


def test_ba_chuc_ossuary_dropped(wd_built):
    recs, stats = wd_built
    assert "Q796398" not in recs and stats["skipped_burial_cemetery_memorial_grave"] >= 1


def test_preferred_rank_coordinate(wd_details, wd_extra):
    e = w.wd_evidence(wd_details["Q478595"], {"airport"}, {"airport"}, wd_extra.get("Q478595"))
    assert e.original_coord == "Point(106.652 10.8188)"  # preferred, though not first when sorted
    assert e.raw["coord_rank"] == "PreferredRank"


def test_equal_rank_coordinates_all_covered(wd_details, wd_extra):
    e = w.wd_evidence(wd_details["Q11824229"], {"airport"}, {"airport"}, wd_extra.get("Q11824229"))
    # three normal-rank points, one ~22 km from the others: chosen deterministically, all covered
    assert e.original_coord == min(c["wkt"] for c in wd_extra["Q11824229"]["coords"])
    assert "3 coordinates of equal rank" in e.description
    for c in wd_extra["Q11824229"]["coords"]:  # every equal-rank point lies inside the radius
        assert w.distance_m(e.lon, e.lat, *w.parse_point(c["wkt"])) <= e.uncertainty_m


# ---- OSM: unnamed memorials and burials ----------------------------------------------------------
@pytest.fixture(scope="module")
def osm_built(osm, active_areas):
    records, stats = w.osm_records(list(osm.values()), active_areas)
    return {r.source_record_id: r for r in records}, stats


def test_unnamed_war_memorials_dropped(osm):
    for k in ("node/12698832683", "way/354299015", "way/1383789529"):
        assert w.osm_evidence(osm[k]) == (None, "unnamed_memorial"), k
    assert w.osm_evidence(osm["node/11063105906"])[0] is not None  # B-52 wreck plaque: inscription


@pytest.mark.parametrize("key", ["node/10723988763", "way/596624577"])
def test_mass_grave_memorials_dropped(osm, key):
    e, why = w.osm_evidence(osm[key])
    assert e is None and why == "burial: known mass grave/ossuary"


@pytest.mark.parametrize(
    "name",
    [
        "Gò mả Tây",
        "Nhà mồ Ba Chúc",
        "Nơi lưu giữ hài cốt",
        "Mộ tập thể",
        "Nghĩa trang xã",
        "nghĩa địa",
        "Ossuary",
        "Mass grave site",
    ],
)
def test_new_burial_name_terms(name):
    assert w.burial_reason({"historic": "memorial", "name": name})


def test_memorial_near_filtered_burial_dropped(osm_built):
    recs, stats = osm_built
    assert "node/6314163138" not in recs  # 131 m from Đài tưởng niệm Liệt sĩ Hạ Long
    assert "way/888611681" not in recs  # 25 m from huyện Đồng Hỷ martyrs' memorial
    # ordinary cemeteries now come from the burial query: Ngọc Hồi victory statue 222 m from Nghĩa trang
    # Liệt sỹ Ngọc Hồi (landuse=cemetery), La Ngà victory statue 132 m from an unnamed landuse=cemetery
    assert "node/5244555570" not in recs and "node/6816764807" not in recs
    assert stats["skipped_memorial_near_burial"] == 2
    assert "node/11063105906" in recs and "node/10784770501" in recs


def test_osm_present_day_not_in_records(osm_built):
    assert "way/371124467" not in osm_built[0]


# ---- Wikidata: battle radius, P276 named in the title, named sea, nationwide campaigns -----------
def test_battle_radius_ignores_subclass_roots(wd_details):
    # every battle type is a subclass of "military operation"; that must not make it an operation
    e = w.wd_evidence(wd_details["Q60524462"], {"battle"}, {"battle", "military operation"})
    assert e.uncertainty_m == 10_000


def test_p276_named_in_title_is_covered_even_when_far():
    detail = {
        "item": {"value": "http://www.wikidata.org/entity/Q4845763"},
        "label_en": {"value": "Battle of Loc Ninh"},
        "types": {"value": "http://www.wikidata.org/entity/Q178561|battle"},
        "dates": {"value": "P580|1972-04-04T00:00:00Z|11;P582|1972-04-07T00:00:00Z|11"},
    }
    extra = {
        "coords": [{"wkt": "Point(106.27027778 11.41888889)", "rank": "NormalRank", "st": "s1"}],
        "locations": [
            {"qid": "Q6711776", "label": "Lộc Ninh", "wkt": "Point(106.579166666 11.858333333)"},
            {"qid": "Q6711776", "label": "Lộc Ninh", "wkt": "Point(106.57854 11.85851)"},
        ],
    }
    e = w.wd_evidence(detail, {"battle"}, {"battle"}, extra)  # real rows: P276 is 59 km from P625
    assert e.uncertainty_m >= 59_000 and e.description.count("Lộc Ninh") == 1


def test_named_sea_covered_country_not(wd_details):
    detail = {
        "item": {"value": "http://www.wikidata.org/entity/Q482402"},
        "label_en": {"value": "Gulf of Tonkin incident"},
        "types": {"value": "http://www.wikidata.org/entity/Q1261499|naval battle"},
    }
    extra = {
        "coords": [{"wkt": "Point(107.25 19.6)", "rank": "NormalRank", "st": "s"}],
        "locations": [
            {"qid": "Q1036098", "label": "Gulf of Tonkin", "wkt": "Point(108.0 20.5)"},
            {"qid": "Q180573", "label": "South Vietnam", "wkt": "Point(106.695 10.777)"},
        ],
    }
    e = w.wd_evidence(detail, {"battle"}, {"battle"}, extra)
    assert e.uncertainty_m >= w.distance_m(107.25, 19.6, 108.0, 20.5)  # the named gulf is covered
    assert "South Vietnam (country-level)" in e.description and e.kind == "other"


def test_nationwide_campaign_dropped():
    tet = {
        "types": {"value": "http://www.wikidata.org/entity/Q831663|military campaign"},
        "label_en": {"value": "Tet Offensive"},
    }
    extra = {"coords": [], "locations": [{"qid": "Q180573", "label": "South Vietnam", "wkt": None}]}
    assert w.is_nationwide_campaign(tet, extra)
    assert not w.is_nationwide_campaign(tet, {"coords": [], "locations": []})


# ---- Present-day military sites, site radii, undated events --------------------------------------
def test_undated_events_dropped(wd_built):
    # Operation Iron Hand (no date, no year in its text); Battle of Battambang (no date, no description);
    # Vietnamese occupation of Poulo Wai (no date): none can be placed inside 1858-1989
    recs, stats = wd_built
    for q in ("Q7097174", "Q9172579", "Q12009712"):
        assert q not in recs, q
    assert stats["skipped_undated_event"] >= 1
    assert "Q107610998" in recs  # undated battle, but its text dates it (1972): kept


@pytest.mark.parametrize("q", ["Q619905", "Q1973031", "Q4115006", "Q43896632", "Q478595"])
def test_wd_present_day_military_sites_dropped(wd_built, q):
    # Bien Hoa (no historic marker), Cam Ranh / Tan Son Nhut AB (Vietnam People's Army/Air Force),
    # Military Medical University (education organization), Tan Son Nhat International Airport
    recs, stats = wd_built
    assert q not in recs
    assert stats["skipped_present_day_military"] >= 5


@pytest.mark.parametrize("q", ["Q7876581", "Q97301157", "Q17064236"])
def test_wd_military_sites_outside_vietnam_war_area_or_active_dropped(wd_built, q):
    recs, stats = wd_built  # Ubon (Thailand), Sanya (China), Ream Naval Base (active, Cambodia)
    assert q not in recs
    assert stats["skipped_outside_indochina"] + stats["skipped_outside_vietnam"] >= 2


@pytest.mark.parametrize(
    "q",
    [
        "Q18391342",
        "Q18639487",
        "Q27627856",
        "Q109795843",
        "Q877185",
        "Q7068799",
        "Q65068235",
        "Q60785495",
        "Q10825547",
        "Q4240280",
    ],
)
def test_wd_historic_military_sites_kept(wd_built, q):
    # former / US / Australian / citadel / French Indochina / South Vietnam; Saigon citadel of 1836 (taken by the French
    # in 1859); Sơn Tây citadel (1822, taken 1883: "Ancient" in its English label is not a pre-1858 date)
    assert wd_built[0][q].kind == "military_site"


def test_wd_site_radius_covers_installation(wd_built, wd_details, wd_extra):
    recs, _ = wd_built
    phu_cat = w.wd_evidence(wd_details["Q7188237"], {"base"}, {"airbase"}, wd_extra.get("Q7188237"))
    assert phu_cat.uncertainty_m >= 5000  # an air base: runways + dispersal, km across
    assert "footprint" in phu_cat.description
    assert recs["Q27627856"].uncertainty_m >= 10_000  # Long Binh Post: ~25 sq mi logistics complex
    assert recs["Q18391342"].uncertainty_m >= 5000  # Camp Eagle: division base camp
    assert recs["Q877185"].uncertainty_m >= 3000  # Huế citadel: ~2.5 km walls
    assert recs["Q18639487"].uncertainty_m >= 1000  # Ben Het special-forces camp
    assert recs["Q109795843"].uncertainty_m >= 1000  # fire support base
    assert all(r.uncertainty_m >= 1000 for r in recs.values() if r.kind == "military_site")


@pytest.mark.parametrize(
    "key", ["relation/16294194", "way/1158398951", "way/1303522938", "way/963999218", "way/358573470"]
)
def test_osm_active_installations_founded_before_1975_skipped(osm, key):
    # academy (operator Bộ Quốc Phòng, 1960), naval academy (1955), army library (opening hours, 1972),
    # 101st marine brigade (unit name, 1964), Nước Mặn airfield (still landuse=military, access=no)
    assert w.osm_evidence(osm[key]) == (None, "present_day_military")


def test_osm_founding_year_alone_is_not_wartime():
    el = {
        "type": "node",
        "id": 1,
        "lat": 16.0,
        "lon": 108.0,
        "tags": {"landuse": "military", "military": "base", "name": "Căn cứ Đồng Dù", "start_date": "1966"},
    }
    assert w.osm_evidence(el) == (None, "present_day_military")
    old = dict(el, tags=dict(el["tags"], description="Căn cứ cũ của quân đội Hoa Kỳ"))
    assert w.osm_evidence(old)[0] is not None


def test_osm_place_name_my_is_not_american_and_command_posts_skipped(osm):
    # 'Cẩm Mỹ' is a district, not 'Mỹ' (American); 'BCH Quân sự' is a serving district command
    assert w.osm_evidence(osm["way/726864822"]) == (None, "present_day_military")


def test_wd_wartime_hospital_and_ground_base_kept_air_base_without_marker_dropped(wd_built):
    recs, _ = wd_built
    assert "Q110967638" in recs  # 85th Evacuation Hospital: United States Army, historic
    assert "Q18639566" in recs  # Cửa Việt Base: ground base founded 1966
    assert "Q10822627" not in recs  # Hòa Lạc Air Base (1967): air bases are reused; no marker


def test_disused_aeroway_inside_active_airport_dropped(osm, osm_built):
    # an abandoned taxiway inside Tân Sơn Nhất (an active civil/military airport) is part of a
    # present-day installation, not a wartime site
    assert w.osm_evidence(osm["way/197139989"])[0] is not None  # alone it looks disused
    recs, stats = osm_built
    assert "way/197139989" not in recs and stats["skipped_inside_active_aerodrome"] == 1
    assert "node/6952947810" in recs  # disused airfield on its own: kept


# ---- Wikidata: site dates, handovers, border area, kinds, crimes ---------------------------------
def test_site_inception_or_dissolution_is_not_an_event_date(wd_built):
    # Quảng Trị citadel (built 1809, fought over 1972): a site founded before 1858 with no end date stood
    # through the window; its date is described as the inception it is
    recs, _ = wd_built
    qt = recs["Q10825609"]
    assert qt.kind == "military_site" and qt.date_start == "1809"
    assert "inception (P571) 1809" in qt.description and "not an event date" in qt.description
    assert w.site_in_window({"P571": ["1809"]}) and w.site_in_window({"P576": ["2012-12-02"]})
    assert not w.site_in_window({"P576": ["1802"]}) and not w.site_in_window({"P571": ["1995"]})


def test_closed_civil_airport_is_not_military(wd_details):
    # Old Phu Quoc Airport: "former airport", P576 2012; nothing says military, so not kept as a site
    assert w.airport_is_military(wd_details["Q203395"], {"airport"}) is None


def test_air_base_handover_year_is_not_a_closure(wd_built):
    # Phu Cat Air Base: P576 = 1975 is when the US left; it is an active air force base (its airport twin
    # Q193408 is dropped too)
    recs, stats = wd_built
    assert "Q7188237" not in recs and "Q193408" not in recs and "Q18737191" not in recs


def test_wd_site_inside_present_day_installation_dropped(wd_built):
    # Camp Alpha (US processing centre) lies inside today's Tân Sơn Nhất (OSM way/54242195)
    recs, stats = wd_built
    assert "Q110974606" not in recs and stats["skipped_inside_present_day_military"] >= 1


def test_p276_places_in_region_always_covered(wd_built):
    recs, _ = wd_built
    el_paso = recs["Q30595417"]  # item point 91 km from Bình Long, where it was fought
    assert el_paso.uncertainty_m >= w.distance_m(106.8, 10.9, 106.601, 11.694)
    assert "Location (P276): Bình Long" in el_paso.description
    # a river's or a gulf's one point is not a place to reach: test_p276_river_or_gulf_is_not_a_point_to_reach
    assert "not covered" in recs["Q247253"].description  # Khe Sanh -> Gapyeong County (Korea): bad value


@pytest.mark.parametrize("q", ["Q702579", "Q5759430", "Q85760415", "Q9172579", "Q12009712", "Q63045308"])
def test_events_and_sites_outside_vietnam_dropped(wd_built, q):
    # Ko Chang (Thai waters, 1941), Fall of Phnom Penh x2 (Cambodian civil war, 70 km from the border),
    # Battambang (Cambodia, undated), Poulo Wai (Cambodian island, undated), French Lighthouse (Cambodia)
    assert q not in wd_built[0]


def test_border_area_events_kept_and_say_so(wd_built):
    recs, stats = wd_built
    for q in ("Q4872400", "Q4871558", "Q4871827"):  # Snuol (Cambodia), Lima Site 85, Muong Khoua (Laos)
        assert "outside vietnam's mapped boundary" in recs[q].description.lower(), q
    assert "Outside Vietnam's mapped boundary (Laos)" in recs["Q4871827"].description
    assert stats["skipped_outside_vietnam"] >= 3
    assert "Outside" not in recs["Q60524462"].description  # Tri Phap, inside Vietnam


def test_kinds_follow_what_happened(wd_built):
    recs, stats = wd_built
    assert recs["Q2627940"].kind == "air_strike"  # 1962 Independence Palace bombing: aerial attack
    for q in ("Q482402", "Q175767", "Q5037043"):  # naval battles are not ground combat
        assert recs[q].kind == "other", q
    assert "Q5951632" not in recs and stats["skipped_not_a_war_event"] == 1  # tear gas on protesters


def test_criminal_cases_dropped_war_crimes_kept(wd_built, wd_details):
    recs, stats = wd_built
    assert "Q111234364" not in recs and stats["skipped_criminal_case"] == 1
    assert "Q183421" in recs  # My Lai: "war crime" is not a criminal case
    undated = {k: v for k, v in wd_details["Q111234364"].items() if k != "dates"}
    undated["desc_en"] = {"value": "Vietnam criminal case"}
    assert w.wd_skip_reason("Q111234364", undated, {"massacre"}, set()) == "criminal_case"
    in_window = dict(wd_details["Q111234364"], dates={"value": "P585|1972-07-07T00:00:00Z|11"})
    assert w.wd_skip_reason("Q111234364", in_window, {"massacre"}, set()) == "criminal_case"


def test_date_query_binds_statement_values_not_qualifiers(wd_details):
    q = w.wd_detail_query(["Q7097714"])
    for p in w.WD_DATE_PROPS:
        assert f"(p:{p} psv:{p})" in q
    assert "VALUES (?dp ?psv)" in q
    # Operation Wheeler/Wallowa has two real P582 statements (1968-02, 1968-11-11); the end is the latest
    dates, start, end = w.wd_dates(wd_details["Q7097714"])
    assert dates["P582"] == ["1968-02", "1968-11-11"] and (start, end) == ("1967-09-11", "1968-11-11")


# ---- OSM: memorial complexes, installations, exhibits, steles, pre-1858, footprints --------------
@pytest.mark.parametrize("key", ["node/4409284591", "node/5353612634"])
def test_memorial_complexes_with_their_dead_buried_dropped(osm, key):
    # Đồng Lộc junction (the ten volunteers' graves are in the complex), Hang Tám Cô (the eight buried there)
    assert w.osm_evidence(osm[key]) == (None, "burial: known mass grave/ossuary")


def test_fallen_soldiers_altar_is_a_martyrs_memorial(osm):
    assert w.osm_evidence(osm["node/6251472272"]) == (None, "burial: martyrs' memorial")  # trận vong


def test_roc_memorial_not_at_a_mapped_burial(osm, osm_built):
    # Phú Quốc memorial to Republic of China soldiers who died of illness: the nearest mapped cemetery
    # (Nghĩa trang liệt sĩ Phú Quốc, way/1117543945) is 675 m away and its tags name no grave
    e = osm["node/13357233094"]
    assert 600 < w.distance_to_element_m(e["lon"], e["lat"], osm["way/1117543945"]) < 700
    assert "node/13357233094" in osm_built[0]


@pytest.mark.parametrize(
    "key",
    [
        "way/1447362087",
        "node/7685720243",
        "way/1024899770",
        "node/13675000046",
        "way/1160479180",
        "node/5256962158",
        "node/7375958122",
    ],
)
def test_features_inside_present_day_installations_dropped(osm, osm_built, key):
    # bunkers inside Cam Ranh naval base, Thành Sơn and Kép air bases, a VPA barracks and Hòn Hải
    # (landuse=military); the victory statue inside Military Region 9 HQ; the Phước Thành memorial at the
    # edge (3 m) of Lữ đoàn 171. Alone they look historic; inside a serving base they are out of scope.
    recs, stats = osm_built
    assert w.osm_evidence(osm[key])[0] is not None
    assert key not in recs
    assert stats["skipped_inside_present_day_military"] >= 7


def test_present_day_check_runs_on_bunkers():
    el = {"type": "node", "id": 1, "lat": 16.0, "lon": 108.0, "tags": {"military": "bunker", "access": "military"}}
    assert w.osm_evidence(el) == (None, "present_day_military")
    # a disused bunker named "Commander bunker" is not a serving command
    assert w.osm_evidence(
        {
            "type": "node",
            "id": 5922616509,
            "lat": 11.145,
            "lon": 106.464,
            "tags": {"disused": "yes", "military": "bunker", "name": "Commander bunker"},
        }
    )[0]


@pytest.mark.parametrize("key", ["node/9960625535", "node/9960625539"])
def test_equipment_on_display_dropped(osm, key):
    # "Pháo 130 K-59-1", "Máy bay 6144": weapons on display beside Biên Hòa air base, not event sites
    assert w.osm_evidence(osm[key]) == (None, "equipment_exhibit")


@pytest.mark.parametrize(
    "key",
    [
        "node/12193933204",
        "node/11086025940",
        "node/6894264787",
        "node/7022951440",
        "node/6535217831",
        "node/12143156601",
        "node/7968147349",
        "node/5390051726",
        "node/7513022915",
        "node/11784634072",
        "node/13874818779",
    ],
)
def test_non_war_steles_and_ruins_dropped(osm, key):
    # typhoon victims, reservoir inauguration, royal / heritage-ranking / poetic steles, a scholar, a gift
    # to Lũng Cú residents, Uncle Ho statue, 1920 pigeon houses, a Buddhist monastery, church ruins
    e, why = w.osm_evidence(osm[key])
    assert e is None and why in ("monument_not_war_related", "ruins_without_military_context"), (key, why)


def test_war_words_whole_words_only():
    assert not w.WAR_RE.search("công trình tặng đồng bào lũng cú")  # "đồng" is not "đồn"
    assert w.WAR_RE.search("di tích đồn cao") and w.WAR_RE.search("bót catinat")


@pytest.mark.parametrize("key", ["node/6278479148", "node/4305631603", "node/4524565893", "node/14076086101"])
def test_pre_1858_sites_dropped(osm, key):
    # Bạch Đằng (938/1288), Mạc-dynasty citadel (16th c.), Cham tower, archaeological site
    assert w.osm_evidence(osm[key]) == (None, "pre_1858_site")


def test_tran_hung_dao_forest_is_not_pre_1858(osm):
    # Khu di tích rừng Trần Hưng Đạo (Cao Bằng) is where the Propaganda Liberation Army was founded in
    # 1944; named after the 13th-century general, but not a 13th-century site. Nothing in its tags says war.
    assert w.osm_evidence(osm["node/11596828663"]) == (None, "monument_not_war_related")


def test_artillery_position_is_not_a_battlefield(osm):
    e, _ = w.osm_evidence(osm["node/10291730841"])
    assert e.kind == "military_site" and "not a combat site" in e.description


def test_citadel_ruins_kept(osm):
    assert w.osm_evidence(osm["way/478473546"])[0].kind == "military_site"  # Di tích thành Sơn Phòng


@pytest.mark.parametrize(
    "key,radius",
    [
        ("node/4708186090", 3000),  # Nui Dat 1 ATF base
        ("node/6952947810", 5000),  # Đắk Tô Base Camp (airfield)
        ("node/13122726423", 5000),  # Phi trường Cam Ly
        ("node/5560875621", 3000),  # US naval base Sa Đéc
        ("node/11777724187", 1000),  # Căng Bắc Mê (barracks)
        ("node/13765071859", 1000),  # historic=fort
        ("node/9240359646", 1000),  # Di tích Đồn Cao (fort)
    ],
)
def test_node_for_a_whole_installation_sized_for_footprint(osm, key, radius):
    e, _ = w.osm_evidence(osm[key])
    assert e.uncertainty_m >= radius and "footprint" in e.description


def test_small_objects_keep_small_radius(osm):
    assert w.osm_evidence(osm["node/3482754596"])[0].uncertainty_m == 100  # a trench ("Kitchen")
    assert w.osm_evidence(osm["node/5922616509"])[0].uncertainty_m == 25  # a bunker


def test_region_point_in_polygon_and_distance(vn):
    assert vn.contains(105.85, 21.03) and vn.contains(106.0, 11.5)  # Hà Nội; Tây Ninh
    assert not vn.contains(104.921, 11.5696)  # Phnom Penh
    assert 50_000 < vn.distance_m(104.921, 11.5696) < 90_000


# ---- Pre-1858 sites, present-day overlap, P276 classes, aerodromes -------------------------------
@pytest.mark.parametrize("q", ["Q190060", "Q8079948", "Q10841882", "Q2234243", "Q5122391", "Q10825533"])
def test_wd_pre_1858_sites_dropped(wd_built, q):
    # Citadel of the Hồ dynasty (1397, "Thành nhà Hồ"), Đại La and Phú Xuân (Wikidata type "ancient city"),
    # Đồ Bàn (the Champa capital Vijaya), and the 1790 Gia Định / Bát Quái citadel (razed 1835, before the
    # window): the same pre-1858 rule as on the OSM side
    recs, stats = wd_built
    assert q not in recs
    assert stats["skipped_pre_1858_site"] == 6


def test_wd_premodern_rule_matches_osm_rule():
    assert w.PREMODERN_RE.search(w.norm("Thành nhà Hồ")) and w.PREMODERN_RE.search(w.norm("thành nhà Mạc"))
    assert not w.SITE_HISTORIC_RE.search("ancient city")  # "ancient" is not a marker of the war era


def test_site_point_in_time_is_not_called_built_or_closed(wd_built):
    # Con Thien: only P585 (point in time) 1967-02-27; Wikidata does not say what that date marks
    con_thien = wd_built[0]["Q1626571"]
    assert "built / closed" not in con_thien.description
    assert "point in time (P585) 1967-02-27" in con_thien.description
    assert con_thien.date_start is None and con_thien.date_end is None


@pytest.mark.parametrize("q", ["Q23134570", "Q60753289", "Q18640048"])
def test_wd_bases_beside_present_day_installations_dropped(wd_built, q):
    # Cat Lo Naval Base 362 m from the Coast Guard Region 3 HQ (naval_base), Camp Horn 489 m from a
    # landuse=military area, Quản Lợi Base Camp 746 m from Trung đoàn Bộ binh 736: a base whose point lies
    # within 1 km of a serving installation is taken to be reused by it
    recs, stats = wd_built
    assert q not in recs
    assert stats["skipped_next_to_present_day_military"] == 3


def test_wd_site_radius_covering_present_day_installation_says_so(wd_built):
    # Phu Quoc Prison (a POW camp, not a base) lies 774 m from a landuse=military base: kept, but the
    # description says its circle takes in a present-day installation that is out of scope
    d = wd_built[0]["Q10800918"].description
    assert "present-day military installation" in d and "way/1521154235" in d


@pytest.mark.parametrize("q,area", [("Q55315758", "way/1081333880"), ("Q5037043", "way/265244996")])
def test_wd_event_point_inside_present_day_installation_says_so(wd_built, q, area):
    # Tet attacks on Biên Hòa (point inside Biên Hòa air base), Capture of Mỹ Tho (point inside a provincial
    # military command): kept, the event spreads far beyond, but the analyst is told the area is out of scope
    d = wd_built[0][q].description
    assert "inside present-day military installation" in d and area in d


def test_p276_region_is_not_a_point_to_reach(wd_built):
    recs, _ = wd_built
    nghia_lo = recs["Q4871890"]  # P276 Tonkin: a region whose representative point is 169 km away
    assert nghia_lo.uncertainty_m == 10_000
    assert "Tonkin" in nghia_lo.description and "region" in nghia_lo.description
    # Quyet Thang 202: P276 is Q33994, the provincial city of Quảng Ngãi (not the province): a place, covered
    qt = recs["Q3381214"]
    assert qt.uncertainty_m >= w.distance_m(108.1289381, 15.1794882, 108.792222222, 15.120555555)
    # El Paso: P276 Bình Long province (no area in Wikidata), 91 km from the point: the point lies outside
    # any province-sized extent around it, so the radius still reaches it
    assert recs["Q30595417"].uncertainty_m >= 90_000


def test_location_classes(wd_locations):
    assert w.location_class(wd_locations["Q622778"])[0] == "region"  # Tonkin
    assert w.location_class(wd_locations["Q33994"])[0] == "place"  # Quảng Ngãi (city)
    kind, inside = w.location_class(wd_locations["Q36399"])  # Huế: province-level, 4,947 km²
    assert kind == "province" and 55_000 < inside < 65_000
    assert w.location_class(wd_locations["Q1760239"]) == ("province", w.PROVINCE_DEFAULT_M)  # Bình Long
    assert w.location_class(None)[0] == "place"


def test_offshore_coordinate_not_described_as_abroad(wd_built):
    # Operation Hong Kil Dong: no P17, coordinate 20 km outside the mapped boundary; nothing in the record
    # puts it in another country
    d = wd_built[0]["Q497570"].description
    assert "Outside Vietnam's boundary (per its coordinate)" not in d
    assert "names no country" in d


def test_trenches_are_not_present_day_installations(active_areas, osm_built):
    keys = {a.key for a in active_areas}
    assert "way/1165154442" not in keys  # historic trench line at A1 Hill, Điện Biên Phủ
    assert "way/1460504144" not in keys  # landuse=military outline described as "Cat Ba Cannon Fort"
    recs, _ = osm_built
    assert "node/3294335515" in recs  # A1 HILL battlefield, 29 m from that trench
    assert "way/458851649" in recs  # Cannon Fort (historic=fort)


def test_civil_disused_airport_dropped(osm, osm_built):
    # Phu Quoc Old Airport (IATA PQC, operator Southern Airports Services Company): civil, as its Wikidata
    # twin Q203395; the road laid on its runway goes with it
    assert w.osm_evidence(osm["way/1135316110"]) == (None, "civil_aerodrome")
    recs, stats = osm_built
    assert "way/934254484" not in recs and stats["skipped_inside_civil_aerodrome"] == 1


def test_disused_aeroway_kind_needs_military_evidence(osm):
    assert w.osm_evidence(osm["node/6952947810"])[0].kind == "military_site"  # disused:military=airfield
    assert w.osm_evidence(osm["way/477496030"])[0].kind == "military_site"  # Nui Dat runway (1 ATF base)
    bare = w.osm_evidence(osm["way/1041696984"])[0]  # abandoned:aeroway=runway only
    assert bare.kind == "other" and "do not say who built or used it" in bare.description
    assert w.osm_evidence(osm["way/870400493"])[0].kind == "other"  # Tà Cơn runway: no military tag


@pytest.mark.parametrize(
    "key,why",
    [
        ("node/3812282642", "not_a_war_event"),  # Thích Quảng Đức (1963 protest)
        ("node/7368395546", "not_a_war_event"),  # students' enlistment square
        ("node/14152844701", "equipment_exhibit"),  # "Military Bulldozer"
    ],
)
def test_non_war_and_exhibit_memorials_dropped(osm, key, why):
    assert w.osm_evidence(osm[key]) == (None, why)


def test_roc_memorial_says_what_it_commemorates(osm_built):
    d = osm_built[0]["node/13357233094"].description
    assert "died of illness" in d and "marks a commemorated event" not in d
    assert "not stated" in d


# ---- P276 features and provinces, coarse points, named places, boundary, trenches ----------------
def test_p276_river_or_gulf_is_not_a_point_to_reach(wd_built, wd_locations):
    assert w.location_class(wd_locations["Q2904975"])[0] == "feature"  # Ia Drang River
    assert w.location_class(wd_locations["Q212428"])[0] == "feature"  # Gulf of Tonkin
    recs, _ = wd_built
    # Battle of Ia Drang: its coordinate lies ~1.5 km from LZ X-Ray and LZ Albany; the river's one point
    # is 50.6 km away and says nothing about where along the river the battle was
    ia = recs["Q714749"]
    assert ia.uncertainty_m == 10_000
    assert "Ia Drang River, a river" in ia.description and "item coordinate is off" not in ia.description
    tonkin = recs["Q482402"]
    assert tonkin.uncertainty_m == w.NAVAL_RUNNING_RADIUS
    assert "Gulf of Tonkin, a gulf" in tonkin.description and "item coordinate is off" not in tonkin.description
    assert w.location_class({"types": ["river island"], "area_km2": None})[0] == "place"


def test_province_without_area_does_not_claim_point_outside(wd_built):
    recs, _ = wd_built
    for q in ("Q4872414", "Q30595417"):  # Battle of Song Be (Sông Bé province), Operation El Paso (Bình Long)
        d = recs[q].description
        assert "looks to lie outside" not in d and "gives no area" in d, q
    assert recs["Q4872414"].uncertainty_m >= 96_000  # still reaches the province's point: never smaller


def test_coarse_coordinate_widening_is_described(wd_built):
    recs, _ = wd_built
    for q in ("Q18737122", "Q106963887"):  # Đức Phong Camp, Hill 100: Point(107.23 11.8), Point(108.2 15.85)
        e = recs[q]
        assert e.uncertainty_m == 7872
        assert "1 decimal" in e.description and "7.9 km" in e.description, q


def test_site_radius_takes_larger_of_title_and_type():
    # Đức Phong Camp: title says camp (1 km), Wikidata type says airbase (5 km): the larger wins
    assert w.site_radius("Đức Phong Camp", ["airbase"], ())[0] == 5000
    assert w.site_radius("Firebase Ripcord", ["fire support base"], ())[0] == 1000


def test_site_kept_on_country_alone_says_so(wd_built):
    d = wd_built[0]["Q65068235"].description  # Do Son Airfield: only marker is P17 = French Indochina
    assert "only sign" in d and "French Indochina" in d and "in use today" in d
    assert "way/1484018377" in d  # the circle takes in that installation, and says so


def test_imperial_is_not_a_historic_marker():
    assert not w.SITE_HISTORIC_RE.search("imperial guard barracks")


def test_wartime_trenches_are_kept_as_field_works(osm, osm_built):
    # A1 Hill trench lines (barrier=ditch, military=trench) and Củ Chi tunnel entrances: field works,
    # never a present-day installation
    for k in ("way/1165154442", "way/1165154435", "node/3482754609", "node/3482754612"):
        e, why = w.osm_evidence(osm[k])
        assert why == "military=trench" and e.kind == "military_site", k
    recs, stats = osm_built
    for k in ("way/1165154442", "way/1165154435", "node/3482754609"):
        assert k in recs and "trench" in recs[k].description.lower(), k
    assert "A1 HILL" in recs["way/1165154442"].description or "node/3294335515" in recs["way/1165154442"].description


def test_trenches_without_wartime_context_dropped_truthfully(osm_built):
    # Sóc Sơn trench lines: no date, era or mapped wartime site nearby; Hòa Lạc (inside present-day
    # way/1016759175) and Cam Ranh (inside relation/12175787) trench lines: inside present-day installations
    recs, stats = osm_built
    for k in ("way/1146171154", "way/1073109935", "way/1447538851"):
        assert k not in recs, k
    assert stats["skipped_trench_no_wartime_context"] + stats["skipped_inside_present_day_military"] >= 3
    assert stats["skipped_trench_no_wartime_context"] >= 1


def test_all_massacres_are_loss_incidents(wd_built):
    recs, _ = wd_built
    for q in ("Q183421", "Q96408031", "Q96418346"):  # My Lai, Thanh My, Duc Duc
        assert recs[q].kind == "loss_incident" and recs[q].description.startswith("Massacre."), q
    assert not [r for r in recs.values() if "massacre" in r.raw["matched_groups"] and r.kind != "loss_incident"]


def test_named_places_from_own_labels():
    assert w.named_places(["Bù Đốp massacre", "Thảm sát Bù Đốp", "massacre de Bù Đốp"]) == ["Bù Đốp"]
    assert w.named_places(["Battle of A Shau", "Trận A Sầu", None]) == ["A Shau", "A Sầu"]
    assert w.named_places(["Battle of Snuol", None, "Snuol (1971)"]) == ["Snuol"]
    assert w.named_places(["Gulf of Tonkin incident", "Sự kiện Vịnh Bắc Bộ", None]) == []
    assert w.gns_key("Huyện Bù Đốp") == "huyen bu dop"


def test_coordinate_contradicting_named_district_dropped(wd_built, gazetteer):
    # Bù Đốp massacre: label and description say "in Bù Đốp"; its Point(104.9 10.5) is ~250 km from Bù Đốp
    # district (GNS ADM2 at 106.80, 11.99, Bình Phước) and from every other GNS Bù Đốp
    recs, stats = wd_built
    assert "Q136027708" not in recs
    assert stats["skipped_coordinate_contradicts_named_place"] == 1
    ((status, name, f, d),) = w.named_place_check(104.9, 10.5, 7872, ["Bù Đốp"], gazetteer)
    assert status == "contradicts" and d > 240_000


def test_named_place_within_reach_is_covered(wd_built, gazetteer):
    recs, _ = wd_built
    # Duc Duc massacre: Point(108.11 15.751); GNS Đức Dục hamlets at 108.05, 15.767 / 15.783 (unc 1320 m)
    dd = recs["Q96418346"]
    edge = w.distance_m(108.11, 15.751, 108.05, 15.766667) - 1320
    assert dd.uncertainty_m >= edge > 3000
    assert "Its label names Duc Duc" in dd.description and "Đức Dục" in dd.description
    # Battle of A Shau: Point(107.213 16.2593); GNS A Shau village at 107.3667, 16.1 (the stream point is not
    # reached for: a river's one point)
    ash = recs["Q378392"]
    assert ash.uncertainty_m >= w.distance_m(107.213, 16.2593, 107.366667, 16.1) - 1320 > 10_000
    assert ash.description.count("Its label names") == 1  # "A Shau" and "A Sầu" are one place
    for e in recs.values():  # whatever an event's label names in GNS within reach lies inside its radius
        assert all(c["status"] != "reach" or e.uncertainty_m >= c["reach_m"] for c in e.raw["named_places"])


def test_far_commune_namesake_is_a_homonym_not_reached(gazetteer):
    # Battle of Ky Hoa (Saigon, 1861) Point(106.667 10.767); the only GNS Kỳ Hoa is a commune in Hà Tĩnh
    ((status, _, f, d),) = w.named_place_check(106.667, 10.767, 10_000, ["Ky Hoa"], gazetteer)
    assert status == "homonym" and f["dsg"] == "ADM3" and d > 800_000


def test_name_of_p276_river_is_that_river_not_a_namesake(wd_built):
    # Battle of Ia Drang: P276 is the Ia Drang River, so "Ia Drang" is the river; GNS Xã Ia Drang (a commune
    # ~21 km away) is a namesake and is not reached for
    ia = wd_built[0]["Q714749"]
    assert ia.raw["named_places"] == [] and ia.uncertainty_m == 10_000


def test_multi_day_naval_engagement_radius_explained(wd_built):
    t = wd_built[0]["Q482402"]  # Gulf of Tonkin incident, 1964-08-02 to 1964-08-04
    assert t.uncertainty_m == 25_000
    assert "1964-08-02 to 1964-08-04" in t.description and "may lie beyond this radius" in t.description
    # Wikidata gives only the dates, "naval battle", "naval confrontation" and P276 Gulf of Tonkin: nothing
    # about aircraft, a running fight or its extent, so neither is claimed and 25 km is called a default
    for said in ("running fight", "aircraft", "tens of km"):
        assert said not in t.description, said
    assert "project default, not a figure from Wikidata" in t.description
    assert wd_built[0]["Q175767"].uncertainty_m == 10_000  # Johnson South Reef: one day, at the reef


def test_radius_crossing_national_boundary_is_flagged(wd_built, vn):
    # Battle of Bang Bo (1885) Point(106.7517508 21.9897471): inside Vietnam's mapped boundary but ~2 km from
    # the China border, 10 km radius, no P17 country, no P276: the analyst is told the radius crosses it
    b = wd_built[0]["Q4870413"]
    assert vn.contains(b.lon, b.lat) and vn.distance_m(b.lon, b.lat) < 5_000 and b.uncertainty_m == 10_000
    assert "inside Vietnam's mapped boundary but" in b.description
    assert "radius extends beyond that boundary" in b.description
    assert "Wikidata names no country for it" in b.description
    assert b.raw["boundary_m"] == round(vn.distance_m(b.lon, b.lat))
    for e in wd_built[0].values():  # flagged exactly when the radius reaches past the boundary
        crosses = vn.contains(e.lon, e.lat) and vn.distance_m(e.lon, e.lat) < e.uncertainty_m
        assert ("radius extends beyond that boundary" in e.description) == crosses, e.source_record_id


def test_far_p276_says_what_it_is_not_why(wd_built):
    q = wd_built[0]["Q3381214"]  # Operation Quyet Thang 202: P276 Quảng Ngãi (a provincial city), 71.5 km
    assert "far:" not in q.description and "item coordinate is off" not in q.description
    assert "Location (P276): Quảng Ngãi (Wikidata type:" in q.description
    assert "the radius is widened to reach it" in q.description and q.uncertainty_m >= 71_000


def test_trench_context_must_itself_be_marked_wartime(osm_built):
    recs, _ = osm_built
    # Kitchen (node/3482754596): historic=yes, military=trench, name=Kitchen: no wartime marker of its own
    assert w.wartime_marker(recs["node/3482754596"]) is None
    for k in ("node/3482754612", "node/3482754613", "node/3482754609"):
        d = recs[k].description
        assert "Kitchen" not in d and "a mapped wartime site" not in d, k
        assert "Địa đạo Bến Đình (way/207074801), tagged historic=battlefield" in d, k
    assert "A1 HILL (node/3294335515), tagged historic=battlefield" in recs["way/1165154442"].description
    for e in recs.values():  # a memorial is never the context
        assert (
            "kept because" not in e.description
            or "historic=battlefield" in e.description
            or "name or description says" in e.description
        )


# ---- Store sync ----------------------------------------------------------------------------------
def snapshot(db):
    return db.execute(
        "select source, source_record_id, kind, lon, lat, uncertainty_m, title, description, "
        "date_start, date_end, raw_json, withdrawn_at from evidence order by source, source_record_id"
    ).fetchall()


def test_sync_twice_is_idempotent(tmp_path, wd_built, osm_built):
    db = store.connect(tmp_path / "e.sqlite")
    batches = {"wikidata": list(wd_built[0].values()), "osm": list(osm_built[0].values())}
    for sid, recs in batches.items():
        store.upsert_source(db, sid, name=sid)
        assert w.sync(db, sid, recs) == (len(recs), 0, 0)
    first = snapshot(db)
    for sid, recs in batches.items():
        assert w.sync(db, sid, recs) == (len(recs), 0, 0)
    assert snapshot(db) == first
    # a record the source stops yielding is removed by the sync
    assert w.sync(db, "osm", batches["osm"][1:]) == (len(batches["osm"]) - 1, 1, 0)
