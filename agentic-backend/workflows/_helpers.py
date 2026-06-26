"""Helper for the common case: a single ``LlmAgent`` with MCP toolsets.

Wraps :mod:`agent`'s building blocks so simple workflow files don't
need to think about MCP transport details. Workflows that need a
richer ADK shape (``SequentialAgent``, ``ParallelAgent``,
``LoopAgent``, custom ``BaseAgent`` subclasses) skip this helper and
construct their agent directly using the public factories in
:mod:`agent` and the agent types from :mod:`google.adk.agents`.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset

from clients.agent import action_toolset, build_llm_agent, context_toolset
from workflows._base import AccessMode, ToolsetKind, Workflow


def llm_workflow(
    *,
    command_id: int,
    command_name: str,
    description: str,
    instruction: str,
    toolsets: frozenset[ToolsetKind],
    default_access: AccessMode = AccessMode.OPEN,
    ack_message: str | None = None,
) -> Workflow:
    """Build a :class:`Workflow` whose agent is one ``LlmAgent``.

    The returned workflow's ``build_agent`` factory constructs a fresh
    ``LlmAgent`` per request with *instruction* and only the MCP
    toolsets named in *toolsets*. Workflows scoped to one toolset are
    *structurally* incapable of using the other — the unused MCP is
    not attached to the agent at all.
    """

    async def _build(user_email: str) -> LlmAgent:
        return build_llm_agent(
            instruction=instruction,
            tools=_toolsets_for(toolsets, user_email),
        )

    return Workflow(
        command_id=command_id,
        command_name=command_name,
        description=description,
        default_access=default_access,
        build_agent=_build,
        ack_message=ack_message,
    )


def _toolsets_for(
    kinds: frozenset[ToolsetKind], user_email: str
) -> list[MCPToolset]:
    """Build live MCP toolsets matching the kinds the workflow asked for."""
    out: list[MCPToolset] = []
    if ToolsetKind.CONTEXT in kinds:
        out.append(context_toolset(user_email))
    if ToolsetKind.ACTION in kinds:
        out.append(action_toolset())
    return out
