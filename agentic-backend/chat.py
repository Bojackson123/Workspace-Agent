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

import logging
import re
from dataclasses import dataclass
from typing import Any, Final

from fastapi import BackgroundTasks
from google.adk.runners import Runner
from google.genai import types

from access import authorize, authorize_bootstrap_admin
from access_store import AccessStore, VALID_RULE_TYPES
from agent import build_agent_for_workflow
from chat_client import post_message_to_space
from config import settings
from sessions import SessionStore
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

log = logging.getLogger(__name__)

# Subset of Google Chat event types the agent reacts to.
EVENT_ADDED_TO_SPACE: Final = "ADDED_TO_SPACE"
EVENT_MESSAGE: Final = "MESSAGE"

WELCOME_MESSAGE: Final = (
    "Hello! I am your Dual-MCP Workspace Assistant. "
    "Type `/help` to see available commands, or just ask me a question."
)

_GRANT_USAGE: Final = (
    "Usage: `/grant <command> <type>:<principal>`\n"
    "  type is one of `email`, `domain`, `group`.\n"
    "  example: `/grant /audit group:finance-leads@example.com`"
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

        return cls(
            event_type=body.get("type", ""),
            user_email=(body.get("user") or {}).get("email"),
            prompt=_clean_prompt(message),
            slash_command_id=command_id,
            thread_name=thread_name,
            space_resource=space_resource,
            message_thread=message_thread,
        )


def _conversation_key(body: dict, message: dict) -> str:
    """Return the stable key identifying the conversation a message belongs to.

    In ROOM spaces, users deliberately reply-in-thread to start sub-
    conversations, so threads are the right grain. In DMs and group
    chats, Chat creates a fresh ``thread.name`` for every top-level
    message unless the user explicitly clicks "Reply in thread" — keying
    by thread there would split a continuous conversation across many
    one-shot sessions. We key by ``space.name`` for those space types so
    the whole DM behaves as one conversation, matching user expectation.
    """
    space = body.get("space") or {}
    space_type = (space.get("type") or space.get("spaceType") or "").upper()
    thread_name = (message.get("thread") or {}).get("name") or ""
    space_name = space.get("name") or ""

    if space_type in {"DIRECT_MESSAGE", "DM", "GROUP_CHAT"}:
        return space_name or thread_name
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

    # ---- Regular workflow dispatch ----
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

    try:
        resolved = await _session_store.resolve(
            user_email=event.user_email,
            thread_name=event.thread_name,
            new_workflow_id=event.slash_command_id,
        )
    except Exception:
        log.exception("Failed to resolve session for %s", event.user_email)
        return chat_text(
            "I ran into an unexpected error while processing that request."
        )

    workflow = _workflow_for(resolved.active_workflow_id)
    log.info(
        "Scheduling %s for user=%s thread=%s (new_session=%s)",
        workflow.command_name,
        event.user_email,
        event.thread_name,
        resolved.is_new,
    )

    background_tasks.add_task(
        _run_agent_and_post,
        workflow=workflow,
        user_email=event.user_email,
        session_id=resolved.session.id,
        prompt=event.prompt,
        space_resource=event.space_resource,
        message_thread=event.message_thread,
    )

    # Only slash-command invocations carry an opt-in ack — plain
    # follow-ups inside an active session stay silent so the thread
    # doesn't fill with "working on it" noise.
    if event.slash_command_id is not None and workflow.ack_message:
        return chat_text(workflow.ack_message)
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


async def _run_agent_and_post(
    *,
    workflow: Workflow,
    user_email: str,
    session_id: str,
    prompt: str,
    space_resource: str,
    message_thread: str,
) -> None:
    """Background task: run the agent, post its reply back to Chat.

    Lives off the request lifecycle so a long workflow no longer
    blocks the webhook. Failures are reported back into the same
    space as a Chat message — there is no synchronous response left
    to attach an error to.
    """
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
            "I ran into an unexpected error while processing that request.",
            thread_name=message_thread or None,
        )
        return

    text = _markdown_to_chat(reply) if reply else "I wasn't able to generate a response."
    await post_message_to_space(
        space_resource,
        text,
        thread_name=message_thread or None,
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

    final_text = ""
    async for event in runner.run_async(
        user_id=user_email,
        session_id=session_id,
        new_message=message,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = event.content.parts[0].text or ""

    return final_text


def get_access_store() -> AccessStore:
    """Expose the process-wide :class:`AccessStore` to auxiliary tools."""
    return _access_store
