"""Google Chat webhook handling.

Translates an inbound Chat payload into a workflow-scoped agent run and
renders the agent's response into the JSON envelope Chat expects back.
The package is organised as:

- :mod:`chat.events` — typed views of inbound payloads and their parsing.
- :mod:`chat.formatting` — outbound envelope + markdown→Chat translation.
- :mod:`chat.dispatch` — top-level event routing (the public entry point).
- :mod:`chat.reserved` — ``/exit``/``/help``/admin commands (no LLM).
- :mod:`chat.runner` — agent execution and the reply-posting background tasks.
- :mod:`chat.cards` — the suspend/resume card and dialog form workflows.
- :mod:`chat.stores` — the process-wide session and access-rule singletons.
"""

from __future__ import annotations

from chat.dispatch import handle_event
from chat.events import CardClickedEvent, ChatEvent
from chat.runner import get_access_store

__all__ = [
    "CardClickedEvent",
    "ChatEvent",
    "get_access_store",
    "handle_event",
]
