"""LLM-agent building blocks for the workflow package.

This module exposes the low-level factories every workflow can reuse:

* :func:`context_toolset` — MCPToolset wired to Context MCP for a
  specific user (their email is sent as a header, never as a tool
  argument).
* :func:`action_toolset` — MCPToolset wired to Action MCP (no user
  identity; the MCP authenticates as its own service account).
* :func:`build_llm_agent` — stock ``LlmAgent`` constructor using this
  app's model + description.

Workflow files import these directly to assemble whatever ADK shape
they need (single ``LlmAgent``, ``SequentialAgent``, ``LoopAgent``,
custom ``BaseAgent`` subclass). The dispatcher then calls
:func:`build_agent_for_workflow`, which simply delegates to the
workflow's own ``build_agent`` factory.

We import only from :mod:`workflows._base` for type symbols — never
from :mod:`workflows` — so the helper layer in
:mod:`workflows._helpers` can depend on this module without creating
a cycle.
"""

from __future__ import annotations

from functools import cache

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.models import Gemini
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams
from google.genai import types

from config import settings
from workflows._base import Workflow

AGENT_DESCRIPTION = (
    "A Google Workspace assistant that reads user context via a "
    "read-only Context MCP and writes outputs via an Action MCP."
)


# Per-request operation timeout (seconds) for the MCP Streamable HTTP
# client. The ADK default is 5s, which is too tight for cold Cloud Run
# instances: initialize + tools/list can comfortably exceed 5s on a cold
# start, and individual tool calls perform Google Workspace API requests
# that take 1–3s each. 30s gives cold starts headroom without masking
# genuinely stuck calls. ``sse_read_timeout`` stays at the SDK default
# of 300s for the long-lived GET /mcp stream.
_MCP_OPERATION_TIMEOUT_S: float = 30.0


def context_toolset(user_email: str) -> MCPToolset:
    """Build the Context MCP toolset bound to *user_email*.

    The email is sent as a header (not a tool argument) so that the LLM
    is structurally incapable of impersonating another user — even
    under prompt injection.
    """
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{settings().context_mcp_url}/mcp",
            headers={"X-User-Email": user_email},
            timeout=_MCP_OPERATION_TIMEOUT_S,
        )
    )


def action_toolset() -> MCPToolset:
    """Build the Action MCP toolset.

    No user identifier is sent: the Action MCP authenticates as a
    single service account and writes only to the designated Shared
    Drive.
    """
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{settings().action_mcp_url}/mcp",
            timeout=_MCP_OPERATION_TIMEOUT_S,
        )
    )


# HTTP status codes worth retrying: 429 (the RESOURCE_EXHAUSTED quota bounce
# that motivates this), 408 (request timeout), and the transient 5xx family.
_RETRYABLE_STATUS_CODES = [408, 429, 500, 502, 503, 504]


@cache
def gemini_model() -> Gemini:
    """The app's Gemini model, configured with HTTP-level retry.

    Every ``LlmAgent`` in every workflow should use this instead of passing the
    bare ``cfg.agent_model`` name string, so a transient Vertex 429 is retried
    at the google-genai request layer with exponential backoff + jitter. That
    layer wraps *all* model calls — including the nested ones the google_search
    grounding tool makes as its own ``AgentTool`` — which an agent-level retry
    cannot reach (by the time a 429 from inside a tool surfaces, the outer agent
    has already streamed events and can't be safely re-run).

    Cached: the model holds a lazily-built genai client and is safe to share
    across agents and concurrent runs (each call is independent).
    """
    cfg = settings()
    return Gemini(
        model=cfg.agent_model,
        retry_options=types.HttpRetryOptions(
            attempts=cfg.model_retry_attempts,
            initial_delay=cfg.model_retry_initial_delay,
            max_delay=cfg.model_retry_max_delay,
            exp_base=2.0,
            jitter=1.0,
            http_status_codes=_RETRYABLE_STATUS_CODES,
        ),
    )


def build_llm_agent(
    *,
    instruction: str,
    tools: list[MCPToolset] | None = None,
) -> LlmAgent:
    """Construct an ``LlmAgent`` with the app-standard model + description."""
    cfg = settings()
    return LlmAgent(
        name=cfg.agent_name,
        model=gemini_model(),
        description=AGENT_DESCRIPTION,
        instruction=instruction,
        tools=tools or [],
    )


# Discovered by the ADK CLI (``adk run`` / ``adk web``). Toolsets are
# empty here because the CLI cannot supply a user email or invoke a
# workflow's async factory; live tools are wired in per request.
root_agent = build_llm_agent(
    instruction="(placeholder — live tools are wired per-request)",
)


async def build_agent_for_workflow(
    workflow: Workflow, user_email: str
) -> BaseAgent:
    """Ask *workflow* for the ADK agent to run this request against.

    The dispatcher does not care which ADK agent type comes back —
    ``LlmAgent``, ``SequentialAgent``, ``LoopAgent``, or any other
    ``BaseAgent`` subclass. A fresh build per request keeps MCP
    transports short-lived (avoiding stale ADC tokens and half-open
    streamable-HTTP sessions).
    """
    return await workflow.build_agent(user_email)
