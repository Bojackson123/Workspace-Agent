"""Final-artifact egress check.

Scans generated text for patterns that suggest internal data (email
addresses outside allowed domains, internal URL patterns) was leaked
into the output. Returns a list of finding strings; empty means clean.

This is a best-effort heuristic, not a security guarantee.
"""

from __future__ import annotations

import re


# Patterns that indicate internal-only content that should not appear
# in externally visible artifacts.
_INTERNAL_URL_RE = re.compile(
    r"https?://[^\s/]*(internal|corp|intranet|backstage)[^\s]*",
    re.IGNORECASE,
)
_LEAKED_ID_RE = re.compile(
    r"\b(spreadsheetId|documentId|fileId)\s*[:=]\s*['\"]?[A-Za-z0-9_-]{20,}",
)


def egress_check(text: str, allowed_email_domains: frozenset[str]) -> list[str]:
    """Return a list of findings that suggest internal data leakage.

    Args:
        text: The artifact text to scan.
        allowed_email_domains: Domains whose email addresses are expected
            in the output (e.g. the meeting attendees' domains). Addresses
            from other domains are flagged.

    Returns:
        List of finding strings. Empty list means no issues found.
    """
    findings: list[str] = []

    for match in _INTERNAL_URL_RE.finditer(text):
        findings.append(f"Possible internal URL: {match.group()[:80]}")

    for match in _LEAKED_ID_RE.finditer(text):
        findings.append(f"Possible leaked resource ID: {match.group()[:80]}")

    # Flag email addresses outside allowed domains.
    email_re = re.compile(r"[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})")
    for match in email_re.finditer(text):
        domain = match.group(1).lower()
        if allowed_email_domains and domain not in allowed_email_domains:
            findings.append(f"Email from unexpected domain: {match.group()[:80]}")

    return findings
