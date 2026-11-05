"""Tests for the HES-vs-GNS datum calibration, on three real HES hamlet / GNS place pairs
(tests/fixtures/datum_check/pairs.json); no evidence store is needed."""

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "ingest"))
sys.path.insert(0, str(ROOT / "scripts"))
import datum_check as dc  # noqa: E402

FIX = json.loads((ROOT / "tests" / "fixtures" / "datum_check" / "pairs.json").read_text(encoding="utf-8"))


def test_norm_name_matches_hes_and_gns_spellings():
    for h, g in zip(FIX["hes"], FIX["gns"], strict=True):
        assert dc.norm_name(h["gvn_hamlet"]) == dc.norm_name(g["title"]) == dc.norm_name(g["full_nm_nd"])
    assert dc.norm_name("TAN-AN") == "tanan"
    assert dc.norm_name("Ấp Tân An") == "tanan"
    assert dc.norm_name("Đông Hòa 2") == "donghoa2"
    assert dc.norm_name("Ap") == "ap"  # a lone prefix is kept, not emptied


def test_decode_both_gives_the_known_indian1960_wgs84_shift():
    # ~530 m, mostly east-south-east: the Indian 1960 -> WGS84 shift in Vietnam.
    ind, wgs = dc.decode_both("YD466583", "48Q")
    e, n = dc.enu_offset(*ind, *wgs)
    assert 380 < e < 460 and -370 < n < -290
    assert 500 < math.hypot(e, n) < 560


def test_match_pairs_on_real_excerpt():
    idx = dc.GnsIndex()
    for g in FIX["gns"]:
        idx.add(dc.norm_name(g["full_nm_nd"]), g["ufi"], g["lon"], g["lat"], bool(g["minute_rounded"]), g["title"])
    hes = [
        {
            "usid": h["usid"],
            "grid": h["original_coord"],
            "zone": h["zone"],
            "name": h["gvn_hamlet"],
            "province": h["gvn_province"],
        }
        for h in FIX["hes"]
    ]
    pairs, stats = dc.match_pairs(hes, idx)
    assert stats["matched"] == 3
    by = {p["usid"]: p for p in pairs}
    # Da Phuoc and Kon Solang lie ~1-2 km from GNS; the window is 3 km.
    for p in pairs:
        assert p["d_ind"] < 3500 and p["d_wgs"] < 3500
        assert abs(p["d_ind"] - math.hypot(*p["off_ind"])) < 1e-6
    assert by["206011816"]["gns_name"] == "Kon Sơ Lăng"


def test_ambiguous_names_are_skipped():
    idx = dc.GnsIndex()
    lon, lat = dc.decode_both("YD466583", "48Q")[0]
    idx.add("mythuy", "a", lon, lat, False, "x")
    idx.add("mythuy", "b", lon + 0.005, lat, False, "y")
    pairs, stats = dc.match_pairs([{"usid": "1", "grid": "YD466583", "zone": "48Q", "name": "MY-THUY"}], idx)
    assert not pairs and stats["ambiguous: >1 GNS feature with that name in window"] == 1


def test_summarise_recovers_planted_offset():
    # GNS points planted exactly on the Indian 1960 reading: median offset ~0, WGS84 off by ~0.5-0.6 km.
    pairs = []
    for h in FIX["hes"]:
        ind, wgs = dc.decode_both(h["original_coord"], h["zone"])
        oi, ow = (0.0, 0.0), dc.enu_offset(*wgs, *ind)
        pairs.append(
            {
                "ind": ind,
                "wgs": wgs,
                "off_ind": oi,
                "off_wgs": ow,
                "shift_wgs_minus_ind": dc.enu_offset(*ind, *wgs),
                "d_ind": 0.0,
                "d_wgs": math.hypot(*ow),
            }
        )
    s = dc.summarise(pairs)
    assert s["indian1960_reading"]["distance_to_gns_m"]["p50"] == 0
    assert 480 < s["wgs84_reading"]["distance_to_gns_m"]["p50"] < 650
    assert s["share_closer_on_indian1960"] == 1.0


def test_quantiles():
    q = dc.quantiles([0, 10, 20, 30, 40])
    assert q["p50"] == 20 and q["p25"] == 10 and q["p75"] == 30
