"""Generalised suspend/resume form gate.

Several engines suspend their pipeline to collect input from the user via a
Chat card, then resume on submit. The pipeline half of that pattern is always
the same shape: read a ``*_STATE`` key, decide whether the form is already
resolved (continue), whether the gate is even active yet (skip), and otherwise
either post the form (set the state to ``PENDING``) or auto-resolve it (e.g.
mark ``SKIPPED`` when there is nothing to ask). The chat layer reacts to the
``PENDING`` marker by posting the card and patches the state to ``RESOLVED`` on
submit before re-running the (idempotent) pipeline.

:class:`FormGate` captures that shape declaratively. It replaces the
hand-written ``GuidanceGate`` / ``GapFillGate`` / ``OwnerAssignmentGate`` —
each is now this one class wired with different predicates.

Decision order in :meth:`_run_async_impl`:

1. ``is_resolved`` → continue (the card was submitted, or work is already done).
2. ``precondition`` set and false → skip silently (the gate isn't active yet —
   e.g. the parser produced nothing, or an upstream form is still pending).
3. ``should_prompt`` → set ``state_key = pending_value`` so chat posts the form.
4. ``auto_value`` set → set ``state_key = auto_value`` (the "nothing to ask,
   carry on" branch, e.g. ``SKIPPED``).
5. otherwise → skip silently.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from pydantic import PrivateAttr

from workflows.common.events import model_event

# A state predicate: reads the full session state, returns a bool.
StatePredicate = Callable[[dict], bool]


def _always(_state: dict) -> bool:
    return True


class FormGate(BaseAgent):
    """Suspend the pipeline for a Chat form, driven by state predicates.

    Args:
        state_key: the ``*_STATE`` session key this gate owns.
        is_resolved: ``(state) -> bool`` — true once the form is submitted (or
            the work it guards is already complete). Pass through.
        should_prompt: ``(state) -> bool`` — true when the form should be
            posted. Defaults to always-true (prompt whenever active and
            unresolved).
        precondition: optional ``(state) -> bool`` — when given and false, the
            gate is inactive and skips silently without touching ``state_key``.
        pending_text: status line emitted on the PENDING (post-the-form) branch.
        pending_value: value written to ``state_key`` to trigger the form
            (default ``"PENDING"``).
        auto_value: optional value written to ``state_key`` when active +
            unresolved but ``should_prompt`` is false (e.g. ``"SKIPPED"``).
        auto_text / resolved_text / skip_text: status lines for the
            auto-resolve, resolved, and skip branches.
    """

    state_key: str
    pending_value: str
    auto_value: str | None
    pending_text: str
    auto_text: str
    resolved_text: str
    skip_text: str
    model_config = {"arbitrary_types_allowed": True}

    # PrivateAttr keeps the callables out of Pydantic/ADK graph serialization
    # (see GateAgent / GuardAgent for the same rationale).
    _is_resolved: StatePredicate = PrivateAttr()
    _should_prompt: StatePredicate = PrivateAttr()
    _precondition: StatePredicate | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        state_key: str,
        is_resolved: StatePredicate,
        should_prompt: StatePredicate = _always,
        precondition: StatePredicate | None = None,
        pending_text: str,
        pending_value: str = "PENDING",
        auto_value: str | None = None,
        auto_text: str = "",
        resolved_text: str = "",
        skip_text: str = "",
        **kwargs: Any,
    ) -> None:
        super().__init__(
            state_key=state_key,
            pending_value=pending_value,
            auto_value=auto_value,
            pending_text=pending_text,
            auto_text=auto_text,
            resolved_text=resolved_text,
            skip_text=skip_text,
            **kwargs,
        )
        self._is_resolved = is_resolved
        self._should_prompt = should_prompt
        self._precondition = precondition

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        if self._is_resolved(state):
            yield model_event(
                self.name, self.resolved_text or f"{self.name}: resolved — continuing."
            )
            return

        if self._precondition is not None and not self._precondition(state):
            yield model_event(
                self.name, self.skip_text or f"{self.name}: skipped — not active yet."
            )
            return

        if self._should_prompt(state):
            yield model_event(
                self.name,
                self.pending_text,
                **{self.state_key: self.pending_value},
            )
            return

        if self.auto_value is not None:
            yield model_event(
                self.name,
                self.auto_text or f"{self.name}: {self.auto_value} — continuing.",
                **{self.state_key: self.auto_value},
            )
            return

        yield model_event(
            self.name, self.skip_text or f"{self.name}: nothing to prompt — continuing."
        )
