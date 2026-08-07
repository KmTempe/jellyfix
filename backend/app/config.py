from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    environment: str
    root_path: str
    jellyfin_url: str
    public_origin: str
    trusted_hosts: tuple[str, ...]
    database_path: str
    storage_path: str
    max_body_bytes: int
    jellyfin_timeout_seconds: float
    ticket_creations_per_user_hour: int
    comments_per_user_hour: int
    open_tickets_per_user: int
    comments_per_ticket: int
    ticket_creations_per_ip_hour: int
    ticket_creations_global_hour: int
    comments_per_ip_hour: int
    comments_global_hour: int
    retention_days: int
    smtp_server: str
    smtp_port: int
    smtp_user: str
    smtp_password_file: str
    smtp_password: str
    email_from: str
    email_to: str
    smtp_timeout_seconds: float
    smtp_messages_per_hour: int
    outbox_pending_cap: int
    allow_active_ticket_deletion: bool
    resolved_ticket_cooldown_seconds: int
    wizarr_base_url: str
    wizarr_token_file: str
    wizarr_timeout_seconds: float
    wizarr_cache_ttl_seconds: int
    wizarr_reply_to_required: bool

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


def _required(name: str, value: str, production: bool) -> str:
    if production and not value:
        raise RuntimeError(f"{name} is required")
    return value


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise RuntimeError("PUBLIC_ORIGIN must be an absolute origin like https://example.com")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def load_settings() -> Settings:
    environment = os.getenv("JELLYFIX_ENV", os.getenv("ENVIRONMENT", "production")).lower()
    production = environment == "production"
    root_path = os.getenv("ROOT_PATH", "/jellyfix").rstrip("/") or "/jellyfix"

    jellyfin_url = _required("JELLYFIN_URL", os.getenv("JELLYFIN_URL", ""), production).rstrip("/")
    public_origin_raw = _required("PUBLIC_ORIGIN", os.getenv("PUBLIC_ORIGIN", ""), production)
    public_origin = _origin(public_origin_raw or "http://testserver")
    public_host = urlsplit(public_origin).netloc
    trusted_hosts = tuple(_split_csv(os.getenv("TRUSTED_HOSTS", public_host)) or [public_host])

    storage_path = os.getenv("STORAGE_PATH", "/data" if production else ".")
    database_path = os.getenv("DATABASE_PATH", str(Path(storage_path) / "tickets.db"))

    smtp_password_file = os.getenv("SMTP_PASSWORD_FILE", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_server = os.getenv("SMTP_SERVER", "")
    smtp_user = os.getenv("SMTP_USER", "")
    if production and smtp_server and smtp_user and not smtp_password_file:
        raise RuntimeError("SMTP_PASSWORD_FILE is required when SMTP is enabled in production")

    return Settings(
        environment=environment,
        root_path=root_path,
        jellyfin_url=jellyfin_url,
        public_origin=public_origin,
        trusted_hosts=trusted_hosts,
        database_path=database_path,
        storage_path=storage_path,
        max_body_bytes=int(os.getenv("MAX_BODY_BYTES", "16384")),
        jellyfin_timeout_seconds=float(os.getenv("JELLYFIN_TIMEOUT_SECONDS", "5")),
        ticket_creations_per_user_hour=int(os.getenv("TICKET_CREATIONS_PER_USER_HOUR", "5")),
        comments_per_user_hour=int(os.getenv("COMMENTS_PER_USER_HOUR", "30")),
        open_tickets_per_user=int(os.getenv("OPEN_TICKETS_PER_USER", "10")),
        comments_per_ticket=int(os.getenv("COMMENTS_PER_TICKET", "200")),
        ticket_creations_per_ip_hour=int(os.getenv("TICKET_CREATIONS_PER_IP_HOUR", "20")),
        ticket_creations_global_hour=int(os.getenv("TICKET_CREATIONS_GLOBAL_HOUR", "100")),
        comments_per_ip_hour=int(os.getenv("COMMENTS_PER_IP_HOUR", "120")),
        comments_global_hour=int(os.getenv("COMMENTS_GLOBAL_HOUR", "500")),
        retention_days=int(os.getenv("RETENTION_DAYS", "90")),
        smtp_server=smtp_server,
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=smtp_user,
        smtp_password_file=smtp_password_file,
        smtp_password=smtp_password,
        email_from=os.getenv("EMAIL_FROM", smtp_user),
        email_to=os.getenv("EMAIL_TO", smtp_user),
        smtp_timeout_seconds=float(os.getenv("SMTP_TIMEOUT_SECONDS", "10")),
        smtp_messages_per_hour=int(os.getenv("SMTP_MESSAGES_PER_HOUR", "20")),
        outbox_pending_cap=int(os.getenv("OUTBOX_PENDING_CAP", "500")),
        allow_active_ticket_deletion=_bool(
            "ALLOW_ACTIVE_TICKET_DELETION", os.getenv("ALLOW_ACTIVE_TICKET_DELETION", "false")
        ),
        resolved_ticket_cooldown_seconds=int(os.getenv("RESOLVED_TICKET_COOLDOWN_SECONDS", "300")),
        wizarr_base_url=os.getenv("WIZARR_BASE_URL", "").rstrip("/"),
        wizarr_token_file=os.getenv("WIZARR_TOKEN_FILE", ""),
        wizarr_timeout_seconds=float(os.getenv("WIZARR_TIMEOUT_SECONDS", "5")),
        wizarr_cache_ttl_seconds=int(os.getenv("WIZARR_CACHE_TTL_SECONDS", "300")),
        wizarr_reply_to_required=_bool(
            "WIZARR_REPLY_TO_REQUIRED", os.getenv("WIZARR_REPLY_TO_REQUIRED", "false")
        ),
    )
