"""Workspace Assistant agent definition and per-request factory.

The agent is wired to two MCP toolsets:

* **Context MCP** — read-only access to the user's personal data. The
  user's identity is carried as an ``X-User-Email`` header on the MCP
  transport so the server can impersonate the user via Domain-Wide
  Delegation. No user identifier appears in tool arguments.

* **Action MCP** — read/write access to a designated Shared Drive,
  authenticated with the service account's own ADC credentials.

A module-level ``root_agent`` exists for the ADK CLI (``adk run`` /
``adk web``); live MCP connections are built fresh per request inside
``build_agent_for_user``.
"""

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import MCPToolset, StreamableHTTPConnectionParams

from config import settings

AGENT_DESCRIPTION = (
    "A Google Workspace assistant that reads user context via a "
    "read-only Context MCP and writes outputs via an Action MCP."
)

AGENT_INSTRUCTION = """\
You are an enterprise Google Workspace assistant.

You have access to two groups of tools — Context tools and Action tools.

**Context tools** search and retrieve the user's personal data (emails,
documents, chat history). The server behind these tools securely extracts
the user's identity from the network transport, so you do NOT need to pass
any user identifier.

**Action tools** create, modify, and manage Docs, Sheets, and other files
on a designated Shared Drive. The server uses its own service-account
identity.

Rules:
1. Use Context tools only to *read* personal data for grounding your
   answers. Never attempt to write or modify personal data.
2. Use Action tools to produce any outputs (documents, spreadsheets,
   files). All outputs MUST target the designated Shared Drive.
3. Do NOT use Action tools to manipulate data found via Context tools
   directly. Summarise, transform, or reference it instead.
4. Before modifying any existing file on the Shared Drive, always query
   the Action tool ``search_drive`` to obtain the exact file/folder ID
   first.
"""


def _context_toolset(user_email: str) -> MCPToolset:
    """Build the Context MCP toolset bound to *user_email*.

    The email is sent as a header (not a tool argument) so that the LLM
    is structurally incapable of impersonating another user — even under
    prompt injection.
    """
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{settings().context_mcp_url}/mcp",
            headers={"X-User-Email": user_email},
        )
    )


def _action_toolset() -> MCPToolset:
    """Build the Action MCP toolset.

    No user identifier is sent: the Action MCP authenticates as a single
    service account and writes only to the designated Shared Drive.
    """
    return MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=f"{settings().action_mcp_url}/mcp",
        )
    )


def _build_agent(toolsets: list[MCPToolset] | None = None) -> LlmAgent:
    """Construct an ``LlmAgent`` with the standard description, model, and prompt."""
    cfg = settings()
    return LlmAgent(
        name=cfg.agent_name,
        model=cfg.agent_model,
        description=AGENT_DESCRIPTION,
        instruction=AGENT_INSTRUCTION,
        tools=toolsets or [],
    )


# Discovered by the ADK CLI. Toolsets are deliberately empty because the
# CLI cannot supply a user email; live tools are wired in per request.
root_agent = _build_agent()


async def build_agent_for_user(user_email: str) -> LlmAgent:
    """Construct an agent with fresh MCP connections for one inbound request.

    A fresh build per request avoids two failure modes:

    * Stale ADC tokens cached inside long-lived MCP transports.
    * Half-open streamable-HTTP sessions left over from a prior request.
    """
    return _build_agent(
        toolsets=[
            _context_toolset(user_email),
            _action_toolset(),
        ]
    )
