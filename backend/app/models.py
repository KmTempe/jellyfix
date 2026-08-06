from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field, validator


ITEM_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


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
