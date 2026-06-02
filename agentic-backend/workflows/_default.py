"""The default (free-form) workflow.

Used when the user sends a plain message with no active session and no
slash command. Provides both Context and Action toolsets and the
original dual-MCP catch-all instruction. Lives in its own module (not
the ``WORKFLOWS`` registry) because the dispatcher needs to import it
directly as a fallback even though it has no slash command.
"""

from workflows._base import AccessMode, ToolsetKind, Workflow
from workflows._helpers import llm_workflow

_INSTRUCTION = """\
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


DEFAULT_WORKFLOW: Workflow = llm_workflow(
    command_id=0,
    command_name="<default>",
    description="Free-form Workspace assistant (no slash command).",
    instruction=_INSTRUCTION,
    toolsets=frozenset({ToolsetKind.CONTEXT, ToolsetKind.ACTION}),
    default_access=AccessMode.OPEN,
)
