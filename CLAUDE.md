# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Google Chat assistant that reads a user's Workspace context (Gmail, Drive,
Docs, Calendar, Chat) and produces outputs (Docs, Sheets, files) into a
designated Shared Drive. Three separately-deployed services, all Cloud Run,
all Python 3.12 + [`uv`](https://github.com/astral-sh/uv):

- **`agentic-backend/`** — FastAPI webhook + the Google ADK agent (Vertex AI
  `gemini-2.5-flash`). Receives Chat events, dispatches to a slash-command
  workflow, runs the agent, posts replies back.
- **`mcp-servers/context-mcp/`** — read-only MCP. Impersonates the calling
  user via Domain-Wide Delegation. Gmail/Drive/Docs/Calendar/Directory tools,
  all `.readonly`.
- **`mcp-servers/action-mcp/`** — read/write MCP. Authenticates as its own
  service account; writes only into one Shared Drive. Drive/Docs/Sheets +
  RFI file (`.xlsx`/`.docx`) tools.

`Docs/Code-Walkthrough.md` traces one request entry-point→endpoint, file by
file — the fastest way to orient. `Docs/Architecture.md` is the source of truth
— full cloud/IAM/setup detail and the numbered list of code invariants (§7).
`Docs/Workflow-Engine.md` and `Docs/Meeting-Workflow.md` have Mermaid
flowcharts. Read those before any structural change; the summary below is
orientation, not a replacement.

**`agentic-backend/` layout** — only `main.py` sits at the root; everything
else is grouped by concern into packages:

```
main.py            # FastAPI app entrypoint
engine/            # composable engine framework (EngineSpec + compiler + registry)
workflows/         # one package per slash command; each declares an EngineSpec
chat/              # all Google Chat I/O: dispatch, cards, client.py, security.py
access/            # access control: policy.py, store.py, manage.py (CLI)
sessions/          # ADK session store (__init__.py is the module)
clients/           # outbound service clients: agent.py (model/toolsets), mcp_client.py
config/            # settings (__init__.py is the module)
observability/     # logging_setup.py, tracing_setup.py
dev/               # `adk web dev/` harness
```

## Commands

Each service is its own `uv` project (separate `pyproject.toml` / `.venv`).
Run commands from inside that service's directory.

```powershell
# Run the three services locally (three terminals):
cd mcp-servers\context-mcp ; uv run uvicorn server:app --port 8002
cd mcp-servers\action-mcp  ; uv run uvicorn server:app --port 8001
cd agentic-backend         ; uv run uvicorn main:app --port 8080

# Required env before starting action-mcp:
$env:SHARED_DRIVE_ID = "<shared-drive-id>"
# Optional, to exercise /grant /revoke /list-access as yourself:
$env:BOOTSTRAP_ADMIN_EMAILS = "you@example.com"

# Iterate on a workflow agent without the webhook layer (set DEV_USER_EMAIL
# and optionally DEV_WORKFLOW=/meeting|/review|... in agentic-backend/.env):
cd agentic-backend ; uv run adk web dev/

# Break-glass access-rule CLI (same DB as the running backend):
cd agentic-backend ; uv run python -m access.manage grant /review email:alice@example.com
cd agentic-backend ; uv run python -m access.manage list /review

# Dependencies
uv sync          # install/refresh from uv.lock
uv add <pkg>     # add a dependency
```

There is **no test suite, linter, or formatter configured** in this repo —
don't claim a change is verified by tests. Verify by running the relevant
service and exercising the path.

Local state lives in `agentic-backend/sessions.db` (SQLite; holds ADK
sessions *and* the `workflow_access_rules` table). Delete the file to reset.
Defaults in `config/` point MCP URLs at `localhost:8001`/`8002` and leave
`CHAT_APP_AUDIENCE` unset (skips the JWT audience check) for local dev.

Note: use `adk web dev/` (above) to test a real workflow. There is no
root-level agent package anymore (only `main.py` lives at the backend root);
the bare `root_agent` in `clients/agent.py` has no toolsets or user identity
and is not wired for CLI discovery.

## Architecture: the parts that span files

**The dual-MCP split is the security model, not just deployment topology.**
Personal data is reachable only through Context MCP (read-only, impersonates
the user). Writes happen only through Action MCP (acts as itself, confined to
the Shared Drive). The LLM never sees the user's email — it travels as the
`X-User-Email` transport header, set by the backend after JWT verification.
A prompt-injected or hallucinating agent therefore *structurally* cannot reach
the user's personal data with write scopes, or write outside the Shared Drive.

**Request lifecycle (the `agentic-backend/chat/` package):** verify Chat JWT
(`chat/security.py`, before the body is parsed) → `chat/dispatch.py` parses the
event and dispatches by `commandId` → authorize (`access/policy.py` +
`access/store.py`) → resolve session (`sessions/`) → build a fresh agent
(`clients/agent.py`) → `chat/runner.py` runs it and posts the reply via
`chat/client.py`. The webhook **always returns `{}` synchronously** and runs
the agent inside FastAPI `BackgroundTasks`, because multi-tool agent runs blow
past Chat's ~6s "not responding" banner and 30s timeout. Slash invocations are
private in Chat and can't take threaded bot replies, so `runner._run_slash_workflow`
first posts a **public anchor message**, captures the thread Chat creates, and
uses that thread as both the ADK session key and the reply target. Plain
follow-ups (`runner._run_plain_workflow`) reply into the inbound thread.

The `chat/` package splits the concerns: `events.py` (typed payload parsing),
`formatting.py` (markdown→Chat), `dispatch.py` (routing — the public
`handle_event`), `reserved.py` (`/exit`/`/help`/admin), `runner.py` (agent
execution + reply-posting tasks), `stores.py` (the session/access singletons),
and `cards/` for the suspend/resume form UIs. **Card dispatch is registry-keyed,
not if-laddered** (`cards/registry.py`): CARD_CLICKED events route by action
function via `@register_card` (unknown functions fall back to the meeting
owner-assignment handler), and per-workflow pre-run / post-run hooks route by
`command_id` via `@register_prepare` / `@register_post` (`register_default_post`
is the meeting fallback used by workflows without their own). The runner↔cards
import cycle is broken by a lazy `from chat import cards` inside
`_run_slash_workflow`.

**Workflows (`agentic-backend/workflows/`):** one module/package per slash
command, each exporting `WORKFLOW: Workflow`. `__init__.py` imports them
explicitly into `_REGISTERED` → the `WORKFLOWS` dispatch dict (keyed by
`command_id`). `workflow.build_agent(user_email)` is an async factory the
dispatcher calls per request; it may return **any** ADK `BaseAgent`:

- Single-`LlmAgent` shape → use `llm_workflow(...)` (`_helpers.py`); the
  free-form `<default>` workflow (`_default.py`) is the one in-tree example.
- **Multi-stage engines are declared, not hand-wired**, via the composable
  engine framework in `engine/`. An engine is an `EngineSpec`
  (`spec.py`) — an ordered list of typed stage specs (`LlmStageSpec`,
  `GateStageSpec`, `FormGateStageSpec`, `SequentialStageSpec`, `LoopStageSpec`,
  `CustomStageSpec`) — that `build_engine(spec, user_email)` (`compiler.py`)
  compiles into the `SequentialAgent`. The genuinely-code parts each stage
  needs (output schemas, gate-check sets, state predicates, instruction
  providers, bespoke `BaseAgent` factories) are registered by **string key** in
  `registry.py` and referenced from the spec — so a workflow is authored as
  data plus a handful of small registered components. Two reusable stage
  primitives live here too: `FormGate` (`form_gate.py`, the generalised
  suspend/resume gate) and `IdempotentAssembler` (`assembler.py`, the
  completed-marker-guarded deterministic writer).
- The live engine packages — `meeting_engine/`, `review_board/`, `rfi_engine/`
  — are each `agents.py` (the `EngineSpec` + its registered components +
  `WORKFLOW`) + `schemas.py`, plus per-engine helpers split out as size
  warrants (`gate_checks.py`, `artifacts.py`, `research.py`). The compiled
  pipeline shape matches the docstring at the top of each `agents.py`.

`workflows/common/` holds the reusable building blocks: `gate.py` (`GateAgent`),
`loop_exit.py`, `conditional.py` (`GuardAgent` — the skip-or-delegate wrapper
shared by the engine guards), `events.py` (`model_event`), `state_parse.py`
(`coerce_model`/`coerce_model_list` for the dict|JSON|model-dump values ADK
stores), `grounding.py`, `egress.py`, `retry.py`, `state_keys.py` (canonical
`session.state` key constants — never inline these strings).

Toolset scoping is structural: a workflow declaring only `{CONTEXT}` has no
Action MCP attached to its agent at all. Splitting a multi-stage workflow into
stages with disjoint toolsets is the in-agent analogue of the dual-MCP
boundary.

**Adding a slash command is a two-place lockstep change:** (1) a workflow
module + an import line in `workflows/__init__.py`, and (2) registering the
matching numeric `command_id` in the Google Cloud console (Chat API →
Configuration → Commands). An ID in one place but not the other silently
fails. See README "Adding a slash-command workflow" and Architecture §4.6 for
the full ID table.

For a multi-stage engine, the workflow module is an `EngineSpec` plus its
registered components (see `rfi_engine/agents.py` as the worked example), and
`build_agent` is just `build_engine(SPEC, user_email)`. If a stage needs a
behaviour the framework doesn't have yet, add one reusable class in
`engine/` and reference it from the spec. Any card/form UI a workflow
needs lives in `chat/cards/` and registers itself via the `command_id`-keyed
`register_prepare` / `register_post` hooks (and `@register_card` for button
submits) — no dispatch if-ladder to edit.

**Reserved commands** (`/exit`, `/help`, `/grant`, `/revoke`, `/list-access`)
are handled inline by the dispatcher, never routed through the LLM, and have
no `WORKFLOWS` entry — but still need Cloud-console registration. Admin
commands are gated by `BOOTSTRAP_ADMIN_EMAILS`, deliberately **not** by the
`workflow_access_rules` table they manage.

**Access control** is backend-only (Chat shows every command to every user).
A workflow's `default_access` (`OPEN`/`RESTRICTED`) decides the empty-table
case; any rows in `workflow_access_rules` (email/domain kinds) are
authoritative when present. Compiled policies are cached per process for
`ACCESS_CACHE_TTL_SECONDS`.

## Non-obvious invariants (don't regress these)

These have each cost real debugging time. Architecture.md §7 has the full list.

- **`agent-backend` Cloud Run must run with `--no-cpu-throttling`.** The
  background task outlives the synchronous ack; default per-request CPU
  allocation throttles the instance the moment the ack returns and the
  outbound TLS handshake to `chat.googleapis.com` intermittently dies with
  `SSLEOFError`. The MCP services do **not** need this flag.
- **Context MCP's `UserEmailMiddleware` must stay pure ASGI**
  (`context-mcp/identity.py`), never `BaseHTTPMiddleware`. The latter wraps
  the response through `anyio`, breaking the MCP Streamable HTTP SSE channel,
  and runs the handler in a separate task so the identity `ContextVar` isn't
  visible to tools.
- **MCP toolsets pass an explicit `timeout=30.0`** to
  `StreamableHTTPConnectionParams` (`clients/agent.py`). ADK's 5s default is too tight
  for a cold-start MCP Cloud Run instance.
- **Vertex/Gemini:** set `GOOGLE_CLOUD_LOCATION` (google-genai reads that, not
  `LOCATION`). `main.py` sets `GOOGLE_GENAI_USE_VERTEXAI=1` *before* importing
  google-genai. Models run through an HTTP-retry layer for transient 429
  `RESOURCE_EXHAUSTED` (`config/` `model_retry_*`).
- **A fresh agent is built per request** so MCP transports stay short-lived
  (avoids stale ADC tokens / half-open streamable-HTTP sessions).
- **Action MCP creates Docs/Sheets via the Drive API** (`drive.files.create`
  with `parents=[shared_drive_id]`), never the native `documents.create` /
  `spreadsheets.create`, which would orphan the file in the SA's hidden root.
  Drive list/get calls always set `supportsAllDrives=True`.
- **Context MCP's user-service factories (`clients.py`) are not cached per
  user** — caching would smear identity across concurrent requests. Action
  MCP's services *are* cached (no per-user identity there).
- **`clients/mcp_client.py`** is a separate direct (non-LLM) Context MCP client for
  flows the backend must drive deterministically (e.g. the meeting pipeline's
  calendar creation). It sends the same `X-User-Email` header.

## Environment variables

Backend: `CONTEXT_MCP_SERVICE`, `ACTION_MCP_SERVICE`, `CHAT_APP_AUDIENCE`
(set in prod), `GOOGLE_CLOUD_PROJECT`, `LOCATION`/`GOOGLE_CLOUD_LOCATION`,
`SESSION_DB_URL` (Cloud SQL Postgres in prod), `SESSION_TTL_SECONDS`,
`BOOTSTRAP_ADMIN_EMAILS`, `AGENT_MODEL`, plus the `RFI_RESEARCH_*` tuning
knobs. Context MCP: `ALLOWED_HOSTS` (+ DWD config in
the Workspace admin console). Action MCP: `SHARED_DRIVE_ID` (fails loudly if
unset), `ALLOWED_HOSTS`. See `config/` and Architecture §2 for the full set.
