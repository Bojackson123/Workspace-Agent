"""Google Docs tools for the Action MCP.

These tools operate on documents that already exist on the Shared
Drive — use ``create_workspace_file`` from the Drive tools (or
``create_document`` below) to create one first.
"""

from typing import Final

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import docs as docs_service
from clients import drive as drive_service
from config import settings

# The Docs body always ends with a trailing newline element. Inserting at
# the document's reported end index would land *after* that newline and
# raise an error, so we subtract one to insert before it.
_TRAILING_NEWLINE_OFFSET: Final = 1


def _document_end_index(document_id: str) -> int:
    """Return the insertion index for appending text at the end of a doc."""
    doc = docs_service().documents().get(
        documentId=document_id,
        fields="body(content(endIndex))",
    ).execute()
    content = doc.get("body", {}).get("content", [])
    if not content:
        # Empty doc — the only valid insertion index is 1.
        return 1
    return content[-1].get("endIndex", 1) - _TRAILING_NEWLINE_OFFSET


def create_document(title: str, parent_folder_id: str | None = None) -> str:
    """Create a new Google Doc on the Shared Drive.

    A thin wrapper over the Drive ``files.create`` call — provided for
    convenience so the LLM does not need to know the Doc MIME type.
    """
    parent = parent_folder_id or settings().require_shared_drive_id()
    try:
        created = drive_service().files().create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.document",
                "parents": [parent],
            },
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error creating document: {exc}"
    return (
        f"Document '{created.get('name')}' created. "
        f"ID: {created.get('id')} | Link: {created.get('webViewLink')}"
    )


def read_document(document_id: str) -> str:
    """Return the plain-text contents of a Google Doc.

    Inline objects, tables, and other structural elements are skipped —
    only paragraph text runs contribute to the output.
    """
    try:
        doc = docs_service().documents().get(documentId=document_id).execute()
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


def append_text(document_id: str, text: str) -> str:
    """Append *text* to the end of a Google Doc."""
    try:
        index = _document_end_index(document_id)
        docs_service().documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"insertText": {"location": {"index": index}, "text": text}}
                ]
            },
        ).execute()
    except HttpError as exc:
        return f"Error appending text: {exc}"
    return f"Appended {len(text)} characters to {document_id}."


def insert_text(document_id: str, index: int, text: str) -> str:
    """Insert *text* at *index* within a Google Doc.

    Args:
        index: 1-based character offset. Index 1 is the start of the body.
    """
    if index < 1:
        return "Error: index must be >= 1 (1 is the start of the document body)."
    try:
        docs_service().documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"insertText": {"location": {"index": index}, "text": text}}
                ]
            },
        ).execute()
    except HttpError as exc:
        return f"Error inserting text: {exc}"
    return f"Inserted {len(text)} characters at index {index}."


def replace_text(
    document_id: str,
    find: str,
    replace: str,
    match_case: bool = True,
) -> str:
    """Replace every occurrence of *find* with *replace* in a Google Doc."""
    try:
        response = docs_service().documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "replaceAllText": {
                            "containsText": {"text": find, "matchCase": match_case},
                            "replaceText": replace,
                        }
                    }
                ]
            },
        ).execute()
    except HttpError as exc:
        return f"Error replacing text: {exc}"

    # The reply mirrors the requests array; pull out the count for feedback.
    replies = response.get("replies", [])
    occurrences = (
        replies[0].get("replaceAllText", {}).get("occurrencesChanged", 0)
        if replies
        else 0
    )
    return f"Replaced {occurrences} occurrence(s) of {find!r}."


_TOOLS = (
    create_document,
    read_document,
    append_text,
    insert_text,
    replace_text,
)


def register(mcp: FastMCP) -> None:
    """Register every Docs tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
