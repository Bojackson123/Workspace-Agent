"""Component registry for the declarative engine.

An :class:`~engine.spec.EngineSpec` is data: it names its stages and
prompts inline, but the genuinely-code parts — output schemas, gate-check
sets, state predicates, instruction providers, and bespoke ``BaseAgent``
factories — can't live in JSON. They live here instead, keyed by string, and
the spec references them by key.

Each engine registers its components at import time (usually right next to its
spec), then the compiler resolves them when it builds the agent. Adding a new
behaviour to a workflow means writing one small component, registering it under
a key, and referencing that key — reusable from then on.

Registries:
    instructions   key -> ``str`` or ``(ReadonlyContext) -> str``  (LLM prompt)
    predicates     key -> ``(state: dict) -> bool``
    check_sets     key -> ``list[(state) -> GateCheck]``           (gate checks)
    schemas        key -> ``type[BaseModel]``                      (output_schema)
    agents         key -> ``(user_email: str) -> BaseAgent``       (custom stage)
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from google.adk.agents import BaseAgent
    from pydantic import BaseModel

    from workflows.common.gate import GateCheck

_T = TypeVar("_T", bound=Callable)

# Instruction = literal prompt text or a provider callable ADK accepts as an
# LlmAgent ``instruction`` (it may read ReadonlyContext to inject state).
Instruction = Any
StatePredicate = Callable[[dict], bool]
CheckSet = "list[Callable[[dict], GateCheck]]"
AgentFactory = "Callable[[str], BaseAgent]"

_INSTRUCTIONS: dict[str, Any] = {}
_PREDICATES: dict[str, StatePredicate] = {}
_CHECK_SETS: dict[str, Any] = {}
_SCHEMAS: dict[str, Any] = {}
_AGENTS: dict[str, Any] = {}


# ── Registration ────────────────────────────────────────────────────────────


def instruction(key: str) -> Callable[[_T], _T]:
    """Register an instruction provider ``(ReadonlyContext) -> str`` under *key*."""

    def deco(fn: _T) -> _T:
        _INSTRUCTIONS[key] = fn
        return fn

    return deco


def register_instruction(key: str, value: Any) -> None:
    """Register a literal instruction string (or provider) under *key*."""
    _INSTRUCTIONS[key] = value


def predicate(key: str) -> Callable[[_T], _T]:
    """Register a state predicate ``(state) -> bool`` under *key*."""

    def deco(fn: _T) -> _T:
        _PREDICATES[key] = fn
        return fn

    return deco


def agent_factory(key: str) -> Callable[[_T], _T]:
    """Register a custom-agent factory ``(user_email) -> BaseAgent`` under *key*."""

    def deco(fn: _T) -> _T:
        _AGENTS[key] = fn
        return fn

    return deco


def register_checks(key: str, checks: Any) -> None:
    """Register a gate ``list[(state) -> GateCheck]`` under *key*."""
    _CHECK_SETS[key] = checks


def register_schema(key: str, model: type[BaseModel]) -> None:
    """Register a Pydantic ``output_schema`` class under *key*."""
    _SCHEMAS[key] = model


# ── Resolution ──────────────────────────────────────────────────────────────


def _resolve(table: dict[str, Any], key: str, kind: str) -> Any:
    try:
        return table[key]
    except KeyError:
        raise KeyError(
            f"No {kind} registered under {key!r}. Register it with the matching "
            f"engine.registry helper before building the spec."
        ) from None


def get_instruction(key: str) -> Any:
    return _resolve(_INSTRUCTIONS, key, "instruction")


def get_predicate(key: str) -> StatePredicate:
    return _resolve(_PREDICATES, key, "predicate")


def get_checks(key: str) -> Any:
    return _resolve(_CHECK_SETS, key, "check set")


def get_schema(key: str) -> Any:
    return _resolve(_SCHEMAS, key, "schema")


def get_agent_factory(key: str) -> Any:
    return _resolve(_AGENTS, key, "agent factory")


# ── Framework built-ins ──────────────────────────────────────────────────────


@predicate("always")
def _always(_state: dict) -> bool:
    """Always-true predicate — e.g. a FormGate that prompts whenever active."""
    return True
