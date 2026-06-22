"""Strip markdown the research LLM may emit so it doesn't show verbatim in a cell.

Spreadsheet/Word cells store literal text — they cannot render markdown — so
``**ISO 9001**`` or ``- item`` would appear as-is. This converts the common
syntax to clean prose before write-back.
"""

from __future__ import annotations

import re

_MD_CODE_FENCE = re.compile(r"^\s*```.*$", re.MULTILINE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|`)(?=\S)(.+?)(?<=\S)\1")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)
_MD_HRULE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$", re.MULTILINE)


def _md_to_plain(text: str) -> str:
    """Convert markdown the LLM may emit into plain text for a cell.

    Cheap, dependency-free, and conservative: it removes emphasis/heading/quote
    markers, unwraps links to ``text (url)``, and normalises bullets to "• " so
    list answers stay readable. Leaves ordinary prose untouched.
    """
    if not text or not isinstance(text, str):
        return text or ""
    out = _MD_HRULE.sub("", text)
    out = _MD_CODE_FENCE.sub("", out)
    out = _MD_IMAGE.sub(r"\1", out)
    # "[Sanmina](https://…)" → "Sanmina (https://…)"; bare "[label](label)"
    # (text == url) collapses to just the label.
    out = _MD_LINK.sub(
        lambda m: m.group(1) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})",
        out,
    )
    out = _MD_HEADING.sub("", out)
    out = _MD_BLOCKQUOTE.sub("", out)
    out = _MD_BULLET.sub(r"\1• ", out)
    # Emphasis last, after structural markers are gone. Run twice to catch
    # nested/adjacent runs (e.g. "**bold _and_ italic**").
    out = _MD_EMPHASIS.sub(r"\2", out)
    out = _MD_EMPHASIS.sub(r"\2", out)
    # Collapse the blank lines left by stripped fences/rules.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
