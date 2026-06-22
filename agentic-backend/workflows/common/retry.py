"""Shared backoff + jitter retry for workflow LLM calls.

Vertex/Gemini calls on this project routinely bounce off the edge of a tight
DSQ quota pool and come back ``429 RESOURCE_EXHAUSTED``. Those are transient —
the per-minute window refills and a later attempt succeeds.

The primary defence is HTTP-level retry configured on the Gemini model itself
(see ``agent.gemini_model``), which transparently retries *every* model call,
including the nested ones the google_search grounding tool makes. This module
provides the secondary, application-level retry for code that drives an agent
in its own isolated ``Runner`` and wants to layer extra handling on top — namely
/rfi's parallel research batches, which fall back to flagging a batch's
questions for human gap-fill once retries are exhausted.

:func:`retry_async` wraps any awaitable factory: it catches the 429, waits a
little longer each time, and adds random jitter so concurrent callers don't all
retry in lockstep and re-collide on the next quota window.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


def is_resource_exhausted(exc: BaseException) -> bool:
    """True if *exc* (or anything in its cause chain) is a Vertex 429.

    ADK wraps the google-genai ``ClientError`` in a private
    ``_ResourceExhaustedError``, so rather than import a private type we walk
    the ``__cause__`` / ``__context__`` chain and match the marker both layers
    carry in their message — robust across the wrapping. Guards against cyclic
    chains with a seen-set.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur)
        if "RESOURCE_EXHAUSTED" in text or "429" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _backoff_delay(base_delay: float, attempt: int, jitter: float) -> float:
    """Exponential delay for *attempt* (0-based) plus up to *jitter* fraction."""
    delay = base_delay * (2 ** attempt)
    return delay + random.uniform(0, delay * jitter)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    attempts: int = 4,
    base_delay: float = 4.0,
    jitter: float = 0.25,
    should_retry: Callable[[BaseException], bool] = is_resource_exhausted,
    label: str = "llm call",
) -> T:
    """Await ``fn()`` and retry transient failures with backoff + jitter.

    Retries when ``should_retry(exc)`` is true (default: Vertex 429s). Re-raises
    immediately on any other error and after the final attempt. Delays grow as
    ``base_delay * 2**i`` with up to ``jitter`` extra (4s, 8s, 16s by default →
    ~28s max wait), giving a per-minute quota window time to refill while
    de-syncing concurrent callers.

    ``fn`` is a zero-arg factory returning a fresh awaitable so each attempt is
    a clean retry, not a re-await of a spent coroutine.
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 — predicate decides retryability
            last = attempt == attempts - 1
            if last or not should_retry(exc):
                raise
            delay = _backoff_delay(base_delay, attempt, jitter)
            log.warning(
                "%s: transient error (%s); retry %d/%d in %.1fs",
                label, type(exc).__name__, attempt + 1, attempts - 1, delay,
            )
            await asyncio.sleep(delay)
    # Unreachable: the loop either returns or raises on the final attempt.
    raise AssertionError("retry_async exhausted without returning or raising")
