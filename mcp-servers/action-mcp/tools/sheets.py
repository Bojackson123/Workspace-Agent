"""Google Sheets tools for the Action MCP.

Ranges follow A1 notation (e.g. ``Sheet1!A1:C10``). Values are passed
as nested lists where each inner list is a row.
"""

from typing import Final

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import drive as drive_service
from clients import sheets as sheets_service
from config import settings

from ._retry import retry_on_transient

# Use USER_ENTERED so the Sheets value parser interprets formulas, dates,
# and numbers the same way a human typing into the UI would.
_VALUE_INPUT_OPTION: Final = "USER_ENTERED"


def create_spreadsheet(title: str, parent_folder_id: str | None = None) -> str:
    """Create a new Google Sheet on the Shared Drive.

    Created via the Drive API (rather than ``spreadsheets.create``) so the
    file lands in the Shared Drive in a single call — Sheets' native
    ``create`` would place it in the service account's root drive.
    """
    parent = parent_folder_id or settings().require_shared_drive_id()
    try:
        created = drive_service().files().create(
            body={
                "name": title,
                "mimeType": "application/vnd.google-apps.spreadsheet",
                "parents": [parent],
            },
            fields="id, name, webViewLink",
            supportsAllDrives=True,
        ).execute()
    except HttpError as exc:
        return f"Error creating spreadsheet: {exc}"
    return (
        f"Spreadsheet '{created.get('name')}' created. "
        f"ID: {created.get('id')} | Link: {created.get('webViewLink')}"
    )


def read_range(spreadsheet_id: str, range_a1: str) -> str:
    """Read a rectangular range from a spreadsheet.

    Args:
        spreadsheet_id: The spreadsheet's file ID.
        range_a1: A range in A1 notation, e.g. ``Sheet1!A1:C10``.

    Returns:
        The range rendered as tab-separated rows, one per line, or a
        message if the range is empty.
    """
    try:
        response = sheets_service().spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
        ).execute()
    except HttpError as exc:
        return f"Error reading range: {exc}"

    rows = response.get("values", [])
    if not rows:
        return f"Range {range_a1!r} is empty."
    return "\n".join("\t".join(str(cell) for cell in row) for row in rows)


def write_range(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list[str]],
) -> str:
    """Overwrite a range with *values*.

    Args:
        values: Rows of cell values. Inner lists may contain strings,
            numbers, or formula strings (``"=SUM(A1:A10)"``).
    """
    try:
        response = retry_on_transient(lambda: sheets_service().spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=_VALUE_INPUT_OPTION,
            body={"values": values},
        ).execute())
    except HttpError as exc:
        return f"Error writing range: {exc}"
    return (
        f"Wrote {response.get('updatedCells', 0)} cell(s) "
        f"to {response.get('updatedRange', range_a1)}."
    )


def append_rows(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list[str]],
) -> str:
    """Append rows after the last row of data in *range_a1*.

    ``range_a1`` is used only to locate the target table — the actual
    write happens at the first empty row beneath the existing data.
    """
    try:
        response = retry_on_transient(lambda: sheets_service().spreadsheets().values().append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=_VALUE_INPUT_OPTION,
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute())
    except HttpError as exc:
        return f"Error appending rows: {exc}"
    updates = response.get("updates", {})
    return (
        f"Appended {updates.get('updatedRows', 0)} row(s) "
        f"to {updates.get('updatedRange', range_a1)}."
    )


def clear_range(spreadsheet_id: str, range_a1: str) -> str:
    """Clear every cell in *range_a1* (values only — formatting is preserved)."""
    try:
        response = sheets_service().spreadsheets().values().clear(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            body={},
        ).execute()
    except HttpError as exc:
        return f"Error clearing range: {exc}"
    return f"Cleared {response.get('clearedRange', range_a1)}."


def add_sheet(spreadsheet_id: str, title: str) -> str:
    """Add a new tab to an existing spreadsheet."""
    try:
        response = sheets_service().spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "requests": [
                    {"addSheet": {"properties": {"title": title}}}
                ]
            },
        ).execute()
    except HttpError as exc:
        return f"Error adding sheet: {exc}"
    properties = (
        response.get("replies", [{}])[0]
        .get("addSheet", {})
        .get("properties", {})
    )
    return (
        f"Added sheet '{properties.get('title', title)}' "
        f"(ID: {properties.get('sheetId')})."
    )


_TOOLS = (
    create_spreadsheet,
    read_range,
    write_range,
    append_rows,
    clear_range,
    add_sheet,
)


def register(mcp: FastMCP) -> None:
    """Register every Sheets tool defined in this module onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
