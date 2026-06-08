"""Canonical session.state key constants.

Import these everywhere state is read or written — never use inline strings.
"""

# ── Meeting Engine ─────────────────────────────────────────────────────────
MTG_PARSED = "mtg_parsed"
MTG_EMAIL_DRAFTS = "mtg_email_drafts"
MTG_CALENDAR_HOLDS = "mtg_calendar_holds"
MTG_TRACKER_ROWS = "mtg_tracker_rows"
MTG_NOTES_DOC = "mtg_notes_doc"
MTG_GATE_VERDICT = "mtg_gate_verdict"
MTG_GATE_FAILED = "mtg_gate_failed"
MTG_ASSEMBLY_STATUS = "mtg_assembly_status"
MTG_OWNER_GATE_STATE = "mtg_owner_gate_state"   # "PENDING" | "RESOLVED"
MTG_OWNER_CARD_MSG = "mtg_owner_card_msg"        # Chat message.name of the posted card
MTG_CALENDAR_EVENT_IDS = "mtg_calendar_event_ids"  # {action_item_id: calendar event id}

# ── Review Board ───────────────────────────────────────────────────────────
RVW_FILL_CONTRACT = "rvw_fill_contract"
RVW_SECTIONS = "rvw_sections"
RVW_LEDGER = "rvw_ledger"
RVW_GATE_VERDICT = "rvw_gate_verdict"
RVW_GATE_FAILED = "rvw_gate_failed"
