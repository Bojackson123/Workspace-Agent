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


@cache
def settings() -> Settings:
    """Return the process-wide ``Settings`` instance, building it on first call."""
    return Settings(
        context_mcp_url=os.getenv("CONTEXT_MCP_SERVICE", "http://localhost:8002"),
        action_mcp_url=os.getenv("ACTION_MCP_SERVICE", "http://localhost:8001"),
        chat_audience=os.getenv("CHAT_APP_AUDIENCE"),
        location=os.getenv("LOCATION", "us-central1"),
    )
