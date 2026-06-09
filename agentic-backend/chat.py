"""Google Chat webhook event handling.

Translates an inbound Chat payload into a workflow-scoped agent run and
renders the agent's response into the JSON envelope Chat expects back.

A single Chat app exposes multiple slash commands; routing happens here.
For each ``MESSAGE`` event we:

1. Parse out the slash command (if any), Chat thread, and prompt text.
2. Handle reserved commands (``/exit``, ``/help``, ``/grant``,
   ``/revoke``, ``/list-access``) directly without an LLM call.
3. Authorize the user against the workflow's table-backed
   :class:`AccessPolicy` (combined with its code-side
   :class:`AccessMode`). The Chat UI cannot hide commands per-user, so
   denials must happen here.
4. Resolve a persistent ADK session keyed by ``(user_email, thread)``
   so multi-turn continuations stay inside the active workflow.
5. Run the workflow-scoped agent and return its reply.

Admin slash commands (``/grant`` / ``/revoke`` / ``/list-access``)
modify the ACL table but are themselves governed by env-driven
bootstrap admins, not by the table they manage — see
:func:`access.authorize_bootstrap_admin` for the rationale.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from fastapi import BackgroundTasks
from google.adk.events import Event, EventActions
from google.adk.runners import Runner
from google.genai import types
from opentelemetry import trace as otel_trace

from access import authorize, authorize_bootstrap_admin
from access_store import AccessStore, VALID_RULE_TYPES
from agent import build_agent_for_workflow
from chat_client import post_card_to_space, post_message_to_space, update_card_in_space
from config import settings
from sessions import SessionStore, STATE_ACTIVE_WORKFLOW_ID, _session_id_for
from workflows import (
    ADMIN_COMMAND_IDS,
    DEFAULT_WORKFLOW,
    RESERVED_COMMAND_NAMES,
    RESERVED_EXIT_COMMAND_ID,
    RESERVED_GRANT_COMMAND_ID,
    RESERVED_HELP_COMMAND_ID,
    RESERVED_LIST_ACCESS_COMMAND_ID,
    RESERVED_REVOKE_COMMAND_ID,
    WORKFLOWS,
    Workflow,
    get_workflow,
    get_workflow_by_name,
)

from mcp_client import call_context_tool
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
_tracer = otel_trace.get_tracer(__name__)

# Subset of Google Chat event types the agent reacts to.
EVENT_ADDED_TO_SPACE: Final = "ADDED_TO_SPACE"
EVENT_MESSAGE: Final = "MESSAGE"
EVENT_CARD_CLICKED: Final = "CARD_CLICKED"

WELCOME_MESSAGE: Final = (
    "Hello! I am your Dual-MCP Workspace Assistant. "
    "Type `/help` to see available commands, or just ask me a question."
)

_GRANT_USAGE: Final = (
    "Usage: `/grant <command> <type>:<principal>`\n"
    "  type is one of `email`, `domain`.\n"
    "  examples: `/grant /audit email:alice@example.com`  "
    "`/grant /audit domain:example.com`"
)
_REVOKE_USAGE: Final = (
    "Usage: `/revoke <command> <type>:<principal>` "
    "(same syntax as `/grant`)."
)
_LIST_USAGE: Final = "Usage: `/list-access <command>`"

# Process-wide stores. The underlying ``DatabaseSessionService`` manages
# its own connection pool so sharing the engine across both stores is
# safe — see ``SessionStore.engine``.
_session_store = SessionStore.from_settings()
_access_store = AccessStore(_session_store.engine)


@dataclass(frozen=True, slots=True)
class ChatEvent:
    """A minimal, well-typed view of an inbound Google Chat payload."""

    event_type: str
    user_email: str | None
    # Chat user resource name (``users/USER_ID``) and display name.
    # ``user_resource`` is used to build a clickable ``<users/ID>``
    # mention in the public anchor message; ``user_display`` is a
    # human-readable fallback for logs and rendered text.
    user_resource: str
    user_display: str
    prompt: str
    slash_command_id: int | None
    thread_name: str
    # Raw Chat resource names needed to post async replies back into the
    # same conversation via the Chat REST API. ``conversation_key`` is
    # the session bucket (space-or-thread depending on space type);
    # ``space_resource`` / ``message_thread`` are the literal names
    # Chat expects in REST calls.
    space_resource: str
    message_thread: str

    @classmethod
    def from_payload(cls, body: dict) -> "ChatEvent":
        """Extract the fields this agent cares about from a Chat event body."""
        message = body.get("message") or {}
        slash_command = message.get("slashCommand") or {}
        raw_command_id = slash_command.get("commandId")
        try:
            command_id = int(raw_command_id) if raw_command_id is not None else None
        except (TypeError, ValueError):
            command_id = None

        space = body.get("space") or {}
        space_resource = space.get("name") or ""
        message_thread = (message.get("thread") or {}).get("name") or ""

        thread_name = _conversation_key(body, message)

        user = body.get("user") or {}

        return cls(
            event_type=body.get("type", ""),
            user_email=user.get("email"),
            user_resource=user.get("name") or "",
            user_display=user.get("displayName") or "",
            prompt=_clean_prompt(message),
            slash_command_id=command_id,
            thread_name=thread_name,
            space_resource=space_resource,
            message_thread=message_thread,
        )


@dataclass(frozen=True, slots=True)
class CardClickedEvent:
    """A minimal, well-typed view of a CARD_CLICKED payload."""

    user_email: str
    space_resource: str
    message_name: str    # card message to update in place
    thread_name: str     # session lookup key
    invoker_email: str   # stored in button actionParameters
    decision: str        # "assign" | "skip"
    invoked_function: str  # onClick action function — routes the handler
    params: dict         # all button actionParameters (key -> value)
    form_inputs: dict    # raw common.formInputs dict

    @classmethod
    def from_payload(cls, body: dict) -> "CardClickedEvent":
        user = body.get("user") or {}
        space = body.get("space") or {}
        message = body.get("message") or {}
        thread = message.get("thread") or {}
        action = body.get("action") or {}
        common = body.get("common") or {}
        params = {p["key"]: p["value"] for p in (action.get("parameters") or [])}
        form_inputs = common.get("formInputs") or {}
        return cls(
            user_email=user.get("email") or "",
            space_resource=space.get("name") or "",
            message_name=message.get("name") or "",
            thread_name=thread.get("name") or space.get("name") or "",
            invoker_email=params.get("invoker_email") or user.get("email") or "",
            decision=params.get("decision") or "",
            invoked_function=action.get("function") or common.get("invokedFunction") or "",
            params=params,
            form_inputs=form_inputs,
        )


def _conversation_key(body: dict, message: dict) -> str:
    """Return the stable key identifying the conversation a message belongs to.

    We always key by ``message.thread.name`` so each Chat thread gets
    its own isolated session and memory. In DMs and group chats Chat
    creates a fresh thread for every top-level message — that's exactly
    the boundary we want: each new top-level prompt starts a fresh
    conversation, while replies inside a thread continue it. We fall
    back to ``space.name`` only when thread.name is unexpectedly empty.
    """
    space = body.get("space") or {}
    thread_name = (message.get("thread") or {}).get("name") or ""
    space_name = space.get("name") or ""
    return thread_name or space_name


def _clean_prompt(message: dict) -> str:
    """Return ``message.text`` with any leading slash command stripped.

    Chat tags the slash command's character range via a ``SLASH_COMMAND``
    annotation; using ``length`` from that annotation is more robust
    than splitting on whitespace.
    """
    text = message.get("text") or ""
    for annotation in message.get("annotations") or []:
        if annotation.get("type") == "SLASH_COMMAND":
            length = annotation.get("length")
            if isinstance(length, int) and length > 0:
                return text[length:].lstrip()
    if text.startswith("/"):
        _, _, rest = text.partition(" ")
        return rest.lstrip()
    return text


def chat_text(text: str) -> dict[str, str]:
    """Wrap *text* in the JSON envelope Chat expects from a webhook response."""
    return {"text": text}


# Google Chat renders a small custom format in message text (``*bold*``,
# ``_italic_``, ``~strike~``, ``` `code` ```, ```` ```code``` ````,
# ``<url|text>``). Standard markdown (``**bold**``, ``# headers``,
# ``- bullets``, ``[text](url)``) is shown literally. LLM responses use
# standard markdown, so :func:`_markdown_to_chat` translates them before
# we send the envelope back. Internal messages built in this module
# (help, admin replies) are already authored in Chat format and bypass
# the converter.
_CODE_FENCE_RE: Final = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE: Final = re.compile(r"`[^`\n]+`")
_HEADER_RE: Final = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t#]*$", re.MULTILINE)
_BULLET_RE: Final = re.compile(r"^([ \t]*)[-*+][ \t]+", re.MULTILINE)
_BOLD_RE: Final = re.compile(r"\*\*([^*\n]+?)\*\*|__([^_\n]+?)__")
_LINK_RE: Final = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_PLACEHOLDER_RE: Final = re.compile(r"\x00CB(\d+)\x00")


def _markdown_to_chat(text: str) -> str:
    """Translate standard markdown to Google Chat's text formatting.

    Code spans and fenced blocks are stashed first so substitutions
    don't reach inside them — e.g. ``**`` inside a Python snippet must
    survive untouched.
    """
    stash: list[str] = []

    def _save(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\x00CB{len(stash) - 1}\x00"

    text = _CODE_FENCE_RE.sub(_save, text)
    text = _INLINE_CODE_RE.sub(_save, text)

    text = _HEADER_RE.sub(lambda m: f"*{m.group(1)}*", text)
    text = _BULLET_RE.sub(lambda m: f"{m.group(1)}• ", text)
    text = _BOLD_RE.sub(lambda m: f"*{m.group(1) or m.group(2)}*", text)
    text = _LINK_RE.sub(lambda m: f"<{m.group(2)}|{m.group(1)}>", text)

    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], text)


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
        return await _handle_card_clicked(body, background_tasks)

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


async def _handle_exit(event: ChatEvent) -> dict[str, str]:
    """Clear the active workflow for this thread."""
    cleared = await _session_store.clear(
        user_email=event.user_email or "",
        thread_name=event.thread_name,
    )
    if cleared:
        return chat_text("Conversation cleared. I'll start fresh next time.")
    return chat_text("No active conversation to clear.")


def _handle_help(user_email: str) -> dict[str, str]:
    """List commands the user has access to."""
    lines = ["Available commands:"]
    for workflow in WORKFLOWS.values():
        lines.append(f"• `{workflow.command_name}` — {workflow.description}")
    lines.append("• `/exit` — end the current conversation.")
    lines.append("• `/help` — show this list.")
    if authorize_bootstrap_admin(user_email):
        lines.append("")
        lines.append("Admin commands (you are a bootstrap admin):")
        lines.append("• `/grant <command> <type>:<principal>` — add an access rule.")
        lines.append("• `/revoke <command> <type>:<principal>` — remove an access rule.")
        lines.append("• `/list-access <command>` — show rules for a command.")
    return chat_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Admin commands — table is touched only via these handlers
# ---------------------------------------------------------------------------


async def _handle_admin_command(event: ChatEvent) -> dict[str, str]:
    """Authorize the caller as a bootstrap admin and dispatch."""
    assert event.user_email is not None  # caller already checked
    if not authorize_bootstrap_admin(event.user_email):
        log.warning(
            "Admin command denied: user=%s command_id=%s",
            event.user_email,
            event.slash_command_id,
        )
        return chat_text(
            "Admin commands are restricted to bootstrap admins."
        )

    if event.slash_command_id == RESERVED_GRANT_COMMAND_ID:
        return await _handle_grant(event)
    if event.slash_command_id == RESERVED_REVOKE_COMMAND_ID:
        return await _handle_revoke(event)
    if event.slash_command_id == RESERVED_LIST_ACCESS_COMMAND_ID:
        return await _handle_list_access(event)
    return chat_text("Unrecognised admin command.")


async def _handle_grant(event: ChatEvent) -> dict[str, str]:
    parsed = _parse_grant_revoke(event.prompt, _GRANT_USAGE)
    if isinstance(parsed, str):
        return chat_text(parsed)  # error message
    workflow, rule_type, principal = parsed
    inserted = await _access_store.grant(
        command_id=workflow.command_id,
        rule_type=rule_type,
        principal=principal,
        created_by=event.user_email or "",
    )
    log.info(
        "GRANT command=%s rule=%s:%s by=%s new=%s",
        workflow.command_name,
        rule_type,
        principal,
        event.user_email,
        inserted,
    )
    verb = "Granted" if inserted else "Already granted"
    return chat_text(
        f"{verb}: `{workflow.command_name}` → {rule_type}:`{principal}`."
    )


async def _handle_revoke(event: ChatEvent) -> dict[str, str]:
    parsed = _parse_grant_revoke(event.prompt, _REVOKE_USAGE)
    if isinstance(parsed, str):
        return chat_text(parsed)
    workflow, rule_type, principal = parsed
    removed = await _access_store.revoke(
        command_id=workflow.command_id,
        rule_type=rule_type,
        principal=principal,
    )
    log.info(
        "REVOKE command=%s rule=%s:%s by=%s removed=%s",
        workflow.command_name,
        rule_type,
        principal,
        event.user_email,
        removed,
    )
    if removed:
        return chat_text(
            f"Revoked: `{workflow.command_name}` → {rule_type}:`{principal}`."
        )
    return chat_text(
        f"No matching rule found for `{workflow.command_name}` → "
        f"{rule_type}:`{principal}`."
    )


async def _handle_list_access(event: ChatEvent) -> dict[str, str]:
    parts = event.prompt.split()
    if not parts:
        return chat_text(_LIST_USAGE)
    workflow = _resolve_command_arg(parts[0])
    if workflow is None:
        return chat_text(f"No such command: `{parts[0]}`.")
    rules = await _access_store.list_rules(workflow.command_id)
    if not rules:
        mode = workflow.default_access.value
        return chat_text(
            f"No rules for `{workflow.command_name}` "
            f"(default-access: {mode})."
        )
    lines = [f"Rules for `{workflow.command_name}` (default-access: "
             f"{workflow.default_access.value}):"]
    for row in rules:
        lines.append(
            f"• {row.rule_type}:`{row.principal}` "
            f"— by {row.created_by} at {row.created_at}"
        )
    return chat_text("\n".join(lines))


def _parse_grant_revoke(
    prompt: str,
    usage: str,
) -> tuple[Workflow, str, str] | str:
    """Parse a ``<command> <type>:<principal>`` admin argument.

    Returns a ``(workflow, rule_type, principal)`` tuple on success or
    a user-formatted error string on failure. ``usage`` is the message
    shown when the syntax itself is wrong (differs between
    ``/grant`` and ``/revoke``).
    """
    parts = prompt.split()
    if len(parts) < 2:
        return usage
    command_arg, rule_arg = parts[0], parts[1]
    # Catch references to reserved commands first — they aren't in the
    # WORKFLOWS registry, so the regular lookup would just say "no such
    # command" and hide the real reason: admin commands are governed
    # by env vars, not by the rules table they manage.
    reserved_id = _resolve_reserved(command_arg)
    if reserved_id is not None:
        if reserved_id in ADMIN_COMMAND_IDS:
            return (
                "Admin commands are governed by `BOOTSTRAP_ADMIN_*` env "
                "vars, not the rules table. Update the env vars instead."
            )
        return (
            f"`{command_arg}` is a reserved command and has no access "
            "rules."
        )
    workflow = _resolve_command_arg(command_arg)
    if workflow is None:
        return f"No such command: `{command_arg}`."
    if ":" not in rule_arg:
        return usage
    rule_type, _, principal = rule_arg.partition(":")
    rule_type = rule_type.strip().lower()
    principal = principal.strip()
    if rule_type not in VALID_RULE_TYPES:
        valid = ", ".join(sorted(VALID_RULE_TYPES))
        return f"Unknown rule type `{rule_type}`. Use one of: {valid}."
    if not principal:
        return usage
    return workflow, rule_type, principal


def _resolve_command_arg(arg: str) -> Workflow | None:
    """Map a user-typed ``/foo`` or numeric ID to a workflow."""
    if arg.isdigit():
        return get_workflow(int(arg))
    return get_workflow_by_name(arg)


def _resolve_reserved(arg: str) -> int | None:
    """Return the reserved command ID for ``/exit`` etc., else ``None``."""
    if arg.isdigit():
        cid = int(arg)
        return cid if cid in set(RESERVED_COMMAND_NAMES.values()) else None
    name = arg if arg.startswith("/") else f"/{arg}"
    return RESERVED_COMMAND_NAMES.get(name)


# ---------------------------------------------------------------------------
# Agent run
# ---------------------------------------------------------------------------


_FOLLOWUP_HINT: Final = "_Follow up messages will reply to this thread._"

_GENERIC_REPLY_FAILED: Final = (
    "I ran into an unexpected error while processing that request."
)


# ---------------------------------------------------------------------------
# Owner-assignment card — builder, handler, resume task
# ---------------------------------------------------------------------------

_OWNER_ASSIGNMENT_FUNCTION: Final = "owner_assignment"
_OPEN_INVITE_DIALOG_FUNCTION: Final = "open_invite_dialog"
_SUBMIT_INVITES_FUNCTION: Final = "submit_invites"

_STATE_KEYS_TO_RESET_ON_CARD: Final = [
    # Clear fan-out outputs so the LLM agents always start from a blank slate
    # on resume.  ConditionalFanOutAgent prevents them from running on the
    # first pass, but sessions created before that fix may still have stale
    # values in state that bias LLM output even without an explicit idempotency
    # check.  Clearing here is defensive and a no-op for clean sessions.
    MTG_EMAIL_DRAFTS,
    MTG_CALENDAR_HOLDS,
    MTG_TRACKER_ROWS,
    MTG_NOTES_DOC,
    MTG_GATE_VERDICT,
    MTG_GATE_FAILED,
    MTG_ASSEMBLY_STATUS,
]


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


def _confirmation_card_body(text: str) -> dict:
    """A minimal confirmation card that replaces the owner-assignment form."""
    return {
        "cardsV2": [{
            "cardId": "owner_assignment",
            "card": {
                "sections": [{
                    "widgets": [{
                        "textParagraph": {"text": text}
                    }]
                }]
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
    log.info(
        "card.parse_form_inputs.raw",
        extra={"json_fields": {
            "keys": list(form_inputs.keys()),
            "inputs": {
                k: {ik: iv for ik, iv in v.items()} if isinstance(v, dict) else v
                for k, v in form_inputs.items()
            },
        }},
    )
    assignments: dict[str, dict[str, str]] = {}
    for key, inp in form_inputs.items():
        if "::" not in key:
            log.info("card.parse_form_inputs.skip_no_sep key=%r", key)
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
        log.info(
            "card.parse_form_inputs.field",
            extra={"json_fields": {
                "widget_key": key,
                "field": field,
                "item_id": item_id,
                "raw_inp_keys": list(inp.keys()) if isinstance(inp, dict) else None,
                "parsed_val": val,
                "kept": bool(val),
            }},
        )
        if val:
            assignments.setdefault(item_id, {})[field] = val
    log.info(
        "card.parse_form_inputs.result",
        extra={"json_fields": {"assignments": assignments}},
    )
    return assignments


def _epoch_ms_to_date(ms_str: str) -> str | None:
    """Convert a dateTimePicker epoch-millisecond string to an ISO 8601 date.

    Returns ``None`` if the string is absent or unparseable.
    """
    try:
        from datetime import timezone as _tz
        ts = int(ms_str) / 1000
        result = datetime.fromtimestamp(ts, tz=_tz.utc).date().isoformat()
        log.info("card.epoch_ms_to_date ms_str=%r -> %r", ms_str, result)
        return result
    except Exception:
        log.warning("card.epoch_ms_to_date failed to parse ms_str=%r", ms_str, exc_info=True)
        return None


async def _handle_card_clicked(
    body: dict, background_tasks: BackgroundTasks
) -> dict[str, Any]:
    """Route a CARD_CLICKED event by its invoked action function.

    Three card actions exist:
    * ``owner_assignment`` — the owner/due-date form (handled below).
    * ``open_invite_dialog`` — opens the calendar-invite dialog.
    * ``submit_invites`` — applies the chosen attendees to the events.
    """
    evt = CardClickedEvent.from_payload(body)

    if evt.invoked_function == _OPEN_INVITE_DIALOG_FUNCTION:
        return await _handle_open_invite_dialog(evt)
    if evt.invoked_function == _SUBMIT_INVITES_FUNCTION:
        return await _handle_submit_invites(evt, background_tasks)

    # Default / legacy: the owner-assignment form.
    raw_form_inputs = evt.form_inputs
    log.info(
        "card.raw_payload",
        extra={"json_fields": {
            "action": (body.get("action") or {}),
            "form_input_keys": list(raw_form_inputs.keys()),
            "form_inputs_raw": raw_form_inputs,
        }},
    )

    log.info(
        "card.parsed_event",
        extra={"json_fields": {
            "invoker_email": evt.invoker_email,
            "decision": evt.decision,
            "thread_name": evt.thread_name,
            "message_name": evt.message_name,
            "form_inputs": evt.form_inputs,
        }},
    )

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

    # Parse form inputs and apply to MTG_PARSED
    assignments = _parse_form_inputs(evt.form_inputs)
    log.info(
        "card.handler.assignments",
        extra={"json_fields": {
            "session_id": session_id,
            "decision": evt.decision,
            "assignments": assignments,
            "item_ids_in_assignments": list(assignments.keys()),
        }},
    )

    parsed_data = session.state.get(MTG_PARSED) or {}
    try:
        parsed = ParsedMeeting.model_validate(parsed_data)
    except Exception:
        log.exception("CARD_CLICKED: failed to parse MTG_PARSED for session %s", session_id)
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "An error occurred processing the form. Please run `/meeting` again."
        )}

    log.info(
        "card.handler.before_apply",
        extra={"json_fields": {
            "session_id": session_id,
            "items": [
                {"id": i.id, "owner": i.owner, "due_date": i.due_date}
                for i in parsed.action_items
            ],
        }},
    )

    for item in parsed.action_items:
        if item.id in assignments:
            a = assignments[item.id]
            old_owner, old_due = item.owner, item.due_date
            if a.get("assignee"):
                # Normalize to display name so the format matches parser
                # output ("Sarah Chen", not "Sarah Chen (email@...)").
                # This ensures the email_drafter groups all items for the
                # same person into one draft regardless of how the owner was
                # originally stored.
                item.owner = _attendee_display_name(a["assignee"])
            if a.get("due"):
                item.due_date = _epoch_ms_to_date(a["due"])
            log.info(
                "card.handler.apply",
                extra={"json_fields": {
                    "item_id": item.id,
                    "owner_before": old_owner,
                    "owner_after": item.owner,
                    "due_before": old_due,
                    "due_after": item.due_date,
                    "raw_assignee": a.get("assignee"),
                    "raw_due_ms": a.get("due"),
                }},
            )
        else:
            log.info(
                "card.handler.no_assignment",
                extra={"json_fields": {
                    "item_id": item.id,
                    "owner": item.owner,
                    "due_date": item.due_date,
                    "reason": "item_id not in submitted assignments",
                }},
            )

    log.info(
        "card.handler.after_apply",
        extra={"json_fields": {
            "session_id": session_id,
            "items": [
                {"id": i.id, "owner": i.owner, "due_date": i.due_date}
                for i in parsed.action_items
            ],
        }},
    )

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
        log.info(
            "card.handler.patch_persisted",
            extra={"json_fields": {
                "session_id": session_id,
                "state_keys_written": [k for k, v in state_patch.items() if v is not None],
                "state_keys_cleared": [k for k, v in state_patch.items() if v is None],
            }},
        )
    except Exception:
        log.exception("CARD_CLICKED: failed to persist state patch for session %s", session_id)
        return {"actionResponse": {"type": "UPDATE_MESSAGE"}, **_confirmation_card_body(
            "An error occurred saving your assignments. Please try again."
        )}

    workflow = _workflow_for(session.state.get(STATE_ACTIVE_WORKFLOW_ID))
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
    # Confirm the state patch was actually persisted before re-running.
    cfg = settings()
    resume_session = await _session_store.service.get_session(
        app_name=cfg.app_name,
        user_id=invoker_email,
        session_id=session_id,
    )
    if resume_session:
        parsed_data = resume_session.state.get(MTG_PARSED) or {}
        items_snapshot = []
        try:
            from workflows.meeting_engine.schemas import ParsedMeeting as _PM
            pm = _PM.model_validate(parsed_data)
            items_snapshot = [
                {"id": i.id, "owner": i.owner, "due_date": i.due_date}
                for i in pm.action_items
            ]
        except Exception:
            items_snapshot = [{"raw_keys": list(parsed_data.keys())}]
        log.info(
            "card.resume.state_check",
            extra={"json_fields": {
                "session_id": session_id,
                "gate_state": resume_session.state.get(MTG_OWNER_GATE_STATE),
                "assembly_status": resume_session.state.get(MTG_ASSEMBLY_STATUS),
                "email_drafts_present": resume_session.state.get(MTG_EMAIL_DRAFTS) is not None,
                "items": items_snapshot,
            }},
        )
    else:
        log.warning(
            "card.resume.session_missing session_id=%s invoker=%s",
            session_id,
            invoker_email,
        )

    try:
        reply = await _run_agent(
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
            _GENERIC_REPLY_FAILED,
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


async def _handle_open_invite_dialog(evt: CardClickedEvent) -> dict[str, Any]:
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
    # The USER picker's search is server-side and never reaches us, but the
    # SELECTED users/<id> values DO arrive here on submit — log them so the
    # end-to-end selection→resolution path is visible in Cloud Run logs.
    log.info(
        "invite.submit.selections",
        extra={"json_fields": {
            "invoker_email": invoker_email,
            "thread_name": thread_name,
            "raw_form_input_keys": list(evt.form_inputs.keys()),
            "chosen": chosen,
        }},
    )
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
        resolved_emails: list[str] = []
        if org_ids:
            try:
                resolved_json = await call_context_tool(
                    invoker_email, "resolve_people_emails", {"resource_names": org_ids},
                )
                for entry in json.loads(resolved_json):
                    if entry.get("email"):
                        resolved_emails.append(entry["email"])
                        emails.add(entry["email"])
            except Exception:
                log.exception("invite.apply: failed to resolve org people")
                results.append(f"• {item_id}: could not resolve org people")
        log.info(
            "invite.apply.item",
            extra={"json_fields": {
                "item_id": item_id,
                "event_id": event_id,
                "attendee_emails": sel.get("attendees", []),
                "external_raw": sel.get("ext", []),
                "org_resource_names": org_ids,
                "org_resolved_emails": resolved_emails,
                "final_emails": sorted(emails),
            }},
        )
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
      3. Run the agent.
      4. Post the final reply into the anchor thread so it nests
         directly under the announcement.
    """
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

    # Check whether the owner-assignment gate suspended the pipeline.
    # If so, post the card form instead of a text reply.
    cfg = settings()
    post_run_session = await _session_store.service.get_session(
        app_name=cfg.app_name,
        user_id=user_email,
        session_id=resolved.session.id,
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
            log.exception("Failed to build owner card for session %s", resolved.session.id)
            await post_message_to_space(space_resource, _GENERIC_REPLY_FAILED, thread_name=anchor_thread)
            return
        incomplete = [i for i in parsed.action_items if not i.owner or not i.due_date]
        card = _build_owner_assignment_card(incomplete, parsed.attendees, parsed.title, user_email)
        card_msg_name = await post_card_to_space(space_resource, card, thread_name=anchor_thread)
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
        thread_name=anchor_thread,
    )
    # If the meeting pipeline created calendar reminders (no owner card was
    # needed), offer the optional invite dialog.
    await _post_invite_card_if_ready(
        space_resource=space_resource,
        thread_name=anchor_thread,
        user_email=user_email,
        session_id=resolved.session.id,
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
