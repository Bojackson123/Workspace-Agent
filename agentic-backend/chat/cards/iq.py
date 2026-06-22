"""Customer IQ card UI: the optional tailoring form, seeding, and resume.

``/iq`` posts an optional tailoring form first; submitting it (or
skipping) resolves the gate and runs the research → dossier pipeline.
The company name is seeded into state up front so the post-form resume
run — which carries a generic prompt — still knows the subject.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from fastapi import BackgroundTasks

from chat import runner
from chat.cards.common import (
    _append_state,
    _confirmation_card_body,
    _form_text,
    _form_values,
)
from chat.cards.registry import register_card
from chat.events import CardClickedEvent
from chat.formatting import _markdown_to_chat
from chat.stores import _session_store
from chat_client import post_card_to_space, post_message_to_space
from config import settings
from sessions import STATE_ACTIVE_WORKFLOW_ID, _session_id_for
from workflows import Workflow
from workflows.common.state_keys import (
    IQ_COMPANY_NAME,
    IQ_TAILOR,
    IQ_TAILOR_CARD_MSG,
    IQ_TAILOR_STATE,
)
from workflows.iq_engine import SANMINA_CAPABILITIES

log = logging.getLogger(__name__)

_IQ_TAILOR_FUNCTION: Final = "iq_tailor_submit"


async def prepare_iq(*, prompt: str, session: Any) -> str | None:
    """Seed ``IQ_COMPANY_NAME`` from the slash prompt before the pipeline runs.

    Returns ``None`` on success or a user-facing error string to post. The
    company must live in state because the resume run (after the form) carries a
    generic prompt, not the company name.
    """
    company = (prompt or "").strip()
    if not company:
        return "Tell me which company to profile, e.g. `/iq Acme Corp`."
    await _append_state(session, {IQ_COMPANY_NAME: company})
    return None


def _build_iq_tailor_card(company: str, invoker_email: str) -> dict:
    """Optional tailoring form posted as the first response to ``/iq``.

    Every lever is optional; the "Skip — use defaults" button resolves the gate
    with empty tailoring so the run reproduces the pre-form behaviour.
    """
    segment_items = [
        {"text": label, "value": value} for value, label in SANMINA_CAPABILITIES
    ]
    purpose_items = [
        {"text": "General (default)", "value": "general", "selected": True},
        {"text": "Cold outreach prep", "value": "cold_outreach"},
        {"text": "QBR / account-review prep", "value": "qbr"},
        {"text": "Executive briefing", "value": "exec_briefing"},
    ]
    source_items = [
        {"text": "Shared Drive + web (default)", "value": "drive_web", "selected": True},
        {"text": "Web only", "value": "web_only"},
        {"text": "Shared Drive only", "value": "drive_only"},
    ]
    widgets: list[dict] = [
        {"selectionInput": {
            "name": "segments",
            "label": "Segment lens — capabilities to score fit against (optional)",
            "type": "CHECK_BOX",
            "items": segment_items,
        }},
        {"textInput": {
            "name": "context",
            "label": "What you already know (optional)",
            "hintText": "e.g. met their VP Ops at a trade show; standing up a Texas plant",
            "type": "MULTIPLE_LINE",
        }},
        {"selectionInput": {
            "name": "purpose",
            "label": "Purpose / audience",
            "type": "DROPDOWN",
            "items": purpose_items,
        }},
        {"textInput": {
            "name": "geo",
            "label": "Geographic focus (optional)",
            "hintText": "e.g. North America, EMEA, Penang",
            "type": "SINGLE_LINE",
        }},
        {"selectionInput": {
            "name": "sources",
            "label": "Data sources",
            "type": "DROPDOWN",
            "items": source_items,
        }},
        {"buttonList": {"buttons": [
            {"text": "Generate dossier", "onClick": {"action": {
                "function": _IQ_TAILOR_FUNCTION,
                "parameters": [
                    {"key": "invoker_email", "value": invoker_email},
                    {"key": "decision", "value": "apply"},
                ],
            }}},
            {"text": "Skip — use defaults", "onClick": {"action": {
                "function": _IQ_TAILOR_FUNCTION,
                "parameters": [
                    {"key": "invoker_email", "value": invoker_email},
                    {"key": "decision", "value": "skip"},
                ],
            }}},
        ]}},
    ]
    return {"cardsV2": [{
        "cardId": "iq_tailor",
        "card": {
            "header": {
                "title": "Tailor the Customer IQ",
                "subtitle": company or "Customer IQ",
            },
            "sections": [{"widgets": widgets}],
        },
    }]}


async def post_iq_result(
    *,
    space_resource: str,
    thread_name: str,
    user_email: str,
    session_id: str,
    reply: str,
) -> None:
    """Post the tailoring form (if still pending) or the final dossier reply."""
    cfg = settings()
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=user_email, session_id=session_id,
    )
    state = session.state if session else {}

    if state.get(IQ_TAILOR_STATE) == "PENDING":
        card = _build_iq_tailor_card(state.get(IQ_COMPANY_NAME) or "", user_email)
        msg = await post_card_to_space(space_resource, card, thread_name=thread_name)
        if msg and session:
            await _append_state(session, {IQ_TAILOR_CARD_MSG: msg})
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to complete the Customer IQ workflow."
    await post_message_to_space(space_resource, text, thread_name=thread_name)


@register_card(_IQ_TAILOR_FUNCTION)
async def _handle_iq_tailor_submit(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Apply the tailoring selections (or skip) and re-run the /iq pipeline."""
    if not evt.invoker_email or not evt.thread_name:
        return {}
    cfg = settings()
    session_id = _session_id_for(evt.thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=evt.invoker_email, session_id=session_id,
    )
    if session is None:
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "This form has expired. Run `/iq <company>` again to start fresh."
        )}
    if session.state.get(IQ_TAILOR_STATE) != "PENDING":
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "Already processed."
        )}

    if evt.decision == "skip":
        tailor: dict[str, Any] = {}
        note = "Skipping tailoring — researching with defaults now. I'll post the dossier here."
    else:
        tailor = {
            "segments": _form_values(evt.form_inputs, "segments"),
            "context": _form_text(evt.form_inputs, "context"),
            "purpose": _form_text(evt.form_inputs, "purpose"),
            "geo": _form_text(evt.form_inputs, "geo"),
            "sources": _form_text(evt.form_inputs, "sources"),
        }
        note = "Got it — researching with your tailoring now. I'll post the dossier here."

    await _append_state(session, {IQ_TAILOR: tailor, IQ_TAILOR_STATE: "RESOLVED"})

    workflow = runner._workflow_for(session.state.get(STATE_ACTIVE_WORKFLOW_ID))
    background_tasks.add_task(
        _resume_iq_after_card,
        workflow=workflow,
        invoker_email=evt.invoker_email,
        space_resource=evt.space_resource,
        thread_name=evt.thread_name,
        session_id=session_id,
    )
    return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(note)}


async def _resume_iq_after_card(
    *,
    workflow: Workflow,
    invoker_email: str,
    space_resource: str,
    thread_name: str,
    session_id: str,
) -> None:
    """Background task: re-run the /iq pipeline after the tailoring form."""
    reply = ""
    try:
        reply = await runner._run_agent(
            workflow=workflow,
            user_email=invoker_email,
            session_id=session_id,
            prompt="(Customer IQ tailoring submitted via card — please continue)",
        )
    except Exception:
        log.exception(
            "IQ card resume failed: workflow=%s user=%s",
            workflow.command_name, invoker_email,
        )

    await post_iq_result(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=invoker_email,
        session_id=session_id,
        reply=reply,
    )
