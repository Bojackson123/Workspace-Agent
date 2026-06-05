"""Pydantic schemas for the Red-Team Review Board."""

from typing import Literal

from pydantic import BaseModel

from workflows.common.grounding import Claim


class TemplateField(BaseModel):
    id: str
    name: str
    requirement: str = ""
    mandatory: bool = True
    needs_quantitative: bool = False


class FillContract(BaseModel):
    doc_title: str
    fields: list[TemplateField] = []


class FilledSection(BaseModel):
    field_id: str
    content: str = ""
    claims: list[Claim] = []


class Objection(BaseModel):
    id: str
    field_id: str
    claim_under_attack: str
    type: Literal[
        "unsupported",
        "fabricated_number",
        "hand_waved_risk",
        "circular_reasoning",
        "missing_alternative",
        "optimistic_assumption",
    ]
    severity: Literal["CRITICAL", "MAJOR", "MINOR"]
    status: Literal["OPEN", "RESOLVED", "ACCEPTED_RISK", "DISPUTED"]
    resolution_note: str | None = None


class ObjectionLedger(BaseModel):
    objections: list[Objection] = []
