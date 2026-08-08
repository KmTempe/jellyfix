from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings
from .database import Database
from .libredesk import LibredeskClient, LibredeskPermanentError, LibredeskUnavailable
from .repositories import iso, parse_iso, utcnow
from .wizarr import ReplyToUnavailable, WizarrEmailLookup


RETRY_DELAYS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30)]
MAX_RECONCILE_MESSAGE_PAGES = 100


def _redact_error(exc: Exception) -> str:
    match = re.search(r"\b([1-5]\d\d)\b", str(exc))
    suffix = f": HTTP {match.group(1)}" if match else ""
    return f"{exc.__class__.__name__}{suffix}"[:200]


class _SafeLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _same_origin(url: str, expected_origin: str) -> bool:
    parsed = urlsplit(url)
    expected = urlsplit(expected_origin)
    return (
        parsed.scheme in {"http", "https"}
        and not parsed.username
        and not parsed.password
        and parsed.scheme.lower() == expected.scheme.lower()
        and parsed.netloc.lower() == expected.netloc.lower()
    )


def _csat_metadata(payload: dict, settings: Settings, message_uuid: str) -> dict:
    metadata: dict = {"kind": "csat", "source_message_uuid": message_uuid, "actions": []}
    parser = _SafeLinkParser()
    try:
        parser.feed(str(payload.get("content") or ""))
        parser.close()
    except Exception:
        return metadata
    if settings.libredesk_public_url:
        for href in parser.hrefs:
            if _same_origin(href, settings.libredesk_public_url):
                metadata["actions"] = [{"label": "Rate this support", "url": href}]
                break
    return metadata


def _author_name(author: dict, fallback: str) -> str:
    full_name = str(author.get("full_name") or author.get("name") or "").strip()
    if full_name:
        return full_name
    combined = " ".join(
        value for value in (str(author.get("first_name") or "").strip(), str(author.get("last_name") or "").strip()) if value
    )
    return combined or fallback


def _defer(conn, table: str, row_id: str, attempts: int, exc: Exception, *, permanent: bool = False) -> None:
    now = utcnow()
    if permanent:
        status, next_attempt = "failed", now
    else:
        attempt = attempts + 1
        delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
        status, next_attempt, attempts = "pending", now + delay, attempt
    conn.execute(
        f"UPDATE {table} SET attempt_count = ?, next_attempt_at = ?, status = ?, last_error = ?, updated_at = ? WHERE id = ?",
        (attempts, iso(next_attempt), status, _redact_error(exc), iso(now), row_id),
    )


def _receipt(conn, remote_key: str) -> bool:
    try:
        conn.execute("INSERT INTO sync_receipts (provider, remote_key, created_at) VALUES ('libredesk', ?, ?)", (remote_key, iso(utcnow())))
        return True
    except sqlite3.IntegrityError:
        return False


def _outbox_rows(db: Database):
    with db.connect() as conn:
        return conn.execute(
            "SELECT * FROM notification_outbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY created_at ASC LIMIT 20",
            (iso(utcnow()),),
        ).fetchall()


def _mapping(conn, ticket_id: str):
    return conn.execute("SELECT * FROM ticket_integrations WHERE ticket_id = ?", (ticket_id,)).fetchone()


def _process_outbox_row(db: Database, settings: Settings, wizarr: WizarrEmailLookup, libredesk: LibredeskClient, row) -> bool:
    payload = json.loads(row["payload"])
    try:
        if row["event_type"] == "ticket_created":
            contact_email = payload.get("contact_email") or payload.get("reply_to")
            if not contact_email and (payload.get("contact_email_required") or payload.get("reply_to_required")):
                contact_email = wizarr.lookup(str(payload.get("reporter_id") or ""), str(payload.get("reporter_name") or ""))
                payload["contact_email"] = contact_email
                with db.transaction() as conn:
                    conn.execute("UPDATE notification_outbox SET payload = ?, updated_at = ? WHERE id = ?", (json.dumps(payload), iso(utcnow()), row["id"]))
            if not contact_email:
                raise ReplyToUnavailable("Wizarr email is required")
            with db.transaction() as conn:
                mapping = _mapping(conn, row["ticket_id"])
            if not mapping:
                subject = libredesk.conversation_subject(payload)
                matches = libredesk.search_conversations(subject)
                if len(matches) > 1:
                    raise LibredeskUnavailable("Multiple LibreDesk conversations matched the ticket subject")
                remote = matches[0] if matches else libredesk.create_conversation(payload, contact_email)
                with db.transaction() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO ticket_integrations (ticket_id, provider, conversation_id, conversation_uuid, contact_email, created_at, updated_at) VALUES (?, 'libredesk', ?, ?, ?, ?, ?)",
                        (row["ticket_id"], remote.get("id"), remote["uuid"], contact_email, iso(utcnow()), iso(utcnow())),
                    )
                    mapping = _mapping(conn, row["ticket_id"])
            if not mapping:
                raise LibredeskUnavailable("LibreDesk conversation mapping could not be stored")
            libredesk.assign_team(mapping["conversation_uuid"])
            libredesk.add_tag(mapping["conversation_uuid"])
        elif row["event_type"] == "comment_created":
            with db.transaction() as conn:
                mapping = _mapping(conn, row["ticket_id"])
                comment = conn.execute(
                    "SELECT c.author_id, c.author_role, c.is_admin, t.reporter_id FROM comments c JOIN tickets t ON t.id = c.ticket_id WHERE c.id = ?",
                    (payload.get("comment_id"),),
                ).fetchone()
            if not mapping:
                raise LibredeskUnavailable("Waiting for LibreDesk conversation")
            if not comment:
                raise LibredeskPermanentError("Comment for LibreDesk sync was not found")
            sender_type = "contact" if comment["author_id"] == comment["reporter_id"] else "agent"
            if payload.get("sender_type") != sender_type:
                payload["sender_type"] = sender_type
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE notification_outbox SET payload = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(payload), iso(utcnow()), row["id"]),
                    )
            remote = libredesk.send_message(mapping["conversation_uuid"], payload["message"], sender_type, payload.get("comment_id", ""))
            with db.transaction() as conn:
                _receipt(conn, f"message:{remote['uuid']}")
                conn.execute("UPDATE comments SET delivery_status = 'sent' WHERE id = ?", (payload.get("comment_id"),))
        elif row["event_type"] == "status_changed":
            with db.transaction() as conn:
                mapping = _mapping(conn, row["ticket_id"])
            if not mapping:
                raise LibredeskUnavailable("Waiting for LibreDesk conversation")
            remote_status = "Resolved" if payload["status"] == "resolved" else "Open"
            libredesk.update_status(mapping["conversation_uuid"], remote_status)
        else:
            raise LibredeskPermanentError("Unknown outbox event")
        with db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET status = 'sent', last_error = NULL, updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
        return True
    except ReplyToUnavailable as exc:
        with db.transaction() as conn:
            conn.execute("UPDATE notification_outbox SET next_attempt_at = ?, last_error = ?, updated_at = ? WHERE id = ?", (iso(utcnow() + timedelta(minutes=1)), _redact_error(exc), iso(utcnow()), row["id"]))
    except LibredeskPermanentError as exc:
        with db.transaction() as conn:
            _defer(conn, "notification_outbox", row["id"], int(row["attempt_count"]), exc, permanent=True)
            if row["event_type"] == "comment_created":
                conn.execute("UPDATE comments SET delivery_status = 'error' WHERE id = ?", (payload.get("comment_id"),))
    except Exception as exc:
        with db.transaction() as conn:
            _defer(conn, "notification_outbox", row["id"], int(row["attempt_count"]), exc)
            if row["event_type"] == "comment_created":
                conn.execute("UPDATE comments SET delivery_status = 'error' WHERE id = ?", (payload.get("comment_id"),))
    return False


def process_outbox_once(db: Database, settings: Settings, wizarr: WizarrEmailLookup, libredesk: LibredeskClient) -> int:
    return sum(_process_outbox_row(db, settings, wizarr, libredesk, row) for row in _outbox_rows(db))


def enqueue_webhook(db: Database, body: bytes) -> None:
    payload = json.loads(body)
    event_type = str(payload.get("event") or "")
    if event_type not in {"message.created", "conversation.status_changed"}:
        return
    digest = hashlib.sha256(body).hexdigest()
    now = iso(utcnow())
    with db.transaction() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO integration_inbox (id, dedupe_key, event_type, payload, next_attempt_at, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (str(uuid.uuid4()), f"webhook:{digest}", event_type, body.decode("utf-8"), now, now, now),
        )


def webhook_signature_valid(settings: Settings, body: bytes, signature: str | None) -> bool:
    if not signature or not settings.libredesk_webhook_secret_file:
        return False
    try:
        secret = Path(settings.libredesk_webhook_secret_file).read_text(encoding="utf-8").strip().encode("utf-8")
    except OSError:
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _remote_ticket_status(remote_status: str) -> str | None:
    normalized = remote_status.strip().lower()
    if normalized in {"resolved", "closed"}:
        return "resolved"
    if normalized in {"open", "pending", "snoozed"}:
        return "in_progress"
    return None


def _remote_status_timestamp(event: dict, payload: dict) -> str | None:
    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    raw = event.get("timestamp") or payload.get("updated_at") or conversation.get("updated_at")
    if not raw:
        return None
    try:
        return iso(parse_iso(str(raw)))
    except (TypeError, ValueError):
        return None


def _apply_remote_status(conn, ticket, remote_status: str) -> bool:
    target = _remote_ticket_status(remote_status)
    if not target:
        return False
    if ticket["status"] == target:
        return False
    if ticket["status"] == "resolved" and target == "in_progress":
        conflict = conn.execute("SELECT 1 FROM tickets WHERE reporter_id = ? AND jellyfin_item_id = ? AND status IN ('new', 'in_progress') AND id != ?", (ticket["reporter_id"], ticket["jellyfin_item_id"], ticket["id"])).fetchone()
        if conflict:
            return False
    now = iso(utcnow())
    try:
        conn.execute("UPDATE tickets SET status = ?, resolved_at = ?, updated_at = ?, version = version + 1 WHERE id = ?", (target, now if target == "resolved" else None, now, ticket["id"]))
    except sqlite3.IntegrityError:
        return False
    conn.execute("INSERT INTO status_events (id, ticket_id, actor_id, actor_name, before_status, after_status, created_at) VALUES (?, ?, 'libredesk', 'LibreDesk', ?, ?, ?)", (str(uuid.uuid4()), ticket["id"], ticket["status"], target, now))
    return True


def _sync_remote_status(conn, mapping, ticket, remote_status: str, status_at: str | None) -> bool:
    if not _remote_ticket_status(remote_status):
        return False
    previous = mapping["last_remote_status_at"]
    if status_at and previous and status_at <= previous:
        return False
    changed = _apply_remote_status(conn, ticket, remote_status)
    if status_at:
        conn.execute(
            "UPDATE ticket_integrations SET last_remote_status_at = ?, updated_at = ? WHERE ticket_id = ?",
            (status_at, iso(utcnow()), mapping["ticket_id"]),
        )
    return changed


def _process_inbox_row(db: Database, settings: Settings, row) -> None:
    event = json.loads(row["payload"])
    payload = event.get("payload") or {}
    conversation_uuid = str(payload.get("conversation_uuid") or payload.get("conversation", {}).get("uuid") or "")
    if not conversation_uuid:
        raise LibredeskPermanentError("Webhook did not include a conversation UUID")
    with db.transaction() as conn:
        mapping = conn.execute("SELECT * FROM ticket_integrations WHERE conversation_uuid = ?", (conversation_uuid,)).fetchone()
        if not mapping:
            conn.execute("UPDATE integration_inbox SET status = 'ignored', updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
            return
        ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (mapping["ticket_id"],)).fetchone()
        if row["event_type"] == "message.created":
            message_uuid = str(payload.get("uuid") or "")
            if not message_uuid:
                conn.execute("UPDATE integration_inbox SET status = 'ignored', updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
                return
            if payload.get("private") or payload.get("type") == "activity":
                conn.execute("UPDATE integration_inbox SET status = 'ignored', updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
                return
            if not _receipt(conn, f"message:{message_uuid}"):
                conn.execute("UPDATE integration_inbox SET status = 'ignored', updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))
                return
            text = str(payload.get("text_content") or "").strip()
            remote_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
            is_csat = bool(remote_meta.get("is_csat"))
            metadata = _csat_metadata(payload, settings, message_uuid) if is_csat else {"kind": "message", "source_message_uuid": message_uuid, "actions": []}
            if is_csat and not text:
                text = "Please rate your support experience."
            if not text and payload.get("attachments"):
                text = "Attachment received in LibreDesk; open LibreDesk to view it."
            if text:
                sender = str(payload.get("sender_type") or "contact")
                author = payload.get("author") or {}
                author_type = str(author.get("type") or "")
                is_agent = sender == "agent" or author_type == "agent"
                role = "agent" if is_agent else "reporter"
                author_name = _author_name(author, "LibreDesk agent" if is_agent else ticket["reporter_name"])
                author_id = f"libredesk:{payload.get('sender_id', 'agent')}" if is_agent else ticket["reporter_id"]
                conn.execute(
                    "INSERT INTO comments (id, ticket_id, author_id, author_name, is_admin, author_role, message, metadata_json, delivery_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'sent', ?)",
                    (str(uuid.uuid4()), ticket["id"], author_id, author_name[:200], int(is_agent), role, text[:2000], json.dumps(metadata, ensure_ascii=True), iso(utcnow())),
                )
                if is_agent and ticket["status"] == "new":
                    _apply_remote_status(conn, ticket, "Open")
        else:
            remote_status = str(payload.get("new_status") or "")
            _sync_remote_status(conn, mapping, ticket, remote_status, _remote_status_timestamp(event, payload))
        conn.execute("UPDATE integration_inbox SET status = 'processed', last_error = NULL, updated_at = ? WHERE id = ?", (iso(utcnow()), row["id"]))


def process_inbox_once(db: Database, settings: Settings) -> int:
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM integration_inbox WHERE status = 'pending' AND next_attempt_at <= ? ORDER BY created_at ASC LIMIT 20", (iso(utcnow()),)).fetchall()
    processed = 0
    for row in rows:
        try:
            _process_inbox_row(db, settings, row)
            processed += 1
        except LibredeskPermanentError as exc:
            with db.transaction() as conn:
                _defer(conn, "integration_inbox", row["id"], int(row["attempt_count"]), exc, permanent=True)
        except Exception as exc:
            with db.transaction() as conn:
                _defer(conn, "integration_inbox", row["id"], int(row["attempt_count"]), exc)
    return processed


def reconcile_once(db: Database, settings: Settings, libredesk: LibredeskClient) -> None:
    if not libredesk.enabled:
        return
    cutoff = iso(utcnow() - timedelta(seconds=max(settings.libredesk_reconcile_seconds, 30)))
    with db.connect() as conn:
        mappings = conn.execute("SELECT * FROM ticket_integrations WHERE last_reconciled_at IS NULL OR last_reconciled_at <= ? ORDER BY last_reconciled_at ASC LIMIT 20", (cutoff,)).fetchall()
    for mapping in mappings:
        try:
            conversation = libredesk.conversation(mapping["conversation_uuid"])
            with db.transaction() as conn:
                current_mapping = conn.execute("SELECT * FROM ticket_integrations WHERE ticket_id = ?", (mapping["ticket_id"],)).fetchone()
                ticket = conn.execute("SELECT * FROM tickets WHERE id = ?", (mapping["ticket_id"],)).fetchone()
                if current_mapping and ticket:
                    _sync_remote_status(
                        conn,
                        current_mapping,
                        ticket,
                        str(conversation.get("status") or ""),
                        _remote_status_timestamp({}, conversation),
                    )
            for page in range(1, MAX_RECONCILE_MESSAGE_PAGES + 1):
                messages = libredesk.messages(mapping["conversation_uuid"], page=page)
                for message in messages:
                    event = {"event": "message.created", "payload": message}
                    enqueue_webhook(db, json.dumps(event, sort_keys=True).encode())
                if len(messages) < 100:
                    break
            with db.transaction() as conn:
                conn.execute("UPDATE ticket_integrations SET last_reconciled_at = ?, updated_at = ? WHERE ticket_id = ?", (iso(utcnow()), iso(utcnow()), mapping["ticket_id"]))
        except Exception:
            continue


async def outbox_worker(db: Database, settings: Settings, wizarr: WizarrEmailLookup, libredesk: LibredeskClient) -> None:
    counter = 0
    while True:
        try:
            await asyncio.to_thread(process_outbox_once, db, settings, wizarr, libredesk)
            await asyncio.to_thread(process_inbox_once, db, settings)
            counter += 1
            if counter % max(1, settings.libredesk_reconcile_seconds // 5) == 0:
                await asyncio.to_thread(reconcile_once, db, settings, libredesk)
        except Exception:
            pass
        await asyncio.sleep(5)
