"""/report — multi-step deterministic workflow.

Demonstrates that a workflow's ``build_agent`` factory can return any
``BaseAgent`` subclass — here, an ADK ``SequentialAgent`` chaining a
research stage (Context MCP, read-only) into a drafting stage
(Action MCP, write-only). The dispatcher is unchanged; it just calls
``build_agent`` and runs whatever comes back.

Each sub-agent is scoped to a single MCP toolset, so the research
stage *structurally* cannot write to the Shared Drive and the
drafting stage *structurally* cannot read personal data. Splitting
the workflow into stages with disjoint toolsets is the multi-agent
analogue of the dual-MCP boundary the single-agent workflows enforce.

Note: ADK marks ``SequentialAgent`` as deprecated in favour of a
forthcoming ``Workflow`` agent type that has not shipped at the time
of writing. When it does, migrate this file to the new type — the
:class:`workflows.Workflow` dispatch abstraction in this package is
unaffected.
"""

from google.adk.agents import LlmAgent, SequentialAgent

from agent import action_toolset, context_toolset, gemini_model
from workflows._base import AccessMode, Workflow

_RESEARCHER_INSTRUCTION = """\
You are the research stage of a report-generation pipeline. Use the
Context tools to gather the data the user is asking about from their
personal Workspace data (Gmail, Drive, Docs). Produce a concise,
well-structured summary of the findings — bullets, headings, exact
source titles where relevant. Do NOT draft a final document; that is
the next stage's job.
"""

_DRAFTER_INSTRUCTION = """\
You are the drafting stage of a report-generation pipeline. The
previous stage produced a research summary that is already in the
conversation. Create a polished Google Doc on the Shared Drive based
on that summary using the Action tools — search for an existing
target file if the user named one, otherwise create a new Doc. Return
the file's URL in your reply.
"""


async def _build(user_email: str) -> SequentialAgent:
    """Construct the report pipeline for one inbound request.

    A fresh build per request keeps MCP transports short-lived (avoids
    stale ADC tokens and half-open streams) and binds each sub-agent
    to its disjoint toolset.
    """
    researcher = LlmAgent(
        name="report_researcher",
        model=gemini_model(),
        instruction=_RESEARCHER_INSTRUCTION,
        tools=[context_toolset(user_email)],
    )
    drafter = LlmAgent(
        name="report_drafter",
        model=gemini_model(),
        instruction=_DRAFTER_INSTRUCTION,
        tools=[action_toolset()],
    )
    return SequentialAgent(
        name="report_pipeline",
        sub_agents=[researcher, drafter],
    )


WORKFLOW = Workflow(
    command_id=3,
    command_name="/report",
    description="Research a topic from personal data, then draft a Doc.",
    default_access=AccessMode.OPEN,
    build_agent=_build,
    ack_message=(
        "On it — researching your Workspace data, then drafting a Doc on "
        "the Shared Drive. This usually takes a minute or so; I'll reply "
        "here with the link when it's ready."
    ),
)
