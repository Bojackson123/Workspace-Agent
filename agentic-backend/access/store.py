"""Database-backed workflow access rules.

Workflow definitions (prompt, toolsets, command ID) live in code and
change at deploy time. *Who may invoke each workflow* changes far more
often and is owned by ops, not engineering — so it lives in a SQL
table managed at runtime via the ``/grant`` / ``/revoke`` reserved
slash commands.

Each row is one principal (an email or a domain) that may invoke one
workflow. A workflow with no rows falls back to the
:class:`workflows.AccessMode` declared in code: ``OPEN`` means anyone
may use it, ``RESTRICTED`` means nobody may use it until rows are
added.

The store keeps a per-process cache of compiled
:class:`access.AccessPolicy` objects so the request path does not
hit the database. The cache is refreshed lazily; an admin's grant on
one Cloud Run instance becomes visible on peer instances after at
most ``ACCESS_CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Final

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from config import settings

log = logging.getLogger(__name__)


# Valid values for ``WorkflowAccessRule.rule_type``. Defined here (not
# as an Enum column) so adding a kind is a code-only change with no
# DB migration.
RULE_TYPE_EMAIL: Final = "email"
RULE_TYPE_DOMAIN: Final = "domain"
VALID_RULE_TYPES: Final[frozenset[str]] = frozenset(
    {RULE_TYPE_EMAIL, RULE_TYPE_DOMAIN}
)


class _Base(DeclarativeBase):
    """Private base class; only our tables hang off this metadata."""


class WorkflowAccessRule(_Base):
    """One principal allowed to invoke one workflow.

    Rule kinds map 1:1 onto :class:`access.AccessPolicy` fields:
    ``email`` populates ``allowed_emails``, ``domain`` populates
    ``allowed_domains``.
    """

    __tablename__ = "workflow_access_rules"
    __table_args__ = (
        UniqueConstraint(
            "command_id", "rule_type", "principal",
            name="uq_war_command_type_principal",
        ),
        Index("idx_war_command", "command_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    command_id = Column(Integer, nullable=False)
    rule_type = Column(String(16), nullable=False)
    principal = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    created_by = Column(Text, nullable=False)


@dataclass(frozen=True, slots=True)
class RuleRow:
    """A flat view of one rules-table row for display / CLI."""

    command_id: int
    rule_type: str
    principal: str
    created_by: str
    created_at: str  # ISO 8601, formatted for chat output


class AccessStore:
    """Per-process façade over the ``workflow_access_rules`` table.

    Shares an :class:`AsyncEngine` with the ADK session service so the
    backend opens exactly one connection pool per Cloud Run instance.
    Call :meth:`init_schema` once at startup to create the table.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        # command_id -> (policy, cache_expiry_epoch)
        self._cache: dict[int, tuple["AccessPolicy", float]] = {}
        self._schema_ready = False

    async def init_schema(self) -> None:
        """Create the table if it does not exist. Safe to call repeatedly."""
        if self._schema_ready:
            return
        async with self._engine.begin() as conn:
            await conn.run_sync(_Base.metadata.create_all)
        self._schema_ready = True

    # -- Read path ---------------------------------------------------------

    async def load(self, command_id: int) -> "AccessPolicy":
        """Return the cached :class:`AccessPolicy` for *command_id*.

        Reads from the DB on cache miss / expiry. An empty policy
        (``is_open == True``) means "no rules in the DB" — the caller
        is responsible for applying the workflow's :class:`AccessMode`
        default to decide allow vs. deny.
        """
        from access.policy import AccessPolicy  # local import: avoid module cycle

        now = time.time()
        cached = self._cache.get(command_id)
        if cached is not None and cached[1] > now:
            return cached[0]

        await self.init_schema()
        async with self._sessionmaker() as sql_session:
            stmt = select(WorkflowAccessRule.rule_type, WorkflowAccessRule.principal).where(
                WorkflowAccessRule.command_id == command_id
            )
            result = await sql_session.execute(stmt)
            rows = result.all()

        emails: set[str] = set()
        domains: set[str] = set()
        for rule_type, principal in rows:
            if rule_type == RULE_TYPE_EMAIL:
                emails.add(principal)
            elif rule_type == RULE_TYPE_DOMAIN:
                domains.add(principal)
            else:
                # Unknown rule_type — silently drop. We never want a
                # stray row to crash authorisation. Most commonly this
                # is a legacy ``group`` row left over from an earlier
                # schema.
                log.warning(
                    "Dropping rule with unknown type %r for command_id=%d",
                    rule_type,
                    command_id,
                )

        policy = AccessPolicy(
            allowed_emails=frozenset(emails),
            allowed_domains=frozenset(domains),
        )
        ttl = settings().access_cache_ttl_seconds
        self._cache[command_id] = (policy, now + ttl)
        return policy

    async def list_rules(self, command_id: int) -> list[RuleRow]:
        """Return rows for display via ``/list-access``."""
        await self.init_schema()
        async with self._sessionmaker() as sql_session:
            stmt = (
                select(
                    WorkflowAccessRule.command_id,
                    WorkflowAccessRule.rule_type,
                    WorkflowAccessRule.principal,
                    WorkflowAccessRule.created_by,
                    WorkflowAccessRule.created_at,
                )
                .where(WorkflowAccessRule.command_id == command_id)
                .order_by(
                    WorkflowAccessRule.rule_type,
                    WorkflowAccessRule.principal,
                )
            )
            result = await sql_session.execute(stmt)
            rows = result.all()
        return [
            RuleRow(
                command_id=row.command_id,
                rule_type=row.rule_type,
                principal=row.principal,
                created_by=row.created_by,
                created_at=row.created_at.isoformat() if row.created_at else "",
            )
            for row in rows
        ]

    # -- Write path --------------------------------------------------------

    async def grant(
        self,
        *,
        command_id: int,
        rule_type: str,
        principal: str,
        created_by: str,
    ) -> bool:
        """Insert a rule. Returns ``True`` if newly created, ``False`` on conflict.

        Idempotent: granting twice is a no-op (the unique constraint
        absorbs the second write).
        """
        _validate_rule_type(rule_type)
        principal = principal.strip()
        if not principal:
            raise ValueError("principal must be non-empty")

        await self.init_schema()
        async with self._sessionmaker() as sql_session:
            dialect = self._engine.dialect.name
            values = {
                "command_id": command_id,
                "rule_type": rule_type,
                "principal": principal,
                "created_by": created_by,
            }
            if dialect == "postgresql":
                stmt = pg_insert(WorkflowAccessRule).values(**values)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["command_id", "rule_type", "principal"]
                )
            elif dialect == "sqlite":
                stmt = sqlite_insert(WorkflowAccessRule).values(**values)
                stmt = stmt.on_conflict_do_nothing(
                    index_elements=["command_id", "rule_type", "principal"]
                )
            else:
                # MySQL/MariaDB or anything else — emulate with insert + ignore
                stmt = WorkflowAccessRule.__table__.insert().values(**values)
            try:
                result = await sql_session.execute(stmt)
                inserted = (result.rowcount or 0) > 0
                await sql_session.commit()
            except Exception:
                await sql_session.rollback()
                raise

        self._cache.pop(command_id, None)
        return inserted

    async def revoke(
        self,
        *,
        command_id: int,
        rule_type: str,
        principal: str,
    ) -> bool:
        """Delete a rule. Returns ``True`` if a row was actually removed."""
        _validate_rule_type(rule_type)
        await self.init_schema()
        async with self._sessionmaker() as sql_session:
            stmt = delete(WorkflowAccessRule).where(
                WorkflowAccessRule.command_id == command_id,
                WorkflowAccessRule.rule_type == rule_type,
                WorkflowAccessRule.principal == principal.strip(),
            )
            result = await sql_session.execute(stmt)
            await sql_session.commit()
        removed = (result.rowcount or 0) > 0
        if removed:
            self._cache.pop(command_id, None)
        return removed

    def invalidate(self, command_id: int | None = None) -> None:
        """Drop cached policies. ``None`` clears everything."""
        if command_id is None:
            self._cache.clear()
        else:
            self._cache.pop(command_id, None)


def _validate_rule_type(rule_type: str) -> None:
    if rule_type not in VALID_RULE_TYPES:
        raise ValueError(
            f"Unknown rule_type {rule_type!r}; expected one of "
            f"{sorted(VALID_RULE_TYPES)}"
        )
