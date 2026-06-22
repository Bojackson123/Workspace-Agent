"""Render outbound text into the JSON envelope and formatting Google Chat expects."""

from __future__ import annotations

import re
from typing import Final


def chat_text(text: str) -> dict[str, str]:
    """Wrap *text* in the JSON envelope Chat expects from a webhook response."""
    return {"text": text}


# Google Chat renders a small custom format in message text (``*bold*``,
# ``_italic_``, ``~strike~``, ``` `code` ```, ```` ```code``` ````,
# ``<url|text>``). Standard markdown (``**bold**``, ``# headers``,
# ``- bullets``, ``[text](url)``) is shown literally. LLM responses use
# standard markdown, so :func:`_markdown_to_chat` translates them before
# we send the envelope back. Internal messages built in this module
# (help, admin replies) are already authored in Chat format and bypass
# the converter.
_CODE_FENCE_RE: Final = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE: Final = re.compile(r"`[^`\n]+`")
_HEADER_RE: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_BULLET_RE: Final = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_BOLD_RE: Final = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_LINK_RE: Final = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_PLACEHOLDER_RE: Final = re.compile(r"\x00CB(\d+)\x00")


def _markdown_to_chat(text: str) -> str:
    """Translate standard markdown to Google Chat's text formatting.

    Code spans and fenced blocks are stashed first so substitutions
    don't reach inside them — e.g. ``**`` inside a Python snippet must
    survive untouched.
    """
    stash: list[str] = []

    def _save(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x00CB{len(stash) - 1}\x00"

    text = _CODE_FENCE_RE.sub(_save, text)
    text = _INLINE_CODE_RE.sub(_save, text)

    text = _HEADER_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_RE.sub(lambda m: f"*{m.group(1) or m.group(2)}*", text)
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)

    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)
