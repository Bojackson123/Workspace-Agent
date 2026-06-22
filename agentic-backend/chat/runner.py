"""Agent execution and the background tasks that post replies back to Chat.

Webhook handlers return immediately (an ack or ``{}``); the actual agent
run happens here, on FastAPI's background-task queue, so a long workflow
never holds the request open past Chat's "not responding" timer. Slash
invocations follow the public-anchor flow (a fresh top-level thread is
created, the ADK session is keyed to it, and every reply is posted into
it); plain messages reply straight into their existing thread.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Final

from google.adk.runners import Runner
from google.genai import types
from opentelemetry import trace as otel_trace

from access_store import AccessStore
from agent import build_agent_for_workflow
from chat.formatting import _markdown_to_chat
from chat.stores import _access_store, _session_store
from chat_client import post_message_to_space
from config import settings
from workflows import DEFAULT_WORKFLOW, Workflow, get_workflow
from workflows.common.state_keys import (
    MTG_CALENDAR_HOLDS,
    MTG_EMAIL_DRAFTS,
    MTG_PARSED,
)

log = logging.getLogger(__name__)
_tracer = otel_trace.get_tracer(__name__)

_FOLLOWUP_HINT: Final = "_Follow up messages will reply to this thread._"

_GENERIC_REPLY_FAILED: Final = (
    "I ran into an unexpected error while processing that request."
)


def _workflow_for(active_workflow_id: int | None) -> Workflow:
    """Resolve an ``active_workflow_id`` (possibly stale) to a Workflow."""
    if active_workflow_id is None:
        return DEFAULT_WORKFLOW
    workflow = get_workflow(active_workflow_id)
    if workflow is None:
        log.warning(
            "Active session references unknown workflow id=%s; "
            "falling back to default.",
            active_workflow_id,
        )
        return DEFAULT_WORKFLOW
    return workflow


def _build_anchor_text(
    *,
    user_resource: str,
    user_display: str,
    workflow: Workflow,
) -> str:
    """Build the public anchor message that starts a slash workflow.

    The user is mentioned (with a clickable ``<users/ID>`` annotation
    when we have their resource name, or just their display name as a
    fallback) so the run shows up in their mentions. The workflow's
    ack message is the body. A trailing hint tells the space that
    follow-ups belong inside the thread we're about to create.
    """
    if user_resource:
        mention = f"<{user_resource}>"
    elif user_display:
        mention = user_display
    else:
        mention = "Someone"
    body = workflow.ack_message or f"Working on `{workflow.command_name}`..."
    return f"{mention} {body}\n\n{_FOLLOWUP_HINT}"


async def _run_slash_workflow(
    *,
    workflow: Workflow,
    user_email: str,
    user_resource: str,
    user_display: str,
    prompt: str,
    space_resource: str,
    attachments: tuple[Any, ...] = (),
) -> None:
    """Background task for slash-command invocations (Option C anchor flow).

    Steps:
      1. Post a public anchor message in the space (no thread targeting).
         Chat creates a new top-level thread; the REST response carries
         its ``thread.name`` back to us.
      2. Resolve an ADK session keyed by that thread, with the slash
         command's workflow as the active workflow. This is the session
         the agent writes to and that future plain-message follow-ups
         inside the thread will reuse.
      3. Run any per-workflow preparation (e.g. RFI file intake), then
         the agent, then post the result — both delegated to the card
         layer, which owns the suspend/resume form UI.
    """
    # Imported lazily: the card layer imports this module for the resume
    # handlers, so a top-level import here would be circular.
    from chat import cards

    anchor_text = _build_anchor_text(
        user_resource=user_resource,
        user_display=user_display,
        workflow=workflow,
    )
    anchor_thread = await post_message_to_space(space_resource, anchor_text)
    if not anchor_thread:
        log.error(
            "Anchor post failed; aborting slash workflow user=%s command=%s",
            user_email,
            workflow.command_name,
        )
        return

    try:
        resolved = await _session_store.resolve(
            user_email=user_email,
            thread_name=anchor_thread,
            new_workflow_id=workflow.command_id,
        )
    except Exception:
        log.exception(
            "Session resolve failed for anchor thread=%s user=%s",
            anchor_thread,
            user_email,
        )
        await post_message_to_space(
            space_resource,
            _GENERIC_REPLY_FAILED,
            thread_name=anchor_thread,
        )
        return

    prep_error = await cards.prepare_slash_workflow(
        workflow=workflow,
        prompt=prompt,
        attachments=attachments,
        session=resolved.session,
    )
    if prep_error:
        await post_message_to_space(
            space_resource, prep_error, thread_name=anchor_thread
        )
        return

    try:
        reply = await _run_agent(
            workflow=workflow,
            user_email=user_email,
            session_id=resolved.session.id,
            prompt=prompt,
        )
    except Exception:
        log.exception(
            "Background agent run failed: workflow=%s user=%s",
            workflow.command_name,
            user_email,
        )
        await post_message_to_space(
            space_resource,
            _GENERIC_REPLY_FAILED,
            thread_name=anchor_thread,
        )
        return

    await cards.post_slash_workflow_result(
        workflow=workflow,
        space_resource=space_resource,
        thread_name=anchor_thread,
        user_email=user_email,
        session_id=resolved.session.id,
        reply=reply,
    )


async def _run_plain_workflow(
    *,
    workflow: Workflow,
    user_email: str,
    session_id: str,
    prompt: str,
    space_resource: str,
    message_thread: str,
) -> None:
    """Background task for plain-message continuations.

    Plain messages in non-slash threads can be replied to directly with
    ``thread.name`` — no anchor dance needed. Used for free-form
    follow-ups inside an anchor thread the bot previously created, and
    for plain @mentions / DMs.
    """
    thread_name = message_thread or None
    try:
        reply = await _run_agent(
            workflow=workflow,
            user_email=user_email,
            session_id=session_id,
            prompt=prompt,
        )
    except Exception:
        log.exception(
            "Background agent run failed: workflow=%s user=%s",
            workflow.command_name,
            user_email,
        )
        await post_message_to_space(
            space_resource,
            _GENERIC_REPLY_FAILED,
            thread_name=thread_name,
        )
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to generate a response."
    await post_message_to_space(
        space_resource,
        text,
        thread_name=thread_name,
    )


def _log_adk_event(event: Any, *, workflow: str, session_id: str) -> None:
    """Emit structured log entries for notable ADK pipeline events.

    Three categories are captured:
    - ``adk.tool_call``     — LLM requested a tool; logs name + truncated args.
    - ``adk.tool_response`` — Tool returned; logs name + truncated response.
    - ``adk.state_delta``   — Agent wrote to session state; logs changed keys
                              and value sizes (not the values themselves, which
                              can be large — use Cloud SQL for full inspection).
    """
    base: dict[str, Any] = {
        "workflow": workflow,
        "session_id": session_id,
        "author": event.author,
    }

    if event.actions and event.actions.state_delta:
        delta = event.actions.state_delta
        log.info(
            "adk.state_delta",
            extra={"json_fields": {
                **base,
                "keys_written": list(delta.keys()),
                "value_sizes": {k: len(str(v)) for k, v in delta.items()},
            }},
        )
        # Targeted: whenever mtg_parsed is written, log item owners+dates
        # so we can spot stale overwrites without querying Cloud SQL.
        if MTG_PARSED in delta:
            parsed_val = delta[MTG_PARSED]
            items_snap = []
            if isinstance(parsed_val, dict):
                for i in parsed_val.get("action_items") or []:
                    items_snap.append({
                        "id": i.get("id"),
                        "owner": i.get("owner"),
                        "due_date": i.get("due_date"),
                    })
            log.info(
                "adk.state_delta.mtg_parsed",
                extra={"json_fields": {
                    **base,
                    "items": items_snap,
                    "is_null": parsed_val is None,
                }},
            )
        if MTG_EMAIL_DRAFTS in delta:
            drafts_val = delta[MTG_EMAIL_DRAFTS]
            # ADK output_key stores the raw LLM text as a string; parse it.
            drafts_list = drafts_val
            if isinstance(drafts_list, str):
                try:
                    drafts_list = json.loads(drafts_list)
                except Exception:
                    drafts_list = []
            drafts_snap: list = []
            if isinstance(drafts_list, list):
                for d in drafts_list:
                    if isinstance(d, dict):
                        drafts_snap.append({"owner": d.get("owner"), "subject": d.get("subject")})
            log.info(
                "adk.state_delta.mtg_email_drafts",
                extra={"json_fields": {
                    **base,
                    "count": len(drafts_snap),
                    "drafts": drafts_snap,
                    "is_null": drafts_val is None,
                    "raw_type": type(drafts_val).__name__,
                }},
            )
        if MTG_CALENDAR_HOLDS in delta:
            holds_val = delta[MTG_CALENDAR_HOLDS]
            holds_list = holds_val
            if isinstance(holds_list, str):
                try:
                    holds_list = json.loads(holds_list)
                except Exception:
                    holds_list = []
            holds_snap: list = []
            if isinstance(holds_list, list):
                for h in holds_list:
                    if isinstance(h, dict):
                        holds_snap.append({
                            "action_item_id": h.get("action_item_id"),
                            "summary": h.get("summary"),
                            "start_datetime": h.get("start_datetime"),
                        })
            log.info(
                "adk.state_delta.mtg_calendar_holds",
                extra={"json_fields": {
                    **base,
                    "count": len(holds_snap),
                    "holds": holds_snap,
                    "is_null": holds_val is None,
                    "raw_type": type(holds_val).__name__,
                }},
            )

    if not (event.content and event.content.parts):
        return

    for part in event.content.parts:
        fc = getattr(part, "function_call", None)
        if fc:
            raw_args = dict(fc.args) if fc.args else {}
            args_preview = {
                k: (str(v)[:300] + "…" if len(str(v)) > 300 else v)
                for k, v in raw_args.items()
            }
            log.info(
                "adk.tool_call",
                extra={"json_fields": {**base, "tool": fc.name, "args": args_preview}},
            )

        fr = getattr(part, "function_response", None)
        if fr:
            resp_str = str(fr.response or "")
            log.info(
                "adk.tool_response",
                extra={"json_fields": {
                    **base,
                    "tool": fr.name,
                    "response_preview": resp_str[:300] + "…" if len(resp_str) > 300 else resp_str,
                }},
            )


async def _run_agent(
    *,
    workflow: Workflow,
    user_email: str,
    session_id: str,
    prompt: str,
) -> str:
    """Drive a single prompt through a workflow-scoped ADK agent."""
    cfg = settings()
    agent = await build_agent_for_workflow(workflow, user_email)

    runner = Runner(
        agent=agent,
        app_name=cfg.app_name,
        session_service=_session_store.service,
    )

    message = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    with _tracer.start_as_current_span(
        "adk_run",
        attributes={
            "workflow": workflow.command_name,
            "session.id": session_id,
        },
    ):
        log.info(
            "adk.run_start",
            extra={"json_fields": {
                "workflow": workflow.command_name,
                "session_id": session_id,
                "user": user_email,
            }},
        )

        final_text = ""
        async for event in runner.run_async(
            user_id=user_email,
            session_id=session_id,
            new_message=message,
        ):
            _log_adk_event(event, workflow=workflow.command_name, session_id=session_id)
            if event.is_final_response() and event.content and event.content.parts:
                final_text = event.content.parts[0].text or ""

        log.info(
            "adk.run_end",
            extra={"json_fields": {
                "workflow": workflow.command_name,
                "session_id": session_id,
            }},
        )
    return final_text


def get_access_store() -> AccessStore:
    """Expose the process-wide :class:`AccessStore` to auxiliary tools."""
    return _access_store
