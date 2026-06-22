"""Typed reads of the RFI question/answer sets out of session state."""

from __future__ import annotations

from workflows.common.state_keys import RFI_ANSWERS, RFI_QUESTIONS
from workflows.common.state_parse import coerce_model_list
from workflows.rfi_engine.schemas import RFIAnswer, RFIQuestion


def _answers_list(state: dict) -> list[RFIAnswer]:
    """Read ``RFI_ANSWERS`` (dict, JSON string, or None) into a typed list."""
    return coerce_model_list(state.get(RFI_ANSWERS), RFIAnswer, wrapper_key="answers")


def _questions_list(state: dict) -> list[RFIQuestion]:
    """Read ``RFI_QUESTIONS`` (dict, JSON string, or None) into a typed list."""
    return coerce_model_list(state.get(RFI_QUESTIONS), RFIQuestion, wrapper_key="questions")
