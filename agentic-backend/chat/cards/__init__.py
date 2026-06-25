"""Card / form workflows for Google Chat.

Importing this package registers every CARD_CLICKED handler (via the
``@register_card`` decorators in the submodules). It also exposes the
slash-runner bridge — :func:`prepare_slash_workflow` and
:func:`post_slash_workflow_result` — which route per-workflow
attachment intake and result/form posting to the right submodule.
"""

from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks

from chat.cards import iq, meeting, rfi
from chat.cards.common import _IQ_COMMAND_ID, _RFI_COMMAND_ID
from chat.cards.registry import _CARD_HANDLERS
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

    Registered functions (invite dialog, RFI/IQ form submits) dispatch
    directly; everything else — including the owner-assignment form,
    which carries no distinct function — falls back to the owner handler.
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
    if workflow.command_id == _RFI_COMMAND_ID:
        return await rfi.prepare_rfi_file(attachments=attachments, session=session)
    if workflow.command_id == _IQ_COMMAND_ID:
        return await iq.prepare_iq(prompt=prompt, session=session)
    return None


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
    if workflow.command_id == _RFI_COMMAND_ID:
        await rfi.post_rfi_result(
            space_resource=space_resource,
            thread_name=thread_name,
            user_email=user_email,
            session_id=session_id,
            reply=reply,
        )
        return
    if workflow.command_id == _IQ_COMMAND_ID:
        await iq.post_iq_result(
            space_resource=space_resource,
            thread_name=thread_name,
            user_email=user_email,
            session_id=session_id,
            reply=reply,
        )
        return
    await meeting.post_default_result(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=user_email,
        session_id=session_id,
        reply=reply,
    )
