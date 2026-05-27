"""Runtime configuration for the Context MCP server.

Mirrors the Action MCP's settings pattern: environment lookups happen
once, behind a cached ``settings()`` accessor.
"""

import os
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlparse


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated allowed-hosts list, stripping any URL scheme."""
    hosts: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host = urlparse(entry).hostname if entry.startswith("http") else entry
        if host:
            hosts.append(host)
    return tuple(hosts)


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable view of the Context MCP's runtime configuration."""

    server_name: str = "Context MCP Server"
    allowed_hosts: tuple[str, ...] = ("localhost",)


@cache
def settings() -> Settings:
    """Return the process-wide ``Settings`` instance, building it on first call."""
    return Settings(
        allowed_hosts=_parse_allowed_hosts(os.getenv("ALLOWED_HOSTS", "localhost")),
    )
