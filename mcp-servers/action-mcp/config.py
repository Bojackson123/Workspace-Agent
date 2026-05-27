"""Runtime configuration for the Action MCP server.

Settings are read from the environment exactly once and exposed through
a cached ``settings()`` accessor.
"""

import os
from dataclasses import dataclass
from functools import cache
from urllib.parse import urlparse


def _parse_allowed_hosts(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated allowed-hosts list, stripping any URL scheme.

    Accepts entries in either form so operators can paste a Cloud Run URL
    verbatim:

        ALLOWED_HOSTS="action-mcp-xyz.run.app,localhost"
        ALLOWED_HOSTS="https://action-mcp-xyz.run.app"
    """
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
    """Immutable view of the Action MCP's runtime configuration."""

    server_name: str = "Action MCP Server"

    # ID of the Shared Drive every write operation must target. Required at
    # runtime — the server fails loudly if any tool is invoked without it.
    shared_drive_id: str | None = None

    # Allowed Host headers for the streamable-HTTP transport.
    allowed_hosts: tuple[str, ...] = ("localhost",)

    def require_shared_drive_id(self) -> str:
        """Return the Shared Drive ID, raising a clear error if it's unset."""
        if not self.shared_drive_id:
            raise RuntimeError(
                "SHARED_DRIVE_ID environment variable is not set; "
                "Action MCP cannot perform Drive operations."
            )
        return self.shared_drive_id


@cache
def settings() -> Settings:
    """Return the process-wide ``Settings`` instance, building it on first call."""
    return Settings(
        shared_drive_id=os.getenv("SHARED_DRIVE_ID") or None,
        allowed_hosts=_parse_allowed_hosts(os.getenv("ALLOWED_HOSTS", "localhost")),
    )
