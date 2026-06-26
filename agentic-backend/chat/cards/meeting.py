"""Meeting workflow card UI: owner-assignment form and calendar-invite dialog.

The pipeline suspends when action items are missing owners/due dates; the
owner-assignment form collects them and resumes the run. Separately, an
optional invite dialog lets the user add attendees to the personal
calendar reminders the pipeline created.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Final

from fastapi import BackgroundTasks
from google.adk.events import Event, EventActions

from chat import runner
from chat.cards.common import _confirmation_card_body
from chat.cards.registry import register_card, register_default_post
from chat.events import CardClickedEvent
from chat.formatting import _markdown_to_chat
from chat.stores import _session_store
from chat.client import (
    post_card_to_space,
    post_message_to_space,
    update_card_in_space,
)
from config import settings
from clients.mcp_client import call_context_tool
from sessions import STATE_ACTIVE_WORKFLOW_ID, _session_id_for
from workflows import Workflow
from workflows.common.state_keys import (
    MTG_ASSEMBLY_STATUS,
    MTG_CALENDAR_EVENT_IDS,
    MTG_CALENDAR_HOLDS,
    MTG_EMAIL_DRAFTS,
    MTG_GATE_FAILED,
    MTG_GATE_VERDICT,
    MTG_NOTES_DOC,
    MTG_OWNER_CARD_MSG,
    MTG_OWNER_GATE_STATE,
    MTG_PARSED,
    MTG_TRACKER_ROWS,
)
from workflows.meeting_engine.schemas import ActionItem, ParsedMeeting

log = logging.getLogger(__name__)

_OWNER_ASSIGNMENT_FUNCTION: Final = "owner_assignment"
_OPEN_INVITE_DIALOG_FUNCTION: Final = "open_invite_dialog"
_SUBMIT_INVITES_FUNCTION: Final = "submit_invites"

# Fan-out outputs cleared on resume so the LLM agents regenerate from the
# patched MTG_PARSED rather than reusing stale values from history.
_STATE_KEYS_TO_RESET_ON_CARD: Final = [
    MTG_EMAIL_DRAFTS,
    MTG_CALENDAR_HOLDS,
    MTG_TRACKER_ROWS,
    MTG_NOTES_DOC,
    MTG_GATE_VERDICT,
    MTG_GATE_FAILED,
    MTG_ASSEMBLY_STATUS,
]


# ---------------------------------------------------------------------------
# Owner-assignment form
# ---------------------------------------------------------------------------


def _attendee_display_name(attendee: str) -> str:
    """Return the human-readable name from an attendee string.

    Handles three formats produced by the parser:
    - ``"Full Name (email@domain.com)"`` → ``"Full Name"``
    - ``"email@domain.com"``             → ``"email@domain.com"`` (unchanged)
    - ``"Full Name"``                    → ``"Full Name"`` (unchanged)
    """
    if "(" in attendee and attendee.endswith(")"):
        return attendee[: attendee.rfind("(")].strip()
    return attendee


def _date_to_epoch_ms(date_str: str) -> str | None:
    """Convert an ISO 8601 date string (YYYY-MM-DD) to epoch milliseconds.

    Returns a string suitable for ``dateTimePicker.valueMsEpoch``, or
    ``None`` if the input is absent or unparseable.
    """
    try:
        from datetime import timezone as _tz
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=_tz.utc)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return None


def _build_owner_assignment_card(
    incomplete_items: list[ActionItem],
    attendees: list[str],
    meeting_title: str,
    invoker_email: str,
) -> dict:
    """Build a Cards v2 message body for the owner/due-date assignment form.

    One section per incomplete action item (missing owner and/or due date).
    Each section shows an attendee dropdown and a date picker. Known values
    are pre-populated so the user only needs to fill in what's missing.
    Two submit buttons differentiated by a ``decision`` action parameter.
    """
    sections = []
    for item in incomplete_items:
        # Build dropdown options; pre-select the existing owner if set.
        # If the owner is set but not in the attendees list, prepend it.
        options: list[dict] = []
        owner_matched = False
        for a in attendees:
            selected = bool(item.owner and a == item.owner)
            options.append({"text": _attendee_display_name(a), "value": a, "selected": selected})
            if selected:
                owner_matched = True
        if item.owner and not owner_matched:
            options.insert(0, {"text": _attendee_display_name(item.owner), "value": item.owner, "selected": True})

        date_widget: dict = {
            "dateTimePicker": {
                "name": f"due::{item.id}",
                "label": "Due date",
                "type": "DATE_ONLY",
            }
        }
        if item.due_date:
            epoch_ms = _date_to_epoch_ms(item.due_date)
            if epoch_ms:
                date_widget["dateTimePicker"]["valueMsEpoch"] = epoch_ms

        missing = []
        if not item.owner:
            missing.append("owner")
        if not item.due_date:
            missing.append("due date")
        missing_label = f"  —  missing: {', '.join(missing)}" if missing else ""

        sections.append({
            "header": f"{item.id}: {item.description}{missing_label}",
            "widgets": [
                {
                    "selectionInput": {
                        "name": f"assignee::{item.id}",
                        "label": "Assign to",
                        "type": "DROPDOWN",
                        "items": options,
                    }
                },
                date_widget,
            ],
        })

    n = len(incomplete_items)
    button_params = [{"key": "invoker_email", "value": invoker_email}]
    sections.append({
        "widgets": [{
            "buttonList": {
                "buttons": [
                    {
                        "text": "Confirm & continue",
                        "onClick": {"action": {
                            "function": _OWNER_ASSIGNMENT_FUNCTION,
                            "parameters": button_params + [{"key": "decision", "value": "assign"}],
                        }},
                    },
                    {
                        "text": "Skip for now",
                        "onClick": {"action": {
                            "function": _OWNER_ASSIGNMENT_FUNCTION,
                            "parameters": button_params + [{"key": "decision", "value": "skip"}],
                        }},
                    },
                ]
            }
        }]
    })

    return {
        "cardsV2": [{
            "cardId": "owner_assignment",
            "card": {
                "header": {
                    "title": f"{n} action item{'s' if n != 1 else ''} need details",
                    "subtitle": f"Meeting: {meeting_title}  •  Optional",
                },
                "sections": sections,
            },
        }]
    }


def _parse_form_inputs(form_inputs: dict) -> dict[str, dict[str, str]]:
    """Parse ``common.formInputs`` into ``{item_id: {assignee?, due?}}``.

    Widget names follow ``<field>::<item_id>`` encoding. ``selectionInput``
    values arrive under ``stringInputs.value``; ``dateTimePicker`` with
    ``DATE_ONLY`` arrives under ``dateInput.msSinceEpoch`` and with
    ``DATE_AND_TIME`` under ``dateTimeInput.msSinceEpoch``. Both are handled.
    Values that are absent or empty are omitted from the inner dict.
    """
    assignments: dict[str, dict[str, str]] = {}
    for key, inp in form_inputs.items():
        if "::" not in key:
            continue
        field, item_id = key.split("::", 1)
        val = None
        if inp.get("stringInputs"):
            values = inp["stringInputs"].get("value") or []
            val = values[0] if values else None
        else:
            # DATE_ONLY dateTimePicker widgets send "dateInput" in the
            # response (not "dateTimeInput" — that key is only used for
            # DATE_AND_TIME pickers). Accept both to be safe.
            date_inp = inp.get("dateInput") or inp.get("dateTimeInput")
            if date_inp:
                val = date_inp.get("msSinceEpoch") or None
        if val:
            assignments.setdefault(item_id, {})[field] = val
    return assignments


def _epoch_ms_to_date(ms_str: str) -> str | None:
    """Convert a dateTimePicker epoch-millisecond string to an ISO 8601 date.

    Returns ``None`` if the string is absent or unparseable.
    """
    try:
        from datetime import timezone as _tz
        ts = int(ms_str) / 1000
        return datetime.fromtimestamp(ts, tz=_tz.utc).date().isoformat()
    except Exception:
        log.warning("card.epoch_ms_to_date failed to parse ms_str=%r", ms_str, exc_info=True)
        return None


async def handle_owner_assignment(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Apply the owner/due-date form and resume the meeting pipeline.

    This is the default CARD_CLICKED handler — the owner-assignment form
    carries no distinct action function, so any unrecognised function
    routes here.
    """
    if not evt.invoker_email or not evt.thread_name:
        log.warning("CARD_CLICKED missing invoker_email or thread_name; ignoring")
        return {}

    cfg = settings()
    session_id = _session_id_for(evt.thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name,
        user_id=evt.invoker_email,
        session_id=session_id,
    )

    # Idempotency: already processed or session expired
    if session is None:
        log.info(
            "CARD_CLICKED: session not found for invoker=%s thread=%s",
            evt.invoker_email,
            evt.thread_name,
        )
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "This form has expired. Run `/meeting` again to start fresh."
        )}

    if session.state.get(MTG_OWNER_GATE_STATE) != "PENDING":
        log.info(
            "CARD_CLICKED: gate state is %r (not PENDING), ignoring duplicate",
            session.state.get(MTG_OWNER_GATE_STATE),
        )
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "Already processed."
        )}

    assignments = _parse_form_inputs(evt.form_inputs)

    parsed_data = session.state.get(MTG_PARSED) or {}
    try:
        parsed = ParsedMeeting.model_validate(parsed_data)
    except Exception:
        log.exception("CARD_CLICKED: failed to parse MTG_PARSED for session %s", session_id)
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "An error occurred processing the form. Please run `/meeting` again."
        )}

    for item in parsed.action_items:
        if item.id in assignments:
            a = assignments[item.id]
            if a.get("assignee"):
                # Normalize to display name so the format matches parser
                # output ("Sarah Chen", not "Sarah Chen (email@...)").
                # This ensures the email_drafter groups all items for the
                # same person into one draft regardless of how the owner was
                # originally stored.
                item.owner = _attendee_display_name(a["assignee"])
            if a.get("due"):
                item.due_date = _epoch_ms_to_date(a["due"])

    # Build state patch: update MTG_PARSED, mark gate resolved, clear stale state.
    # Fan-out keys (email drafts, calendar holds, tracker rows) are cleared so
    # the fan-out LLM agents regenerate from the updated MTG_PARSED rather than
    # re-using stale values from conversation history.
    state_patch: dict[str, Any] = {
        MTG_PARSED: parsed.model_dump(),
        MTG_OWNER_GATE_STATE: "RESOLVED",
    }
    for key in _STATE_KEYS_TO_RESET_ON_CARD:
        state_patch[key] = None

    # Persist state patch via append_event before the background re-run
    patch_event = Event(
        author="card_handler",
        actions=EventActions(state_delta=state_patch),
    )
    try:
        await _session_store.service.append_event(session, patch_event)
    except Exception:
        log.exception("CARD_CLICKED: failed to persist state patch for session %s", session_id)
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "An error occurred saving your assignments. Please try again."
        )}

    workflow = runner._workflow_for(session.state.get(STATE_ACTIVE_WORKFLOW_ID))
    card_message_name = session.state.get(MTG_OWNER_CARD_MSG) or evt.message_name

    background_tasks.add_task(
        _resume_after_card,
        workflow=workflow,
        invoker_email=evt.invoker_email,
        space_resource=evt.space_resource,
        thread_name=evt.thread_name,
        card_message_name=card_message_name,
        session_id=session_id,
    )

    return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
        "Processing your assignments... I'll reply here when the artifacts are ready."
    )}


async def _resume_after_card(
    *,
    workflow: Workflow,
    invoker_email: str,
    space_resource: str,
    thread_name: str,
    card_message_name: str,
    session_id: str,
) -> None:
    """Background task: re-run the meeting pipeline after card submission.

    The state patch has already been applied by the sync handler.
    The pipeline re-runs with updated MTG_PARSED and MTG_OWNER_GATE_STATE
    == "RESOLVED"; all idempotency guards skip already-completed stages
    while cleared draft keys cause the fan-out agents to regenerate.
    """
    try:
        reply = await runner._run_agent(
            workflow=workflow,
            user_email=invoker_email,
            session_id=session_id,
            prompt="(owner assignments submitted via card form — please complete the workflow)",
        )
    except Exception:
        log.exception(
            "Card resume failed: workflow=%s user=%s",
            workflow.command_name,
            invoker_email,
        )
        await post_message_to_space(
            space_resource,
            runner._GENERIC_REPLY_FAILED,
            thread_name=thread_name,
        )
        if card_message_name:
            await update_card_in_space(
                card_message_name,
                _confirmation_card_body("An error occurred. Please run `/meeting` again."),
            )
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to complete the meeting workflow."
    await post_message_to_space(space_resource, text, thread_name=thread_name)
    if card_message_name:
        await update_card_in_space(
            card_message_name,
            _confirmation_card_body("Done — see thread for the full summary."),
        )
    await _post_invite_card_if_ready(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=invoker_email,
        session_id=session_id,
    )


# ---------------------------------------------------------------------------
# Calendar-invite dialog — prompt card, dialog builder, handlers, patch task
# ---------------------------------------------------------------------------
#
# Calendar reminders are created as personal (attendee-less) events by the
# pipeline. This optional flow lets the user invite people *afterward*: a small
# prompt card opens a modal dialog with, per dated action item, a checkbox of
# the meeting attendees, an org-directory people search (USER data source),
# and a free-text field for arbitrary external emails. On submit we resolve the
# org selections to addresses (People API) and patch the chosen events.


def _attendee_email(attendee: str) -> str | None:
    """Return the bare email from a "Name (email)" / "email" attendee, else None."""
    a = attendee.strip()
    if a.endswith(")") and "(" in a:
        inner = a[a.rfind("(") + 1 : -1].strip()
        return inner if "@" in inner else None
    return a if "@" in a and " " not in a else None


def _build_invite_prompt_card(invoker_email: str, n_events: int) -> dict:
    """Small card with a button that opens the invite dialog."""
    plural = "s" if n_events != 1 else ""
    return {
        "cardsV2": [{
            "cardId": "calendar_invites_prompt",
            "card": {
                "header": {
                    "title": "Invite people to your reminders?",
                    "subtitle": "Optional — reminders are personal by default",
                },
                "sections": [{
                    "widgets": [
                        {"textParagraph": {"text": (
                            f"I created {n_events} personal calendar reminder{plural} "
                            "on your calendar. Want to invite attendees to any of them?"
                        )}},
                        {"buttonList": {"buttons": [{
                            "text": "Configure invites",
                            "onClick": {"action": {
                                "function": _OPEN_INVITE_DIALOG_FUNCTION,
                                "interaction": "OPEN_DIALOG",
                                "parameters": [
                                    {"key": "invoker_email", "value": invoker_email},
                                ],
                            }},
                        }]}},
                    ],
                }],
            },
        }]
    }


async def _post_invite_card_if_ready(
    *,
    space_resource: str,
    thread_name: str,
    user_email: str,
    session_id: str,
) -> None:
    """Post the invite prompt card if calendar events were created this run."""
    cfg = settings()
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=user_email, session_id=session_id,
    )
    if session is None:
        return
    event_ids = session.state.get(MTG_CALENDAR_EVENT_IDS) or {}
    if not event_ids:
        return
    card = _build_invite_prompt_card(user_email, len(event_ids))
    await post_card_to_space(space_resource, card, thread_name=thread_name)


def _invite_dialog_response(sections: list[dict]) -> dict[str, Any]:
    """Wrap *sections* in a Chat DIALOG actionResponse."""
    return {"actionResponse": {"type": "DIALOG", "dialogAction": {"dialog": {
        "body": {"sections": sections},
    }}}}


def _invite_action_status(status_code: str, message: str) -> dict[str, Any]:
    """A terminal dialog response that shows a toast and closes (no body)."""
    return {"actionResponse": {"type": "DIALOG", "dialogAction": {"actionStatus": {
        "statusCode": status_code,
        "userFacingMessage": message,
    }}}}


def _build_invite_dialog(
    event_ids: dict[str, str],
    parsed: ParsedMeeting,
    *,
    invoker_email: str,
    space_resource: str,
    thread_name: str,
) -> dict[str, Any]:
    """Build the per-item invite dialog from the created events + parsed meeting."""
    items_by_id = {i.id: i for i in parsed.action_items}
    attendee_options = [
        {"text": _attendee_display_name(a), "value": email, "selected": False}
        for a in parsed.attendees
        if (email := _attendee_email(a))
    ]

    sections: list[dict] = []
    for item_id in event_ids:
        item = items_by_id.get(item_id)
        desc = item.description if item else item_id
        due = item.due_date if (item and item.due_date) else "no date"
        widgets: list[dict] = []
        if attendee_options:
            widgets.append({"selectionInput": {
                "name": f"attendees::{item_id}",
                "label": "Meeting attendees",
                "type": "CHECK_BOX",
                "items": [dict(o) for o in attendee_options],
            }})
        widgets.append({"selectionInput": {
            "name": f"org::{item_id}",
            "label": "Search people in your organisation",
            "type": "MULTI_SELECT",
            "multiSelectMaxSelectedItems": 20,
            "multiSelectMinQueryLength": 1,
            # CommonDataSource enum value is USER searches the
            # caller's Google Workspace directory.
            "platformDataSource": {"commonDataSource": "USER"},
        }})
        widgets.append({"textInput": {
            "name": f"ext::{item_id}",
            "label": "Other emails (comma-separated)",
            "type": "SINGLE_LINE",
        }})
        sections.append({"header": f"{item_id}: {desc}  •  due {due}", "widgets": widgets})

    sections.append({"widgets": [{"buttonList": {"buttons": [{
        "text": "Send invites",
        "onClick": {"action": {
            "function": _SUBMIT_INVITES_FUNCTION,
            "parameters": [
                {"key": "invoker_email", "value": invoker_email},
                {"key": "space_resource", "value": space_resource},
                {"key": "thread_name", "value": thread_name},
            ],
        }},
    }]}}]})

    return _invite_dialog_response(sections)


def _parse_invite_form_inputs(form_inputs: dict) -> dict[str, dict[str, list[str]]]:
    """Parse dialog formInputs into ``{item_id: {field: [values]}}``.

    Widget names are ``<field>::<item_id>`` where field is ``attendees`` (emails),
    ``org`` (``users/<id>`` resource names), or ``ext`` (one comma-separated text).
    """
    out: dict[str, dict[str, list[str]]] = {}
    for key, inp in form_inputs.items():
        if "::" not in key or not isinstance(inp, dict):
            continue
        field, item_id = key.split("::", 1)
        values = (inp.get("stringInputs") or {}).get("value") or []
        values = [v for v in values if v]
        if values:
            out.setdefault(item_id, {}).setdefault(field, []).extend(values)
    return out


@register_card(_OPEN_INVITE_DIALOG_FUNCTION)
async def _handle_open_invite_dialog(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Build and return the calendar-invite dialog from session state."""
    if not evt.invoker_email or not evt.thread_name:
        return _invite_action_status("INVALID_ARGUMENT", "Couldn't identify the conversation.")
    cfg = settings()
    session_id = _session_id_for(evt.thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=evt.invoker_email, session_id=session_id,
    )
    if session is None:
        return _invite_action_status("NOT_FOUND", "This form has expired. Run `/meeting` again.")
    event_ids = session.state.get(MTG_CALENDAR_EVENT_IDS) or {}
    parsed_data = session.state.get(MTG_PARSED) or {}
    if not event_ids or not parsed_data:
        return _invite_action_status("NOT_FOUND", "No calendar reminders to configure.")
    try:
        parsed = ParsedMeeting.model_validate(parsed_data)
    except Exception:
        log.exception("invite.open: failed to parse MTG_PARSED")
        return _invite_action_status("INTERNAL", "Something went wrong opening the form.")
    return _build_invite_dialog(
        event_ids, parsed,
        invoker_email=evt.invoker_email,
        space_resource=evt.space_resource,
        thread_name=evt.thread_name,
    )


@register_card(_SUBMIT_INVITES_FUNCTION)
async def _handle_submit_invites(
    evt: CardClickedEvent, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Validate the invite selections and enqueue the patch task."""
    thread_name = evt.params.get("thread_name") or evt.thread_name
    space_resource = evt.params.get("space_resource") or evt.space_resource
    invoker_email = evt.invoker_email
    if not invoker_email or not thread_name:
        return _invite_action_status("INVALID_ARGUMENT", "Couldn't identify the conversation.")

    selections = _parse_invite_form_inputs(evt.form_inputs)
    chosen = {iid: sel for iid, sel in selections.items() if any(sel.values())}
    if not chosen:
        return _invite_action_status("OK", "No people selected — nothing to invite.")

    background_tasks.add_task(
        _apply_invites,
        invoker_email=invoker_email,
        thread_name=thread_name,
        space_resource=space_resource,
        selections=chosen,
    )
    n = len(chosen)
    return _invite_action_status(
        "OK", f"Sending invites for {n} reminder{'s' if n != 1 else ''} — I'll confirm in the thread."
    )


async def _apply_invites(
    *,
    invoker_email: str,
    thread_name: str,
    space_resource: str,
    selections: dict[str, dict[str, list[str]]],
) -> None:
    """Background task: resolve org selections to emails and patch the events."""
    cfg = settings()
    session_id = _session_id_for(thread_name)
    session = await _session_store.service.get_session(
        app_name=cfg.app_name, user_id=invoker_email, session_id=session_id,
    )
    if session is None:
        await post_message_to_space(
            space_resource, "Couldn't load the meeting session to send invites.",
            thread_name=thread_name,
        )
        return

    event_ids = session.state.get(MTG_CALENDAR_EVENT_IDS) or {}
    results: list[str] = []
    for item_id, sel in selections.items():
        event_id = event_ids.get(item_id)
        if not event_id:
            continue
        emails: set[str] = set(sel.get("attendees", []))
        for raw in sel.get("ext", []):
            for piece in re.split(r"[,;\s]+", raw):
                piece = piece.strip()
                if "@" in piece:
                    emails.add(piece)
        org_ids = sel.get("org", [])
        if org_ids:
            try:
                resolved_json = await call_context_tool(
                    invoker_email, "resolve_people_emails", {"resource_names": org_ids},
                )
                for entry in json.loads(resolved_json):
                    if entry.get("email"):
                        emails.add(entry["email"])
            except Exception:
                log.exception("invite.apply: failed to resolve org people")
                results.append(f"• {item_id}: could not resolve org people")
        if not emails:
            continue
        try:
            await call_context_tool(
                invoker_email, "update_calendar_event_attendees",
                {"event_id": event_id, "attendee_emails": sorted(emails)},
            )
            results.append(f"• {item_id}: invited {len(emails)} ({', '.join(sorted(emails))})")
        except Exception as exc:
            log.exception("invite.apply: failed to patch event %s", event_id)
            results.append(f"• {item_id}: failed ({exc})")

    summary = (
        "Calendar invites updated:\n" + "\n".join(results)
        if results else "No invites were applied."
    )
    await post_message_to_space(space_resource, summary, thread_name=thread_name)


# ---------------------------------------------------------------------------
# Post-run result for meeting and the generic (non-RFI) workflows
# ---------------------------------------------------------------------------


@register_default_post
async def post_default_result(
    *,
    space_resource: str,
    thread_name: str,
    user_email: str,
    session_id: str,
    reply: str,
) -> None:
    """Post the owner-assignment card if the gate suspended, else the reply.

    Used for ``/meeting`` and every other workflow without its own
    suspend/resume form. The owner-gate check and invite-card offer are
    no-ops for non-meeting workflows (their state keys are absent).
    """
    cfg = settings()
    post_run_session = await _session_store.service.get_session(
        app_name=cfg.app_name,
        user_id=user_email,
        session_id=session_id,
    )
    gate_state = (
        post_run_session.state.get(MTG_OWNER_GATE_STATE)
        if post_run_session
        else None
    )

    if gate_state == "PENDING":
        parsed_data = (post_run_session.state or {}).get(MTG_PARSED) or {}
        try:
            parsed = ParsedMeeting.model_validate(parsed_data)
        except Exception:
            log.exception("Failed to build owner card for session %s", session_id)
            await post_message_to_space(space_resource, runner._GENERIC_REPLY_FAILED, thread_name=thread_name)
            return
        incomplete = [i for i in parsed.action_items if not i.owner or not i.due_date]
        card = _build_owner_assignment_card(incomplete, parsed.attendees, parsed.title, user_email)
        card_msg_name = await post_card_to_space(space_resource, card, thread_name=thread_name)
        # Persist the card message name so the CARD_CLICKED handler can update it
        if card_msg_name and post_run_session:
            msg_name_event = Event(
                author="slash_workflow",
                actions=EventActions(state_delta={MTG_OWNER_CARD_MSG: card_msg_name}),
            )
            try:
                await _session_store.service.append_event(post_run_session, msg_name_event)
            except Exception:
                log.warning("Failed to persist card message name; card update on submit will be skipped")
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to generate a response."
    await post_message_to_space(
        space_resource,
        text,
        thread_name=thread_name,
    )
    # If the meeting pipeline created calendar reminders (no owner card was
    # needed), offer the optional invite dialog.
    await _post_invite_card_if_ready(
        space_resource=space_resource,
        thread_name=thread_name,
        user_email=user_email,
        session_id=session_id,
    )
