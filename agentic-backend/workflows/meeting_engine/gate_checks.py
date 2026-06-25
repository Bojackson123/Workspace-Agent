"""Pure-Python gate checks for the meeting pipeline.

Each check reads ``MTG_PARSED`` from state and returns a :class:`GateCheck`.
Missing owners/due-dates are WARNINGs (they drive the owner-assignment
card, not a hard stop); missing source references are BLOCKERs.
"""

from __future__ import annotations

from datetime import datetime, timezone

from workflows.common.gate import GateCheck
from workflows.common.state_keys import MTG_PARSED
from workflows.meeting_engine._identity import _identity_keys
from workflows.meeting_engine.schemas import ParsedMeeting


def _check_all_owners(state: dict) -> GateCheck:
    parsed_data = state.get(MTG_PARSED)
    if not parsed_data:
        return GateCheck(
            id="owners_assigned",
            passed=False,
            severity="BLOCKER",
            detail="No parsed meeting data found in state.",
        )
    parsed = ParsedMeeting.model_validate(parsed_data)
    missing = [a.id for a in parsed.action_items if not a.owner]
    return GateCheck(
        id="owners_assigned",
        passed=not missing,
        severity="WARNING",
        detail=(
            f"Action items without owner: {missing}" if missing
            else "All action items have an owner."
        ),
    )


def _check_all_due_dates(state: dict) -> GateCheck:
    parsed_data = state.get(MTG_PARSED)
    if not parsed_data:
        return GateCheck(
            id="due_dates_set",
            passed=False,
            severity="BLOCKER",
            detail="No parsed meeting data found in state.",
        )
    parsed = ParsedMeeting.model_validate(parsed_data)
    missing = [a.id for a in parsed.action_items if not a.due_date]
    return GateCheck(
        id="due_dates_set",
        passed=not missing,
        severity="WARNING",
        detail=(
            f"Action items without due date: {missing}" if missing
            else "All action items have a due date."
        ),
    )


def _check_sources(state: dict) -> GateCheck:
    parsed_data = state.get(MTG_PARSED)
    if not parsed_data:
        return GateCheck(
            id="sources_present",
            passed=False,
            severity="BLOCKER",
            detail="No parsed meeting data found in state.",
        )
    parsed = ParsedMeeting.model_validate(parsed_data)
    missing_ids: list[str] = []
    for item in parsed.action_items:
        if not item.sources:
            missing_ids.append(f"action:{item.id}")
    for dec in parsed.decisions:
        if not dec.sources:
            missing_ids.append(f"decision:{dec.id}")
    return GateCheck(
        id="sources_present",
        passed=not missing_ids,
        severity="BLOCKER",
        detail=(
            f"Items/decisions with no source references: {missing_ids}"
            if missing_ids
            else "All items and decisions have source references."
        ),
    )


def _warn_stale_dates(state: dict) -> GateCheck:
    parsed_data = state.get(MTG_PARSED)
    if not parsed_data:
        return GateCheck(
            id="dates_not_stale",
            passed=True,
            severity="WARNING",
            detail="No parsed meeting data (skipping stale-date check).",
        )
    parsed = ParsedMeeting.model_validate(parsed_data)
    today = datetime.now(timezone.utc).date().isoformat()
    stale = [
        a.id for a in parsed.action_items
        if a.due_date and a.due_date <= today
    ]
    return GateCheck(
        id="dates_not_stale",
        passed=not stale,
        severity="WARNING",
        detail=(
            f"Action items with past or today due dates: {stale}"
            if stale
            else "No stale due dates."
        ),
    )


def _warn_unattributed_attendees(state: dict) -> GateCheck:
    parsed_data = state.get(MTG_PARSED)
    if not parsed_data:
        return GateCheck(
            id="attendees_attributed",
            passed=True,
            severity="WARNING",
            detail="No parsed meeting data (skipping attendee check).",
        )
    parsed = ParsedMeeting.model_validate(parsed_data)
    # Match each attendee against the owners by identity keys (name and/or
    # email), so a "Name (email)" attendee still matches a name-only owner.
    # A plain string-equality check fails on the combined "Name (email)" form.
    owner_key_sets = [
        _identity_keys(item.owner)
        for item in parsed.action_items
        if item.owner
    ]
    unattributed = [
        a for a in parsed.attendees
        if not any(_identity_keys(a) & owner_keys for owner_keys in owner_key_sets)
    ]
    return GateCheck(
        id="attendees_attributed",
        passed=not unattributed,
        severity="WARNING",
        detail=(
            f"Attendees with no attributed items: {unattributed}"
            if unattributed
            else "All attendees attributed."
        ),
    )


_MEETING_GATE_CHECKS = [
    _check_all_owners,
    _check_all_due_dates,
    _check_sources,
    _warn_stale_dates,
    _warn_unattributed_attendees,
]
