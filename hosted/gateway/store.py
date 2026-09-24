"""control.db -- tenants, sessions and proxy tokens. The gateway's alone.

On the VM, control/ is owned by UID 10002 with mode 0700, so the one-writer
rule is enforced by file ownership rather than by convention. The proxy never
opens this file: it asks over run/gateway/gateway.sock (internal.py) and gets
back a tenant id and a status, and nothing else.

Neither a cookie value nor a token plaintext is stored. A stolen copy of this
file is a list of hashes, not a set of live credentials.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from hosted.core.tenant import (
    STATUSES,
    is_tenant_id,
    new_proxy_token,
    new_tenant_id,
    next_project_id,
    normalise_timezone,
    token_hash,
)
from hosted.ports.control import Tenant

SESSION_TTL_SECONDS = 30 * 24 * 3600

PRAGMAS = (
    # WAL: the gateway reads sessions on every request and writes rarely, and
    # a reader must never block on a writer.
    "PRAGMA journal_mode=WAL",
    # FULL, not NORMAL. In WAL mode NORMAL can lose the last committed
    # transactions on power loss, and the transactions this file carries are
    # a token revocation and a tenant disable -- writes whose entire purpose
    # is that they hold. They are also rare, so FULL costs nothing here.
    "PRAGMA synchronous=FULL",
    # backup.sh opens this file from a second process (spec, F3).
    "PRAGMA busy_timeout=5000",
)
# No foreign_keys pragma: neither table declares a foreign key, so turning it
# on would enforce nothing while implying a constraint that is not there.

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant (
  id          TEXT PRIMARY KEY,
  sub         TEXT NOT NULL UNIQUE,
  email       TEXT NOT NULL,
  timezone    TEXT NOT NULL DEFAULT 'UTC',
  status      TEXT NOT NULL DEFAULT 'active',
  project_id  INTEGER NOT NULL UNIQUE,
  created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS session (
  hash        TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS session_by_tenant ON session(tenant_id);
CREATE TABLE IF NOT EXISTS proxy_token (
  hash        TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  issued_at   REAL NOT NULL,
  revoked_at  REAL
);
CREATE INDEX IF NOT EXISTS token_by_tenant ON proxy_token(tenant_id);
"""

_COLUMNS = "id, sub, email, timezone, status, project_id, created_at"


class ControlDb:
    """The MVP ControlStore. Synchronous: every call here is microseconds on a
    local file, and the gateway's event loop is not worth an executor for it.

    ONE CONNECTION, BEHIND ONE LOCK. check_same_thread=False is what lets the
    connection outlive the thread that made it, which a single-threaded event
    loop does not need -- until E1 puts one of these calls in an executor to
    keep a slow disk off the loop, and then two threads share a connection
    that sqlite3 does not serialise. The lock costs nothing today and removes
    the class of bug that would otherwise arrive with the first executor.

    Every public method holds the lock for the whole of its work, statement
    and commit together, so no other thread can see or interleave with a
    half-finished change. The lock is a plain Lock rather than an RLock, so
    the two methods that need a second method's SQL call the _locked helper
    beside it instead of re-entering the public one -- which also makes
    issue_token's revoke-then-insert a single critical section, the thing that
    makes "exactly one live token per tenant" true under concurrency.
    """

    def __init__(self, path: Path, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            for pragma in PRAGMAS:
                self._conn.execute(pragma)
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def journal_mode(self) -> str:
        with self._lock:
            return self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()

    @staticmethod
    def _tenant(row: sqlite3.Row | None) -> Tenant | None:
        return Tenant(*row) if row is not None else None

    def create_tenant(self, *, sub: str, email: str, timezone: str) -> Tenant:
        with self._lock:
            used = [r[0] for r in self._conn.execute("SELECT project_id FROM tenant")]
            record = Tenant(id=new_tenant_id(), sub=sub, email=email,
                            timezone=normalise_timezone(timezone), status="active",
                            project_id=next_project_id(used), created_at=self._now())
            self._conn.execute(
                f"INSERT INTO tenant ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record.id, record.sub, record.email, record.timezone, record.status,
                 record.project_id, record.created_at))
            self._conn.commit()
            return record

    def tenant_by_sub(self, sub: str) -> Tenant | None:
        with self._lock:
            return self._tenant(self._conn.execute(
                f"SELECT {_COLUMNS} FROM tenant WHERE sub = ?", (sub,)).fetchone())

    def tenant_by_id(self, tenant_id: str) -> Tenant | None:
        with self._lock:
            return self._tenant(self._conn.execute(
                f"SELECT {_COLUMNS} FROM tenant WHERE id = ?", (tenant_id,)).fetchone())

    def tenant_by_email(self, email: str) -> Tenant | None:
        """tenant.sh names a tenant by email. Two Supabase users cannot share
        one, so this is a lookup and not a search."""
        with self._lock:
            return self._tenant(self._conn.execute(
                f"SELECT {_COLUMNS} FROM tenant WHERE email = ?", (email,)).fetchone())

    def set_status(self, tenant_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}, not {status!r}")
        with self._lock:
            self._conn.execute("UPDATE tenant SET status = ? WHERE id = ?",
                               (status, tenant_id))
            self._conn.commit()

    def set_timezone(self, tenant_id: str, timezone: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE tenant SET timezone = ? WHERE id = ?",
                               (normalise_timezone(timezone), tenant_id))
            self._conn.commit()

    def create_session(self, *, tenant_id: str, value: str, expires_at: float) -> None:
        if not is_tenant_id(tenant_id):
            raise ValueError(f"not a tenant id: {tenant_id!r}")
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO session (hash, tenant_id, expires_at) "
                "VALUES (?, ?, ?)",
                (token_hash(value), tenant_id, expires_at))
            self._conn.commit()

    def session_tenant(self, value: str, now: float) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT tenant_id FROM session WHERE hash = ? AND expires_at > ?",
                (token_hash(value), now)).fetchone()
        return row[0] if row else None

    def delete_sessions(self, tenant_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM session WHERE tenant_id = ?", (tenant_id,))
            self._conn.commit()

    def issue_token(self, tenant_id: str) -> str:
        """Issuing revokes the tenant's previous token, so exactly one is live
        and it belongs to the container the gateway is about to start."""
        token = new_proxy_token()
        with self._lock:
            self._revoke_tokens_locked(tenant_id)
            self._conn.execute(
                "INSERT INTO proxy_token (hash, tenant_id, issued_at) VALUES (?, ?, ?)",
                (token_hash(token), tenant_id, self._now()))
            self._conn.commit()
        return token

    def revoke_tokens(self, tenant_id: str) -> None:
        with self._lock:
            self._revoke_tokens_locked(tenant_id)
            self._conn.commit()

    def _revoke_tokens_locked(self, tenant_id: str) -> None:
        """The revoke half of issue_token, so the two are one transaction."""
        self._conn.execute(
            "UPDATE proxy_token SET revoked_at = ? WHERE tenant_id = ? AND revoked_at IS NULL",
            (self._now(), tenant_id))

    def tenant_for_token_hash(self, digest: str) -> tuple[str, str] | None:
        """(tenant id, status) for a live token. The proxy's only question."""
        with self._lock:
            row = self._conn.execute(
                "SELECT t.id, t.status FROM proxy_token p JOIN tenant t ON t.id = p.tenant_id "
                "WHERE p.hash = ? AND p.revoked_at IS NULL", (digest,)).fetchone()
        return (row[0], row[1]) if row else None

    def delete_tenant(self, tenant_id: str) -> None:
        """The row goes; the archive keeps the files for 30 days. A tenant id
        is never reissued, because its project id is never reissued either."""
        with self._lock:
            self._conn.execute("DELETE FROM session WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute("DELETE FROM proxy_token WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute("DELETE FROM tenant WHERE id = ?", (tenant_id,))
            self._conn.commit()
