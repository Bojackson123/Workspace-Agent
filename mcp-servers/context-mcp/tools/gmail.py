"""Gmail tools for the Context MCP (read-only)."""

import base64
import logging

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import gmail as gmail_service
from identity import current_user_email

log = logging.getLogger(__name__)

_BODY_CHAR_LIMIT = 20_000


def _subject(message_payload: dict) -> str:
    """Pull the Subject header out of a Gmail message metadata payload."""
    headers = message_payload.get("payload", {}).get("headers", [])
    return next(
        (h["value"] for h in headers if h.get("name") == "Subject"),
        "No Subject",
    )


def _header(message_payload: dict, name: str) -> str:
    headers = message_payload.get("payload", {}).get("headers", [])
    return next(
        (h["value"] for h in headers if h.get("name", "").lower() == name.lower()),
        "",
    )


def _decode_body(data: str) -> str:
    """Decode a Gmail base64url-encoded body part to UTF-8 text."""
    return base64.urlsafe_b64decode(data.encode("ascii")).decode(
        "utf-8", errors="replace"
    )


def _extract_text(payload: dict) -> str:
    """Walk a Gmail message payload and return the best plain-text body.

    Prefers ``text/plain`` parts; falls back to ``text/html`` (returned
    as-is — the LLM can read HTML fine and stripping it would risk
    hiding structure). Returns an empty string if no text body exists.
    """
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")

    if mime_type == "text/plain" and data:
        return _decode_body(data)

    plain = ""
    html = ""
    for part in payload.get("parts", []) or []:
        text = _extract_text(part)
        if not text:
            continue
        if part.get("mimeType") == "text/plain" and not plain:
            plain = text
        elif part.get("mimeType") == "text/html" and not html:
            html = text

    if plain:
        return plain
    if html:
        return html
    if mime_type == "text/html" and data:
        return _decode_body(data)
    return ""


def search_emails(query: str, max_results: int = 5) -> str:
    """Search the calling user's Gmail using a standard Gmail query.

    Args:
        query: Gmail search expression (e.g. ``from:alice newer_than:7d``).
        max_results: Cap on the number of messages returned. Defaults to 5.

    Returns:
        A newline-separated list of ``- Email ID: ... | Subject: ...`` rows,
        or a message indicating no matches.
    """
    user_email = current_user_email()
    service = gmail_service(user_email)

    try:
        listing = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=max_results,
        ).execute()
    except HttpError as exc:
        log.exception("Gmail list failed for %s: %s", user_email, exc)
        return f"Error searching emails: {exc}"
    except Exception as exc:
        log.exception("Gmail list crashed for %s", user_email)
        return f"Error searching emails: {exc}"

    messages = listing.get("messages", [])
    if not messages:
        return "No emails found matching the query."

    rows: list[str] = []
    for entry in messages:
        try:
            message = service.users().messages().get(
                userId="me",
                id=entry["id"],
                format="metadata",
                metadataHeaders=["Subject"],
            ).execute()
        except HttpError as exc:
            rows.append(f"- Email ID: {entry['id']} | (failed to fetch: {exc})")
            continue
        rows.append(f"- Email ID: {entry['id']} | Subject: {_subject(message)}")

    return "\n".join(rows)


def get_email(email_id: str) -> str:
    """Fetch the full content of a single email by its Gmail message ID.

    Args:
        email_id: The Gmail message ID returned by :func:`search_emails`.

    Returns:
        A string containing the email's headers (From, To, Date, Subject)
        followed by its body. Bodies longer than ~20k characters are
        truncated with a marker so the LLM context isn't blown out by a
        single huge message.
    """
    user_email = current_user_email()
    service = gmail_service(user_email)

    try:
        message = service.users().messages().get(
            userId="me",
            id=email_id,
            format="full",
        ).execute()
    except HttpError as exc:
        log.exception("Gmail get failed for %s id=%s: %s", user_email, email_id, exc)
        return f"Error fetching email {email_id}: {exc}"
    except Exception as exc:
        log.exception("Gmail get crashed for %s id=%s", user_email, email_id)
        return f"Error fetching email {email_id}: {exc}"

    payload = message.get("payload", {})
    body = _extract_text(payload) or "(no text body found)"
    if len(body) > _BODY_CHAR_LIMIT:
        body = body[:_BODY_CHAR_LIMIT] + "\n…(truncated)"

    return (
        f"From: {_header(payload, 'From')}\n"
        f"To: {_header(payload, 'To')}\n"
        f"Date: {_header(payload, 'Date')}\n"
        f"Subject: {_subject(payload)}\n"
        f"\n"
        f"{body}"
    )


_TOOLS = (search_emails, get_email)


def register(mcp: FastMCP) -> None:
    """Register every Gmail tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
