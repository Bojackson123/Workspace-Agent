"""Compile an :class:`~engine.spec.EngineSpec` into an ADK agent.

:func:`build_engine` is the per-request factory an engine's ``Workflow``
delegates to: it walks the spec's stages, resolves each one's registered
components, and assembles a fresh ``SequentialAgent``. A fresh build per
request keeps MCP transports short-lived, exactly as the hand-written
``_build`` factories did.
"""

from __future__ import annotations

from google.adk.agents import BaseAgent, LlmAgent, LoopAgent, SequentialAgent
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types

from clients.agent import action_toolset, context_toolset, gemini_model
from workflows.common.conditional import GuardAgent
from workflows.common.gate import GateAgent
from workflows.common.loop_exit import LoopExitChecker
from engine import registry
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
    StageSpec,
    ToolsetRef,
)


async def build_engine(spec: EngineSpec, user_email: str) -> SequentialAgent:
    """Build the ``SequentialAgent`` described by *spec* for *user_email*."""
    return SequentialAgent(
        name=spec.name,
        sub_agents=[_build_stage(stage, user_email) for stage in spec.stages],
    )


def _build_stage(stage: StageSpec, user_email: str) -> BaseAgent:
    if isinstance(stage, LlmStageSpec):
        return _maybe_guard(_build_llm(stage, user_email), stage.guard)
    if isinstance(stage, GateStageSpec):
        return _build_gate(stage)
    if isinstance(stage, FormGateStageSpec):
        return _build_form_gate(stage)
    if isinstance(stage, CustomStageSpec):
        factory = registry.get_agent_factory(stage.factory)
        return _maybe_guard(factory(user_email), stage.guard)
    if isinstance(stage, SequentialStageSpec):
        return _maybe_guard(_build_sequential(stage, user_email), stage.guard)
    if isinstance(stage, LoopStageSpec):
        return _build_loop(stage, user_email)
    raise TypeError(f"Unknown stage spec: {stage!r}")


def _build_sequential(stage: SequentialStageSpec, user_email: str) -> SequentialAgent:
    return SequentialAgent(
        name=stage.name,
        sub_agents=[_build_stage(s, user_email) for s in stage.sub_stages],
    )


def _build_llm(stage: LlmStageSpec, user_email: str) -> LlmAgent:
    instruction = (
        registry.get_instruction(stage.instruction)
        if stage.instruction is not None
        else stage.instruction_text
    )
    kwargs: dict = {
        "name": stage.name,
        "model": gemini_model(),
        "instruction": instruction,
        "tools": _tools(stage.toolsets, user_email),
    }
    if stage.output_schema is not None:
        kwargs["output_schema"] = registry.get_schema(stage.output_schema)
    if stage.output_key is not None:
        kwargs["output_key"] = stage.output_key
    if stage.temperature is not None:
        kwargs["generate_content_config"] = types.GenerateContentConfig(
            temperature=stage.temperature
        )
    return LlmAgent(**kwargs)


def _build_gate(stage: GateStageSpec) -> GateAgent:
    return GateAgent(
        name=stage.name,
        checks=registry.get_checks(stage.checks),
        verdict_key=stage.verdict_key,
        failed_key=stage.failed_key,
    )


def _build_form_gate(stage: FormGateStageSpec) -> FormGate:
    return FormGate(
        name=stage.name,
        state_key=stage.state_key,
        is_resolved=registry.get_predicate(stage.is_resolved),
        should_prompt=registry.get_predicate(stage.should_prompt),
        precondition=(
            registry.get_predicate(stage.precondition)
            if stage.precondition is not None
            else None
        ),
        pending_text=stage.pending_text,
        pending_value=stage.pending_value,
        auto_value=stage.auto_value,
        auto_text=stage.auto_text,
        resolved_text=stage.resolved_text,
        skip_text=stage.skip_text,
    )


def _build_loop(stage: LoopStageSpec, user_email: str) -> LoopAgent:
    sub_agents = [_build_stage(s, user_email) for s in stage.sub_stages]
    sub_agents.append(
        LoopExitChecker(
            name=f"{stage.name}_exit",
            exit_predicate=registry.get_predicate(stage.exit_predicate),
        )
    )
    return LoopAgent(
        name=stage.name,
        sub_agents=sub_agents,
        max_iterations=stage.max_iterations,
    )


def _maybe_guard(agent: BaseAgent, guard: GuardSpec | None) -> BaseAgent:
    if guard is None:
        return agent
    return GuardAgent(
        name=f"{agent.name}_guard",
        skip_when=registry.get_predicate(guard.skip_when),
        skip_text=guard.skip_text,
        sub_agent=agent,
        restore_key=guard.restore_key,
    )


def _tools(refs: list[ToolsetRef], user_email: str) -> list:
    tools: list = []
    for ref in refs:
        if ref is ToolsetRef.CONTEXT:
            tools.append(context_toolset(user_email))
        elif ref is ToolsetRef.ACTION:
            tools.append(action_toolset())
        elif ref is ToolsetRef.GOOGLE_SEARCH:
            tools.append(GoogleSearchTool(bypass_multi_tools_limit=True))
    return tools
