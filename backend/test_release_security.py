from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import load_settings
from app.factory import create_app


class ReleaseSecurityTests(unittest.TestCase):
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
        self.settings = load_settings()

    def test_health_is_public_but_ticket_history_requires_authentication(self):
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get("/api/v1/healthz").status_code, 200)
            self.assertEqual(client.get("/api/v1/tickets/mine").status_code, 401)

    def test_trusted_host_and_write_origin_are_enforced(self):
        with TestClient(create_app(self.settings)) as client:
            self.assertEqual(client.get("/api/v1/healthz", headers={"Host": "invalid.test"}).status_code, 400)
            response = client.post(
                "/api/v1/tickets",
                json={"item_id": "a" * 32, "issue_type": "other", "message": "Report"},
            )
            self.assertEqual(response.status_code, 403)

    def test_webhook_route_accepts_valid_signature_without_browser_origin(self):
        secret_path = Path(self.temp_dir.name) / "webhook-secret"
        secret_path.write_text("test-secret", encoding="utf-8")
        settings = replace(self.settings, libredesk_webhook_secret_file=str(secret_path))
        body = json.dumps(
            {
                "event": "message.created",
                "payload": {"uuid": "message-1", "conversation_uuid": "conversation-1"},
            }
        ).encode("utf-8")
        signature = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
        with TestClient(create_app(settings)) as client:
            accepted = client.post(
                "/api/v1/integrations/libredesk/webhook",
                content=body,
                headers={"Content-Type": "application/json", "X-Libredesk-Signature": signature},
            )
            rejected = client.post(
                "/api/v1/integrations/libredesk/webhook",
                content=body,
                headers={"Content-Type": "application/json", "X-Libredesk-Signature": "sha256=invalid"},
            )
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(rejected.status_code, 401)
        with create_app(settings).state.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM integration_inbox").fetchone()[0], 1)

    def test_webhook_route_enforces_request_size_limit(self):
        secret_path = Path(self.temp_dir.name) / "webhook-secret"
        secret_path.write_text("test-secret", encoding="utf-8")
        settings = replace(
            self.settings,
            libredesk_webhook_secret_file=str(secret_path),
            max_body_bytes=8,
        )
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/api/v1/integrations/libredesk/webhook",
                content=b'{"event":"message.created"}',
                headers={"Content-Type": "application/json", "X-Libredesk-Signature": "sha256=unused"},
            )
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
