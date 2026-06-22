"""Coerce loosely-typed session-state values into Pydantic models.

ADK stores ``output_key`` results as whatever the LLM emitted — a dict, a
JSON string, or (after a Python agent wrote it) a model dump. These helpers
normalise that into typed models, tolerating partial/garbled entries rather
than raising, so a single malformed item can't sink a whole pipeline stage.
"""

from __future__ import annotations

import json
from typing import TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def coerce_model_list(
    raw: object,
    model_cls: type[ModelT],
    *,
    wrapper_key: str | None = None,
) -> list[ModelT]:
    """Parse *raw* into a list of *model_cls*, skipping unparseable entries.

    *raw* may be ``None``/empty (→ ``[]``), a JSON string, a ``{wrapper_key:
    [...]}`` dict, or an already-decoded list. Items that fail validation are
    dropped (tolerating partial LLM output).
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if wrapper_key and isinstance(raw, dict):
        raw = raw.get(wrapper_key, [])
    out: list[ModelT] = []
    for item in raw or []:
        try:
            out.append(model_cls.model_validate(item))
        except Exception:  # noqa: BLE001 — tolerate partial/garbled entries
            continue
    return out
