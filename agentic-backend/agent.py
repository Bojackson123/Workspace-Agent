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

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams

from config import settings
from workflows._base import Workflow

AGENT_DESCRIPTION = (
    "A Google Workspace assistant that reads user context via a "
    "read-only Context MCP and writes outputs via an Action MCP."
)


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
        )
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
        model=cfg.agent_model,
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
