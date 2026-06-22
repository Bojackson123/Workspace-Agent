"""Helpers for emitting ADK events from pure-Python pipeline agents."""

from __future__ import annotations

from google.adk.events import Event, EventActions
from google.genai import types


def model_event(author: str, text: str, **delta: object) -> Event:
    """Build a model-role :class:`Event`, optionally carrying a state delta.

    Pure-Python pipeline agents (gates, conditional guards) use this to surface
    a short status line to the conversation and, when ``delta`` is given, patch
    session state in the same event.
    """
    actions = EventActions(state_delta=dict(delta)) if delta else EventActions()
    return Event(
        author=author,
        actions=actions,
        content=types.Content(role="model", parts=[types.Part(text=text)]),
    )
