from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from .auth import MediaContext, UserContext
from .config import Settings
from .models import TicketStatus


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def row_to_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


class TicketRepository:
    def __init__(self, settings: Settings):
        self.settings = settings

    def ticket_with_comments(self, conn, ticket_id: str, user: UserContext) -> dict[str, Any]:
        ticket = row_to_dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        if not user.is_admin and ticket["reporter_id"] != user.user_id:
            raise HTTPException(status_code=404, detail="Ticket not found")
        comments = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM comments WHERE ticket_id = ? ORDER BY created_at ASC",
                (ticket_id,),
            ).fetchall()
        ]
        return {"ticket": ticket, "comments": comments}

    def current_user_ticket_for_item(self, conn, item_id: str, user: UserContext) -> dict[str, Any] | None:
        ticket = row_to_dict(
            conn.execute(
                "SELECT * FROM tickets WHERE reporter_id = ? AND jellyfin_item_id = ?",
                (user.user_id, item_id),
            ).fetchone()
        )
        if not ticket:
            return None
        return {"ticket": ticket}

    def create_ticket(
        self,
        conn,
        user: UserContext,
        media: MediaContext,
        issue_type: str,
        message: str,
        client_ip: str,
    ) -> dict[str, str]:
        existing = row_to_dict(
            conn.execute(
                "SELECT id FROM tickets WHERE reporter_id = ? AND jellyfin_item_id = ?",
                (user.user_id, media.item_id),
            ).fetchone()
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail={"message": "Ticket already exists", "ticket_id": existing["id"]},
            )

        open_count = conn.execute(
            "SELECT COUNT(*) FROM tickets WHERE reporter_id = ? AND status != 'resolved'",
            (user.user_id,),
        ).fetchone()[0]
        if open_count >= self.settings.open_tickets_per_user:
            raise HTTPException(status_code=429, detail="Too many open tickets", headers={"Retry-After": "3600"})

        pending = conn.execute(
            "SELECT COUNT(*) FROM notification_outbox WHERE status = 'pending'"
        ).fetchone()[0]
        if pending >= self.settings.outbox_pending_cap:
            raise HTTPException(status_code=429, detail="Notification queue is full", headers={"Retry-After": "3600"})

        self._admit_rate(conn, "ticket_create", user.user_id, client_ip)

        now = iso(utcnow())
        ticket_id = str(uuid.uuid4())
        comment_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())
        outbox_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO tickets
            (id, reporter_id, reporter_name, jellyfin_item_id, item_name, issue_type, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
            """,
            (ticket_id, user.user_id, user.name, media.item_id, media.item_name, issue_type, now, now),
        )
        conn.execute(
            """
            INSERT INTO comments
            (id, ticket_id, author_id, author_name, is_admin, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (comment_id, ticket_id, user.user_id, user.name, int(user.is_admin), message, now),
        )
        conn.execute(
            """
            INSERT INTO status_events
            (id, ticket_id, actor_id, actor_name, before_status, after_status, created_at)
            VALUES (?, ?, ?, ?, NULL, 'new', ?)
            """,
            (event_id, ticket_id, user.user_id, user.name, now),
        )
        payload = json.dumps(
            {
                "ticket_id": ticket_id,
                "item_name": media.item_name,
                "issue_type": issue_type,
                "reporter_name": user.name,
                "message": message,
            },
            ensure_ascii=True,
        )
        conn.execute(
            """
            INSERT INTO notification_outbox
            (id, dedupe_key, ticket_id, event_type, payload, next_attempt_at, status, created_at, updated_at)
            VALUES (?, ?, ?, 'ticket_created', ?, ?, 'pending', ?, ?)
            """,
            (outbox_id, f"ticket-created:{ticket_id}", ticket_id, payload, now, now, now),
        )
        return {"id": ticket_id, "status": "new"}

    def add_comment(self, conn, ticket_id: str, user: UserContext, message: str, client_ip: str) -> dict[str, str]:
        ticket = row_to_dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
        if not ticket or (not user.is_admin and ticket["reporter_id"] != user.user_id):
            raise HTTPException(status_code=404, detail="Ticket not found")
        if ticket["status"] == "resolved" and not user.is_admin:
            raise HTTPException(status_code=403, detail="Resolved tickets are locked")

        count = conn.execute("SELECT COUNT(*) FROM comments WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]
        if count >= self.settings.comments_per_ticket:
            raise HTTPException(status_code=429, detail="Too many comments", headers={"Retry-After": "3600"})

        self._admit_rate(conn, "comment", user.user_id, client_ip)
        now = iso(utcnow())
        comment_id = str(uuid.uuid4())
        conn.execute(
            """
            INSERT INTO comments
            (id, ticket_id, author_id, author_name, is_admin, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (comment_id, ticket_id, user.user_id, user.name, int(user.is_admin), message, now),
        )
        return {"id": comment_id, "status": "added"}

    def update_status(self, conn, ticket_id: str, user: UserContext, status: str) -> dict[str, str]:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin required")
        ticket = row_to_dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        before = ticket["status"]
        allowed = {
            "new": {"in_progress", "resolved"},
            "in_progress": {"resolved"},
            "resolved": {"in_progress"},
        }
        if status == before:
            return {"id": ticket_id, "status": before}
        if status not in allowed[before]:
            raise HTTPException(status_code=409, detail="Invalid status transition")

        now = iso(utcnow())
        resolved_at = now if status == "resolved" else None
        conn.execute(
            """
            UPDATE tickets
            SET status = ?, updated_at = ?, resolved_at = ?, version = version + 1
            WHERE id = ?
            """,
            (status, now, resolved_at, ticket_id),
        )
        conn.execute(
            """
            INSERT INTO status_events
            (id, ticket_id, actor_id, actor_name, before_status, after_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (str(uuid.uuid4()), ticket_id, user.user_id, user.name, before, status, now),
        )
        return {"id": ticket_id, "status": status}

    def delete_tickets(self, conn, ticket_ids: list[str], user: UserContext) -> dict[str, list[str]]:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin required")

        placeholders = ", ".join("?" for _ticket_id in ticket_ids)
        rows = conn.execute(f"SELECT id FROM tickets WHERE id IN ({placeholders})", ticket_ids).fetchall()
        found_ids = {row["id"] for row in rows}
        if len(found_ids) != len(ticket_ids):
            raise HTTPException(status_code=404, detail="Ticket not found")

        if not self.settings.allow_active_ticket_deletion:
            active_ticket_ids = [
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM tickets WHERE id IN ({placeholders}) AND status != 'resolved'", ticket_ids
                ).fetchall()
            ]
            if active_ticket_ids:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Selected tickets include new or in-progress tickets. Resolve them first or enable active-ticket deletion.",
                        "active_ticket_ids": active_ticket_ids,
                    },
                )

        # Foreign-key cascades remove comments, status events, and unsent outbox entries atomically.
        conn.execute(f"DELETE FROM tickets WHERE id IN ({placeholders})", ticket_ids)
        return {"deleted_ids": ticket_ids}

    def bulk_update_status(self, conn, ticket_ids: list[str], user: UserContext, status: str) -> dict[str, list[str] | str]:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin required")

        placeholders = ", ".join("?" for _ticket_id in ticket_ids)
        rows = conn.execute(f"SELECT * FROM tickets WHERE id IN ({placeholders})", ticket_ids).fetchall()
        tickets = {row["id"]: dict(row) for row in rows}
        if len(tickets) != len(ticket_ids):
            raise HTTPException(status_code=404, detail="Ticket not found")

        allowed = {
            "new": {"in_progress", "resolved"},
            "in_progress": {"resolved"},
            "resolved": {"in_progress"},
        }
        now = iso(utcnow())
        changed_ids = []
        for ticket_id in ticket_ids:
            ticket = tickets[ticket_id]
            before = ticket["status"]
            if before == status:
                continue
            if status not in allowed[before]:
                raise HTTPException(status_code=409, detail="Invalid status transition")
            resolved_at = now if status == "resolved" else None
            conn.execute(
                """
                UPDATE tickets
                SET status = ?, updated_at = ?, resolved_at = ?, version = version + 1
                WHERE id = ?
                """,
                (status, now, resolved_at, ticket_id),
            )
            conn.execute(
                """
                INSERT INTO status_events
                (id, ticket_id, actor_id, actor_name, before_status, after_status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), ticket_id, user.user_id, user.name, before, status, now),
            )
            changed_ids.append(ticket_id)
        return {"status": status, "updated_ids": changed_ids}

    def admin_tickets(self, conn, user: UserContext, status: str | None, cursor: str | None, limit: int) -> dict[str, Any]:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail="Admin required")
        if status is not None and status not in {member.value for member in TicketStatus}:
            raise HTTPException(status_code=422, detail="Invalid status filter")
        limit = min(max(limit, 1), 100)
        params: list[Any] = []
        where = []
        if status:
            where.append("status = ?")
            params.append(status)
        if cursor:
            where.append("created_at < ?")
            params.append(cursor)
        sql = "SELECT * FROM tickets"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit + 1)
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        next_cursor = rows[limit]["created_at"] if len(rows) > limit else None
        return {"tickets": rows[:limit], "next_cursor": next_cursor}

    def purge_resolved(self, conn) -> int:
        cutoff = iso(utcnow() - timedelta(days=self.settings.retention_days))
        rows = conn.execute(
            "SELECT id FROM tickets WHERE status = 'resolved' AND resolved_at < ?",
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute("DELETE FROM tickets WHERE id = ?", (row["id"],))
        return len(rows)

    def _admit_rate(self, conn, action: str, user_id: str, client_ip: str) -> None:
        if action == "ticket_create":
            limits = [
                ("user", user_id, self.settings.ticket_creations_per_user_hour),
                ("ip", client_ip, self.settings.ticket_creations_per_ip_hour),
                ("global", "global", self.settings.ticket_creations_global_hour),
            ]
        else:
            limits = [
                ("user", user_id, self.settings.comments_per_user_hour),
                ("ip", client_ip, self.settings.comments_per_ip_hour),
                ("global", "global", self.settings.comments_global_hour),
            ]
        window_start = iso(utcnow() - timedelta(hours=1))
        for key_type, key_value, limit in limits:
            count = conn.execute(
                """
                SELECT COUNT(*) FROM rate_events
                WHERE key_type = ? AND key_value = ? AND action = ? AND created_at >= ?
                """,
                (key_type, key_value, action, window_start),
            ).fetchone()[0]
            if count >= limit:
                raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": "3600"})
        now = iso(utcnow())
        for key_type, key_value, _limit in limits:
            conn.execute(
                "INSERT INTO rate_events (id, key_type, key_value, action, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), key_type, key_value, action, now),
            )
