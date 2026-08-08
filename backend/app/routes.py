from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from .auth import JellyfinClient, JellyfinUnauthorized, JellyfinUnavailable, UserContext, get_current_user, get_jellyfin_client
from .database import Database
from .models import (
    ITEM_ID_RE,
    TICKET_ID_RE,
    CommentCreate,
    StatusUpdate,
    TicketBatchStatusUpdate,
    TicketCreate,
    TicketDeleteBatch,
    TicketStatus,
)
from .repositories import TicketRepository
from .notifications import enqueue_webhook, webhook_signature_valid
from .wizarr import ReplyToUnavailable, WizarrEmailLookup


router = APIRouter(prefix="/api/v1")


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_repo(request: Request) -> TicketRepository:
    return request.app.state.ticket_repo


def get_wizarr_email_lookup(request: Request) -> WizarrEmailLookup:
    return request.app.state.wizarr_email_lookup


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    if request.client:
        return request.client.host
    return "unknown"


@router.get("/healthz")
def healthz():
    return {"status": "ok"}


@router.get("/me")
def me(request: Request, user: UserContext = Depends(get_current_user)):
    return {
        "id": user.user_id,
        "name": user.name,
        "is_admin": user.is_admin,
        "allow_active_ticket_deletion": user.is_admin and request.app.state.settings.allow_active_ticket_deletion,
    }


@router.get("/items/{item_id}/ticket")
def item_ticket(
    item_id: str,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    if not ITEM_ID_RE.fullmatch(item_id):
        raise HTTPException(status_code=422, detail="Invalid item ID")
    with db.connect() as conn:
        ticket = repo.current_user_ticket_for_item(conn, item_id.lower(), user)
    return ticket or {"ticket": None}


@router.post("/tickets")
def create_ticket(
    payload: TicketCreate,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
    jellyfin: JellyfinClient = Depends(get_jellyfin_client),
    wizarr_email_lookup: WizarrEmailLookup = Depends(get_wizarr_email_lookup),
):
    try:
        media = jellyfin.validate_media(user.token, user.user_id, payload.item_id)
    except JellyfinUnauthorized as exc:
        raise HTTPException(status_code=404, detail="Media not found") from exc
    except JellyfinUnavailable as exc:
        raise HTTPException(status_code=503, detail="Jellyfin media validation unavailable") from exc

    contact_email = None
    contact_email_required = request.app.state.settings.wizarr_email_required
    if wizarr_email_lookup.enabled:
        try:
            contact_email = wizarr_email_lookup.lookup(user.user_id, user.name)
        except ReplyToUnavailable:
            # The durable outbox will retry this lookup before sending the notification.
            pass

    with db.transaction() as conn:
        return repo.create_ticket(
            conn,
            user=user,
            media=media,
            issue_type=payload.issue_type.value if hasattr(payload.issue_type, "value") else str(payload.issue_type),
            message=payload.message,
            client_ip=client_ip(request),
            contact_email=contact_email,
            contact_email_required=contact_email_required,
        )


@router.post("/integrations/libredesk/webhook", status_code=202)
async def libredesk_webhook(request: Request):
    body = await request.body()
    if not webhook_signature_valid(request.app.state.settings, body, request.headers.get("x-libredesk-signature")):
        raise HTTPException(status_code=401, detail="Invalid LibreDesk webhook signature")
    try:
        enqueue_webhook(request.app.state.db, body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid LibreDesk webhook payload") from exc
    return Response(status_code=202)


@router.get("/tickets/mine")
def my_tickets(
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    with db.connect() as conn:
        return repo.my_tickets(conn, user, cursor, limit)


@router.get("/tickets/{ticket_id}")
def ticket_details(
    ticket_id: str,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    with db.connect() as conn:
        return repo.ticket_with_comments(conn, ticket_id, user)


@router.post("/tickets/{ticket_id}/comments")
def add_comment(
    ticket_id: str,
    payload: CommentCreate,
    request: Request,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    with db.transaction() as conn:
        return repo.add_comment(conn, ticket_id, user, payload.message, client_ip(request))


@router.patch("/tickets/{ticket_id}/status")
def update_status(
    ticket_id: str,
    payload: StatusUpdate,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    status = payload.status.value if hasattr(payload.status, "value") else str(payload.status)
    with db.transaction() as conn:
        return repo.update_status(conn, ticket_id, user, status)


@router.patch("/tickets/status")
def bulk_update_status(
    payload: TicketBatchStatusUpdate,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    status = payload.status.value if hasattr(payload.status, "value") else str(payload.status)
    with db.transaction() as conn:
        return repo.bulk_update_status(conn, payload.ticket_ids, user, status)


@router.delete("/tickets")
def delete_tickets(
    payload: TicketDeleteBatch,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    with db.transaction() as conn:
        return repo.delete_tickets(conn, payload.ticket_ids, user)


@router.delete("/tickets/{ticket_id}")
def delete_ticket(
    ticket_id: str,
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    if not TICKET_ID_RE.fullmatch(ticket_id):
        raise HTTPException(status_code=422, detail="Invalid ticket ID")
    with db.transaction() as conn:
        return repo.delete_tickets(conn, [ticket_id], user)


@router.get("/admin/tickets")
def admin_tickets(
    status: TicketStatus | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    user: UserContext = Depends(get_current_user),
    db: Database = Depends(get_db),
    repo: TicketRepository = Depends(get_repo),
):
    status_value = status.value if hasattr(status, "value") else status
    with db.connect() as conn:
        return repo.admin_tickets(conn, user, status_value, cursor, limit)
