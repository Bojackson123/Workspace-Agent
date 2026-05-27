# Architecture — Dual-MCP Google Workspace Agent

This document describes how the Sanmina Agentic PoC is built, how it
runs in Google Cloud, and how to reproduce the stack from scratch in a
new environment. It is the source of truth for the code, the cloud
resources, and the IAM model.

---

## 1. System overview

The system is a Google Chat-fronted Workspace assistant. The
human-facing surface is a single Chat app; the brain is a Vertex AI
LLM driven through the Google Agent Development Kit (ADK). All
Workspace I/O is brokered through two separately-deployed Model Context
Protocol (MCP) servers.

The dual-MCP split is the security model. Personal data is fetched via
one server with **read-only** scopes that **impersonates the calling
user**. Outputs are produced via a second server with **read/write**
scopes that **acts as itself** and can only touch a designated Shared
Drive. The LLM never holds, sees, or passes any user-identifying
token — it only emits tool calls.

```
                  ┌────────────────────┐
   Human user ───►│   Google Chat app  │
                  └─────────┬──────────┘
                            │ HTTPS POST + OIDC bearer
                            │ (chat@system.gserviceaccount.com)
                            ▼
                  ┌────────────────────┐
                  │  Agent Backend     │  Cloud Run, public ingress
                  │  (FastAPI + ADK)   │  Service account: backend-sa
                  └──┬──────────────┬──┘
                     │              │
                     │              │
        X-User-Email │              │ (no user identity)
        header       │              │
                     ▼              ▼
       ┌─────────────────┐   ┌─────────────────┐
       │ Context MCP     │   │ Action MCP      │   Cloud Run
       │ Read-only,      │   │ Read/Write,     │
       │ DWD impersonate │   │ acts as itself  │
       │ context-mcp-sa  │   │ action-mcp-sa   │
       └────────┬────────┘   └────────┬────────┘
                │                     │
                ▼                     ▼
       ┌─────────────────┐   ┌─────────────────┐
       │ User's personal │   │  Shared Drive   │
       │ Gmail / Drive / │   │  (action-mcp-sa │
       │ Docs / Chat     │   │   is a member)  │
       └─────────────────┘   └─────────────────┘
```

### Request flow

1. **Chat → Backend.** Google Chat POSTs the event JSON to the backend
   with an OIDC bearer token in `Authorization`.
2. **JWT verification.** `agentic-backend/security.py` verifies the
   token signature, the `aud` claim, and that `email ==
   chat@system.gserviceaccount.com`. The body is *not* parsed before
   this check passes.
3. **Identity extraction.** `agentic-backend/chat.py` reads
   `body.user.email` from the verified payload.
4. **Agent build.** `agentic-backend/agent.py:build_agent_for_user`
   constructs a fresh `LlmAgent` per request with two MCP toolsets:
    - **Context toolset** — `X-User-Email: <user>` header attached at
      the transport layer.
    - **Action toolset** — no user identity attached at all.
5. **Tool calls.**
    - Context MCP middleware reads the header into a contextvar; each
      tool mints DWD credentials for that user and calls the Workspace
      API as them.
    - Action MCP authenticates with ADC (its own runtime SA) and writes
      to the Shared Drive.
6. **Reply.** The final ADK response text is wrapped in
   `{"text": "..."}` and returned to Chat.

### Why the email lives in a header, not a tool argument

The LLM never emits the user's email. If it tried to pass another email
through a tool argument, the Context MCP would ignore it — the
`UserEmailMiddleware` reads only `X-User-Email`, which is set by the
backend after JWT verification. Prompt injection cannot escalate
identity, because identity is structurally absent from the LLM's I/O
surface.

---

## 2. Component reference

### 2.1 Agent Backend (`agentic-backend/`)

FastAPI app, single POST endpoint at `/`, runs the ADK agent
per-request.

| File | Responsibility |
| --- | --- |
| `main.py` | FastAPI app. Sets `GOOGLE_GENAI_USE_VERTEXAI=1` and `LOCATION` *before* importing google-genai, loads `.env`, mounts `verify_chat_jwt` as a dependency. |
| `security.py` | `verify_chat_jwt` — validates the Google Chat OIDC token via `google.oauth2.id_token.verify_oauth2_token`, checks audience matches `CHAT_APP_AUDIENCE`, and rejects unless `claims.email == chat@system.gserviceaccount.com`. |
| `chat.py` | `handle_event` / `_handle_message` / `_run_agent`. Parses Chat payloads, builds a per-request ADK `Runner` over an `InMemorySessionService`, streams the final response. |
| `agent.py` | `build_agent_for_user(user_email)` — constructs an `LlmAgent` (`gemini-2.5-flash`) with two `MCPToolset`s. Module-level `root_agent` exists for the ADK CLI but has no toolsets attached (the CLI cannot supply an email). |
| `config.py` | Memoised `Settings`: `context_mcp_url`, `action_mcp_url`, `chat_audience`, `location`. |
| `Dockerfile` | Two-stage `uv` build → distroless-ish `python:3.12-slim`, non-root user, `uvicorn main:app --workers 1` on port 8080. |

**Why a fresh agent per request.** Long-lived MCP transports can cache
ADC tokens past their refresh window or leave half-open
streamable-HTTP sessions. Rebuilding the agent (and the underlying
toolsets) per webhook event sidesteps both failure modes.

**Required env vars:**

- `CONTEXT_MCP_SERVICE` — base URL of the Context MCP (e.g.
  `https://context-mcp-xxx-uc.a.run.app`).
- `ACTION_MCP_SERVICE` — base URL of the Action MCP.
- `CHAT_APP_AUDIENCE` — the Cloud Run URL of the backend itself. Used as
  the `aud` check on inbound JWTs. **Set this in production.** When
  unset (local dev), the audience check is skipped.
- `LOCATION` — Vertex AI region (default `us-central1`).
- `GOOGLE_CLOUD_PROJECT` — Vertex AI project (picked up by
  google-genai).

### 2.2 Context MCP (`mcp-servers/context-mcp/`)

Read-only MCP server. Impersonates the calling user via Domain-Wide
Delegation and exposes Gmail / Drive / Docs read tools.

| File | Responsibility |
| --- | --- |
| `server.py` | Builds `FastMCP` with `TransportSecuritySettings(allowed_hosts=...)`, registers all tools, exposes `app = mcp.streamable_http_app()`, and attaches `UserEmailMiddleware` *after* the ASGI app is constructed so the middleware wraps every MCP request. |
| `identity.py` | `UserEmailMiddleware` copies `X-User-Email` → contextvar. `current_user_email()` reads it and raises if absent — tools cannot silently impersonate "no one." |
| `auth.py` | `get_dwd_credentials(user_email)`. Three deployment paths: (a) Cloud Run / GCE — Compute Engine credentials sign DWD JWTs remotely via IAM `signBlob`. (b) Local dev with `gcloud auth application-default login --impersonate-service-account=` — same `signBlob` path. (c) Local dev with `GOOGLE_APPLICATION_CREDENTIALS` pointing at a JSON key — local signing. |
| `clients.py` | Per-user `gmail() / drive() / docs()` factories. **No caching by user** — caching here would smear identity across concurrent requests on a long-running worker. |
| `tools/gmail.py` | `search_emails(query, max_results)` — Gmail query language, returns `Email ID + Subject` rows. |
| `tools/drive.py` | `search_my_drive`, `get_my_file_metadata` — narrow field selection (`id, name, mimeType, modifiedTime, owners(emailAddress), webViewLink`). |
| `tools/docs.py` | `read_my_document(document_id)` — plain-text extraction; skips inline objects / tables. |
| `Dockerfile` | Identical layout to backend. Runs `uvicorn server:app` on port 8080. |

**Scopes minted by `auth.py`:**

```
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/documents.readonly
https://www.googleapis.com/auth/chat.spaces.readonly
https://www.googleapis.com/auth/chat.memberships.readonly
https://www.googleapis.com/auth/chat.messages.readonly
```

These exact scopes must be authorized on the DWD client ID in the
Workspace admin console — Google rejects DWD calls for any scope not on
the allow-list.

**Required env vars:**

- `ALLOWED_HOSTS` — comma-separated list of Host headers the MCP
  transport will accept. Set to the service's Cloud Run hostname (URL
  form is also accepted; scheme is stripped). Defaults to `localhost`.

### 2.3 Action MCP (`mcp-servers/action-mcp/`)

Read/write MCP server. Authenticates as its own runtime service
account; only writes to a designated Shared Drive.

| File | Responsibility |
| --- | --- |
| `server.py` | Builds `FastMCP` with transport security, registers all tools, exposes the streamable-HTTP ASGI app. No identity middleware — there is no per-user identity here. |
| `auth.py` | `get_action_credentials()` — `google.auth.default(scopes=...)` with `drive`, `documents`, `spreadsheets` scopes. Re-applies scopes via `with_scopes` when ADC returns un-scoped Compute Engine creds. |
| `clients.py` | `@cache`-d Drive / Docs / Sheets services — the underlying credentials handle token refresh, so caching the service object for the process lifetime is safe. |
| `tools/drive.py` | `search_drive`, `list_files`, `get_file_metadata`, `create_workspace_file`, `create_folder`, `copy_file`, `move_file`, `delete_file`. **`_resolve_parent` centralises the "default to Shared Drive root" behaviour** so no tool can accidentally write to the SA's hidden root drive. Every list/get uses `supportsAllDrives=True` + `corpora="drive"` + `driveId=<shared>`. |
| `tools/docs.py` | `create_document`, `read_document`, `append_text`, `insert_text`, `replace_text`. `create_document` goes through the **Drive** API (not `documents.create`) so the file lands in the Shared Drive in one call. |
| `tools/sheets.py` | `create_spreadsheet`, `read_range`, `write_range`, `append_rows`, `clear_range`, `add_sheet`. Same Drive-API trick for creation; A1 notation for ranges; `valueInputOption=USER_ENTERED` so formulas/dates parse like UI input. |
| `config.py` | `SHARED_DRIVE_ID` (required at runtime — fails loudly via `require_shared_drive_id`), `ALLOWED_HOSTS`. |
| `Dockerfile` | Same as Context MCP. |

**Why the Drive API for `create_document` / `create_spreadsheet`.**
The Docs/Sheets native `create` endpoints don't accept a `parents`
field, so the file would be created in the calling SA's root drive
(which is also hidden — there is no UI to retrieve it). Creating via
`drive.files.create` with a `parents=[shared_drive_id]` is the only way
to atomically land the file in the Shared Drive.

**Scopes minted by `auth.py`:**

```
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/documents
https://www.googleapis.com/auth/spreadsheets
```

Note these are intentionally broader than the Context MCP's read-only
scopes. The security boundary is the **separation of servers**, not a
finer-grained scope split inside one server.

**Required env vars:**

- `SHARED_DRIVE_ID` — Drive ID of the target Shared Drive (the long
  string visible in the URL when you open the Shared Drive). The
  service fails loudly on any tool call if this is unset.
- `ALLOWED_HOSTS` — same convention as Context MCP.

---

## 3. Cloud resources

Everything below assumes a single GCP project (`<PROJECT_ID>`) and a
single region (`us-central1`). Adjust as needed.

### 3.1 Inventory

| Resource | Purpose |
| --- | --- |
| Cloud Run service `agent-backend` | FastAPI webhook. **Public ingress** (Google Chat must be able to reach it). |
| Cloud Run service `context-mcp` | Read-only MCP. Currently public ingress for PoC. |
| Cloud Run service `action-mcp` | Read/write MCP. Currently public ingress for PoC. |
| Artifact Registry repo | Stores the three container images. |
| Cloud Build (optional) | Build + push pipeline. Manual `gcloud builds submit` works for the PoC. |
| Service account `backend-sa@…` | Identity for `agent-backend`. Needs Vertex AI access. |
| Service account `context-mcp-sa@…` | Identity for `context-mcp`. Domain-Wide Delegation enabled, authorized for Workspace read scopes. |
| Service account `action-mcp-sa@…` | Identity for `action-mcp`. Member of the Shared Drive with **Contributor** (or **Manager**) permissions. |
| Shared Drive | Sole legal destination for any file the Action MCP creates. `action-mcp-sa` is a Drive member. |
| Vertex AI / Gemini | LLM provider. `gemini-2.5-flash` in `us-central1`. |
| Google Chat app | The user-facing surface. Webhook points at `agent-backend`'s Cloud Run URL. |

### 3.2 Enabled APIs

In the GCP project:

- `run.googleapis.com`
- `artifactregistry.googleapis.com`
- `cloudbuild.googleapis.com` (only if you use Cloud Build)
- `iamcredentials.googleapis.com` (required for the IAM `signBlob` path
  in `context-mcp/auth.py`)
- `aiplatform.googleapis.com` (Vertex AI)
- `chat.googleapis.com`
- `gmail.googleapis.com`
- `drive.googleapis.com`
- `docs.googleapis.com`
- `sheets.googleapis.com`

### 3.3 IAM roles

#### `backend-sa` (Cloud Run runtime SA for `agent-backend`)

| Role | Why |
| --- | --- |
| `roles/aiplatform.user` | Call Vertex AI / Gemini. |
| `roles/run.invoker` on `context-mcp` (*future*) | Mint ID tokens to call the MCP. Not required while MCP services are public. |
| `roles/run.invoker` on `action-mcp` (*future*) | Same. |
| `roles/logging.logWriter` | Cloud Run runtime logs. Granted by default in most setups. |

No Workspace scopes — the backend never calls Workspace APIs directly.

#### `context-mcp-sa` (Cloud Run runtime SA for `context-mcp`)

| Role | Why |
| --- | --- |
| `roles/iam.serviceAccountTokenCreator` **on itself** | The `signBlob` path in `auth.py` needs to sign DWD JWTs via IAM. Without this the impersonation flow fails on Cloud Run. |
| `roles/logging.logWriter` | Cloud Run runtime logs. |

DWD setup is performed in the Workspace admin console (§4.2), not in
GCP IAM.

`context-mcp-sa` does **not** need any Drive/Gmail/Docs role in GCP IAM
— DWD is its own authorization channel.

#### `action-mcp-sa` (Cloud Run runtime SA for `action-mcp`)

| Role | Why |
| --- | --- |
| `roles/logging.logWriter` | Cloud Run runtime logs. |

No project-level Workspace IAM is required. Authorization is granted by
**membership on the Shared Drive** (Contributor or Manager), not by an
IAM role.

### 3.4 Networking and ingress (current PoC state)

- `agent-backend` — ingress `all`, allow unauthenticated (Google Chat
  posts from the public internet; identity comes from the OIDC token,
  not the network).
- `context-mcp` — ingress `all`, allow unauthenticated for now.
- `action-mcp` — ingress `all`, allow unauthenticated for now.

The intent is to harden the MCP services later — see §6.

---

## 4. Reproducible setup (production)

This is the click-by-click path for standing the stack up in a fresh
environment.

### 4.1 GCP project bootstrap

```bash
PROJECT_ID=<your-project>
REGION=us-central1

gcloud config set project $PROJECT_ID

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com \
  aiplatform.googleapis.com \
  chat.googleapis.com \
  gmail.googleapis.com \
  drive.googleapis.com \
  docs.googleapis.com \
  sheets.googleapis.com

# Service accounts
for sa in backend-sa context-mcp-sa action-mcp-sa; do
  gcloud iam service-accounts create $sa \
    --display-name "$sa"
done

# Vertex AI for the backend SA
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:backend-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Token creator on context-mcp-sa for itself (signBlob path)
gcloud iam service-accounts add-iam-policy-binding \
  context-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com \
  --member="serviceAccount:context-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

# Artifact Registry repo
gcloud artifacts repositories create dual-mcp \
  --repository-format=docker \
  --location=$REGION
```

### 4.2 Workspace admin console — Domain-Wide Delegation

DWD is what lets `context-mcp-sa` impersonate end users. This is
configured in `admin.google.com` by a Workspace super-admin, **not** in
GCP.

1. Note the **OAuth client ID** of `context-mcp-sa`. You can read it
   from `gcloud iam service-accounts describe
   context-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com
   --format='value(oauth2ClientId)'`.
2. Sign in to `admin.google.com` as a super-admin.
3. **Security → Access and data control → API controls → Domain-wide
   delegation → Manage Domain Wide Delegation → Add new.**
4. Paste the OAuth client ID.
5. Paste the comma-separated scopes from `context-mcp/auth.py`:

    ```
    https://www.googleapis.com/auth/gmail.readonly,
    https://www.googleapis.com/auth/drive.readonly,
    https://www.googleapis.com/auth/documents.readonly,
    https://www.googleapis.com/auth/chat.spaces.readonly,
    https://www.googleapis.com/auth/chat.memberships.readonly,
    https://www.googleapis.com/auth/chat.messages.readonly
    ```

6. Save.

Propagation can take up to ~30 minutes. Symptom of incomplete
propagation: `unauthorized_client: Client is unauthorized to retrieve
access tokens using this method`.

**Do not authorize Action MCP scopes for any client ID via DWD.** The
Action MCP must never be able to impersonate users — its access path is
ADC + Shared Drive membership only.

### 4.3 Shared Drive setup

1. In `drive.google.com`, create a new Shared Drive (or pick an
   existing one).
2. **Manage members → Add** the **email address** of
   `action-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com` with role
   **Contributor** (sufficient for create/read/update of items) or
   **Manager** (also allows trashing/restoring drive-level items and
   managing membership).
3. Copy the Drive ID from the URL — it is the segment after
   `/drive/folders/` when you open the Shared Drive root. This is the
   value you'll pass as `SHARED_DRIVE_ID`.

The Drive ID is not a secret, but treat it as configuration: every
write the Action MCP performs is parented under it.

### 4.4 Build and deploy

From the repo root:

```bash
# Build & push images
for svc in agent-backend:agentic-backend \
           context-mcp:mcp-servers/context-mcp \
           action-mcp:mcp-servers/action-mcp; do
  name=${svc%%:*}
  dir=${svc#*:}
  gcloud builds submit $dir \
    --tag=${REGION}-docker.pkg.dev/${PROJECT_ID}/dual-mcp/${name}:latest
done

# Deploy Context MCP
gcloud run deploy context-mcp \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/dual-mcp/context-mcp:latest \
  --region=$REGION \
  --service-account=context-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --ingress=all \
  --set-env-vars="ALLOWED_HOSTS=context-mcp-<hash>-uc.a.run.app"
# (You'll know the hash after the first deploy — redeploy with the
#  correct ALLOWED_HOSTS once you have the URL.)

# Deploy Action MCP
gcloud run deploy action-mcp \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/dual-mcp/action-mcp:latest \
  --region=$REGION \
  --service-account=action-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --ingress=all \
  --set-env-vars="SHARED_DRIVE_ID=<drive-id>,ALLOWED_HOSTS=action-mcp-<hash>-uc.a.run.app"

# Deploy the backend
gcloud run deploy agent-backend \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/dual-mcp/agent-backend:latest \
  --region=$REGION \
  --service-account=backend-sa@${PROJECT_ID}.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --ingress=all \
  --set-env-vars="\
GOOGLE_CLOUD_PROJECT=${PROJECT_ID},\
LOCATION=${REGION},\
CONTEXT_MCP_SERVICE=https://context-mcp-<hash>-uc.a.run.app,\
ACTION_MCP_SERVICE=https://action-mcp-<hash>-uc.a.run.app,\
CHAT_APP_AUDIENCE=https://agent-backend-<hash>-uc.a.run.app"
```

After each service is deployed once, copy the actual URL back into the
relevant env var (`CHAT_APP_AUDIENCE`, `CONTEXT_MCP_SERVICE`,
`ACTION_MCP_SERVICE`, `ALLOWED_HOSTS`) and redeploy.

### 4.5 Google Chat app configuration

In the GCP project, **APIs & Services → Google Chat API → Configuration**:

- App name, avatar, description: your choice.
- Functionality: at minimum *Receive 1:1 messages* and *Join spaces and
  group conversations*.
- Connection settings: **App URL** = the `agent-backend` Cloud Run URL.
- Permissions: restrict to your Workspace domain.
- Save.

Then install the app into a test space and send it a message.

---

## 5. Local development

`agentic-backend/config.py` defaults to `localhost:8001` /
`localhost:8002` and leaves `CHAT_APP_AUDIENCE` unset (which short-
circuits the audience check). For local DWD impersonation to work, one
of the following must be true on the Context MCP process:

- `GOOGLE_APPLICATION_CREDENTIALS` points at a JSON key file for a
  service account that is itself authorized for DWD (the local-signing
  branch of `auth.py`), **or**
- you've run `gcloud auth application-default login --impersonate-
  service-account=context-mcp-sa@${PROJECT_ID}.iam.gserviceaccount.com`
  and your user has `roles/iam.serviceAccountTokenCreator` on
  `context-mcp-sa` (the remote-signing branch).

There is no need to expose a Cloud Run URL during local dev — invoke
the backend directly with a hand-crafted JSON payload to test event
handling.

---

## 6. Future work — locking down the MCP services

Today both MCP services are `--allow-unauthenticated` with `--ingress=
all`. The backend has to be public (Google Chat posts from the open
internet), but the MCP services do not. Two ways to harden, in
increasing order of cost:

### Option A — Cloud Run IAM invocation (lowest cost, strong)

1. Redeploy each MCP service with `--no-allow-unauthenticated`.
2. Grant `roles/run.invoker` on `context-mcp` and `action-mcp` to
   `backend-sa`.
3. Modify the backend's `MCPToolset` connection to attach an OIDC ID
   token to each request, with the target service URL as the audience.
   ADK currently does not do this out of the box for the
   `StreamableHTTPConnectionParams` transport, so this requires a small
   custom transport that:
    - Calls
      `google.auth.transport.requests.Request().fetch_id_token(audience=<mcp-url>)`
      using the backend's default credentials.
    - Injects `Authorization: Bearer <id-token>` into the request
      headers alongside `X-User-Email`.
4. Keep `--ingress=all` (still reachable from the public internet, but
   only by callers who can mint a valid ID token).

This is the recommended next step. It removes the public attack surface
on the MCP services without requiring a VPC or any private networking.

### Option B — Private ingress + VPC connector

1. Put all three services on a Serverless VPC connector.
2. Set MCP services to `--ingress=internal`.
3. Keep the backend at `--ingress=all` (Chat needs to reach it).
4. Pair with the IAM invoker setup from Option A for defense-in-depth.

More moving parts (VPC, connector, IP allocation) but stronger
isolation — the MCP services are no longer reachable from outside the
project's serverless network at all.

### Other suggestions worth discussing

- **`ALLOWED_HOSTS` is not a security control.** It's a transport-layer
  Host-header filter that prevents trivial cross-origin smuggling, but
  any attacker who can reach the service can also set the right Host
  header. Treat it as defence in depth, not a gate.
- **Move the FastMCP session service off in-memory.** Today
  `chat.py` uses `InMemorySessionService`. That's fine for single-turn
  webhook events, but if you ever want multi-turn conversations you'll
  need Firestore-backed sessions (and the backend SA will need
  `roles/datastore.user`).
- **Vertex AI region pinning.** `LOCATION` defaults to `us-central1`.
  If Sanmina has a data residency requirement, set it explicitly and
  document it as a constraint — the `gemini-2.5-flash` model needs to
  be available in the chosen region.
- **Per-tenant Shared Drives.** Today there is exactly one
  `SHARED_DRIVE_ID`. If you ever serve multiple business units from the
  same backend, the Action MCP will need a tenant → drive resolver
  (likely keyed off the user's email domain or Workspace OU).
- **Logging redaction.** Log lines in `chat.py` include
  `event.user_email`. That's fine within a single Workspace tenant but
  worth reviewing if logs are ever exported to a third-party SIEM.
- **Image scanning.** Artifact Registry's vulnerability scanning is
  free for low-volume use — turn it on. The `python:3.12-slim` base is
  small but not vulnerability-free.

---

## 7. Operating rules baked into the code

These are the invariants the code enforces. Future changes should
preserve them or document the reason for breaking them.

1. **The agent backend never trusts a Chat payload before JWT
   verification.** `verify_chat_jwt` runs as a FastAPI dependency
   before the request body is parsed.
2. **The LLM never sees the user's email.** It's attached to MCP
   requests as a transport header (`X-User-Email`) by code, not by the
   LLM.
3. **Context MCP tools cannot run without a bound user.**
   `current_user_email()` raises if the contextvar is unset.
4. **Action MCP tools cannot write outside the Shared Drive.**
   `_resolve_parent` defaults to `SHARED_DRIVE_ID`; the runtime fails
   loudly if that env var is unset.
5. **Document/Sheet creation goes through the Drive API.** Native
   `documents.create` / `spreadsheets.create` would orphan files in the
   SA's hidden root drive — we never call them.
6. **Drive list/get calls always set `supportsAllDrives=True`.**
   Required to even *see* Shared Drive items; omitting it produces
   confusingly empty results.
7. **Read-only scopes live in Context MCP, read/write scopes live in
   Action MCP, and never the twain shall meet.** The security boundary
   is the separation of servers, not finer-grained scopes inside one
   server.
