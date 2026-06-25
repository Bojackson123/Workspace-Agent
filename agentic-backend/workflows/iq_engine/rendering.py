"""Dossier rendering and tailoring helpers for the Customer IQ engine.

Holds the Sanmina capability catalogue (shared with the /iq tailoring
card), the ``{{placeholder}}`` mapping the assembler fills, and the
tailoring-block renderer that steers the research/structuring prompts.
"""

from __future__ import annotations

from workflows.common.state_keys import IQ_TAILOR
from workflows.iq_engine.schemas import CustomerIQReport

# Sanmina capabilities — single source of truth shared by the /iq tailoring
# card's "segment lens" options (the chat layer) and the tailoring instruction
# block. Each entry is (form_value, human_label).
SANMINA_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("pcba", "PCB assembly (PCBA)"),
    ("system_assembly", "System / box-build assembly"),
    ("jdm", "JDM & design services"),
    ("enclosures", "Enclosures & precision machining"),
    ("optical_rf", "Optical & RF modules"),
    ("cable_backplane", "Cable & backplane"),
    ("defense_aerospace", "Defense & aerospace (ITAR / AS9100)"),
    ("medical", "Medical (ISO 13485)"),
    ("automotive", "Automotive (IATF 16949)"),
    ("cloud_datacenter", "Cloud & data-center infrastructure"),
    ("supply_chain", "Supply-chain management"),
    ("test_repair", "Test & repair"),
)
_CAPABILITY_LABELS = dict(SANMINA_CAPABILITIES)

# Human-readable framing for the "purpose / audience" lever.
_PURPOSE_GUIDANCE = {
    "cold_outreach": (
        "Cold-outreach prep — keep it crisp and action-oriented; emphasise the "
        "opening angle and concrete next steps."
    ),
    "qbr": (
        "QBR / account-review prep — emphasise current state, recent changes, "
        "and strategic context."
    ),
    "exec_briefing": (
        "Executive briefing — lead with the headline verdict; concise and "
        "high-level, lighter on granular detail."
    ),
}

# Human-readable labels for SourceRef.kind, shown in the dossier's Sources list.
_SOURCE_KIND_LABELS = {
    "web": "Web",
    "drive_file": "Drive File",
    "doc_span": "Doc Span",
    "sheet_cell": "Sheet Cell",
    "transcript_span": "Transcript",
}


def _text(value: str) -> str:
    """Render a scalar field; fall back to an em dash when empty."""
    return value.strip() if value and value.strip() else "—"


def _bullets(items: list[str]) -> str:
    """Render a string list as a ``•``-prefixed block (one item per line)."""
    cleaned = [i.strip() for i in items if i and i.strip()]
    return "\n".join(f"• {i}" for i in cleaned) if cleaned else "None identified."


def _placeholders(report: CustomerIQReport, generated_date: str) -> dict[str, str]:
    """Map every ``{{token}}`` to its filled value for the assembler.

    List fields become ``•``-prefixed text blocks (plain lines, not native Docs
    bullets — the trade-off of the copy-template + replace_text approach).
    """
    contacts = [
        "• "
        + c.name
        + (f" — {c.title}" if c.title else "")
        + (f" ({c.note})" if c.note else "")
        for c in report.key_contacts
        if c.name.strip()
    ]
    sources = [
        f"• [{_SOURCE_KIND_LABELS.get(s.kind, s.kind)}] {s.locator}"
        + (f" — {s.quote}" if s.quote else "")
        for s in report.sources
        if s.locator.strip()
    ]
    return {
        "{{company_name}}": _text(report.company_name),
        "{{generated_date}}": generated_date,
        "{{executive_summary}}": _text(report.executive_summary),
        "{{fit_tier}}": _text(report.fit.tier),
        "{{recommended_segment}}": _text(report.fit.recommended_segment),
        "{{fit_rationale}}": _text(report.fit.rationale),
        "{{legal_name}}": _text(report.legal_name),
        "{{headquarters}}": _text(report.headquarters),
        "{{founded}}": _text(report.founded),
        "{{ownership}}": _text(report.ownership),
        "{{website}}": _text(report.website),
        "{{employee_count}}": _text(report.employee_count),
        "{{revenue}}": _text(report.revenue),
        "{{business_description}}": _text(report.business_description),
        "{{products}}": _bullets(report.products),
        "{{end_markets}}": _bullets(report.end_markets),
        "{{manufacturing_footprint}}": _text(report.manufacturing_footprint),
        "{{current_ems_providers}}": _bullets(report.current_ems_providers),
        "{{opportunity_signals}}": _bullets(report.opportunity_signals),
        "{{compliance_needs}}": _bullets(report.compliance_needs),
        "{{competitors}}": _bullets(report.competitors),
        "{{key_contacts}}": "\n".join(contacts) if contacts else "None identified.",
        "{{recent_news}}": _bullets(report.recent_news),
        "{{risk_flags}}": _bullets(report.risk_flags),
        "{{recommended_next_steps}}": _bullets(report.recommended_next_steps),
        "{{sources}}": "\n".join(sources) if sources else "None cited.",
    }


def _tailoring_block(state: dict, stage: str) -> str:
    """Render the caller's tailoring selections into an instruction block.

    ``stage`` is ``"research"`` (segment lens as research focus, account context,
    geography, data-source directive) or ``"structuring"`` (purpose/audience tone
    and the segment-lens bias for the fit verdict). Returns ``""`` when nothing
    was selected so the prompt stays at its default behaviour.
    """
    tailor = state.get(IQ_TAILOR) or {}
    if not tailor:
        return ""

    lines: list[str] = []
    segments = [s for s in (tailor.get("segments") or []) if s]
    seg_labels = [_CAPABILITY_LABELS.get(s, s) for s in segments]

    if stage == "research":
        if seg_labels:
            lines.append(
                "- Segment lens: the caller cares about fit for these Sanmina "
                "capabilities — " + ", ".join(seg_labels) + ". Bias the research "
                "toward evidence that confirms or rules out that fit (relevant "
                "products, certifications, programmes, incumbents)."
            )
        context = (tailor.get("context") or "").strip()
        if context:
            lines.append(
                "- Known account context from the rep (treat as a lead to verify "
                f"with your tools, not as fact): {context}"
            )
        geo = (tailor.get("geo") or "").strip()
        if geo:
            lines.append(
                "- Geographic focus: emphasise the company's operations, "
                f"manufacturing footprint, and outreach routing in/around {geo}."
            )
        sources = (tailor.get("sources") or "").strip()
        if sources == "web_only":
            lines.append(
                "- Data sources: use ONLY public web search (google_search). Do "
                "NOT call search_drive or read_document."
            )
        elif sources == "drive_only":
            lines.append(
                "- Data sources: use ONLY Sanmina's Shared Drive (search_drive / "
                "read_document). Do NOT call google_search."
            )
    else:  # structuring
        purpose = (tailor.get("purpose") or "").strip()
        guidance = _PURPOSE_GUIDANCE.get(purpose)
        if guidance:
            lines.append(f"- Purpose / audience: {guidance}")
        if seg_labels:
            lines.append(
                "- Segment lens: judge fit.tier through these Sanmina "
                "capabilities — " + ", ".join(seg_labels) + " — and prefer one of "
                "them for fit.recommended_segment. If the company is a poor fit "
                "for all of them, say so plainly rather than forcing a match."
            )

    if not lines:
        return ""
    return "CALLER TAILORING (honour these):\n" + "\n".join(lines)
