from __future__ import annotations

import html
import json
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import Settings


class LibredeskUnavailable(Exception):
    pass


class LibredeskPermanentError(Exception):
    pass


class LibredeskClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(self.settings.libredesk_base_url and self.settings.libredesk_credential_file and self.settings.libredesk_inbox_id)

    def _auth(self) -> httpx.BasicAuth:
        try:
            raw = Path(self.settings.libredesk_credential_file).read_text(encoding="utf-8").strip()
            key, secret = raw.split(":", 1)
        except (OSError, ValueError) as exc:
            raise LibredeskUnavailable("LibreDesk credentials are unavailable") from exc
        if not key or not secret:
            raise LibredeskUnavailable("LibreDesk credentials are unavailable")
        return httpx.BasicAuth(key, secret)

    def _request(self, method: str, path: str, *, payload: dict | None = None, query: dict | None = None) -> dict:
        if not self.enabled:
            raise LibredeskUnavailable("LibreDesk is not configured")
        url = f"{self.settings.libredesk_base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        try:
            response = httpx.request(method, url, auth=self._auth(), json=payload, timeout=self.settings.libredesk_timeout_seconds)
        except httpx.HTTPError as exc:
            raise LibredeskUnavailable("LibreDesk request failed") from exc
        if response.status_code in {400, 404, 409, 422}:
            raise LibredeskPermanentError(f"LibreDesk HTTP {response.status_code}")
        if response.status_code >= 300:
            raise LibredeskUnavailable(f"LibreDesk HTTP {response.status_code}")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise LibredeskUnavailable("LibreDesk returned invalid JSON") from exc
        if body.get("status") != "success":
            raise LibredeskUnavailable("LibreDesk returned an unsuccessful response")
        return body.get("data") or {}

    @staticmethod
    def initial_content(payload: dict) -> str:
        values = {
            "ticket": payload["ticket_id"],
            "media": payload.get("item_name", ""),
            "issue": payload.get("issue_type", ""),
            "reporter": payload.get("reporter_name", ""),
            "message": payload.get("message", ""),
        }
        return (
            f"<p><strong>JellyFix ticket:</strong> {html.escape(str(values['ticket']))}</p>"
            f"<p><strong>Media:</strong> {html.escape(str(values['media']))}<br>"
            f"<strong>Issue:</strong> {html.escape(str(values['issue']))}<br>"
            f"<strong>Reporter:</strong> {html.escape(str(values['reporter']))}</p>"
            f"<p>{html.escape(str(values['message'])).replace(chr(10), '<br>')}</p>"
        )

    def create_conversation(self, payload: dict, contact_email: str) -> dict:
        data = self._request(
            "POST",
            "/api/v1/conversations",
            payload={
                "subject": f"{self.settings.libredesk_subject_prefix}{payload['ticket_id']}",
                "content": self.initial_content(payload),
                "inbox_id": self.settings.libredesk_inbox_id,
                "team_id": self.settings.libredesk_team_id or None,
                "contact_email": contact_email,
                "first_name": payload.get("reporter_name") or "Jellyfin user",
                "external_user_id": payload.get("reporter_id") or None,
                "initiator": "contact",
            },
        )
        if not data.get("uuid"):
            raise LibredeskUnavailable("LibreDesk did not return a conversation UUID")
        return data

    def add_tag(self, conversation_uuid: str) -> None:
        if self.settings.libredesk_tag:
            self._request("POST", f"/api/v1/conversations/{conversation_uuid}/tags", payload={"tags": [self.settings.libredesk_tag], "action": "add_tags"})

    def assign_team(self, conversation_uuid: str) -> None:
        if self.settings.libredesk_team_id:
            self._request(
                "PUT",
                f"/api/v1/conversations/{conversation_uuid}/assignee/team",
                payload={"assignee_id": self.settings.libredesk_team_id},
            )

    def send_message(self, conversation_uuid: str, message: str, sender_type: str, echo_id: str = "") -> dict:
        if sender_type not in {"agent", "contact"}:
            raise LibredeskPermanentError("Invalid LibreDesk sender type")
        payload = {"message": html.escape(message).replace("\n", "<br>"), "sender_type": sender_type, "private": False}
        if echo_id:
            payload["echo_id"] = echo_id
        data = self._request("POST", f"/api/v1/conversations/{conversation_uuid}/messages", payload=payload)
        if not data.get("uuid"):
            raise LibredeskUnavailable("LibreDesk message response did not include a UUID")
        return data

    def update_status(self, conversation_uuid: str, status: str) -> None:
        self._request("PUT", f"/api/v1/conversations/{conversation_uuid}/status", payload={"status": status})

    def conversation(self, conversation_uuid: str) -> dict:
        return self._request("GET", f"/api/v1/conversations/{conversation_uuid}")

    def messages(self, conversation_uuid: str, page: int = 1) -> list[dict]:
        data = self._request("GET", f"/api/v1/conversations/{conversation_uuid}/messages", query={"page": page, "page_size": 100})
        return data.get("results", []) if isinstance(data, dict) else []
