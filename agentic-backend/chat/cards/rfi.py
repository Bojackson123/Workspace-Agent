"""RFI response engine card UI: attachment intake, scope/gap forms, resume.

The ``/rfi`` pipeline suspends twice — once for a scope-guidance form
(before research) and once for a gap-fill form (questions research
couldn't answer). Each submission patches state and re-runs the
(idempotent) pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any, Final

from fastapi import BackgroundTasks

from chat import runner
from chat.cards.common import _append_state, _confirmation_card_body, _form_text
from chat.cards.registry import register_card
from chat.events import Attachment, CardClickedEvent
from chat.formatting import _markdown_to_chat
from chat.stores import _session_store
from chat_client import download_attachment, post_card_to_space, post_message_to_space
from config import settings
from mcp_client import call_action_tool
from sessions import STATE_ACTIVE_WORKFLOW_ID, _session_id_for
from workflows import Workflow
from workflows.common.state_keys import (
    RFI_ANSWERS,
    RFI_ASSEMBLY_STATUS,
    RFI_COMPLETED_MARKER,
    RFI_FILE_ID,
    RFI_FILE_NAME,
    RFI_FILLED_LINK,
    RFI_GAP_CARD_MSG,
    RFI_GAP_STATE,
    RFI_GUIDANCE,
    RFI_GUIDANCE_CARD_MSG,
    RFI_GUIDANCE_STATE,
    RFI_QUESTIONS,
)

log = logging.getLogger(__name__)

_RFI_GUIDANCE_FUNCTION: Final = "rfi_guidance_submit"
_RFI_GAP_FUNCTION: Final = "rfi_gap_submit"

_XLSX_MIME: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOCX_MIME: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Downstream state cleared whenever the scope form is submitted, so the
# re-run regenerates from the patched inputs instead of reusing stale values.
_RFI_KEYS_TO_RESET_ON_GUIDANCE: Final = [
    RFI_ANSWERS, RFI_GAP_STATE, RFI_FILLED_LINK,
]


def _is_rfi_attachment(att: Attachment) -> bool:
    name = att.content_name.lower()
    ctype = att.content_type or ""
    return (
        name.endswith(".xlsx") or name.endswith(".docx")
        or "spreadsheet" in ctype or "wordprocessing" in ctype
    )


def _guess_mime(name: str) -> str:
    return _DOCX_MIME if name.lower().endswith(".docx") else _XLSX_MIME


async def prepare_rfi_file(
    *, attachments: tuple[Attachment, ...], session: Any
) -> str | None:
    """Download the attached RFI, store it on the Shared Drive, seed state.

    Returns ``None`` on success or a user-facing error string to post.
    """
    if not attachments:
        return (
            "Please attach the RFI as an .xlsx or .docx file and run `/rfi` again."
        )
    rfi_atts = [a for a in attachments if _is_rfi_attachment(a)]
    att = rfi_atts[0] if rfi_atts else attachments[0]
    if not att.resource_name:
        return (
            "I can only read files attached directly to the message. Download the "
            "RFI to your device and attach it to `/rfi` rather than linking it."
        )

    data = await download_attachment(att.resource_name)
    if not data:
        return "I couldn't download the attached file. Please try attaching it again."

    content_b64 = base64.b64encode(data).decode("ascii")
    mime = att.content_type or _guess_mime(att.content_name)
    try:
        result = await call_action_tool("upload_binary_file", {
            "name": att.content_name or "rfi-upload.xlsx",
            "mime_type": mime,
            "content_b64": content_b64,
        })
        parsed = json.loads(result)
    except Exception:
        log.exception("rfi.prepare: upload_binary_file failed")
        return "I couldn't store the RFI file for processing. Please try again."

    if parsed.get("error") or not parsed.get("file_id"):
        return f"I couldn't store the RFI file: {parsed.get('error', 'unknown error')}"

    await _append_state(session, {
        RFI_FILE_ID: parsed["file_id"],
        RFI_FILE_NAME: parsed.get("name") or att.content_name,
    })
    return None


def _build_rfi_guidance_card(
    n_questions: int, file_name: str, invoker_email: str
) -> dict:
    """Form 1 — collect scope guidance to steer the research."""
    fields = [
        ("facilities", "Target facilities / sites", "e.g. Guadalajara, Penang"),
        ("segment", "Business segment", "e.g. Medical, Defense & Aerospace, Cloud"),
        ("customer", "Prospective customer", "Customer or company name"),
        ("industry", "Customer industry", "e.g. Medical devices, Automotive"),
    ]
    widgets: list[dict] = [
        {"textInput": {"name": name, "label": label, "hintText": hint, "type": "SINGLE_LINE"}}
        for name, label, hint in fields
    ]
    widgets.append({"buttonList": {"buttons": [{
        "text": "Start research",
        "onClick": {"action": {
            "function": _RFI_GUIDANCE_FUNCTION,
            "parameters": [{"key": "invoker_email", "value": invoker_email}],
        }},
    }]}})
    return {"cardsV2": [{
        "cardId": "rfi_guidance",
        "card": {
            "header": {
                "title": "Guide the RFI research",
                "subtitle": f"{n_questions} question(s) from {file_name}",
            },
            "sections": [{"widgets": widgets}],
        },
    }]}


def _build_rfi_gap_card(
    gap_items: list[tuple[str, str]], invoker_email: str
) -> dict:
    """Form 2 — one input per question research couldn't answer confidently."""
    sections: list[dict] = []
    for qid, text in gap_items[:20]:
        sections.append({
            "header": qid,
            "widgets": [
                {"textParagraph": {"text": text}},
                {"textInput": {"name": f"ans::{qid}", "label": "Your answer", "type": "MULTIPLE_LINE"}},
            ],
        })
    sections.append({"widgets": [{"buttonList": {"buttons": [{
        "text": "Submit answers",
        "onClick": {"action": {
            "function": _RFI_GAP_FUNCTION,
            "parameters": [{"key": "invoker_email", "value": invoker_email}],
        }},
    }]}}]})
    n = len(gap_items)
    return {"cardsV2": [{
        "cardId": "rfi_gap",
        "card": {
            "header": {
                "title": f"{n} question{'s' if n != 1 else ''} need your input",
                "subtitle": "Fill in what the research couldn't answer",
            },
            "sections": sections,
        },
    }]}


def _rfi_questions(state: dict) -> list[dict]:
    """Unwrap RFI_QUESTIONS (an ``RFIQuestionSet`` dict) into a list of dicts."""
    raw = state.get(RFI_QUESTIONS) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = raw.get("questions", [])
    return raw if isinstance(raw, list) else []


def _rfi_gap_items(state: dict) -> list[tuple[str, str]]:
    """Return ``(question_id, question_text)`` for every needs_human answer."""
    text_by_id = {
        q.get("id"): q.get("text", "")
        for q in _rfi_questions(state)
    }
    answers_raw = state.get(RFI_ANSWERS) or {}
    if isinstance(answers_raw, str):
        try:
            answers_raw = json.loads(answers_raw)
        except json.JSONDecodeError:
            answers_raw = {}
    answers = answers_raw.get("answers", []) if isinstance(answers_raw, dict) else answers_raw
    items: list[tuple[str, str]] = []
    for a in answers or []:
        if isinstance(a, dict) and a.get("needs_human"):
            qid = a.get("question_id", "")
            items.append((qid, text_by_id.get(qid, qid)))
    return items


async def post_rfi_result(
    *,
    space_resource: str,
    thread_name: str,
    user_email: str,
    session_id: str,
    reply: str,
) -> None:
    """Post the next RFI form (scope / gap) or the final reply, by state."""
    cfg = settings()
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=user_email, session_id=session_id,
    )
    state = session.state if session else {}

    if state.get(RFI_GUIDANCE_STATE) == "PENDING":
        card = _build_rfi_guidance_card(
            len(_rfi_questions(state)),
            state.get(RFI_FILE_NAME) or "your RFI",
            user_email,
        )
        msg = await post_card_to_space(space_resource, card, thread_name=thread_name)
        if msg and session:
            await _append_state(session, {RFI_GUIDANCE_CARD_MSG: msg})
        return

    if state.get(RFI_GAP_STATE) == "PENDING":
        gaps = _rfi_gap_items(state)
        card = _build_rfi_gap_card(gaps, user_email)
        msg = await post_card_to_space(space_resource, card, thread_name=thread_name)
        if msg and session:
            await _append_state(session, {RFI_GAP_CARD_MSG: msg})
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to complete the RFI workflow."
    await post_message_to_space(space_resource, text, thread_name=thread_name)


@register_card(_RFI_GUIDANCE_FUNCTION)
async def _handle_rfi_guidance_submit(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Apply Form 1 scope guidance and re-run the RFI pipeline."""
    if not evt.invoker_email or not evt.thread_name:
        return {}
    cfg = settings()
    session_id = _session_id_for(evt.thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=evt.invoker_email, session_id=session_id,
    )
    if session is None:
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "This form has expired. Run `/rfi` again to start fresh."
        )}
    if session.state.get(RFI_GUIDANCE_STATE) != "PENDING":
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "Already processed."
        )}

    guidance = {
        "facilities": _form_text(evt.form_inputs, "facilities"),
        "segment": _form_text(evt.form_inputs, "segment"),
        "customer": _form_text(evt.form_inputs, "customer"),
        "industry": _form_text(evt.form_inputs, "industry"),
    }
    state_patch: dict[str, Any] = {RFI_GUIDANCE: guidance, RFI_GUIDANCE_STATE: "RESOLVED"}
    for key in _RFI_KEYS_TO_RESET_ON_GUIDANCE:
        state_patch[key] = None
    await _append_state(session, state_patch)

    workflow = runner._workflow_for(session.state.get(STATE_ACTIVE_WORKFLOW_ID))
    background_tasks.add_task(
        _resume_rfi_after_card,
        workflow=workflow,
        invoker_email=evt.invoker_email,
        space_resource=evt.space_resource,
        thread_name=evt.thread_name,
        session_id=session_id,
    )
    return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
        "Got it — researching answers now. I'll follow up here."
    )}


@register_card(_RFI_GAP_FUNCTION)
async def _handle_rfi_gap_submit(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Merge Form 2 human answers and re-run the RFI pipeline to assemble."""
    if not evt.invoker_email or not evt.thread_name:
        return {}
    cfg = settings()
    session_id = _session_id_for(evt.thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=evt.invoker_email, session_id=session_id,
    )
    if session is None:
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "This form has expired. Run `/rfi` again to start fresh."
        )}
    if session.state.get(RFI_GAP_STATE) != "PENDING":
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "Already processed."
        )}

    # Parse ans::<qid> inputs into {qid: text}.
    submitted: dict[str, str] = {}
    for key, inp in evt.form_inputs.items():
        if "::" not in key or not isinstance(inp, dict):
            continue
        _field, qid = key.split("::", 1)
        vals = (inp.get("stringInputs") or {}).get("value") or []
        if vals and vals[0].strip():
            submitted[qid] = vals[0].strip()

    answers_raw = session.state.get(RFI_ANSWERS) or {}
    if isinstance(answers_raw, str):
        try:
            answers_raw = json.loads(answers_raw)
        except json.JSONDecodeError:
            answers_raw = {}
    answers = answers_raw.get("answers", []) if isinstance(answers_raw, dict) else (answers_raw or [])
    for a in answers:
        if isinstance(a, dict) and a.get("question_id") in submitted:
            a["answer"] = submitted[a["question_id"]]
            a["needs_human"] = False

    state_patch: dict[str, Any] = {
        RFI_ANSWERS: {"answers": answers},
        RFI_GAP_STATE: "RESOLVED",
    }
    await _append_state(session, state_patch)

    workflow = runner._workflow_for(session.state.get(STATE_ACTIVE_WORKFLOW_ID))
    background_tasks.add_task(
        _resume_rfi_after_card,
        workflow=workflow,
        invoker_email=evt.invoker_email,
        space_resource=evt.space_resource,
        thread_name=evt.thread_name,
        session_id=session_id,
    )
    return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
        "Thanks — filling in your answers and finishing the response file."
    )}


# Bounded auto-resume when the final fill fails transiently. The assembler and
# fill_rfi_answers are both idempotent, so re-running the (now research-free)
# pipeline only completes the write — it can't duplicate work or files.
_RFI_RESUME_ATTEMPTS: Final = 3
_RFI_RESUME_BACKOFF_S: Final = 3.0
_RFI_RESUME_EXHAUSTED_HINT: Final = (
    "⚠️ I couldn't finish writing the response file just now. Your answers are "
    "saved — reply anything in this thread and I'll pick up where it left off."
)


def _rfi_assembly_failed(state: dict) -> bool:
    """True if the run should have produced the filled file but didn't.

    Distinguishes a genuine assembler failure (worth retrying) from a legitimate
    suspension: returns False while either form is still pending (the run is
    meant to stop there) and False once the response file is written.
    """
    if not _rfi_questions(state):
        return False  # nothing to assemble — not a retryable state
    if state.get(RFI_GUIDANCE_STATE) == "PENDING":
        return False
    if state.get(RFI_GAP_STATE) == "PENDING":
        return False
    return RFI_COMPLETED_MARKER not in (state.get(RFI_ASSEMBLY_STATUS) or "")


async def _resume_rfi_after_card(
    *,
    workflow: Workflow,
    invoker_email: str,
    space_resource: str,
    thread_name: str,
    session_id: str,
) -> None:
    """Background task: re-run the RFI pipeline after a form submission.

    The submitted answers are already persisted, and both the assembler and
    ``fill_rfi_answers`` are idempotent, so a transient failure on the final
    write is retried by re-running the (research-free) pipeline a few times. On
    persistent failure the user is told that any reply in this thread retries —
    that path re-runs the same idempotent pipeline.
    """
    cfg = settings()
    reply = ""
    for attempt in range(_RFI_RESUME_ATTEMPTS):
        try:
            reply = await runner._run_agent(
                workflow=workflow,
                user_email=invoker_email,
                session_id=session_id,
                prompt="(RFI form submitted via card — please continue the workflow)",
            )
        except Exception:
            log.exception(
                "RFI card resume failed (attempt %d/%d): workflow=%s user=%s",
                attempt + 1, _RFI_RESUME_ATTEMPTS, workflow.command_name, invoker_email,
            )
            reply = ""

        session = await _session_store.service.get_session(
            app_name=cfg.app_name, user_id=invoker_email, session_id=session_id,
        )
        if not _rfi_assembly_failed(session.state if session else {}):
            break  # completed, or legitimately suspended at the next form
        if attempt < _RFI_RESUME_ATTEMPTS - 1:
            await asyncio.sleep(_RFI_RESUME_BACKOFF_S * (attempt + 1))
    else:
        # Exhausted every attempt with the write still pending.
        reply = (f"{reply}\n\n" if reply else "") + _RFI_RESUME_EXHAUSTED_HINT

    await post_rfi_result(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=invoker_email,
        session_id=session_id,
        reply=reply,
    )
