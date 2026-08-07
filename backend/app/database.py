from __future__ import annotations

import contextlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 2

TICKETS_TABLE = """
CREATE TABLE {name} (
    id TEXT PRIMARY KEY,
    reporter_id TEXT NOT NULL,
    reporter_name TEXT NOT NULL,
    jellyfin_item_id TEXT NOT NULL,
    item_name TEXT NOT NULL,
    issue_type TEXT NOT NULL CHECK (issue_type IN ('audio', 'subtitles', 'video_quality', 'wrong_language', 'other')),
    status TEXT NOT NULL CHECK (status IN ('new', 'in_progress', 'resolved')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    version INTEGER NOT NULL DEFAULT 1
)
"""

SCHEMA = f"""
{TICKETS_TABLE.format(name='IF NOT EXISTS tickets')};

CREATE INDEX IF NOT EXISTS idx_tickets_reporter_item ON tickets (reporter_id, jellyfin_item_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status_created ON tickets (status, created_at);

CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_admin IN (0, 1)),
    message TEXT NOT NULL CHECK (length(message) BETWEEN 1 AND 2000),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_comments_ticket_created ON comments (ticket_id, created_at);

CREATE TABLE IF NOT EXISTS status_events (
    id TEXT PRIMARY KEY,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    actor_name TEXT NOT NULL,
    before_status TEXT,
    after_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'failed')),
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox (status, next_attempt_at);

CREATE TABLE IF NOT EXISTS rate_events (
    id TEXT PRIMARY KEY,
    key_type TEXT NOT NULL,
    key_value TEXT NOT NULL,
    action TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_events_window ON rate_events (key_type, key_value, action, created_at);
"""

LIFECYCLE_INDEXES = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_active_reporter_item
    ON tickets (reporter_id, jellyfin_item_id)
    WHERE status IN ('new', 'in_progress')
    """,
    "CREATE INDEX IF NOT EXISTS idx_tickets_reporter_created_id ON tickets (reporter_id, created_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_reporter_item_resolved ON tickets (reporter_id, jellyfin_item_id, resolved_at DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tickets_status_created ON tickets (status, created_at)",
)


class Database:
    def __init__(self, path: str):
        self.path = path

    def init(self) -> None:
        parent = Path(self.path).resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        if self._needs_lifecycle_migration():
            self._create_verified_backup()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate_ticket_lifecycle(conn)

    def _needs_lifecycle_migration(self) -> bool:
        if not Path(self.path).exists():
            return False
        with self.connect() as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tickets'"
            ).fetchone()
            if not table:
                return False
            if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                return True
            return any(
                row["unique"] and row["origin"] == "u"
                for row in conn.execute("PRAGMA index_list('tickets')").fetchall()
            )

    def _create_verified_backup(self) -> Path:
        source_path = Path(self.path)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = source_path.with_name(f"{source_path.stem}.pre-lifecycle-v{SCHEMA_VERSION}-{timestamp}{source_path.suffix}")
        source = sqlite3.connect(source_path)
        backup = sqlite3.connect(backup_path)
        try:
            source.backup(backup)
        finally:
            backup.close()
            source.close()
        validation = sqlite3.connect(f"file:{backup_path.as_posix()}?mode=ro", uri=True)
        try:
            result = validation.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            validation.close()
        if result != "ok":
            backup_path.unlink(missing_ok=True)
            raise RuntimeError("Ticket database backup integrity check failed")
        return backup_path

    def _migrate_ticket_lifecycle(self, conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        legacy_unique = any(
            row["unique"] and row["origin"] == "u"
            for row in conn.execute("PRAGMA index_list('tickets')").fetchall()
        )
        if version >= SCHEMA_VERSION and not legacy_unique:
            self._ensure_lifecycle_indexes(conn)
            return

        ticket_count = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
        ticket_ids = {
            row["id"] for row in conn.execute("SELECT id FROM tickets").fetchall()
        }
        related_counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("comments", "status_events", "notification_outbox")
        }

        if legacy_unique:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(TICKETS_TABLE.format(name="tickets_lifecycle_new"))
                conn.execute(
                    """
                    INSERT INTO tickets_lifecycle_new
                    (id, reporter_id, reporter_name, jellyfin_item_id, item_name, issue_type, status,
                     created_at, updated_at, resolved_at, version)
                    SELECT id, reporter_id, reporter_name, jellyfin_item_id, item_name, issue_type, status,
                           created_at, updated_at, resolved_at, version
                    FROM tickets
                    """
                )
                conn.execute("DROP TABLE tickets")
                conn.execute("ALTER TABLE tickets_lifecycle_new RENAME TO tickets")
                self._ensure_lifecycle_indexes(conn)
                self._verify_migration(conn, ticket_count, ticket_ids, related_counts)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
            finally:
                conn.execute("PRAGMA foreign_keys = ON")
        else:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_lifecycle_indexes(conn)
                self._verify_migration(conn, ticket_count, ticket_ids, related_counts)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()

    @staticmethod
    def _ensure_lifecycle_indexes(conn: sqlite3.Connection) -> None:
        for statement in LIFECYCLE_INDEXES:
            conn.execute(statement)

    @staticmethod
    def _verify_migration(
        conn: sqlite3.Connection,
        ticket_count: int,
        ticket_ids: set[str],
        related_counts: dict[str, int],
    ) -> None:
        migrated_ids = {row["id"] for row in conn.execute("SELECT id FROM tickets").fetchall()}
        if conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] != ticket_count or migrated_ids != ticket_ids:
            raise RuntimeError("Ticket migration did not preserve ticket records")
        for table, count in related_counts.items():
            if conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != count:
                raise RuntimeError(f"Ticket migration did not preserve {table} records")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Ticket migration foreign key check failed")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Ticket migration integrity check failed")

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()
