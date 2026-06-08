"""Domain-Wide Delegation credentials for the Context MCP.

The server impersonates the calling end user via Google Workspace DWD.
The logic here is deliberately portable across three deployments:

1. **Cloud Run / GCE** — ADC returns Compute Engine credentials.
   The DWD JWT is signed remotely via the IAM ``signBlob`` API.

2. **Local dev with an impersonated SA** — ``gcloud auth
   application-default login --impersonate-service-account=…`` gives
   impersonated credentials. Same remote ``signBlob`` path as Cloud Run.

3. **Local dev with a key file** — ``GOOGLE_APPLICATION_CREDENTIALS``
   points at a service-account JSON key. ``google.auth.default()``
   returns ``service_account.Credentials`` directly and can sign
   locally without IAM.

For deployments 1 and 2 the calling identity must hold
``iam.serviceAccountTokenCreator`` (or at minimum
``iam.serviceAccounts.signBlob``) on the target service account.

The auth flow and identity propagation here are intentional and form
part of the security boundary between the Context and Action MCPs —
they should not be modified without a corresponding architecture review.
"""

import google.auth
from google.auth.iam import Signer
from google.auth.transport.requests import Request
from google.oauth2 import service_account

# Scopes for personal user data. Read-only scopes cover Gmail/Drive/Docs
# browsing; gmail.compose and calendar.events are write-narrow scopes added
# for the Meeting Engine (draft creation and calendar holds only).
# Enforcement is structural — the LLM can never escalate beyond these scopes.
#
# DEPLOYMENT NOTE: The DWD grant in Google Admin Console must authorise the
# full scope set below for context-mcp-sa's client ID — DWD authorises the
# requested scopes atomically, so any scope missing from the Admin Console
# entry fails every impersonated token request. In particular this includes
# gmail.compose, calendar.events, and directory.readonly (People API lookups
# for the meeting invite dialog's org people picker).
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.memberships.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/directory.readonly"
)

# Scope used when bootstrapping ADC for the IAM signBlob call. We MUST
# request cloud-platform here — asking for Gmail/Drive scopes at this
# stage would cause the IAM API to reject the credentials.
_IAM_BOOTSTRAP_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/cloud-platform",
)


def get_dwd_credentials(user_email: str) -> service_account.Credentials:
    """Return credentials that impersonate *user_email* via DWD.

    Two code paths exist depending on what ``google.auth.default()``
    returns: a local key file can sign DWD JWTs locally, while ADC on
    Cloud Run / impersonated SAs must sign remotely via IAM signBlob.
    """
    creds, _project = google.auth.default(scopes=list(_IAM_BOOTSTRAP_SCOPES))

    if isinstance(creds, service_account.Credentials):
        # Key file path — re-scope to the Workspace APIs and bind the
        # subject. The credentials can sign the DWD JWT locally.
        return creds.with_scopes(list(SCOPES)).with_subject(user_email)

    # Non-key-file path (Cloud Run, impersonated SA, etc.). Refresh so
    # the credentials carry an access token we can pass to IAM.
    creds.refresh(Request())

    signer = Signer(
        request=Request(),
        credentials=creds,
        service_account_email=creds.service_account_email,
    )

    return service_account.Credentials(
        signer=signer,
        service_account_email=creds.service_account_email,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=list(SCOPES),
        subject=user_email,
    )
