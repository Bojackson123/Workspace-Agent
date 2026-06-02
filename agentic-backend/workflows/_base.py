"""Core types for the workflow package.

Every workflow module in this package exposes a ``WORKFLOW: Workflow``
constant. The dispatcher dispatches on ``Workflow.command_id`` and
calls ``Workflow.build_agent(user_email)`` to produce an ADK agent for
the inbound request — single ``LlmAgent``, ``SequentialAgent``,
``LoopAgent``, custom ``BaseAgent`` subclass, or anything else the SDK
supports. The dispatcher is intentionally agnostic to which.

This module deliberately knows nothing about ADK agent construction or
MCP toolsets — those concerns live in ``agent.py`` (low-level building
blocks) and ``workflows._helpers`` (the common single-LLM case). That
import direction keeps the type definitions free of cycles.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent


class ToolsetKind(StrEnum):
    """The MCP toolsets a workflow can be granted."""

    CONTEXT = "context"  # read-only personal data, DWD-impersonated
    ACTION = "action"    # read/write Shared Drive, service-account


class AccessMode(StrEnum):
    """What happens when no DB rules exist for a workflow.

    Rules in ``workflow_access_rules`` are always authoritative when
    present; ``AccessMode`` only decides the empty-table behaviour.
    """

    OPEN = "open"
    RESTRICTED = "restricted"


# Reserved command IDs handled directly by the dispatcher, not the LLM.
RESERVED_EXIT_COMMAND_ID: Final = 999
RESERVED_HELP_COMMAND_ID: Final = 998
RESERVED_GRANT_COMMAND_ID: Final = 997
RESERVED_REVOKE_COMMAND_ID: Final = 996
RESERVED_LIST_ACCESS_COMMAND_ID: Final = 995

# Command IDs whose access is governed by env vars (bootstrap admins)
# and NOT by the rules table they manage.
ADMIN_COMMAND_IDS: Final[frozenset[int]] = frozenset({
    RESERVED_GRANT_COMMAND_ID,
    RESERVED_REVOKE_COMMAND_ID,
    RESERVED_LIST_ACCESS_COMMAND_ID,
})

# Names users can type for reserved commands. Used by the ``/grant`` /
# ``/revoke`` parsers to give a clear error when someone references a
# reserved command (which isn't in the WORKFLOWS registry).
RESERVED_COMMAND_NAMES: Final[dict[str, int]] = {
    "/exit": RESERVED_EXIT_COMMAND_ID,
    "/help": RESERVED_HELP_COMMAND_ID,
    "/grant": RESERVED_GRANT_COMMAND_ID,
    "/revoke": RESERVED_REVOKE_COMMAND_ID,
    "/list-access": RESERVED_LIST_ACCESS_COMMAND_ID,
}


# Async factory signature the dispatcher invokes per request.
AgentFactory = Callable[[str], Awaitable["BaseAgent"]]


@dataclass(frozen=True, slots=True, kw_only=True)
class Workflow:
    """A slash-command-driven workflow.

    Attributes:
        command_id: Numeric ID assigned in the Chat API console (1–1000).
            Used as the primary lookup key by the dispatcher.
        command_name: The slash command as the user types it
            (e.g. ``"/draft"``). Used for logs, ``/help``, and the
            ``/grant`` parser.
        description: One-line summary surfaced in ``/help`` listings.
        default_access: Empty-table behaviour. ``OPEN`` for general
            workflows, ``RESTRICTED`` for sensitive ones that must be
            granted explicitly.
        build_agent: Async factory that takes the verified caller email
            and returns a fully-wired ADK agent for this request. The
            dispatcher calls this once per webhook so MCP connections
            are always fresh.
        ack_message: Optional text to post immediately when this
            workflow is invoked. When set, the webhook returns this
            string synchronously and the agent run is dispatched to a
            background task that posts the final reply via the Chat
            REST API. When ``None``, the dispatcher returns an empty
            envelope (no visible ack) and still runs the agent in the
            background. Set for long-running workflows so the user sees
            something within Chat's ~6s "not responding" window.
    """

    command_id: int
    command_name: str
    description: str
    default_access: AccessMode
    build_agent: AgentFactory
    ack_message: str | None = None
