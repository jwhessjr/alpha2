"""
Stage V ownership-controls, revised data model (gap #1 fix).

Core change from the prior Phase 1 session: a hard separation between
CURRENT OBSERVATIONS (always freshly read from the database — dates,
claim amounts, identity status, validation status, freshness) and
CURATED STRUCTURAL EVIDENCE (the one-time, manually-reviewed structure
determination, e.g. "this is an Up-C with these exchange terms" — which
does NOT change every quarter and should NOT be re-derived from a fresh
query, but also must never silently override current data).

`OwnershipProfile` now holds ONLY current observations. `CuratedStructuralEvidence`
is a separate object, attached (not merged) after a `revalidate()` check
(see assessment_engine.py) decides whether it's still safe to rely on.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DataStatus(Enum):
    MISSING = "missing"
    EXPLICIT_ZERO = "explicit_zero"
    NEGATIVE = "negative"
    POSITIVE = "positive"


class OwnershipStructureType(Enum):
    SIMPLE_SUBSIDIARY = "simple_subsidiary"
    UP_C_MINORITY_COMMON = "up_c_minority_common"
    UP_C_MAJORITY_COMMON = "up_c_majority_common"
    MULTI_ENTITY_MIXED = "multi_entity_mixed"
    MANAGER_VIE_NO_EQUITY_CLAIM = "manager_vie_no_equity_claim"
    MISLABELED_PARENT_PREFERRED = "mislabeled_parent_preferred"
    STALE_OR_DEREGISTERED = "stale_or_deregistered"
    NO_CLAIM = "no_claim"
    UNKNOWN = "unknown"


class EvidenceStatus(Enum):
    FILING_VERIFIED = "filing_verified"
    DATABASE_SCREENED_ONLY = "database_screened_only"
    UNVERIFIED_INTERNAL_CONTRADICTION = "unverified_internal_contradiction"
    FABRICATED_DISCARDED = "fabricated_discarded"
    NOT_APPLICABLE_NO_CLAIM = "not_applicable_no_claim"
    NEEDS_REVALIDATION = "needs_revalidation"
    """NEW (gap #1): curated structural evidence exists, but a revalidation
    trigger fired against CURRENT data (newer filing, changed claim balance,
    identity/quarantine event, or an internal contradiction in the current
    observation vs. what was true at curation time). Distinct from
    DATABASE_SCREENED_ONLY — this ticker WAS once reviewed, and that
    review's provenance is still visible, but it cannot be relied on
    as-is until re-checked. Distinct from FILING_VERIFIED for the same
    reason: reusing a stale conclusion is exactly what this status exists
    to prevent."""


@dataclass(frozen=True)
class EvidenceRecord:
    status: EvidenceStatus
    source_detail: str
    as_of_date: Optional[str] = None


@dataclass
class Entity:
    name: str
    is_registrant: bool
    is_valued_entity: bool
    evidence: EvidenceRecord


@dataclass
class DebtPosition:
    value: float
    status: DataStatus
    entity: str
    evidence: EvidenceRecord
    includes_finance_lease_style: bool = False


@dataclass
class CashPosition:
    value: float
    status: DataStatus
    entity: str
    evidence: EvidenceRecord


@dataclass
class PreferredClaim:
    face_value: Optional[float]
    fair_value_at_issuance: Optional[float]
    redemption_value: Optional[float]
    dividend_rate: Optional[float]
    level: str
    entity: str
    conversion_terms: str
    status: DataStatus
    evidence: EvidenceRecord
    embedded_in_consolidated_nci: bool = False

    def best_available_value(self) -> Optional[float]:
        for v in (self.redemption_value, self.face_value, self.fair_value_at_issuance):
            if v is not None:
                return v
        return None


@dataclass
class CommonOwnershipClaim:
    outside_ownership_pct: Optional[float]
    ownership_pct_as_of: Optional[str]
    ownership_pct_is_period_average: bool
    is_majority: Optional[bool]
    exchange_ratio_confirmed: bool
    entity: str
    status: DataStatus
    evidence: EvidenceRecord
    internal_contradiction_note: Optional[str] = None


@dataclass
class ParentSpecificItem:
    description: str
    value: Optional[float]
    is_contingent: bool
    evidence: EvidenceRecord


@dataclass
class ShareDenominator:
    parent_shares: float
    parent_shares_as_of: str
    outside_units_already_included: bool
    total_fungible_units_estimate: Optional[float] = None
    total_fungible_units_estimate_range: Optional[tuple[float, float]] = None
    evidence: Optional[EvidenceRecord] = None


@dataclass
class MaterialityAssessment:
    """NEW (gap #4). An IMMATERIAL materiality conclusion requires one of
    these to exist and be filing-verified — a small book-value ratio alone
    is never sufficient (see assessment_engine.py's materiality logic)."""
    rationale: str
    upper_bound_estimate: Optional[float]
    evidence: EvidenceRecord
    assessed_date: str


@dataclass
class CuratedStructuralEvidence:
    """NEW (gap #1). The one-time, manually-reviewed structural
    determination for a ticker — separate from, and never a substitute
    for, current observations. `effective_date`/`scope`/`revalidation_policy`
    make explicit WHEN this is valid, WHAT was actually checked, and WHEN
    it needs a fresh look."""
    structure_type: OwnershipStructureType
    structure_evidence: EvidenceRecord
    effective_date: str
    scope: str
    revalidation_policy: str
    ordinary_nci_at_curation: Optional[float] = None
    redeemable_nci_at_curation: Optional[float] = None
    common_claims: list[CommonOwnershipClaim] = field(default_factory=list)
    preferred_claims: list[PreferredClaim] = field(default_factory=list)
    share_denominator: Optional[ShareDenominator] = None
    parent_specific_items: list[ParentSpecificItem] = field(default_factory=list)
    known_unresolved_caveats: list[str] = field(default_factory=list)
    nci_claims_verified_nonoverlapping: bool = False
    materiality_assessment: Optional[MaterialityAssessment] = None
    debt: list[DebtPosition] = field(default_factory=list)
    cash: list[CashPosition] = field(default_factory=list)


@dataclass
class OwnershipProfile:
    """CURRENT OBSERVATIONS ONLY. Never populated from curated evidence —
    always a fresh read (see full_universe_screen.py /
    lib_hg_ownership_controls_RESEARCH_COPY.fetch_nci_claims())."""
    ticker: str
    ordinary_nci_book: Optional[float] = None
    ordinary_nci_status: DataStatus = DataStatus.MISSING
    redeemable_nci_book: Optional[float] = None
    redeemable_nci_status: DataStatus = DataStatus.MISSING
    balance_sheet_as_of: Optional[str] = None
    balance_sheet_freshness_days: Optional[int] = None
    fundamentals_quarantined: bool = False
    identity_quarantined: bool = False
    no_company_match: bool = False
    registrant_deregistered: bool = False
