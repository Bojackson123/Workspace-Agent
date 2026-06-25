"""RFI file tools for the Action MCP — parse and fill customer templates.

These tools let the RFI Response Engine work with a customer's *native*
``.xlsx`` / ``.docx`` RFI document instead of converting it to a Google-native
format, so the filled result is byte-for-byte the customer's own template.

Three tools, all keyed by a Shared-Drive file id:

* ``upload_binary_file`` — store base64 bytes (a Chat attachment the backend
  downloaded) as a new file on the Shared Drive; returns its id.
* ``dump_rfi_structure`` — download the file and dump its raw structure
  (spreadsheet cells with addresses, or Word tables/paragraphs with indices)
  so the parser LLM can choose precise answer write-back anchors.
* ``fill_rfi_answers`` — download the file, write each answer into its recorded
  location, and upsert the result as a "… — Sanmina Response" file.

Unlike the Docs/Sheets tools, these return **JSON strings** because they are
driven deterministically by the backend (parsed in Python), not narrated to an
LLM. Format-specific dump/fill lives in :mod:`xlsx` and :mod:`docx`; Drive
binary I/O in :mod:`binary_io`; markdown stripping in :mod:`_markdown`.
"""

from __future__ import annotations

import json
import logging
import os

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from .binary_io import (
    DOCX_MIME,
    XLSX_MIME,
    _download_bytes,
    _file_meta,
    _is_docx,
    _is_xlsx,
    _upsert_bytes,
    upload_binary_file,
)
from .docx import _dump_docx, _fill_docx
from .xlsx import _dump_xlsx, _fill_xlsx

log = logging.getLogger(__name__)


def dump_rfi_structure(file_id: str) -> str:
    """Dump an RFI file's raw structure with precise write-back anchors.

    Returns JSON describing the spreadsheet cells — each with its real address
    (e.g. ``"Responses!B5"``) — or the Word tables/paragraphs (with table, row,
    and column indices). An LLM parser reads this to identify the questions and
    choose, FROM THESE REAL ANCHORS, where each answer should be written, so
    write-back stays reliable across arbitrarily-structured customer RFIs.

    Anchor formats ``fill_rfi_answers`` expects back:
      - spreadsheet cell: ``"<SheetName>!<A1>"``   (e.g. ``"Responses!C7"``)
      - Word table cell:  ``"tbl-<t>!r<row>c<col>"`` (0-based indices)
      - Word paragraph:   ``"para-<index>"``        (answer inserted next line)
    """
    try:
        meta = _file_meta(file_id)
        data = _download_bytes(file_id)
    except HttpError as exc:
        return json.dumps({"error": f"Could not read file: {exc}"})

    name, mime = meta.get("name", ""), meta.get("mimeType", "")
    try:
        if _is_xlsx(name, mime):
            structure = _dump_xlsx(data)
        elif _is_docx(name, mime):
            structure = _dump_docx(data)
        else:
            return json.dumps({
                "error": f"Unsupported file type: {name!r} ({mime}). "
                         "Attach an .xlsx or .docx RFI."
            })
    except Exception as exc:  # noqa: BLE001 — surface parse failures as JSON
        log.exception("rfi.dump failed for %s", file_id)
        return json.dumps({"error": f"Failed to read {name!r}: {exc}"})

    structure["file_name"] = name
    return json.dumps(structure)


def _response_name(original: str) -> str:
    """Derive the filled-file name: ``RFI.xlsx`` → ``RFI — Sanmina Response.xlsx``."""
    stem, ext = os.path.splitext(original)
    return f"{stem} — Sanmina Response{ext}"


def fill_rfi_answers(
    file_id: str,
    answers_json: str,
    response_file_id: str | None = None,
    parent_folder_id: str | None = None,
) -> str:
    """Write answers into the RFI template and upsert a filled copy.

    Args:
        file_id: Shared-Drive id of the original RFI file.
        answers_json: JSON array of ``{"location": <answer_location>,
            "answer": <text>}`` objects. ``location`` values come from
            ``dump_rfi_structure``' anchors.
        response_file_id: id of a response file a previous fill produced. When
            given, its content is overwritten in place instead of creating a new
            file — so retries/re-runs converge onto one file. Idempotent even
            without it: a same-named response file in the parent is reused.

    Returns a JSON object ``{file_id, name, link, written}``.
    """
    try:
        answers = json.loads(answers_json)
        if not isinstance(answers, list):
            raise ValueError("answers_json must be a JSON array")
    except (json.JSONDecodeError, ValueError) as exc:
        return json.dumps({"error": f"Invalid answers_json: {exc}"})

    try:
        meta = _file_meta(file_id)
        data = _download_bytes(file_id)
    except HttpError as exc:
        return json.dumps({"error": f"Could not read file: {exc}"})

    name, mime = meta.get("name", ""), meta.get("mimeType", "")
    try:
        if _is_xlsx(name, mime):
            filled, out_mime = _fill_xlsx(data, answers), XLSX_MIME
        elif _is_docx(name, mime):
            filled, out_mime = _fill_docx(data, answers), DOCX_MIME
        else:
            return json.dumps({"error": f"Unsupported file type: {name!r} ({mime})."})
    except Exception as exc:  # noqa: BLE001
        log.exception("rfi.fill failed for %s", file_id)
        return json.dumps({"error": f"Failed to fill {name!r}: {exc}"})

    try:
        result = _upsert_bytes(
            _response_name(name), out_mime, filled, response_file_id, parent_folder_id
        )
    except HttpError as exc:
        return json.dumps({"error": f"Upload of filled file failed: {exc}"})
    result["written"] = len(answers)
    return json.dumps(result)


_TOOLS = (
    upload_binary_file,
    dump_rfi_structure,
    fill_rfi_answers,
)


def register(mcp: FastMCP) -> None:
    """Register every RFI file tool defined in this package onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
