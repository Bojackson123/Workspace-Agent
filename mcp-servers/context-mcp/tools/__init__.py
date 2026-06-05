"""Tool groups exposed by the Context MCP.

Each submodule defines a flat set of read-only tool functions plus a
``register(mcp)`` entry point that binds them onto a ``FastMCP``
instance. ``register_all`` is a convenience helper for ``server.py``.
"""

from mcp.server.fastmcp import FastMCP

from . import calendar, docs, drive, gmail, gmail_write


def register_all(mcp: FastMCP) -> None:
    """Register every Context MCP tool onto *mcp*."""
    gmail.register(mcp)
    gmail_write.register(mcp)
    drive.register(mcp)
    docs.register(mcp)
    calendar.register(mcp)


__all__ = ["register_all", "gmail", "gmail_write", "drive", "docs", "calendar"]
