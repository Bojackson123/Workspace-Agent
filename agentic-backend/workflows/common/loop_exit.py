"""Loop exit checker agent.

LoopExitChecker is placed last inside an ADK LoopAgent. When the exit
predicate returns True, it yields an escalate event to break the loop.
It makes NO model calls — exit logic is pure Python.

Usage::

    from workflows.common.loop_exit import LoopExitChecker

    def no_open_critical(state: dict) -> bool:
        ledger_data = state.get(RVW_LEDGER)
        if not ledger_data:
            return False
        ledger = ObjectionLedger.model_validate(ledger_data)
        return not any(
            o for o in ledger.objections
            if o.severity in {"CRITICAL", "MAJOR"}
            and o.status in {"OPEN", "DISPUTED"}
        )

    checker = LoopExitChecker(
        name="review_loop_exit",
        exit_predicate=no_open_critical,
    )
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types
from pydantic import PrivateAttr


class LoopExitChecker(BaseAgent):
    """Escalates (exits the LoopAgent) when *exit_predicate* returns True.

    Args:
        exit_predicate: ``(state: dict) -> bool``. Called with the full
            session state. Return True to break the loop.
    """

    model_config = {"arbitrary_types_allowed": True}

    # PrivateAttr is invisible to all Pydantic serializers — callables must
    # not appear in ADK's build_graph introspection or state serialization.
    _exit_predicate: Callable[[dict], bool] = PrivateAttr()

    def __init__(
        self,
        *,
        exit_predicate: Callable[[dict], bool],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._exit_predicate = exit_predicate

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        should_exit = False
        try:
            should_exit = self._exit_predicate(state)
        except Exception:
            pass  # predicate failure → keep looping

        if should_exit:
            yield Event(
                author=self.name,
                actions=EventActions(escalate=True),
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Exit condition met — stopping loop.")],
                ),
            )
        else:
            yield Event(
                author=self.name,
                content=types.Content(
                    role="model",
                    parts=[types.Part(text="Loop continues — open objections remain.")],
                ),
            )
