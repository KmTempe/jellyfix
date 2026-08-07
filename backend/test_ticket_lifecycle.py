from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import MediaContext, UserContext, get_current_user
from app.config import load_settings
from app.database import Database
from app.factory import create_app
from app.notifications import _message, process_outbox_once
from app.repositories import TicketRepository, iso
from app.wizarr import ReplyToUnavailable, WizarrEmailLookup


NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)
ITEM_ID = "a" * 32


class UnavailableWizarr:
    def lookup(self, _user_id: str, _username: str) -> str:
        raise ReplyToUnavailable("offline")


class TicketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "JELLYFIX_ENV": "development",
                "DATABASE_PATH": str(Path(self.temp_dir.name) / "tickets.db"),
                "PUBLIC_ORIGIN": "http://testserver",
                "TRUSTED_HOSTS": "testserver",
                "RESOLVED_TICKET_COOLDOWN_SECONDS": "300",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = load_settings()
        self.db = Database(self.settings.database_path)
        self.db.init()
        self.repo = TicketRepository(self.settings)
        self.member = UserContext(token="member", user_id="member", name="Member", is_admin=False)
        self.other = UserContext(token="other", user_id="other", name="Other", is_admin=False)
        self.admin = UserContext(token="admin", user_id="admin", name="Admin", is_admin=True)

    def add_ticket(
        self,
        *,
        reporter_id: str = "member",
        item_id: str = ITEM_ID,
        status: str = "resolved",
        created_at: datetime = NOW,
        resolved_at: datetime | None = None,
    ) -> str:
        ticket_id = str(uuid.uuid4())
        created = iso(created_at)
        resolved = iso(resolved_at) if resolved_at else (created if status == "resolved" else None)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tickets
                (id, reporter_id, reporter_name, jellyfin_item_id, item_name, issue_type, status,
                 created_at, updated_at, resolved_at)
                VALUES (?, ?, 'Reporter', ?, 'Media', 'other', ?, ?, ?, ?)
                """,
                (ticket_id, reporter_id, item_id, status, created, created, resolved),
            )
        return ticket_id

    def create_ticket(self) -> dict[str, str]:
        with self.db.transaction() as conn:
            return self.repo.create_ticket(
                conn,
                self.member,
                MediaContext(item_id=ITEM_ID, item_name="Media"),
                "other",
                "Report",
                "127.0.0.1",
            )

    def test_multiple_resolved_tickets_are_allowed_but_only_one_active_ticket_is_allowed(self):
        first = self.add_ticket(status="resolved")
        second = self.add_ticket(status="resolved")
        self.assertNotEqual(first, second)
        self.add_ticket(status="new")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_ticket(status="in_progress")

    def test_cooldown_returns_429_at_299_seconds_and_allows_creation_at_300_seconds(self):
        resolved_ticket = self.add_ticket(status="resolved", resolved_at=NOW)
        with patch("app.repositories.utcnow", return_value=NOW + timedelta(seconds=299)):
            with self.assertRaises(HTTPException) as error:
                self.create_ticket()
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.headers["Retry-After"], "1")
        self.assertEqual(error.exception.detail["ticket_id"], resolved_ticket)
        self.assertEqual(error.exception.detail["cooldown_expires_at"], iso(NOW + timedelta(seconds=300)))

        with patch("app.repositories.utcnow", return_value=NOW + timedelta(seconds=300)):
            created = self.create_ticket()
        self.assertEqual(created["status"], "new")

    def test_item_ticket_returns_resolved_ticket_only_during_cooldown(self):
        ticket_id = self.add_ticket(status="resolved", resolved_at=NOW)
        with self.db.connect() as conn, patch("app.repositories.utcnow", return_value=NOW + timedelta(seconds=299)):
            result = self.repo.current_user_ticket_for_item(conn, ITEM_ID, self.member)
        self.assertEqual(result["ticket"]["id"], ticket_id)
        self.assertEqual(result["ticket"]["cooldown_expires_at"], iso(NOW + timedelta(seconds=300)))
        with self.db.connect() as conn, patch("app.repositories.utcnow", return_value=NOW + timedelta(seconds=300)):
            self.assertIsNone(self.repo.current_user_ticket_for_item(conn, ITEM_ID, self.member))

    def test_active_ticket_returns_conflict_without_affecting_resolved_history(self):
        historical = self.add_ticket(status="resolved", resolved_at=NOW - timedelta(seconds=301))
        active = self.add_ticket(status="new")
        with self.assertRaises(HTTPException) as error:
            self.create_ticket()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["ticket_id"], active)
        with self.db.connect() as conn:
            self.assertEqual(
                conn.execute("SELECT status FROM tickets WHERE id = ?", (historical,)).fetchone()["status"],
                "resolved",
            )

    def test_my_ticket_history_is_paginated_and_owner_only(self):
        oldest = self.add_ticket(created_at=NOW - timedelta(seconds=2))
        middle = self.add_ticket(created_at=NOW - timedelta(seconds=1))
        newest = self.add_ticket(created_at=NOW)
        self.add_ticket(reporter_id="other", created_at=NOW + timedelta(seconds=1))
        app = create_app(self.settings)
        app.dependency_overrides[get_current_user] = lambda: self.member
        with TestClient(app) as client:
            first = client.get("/api/v1/tickets/mine?limit=2")
            self.assertEqual(first.status_code, 200)
            self.assertEqual([row["id"] for row in first.json()["tickets"]], [newest, middle])
            self.assertNotIn("reporter_id", first.json()["tickets"][0])
            second = client.get(f"/api/v1/tickets/mine?limit=2&cursor={first.json()['next_cursor']}")
            self.assertEqual([row["id"] for row in second.json()["tickets"]], [oldest])
            self.assertIsNone(second.json()["next_cursor"])
            forbidden = client.get(f"/api/v1/tickets/{self.add_ticket(reporter_id='other')}")
            self.assertEqual(forbidden.status_code, 404)

    def test_my_ticket_history_is_empty_for_a_user_with_no_tickets(self):
        app = create_app(self.settings)
        app.dependency_overrides[get_current_user] = lambda: self.member
        with TestClient(app) as client:
            response = client.get("/api/v1/tickets/mine")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"tickets": [], "next_cursor": None})

    def test_reply_to_is_added_to_message_and_unavailable_lookup_keeps_outbox_pending(self):
        message = _message(
            self.settings,
            {
                "ticket_id": "ticket",
                "item_name": "Media",
                "issue_type": "other",
                "reporter_name": "Member",
                "message": "Report",
                "reply_to": "member@example.com",
            },
        )
        self.assertEqual(message["Reply-To"], "member@example.com")

        ticket_id = self.add_ticket(status="new")
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO notification_outbox
                (id, dedupe_key, ticket_id, event_type, payload, next_attempt_at, status, created_at, updated_at)
                VALUES (?, ?, ?, 'ticket_created', ?, ?, 'pending', ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    "reply-to-pending",
                    ticket_id,
                    json.dumps({"ticket_id": ticket_id, "reporter_id": "member", "reply_to_required": True}),
                    iso(NOW),
                    iso(NOW),
                    iso(NOW),
                ),
            )
        with patch("app.notifications.utcnow", return_value=NOW):
            self.assertEqual(process_outbox_once(self.db, self.settings, UnavailableWizarr()), 0)
        with self.db.connect() as conn:
            row = conn.execute("SELECT status, attempt_count FROM notification_outbox WHERE dedupe_key = 'reply-to-pending'").fetchone()
        self.assertEqual(dict(row), {"status": "pending", "attempt_count": 0})

    def test_wizarr_reply_to_requires_one_exact_jellyfin_username_and_valid_email(self):
        lookup = WizarrEmailLookup(self.settings)
        self.assertEqual(
            lookup._verified_email(
                {"count": 1, "users": [{"username": "Member", "server_type": "jellyfin", "email": "member@example.com"}]},
                "Member",
            ),
            "member@example.com",
        )
        with self.assertRaises(ValueError):
            lookup._verified_email(
                {"count": 1, "users": [{"username": "Other", "server_type": "jellyfin", "email": "member@example.com"}]},
                "Member",
            )
        with self.assertRaises(ValueError):
            lookup._verified_email(
                {"count": 1, "users": [{"username": "Member", "server_type": "jellyfin", "email": "not an email"}]},
                "Member",
            )
        with self.assertRaises(ValueError):
            lookup._verified_email({"count": 0, "users": []}, "Member")


class TicketMigrationTests(unittest.TestCase):
    def test_legacy_unique_constraint_is_migrated_with_a_verified_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tickets.db"
            ticket_id = str(uuid.uuid4())
            conn = sqlite3.connect(path)
            try:
                conn.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE tickets (
                        id TEXT PRIMARY KEY,
                        reporter_id TEXT NOT NULL,
                        reporter_name TEXT NOT NULL,
                        jellyfin_item_id TEXT NOT NULL,
                        item_name TEXT NOT NULL,
                        issue_type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        resolved_at TEXT,
                        version INTEGER NOT NULL DEFAULT 1,
                        UNIQUE (reporter_id, jellyfin_item_id)
                    );
                    CREATE TABLE comments (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE, author_id TEXT NOT NULL, author_name TEXT NOT NULL, is_admin INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE status_events (id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE, actor_id TEXT NOT NULL, actor_name TEXT NOT NULL, before_status TEXT, after_status TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE notification_outbox (id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE, event_type TEXT NOT NULL, payload TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, status TEXT NOT NULL, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                    CREATE TABLE rate_events (id TEXT PRIMARY KEY, key_type TEXT NOT NULL, key_value TEXT NOT NULL, action TEXT NOT NULL, created_at TEXT NOT NULL);
                    """
                )
                now = iso(NOW)
                conn.execute("INSERT INTO tickets VALUES (?, 'member', 'Member', ?, 'Media', 'other', 'resolved', ?, ?, ?, 1)", (ticket_id, ITEM_ID, now, now, now))
                conn.execute("INSERT INTO comments VALUES (?, ?, 'member', 'Member', 0, 'Report', ?)", (str(uuid.uuid4()), ticket_id, now))
                conn.execute("INSERT INTO status_events VALUES (?, ?, 'member', 'Member', NULL, 'resolved', ?)", (str(uuid.uuid4()), ticket_id, now))
                conn.execute("INSERT INTO notification_outbox VALUES (?, 'legacy-ticket', ?, 'ticket_created', '{}', 0, ?, 'pending', NULL, ?, ?)", (str(uuid.uuid4()), ticket_id, now, now, now))
                conn.commit()
            finally:
                conn.close()
            db = Database(str(path))
            db.init()
            with db.connect() as conn:
                self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM comments").fetchone()[0], 1)
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
                indexes = conn.execute("PRAGMA index_list('tickets')").fetchall()
                self.assertTrue(any(row["name"] == "idx_tickets_active_reporter_item" for row in indexes))
            backups = list(path.parent.glob("tickets.pre-lifecycle-v2-*.db"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
            finally:
                backup.close()
            db.init()
            self.assertEqual(len(list(path.parent.glob("tickets.pre-lifecycle-v2-*.db"))), 1)
