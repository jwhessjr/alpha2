"""
Stage V ownership-controls, revised assessment engine.

Fixes applied this revision (see 00_decision_memo.md for full review
findings):

  Gap #1: `assess()` now takes CURRENT observations (OwnershipProfile) and
  CURATED structural evidence (Optional[CuratedStructuralEvidence]) as two
  SEPARATE parameters. A new `revalidate()` function runs first and can
  downgrade evidence_status to NEEDS_REVALIDATION — this is the only path
  by which curated evidence is ever used, and it is never allowed to
  override a current-data-quality exclusion gate (stale/quarantined/
  identity-ambiguous/deregistered always run against CURRENT fields only).

  Gap #4: SIMPLE_SUBSIDIARY no longer produces CALCULABLE via an
  unsupported "book-value subtraction is a defensible default" shortcut —
  removed entirely; it is NOT_CALCULABLE ("no approved valuation method")
  unless a documented, filing-verified MaterialityAssessment exists, in
  which case the claim can be assessed IMMATERIAL (never CALCULABLE via a
  default method — immateriality and calculability remain independent
  even here). A small book ratio alone, even on a filing-verified
  structure, now yields PENDING_ANALYSIS, not IMMATERIAL, unless that
  MaterialityAssessment is present.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from ownership_model import (
    CuratedStructuralEvidence,
    DataStatus,
    EvidenceStatus,
    OwnershipProfile,
    OwnershipStructureType,
)

# ---------------------------------------------------------------------------
# UNAPPROVED POLICY — proposed thresholds, for review, not yet authorized.
# ---------------------------------------------------------------------------

MATERIALITY_THRESHOLD_RATIO_TO_MKTCAP = 0.02
MANDATORY_REVIEW_RATIO_TO_MKTCAP = 2.0
STALE_BALANCE_SHEET_DAYS = 730

#: NEW (gap #1). If the CURRENT balance sheet postdates the curated
#: evidence's effective_date by more than this many days, revalidation is
#: required — roughly two filing cycles, chosen so routine quarterly
#: rollover within a cycle doesn't thrash, but a skipped cycle does
#: trigger a fresh look. CONFIGURABLE. UNAPPROVED.
REVALIDATION_BALANCE_SHEET_DRIFT_DAYS = 190

#: NEW (gap #1). A current NCI component value differing from its
#: at-curation value by more than this relative amount triggers
#: revalidation — "changed claims cannot inherit an old immateriality
#: clearance," per instruction. CONFIGURABLE. UNAPPROVED.
REVALIDATION_CLAIM_CHANGE_THRESHOLD = 0.10


class CalculabilityStatus(Enum):
    CALCULABLE = "calculable"
    NOT_CALCULABLE = "not_calculable"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE_NO_CLAIM = "not_applicable_no_claim"


class MaterialityStatus(Enum):
    IMMATERIAL = "immaterial"
    MATERIAL = "material"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE_NO_CLAIM = "not_applicable_no_claim"
    PENDING_ANALYSIS = "pending_analysis"
    """NEW (gap #4): ratio suggests possible immateriality AND structure is
    filing-verified, but no documented MaterialityAssessment (rationale +
    bound + evidence + date) exists. This is NOT the same as INDETERMINATE
    (which means we can't even compute the ratio) — PENDING_ANALYSIS means
    we have every input except the one piece of judgment that would let us
    actually clear the claim, and we refuse to invent it."""


class WaccStatus(Enum):
    CONSISTENT = "consistent"
    EXCEPTION_NCI_EXCLUDED = "exception_nci_excluded"
    UNRESOLVED = "unresolved"
    NOT_APPLICABLE_NO_CLAIM = "not_applicable_no_claim"


class OverallReadiness(Enum):
    READY_NO_CLAIM = "ready_no_claim"
    READY_IMMATERIAL = "ready_immaterial"
    SUPPORTED_BRIDGE_WACC_EXCEPTION_PRESENT = "supported_bridge_wacc_exception_present"
    NOT_READY_REVIEW_REQUIRED = "not_ready_review_required"
    NOT_READY_INSUFFICIENT_EVIDENCE = "not_ready_insufficient_evidence"
    NOT_READY_DATA_QUALITY_EXCLUDED = "not_ready_data_quality_excluded"
    NOT_READY_MATERIALITY_ANALYSIS_PENDING = "not_ready_materiality_analysis_pending"
    NOT_READY_REVALIDATION_REQUIRED = "not_ready_revalidation_required"
    """NEW (gap #1): curated evidence exists but a revalidation trigger
    fired. Distinct from NOT_READY_REVIEW_REQUIRED so a reviewer can tell
    "never looked at" / "looked at, needs review for a structural reason"
    apart from "was looked at, but something changed since — check it was
    still true, likely a quick confirmation rather than a fresh full
    review.\""""


@dataclass
class OwnershipAssessment:
    ticker: str
    evidence_status: EvidenceStatus
    calculability: CalculabilityStatus
    calculable_method: Optional[str] = None
    calculability_reasons: list[str] = field(default_factory=list)
    materiality: MaterialityStatus = MaterialityStatus.INDETERMINATE
    materiality_basis: str = ""
    materiality_reasons: list[str] = field(default_factory=list)
    review_required: bool = False
    review_reasons: list[str] = field(default_factory=list)
    wacc_status: WaccStatus = WaccStatus.UNRESOLVED
    wacc_reasons: list[str] = field(default_factory=list)
    overall_readiness: OverallReadiness = OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE
    preserved_caveats: list[str] = field(default_factory=list)
    source_as_of: Optional[str] = None
    revalidation_triggers: list[str] = field(default_factory=list)
    curated_effective_date: Optional[str] = None
    curated_scope: Optional[str] = None


def revalidate(profile: OwnershipProfile, curated: Optional[CuratedStructuralEvidence]) -> tuple[EvidenceStatus, list[str]]:
    """Gap #1's core mechanism. Returns the EFFECTIVE evidence status to
    use for this assessment, plus the list of trigger reasons if any
    fired. Never mutates `curated` — its own stored status/provenance is
    untouched; this only decides whether it's safe to RELY on right now."""
    if curated is None:
        return EvidenceStatus.DATABASE_SCREENED_ONLY, []

    triggers = []

    if profile.balance_sheet_as_of and curated.effective_date:
        try:
            import datetime
            current_d = datetime.date.fromisoformat(profile.balance_sheet_as_of)
            curated_d = datetime.date.fromisoformat(curated.effective_date)
            drift_days = (current_d - curated_d).days
            if drift_days > REVALIDATION_BALANCE_SHEET_DRIFT_DAYS:
                triggers.append(
                    f"newer_filing: current balance sheet ({profile.balance_sheet_as_of}) is {drift_days}d "
                    f"past curated evidence's effective date ({curated.effective_date}) — "
                    f"exceeds the {REVALIDATION_BALANCE_SHEET_DRIFT_DAYS}d drift policy."
                )
        except ValueError:
            pass

    def _relative_change_trigger(label, current_val, current_status, at_curation_val):
        if at_curation_val is None and current_val is None:
            return None
        if (at_curation_val is None) != (current_val is None):
            return f"claim_status_changed: {label} was {'present' if at_curation_val is not None else 'absent'} at curation, now {'present' if current_val is not None else 'absent'}."
        if at_curation_val == 0:
            if current_val != 0:
                return f"claim_status_changed: {label} was zero at curation, now {current_val:,.0f}."
            return None
        rel_change = abs(current_val - at_curation_val) / abs(at_curation_val)
        sign_flip = (at_curation_val > 0) != (current_val > 0) if current_val != 0 else False
        if sign_flip:
            return f"claim_status_changed: {label} sign flipped since curation ({at_curation_val:,.0f} -> {current_val:,.0f})."
        if rel_change > REVALIDATION_CLAIM_CHANGE_THRESHOLD:
            return (f"claim_balance_changed: {label} changed {rel_change*100:.1f}% since curation "
                    f"({at_curation_val:,.0f} -> {current_val:,.0f}), exceeds {REVALIDATION_CLAIM_CHANGE_THRESHOLD*100:.0f}% policy.")
        return None

    ord_current = profile.ordinary_nci_book if profile.ordinary_nci_status in (DataStatus.POSITIVE, DataStatus.NEGATIVE) else None
    red_current = profile.redeemable_nci_book if profile.redeemable_nci_status in (DataStatus.POSITIVE, DataStatus.NEGATIVE) else None
    t1 = _relative_change_trigger("ordinary NCI", ord_current, profile.ordinary_nci_status, curated.ordinary_nci_at_curation)
    t2 = _relative_change_trigger("redeemable NCI", red_current, profile.redeemable_nci_status, curated.redeemable_nci_at_curation)
    for t in (t1, t2):
        if t:
            triggers.append(t)

    if triggers:
        return EvidenceStatus.NEEDS_REVALIDATION, triggers

    # No trigger fired — reuse the curated evidence's OWN recorded status
    # (e.g. UNVERIFIED_INTERNAL_CONTRADICTION for INR stays exactly that;
    # this function only ever DOWNGRADES toward NEEDS_REVALIDATION, never
    # upgrades a contradiction to FILING_VERIFIED).
    return curated.structure_evidence.status, []


def _combined_positive_nci(profile: OwnershipProfile) -> tuple[Optional[float], str]:
    ord_v = profile.ordinary_nci_book if profile.ordinary_nci_status == DataStatus.POSITIVE else None
    red_v = profile.redeemable_nci_book if profile.redeemable_nci_status == DataStatus.POSITIVE else None
    if ord_v is None and red_v is None:
        return None, "no_positive_claim"
    return max(v for v in (ord_v, red_v) if v is not None), "max_of_components"


def _has_negative_component(profile: OwnershipProfile) -> bool:
    return profile.ordinary_nci_status == DataStatus.NEGATIVE or profile.redeemable_nci_status == DataStatus.NEGATIVE


def assess(profile: OwnershipProfile, curated: Optional[CuratedStructuralEvidence], parent_market_cap: Optional[float]) -> OwnershipAssessment:
    effective_evidence_status, revalidation_triggers = revalidate(profile, curated)

    a = OwnershipAssessment(
        ticker=profile.ticker,
        evidence_status=effective_evidence_status,
        calculability=CalculabilityStatus.UNRESOLVED,
        source_as_of=profile.balance_sheet_as_of,
        revalidation_triggers=revalidation_triggers,
        curated_effective_date=curated.effective_date if curated else None,
        curated_scope=curated.scope if curated else None,
    )

    # --- Data-quality exclusion gates — ALWAYS from CURRENT fields, never curated. Run first, unconditionally. ---
    if profile.registrant_deregistered:
        a.calculability = CalculabilityStatus.NOT_CALCULABLE
        a.calculability_reasons = ["Registrant deregistered from SEC — not a live valuation candidate."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Deregistered — remove from active universe or flag explicitly; do not value."]
        a.overall_readiness = OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED
        return a
    if profile.fundamentals_quarantined:
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = ["CURRENT underlying fundamentals row is quarantined at the source — curated evidence, if any, cannot substitute for this."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Quarantined fundamentals record (current observation)."]
        a.overall_readiness = OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED
        return a
    if profile.identity_quarantined or profile.no_company_match:
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = ["CURRENT company identity is quarantined or unresolved — curated evidence, if any, cannot substitute for this."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Identity-ambiguous record (current observation)."]
        a.overall_readiness = OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED
        return a
    if profile.balance_sheet_freshness_days is not None and profile.balance_sheet_freshness_days > STALE_BALANCE_SHEET_DAYS:
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = [f"CURRENT balance sheet is {profile.balance_sheet_freshness_days}d old (> {STALE_BALANCE_SHEET_DAYS}d policy) — curated evidence, however current, cannot bypass this gate."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Stale CURRENT balance sheet — excluded regardless of any curated review history."]
        a.overall_readiness = OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED
        return a

    # --- Deregistration is sourced from curation (a live SEC EDGAR check,
    # e.g. Form 15/25), not from financial_data.db's current snapshot —
    # financial_data.db has no deregistration field at all. This is a
    # terminal fact, not a "might have changed" structural question, so it
    # is checked BEFORE revalidate() rather than being subject to the
    # normal drift/change triggers. ---
    if curated is not None and curated.structure_type == OwnershipStructureType.STALE_OR_DEREGISTERED:
        a.calculability = CalculabilityStatus.NOT_CALCULABLE
        a.calculability_reasons = ["Registrant confirmed deregistered from SEC (curated evidence) — not a live valuation candidate."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Deregistered (curated evidence) — remove from active universe or flag explicitly; do not value."]
        a.overall_readiness = OverallReadiness.NOT_READY_DATA_QUALITY_EXCLUDED
        return a

    # --- Revalidation required: curated evidence exists but can't be relied on as-is. ---
    if effective_evidence_status == EvidenceStatus.NEEDS_REVALIDATION:
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = [f"Curated structural evidence exists (effective {curated.effective_date}) but needs revalidation: " + "; ".join(revalidation_triggers)]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.materiality_reasons = ["Materiality cannot be assessed from a stale curated conclusion once a revalidation trigger has fired — must be re-derived from current evidence after a fresh check."]
        a.review_required = True
        # Deliberately NOT duplicated into review_reasons — revalidation_triggers
        # already carries this text; the formatter prints both fields, so
        # repeating it here would just print the same sentence twice.
        a.overall_readiness = OverallReadiness.NOT_READY_REVALIDATION_REQUIRED
        return a

    # --- No curated evidence at all: current data only tells us a claim exists, not its structure. ---
    if curated is None:
        if profile.ordinary_nci_status == DataStatus.EXPLICIT_ZERO and profile.redeemable_nci_status == DataStatus.EXPLICIT_ZERO:
            a.calculability = CalculabilityStatus.NOT_APPLICABLE_NO_CLAIM
            a.materiality = MaterialityStatus.NOT_APPLICABLE_NO_CLAIM
            if profile.nci_zero_derived_from_balance_sheet_identity:
                a.materiality_basis = (
                    "No NCI claim reported -- derived from balance-sheet identity "
                    "(totalequity == totalequityandnoncontrollinginterests, and "
                    "totalassets == totalliabilities + totalequityandnoncontrollinginterests), "
                    "not a directly-reported explicit-zero tag."
                )
                a.preserved_caveats.append(
                    "NCI confirmed zero via balance-sheet identity derivation, not a "
                    "directly-reported explicit-zero tag -- see hg_ownership_controls.py's "
                    "NCI_IDENTITY_TOLERANCE check."
                )
            else:
                a.materiality_basis = "No NCI claim reported (explicit zero on both fields)."
            a.wacc_status = WaccStatus.NOT_APPLICABLE_NO_CLAIM
            a.overall_readiness = OverallReadiness.READY_NO_CLAIM
            return a
        if _has_negative_component(profile):
            combined, _ = _combined_positive_nci(profile)
            a.calculability = CalculabilityStatus.UNRESOLVED
            a.calculability_reasons = ["Negative NCI present with no positive component and no curated structural evidence — not assumed to mean 'no claim' or 'immaterial.'"]
            a.materiality = MaterialityStatus.INDETERMINATE
            a.review_required = combined is None
            a.review_reasons = ["Negative-only NCI, database-screened — requires review before any conclusion."] if combined is None else []
            a.overall_readiness = OverallReadiness.NOT_READY_REVIEW_REQUIRED if combined is None else OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE
            if combined is not None:
                return a  # falls through below if a positive component also exists — handled generically
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = ["Ownership structure not yet filing-verified — database-screened only."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.materiality_basis = "UNAPPROVED POLICY PROXY: book NCI value divided by parent market cap — not computed as a conclusion here because structure is unverified."
        a.overall_readiness = OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE
        return a

    # --- Curated evidence present and current (no revalidation trigger fired). ---
    structure_type = curated.structure_type

    if effective_evidence_status == EvidenceStatus.UNVERIFIED_INTERNAL_CONTRADICTION:
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = ["Source evidence contains an internal contradiction on a fact the assessment depends on — not resolved."]
        a.materiality = MaterialityStatus.INDETERMINATE
        a.review_required = True
        a.review_reasons = ["Unverified internal contradiction in source filing."]
        a.overall_readiness = OverallReadiness.NOT_READY_REVIEW_REQUIRED
        return a

    if structure_type == OwnershipStructureType.NO_CLAIM:
        # Gap found 2026-11-19: NO_CLAIM is a declared enum value but had no
        # handling branch here -- it fell into the generic UNKNOWN/else
        # branch below and was treated as UNRESOLVED, the opposite of what
        # a confirmed filing-verified no-claim determination should do.
        # Mirrors the curated-is-None explicit-zero auto-clear above, but
        # for the case where current observations are a MIXED
        # explicit-zero/missing pair (so that auto-clear never fires) and a
        # human has filing-verified there is in fact no claim at all.
        a.calculability = CalculabilityStatus.NOT_APPLICABLE_NO_CLAIM
        a.materiality = MaterialityStatus.NOT_APPLICABLE_NO_CLAIM
        a.materiality_basis = "No NCI claim reported -- filing-verified (see curated structural evidence), not merely database-screened."
        a.wacc_status = WaccStatus.NOT_APPLICABLE_NO_CLAIM
        a.overall_readiness = OverallReadiness.READY_NO_CLAIM
        return a

    if structure_type == OwnershipStructureType.MANAGER_VIE_NO_EQUITY_CLAIM:
        a.calculability = CalculabilityStatus.NOT_CALCULABLE
        a.calculability_reasons = ["Registrant holds no direct equity interest in the consolidated entity — the ownership-waterfall model does not apply to this claim type at all, regardless of size."]
        a.review_required = True
        a.review_reasons = ["Requires a fee-stream-based valuation approach instead of any ownership bridge."]
    elif structure_type == OwnershipStructureType.MULTI_ENTITY_MIXED:
        a.calculability = CalculabilityStatus.NOT_CALCULABLE
        a.calculability_reasons = ["Multiple distinct claims/entities contribute to one combined book figure — no single waterfall applies across them."]
        a.review_required = True
        a.review_reasons = ["Requires entity-by-entity decomposition before any single number can be computed."]
    elif structure_type == OwnershipStructureType.MISLABELED_PARENT_PREFERRED:
        has_parent_pref = any(pc.level == "parent" for pc in curated.preferred_claims)
        if has_parent_pref:
            a.calculability = CalculabilityStatus.CALCULABLE
            a.calculable_method = "parent_preferred_only"
            a.calculability_reasons = ["Confirmed parent-level preferred claim with disclosed, contractual terms (face value, conversion price, dividend rate) — value as a senior bounded claim ahead of common; no ownership-unit waterfall needed."]
        else:
            a.calculability = CalculabilityStatus.UNRESOLVED
            a.calculability_reasons = ["Structure flagged mislabeled-parent-preferred but no parent-level PreferredClaim is attached."]
        a.review_required = True
        a.review_reasons = ["Data-pipeline tagging should be corrected at the source (this is a classification artifact, not a valuation judgment call)."]
    elif structure_type in (OwnershipStructureType.UP_C_MINORITY_COMMON, OwnershipStructureType.UP_C_MAJORITY_COMMON):
        if effective_evidence_status != EvidenceStatus.FILING_VERIFIED:
            a.calculability = CalculabilityStatus.UNRESOLVED
            a.calculability_reasons = ["Structure type set but not filing-verified — cannot confirm waterfall preconditions."]
        elif not curated.common_claims:
            a.calculability = CalculabilityStatus.UNRESOLVED
            a.calculability_reasons = ["No common-unit claim attached despite Up-C structure type."]
        else:
            unmet = []
            for c in curated.common_claims:
                if not c.exchange_ratio_confirmed:
                    unmet.append(f"{c.entity}: exchange mechanism not confirmed")
                if c.outside_ownership_pct is None:
                    unmet.append(f"{c.entity}: ownership percentage not disclosed")
                if c.is_majority is None:
                    unmet.append(f"{c.entity}: majority/minority direction not established")
            overlap_risk = any(pc.level == "subsidiary" for pc in curated.preferred_claims) and not curated.nci_claims_verified_nonoverlapping
            if overlap_risk:
                unmet.append("subsidiary-level preferred claim present with unverified non-overlap vs. common/ordinary NCI")
            if unmet:
                a.calculability = CalculabilityStatus.NOT_CALCULABLE
                a.calculability_reasons = [f"Waterfall precondition(s) unmet: {'; '.join(unmet)}"]
                a.review_required = True
                a.review_reasons = ["Structural preconditions for the standard waterfall are not all confirmed."]
            else:
                a.calculability = CalculabilityStatus.CALCULABLE
                a.calculable_method = "common_unit_waterfall"
                a.calculability_reasons = ["All waterfall preconditions confirmed by filing: structure, exchange mechanism, ownership percentage, and preferred/common non-overlap."]
    elif structure_type == OwnershipStructureType.SIMPLE_SUBSIDIARY:
        # Gap #4 fix: book-value subtraction removed as an approved default
        # for CALCULATING a bridge. Deliberately does NOT set
        # review_required here — whether a method is even needed depends
        # on materiality, computed below. If the claim turns out IMMATERIAL
        # (with a documented assessment), no valuation method is needed at
        # all and this is not a reason to force review; if MATERIAL or
        # PENDING_ANALYSIS, review IS forced — see the materiality block.
        a.calculability = CalculabilityStatus.NOT_CALCULABLE
        a.calculability_reasons = [
            "No approved valuation method exists for a simple-subsidiary NCI claim without a documented "
            "economic-claim assessment or a justified valuation-effect bound. Book-value subtraction was "
            "considered and explicitly rejected as an unsupported default — structural simplicity alone "
            "does not establish fair value. (Moot if materiality below resolves to IMMATERIAL.)"
        ]
    else:  # UNKNOWN (shouldn't reach here if curated is not None, defensive)
        a.calculability = CalculabilityStatus.UNRESOLVED
        a.calculability_reasons = ["Ownership structure not yet filing-verified."]

    # --- Materiality: independent axis, gap #4 fix applied. ---
    combined, method = _combined_positive_nci(profile)
    a.materiality_basis = (
        "UNAPPROVED POLICY PROXY: book NCI value divided by parent market cap. This ratio is a screening "
        "aid, not a confirmed economic materiality determination. A small ratio NEVER by itself establishes "
        "immateriality — an IMMATERIAL conclusion additionally requires a documented, filing-verified "
        "MaterialityAssessment (explicit rationale, evidence, date, and either a bound on the claim's "
        "potential valuation effect or direct analysis of the economic claim itself)."
    )
    if combined is None:
        a.materiality = MaterialityStatus.INDETERMINATE
        a.materiality_reasons = ["No positive NCI component found to assess (using CURRENT values)."]
    elif parent_market_cap is None or parent_market_cap <= 0:
        a.materiality = MaterialityStatus.INDETERMINATE
        a.materiality_reasons = ["Parent market cap missing or non-positive."]
    elif effective_evidence_status != EvidenceStatus.FILING_VERIFIED:
        a.materiality = MaterialityStatus.INDETERMINATE
        ratio = combined / parent_market_cap
        a.materiality_reasons = [f"Ratio = {ratio:.4f} (method={method}, using CURRENT claim value), but structure is NOT filing-verified — ratio alone cannot establish anything."]
    else:
        ratio = combined / parent_market_cap
        if ratio >= MATERIALITY_THRESHOLD_RATIO_TO_MKTCAP:
            a.materiality = MaterialityStatus.MATERIAL
            a.materiality_reasons = [f"Ratio = {ratio:.4f} (CURRENT values) >= materiality threshold {MATERIALITY_THRESHOLD_RATIO_TO_MKTCAP}."]
            if ratio > MANDATORY_REVIEW_RATIO_TO_MKTCAP:
                a.review_required = True
                a.review_reasons.append(f"Ratio {ratio:.4f} exceeds mandatory-review threshold {MANDATORY_REVIEW_RATIO_TO_MKTCAP} — large-claim review trigger. Does NOT override the calculability determination above.")
        else:
            ma = curated.materiality_assessment
            if ma is not None and ma.evidence.status == EvidenceStatus.FILING_VERIFIED:
                a.materiality = MaterialityStatus.IMMATERIAL
                a.materiality_reasons = [
                    f"Ratio = {ratio:.4f} (CURRENT values) < threshold, AND a documented MaterialityAssessment "
                    f"exists (assessed {ma.assessed_date}): {ma.rationale}"
                    + (f" Upper bound estimate: {ma.upper_bound_estimate:,.0f}." if ma.upper_bound_estimate is not None else "")
                ]
            else:
                a.materiality = MaterialityStatus.PENDING_ANALYSIS
                a.materiality_reasons = [
                    f"Ratio = {ratio:.4f} (CURRENT values) is below the screening threshold, AND structure is "
                    "filing-verified, but NO documented MaterialityAssessment exists (no rationale, bound, or "
                    "dated evidence of an actual economic-claim analysis). A small book-value ratio alone is "
                    "insufficient to clear this claim as immaterial — pending the specific missing analysis."
                ]
                a.review_required = True
                a.review_reasons.append("Materiality analysis pending — no documented economic-claim assessment exists for this structurally-simple-looking claim.")

    # Gap #4 follow-through: a SIMPLE_SUBSIDIARY claim's "no approved
    # method" finding only forces review if a method would actually be
    # needed — i.e., if materiality did NOT resolve to a documented
    # IMMATERIAL. An immaterial claim needs no valuation method at all.
    if structure_type == OwnershipStructureType.SIMPLE_SUBSIDIARY and a.materiality != MaterialityStatus.IMMATERIAL:
        a.review_required = True
        a.review_reasons.append("Requires either a documented economic-claim assessment (see materiality_assessment) or company-specific valuation work before any method can be approved.")

    # --- WACC: derived from materiality bucket, never from book NCI directly. ---
    if a.materiality in (MaterialityStatus.INDETERMINATE, MaterialityStatus.PENDING_ANALYSIS):
        a.wacc_status = WaccStatus.UNRESOLVED
        a.wacc_reasons = ["Materiality unresolved or pending analysis — cannot assess whether the known WACC NCI-exclusion matters at this ticker's scale."]
    elif a.materiality == MaterialityStatus.IMMATERIAL:
        a.wacc_status = WaccStatus.CONSISTENT
        a.wacc_reasons = ["Claim confirmed immaterial (with documented assessment) — WACC's exclusion of NCI/preferred from capital-structure weights does not matter at this scale."]
    else:
        a.wacc_status = WaccStatus.EXCEPTION_NCI_EXCLUDED
        a.wacc_reasons = ["Confirmed by code read (av_fcff_2.calc_discount_rate): capital-structure weights exclude NCI/preferred unconditionally. Material at this ticker's scale."]

    # --- Overall readiness, composite, last. ---
    if a.review_required:
        if a.materiality == MaterialityStatus.PENDING_ANALYSIS:
            a.overall_readiness = OverallReadiness.NOT_READY_MATERIALITY_ANALYSIS_PENDING
        else:
            a.overall_readiness = OverallReadiness.NOT_READY_REVIEW_REQUIRED
    elif a.calculability == CalculabilityStatus.UNRESOLVED or a.materiality in (MaterialityStatus.INDETERMINATE, MaterialityStatus.PENDING_ANALYSIS):
        a.overall_readiness = (
            OverallReadiness.NOT_READY_MATERIALITY_ANALYSIS_PENDING if a.materiality == MaterialityStatus.PENDING_ANALYSIS
            else OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE
        )
    elif a.materiality == MaterialityStatus.IMMATERIAL:
        a.overall_readiness = OverallReadiness.READY_IMMATERIAL
    elif a.calculability == CalculabilityStatus.CALCULABLE and a.wacc_status == WaccStatus.EXCEPTION_NCI_EXCLUDED:
        a.overall_readiness = OverallReadiness.SUPPORTED_BRIDGE_WACC_EXCEPTION_PRESENT
    else:
        a.overall_readiness = OverallReadiness.NOT_READY_INSUFFICIENT_EVIDENCE

    for item in curated.parent_specific_items:
        if item.is_contingent:
            a.preserved_caveats.append(f"Unquantified contingent item (never cleared by a supported bridge): {item.description}")
    for caveat in curated.known_unresolved_caveats:
        a.preserved_caveats.append(f"Prior-session unresolved item (never cleared by this bridge): {caveat}")

    return a
