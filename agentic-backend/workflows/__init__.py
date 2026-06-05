"""Workflow registry.

Each non-underscore module in this package exposes a
``WORKFLOW: Workflow`` constant. This module imports them explicitly
and assembles the ``WORKFLOWS`` dispatch dict keyed by ``command_id``.

**To add a workflow:**

1. Create a new module in this package exposing
   ``WORKFLOW: Workflow``. Use ``llm_workflow(...)`` for the common
   "one ``LlmAgent`` with toolsets" shape; construct the agent
   directly (``SequentialAgent``, ``LoopAgent``, custom
   ``BaseAgent``, …) for richer flows.
2. Import it below and add it to the ``_REGISTERED`` list.
3. Register its ``command_id`` in the Google Cloud console under
   *Chat API → Configuration → Commands*.

**To temporarily disable a workflow** without deleting code, comment
out its entry in ``_REGISTERED``. The module still imports so its
prompt and helpers stay in editor-friendly reach, but the dispatcher
will report it as unknown.

Discovery is explicit by design: it surfaces import errors at startup
instead of at the first invocation, makes it trivial to toggle a
workflow on/off, and avoids any "magic" that obscures the dispatch
table from a quick reading.
"""

from typing import Final

# Re-export the core types so external callers keep doing
# ``from workflows import Workflow, AccessMode, ...``.
from workflows._base import (
    ADMIN_COMMAND_IDS,
    AccessMode,
    RESERVED_COMMAND_NAMES,
    RESERVED_EXIT_COMMAND_ID,
    RESERVED_GRANT_COMMAND_ID,
    RESERVED_HELP_COMMAND_ID,
    RESERVED_LIST_ACCESS_COMMAND_ID,
    RESERVED_REVOKE_COMMAND_ID,
    ToolsetKind,
    Workflow,
)
from workflows._default import DEFAULT_WORKFLOW
from workflows._helpers import llm_workflow

# ---- Per-command imports -------------------------------------------------
from workflows.draft import WORKFLOW as _DRAFT_WORKFLOW
from workflows.meeting_engine import WORKFLOW as _MEETING_WORKFLOW
from workflows.research import WORKFLOW as _RESEARCH_WORKFLOW
from workflows.review_board import WORKFLOW as _REVIEW_WORKFLOW
from workflows.sequential_report import WORKFLOW as _REPORT_WORKFLOW

# Explicit dispatch list. Comment out an entry to temporarily disable a
# workflow without removing its file.
_REGISTERED: list[Workflow] = [
    _RESEARCH_WORKFLOW,
    _DRAFT_WORKFLOW,
    _REPORT_WORKFLOW,
    _MEETING_WORKFLOW,
    _REVIEW_WORKFLOW,
]


def _build_registry() -> dict[int, Workflow]:
    """Validate and index ``_REGISTERED`` by ``command_id``."""
    out: dict[int, Workflow] = {}
    for workflow in _REGISTERED:
        existing = out.get(workflow.command_id)
        if existing is not None:
            raise RuntimeError(
                "Duplicate command_id "
                f"{workflow.command_id} between {existing.command_name} "
                f"and {workflow.command_name}"
            )
        out[workflow.command_id] = workflow
    return out


WORKFLOWS: Final[dict[int, Workflow]] = _build_registry()


def get_workflow(command_id: int) -> Workflow | None:
    """Look up a workflow by its Chat command ID."""
    return WORKFLOWS.get(command_id)


def get_workflow_by_name(command_name: str) -> Workflow | None:
    """Look up a workflow by its slash-command text (``/foo``)."""
    target = (
        command_name if command_name.startswith("/") else f"/{command_name}"
    )
    for workflow in WORKFLOWS.values():
        if workflow.command_name == target:
            return workflow
    return None


__all__ = [
    "ADMIN_COMMAND_IDS",
    "AccessMode",
    "DEFAULT_WORKFLOW",
    "RESERVED_COMMAND_NAMES",
    "RESERVED_EXIT_COMMAND_ID",
    "RESERVED_GRANT_COMMAND_ID",
    "RESERVED_HELP_COMMAND_ID",
    "RESERVED_LIST_ACCESS_COMMAND_ID",
    "RESERVED_REVOKE_COMMAND_ID",
    "ToolsetKind",
    "WORKFLOWS",
    "Workflow",
    "get_workflow",
    "get_workflow_by_name",
    "llm_workflow",
]
