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

# ── RFI Response Engine ────────────────────────────────────────────────────
RFI_FILE_ID = "rfi_file_id"              # Shared-Drive file id of the uploaded RFI
RFI_FILE_NAME = "rfi_file_name"          # original attachment filename
RFI_QUESTIONS = "rfi_questions"          # list[RFIQuestion] extracted from the file
RFI_GUIDANCE = "rfi_guidance"            # RFIGuidance dict from Form 1
RFI_GUIDANCE_STATE = "rfi_guidance_state"  # "PENDING" | "RESOLVED"
RFI_GUIDANCE_CARD_MSG = "rfi_guidance_card_msg"  # Chat message.name of Form 1 card
RFI_ANSWERS = "rfi_answers"              # list[RFIAnswer] (research + human gap-fill)
RFI_GATE_VERDICT = "rfi_gate_verdict"
RFI_GATE_FAILED = "rfi_gate_failed"
RFI_GAP_STATE = "rfi_gap_state"          # "PENDING" | "RESOLVED" | "SKIPPED"
RFI_GAP_CARD_MSG = "rfi_gap_card_msg"    # Chat message.name of Form 2 card
RFI_FILLED_LINK = "rfi_filled_link"      # webViewLink of the filled response file
RFI_RESPONSE_FILE_ID = "rfi_response_file_id"  # Drive id of the "… — Sanmina Response" file
RFI_ASSEMBLY_STATUS = "rfi_assembly_status"  # contains RFI_COMPLETED_MARKER when done

# Marker stored inside RFI_ASSEMBLY_STATUS once the response file is written.
# Shared by the assembler (writer) and chat.py (resume guard) so they can't drift.
RFI_COMPLETED_MARKER = "<<STATUS:COMPLETED>>"

# ── Customer IQ Engine ─────────────────────────────────────────────────────
IQ_COMPANY_NAME = "iq_company_name"      # company name parsed from the /iq prompt
IQ_RESEARCH = "iq_research"              # free-form grounded research brief (stage 1)
IQ_PROFILE = "iq_profile"                # CustomerIQReport dict from the structuring agent (stage 2)
IQ_FILLED_LINK = "iq_filled_link"        # webViewLink of the filled dossier doc
IQ_ASSEMBLY_STATUS = "iq_assembly_status"  # contains "<<STATUS:COMPLETED>>" when done
IQ_TAILOR = "iq_tailor"                  # dict of the tailoring levers from the form
IQ_TAILOR_STATE = "iq_tailor_state"      # "PENDING" | "RESOLVED"
IQ_TAILOR_CARD_MSG = "iq_tailor_card_msg"  # Chat message.name of the tailoring card
