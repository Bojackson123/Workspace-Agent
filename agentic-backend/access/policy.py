"""Backend authorization for workflow invocations.

Slash commands cannot be hidden from Chat's autocomplete on a per-user
basis (the platform has no per-command audience controls), so every
restricted workflow is enforced here, against the verified
``user.email`` from the Chat OIDC token.

Rules live in the ``workflow_access_rules`` table (see
:mod:`access_store`) and are evaluated as an OR — the caller is
allowed if they match any rule. Two rule kinds are supported:

* email — explicit per-user allowlist
* domain — match on the email's domain (e.g. "internal users only")

Workspace **group** rules are deliberately not supported. Checking
group membership requires a Workspace API call (Admin SDK Directory or
Cloud Identity), which in turn needs a Workspace-authorized
credential (DWD or a Cloud Identity IAM role). If you need
group-style ACLs, model them as a domain rule (everyone in a domain)
or grant the individuals explicitly. The complexity of group lookups
is opt-in for a future revision.

When the rules table has *no* rows for a workflow, the workflow's
:class:`workflows.AccessMode` decides what to do:

* ``OPEN`` — anyone may invoke (suits ``/research``).
* ``RESTRICTED`` — nobody may invoke until rules are granted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from config import settings
from workflows import AccessMode

if TYPE_CHECKING:
    from access.store import AccessStore

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """A compiled view of the rule rows for one workflow.

    Built by :meth:`access_store.AccessStore.load` from the
    ``workflow_access_rules`` table. An empty policy (both sets empty)
    means "no rules in the DB"; the caller decides what to do based on
    the workflow's :class:`AccessMode`.
    """

    allowed_emails: frozenset[str] = field(default_factory=frozenset)
    allowed_domains: frozenset[str] = field(default_factory=frozenset)

    @property
    def is_open(self) -> bool:
        """``True`` if the policy has no rules (table was empty)."""
        return not (self.allowed_emails or self.allowed_domains)


@dataclass(frozen=True, slots=True)
class AccessDecision:
    """The outcome of an authorization check.

    Attributes:
        allowed: Whether the caller may invoke the workflow.
        reason: Short human-readable explanation. Safe to surface in
            chat for denials; useful in logs for allows.
    """

    allowed: bool
    reason: str


async def authorize(
    user_email: str,
    command_id: int,
    default_mode: AccessMode,
    store: "AccessStore",
) -> AccessDecision:
    """Decide whether *user_email* may invoke the workflow with *command_id*.

    The decision matrix:

    +-----------------+-----------------+------------------------------+
    | default_mode    | rules in table  | outcome                      |
    +=================+=================+==============================+
    | OPEN            | none            | allow                        |
    | OPEN            | one or more     | evaluate rules               |
    | RESTRICTED      | none            | deny                         |
    | RESTRICTED      | one or more     | evaluate rules               |
    +-----------------+-----------------+------------------------------+

    So DB rules are always authoritative when present; ``default_mode``
    only chooses the empty-table behaviour.
    """
    policy = await store.load(command_id)

    if policy.is_open:
        if default_mode == AccessMode.OPEN:
            return AccessDecision(True, "open by default")
        return AccessDecision(False, "restricted; no rules configured")

    if user_email in policy.allowed_emails:
        return AccessDecision(True, "email allowlist")

    domain = user_email.rsplit("@", 1)[-1].lower()
    if domain in {d.lower() for d in policy.allowed_domains}:
        return AccessDecision(True, f"domain allowlist ({domain})")

    return AccessDecision(False, _deny_reason(policy))


def authorize_bootstrap_admin(user_email: str) -> bool:
    """Whether *user_email* is allowed to call admin slash commands.

    Bootstrap admins are governed by :envvar:`BOOTSTRAP_ADMIN_EMAILS`
    (comma-separated), **not** by the rules table — losing the table
    cannot lock admins out of the very commands they need to rebuild
    it.
    """
    return user_email in settings().bootstrap_admin_emails


def _deny_reason(policy: AccessPolicy) -> str:
    """Build a denial message that tells the user how to get access."""
    parts: list[str] = []
    if policy.allowed_domains:
        parts.append("users in " + ", ".join(sorted(policy.allowed_domains)))
    if policy.allowed_emails:
        parts.append(f"{len(policy.allowed_emails)} specific user(s)")
    return "restricted to " + "; ".join(parts) if parts else "access denied"
