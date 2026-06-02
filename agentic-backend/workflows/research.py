"""/research — read-only assistant scoped to personal Workspace data.

The simplest workflow shape: one LlmAgent, one toolset, a prompt. Uses
the :func:`llm_workflow` helper rather than constructing the agent by
hand. Compare with :mod:`workflows.sequential_report` for a richer
multi-agent workflow.
"""

from workflows._base import AccessMode, ToolsetKind
from workflows._helpers import llm_workflow

_INSTRUCTION = """\
You are a research assistant scoped to the user's personal Workspace data.

You have read-only access to the user's Gmail, Drive, and Docs via the
Context tools. You do NOT have write access of any kind.

Rules:
1. Ground every claim in something you actually retrieved. If a tool call
   returned nothing relevant, say so — do not speculate.
2. When citing a source, include the document title or message subject
   verbatim so the user can locate it.
3. Summarise; do not exfiltrate. Never paste large verbatim passages of
   personal data when a short summary will do.
"""


WORKFLOW = llm_workflow(
    command_id=1,
    command_name="/research",
    description="Summarise findings from your personal Workspace data.",
    instruction=_INSTRUCTION,
    toolsets=frozenset({ToolsetKind.CONTEXT}),
    default_access=AccessMode.OPEN,
    ack_message="On it — searching your Workspace data. I'll reply here when I have the results.",
)
