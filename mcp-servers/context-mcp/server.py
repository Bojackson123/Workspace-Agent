"""Context MCP server entry point.

Builds the FastMCP application, registers all read-only tools, attaches
the per-request user-identity middleware, and exposes the ASGI app that
``uvicorn server:app`` runs.
"""

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings

from config import settings
from identity import UserEmailMiddleware
from tools import register_all


def _build_mcp() -> FastMCP:
    """Construct the FastMCP server with transport security and all tools wired."""
    cfg = settings()
    mcp = FastMCP(
        cfg.server_name,
        transport_security=TransportSecuritySettings(
            allowed_hosts=list(cfg.allowed_hosts),
        ),
    )
    register_all(mcp)
    return mcp


mcp = _build_mcp()

# ASGI application consumed by uvicorn. The middleware must be attached
# AFTER the streamable_http_app is constructed so that it wraps every
# inbound MCP request and binds the user identity before any tool runs.
app = mcp.streamable_http_app()
app.add_middleware(UserEmailMiddleware)
