"""/draft — write-only assistant scoped to the designated Shared Drive."""

from workflows._base import AccessMode, ToolsetKind
from workflows._helpers import llm_workflow

_INSTRUCTION = """\
You are a drafting assistant scoped to the designated Shared Drive.

You have write access to Docs, Sheets, and Drive files on the Shared
Drive via the Action tools. You do NOT have access to the user's
personal Gmail, Drive, or Docs.

Rules:
1. Treat the user's prompt as the spec for what to create or update.
2. Before modifying any existing file, always call ``search_drive``
   first to resolve the exact file/folder ID.
3. When creating a new file, return its URL in the reply so the user can
   open it directly.
"""


WORKFLOW = llm_workflow(
    command_id=2,
    command_name="/draft",
    description="Create or update a document on the Shared Drive.",
    instruction=_INSTRUCTION,
    toolsets=frozenset({ToolsetKind.ACTION}),
    default_access=AccessMode.OPEN,
    ack_message="On it — drafting on the Shared Drive. I'll reply with the link once it's ready.",
)
