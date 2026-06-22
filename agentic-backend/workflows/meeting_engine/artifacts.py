"""Deterministic fan-out builders for the meeting pipeline.

These derive the follow-up artefacts directly from a ParsedMeeting value.
They are pure functions over the *patched* object, so an owner or due-date
supplied via the card form is always reflected — unlike an LLM that would
read the original (null-owner) parse from conversation history.
"""

from __future__ import annotations

from workflows.meeting_engine._identity import _identity_keys
from workflows.meeting_engine.schemas import (
    ActionItem,
    CalendarHold,
    EmailDraft,
    ParsedMeeting,
    TrackerRow,
)


def _short_name(owner: str) -> str:
    """Best-effort display name: strip a trailing ``(email)`` if present."""
    return owner.split("(")[0].strip() or owner


def _resolve_recipient(owner: str, attendees: list[str]) -> str:
    """Resolve an owner to a sendable recipient by matching the attendee list.

    Owners are stored as display names ("Sarah Chen"), but ``create_gmail_draft``
    needs an address. The attendee list carries the full "Name (email)" form, so
    we match the owner against it and return that fuller string — which the Gmail
    tool turns into "Name <email>". Falls back to the owner unchanged if no
    attendee matches (e.g. no email was ever captured).
    """
    owner_keys = _identity_keys(owner)
    for attendee in attendees:
        if owner_keys & _identity_keys(attendee):
            return attendee
    return owner


def _build_email_drafts(parsed: ParsedMeeting) -> list[EmailDraft]:
    """One email per owner, listing all of that owner's action items.

    Items with no owner are skipped (there is no one to send to). Owners are
    kept in first-seen order so output is stable across runs.
    """
    by_owner: dict[str, list[ActionItem]] = {}
    for item in parsed.action_items:
        if not item.owner:
            continue
        by_owner.setdefault(item.owner, []).append(item)

    drafts: list[EmailDraft] = []
    for owner, items in by_owner.items():
        bullet_lines = [
            f"• {it.description} (due: {it.due_date or 'TBD'})" for it in items
        ]
        lead = "is your action item" if len(items) == 1 else "are your action items"
        body = (
            f"Hi {_short_name(owner)},\n\n"
            f"Following up on \"{parsed.title}\". Here {lead}:\n\n"
            + "\n".join(bullet_lines)
            + "\n\nThanks!"
        )
        drafts.append(
            EmailDraft(
                owner=owner,
                to=_resolve_recipient(owner, parsed.attendees),
                subject=f"Action items from {parsed.title}",
                body=body,
            )
        )
    return drafts


def _build_calendar_holds(parsed: ParsedMeeting) -> list[CalendarHold]:
    """A 30-minute 09:00 reminder hold for every item that has a due_date.

    Holds are created with NO attendees — they are personal reminders on the
    triggerer's own calendar. Inviting other people is an explicit, opt-in step
    handled afterward by the "invite people" dialog (which patches the events),
    so /meeting never adds anyone to an event without the user choosing to.
    """
    holds: list[CalendarHold] = []
    for item in parsed.action_items:
        if not item.due_date:
            continue
        holds.append(
            CalendarHold(
                summary=f"Reminder: {item.description}",
                start_datetime=f"{item.due_date}T09:00:00",
                end_datetime=f"{item.due_date}T09:30:00",
                attendees=[],
                description=(
                    f"Auto-created reminder for action item {item.id}.\n"
                    f"Owner: {item.owner or 'UNASSIGNED'}\n"
                    f"From meeting: {parsed.title}"
                ),
                action_item_id=item.id,
            )
        )
    return holds


def _build_tracker_rows(parsed: ParsedMeeting) -> list[TrackerRow]:
    """One tracker row per action item (owner/due_date may be null)."""
    return [
        TrackerRow(
            id=item.id,
            description=item.description,
            owner=item.owner,
            due_date=item.due_date,
            source_locators=[s.locator for s in item.sources],
        )
        for item in parsed.action_items
    ]
