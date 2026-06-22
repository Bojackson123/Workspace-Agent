"""Shared-Drive binary I/O for the RFI tools, plus ``upload_binary_file``.

Download/upload/upsert helpers keyed by a Shared-Drive file id, with the
create-or-overwrite logic that makes ``fill_rfi_answers`` idempotent.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from typing import Final

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from clients import drive as drive_service

from .._retry import retry_on_transient
from ..drive import _resolve_parent  # reuse Shared-Drive parent resolution

log = logging.getLogger(__name__)

XLSX_MIME: Final = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DOCX_MIME: Final = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _download_bytes(file_id: str) -> bytes:
    """Download a Shared-Drive file's raw bytes."""
    request = drive_service().files().get_media(
        fileId=file_id, supportsAllDrives=True
    )
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _status, done = retry_on_transient(downloader.next_chunk)
    return buffer.getvalue()


def _file_meta(file_id: str) -> dict:
    """Return ``{name, mimeType}`` for a Shared-Drive file."""
    return drive_service().files().get(
        fileId=file_id, fields="name, mimeType", supportsAllDrives=True
    ).execute()


def _upload_bytes(
    name: str, mime_type: str, data: bytes, parent_folder_id: str | None
) -> dict:
    """Create a new Shared-Drive file from *data*; return id/name/link dict."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    created = retry_on_transient(lambda: drive_service().files().create(
        body={"name": name, "parents": [_resolve_parent(parent_folder_id)]},
        media_body=media,
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute())
    return {
        "file_id": created.get("id"),
        "name": created.get("name"),
        "link": created.get("webViewLink"),
    }


def _find_file_in_parent(name: str, parent_folder_id: str | None) -> str | None:
    """Return the id of an existing non-trashed file named *name* under the parent.

    Lets ``fill_rfi_answers`` be idempotent: repeated calls (a client transport
    retry, or a pipeline re-run) converge onto the one "… — Sanmina Response"
    file instead of creating a fresh duplicate each time.
    """
    parent = _resolve_parent(parent_folder_id)
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{escaped}' and '{parent}' in parents and trashed = false"
    resp = retry_on_transient(lambda: drive_service().files().list(
        q=query,
        fields="files(id)",
        corpora="allDrives",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=1,
    ).execute())
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def _update_bytes(file_id: str, mime_type: str, data: bytes) -> dict:
    """Overwrite an existing Shared-Drive file's content; return id/name/link."""
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
    updated = retry_on_transient(lambda: drive_service().files().update(
        fileId=file_id,
        media_body=media,
        fields="id, name, webViewLink",
        supportsAllDrives=True,
    ).execute())
    return {
        "file_id": updated.get("id"),
        "name": updated.get("name"),
        "link": updated.get("webViewLink"),
    }


def _upsert_bytes(
    name: str,
    mime_type: str,
    data: bytes,
    response_file_id: str | None,
    parent_folder_id: str | None,
) -> dict:
    """Create-or-overwrite the response file so repeated fills don't duplicate.

    Prefers the caller-supplied *response_file_id* (the file a prior fill
    produced); falls back to a name lookup in the parent, then to a fresh
    upload. A stale *response_file_id* (file deleted → 404) degrades to the
    lookup/create path rather than failing.
    """
    if response_file_id:
        try:
            return _update_bytes(response_file_id, mime_type, data)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            log.warning(
                "rfi.fill: response file %s missing; recreating", response_file_id
            )
    existing_id = _find_file_in_parent(name, parent_folder_id)
    if existing_id:
        return _update_bytes(existing_id, mime_type, data)
    return _upload_bytes(name, mime_type, data, parent_folder_id)


def _is_xlsx(name: str, mime: str) -> bool:
    return name.lower().endswith(".xlsx") or "spreadsheet" in (mime or "")


def _is_docx(name: str, mime: str) -> bool:
    return name.lower().endswith(".docx") or "wordprocessing" in (mime or "")


def upload_binary_file(
    name: str,
    mime_type: str,
    content_b64: str,
    parent_folder_id: str | None = None,
) -> str:
    """Upload base64-encoded *content_b64* as a new Shared-Drive file.

    Used by the backend to persist a Chat attachment (the customer's RFI) so the
    other RFI tools can read and rewrite it. Returns a JSON object with the new
    ``file_id``, ``name``, and ``link``.
    """
    try:
        data = base64.b64decode(content_b64)
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"Invalid base64 content: {exc}"})
    try:
        result = _upload_bytes(name, mime_type, data, parent_folder_id)
    except (HttpError, OSError) as exc:
        return json.dumps({"error": f"Upload failed: {exc}"})
    return json.dumps(result)
