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
CREATE TABLE IF NOT EXISTS retired_project_id (
  project_id  INTEGER PRIMARY KEY,
  retired_at  REAL NOT NULL
);
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
    the one method that needs a second method's SQL -- issue_token, which must
    revoke before it inserts -- calls the _locked helper beside it instead of
    re-entering the public one. That also makes revoke-then-insert a single
    critical section, which is what makes "exactly one live token per tenant"
    true under concurrency rather than only in a single thread.

    Methods that change a row require a well-formed tenant id; lookups do not,
    and answer None. The guard is on format only: an id that is well formed and
    belongs to nobody still affects no rows, and whether a tenant exists is the
    caller's question, asked with tenant_by_id.
    """

    def __init__(self, path: Path, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        # timeout=0, so PRAGMA busy_timeout is the ONLY place the timeout is
        # set. sqlite3.connect's own `timeout` parameter sets a busy timeout
        # too, and its default of 5.0 seconds is the same 5000 ms -- two
        # sources agreeing by coincidence, which meant deleting the pragma
        # changed nothing and no readback could tell that it had gone.
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=0)
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

    def synchronous(self) -> int:
        """The value in effect, not the text that was sent. A typo such as
        `synchronous=FUL` does not error: SQLite lands on NORMAL while the
        source still reads as a durability declaration, and only a readback
        sees the difference between declared and applied."""
        with self._lock:
            return int(self._conn.execute("PRAGMA synchronous").fetchone()[0])

    def busy_timeout(self) -> int:
        with self._lock:
            return int(self._conn.execute("PRAGMA busy_timeout").fetchone()[0])

    @staticmethod
    def _tenant(row: sqlite3.Row | None) -> Tenant | None:
        return Tenant(*row) if row is not None else None

    @staticmethod
    def _require_tenant_id(tenant_id: str) -> None:
        """Every method that changes a row takes this. A mangled id would
        otherwise update nothing and report success, and `tenant.sh disable`
        on a truncated id would tell an operator it had disabled somebody."""
        if not is_tenant_id(tenant_id):
            raise ValueError(f"not a tenant id: {tenant_id!r}")

    def create_tenant(self, *, sub: str, email: str, timezone: str) -> Tenant:
        """A new tenant, with a project id no tenant has ever held.

        `used` is the live rows AND the retired ones. next_project_id is
        monotonic given a monotonic `used`, and passing it only the live rows
        made it hand a deleted tenant's id straight back to the next arrival.
        Two things then break, and both are stated as guarantees elsewhere: a
        directory keeps its XFS project id, so `tenant.sh delete`'s archive --
        kept thirty days -- would count against whoever inherited the id, and a
        new tenant could be over quota on the day they signed up; and
        address_for_project derives the bridge address from the project id, so
        a stale entry for the deleted tenant would point at a live different
        tenant's dashboard, which has no authentication of its own.

        Raises ValueError when the range is exhausted, which is
        next_project_id's own refusal: about 65,000 tenants over the life of
        one VM, counting deletions. Refusing a signup is the only safe answer,
        because the alternative is reuse and reuse is what this prevents.
        """
        with self._lock:
            used = [r[0] for r in self._conn.execute(
                "SELECT project_id FROM tenant "
                "UNION ALL SELECT project_id FROM retired_project_id")]
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
        """The earliest-created tenant with this address, or None.

        EMAIL IS NOT UNIQUE. Only sub and project_id are, in the schema and in
        the spec, and nothing here stops two tenant rows sharing an address: an
        anonymous account later upgraded, an address reused after a delete, a
        second Supabase identity. So this is a search that returns one row, not
        a lookup, and the row it returns is pinned by created_at and then by id
        so that the same address always answers the same tenant.

        `tenant.sh disable <email>` and `tenant.sh delete <email>` are operator
        commands built on this. Disabling or archiving whichever row SQLite
        reached first is the kind of mistake found afterwards, so the order is
        stated here and tested. An operator who needs certainty should name the
        tenant id.
        """
        with self._lock:
            return self._tenant(self._conn.execute(
                f"SELECT {_COLUMNS} FROM tenant WHERE email = ? "
                "ORDER BY created_at, id LIMIT 1", (email,)).fetchone())

    def set_status(self, tenant_id: str, status: str) -> None:
        self._require_tenant_id(tenant_id)
        if status not in STATUSES:
            raise ValueError(f"status must be one of {sorted(STATUSES)}, not {status!r}")
        with self._lock:
            self._conn.execute("UPDATE tenant SET status = ? WHERE id = ?",
                               (status, tenant_id))
            self._conn.commit()

    def set_timezone(self, tenant_id: str, timezone: str) -> None:
        self._require_tenant_id(tenant_id)
        with self._lock:
            self._conn.execute("UPDATE tenant SET timezone = ? WHERE id = ?",
                               (normalise_timezone(timezone), tenant_id))
            self._conn.commit()

    def create_session(self, *, tenant_id: str, value: str, expires_at: float) -> None:
        self._require_tenant_id(tenant_id)
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
        self._require_tenant_id(tenant_id)
        with self._lock:
            self._conn.execute("DELETE FROM session WHERE tenant_id = ?", (tenant_id,))
            self._conn.commit()

    def issue_token(self, tenant_id: str) -> str:
        """Issuing revokes the tenant's previous token, so exactly one is live
        and it belongs to the container the gateway is about to start."""
        self._require_tenant_id(tenant_id)
        token = new_proxy_token()
        with self._lock:
            self._revoke_tokens_locked(tenant_id)
            self._conn.execute(
                "INSERT INTO proxy_token (hash, tenant_id, issued_at) VALUES (?, ?, ?)",
                (token_hash(token), tenant_id, self._now()))
            self._conn.commit()
        return token

    def revoke_tokens(self, tenant_id: str) -> None:
        self._require_tenant_id(tenant_id)
        with self._lock:
            self._revoke_tokens_locked(tenant_id)
            self._conn.commit()

    def _revoke_tokens_locked(self, tenant_id: str) -> None:
        """The revoke half of issue_token, so the two are one transaction."""
        self._conn.execute(
            "UPDATE proxy_token SET revoked_at = ? WHERE tenant_id = ? AND revoked_at IS NULL",
            (self._now(), tenant_id))

    def has_live_token(self, tenant_id: str) -> bool:
        """Whether this tenant has an un-revoked proxy token.

        THE INVARIANT resync ENFORCES: a running container holds its tenant's
        current token. The gateway never sees the plaintext a container was
        given, so it cannot compare them -- but issue_token revokes the
        previous token in the same transaction as it writes the new one, so
        exactly one row per tenant is ever live, and a running container whose
        tenant has NO live row is a container whose token was revoked out from
        under it.
        """
        self._require_tenant_id(tenant_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM proxy_token WHERE tenant_id = ? AND revoked_at IS NULL "
                "LIMIT 1", (tenant_id,)).fetchone()
        return row is not None

    def tenant_for_token_hash(self, digest: str) -> tuple[str, str] | None:
        """(tenant id, status) for a live token. The proxy's only question."""
        with self._lock:
            row = self._conn.execute(
                "SELECT t.id, t.status FROM proxy_token p JOIN tenant t ON t.id = p.tenant_id "
                "WHERE p.hash = ? AND p.revoked_at IS NULL", (digest,)).fetchone()
        return (row[0], row[1]) if row else None

    def delete_tenant(self, tenant_id: str) -> None:
        """The row goes; the archive keeps the files for 30 days.

        The project id is retired rather than freed, in the same transaction
        that removes the row, so the next create_tenant still sees it. That is
        what makes this sentence true instead of aspirational: a project id is
        never reissued, and because the bridge address is derived from it, an
        address is never reissued either.
        """
        self._require_tenant_id(tenant_id)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO retired_project_id (project_id, retired_at) "
                "SELECT project_id, ? FROM tenant WHERE id = ?", (self._now(), tenant_id))
            self._conn.execute("DELETE FROM session WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute("DELETE FROM proxy_token WHERE tenant_id = ?", (tenant_id,))
            self._conn.execute("DELETE FROM tenant WHERE id = ?", (tenant_id,))
            self._conn.commit()
