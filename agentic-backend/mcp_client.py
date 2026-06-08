"""Direct Context MCP client for backend-driven (non-LLM) tool calls.

Most tool calls go through the ADK ``MCPToolset`` because an LLM is choosing
them. A few flows, though, need the backend itself to invoke a Context MCP tool
deterministically — with no LLM in the loop:

* the meeting pipeline's deterministic calendar-creation step, and
* the "invite people" dialog handler (resolve org users → emails, then patch
  the calendar events).

This module opens a short-lived Streamable HTTP MCP session for one tool call,
passing the calling user's email in the ``X-User-Email`` header exactly like
:func:`agent.context_toolset` — so the Context MCP impersonates the same user
via DWD. The session is per-call by design (mirrors the per-request transport
lifecycle the toolsets use) and never reused across users.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import TextContent

from config import settings

log = logging.getLogger(__name__)

# The Context MCP binds identity from this header (see context-mcp/identity.py).
# agent.context_toolset sends the same header; HTTP header names are
# case-insensitive so the exact casing here does not matter.
_USER_EMAIL_HEADER = "X-User-Email"

# Matches agent.context_toolset's per-operation timeout — cold Cloud Run MCP
# instances plus a Workspace API round trip can exceed the SDK's 5s default.
_MCP_OPERATION_TIMEOUT_S: float = 30.0


def _result_text(content: list[Any]) -> str:
    """Join the text parts of a CallToolResult's content into one string."""
    parts = [c.text for c in content if isinstance(c, TextContent)]
    return "".join(parts)


async def call_context_tool(
    user_email: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Call a single Context MCP tool as *user_email* and return its text result.

    Raises on transport/protocol failure or if the tool reports an error, so
    callers can surface a clear message; tool-level "soft" errors (returned as
    text) come back as the string for the caller to inspect.
    """
    url = f"{settings().context_mcp_url}/mcp"
    headers = {_USER_EMAIL_HEADER: user_email}
    async with streamablehttp_client(
        url, headers=headers, timeout=_MCP_OPERATION_TIMEOUT_S
    ) as (read, write, _get_session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments or {})
            text = _result_text(result.content)
            if result.isError:
                log.error("Context tool %s errored: %s", tool_name, text)
                raise RuntimeError(f"Context tool {tool_name} failed: {text}")
            return text
