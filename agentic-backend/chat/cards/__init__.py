"""Card / form workflows for Google Chat.

Importing this package registers every card handler and dispatch hook (via the
``@register_*`` decorators in the submodules). It exposes the slash-runner
bridge — :func:`prepare_slash_workflow` and :func:`post_slash_workflow_result`
— which route per-workflow attachment intake and result/form posting through
the ``command_id``-keyed registries in :mod:`chat.cards.registry`. A workflow
opts into card behaviour by decorating its hooks; the dispatch here is generic.
"""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks

from chat.cards import meeting, rfi  # noqa: F401 — import registers hooks/handlers
from chat.cards.registry import (
    _CARD_HANDLERS,
    get_post_hook,
    get_prepare_hook,
)
from chat.events import CardClickedEvent
from workflows import Workflow

__all__ = [
    "handle_card_clicked",
    "prepare_slash_workflow",
    "post_slash_workflow_result",
]


async def handle_card_clicked(
    body: dict, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Route a CARD_CLICKED event to its handler by invoked action function.

    Registered functions (invite dialog, RFI form submits) dispatch directly;
    everything else — including the owner-assignment form, which carries no
    distinct function — falls back to the owner handler.
    """
    evt = CardClickedEvent.from_payload(body)
    handler = _CARD_HANDLERS.get(evt.invoked_function, meeting.handle_owner_assignment)
    return await handler(evt, background_tasks)


async def prepare_slash_workflow(
    *,
    workflow: Workflow,
    prompt: str,
    attachments: tuple[Any, ...],
    session: Any,
) -> str | None:
    """Run any per-workflow preparation before the agent runs.

    Returns a user-facing error string to post (and abort) on failure, or
    ``None`` when there's nothing to prepare or prep succeeded.
    """
    hook = get_prepare_hook(workflow.command_id)
    if hook is None:
        return None
    return await hook(prompt=prompt, attachments=attachments, session=session)


async def post_slash_workflow_result(
    *,
    workflow: Workflow,
    space_resource: str,
    thread_name: str,
    user_email: str,
    session_id: str,
    reply: str,
) -> None:
    """Post the agent's result, branching to each workflow's form/UI logic."""
    hook = get_post_hook(workflow.command_id)
    await hook(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=user_email,
        session_id=session_id,
        reply=reply,
    )
