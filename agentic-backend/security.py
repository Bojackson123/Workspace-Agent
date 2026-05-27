"""JWT verification for inbound Google Chat webhook requests.

Google Chat signs every webhook payload with an OIDC token issued by the
``chat@system.gserviceaccount.com`` service account. We verify both the
signature and the issuer email before any request body is trusted —
the agent must never act on an unverified payload.
"""

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from config import settings

# The only identity that may legitimately invoke a Google Chat app webhook.
_CHAT_ISSUER_EMAIL = "chat@system.gserviceaccount.com"

_bearer_scheme = HTTPBearer()


def verify_chat_jwt(
    credentials: HTTPAuthorizationCredentials = Security(_bearer_scheme),
) -> dict:
    """Verify a Google Chat OIDC token and return its decoded claims.

    Raises:
        HTTPException(401): the token is absent, malformed, expired, or
            its signature cannot be verified against Google's public keys.
        HTTPException(403): the token is cryptographically valid but the
            issuer email is not the expected Google Chat service account.
    """
    try:
        claims = id_token.verify_oauth2_token(
            credentials.credentials,
            google_requests.Request(),
            audience=settings().chat_audience,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"Invalid token: {exc}") from exc

    if claims.get("email") != _CHAT_ISSUER_EMAIL:
        raise HTTPException(status_code=403, detail="Invalid issuer email")

    return claims
