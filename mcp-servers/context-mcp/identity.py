"""Per-request user identity for the Context MCP.

The agent backend passes the calling user's email to this server via an
``X-User-Email`` HTTP header. A Starlette middleware copies that header
into a contextvar so individual tools can pick up the identity without
taking it as an explicit argument.

The header-based propagation is a deliberate security choice — the LLM
never sees or passes the user email itself, so it cannot be tricked into
impersonating someone else.
"""

from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

USER_EMAIL_HEADER = "x-user-email"

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


class UserEmailMiddleware(BaseHTTPMiddleware):
    """Bind the ``X-User-Email`` header into the request-local contextvar."""

    async def dispatch(self, request: Request, call_next):
        email = request.headers.get(USER_EMAIL_HEADER)
        if email:
            _user_email.set(email)
        return await call_next(request)
