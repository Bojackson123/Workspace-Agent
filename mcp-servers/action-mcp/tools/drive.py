"""Drive tools for the Action MCP.

All write operations target an explicitly-specified Shared Drive — the
configured ``SHARED_DRIVE_ID`` or a subfolder inside it. The service
account's hidden root drive is never a valid destination.
"""

from typing import Final

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import drive as drive_service
from config import settings

# MIME types used as defaults or constants by the tools below.
FOLDER_MIME: Final = "application/vnd.google-apps.folder"
DEFAULT_DOC_MIME: Final = "application/vnd.google-apps.document"

# Drive list/get fields kept narrow to reduce LLM context bloat.
_FILE_FIELDS: Final = "id, name, mimeType, parents, modifiedTime, webViewLink"

# Common keyword arguments needed to reach Shared Drive items.
_SHARED_DRIVE_KW: Final = {
    "includeItemsFromAllDrives": True,
    "supportsAllDrives": True,
}


def _resolve_parent(parent_folder_id: str | None) -> str:
    """Return *parent_folder_id* if provided, else the Shared Drive root.

    Centralising this prevents tools from accidentally creating files
    parented under the service account's hidden root drive.
    """
    return parent_folder_id or settings().require_shared_drive_id()


def _format_file(item: dict) -> str:
    """Render a single Drive file entry as a compact one-line summary."""
    return (
        f"{item.get('name', '?')} "
        f"(ID: {item.get('id')}, Type: {item.get('mimeType')})"
    )


def search_drive(query: str) -> str:
    """Search the designated Shared Drive for files or folders.

    Args:
        query: A Drive query string, e.g. ``name contains 'report'`` or
            ``mimeType = 'application/vnd.google-apps.folder'``. See
            https://developers.google.com/drive/api/guides/search-files
            for the full grammar.

    Returns:
        A newline-separated list of ``name (ID: ..., Type: ...)`` rows,
        or a message indicating no matches / the encountered error.
    """
    shared_drive_id = settings().require_shared_drive_id()
    try:
        response = drive_service().files().list(
            q=query,
            corpora="drive",
            driveId=shared_drive_id,
            fields=f"nextPageToken, files({_FILE_FIELDS})",
            **_SHARED_DRIVE_KW,
        ).execute()
    except HttpError as exc:
        return f"Error searching drive: {exc}"

    files = response.get("files", [])
    if not files:
        return "No files found."
    return "\n".join(_format_file(f) for f in files)


def list_files(parent_folder_id: str | None = None) -> str:
    """List the immediate children of a folder on the Shared Drive.

    Args:
        parent_folder_id: Optional folder ID. Defaults to the
            Shared Drive root when omitted.
    """
    parent = _resolve_parent(parent_folder_id)
    shared_drive_id = settings().require_shared_drive_id()
    try:
        response = drive_service().files().list(
            q=f"'{parent}' in parents and trashed = false",
            corpora="drive",
            driveId=shared_drive_id,
            fields=f"files({_FILE_FIELDS})",
            **_SHARED_DRIVE_KW,
        ).execute()
    except HttpError as exc:
        return f"Error listing files: {exc}"

    files = response.get("files", [])
    if not files:
        return f"No files found under folder {parent}."
    return "\n".join(_format_file(f) for f in files)


def get_file_metadata(file_id: str) -> str:
    """Fetch metadata for a single file on the Shared Drive."""
    try:
        meta = drive_service().files().get(
            fileId=file_id,
            fields=_FILE_FIELDS,
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error fetching file metadata: {exc}"
    return (
        f"Name: {meta.get('name')}\n"
        f"ID: {meta.get('id')}\n"
        f"Type: {meta.get('mimeType')}\n"
        f"Modified: {meta.get('modifiedTime')}\n"
        f"Link: {meta.get('webViewLink')}"
    )


def create_workspace_file(
    name: str,
    mime_type: str = DEFAULT_DOC_MIME,
    parent_folder_id: str | None = None,
) -> str:
    """Create a new Workspace file (Doc, Sheet, Slide, …) on the Shared Drive.

    Args:
        name: Display name for the new file.
        mime_type: Workspace MIME type. Defaults to Google Docs.
        parent_folder_id: Target folder inside the Shared Drive. If
            omitted, the file is placed at the Shared Drive root.
    """
    try:
        created = drive_service().files().create(
            body={
                "name": name,
                "mimeType": mime_type,
                "parents": [_resolve_parent(parent_folder_id)],
            },
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error creating file: {exc}"
    return (
        f"File '{created.get('name')}' created. "
        f"ID: {created.get('id')} | Link: {created.get('webViewLink')}"
    )


def create_folder(name: str, parent_folder_id: str | None = None) -> str:
    """Create a new folder on the Shared Drive."""
    return create_workspace_file(
        name=name,
        mime_type=FOLDER_MIME,
        parent_folder_id=parent_folder_id,
    )


def copy_file(
    file_id: str,
    new_name: str,
    parent_folder_id: str | None = None,
) -> str:
    """Copy a file into a folder on the Shared Drive."""
    try:
        copied = drive_service().files().copy(
            fileId=file_id,
            body={
                "name": new_name,
                "parents": [_resolve_parent(parent_folder_id)],
            },
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error copying file: {exc}"
    return (
        f"File copied as '{copied.get('name')}'. "
        f"ID: {copied.get('id')} | Link: {copied.get('webViewLink')}"
    )


def move_file(file_id: str, new_parent_folder_id: str) -> str:
    """Move a file into a different folder on the Shared Drive.

    Args:
        file_id: ID of the file to move.
        new_parent_folder_id: Destination folder. Must live inside the
            Shared Drive.
    """
    try:
        current = drive_service().files().get(
            fileId=file_id,
            fields="parents",
            supportsAllDrives=True,
        ).execute()
        previous_parents = ",".join(current.get("parents", []))
        updated = drive_service().files().update(
            fileId=file_id,
            addParents=new_parent_folder_id,
            removeParents=previous_parents,
            fields="id, name, parents",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error moving file: {exc}"
    return (
        f"File '{updated.get('name')}' moved. "
        f"New parents: {', '.join(updated.get('parents', []))}"
    )


def delete_file(file_id: str) -> str:
    """Permanently delete a file from the Shared Drive."""
    try:
        drive_service().files().delete(
            fileId=file_id,
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error deleting file: {exc}"
    return f"File {file_id} deleted."


# Order matters only for human readability when the tools surface as a list.
_TOOLS = (
    search_drive,
    list_files,
    get_file_metadata,
    create_workspace_file,
    create_folder,
    copy_file,
    move_file,
    delete_file,
)


def register(mcp: FastMCP) -> None:
    """Register every Drive tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
