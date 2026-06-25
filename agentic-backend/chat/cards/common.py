"""Helpers shared across the card/form workflows.

Includes the command-id literals that branch the slash runner into the
attachment-intake / tailoring-form paths. They are kept as literals
(rather than imported from the workflow modules) to avoid pulling the
workflow registry into the card layer at import time.
"""

from __future__ import annotations

import logging
from typing import Any, Final

from google.adk.events import Event, EventActions

from chat.stores import _session_store

log = logging.getLogger(__name__)

# Command id of the RFI workflow — the slash runner uses this to branch
# into the attachment-intake + form-posting path.
_RFI_COMMAND_ID: Final = 6

# Command id of the Customer IQ workflow — branches the runner into the
# tailoring-form path (post the form first, then run on submit).
_IQ_COMMAND_ID: Final = 7


def _confirmation_card_body(text: str) -> dict:
    """A minimal confirmation card that replaces a form in place."""
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


def _form_text(form_inputs: dict, key: str) -> str:
    """Read a single text value from a form input by widget name."""
    inp = form_inputs.get(key) or {}
    vals = (inp.get("stringInputs") or {}).get("value") or []
    return (vals[0] if vals else "").strip()


def _form_values(form_inputs: dict, key: str) -> list[str]:
    """Read every selected value from a multi-select form input by widget name."""
    inp = form_inputs.get(key) or {}
    vals = (inp.get("stringInputs") or {}).get("value") or []
    return [v.strip() for v in vals if isinstance(v, str) and v.strip()]


async def _append_state(session: Any, delta: dict[str, Any]) -> None:
    """Persist a state delta onto *session* via an ADK event (best effort)."""
    try:
        await _session_store.service.append_event(
            session, Event(author="card", actions=EventActions(state_delta=delta))
        )
    except Exception:
        log.exception("card: failed to persist state delta %s", list(delta.keys()))
