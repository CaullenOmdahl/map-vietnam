"""Every runnable script answers --help on its own: it prints usage and exits 0 without downloading or writing.

Each script is started as a separate process the way a user would start it (no PYTHONPATH, from another
directory), so this also catches a script whose imports only work under start.py or pytest's import path.

Run:  .venv/bin/python -m pytest -q tests/test_cli.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = [
    "scripts/build_geotiff.py",
    "scripts/build_tiles.py",
    "scripts/catalog.py",
    "scripts/compare_overlay.py",
    "scripts/desktop.py",
    "scripts/download_maps.py",
    "scripts/georef_scan.py",
    "scripts/hub/hub_server.py",
    "scripts/l7014_meta.py",
    "scripts/library.py",
    "scripts/map_library_catalogue.py",
    "scripts/mcp_server.py",
    "scripts/place_sheets.py",
    "scripts/records.py",
    "scripts/server.py",
    "scripts/sheet_lists.py",
    "scripts/ingest/caccf.py",
    "scripts/ingest/datum_check.py",
    "scripts/ingest/gns.py",
    "scripts/ingest/hes_gazetteer.py",
    "scripts/ingest/thor.py",
    "scripts/ingest/vhpa.py",
    "scripts/ingest/vn_relics.py",
    "scripts/ingest/wiki_mia.py",
    "scripts/ingest/wikidata_osm.py",
]


def test_every_script_is_listed():
    found = {
        str(p.relative_to(ROOT))
        for p in (ROOT / "scripts").rglob("*.py")
        if 'if __name__ == "__main__"' in p.read_text(encoding="utf-8")
    }
    assert found == set(SCRIPTS)


@pytest.mark.parametrize("script", SCRIPTS)
def test_help_prints_usage_and_does_nothing_else(script, tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("usage: ")
    assert not list(tmp_path.iterdir())
