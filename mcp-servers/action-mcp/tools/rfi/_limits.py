"""Caps that keep an RFI structure dump within the parser LLM's context budget."""

from __future__ import annotations

from typing import Final

_MAX_CELLS_PER_SHEET: Final = 600
_MAX_PARAGRAPHS: Final = 400
_MAX_TABLE_ROWS: Final = 200
_MAX_VALUE_LEN: Final = 500
