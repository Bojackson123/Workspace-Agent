"""Google Docs tools for the Action MCP.

These tools operate on documents that already exist on the Shared
Drive — use ``create_workspace_file`` from the Drive tools (or
``create_document`` below) to create one first.
"""

import re
from typing import Final

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import docs as docs_service
from clients import drive as drive_service
from config import settings

from ._retry import retry_on_transient

# Markdown line patterns, recognised by append_markdown.
_HEADING_RE: Final = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE: Final = re.compile(r"^\s*[-*+•]\s+(.*)$")
_BOLD_RE: Final = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_INLINE_CODE_RE: Final = re.compile(r"`([^`]+)`")

# The Docs body always ends with a trailing newline element. Inserting at
# the document's reported end index would land *after* that newline and
# raise an error, so we subtract one to insert before it.
_TRAILING_NEWLINE_OFFSET: Final = 1


def _document_end_index(document_id: str) -> int:
    """Return the insertion index for appending text at the end of a doc."""
    doc = retry_on_transient(lambda: docs_service().documents().get(
        documentId=document_id,
        fields="body(content(endIndex))",
    ).execute())
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
        retry_on_transient(lambda: docs_service().documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {"insertText": {"location": {"index": index}, "text": text}}
                ]
            },
        ).execute())
    except HttpError as exc:
        return f"Error appending text: {exc}"
    return f"Appended {len(text)} characters to {document_id}."


def _inline_styling(content: str) -> tuple[str, list[tuple[int, int]]]:
    """Strip inline markdown from one line → (clean_text, bold_ranges).

    Inline code backticks are removed without styling; bold markers
    (``**..**`` / ``__..__``) are removed and their spans recorded as
    (start, end) offsets within the cleaned line.
    """
    content = _INLINE_CODE_RE.sub(r"\1", content)
    out: list[str] = []
    bold: list[tuple[int, int]] = []
    i, n = 0, len(content)
    while i < n:
        if content.startswith("**", i) or content.startswith("__", i):
            marker = content[i : i + 2]
            end = content.find(marker, i + 2)
            if end != -1:
                inner = content[i + 2 : end]
                start_clean = sum(len(p) for p in out)
                out.append(inner)
                bold.append((start_clean, start_clean + len(inner)))
                i = end + 2
                continue
        out.append(content[i])
        i += 1
    return "".join(out), bold


def _markdown_to_doc_requests(
    markdown: str, start_index: int
) -> list[dict]:
    """Convert *markdown* into Docs ``batchUpdate`` requests.

    Emits one ``insertText`` for the whole cleaned text, then paragraph-level
    styling (headings, bullets) and text-level styling (bold) with absolute
    indices. Indices assume the text lands at *start_index* (the document's
    current end), so styling requests in the same batch reference the
    post-insert positions. Styling requests do not change text length, so the
    indices stay valid across the batch.
    """
    lines = markdown.split("\n")
    clean_lines: list[str] = []
    kinds: list[tuple[str, int]] = []  # ("heading", level) | ("bullet", 0) | ("para", 0)
    bold_abs: list[tuple[int, int]] = []

    pos = start_index
    for raw in lines:
        heading = _HEADING_RE.match(raw)
        bullet = _BULLET_RE.match(raw)
        if heading:
            content, kind = heading.group(2), ("heading", len(heading.group(1)))
        elif bullet:
            content, kind = bullet.group(1), ("bullet", 0)
        else:
            content, kind = raw, ("para", 0)
        clean, bolds = _inline_styling(content)
        for s, e in bolds:
            bold_abs.append((pos + s, pos + e))
        clean_lines.append(clean)
        kinds.append(kind)
        pos += len(clean) + 1  # + the newline that terminates this paragraph

    full_text = "\n".join(clean_lines) + "\n"
    requests: list[dict] = [
        {"insertText": {"location": {"index": start_index}, "text": full_text}}
    ]

    pos = start_index
    for clean, kind in zip(clean_lines, kinds):
        para_start, para_end = pos, pos + len(clean) + 1  # include the newline
        if kind[0] == "heading":
            requests.append({
                "updateParagraphStyle": {
                    "range": {"startIndex": para_start, "endIndex": para_end},
                    "paragraphStyle": {"namedStyleType": f"HEADING_{kind[1]}"},
                    "fields": "namedStyleType",
                }
            })
        elif kind[0] == "bullet":
            requests.append({
                "createParagraphBullets": {
                    "range": {"startIndex": para_start, "endIndex": para_end},
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }
            })
        pos = para_end

    for s, e in bold_abs:
        if e > s:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": s, "endIndex": e},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })
    return requests


def append_markdown(document_id: str, markdown: str) -> str:
    """Append *markdown* to a Google Doc, rendered as native formatting.

    Supports ATX headings (``# … ######``), unordered bullets
    (``-`` / ``*`` / ``•``), bold (``**…**`` / ``__…__``), and inline code.
    Anything else is inserted as plain text. Prefer this over ``append_text``
    for LLM-authored markdown so the document does not show literal ``#`` /
    ``-`` / ``**`` characters.
    """
    try:
        index = _document_end_index(document_id)
        requests = _markdown_to_doc_requests(markdown, index)
        retry_on_transient(lambda: docs_service().documents().batchUpdate(
            documentId=document_id,
            body={"requests": requests},
        ).execute())
    except HttpError as exc:
        # Last resort: never lose the content. Insert the raw text unformatted
        # so the document still gets the notes even if the styled batch failed.
        try:
            index = _document_end_index(document_id)
            retry_on_transient(lambda: docs_service().documents().batchUpdate(
                documentId=document_id,
                body={"requests": [
                    {"insertText": {"location": {"index": index}, "text": markdown}}
                ]},
            ).execute())
        except HttpError as exc2:
            return f"Error appending markdown: {exc2}"
        return (
            f"Appended {len(markdown)} characters as PLAIN TEXT to {document_id} "
            f"(markdown formatting skipped after error: {exc})."
        )
    return f"Appended {len(markdown)} characters of markdown to {document_id}."


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
    append_markdown,
    insert_text,
    replace_text,
)


def register(mcp: FastMCP) -> None:
    """Register every Docs tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
