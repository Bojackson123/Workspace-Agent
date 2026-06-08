"""Directory (People API) tools for the Context MCP.

Resolves the org people-picker selections from a Google Chat card into email
addresses. The Chat ``MULTI_SELECT`` widget backed by the ``USER`` common data
source returns selected users as ``users/<id>`` resource names — not emails — so
the invite dialog must resolve them here before they can be added as calendar
attendees.

Requires the ``directory.readonly`` DWD scope (declared in :mod:`auth`).
"""

import json
import logging

from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP

from clients import people as people_service
from identity import current_user_email

log = logging.getLogger(__name__)


def _to_person_resource(raw: str) -> str:
    """Normalise a Chat ``users/<id>`` selection to a People ``people/<id>``.

    The numeric id is shared between the Chat user resource and the People API
    person resource. Accepts ``users/123``, ``people/123``, or a bare ``123``.
    """
    raw = raw.strip()
    ident = raw.split("/")[-1] if "/" in raw else raw
    return f"people/{ident}"


def resolve_people_emails(resource_names: list[str]) -> str:
    """Resolve Chat user selections to email addresses via the People API.

    Args:
        resource_names: Selected values from a USER multi-select
            (``users/<id>``), or bare/``people/<id>`` ids.

    Returns:
        A JSON array of objects ``{"resource_name", "email", "name"}``. Entries
        that cannot be resolved (or have no email) are returned with
        ``email=null`` so the caller can report partial failures.
    """
    user_email = current_user_email()
    service = people_service(user_email)

    results: list[dict] = []
    for raw in resource_names:
        resource = _to_person_resource(raw)
        entry: dict = {"resource_name": raw, "email": None, "name": None}
        try:
            person = service.people().get(
                resourceName=resource,
                personFields="emailAddresses,names",
            ).execute()
            emails = person.get("emailAddresses") or []
            if emails:
                entry["email"] = emails[0].get("value")
            names = person.get("names") or []
            if names:
                entry["name"] = names[0].get("displayName")
        except HttpError as exc:
            log.warning("People.get failed for %s: %s", resource, exc)
        except Exception:
            log.exception("People.get crashed for %s", resource)
        results.append(entry)

    return json.dumps(results, ensure_ascii=False)


_TOOLS = (resolve_people_emails,)


def register(mcp: FastMCP) -> None:
    """Register Directory tools onto *mcp*."""
    for fn in _TOOLS:
        mcp.tool()(fn)
