"""CARD_CLICKED handler registry.

Card modules register their submit/dialog handlers here by the action
``function`` string carried on the button's ``onClick``. The package
dispatcher (:func:`chat.cards.handle_card_clicked`) looks a handler up by
that string, falling back to the owner-assignment handler for the
form's legacy empty-function path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import BackgroundTasks

if TYPE_CHECKING:
    from chat.events import CardClickedEvent

CardHandler = Callable[
    ["CardClickedEvent", BackgroundTasks], Awaitable[dict[str, Any]]
]

_CARD_HANDLERS: dict[str, CardHandler] = {}


def register_card(function_name: str) -> Callable[[CardHandler], CardHandler]:
    """Register *function_name* → handler for CARD_CLICKED dispatch."""

    def decorator(handler: CardHandler) -> CardHandler:
        _CARD_HANDLERS[function_name] = handler
        return handler

    return decorator
