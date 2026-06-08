"""Calendar tools for the Context MCP.

Creates calendar events in the impersonated user's primary calendar via DWD.
Events are created as regular calendar entries — the user can edit or delete
them from their Google Calendar before accepting/sending invites.
"""

import logging

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import calendar as calendar_service
from identity import current_user_email

log = logging.getLogger(__name__)


def _to_attendee_email(raw: str) -> str | None:
    """Extract a bare email from an attendee string, or ``None`` if there isn't one.

    The Calendar API rejects anything that is not a plain address (e.g.
    ``"Sarah Chen (sarah@corp.com)"`` or a name-only ``"Sarah Chen"``). We accept
    ``"Name (email)"`` and bare ``"email"`` and drop everything else so one bad
    attendee can't fail the whole event.
    """
    raw = raw.strip()
    if raw.endswith(")") and "(" in raw:
        inner = raw[raw.rfind("(") + 1 : -1].strip()
        return inner if "@" in inner else None
    return raw if "@" in raw and " " not in raw else None


def create_calendar_event(
    summary: str,
    start_datetime: str,
    end_datetime: str,
    attendees: list[str],
    description: str = "",
) -> str:
    """Create a Google Calendar event in the calling user's primary calendar.

    Args:
        summary: Event title.
        start_datetime: ISO 8601 datetime string, e.g. ``"2025-06-10T14:00:00"``.
            Assumed to be in the user's local timezone if no offset is given.
        end_datetime: ISO 8601 datetime string for the event end.
        attendees: List of attendee email addresses.
        description: Optional event description / agenda.

    Returns:
        A confirmation string containing the event link, or an error message.
    """
    user_email = current_user_email()
    service = calendar_service(user_email)

    # Keep only valid addresses; a single malformed attendee 400s the whole call.
    valid_emails: list[str] = []
    seen: set[str] = set()
    for raw in attendees:
        email = _to_attendee_email(raw)
        if email is None:
            log.warning("Dropping non-email calendar attendee %r", raw)
            continue
        if email.lower() not in seen:
            seen.add(email.lower())
            valid_emails.append(email)

    event_body: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": "UTC"},
        "end": {"dateTime": end_datetime, "timeZone": "UTC"},
        "attendees": [{"email": addr} for addr in valid_emails],
    }

    try:
        event = service.events().insert(
            calendarId="primary",
            body=event_body,
            sendUpdates="none",  # don't auto-send invites; human reviews first
        ).execute()
    except HttpError as exc:
        log.exception("Calendar event creation failed for %s: %s", user_email, exc)
        return f"Error creating calendar event '{summary}': {exc}"
    except Exception as exc:
        log.exception("Calendar event creation crashed for %s", user_email)
        return f"Error creating calendar event '{summary}': {exc}"

    event_id = event.get("id", "unknown")
    link = event.get("htmlLink", "no-link")
    # Include the id in a stable, parseable form so deterministic callers can
    # map an action item to the event they just created (for later patching).
    return f"Calendar event created: {summary!r} — id={event_id} — {link}"


def update_calendar_event_attendees(
    event_id: str,
    attendee_emails: list[str],
) -> str:
    """Add attendees to an existing event on the user's primary calendar.

    Merges *attendee_emails* into the event's current attendee list (existing
    attendees are preserved, duplicates ignored) via ``events.patch`` with
    ``sendUpdates="none"`` so no invite emails are auto-sent. Invalid (non-email)
    entries are dropped. Used by the post-meeting "invite people" dialog.

    Args:
        event_id: The calendar event id (as returned by create_calendar_event).
        attendee_emails: Email addresses to add as attendees.

    Returns:
        A confirmation string, or an error message.
    """
    user_email = current_user_email()
    service = calendar_service(user_email)

    new_emails = []
    seen: set[str] = set()
    for raw in attendee_emails:
        email = _to_attendee_email(raw)
        if email and email.lower() not in seen:
            seen.add(email.lower())
            new_emails.append(email)
    if not new_emails:
        return f"No valid attendee emails to add to event {event_id}."

    try:
        existing = service.events().get(
            calendarId="primary", eventId=event_id,
        ).execute()
        current = existing.get("attendees", []) or []
        current_emails = {
            a.get("email", "").lower() for a in current if a.get("email")
        }
        merged = list(current)
        for email in new_emails:
            if email.lower() not in current_emails:
                merged.append({"email": email})

        service.events().patch(
            calendarId="primary",
            eventId=event_id,
            body={"attendees": merged},
            sendUpdates="none",  # don't auto-send invites; human reviews first
        ).execute()
    except HttpError as exc:
        log.exception("Calendar attendee update failed for %s: %s", user_email, exc)
        return f"Error adding attendees to event {event_id}: {exc}"
    except Exception as exc:
        log.exception("Calendar attendee update crashed for %s", user_email)
        return f"Error adding attendees to event {event_id}: {exc}"

    return f"Added {len(new_emails)} attendee(s) to event {event_id}."


_TOOLS = (create_calendar_event, update_calendar_event_attendees)


def register(mcp: FastMCP) -> None:
    """Register Calendar tools onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
