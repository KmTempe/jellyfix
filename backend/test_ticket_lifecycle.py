from __future__ import annotations

import json
import hashlib
import hmac
import os
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import MediaContext, UserContext, get_current_user
from app.config import load_settings
from app.database import Database
from app.factory import create_app
from app.libredesk import LibredeskClient, LibredeskPermanentError, LibredeskUnavailable
from app.notifications import enqueue_webhook, process_inbox_once, process_outbox_once, reconcile_once, webhook_signature_valid
from app.repositories import TicketRepository, iso
from app.wizarr import ReplyToUnavailable, WizarrEmailLookup


NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)
ITEM_ID = "a" * 32


class UnavailableWizarr:
    def lookup(self, _user_id: str, _username: str) -> str:
        raise ReplyToUnavailable("offline")


class RecordingLibreDesk:
    def __init__(self):
        self.messages: list[dict] = []

    def send_message(self, conversation_uuid: str, message: str, sender_type: str, echo_id: str) -> dict:
        self.messages.append(
            {
                "conversation_uuid": conversation_uuid,
                "message": message,
                "sender_type": sender_type,
                "echo_id": echo_id,
            }
        )
        return {"uuid": "remote-message-1"}


class ReconcileLibreDesk:
    enabled = True

    def __init__(self, status: str, updated_at: str, pages: dict[int, list[dict]] | None = None):
        self.status = status
        self.updated_at = updated_at
        self.pages = pages or {}
        self.requested_pages: list[int] = []

    def conversation(self, _conversation_uuid: str) -> dict:
        return {"status": self.status, "updated_at": self.updated_at}

    def messages(self, _conversation_uuid: str, page: int = 1) -> list[dict]:
        self.requested_pages.append(page)
        return self.pages.get(page, [])


class RecoveryLibreDesk:
    def __init__(self, matches: list[dict]):
        self.matches = matches
        self.create_calls = 0
        self.assigned: list[str] = []
        self.tagged: list[str] = []

    @staticmethod
    def conversation_subject(payload: dict) -> str:
        return f"jellyfin-issue#{payload['ticket_id']}"

    def search_conversations(self, _subject: str) -> list[dict]:
        return self.matches

    def create_conversation(self, payload: dict, _contact_email: str) -> dict:
        self.create_calls += 1
        return {"id": 10, "uuid": f"created-{payload['ticket_id']}"}

    def assign_team(self, conversation_uuid: str) -> None:
        self.assigned.append(conversation_uuid)

    def add_tag(self, conversation_uuid: str) -> None:
        self.tagged.append(conversation_uuid)


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

    def test_wizarr_unavailable_keeps_libredesk_creation_pending_and_content_is_escaped(self):
        content = LibredeskClient.initial_content(
            {"ticket_id": "ticket", "item_name": "<Media>", "issue_type": "other", "reporter_name": "Member", "message": "<script>"}
        )
        self.assertIn("&lt;Media&gt;", content)
        self.assertIn("&lt;script&gt;", content)

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
                    json.dumps({"ticket_id": ticket_id, "reporter_id": "member", "contact_email_required": True}),
                    iso(NOW),
                    iso(NOW),
                    iso(NOW),
                ),
            )
        with patch("app.notifications.utcnow", return_value=NOW):
            self.assertEqual(process_outbox_once(self.db, self.settings, UnavailableWizarr(), object()), 0)
        with self.db.connect() as conn:
            row = conn.execute("SELECT status, attempt_count FROM notification_outbox WHERE dedupe_key = 'reply-to-pending'").fetchone()
        self.assertEqual(dict(row), {"status": "pending", "attempt_count": 0})

    def test_libredesk_webhook_signature_and_deduped_inbox(self):
        secret_path = Path(self.temp_dir.name) / "webhook-secret"
        secret_path.write_text("test-secret", encoding="utf-8")
        settings = replace(self.settings, libredesk_webhook_secret_file=str(secret_path))
        body = json.dumps({"event": "message.created", "payload": {"uuid": "remote-message"}}).encode("utf-8")
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(webhook_signature_valid(settings, body, signature))
        self.assertFalse(webhook_signature_valid(settings, body, "sha256=invalid"))
        enqueue_webhook(self.db, body)
        enqueue_webhook(self.db, body)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM integration_inbox").fetchone()[0], 1)

    def test_reporter_admin_comment_syncs_as_contact_and_marks_sent(self):
        ticket_id = self.add_ticket(reporter_id=self.admin.user_id, status="new")
        comment_id = str(uuid.uuid4())
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at) VALUES (?, 'libredesk', 1, 'conversation-1', 'member@example.com', ?, ?)",
                (ticket_id, now, now),
            )
            conn.execute(
                "INSERT INTO comments (id, ticket_id, author_id, author_name, is_admin, author_role, message, metadata_json, delivery_status, created_at) VALUES (?, ?, ?, ?, 0, 'reporter', 'Reply', '{}', 'pending', ?)",
                (comment_id, ticket_id, self.admin.user_id, self.admin.name, now),
            )
            conn.execute(
                "INSERT INTO notification_outbox (id, dedupe_key, ticket_id, event_type, payload, next_attempt_at, status, created_at, updated_at) VALUES (?, ?, ?, 'comment_created', ?, ?, 'pending', ?, ?)",
                (str(uuid.uuid4()), f"comment-created:{comment_id}", ticket_id, json.dumps({"comment_id": comment_id, "message": "Reply", "sender_type": "agent"}), now, now, now),
            )
        libredesk = RecordingLibreDesk()
        self.assertEqual(process_outbox_once(self.db, self.settings, UnavailableWizarr(), libredesk), 1)
        self.assertEqual(libredesk.messages, [{"conversation_uuid": "conversation-1", "message": "Reply", "sender_type": "contact", "echo_id": comment_id}])
        with self.db.connect() as conn:
            outbox = conn.execute("SELECT status, payload FROM notification_outbox WHERE dedupe_key = ?", (f"comment-created:{comment_id}",)).fetchone()
            comment = conn.execute("SELECT author_role, is_admin, delivery_status FROM comments WHERE id = ?", (comment_id,)).fetchone()
        self.assertEqual(outbox["status"], "sent")
        self.assertEqual(json.loads(outbox["payload"])["sender_type"], "contact")
        self.assertEqual(dict(comment), {"author_role": "reporter", "is_admin": 0, "delivery_status": "sent"})

    def test_admin_reply_to_another_reporter_is_an_agent(self):
        ticket_id = self.add_ticket(reporter_id=self.member.user_id, status="new")
        with self.db.transaction() as conn:
            self.repo.add_comment(conn, ticket_id, self.admin, "Support reply", "127.0.0.1")
            comment = conn.execute("SELECT author_role, is_admin FROM comments WHERE ticket_id = ?", (ticket_id,)).fetchone()
            outbox = conn.execute("SELECT payload FROM notification_outbox WHERE ticket_id = ? AND event_type = 'comment_created'", (ticket_id,)).fetchone()
        self.assertEqual(dict(comment), {"author_role": "agent", "is_admin": 1})
        self.assertEqual(json.loads(outbox["payload"])["sender_type"], "agent")

    def test_libredesk_agent_name_csat_link_and_private_note_handling(self):
        ticket_id = self.add_ticket(status="new")
        now = iso(NOW)
        settings = replace(self.settings, libredesk_public_url="https://desk.example.test")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at) VALUES (?, 'libredesk', 1, 'conversation-1', 'member@example.com', ?, ?)",
                (ticket_id, now, now),
            )
        agent_event = {
            "event": "message.created",
            "payload": {
                "uuid": "agent-message", "conversation_uuid": "conversation-1", "sender_id": 34,
                "sender_type": "agent", "type": "outgoing", "private": False, "text_content": "Hello",
                "author": {"id": 34, "type": "agent", "first_name": "Support", "last_name": "Agent"},
            },
        }
        csat_event = {
            "event": "message.created",
            "payload": {
                "uuid": "csat-message", "conversation_uuid": "conversation-1", "sender_id": 34,
                "sender_type": "agent", "type": "outgoing", "private": False, "text_content": "Please rate this conversation",
                "content": '<p>Please rate this conversation <a href="https://desk.example.test/csat/token">Rate</a></p>',
                "meta": {"is_csat": True, "csat_uuid": "csat-1"},
                "author": {"type": "agent", "first_name": "Support", "last_name": "Agent"},
            },
        }
        private_event = {
            "event": "message.created",
            "payload": {"uuid": "private-message", "conversation_uuid": "conversation-1", "sender_type": "agent", "type": "outgoing", "private": True, "text_content": "Internal"},
        }
        for event in (agent_event, csat_event, private_event):
            enqueue_webhook(self.db, json.dumps(event).encode("utf-8"))
        self.assertEqual(process_inbox_once(self.db, settings), 3)
        with self.db.connect() as conn:
            comments = conn.execute("SELECT author_name, author_role, is_admin, message, metadata_json FROM comments WHERE ticket_id = ? ORDER BY created_at", (ticket_id,)).fetchall()
        self.assertEqual(len(comments), 2)
        self.assertEqual(dict(comments[0])["author_name"], "Support Agent")
        self.assertEqual(dict(comments[0])["author_role"], "agent")
        metadata = json.loads(comments[1]["metadata_json"])
        self.assertEqual(metadata["kind"], "csat")
        self.assertEqual(metadata["actions"], [{"label": "Rate this support", "url": "https://desk.example.test/csat/token"}])

    def test_libredesk_message_payload_uses_echo_id_for_contact(self):
        client = LibredeskClient(self.settings)
        with patch.object(client, "_request", return_value={"uuid": "remote-message"}) as request:
            self.assertEqual(client.send_message("conversation-1", "<reply>", "contact", "local-comment"), {"uuid": "remote-message"})
        self.assertEqual(
            request.call_args.kwargs["payload"],
            {"message": "&lt;reply&gt;", "sender_type": "contact", "private": False, "echo_id": "local-comment"},
        )

    def test_libredesk_conversation_search_filters_to_matching_subjects(self):
        client = LibredeskClient(self.settings)
        with patch.object(
            client,
            "_request",
            return_value={
                "results": [
                    {"uuid": "match", "subject": "jellyfin-issue#ticket [#10]"},
                    {"uuid": "other", "subject": "another-ticket"},
                    {"subject": "jellyfin-issue#ticket"},
                ]
            },
        ) as request:
            matches = client.search_conversations("jellyfin-issue#ticket")
        self.assertEqual(matches, [{"uuid": "match", "subject": "jellyfin-issue#ticket [#10]"}])
        self.assertEqual(request.call_args.kwargs["query"], {"query": "jellyfin-issue#ticket"})

    def test_libredesk_creation_retry_recovers_existing_conversation_without_duplicate(self):
        ticket_id = self.add_ticket(status="new")
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_outbox (id, dedupe_key, ticket_id, event_type, payload, attempt_count, next_attempt_at, status, created_at, updated_at) VALUES (?, ?, ?, 'ticket_created', ?, 1, ?, 'pending', ?, ?)",
                (
                    str(uuid.uuid4()),
                    f"ticket-created:{ticket_id}",
                    ticket_id,
                    json.dumps({"ticket_id": ticket_id, "reporter_name": "Member", "contact_email": "member@example.com"}),
                    now,
                    now,
                    now,
                ),
            )
        libredesk = RecoveryLibreDesk([{"id": 9, "uuid": "existing-conversation", "subject": f"jellyfin-issue#{ticket_id}"}])
        self.assertEqual(process_outbox_once(self.db, self.settings, UnavailableWizarr(), libredesk), 1)
        self.assertEqual(libredesk.create_calls, 0)
        with self.db.connect() as conn:
            mapping = conn.execute("SELECT conversation_uuid FROM ticket_integrations WHERE ticket_id = ?", (ticket_id,)).fetchone()
            outbox = conn.execute("SELECT status FROM notification_outbox WHERE ticket_id = ?", (ticket_id,)).fetchone()
        self.assertEqual(mapping["conversation_uuid"], "existing-conversation")
        self.assertEqual(outbox["status"], "sent")

    def test_libredesk_creation_with_ambiguous_matches_stays_pending(self):
        ticket_id = self.add_ticket(status="new")
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO notification_outbox (id, dedupe_key, ticket_id, event_type, payload, next_attempt_at, status, created_at, updated_at) VALUES (?, ?, ?, 'ticket_created', ?, ?, 'pending', ?, ?)",
                (
                    str(uuid.uuid4()),
                    f"ticket-created:{ticket_id}",
                    ticket_id,
                    json.dumps({"ticket_id": ticket_id, "contact_email": "member@example.com"}),
                    now,
                    now,
                    now,
                ),
            )
        libredesk = RecoveryLibreDesk([
            {"uuid": "first", "subject": f"jellyfin-issue#{ticket_id}"},
            {"uuid": "second", "subject": f"jellyfin-issue#{ticket_id} [#2]"},
        ])
        with patch("app.notifications.utcnow", return_value=NOW):
            self.assertEqual(process_outbox_once(self.db, self.settings, UnavailableWizarr(), libredesk), 0)
        self.assertEqual(libredesk.create_calls, 0)
        with self.db.connect() as conn:
            outbox = conn.execute("SELECT status, attempt_count, last_error FROM notification_outbox WHERE ticket_id = ?", (ticket_id,)).fetchone()
        self.assertEqual(outbox["status"], "pending")
        self.assertEqual(outbox["attempt_count"], 1)
        self.assertNotIn("conversation", outbox["last_error"].lower())

    def test_libredesk_http_errors_are_classified_without_response_body(self):
        credential_path = Path(self.temp_dir.name) / "libredesk-credential"
        credential_path.write_text("key:secret", encoding="utf-8")
        settings = replace(
            self.settings,
            libredesk_base_url="http://libredesk.test",
            libredesk_credential_file=str(credential_path),
            libredesk_inbox_id=1,
        )
        client = LibredeskClient(settings)
        request = httpx.Request("GET", "http://libredesk.test/api/v1/conversations/search")
        for status in (401, 403, 429, 500):
            response = httpx.Response(status, request=request, text="sensitive response body")
            with patch("app.libredesk.httpx.request", return_value=response):
                with self.assertRaises(LibredeskUnavailable) as error:
                    client.search_conversations("jellyfin-issue#ticket")
            self.assertNotIn("sensitive", str(error.exception))
        response = httpx.Response(400, request=request, text="sensitive response body")
        with patch("app.libredesk.httpx.request", return_value=response):
            with self.assertRaises(LibredeskPermanentError) as error:
                client.search_conversations("jellyfin-issue#ticket")
        self.assertNotIn("sensitive", str(error.exception))

    def test_libredesk_credentials_require_key_and_secret(self):
        credential_path = Path(self.temp_dir.name) / "libredesk-credential"
        settings = replace(self.settings, libredesk_credential_file=str(credential_path))
        client = LibredeskClient(settings)
        for invalid in ("", "key-only", ":secret", "key:"):
            credential_path.write_text(invalid, encoding="utf-8")
            with self.assertRaises(LibredeskUnavailable):
                client._auth()

    def test_repeated_libredesk_open_status_events_reopen_ticket_each_time(self):
        ticket_id = self.add_ticket(status="new")
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at) VALUES (?, 'libredesk', 1, 'conversation-1', 'member@example.com', ?, ?)",
                (ticket_id, now, now),
            )
        for event_time, status in [
            ("2026-08-07T00:00:01Z", "Resolved"),
            ("2026-08-07T00:00:02Z", "Open"),
            ("2026-08-07T00:00:03Z", "Closed"),
            ("2026-08-07T00:00:04Z", "Open"),
        ]:
            enqueue_webhook(
                self.db,
                json.dumps(
                    {
                        "event": "conversation.status_changed",
                        "timestamp": event_time,
                        "payload": {"conversation_uuid": "conversation-1", "new_status": status},
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(process_inbox_once(self.db, self.settings), 1)
        with self.db.connect() as conn:
            ticket = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
            events = conn.execute("SELECT after_status FROM status_events WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)).fetchall()
            mapping = conn.execute("SELECT last_remote_status_at FROM ticket_integrations WHERE ticket_id = ?", (ticket_id,)).fetchone()
        self.assertEqual(ticket["status"], "in_progress")
        self.assertEqual([row["after_status"] for row in events], ["resolved", "in_progress", "resolved", "in_progress"])
        self.assertEqual(mapping["last_remote_status_at"], "2026-08-07T00:00:04+00:00")

    def test_reconciliation_repairs_a_status_mismatch_from_current_libredesk_state(self):
        ticket_id = self.add_ticket(status="resolved")
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at, last_remote_status_at) VALUES (?, 'libredesk', 1, 'conversation-1', 'member@example.com', ?, ?, ?)",
                (ticket_id, now, now, "2026-08-07T00:00:01+00:00"),
            )
        reconcile_once(self.db, self.settings, ReconcileLibreDesk("Open", "2026-08-07T00:00:02Z"))
        with self.db.connect() as conn:
            ticket = conn.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        self.assertEqual(ticket["status"], "in_progress")

    def test_reconciliation_reads_message_pages_until_a_short_page(self):
        ticket_id = self.add_ticket(status="new")
        now = iso(NOW)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at) VALUES (?, 'libredesk', 1, 'conversation-1', 'member@example.com', ?, ?)",
                (ticket_id, now, now),
            )
        first_page = [
            {"uuid": f"message-{index}", "conversation_uuid": "conversation-1", "text_content": str(index)}
            for index in range(100)
        ]
        second_page = [{"uuid": "message-100", "conversation_uuid": "conversation-1", "text_content": "100"}]
        libredesk = ReconcileLibreDesk("Open", "2026-08-07T00:00:02Z", {1: first_page, 2: second_page})
        reconcile_once(self.db, self.settings, libredesk)
        self.assertEqual(libredesk.requested_pages, [1, 2])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM integration_inbox").fetchone()[0], 101)

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
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 5)
                indexes = conn.execute("PRAGMA index_list('tickets')").fetchall()
                self.assertTrue(any(row["name"] == "idx_tickets_active_reporter_item" for row in indexes))
            backups = list(path.parent.glob("tickets.pre-lifecycle-v5-*.db"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(backups[0])
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 1)
            finally:
                backup.close()
            db.init()
            self.assertEqual(len(list(path.parent.glob("tickets.pre-lifecycle-v5-*.db"))), 1)
