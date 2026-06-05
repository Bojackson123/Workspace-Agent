"""Gmail write tools for the Context MCP.

Provides draft creation only — this server never sends email. Drafts land
in the impersonated user's Gmail account via DWD, so they appear in their
Drafts folder awaiting human review before any send.
"""

import base64
import logging
from email.mime.text import MIMEText

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import gmail as gmail_service
from identity import current_user_email

log = logging.getLogger(__name__)


def create_gmail_draft(to: str, subject: str, body: str) -> str:
    """Create a Gmail draft in the calling user's account.

    The draft is saved but NEVER sent. The user must open Gmail and send
    it manually after reviewing.

    Args:
        to: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.

    Returns:
        A confirmation string containing the draft ID, or an error message.
    """
    user_email = current_user_email()
    service = gmail_service(user_email)

    msg = MIMEText(body)
    msg["to"] = to
    msg["from"] = user_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    try:
        draft = service.users().drafts().create(
            userId="me",
            body={"message": {"raw": raw}},
        ).execute()
    except HttpError as exc:
        log.exception("Gmail draft creation failed for %s: %s", user_email, exc)
        return f"Error creating draft for {to}: {exc}"
    except Exception as exc:
        log.exception("Gmail draft creation crashed for %s", user_email)
        return f"Error creating draft for {to}: {exc}"

    draft_id = draft.get("id", "unknown")
    return f"Draft created (id={draft_id}) to={to} subject={subject!r}"


_TOOLS = (create_gmail_draft,)


def register(mcp: FastMCP) -> None:
    """Register Gmail write tools onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
