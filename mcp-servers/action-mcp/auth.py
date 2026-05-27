"""Application Default Credentials for the Action MCP.

The Action MCP authenticates as its own Service Account — no user
impersonation. On Cloud Run this resolves to the runtime service
account; locally it falls back to ``gcloud auth application-default
login`` credentials (including impersonated SAs).
"""

import google.auth
from google.auth.credentials import Credentials

# Read/Write scopes for every Workspace API the Action MCP can call.
# These are intentionally broader than the Context MCP's read-only scopes —
# the security boundary lives in the separation of servers, not in scope
# subdivisions within this one.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
)


def get_action_credentials() -> Credentials:
    """Return ADC credentials with the Workspace read/write scopes attached.

    Some credential types (notably Compute Engine credentials on Cloud Run)
    do not embed scopes at construction time and must be re-scoped after
    the fact — ``requires_scopes`` flags that case.
    """
    credentials, _project = google.auth.default(scopes=list(SCOPES))
    if credentials.requires_scopes:
        credentials = credentials.with_scopes(list(SCOPES))
    return credentials
