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
from google.adk.events import Event, EventActions
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types

from agent import action_toolset, gemini_model
from config import settings
from mcp_client import call_action_tool
from workflows._base import AccessMode, Workflow
from workflows.common.state_keys import (
    IQ_ASSEMBLY_STATUS,
    IQ_COMPANY_NAME,
    IQ_FILLED_LINK,
    IQ_PROFILE,
    IQ_RESEARCH,
    IQ_TAILOR,
    IQ_TAILOR_STATE,
)
from workflows.iq_engine.schemas import CustomerIQReport

log = logging.getLogger(__name__)

_COMPLETED_MARKER = "<<STATUS:COMPLETED>>"

# Sanmina capabilities — single source of truth shared by the /iq tailoring
# card's "segment lens" options (chat.py) and the tailoring instruction block.
# Each entry is (form_value, human_label).
SANMINA_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("pcba", "PCB assembly (PCBA)"),
    ("system_assembly", "System / box-build assembly"),
    ("jdm", "JDM & design services"),
    ("enclosures", "Enclosures & precision machining"),
    ("optical_rf", "Optical & RF modules"),
    ("cable_backplane", "Cable & backplane"),
    ("defense_aerospace", "Defense & aerospace (ITAR / AS9100)"),
    ("medical", "Medical (ISO 13485)"),
    ("automotive", "Automotive (IATF 16949)"),
    ("cloud_datacenter", "Cloud & data-center infrastructure"),
    ("supply_chain", "Supply-chain management"),
    ("test_repair", "Test & repair"),
)
_CAPABILITY_LABELS = dict(SANMINA_CAPABILITIES)

# Human-readable framing for the "purpose / audience" lever.
_PURPOSE_GUIDANCE = {
    "cold_outreach": (
        "Cold-outreach prep — keep it crisp and action-oriented; emphasise the "
        "opening angle and concrete next steps."
    ),
    "qbr": (
        "QBR / account-review prep — emphasise current state, recent changes, "
        "and strategic context."
    ),
    "exec_briefing": (
        "Executive briefing — lead with the headline verdict; concise and "
        "high-level, lighter on granular detail."
    ),
}

# Human-readable labels for SourceRef.kind, shown in the dossier's Sources list.
_SOURCE_KIND_LABELS = {
    "web": "Web",
    "drive_file": "Drive File",
    "doc_span": "Doc Span",
    "sheet_cell": "Sheet Cell",
    "transcript_span": "Transcript",
}

# copy_file returns a human-readable line: "... ID: <id> | Link: <url>".
_COPY_RESULT_RE = re.compile(r"ID:\s*(\S+)\s*\|\s*Link:\s*(\S+)")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _model_event(author: str, text: str, **delta: object) -> Event:
    """Build a model-role Event, optionally carrying a state delta."""
    actions = EventActions(state_delta=dict(delta)) if delta else EventActions()
    return Event(
        author=author,
        actions=actions,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )


def _text(value: str) -> str:
    """Render a scalar field; fall back to an em dash when empty."""
    return value.strip() if value and value.strip() else "—"


def _bullets(items: list[str]) -> str:
    """Render a string list as a ``•``-prefixed block (one item per line)."""
    cleaned = [i.strip() for i in items if i and i.strip()]
    return "\n".join(f"• {i}" for i in cleaned) if cleaned else "None identified."


def _placeholders(report: CustomerIQReport, generated_date: str) -> dict[str, str]:
    """Map every ``{{token}}`` to its filled value for the assembler.

    List fields become ``•``-prefixed text blocks (plain lines, not native Docs
    bullets — the trade-off of the copy-template + replace_text approach).
    """
    contacts = [
        "• "
        + c.name
        + (f" — {c.title}" if c.title else "")
        + (f" ({c.note})" if c.note else "")
        for c in report.key_contacts
        if c.name.strip()
    ]
    sources = [
        f"• [{_SOURCE_KIND_LABELS.get(s.kind, s.kind)}] {s.locator}"
        + (f" — {s.quote}" if s.quote else "")
        for s in report.sources
        if s.locator.strip()
    ]
    return {
        "{{company_name}}": _text(report.company_name),
        "{{generated_date}}": generated_date,
        "{{executive_summary}}": _text(report.executive_summary),
        "{{fit_tier}}": _text(report.fit.tier),
        "{{recommended_segment}}": _text(report.fit.recommended_segment),
        "{{fit_rationale}}": _text(report.fit.rationale),
        "{{legal_name}}": _text(report.legal_name),
        "{{headquarters}}": _text(report.headquarters),
        "{{founded}}": _text(report.founded),
        "{{ownership}}": _text(report.ownership),
        "{{website}}": _text(report.website),
        "{{employee_count}}": _text(report.employee_count),
        "{{revenue}}": _text(report.revenue),
        "{{business_description}}": _text(report.business_description),
        "{{products}}": _bullets(report.products),
        "{{end_markets}}": _bullets(report.end_markets),
        "{{manufacturing_footprint}}": _text(report.manufacturing_footprint),
        "{{current_ems_providers}}": _bullets(report.current_ems_providers),
        "{{opportunity_signals}}": _bullets(report.opportunity_signals),
        "{{compliance_needs}}": _bullets(report.compliance_needs),
        "{{competitors}}": _bullets(report.competitors),
        "{{key_contacts}}": "\n".join(contacts) if contacts else "None identified.",
        "{{recent_news}}": _bullets(report.recent_news),
        "{{risk_flags}}": _bullets(report.risk_flags),
        "{{recommended_next_steps}}": _bullets(report.recommended_next_steps),
        "{{sources}}": "\n".join(sources) if sources else "None cited.",
    }


def _tailoring_block(state: dict, stage: str) -> str:
    """Render the caller's tailoring selections into an instruction block.

    ``stage`` is ``"research"`` (segment lens as research focus, account context,
    geography, data-source directive) or ``"structuring"`` (purpose/audience tone
    and the segment-lens bias for the fit verdict). Returns ``""`` when nothing
    was selected so the prompt stays at its default behaviour.
    """
    tailor = state.get(IQ_TAILOR) or {}
    if not tailor:
        return ""

    lines: list[str] = []
    segments = [s for s in (tailor.get("segments") or []) if s]
    seg_labels = [_CAPABILITY_LABELS.get(s, s) for s in segments]

    if stage == "research":
        if seg_labels:
            lines.append(
                "- Segment lens: the caller cares about fit for these Sanmina "
                "capabilities — " + ", ".join(seg_labels) + ". Bias the research "
                "toward evidence that confirms or rules out that fit (relevant "
                "products, certifications, programmes, incumbents)."
            )
        context = (tailor.get("context") or "").strip()
        if context:
            lines.append(
                "- Known account context from the rep (treat as a lead to verify "
                f"with your tools, not as fact): {context}"
            )
        geo = (tailor.get("geo") or "").strip()
        if geo:
            lines.append(
                "- Geographic focus: emphasise the company's operations, "
                f"manufacturing footprint, and outreach routing in/around {geo}."
            )
        sources = (tailor.get("sources") or "").strip()
        if sources == "web_only":
            lines.append(
                "- Data sources: use ONLY public web search (google_search). Do "
                "NOT call search_drive or read_document."
            )
        elif sources == "drive_only":
            lines.append(
                "- Data sources: use ONLY Sanmina's Shared Drive (search_drive / "
                "read_document). Do NOT call google_search."
            )
    else:  # structuring
        purpose = (tailor.get("purpose") or "").strip()
        guidance = _PURPOSE_GUIDANCE.get(purpose)
        if guidance:
            lines.append(f"- Purpose / audience: {guidance}")
        if seg_labels:
            lines.append(
                "- Segment lens: judge fit.tier through these Sanmina "
                "capabilities — " + ", ".join(seg_labels) + " — and prefer one of "
                "them for fit.recommended_segment. If the company is a poor fit "
                "for all of them, say so plainly rather than forcing a match."
            )

    if not lines:
        return ""
    return "CALLER TAILORING (honour these):\n" + "\n".join(lines)


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
            yield _model_event(self.name, "Tailoring gate: RESOLVED — continuing.")
            return
        yield _model_event(
            self.name,
            "Tailoring gate: PENDING — tailoring form will be posted.",
            **{IQ_TAILOR_STATE: "PENDING"},
        )


class ConditionalLlmAgent(BaseAgent):
    """Run the wrapped LLM stage only once the tailoring form is resolved.

    Mirrors the RFI engine's conditional wrappers: while the gate is ``PENDING``
    the inner ``LlmAgent`` is skipped so no research/structuring happens before
    the user has tailored the run.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        if ctx.session.state.get(IQ_TAILOR_STATE) == "PENDING":
            yield _model_event(
                self.name, f"{self.name}: skipped — awaiting the tailoring form."
            )
            return
        async for event in self.sub_agents[0].run_async(ctx):
            yield event


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
            yield _model_event(
                self.name, "Assembler skipped — awaiting the tailoring form."
            )
            return

        if _COMPLETED_MARKER in (state.get(IQ_ASSEMBLY_STATUS) or ""):
            link = state.get(IQ_FILLED_LINK)
            yield _model_event(
                self.name,
                "This Customer IQ is already complete in this thread"
                + (f": {link}" if link else "")
                + ". Use /exit to start fresh.",
            )
            return

        raw = state.get(IQ_PROFILE)
        if not raw:
            yield _model_event(
                self.name,
                "I couldn't research that company — no profile was produced. "
                "Try `/iq <company name>` with a clearer company name.",
            )
            return
        try:
            report = CustomerIQReport.model_validate(raw)
        except Exception as exc:  # noqa: BLE001 — tolerate partial LLM output
            log.exception("iq.assembler: profile failed validation")
            yield _model_event(self.name, f"Couldn't read the research result: {exc}")
            return

        template_id = settings().iq_template_doc_id
        if not template_id:
            yield _model_event(
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
            yield _model_event(self.name, f"Failed to copy the IQ template: {exc}")
            return

        match = _COPY_RESULT_RE.search(copy_result)
        if not match:
            yield _model_event(
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

        yield _model_event(
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
            ConditionalLlmAgent(name="iq_research_gate", sub_agents=[research]),
            ConditionalLlmAgent(name="iq_structurer_gate", sub_agents=[structurer]),
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
