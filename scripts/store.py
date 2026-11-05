"""The evidence store: one SQLite file holding normalised records from every source.

Ingesters (scripts/ingest/) build Evidence records and write them with upsert_source plus
sync_source or upsert_many; scripts/records.py takes a source out again with remove_source; the
server reads them and keeps cases (pinned records, candidate sites, notes) in the same file. Default location: data/evidence.sqlite. Import it; there is
nothing to run.

Rules the schema and the Evidence class enforce (the four rules at the top of docs/how-it-works.md):
- every point keeps its coordinate exactly as the source gave it, plus the datum;
- every point has an uncertainty radius > 0, so precision is never overstated;
- known formal burials are not a kind of evidence and cannot be stored.

Coordinates (lon, lat) are WGS84 degrees; uncertainty radii are metres; dates are ISO strings of
year, month or day precision; timestamps are UTC ISO 8601.
"""

import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "evidence.sqlite"

KINDS = (
    "loss_incident",  # person killed or missing at a place
    "aircraft_crash",
    "air_strike",
    "ground_combat",
    "field_burial",  # reported hasty/temporary/unmarked burial, never a formal grave
    "placename",  # hamlet/feature name with a position, for placing old reports
    "military_site",  # base, outpost, firebase, airstrip
    "other",
)
# A date as precise as the source gives it, and no more.
DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")

SCHEMA = """
create table if not exists sources (
  id text primary key, name text not null, url text, licence text, notes text, retrieved_at text
);
create table if not exists evidence (
  id integer primary key,
  source text not null references sources(id),
  source_record_id text not null,
  kind text not null,
  date_start text, date_end text,
  lon real not null, lat real not null, uncertainty_m real not null,
  original_coord text not null, original_datum text not null,
  title text not null, description text, url text, raw_json text,
  withdrawn_at text, withdrawn_reason text,
  unique (source, source_record_id)
);
create index if not exists evidence_lonlat on evidence(lon, lat);
create table if not exists cases (
  id integer primary key, name text not null unique, summary text, created_at text not null
);
create table if not exists case_items (
  case_id integer not null references cases(id) on delete cascade,
  evidence_id integer not null references evidence(id) on delete cascade,
  role text default 'evidence', note text,
  primary key (case_id, evidence_id)
);
create table if not exists sites (
  id integer primary key, case_id integer references cases(id) on delete cascade,
  name text not null, lon real not null, lat real not null, uncertainty_m real not null,
  rationale text, created_at text not null
);
create table if not exists media (
  id integer primary key, case_id integer references cases(id) on delete set null,
  path text not null, kind text not null, taken text,
  lon real, lat real, uncertainty_m real, caption text, created_at text not null
);
create table if not exists notes (
  id integer primary key, case_id integer references cases(id) on delete cascade,
  body text not null, created_at text not null
);
"""


@dataclass
class Evidence:
    """One record from one source, validated on construction (ValueError if it breaks a rule)."""

    source: str
    source_record_id: str
    kind: str
    lon: float
    lat: float
    uncertainty_m: float
    original_coord: str
    original_datum: str
    title: str
    date_start: str | None = None
    date_end: str | None = None
    description: str | None = None
    url: str | None = None
    raw: dict = field(default_factory=dict)  # the source's own fields, kept for audit as raw_json

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"kind {self.kind!r} not allowed (known burials are out of scope)")
        if not self.uncertainty_m or self.uncertainty_m <= 0:
            raise ValueError("uncertainty_m must be > 0")
        if not self.original_coord or not self.original_datum:
            raise ValueError("original_coord and original_datum are required")
        if not (-180 <= self.lon <= 180 and -90 <= self.lat <= 90):
            raise ValueError(f"bad coordinate {self.lon}, {self.lat}")
        for d in (self.date_start, self.date_end):
            if d and not DATE_RE.match(d):
                raise ValueError(f"date {d!r} must be YYYY, YYYY-MM or YYYY-MM-DD")


def now():
    """Current UTC time as an ISO 8601 string to the second, the store's timestamp format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    """Open (creating if needed) the store at `path`, a Path, and bring its schema up to date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60)  # ingesters may write concurrently
    db.execute("pragma foreign_keys = on")
    db.executescript(SCHEMA)
    # Stores made before records could be withdrawn lack these columns; add them in place.
    cols = {r[1] for r in db.execute("pragma table_info(evidence)")}
    for col in ("withdrawn_at", "withdrawn_reason"):
        if col not in cols:
            db.execute(f"alter table evidence add column {col} text")
    return db


def upsert_source(db, source_id, *, name, url=None, licence=None, notes=None):
    """Create or update a source's description and stamp it as retrieved now. Commits."""
    db.execute(
        "insert into sources (id, name, url, licence, notes, retrieved_at) values (?,?,?,?,?,?) "
        "on conflict(id) do update set name=excluded.name, url=excluded.url, licence=excluded.licence, "
        "notes=excluded.notes, retrieved_at=excluded.retrieved_at",
        (source_id, name, url, licence, notes, now()),
    )
    db.commit()


def upsert_evidence(db, e: Evidence):
    """Insert `e`, or replace the record with the same (source, source_record_id). Does not commit.

    Raises ValueError if the source has not been registered with upsert_source.
    """
    if not db.execute("select 1 from sources where id = ?", (e.source,)).fetchone():
        raise ValueError(f"unknown source {e.source!r}: call upsert_source first")
    row = asdict(e)
    row["raw_json"] = json.dumps(row.pop("raw"), ensure_ascii=False, default=str)
    row["withdrawn_at"] = row["withdrawn_reason"] = None  # a record the source lists again is current again
    cols = list(row)
    db.execute(
        f"insert into evidence ({','.join(cols)}) values ({','.join('?' * len(cols))}) "
        f"on conflict(source, source_record_id) do update set "
        + ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("source", "source_record_id")),
        [row[c] for c in cols],
    )


def upsert_many(db, records):
    """Upsert every record (additions only: nothing is deleted), commit, and return the count."""
    n = 0
    for e in records:
        upsert_evidence(db, e)
        n += 1
    db.commit()
    return n


def sync_source(db, source, records, reason="no longer in the source"):
    """Make the store hold exactly `records` for `source`, without breaking anyone's case.

    Records the source no longer gives are deleted, unless an analyst has put them in a case: those
    are kept but marked withdrawn (hidden from the map and search, still shown in the case).
    Returns (kept, deleted, withdrawn)."""
    kept = set()
    for e in records:
        upsert_evidence(db, e)
        kept.add(e.source_record_id)
    stale = [
        (i, rid)
        for i, rid in db.execute(
            "select id, source_record_id from evidence where source = ? and withdrawn_at is null", (source,)
        )
        if rid not in kept
    ]
    in_case = {i for (i,) in db.execute("select distinct evidence_id from case_items")}
    gone = [i for i, _ in stale if i not in in_case]
    held = [i for i, _ in stale if i in in_case]
    db.executemany("delete from evidence where id = ?", [(i,) for i in gone])
    db.executemany(
        "update evidence set withdrawn_at = ?, withdrawn_reason = ? where id = ?", [(now(), reason, i) for i in held]
    )
    db.commit()
    return len(kept), len(gone), len(held)


def remove_source(db, source, reason="its source was removed"):
    """Take a whole source out of the store, without breaking anyone's case.

    Its records go the way sync_source removes them: deleted, unless an analyst has put them in a
    case, in which case they are kept and marked withdrawn. The source's own entry goes too, unless
    such records still refer to it. Returns (deleted, withdrawn)."""
    _, deleted, withdrawn = sync_source(db, source, [], reason)
    if not db.execute("select 1 from evidence where source = ? limit 1", (source,)).fetchone():
        db.execute("delete from sources where id = ?", (source,))
        db.commit()
    return deleted, withdrawn
