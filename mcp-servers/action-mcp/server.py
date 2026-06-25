"""Action MCP server entry point.

Builds the FastMCP application, registers every tool group, and exposes
the ASGI app that ``uvicorn server:app`` runs.
"""

from dotenv import load_dotenv

# Load environment variables before any module reads from them.
load_dotenv()

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.server.fastmcp.server import TransportSecuritySettings  # noqa: E402

from config import settings  # noqa: E402
from tools import register_all  # noqa: E402


def _build_mcp() -> FastMCP:
    """Construct the FastMCP server with transport security and all tools wired."""
    cfg = settings()
    # Stateless streamable HTTP: each request is self-contained, so no MCP
    # session id is minted and no per-instance session registry is consulted.
    # This is required on Cloud Run — instances are load-balanced and recycled,
    # so a stateful session created on one instance is unknown to the next and
    # follow-up requests get 404 "Session not found". json_response returns each
    # result as a single JSON POST response (no long-lived GET SSE stream). The
    # tools are plain request/response (no notifications/progress/sampling), so
    # statelessness loses nothing.
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

# ASGI application consumed by uvicorn.
app = mcp.streamable_http_app()
