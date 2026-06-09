# `/meeting` — Meeting Action Engine Flowcharts

`/meeting <transcript-doc-url>` turns a meeting transcript into follow-up
artefacts: per-owner email drafts, personal calendar reminders, a tracker
sheet, and a formatted notes doc — with a human-in-the-loop gate when action
items are missing owners or due dates.

The implementation is a `SequentialAgent` (`workflows/meeting_engine/agents.py`)
whose stages mix pure-Python `BaseAgent`s (deterministic, idempotent) with two
`LlmAgent`s (the transcript parse and the notes prose). The card suspend/resume
plumbing lives in `chat.py`.

> **Why so much is deterministic.** After the owner-assignment card patches
> owners/due-dates into `MTG_PARSED`, the email/calendar/tracker artefacts are
> computed in Python — *not* by an LLM reading conversation history (where the
> owners were still null). The pure functions always reflect the patched
> object. Only the notes prose stays an LLM, and it is fed the patched JSON
> explicitly.

---

## 1. The pipeline at a glance

```mermaid
flowchart LR
    P["meeting_pipeline (SequentialAgent)"] --> A["parser<br/>ConditionalParserAgent"]
    A --> B["gate<br/>GateAgent (pure Python)"]
    B --> C["owner_gate<br/>OwnerAssignmentGate"]
    C --> D["fan_out<br/>ConditionalFanOutAgent"]
    D --> E["assembler<br/>LlmAgent"]
```

The same pipeline runs **twice** when the owner gate trips: once on the
initial invocation (suspends at the gate, posts a card) and once on card
submission (resumes with patched state). Idempotency guards in each stage
make the second pass safe.

---

## 2. End-to-end happy path vs gated path

```mermaid
flowchart TD
    START["/meeting doc-url"] --> PARSE["parser fetches doc then ParsedMeeting then MTG_PARSED"]
    PARSE --> GATE["gate writes MTG_GATE_VERDICT / MTG_GATE_FAILED"]
    GATE --> HARD{"hard BLOCKER failed?<br/>(e.g. missing source refs)"}
    HARD -->|"yes"| FAIL["assembler MODE A: 'Gate Report: FAILED' — stop"]
    HARD -->|"no"| OWN{"any item missing owner or due date?"}

    OWN -->|"yes"| PEND["owner_gate then PENDING<br/>fan-out skipped<br/>assembler MODE 0: 'form sent'"]
    PEND --> CARD["chat.py posts owner-assignment card"]
    CARD --> SUBMIT["user submits card (CARD_CLICKED)"]
    SUBMIT --> PATCH["patch MTG_PARSED owners/dates<br/>MTG_OWNER_GATE_STATE = RESOLVED<br/>clear stale fan-out keys"]
    PATCH --> RERUN["re-run pipeline"]

    OWN -->|"no"| FANOUT["fan-out + assembler MODE E"]
    RERUN --> FANOUT
    FANOUT --> ARTE["create drafts, reminders, tracker, notes"]
    ARTE --> INVITE["optional: invite-people card"]
```

---

## 3. Stage 1 — parser (`ConditionalParserAgent`)

A pure-Python guard wrapping the parsing `LlmAgent`. On a re-run the parsed
value already exists (patched by the card handler), so it is re-emitted
verbatim — the LLM never runs again and cannot overwrite the patch with the
stale null-owner parse from history.

```mermaid
flowchart TD
    PA["ConditionalParserAgent"] --> Q{"MTG_PARSED already in state?"}
    Q -->|"yes (re-run)"| RE["re-emit stored ParsedMeeting via state_delta — no LLM"]
    Q -->|"no (first run)"| LLM["meeting_parser_llm:<br/>read_my_document(doc id from URL)<br/>then structured ParsedMeeting<br/>then MTG_PARSED"]
```

> The transcript is untrusted input: the parser instruction explicitly
> extracts information *from* it and ignores any embedded instructions.

---

## 4. Stage 2 — gate (`GateAgent`, pure Python)

Runs five check functions over `MTG_PARSED`. No model calls — gate logic is
Python predicates. One BLOCKER (`sources_present`); the rest are WARNINGs that
do not stop the pipeline but drive the owner card and the notes "Flags &
Warnings" section.

```mermaid
flowchart TD
    G["GateAgent over MTG_PARSED"] --> C1["owners_assigned (WARNING)"]
    G --> C2["due_dates_set (WARNING)"]
    G --> C3["sources_present (BLOCKER)"]
    G --> C4["dates_not_stale (WARNING)"]
    G --> C5["attendees_attributed (WARNING)"]
    C1 --> V["GateVerdict then MTG_GATE_VERDICT"]
    C2 --> V
    C3 --> V
    C4 --> V
    C5 --> V
    V --> F{"any BLOCKER failed?"}
    F -->|"yes"| FF["MTG_GATE_FAILED = True"]
    F -->|"no"| FP["MTG_GATE_FAILED = False"]
```

---

## 5. Stage 3 — owner gate + suspend/resume

This is the human-in-the-loop heart of the workflow. `OwnerAssignmentGate`
can't stop the `SequentialAgent`, so it sets `MTG_OWNER_GATE_STATE = PENDING`
and relies on `ConditionalFanOutAgent` to skip the fan-out and the assembler's
MODE 0 to emit the "form sent" message.

### 5a. The gate decision

```mermaid
flowchart TD
    OG["OwnerAssignmentGate"] --> S{"MTG_OWNER_GATE_STATE"}
    S -->|"RESOLVED (card already submitted)"| PASS["passthrough then continue to fan-out"]
    S -->|"unset (first pass)"| CK{"owners_assigned OR due_dates_set failed in verdict?"}
    CK -->|"no"| PASS
    CK -->|"yes"| PEND["set MTG_OWNER_GATE_STATE = PENDING"]
    PEND --> SKIP["ConditionalFanOutAgent skips the fan-out sequence"]
    SKIP --> ASM0["assembler MODE 0: 'An owner assignment form has been sent...'"]
```

### 5b. Card post then submit then resume

The background slash runner notices `PENDING` after the run and posts the
card; the `CARD_CLICKED` handler patches state and re-runs the pipeline.

```mermaid
flowchart TD
    POST["_run_slash_workflow sees PENDING then posts owner-assignment card<br/>(per incomplete item: attendee dropdown + date picker)"] --> CLICK["user clicks Confirm / Skip then CARD_CLICKED"]
    CLICK --> H["_handle_card_clicked"]
    H --> IDEM{"session exists AND state == PENDING?"}
    IDEM -->|"no"| EXPm["'expired' / 'already processed' card — stop"]
    IDEM -->|"yes"| APPLY["_parse_form_inputs then apply owner/due-date to each ActionItem in MTG_PARSED"]
    APPLY --> PATCH["append_event state_delta:<br/>MTG_PARSED = patched<br/>MTG_OWNER_GATE_STATE = RESOLVED<br/>clear fan-out + gate keys"]
    PATCH --> RESUME["enqueue _resume_after_card"]
    RESUME --> R2["re-run pipeline:<br/>parser passthrough, gate, owner_gate RESOLVED, fan-out RUNS, assembler MODE E"]
    R2 --> REPLY["post final summary into thread + update card to 'Done'"]
```

State keys touched across the suspend/resume boundary:

| Key | Set by | Meaning |
| --- | --- | --- |
| `MTG_PARSED` | parser / card handler | structured meeting; patched in place by the card |
| `MTG_GATE_VERDICT` / `MTG_GATE_FAILED` | gate | check results; cleared before re-run |
| `MTG_OWNER_GATE_STATE` | owner gate / card handler | `PENDING` then `RESOLVED` |
| `MTG_OWNER_CARD_MSG` | slash runner | card message name, so the handler can update it |
| `MTG_EMAIL_DRAFTS` / `MTG_CALENDAR_HOLDS` / `MTG_TRACKER_ROWS` | fan-out | cleared on card submit, recomputed on re-run |
| `MTG_CALENDAR_EVENT_IDS` | calendar creator | `{action_item_id: event_id}` for the invite dialog |
| `MTG_NOTES_DOC` | notes writer | notes prose |
| `MTG_ASSEMBLY_STATUS` | assembler | ends with `<<STATUS:COMPLETED>>` |

---

## 6. Stage 4 — fan-out (`ConditionalFanOutAgent`)

Skipped entirely while the owner gate is `PENDING`. Otherwise it runs a small
`SequentialAgent`: deterministic compute then deterministic calendar creation
then LLM notes prose.

```mermaid
flowchart TD
    FO["ConditionalFanOutAgent"] --> Q{"MTG_OWNER_GATE_STATE == PENDING?"}
    Q -->|"yes"| SK["skip — yield notice (lets card flow proceed)"]
    Q -->|"no"| SEQ["meeting_fan_out (SequentialAgent)"]

    SEQ --> CO["MeetingFanOutAgent (pure Python, over patched MTG_PARSED)"]
    CO --> CO1["_build_email_drafts then MTG_EMAIL_DRAFTS<br/>(one email per owner, resolved recipient)"]
    CO --> CO2["_build_calendar_holds then MTG_CALENDAR_HOLDS<br/>(09:00 reminder per dated item, no attendees)"]
    CO --> CO3["_build_tracker_rows then MTG_TRACKER_ROWS"]

    SEQ --> CC["CalendarCreatorAgent (pure Python)"]
    CC --> CCg{"guards: PENDING? GATE_FAILED? already created?"}
    CCg -->|"any true"| CCs["skip (idempotent / no events on a blocked run)"]
    CCg -->|"all clear"| CCr["create_calendar_event per hold<br/>then MTG_CALENDAR_EVENT_IDS {item_id: event_id}"]

    SEQ --> NW["notes_writer (LlmAgent)<br/>fed patched ParsedMeeting JSON then MTG_NOTES_DOC"]
```

Calendar reminders are created here (deterministically), **not** by the
assembler — so the `{action_item_id: event_id}` map is reliably captured for
the later invite dialog. They are personal (attendee-less) by default;
inviting people is an explicit opt-in step (§8).

---

## 7. Stage 5 — assembler (`LlmAgent`, four modes)

The only stage that writes to Workspace. It branches on session state before
doing anything, so re-runs and blocked runs short-circuit cleanly.

```mermaid
flowchart TD
    AS["meeting_assembler"] --> M{"session state"}
    M -->|"MTG_OWNER_GATE_STATE == PENDING"| M0["MODE 0: 'form sent, will continue' — no tools"]
    M -->|"MTG_GATE_FAILED == True"| MA["MODE A: 'Gate Report: FAILED' + blockers — no tools"]
    M -->|"MTG_ASSEMBLY_STATUS has STATUS:COMPLETED"| MB["MODE B: 'already complete, use /exit' — no tools"]
    M -->|"otherwise"| ME["MODE E: create artifacts"]

    ME --> ME1["create_gmail_draft per EmailDraft<br/>(skip empty 'to')"]
    ME --> ME2["create_spreadsheet (if needed) + append_rows<br/>from MTG_TRACKER_ROWS"]
    ME --> ME3["create_document + append_markdown notes<br/>+ append_markdown 'Flags & Warnings'"]
    ME1 --> SUM["summary of what was created"]
    ME2 --> SUM
    ME3 --> SUM
    SUM --> DONE["end with line: <<STATUS:COMPLETED>>"]
```

> The assembler is told **not** to create calendar events — those already
> exist from the fan-out. It only drafts emails, writes the tracker, and
> writes the notes doc.

---

## 8. Post-completion — optional calendar invites

Reminders are personal by default. After a successful run that created
events, the bot offers a card to invite people; selections are resolved
(org directory + free-text emails) and patched onto the existing events.

```mermaid
flowchart TD
    DONE["pipeline complete AND MTG_CALENDAR_EVENT_IDS present"] --> IC["_post_invite_card_if_ready then 'Invite people?' prompt card"]
    IC --> OPEN["user clicks 'Configure invites' then open_invite_dialog (OPEN_DIALOG)"]
    OPEN --> DLG["dialog, per dated item:<br/>attendee checkboxes<br/>org people search (USER data source)<br/>free-text 'other emails'"]
    DLG --> SUB["'Send invites' then submit_invites"]
    SUB --> CHK{"any people selected?"}
    CHK -->|"no"| NOOP["toast: nothing to invite"]
    CHK -->|"yes"| AP["_apply_invites (background)"]
    AP --> RES["resolve_people_emails for org picks (People API)"]
    RES --> UPD["update_calendar_event_attendees per event_id"]
    UPD --> CONF["post summary into the thread"]
```

---

## Related

- [`Workflow-Engine.md`](./Workflow-Engine.md) — the dispatch, authorization,
  background-task, and session machinery that hosts this workflow.
- [`Architecture.md`](./Architecture.md) — system architecture and the
  dual-MCP security model the artefact writes depend on.
- Source: `agentic-backend/workflows/meeting_engine/agents.py` (pipeline),
  `schemas.py` (data shapes), and the card handlers in `agentic-backend/chat.py`.
