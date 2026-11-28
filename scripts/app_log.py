"""Secret-scrubbed rotating error logging for Map Vietnam desktop & Hub servers."""

import os
import re
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

_TOKEN_PATTERNS = (
    (re.compile(r"mvhub_[0-9a-f]{8}_[0-9a-f]{16,96}", re.I), "mvhub_[REDACTED]"),
    (re.compile(r"(Authorization:\s*Bearer\s+)[^\s,;\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"([?&](?:api_key|apikey|key|token|secret|password|auth)=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
    (re.compile(r"(?<![A-Za-z0-9_])((?:api_key|apikey|token|secret|password|auth)=)[^&\s\"']+", re.I), r"\1[REDACTED]"),
)


def scrub_secrets(text, extra_secrets=()):
    """Redact API keys, Bearer tokens, and secret query parameters from text before logging."""
    out = str(text or "")
    if not out:
        return out
    for secret in extra_secrets:
        s = str(secret or "")
        if len(s) >= 4:
            out = out.replace(s, "[REDACTED]")
    for pat, repl in _TOKEN_PATTERNS:
        out = pat.sub(repl, out)
    return out


class AppLogger:
    """Thread-safe rotating text log file (`0600` permissions) for diagnostics & troubleshooting."""

    def __init__(self, log_path, *, max_bytes=1 << 20, backups=2, echo_stderr=False):
        self.path = Path(log_path)
        self.max_bytes = max(4096, int(max_bytes))
        self.backups = max(1, int(backups))
        self.echo_stderr = bool(echo_stderr)
        self._lock = threading.Lock()

    def _rotate_if_needed(self):
        try:
            if not self.path.exists() or self.path.stat().st_size < self.max_bytes:
                return
            oldest = self.path.with_name(f"{self.path.name}.{self.backups}")
            oldest.unlink(missing_ok=True)
            for idx in range(self.backups - 1, 0, -1):
                src = self.path.with_name(f"{self.path.name}.{idx}")
                if src.exists():
                    src.replace(self.path.with_name(f"{self.path.name}.{idx + 1}"))
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))
        except OSError:
            pass

    def log(self, level, source, message, *, exc=None, secrets=()):
        """Append a scrubbed timestamped entry (and optional exception traceback) to `self.path`."""
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        clean_src = scrub_secrets(str(source or "app").replace("\n", " ")[:120], secrets)
        clean_msg = scrub_secrets(str(message or "").rstrip(), secrets)
        entry = f"{ts} [{str(level or 'ERROR').upper()}] [{clean_src}] {clean_msg}\n"
        if exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            entry += scrub_secrets(tb, secrets).rstrip() + "\n"

        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate_if_needed()
                fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(entry)
                if os.name == "posix":
                    try:
                        os.chmod(self.path, 0o600)
                    except OSError:
                        pass
            except OSError:
                pass
            if self.echo_stderr:
                try:
                    sys.stderr.write(entry)
                    sys.stderr.flush()
                except OSError:
                    pass

    def tail(self, lines=120):
        """Return up to the last `lines` log lines (oldest-to-newest) or `[]`."""
        n = max(1, min(1000, int(lines)))
        with self._lock:
            try:
                if not self.path.is_file():
                    return []
                raw = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
                return raw[-n:]
            except OSError:
                return []

    def clear(self):
        """Clear the current log file and backups."""
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
                for idx in range(1, self.backups + 1):
                    self.path.with_name(f"{self.path.name}.{idx}").unlink(missing_ok=True)
            except OSError:
                pass
