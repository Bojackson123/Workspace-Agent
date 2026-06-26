"""Idempotent deterministic assembler base class.

The final stage of several engines writes the workflow's output (a filled
file, a doc, …) and must be safe to re-run: a card resume re-runs the whole
pipeline, so the assembler has to report the existing artifact instead of
duplicating it. The shape is always:

1. If a completed marker is already in ``status_key`` → report the existing
   link and stop.
2. Otherwise do the work; on success record the link(s) and stamp the marker.
3. On a recoverable problem, emit a plain message and stop *without* stamping
   the marker (so a retry can still complete).

:class:`IdempotentAssembler` captures steps 1–3; subclasses implement only
:meth:`assemble`, returning an :class:`AssemblyResult` on success or raising
:class:`AssemblyAbort` to surface a message without marking the run complete.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from workflows.common.events import model_event


@dataclass
class AssemblyResult:
    """A successful assembly: the summary to post and any state to persist.

    The completed marker is added to ``delta`` by the base class — subclasses
    only supply the artifact-specific keys (link, file id, …).
    """

    summary_lines: list[str]
    delta: dict[str, Any] = field(default_factory=dict)


class AssemblyAbort(Exception):
    """Raise from :meth:`assemble` to emit *message* and stop.

    The completed marker is NOT written, so the run remains retryable (e.g. a
    transient write failure, or missing inputs the user can still supply).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class IdempotentAssembler(BaseAgent):
    """Deterministic assembler with a completed-marker short-circuit.

    Args:
        status_key: state key holding the completion marker.
        completed_marker: substring written into ``status_key`` once done; its
            presence on a later run short-circuits to the "already complete"
            message.
        link_key: optional state key holding the artifact link, appended to the
            "already complete" message when present.
        already_text: the "already complete" message body.
    """

    status_key: str
    completed_marker: str
    link_key: str | None = None
    already_text: str = (
        "This workflow is already complete in this thread. Use /exit to start fresh."
    )

    @abstractmethod
    async def assemble(self, state: dict) -> AssemblyResult:
        """Produce the artifact and return its summary + state delta.

        Raise :class:`AssemblyAbort` to emit a message and stop without marking
        the run complete.
        """
        raise NotImplementedError

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        if self.completed_marker in (state.get(self.status_key) or ""):
            link = state.get(self.link_key) if self.link_key else None
            yield model_event(
                self.name,
                self.already_text + (f" {link}" if link else ""),
            )
            return

        try:
            result = await self.assemble(state)
        except AssemblyAbort as abort:
            yield model_event(self.name, abort.message)
            return

        delta = {**result.delta, self.status_key: self.completed_marker}
        yield model_event(self.name, "\n".join(result.summary_lines), **delta)
