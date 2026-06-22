"""Pydantic schemas for the RFI Response Engine."""

from pydantic import BaseModel

from workflows.common.grounding import SourceRef


class RFIQuestion(BaseModel):
    """One question extracted from the customer's RFI template.

    ``answer_location`` is the opaque locator produced by the Action MCP's
    ``extract_rfi_questions`` tool — a spreadsheet cell (``"Sheet1!C5"``), a
    Word table cell (``"tbl-0!r3c2"``), or a Word paragraph anchor
    (``"para-12"``). It is passed back verbatim to ``fill_rfi_answers``.
    """

    id: str
    text: str
    answer_location: str
    mandatory: bool = True
    category: str | None = None


class RFIQuestionSet(BaseModel):
    """Wrapper so the parser ``LlmAgent`` can emit a list via ``output_schema``."""

    questions: list[RFIQuestion] = []


class RFIGuidance(BaseModel):
    """Scope guidance collected from the user via the upfront form (Form 1)."""

    facilities: str = ""
    segment: str = ""
    customer: str = ""
    industry: str = ""


class RFIAnswer(BaseModel):
    """A drafted answer to one RFI question.

    ``needs_human`` is set by the research agent when it cannot produce a
    confident, grounded answer; those questions are surfaced in the gap-fill
    form (Form 2) for a human to complete.
    """

    question_id: str
    answer: str = ""
    sources: list[SourceRef] = []
    needs_human: bool = False


class RFIAnswerSet(BaseModel):
    """Wrapper so the research ``LlmAgent`` can emit a list via ``output_schema``."""

    answers: list[RFIAnswer] = []
