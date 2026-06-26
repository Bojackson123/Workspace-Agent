"""Card dispatch registries.

Three string/id-keyed registries let a workflow's card module wire itself in by
decorating its handlers — so adding a workflow never means editing a dispatch
if-ladder:

* ``_CARD_HANDLERS`` — CARD_CLICKED submit/dialog handlers, keyed by the action
  ``function`` string on the button's ``onClick``.
* ``_PREPARE_HOOKS`` — per-``command_id`` pre-run preparation (e.g. RFI file
  intake), run before the agent.
* ``_POST_HOOKS`` — per-``command_id`` result/form posting, run after the
  agent. A default hook handles every workflow without its own.
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
# Pre-run prep: (*, prompt, attachments, session) -> user-facing error or None.
PrepareHook = Callable[..., Awaitable["str | None"]]
# Post-run: (*, space_resource, thread_name, user_email, session_id, reply) -> None.
PostHook = Callable[..., Awaitable[None]]

_CARD_HANDLERS: dict[str, CardHandler] = {}
_PREPARE_HOOKS: dict[int, PrepareHook] = {}
_POST_HOOKS: dict[int, PostHook] = {}
_DEFAULT_POST: list[PostHook] = []  # single-slot; set via register_default_post


def register_card(function_name: str) -> Callable[[CardHandler], CardHandler]:
    """Register *function_name* → handler for CARD_CLICKED dispatch."""

    def decorator(handler: CardHandler) -> CardHandler:
        _CARD_HANDLERS[function_name] = handler
        return handler

    return decorator


def register_prepare(command_id: int) -> Callable[[PrepareHook], PrepareHook]:
    """Register a pre-run preparation hook for *command_id*."""

    def decorator(hook: PrepareHook) -> PrepareHook:
        _PREPARE_HOOKS[command_id] = hook
        return hook

    return decorator


def register_post(command_id: int) -> Callable[[PostHook], PostHook]:
    """Register a post-run result/form hook for *command_id*."""

    def decorator(hook: PostHook) -> PostHook:
        _POST_HOOKS[command_id] = hook
        return hook

    return decorator


def register_default_post(hook: PostHook) -> PostHook:
    """Register the fallback post-run hook for workflows without their own."""
    _DEFAULT_POST[:] = [hook]
    return hook


def get_prepare_hook(command_id: int) -> PrepareHook | None:
    return _PREPARE_HOOKS.get(command_id)


def get_post_hook(command_id: int) -> PostHook:
    hook = _POST_HOOKS.get(command_id)
    if hook is not None:
        return hook
    if not _DEFAULT_POST:
        raise RuntimeError("No default post hook registered (chat.cards.meeting).")
    return _DEFAULT_POST[0]
