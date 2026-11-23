"""library: maps are added only when asked, from the catalogue's URLs only, one at a time, and can be
removed again, files and all. Runs against a fake archive (tests/fakeweb.py); no network."""

import json
import threading
import time
from pathlib import Path

import catalog
import download_maps
import files
import library
import pytest
import tiling
from fakeweb import FakeArchive, make_library, path_of
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def archive():
    a = FakeArchive()
    yield a
    a.close()


@pytest.fixture
def lib(tmp_path, archive, monkeypatch):
    return make_library(tmp_path, archive, monkeypatch)


def states(lib):
    return {m["id"]: m["state"] for m in lib.listing()["maps"]}


def layers(lib):
    return json.loads((lib.root / "web" / "catalog.json").read_text(encoding="utf-8"))["layers"]


def test_nothing_is_downloaded_or_installed_until_asked(lib, archive):
    assert set(states(lib).values()) == {"available"}
    assert archive.requests == [] and not (lib.root / "web" / "tiles").exists()
    assert lib.listing()["queue_bytes"] == 0


def test_adding_a_sheet_downloads_builds_and_lists_it(lib, archive):
    assert lib.add(["L7014A"]) == ["L7014A"]
    listing = lib.listing()
    assert states(lib)["L7014A"] == "queued"
    assert listing["queue_bytes"] == lib.entries()["L7014A"]["bytes"]
    seen = []
    lib.run_pending(lambda job: seen.append(job.state))

    assert seen[0] == "downloading" and "building" in seen and seen[-1] == "installed"
    assert archive.requests == [path_of(lib, "L7014A")]  # the first URL worked: no mirror asked
    assert lib.tiles_path("L7014A").exists()
    # The download is kept (a rebuild needs no network); the build leaves nothing else behind.
    assert (lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf").exists()
    assert not list(lib.root.rglob("*.part")) and not list(lib.root.rglob("*.partial"))
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    assert entry["state"] == "installed" and entry["disk_bytes"] > entry["bytes"] and entry["progress"] is None
    (layer,) = layers(lib)
    assert layer["id"] == "L7014A" and layer["title"] == "6129 II, near Cần Thơ" and layer["date"] == "1969"
    assert layer["series"] == "AMS L7014" and layer["tiles"] == "tiles/L7014A/{z}/{x}/{y}.webp"
    assert lib.add(["L7014A"]) == []  # installed already: nothing to do


def test_the_viewer_can_describe_maps_before_they_are_added(lib):
    listing = lib.listing()
    # Series names and scales, so a row can say "1:50,000" without a second request.
    assert set(listing["series"]) >= {m["series"] for m in listing["maps"] if m["series"]}
    # A scan's year comes from its control file, so it is known before anything is downloaded.
    scan = next(m for m in listing["maps"] if m["id"] == "scan_test")
    assert scan["state"] == "available" and scan["date"] == "1909"


def test_catalog_lists_a_map_by_the_time_it_is_reported_installed(lib):
    # The viewer reloads catalog.json the moment it sees "installed"; the file must be ready.
    in_catalog = []
    lib.add(["L7014A"])
    lib.run_pending(lambda job: job.state == "installed" and in_catalog.extend(layer["id"] for layer in layers(lib)))
    assert in_catalog == ["L7014A"]


def test_a_failing_host_falls_back_to_the_next_url(lib, archive):
    archive.status[path_of(lib, "L7014B", 0)] = 404
    lib.add(["L7014B"])
    lib.run_pending()
    assert states(lib)["L7014B"] == "installed"
    assert archive.requests == [path_of(lib, "L7014B", 0), path_of(lib, "L7014B", 1)]


def test_busy_servers_are_retried_and_a_lasting_failure_is_explained(lib, archive):
    for i in (0, 1):
        archive.status[path_of(lib, "L7014B", i)] = 503
    lib.add(["L7014B"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014B")
    assert entry["state"] == "failed" and "HTTP 503" in entry["error"]
    assert len(archive.requests) == 2 * download_maps.ATTEMPTS
    assert not list(lib.root.rglob("*.part")) and entry["disk_bytes"] is None
    # Adding it again tries again, and this time the archive is back.
    archive.status.clear()
    assert lib.add(["L7014B"]) == ["L7014B"]
    lib.run_pending()
    assert states(lib)["L7014B"] == "installed"


def test_an_error_page_instead_of_a_pdf_fails_cleanly(lib, archive):
    for i in (0, 1):
        archive.files[path_of(lib, "L7014A", i)] = b"<html>try later</html>"
    lib.add(["L7014A"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    assert entry["state"] == "failed" and "not a PDF" in entry["error"]
    assert not (lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf").exists()


def test_a_changed_archive_file_is_noted_but_used(lib, archive):
    lib.entries()["L7014A"]  # load
    path = lib.catalogue_path
    cat = json.loads(path.read_text(encoding="utf-8"))
    cat["maps"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(cat), encoding="utf-8")
    lib.add(["L7014A"])
    notes = []
    lib.run_pending(lambda job: notes.extend(job.notes))
    assert states(lib)["L7014A"] == "installed" and any("differs" in n for n in notes)


def test_a_failed_build_is_reported_with_its_log(lib, archive, monkeypatch):
    lib.add(["L7014A"])
    monkeypatch.setattr(lib, "build_command", lambda entry, path, out: [lib.python, "-c", "raise SystemExit('boom')"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    assert entry["state"] == "failed" and "boom" in entry["error"] and "L7014A.log" in entry["error"]
    assert not lib.tiles_path("L7014A").exists()


def test_scans_are_placed_by_their_control_points(lib):
    lib.add(["scan_test"])
    lib.run_pending()
    assert states(lib)["scan_test"] == "installed"
    assert (lib.root / "data" / "raw" / "scans" / "scan_test" / "work.jpg").exists()
    (layer,) = layers(lib)
    assert layer["date"] == "1909" and layer["maxzoom"] == 9
    assert layer["bounds"] == pytest.approx([105.0, 10.0, 105.6, 10.4], abs=0.01)


def test_a_scan_on_the_wrong_pixel_grid_is_refused(lib):
    cat = json.loads(lib.catalogue_path.read_text(encoding="utf-8"))
    cat["maps"][3]["work_size"] = [601, 400]
    lib.catalogue_path.write_text(json.dumps(cat), encoding="utf-8")
    lib.add(["scan_test"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "scan_test")
    assert entry["state"] == "failed" and "601 x 400" in entry["error"] and entry["disk_bytes"] is None


def test_jog_sheets_render_at_300_dpi_and_build_on_wgs84(lib):
    cmd = lib.build_command(lib.entries()["JOG_NF4811"], Path("j.pdf"), Path("j.mbtiles"))
    assert cmd[-2:] == ["--dpi", "300"]
    lib.add(["JOG_NF4811"])
    lib.run_pending()
    assert states(lib)["JOG_NF4811"] == "installed"
    assert tiling.read_metadata(lib.tiles_path("JOG_NF4811"))["datum"] == "WGE"


def test_removing_deletes_tiles_and_download_and_updates_the_viewer(lib):
    lib.add(["L7014A", "scan_test"])
    lib.run_pending()
    assert {layer["id"] for layer in layers(lib)} == {"L7014A", "scan_test"}
    assert lib.remove(["L7014A", "scan_test"]) == ["L7014A", "scan_test"]
    assert not lib.tiles_path("L7014A").exists()
    assert not (lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf").exists()
    assert not (lib.root / "data" / "raw" / "scans" / "scan_test").exists()
    assert layers(lib) == [] and set(states(lib).values()) == {"available"}


def test_removing_a_queued_map_means_it_is_never_fetched(lib, archive):
    lib.add(["L7014A", "L7014B"])
    lib.remove(["L7014B"])
    lib.run_pending()
    assert states(lib)["L7014B"] == "available" and states(lib)["L7014A"] == "installed"
    assert all("L7014B" not in r for r in archive.requests)


def test_removing_a_map_while_it_downloads_stops_it(lib, archive):
    archive.slow[path_of(lib, "L7014A")] = 0.2
    archive.files[path_of(lib, "L7014A")] += b"%" * (1 << 20)  # long enough to catch midway
    downloading = threading.Event()
    lib.add(["L7014A"])
    worker = threading.Thread(target=lib.run_pending, args=(lambda j: j.progress and downloading.set(),))
    worker.start()
    assert downloading.wait(10)
    lib.remove(["L7014A"])
    worker.join(10)
    assert not worker.is_alive()
    assert states(lib)["L7014A"] == "available"
    assert not list((lib.root / "data" / "raw").rglob("*.pdf*"))  # neither the file nor its .part


def test_removing_a_map_while_it_builds_stops_the_builder(lib, monkeypatch):
    building = threading.Event()
    slow = [lib.python, "-c", "import time; print('progress 0.1', flush=True); time.sleep(60)"]
    monkeypatch.setattr(lib, "build_command", lambda entry, path, out: slow)
    lib.add(["L7014A"])
    worker = threading.Thread(target=lib.run_pending, args=(lambda j: j.progress and building.set(),))
    worker.start()
    assert building.wait(20)
    lib.remove(["L7014A"])
    worker.join(10)
    assert not worker.is_alive() and states(lib)["L7014A"] == "available"
    assert not (lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf").exists()


def test_only_catalogue_maps_can_be_added(lib):
    with pytest.raises(library.LibraryError, match="not in the map list: https://evil.example/x.pdf"):
        lib.add(["https://evil.example/x.pdf"])
    with pytest.raises(library.LibraryError, match="nope"):
        lib.remove(["nope"])


def test_downloads_refuse_addresses_that_are_not_https(tmp_path):
    entry = {"id": "x", "series": "l7014", "kind": "geopdf", "urls": ["http://example.org/x.pdf"]}
    with pytest.raises(download_maps.MapFetchError, match="not an https address"):
        download_maps.fetch_map(entry, tmp_path)


def test_the_background_worker_works_through_the_queue(lib):
    lib.start_worker()
    lib.add(["L7014A", "L7014B"])
    assert lib.wait_idle(60)
    assert states(lib)["L7014A"] == states(lib)["L7014B"] == "installed"


def test_another_process_adding_maps_makes_this_one_wait(lib):
    lib.add(["L7014A"])
    with lib.exclusive():
        worker = threading.Thread(target=lib.run_pending)
        worker.start()
        worker.join(1)
        assert worker.is_alive() and states(lib)["L7014A"] == "queued"
    worker.join(30)
    assert states(lib)["L7014A"] == "installed"


def test_leftovers_of_interrupted_work_are_cleaned_up(lib):
    tiles = lib.root / "web" / "tiles"
    tiles.mkdir(parents=True)
    (tiles / "L7014B.mbtiles.partial").write_bytes(b"x")
    (lib.root / "data" / "raw" / "l7014").mkdir(parents=True)
    (lib.root / "data" / "raw" / "l7014" / "L7014B_geo.pdf.part").write_bytes(b"x")
    lib.add(["L7014A"])
    lib.run_pending()
    assert not list(lib.root.rglob("*.part")) and not list(tiles.glob("*.partial"))


def test_area_and_region_selection(lib):
    assert {m["id"] for m in lib.select(region="north")} == {"JOG_NF4811"}
    assert {m["id"] for m in lib.select(area=library.parse_area("105.0,10.39,105.56,10.41"))} == {"L7014A", "scan_test"}
    for bad in ("1,2,3", "106,10,105,11", "a,b,c,d"):
        with pytest.raises(library.LibraryError):
            library.parse_area(bad)
    with pytest.raises(library.LibraryError):
        lib.select(region="west")


def test_command_line_list_add_and_remove(lib, monkeypatch, capsys):
    monkeypatch.setattr(library, "Library", lambda: lib)
    assert library.main(["list", "--region", "south"]) == 0
    out = capsys.readouterr().out
    assert "L7014A" in out and "JOG_NF4811" not in out and "3 maps" in out

    assert library.main(["add", "--area", "105.58,10.42,105.59,10.43", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "1 map:" in out and "[1/1] 6129 II, near Cần Thơ: ready" in out and "1 of 1 maps added" in out
    assert library.main(["list", "--installed"]) == 0
    assert "1 maps" in capsys.readouterr().out

    assert library.main(["add", "L7014A", "--region", "south"]) == 2
    assert "not both" in capsys.readouterr().err
    assert library.main(["remove", "--all", "--yes"]) == 0
    assert "Removed 1 map." in capsys.readouterr().out and lib.installed_ids() == []


def test_old_png_tile_folders_are_converted_in_place(lib):
    folder = lib.root / "web" / "tiles" / "L7014A"
    for z, x, y in [(8, 203, 120), (9, 406, 241)]:
        (folder / str(z) / str(x)).mkdir(parents=True)
        Image.new("RGBA", (256, 256), (10, 20, 30, 255)).save(folder / str(z) / str(x) / f"{y}.png")
    (folder / "9" / "407").mkdir()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(folder / "9" / "407" / "241.png")
    old_work = lib.root / "data" / "work" / "l7014"
    old_work.mkdir(parents=True)
    (old_work / "L7014A.bounds.json").write_text(
        json.dumps({"bounds": [105.75, 10.0, 106.0, 10.25], "maxzoom": 9}), encoding="utf-8"
    )
    (old_work / "L7014A.tif").write_bytes(b"big intermediate")

    assert lib.migrate_legacy(say=lambda text: None) == ["L7014A"]

    assert not folder.exists() and not list(old_work.iterdir())
    meta = tiling.read_metadata(lib.tiles_path("L7014A"))
    assert catalog.parse_bounds(meta["bounds"]) == [105.75, 10.0, 106.0, 10.25] and meta["maxzoom"] == "9"
    db = tiling.connect_read_only(lib.tiles_path("L7014A"))
    assert tiling.read_tile(db, 9, 406, 241) and tiling.read_tile(db, 9, 407, 241) is None  # empty tile dropped
    assert states(lib)["L7014A"] == "installed" and [layer["id"] for layer in layers(lib)] == ["L7014A"]


def test_maps_installed_but_not_in_the_catalogue_can_still_be_removed(lib):
    with tiling.MBTilesWriter(lib.tiles_path("handmade")) as w:
        w.metadata = tiling.standard_metadata("Hand made", [105, 10, 106, 11], 8, 12)
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "handmade")
    assert entry["state"] == "installed" and entry["title"] == "Hand made" and entry["urls"] == []
    with pytest.raises(library.LibraryError):
        lib.add(["handmade"])
    assert lib.remove(["handmade"]) == ["handmade"] and not lib.tiles_path("handmade").exists()


REAL_CATALOGUE = ROOT / "data" / "sources" / "map_library.json"


def test_the_real_catalogue_has_the_shape_the_library_needs():
    cat = json.loads(REAL_CATALOGUE.read_text(encoding="utf-8"))
    ids = [m["id"] for m in cat["maps"]]
    assert len(ids) == len(set(ids))
    for m in cat["maps"]:
        assert m["series"] in cat["series"] and m["kind"] in ("geopdf", "scan")
        assert m["region"] in library.REGIONS
        w, s, e, n = m["bounds"]
        assert w < e and s < n
        assert m["urls"] and all(u.startswith("https://") for u in m["urls"])
        assert isinstance(m["bytes"], int) and m["bytes"] > 0
        if m["kind"] == "scan":
            assert (ROOT / "data" / "scans" / f"{m['id']}.json").exists()


# ---- review fixes: choosing, failure, cleanup, and another process --------------------------
def test_add_never_means_every_map(lib, monkeypatch, capsys, archive):
    monkeypatch.setattr(library, "Library", lambda: lib)
    assert library.main(["add", "--yes"]) == 2
    assert "name the maps to add" in capsys.readouterr().err
    assert lib.listing()["queue_bytes"] == 0 and archive.requests == []


def test_without_a_terminal_add_asks_nobody_and_adds_nothing(lib, monkeypatch, capsys, archive):
    monkeypatch.setattr(library, "Library", lambda: lib)
    monkeypatch.setattr(library.sys.stdin, "isatty", lambda: False)
    assert library.main(["add", "L7014A"]) == 0
    out = capsys.readouterr().out
    assert "add --yes to go ahead" in out and "Nothing was added." in out
    assert archive.requests == [] and states(lib)["L7014A"] == "available"


def test_a_failed_build_keeps_only_its_log_so_trying_again_downloads_afresh(lib, archive, monkeypatch):
    real_build_command, failing = lib.build_command, [True]

    def build_command(entry, path, out):
        return [lib.python, "-c", "raise SystemExit('boom')"] if failing[0] else real_build_command(entry, path, out)

    monkeypatch.setattr(lib, "build_command", build_command)
    lib.add(["L7014A"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    # The message names the log relative to the project folder, never a home folder path.
    assert "(details: data/work/maps/logs/L7014A.log)" in entry["error"]
    assert lib.log_path("L7014A").exists() and entry["disk_bytes"] is None
    assert not (lib.root / "data" / "raw" / "l7014").exists()
    failing[0] = False
    lib.add(["L7014A"])
    lib.run_pending()
    assert states(lib)["L7014A"] == "installed" and len(archive.requests) == 2
    lib.remove(["L7014A"])
    assert not lib.log_path("L7014A").exists()  # removing a map leaves nothing of it behind


def test_a_stuck_builder_is_stopped(lib, monkeypatch):
    monkeypatch.setattr(library, "BUILD_TIMEOUT_S", 1)
    stuck = [lib.python, "-c", "import time; print('progress oops', flush=True); time.sleep(60)"]
    monkeypatch.setattr(lib, "build_command", lambda entry, path, out: stuck)
    lib.add(["L7014A"])
    lib.run_pending()
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    assert entry["state"] == "failed" and "was stopped" in entry["error"]


def test_progress_lines_are_read_leniently():
    assert library.progress_of("progress 0.25\n") == 0.25 and library.progress_of("progress 1.5") == 1.0
    assert library.progress_of("progress oops") is None and library.progress_of("Rendering ...") is None


def test_catalog_rewrites_from_many_threads_never_collide(lib):
    errors = []

    def rewrite():
        try:
            for _ in range(30):
                lib.write_catalog()
        except Exception as e:  # the old fixed temp name made FileNotFoundError here
            errors.append(e)

    threads = [threading.Thread(target=rewrite) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and layers(lib) == []
    assert not [p for p in (lib.root / "web").iterdir() if p.name != "catalog.json"]


def test_outlines_are_drawn_on_wgs84(lib):
    by_id = {m["id"]: m for m in lib.listing()["maps"]}
    # A sheet's catalogue corners are on Indian 1960; on WGS84 they lie about 400 m west.
    w, s, e, n = by_id["L7014A"]["bounds"]
    ow, os_, oe, on = by_id["L7014A"]["outline"]
    assert -0.006 < ow - w < -0.002 and -0.006 < oe - e < -0.002 and 0 < os_ - s < 0.004
    assert by_id["scan_test"]["outline"] == by_id["scan_test"]["bounds"]  # scans are fitted on WGS84
    assert by_id["L7014A"]["disk_estimate"] == round(by_id["L7014A"]["bytes"] * library.DEFAULT_DISK_PER_DOWNLOAD_BYTE)
    lib.add(["L7014A"])
    lib.run_pending()
    installed = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    meta = tiling.read_metadata(lib.tiles_path("L7014A"))
    assert installed["outline"] == catalog.parse_bounds(meta["bounds"])


def test_a_jog_sheet_is_cut_at_its_frame_not_at_its_scan(lib):
    """The fixture's JOG frame stops short of the sheet's neatline on every side, as a real one
    stops short of the strip of margin around its scan."""
    lib.add(["JOG_NF4811"])
    lib.run_pending()
    w, s, e, n = catalog.parse_bounds(tiling.read_metadata(lib.tiles_path("JOG_NF4811"))["bounds"])
    assert [w, s, e, n] == pytest.approx([105.05, 21.1, 105.2, 21.24], abs=2e-3)


def test_maps_cut_short_by_a_stop_are_cleaned_up_and_reported_when_the_server_starts(lib):
    tiles = lib.root / "web" / "tiles"
    tiles.mkdir(parents=True)
    (tiles / "L7014B.mbtiles.partial").write_bytes(b"x")
    raw = lib.root / "data" / "raw" / "l7014"
    raw.mkdir(parents=True)
    (raw / "L7014A_geo.pdf.part").write_bytes(b"x")
    with lib.exclusive():  # someone is working: their files are not leftovers
        assert lib.recover_interrupted() == []
    assert (raw / "L7014A_geo.pdf.part").exists()
    assert lib.recover_interrupted() == ["L7014A", "L7014B"]
    assert not list(lib.root.rglob("*.part")) and not list(tiles.glob("*.partial"))
    entry = next(m for m in lib.listing()["maps"] if m["id"] == "L7014A")
    assert entry["state"] == "failed" and "interrupted" in entry["error"]
    assert lib.add(["L7014A"]) == ["L7014A"]  # trying again works as for any failure


def test_old_folders_that_cannot_be_converted_are_left_and_the_rest_go_on(lib):
    tiles = lib.root / "web" / "tiles"
    (tiles / "notes").mkdir(parents=True)  # not a tile folder at all: not ours to touch
    (tiles / "L7014B" / "9").mkdir(parents=True)  # a zoom folder with no tiles in it
    good = tiles / "L7014A" / "9" / "406"
    good.mkdir(parents=True)
    Image.new("RGBA", (256, 256), (10, 20, 30, 255)).save(good / "241.png")
    said = []
    assert lib.migrate_legacy(say=said.append) == ["L7014A"]
    assert (tiles / "notes").exists() and (tiles / "L7014B").exists()
    assert any("could not convert L7014B" in line for line in said)


def test_old_folders_are_counted_until_they_are_converted_in_the_background(lib):
    assert lib.legacy_status() == {"count": 0, "bytes": 0, "state": "idle", "error": None, "converted": 0}
    tiles = lib.root / "web" / "tiles"
    (tiles / "notes").mkdir(parents=True)  # not a tile folder: not counted
    good = tiles / "L7014A" / "9" / "406"
    good.mkdir(parents=True)
    Image.new("RGBA", (256, 256), (10, 20, 30, 255)).save(good / "241.png")
    status = lib.legacy_status()
    assert status["count"] == 1 and status["bytes"] == (good / "241.png").stat().st_size
    assert lib.listing()["legacy"] == status

    lib.start_migration()
    lib.start_migration()  # asking twice while it runs starts nothing more
    deadline = time.monotonic() + 20
    while lib.legacy_status()["state"] == "converting" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert lib.legacy_status() == {"count": 0, "bytes": 0, "state": "idle", "error": None, "converted": 1}
    assert states(lib)["L7014A"] == "installed" and [layer["id"] for layer in layers(lib)] == ["L7014A"]


def test_stop_ends_the_map_being_added_and_forgets_the_queue(lib, monkeypatch):
    slow = [lib.python, "-c", "import time; print('progress 0.1', flush=True); time.sleep(60)"]
    monkeypatch.setattr(lib, "build_command", lambda entry, path, out: slow)
    lib.add(["L7014A", "L7014B"])
    lib.start_worker()
    deadline = time.monotonic() + 20
    while not (states(lib)["L7014A"] == "building" and lib._jobs["L7014A"].proc) and time.monotonic() < deadline:
        time.sleep(0.02)
    builder = lib._jobs["L7014A"].proc
    assert lib.stop(timeout=20) is True
    assert builder.poll() is not None  # no builder is left running when Map Vietnam quits
    assert states(lib)["L7014B"] == "available" and not lib.tiles_path("L7014A").exists()


class OtherProcess:
    """Another process adding `map_id`: a child that holds the worker lock and has written
    current.json, as `start.py maps add` does, until stopped."""

    def __init__(self, lib, map_id, state="building"):
        import subprocess
        import sys

        code = (
            "import fcntl, sys, time; f = open(sys.argv[1], 'a+'); fcntl.flock(f, fcntl.LOCK_EX);"
            "print('locked', flush=True); time.sleep(60)"
        )
        lib.work.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [sys.executable, "-c", code, str(lib.work / "worker.lock")],
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        assert self.proc.stdout.readline().strip() == "locked"
        library.write_json(lib.work / "current.json", {"id": map_id, "pid": self.proc.pid, "state": state})

    def stop(self):
        self.proc.kill()
        self.proc.wait()


@pytest.mark.skipif(files.fcntl is None, reason="the stand-in for another process uses fcntl")
def test_a_map_another_process_is_adding_shows_its_state_and_can_be_removed(lib):
    other = OtherProcess(lib, "L7014A")
    try:
        assert states(lib)["L7014A"] == "building"
        raw = lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"%PDF- being built from")
        assert lib.remove(["L7014A", "L7014B"]) == ["L7014A"]
        # The other process owns the files of the map it is on: it deletes them once stopped.
        assert raw.exists() and lib.cancel_requested("L7014A")
        # A map in its queue gets a request too, so it is skipped when its turn comes.
        assert lib.cancel_requested("L7014B")
    finally:
        other.stop()
    # Once that process is gone, its record means nothing.
    assert states(lib)["L7014A"] == "available"
    # Adding a map again overrules an earlier removal.
    lib.add(["L7014B"])
    assert not lib.cancel_requested("L7014B")


def test_a_removal_asked_for_by_another_process_stops_this_one(lib, monkeypatch):
    building = threading.Event()
    slow = [lib.python, "-c", "import time; print('progress 0.1', flush=True); time.sleep(60)"]
    monkeypatch.setattr(lib, "build_command", lambda entry, path, out: slow)
    lib.add(["L7014A", "L7014B"])
    lib.request_cancel("L7014B")  # removed elsewhere while waiting in this process's queue
    seen = []

    def report(job):
        seen.append((job.entry["id"], job.state))
        if job.state == "building" and job.progress:
            building.set()

    worker = threading.Thread(target=lib.run_pending, args=(report,))
    worker.start()
    assert building.wait(20)
    current = json.loads((lib.work / "current.json").read_text(encoding="utf-8"))
    assert current["id"] == "L7014A" and current["state"] == "building"
    lib.request_cancel("L7014A")  # what remove() in the viewer's server does
    worker.join(10)
    assert not worker.is_alive()
    assert ("L7014A", "removed") in seen and ("L7014B", "removed") in seen
    assert set(states(lib).values()) == {"available"}
    assert not (lib.root / "data" / "raw" / "l7014").exists() and not lib.cancel_requested("L7014A")
    assert not (lib.work / "current.json").exists()


def test_a_map_another_process_added_meanwhile_is_not_added_twice(lib, archive):
    lib.add(["L7014A"])
    with tiling.MBTilesWriter(lib.tiles_path("L7014A")) as w:  # the command line finished it first
        w.metadata = tiling.standard_metadata("A", [105.5, 10.4, 105.6, 10.44], 8, 12)
    lib.run_pending()
    assert states(lib)["L7014A"] == "installed" and archive.requests == []


def test_a_delete_refused_while_the_file_is_open_is_retried(tmp_path, monkeypatch):
    # Windows will not delete a file the server has open for a tile; the delete waits and tries again.
    path = tmp_path / "m.mbtiles"
    path.write_bytes(b"x")
    real_unlink, refusals = type(path).unlink, []

    def unlink(self, missing_ok=False):
        if len(refusals) < 2:
            refusals.append(self)
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(type(path), "unlink", unlink)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    files.delete(path)
    assert len(refusals) == 2 and not path.exists()


def test_sizes_ignore_files_that_vanish_while_counting(tmp_path, monkeypatch):
    folder = tmp_path / "old"
    (folder / "1").mkdir(parents=True)
    (folder / "1" / "a.png").write_bytes(b"x" * 10)
    (folder / "1" / "b.png").write_bytes(b"x" * 5)
    assert files.path_bytes(folder) == 15 and files.path_bytes(tmp_path / "none") == 0
    real_stat = Path.stat

    def vanishing(self, *args, **kwargs):  # a file deleted between being listed and measured
        if self.name == "b.png":
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", vanishing)
    assert files.path_bytes(folder) == 10


def test_a_json_file_that_cannot_be_written_leaves_nothing_behind(tmp_path):
    with pytest.raises(TypeError):
        files.write_json(tmp_path / "state.json", {"x": object()})
    assert list(tmp_path.iterdir()) == []
    files.write_json(tmp_path / "state.json", {"x": 1})
    assert files.read_json(tmp_path / "state.json") == {"x": 1}


def test_a_legacy_folder_deleted_while_listing_is_skipped(lib, monkeypatch):
    gone = lib.tiles / "L7014A"
    (gone / "9").mkdir(parents=True)
    real_iterdir = Path.iterdir

    def deleted_meanwhile(self):
        if self == gone:
            raise FileNotFoundError(self)
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", deleted_meanwhile)
    assert lib.legacy_folders() == []
    assert lib.legacy_status()["count"] == 0


def test_a_folder_that_stays_locked_is_reported_not_silently_left(tmp_path, monkeypatch):
    folder = tmp_path / "raw"
    folder.mkdir()
    (folder / "big.pdf").write_bytes(b"x")

    def refuse(path, onexc=None):  # every file refused: nothing is deleted
        onexc(None, str(path), PermissionError("in use"))

    monkeypatch.setattr(files.shutil, "rmtree", refuse)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    with pytest.raises(files.StillInUse, match="still in use"):
        files.delete(folder)
    assert (folder / "big.pdf").exists()


def test_a_folder_delete_is_retried_until_the_lock_lets_go(tmp_path, monkeypatch):
    folder = tmp_path / "raw"
    folder.mkdir()
    (folder / "f").write_bytes(b"x")
    real_rmtree, tries = files.shutil.rmtree, []

    def rmtree(path, onexc=None):
        tries.append(path)
        if len(tries) < 3:
            onexc(None, str(path), PermissionError("in use"))  # refused, nothing deleted
        else:
            real_rmtree(path, onexc=onexc)

    monkeypatch.setattr(files.shutil, "rmtree", rmtree)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    files.delete(folder)
    assert len(tries) == 3 and not folder.exists()


def lock_file_path(monkeypatch, locked):
    """Make deleting the file `locked` fail the way Windows does when another program has it open."""
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self == locked:
            raise PermissionError("in use")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(files.time, "sleep", lambda s: None)


def test_a_removal_blocked_by_a_locked_tile_file_leaves_a_retryable_map(lib, monkeypatch):
    lib.add(["L7014A", "L7014B"])
    lib.run_pending()
    locked = lib.tiles_path("L7014A")
    with monkeypatch.context() as m:
        lock_file_path(m, locked)
        with pytest.raises(library.LibraryError, match="still in use; close other programs and try Remove again"):
            lib.remove(["L7014A", "L7014B"])
    assert not lib.tiles_path("L7014B").exists()  # the others were removed
    mine = {m["id"]: m for m in lib.listing()["maps"]}["L7014A"]
    assert mine["state"] == "failed" and "still in use" in mine["error"] and locked.exists()

    # The program lets go: removing again finishes the job.
    assert lib.remove(["L7014A"]) == ["L7014A"]
    assert states(lib)["L7014A"] == "available" and layers(lib) == []


def test_a_removal_blocked_after_the_tiles_went_never_looks_available(lib, monkeypatch):
    lib.add(["L7014A"])
    lib.run_pending()
    pdf = lib.root / "data" / "raw" / "l7014" / "L7014A_geo.pdf"
    lock_file_path(monkeypatch, pdf)
    with pytest.raises(library.LibraryError, match="try Remove again"):
        lib.remove(["L7014A"])
    assert not lib.tiles_path("L7014A").exists() and pdf.exists()
    assert states(lib)["L7014A"] == "failed"  # not "available" with the download still on disk


def test_a_damaged_catalogue_is_named_in_plain_words(lib):
    lib.catalogue_path.write_text('{"series": ', encoding="utf-8")
    with pytest.raises(library.LibraryError) as e:
        lib.catalogue()
    assert "map_library.json" in str(e.value) and "download Map Vietnam again" in str(e.value)


def test_a_map_removed_while_building_with_a_locked_tile_file_ends_retryable(lib, monkeypatch):
    lib.add(["L7014A"])
    locked = lib.tiles_path("L7014A")
    locked.parent.mkdir(parents=True, exist_ok=True)
    locked.write_bytes(b"tiles")

    def removed_meanwhile(job, report):  # remove() asked the worker to stop and delete
        job.remove = True
        lib._jobs.pop("L7014A")  # a stopped build forgets its job

    monkeypatch.setattr(lib, "_run", removed_meanwhile)
    lock_file_path(monkeypatch, locked)
    lib.run_pending()
    mine = {m["id"]: m for m in lib.listing()["maps"]}["L7014A"]
    assert mine["state"] == "failed" and "try Remove again" in mine["error"] and locked.exists()


def test_a_refused_folder_delete_keeps_the_refusal_as_its_cause(tmp_path, monkeypatch):
    folder = tmp_path / "raw"
    folder.mkdir()
    (folder / "f").write_bytes(b"x")
    refusal = PermissionError("in use")
    monkeypatch.setattr(files.shutil, "rmtree", lambda path, onexc=None: onexc(None, str(path), refusal))
    monkeypatch.setattr(files.time, "sleep", lambda s: None)
    with pytest.raises(files.StillInUse) as e:
        files.delete(folder)
    assert e.value.__cause__ is refusal
