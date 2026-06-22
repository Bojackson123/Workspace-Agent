"""Direct Context MCP client for backend-driven (non-LLM) tool calls.

Most tool calls go through the ADK ``MCPToolset`` because an LLM is choosing
them. A few flows, though, need the backend itself to invoke a Context MCP tool
deterministically — with no LLM in the loop:

* the meeting pipeline's deterministic calendar-creation step, and
* the "invite people" dialog handler (resolve org users → emails, then patch
  the calendar events).

This module opens a short-lived Streamable HTTP MCP session for one tool call,
passing the calling user's email in the ``X-User-Email`` header exactly like
:func:`agent.context_toolset` — so the Context MCP impersonates the same user
via DWD. The session is per-call by design (mirrors the per-request transport
lifecycle the toolsets use) and never reused across users.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.exceptions import McpError
from mcp.types import TextContent

from config import settings

log = logging.getLogger(__name__)

T = TypeVar("T")

# The Context MCP binds identity from this header (see context-mcp/identity.py).
# agent.context_toolset sends the same header; HTTP header names are
# case-insensitive so the exact casing here does not matter.
_USER_EMAIL_HEADER = "X-User-Email"

# Matches agent.context_toolset's per-operation timeout — cold Cloud Run MCP
# instances plus a Workspace API round trip can exceed the SDK's 5s default.
_MCP_OPERATION_TIMEOUT_S: float = 30.0

# Heavy, deterministic writes (e.g. fill_rfi_answers downloads a workbook, writes
# 60+ answers, and re-uploads) can legitimately take far longer than a read. Give
# them a wider ceiling so a slow-but-healthy call isn't mistaken for a dropped
# session — callers pair this with retry_on_timeout=False (see call_action_tool).
# Public: the RFI assembler imports it as the contract for fill_rfi_answers.
MCP_WRITE_TIMEOUT_S: float = 120.0

# Transient-transport retry. A Streamable HTTP MCP session can drop mid-call when
# the Cloud Run MCP instance is recycled/autoscaled ("Session terminated",
# truncated response, TLS reset). Re-running the operation opens a fresh
# connection to a (usually healthy) instance. Delays grow 0.5s, 1s → ~1.5s max.
_MCP_MAX_ATTEMPTS = 3
_MCP_BASE_DELAY_S = 0.5


def _is_transient_transport_error(
    exc: BaseException, retry_on_timeout: bool = True
) -> bool:
    """True if *exc* looks like a transient MCP transport failure worth retrying.

    Recurses into ``BaseExceptionGroup`` because the MCP client wraps transport
    failures in anyio task-group exception groups (often nested). Deliberately
    excludes the ``RuntimeError`` we raise for tool-level errors — those are
    deterministic and must not be retried.

    When *retry_on_timeout* is False, pure client-side timeouts are treated as
    non-transient. A heavy non-idempotent write that times out client-side may
    still be running server-side; reconnecting would run it a second time
    concurrently. Callers of such writes pass False and rely on a wider timeout
    (plus server-side idempotency) instead. Unambiguous drops — a "Session
    terminated" McpError, connection/SSL/OS errors — are still retried.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(
            _is_transient_transport_error(e, retry_on_timeout)
            for e in exc.exceptions
        )
    if isinstance(exc, McpError):
        msg = str(exc).lower()
        if "terminat" in msg:
            return True
        if "timeout" in msg or "timed out" in msg:
            return retry_on_timeout
        return False
    # TimeoutError is an OSError subclass, so gate it before the catch-all below.
    if isinstance(exc, TimeoutError):
        return retry_on_timeout
    return isinstance(exc, (ConnectionError, ssl.SSLError, OSError))


async def _with_transport_retry(
    op: Callable[[], Awaitable[T]], label: str, *, retry_on_timeout: bool = True
) -> T:
    """Run *op*, retrying transient transport failures with backoff.

    Non-transient errors (including tool-level ``RuntimeError``) and the final
    attempt re-raise immediately. ``retry_on_timeout=False`` additionally treats
    client-side timeouts as non-transient (see ``_is_transient_transport_error``).
    """
    last_exc: BaseException | None = None
    for i in range(_MCP_MAX_ATTEMPTS):
        try:
            return await op()
        except Exception as exc:  # noqa: BLE001 — re-raised below if not transient
            if (
                not _is_transient_transport_error(exc, retry_on_timeout)
                or i == _MCP_MAX_ATTEMPTS - 1
            ):
                raise
            last_exc = exc
            delay = _MCP_BASE_DELAY_S * (2 ** i)
            log.warning(
                "Transient MCP transport error on %s (%s); retry %d/%d in %.1fs",
                label, type(exc).__name__, i + 1, _MCP_MAX_ATTEMPTS - 1, delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None  # unreachable: loop either returns or raises
    raise last_exc


def _result_text(content: list[Any]) -> str:
    """Join the text parts of a CallToolResult's content into one string."""
    parts = [c.text for c in content if isinstance(c, TextContent)]
    return "".join(parts)


async def call_context_tool(
    user_email: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> str:
    """Call a single Context MCP tool as *user_email* and return its text result.

    Transient transport failures (dropped session, TLS reset, timeout) are
    retried on a fresh connection; after the final attempt — or on a tool-level
    error — it raises so callers can surface a clear message. Tool-level "soft"
    errors (returned as text) come back as the string for the caller to inspect.
    """
    url = f"{settings().context_mcp_url}/mcp"
    headers = {_USER_EMAIL_HEADER: user_email}

    async def _op() -> str:
        async with streamablehttp_client(
            url, headers=headers, timeout=_MCP_OPERATION_TIMEOUT_S
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})
                text = _result_text(result.content)
                if result.isError:
                    log.error("Context tool %s errored: %s", tool_name, text)
                    raise RuntimeError(f"Context tool {tool_name} failed: {text}")
                return text

    return await _with_transport_retry(_op, f"context:{tool_name}")


async def call_action_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    timeout: float = _MCP_OPERATION_TIMEOUT_S,
    retry_on_timeout: bool = True,
) -> str:
    """Call a single Action MCP tool and return its text result.

    The Action MCP authenticates as a service account, so — unlike
    :func:`call_context_tool` — no ``X-User-Email`` header is sent. Used by the
    RFI engine's deterministic steps (upload the attachment, extract questions,
    fill answers) that must run without an LLM in the loop.

    Transient transport failures (dropped session, TLS reset) are retried on a
    fresh connection; after the final attempt — or on a tool-level error — it
    raises. ``timeout`` is the per-operation ceiling and ``retry_on_timeout``
    controls whether a client-side timeout counts as transient.

    Heavy non-idempotent writes (``fill_rfi_answers``) should pass a wider
    ``timeout`` and ``retry_on_timeout=False`` so a slow-but-healthy call isn't
    retried into a second concurrent server-side write. ``fill_rfi_answers`` is
    also idempotent server-side (it upserts the deterministically-named response
    file), so even an unambiguous-drop retry converges onto one Drive file rather
    than duplicating.
    """
    url = f"{settings().action_mcp_url}/mcp"

    async def _op() -> str:
        async with streamablehttp_client(
            url, timeout=timeout
        ) as (read, write, _get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments or {})
                text = _result_text(result.content)
                if result.isError:
                    log.error("Action tool %s errored: %s", tool_name, text)
                    raise RuntimeError(f"Action tool {tool_name} failed: {text}")
                return text

    return await _with_transport_retry(
        _op, f"action:{tool_name}", retry_on_timeout=retry_on_timeout
    )
