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
    # Stateless streamable HTTP: each request is self-contained, so no MCP
    # session id is minted and no per-instance session registry is consulted.
    # This is required on Cloud Run — instances are load-balanced and recycled,
    # so a stateful session created on one instance is unknown to the next and
    # follow-up requests get 404 "Session not found". json_response returns each
    # result as a single JSON POST response (no long-lived GET SSE stream).
    #
    # Identity is preserved: UserEmailMiddleware (pure ASGI) binds X-User-Email
    # into a contextvar in the request's own task, and stateless mode spawns the
    # per-request server task from that same task — so each tool call sees the
    # identity of the request that carried it (see identity.py).
    mcp = FastMCP(
        cfg.server_name,
        stateless_http=True,
        json_response=True,
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
