"""Per-user Google API client builders for the Context MCP.

Each call mints a fresh credentials object via Domain-Wide Delegation
and builds a discovery-backed service. We deliberately do NOT cache by
user — caching here would smear identity across requests on a long-
running worker and undermine the per-request impersonation model.
"""

from googleapiclient.discovery import Resource, build

from auth import get_dwd_credentials


def _service(api: str, version: str, user_email: str) -> Resource:
    return build(
        api,
        version,
        credentials=get_dwd_credentials(user_email),
        cache_discovery=False,
    )


def gmail(user_email: str) -> Resource:
    """Gmail v1 service authenticated as *user_email*."""
    return _service("gmail", "v1", user_email)


def drive(user_email: str) -> Resource:
    """Drive v3 service authenticated as *user_email*."""
    return _service("drive", "v3", user_email)


def docs(user_email: str) -> Resource:
    """Docs v1 service authenticated as *user_email*."""
    return _service("docs", "v1", user_email)


def calendar(user_email: str) -> Resource:
    """Calendar v3 service authenticated as *user_email*."""
    return _service("calendar", "v3", user_email)
