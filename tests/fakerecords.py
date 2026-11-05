"""Fake record sources for the tests: a project folder in a temporary directory whose "ingesters"
are one small script that does what a plan file tells it, so a test can make a run wait, fail,
crash, ask for a key or say something, and can read back what ran, in order and with which arguments.

No test touches the network or anything outside its temporary folder: the fake writes its
records to the store under that folder, and the Kaggle key is a made-up one.
"""

import json
import os
import time
from pathlib import Path

from records import Source

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
# A user name and key that exist nowhere else, so finding either in an answer or a log is a leak.
USER, KEY = "test-user-9f3a", "0123456789abcdef0123456789abcdef"

# What every fake source runs: it does what plan.json says for its name, so a test can make a
# run wait, fail, crash or print something, and ran.log says what ran, in order, with what.
FAKE_INGESTER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {scripts!r})
import store

root = Path(__file__).parent
name, extra = sys.argv[1], sys.argv[2:]
plan = json.loads((root / "plan.json").read_text()).get(name, {{}})
with open(root / "ran.log", "a") as f:
    f.write(" ".join([name, *extra]) + "\\n")
print("progress 0.25 Downloading 1.0 MB of 4.0 MB", flush=True)
for line in plan.get("says", []):
    print(line, flush=True)
if plan.get("wait"):
    (root / (name + ".started")).touch()
    while not (root / (name + ".go")).exists():
        time.sleep(0.02)
if plan.get("fail"):
    sys.exit(plan["fail"])
if plan.get("wants_key"):  # what thor.py does when Kaggle asks for a key: one sentence, status 3
    print(plan["wants_key"], file=sys.stderr, flush=True)
    sys.exit(3)
if plan.get("crash"):
    raise RuntimeError(plan["crash"])
folder = root / "data" / "raw" / name
folder.mkdir(parents=True, exist_ok=True)
(folder / "download.bin").write_bytes(b"x" * 1000)
db = store.connect(root / "data" / "evidence.sqlite")
store.upsert_source(db, name, name=name)
store.sync_source(db, name, [
    store.Evidence(source=name, source_record_id=str(i), kind="other", lon=105.5 + i / 100, lat=10.5,
                   uncertainty_m=100, original_coord="x", original_datum="wgs84", title=name + " record " + str(i))
    for i in range(plan.get("records", 2))
])
for path in plan.get("makes", []):
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    (root / path).write_text("{{}}")
print("progress - Saving the records", flush=True)
"""


def source(sid, **kw):
    """A fake source: its ingester is the shared fake, told its name."""
    kw.setdefault("store_ids", (sid,))
    kw.setdefault("raw_folders", (sid,))
    return Source(
        sid, f"Source {sid}", f"About {sid}.", 1, "a moment", "test", script="fake_ingest.py", args=(sid,), **kw
    )


def fake_sources():
    """base <- child <- grandchild; an opt-in one; a slow one that steps aside; the air strikes."""
    return (
        source("base", produces=("data/work/base_index.json",), work_files=("data/work/base_summary.json",)),
        source("child", needs=("base",), reads_raw=("base",)),
        source("grandchild", needs=("child",)),
        source("check", needs=("base",), store_ids=(), raw_folders=(), listed=False),
        source("optional", opt_in=True, standard=False, warning="This one is slow and comes from a copyrighted site."),
        source("thor", uses_key=True, standard=False, refresh_args=()),
        source("slow", yields=True, refresh_args=("--refresh-index",)),
    )


def make_home(root, monkeypatch):
    """Make `root` a project folder with the fake ingester and its plan, and take every Kaggle
    setting out of the environment (kaggle.json is looked for under root/kaggle). Returns root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "fake_ingest.py").write_text(FAKE_INGESTER.format(scripts=str(SCRIPTS)))
    set_plan(root, base={"makes": ["data/work/base_index.json", "data/work/base_summary.json"]})
    env = {k: v for k, v in os.environ.items() if not k.startswith("KAGGLE_")}
    monkeypatch.setattr(os, "environ", {**env, "KAGGLE_CONFIG_DIR": str(root / "kaggle")})
    return root


def set_plan(root, **plans):
    """Merge per-source instructions for the fake ingester into plan.json."""
    path = root / "plan.json"
    plan = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps({**plan, **plans}))


def ran(root):
    """What the fake ingesters ran, in order: one "name arg ..." line each."""
    log = root / "ran.log"
    return log.read_text().splitlines() if log.exists() else []


def wait_for(condition, timeout=20):
    """Poll until condition() is true; fail the test if it never is."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting")
