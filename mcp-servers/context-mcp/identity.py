"""Per-request user identity for the Context MCP.

The agent backend passes the calling user's email to this server via an
``X-User-Email`` HTTP header. A pure ASGI middleware copies that header
into a contextvar so individual tools can pick up the identity without
taking it as an explicit argument. See :class:`UserEmailMiddleware` for
why this is implemented as a pure ASGI middleware and not a Starlette
``BaseHTTPMiddleware`` subclass.

The header-based propagation is a deliberate security choice — the LLM
never sees or passes the user email itself, so it cannot be tricked into
impersonating someone else.
"""

from contextvars import ContextVar

USER_EMAIL_HEADER = "x-user-email"
_USER_EMAIL_HEADER_BYTES = USER_EMAIL_HEADER.encode("latin-1")

# Defaulted to ``None`` so a missing header is detectable as a clear error
# (rather than silently impersonating no-one).
_user_email: ContextVar[str | None] = ContextVar("user_email", default=None)


def current_user_email() -> str:
    """Return the user email bound to the current request.

    Raises:
        RuntimeError: if no identity is bound — indicates either that
            the middleware did not run or that the inbound request did
            not carry the expected header.
    """
    email = _user_email.get()
    if not email:
        raise RuntimeError(
            "User email context is not set — ensure the X-User-Email "
            "header is present on the inbound MCP request."
        )
    return email


class UserEmailMiddleware:
    """Bind the ``X-User-Email`` header into the request-local contextvar.

    Implemented as a pure ASGI middleware (not ``BaseHTTPMiddleware``) so
    that it does not wrap the response body — the MCP Streamable HTTP
    transport keeps the GET /mcp SSE stream open and writes JSON-RPC
    responses to it asynchronously; wrapping that stream breaks SSE
    delivery. Pure ASGI also runs in the same task as the downstream
    handler, so ``ContextVar.set`` here is visible to tool calls.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            for name, value in scope.get("headers") or ():
                if name == _USER_EMAIL_HEADER_BYTES:
                    _user_email.set(value.decode("latin-1"))
                    break
        await self.app(scope, receive, send)
