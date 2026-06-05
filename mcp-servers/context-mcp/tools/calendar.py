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

    event_body: dict = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_datetime, "timeZone": "UTC"},
        "end": {"dateTime": end_datetime, "timeZone": "UTC"},
        "attendees": [{"email": addr} for addr in attendees],
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

    link = event.get("htmlLink", "no-link")
    return f"Calendar event created: {summary!r} — {link}"


_TOOLS = (create_calendar_event,)


def register(mcp: FastMCP) -> None:
    """Register Calendar tools onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
