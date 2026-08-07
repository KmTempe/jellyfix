from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, validator


ITEM_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
TICKET_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class IssueType(str, Enum):
    audio = "audio"
    subtitles = "subtitles"
    video_quality = "video_quality"
    wrong_language = "wrong_language"
    other = "other"


class TicketStatus(str, Enum):
    new = "new"
    in_progress = "in_progress"
    resolved = "resolved"


class StrictModel(BaseModel):
    class Config:
        extra = "forbid"
        use_enum_values = True


class TicketCreate(StrictModel):
    item_id: str
    issue_type: IssueType
    message: str = Field(..., min_length=1, max_length=2000)

    @validator("item_id")
    def validate_item_id(cls, value: str) -> str:
        if not ITEM_ID_RE.fullmatch(value):
            raise ValueError("item_id must be a Jellyfin UUID")
        return value.lower()


class CommentCreate(StrictModel):
    message: str = Field(..., min_length=1, max_length=2000)


class StatusUpdate(StrictModel):
    status: TicketStatus


class TicketDeleteBatch(StrictModel):
    ticket_ids: list[str] = Field(..., min_length=1, max_length=100)

    @validator("ticket_ids")
    def validate_ticket_ids(cls, ticket_ids: list[str]) -> list[str]:
        normalized = [value.lower() for value in ticket_ids]
        if any(not TICKET_ID_RE.fullmatch(value) for value in normalized):
            raise ValueError("ticket_ids must contain UUIDs")
        if len(set(normalized)) != len(normalized):
            raise ValueError("ticket_ids must be unique")
        return normalized


class TicketBatchStatusUpdate(TicketDeleteBatch):
    status: TicketStatus

    @validator("status")
    def validate_batch_status(cls, value: TicketStatus | str) -> TicketStatus | str:
        if value == TicketStatus.new or value == "new":
            raise ValueError("Bulk status updates must target in_progress or resolved")
        return value
