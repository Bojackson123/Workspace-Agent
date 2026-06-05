"""Pydantic schemas for the Meeting Action Engine."""

from pydantic import BaseModel

from workflows.common.grounding import SourceRef


class ActionItem(BaseModel):
    id: str
    description: str
    owner: str | None = None  # email or name; None means UNASSIGNED
    due_date: str | None = None  # ISO 8601 date string, e.g. "2026-06-20"
    sources: list[SourceRef] = []  # transcript spans


class Decision(BaseModel):
    id: str
    text: str
    sources: list[SourceRef] = []


class ParsedMeeting(BaseModel):
    title: str
    attendees: list[str]
    decisions: list[Decision]
    action_items: list[ActionItem]


# ── Intermediate planning types (no tools; written to state by fan-out) ────

class EmailDraft(BaseModel):
    owner: str
    subject: str
    body: str


class CalendarHold(BaseModel):
    summary: str
    start_datetime: str  # ISO 8601
    end_datetime: str    # ISO 8601
    attendees: list[str]
    description: str
    action_item_id: str


class TrackerRow(BaseModel):
    id: str
    description: str
    owner: str | None
    due_date: str | None  # ISO 8601 date string
    source_locators: list[str]
