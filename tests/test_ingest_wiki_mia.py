"""Parsing tests for the Wikipedia US MIA list ingester, on small real excerpts of the pages."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import store  # noqa: E402
import wiki_mia as w  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "wiki_mia"
TEXT = (FIX / "excerpt_1966-68.wikitext").read_text(encoding="utf-8")
NAMED = (FIX / "excerpt_named_places.wikitext").read_text(encoding="utf-8")
PROV = w.load_provinces(FIX / "provinces_subset.json")
GNS = w.load_gns(FIX / "gns_subset.json")


def rows():
    return [(s, {k: w.clean(v) for k, v in c.items()}) for s, _, c in w.parse_tables(TEXT)]


def by_name(prefix):
    return next(c for _, c in rows() if c["name"].startswith(prefix))


def test_row_count_and_sections():
    r = rows()
    assert len(r) == 15
    assert [s for s, _ in r].count("1967") == 6


def test_rowspan_expands_service():
    # "rowspan=3 | USAF" on Callanan carries down to Greenley and Godfrey.
    assert by_name("Greenley")["service"] == "USAF"
    assert by_name("Greenley")["unit"] == "774th Troop Carrier Squadron"
    assert by_name("Godfrey")["location"] == "South Vietnam, Sóc Trăng"
    assert by_name("Schworer")["service"] == "US Army"


def test_continuation_line_cells():
    # Nightingale's row breaks onto a new line starting " || ".
    c = by_name("Nightingale")
    assert c["unit"] == "VS-23, USS Yorktown"
    assert c["location"] == "North Vietnam, Gulf of Tonkin"
    assert c["status"] == "Killed in action, body not recovered"


def test_clean_links_templates_refs():
    c = by_name("MacLaughlin")
    assert c["unit"] == "VA-76, USS Enterprise (CVN-65)"
    assert "<ref" not in c["circumstances"] and "virtualwall" not in c["circumstances"]


def test_status_classes():
    assert w.status_class("Killed in action, body not recovered") == "unrecovered"
    assert w.status_class("Died in captivity, remains not returned") == "unrecovered"
    assert w.status_class("Presumptive finding of death") == "unrecovered"
    assert w.status_class("Missing") == "unrecovered"
    assert w.status_class("Unaccounted for") == "unrecovered"
    assert w.status_class(by_name("Krogman")["status"]) == "recovered"
    assert w.status_class("Recovered, September 2012") == "recovered"
    assert w.status_class("Missing in action; remains recovered in 1999 and identified in 2003") == "recovered"


def test_locate():
    p, place, why = w.locate("South Vietnam, Kien Phong Province", PROV)
    assert (p["code"], why) == ("432", "province")
    p, place, why = w.locate("South Vietnam, Đồng Tháp Province", PROV)
    assert p["code"] == "432+446" and why == "modern_province"
    assert p["radius_m"] > PROV["432"]["radius_m"]
    assert w.locate("North Vietnam, Gulf of Tonkin", PROV)[2] == "at_sea"
    assert w.locate("Laos, Savannakhet Province", PROV)[2] == "laos"
    assert w.locate("North Vietnam, Thanh Hóa Province", PROV)[2] == "north_vietnam"
    assert w.locate("South Vietnam", PROV)[2] == "no_province"
    assert w.locate("South Vietnam, Pleiku", PROV)[0]["code"] == "208"


def test_records_end_to_end():
    recs, stats = w.records({"1966–67": TEXT}, PROV, GNS)
    ids = {e.source_record_id: e for e in recs}
    assert stats["status_recovered"] == 1  # Krogman, accounted for 2020
    s = ids["1967-schworer_ronald_p"]
    assert s.kind == "loss_incident" and s.date_start == "1967-04-09"
    assert s.original_coord == "South Vietnam, Kien Phong Province"
    assert s.url.startswith("https://en.wikipedia.org/wiki/List_of_United_States")
    c = ids["1966-callanan_richard_j"]
    assert c.kind == "aircraft_crash" and c.raw["province"] == "PLEIKU"
    assert "https://www.virtualwall.org/dc/CallananRJ01a.htm" in c.raw["ref_urls"]
    # Out-of-RVN, at-sea, and province-less rows are skipped and counted.
    assert "1967-welch_robert_j" not in ids and "1966-greenley_jon_a" not in ids
    assert stats["skip_at_sea"] == 4 and stats["skip_no_province"] == 1
    # A plain "<X> Province" row: province centroid, radius = the province's covering radius.
    a = ids["1967-anspach_robert_a"]
    assert a.raw["placement"] == "modern_province" and a.original_datum == "province-centroid"


def covers(e, lon, lat, r=0.0):
    return w.dist_m(e.lon, e.lat, lon, lat) + r <= e.uncertainty_m + 1


def test_schworer_citation_conflict_covers_both_places():
    """The row says Kien Phong; its own Virtual Wall citation says Gia Dinh / the Rung Sat."""
    recs, _ = w.records({"1966–67": TEXT}, PROV, GNS)
    s = next(e for e in recs if e.source_record_id == "1967-schworer_ronald_p")
    assert s.raw["placement"] == "citation_conflict" and s.original_datum == "conflict-cover"
    assert "CONFLICT" in s.description and "Rung Sat" in s.description and "KIEN PHONG" in s.description
    kp, gd = PROV["432"], PROV["329"]
    assert covers(s, kp["lon"], kp["lat"], kp["radius_m"]) and covers(s, gd["lon"], gd["lat"], gd["radius_m"])
    assert s.raw["citation_conflict"]["separation_m"] > 100_000


def test_named_places_geocoded_from_gns_with_feature_radius():
    recs, stats = w.records({"1968–69": NAMED}, PROV, GNS)
    by = {e.raw["name"]: e for e in recs}
    lv = by["Phillips, Daniel R"]  # "South Vietnam, Lang Vei": a village, GNS PPL
    assert lv.raw["placement"] == "gns_place" and lv.original_datum == "gns-place-geocode"
    assert lv.uncertainty_m == 3000 and covers(lv, 106.683333, 16.6)
    assert "Làng Vei" in lv.description and "PPL" in lv.description
    ks = by["Pepper, Anthony J"]  # Khe Sanh: battle area round the town (GNS PPLA2)
    assert ks.raw["placement"] == "gns_place" and ks.uncertainty_m == 12000
    assert covers(ks, 106.73, 16.655)  # Khe Sanh Combat Base
    assert ks.uncertainty_m < PROV["101"]["radius_m"]
    ash = by["Crook, Elliott"]  # A Shau Valley: covers A Shau camp and A Luoi + 10 km
    assert ash.raw["placement"] == "gns_place" and 15_000 <= ash.uncertainty_m <= 30_000
    assert covers(ash, 107.366667, 16.1, 10_000) and covers(ash, 107.233849, 16.272282, 10_000)
    cl = by["Greenleaf, Joseph G"]  # "Cam Lo District": the wartime HES district
    assert cl.raw["placement"] == "hes_district" and cl.original_datum == "hes-district-centroid"
    d = PROV["101"]["districts"]["10104"]
    assert (cl.lon, cl.lat, cl.uncertainty_m) == (d["lon"], d["lat"], d["radius_m"])
    cs = by["Berube, Kenneth A"]  # Chu Lai base area
    assert cs.raw["placement"] == "gns_place" and covers(cs, 108.7, 15.4)
    # Con Thien is not in GNS: whole-province fallback, said so in the description.
    ct = by["Wilson, Wayne V"]
    assert ct.raw["placement"] == "province_fallback" and ct.uncertainty_m == PROV["101"]["radius_m"]
    assert "not found" in ct.description
    assert stats["placement_gns_place"] >= 4


def test_identified_remains_are_excluded():
    """Wall's status reads '... On 30 August 2012 the DPMO announced that his remains had been
    identified'; a self-closing <ref name="DPMO-MIA/KIA-BNR1"/> used to swallow that sentence."""
    c = next(
        c
        for _, c in ((s, {k: w.clean(v) for k, v in c.items()}) for s, _, c in w.parse_tables(NAMED))
        if c["name"].startswith("Wall")
    )
    assert "remains had been identified" in c["status"]
    assert w.status_class(c["status"]) == "recovered"
    recs, stats = w.records({"1968–69": NAMED}, PROV, GNS)
    names = {e.raw["name"] for e in recs}
    assert not names & {"Wall, Jerry M", "Johnstone, James M", "Whited, James L"}
    assert stats["status_recovered"] == 3


def test_ids_stable_and_content_derived():
    recs, _ = w.records({"1966–67": TEXT}, PROV, GNS)
    head, *secs = re.split(r"(?m)^(?===\d{4}==)", TEXT)  # put the year sections in reverse order
    recs_shuffled, _ = w.records({"1966–67": head + "".join(reversed(secs))}, PROV, GNS)
    assert sorted(e.source_record_id for e in recs) == sorted(e.source_record_id for e in recs_shuffled)
    assert [e.source_record_id for e in recs] != [e.source_record_id for e in recs_shuffled]
    assert all(not e.source_record_id.split("-")[-1].isdigit() for e in recs)
    a = {"name": "Smith, John", "date": "May 1", "unit": "A", "location": "X", "circumstances": "c1"}
    b = dict(a, date="June 2")
    c = dict(a, unit="B")
    assert w.record_ids([("1968", a)]) == ["1968-smith_john"]
    assert w.record_ids([("1968", a), ("1968", b)]) == ["1968-smith_john-1968-05-01", "1968-smith_john-1968-06-02"]
    ids = w.record_ids([("1968", a), ("1968", c)])
    assert len(set(ids)) == 2 and all(i.startswith("1968-smith_john-1968-05-01-") for i in ids)
    assert w.record_ids([("1968", c), ("1968", a)]) == ids[::-1]  # order does not matter


def test_sync_twice_is_a_no_op(tmp_path):
    recs, _ = w.records({"1966–67": TEXT, "1968–69": NAMED}, PROV, GNS)
    db = store.connect(tmp_path / "e.sqlite")
    assert w.save(db, recs) == (len(recs), 0, 0)

    def dump():
        return db.execute("select * from evidence order by source_record_id").fetchall()

    first = dump()
    assert w.save(db, recs) == (len(recs), 0, 0)
    assert dump() == first


def test_parse_date():
    assert w.parse_date("April 9", "1967") == "1967-04-09"
    assert w.parse_date("April", "1967") == "1967-04"
    assert w.parse_date("", "1967") == "1967"
