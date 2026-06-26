"""The composable engine framework.

A workflow "engine" is declared as an :class:`EngineSpec` — an ordered list of
typed stage specs (LLM, gate, form-gate, loop, custom) — and built per request
by :func:`build_engine`. The code-y parts each stage needs (output schemas,
gate checks, state predicates, instruction providers, bespoke agents) are
registered by string key in :mod:`engine.registry` and referenced
from the spec.

To add a workflow: write its spec + register any new components, then wrap it
in a ``Workflow`` whose ``build_agent`` calls :func:`build_engine`. See
``workflows/rfi_engine/agents.py`` for the worked example.
"""

from __future__ import annotations

from engine import registry
from engine.assembler import (
    AssemblyAbort,
    AssemblyResult,
    IdempotentAssembler,
)
from engine.compiler import build_engine
from engine.form_gate import FormGate
from engine.spec import (
    CustomStageSpec,
    EngineSpec,
    FormGateStageSpec,
    GateStageSpec,
    GuardSpec,
    LlmStageSpec,
    LoopStageSpec,
    SequentialStageSpec,
    ToolsetRef,
)

__all__ = [
    "AssemblyAbort",
    "AssemblyResult",
    "CustomStageSpec",
    "EngineSpec",
    "FormGate",
    "FormGateStageSpec",
    "GateStageSpec",
    "GuardSpec",
    "IdempotentAssembler",
    "LlmStageSpec",
    "LoopStageSpec",
    "SequentialStageSpec",
    "ToolsetRef",
    "build_engine",
    "registry",
]
