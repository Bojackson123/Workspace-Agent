# Sanmina Agentic PoC — Dual-MCP Google Workspace Agent

An enterprise Google Chat assistant that reads a user's Workspace context
(Gmail, Drive, Docs, Chat history) and produces outputs (Docs, Sheets,
files) into a designated Shared Drive — with a hard security boundary
between the two halves.

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
steps.

## Repository layout

```
.
├── agentic-backend/         FastAPI webhook + ADK agent
│   ├── main.py              POST / entry point + JWT verification wiring
│   ├── security.py          Google Chat OIDC token verification
│   ├── chat.py              Chat event → agent run → reply envelope
│   ├── agent.py             LlmAgent factory wired to both MCP toolsets
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
    └── Architecture.md      Full architecture + cloud setup + IAM
```

## Quick start (local)

Each service uses [`uv`](https://github.com/astral-sh/uv) for dependency
management and Python 3.12+.

```powershell
# Authenticate ADC for both MCP servers
gcloud auth application-default login

# Set required env vars
$env:SHARED_DRIVE_ID = "<your-shared-drive-id>"

# In three separate terminals:
cd mcp-servers\context-mcp ; uv run uvicorn server:app --port 8002
cd mcp-servers\action-mcp  ; uv run uvicorn server:app --port 8001
cd agentic-backend         ; uv run uvicorn main:app --port 8080
```

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
inject a user email, so Context MCP tools will not work in that mode —
the live MCP toolsets are wired only inside the per-request
`build_agent_for_user` factory.

## Deployment

All three services deploy as Cloud Run containers. See
[`Docs/Architecture.md`](Docs/Architecture.md) for:

- The full cloud-resource inventory (Cloud Run services, service
  accounts, Vertex AI, Shared Drive).
- Per-service IAM roles and Workspace API scopes.
- Step-by-step Workspace admin console + GCP setup for reproducing the
  stack in a new environment.
- Request flow diagrams and the security boundary rationale.
