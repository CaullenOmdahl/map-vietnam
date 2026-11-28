"""scripts/base_cache.py: offline base-map tile and font glyph SQLite LRU cache."""

import time

import base_cache
import pytest
from fakerecords import wait_for


def test_tile_math_and_count():
    assert base_cache.lonlat_to_tile(0.0, 0.0, 0) == (0, 0)
    # Cao Lanh (105.63, 10.46) at z10
    x, y = base_cache.lonlat_to_tile(105.63, 10.46, 10)
    assert (x, y) == (812, 482)
    assert base_cache.count_tiles(base_cache.VIETNAM_BOUNDS, 0, 0) == 1
    assert base_cache.count_tiles(base_cache.VIETNAM_BOUNDS, 0, 2) == 3  # 1 + 1 + 1 across z0..z2
    assert base_cache.count_tiles(base_cache.VIETNAM_BOUNDS, 0, 3) == 4  # 1 tile at z3 too


def test_get_tile_caches_on_hit_and_handles_offline_and_disabled(tmp_path):
    cache = base_cache.BaseCache(tmp_path)
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return b"PNGDATA", "image/png"

    data, ctype = cache.get_tile("osm", 5, 25, 14, fetcher=fake_fetch)
    assert (data, ctype) == (b"PNGDATA", "image/png") and len(calls) == 1

    # Second read comes from SQLite, not upstream.
    data2, ctype2 = cache.get_tile(
        "osm", 5, 25, 14, fetcher=lambda u: (_ for _ in ()).throw(AssertionError("hit network"))
    )
    assert (data2, ctype2) == (b"PNGDATA", "image/png")

    # Offline uncached miss returns (None, None).
    def offline(url):
        raise OSError("network unreachable")

    assert cache.get_tile("osm", 5, 26, 14, fetcher=offline) == (None, None)

    # Disabling cache-as-you-browse still serves fetched tiles without persisting new ones.
    cache.set_config(enabled=False)
    data3, _ = cache.get_tile("sat", 5, 25, 14, fetcher=lambda u: (b"JPGDATA", "image/jpeg"))
    assert data3 == b"JPGDATA"
    assert not cache.has_tile("sat", 5, 25, 14)


def test_lru_eviction_enforces_max_bytes(tmp_path, monkeypatch):
    cache = base_cache.BaseCache(tmp_path)
    monkeypatch.setattr(base_cache, "MIN_MAX_BYTES", 100)
    cache.set_config(max_bytes=250)

    t = 1000.0
    monkeypatch.setattr(base_cache.time, "time", lambda: t)
    cache._store_tile("osm", 4, 0, 0, "image/png", b"a" * 100)
    t = 1001.0
    cache._store_tile("osm", 4, 0, 1, "image/png", b"b" * 100)
    t = 1002.0
    cache._store_tile("osm", 4, 0, 2, "image/png", b"c" * 100)

    # Total was 300 > 250, so oldest tile (0,0) was evicted.
    assert not cache.has_tile("osm", 4, 0, 0)
    assert cache.has_tile("osm", 4, 0, 1) and cache.has_tile("osm", 4, 0, 2)

    # Shrinking max_bytes evicts down to the new cap.
    cache.set_config(max_bytes=120)
    assert not cache.has_tile("osm", 4, 0, 1) and cache.has_tile("osm", 4, 0, 2)


def test_glyph_cache_and_validation(tmp_path):
    cache = base_cache.BaseCache(tmp_path)
    seen = []
    got = cache.get_glyph(
        "Noto Sans Medium", "0-255", fetcher=lambda u: (seen.append(u), (b"PBF", "application/x-protobuf"))[1]
    )
    assert got == b"PBF" and len(seen) == 1
    assert (
        cache.get_glyph("Noto Sans Medium", "0-255", fetcher=lambda u: (_ for _ in ()).throw(AssertionError("hit")))
        == b"PBF"
    )

    with pytest.raises(ValueError):
        cache.get_glyph("../etc/passwd", "0-255")
    with pytest.raises(ValueError):
        cache.get_glyph("Noto Sans Medium", "bad")


def test_estimate_prefetch_skip_and_clear(tmp_path):
    cache = base_cache.BaseCache(tmp_path)
    est = cache.estimate(["osm", "sat"], "vietnam", 0, 2)
    assert est["tiles_per_base"] == 3
    assert est["total_tiles"] == 6
    assert not est["exceeds_max_cache"]

    # Rejects when estimated size exceeds max_bytes, or when total tiles exceeds MAX_PRECACHE_TILES.
    cache.set_config(max_bytes=base_cache.MIN_MAX_BYTES)
    big = cache.estimate(["osm", "sat"], "vietnam", 0, 12)
    assert big["exceeds_max_cache"] and not big["exceeds_tile_limit"]
    with pytest.raises(ValueError, match="maximum cache size"):
        cache.start_prefetch(["osm", "sat"], "vietnam", 0, 12)
    with pytest.raises(ValueError, match="limit"):
        cache.start_prefetch(["osm"], [-10, -10, 10, 10], 0, 16)

    # Pre-seed one tile so prefetch skips it.
    cache._store_tile("osm", 0, 0, 0, "image/png", b"PRE")
    fetched = []

    def fake_fetch(url):
        fetched.append(url)
        return b"TILE", "image/png"

    cache.start_prefetch(["osm"], base_cache.VIETNAM_BOUNDS, 0, 1, label="Test VN", fetcher=fake_fetch)
    wait_for(lambda: cache.status()["job"]["state"] == "done")
    st = cache.status()
    assert st["job"]["done"] == 2 and st["job"]["skipped"] == 1 and len(fetched) == 1

    cleared = cache.clear()
    assert cleared["tile_count"] == 0 and cleared["used_bytes"] == 0


def test_prefetch_cancel(tmp_path):
    cache = base_cache.BaseCache(tmp_path)
    entered = []

    def slow_fetch(url):
        entered.append(url)
        time.sleep(0.05)
        return b"X", "image/png"

    cache.start_prefetch(["osm"], base_cache.VIETNAM_BOUNDS, 0, 6, label="Slow", fetcher=slow_fetch)
    wait_for(lambda: len(entered) >= 1)
    st = cache.cancel()
    assert st["job"]["state"] == "cancelled"
    assert st["job"]["done"] < st["job"]["total"]
