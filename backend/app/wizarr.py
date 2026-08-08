from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .auth import NoRedirectHandler
from .config import Settings


LOGGER = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class ReplyToUnavailable(Exception):
    """The configured Wizarr service could not provide a verified email yet."""


class WizarrEmailLookup:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.opener = urllib.request.build_opener(NoRedirectHandler)
        self._cache: dict[str, tuple[float, str]] = {}
        self.enabled = self._valid_configuration()

    def _valid_configuration(self) -> bool:
        if not (self.settings.wizarr_base_url and self.settings.wizarr_token_file):
            return False
        parsed = urlsplit(self.settings.wizarr_base_url)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.netloc)
        )

    def lookup(self, jellyfin_user_id: str, jellyfin_username: str) -> str:
        if not self.enabled:
            raise ReplyToUnavailable("Wizarr Reply-To lookup is not configured")
        cache_key = f"{jellyfin_user_id}:{jellyfin_username}"
        cached = self._cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            token = Path(self.settings.wizarr_token_file).read_text(encoding="utf-8").strip()
            if not token:
                raise ReplyToUnavailable("Wizarr token file is empty")
            request = urllib.request.Request(
                f"{self.settings.wizarr_base_url}/api/users?{urlencode({'username': jellyfin_username})}",
                headers={"Accept": "application/json", "X-API-Key": token},
                method="GET",
            )
            with self.opener.open(request, timeout=self.settings.wizarr_timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
            email = self._verified_email(payload, jellyfin_username)
        except (OSError, TimeoutError, urllib.error.HTTPError, json.JSONDecodeError, ValueError) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            LOGGER.warning("Wizarr Reply-To lookup unavailable (%s)", exc.__class__.__name__)
            raise ReplyToUnavailable("Wizarr Reply-To lookup unavailable") from exc
        ttl = max(self.settings.wizarr_cache_ttl_seconds, 0)
        self._cache[cache_key] = (time.monotonic() + ttl, email)
        return email

    @staticmethod
    def _verified_email(payload: object, jellyfin_username: str) -> str:
        users = payload.get("users") if isinstance(payload, dict) else payload
        if not isinstance(users, list) or len(users) != 1 or not isinstance(users[0], dict):
            raise ValueError("Wizarr response was missing or ambiguous")
        user = users[0]
        if str(user.get("username") or "") != jellyfin_username:
            raise ValueError("Wizarr response username did not match")
        if str(user.get("server_type") or "").lower() != "jellyfin":
            raise ValueError("Wizarr response was not a Jellyfin user")
        email = str(user.get("email") or "").strip()
        _, parsed = parseaddr(email)
        if parsed != email or not EMAIL_RE.fullmatch(email) or "\r" in email or "\n" in email:
            raise ValueError("Wizarr response email was invalid")
        return email
