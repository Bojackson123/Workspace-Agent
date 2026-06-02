"""Runtime configuration for the Dual-MCP agent backend.

All settings are read from the process environment exactly once. Keeping
the lookups behind a memoised ``settings()`` accessor avoids scattering
``os.getenv`` calls across the codebase and makes the configuration easy
to override in tests.
"""

import os
from dataclasses import dataclass
from functools import cache


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable view of the agent backend's runtime configuration."""

    # Identifies the application to ADK's session service.
    app_name: str = "dual-mcp-agent"

    # Vertex AI model used by the assistant.
    agent_model: str = "gemini-2.5-flash"
    agent_name: str = "workspace_assistant"

    # MCP service endpoints. Defaults point at the local dev compose setup.
    context_mcp_url: str = "http://localhost:8002"
    action_mcp_url: str = "http://localhost:8001"

    # OIDC audience expected on inbound Google Chat JWTs. When ``None`` the
    # ``google.oauth2.id_token`` library skips the audience check — useful for
    # local development where there is no Cloud Run URL to bind to.
    chat_audience: str | None = None

    # Vertex AI location passed through to google-genai.
    location: str = "us-central1"

    # SQLAlchemy URL for the ADK ``DatabaseSessionService``. Defaults to a
    # local SQLite file so the backend works out of the box; production
    # should point at Cloud SQL Postgres via the Cloud SQL connector
    # (e.g. ``postgresql+asyncpg://...``).
    session_db_url: str = "sqlite+aiosqlite:///./sessions.db"

    # Idle TTL for multi-turn Chat sessions. After this many seconds of
    # inactivity, the next inbound message in the same thread falls back
    # to the default agent rather than resuming the previous workflow.
    session_ttl_seconds: int = 30 * 60  # 30 minutes

    # ---- Access control ---------------------------------------------------
    # Bootstrap admins for the ``/grant`` / ``/revoke`` / ``/list-access``
    # reserved commands. These are intentionally NOT managed via the
    # rules table — losing the table cannot lock admins out, and a
    # compromised admin command cannot escalate by editing its own ACL.
    bootstrap_admin_emails: frozenset[str] = frozenset()

    # TTL on the per-command access-rule cache. A grant on one instance
    # becomes visible on peer instances after at most this many seconds.
    access_cache_ttl_seconds: int = 60


@cache
def settings() -> Settings:
    """Return the process-wide ``Settings`` instance, building it on first call."""
    return Settings(
        context_mcp_url=os.getenv("CONTEXT_MCP_SERVICE", "http://localhost:8002"),
        action_mcp_url=os.getenv("ACTION_MCP_SERVICE", "http://localhost:8001"),
        chat_audience=os.getenv("CHAT_APP_AUDIENCE"),
        location=os.getenv("LOCATION", "us-central1"),
        session_db_url=os.getenv(
            "SESSION_DB_URL", "sqlite+aiosqlite:///./sessions.db"
        ),
        session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "1800")),
        bootstrap_admin_emails=_parse_emails(os.getenv("BOOTSTRAP_ADMIN_EMAILS")),
        access_cache_ttl_seconds=int(os.getenv("ACCESS_CACHE_TTL_SECONDS", "60")),
    )


def _parse_emails(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated env var into a frozen set of trimmed emails."""
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
