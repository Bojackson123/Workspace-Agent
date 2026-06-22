"""Tool groups exposed by the Action MCP.

Each submodule defines a flat set of tool functions plus a ``register(mcp)``
entry point that binds them onto a ``FastMCP`` instance. ``register_all``
is a convenience helper used by ``server.py``.
"""

from mcp.server.fastmcp import FastMCP

from . import docs, drive, rfi_files, sheets


def register_all(mcp: FastMCP) -> None:
    """Register every Action MCP tool onto *mcp*."""
    drive.register(mcp)
    docs.register(mcp)
    sheets.register(mcp)
    rfi_files.register(mcp)


__all__ = ["register_all", "docs", "drive", "rfi_files", "sheets"]
