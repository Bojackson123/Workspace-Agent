"""A guard wrapper that runs its inner agent only when a condition is unmet.

Several pipelines wrap a stage so it is skipped on re-runs or while a form is
pending — e.g. "don't re-parse if the questions are already in state", "don't
fan out while the owner gate is PENDING". :class:`GuardAgent` captures that
shape: when ``skip_when(state)`` is true it emits a short status event (and may
re-emit a stored value to keep it in conversation history) instead of
delegating to the wrapped agent.

This does NOT replace agents that perform real work or set state on the
"taken" branch (gates, the RFI research fan-out) — only the pure
skip-or-delegate wrappers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from pydantic import PrivateAttr

from workflows.common.events import model_event


class GuardAgent(BaseAgent):
    """Delegate to the wrapped agent unless *skip_when* says to skip.

    Args:
        skip_when: ``(state: dict) -> bool``. When true, the wrapped agent is
            NOT run; a status event carrying *skip_text* is emitted instead.
        skip_text: the status line emitted on the skip branch.
        sub_agent: the agent to run when not skipping.
        restore_key: when set, the skip event re-emits ``state[restore_key]`` as
            a state delta — used by the parser guards to keep the stored value
            authoritative in conversation history rather than letting a later
            agent regenerate it.
    """

    model_config = {"arbitrary_types_allowed": True}

    # PrivateAttr keeps the callable out of Pydantic/ADK graph serialization
    # (see LoopExitChecker for the same rationale).
    _skip_when: Callable[[dict], bool] = PrivateAttr()
    _skip_text: str = PrivateAttr()
    _restore_key: str | None = PrivateAttr(default=None)

    def __init__(
        self,
        *,
        skip_when: Callable[[dict], bool],
        skip_text: str,
        sub_agent: BaseAgent,
        restore_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(sub_agents=[sub_agent], **kwargs)
        self._skip_when = skip_when
        self._skip_text = skip_text
        self._restore_key = restore_key

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        if self._skip_when(state):
            delta = (
                {self._restore_key: state.get(self._restore_key)}
                if self._restore_key is not None
                else {}
            )
            yield model_event(self.name, self._skip_text, **delta)
            return
        # Delegate via run_async (not _run_async_impl) so the ADK framework
        # updates ctx.agent to the inner agent before entering its flow.
        async for event in self.sub_agents[0].run_async(ctx):
            yield event
