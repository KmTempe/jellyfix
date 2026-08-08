from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.config import load_settings
from app.factory import create_app
from app.notifications import process_outbox_once


ITEM_ID = "a" * 32


async def idle_worker(*_args) -> None:
    while True:
        await asyncio.sleep(3600)


class IntegrationHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, _format: str, *_args) -> None:
        return

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body: dict | None = None) -> None:
        parsed = urlsplit(self.path)
        self.requests.append(
            {
                "method": self.command,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers),
                "body": body or {},
            }
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self._record()
        if parsed.path == "/Users/Me":
            if self.headers.get("X-Emby-Token") != "valid-token":
                self._send({"error": "unauthorized"}, 401)
                return
            self._send(
                {
                    "Id": "user-1",
                    "Name": "Integration User",
                    "Policy": {"IsAdministrator": True},
                }
            )
            return
        if parsed.path == f"/Items/{ITEM_ID}":
            if parse_qs(parsed.query).get("userId") != ["user-1"]:
                self._send({"error": "not found"}, 404)
                return
            self._send({"Id": ITEM_ID, "Name": "Integration Movie"})
            return
        if parsed.path == "/api/users":
            if self.headers.get("X-API-Key") != "wizarr-test-key":
                self._send({"error": "unauthorized"}, 401)
                return
            self._send(
                {
                    "users": [
                        {
                            "username": "Integration User",
                            "server_type": "jellyfin",
                            "email": "integration@example.test",
                        }
                    ]
                }
            )
            return
        if parsed.path == "/api/v1/conversations/search":
            self._send({"status": "success", "data": {"results": []}})
            return
        self._send({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._body()
        self._record(body)
        if self.path == "/api/v1/conversations":
            self._send({"status": "success", "data": {"id": 10, "uuid": "conversation-1"}})
            return
        if self.path == "/api/v1/conversations/conversation-1/tags":
            self._send({"status": "success", "data": {}})
            return
        self._send({"error": "not found"}, 404)

    def do_PUT(self) -> None:  # noqa: N802
        body = self._body()
        self._record(body)
        if self.path == "/api/v1/conversations/conversation-1/assignee/team":
            self._send({"status": "success", "data": {}})
            return
        self._send({"error": "not found"}, 404)


class ServiceStub:
    def __enter__(self):
        IntegrationHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), IntegrationHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class HttpIntegrationTests(unittest.TestCase):
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
            },
            clear=True,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def settings(self, base_url: str):
        wizarr_token = Path(self.temp_dir.name) / "wizarr-token"
        wizarr_token.write_text("wizarr-test-key", encoding="utf-8")
        libredesk_credentials = Path(self.temp_dir.name) / "libredesk-credentials"
        libredesk_credentials.write_text("api-key:api-secret", encoding="utf-8")
        return replace(
            load_settings(),
            jellyfin_url=base_url,
            jellyfin_timeout_seconds=0.5,
            wizarr_base_url=base_url,
            wizarr_token_file=str(wizarr_token),
            wizarr_email_required=True,
            wizarr_timeout_seconds=0.5,
            libredesk_base_url=base_url,
            libredesk_credential_file=str(libredesk_credentials),
            libredesk_inbox_id=1,
            libredesk_team_id=1,
            libredesk_timeout_seconds=0.5,
        )

    @staticmethod
    def request_headers(token: str = "valid-token") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Origin": "http://testserver",
            "Host": "testserver",
        }

    def test_ticket_creation_and_delivery_use_real_http_clients(self):
        with ServiceStub() as stub:
            app = create_app(self.settings(stub.base_url))
            with patch("app.factory.outbox_worker", idle_worker), TestClient(app) as client:
                identity = client.get("/api/v1/me", headers=self.request_headers())
                created = client.post(
                    "/api/v1/tickets",
                    headers=self.request_headers(),
                    json={"item_id": ITEM_ID, "issue_type": "audio", "message": "Audio is out of sync"},
                )
                self.assertEqual(identity.status_code, 200)
                self.assertEqual(identity.json()["id"], "user-1")
                self.assertTrue(identity.json()["is_admin"])
                self.assertEqual(created.status_code, 200)
                self.assertEqual(created.json()["status"], "new")

                delivered = process_outbox_once(
                    app.state.db,
                    app.state.settings,
                    app.state.wizarr_email_lookup,
                    app.state.libredesk_client,
                )

            self.assertEqual(delivered, 1)
            with app.state.db.connect() as conn:
                outbox = conn.execute("SELECT status FROM notification_outbox").fetchone()
                ticket = conn.execute("SELECT item_name FROM tickets").fetchone()
                mapping = conn.execute(
                    "SELECT conversation_uuid, contact_email FROM ticket_integrations"
                ).fetchone()
            self.assertEqual(ticket["item_name"], "Integration Movie")
            self.assertEqual(outbox["status"], "sent")
            self.assertEqual(mapping["conversation_uuid"], "conversation-1")
            self.assertEqual(mapping["contact_email"], "integration@example.test")

            paths = [request["path"] for request in IntegrationHandler.requests]
            self.assertIn("/Users/Me", paths)
            self.assertIn(f"/Items/{ITEM_ID}", paths)
            self.assertIn("/api/users", paths)
            self.assertIn("/api/v1/conversations/search", paths)
            self.assertIn("/api/v1/conversations", paths)
            conversation_request = next(
                request for request in IntegrationHandler.requests if request["path"] == "/api/v1/conversations"
            )
            self.assertEqual(conversation_request["body"]["contact_email"], "integration@example.test")
            expected_basic = base64.b64encode(b"api-key:api-secret").decode("ascii")
            self.assertEqual(conversation_request["headers"]["Authorization"], f"Basic {expected_basic}")

    def test_invalid_token_and_unavailable_jellyfin_are_rejected(self):
        with ServiceStub() as stub:
            app = create_app(self.settings(stub.base_url))
            with patch("app.factory.outbox_worker", idle_worker), TestClient(app) as client:
                invalid = client.get("/api/v1/me", headers=self.request_headers("invalid-token"))
            self.assertEqual(invalid.status_code, 401)

        unavailable_settings = self.settings("http://127.0.0.1:1")
        unavailable_app = create_app(unavailable_settings)
        with patch("app.factory.outbox_worker", idle_worker), TestClient(unavailable_app) as client:
            unavailable = client.get("/api/v1/me", headers=self.request_headers())
        self.assertEqual(unavailable.status_code, 503)

    def test_external_delivery_failure_leaves_ticket_pending(self):
        with ServiceStub() as stub:
            settings = replace(
                self.settings(stub.base_url),
                wizarr_base_url="http://127.0.0.1:1",
                libredesk_base_url="http://127.0.0.1:1",
            )
            app = create_app(settings)
            with patch("app.factory.outbox_worker", idle_worker), TestClient(app) as client:
                created = client.post(
                    "/api/v1/tickets",
                    headers=self.request_headers(),
                    json={"item_id": ITEM_ID, "issue_type": "other", "message": "Delivery retry"},
                )
                self.assertEqual(created.status_code, 200)
                delivered = process_outbox_once(
                    app.state.db,
                    app.state.settings,
                    app.state.wizarr_email_lookup,
                    app.state.libredesk_client,
                )
            self.assertEqual(delivered, 0)
            with app.state.db.connect() as conn:
                outbox = conn.execute(
                    "SELECT status, last_error FROM notification_outbox"
                ).fetchone()
            self.assertEqual(outbox["status"], "pending")
            self.assertEqual(outbox["last_error"], "ReplyToUnavailable")


if __name__ == "__main__":
    unittest.main()
