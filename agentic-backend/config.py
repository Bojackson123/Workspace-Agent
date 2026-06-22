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

    # Vertex AI model used by the assistant. Override per environment with the
    # ``AGENT_MODEL`` env var (e.g. set on the Cloud Run service); defaults to
    # gemini-2.5-flash for local dev.
    agent_model: str = "gemini-2.5-flash"
    agent_name: str = "workspace_assistant"

    # MCP service endpoints. Defaults point at the local dev compose setup.
    context_mcp_url: str = "http://localhost:8002"
    action_mcp_url: str = "http://localhost:8001"

    # OIDC audience expected on inbound Google Chat JWTs. When ``None`` the
    # ``google.oauth2.id_token`` library skips the audience check — useful for
    # local development where there is no Cloud Run URL to bind to.
    chat_audience: str | None = None

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

    # ---- Model retry (Vertex 429s) ---------------------------------------
    # Every LlmAgent runs through a Gemini model configured with HTTP-level
    # retry (see agent.gemini_model), so a transient 429 RESOURCE_EXHAUSTED is
    # retried at the request layer with exponential backoff + jitter — no matter
    # where in the agent tree it surfaces, including inside the google_search
    # grounding tool's own nested model calls. This is the right layer: a 429
    # raised mid-pipeline (after a stage has already emitted events) cannot be
    # retried by re-running an agent, but it can be retried in place here.
    # ``attempts`` counts the original try (so 6 = 1 + 5 retries); delays grow
    # geometrically from ``initial_delay`` (1s, 2s, 4s, …) capped at ``max_delay``.
    model_retry_attempts: int = 6
    model_retry_initial_delay: float = 1.0
    model_retry_max_delay: float = 60.0

    # ---- Customer IQ (/iq) ------------------------------------------------
    # Google Doc template (with ``{{placeholder}}`` tokens) the /iq workflow
    # copies and fills. Must live on the Shared Drive the Action MCP service
    # account can read. ``None`` disables the workflow's doc assembly.
    iq_template_doc_id: str | None = None

    # ---- RFI (/rfi) research ----------------------------------------------
    # The research stage answers questions in concurrent batches instead of one
    # serial pass. ``rfi_research_chunk_size`` is how many questions each batch
    # researches; ``rfi_research_concurrency`` caps how many batches run at once
    # (bounding simultaneous LLM calls and Action MCP connections). Tune down if
    # Gemini quota or cold Action MCP instances struggle with the burst.
    rfi_research_chunk_size: int = 8
    rfi_research_concurrency: int = 5

    # A batch that hits a Vertex 429 RESOURCE_EXHAUSTED is retried with
    # exponential backoff + jitter before its questions are flagged for human
    # gap-fill. Gemini DSQ pools are tight on this project, so most 429s are
    # transient "edge of quota" bounces that succeed on a later attempt. Delays
    # grow as ``base_delay * 2**i`` (4s, 8s, 16s by default → ~28s max wait per
    # batch), giving the per-minute quota window time to refill.
    rfi_research_max_attempts: int = 4
    rfi_research_retry_base_delay: float = 4.0


@cache
def settings() -> Settings:
    """Return the process-wide ``Settings`` instance, building it on first call."""
    return Settings(
        agent_model=os.getenv("AGENT_MODEL", "gemini-2.5-flash"),
        context_mcp_url=os.getenv("CONTEXT_MCP_SERVICE", "http://localhost:8002"),
        action_mcp_url=os.getenv("ACTION_MCP_SERVICE", "http://localhost:8001"),
        chat_audience=os.getenv("CHAT_APP_AUDIENCE"),
        session_db_url=os.getenv(
            "SESSION_DB_URL", "sqlite+aiosqlite:///./sessions.db"
        ),
        session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", "1800")),
        bootstrap_admin_emails=_parse_emails(os.getenv("BOOTSTRAP_ADMIN_EMAILS")),
        access_cache_ttl_seconds=int(os.getenv("ACCESS_CACHE_TTL_SECONDS", "60")),
        model_retry_attempts=int(os.getenv("MODEL_RETRY_ATTEMPTS", "6")),
        model_retry_initial_delay=float(
            os.getenv("MODEL_RETRY_INITIAL_DELAY", "1.0")
        ),
        model_retry_max_delay=float(os.getenv("MODEL_RETRY_MAX_DELAY", "60.0")),
        iq_template_doc_id=os.getenv("IQ_TEMPLATE_DOC_ID") or None,
        rfi_research_chunk_size=int(os.getenv("RFI_RESEARCH_CHUNK_SIZE", "8")),
        rfi_research_concurrency=int(os.getenv("RFI_RESEARCH_CONCURRENCY", "5")),
        rfi_research_max_attempts=int(os.getenv("RFI_RESEARCH_MAX_ATTEMPTS", "4")),
        rfi_research_retry_base_delay=float(
            os.getenv("RFI_RESEARCH_RETRY_BASE_DELAY", "4.0")
        ),
    )


def _parse_emails(raw: str | None) -> frozenset[str]:
    """Parse a comma-separated env var into a frozen set of trimmed emails."""
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())
