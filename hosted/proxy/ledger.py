"""ledger.db -- spend and platform-call times. The proxy's alone.

On the VM, ledger/ is owned by UID 10003 with mode 0700. The gateway never
opens this file: it reads spend over run/proxy/proxy.sock (internal.py), and
when that cannot answer it applies free's turn limit and /account says "Spend
is unavailable right now."

RESERVATIONS ARE AN AGGREGATE, not a row each, because that is what makes a
crash recoverable. The spec's spend table has one settled and one reserved
column per tenant per month; when the proxy starts, whatever is still reserved
is settled at its full amount, since the upstream call it was holding may
already have been billed. Per-reservation rows would have to be matched back
to calls that no longer exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    # FULL, not NORMAL: the transaction NORMAL can lose here is settled spend.
    "PRAGMA synchronous=FULL",
    "PRAGMA busy_timeout=5000",
)
# No foreign_keys pragma: neither table declares a foreign key, so turning it
# on would enforce nothing while implying a constraint that is not there.

SCHEMA = """
CREATE TABLE IF NOT EXISTS spend (
  tenant_id  TEXT NOT NULL,
  month      TEXT NOT NULL,
  settled    REAL NOT NULL DEFAULT 0,
  reserved   REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (tenant_id, month)
);
CREATE TABLE IF NOT EXISTS platform_call (
  tenant_id  TEXT PRIMARY KEY,
  at         REAL NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        for pragma in PRAGMAS:
            self._conn.execute(pragma)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def journal_mode(self) -> str:
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()

    def _row(self, tenant_id: str, month: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO spend (tenant_id, month) VALUES (?, ?)",
            (tenant_id, month))

    def spend(self, tenant_id: str, month: str) -> tuple[float, float]:
        row = self._conn.execute(
            "SELECT settled, reserved FROM spend WHERE tenant_id = ? AND month = ?",
            (tenant_id, month)).fetchone()
        return (row[0], row[1]) if row else (0.0, 0.0)

    def reserve(self, tenant_id: str, month: str, dollars: float) -> None:
        self._row(tenant_id, month)
        self._conn.execute(
            "UPDATE spend SET reserved = reserved + ? WHERE tenant_id = ? AND month = ?",
            (dollars, tenant_id, month))
        self._conn.commit()

    def settle(self, tenant_id: str, month: str, *, reserved: float, actual: float) -> None:
        """Release `reserved` and charge `actual`, in one transaction, so the
        two can never be seen apart by the /account read on the other socket."""
        self._row(tenant_id, month)
        self._conn.execute(
            "UPDATE spend SET settled = settled + ?, reserved = max(0, reserved - ?) "
            "WHERE tenant_id = ? AND month = ?",
            (actual, reserved, tenant_id, month))
        self._conn.commit()

    def release(self, tenant_id: str, month: str, dollars: float) -> None:
        self._row(tenant_id, month)
        self._conn.execute(
            "UPDATE spend SET reserved = max(0, reserved - ?) "
            "WHERE tenant_id = ? AND month = ?", (dollars, tenant_id, month))
        self._conn.commit()

    def settle_leftovers(self) -> float:
        """Called once when the proxy starts. Returns the dollars moved."""
        total = self._conn.execute(
            "SELECT COALESCE(SUM(reserved), 0) FROM spend").fetchone()[0]
        self._conn.execute("UPDATE spend SET settled = settled + reserved, reserved = 0 "
                           "WHERE reserved > 0")
        self._conn.commit()
        return float(total)

    def record_platform_call(self, tenant_id: str, at: float) -> None:
        """Recorded before any refusal, so a tenant whose calls are all
        refused still counts as free for the turn limit. Never moves
        backwards: calls can land out of order and the turn rule reads "a
        platform call in the past hour"."""
        self._conn.execute(
            "INSERT INTO platform_call (tenant_id, at) VALUES (?, ?) "
            "ON CONFLICT(tenant_id) DO UPDATE SET at = max(at, excluded.at)",
            (tenant_id, at))
        self._conn.commit()

    def last_platform_call(self, tenant_id: str) -> float | None:
        row = self._conn.execute(
            "SELECT at FROM platform_call WHERE tenant_id = ?", (tenant_id,)).fetchone()
        return row[0] if row else None
