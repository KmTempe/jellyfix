from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import quote

from fastapi import Depends, Header, HTTPException, Request

from .config import Settings


class JellyfinUnavailable(Exception):
    pass


class JellyfinUnauthorized(Exception):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


@dataclass(frozen=True)
class UserContext:
    token: str
    user_id: str
    name: str
    is_admin: bool


@dataclass(frozen=True)
class MediaContext:
    item_id: str
    item_name: str


class JellyfinClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.opener = urllib.request.build_opener(NoRedirectHandler)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f'MediaBrowser Token="{token}"',
            "X-Emby-Token": token,
            "X-MediaBrowser-Token": token,
        }

    def _get_json(self, path: str, token: str) -> dict:
        if not self.settings.jellyfin_url:
            raise JellyfinUnavailable("Jellyfin URL is not configured")
        request = urllib.request.Request(
            f"{self.settings.jellyfin_url}{path}",
            headers=self._headers(token),
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.settings.jellyfin_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                raise JellyfinUnavailable("Jellyfin redirected validation request") from exc
            if exc.code in {401, 403}:
                raise JellyfinUnauthorized("Jellyfin rejected token") from exc
            if exc.code == 404:
                raise
            raise JellyfinUnavailable("Jellyfin validation failed") from exc
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            raise JellyfinUnavailable("Jellyfin validation failed") from exc

    def validate_user(self, token: str) -> UserContext:
        payload = self._get_json("/Users/Me", token)
        user_id = str(payload.get("Id") or "")
        if not user_id:
            raise JellyfinUnauthorized("Jellyfin response did not contain a user ID")
        policy = payload.get("Policy") or {}
        return UserContext(
            token=token,
            user_id=user_id,
            name=str(payload.get("Name") or "Jellyfin User"),
            is_admin=bool(policy.get("IsAdministrator")),
        )

    def validate_media(self, token: str, user_id: str, item_id: str) -> MediaContext:
        try:
            payload = self._get_json(f"/Items/{quote(item_id)}?userId={quote(user_id)}", token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise JellyfinUnauthorized("Media is not accessible") from exc
            raise
        payload_id = str(payload.get("Id") or item_id)
        item_name = str(payload.get("Name") or "Unknown media")
        return MediaContext(item_id=payload_id.lower(), item_name=item_name[:500])


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Invalid Authorization header")
    return token.strip()


def get_jellyfin_client(request: Request) -> JellyfinClient:
    return request.app.state.jellyfin_client


def get_current_user(
    authorization: str | None = Header(default=None),
    client: JellyfinClient = Depends(get_jellyfin_client),
) -> UserContext:
    token = extract_bearer(authorization)
    try:
        return client.validate_user(token)
    except JellyfinUnauthorized as exc:
        raise HTTPException(status_code=401, detail="Invalid Jellyfin token") from exc
    except JellyfinUnavailable as exc:
        raise HTTPException(status_code=503, detail="Jellyfin validation unavailable") from exc
