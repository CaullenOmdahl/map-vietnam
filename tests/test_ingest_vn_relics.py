"""Parsing tests for the vi.wikipedia national-relic ingester, on tiny real excerpts
(tests/fixtures/vn_relics/: two wikitext excerpts, a gns_index.json excerpt, one
prop=coordinates API answer trimmed to the pages used here and a Wikidata district/commune
answer trimmed to a few districts)."""

import json
from pathlib import Path

import pytest
import store
from ingest import vn_relics as v

FIX = Path(__file__).parent / "fixtures" / "vn_relics"


@pytest.fixture(scope="module")
def main():
    return v.main_rows((FIX / "main_excerpt.wikitext").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def special():
    return v.special_rows((FIX / "special_excerpt.wikitext").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def gaz():
    return v.Gazetteer(
        json.loads((FIX / "gns_index_excerpt.json").read_text(encoding="utf-8")),
        districts=v.parse_district_members(
            json.loads((FIX / "wdqs_district_communes.json").read_text(encoding="utf-8"))
        ),
    )


@pytest.fixture(scope="module")
def coords():
    return v.parse_coords(json.loads((FIX / "coords_api.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def built(main, special, gaz, coords):
    records, stats = v.build(main, special, gaz, coords)
    return {e.title: e for e in records}, stats


def by_name(rows, text):
    return next(r for r in rows if text in v.plain(r["name_wt"]))


# ---- wikitext parsing ------------------------------------------------------------------------
def test_main_rows_columns_and_province(main):
    r = by_name(main, "Xẻo Quít")
    assert r["province"] == "dong thap"
    assert v.plain(r["place_wt"]) == "Xã Mỹ Hiệp"
    assert r["category"] == "Lịch sử" and r["recognised"] == "09/04/1992" and r["notes"] == "420/QĐ"


def test_main_rows_multiline_cells_and_refs(main):
    r = by_name(main, "tập kết ra Bắc")  # one cell per line, ref in the last cell
    assert v.plain(r["place_wt"]) == "Phường Cao Lãnh"
    assert r["recognised"] == "2023" and r["notes"] == ""
    lai = by_name(main, "Đền thờ vua Lê Lợi")  # '|| 2012' then the note on its own '|' line
    assert lai["province"] == "lai chau" and lai["notes"] == "218/QĐ-BVHTTDL"


def test_special_rows_with_and_without_image_column(special):
    dbp = by_name(special, "Điện Biên Phủ")
    assert dbp["province"] == "dien bien"
    assert v.parse_coord_template(dbp["place_wt"]) == ("{{Coord|21|23|13|N|103|0|56|E}}", 103.015556, 21.386944)
    ap = by_name(special, "Ấp Bắc")
    assert ap["province"] == "dong thap" and v.links(ap["name_wt"])[0][0] == "Trận Ấp Bắc"
    nt = by_name(special, "Núi Thành")  # 'đang lập hồ sơ' table: no image column, 'Loại hình'
    assert nt["province"] == "da nang" and nt["category"] == "Lịch sử"


# ---- classification --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,decision,kind",
    [
        ("Căn cứ kháng chiến Tỉnh ủy Kiến Phong (Xẻo Quít)", "keep", "military_site"),
        ("Chiến thắng Giồng Thị Đam - Gò Quản Cung", "keep", "ground_combat"),
        ("Vụ Thảm sát Mỹ Thủy", "keep", "loss_incident"),  # a massacre is a loss of life, not combat
        ("Vụ Thảm sát Bình Thành", "burial", None),  # victims buried in a common pit on site
        # the battery that fired, not where the aircraft fell
        ("Trận địa tên lửa T5, Nơi bắn rơi máy B52 đầu tiên ở Việt Nam", "keep", "military_site"),
        # a command post / prison is a site even when the name mentions a campaign or a death
        ("Sở chỉ huy tiền phương chiến dịch Hồ Chí Minh", "keep", "military_site"),
        ("Sở Chỉ huy tiền phương Phân khu 6 trong chiến dịch Mậu Thân 1968", "keep", "military_site"),
        ("Khu trại giam bệnh viện Chợ Quán - nơi đồng chí Trần Phú hy sinh", "keep", "military_site"),
        (
            "Căn cứ Tà Thiết + Căn cứ BCH Quân giải phóng miền Nam Việt Nam + Căn cứ BCH Chiến dịch Hồ Chí Minh",
            "keep",
            "military_site",
        ),
        # air raids
        ("Địa điểm các Thanh niên xung phong Đại đội 915 hy sinh tại Lưu Xá (12/1972)", "keep", "air_strike"),
        ("Khu di tích Hàm Rồng", "keep", "air_strike"),
        # 'ATK2' is a safe zone; a house listed as part of it '(ATK2)' is not itself a war site
        ("ATK2 (10 di tích)", "keep", "military_site"),
        ("Nhà ông Ngô Văn Thấu (ATK2)", "not_war", None),
        # 'Lăng' is a tomb only as a head noun: 'Chi Lăng' is a place name
        ("Di tích lịch sử Chi Lăng", "not_war", None),
        ("Đền, Lăng Thái sư Trần Thủ Độ", "burial", None),
        ("Lăng Tứ Kiệt", "burial", None),
        ("Mộ và đền thờ Thủ Khoa Huân", "burial", None),
        ("Làng Kháng chiến Nguyên Xá, Nghĩa trang liệt sĩ Đông Hưng", "burial", None),
        ("Chùa Kiến An Cung", "worship", None),
        ("Đình Long Hưng (Khu di tích Nam Kỳ Khởi Nghĩa năm 1940)", "worship", None),
        ("Khu di tích chiến thắng Rạch Gầm – Xoài Mút", "pre1858", None),
        ("Cụm di tích Từ Lương Xâm - Căn cứ bản doanh của Ngô Quyền năm 938", "pre1858", None),
        ("Thương cảng Vân Đồn", "not_war", None),
        # known burials on site: graves/ossuary in the name (incl. parentheticals) or a listed site
        ("Địa điểm Cuộc nổi dậy của đồng bào Stiêng (Mả thằng Tây)", "burial", None),
        ("Khu chứng tích tội ác của bọn diệt chủng Pôn Pốt", "burial", None),  # Ba Chúc ossuary
        ("Khu chứng tích Sơn Mỹ", "burial", None),  # victims' graves on site
        ("Địa điểm chứng tích tội ác Mỹ - Ngụy ở Biệt khu Hải Yến - Bình Hưng", "burial", None),  # burial pits
        ("Các Chứng tích về cuộc thảm sát 286 người vô tội do thực dân Pháp tiến hành năm 1947", "burial", None),
        ("Nhà mồ Ba Chúc", "burial", None),
        ("Địa điểm mộ tập thể 3000 người", "burial", None),
        ("Nơi quy tập hài cốt liệt sĩ", "burial", None),
        ("Nghia trang liet si Truong Son", "burial", None),  # unaccented text
        ("Căn cứ Núi Dinh", "keep", "military_site"),
        ("Địa điểm tập kết ra Bắc năm 1954 tại Cao Lãnh", "not_war", None),
    ],
)
def test_classify(name, decision, kind):
    d, k, _ = v.classify(name)
    assert (d, k) == (decision, kind)


def test_event_dates_only_from_wartime_years():
    assert v.event_dates("Chiến thắng Ấp Bắc (2/1/1963)") == ("1963-01-02", "1963-01-02")
    assert v.event_dates("Khu căn cứ kháng chiến tỉnh Đắk Lắk (1965-1975)") == ("1965", "1975")
    assert v.event_dates("Chiến thắng Giồng Thị Đam - Gò Quản Cung") == (None, None)
    assert v.event_dates("An toàn khu Định Hóa (gồm 13 di tích): Nơi thành lập ... 20/10/1950") == (None, None)


# ---- geocoding -------------------------------------------------------------------------------
def test_place_parts_levels():
    parts = v.place_parts("Xã [[Mỹ Lợi, Đồng Tháp|Mỹ Lợi]]")  # comma inside the link must not split
    assert parts == [("Mỹ Lợi", "commune", "Mỹ Lợi, Đồng Tháp", 0)]
    parts = v.place_parts("[[Mường So]], [[Phong Thổ (huyện)|Phong Thổ]]")
    assert [(n, h, s) for n, h, _, s in parts] == [("Mường So", None, 0), ("Phong Thổ", "district", 1)]


def test_parse_coords_follows_redirects(coords):
    assert coords["Mỹ Lợi, Đồng Tháp"]["page"] == "Mỹ Lợi"
    assert "Chiến khu Ngọc Trạo" not in coords  # page has no coordinate


def test_commune_match_in_gns(built):
    recs, _ = built
    e = recs["Căn cứ kháng chiến Tỉnh ủy Kiến Phong (Xẻo Quít)"]
    assert (e.original_datum, e.kind) == ("commune-centroid", "military_site")
    assert e.uncertainty_m == v.AREA_FLOOR_M["base_area"]  # a base area, not the commune
    assert (e.lon, e.lat) == (105.812437, 10.372737)
    assert e.original_coord == "Xã Mỹ Hiệp" == e.raw["dia_ban"]  # verbatim location cell
    assert e.raw["geocode_level"] == "commune" and e.date_start is None


def point_site_at(main, place_text, name="Đồn Bình Thành"):
    """A point relic (a post) at the place of an existing fixture row (geocoding tests)."""
    r = by_name(main, place_text)
    return dict(r, name_wt=name)


def test_same_name_communes_disambiguated_by_commune_article(main, gaz, coords):
    row = point_site_at(main, "Vụ Thảm sát Bình Thành")  # two 'Xã Bình Thành' in old Đồng Tháp
    (e,), _ = v.build([row], [], gaz, coords)
    assert abs(e.lat - 10.56) < 0.01
    assert any("disambiguated" in n for n in e.raw["geocode_notes"])
    # 'Xã Bình Thành' alone is a post-2025 (merged) commune: the GNS match is only its namesake part
    for _, f in gaz.nearest_communes((e.lon, e.lat), v.MERGED_PARTS):
        assert covers(e, f["lon"], f["lat"], gaz.commune_extent(f))


def test_post_2025_commune_falls_back_to_commune_article(built):
    e = built[0]["Chiến thắng Cổ Cò"]  # Mỹ Lợi (new commune) is not a GNS name in Đồng Tháp
    assert e.original_datum == "commune-centroid" and e.uncertainty_m == v.AREA_FLOOR_M["battle"]
    assert (e.lon, e.lat) == (105.915219, 10.33485)


def test_relic_article_coordinate_and_special_merge(built):
    recs, stats = built
    e = recs["Chiến thắng Ấp Bắc (2/1/1963)"]
    assert e.original_datum == "article-coordinate" and e.uncertainty_m == v.AREA_FLOOR_M["battle"]
    assert (e.lon, e.lat) == (106.195833, 10.438056) and e.date_start == "1963-01-02"
    assert e.raw["special"]["name"] == "Địa điểm Chiến thắng Ấp Bắc"  # special row merged, not duplicated
    assert not any("Địa điểm Chiến thắng Ấp Bắc" == t for t in recs)
    # Ấp Bắc, and special 'Chiến khu Đ (đang lập hồ sơ)' into Đồng Nai's '... ([[Chiến khu Đ]])'
    # + 'Nhà tù Côn Đảo', and special 'Nhà tù Phú Quốc' into main 'Nhà lao Cây Dừa ([[Nhà tù Phú Quốc]])'
    # + ATK Chợ Đồn, Căn cứ Bộ chỉ huy QGP (= Tà Thiết's '+ Căn cứ BCH ...'), An toàn khu II (= ATK2)
    assert stats["special:merged_into_main"] == 7
    assert built[0]["Căn cứ Khu ủy miền Đông Nam Bộ (Chiến khu Đ)"].raw["special"]["name"].startswith("Chiến khu Đ")


def test_listed_coordinate_wins(built):
    e = built[0]["Chiến trường Điện Biên Phủ"]
    assert e.original_coord == "{{Coord|21|23|13|N|103|0|56|E}}" and e.original_datum == "article-coordinate"


def test_burials_dropped_and_counted(built):
    recs, stats = built
    assert not any("Lăng" in t or "Mộ" in t for t in recs)
    # Lăng, Mộ, Mả thằng Tây, Ba Chúc, Sơn Mỹ + checked massacre sites with graves on site:
    # Bình Thành (Đồng Tháp), Phong Nẫm (286 người), Biệt khu Hải Yến - Bình Hưng
    # + Lăng Trương Định
    assert stats["main:burial"] == 9
    assert not any(k in t for t in recs for k in ("Pôn Pốt", "Sơn Mỹ", "Stiêng", "Bình Thành", "286", "Hải Yến"))


def test_records_are_valid_evidence(built):
    recs, _ = built
    assert len({e.source_record_id for e in recs.values()}) == len(recs)
    for e in recs.values():
        assert isinstance(e, store.Evidence) and e.source == "vn_relics" and e.kind in store.KINDS


# ---- radius sized to the area the relic names ------------------------------------------------
def covers(e, lon, lat, r=0.0):
    """The record's circle contains the point, or the whole circle of radius r around it."""
    return v.km((e.lon, e.lat), (lon, lat)) * 1000 + r <= e.uncertainty_m + 1  # radii are rounded up


@pytest.mark.parametrize(
    "title,area",
    [
        ("Chiến khu Đ", "war_zone"),
        ("Căn cứ Khu ủy miền Đông Nam Bộ (Chiến khu Đ)", "war_zone"),
        ("Chiến khu Ngọc Trạo", "war_zone"),
        ("Chiến trường Điện Biên Phủ", "battlefield"),
        ("Chiến thắng Giồng Thị Đam - Gò Quản Cung", "battle"),
    ],
)
def test_large_areas_get_area_sized_radius(built, title, area):
    e = built[0][title]
    assert e.raw["area_class"] == area and e.uncertainty_m >= v.AREA_FLOOR_M[area]


def test_area_class_heads():
    assert v.area_class("Sở chỉ huy tiền phương chiến dịch Hồ Chí Minh") is None  # a command post
    assert v.area_class("Địa điểm thành lập cơ quan chỉ huy Chiến khu Nguyễn Huệ") is None
    assert v.area_class("Địa điểm chiến thắng Chiến dịch Tây Quảng Đức") == "campaign"
    assert v.area_class("Vụ Thảm sát Bình Thành") is None
    assert v.area_class("An toàn khu Định Hóa") == "war_zone"
    assert v.area_class("Căn cứ Núi Bà") == "base_area"
    assert v.area_class("Địa đạo Vĩnh Mốc và Hệ thống làng hầm Vĩnh Linh") == "network"
    assert v.area_class("Địa đạo Củ Chi") is None and v.area_class("Đồng khởi Bến Tre") == "battle"
    assert v.area_class("Các trọng điểm trên đường 20 Quyết Thắng: Km 10.5") == "route_section"
    assert v.AREA_FLOOR_M["war_zone"] >= 25000 and v.AREA_FLOOR_M["campaign"] >= 25000
    assert v.AREA_FLOOR_M["base_area"] >= 20000 and v.AREA_FLOOR_M["battlefield"] >= 10000


def test_massacre_village_stays_commune_sized(main, gaz, coords):
    row = point_site_at(main, "Vụ Thảm sát Bình Thành", name="Vụ Thảm sát Mỹ Thủy")
    (e,), _ = v.build([row], [], gaz, coords)
    (post,), _ = v.build([point_site_at(main, "Vụ Thảm sát Bình Thành")], [], gaz, coords)
    assert e.raw["area_class"] is None and e.uncertainty_m == post.uncertainty_m  # no battle-area floor


def test_listed_coordinate_radius_covers_every_named_commune(built, gaz, coords):
    e = built[0]["Chiến trường Điện Biên Phủ"]  # list names Điện Biên Phủ, Mường Phăng, Thanh Nưa
    for t in ("Điện Biên Phủ (phường)", "Mường Phăng", "Thanh Nưa"):
        c = coords[t]
        assert covers(e, c["lon"], c["lat"]), t


def test_battle_placed_away_from_its_location_column_covers_both(built, coords):
    e = built[0]["Khu di tích Trận chiến đấu 81 ngày đêm tại Quảng Trị"]
    # the stored point is a midpoint: the datum and the verbatim field say so and give both inputs
    assert e.original_datum == "midpoint-of-two-places"
    assert e.original_coord.startswith("Bến Quan, Vĩnh Linh") and "Thành cổ Quảng Trị" in e.original_coord
    assert (e.date_start, e.date_end) == ("1972-06-28", "1972-09-16")
    citadel = coords["Thành cổ Quảng Trị"]
    ben_quan = e.raw["event_elsewhere"]["listed_point"]
    assert v.km(tuple(ben_quan[:2]), (citadel["lon"], citadel["lat"])) > 25  # the contradiction is real
    assert covers(e, citadel["lon"], citadel["lat"]) and covers(e, *ben_quan[:2])
    assert e.uncertainty_m >= v.km(tuple(ben_quan[:2]), (citadel["lon"], citadel["lat"])) * 1000 / 2 + 2000
    assert "Thành cổ Quảng Trị" in e.description and "Bến Quan" in e.description
    assert "contradict" in e.description.lower()


def test_original_coord_is_the_verbatim_location_cell(built):
    for e in built[0].values():
        if e.original_datum in ("commune-centroid", "district-centroid", "province-centroid", "enclosing-circle"):
            assert e.original_coord.startswith(e.raw["dia_ban"])
        if e.original_datum != "article-coordinate" and e.raw["geocode_level"] in ("commune", "district", "province"):
            assert e.original_coord == e.raw["dia_ban"]


# ---- stable ids and sync ---------------------------------------------------------------------
def test_ids_are_stable_slugs_with_province(main, special, gaz, coords, built):
    ids = {e.title: e.source_record_id for e in built[0].values()}
    again, _ = v.build(main, special, gaz, coords)
    assert ids == {e.title: e.source_record_id for e in again}
    assert ids["Chiến khu Đ"] == "main:ho chi minh:chien khu d"
    assert (
        ids["Căn cứ kháng chiến Tỉnh ủy Kiến Phong (Xẻo Quít)"]
        == "main:dong thap:can cu khang chien tinh uy kien phong"
    )
    assert not any("#" in i for i in ids.values())
    # reversing row order must not change any id
    rev, _ = v.build(main[::-1], special[::-1], gaz, coords)
    assert ids == {e.title: e.source_record_id for e in rev}


def test_same_name_in_one_province_is_disambiguated_by_place_not_row_order(gaz, coords):
    rows = v.main_rows((FIX / "main_excerpt.wikitext").read_text(encoding="utf-8"))
    r = by_name(rows, "Xẻo Quít")
    twin = dict(r, place_wt="Xã [[Bình Thành, Đồng Tháp|Bình Thành]]")
    a, _ = v.build([r, twin], [], gaz, coords)
    b, _ = v.build([twin, r], [], gaz, coords)
    ida = sorted(e.source_record_id for e in a)
    assert ida == sorted(e.source_record_id for e in b) and len(set(ida)) == 2
    assert "main:dong thap:can cu khang chien tinh uy kien phong@xa my hiep" in ida


def test_sync_is_idempotent_and_deletes_dropped_rows(tmp_path, built):
    recs = list(built[0].values())
    db = store.connect(tmp_path / "e.sqlite")
    assert v.write(db, recs)[:3] == (len(recs), 0, 0)
    snap = list(db.execute("select * from evidence order by id"))
    assert v.write(db, recs)[:3] == (len(recs), 0, 0)
    assert list(db.execute("select * from evidence order by id")) == snap  # same ids, same rows
    assert v.write(db, recs[1:])[:3] == (len(recs) - 1, 1, 0)
    assert db.execute("select count(*) from evidence").fetchone()[0] == len(recs) - 1


# ---- a listed coordinate that marks a larger named place ---------------------------------
def test_coord_template_extent():
    assert v.coord_extent("{{coord|8|40|57|N|106|36|26|E|scale:500000|name=đảo Côn Sơn|display=inline}}") == (
        25000.0,
        "đảo Côn Sơn",
    )
    assert v.coord_extent("{{Coord|21|23|13|N|103|0|56|E}}") == (None, None)
    assert v.coord_extent("{{coord|10|20|N|106|30|E|dim:4km}}") == (2000.0, None)


def test_island_coordinate_covers_the_island(built):
    e = built[0]["Nhà tù Côn Đảo"]  # list coordinate is labelled 'đảo Côn Sơn' at scale 1:500000
    assert e.original_coord.startswith("{{coord|8|40|57|N|106|36|26|E|scale:500000")
    # Côn Sơn island runs ~106.53-106.68 E, 8.63-8.76 N: every corner within the radius
    for lon, lat in ((106.53, 8.63), (106.68, 8.63), (106.53, 8.76), (106.68, 8.76)):
        assert covers(e, lon, lat)
    assert "đảo Côn Sơn" in e.description


# ---- duplicates across the two lists ---------------------------------------------------------
def test_special_row_merged_via_parenthetical_alias(built):
    recs, _ = built
    ids = {e.source_record_id for e in recs.values()}
    assert "special:an giang:tu phu quoc" not in ids
    e = recs["Nhà lao Cây Dừa (Nhà tù Phú Quốc)"]
    assert e.source_record_id == "main:an giang:lao cay dua" and e.raw["special"]["name"] == "Nhà tù Phú Quốc"


# ---- ids do not depend on component lists ----------------------------------------------------
def test_id_ignores_component_list(main):
    atk = by_name(main, "An toàn khu Định Hóa")
    assert v.id_key(atk["name_wt"]) == "an toan khu dinh hoa"
    reworded = atk["name_wt"].replace("Thắng cảnh", "Danh thắng").replace("13 di tích", "14 di tích")
    assert v.id_key(reworded) == "an toan khu dinh hoa"
    assert v.id_key("Các trọng điểm trên đường 20 Quyết Thắng: Km 10.5; Km 14 trọng điểm Trà Ang") == (
        "cac trong diem tren duong 20 quyet thang"
    )
    assert v.id_key("[[Nhà lao Cây Dừa]] ([[Nhà tù Phú Quốc]])") == "lao cay dua"  # unchanged


# ---- massacre / mass-death sites checked for victims' graves on site ------------------------
def test_mass_death_sites_need_a_check():
    assert v.needs_burial_check("Địa điểm Thảm sát Mỹ Thủy")
    assert v.needs_burial_check("Cuộc thảm sát 129 người dân vô tội của Mỹ–Ngụy")
    assert v.needs_burial_check("Địa điểm các Thanh niên xung phong Đại đội 915 hy sinh tại Lưu Xá (12/1972)")
    assert not v.needs_burial_check("Khu trại giam bệnh viện Chợ Quán - nơi đồng chí Trần Phú hy sinh")
    assert not v.needs_burial_check("Chiến khu Đ")
    for key, (verdict, why) in v.MASS_DEATH_CHECKED.items():
        assert verdict in ("no_grave_on_site", "unresolved") and why
        assert key not in v.KNOWN_ON_SITE_BURIAL


def test_checked_massacre_site_carries_the_check(built):
    e = built[0]["Địa điểm Thảm sát Mỹ Thủy"]
    assert e.raw["burial_check"]["verdict"] == "no_grave_on_site"
    assert "memorial" in e.raw["burial_check"]["evidence"] and "grave" in e.description


def test_unresolved_and_unchecked_massacre_sites_are_flagged(main, gaz, coords):
    # Tân Thanh Tây: no source found either way -> kept, flagged unresolved
    rows = [
        point_site_at(main, "Vụ Thảm sát Bình Thành", name="Cuộc thảm sát 129 người dân vô tội của Mỹ–Ngụy"),
        point_site_at(main, "Vụ Thảm sát Bình Thành", name="Vụ thảm sát Xóm Mới"),
    ]  # not in the registry
    recs, stats = v.build(rows, [], gaz, coords)
    by = {e.title: e for e in recs}
    assert by["Cuộc thảm sát 129 người dân vô tội của Mỹ–Ngụy"].raw["burial_check"]["verdict"] == "unresolved"
    assert "not ruled out" in by["Cuộc thảm sát 129 người dân vô tội của Mỹ–Ngụy"].description
    assert by["Vụ thảm sát Xóm Mới"].raw["burial_check"]["verdict"] == "unchecked"
    assert stats["mass_death_unchecked"] == 1


# ---- round 2: every place the source names is covered ----------------------------------------
def by_id(built, rid):
    return next(e for e in built[0].values() if e.source_record_id == rid)


def gns_point(gaz, key, name, adm1):
    f = next(f for f in gaz.names[key] if f["name"] == name and f["adm1"] == adm1)
    return f["lon"], f["lat"]


def test_route_radius_covers_the_located_key_points(built, gaz):
    e = by_id(built, "main:quang tri:cac trong diem tren duong 12a")
    # list commune Võ Ninh is ~100 km from Cha Lo (Lao border); both are named by the source
    for key, name in (
        ("vo ninh", "Xã Võ Ninh"),
        ("cha lo", "Cha Lo"),
        ("la trong", "La Trọng"),
        ("bai dinh", "Bãi Định"),
    ):
        assert covers(e, *gns_point(gaz, key, name, "VN-24")), name
    assert e.original_datum == "enclosing-circle" and e.original_coord.startswith("Võ Ninh, Quảng Ninh")
    assert "Ngầm Khe Ve" in e.description and "not located" in e.description  # disclosed, not hidden
    road20 = by_id(built, "main:quang tri:cac trong diem tren duong 20 quyet thang")
    assert "Trà Ang" in road20.description and "not located" in road20.description
    tan_trach = next(f for f in gaz.names["tan trach"] if f["name"] == "Xã Tân Trạch" and f["adm1"] == "VN-24")
    assert covers(road20, tan_trach["lon"], tan_trach["lat"], gaz.commune_extent(tan_trach))


def test_communes_named_in_the_relic_name_place_and_identify_it(built, gaz):
    e = by_id(built, "main:quang tri:hang dong xa hoa thanh xa hoa tien")  # not the generic 'hang dong'
    for key, name in (("hoa thanh", "Xã Hóa Thanh"), ("hoa tien", "Xã Hóa Tiến")):
        assert covers(e, *gns_point(gaz, key, name, "VN-24")), name
    # two communes: the point is the middle of a circle around both, not the centroid of either
    assert e.original_datum == "enclosing-circle" and e.original_coord.startswith("Minh Hóa")
    assert "Xã Hoá Thanh" in e.original_coord and "circle enclosing" in e.description
    assert v.id_key("Hang động: – Xã Hoá Thanh (hang Thanh Lạng) – Xã Hoá Tiến") == "hang dong xa hoa thanh xa hoa tien"


def test_district_radius_is_measured_and_at_least_the_gns_adm2_extent(built, gaz):
    import gns

    assert v.UNC_DISTRICT == gns.EXTENT_M["ADM2"] == 12000
    e = by_id(built, "main:ho chi minh:dia dao cu chi")
    assert e.original_datum == "district-centroid" and e.uncertainty_m >= 12000
    cu_chi = next(f for f in gaz.names["cu chi"] if f["dsg"] == "ADM2")
    peers = list(gaz.adm2_by_adm1["VN-SG"].values())
    mine = [
        c
        for c in gaz.communes_near((cu_chi["lon"], cu_chi["lat"]), 80)
        if c["adm1"] == "VN-SG"
        and min(peers, key=lambda g: v.km((c["lon"], c["lat"]), (g["lon"], g["lat"])))["ufi"] == cu_chi["ufi"]
    ]
    assert len(mine) >= 15  # Củ Chi's communes, incl. Phú Mỹ Hưng (Bến Dược) ~16 km north
    for c in mine:
        assert covers(e, c["lon"], c["lat"], gaz.commune_extent(c)), c["name"]
    assert e.uncertainty_m > 16000


def test_peer_district_named_beside_a_commune_is_covered(built, gaz):
    e = by_id(built, "special:dien bien:tran dia phao 105mm cua dai doi 805")
    dist = next(f for f in gaz.names["dien bien"] if f["dsg"] == "ADM2")
    assert covers(e, dist["lon"], dist["lat"], gaz.district_extent(dist))
    assert any("named beside" in n for n in e.raw["geocode_notes"])


def test_peer_commune_missing_from_gns_is_located_not_dropped(built, gaz, coords):
    e = by_id(built, "main:quang tri:lang chien dau cu nam")  # 'Hải Trạch & Thanh Trạch, Bố Trạch'
    assert covers(e, *gns_point(gaz, "thanh trach", "Xã Thanh Trạch", "VN-24"))
    hai = coords["Hải Trạch"]  # redirects to the post-2025 commune that absorbed it
    assert hai["page"] == "Đông Trạch" and covers(e, hai["lon"], hai["lat"])
    assert "Hải Trạch" in e.description


def test_missing_peer_commune_falls_back_to_its_district(main, gaz, coords):
    r = dict(by_name(main, "Cự Nẫm"), place_wt="Xã Không Có & Thanh Trạch, [[Bố Trạch (huyện)|Bố Trạch]]")
    (e,), _ = v.build([r], [], gaz, coords)
    bt = next(f for f in gaz.names["bo trach"] if f["dsg"] == "ADM2")
    assert covers(e, bt["lon"], bt["lat"], gaz.district_extent(bt)) and "could not be located" in e.description


def test_post_2025_commune_matched_to_old_namesake_is_not_commune_sized(built, gaz):
    for rid in (
        "main:ca mau:cac dia diem thuoc xu uy nam bo trung uong cuc mien nam",  # Xã Trần Văn Thời
        "main:dong thap:co quan giao buu thong tin vo tuyen dien nam bo",  # Xã Phú Cường
        "main:dong thap:tham sat cho giua 1940",
    ):  # Xã Vĩnh Kim
        e = by_id(built, rid)
        assert e.uncertainty_m > v.UNC_COMMUNE_WIKI, rid
        assert any("post-2025" in n for n in e.raw["geocode_notes"]), rid
        for _, f in gaz.nearest_communes((e.lon, e.lat), v.MERGED_PARTS):
            assert covers(e, f["lon"], f["lat"], gaz.commune_extent(f)), (rid, f["name"])


# ---- duplicates --------------------------------------------------------------------------------
def test_atk_and_bch_spellings_merge_into_the_main_relic(built):
    ids = {e.source_record_id for e in built[0].values()}
    for gone in (
        "special:thai nguyen:an toan khu cho don",
        "special:dong nai:can cu bo chi huy quan giai phong mien nam viet nam",
        "special:bac ninh:an toan khu ii hiep hoa",
    ):
        assert gone not in ids
    assert by_id(built, "main:thai nguyen:atk cho don").raw["special"]["name"].startswith("An toàn khu (ATK) Chợ Đồn")
    tt = by_id(built, "main:dong nai:can cu ta thiet")  # id no longer carries its '+' component list
    assert tt.raw["special"]["name"] == "Căn cứ Bộ chỉ huy Quân giải phóng miền Nam Việt Nam"
    assert (tt.kind, tt.raw["area_class"]) == ("military_site", "base_area")  # a base, not a campaign
    loc_ninh = by_id(built, "main:dong nai:can cu ta thiet")
    assert "Lộc Ninh" in loc_ninh.description
    assert by_id(built, "main:bac ninh:atk2").raw["special"]["name"] == "An toàn khu II Hiệp Hòa"


# ---- kinds ---------------------------------------------------------------------------------------
def test_kinds_are_what_the_relic_is(built):
    t5 = by_id(built, "main:quang tri:tran dia ten lua t5 noi ban roi may b52 dau tien o viet nam")
    assert t5.kind == "military_site" and "where the aircraft fell is not given" in t5.description
    for rid in (
        "main:ho chi minh:so chi huy tien phuong chien dich ho chi minh",
        "main:ho chi minh:so chi huy tien phuong phan khu 6 trong chien dich mau than 1968",
        "main:ho chi minh:trai giam benh vien cho quan noi dong chi tran phu hy sinh",
    ):
        e = by_id(built, rid)
        assert e.kind == "military_site" and e.raw["classified_as"] != "battle", rid
    lx = by_id(built, "main:thai nguyen:cac thanh nien xung phong dai doi 915 hy sinh tai luu xa")
    assert lx.kind == "air_strike" and lx.raw["area_class"] is None and lx.date_start == "1972-12"
    nd = by_id(built, "main:ninh binh:nhung di tich lich su chong my cuu nuoc tai thanh pho nam dinh")
    assert nd.kind == "air_strike" and nd.raw["classified_as"] != "massacre"
    assert by_id(built, "main:quang tri:tham sat my thuy").kind == "loss_incident"


def test_names_the_relic_name_gives_are_covered(built, gaz):
    nd = by_id(built, "main:ninh binh:nhung di tich lich su chong my cuu nuoc tai thanh pho nam dinh")
    city = next(f for f in gaz.names["nam dinh"] if f["dsg"] == "ADM2" and f["adm1"] == "VN-67")
    assert covers(nd, city["lon"], city["lat"], gaz.district_extent(city))  # '... tại thành phố Nam Định'


# ---- parser, failures ----------------------------------------------------------------------------
def test_row_continued_on_a_double_pipe_line(main, built):
    r = by_name(main, "Xuân Mậu Thân 1968")  # cells continue on a line starting with '||'
    assert v.plain(r["place_wt"]) == "Phú Yên" and r["category"] == "Lịch sử" and r["recognised"] == "2017"
    e = by_id(built, "main:dak lak:dien ra cuoc tong tien cong va noi day xuan mau than 1968")
    assert e.original_coord == "Phú Yên" and "|" not in e.raw["dia_ban"]


def test_failed_coordinate_batch_aborts_the_run(tmp_path, monkeypatch):
    bad = tmp_path / "coords_x.json"
    bad.write_text('{"error": {"code": "maxlag", "info": "Waiting for a database server"}}', encoding="utf-8")
    monkeypatch.setattr(v, "cached_get", lambda *a, **k: bad)
    with pytest.raises(RuntimeError, match="maxlag"):
        v.fetch_coords(["Thành cổ Quảng Trị"])


# ---- ids ------------------------------------------------------------------------------------------
def test_new_same_name_row_does_not_rename_the_stored_record(gaz, coords):
    rows = v.main_rows((FIX / "main_excerpt.wikitext").read_text(encoding="utf-8"))
    r = by_name(rows, "Xẻo Quít")
    (old,), _ = v.build([r], [], gaz, coords)
    stored = {old.source_record_id: {"title": old.title, "dia_ban": old.raw["dia_ban"]}}
    assert old.source_record_id == "main:dong thap:can cu khang chien tinh uy kien phong"
    twin = dict(r, place_wt="Xã [[Bình Thành, Đồng Tháp|Bình Thành]]")
    for order in ([r, twin], [twin, r]):
        recs, _ = v.build(order, [], gaz, coords, previous=stored)
        ids = {e.raw["dia_ban"]: e.source_record_id for e in recs}
        assert ids["Xã Mỹ Hiệp"] == "main:dong thap:can cu khang chien tinh uy kien phong"  # unchanged
        assert ids["Xã Bình Thành"] == "main:dong thap:can cu khang chien tinh uy kien phong@xa binh thanh"
    # once stored, the suffixed id survives an edit of its location cell (matched by title + cell)
    stored = {e.source_record_id: {"title": e.title, "dia_ban": e.raw["dia_ban"]} for e in recs}
    moved = dict(twin, place_wt="Xã [[Bình Thành, Đồng Tháp|Bình Thành]], tỉnh Đồng Tháp")
    recs2, _ = v.build([r, moved], [], gaz, coords, previous=stored)
    assert sorted(e.source_record_id for e in recs2) == sorted(stored)


# ---- graves inside composite relics; proposed relics -------------------------------------------
def test_composite_relic_with_a_known_grave_says_so(built):
    e = by_id(built, "special:dong thap:cac dia diem khoi nghia truong dinh")
    assert e.raw["includes_known_grave"] and "Lăng Trương Định" in e.description
    assert "known grave" in e.description
    assert not any(t.startswith("Lăng Trương Định") for t in built[0])  # the tomb itself is dropped


def test_proposed_relic_is_not_called_a_listed_one(built):
    e = by_id(built, "special:da nang:chien thang nui thanh")
    assert e.raw["status"] == "proposed" and e.description.startswith("Proposed special national relic")


# ---- coverage of in-scope sites ---------------------------------------------------------------
@pytest.mark.parametrize(
    "rid,kind",
    [
        ("main:quang tri:nga ba cau ga", "other"),  # the list gives no event (see below)
        ("main:quang tri:nga ba long hung", "ground_combat"),  # note: KDT Trận chiến Quảng Trị
        ("main:quang tri:cac diem vuot duong 9 cua duong day 559", "military_site"),
        ("main:quang tri:cau treo ben tat", "military_site"),  # note: KDT Đường Trường Sơn
        ("main:thanh hoa:ham rong", "air_strike"),
        ("main:bac ninh:atk2", "military_site"),
    ],
)
def test_war_sites_without_a_war_word_are_kept(built, rid, kind):
    assert by_id(built, rid).kind == kind


def test_atk2_spills_into_the_neighbouring_province_and_is_covered(built, gaz):
    e = by_id(built, "main:bac ninh:atk2")  # 'Hiệp Hòa, Phổ Yên & Phú Bình': the last two in Thái Nguyên
    for key in ("pho yen", "phu binh"):
        f = next(f for f in gaz.names[key] if f["dsg"] == "ADM2" and f["adm1"] == "VN-69")
        assert covers(e, f["lon"], f["lat"], gaz.district_extent(f)), key


# ---- round 3 -------------------------------------------------------------------------------------
def test_unsourced_war_claim_is_not_shown(built):
    e = by_id(built, "main:quang tri:nga ba cau ga")  # row: '... || Lịch sử đặc biệt|| 1986|| đặc biệt 2013'
    assert "fought" not in e.description and "81 ngày đêm" not in e.description
    assert "đặc biệt 2013" in e.description and "does not say" in e.description


def test_commune_namesake_in_another_district_is_not_used(built, gaz):
    e = by_id(built, "main:quang tri:cang quan su dong ha")  # 'Phường 2, Đông Hà'
    p2 = next(f for f in gaz.names["phuong 2"] if f["adm1"] == "VN-25")  # Phường 2 of Quảng Trị town
    assert "11876599" not in " ".join(e.raw["geocode_matched"])
    dong_ha = next(f for f in gaz.names["dong ha"] if f["dsg"] == "ADM2")
    assert covers(e, dong_ha["lon"], dong_ha["lat"], gaz.district_extent(dong_ha))
    assert not gaz.in_district((p2["lon"], p2["lat"]), dong_ha)
    assert any("another district" in n for n in e.raw["geocode_notes"])


def test_named_components_are_covered_when_the_relic_article_places_it(built, gaz, coords):
    e = by_id(built, "main:quang tri:vuc ben pha xuan son dong phong nha")
    assert e.original_datum == "article-coordinate"
    xs = next(f for f in gaz.names["xuan son"] if f["adm1"] == "VN-24")  # Bến phà Xuân Sơn
    assert covers(e, xs["lon"], xs["lat"])
    pn = coords["Động Phong Nha"]
    assert covers(e, pn["lon"], pn["lat"], v.UNC_ARTICLE)
    assert "Xuân Sơn" in e.description


def test_both_banks_relic_covers_the_bridge_and_both_banks(built, gaz):
    e = by_id(built, "main:quang tri:vuc doi bo hien luong")  # '(bao gồm [[Cầu Hiền Lương]], ...)'
    bridge = next(f for f in gaz.names["hien luong"] if f["dsg"] == "BDG")
    assert covers(e, bridge["lon"], bridge["lat"], 1000)  # both bank ends and the posts beside them


def test_multi_place_circle_is_not_called_a_centroid(built):
    for e in built[0].values():
        circles = e.raw.get("geocode_circles") or []
        if e.original_datum in ("commune-centroid", "district-centroid", "province-centroid"):
            assert len(circles) <= 1, (e.source_record_id, circles)
    e = by_id(built, "main:quang tri:lang chien dau cu nam")  # 'Hải Trạch & Thanh Trạch, Bố Trạch'
    assert e.original_datum == "enclosing-circle" and "circle enclosing" in e.description


def test_province_radius_covers_every_gns_commune_of_the_province(gaz):
    for table, codes_of in (
        (gaz.provinces, lambda k: v.PROVINCE_CODES[k]),
        (gaz.old_provinces, lambda k: [v.OLD_PROVINCE_CODE[k]]),
    ):
        for prov, (lon, lat, r) in table.items():
            for c in gaz.adm3:
                if c["adm1"] in codes_of(prov):
                    d = v.km((lon, lat), (c["lon"], c["lat"])) * 1000 + gaz.commune_extent(c)
                    assert d <= r + 1, (prov, c["name"], d, r)
    xh = next(f for f in gaz.names["xuan hai"] if f["adm1"] == "VN-32")  # Xuân Hải, Phú Yên
    lon, lat, r = gaz.old_provinces["phu yen"]
    assert v.km((lon, lat), (xh["lon"], xh["lat"])) * 1000 + gaz.commune_extent(xh) <= r + 1


@pytest.mark.parametrize(
    "district,adm1,communes",
    [
        ("van chan", "VN-06", [("dai lich", "Xã Đại Lịch"), ("phu nham", "Xã Phù Nham")]),
        ("quang ninh", "VN-24", [("vo ninh", "Xã Võ Ninh"), ("quan hau", "Thị Trấn Quán Hàu")]),
        ("quang trach", "VN-24", [("quang luu", "Xã Quảng Lưu")]),
        ("vinh cuu", "VN-39", [("phu ly", "Xã Phú Lý")]),
        ("phong dien", "VN-26", [("dien mon", "Xã Điền Môn")]),
    ],
)
def test_district_extent_covers_the_communes_wikidata_places_in_it(gaz, district, adm1, communes):
    d = next(f for f in gaz.names[district] if f["dsg"] == "ADM2" and f["adm1"] == adm1)
    ext = gaz.district_extent(d)
    for key, name in communes:
        c = next(f for f in gaz.names[key] if f["name"] == name and f["adm1"] == adm1)
        assert v.km((d["lon"], d["lat"]), (c["lon"], c["lat"])) * 1000 + gaz.commune_extent(c) <= ext + 1, name
        assert gaz.in_district((c["lon"], c["lat"]), d), name


def test_wikidata_member_outliers_are_dropped():
    pts = {
        "Q1": {
            "label": "X",
            "point": (105.0, 21.0),
            "members": [(105.0, 21.0), (105.05, 21.02), (105.1, 21.05), (107.0, 16.8)],
        }
    }
    assert (107.0, 16.8) not in v.filter_members(pts["Q1"])


def test_failed_district_query_aborts_the_run(monkeypatch, tmp_path):
    bad = tmp_path / "x.json"
    bad.write_text('{"error": "timeout"}', encoding="utf-8")
    monkeypatch.setattr(v, "cached_get", lambda *a, **k: bad)
    with pytest.raises(RuntimeError):
        v.fetch_district_members()


@pytest.mark.parametrize(
    "commune,cadm1,district",
    [
        ("Xã Đất Cuốc", "VN-57", "tan uyen"),  # now in Bắc Tân Uyên, split off from Tân Uyên in 2013
        ("Phường Khuê Mỹ", "VN-DN", "ngu hanh son"),  # nearest Wikidata ward is in Hải Châu, but GNS agrees
        ("Xã Tân Hải", "VN-43", "tan thanh"),  # Tân Thành renamed Phú Mỹ (2018): same GNS point
    ],
)
def test_commune_is_not_rejected_on_one_weak_signal(gaz, commune, cadm1, district):
    key = v.normalise(commune.split(" ", 1)[1])
    c = next(f for f in gaz.names[key] if f["name"] == commune and f["adm1"] == cadm1)
    d = next(f for f in gaz.names[district] if f["dsg"] == "ADM2" and f["adm1"] == cadm1)
    assert gaz.in_district((c["lon"], c["lat"]), d)


def test_run_uses_the_wikidata_districts(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_gaz(index, districts=None, borders=None):
        seen["districts"] = districts
        raise Stop

    monkeypatch.setattr(v, "load", lambda refresh=False: ([], []))
    monkeypatch.setattr(v, "GNS_INDEX", FIX / "gns_index_excerpt.json")
    monkeypatch.setattr(v, "fetch_province_borders", lambda refresh=False: {})
    monkeypatch.setattr(
        v, "fetch_district_members", lambda refresh=False: {"Q1": {"label": "X", "point": None, "members": []}}
    )
    monkeypatch.setattr(v, "Gazetteer", fake_gaz)
    with pytest.raises(Stop):
        v.main([])
    assert seen["districts"] == {"Q1": {"label": "X", "point": None, "members": []}}


# ---- round 3: degraded Wikidata answers, province radius, far namesakes ---------------------
def _serve(monkeypatch, tmp_path, payload):
    path = tmp_path / "answer.json"
    path.write_text(payload, encoding="utf-8")
    monkeypatch.setattr(v, "cached_get", lambda *a, **k: path)
    return path


def test_empty_district_answer_aborts_the_run(monkeypatch, tmp_path):
    path = _serve(monkeypatch, tmp_path, '{"head": {"vars": []}, "results": {"bindings": []}}')
    with pytest.raises(RuntimeError):
        v.fetch_district_members()
    assert not path.exists()  # the bad answer is not kept in the cache
    with pytest.raises(RuntimeError):
        v.parse_district_members({"results": {"bindings": []}})


def test_truncated_district_answer_aborts_the_run(monkeypatch, tmp_path):
    # a real but partial answer (the test excerpt: a few districts) is not a full country answer
    path = _serve(monkeypatch, tmp_path, (FIX / "wdqs_district_communes.json").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="districts"):
        v.fetch_district_members()
    assert not path.exists()


@pytest.fixture(scope="module")
def borders():
    return v.parse_province_borders(json.loads((FIX / "osm_province_borders.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def gaz_b(borders):
    return v.Gazetteer(
        json.loads((FIX / "gns_index_excerpt.json").read_text(encoding="utf-8")),
        districts=v.parse_district_members(
            json.loads((FIX / "wdqs_district_communes.json").read_text(encoding="utf-8"))
        ),
        borders=borders,
    )


def test_province_radius_covers_every_land_border_vertex(gaz_b, borders):
    pts = borders["VN-32"]  # Phú Yên's land borders (OSM, as of 2025-03-01, sea boundaries left out)
    assert len(pts) > 5000
    lon, lat, r = gaz_b.old_provinces["phu yen"]
    assert all(v.km((lon, lat), p) * 1000 <= r for p in pts)
    # 13°41'N northern tip near Phú Mỡ: 63 km from the round-3 record point, which had r = 60.7 km
    assert max(p[1] for p in pts) > 13.69


def test_province_wide_record_covers_the_whole_province(main, gaz_b, borders, coords):
    r = by_name(main, "Xuân Mậu Thân 1968")
    (e,), _ = v.build([r], [], gaz_b, coords)
    assert e.original_datum == "province-centroid"
    assert all(covers(e, *p) for p in borders["VN-32"])


def test_failed_or_incomplete_province_border_answer_aborts_the_run(monkeypatch, tmp_path, borders):
    with pytest.raises(RuntimeError):
        v.parse_province_borders({"elements": [], "remark": "runtime error: timeout"})
    with pytest.raises(RuntimeError):
        v.parse_province_borders({"elements": []})
    with pytest.raises(RuntimeError, match="VN-"):
        v.check_province_borders(borders)  # only Phú Yên: every pre-2025 province is needed
    path = _serve(monkeypatch, tmp_path, (FIX / "osm_province_borders.json").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError):
        v.fetch_province_borders()
    assert not path.exists()


def test_run_uses_the_province_borders(monkeypatch):
    seen = {}

    class Stop(Exception):
        pass

    def fake_gaz(index, districts=None, borders=None):
        seen["borders"] = borders
        raise Stop

    monkeypatch.setattr(v, "load", lambda refresh=False: ([], []))
    monkeypatch.setattr(v, "GNS_INDEX", FIX / "gns_index_excerpt.json")
    monkeypatch.setattr(v, "fetch_district_members", lambda refresh=False: {})
    monkeypatch.setattr(v, "fetch_province_borders", lambda refresh=False: {"VN-32": [(109.0, 13.0)]})
    monkeypatch.setattr(v, "Gazetteer", fake_gaz)
    with pytest.raises(Stop):
        v.main([])
    assert seen["borders"] == {"VN-32": [(109.0, 13.0)]}


@pytest.mark.parametrize(
    "commune,ufi,district,dadm1",
    [
        ("tan tien", "12063124", "bien hoa", "VN-39"),  # Tân Tiến, Bù Đốp: 112 km from Biên Hòa
        ("tan lap", "12102598", "tan bien", "VN-37"),  # Tân Lập, Thủ Thừa (Long An): 100 km from Tân Biên
    ],
)
def test_far_namesake_commune_is_not_in_the_named_district(gaz, commune, ufi, district, dadm1):
    c = next(f for f in gaz.names[commune] if f["ufi"] == ufi)
    d = next(f for f in gaz.names[district] if f["dsg"] == "ADM2" and f["adm1"] == dadm1)
    assert v.km((c["lon"], c["lat"]), (d["lon"], d["lat"])) * 1000 > gaz.district_extent(d)
    assert not gaz.in_district((c["lon"], c["lat"]), d)
