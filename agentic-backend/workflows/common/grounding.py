"""Source-reference types and provenance validation.

Every quantitative claim in agent output must carry at least one SourceRef.
``validate_grounding`` is pure Python and called by gate agents — no LLM.
"""

from typing import Literal

from pydantic import BaseModel


class SourceRef(BaseModel):
    kind: Literal["sheet_cell", "doc_span", "transcript_span", "drive_file"]
    locator: str  # e.g. "Budget!B12:B14", "doc#para-37", "00:14:22-00:15:01"
    quote: str | None = None  # short supporting excerpt


class Claim(BaseModel):
    text: str
    value: str | None = None  # the number/date/owner, if quantitative
    sources: list[SourceRef] = []  # MUST be non-empty for quantitative claims


def validate_grounding(claims: list[Claim]) -> list[str]:
    """Return texts of quantitative claims that have no source references."""
    return [
        c.text
        for c in claims
        if c.value is not None and not c.sources
    ]
