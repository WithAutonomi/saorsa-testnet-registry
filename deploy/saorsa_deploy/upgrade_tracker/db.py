"""SQLite persistence for the upgrade tracker."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

STATUSES = ("pending", "scheduled", "in_progress", "completed", "rolled_back")

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_upgrades (
    host TEXT NOT NULL,
    service_number TEXT NOT NULL,
    from_version TEXT NOT NULL,
    to_version TEXT NOT NULL,
    running_version TEXT NOT NULL,
    detected_at TEXT,
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL,
    last_error TEXT,
    last_error_at TEXT,
    error_count INTEGER NOT NULL DEFAULT 0,
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY (host, service_number, to_version)
);
CREATE INDEX IF NOT EXISTS idx_status ON node_upgrades(status);
CREATE INDEX IF NOT EXISTS idx_scheduled ON node_upgrades(scheduled_at);

CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class NodeUpgrade:
    host: str
    service_number: str
    from_version: str
    to_version: str
    running_version: str
    status: str
    last_synced_at: str
    detected_at: Optional[str] = None
    scheduled_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    error_count: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "NodeUpgrade":
        return cls(
            host=row["host"],
            service_number=row["service_number"],
            from_version=row["from_version"],
            to_version=row["to_version"],
            running_version=row["running_version"],
            status=row["status"],
            last_synced_at=row["last_synced_at"],
            detected_at=row["detected_at"],
            scheduled_at=row["scheduled_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            last_error=row["last_error"],
            last_error_at=row["last_error_at"],
            error_count=row["error_count"],
        )


def default_db_path(testnet_name: str) -> Path:
    base = Path.home() / ".saorsa-deploy" / "upgrades"
    return base / f"{testnet_name}.db"


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def open_db(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # --- metadata ---

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def all_meta(self) -> dict:
        rows = self.conn.execute("SELECT key, value FROM metadata").fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- node rows ---

    def insert_node(self, n: NodeUpgrade) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO node_upgrades ("
            "host, service_number, from_version, to_version, running_version, "
            "detected_at, scheduled_at, started_at, completed_at, status, "
            "last_error, last_error_at, error_count, last_synced_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                n.host, n.service_number, n.from_version, n.to_version,
                n.running_version, n.detected_at, n.scheduled_at, n.started_at,
                n.completed_at, n.status, n.last_error, n.last_error_at,
                n.error_count, n.last_synced_at,
            ),
        )

    def update_node(self, n: NodeUpgrade) -> None:
        self.conn.execute(
            "UPDATE node_upgrades SET "
            "from_version=?, running_version=?, detected_at=?, scheduled_at=?, "
            "started_at=?, completed_at=?, status=?, last_error=?, "
            "last_error_at=?, error_count=?, last_synced_at=? "
            "WHERE host=? AND service_number=? AND to_version=?",
            (
                n.from_version, n.running_version, n.detected_at, n.scheduled_at,
                n.started_at, n.completed_at, n.status, n.last_error,
                n.last_error_at, n.error_count, n.last_synced_at,
                n.host, n.service_number, n.to_version,
            ),
        )

    def get_node(
        self, host: str, service_number: str, to_version: str,
    ) -> Optional[NodeUpgrade]:
        row = self.conn.execute(
            "SELECT * FROM node_upgrades "
            "WHERE host=? AND service_number=? AND to_version=?",
            (host, service_number, to_version),
        ).fetchone()
        return NodeUpgrade.from_row(row) if row else None

    def list_nodes(self) -> list[NodeUpgrade]:
        rows = self.conn.execute(
            "SELECT * FROM node_upgrades "
            "ORDER BY (scheduled_at IS NULL), scheduled_at ASC, host, "
            "CAST(service_number AS INTEGER)"
        ).fetchall()
        return [NodeUpgrade.from_row(r) for r in rows]

    def list_active(self) -> list[NodeUpgrade]:
        """Rows not yet in a terminal state."""
        rows = self.conn.execute(
            "SELECT * FROM node_upgrades WHERE status NOT IN "
            "('completed', 'rolled_back')"
        ).fetchall()
        return [NodeUpgrade.from_row(r) for r in rows]

    def count_by_status(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM node_upgrades GROUP BY status"
        ).fetchall()
        counts = {s: 0 for s in STATUSES}
        for r in rows:
            counts[r["status"]] = r["n"]
        return counts

    def count_total(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM node_upgrades"
        ).fetchone()
        return row["n"]

    def errors_since(self, iso_cutoff: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM node_upgrades "
            "WHERE last_error_at IS NOT NULL AND last_error_at >= ?",
            (iso_cutoff,),
        ).fetchone()
        return row["n"]

    def stuck_since(self, iso_cutoff: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM node_upgrades "
            "WHERE status='in_progress' AND started_at IS NOT NULL "
            "AND started_at < ?",
            (iso_cutoff,),
        ).fetchone()
        return row["n"]


