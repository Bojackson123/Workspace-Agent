"""Memoised Google API client factories for the Action MCP.

Building a discovery-backed service object is expensive (it downloads a
JSON discovery document), so we cache one instance per API per process.
The underlying credentials handle their own access-token refresh, so
caching the service object across the process lifetime is safe.
"""

from functools import cache

from googleapiclient.discovery import Resource, build

from auth import get_action_credentials


def _service(api: str, version: str) -> Resource:
    return build(
        api,
        version,
        credentials=get_action_credentials(),
        cache_discovery=False,
    )


@cache
def drive() -> Resource:
    """Google Drive v3 service."""
    return _service("drive", "v3")


@cache
def docs() -> Resource:
    """Google Docs v1 service."""
    return _service("docs", "v1")


@cache
def sheets() -> Resource:
    """Google Sheets v4 service."""
    return _service("sheets", "v4")
