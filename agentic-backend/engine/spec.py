"""Declarative description of an engine pipeline.

An :class:`EngineSpec` is an ordered list of typed *stage specs* — plain data
(JSON-serialisable) that names each stage and inlines its prompt text, while
referencing the genuinely-code parts (schemas, gate checks, predicates,
instruction providers, custom agents) by string key into
:mod:`engine.registry`. :func:`engine.compiler.build_engine`
turns a spec into the ADK ``SequentialAgent`` for a request.

Stage kinds:
    ``llm``        — an ``LlmAgent`` (optionally guarded / schema'd / tool'd).
    ``gate``       — a deterministic :class:`~workflows.common.gate.GateAgent`.
    ``form_gate``  — a suspend/resume :class:`~engine.form_gate.FormGate`.
    ``loop``       — an ADK ``LoopAgent`` over nested stages.
    ``custom``     — a registered bespoke ``BaseAgent`` factory (research
                     fan-out, deterministic assemblers, …).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class ToolsetRef(StrEnum):
    """Tools a stage's LLM can be granted (mirrors the dual-MCP boundary)."""

    CONTEXT = "context"            # read-only personal data (Context MCP)
    ACTION = "action"             # read/write Shared Drive (Action MCP)
    GOOGLE_SEARCH = "google_search"  # built-in web grounding


class GuardSpec(BaseModel):
    """Wrap a stage in a :class:`~workflows.common.conditional.GuardAgent`."""

    skip_when: str                   # predicate registry key
    skip_text: str
    restore_key: str | None = None   # re-emit state[restore_key] on the skip branch


class LlmStageSpec(BaseModel):
    kind: Literal["llm"] = "llm"
    name: str
    # Exactly one of these: a registered instruction provider key, or literal text.
    instruction: str | None = None
    instruction_text: str | None = None
    toolsets: list[ToolsetRef] = Field(default_factory=list)
    output_schema: str | None = None   # registered schema key
    output_key: str | None = None
    temperature: float | None = None
    guard: GuardSpec | None = None

    @model_validator(mode="after")
    def _one_instruction(self) -> LlmStageSpec:
        if bool(self.instruction) == bool(self.instruction_text):
            raise ValueError(
                f"LlmStageSpec {self.name!r}: set exactly one of "
                "'instruction' (registry key) or 'instruction_text' (literal)."
            )
        return self


class GateStageSpec(BaseModel):
    kind: Literal["gate"] = "gate"
    name: str
    checks: str          # registered check-set key
    verdict_key: str
    failed_key: str


class FormGateStageSpec(BaseModel):
    kind: Literal["form_gate"] = "form_gate"
    name: str
    state_key: str
    is_resolved: str                 # predicate key
    should_prompt: str = "always"    # predicate key
    precondition: str | None = None  # predicate key
    pending_text: str
    pending_value: str = "PENDING"
    auto_value: str | None = None
    auto_text: str = ""
    resolved_text: str = ""
    skip_text: str = ""


class CustomStageSpec(BaseModel):
    kind: Literal["custom"] = "custom"
    name: str
    factory: str             # registered agent-factory key
    guard: GuardSpec | None = None


class SequentialStageSpec(BaseModel):
    """A nested ``SequentialAgent`` over sub-stages, optionally guarded as a unit.

    Used to skip a whole group at once — e.g. the meeting fan-out (compute +
    calendar + notes) is skipped together while the owner form is pending.
    """

    kind: Literal["sequential"] = "sequential"
    name: str
    sub_stages: list[StageSpec]
    guard: GuardSpec | None = None


class LoopStageSpec(BaseModel):
    kind: Literal["loop"] = "loop"
    name: str
    sub_stages: list[StageSpec]
    max_iterations: int
    exit_predicate: str      # predicate key — wired into a LoopExitChecker


StageSpec = Annotated[
    LlmStageSpec
    | GateStageSpec
    | FormGateStageSpec
    | CustomStageSpec
    | SequentialStageSpec
    | LoopStageSpec,
    Field(discriminator="kind"),
]


class EngineSpec(BaseModel):
    """A full engine pipeline: a named, ordered list of stages."""

    name: str
    stages: list[StageSpec]


# Resolve the forward reference in the nested-stage specs' sub_stages.
SequentialStageSpec.model_rebuild()
LoopStageSpec.model_rebuild()
