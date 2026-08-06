from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path

from .config import Settings
from .database import Database
from .repositories import iso, utcnow


RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30)]


def _password(settings: Settings) -> str:
    if settings.smtp_password_file:
        return Path(settings.smtp_password_file).read_text(encoding="utf-8").strip()
    return settings.smtp_password


def _redact_error(exc: Exception) -> str:
    text = exc.__class__.__name__
    return text[:200]


def _message(settings: Settings, payload: dict) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = settings.email_to
    msg["Subject"] = f"JellyFix ticket created: {payload['ticket_id']}"
    body = (
        "A new JellyFix ticket was created.\n\n"
        f"Ticket: {payload['ticket_id']}\n"
        f"Media: {payload['item_name']}\n"
        f"Issue: {payload['issue_type']}\n"
        f"Reporter: {payload['reporter_name']}\n\n"
        f"Message:\n{payload['message']}\n"
    )
    msg.set_content(body)
    return msg


def send_creation_email(settings: Settings, payload: dict) -> None:
    if not settings.smtp_server or not settings.email_to:
        return
    password = _password(settings)
    context = ssl.create_default_context()
    message = _message(settings, payload)
    if settings.smtp_port == 465:
        with smtplib.SMTP_SSL(
            settings.smtp_server,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
            context=context,
        ) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_server, settings.smtp_port, timeout=settings.smtp_timeout_seconds) as server:
            server.starttls(context=context)
            if settings.smtp_user:
                server.login(settings.smtp_user, password)
            server.send_message(message)


def process_outbox_once(db: Database, settings: Settings) -> int:
    sent = 0
    now = iso(utcnow())
    with db.transaction() as conn:
        window_start = iso(utcnow() - timedelta(hours=1))
        sent_last_hour = conn.execute(
            """
            SELECT COUNT(*) FROM notification_outbox
            WHERE status = 'sent' AND updated_at >= ?
            """,
            (window_start,),
        ).fetchone()[0]
        remaining = max(settings.smtp_messages_per_hour - sent_last_hour, 0)
        if remaining <= 0:
            return 0
        rows = conn.execute(
            """
            SELECT * FROM notification_outbox
            WHERE status = 'pending' AND next_attempt_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (now, remaining),
        ).fetchall()

    for row in rows:
        try:
            payload = json.loads(row["payload"])
            send_creation_email(settings, payload)
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE notification_outbox SET status = 'sent', updated_at = ?, last_error = NULL WHERE id = ?",
                    (iso(utcnow()), row["id"]),
                )
            sent += 1
        except Exception as exc:
            attempt = int(row["attempt_count"]) + 1
            if attempt > len(RETRY_DELAYS):
                status = "failed"
                next_attempt = iso(utcnow())
            else:
                status = "pending"
                next_attempt = iso(utcnow() + RETRY_DELAYS[attempt - 1])
            with db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET attempt_count = ?, next_attempt_at = ?, status = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (attempt, next_attempt, status, _redact_error(exc), iso(utcnow()), row["id"]),
                )
    return sent


async def outbox_worker(db: Database, settings: Settings) -> None:
    while True:
        try:
            await asyncio.to_thread(process_outbox_once, db, settings)
        except Exception:
            pass
        await asyncio.sleep(5)
