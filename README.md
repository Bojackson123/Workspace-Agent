# Sanmina Agentic PoC — Dual-MCP Google Workspace Agent

An enterprise Google Chat assistant that reads a user's Workspace context
(Gmail, Drive, Docs, Chat history) and produces outputs (Docs, Sheets,
files) into a designated Shared Drive — with a hard security boundary
between the two halves.

A single Chat app exposes several **slash commands** (`/research`,
`/draft`, …); each routes to its own workflow with its own system prompt
and toolset scope. Conversations are multi-turn per Chat thread, backed
by a persistent session store. Access to each command is governed by a
database-backed rules table — email and domain allowlists, managed at
runtime via `/grant` and `/revoke` slash commands so ops can change
who has access without a deploy.

The assistant is powered by the Google Agent Development Kit (ADK) and
runs against Vertex AI's `gemini-2.5-flash`. Tool calls flow through two
separately-deployed Model Context Protocol (MCP) servers:

- **Context MCP** — read-only, impersonates the calling user via
  Domain-Wide Delegation.
- **Action MCP** — read/write, authenticates as its own service account
  and writes only to a designated Shared Drive.

If the agent is ever prompt-injected or hallucinates an action, it
*structurally* cannot modify the user's personal data: the read-only
credentials live on a different server with different scopes.

See [`Docs/Architecture.md`](Docs/Architecture.md) for the full
architecture, cloud-resource layout, IAM roles, and reproducible setup
steps. For "how it flows" Mermaid diagrams, see
[`Docs/Workflow-Engine.md`](Docs/Workflow-Engine.md) (dispatch,
authorization, background tasks, sessions) and
[`Docs/Meeting-Workflow.md`](Docs/Meeting-Workflow.md) (the `/meeting`
pipeline with its gate and suspend/resume card flow).

## Repository layout

```
.
├── agentic-backend/         FastAPI webhook + ADK agent
│   ├── main.py              POST / entry point + JWT verification wiring
│   ├── security.py          Google Chat OIDC token verification
│   ├── chat.py              Chat event → workflow dispatch → reply envelope
│   ├── agent.py             LlmAgent factory (workflow-scoped toolsets)
│   ├── workflows/           Slash-command registry (one module/package per workflow)
│   │   ├── __init__.py      Explicit dispatch list + lookups
│   │   ├── _base.py         Workflow dataclass, AccessMode, reserved IDs
│   │   ├── _helpers.py      llm_workflow() helper for single-LlmAgent flows
│   │   ├── _default.py      DEFAULT_WORKFLOW (free-form fallback)
│   │   ├── research.py      /research — single LlmAgent on Context MCP
│   │   ├── draft.py         /draft — single LlmAgent on Action MCP
│   │   ├── sequential_report.py   /report — SequentialAgent example
│   │   ├── meeting_engine/  /meeting — gate + suspend/resume card pipeline
│   │   ├── review_board/    /review — adversarial critic LoopAgent pipeline
│   │   └── common/          Shared building blocks (gate, grounding, loop_exit)
│   ├── access.py            Email / domain / Google-Group authorization
│   ├── access_store.py      DB-backed access-rule store
│   ├── sessions.py          Thread-keyed multi-turn session store
│   ├── manage_access.py     Break-glass CLI for access rules
│   ├── config.py            Env-backed settings
│   └── Dockerfile
│
├── mcp-servers/
│   ├── context-mcp/         Read-only personal data (DWD impersonation)
│   │   ├── server.py        FastMCP app + UserEmailMiddleware
│   │   ├── auth.py          DWD credential minting (key file or signBlob)
│   │   ├── identity.py      X-User-Email → contextvar
│   │   ├── clients.py       Per-user Gmail/Drive/Docs services
│   │   ├── tools/           gmail.py, drive.py, docs.py (all .readonly)
│   │   └── Dockerfile
│   │
│   └── action-mcp/          Read/write Shared Drive operations (ADC)
│       ├── server.py        FastMCP app
│       ├── auth.py          ADC + Workspace scopes
│       ├── clients.py       Cached Drive/Docs/Sheets services
│       ├── tools/           drive.py, docs.py, sheets.py
│       └── Dockerfile
│
└── Docs/
    ├── Architecture.md      Full architecture + cloud setup + IAM
    ├── Workflow-Engine.md   Engine flowcharts (dispatch, auth, sessions)
    └── Meeting-Workflow.md  /meeting pipeline flowcharts (gate + cards)
```

## Quick start (local)

Each service uses [`uv`](https://github.com/astral-sh/uv) for dependency
management and Python 3.12+.

```powershell
# Authenticate ADC for both MCP servers
gcloud auth application-default login

# Required: Action MCP needs the target Shared Drive
$env:SHARED_DRIVE_ID = "<your-shared-drive-id>"

# Optional: lets you exercise /grant /revoke /list-access locally as
# yourself. Without this, no one is a bootstrap admin and the admin
# commands are unreachable.
$env:BOOTSTRAP_ADMIN_EMAILS = "you@example.com"

# In three separate terminals:
cd mcp-servers\context-mcp ; uv run uvicorn server:app --port 8002
cd mcp-servers\action-mcp  ; uv run uvicorn server:app --port 8001
cd agentic-backend         ; uv run uvicorn main:app --port 8080
```

The backend creates a local SQLite file `agentic-backend/sessions.db`
on first request (holds both ADK sessions and access rules). Delete
the file to reset state.

Defaults in `agentic-backend/config.py` point at `localhost:8001` /
`localhost:8002`, and `CHAT_APP_AUDIENCE` defaults to `None` so the JWT
audience check is skipped during local development.

You can also iterate on the agent without the webhook layer using the
ADK CLI:

```powershell
cd agentic-backend
uv run adk run agent       # or: uv run adk web
```

Note that `adk run`/`adk web` exercise the bare `root_agent` and cannot
inject a user email or a slash-command workflow, so Context MCP tools
will not work in that mode — the live MCP toolsets are wired only
inside the per-request `build_agent_for_workflow` factory.

## Adding a slash-command workflow

Each command lives in two places that must stay in lockstep:

1. **Google Cloud console → Google Chat API → Configuration → Commands.**
   Register the command with a numeric ID (1–1000), the slash name
   (`/foo`), and a description. This controls what shows up in the
   Chat autocomplete.
2. **A new file under `agentic-backend/workflows/`.** Export a
   `WORKFLOW: Workflow` constant whose `build_agent` factory returns
   any ADK `BaseAgent` — single `LlmAgent`, `SequentialAgent`,
   `LoopAgent`, custom subclass. Then add one import to
   `workflows/__init__.py` so the dispatcher picks it up.

For the common single-LlmAgent case, use the `llm_workflow` helper
(see `workflows/research.py`):

```python
# workflows/audit.py
from workflows._base import AccessMode, ToolsetKind
from workflows._helpers import llm_workflow

WORKFLOW = llm_workflow(
    command_id=6,
    command_name="/audit",
    description="...",
    instruction="...",
    toolsets=frozenset({ToolsetKind.CONTEXT}),
    default_access=AccessMode.RESTRICTED,  # opt in via /grant
)
```

For richer flows, build the agent directly (see
`workflows/sequential_report.py` for a `SequentialAgent` that chains
a research stage into a drafting stage). The dispatcher does not care
which `BaseAgent` subclass `build_agent` returns.

Pass an optional `ack_message="On it — …"` to either `llm_workflow(...)`
or the `Workflow(...)` constructor. The webhook returns that string
synchronously when the slash command is invoked so the user sees
immediate feedback, while the agent run is dispatched onto FastAPI
`BackgroundTasks` and the final reply is posted back via
`chat.spaces.messages.create`. This sidesteps Chat's "… is not
responding" banner (≈6s) and 30s webhook timeout. No extra Chat-API
setup is required beyond enabling `chat.googleapis.com` in the same
project as the backend — the `chat.bot` scope is requested at
runtime, not configured in IAM. See
[`Docs/Architecture.md`](Docs/Architecture.md) §1 step 9 for the full
flow, and §3.3 / §4.6 for the failure-mode canary if posting ever
breaks in production.

> **Deploy note:** the background path requires the `agent-backend`
> Cloud Run service to run with `--no-cpu-throttling` ("CPU is always
> allocated"). With default per-request CPU allocation the instance
> is throttled the moment the ack response is sent, and the
> background task's outbound TLS handshake to `chat.googleapis.com`
> intermittently fails mid-handshake (`SSLEOFError`). See
> [`Docs/Architecture.md`](Docs/Architecture.md) §4.4 and §7
> invariant 14.

Google Chat does not support per-command UI visibility, so restricted
commands are visible to everyone but rejected at the backend with an
audit-logged denial.

The reserved commands `/exit`, `/help`, `/grant`, `/revoke`, and
`/list-access` are handled by the dispatcher (no workflow registry
entries) but still need Cloud-console registration so users can
invoke them. The full ID list to register:

| Cloud-console Command ID | Slash name      |
| ------------------------ | --------------- |
| 1                        | `/research`     |
| 2                        | `/draft`        |
| 3                        | `/report`       |
| 4                        | `/meeting`      |
| 5                        | `/review`       |
| 995                      | `/list-access`  |
| 996                      | `/revoke`       |
| 997                      | `/grant`        |
| 998                      | `/help`         |
| 999                      | `/exit`         |

See [`Docs/Architecture.md`](Docs/Architecture.md) §4.6 for the full
Cloud-console walkthrough.

### Managing access

Access rules live in the `workflow_access_rules` table in the same
database as ADK sessions, and are managed at runtime via slash
commands restricted to bootstrap admins (set `BOOTSTRAP_ADMIN_EMAILS`
as a comma-separated list):

```
/grant /audit email:alice@sanmina.com
/grant /audit domain:sanmina.com
/revoke /audit email:alice@sanmina.com
/list-access /audit
```

Two rule kinds are supported: `email` (per-user allowlist) and
`domain` (everyone in a Workspace domain). Bootstrap admins are
governed by `BOOTSTRAP_ADMIN_EMAILS`, not by the rules table — losing
the table can't lock them out of `/grant`.

Workspace **group** rules are deliberately not supported. Checking
group membership requires a Workspace API (Admin SDK Directory or
Cloud Identity) and a Workspace-authorized credential to call it,
which is meaningful operational complexity for a marginal gain. If
you find you need group-based ACLs, model them as a domain rule or a
small set of explicit emails for now; we can add group lookups back
later with a concrete use case to justify the wiring.

For the cases the chat path can't handle (DB corruption, lost admin
access, automated provisioning), `manage_access.py` is a CLI with the
same `grant` / `revoke` / `list` subcommands.

### Multi-turn semantics

Continuations are tracked per Chat thread: typing a new slash command
resets the conversation, plain follow-ups stay inside the active
workflow until they expire (`SESSION_TTL_SECONDS`, default 30 min) or
the user types `/exit`.

## Deployment

All three services deploy as Cloud Run containers. See
[`Docs/Architecture.md`](Docs/Architecture.md) for:

- The full cloud-resource inventory (Cloud Run services, service
  accounts, Vertex AI, Shared Drive).
- Per-service IAM roles and Workspace API scopes.
- Step-by-step Workspace admin console + GCP setup for reproducing the
  stack in a new environment.
- Request flow diagrams and the security boundary rationale.
