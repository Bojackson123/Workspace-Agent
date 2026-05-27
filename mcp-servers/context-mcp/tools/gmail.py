"""Gmail tools for the Context MCP (read-only)."""

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import gmail as gmail_service
from identity import current_user_email


def _subject(message_payload: dict) -> str:
    """Pull the Subject header out of a Gmail message metadata payload."""
    headers = message_payload.get("payload", {}).get("headers", [])
    return next(
        (h["value"] for h in headers if h.get("name") == "Subject"),
        "No Subject",
    )


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


_TOOLS = (search_emails,)


def register(mcp: FastMCP) -> None:
    """Register every Gmail tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
