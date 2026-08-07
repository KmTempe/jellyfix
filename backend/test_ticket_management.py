from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import UserContext, get_current_user
from app.config import load_settings
from app.database import Database
from app.factory import create_app
from app.repositories import TicketRepository


class TicketManagementTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".test-tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp_dir = tempfile.TemporaryDirectory(dir=temp_root)
        self.addCleanup(self.temp_dir.cleanup)
        self.environment = patch.dict(
            os.environ,
            {
                "JELLYFIX_ENV": "development",
                "DATABASE_PATH": str(Path(self.temp_dir.name) / "tickets.db"),
                "PUBLIC_ORIGIN": "http://testserver",
                "TRUSTED_HOSTS": "testserver",
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = load_settings()
        self.db = Database(self.settings.database_path)
        self.db.init()
        self.repo = TicketRepository(self.settings)
        self.admin = UserContext(token="admin-token", user_id="admin", name="Administrator", is_admin=True)
        self.member = UserContext(token="member-token", user_id="member", name="Member", is_admin=False)

    def insert_ticket(self, status: str) -> str:
        ticket_id = str(uuid.uuid4())
        now = "2026-08-07T00:00:00+00:00"
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tickets
                (id, reporter_id, reporter_name, jellyfin_item_id, item_name, issue_type, status, created_at, updated_at, resolved_at)
                VALUES (?, 'reporter', 'Reporter', ?, 'Test media', 'other', ?, ?, ?, ?)
                """,
                (ticket_id, str(uuid.uuid4()), status, now, now, now if status == "resolved" else None),
            )
            conn.execute(
                """
                INSERT INTO comments (id, ticket_id, author_id, author_name, is_admin, message, created_at)
                VALUES (?, ?, 'reporter', 'Reporter', 0, 'Initial report', ?)
                """,
                (str(uuid.uuid4()), ticket_id, now),
            )
            conn.execute(
                """
                INSERT INTO status_events (id, ticket_id, actor_id, actor_name, before_status, after_status, created_at)
                VALUES (?, ?, 'reporter', 'Reporter', NULL, ?, ?)
                """,
                (str(uuid.uuid4()), ticket_id, status, now),
            )
            conn.execute(
                """
                INSERT INTO notification_outbox
                (id, dedupe_key, ticket_id, event_type, payload, next_attempt_at, status, created_at, updated_at)
                VALUES (?, ?, ?, 'ticket_created', '{}', ?, 'pending', ?, ?)
                """,
                (str(uuid.uuid4()), f"ticket-created:{ticket_id}", ticket_id, now, now, now),
            )
        return ticket_id

    def test_resolved_ticket_deletion_cascades_all_related_records(self):
        ticket_id = self.insert_ticket("resolved")
        with self.db.transaction() as conn:
            result = self.repo.delete_tickets(conn, [ticket_id], self.admin)
        self.assertEqual(result["deleted_ids"], [ticket_id])
        with self.db.connect() as conn:
            for table in ("tickets", "comments", "status_events", "notification_outbox"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_active_ticket_deletion_returns_conflict_without_mutation(self):
        ticket_id = self.insert_ticket("in_progress")
        with self.assertRaises(HTTPException) as error:
            with self.db.transaction() as conn:
                self.repo.delete_tickets(conn, [ticket_id], self.admin)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(error.exception.detail["active_ticket_ids"], [ticket_id])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)

    def test_active_ticket_deletion_is_allowed_only_with_explicit_setting(self):
        ticket_id = self.insert_ticket("new")
        override_repo = TicketRepository(replace(self.settings, allow_active_ticket_deletion=True))
        with self.db.transaction() as conn:
            result = override_repo.delete_tickets(conn, [ticket_id], self.admin)
        self.assertEqual(result["deleted_ids"], [ticket_id])

    def test_non_admin_cannot_delete_or_bulk_update(self):
        ticket_id = self.insert_ticket("resolved")
        with self.assertRaises(HTTPException) as delete_error:
            with self.db.transaction() as conn:
                self.repo.delete_tickets(conn, [ticket_id], self.member)
        self.assertEqual(delete_error.exception.status_code, 403)
        with self.assertRaises(HTTPException) as status_error:
            with self.db.transaction() as conn:
                self.repo.bulk_update_status(conn, [ticket_id], self.member, "in_progress")
        self.assertEqual(status_error.exception.status_code, 403)

    def test_bulk_status_update_handles_new_and_resolved_tickets(self):
        new_ticket = self.insert_ticket("new")
        resolved_ticket = self.insert_ticket("resolved")
        with self.db.transaction() as conn:
            result = self.repo.bulk_update_status(conn, [new_ticket, resolved_ticket], self.admin, "in_progress")
        self.assertEqual(result["updated_ids"], [new_ticket, resolved_ticket])
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, status FROM tickets ORDER BY id").fetchall()
        self.assertEqual({row["id"]: row["status"] for row in rows}, {
            new_ticket: "in_progress",
            resolved_ticket: "in_progress",
        })

    def test_delete_api_requires_origin_admin_and_valid_ids(self):
        ticket_id = self.insert_ticket("resolved")
        app = create_app(self.settings)
        app.dependency_overrides[get_current_user] = lambda: self.member
        with TestClient(app) as client:
            missing_origin = client.request("DELETE", "/api/v1/tickets", json={"ticket_ids": [ticket_id]})
            self.assertEqual(missing_origin.status_code, 403)
            denied = client.request(
                "DELETE",
                "/api/v1/tickets",
                json={"ticket_ids": [ticket_id]},
                headers={"Origin": self.settings.public_origin},
            )
            self.assertEqual(denied.status_code, 403)
            invalid = client.request(
                "DELETE",
                "/api/v1/tickets/not-a-uuid",
                headers={"Origin": self.settings.public_origin},
            )
            self.assertEqual(invalid.status_code, 422)
