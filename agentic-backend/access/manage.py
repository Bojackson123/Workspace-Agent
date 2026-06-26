"""Break-glass CLI for the ``workflow_access_rules`` table.

The primary way to manage access rules is via the ``/grant`` /
``/revoke`` / ``/list-access`` slash commands. This module exists for
the cases the chat path can't handle:

* The bootstrap admin set is misconfigured and nobody can call
  ``/grant``.
* The table has been corrupted and needs to be inspected / nuked.
* You want to seed rules during automated provisioning before any
  user is online.

Usage:

    python manage_access.py grant <command> email:<addr>
    python manage_access.py grant <command> domain:<domain>
    python manage_access.py revoke <command> <type>:<principal>
    python manage_access.py list <command>
    python manage_access.py list-all

``<command>`` is either the slash-command text (``/draft``) or its
numeric ID (``2``). ``--created-by`` defaults to ``cli`` so audit rows
make clear the change did not come from a chat user.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from access.store import AccessStore, VALID_RULE_TYPES
from sessions import SessionStore
from workflows import WORKFLOWS, Workflow, get_workflow, get_workflow_by_name


def _resolve_command(arg: str) -> Workflow:
    workflow = get_workflow(int(arg)) if arg.isdigit() else get_workflow_by_name(arg)
    if workflow is None:
        sys.exit(f"error: unknown command {arg!r}")
    return workflow


def _parse_rule(arg: str) -> tuple[str, str]:
    if ":" not in arg:
        sys.exit(f"error: expected <type>:<principal>, got {arg!r}")
    rule_type, _, principal = arg.partition(":")
    rule_type = rule_type.strip().lower()
    principal = principal.strip()
    if rule_type not in VALID_RULE_TYPES:
        sys.exit(
            f"error: unknown rule type {rule_type!r}; "
            f"expected one of {sorted(VALID_RULE_TYPES)}"
        )
    if not principal:
        sys.exit("error: principal must be non-empty")
    return rule_type, principal


async def _run(args: argparse.Namespace) -> None:
    store = AccessStore(SessionStore.from_settings().engine)

    if args.subcommand == "grant":
        workflow = _resolve_command(args.command)
        rule_type, principal = _parse_rule(args.rule)
        inserted = await store.grant(
            command_id=workflow.command_id,
            rule_type=rule_type,
            principal=principal,
            created_by=args.created_by,
        )
        print(
            f"{'granted' if inserted else 'already-present'}: "
            f"{workflow.command_name} -> {rule_type}:{principal}"
        )

    elif args.subcommand == "revoke":
        workflow = _resolve_command(args.command)
        rule_type, principal = _parse_rule(args.rule)
        removed = await store.revoke(
            command_id=workflow.command_id,
            rule_type=rule_type,
            principal=principal,
        )
        print(
            f"{'revoked' if removed else 'no-match'}: "
            f"{workflow.command_name} -> {rule_type}:{principal}"
        )

    elif args.subcommand == "list":
        workflow = _resolve_command(args.command)
        rules = await store.list_rules(workflow.command_id)
        if not rules:
            print(
                f"{workflow.command_name}: no rules "
                f"(default-access: {workflow.default_access.value})"
            )
            return
        print(f"{workflow.command_name} (default-access: "
              f"{workflow.default_access.value}):")
        for row in rules:
            print(
                f"  {row.rule_type}:{row.principal}  "
                f"by {row.created_by} at {row.created_at}"
            )

    elif args.subcommand == "list-all":
        for workflow in WORKFLOWS.values():
            rules = await store.list_rules(workflow.command_id)
            count = len(rules)
            print(
                f"{workflow.command_name:>20}  "
                f"default={workflow.default_access.value:<10}  "
                f"rules={count}"
            )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--created-by",
        default="cli",
        help="Audit identity recorded on grant. Defaults to 'cli'.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_grant = subparsers.add_parser("grant", help="Add a rule.")
    p_grant.add_argument("command")
    p_grant.add_argument("rule", help="<type>:<principal>")

    p_revoke = subparsers.add_parser("revoke", help="Remove a rule.")
    p_revoke.add_argument("command")
    p_revoke.add_argument("rule", help="<type>:<principal>")

    p_list = subparsers.add_parser("list", help="Show rules for one command.")
    p_list.add_argument("command")

    subparsers.add_parser("list-all", help="Summarise every command.")

    args = parser.parse_args(argv)
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
