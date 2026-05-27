"""Docs tools for the Context MCP (read-only)."""

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import docs as docs_service
from identity import current_user_email


def read_my_document(document_id: str) -> str:
    """Return the plain-text contents of a Google Doc the user can access.

    Inline objects, tables, and other structural elements are skipped —
    only paragraph text runs contribute to the output.
    """
    user_email = current_user_email()
    try:
        doc = docs_service(user_email).documents().get(
            documentId=document_id,
        ).execute()
    except HttpError as exc:
        return f"Error reading document: {exc}"

    parts: list[str] = []
    for element in doc.get("body", {}).get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for run in paragraph.get("elements", []):
            text_run = run.get("textRun")
            if text_run and "content" in text_run:
                parts.append(text_run["content"])
    return "".join(parts).rstrip() or "(document is empty)"


_TOOLS = (read_my_document,)


def register(mcp: FastMCP) -> None:
    """Register every Docs tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
