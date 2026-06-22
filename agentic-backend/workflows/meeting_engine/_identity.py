"""Identity-token helpers for matching owners against attendees.

Owners are stored as display names, attendees as ``"Name (email)"``;
reducing both to a comparable set of keys lets a name-only owner match a
"Name (email)" attendee and vice versa.
"""

from __future__ import annotations


def _normalize_identity(s: str) -> str:
    """Normalize an email or name to a comparable token.

    "sarah.chen@acmecorp.com" -> "sarah chen"
    "Sarah Chen"              -> "sarah chen"
    """
    local = s.split("@")[0]
    return local.lower().replace(".", " ").replace("_", " ").replace("-", " ")


def _identity_keys(s: str) -> set[str]:
    """Comparable identity tokens for an owner/attendee string.

    Splits a "Name (email)", bare email, or bare name into the keys that can
    match it: the lowercased email, its normalised local part, and the
    normalised name. Lets us match "Sarah Chen" against
    "Sarah Chen (sarah.chen@corp.com)" *and* "priya@corp.com" against
    "Priya Nair (priya@corp.com)".
    """
    s = s.strip()
    email: str | None = None
    if s.endswith(")") and "(" in s:
        inner = s[s.rfind("(") + 1 : -1].strip()
        name = s[: s.rfind("(")].strip()
        if "@" in inner:
            email = inner
    elif "@" in s:
        email, name = s, ""
    else:
        name = s

    keys: set[str] = set()
    if email:
        keys.add(email.lower())
        keys.add(_normalize_identity(email))
    if name:
        keys.add(_normalize_identity(name))
    return keys
