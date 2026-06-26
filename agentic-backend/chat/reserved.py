"""Reserved slash commands handled directly, without an LLM call.

``/exit`` and ``/help`` are available to everyone; ``/grant``,
``/revoke`` and ``/list-access`` edit the access-rule table but are
themselves governed by env-driven bootstrap admins, not by the table
they manage — see :func:`access.authorize_bootstrap_admin`.
"""

from __future__ import annotations

import logging
from typing import Final

from access.policy import authorize_bootstrap_admin
from access.store import VALID_RULE_TYPES
from chat.events import ChatEvent
from chat.formatting import chat_text
from chat.stores import _access_store, _session_store
from workflows import (
    ADMIN_COMMAND_IDS,
    RESERVED_COMMAND_NAMES,
    RESERVED_GRANT_COMMAND_ID,
    RESERVED_LIST_ACCESS_COMMAND_ID,
    RESERVED_REVOKE_COMMAND_ID,
    WORKFLOWS,
    Workflow,
    get_workflow,
    get_workflow_by_name,
)

log = logging.getLogger(__name__)

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
