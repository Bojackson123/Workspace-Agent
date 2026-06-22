"""Pydantic schemas for the Customer IQ Engine (/iq).

The research ``LlmAgent`` emits a single ``CustomerIQReport`` via
``output_schema``; the deterministic assembler reads it back from session
state and fills the Google Doc template.
"""

from typing import Literal

from pydantic import BaseModel

from workflows.common.grounding import SourceRef


class KeyContact(BaseModel):
    """A decision-maker worth knowing (procurement, supply chain, ops, eng)."""

    name: str
    title: str = ""
    note: str = ""


class FitAssessment(BaseModel):
    """How good a Sanmina manufacturing-services opportunity this company is."""

    tier: Literal["High", "Medium", "Low"]
    recommended_segment: str  # which Sanmina segment/capability to route to
    rationale: str


class CustomerIQReport(BaseModel):
    """The full Customer IQ dossier for one company.

    Scalar fields map to single ``{{placeholder}}`` tokens; list fields are
    rendered as ``•``-prefixed text blocks by the assembler. Only
    ``company_name``, ``executive_summary`` and ``fit`` are required; the rest
    default empty so a sparsely-documented company still produces a valid doc.
    """

    company_name: str
    executive_summary: str

    # Company overview (firmographics)
    legal_name: str = ""
    headquarters: str = ""
    founded: str = ""
    ownership: str = ""  # public / private / PE-backed, + ticker if public
    website: str = ""
    employee_count: str = ""
    revenue: str = ""

    # Business & products
    business_description: str = ""
    products: list[str] = []
    end_markets: list[str] = []

    # Manufacturing & supply-chain footprint
    manufacturing_footprint: str = ""
    current_ems_providers: list[str] = []  # incumbents to displace

    # Fit assessment
    fit: FitAssessment

    # Signals & context
    opportunity_signals: list[str] = []
    compliance_needs: list[str] = []
    competitors: list[str] = []
    key_contacts: list[KeyContact] = []
    recent_news: list[str] = []
    risk_flags: list[str] = []
    recommended_next_steps: list[str] = []

    sources: list[SourceRef] = []
