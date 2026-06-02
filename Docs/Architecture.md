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
                  │ /research /draft   │
                  │ /report /help ...  │
                  └─────────┬──────────┘
                            │ HTTPS POST + OIDC bearer
                            │ (chat@system.gserviceaccount.com)
                            ▼
        ┌───────────────────────────────────────────┐
        │            Agent Backend                  │  Cloud Run
        │            (FastAPI + ADK)                │  backend-sa
        │                                           │
        │  security.py     → verify Chat JWT        │
        │  chat.py         → parse slashCommand,    │
        │                    thread, prompt;        │
        │                    /grant /revoke /help   │
        │  workflows.py    → resolve command_id     │
        │  access.py       → authorize              │
        │  access_store.py ←┐ (rules table)         │
        │  sessions.py    ←─┤ Postgres              │
        │                   └─► Cloud SQL           │
        │                      (sessions + ACL)     │
        │  agent.py        → build workflow-scoped  │
        │                    LlmAgent               │
        └──┬──────────────────────────────────┬─────┘
           │                                  │
           │ X-User-Email                     │ (no user identity)
           ▼                                  ▼
   ┌─────────────────┐                 ┌─────────────────┐
   │ Context MCP     │                 │ Action MCP      │   Cloud Run
   │ Read-only,      │                 │ Read/Write,     │
   │ DWD impersonate │                 │ acts as itself  │
   │ context-mcp-sa  │                 │ action-mcp-sa   │
   └────────┬────────┘                 └────────┬────────┘
            │                                   │
            ▼                                   ▼
   ┌─────────────────┐                 ┌─────────────────┐
   │ User's personal │                 │  Shared Drive   │
   │ Gmail / Drive / │                 │  (action-mcp-sa │
   │ Docs / Chat     │                 │   is a member)  │
   └─────────────────┘                 └─────────────────┘
```

### Request flow

1. **Chat → Backend.** Google Chat POSTs the event JSON to the backend
   with an OIDC bearer token in `Authorization`.
2. **JWT verification.** `agentic-backend/security.py` verifies the
   token signature, the `aud` claim, and that `email ==
   chat@system.gserviceaccount.com`. The body is *not* parsed before
   this check passes.
3. **Identity + payload extraction.** `agentic-backend/chat.py` reads
   `body.user.email`, the slash command (`message.slashCommand.commandId`,
   if any), the conversation thread (`message.thread.name`), and the
   prompt text (with the slash-command prefix stripped via the
   `SLASH_COMMAND` annotation).
4. **Workflow dispatch.** The `workflows/` package resolves the
   command ID to a `Workflow` entry. Reserved commands (`/exit`,
   `/help`, plus admin `/grant`, `/revoke`, `/list-access`) are
   handled inline and skip the LLM entirely.
5. **Authorization.** `access.py` asks `access_store.py` for the
   compiled `AccessPolicy` for this `command_id` (cached per process
   for `ACCESS_CACHE_TTL_SECONDS`). If the rules table is empty for
   the command, the workflow's `default_access` (`OPEN` / `RESTRICTED`)
   decides; otherwise the email / domain / group rules are evaluated.
   Denials produce an audit-logged warning and a user-facing
   rejection.
6. **Session resolve.** `sessions.py` looks up an ADK session keyed by
   `(user_email, sha256(thread.name))` in the `DatabaseSessionService`.
   A new slash command always starts a fresh session; a free-form
   continuation reuses the existing one (if within the inactivity TTL)
   and inherits its active workflow.
7. **Agent build.** `agentic-backend/agent.py:build_agent_for_workflow`
   constructs a fresh `LlmAgent` per request with the workflow's system
   instruction and only the MCP toolsets that workflow declares:
    - **Context toolset** — `X-User-Email: <user>` header attached at
      the transport layer.
    - **Action toolset** — no user identity attached at all.
8. **Tool calls.**
    - Context MCP middleware reads the header into a contextvar; each
      tool mints DWD credentials for that user and calls the Workspace
      API as them.
    - Action MCP authenticates with ADC (its own runtime SA) and writes
      to the Shared Drive.
9. **Ack + async reply.** Google Chat shows a *"… is not responding"*
   banner after roughly six seconds and hard-times-out the webhook
   around thirty. Multi-tool agent runs blow past both, so the
   dispatcher splits the response in two:
    - **Synchronous ack** — the webhook immediately returns either the
      workflow's `ack_message` (a short "On it…" line set per workflow
      in `workflows/`) or an empty `{}` envelope when none is
      configured. Plain follow-ups inside an active session always
      return empty so the thread doesn't fill with "working on it"
      noise.
    - **Background run** — the agent run is enqueued onto FastAPI's
      per-request `BackgroundTasks`. When the agent finishes, its
      final text is converted from standard Markdown into Chat's
      flavour (single-asterisk bold, `•` bullets, `<url|text>` links)
      and posted back into the same `space.name` + `thread.name` via
      `chat.spaces.messages.create` using `chat_client.py`.
      Reserved commands (`/help`, `/exit`, `/grant`, …) never use the
      background path — their responses are instant and return
      synchronously.

   ADK persists the conversation turn to the session DB during the
   background run, so the next message in the same thread can resume
   the workflow regardless of which path produced the reply.

### Why the email lives in a header, not a tool argument

The LLM never emits the user's email. If it tried to pass another email
through a tool argument, the Context MCP would ignore it — the
`UserEmailMiddleware` reads only `X-User-Email`, which is set by the
backend after JWT verification. Prompt injection cannot escalate
identity, because identity is structurally absent from the LLM's I/O
surface.

### Workflow engine

A single Chat app exposes multiple slash commands, each driving a
different workflow. The dispatcher itself is tiny — it looks up a
`Workflow` by `command_id`, asks it to build an ADK agent for the
inbound request, and runs whatever comes back. **What kind of ADK
agent comes back is the workflow's choice**: a single `LlmAgent`, a
`SequentialAgent` chaining stages, a `LoopAgent`, a custom `BaseAgent`
subclass — anything the SDK supports.

**Registry — `workflows/` package.** One module per command. Each
non-underscore module exposes a `WORKFLOW: Workflow` constant; the
package `__init__.py` imports them explicitly and assembles a
`dict[int, Workflow]` keyed by `command_id`.

The `Workflow` dataclass (`workflows/_base.py`) carries:

- `command_id` / `command_name` / `description` — metadata.
- `default_access` — an `AccessMode`. Decides what happens when the
  rules table has no rows for this command (`OPEN` allows anyone,
  `RESTRICTED` allows nobody — see "Access control" below).
- `build_agent(user_email) -> BaseAgent` — an async factory the
  dispatcher calls per request. The dispatcher does not care which
  ADK agent type comes back; a fresh build per request keeps MCP
  transports short-lived.

**Common shape via `llm_workflow(...)`.** Single-`LlmAgent` workflows
(the original `/research` and `/draft`) use `workflows/_helpers.py`'s
`llm_workflow(...)` factory — a one-line builder taking
`instruction`, `toolsets` (a `frozenset[ToolsetKind]` choosing
`CONTEXT`, `ACTION`, or both), and `default_access`. Workflows scoped
to one toolset are *structurally* incapable of using the other; the
unused MCP is not attached to the agent.

**Richer shapes via direct ADK construction.** Workflows like
`/report` (`workflows/sequential_report.py`) skip the helper and
build their agent directly using the public factories on `agent.py`
(`context_toolset`, `action_toolset`, `build_llm_agent`) plus
`google.adk.agents.SequentialAgent`. Splitting a workflow into stages
with disjoint toolsets is the multi-agent analogue of the dual-MCP
boundary — a research sub-agent given only the Context toolset
structurally cannot write to the Shared Drive, and a drafting
sub-agent given only the Action toolset structurally cannot read
personal data.

**Adding or disabling workflows.** New file under `workflows/`, plus
one import line in `__init__.py`. To temporarily disable, comment
the entry out of `_REGISTERED` in `__init__.py` — the module still
imports (so its prompt stays editable) but the dispatcher reports it
as unknown.

**The two halves must stay in lockstep.** A `command_id` registered in
the Cloud console but missing from `WORKFLOWS` yields an "Unknown
slash command" reply; a `command_id` in `WORKFLOWS` but missing from
the Cloud console will never reach the backend (the UI never offers
it). Adding a command is a two-step change.

**Slash command UI visibility.** Google Chat does not support
per-user-per-command autocomplete filtering. Every command registered
on the Chat app appears in autocomplete for every user who can see the
app. Access control therefore lives entirely in the backend
(`access.py`) — denied invocations return an informative rejection
("`/audit` is restricted to members of …; contact your admin"), and
each denial writes a structured `WARNING` to Cloud Logging.

**Reserved commands.** Five command IDs are handled directly by the
dispatcher and do not invoke the LLM:

| ID  | Command         | Purpose                                              |
| --- | --------------- | ---------------------------------------------------- |
| 999 | `/exit`         | Clear the active session for this thread.            |
| 998 | `/help`         | List commands the caller has access to.              |
| 997 | `/grant`        | Add an access rule (admins only).                    |
| 996 | `/revoke`       | Remove an access rule (admins only).                 |
| 995 | `/list-access`  | Show rules for a command (admins only).              |

All five must be registered in the Cloud console so users can invoke
them, but none of them get `WORKFLOWS` entries — they're handled in
code. The three admin commands are governed by env vars
(`BOOTSTRAP_ADMIN_EMAILS`, `BOOTSTRAP_ADMIN_GROUP`), **not** by the
rules table they manage — losing the table cannot lock admins out,
and a corrupted rule row cannot escalate by editing an admin
command's own ACL.

**Multi-turn sessions.** Chat marks `message.slashCommand` only on the
first message of an invocation; follow-ups in the same thread are
plain `MESSAGE` events. To support continuations, `sessions.py` keys
ADK sessions by `(user_email, sha256(thread.name))` and stores the
active workflow's `command_id` in `Session.state`. Semantics:

| Inbound event | Behaviour |
| --- | --- |
| New slash command in thread | Delete any existing session for the thread, create a fresh one scoped to the new workflow. Conversation history does not leak between workflows. |
| Free-form message with active session | Reuse the session, run the workflow whose `command_id` it stores. |
| Free-form message, no active session | Run `DEFAULT_WORKFLOW` (the dual-MCP catch-all). |
| Idle > `SESSION_TTL_SECONDS` (default 30 min) | Session is expired on next read, then deleted. Next message falls back to default. |
| `/exit` | Session deleted. |

**Access control.** Workflow *definitions* live in code; *who may
invoke them* lives in the database. Specifically:

- `workflows/_base.py` declares each workflow's `default_access` —
  `OPEN` or `RESTRICTED` — for what to do when no rules exist.
- `workflow_access_rules` (Postgres / SQLite) holds the actual grant
  rows: one principal per row. Two rule kinds, OR-evaluated:
    - `email` — explicit per-user allowlist
    - `domain` — domain-suffix match (e.g. internal users only)

Workspace group rules are deliberately not supported. Group
membership lives in Workspace (not GCP IAM) and querying it requires
a Workspace-authorized credential — DWD impersonating an admin, or a
Cloud Identity IAM role on a dedicated SA. Neither is free
operationally, and email/domain rules cover most real ACL needs. If
this ever needs to change, the schema (`rule_type TEXT`) is forward-
compatible: adding a `group` rule kind is purely a code change.

The decision matrix:

| `default_access` | rules in table | outcome             |
| ---------------- | -------------- | ------------------- |
| `OPEN`           | none           | allow               |
| `OPEN`           | one or more    | evaluate rules      |
| `RESTRICTED`     | none           | deny                |
| `RESTRICTED`     | one or more    | evaluate rules      |

DB rules are always authoritative when present; `default_access` only
chooses the empty-table behaviour. Compiled `AccessPolicy` objects
are cached per process for `ACCESS_CACHE_TTL_SECONDS` (default 60s);
a grant on one Cloud Run instance becomes visible on peers within
that window.

**Managing rules at runtime.** The `/grant`, `/revoke`, and
`/list-access` reserved commands are how operators edit the rules
table without a deploy:

```
/grant <command-name-or-id> email:<addr>
/grant <command-name-or-id> domain:<domain>
/revoke <command-name-or-id> <type>:<principal>
/list-access <command-name-or-id>
```

Examples: `/grant /audit email:alice@example.com`,
`/grant 2 domain:example.com`,
`/revoke /audit domain:example.com`.

The handler parses the body in code; it is never routed through the
LLM. Each accepted change writes a structured log line
(`GRANT command=… rule=…:… by=…`) for audit. Refusing to grant
against a reserved command name (e.g. `/grant /grant …`) returns an
explicit message — defense in depth against admins editing the very
commands that gate them.

For the cases the chat path can't handle (DB corruption, lost
bootstrap-admin access, automated provisioning), `manage_access.py`
exposes the same `grant` / `revoke` / `list` operations as a CLI.

---

## 2. Component reference

### 2.1 Agent Backend (`agentic-backend/`)

FastAPI app, single POST endpoint at `/`, runs the ADK agent
per-request.

| File | Responsibility |
| --- | --- |
| `main.py` | FastAPI app. Sets `GOOGLE_GENAI_USE_VERTEXAI=1` and `LOCATION` *before* importing google-genai, loads `.env`, mounts `verify_chat_jwt` as a dependency. |
| `security.py` | `verify_chat_jwt` — validates the Google Chat OIDC token via `google.oauth2.id_token.verify_oauth2_token`, checks audience matches `CHAT_APP_AUDIENCE`, and rejects unless `claims.email == chat@system.gserviceaccount.com`. |
| `chat.py` | `ChatEvent.from_payload` + `handle_event` / `_handle_message`. Parses Chat payloads (incl. `slashCommand.commandId`, `thread.name`, `space.name`, `SLASH_COMMAND` annotation), dispatches reserved commands inline (`/exit`, `/help`, plus the admin commands `/grant`, `/revoke`, `/list-access`), authorizes against the table-backed `AccessPolicy` combined with the workflow's `default_access`, resolves a persistent session via `SessionStore`, and **enqueues the agent run onto `BackgroundTasks`** instead of awaiting it inline — the sync return is just an ack so the webhook stays inside Chat's "not responding" window. `_markdown_to_chat()` translates standard Markdown into Chat's flavour before the reply is posted. Admin command bodies are parsed in code — never routed through the LLM — so prompt injection cannot reach the ACL writes. |
| `chat_client.py` | Outbound REST client for Chat. `post_message_to_space(space_name, text, thread_name=...)` mints ADC credentials with the `chat.bot` scope and POSTs to `spaces.messages.create`, threading replies into the same conversation. Called only from the background-task path; failures are logged and swallowed since no synchronous response exists to attach an error to. |
| `workflows/` | Package, one module per command. `_base.py` defines the `Workflow` dataclass (with an async `build_agent` factory and an optional `ack_message` shown immediately on slash-command invocation), `AccessMode`/`ToolsetKind` enums, reserved-command IDs, `ADMIN_COMMAND_IDS`, `RESERVED_COMMAND_NAMES`. `_helpers.py` exposes `llm_workflow(...)` for the single-LlmAgent shorthand. `_default.py` holds `DEFAULT_WORKFLOW` (the free-form fallback). Each non-underscore module (`research.py`, `draft.py`, `sequential_report.py`, …) exports a `WORKFLOW` constant; `__init__.py` imports them explicitly into the `WORKFLOWS` dispatch dict and re-exports the public names. |
| `access.py` | `AccessPolicy` dataclass + `authorize(user_email, command_id, default_mode, store)` — consults the store, applies `default_mode` on empty results, evaluates email and domain rules. `authorize_bootstrap_admin(user_email)` checks `BOOTSTRAP_ADMIN_EMAILS` for the admin slash commands. |
| `access_store.py` | `AccessStore` and `WorkflowAccessRule` (SQLAlchemy). `load(command_id)` compiles rule rows into an `AccessPolicy` with per-process TTL caching; `grant` / `revoke` / `list_rules` for the admin slash commands. Shares the SQLAlchemy engine with the session service so the backend has one Cloud SQL pool per process. |
| `manage_access.py` | Break-glass CLI mirroring the admin slash commands. Useful when bootstrap admins are misconfigured or for automated provisioning. |
| `sessions.py` | `SessionStore` wrapping ADK's `DatabaseSessionService`. `resolve(user_email, thread_name, new_workflow_id)` enforces the new-command-resets-session and TTL-expiry semantics; `clear()` implements `/exit`. |
| `agent.py` | Public building blocks workflow files reuse: `context_toolset(user_email)`, `action_toolset()`, `build_llm_agent(instruction, tools)`. `build_agent_for_workflow(workflow, user_email)` delegates to `workflow.build_agent(user_email)` — the dispatcher does not need to know whether the workflow returns one `LlmAgent` or a multi-step `SequentialAgent`. Module-level `root_agent` exists for the ADK CLI but has no toolsets attached. Imports from `workflows._base` only (not the package) to keep `workflows._helpers` → `agent` acyclic. |
| `config.py` | Memoised `Settings`: MCP URLs, `chat_audience`, `location`, `session_db_url`, `session_ttl_seconds`, `bootstrap_admin_emails` (governing admin slash commands), and `access_cache_ttl_seconds` for the compiled-policy cache. |
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
- `SESSION_DB_URL` — SQLAlchemy URL for ADK's
  `DatabaseSessionService`. Defaults to `sqlite+aiosqlite:///./sessions.db`
  (single-instance, ephemeral). **Set this in production** to a
  Cloud SQL Postgres URL (e.g. `postgresql+asyncpg://…`).
- `SESSION_TTL_SECONDS` — inactivity timeout before a Chat-thread
  session is dropped and the next message falls back to the default
  agent (default `1800`, i.e. 30 min).

**Optional env vars (admin commands and access cache):**

- `BOOTSTRAP_ADMIN_EMAILS` — comma-separated emails allowed to call
  `/grant` / `/revoke` / `/list-access`. Bootstrap admins are
  governed here, never via the rules table they manage. Required
  before any `RESTRICTED` workflow ships or any admin command can
  succeed.
- `ACCESS_CACHE_TTL_SECONDS` — how long compiled per-command
  `AccessPolicy` objects are cached in-process (default `60`).
  Bounds the cross-instance propagation delay after a `/grant`.

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
| Service account `backend-sa@…` | Identity for `agent-backend`. Needs Vertex AI access and Cloud SQL client access (for the session store). |
| Service account `context-mcp-sa@…` | Identity for `context-mcp`. Domain-Wide Delegation enabled, authorized for Workspace read scopes. |
| Service account `action-mcp-sa@…` | Identity for `action-mcp`. Member of the Shared Drive with **Contributor** (or **Manager**) permissions. |
| Cloud SQL instance (Postgres) | Stores ADK session rows AND the `workflow_access_rules` table (managed by `access_store.py`). One database, one connection pool, one set of credentials — both stores share the SQLAlchemy engine. A single small instance is sufficient for the PoC. |
| Shared Drive | Sole legal destination for any file the Action MCP creates. `action-mcp-sa` is a Drive member. |
| Vertex AI / Gemini | LLM provider. `gemini-2.5-flash` in `us-central1`. |
| Google Chat app | The user-facing surface. Webhook points at `agent-backend`'s Cloud Run URL. All slash commands are registered in this one app's *Commands* configuration; the backend dispatches by `commandId`. |

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
- `sqladmin.googleapis.com` (Cloud SQL for the session store)

### 3.3 IAM roles

#### `backend-sa` (Cloud Run runtime SA for `agent-backend`)

| Role | Why |
| --- | --- |
| `roles/aiplatform.user` | Call Vertex AI / Gemini. |
| `roles/cloudsql.client` | Connect to the session-store Cloud SQL instance via the Cloud SQL connector. |
| `roles/run.invoker` on `context-mcp` (*future*) | Mint ID tokens to call the MCP. Not required while MCP services are public. |
| `roles/run.invoker` on `action-mcp` (*future*) | Same. |
| `roles/logging.logWriter` | Cloud Run runtime logs. Granted by default in most setups. |

The backend additionally mints tokens with the **`https://www.googleapis.com/auth/chat.bot`** OAuth scope to post async replies back into Chat spaces (see §1 request flow step 9, and `chat_client.py`). `chat.bot` is an OAuth scope, not a GCP IAM role — it does not appear in the IAM "add role" picker and there is no IAM binding to grant. The scope is requested at runtime by `google.auth.default(scopes=[...])`; Chat accepts the resulting call because `backend-sa` lives in the same project as the Chat API configuration, and the Cloud Run metadata server can mint a scoped token for it. The only project-level prerequisite beyond what is already in §3.2 is that **`chat.googleapis.com` is enabled** — there is no separate "bind this SA to the Chat app" step in the Chat API console.

The backend does not call Workspace APIs directly (no Gmail / Drive / Docs / Sheets scopes); the only outbound Google API call is to Chat, for posting async replies.

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

### 4.5 Cloud SQL session store

```bash
INSTANCE=dual-mcp-sessions
DB_NAME=adk_sessions

gcloud sql instances create $INSTANCE \
  --database-version=POSTGRES_15 \
  --tier=db-f1-micro \
  --region=$REGION

gcloud sql databases create $DB_NAME --instance=$INSTANCE

# A dedicated DB user for the backend.
gcloud sql users create backend \
  --instance=$INSTANCE \
  --password=<generated-password>

# Cloud SQL client role for the backend SA.
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:backend-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

Then set `SESSION_DB_URL` on the backend Cloud Run service to a
Cloud-SQL-connector URL pointing at the instance, e.g.

```
SESSION_DB_URL=postgresql+asyncpg://backend:<password>@/adk_sessions?host=/cloudsql/<project>:<region>:dual-mcp-sessions
```

and attach the Cloud SQL instance to the service:

```bash
gcloud run services update agent-backend \
  --region=$REGION \
  --add-cloudsql-instances=${PROJECT_ID}:${REGION}:${INSTANCE}
```

ADK's `DatabaseSessionService` will create its tables on first use.

### 4.6 Google Chat app + slash command registration

In the GCP project, **APIs & Services → Google Chat API → Configuration**:

- App name, avatar, description: your choice.
- Functionality: at minimum *Receive 1:1 messages* and *Join spaces and
  group conversations*.
- Connection settings: **App URL** = the `agent-backend` Cloud Run URL.
- Permissions: restrict to your Workspace domain.

There is no separate field to associate a service account with the
Chat app. Async replies (§1 step 9 and `chat_client.py`) work as long
as `chat.googleapis.com` is enabled (§3.2) and `backend-sa` lives in
this same project — the Cloud Run metadata server can then mint a
token bearing the `chat.bot` OAuth scope, and Chat accepts the
post-back. If async replies fail in production, the canary is a 403
on `chat.spaces.messages.create` in `backend-sa`'s Cloud Logging
stream; check that the Chat API is enabled and that the backend
service is deployed into the project that owns the Chat app config.

**Commands.** Register one entry per workflow in
`agentic-backend/workflows.py`, plus the five reserved commands
(handled by the dispatcher, not the LLM). The `Command ID` field must
match the integer the backend dispatches on:

| Command ID | Name | Description |
| --- | --- | --- |
| 1 | `/research` | Summarise findings from your personal Workspace data. |
| 2 | `/draft` | Create or update a document on the Shared Drive. |
| 3 | `/report` | Research → draft pipeline (`SequentialAgent` example). |
| 995 | `/list-access` | Show access rules for a command (admins only). |
| 996 | `/revoke` | Remove an access rule (admins only). |
| 997 | `/grant` | Add an access rule (admins only). |
| 998 | `/help` | Show available commands. |
| 999 | `/exit` | End the current conversation. |

The admin commands are visible to everyone in the autocomplete (Chat
has no per-user UI filtering), but the backend rejects them for
non-bootstrap-admins with an audit-logged denial.

Save, then install the app into a test space and send it a message.

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

`SESSION_DB_URL` defaults to `sqlite+aiosqlite:///./sessions.db`, so
multi-turn workflows and the `workflow_access_rules` table both work
out of the box against a single local SQLite file (gitignored).
Delete the file to reset all state.

To exercise the admin slash commands locally, set
`BOOTSTRAP_ADMIN_EMAILS=you@example.com` before starting the backend
and POST hand-crafted Chat payloads with your own email as
`user.email`. The `manage_access.py` CLI also works against the
local SQLite file — useful for seeding rules without going through
the webhook:

```powershell
cd agentic-backend
uv run python manage_access.py grant /draft email:alice@example.com
uv run python manage_access.py list /draft
```

Email- and domain-based rules work without any extra configuration.
Workspace group rules are not supported in this build.

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
- **Workflow registry in YAML.** Today `workflows.py` is a Python
  module — typed, grep-friendly, and unit-testable, but every change
  needs a redeploy. Past ~10 workflows a YAML schema (validated at
  startup) lets non-engineers iterate on prompts without touching
  Python. Defer until the registry grows.
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
8. **Slash command IDs are the lockstep contract.** Every
   `command_id` in `workflows.WORKFLOWS` must be registered in the
   Chat API console and vice versa. The backend dispatches by ID, not
   by command text, so the integers must match exactly.
9. **Workflow toolsets are structural.** A workflow scoped to
   `{CONTEXT}` has no Action MCP attached to its agent at all — prompt
   injection cannot route around the registry to reach the other MCP.
10. **Backend authorization is the only access-control gate.** Google
    Chat shows every slash command to every user with the app
    installed; restricted workflows are rejected in `access.py`
    against the verified `user.email`, and every denial is logged.
11. **Workflow boundaries reset conversation history.** A new slash
    command in an active thread deletes the prior ADK session before
    creating the new one, so `/draft`'s system prompt never inherits
    `/research`'s tool-call history (or vice versa).
12. **Workflow definitions live in code; access rules live in the
    database.** Adding a workflow requires a deploy; granting access
    to one does not. The split exists so ops can manage ACLs at
    runtime without touching the codebase.
13. **Admin commands are governed by env vars, not the table they
    manage.** `/grant`, `/revoke`, and `/list-access` check
    `BOOTSTRAP_ADMIN_EMAILS` directly — they cannot grant or revoke
    themselves, and a corrupted `workflow_access_rules` table cannot
    lock admins out of fixing it.
