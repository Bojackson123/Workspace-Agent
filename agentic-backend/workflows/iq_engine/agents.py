"""/iq — Customer IQ Engine.

Pipeline:
  Sequential[
    IQTailorGate    (suspend for the tailoring form on the first run)
    ResearchAgent   (LLM + web search: research the company, score the fit)
    StructuringAgent(LLM: reshape the brief into the dossier schema)
    IQAssembler     (deterministic: copy the Doc template, fill placeholders)
  ]

Invocation: ``/iq <company name>``

The user types a company name after the slash command; it is seeded into
``IQ_COMPANY_NAME``. On the first run the tailoring gate suspends the pipeline so
chat.py can post an optional tailoring form (segment lens, account context,
purpose, geography, data sources); the selections steer the research and
structuring prompts. Once the form is submitted (or skipped), the research agent
profiles the company — grounded in Sanmina's Shared-Drive knowledge base first,
public web second — and emits a structured ``CustomerIQReport`` including a fit
tier and the recommended Sanmina segment. The assembler then copies the
configured Google Doc template and replaces each ``{{placeholder}}`` token,
returning a link to the filled dossier.

The assembler is idempotent: a re-run in the same thread reports the existing
doc rather than creating a duplicate.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

from google.adk.agents import BaseAgent, LlmAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.events import Event
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types

from agent import action_toolset, gemini_model
from config import settings
from mcp_client import call_action_tool
from workflows._base import AccessMode, Workflow
from workflows.common.conditional import GuardAgent
from workflows.common.events import model_event
from workflows.common.state_keys import (
    IQ_ASSEMBLY_STATUS,
    IQ_COMPANY_NAME,
    IQ_FILLED_LINK,
    IQ_PROFILE,
    IQ_RESEARCH,
    IQ_TAILOR_STATE,
)
from workflows.iq_engine.rendering import _placeholders, _tailoring_block
from workflows.iq_engine.schemas import CustomerIQReport

log = logging.getLogger(__name__)

_COMPLETED_MARKER = "<<STATUS:COMPLETED>>"

# copy_file returns a human-readable line: "... ID: <id> | Link: <url>".
_COPY_RESULT_RE = re.compile(r"ID:\s*(\S+)\s*\|\s*Link:\s*(\S+)")


# ── Stage 1: grounded research (LLM + tools, free-form) ─────────────────────
#
# No output_schema here: forcing strict JSON in the same call as google_search
# suppresses the grounding step (the model fills the schema from training memory
# instead of searching), which produced stale figures. Keeping this stage
# free-form lets the model actually search and report CURRENT facts; stage 2
# then structures the result.


_RESEARCH_INSTRUCTION = """\
You are the research analyst for Sanmina's Customer IQ Engine. Sanmina is a
global integrated manufacturing solutions (EMS) company. Your job is to gather a
current, well-sourced research brief on a company the user names.

The company to profile is named below. If it is ambiguous (e.g. a common
name), profile the most prominent company by that name and say so.

Research with your tools and gather CURRENT facts covering:
- Firmographics: legal name, HQ, founding year, ownership (public/private/PE,
  ticker if public), website, latest employee count, and most-recent fiscal-year
  revenue.
- Business & products: what they make/sell, and whether their products contain
  electronics or hardware Sanmina could manufacture.
- End markets / industries served.
- Manufacturing & supply chain: do they build in-house or outsource? facility
  locations? which contract manufacturers / EMS firms (Foxconn, Jabil, Flex,
  Celestica, …) do they use today?
- Compliance / certifications their industry implies (ISO 13485, AS9100,
  IATF 16949, ITAR, …).
- Opportunity signals & recent news from roughly the last 12–18 months:
  launches, reshoring/nearshoring, capacity constraints, supply disruptions,
  M&A, geographic expansion.
- Competitors.
- Key decision-makers (procurement, supply chain, operations, engineering), if
  findable.

Tools:
- search_drive / read_document (Action MCP) — Sanmina's internal knowledge base
  on the Shared Drive. Search here FIRST for any prior dealings or account notes.
- google_search — public web. USE IT to get the LATEST figures and news.

RECENCY RULES (important):
- Always prefer the most recent fiscal year and recent reporting. Do NOT rely on
  your training memory for financials, employee counts, or news — search for the
  current numbers.
- Label every figure with its period/date (e.g. "FY2025 revenue: …", "headcount
  as of <date>") and include the source URL inline next to each claim.
- If you cannot verify something, say so rather than guessing.

Write a thorough, well-organised brief in markdown. Do NOT output JSON.
"""


def _research_instruction(ctx: ReadonlyContext) -> str:
    """Anchor the research in the current date and the caller's tailoring."""
    today = datetime.now(timezone.utc).date().isoformat()
    company = (ctx.state.get(IQ_COMPANY_NAME) or "").strip()
    company_line = (
        f"Company to profile: {company}."
        if company
        else "The company to profile is in the user's message."
    )
    parts = [
        _RESEARCH_INSTRUCTION,
        company_line,
        f"Today's date is {today}. Treat any figure older than the most recent "
        "reported period as potentially outdated and search for the current one.",
    ]
    tail = _tailoring_block(ctx.state, "research")
    if tail:
        parts.append(tail)
    return "\n\n".join(parts)


# ── Stage 2: structuring (LLM, schema-only, no tools) ───────────────────────


_STRUCTURING_INSTRUCTION = """\
You convert a research brief into Sanmina's Customer IQ dossier schema. Sanmina
is a global integrated manufacturing solutions (EMS) company.

Use ONLY the facts in the RESEARCH BRIEF below. Do NOT add facts from your own
memory; if the brief does not cover a field, leave it empty.

SANMINA CAPABILITIES (use these to judge fit and pick the recommended segment):
PCB assembly (PCBA), system / box-build assembly, JDM & design services,
enclosures & precision machining (metal/plastics), optical & RF modules,
cable & backplane, defense & aerospace (ITAR / AS9100), medical (ISO 13485),
automotive (IATF 16949), cloud & data-center infrastructure, supply-chain
management, and test & repair.

Produce a single CustomerIQReport. Guidance per field:
- executive_summary: 3-4 sentences — who they are, why they matter to Sanmina,
  and the headline fit verdict.
- fit.tier: High / Medium / Low based on whether their products plausibly need
  Sanmina's services (do they ship hardware/electronics?), the scale and
  regulatory fit, and any opening to displace an incumbent.
- fit.recommended_segment: must name a Sanmina capability from the list above.
- fit.rationale: explain the call.
- current_ems_providers: contract manufacturers / EMS firms they use today —
  the incumbents to displace.
- recommended_next_steps: concrete outreach angle and which Sanmina segment to
  route the lead to.
- sources: carry over the evidence from the brief. For a public web page use
  kind "web" with the full URL in `locator`. For Sanmina knowledge-base material
  on the Shared Drive use kind "drive_file" (a whole file) or "doc_span" (a
  passage within a doc), with the file or passage locator. Pick the kind that
  matches where the fact actually came from — do NOT label web pages as
  "drive_file".

It is better to leave a field empty than to invent content. Output only the
CustomerIQReport JSON matching the schema.
"""


def _structuring_instruction(ctx: ReadonlyContext) -> str:
    """Feed the stage-1 research brief and the caller's tailoring to the model."""
    brief = ctx.state.get(IQ_RESEARCH) or "(no research brief was produced)"
    parts = [_STRUCTURING_INSTRUCTION]
    tail = _tailoring_block(ctx.state, "structuring")
    if tail:
        parts.append(tail)
    parts.append(f"RESEARCH BRIEF:\n```\n{brief}\n```\n")
    return "\n\n".join(parts)


# ── Tailoring gate (suspend for the form) ───────────────────────────────────


class IQTailorGate(BaseAgent):
    """Suspend the pipeline until the user submits (or skips) the tailoring form.

    On the first run (no ``IQ_TAILOR_STATE``) it sets the state to ``"PENDING"``
    so chat.py posts the tailoring card. After the card handler patches the
    selections and sets the state to ``"RESOLVED"``, the gate passes through. A
    completed dossier also passes through (the assembler is idempotent and will
    just report the existing link), so a re-run in the same thread doesn't get
    stuck re-prompting for tailoring.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        resolved = state.get(IQ_TAILOR_STATE) == "RESOLVED"
        completed = _COMPLETED_MARKER in (state.get(IQ_ASSEMBLY_STATUS) or "")
        if resolved or completed:
            yield model_event(self.name, "Tailoring gate: RESOLVED — continuing.")
            return
        yield model_event(
            self.name,
            "Tailoring gate: PENDING — tailoring form will be posted.",
            **{IQ_TAILOR_STATE: "PENDING"},
        )


def _tailoring_pending(state: dict) -> bool:
    """True while the tailoring form is awaiting submission (skip the LLM stages)."""
    return state.get(IQ_TAILOR_STATE) == "PENDING"


# ── Assembler (deterministic) ───────────────────────────────────────────────


class IQAssembler(BaseAgent):
    """Copy the Doc template and replace every ``{{placeholder}}`` token.

    Reads the ``CustomerIQReport`` from ``IQ_PROFILE``, copies the configured
    template via the Action MCP, then issues one ``replace_text`` per token.
    Idempotent: once a dossier has been produced (``IQ_ASSEMBLY_STATUS`` carries
    the completed marker) it reports the existing link instead of duplicating.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        if state.get(IQ_TAILOR_STATE) == "PENDING":
            yield model_event(
                self.name, "Assembler skipped — awaiting the tailoring form."
            )
            return

        if _COMPLETED_MARKER in (state.get(IQ_ASSEMBLY_STATUS) or ""):
            link = state.get(IQ_FILLED_LINK)
            yield model_event(
                self.name,
                "This Customer IQ is already complete in this thread"
                + (f": {link}" if link else "")
                + ". Use /exit to start fresh.",
            )
            return

        raw = state.get(IQ_PROFILE)
        if not raw:
            yield model_event(
                self.name,
                "I couldn't research that company — no profile was produced. "
                "Try `/iq <company name>` with a clearer company name.",
            )
            return
        try:
            report = CustomerIQReport.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 — tolerate partial LLM output
            log.exception("iq.assembler: profile failed validation")
            yield model_event(self.name, f"Couldn't read the research result: {exc}")
            return

        template_id = settings().iq_template_doc_id
        if not template_id:
            yield model_event(
                self.name,
                "The Customer IQ doc template isn't configured. Set the "
                "`IQ_TEMPLATE_DOC_ID` environment variable to a Google Doc on "
                "the Shared Drive, then try again.",
            )
            return

        company = report.company_name.strip() or "Unknown Company"

        # 1) Copy the template into a fresh dossier doc on the Shared Drive.
        try:
            copy_result = await call_action_tool(
                "copy_file",
                {"file_id": template_id, "new_name": f"Customer IQ — {company}"},
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("iq.assembler: copy_file failed")
            yield model_event(self.name, f"Failed to copy the IQ template: {exc}")
            return

        match = _COPY_RESULT_RE.search(copy_result)
        if not match:
            yield model_event(
                self.name,
                f"Failed to copy the IQ template: {copy_result}",
            )
            return
        doc_id, link = match.group(1), match.group(2)

        # 2) Fill every placeholder in a SINGLE batchUpdate. One round-trip
        #    instead of ~26 sequential replace_text calls (each of which opened
        #    a fresh MCP session); a missing token is harmless (0 replacements).
        generated_date = datetime.now(timezone.utc).date().isoformat()
        tokens = _placeholders(report, generated_date)
        fill_error: str | None = None
        try:
            await call_action_tool(
                "replace_text_batch",
                {"document_id": doc_id, "replacements": tokens},
            )
        except Exception as exc:  # noqa: BLE001 — report, don't abort the summary
            log.exception("iq.assembler: replace_text_batch failed")
            fill_error = str(exc)

        summary_lines = [
            f"✅ Customer IQ ready: *{company}*",
            f"Fit: {report.fit.tier} — recommend *{report.fit.recommended_segment}*.",
            f"Dossier: {link}",
        ]
        if fill_error:
            summary_lines.append(
                "⚠️ Some fields may not have filled; open the doc to review."
            )

        yield model_event(
            self.name,
            "\n".join(summary_lines),
            **{
                IQ_FILLED_LINK: link,
                IQ_ASSEMBLY_STATUS: _COMPLETED_MARKER,
            },
        )


# ── Pipeline factory ────────────────────────────────────────────────────────


# Token budgets used to express the LOW/MINIMAL intent on Gemini 2.x, which
# does not accept ``thinking_level``. MINIMAL → off; LOW → a small budget that
# still leaves room for tool-selection reasoning.
_THINKING_BUDGET = {
    types.ThinkingLevel.MINIMAL: 0,
    types.ThinkingLevel.LOW: 512,
}


def _thinking(level: types.ThinkingLevel, model: str) -> types.GenerateContentConfig:
    """Cap Gemini's thinking to curb /iq latency.

    Gemini 3 flash defaults to high (dynamic) thinking, which adds latency
    before every tool turn — across two LLM stages that compounds. Stage 1
    still wants some reasoning for tool selection (LOW); stage 2 is mechanical
    schema reshaping (MINIMAL).

    ``thinking_level`` is the Gemini-3 knob; Gemini 2.x rejects it and instead
    takes ``thinking_budget`` (a token count), so translate per model.
    """
    if "gemini-3" in model:
        cfg = types.ThinkingConfig(thinking_level=level)
    else:
        cfg = types.ThinkingConfig(thinking_budget=_THINKING_BUDGET[level])
    return types.GenerateContentConfig(thinking_config=cfg)


async def _build(user_email: str) -> SequentialAgent:
    cfg = settings()

    # Stage 1 — grounded research. Free-form (no output_schema) so google_search
    # actually runs; bypass_multi_tools_limit wraps the native grounding tool as
    # an AgentTool so it can share the request with the Action MCP toolset.
    research = LlmAgent(
        name="iq_research",
        model=gemini_model(),
        instruction=_research_instruction,
        tools=[action_toolset(), GoogleSearchTool(bypass_multi_tools_limit=True)],
        generate_content_config=_thinking(types.ThinkingLevel.LOW, cfg.agent_model),
        output_key=IQ_RESEARCH,
    )

    # Stage 2 — structuring. Schema-only, no tools: reshapes the stage-1 brief
    # into the dossier JSON without re-introducing the grounding/JSON conflict.
    structurer = LlmAgent(
        name="iq_structurer",
        model=gemini_model(),
        instruction=_structuring_instruction,
        generate_content_config=_thinking(types.ThinkingLevel.MINIMAL, cfg.agent_model),
        output_schema=CustomerIQReport,
        output_key=IQ_PROFILE,
    )

    assembler = IQAssembler(name="iq_assembler")

    # The tailoring gate suspends the pipeline on the first run so chat.py can
    # post the form; the LLM stages are wrapped so they skip while it's PENDING
    # and only run once the user has submitted (or skipped) the form.
    #
    # 429 retry is handled at the model layer (see agent.gemini_model), which
    # covers the google_search grounding tool's nested calls too — the place /iq
    # was actually hitting RESOURCE_EXHAUSTED.
    return SequentialAgent(
        name="iq_pipeline",
        sub_agents=[
            IQTailorGate(name="iq_tailor_gate"),
            GuardAgent(
                name="iq_research_gate",
                skip_when=_tailoring_pending,
                skip_text="iq_research_gate: skipped — awaiting the tailoring form.",
                sub_agent=research,
            ),
            GuardAgent(
                name="iq_structurer_gate",
                skip_when=_tailoring_pending,
                skip_text="iq_structurer_gate: skipped — awaiting the tailoring form.",
                sub_agent=structurer,
            ),
            assembler,
        ],
    )


WORKFLOW = Workflow(
    command_id=7,
    command_name="/iq",
    description=(
        "Research a company and produce a Customer IQ dossier scoring the "
        "Sanmina manufacturing-services opportunity."
    ),
    default_access=AccessMode.RESTRICTED,
    build_agent=_build,
    ack_message=(
        "On it — researching the company and building the Customer IQ profile. "
        "I'll post the dossier link here shortly."
    ),
)
