"""Top-level routing for inbound Google Chat events.

For each ``MESSAGE`` we parse the slash command (if any), handle
reserved commands inline, authorize the caller against the workflow's
table-backed :class:`AccessPolicy` (the Chat UI can't hide commands
per-user, so denials must happen here), and enqueue the agent run as a
background task. ``CARD_CLICKED`` events are routed to the card layer.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks

from access import authorize
from chat.cards import handle_card_clicked
from chat.events import (
    EVENT_ADDED_TO_SPACE,
    EVENT_CARD_CLICKED,
    EVENT_MESSAGE,
    WELCOME_MESSAGE,
    ChatEvent,
)
from chat.formatting import chat_text
from chat.reserved import _handle_admin_command, _handle_exit, _handle_help
from chat.runner import _run_plain_workflow, _run_slash_workflow, _workflow_for
from chat.stores import _access_store, _session_store
from workflows import (
    ADMIN_COMMAND_IDS,
    RESERVED_EXIT_COMMAND_ID,
    RESERVED_HELP_COMMAND_ID,
    get_workflow,
)

log = logging.getLogger(__name__)


async def handle_event(
    body: dict, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Route an inbound Chat payload to the right handler.

    *background_tasks* is FastAPI's per-request queue: agent runs are
    enqueued here and executed after the webhook returns its sync
    response, so a long workflow no longer holds the request open
    past Chat's "not responding" timer.
    """
    event = ChatEvent.from_payload(body)

    if event.event_type == EVENT_ADDED_TO_SPACE:
        return chat_text(WELCOME_MESSAGE)

    if event.event_type == EVENT_MESSAGE:
        return await _handle_message(event, background_tasks)

    if event.event_type == EVENT_CARD_CLICKED:
        return await handle_card_clicked(body, background_tasks)

    return chat_text(f"Event type {event.event_type!r} is not supported.")


async def _handle_message(
    event: ChatEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Dispatch a ``MESSAGE`` event to the appropriate workflow.

    Reserved commands (``/help``, ``/exit``, ``/grant`` and friends)
    are fast and stay fully synchronous. Anything that runs the LLM
    goes through the background-task path: we return either the
    workflow's ``ack_message`` or an empty envelope immediately, and
    the agent's final reply is posted back via the Chat REST API.
    """
    if not event.user_email:
        return chat_text("Error: Could not extract user email from payload.")

    # ---- Reserved commands that bypass the LLM ----
    if event.slash_command_id == RESERVED_EXIT_COMMAND_ID:
        return await _handle_exit(event)
    if event.slash_command_id == RESERVED_HELP_COMMAND_ID:
        return _handle_help(event.user_email)
    if event.slash_command_id in ADMIN_COMMAND_IDS:
        return await _handle_admin_command(event)

    # ---- Slash command: Option C — anchor a fresh public thread ----
    # Slash commands are private to the invoker by Chat's design, so
    # we can't post bot replies into the slash thread (Chat rejects
    # private→private threaded replies). Instead the background task
    # posts a NEW public top-level message in the space, captures the
    # returned thread.name, anchors the ADK session there, and routes
    # all subsequent replies (ack + final + follow-ups) into that
    # thread. The user's slash invocation stays private; the bot's
    # work becomes visible in the space.
    if event.slash_command_id is not None:
        workflow = get_workflow(event.slash_command_id)
        if workflow is None:
            log.warning(
                "Unknown slash command id=%s from %s",
                event.slash_command_id,
                event.user_email,
            )
            return chat_text(
                f"Unknown slash command (id={event.slash_command_id}). "
                "Type `/help` to see available commands."
            )
        decision = await authorize(
            event.user_email,
            workflow.command_id,
            workflow.default_access,
            _access_store,
        )
        if not decision.allowed:
            log.warning(
                "Access denied: user=%s command=%s reason=%s",
                event.user_email,
                workflow.command_name,
                decision.reason,
            )
            return chat_text(
                f"`{workflow.command_name}` is {decision.reason}. "
                "Contact your admin if you need access."
            )
        log.info(
            "Scheduling slash %s for user=%s",
            workflow.command_name,
            event.user_email,
        )
        background_tasks.add_task(
            _run_slash_workflow,
            workflow=workflow,
            user_email=event.user_email,
            user_resource=event.user_resource,
            user_display=event.user_display,
            prompt=event.prompt,
            space_resource=event.space_resource,
            attachments=event.attachments,
        )
        return {}

    # ---- Plain message: continue an existing thread ----
    try:
        resolved = await _session_store.resolve(
            user_email=event.user_email,
            thread_name=event.thread_name,
            new_workflow_id=None,
        )
    except Exception:
        log.exception("Failed to resolve session for %s", event.user_email)
        return chat_text(
            "I ran into an unexpected error while processing that request."
        )

    workflow = _workflow_for(resolved.active_workflow_id)
    log.info(
        "Scheduling plain %s for user=%s thread=%s (new_session=%s)",
        workflow.command_name,
        event.user_email,
        event.thread_name,
        resolved.is_new,
    )

    background_tasks.add_task(
        _run_plain_workflow,
        workflow=workflow,
        user_email=event.user_email,
        session_id=resolved.session.id,
        prompt=event.prompt,
        space_resource=event.space_resource,
        message_thread=event.message_thread,
    )

    return {}
