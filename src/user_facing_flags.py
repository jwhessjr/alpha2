"""
Formats an OwnershipAssessment into user-facing output. Gap #3 fix: the
prior session's production note collapsed CHTR's specific caveats into
"4 unresolved item(s)" — this revision prints the ACTUAL caveat text,
not a count, in both the short report-surface form and the structured
API form. Wording is kept concise (each caveat is already a single
sentence in curated_evidence.py) rather than verbose, but nothing is
hidden behind a number.

Core design rule, unchanged: an ABSENT status (no assessment run) is
never rendered as blank — always an explicit "not yet screened" state.
NEW this revision: an assessment FAILURE (exception during fetch/assess)
is a third, equally explicit state — OWNERSHIP_SCREEN_ERROR — distinct
from both "not screened" and every real outcome, per gap #2's failure
policy.
"""
from __future__ import annotations

from typing import Optional

from assessment_engine import OverallReadiness, OwnershipAssessment

NOT_SCREENED_LABEL = "NOT YET SCREENED"
NOT_SCREENED_EXPLANATION = (
    "This ticker has not been run through the Phase 1 ownership-controls screen. "
    "This is NOT a confirmation that its ownership structure is clean — it means "
    "no assessment exists yet. Treat identically to a ticker flagged for review "
    "until screened."
)

ERROR_LABEL = "OWNERSHIP_SCREEN_ERROR"
ERROR_EXPLANATION_TEMPLATE = (
    "The ownership-control screen failed to run for this ticker ({detail}). This is NOT "
    "a clean result and NOT equivalent to 'not yet screened' — it means an attempt was "
    "made and failed. The underlying numeric valuation is UNAFFECTED (this screen never "
    "touches it), but the ownership status itself is unresolved pending investigation."
)

_READINESS_DISPLAY = {
    OverallReadiness.READY_NO_CLAIM: ("CLEAN — no NCI claim", "No noncontrolling-interest claim reported (confirmed explicit zero on both fields)."),
    OverallReadiness.READY_IMMATERIAL: ("CLEAN — immaterial (documented)", "NCI claim confirmed, filing-verified, below the materiality threshold, AND a documented economic-claim assessment supports immateriality (see basis/reasons below)."),
    OverallReadiness.SUPPORTED_BRIDGE_WACC_EXCEPTION_PRESENT: ("PARTIAL — bridge OK, WACC/forecast caveats remain", "The ownership-to-equity bridge itself is calculable and has been computed, but the discount rate and/or operating forecast carry known, separate, unresolved issues. Do NOT read this as a clean valuation."),
    OverallReadiness.NOT_READY_REVIEW_REQUIRED: ("NOT READY — review required", "One or more conditions require a human filing check before this ticker's ownership treatment can be trusted."),
    OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE: ("NOT READY — insufficient evidence", "Database-screened only; ownership structure has not been filing-verified."),
    OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED: ("EXCLUDED — data quality", "Stale, quarantined, identity-ambiguous, or deregistered — not assessable at all right now."),
    OverallReadiness.NOT_READY_MATERIALITY_ANALYSIS_PENDING: ("NOT READY — materiality analysis pending", "Book-value ratio is small and structure is filing-verified, but NO documented economic-claim assessment exists. A small ratio alone does not establish immateriality."),
    OverallReadiness.NOT_READY_REVALIDATION_REQUIRED: ("NOT READY — revalidation required", "Curated structural evidence exists but a change in current data (newer filing, changed claim balance, or an identity/quarantine event) means it cannot be relied on as-is."),
}


def format_for_report_surface(a: Optional[OwnershipAssessment], error: Optional[str] = None) -> str:
    """The short, single-line form for the existing notes-chain surface."""
    if error is not None:
        return f"[{ERROR_LABEL}] {ERROR_EXPLANATION_TEMPLATE.format(detail=error)}"
    if a is None:
        return f"[{NOT_SCREENED_LABEL}] {NOT_SCREENED_EXPLANATION}"
    label, explanation = _READINESS_DISPLAY[a.overall_readiness]
    parts = [f"[{label}]"]
    if a.review_reasons:
        parts.append("Review: " + "; ".join(a.review_reasons))
    if a.revalidation_triggers:
        parts.append("Revalidation triggers: " + "; ".join(a.revalidation_triggers))
    # Gap #3 fix: print the ACTUAL caveats, not a count.
    for caveat in a.preserved_caveats:
        parts.append(caveat)
    parts.append(f"(evidence as of {a.source_as_of or 'unknown date'}"
                  + (f"; curated structural review effective {a.curated_effective_date}" if a.curated_effective_date else "")
                  + ")")
    return " ".join(parts)


def format_for_api(a: Optional[OwnershipAssessment], error: Optional[str] = None) -> dict:
    """The structured form — every axis kept separate."""
    if error is not None:
        return {
            "screened": True, "failed": True,
            "display_label": ERROR_LABEL,
            "explanation": ERROR_EXPLANATION_TEMPLATE.format(detail=error),
            "error_detail": error,
            "evidence_status": None, "calculability": None, "materiality": None,
            "review_required": None, "wacc_status": None, "overall_readiness": None,
            "preserved_caveats": [], "source_as_of": None,
        }
    if a is None:
        return {
            "screened": False, "failed": False,
            "display_label": NOT_SCREENED_LABEL,
            "explanation": NOT_SCREENED_EXPLANATION,
            "evidence_status": None, "calculability": None, "materiality": None,
            "review_required": None, "wacc_status": None, "overall_readiness": None,
            "preserved_caveats": [], "source_as_of": None,
        }
    label, explanation = _READINESS_DISPLAY[a.overall_readiness]
    return {
        "screened": True, "failed": False,
        "display_label": label,
        "explanation": explanation,
        "evidence_status": a.evidence_status.value,
        "calculability": a.calculability.value,
        "calculable_method": a.calculable_method,
        "calculability_reasons": a.calculability_reasons,
        "materiality": a.materiality.value,
        "materiality_basis": a.materiality_basis,
        "materiality_reasons": a.materiality_reasons,
        "review_required": a.review_required,
        "review_reasons": a.review_reasons,
        "wacc_status": a.wacc_status.value,
        "wacc_reasons": a.wacc_reasons,
        "overall_readiness": a.overall_readiness.value,
        "preserved_caveats": a.preserved_caveats,  # full text, never a count
        "revalidation_triggers": a.revalidation_triggers,
        "source_as_of": a.source_as_of,
        "curated_effective_date": a.curated_effective_date,
        "curated_scope": a.curated_scope,
        # Deliberately NOT included: any numeric "corrected" share value or
        # margin-of-safety — ownership readiness stays fully distinct from
        # overall investment suitability and from any numeric output.
    }
