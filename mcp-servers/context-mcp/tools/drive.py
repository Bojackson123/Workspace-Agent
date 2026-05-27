"""Drive tools for the Context MCP (read-only)."""

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import drive as drive_service
from identity import current_user_email

# Narrow field selection — keeps the LLM context focused on what matters.
_FILE_FIELDS = "id, name, mimeType, modifiedTime, owners(emailAddress), webViewLink"


def search_my_drive(query: str, max_results: int = 10) -> str:
    """Search the calling user's Drive using a standard Drive query.

    Args:
        query: Drive query string. See
            https://developers.google.com/drive/api/guides/search-files.
        max_results: Cap on the number of results returned.
    """
    user_email = current_user_email()
    try:
        response = drive_service(user_email).files().list(
            q=query,
            pageSize=max_results,
            fields=f"files({_FILE_FIELDS})",
        ).execute()
    except HttpError as exc:
        return f"Error searching drive: {exc}"

    files = response.get("files", [])
    if not files:
        return "No files found."
    return "\n".join(
        f"{f.get('name', '?')} (ID: {f.get('id')}, Type: {f.get('mimeType')})"
        for f in files
    )


def get_my_file_metadata(file_id: str) -> str:
    """Fetch metadata for a single file in the calling user's Drive."""
    user_email = current_user_email()
    try:
        meta = drive_service(user_email).files().get(
            fileId=file_id,
            fields=_FILE_FIELDS,
        ).execute()
    except HttpError as exc:
        return f"Error fetching file metadata: {exc}"
    owners = ", ".join(o.get("emailAddress", "?") for o in meta.get("owners", []))
    return (
        f"Name: {meta.get('name')}\n"
        f"ID: {meta.get('id')}\n"
        f"Type: {meta.get('mimeType')}\n"
        f"Owners: {owners}\n"
        f"Modified: {meta.get('modifiedTime')}\n"
        f"Link: {meta.get('webViewLink')}"
    )


_TOOLS = (search_my_drive, get_my_file_metadata)


def register(mcp: FastMCP) -> None:
    """Register every Drive tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
