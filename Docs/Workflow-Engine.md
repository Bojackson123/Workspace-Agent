# Workflow Engine — Flowcharts

How the slash-command workflow engine behaves end to end: from an inbound
Google Chat event, through dispatch and authorization, to the background
agent run that posts the reply. This is the companion "how it flows" view
of the **Workflow engine** section in [`Architecture.md`](./Architecture.md);
read that for the prose rationale and the security invariants.

The engine itself is intentionally small: it looks up a `Workflow` by
`command_id`, asks it to build an ADK agent, runs whatever comes back, and
posts the result asynchronously. Everything below is that loop drawn out.

---

## 1. Request lifecycle (top level)

Every request enters through the single FastAPI POST endpoint. The JWT is
verified *before* the body is parsed; only then does `handle_event` route by
event type.

```mermaid
flowchart TD
    A["Google Chat event"] -->|"HTTPS POST + OIDC bearer"| B["FastAPI endpoint  /"]
    B --> C{"verify_chat_jwt"}
    C -->|"invalid token / wrong aud / wrong email"| C1["401 — body never parsed"]
    C -->|"email == chat@system.gserviceaccount.com"| D["handle_event"]
    D --> E{"event_type"}
    E -->|"ADDED_TO_SPACE"| F["return WELCOME_MESSAGE (sync)"]
    E -->|"MESSAGE"| G["_handle_message"]
    E -->|"CARD_CLICKED"| H["_handle_card_clicked"]
    E -->|"other"| I["'not supported' (sync)"]
```

---

## 2. MESSAGE dispatch decision tree

`_handle_message` separates the fast synchronous paths (reserved commands)
from the LLM paths (slash + plain), which always return an empty envelope and
do their work in a `BackgroundTasks` job.

```mermaid
flowchart TD
    M["_handle_message"] --> U{"user_email present?"}
    U -->|"no"| Uerr["return error text (sync)"]
    U -->|"yes"| R{"slash_command_id?"}

    R -->|"/exit (999)"| EX["_handle_exit: clear session then sync reply"]
    R -->|"/help (998)"| HE["_handle_help: list commands then sync reply"]
    R -->|"/grant /revoke /list-access (995-997)"| AD["_handle_admin_command"]
    R -->|"other slash id"| SL["workflow lookup"]
    R -->|"none — plain message"| PL["session resolve by thread"]

    AD --> ADa{"bootstrap admin? (env var)"}
    ADa -->|"no"| ADb["denied (audit-logged) then sync reply"]
    ADa -->|"yes"| ADc["mutate workflow_access_rules then sync reply"]

    SL --> SLa{"workflow found?"}
    SLa -->|"no"| SLb["'Unknown slash command' sync reply"]
    SLa -->|"yes"| SLc{"authorize: allowed?"}
    SLc -->|"no"| SLd["access-denied reply (audit-logged)"]
    SLc -->|"yes"| SLe["enqueue _run_slash_workflow then return empty {}"]

    PL --> PLa["_workflow_for(active_workflow_id)"]
    PLa --> PLb["enqueue _run_plain_workflow then return empty {}"]
```

Reserved commands (`/exit`, `/help`, and the three admin commands) never
touch the LLM and never use the background path — their replies are static
and return straight from the webhook.

---

## 3. The two background paths

The webhook has already returned. These run after the response is sent, and
post their results back via the Chat REST API. CPU must stay allocated
(`--no-cpu-throttling`) so these outbound TLS handshakes don't stall.

### 3a. Slash invocation — the "Option C" public anchor flow

A slash invocation is private to the invoker, and Chat rejects threaded bot
replies into a private message. So the bot posts a *new public anchor
message*, captures the thread Chat creates for it, and uses that thread as
both the session key and the reply target for everything that follows.

```mermaid
flowchart TD
    S["_run_slash_workflow"] --> S1["post public anchor message in space (no thread target)"]
    S1 --> S2{"anchor thread.name captured?"}
    S2 -->|"no"| S2e["log error, abort"]
    S2 -->|"yes"| S3["resolve FRESH ADK session keyed by anchor thread, scoped to workflow"]
    S3 --> S4["_run_agent: build_agent_for_workflow then Runner.run_async"]
    S4 --> S5{"owner gate left state == PENDING? (/meeting only)"}
    S5 -->|"yes"| S6["post owner-assignment card into anchor thread"]
    S5 -->|"no"| S7["_markdown_to_chat then post final reply into anchor thread"]
    S7 --> S8["_post_invite_card_if_ready (if calendar events were created)"]
```

### 3b. Plain continuation — reply directly in the existing thread

A follow-up typed inside an existing thread already has a usable
`thread.name`, so no anchor dance is needed.

```mermaid
flowchart TD
    P["_run_plain_workflow"] --> P1["_run_agent against the resolved session"]
    P1 --> P2["_markdown_to_chat then post reply with thread.name = inbound message_thread"]
```

Both paths converge on `_run_agent`, which builds the workflow's agent fresh,
drives one prompt through `Runner.run_async`, logs each ADK event
(tool calls, tool responses, state deltas), and returns the final text.

---

## 4. Registry & agent build

One module per command exposes a `WORKFLOW` constant. The package `__init__`
imports them explicitly into `_REGISTERED`, validates uniqueness, and indexes
by `command_id`. The dispatcher never knows *which* ADK shape a workflow
builds — that's the workflow's own choice.

```mermaid
flowchart LR
    subgraph reg["workflows package (explicit registry)"]
      direction TB
      L["_REGISTERED list"] --> BR["_build_registry (dedupe command_id)"]
      BR --> DICT["WORKFLOWS: dict of command_id to Workflow"]
    end

    DISP["dispatcher (chat.py)"] -->|"get_workflow(command_id)"| DICT
    DICT --> WF["Workflow dataclass<br/>command_id · command_name · description<br/>default_access · build_agent · ack_message"]
    WF -->|"await build_agent(user_email)"| SHAPE{"agent shape (workflow's choice)"}
    SHAPE --> A1["single LlmAgent<br/>(via llm_workflow helper — the &lt;default&gt; workflow)"]
    SHAPE --> A2["SequentialAgent compiled from an EngineSpec<br/>(/meeting · /review · /rfi)"]
    SHAPE --> A3["custom BaseAgent subclass"]

    WF -.->|"declares toolsets"| T1["Context MCP<br/>(read-only, DWD-impersonated)"]
    WF -.->|"declares toolsets"| T2["Action MCP<br/>(read/write, acts as itself)"]
```

A workflow scoped to one toolset is *structurally* incapable of using the
other — the unused MCP is never attached to the agent.

---

## 4b. Composable engine (`EngineSpec`)

The three multi-stage workflows aren't hand-wired — each is declared as an
`EngineSpec` (`engine/spec.py`): an ordered list of typed stage specs.
`build_engine(spec, user_email)` (`compiler.py`) compiles it into the
`SequentialAgent` the dispatcher runs. Stages name their code dependencies by
string key into `engine/registry.py`, so the structure + prompts read
as data while schemas, gate checks, predicates, instruction providers and
bespoke agents stay in code.

```mermaid
flowchart LR
    SPEC["EngineSpec<br/>(ordered stage specs)"] --> CMP["build_engine(spec, user_email)"]
    CMP --> SEQ["SequentialAgent"]

    subgraph kinds["stage kinds → compiled agent"]
      direction TB
      K1["LlmStageSpec → LlmAgent<br/>(optionally GuardAgent-wrapped)"]
      K2["GateStageSpec → GateAgent"]
      K3["FormGateStageSpec → FormGate<br/>(suspend/resume)"]
      K4["SequentialStageSpec → nested SequentialAgent"]
      K5["LoopStageSpec → LoopAgent + LoopExitChecker"]
      K6["CustomStageSpec → registered BaseAgent factory"]
    end

    CMP -.-> kinds
    REG["registry.py<br/>schemas · checks · predicates · instructions · agents"] -.->|"resolved by key"| CMP
```

Two reusable primitives live alongside the compiler: `FormGate` (the
generalised suspend/resume gate that replaced the per-engine guidance/gap/owner
gates) and `IdempotentAssembler` (the completed-marker-guarded deterministic
writer). Adding a workflow = write a spec + register any new component; adding a
*capability* the framework lacks = one new stage class, reused thereafter.

---

## 5. Access control

Workflow *definitions* live in code; *who may invoke them* lives in the
database. `authorize` combines the table rules with the workflow's
code-declared `default_access` (the empty-table behaviour).

```mermaid
flowchart TD
    AZ["authorize(user_email, command_id, default_access, store)"] --> Q{"rules in table for this command?"}
    Q -->|"none"| D{"default_access"}
    D -->|"OPEN"| ALLOW["ALLOW"]
    D -->|"RESTRICTED"| DENY["DENY"]
    Q -->|"one or more"| EV{"any email OR domain rule matches caller?"}
    EV -->|"yes"| ALLOW
    EV -->|"no"| DENY
    ALLOW --> RUN["run workflow"]
    DENY --> REJ["audit WARNING + user-facing rejection"]
```

| `default_access` | rules in table | outcome        |
| ---------------- | -------------- | -------------- |
| `OPEN`           | none           | allow          |
| `OPEN`           | one or more    | evaluate rules |
| `RESTRICTED`     | none           | deny           |
| `RESTRICTED`     | one or more    | evaluate rules |

The three admin commands (`/grant`, `/revoke`, `/list-access`) are a separate
gate entirely: governed by `BOOTSTRAP_ADMIN_EMAILS` env var, **not** by the
rules table they manage — so a corrupted table can never lock admins out.

---

## 6. Session lifecycle (multi-turn)

Chat marks `slashCommand` only on the first message; follow-ups are plain
`MESSAGE` events. Sessions are keyed by `(user_email, sha256(thread.name))`
and carry the active workflow's `command_id` in `Session.state`, so a thread
stays inside its workflow across turns.

```mermaid
flowchart TD
    IN["inbound message"] --> K["key = (user_email, sha256(thread.name))"]
    K --> Q{"new_workflow_id set? (i.e. a slash command)"}
    Q -->|"yes"| DEL["delete any existing session at this key"]
    DEL --> NEW["create FRESH session scoped to the new workflow"]
    Q -->|"no (plain continuation)"| EX{"existing session?"}
    EX -->|"yes, within TTL"| REUSE["reuse — inherit active_workflow_id"]
    EX -->|"expired (idle > SESSION_TTL_SECONDS)"| EXP["delete then one-shot DEFAULT_WORKFLOW session"]
    EX -->|"none"| ONE["create one-shot DEFAULT_WORKFLOW session"]
```

Because a slash command always deletes and recreates the session, workflow
boundaries reset conversation history: `/review`'s prompt never inherits
`/meeting`'s tool-call history. `/exit` simply deletes the thread's session.

---

## Related

- [`Architecture.md`](./Architecture.md) — full system architecture, cloud
  resources, IAM, and the operating invariants.
- [`Meeting-Workflow.md`](./Meeting-Workflow.md) — the `/meeting` pipeline
  (`SequentialAgent` with a gate, a suspend/resume card, and deterministic
  fan-out) drawn out in detail.
