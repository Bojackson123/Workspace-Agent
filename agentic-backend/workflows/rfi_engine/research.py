"""Concurrent, grounded research for the RFI engine.

Questions are split into fixed-size batches and researched concurrently
(bounded by ``rfi_research_concurrency``), each in an isolated in-memory
session so the batches never race on shared state. A 429 is retried with
backoff; persistent failures flag their questions for human gap-fill.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from google.adk.agents import BaseAgent, LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types

from clients.agent import action_toolset, gemini_model
from config import settings
from workflows.common.events import model_event
from workflows.common.retry import retry_async
from workflows.common.state_keys import (
    RFI_ANSWERS,
    RFI_FILE_ID,
    RFI_GUIDANCE,
    RFI_GUIDANCE_STATE,
    RFI_QUESTIONS,
)
from workflows.rfi_engine._helpers import _answers_list, _questions_list
from workflows.rfi_engine.schemas import RFIAnswer, RFIAnswerSet, RFIQuestion

log = logging.getLogger(__name__)


_RESEARCH_INSTRUCTION = f"""\
You are the research analyst for Sanmina's RFI Response Engine. Sanmina is a
global integrated manufacturing solutions (EMS) company. A prospective customer
has sent a Request for Information; your job is to answer each question on
Sanmina's behalf, grounded in evidence.

You will be given the user's SCOPE GUIDANCE and the list of QUESTIONS below.

Tools:
- search_drive / read_document (Action MCP) — Sanmina's internal knowledge base
  on the Shared Drive. ALWAYS search here FIRST and prefer it.
- google_search (built-in Google Search grounding) — public web fallback for
  Sanmina facts (facilities, certifications, financials) not found in the
  knowledge base.

For every question:
- Draft a concise, professional answer written from Sanmina's perspective,
  tailored to the scope guidance (target facilities, business segment, and
  customer/industry).
- Write the `answer` as PLAIN TEXT — it is written directly into a spreadsheet
  or Word cell that cannot render markdown. Do NOT use markdown syntax: no
  **bold**/*italic*, no `# headings`, no backticks, no `[text](url)` links
  (write the URL plainly), and no `-`/`*` bullet markers. If you must list
  items, separate them with newlines or use plain sentences.
- Cite your evidence in `sources` (a drive_file/doc_span locator for KB
  material; a drive_file locator with the URL for web results). Do not
  fabricate facts or numbers.
- If you cannot find sufficient grounded evidence to answer confidently, set
  `needs_human: true` and leave `answer` empty (or a short note on what's
  needed). A human will fill these in afterwards. It is better to flag a gap
  than to invent an answer.

Output a single JSON object matching the schema: {{"answers": [{{"question_id":
"...", "answer": "...", "sources": [...], "needs_human": false}}, ...]}} with one
entry per question (use the exact question ids given).
"""


def _research_instruction(ctx: ReadonlyContext) -> str:
    """Inject the scope guidance + questions JSON into the research prompt."""
    guidance = ctx.state.get(RFI_GUIDANCE) or {}
    questions = ctx.state.get(RFI_QUESTIONS) or []
    return (
        f"{_RESEARCH_INSTRUCTION}\n\n"
        "SCOPE GUIDANCE (authoritative):\n```json\n"
        f"{json.dumps(guidance, indent=2, ensure_ascii=False)}\n```\n\n"
        "QUESTIONS:\n```json\n"
        f"{json.dumps(questions, indent=2, ensure_ascii=False)}\n```\n"
    )


def _build_research_llm() -> LlmAgent:
    """Build the research ``LlmAgent`` used to answer one batch of questions.

    Identical configuration regardless of batch: the question slice and scope
    guidance are supplied through session state, which ``_research_instruction``
    reads. A fresh ``action_toolset()`` per agent means each concurrent batch
    gets its own Action MCP connection (bounded by the batch semaphore).
    """
    return LlmAgent(
        name="rfi_research",
        model=gemini_model(),
        instruction=_research_instruction,
        # google_search is a model-native grounding tool; bypass_multi_tools_limit
        # lets ADK wrap it as an AgentTool so it can run alongside the Action MCP
        # toolset (built-in grounding can't share a request with function tools).
        tools=[action_toolset(), GoogleSearchTool(bypass_multi_tools_limit=True)],
        output_schema=RFIAnswerSet,
        output_key=RFI_ANSWERS,
    )


def _chunk(items: list, size: int) -> list[list]:
    """Split *items* into consecutive chunks of at most *size* (size >= 1)."""
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _run_research_once(
    questions: list[RFIQuestion],
    guidance: dict,
    file_id: str,
    app_name: str,
) -> list[RFIAnswer]:
    """Run the research agent once in an isolated session; return its answers.

    Raises on model/transport errors (e.g. a 429) so the caller can decide
    whether to retry. Returns ``[]`` when the run completes but yields no
    parseable answers.
    """
    session_service = InMemorySessionService()
    user_id = "rfi-batch"
    session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        state={
            RFI_FILE_ID: file_id,
            RFI_GUIDANCE: guidance,
            RFI_QUESTIONS: [q.model_dump() for q in questions],
        },
    )
    runner = Runner(
        agent=_build_research_llm(),
        app_name=app_name,
        session_service=session_service,
    )
    message = types.Content(
        role="user",
        parts=[types.Part(text="Research and answer the questions in state.")],
    )
    async for _event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=message,
    ):
        pass

    final = await session_service.get_session(
        app_name=app_name, user_id=user_id, session_id=session.id
    )
    return _answers_list(final.state if final else {})


async def _research_batch(
    questions: list[RFIQuestion],
    guidance: dict,
    file_id: str,
    app_name: str,
) -> list[RFIAnswer]:
    """Research one batch of questions in an isolated session; return answers.

    Runs a dedicated research ``LlmAgent`` against a throwaway
    ``InMemorySessionService`` seeded with just this batch's questions, so
    concurrent batches never race on the parent session's state.

    A 429 RESOURCE_EXHAUSTED is retried with exponential backoff + jitter
    (``rfi_research_max_attempts`` / ``rfi_research_retry_base_delay``) — Gemini
    DSQ pools are tight here, so most 429s are transient and clear on a later
    attempt. On any other failure, or once retries are exhausted, the batch's
    questions come back flagged ``needs_human`` so they surface in the gap-fill
    form (Form 2) rather than silently vanishing.
    """
    qids = [q.id for q in questions]
    cfg = settings()
    try:
        # 429s are retried with backoff + jitter (de-syncing the concurrent
        # batches); any other error raises straight out to the handler below.
        answers = await retry_async(
            lambda: _run_research_once(questions, guidance, file_id, app_name),
            attempts=cfg.rfi_research_max_attempts,
            base_delay=cfg.rfi_research_retry_base_delay,
            label=f"rfi.research batch {qids}",
        )
        if answers:
            return answers
        log.warning("rfi.research: batch produced no answers for %s", qids)
    except Exception:  # noqa: BLE001 — keep other batches alive
        log.exception("rfi.research: batch failed for %s", qids)
    # Failure or empty output: flag every question for human gap-fill.
    return [RFIAnswer(question_id=qid, needs_human=True) for qid in qids]


class ConditionalResearchAgent(BaseAgent):
    """Research all questions in concurrent batches, exactly once.

    Skips while the guidance gate is ``PENDING`` (Form 1 not yet submitted) and
    skips once ``RFI_ANSWERS`` already exists (so a Form 2 resume does not
    re-run research and clobber the human's gap-fill edits).

    On the research pass the questions are split into fixed-size batches and
    researched concurrently (bounded by ``rfi_research_concurrency``), then the
    answers are merged back into the original question order. This replaces the
    old single-agent pass that researched all questions serially.
    """

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator:
        state = ctx.session.state
        questions = _questions_list(state)
        if state.get(RFI_GUIDANCE_STATE) == "PENDING" or not questions:
            yield model_event(self.name, "Research skipped — awaiting scope guidance.")
            return
        if state.get(RFI_ANSWERS):
            yield model_event(self.name, "Research already complete — using stored answers.")
            return

        cfg = settings()
        guidance = state.get(RFI_GUIDANCE) or {}
        file_id = state.get(RFI_FILE_ID) or ""
        batches = _chunk(questions, cfg.rfi_research_chunk_size)
        semaphore = asyncio.Semaphore(max(1, cfg.rfi_research_concurrency))

        log.info(
            "rfi.research: %d questions in %d batch(es), concurrency=%d",
            len(questions), len(batches), cfg.rfi_research_concurrency,
        )

        async def _guarded(batch: list[RFIQuestion]) -> list[RFIAnswer]:
            async with semaphore:
                return await _research_batch(
                    batch, guidance, file_id, cfg.app_name
                )

        results = await asyncio.gather(*(_guarded(b) for b in batches))

        # Merge in original question order. Every question MUST yield an entry:
        # a batch's LLM sometimes returns fewer answers than it was given (it
        # silently omits a question, or answers it with an empty string and no
        # needs_human flag). Such questions have no answer object, so without
        # this reconciliation they'd vanish from BOTH the gap-fill form (which
        # only surfaces needs_human answers) and the assembler's unanswered
        # report — ending up blank in the file with nobody asked to fill them.
        # Flag any missing/empty-and-unflagged question needs_human so Form 2
        # catches it.
        merged_by_id: dict[str, RFIAnswer] = {}
        for batch_answers in results:
            for ans in batch_answers:
                merged_by_id[ans.question_id] = ans
        ordered = []
        for q in questions:
            ans = merged_by_id.get(q.id)
            if ans is None or (not ans.answer.strip() and not ans.needs_human):
                ans = RFIAnswer(question_id=q.id, needs_human=True)
            ordered.append(ans.model_dump())

        answered = sum(1 for a in ordered if a.get("answer", "").strip())
        yield model_event(
            self.name,
            f"Researched {answered} of {len(questions)} question(s) "
            f"across {len(batches)} batch(es); "
            f"{len(questions) - answered} flagged for human gap-fill.",
            **{RFI_ANSWERS: {"answers": ordered}},
        )
