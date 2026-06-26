"""Process-wide session and access-rule stores.

Both stores share a single SQLAlchemy engine: the ADK
``DatabaseSessionService`` owns its connection pool, and the access-rule
table is layered onto the same engine — see ``SessionStore.engine``.

Keeping the singletons in their own leaf module lets every part of the
``chat`` package import them without forming an import cycle.
"""

from __future__ import annotations

from access.store import AccessStore
from sessions import SessionStore

_session_store = SessionStore.from_settings()
_access_store = AccessStore(_session_store.engine)
