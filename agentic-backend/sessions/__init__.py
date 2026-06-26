"""Persistent multi-turn session store backed by ADK's ``DatabaseSessionService``.

Google Chat marks a message as a slash command only on the *first* turn —
follow-up replies in the same thread are plain ``MESSAGE`` events with
no ``slashCommand`` field. To support multi-turn workflows we therefore
have to remember, on the backend, which workflow is currently active in
each Chat thread.

This module keys ADK sessions by ``(user_email, thread.name)`` and
stores the active workflow's ``command_id`` in :pyattr:`Session.state`.
The store also enforces an inactivity TTL so abandoned conversations
naturally fall back to the default agent on next message.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Final

from google.adk.sessions import BaseSessionService, DatabaseSessionService, Session
from sqlalchemy.ext.asyncio import AsyncEngine

from config import settings

log = logging.getLogger(__name__)

# Session.state keys. Centralised so a typo can't silently desync writers
# and readers.
STATE_ACTIVE_WORKFLOW_ID: Final = "active_workflow_id"


def _session_id_for(thread_name: str) -> str:
    """Derive a stable, storage-safe session id from a Chat thread name.

    Chat thread names look like ``spaces/AAA/threads/BBB``; the slashes
    are awkward in some storage backends and the full string is longer
    than necessary. A truncated SHA-256 is collision-resistant in
    practice for our scale and keeps the key opaque.
    """
    return hashlib.sha256(thread_name.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class ResolvedSession:
    """The session a single inbound webhook should run against.

    Attributes:
        session: The ADK :class:`Session` to hand to ``Runner.run_async``.
        active_workflow_id: The workflow this session is currently
            scoped to. ``None`` means "use the default workflow."
        is_new: ``True`` if the session was created on this request.
            Useful for logging "started new conversation" lines.
    """

    session: Session
    active_workflow_id: int | None
    is_new: bool


class SessionStore:
    """Thin wrapper over ADK's session service for thread-keyed lookups.

    All ADK session APIs are exposed via ``service`` for callers that
    need to drive the Runner directly; the higher-level helpers below
    encapsulate the chat-specific semantics (TTL, thread-keying,
    workflow switching).
    """

    def __init__(self, service: BaseSessionService) -> None:
        self.service = service

    @classmethod
    def from_settings(cls) -> "SessionStore":
        """Build a store backed by the configured database URL."""
        cfg = settings()
        return cls(DatabaseSessionService(db_url=cfg.session_db_url))

    @property
    def engine(self) -> AsyncEngine:
        """The SQLAlchemy engine ADK opened against the session database.

        Surfaced so :class:`access_store.AccessStore` can reuse the same
        connection pool — opening a second pool against the same
        Cloud SQL instance would waste connections and complicate
        capacity planning.
        """
        # ``DatabaseSessionService`` exposes ``db_engine`` as part of its
        # construction; reaching across the abstraction boundary here is
        # deliberate and documented (see also the type assertion).
        engine = getattr(self.service, "db_engine", None)
        if not isinstance(engine, AsyncEngine):
            raise RuntimeError(
                "SessionStore.engine requires DatabaseSessionService; "
                f"got {type(self.service).__name__}"
            )
        return engine

    async def resolve(
        self,
        *,
        user_email: str,
        thread_name: str,
        new_workflow_id: int | None,
    ) -> ResolvedSession:
        """Resolve the session for an inbound message.

        Semantics:

        * ``new_workflow_id`` is set (the user typed a slash command):
          any existing session for this thread is deleted and a fresh
          one is created scoped to that workflow. This guarantees that
          starting a new workflow does not inherit the prior workflow's
          conversation history or system prompt context.
        * ``new_workflow_id`` is ``None`` (free-form continuation):
          we look up the existing session. If it has expired (idle
          longer than ``settings().session_ttl_seconds``) we delete it
          and report no active workflow, so the caller falls back to
          the default agent.
        """
        cfg = settings()
        session_id = _session_id_for(thread_name)
        existing = await self.service.get_session(
            app_name=cfg.app_name,
            user_id=user_email,
            session_id=session_id,
        )

        if new_workflow_id is not None:
            # Explicit workflow start/switch: drop the old session
            # entirely so the LLM starts with a clean slate under the
            # new system prompt.
            if existing is not None:
                await self.service.delete_session(
                    cfg.app_name, user_email, session_id
                )
            session = await self.service.create_session(
                app_name=cfg.app_name,
                user_id=user_email,
                session_id=session_id,
                state={STATE_ACTIVE_WORKFLOW_ID: new_workflow_id},
            )
            return ResolvedSession(
                session=session,
                active_workflow_id=new_workflow_id,
                is_new=True,
            )

        # Free-form continuation. Honour the existing session if it is
        # still within its TTL; otherwise expire it.
        if existing is not None and not _is_expired(existing, cfg.session_ttl_seconds):
            return ResolvedSession(
                session=existing,
                active_workflow_id=existing.state.get(STATE_ACTIVE_WORKFLOW_ID),
                is_new=False,
            )

        if existing is not None:
            log.info(
                "Expiring idle session for %s in thread %s",
                user_email,
                thread_name,
            )
            await self.service.delete_session(
                cfg.app_name, user_email, session_id
            )

        # No persistent session: build a one-shot for this request.
        # We still create a row so the Runner has somewhere to write
        # its events; it will simply not be reused next turn.
        session = await self.service.create_session(
            app_name=cfg.app_name,
            user_id=user_email,
            session_id=session_id,
        )
        return ResolvedSession(
            session=session, active_workflow_id=None, is_new=True
        )

    async def clear(self, *, user_email: str, thread_name: str) -> bool:
        """Delete the session for a thread. Returns ``True`` if one existed."""
        cfg = settings()
        session_id = _session_id_for(thread_name)
        existing = await self.service.get_session(
            app_name=cfg.app_name,
            user_id=user_email,
            session_id=session_id,
        )
        if existing is None:
            return False
        await self.service.delete_session(cfg.app_name, user_email, session_id)
        return True


def _is_expired(session: Session, ttl_seconds: int) -> bool:
    """Whether a session's last activity is older than the TTL.

    A freshly-created session (no events yet) has
    ``last_update_time == 0`` — we treat that as "just now" rather than
    "expired forever ago" to avoid immediately discarding sessions we
    just made.
    """
    if session.last_update_time <= 0:
        return False
    return (time.time() - session.last_update_time) > ttl_seconds
