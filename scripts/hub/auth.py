"""Scoped client API keys and upstream provider secret vault for the Map Vietnam Hub.

Client keys have the form `mvhub_<8-hex-id>_<48-hex-secret>`. Plaintext secrets are shown once
when minted (or supplied via `HUB_BOOTSTRAP_ADMIN_KEY`) and stored in `hub.sqlite` only as salted
`hashlib.scrypt` digests verified in constant time with `hmac.compare_digest`.

Upstream secrets (USGS ERS M2M, Kaggle, NASA Earthdata, proprietary tile/STAC feeds) come from
Railway Sealed Variables / environment variables or the admin vault table. `scrub_secrets` strips
every known secret value from log lines and error messages before anything is stored or returned.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

KEY_PREFIX = "mvhub_"
KEY_RE = re.compile(r"^mvhub_([0-9a-f]{8})_([0-9a-f]{16,96})$")
VALID_SCOPES = frozenset({"read", "request_build", "upload", "admin"})
DEFAULT_CLIENT_SCOPES = ("read", "request_build", "upload")
ALL_SCOPES = ("read", "request_build", "upload", "admin")

# Scrypt parameters: interactive login-grade hardness (~15 ms per check, resistant to offline cracking).
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_LEN = 32

UPSTREAM_SECRET_KEYS = (
    "USGS_USERNAME",
    "USGS_M2M_TOKEN",
    "KAGGLE_USERNAME",
    "KAGGLE_KEY",
    "EARTHDATA_TOKEN",
    "MAPVIETNAM_CONTACT",
    "PROPRIETARY_SOURCES_JSON",
)


class AuthError(PermissionError):
    """Raised when a request has no valid API key or lacks the required scope."""

    def __init__(self, message, status=401):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Principal:
    """An authenticated caller verified against `api_keys`."""

    key_id: str
    label: str
    scopes: frozenset[str]
    group_tag: str = ""

    def has_scope(self, scope):
        """True when this key holds `scope` (an `admin` key implicitly holds all scopes)."""
        return "admin" in self.scopes or scope in self.scopes

    def require(self, scope):
        """Raise AuthError(403) unless this key holds `scope`."""
        if not self.has_scope(scope):
            raise AuthError(f"This API key does not have '{scope}' permission on this Hub.", status=403)


def utc_now():
    """ISO-8601 UTC timestamp to the second."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def init_auth_tables(db):
    """Create the `api_keys` and `vault_secrets` tables if absent."""
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS api_keys (
            key_id TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            salt BLOB NOT NULL,
            digest BLOB NOT NULL,
            scopes TEXT NOT NULL,
            group_tag TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE IF NOT EXISTS vault_secrets (
            name TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )


def _derive(secret_text, salt):
    """Scrypt digest bytes for `secret_text` and 16-byte `salt`."""
    return hashlib.scrypt(
        secret_text.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_LEN,
    )


def normalize_scopes(scopes):
    """Validate and sort an iterable or comma-separated string of scope names."""
    if isinstance(scopes, str):
        items = [s.strip() for s in scopes.split(",") if s.strip()]
    else:
        items = [str(s).strip() for s in (scopes or []) if str(s).strip()]
    if not items:
        raise ValueError("Specify at least one scope (read, request_build, upload, admin).")
    unknown = set(items) - VALID_SCOPES
    if unknown:
        raise ValueError(f"Unknown scope(s): {', '.join(sorted(unknown))}")
    if "read" not in items:
        items.append("read")
    return sorted(set(items))


def parse_token(token):
    """Return `(key_id, secret_part)` for a well-formed `mvhub_...` token, else `(None, None)`."""
    if not isinstance(token, str):
        return None, None
    m = KEY_RE.fullmatch(token.strip())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def mint_key(db, label, scopes=DEFAULT_CLIENT_SCOPES, group_tag="", *, fixed_token=None):
    """Create (or idempotently update when `fixed_token` is supplied) a Hub API key.

    Returns `{"key_id", "token", "label", "scopes", "group_tag", "created_at"}`.
    """
    clean_label = " ".join(str(label or "Client key").split())[:120] or "Client key"
    clean_scopes = normalize_scopes(scopes)
    clean_group = re.sub(r"[^A-Za-z0-9_.-]", "", str(group_tag or ""))[:64]

    if fixed_token is not None:
        parsed_id, secret_part = parse_token(fixed_token)
        if parsed_id and secret_part:
            key_id = parsed_id
            token = fixed_token.strip()
        else:
            # Allow a human-chosen bootstrap secret in HUB_BOOTSTRAP_ADMIN_KEY by deriving a deterministic id
            # while still hashing the full token secret with scrypt.
            raw = str(fixed_token).strip()
            if len(raw) < 16:
                raise ValueError("Bootstrap admin key must be at least 16 characters.")
            key_id = hashlib.sha256(f"mvhub-id:{raw}".encode()).hexdigest()[:8]
            secret_part = raw
            token = raw
    else:
        key_id = secrets.token_hex(4)
        secret_part = secrets.token_hex(24)
        token = f"{KEY_PREFIX}{key_id}_{secret_part}"

    salt = secrets.token_bytes(16)
    digest = _derive(secret_part, salt)
    now = utc_now()
    with db:
        db.execute(
            """
            INSERT INTO api_keys (key_id, label, salt, digest, scopes, group_tag, created_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(key_id) DO UPDATE SET
                label = excluded.label,
                salt = excluded.salt,
                digest = excluded.digest,
                scopes = excluded.scopes,
                group_tag = excluded.group_tag,
                revoked_at = NULL
            """,
            (key_id, clean_label, salt, digest, ",".join(clean_scopes), clean_group, now),
        )
    return {
        "key_id": key_id,
        "token": token,
        "label": clean_label,
        "scopes": clean_scopes,
        "group_tag": clean_group,
        "created_at": now,
    }


def ensure_bootstrap_admin(db, env=None):
    """If `HUB_BOOTSTRAP_ADMIN_KEY` is set in `env` (or `os.environ`), register it as an admin key."""
    env = os.environ if env is None else env
    raw = (env.get("HUB_BOOTSTRAP_ADMIN_KEY") or "").strip()
    if not raw:
        return None
    return mint_key(db, "Bootstrap Admin", scopes=ALL_SCOPES, fixed_token=raw)


def authenticate(db, auth_header):
    """Verify an `Authorization: Bearer <token>` header and return a `Principal`, or raise AuthError."""
    if not auth_header or not isinstance(auth_header, str):
        raise AuthError("Missing Authorization Bearer token.", status=401)
    parts = auth_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise AuthError("Use Authorization: Bearer <hub_api_key>.", status=401)
    raw_token = parts[1].strip()
    key_id, secret_part = parse_token(raw_token)
    if not key_id:
        # Support custom bootstrap secret hashed via `mvhub-id:<raw>`.
        key_id = hashlib.sha256(f"mvhub-id:{raw_token}".encode()).hexdigest()[:8]
        secret_part = raw_token

    row = db.execute(
        "SELECT key_id, label, salt, digest, scopes, group_tag, revoked_at FROM api_keys WHERE key_id = ?",
        (key_id,),
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        raise AuthError("Invalid or revoked Hub API key.", status=401)
    candidate = _derive(secret_part, bytes(row["salt"]))
    if not hmac.compare_digest(candidate, bytes(row["digest"])):
        raise AuthError("Invalid or revoked Hub API key.", status=401)
    scopes = frozenset(s for s in row["scopes"].split(",") if s)
    return Principal(key_id=row["key_id"], label=row["label"], scopes=scopes, group_tag=row["group_tag"] or "")


def list_keys(db):
    """Metadata of all keys (never the secret or digest)."""
    rows = db.execute(
        "SELECT key_id, label, scopes, group_tag, created_at, revoked_at FROM api_keys ORDER BY created_at DESC"
    ).fetchall()
    return [
        {
            "key_id": r["key_id"],
            "label": r["label"],
            "scopes": [s for s in r["scopes"].split(",") if s],
            "group_tag": r["group_tag"],
            "created_at": r["created_at"],
            "active": r["revoked_at"] is None,
            "revoked_at": r["revoked_at"],
        }
        for r in rows
    ]


def revoke_key(db, key_id):
    """Revoke `key_id`; returns True when an active key was revoked."""
    with db:
        cur = db.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
            (utc_now(), str(key_id)),
        )
    return cur.rowcount > 0


class UpstreamVault:
    """Server-only store for upstream provider credentials.

    Environment variables (e.g. Railway Sealed Variables) take precedence over values saved via the
    admin endpoint in `vault_secrets`, matching `.env` conventions elsewhere in Map Vietnam.
    """

    def __init__(self, db_factory, env=None):
        self._db_factory = db_factory
        self._env = os.environ if env is None else env

    def get(self, name):
        """Return secret `name`, or `""` if not configured."""
        env_val = (self._env.get(name) or "").strip()
        if env_val:
            return env_val
        with self._db_factory() as db:
            row = db.execute("SELECT value FROM vault_secrets WHERE name = ?", (name,)).fetchone()
            return (row["value"] if row else "").strip()

    def set_many(self, updates):
        """Set or delete (`None` / `""`) allowed upstream secret names in `vault_secrets`."""
        unknown = set(updates) - set(UPSTREAM_SECRET_KEYS)
        if unknown:
            raise ValueError(f"Unsupported upstream secret key(s): {', '.join(sorted(unknown))}")
        now = utc_now()
        with self._db_factory() as db, db:
            for name, val in updates.items():
                clean = "" if val is None else str(val).strip()
                if clean:
                    db.execute(
                        """
                        INSERT INTO vault_secrets (name, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (name, clean, now),
                    )
                else:
                    db.execute("DELETE FROM vault_secrets WHERE name = ?", (name,))

    def status(self):
        """Non-sensitive boolean capability summary safe to show authenticated clients."""
        proprietary = self.proprietary_sources()
        return {
            "usgs_m2m": bool(self.get("USGS_USERNAME") and self.get("USGS_M2M_TOKEN")),
            "kaggle": bool(self.get("KAGGLE_USERNAME") and self.get("KAGGLE_KEY")),
            "earthdata": bool(self.get("EARTHDATA_TOKEN")),
            "contact_configured": bool(self.get("MAPVIETNAM_CONTACT")),
            "proprietary_count": len(proprietary),
        }

    def proprietary_sources(self):
        """Parsed list of operator-configured proprietary tile/DEM sources (without leaking URLs/keys).

        `PROPRIETARY_SOURCES_JSON` is a JSON array of objects:
        `[{"id": "planet_2024", "title": "Planet Basemap", "tiles": "https://.../{z}/{x}/{y}.png?api_key=...",
           "encoding": null | "terrarium", "format": "png" | "webp", "maxzoom": 18, "bounds": [102,8,110,24],
           "attribution": "Planet Labs", "stream_only": true}]`.
        """
        raw = self.get("PROPRIETARY_SOURCES_JSON")
        if not raw:
            return []
        try:
            items = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(items, list):
            return []
        valid = []
        for item in items:
            if isinstance(item, dict) and item.get("id") and item.get("tiles"):
                valid.append(item)
        return valid

    def scrub(self, text):
        """Replace any configured secret values in `text` with `[REDACTED]`."""
        out = str(text or "")
        if not out:
            return out
        sensitive = []
        for k in ("USGS_M2M_TOKEN", "KAGGLE_KEY", "EARTHDATA_TOKEN", "HUB_BOOTSTRAP_ADMIN_KEY"):
            val = self.get(k) if k != "HUB_BOOTSTRAP_ADMIN_KEY" else (self._env.get(k) or "").strip()
            if val and len(val) >= 4:
                sensitive.append(val)
        for src in self.proprietary_sources():
            url = str(src.get("tiles") or "")
            if "?" in url:
                sensitive.append(url.split("?", 1)[1])
        for secret in sorted(set(sensitive), key=len, reverse=True):
            out = out.replace(secret, "[REDACTED]")
        return re.sub(r"mvhub_[0-9a-f]{8}_[0-9a-f]{16,96}", "mvhub_[REDACTED]", out)
