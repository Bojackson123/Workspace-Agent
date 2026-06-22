"""Pure-Python completeness/grounding checks for the RFI gate."""

from __future__ import annotations

from workflows.common.gate import GateCheck
from workflows.rfi_engine._helpers import _answers_list, _questions_list


def _check_all_answered(state: dict) -> GateCheck:
    questions = _questions_list(state)
    answers = {a.question_id: a for a in _answers_list(state)}
    mandatory = [q.id for q in questions if q.mandatory]
    missing = [
        qid for qid in mandatory
        if qid not in answers or (not answers[qid].answer.strip() and not answers[qid].needs_human)
    ]
    return GateCheck(
        id="all_mandatory_answered",
        passed=not missing,
        severity="WARNING",
        detail=(
            f"Mandatory questions with no answer or human flag: {missing}"
            if missing else "All mandatory questions are addressed."
        ),
    )


def _check_grounding(state: dict) -> GateCheck:
    ungrounded: list[str] = []
    for ans in _answers_list(state):
        if ans.needs_human:
            continue
        # Reuse the quantitative-grounding validator: treat each source-less
        # answer carrying a numeric claim as ungrounded. We approximate by
        # flagging answered questions that cite no sources at all.
        if ans.answer.strip() and not ans.sources:
            ungrounded.append(ans.question_id)
    return GateCheck(
        id="answers_grounded",
        passed=not ungrounded,
        severity="WARNING",
        detail=(
            f"Answers with no cited sources: {ungrounded}"
            if ungrounded else "All answers cite at least one source."
        ),
    )


def _count_gaps(state: dict) -> GateCheck:
    gaps = [a.question_id for a in _answers_list(state) if a.needs_human]
    return GateCheck(
        id="human_gaps",
        passed=not gaps,
        severity="WARNING",
        detail=(
            f"Questions needing human input: {gaps}" if gaps
            else "No questions require human input."
        ),
    )


_RFI_GATE_CHECKS = [_check_all_answered, _check_grounding, _count_gaps]
