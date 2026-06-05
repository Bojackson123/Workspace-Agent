"""Deterministic gate agent base class.

GateAgent is a pure-Python BaseAgent that runs a list of check functions
against session state and writes a structured verdict. It makes NO model
calls — gate logic must be expressed as Python predicates, not LLM judgment.

Usage::

    from workflows.common.gate import GateAgent, GateCheck

    def check_owners(state: dict) -> GateCheck:
        parsed = ParsedMeeting.model_validate(state[MTG_PARSED])
        missing = [a.id for a in parsed.action_items if not a.owner]
        return GateCheck(
            id="owners_assigned",
            passed=not missing,
            severity="BLOCKER",
            detail=f"Unowned items: {missing}" if missing else "All items owned.",
        )

    gate = GateAgent(
        name="meeting_gate",
        checks=[check_owners, ...],
        verdict_key=MTG_GATE_VERDICT,
        failed_key=MTG_GATE_FAILED,
    )
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import BaseModel, PrivateAttr


class GateCheck(BaseModel):
    id: str
    passed: bool
    severity: Literal["BLOCKER", "WARNING"]
    detail: str


class GateVerdict(BaseModel):
    passed: bool  # False if any BLOCKER failed
    checks: list[GateCheck]


class GateAgent(BaseAgent):
    """Pure-Python gate agent — no LLM calls.

    Args:
        checks: List of callables ``(state: dict) -> GateCheck``. Each
            receives the full session state dict and returns one check result.
        verdict_key: State key under which the ``GateVerdict`` dict is stored.
        failed_key: State key set to ``True`` when any BLOCKER fails.
    """

    verdict_key: str
    failed_key: str
    model_config = {"arbitrary_types_allowed": True}

    # PrivateAttr is invisible to all Pydantic serializers (including ADK's
    # build_graph introspection), unlike Field(exclude=True) which only covers
    # model_dump(). Callables cannot be JSON-serialised so they must be private.
    _checks: list[Callable[[dict], GateCheck]] = PrivateAttr()

    def __init__(
        self,
        *,
        checks: list[Callable[[dict], GateCheck]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._checks = checks

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        results: list[GateCheck] = []
        for check_fn in self._checks:
            try:
                result = check_fn(state)
            except Exception as exc:
                result = GateCheck(
                    id=getattr(check_fn, "__name__", "unknown"),
                    passed=False,
                    severity="BLOCKER",
                    detail=f"Check raised an exception: {exc}",
                )
            results.append(result)

        any_blocker_failed = any(
            not r.passed and r.severity == "BLOCKER" for r in results
        )
        verdict = GateVerdict(passed=not any_blocker_failed, checks=results)

        verdict_dict = verdict.model_dump()
        yield Event(
            author=self.name,
            actions=EventActions(
                state_delta={
                    self.verdict_key: verdict_dict,
                    self.failed_key: not verdict.passed,
                }
            ),
            content=types.Content(
                role="model",
                parts=[types.Part(text=json.dumps(verdict_dict, indent=2))],
            ),
        )
