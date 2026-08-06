from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
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
    version INTEGER NOT NULL DEFAULT 1,
    UNIQUE (reporter_id, jellyfin_item_id)
);

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


class Database:
    def __init__(self, path: str):
        self.path = path

    def init(self) -> None:
        parent = Path(self.path).resolve().parent
        parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

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
