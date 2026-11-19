"""
The curated structural-evidence lookup table (gap #1 fix). Replaces the
prior session's `fixtures.py`, which returned a FULL OwnershipProfile
(current + structural facts blended) per ticker — the exact design gap
#1 identified. Each entry here returns ONLY a `CuratedStructuralEvidence`
object: the structural facts (structure type, claims, share denominator,
caveats) plus its own `effective_date`/`scope`/`revalidation_policy` and,
where relevant, the claim amounts AS THEY WERE at curation time (so a
later revalidation check can detect drift). Current observations
(balance sheet date, claim amounts NOW, quarantine flags) are NEVER
stored here — they come from a fresh query every time, in
full_universe_screen.py / lib_hg_ownership_controls_RESEARCH_COPY.py.

Every fact below is traceable to an accession number already cited in
stage_v_population_nci_audit/03_filing_verification_appendix.md.
"""
from __future__ import annotations

from ownership_model import (
    CommonOwnershipClaim,
    CuratedStructuralEvidence,
    CashPosition,
    DataStatus,
    DebtPosition,
    EvidenceRecord,
    EvidenceStatus,
    MaterialityAssessment,
    OwnershipStructureType,
    ParentSpecificItem,
    PreferredClaim,
    ShareDenominator,
)

FV = lambda detail, as_of=None: EvidenceRecord(EvidenceStatus.FILING_VERIFIED, detail, as_of)
UNVERIFIED_CONTRADICTION = lambda detail: EvidenceRecord(EvidenceStatus.UNVERIFIED_INTERNAL_CONTRADICTION, detail)
DB_SCREENED = lambda detail: EvidenceRecord(EvidenceStatus.DATABASE_SCREENED_ONLY, detail)

#: UNAPPROVED POLICY default revalidation policy text, reused where a
#: ticker has no special-cased rule. Concrete trigger values live in
#: assessment_engine.py (REVALIDATION_BALANCE_SHEET_DRIFT_DAYS,
#: REVALIDATION_CLAIM_CHANGE_THRESHOLD) — stated here in prose so the
#: curated record is self-documenting without needing to cross-reference
#: the engine's constants to understand what "needs revalidation" means
#: for this specific ticker.
DEFAULT_REVALIDATION_POLICY = (
    "Revalidate if: (a) current balance sheet postdates effective_date by more than "
    "the engine's configured drift window, (b) either NCI component's current value "
    "differs from its at-curation value by more than the configured relative "
    "threshold, (c) either component's sign/status flips vs. at-curation, or "
    "(d) identity/quarantine flags newly fire. See assessment_engine.py's "
    "REVALIDATION_* constants for the exact, UNAPPROVED threshold values."
)


def chtr_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001091667-26-000073, Note 9 (R15.htm) + cover page (R1.htm)", "2026-09-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.UP_C_MINORITY_COMMON,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Ownership %, exchange mechanism, preferred terms, debt/cash, and TRA verified against the "
              "2026-09-30 10-Q (Note 9, R15/R43.htm) and the Cox Transactions note (R8/R34-R36.htm).",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=18_837_000_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    e.debt = [DebtPosition(108_748_000_000.0, DataStatus.POSITIVE, "Charter Holdings", ev, includes_finance_lease_style=True)]
    e.cash = [CashPosition(573_000_000.0, DataStatus.POSITIVE, "Charter Holdings", ev)]
    e.preferred_claims = [
        PreferredClaim(
            face_value=6_000_000_000.0, fair_value_at_issuance=5_852_000_000.0, redemption_value=None,
            dividend_rate=0.06875, level="subsidiary", entity="Charter Holdings",
            conversion_terms="12.6M common units @ $477.41; callable after 2031-08-19 if price > 130% of conversion price",
            status=DataStatus.POSITIVE, evidence=ev, embedded_in_consolidated_nci=True,
        )
    ]
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.30, ownership_pct_as_of="2026-09-30", ownership_pct_is_period_average=True,
            is_majority=False, exchange_ratio_confirmed=True, entity="Charter Holdings",
            status=DataStatus.POSITIVE, evidence=ev,
        )
    ]
    e.parent_specific_items = [
        ParentSpecificItem("Tax Receivable Agreement — Charter pays Cox/A-N 50% of future exchange-related tax benefits",
                            value=None, is_contingent=True, evidence=ev)
    ]
    e.known_unresolved_caveats = [
        "26.9% Cable-TV Year-5 growth anchor confirmed (Stage V1) to be a small-sample industry artifact, not re-derived or corrected in this bridge.",
        "TTM EBIT ($6,006M) blends ~46 weeks pre-Cox with only ~6 weeks post-Cox contribution (deal closed 2026-08-19) — understated, non-representative base for the combined entity; not revised here.",
        "WACC (7.11%) excludes NCI from capital-structure weights, and an attempted FCFE reconciliation found the implied cost of equity (24-36%) and required leverage path ($45.6B net borrowing in Year 6 alone) both implausible — neither issue is resolved by this bridge.",
    ]
    e.share_denominator = ShareDenominator(
        parent_shares=114_454_221.0, parent_shares_as_of="2026-09-30",
        outside_units_already_included=False,
        total_fungible_units_estimate_range=(160_000_000.0, 163_500_000.0),
        evidence=ev,
    )
    return e


def cmcsa_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q, redeemable NCI note — 'interests in consolidated subsidiaries that have redemption features'", "2026-09-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.SIMPLE_SUBSIDIARY,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Generic redemption-features description reviewed against the 10-Q note. NO segment-level or "
              "subsidiary-level analysis of what this specific claim could be worth was performed — the "
              "filing does not name the subsidiary/subsidiaries involved, their size, or any valuation-relevant "
              "detail beyond the existence of 'redemption features.'",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=-33_000_000.0,
        redeemable_nci_at_curation=171_000_000.0,
        nci_claims_verified_nonoverlapping=True,
        # Deliberately NO MaterialityAssessment attached (gap #4) — the
        # recorded evidence does not support a documented economic-claim
        # assessment or a justified bound; only a generic description and
        # a small book-value ratio exist. Per instruction, this is NOT
        # forced to preserve the prior READY_IMMATERIAL clearance.
        materiality_assessment=None,
    )
    return e


def hmh_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001628280-26-070108", "2026-09-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.UP_C_MINORITY_COMMON,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Up-C structure and 50%/25% outside stakes (Baker Hughes/Akastor) confirmed against the 10-Q. "
              "Exact exchange ratio NOT located in the reviewed excerpt — flagged, not assumed.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=641_600_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.75, ownership_pct_as_of="2026-09-30", ownership_pct_is_period_average=False,
            is_majority=True, exchange_ratio_confirmed=False, entity="HMH B.V.",
            status=DataStatus.POSITIVE, evidence=ev,
        )
    ]
    return e


def prok_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001193125-25-274303", None)
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.UP_C_MAJORITY_COMMON,
        structure_evidence=ev,
        effective_date="2025-09-30",
        scope="Up-C structure, 56.1% majority outside common interest, and 1:1 exchange mechanism confirmed "
              "against the cited 10-Q. Later filings not independently re-confirmed to the same depth.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=1_106_370_000.0,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.561, ownership_pct_as_of=None, ownership_pct_is_period_average=False,
            is_majority=True, exchange_ratio_confirmed=True, entity="PKLP",
            status=DataStatus.POSITIVE, evidence=ev,
        )
    ]
    return e


def inr_evidence() -> CuratedStructuralEvidence:
    ev = UNVERIFIED_CONTRADICTION(
        "10-Q accn 0002029118-26-000098 — R19.htm narrative says Legacy 70.6%/Company 29.4%; "
        "R56.htm detail table showed labels reversed, not independently resolved"
    )
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.UP_C_MAJORITY_COMMON,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Up-C structure confirmed; majority/minority DIRECTION unresolved (internal contradiction).",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=1_050_186_000.0,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.706, ownership_pct_as_of="2026-06-30", ownership_pct_is_period_average=False,
            is_majority=True, exchange_ratio_confirmed=True, entity="INR Holdings LLC",
            status=DataStatus.POSITIVE, evidence=ev,
            internal_contradiction_note="R56.htm detail table showed 70.60%/29.40% with labels appearing reversed vs. R19's prose",
        )
    ]
    return e


def fubo_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001628280-26-053535; Disney ~70% stake per public reporting, not independently "
            "re-confirmed to a specific filing paragraph", "2026-06-30")
    ev_unconfirmed = EvidenceRecord(EvidenceStatus.DATABASE_SCREENED_ONLY, "Disney ~70% figure is from public reporting, not confirmed to a specific filing paragraph")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.UP_C_MAJORITY_COMMON,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Reverse-acquisition structure and redeemable-mezzanine classification confirmed. Disney's exact "
              "ownership % and exchange mechanism NOT confirmed to a specific filing paragraph — flagged.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=1_825_516_000.0,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.70, ownership_pct_as_of="2026-06-30", ownership_pct_is_period_average=False,
            is_majority=True, exchange_ratio_confirmed=False, entity="Hulu Live/Fubo combined entity",
            status=DataStatus.POSITIVE, evidence=ev_unconfirmed,
        )
    ]
    return e


def lcid_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001628280-26-052606, 'Redeemable Convertible Preferred Stock' note", "2026-06-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.MISLABELED_PARENT_PREFERRED,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Series A/B/C redeemable convertible preferred terms (face value, conversion price, dividend "
              "rate) confirmed directly against the 10-Q note, matching the local DB figure to the dollar.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=2_906_257_000.0,
        nci_claims_verified_nonoverlapping=True,
    )
    e.preferred_claims = [
        PreferredClaim(1_469_500_000.0, None, None, 0.0, "parent", "Lucid Group, Inc.", "Series A, $35.95 conversion price", DataStatus.POSITIVE, ev),
        PreferredClaim(1_032_500_000.0, None, None, 0.0, "parent", "Lucid Group, Inc.", "Series B, $43.80 conversion price", DataStatus.POSITIVE, ev),
        PreferredClaim(404_300_000.0, None, None, 0.09, "parent", "Lucid Group, Inc.", "Series C, 9.00% dividend, $10.82 conversion price", DataStatus.POSITIVE, ev),
    ]
    return e


def rhld_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001104659-26-091709 — 'does not own any equity interests or common stock in GPGI Holdings'", "2026-06-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.MANAGER_VIE_NO_EQUITY_CLAIM,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Management-fee-only relationship (2.5% of GPGI's trailing-12-month Adjusted EBITDA) confirmed "
              "directly against the 10-Q's explicit statement of no equity interest.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=3_084_900_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    e.parent_specific_items = [
        ParentSpecificItem("Management fee: 2.5% of GPGI Holdings' trailing-12-month Adjusted EBITDA", value=None, is_contingent=False, evidence=ev)
    ]
    return e


def csiq_evidence() -> CuratedStructuralEvidence:
    ev = FV("20-F accn 0001104659-26-041976", "2025-12-31")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.MULTI_ENTITY_MIXED,
        structure_evidence=ev,
        effective_date="2025-12-31",
        scope="Two unrelated claims confirmed (CSI Solar Shanghai-listed minority + Recurrent Energy project "
              "preferred) — dollar split between the two NOT confirmed to the specific balance.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=1_135_098_000.0,
        redeemable_nci_at_curation=326_559_000.0,
        nci_claims_verified_nonoverlapping=False,
    )
    return e


def inv_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-K accn 0002001557-26-000060", "2025-12-31")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.MULTI_ENTITY_MIXED,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Three subsidiaries at three different ownership percentages (37.1/43.2/68.5%) plus parent-level "
              "preferred confirmed against the 10-K.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=270_125_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=False,
    )
    return e


def fip_evidence() -> CuratedStructuralEvidence:
    ev = FV("10-Q accn 0001899883-26-000043, Note 15", "2026-09-30")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.MULTI_ENTITY_MIXED,
        structure_evidence=ev,
        effective_date="2026-06-30",
        scope="Common NCI + separate RailCo preferred (book $1.04-1.16B vs. disclosed $1.4B redemption value) "
              "confirmed against the 10-Q.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=-188_947_000.0,
        redeemable_nci_at_curation=1_157_045_000.0,
        nci_claims_verified_nonoverlapping=True,
    )
    e.preferred_claims = [
        PreferredClaim(None, None, 1_400_000_000.0, None, "subsidiary", "RailCo", "Series A RailCo redeemable preferred, 1,000,000 shares", DataStatus.POSITIVE, ev, embedded_in_consolidated_nci=True),
    ]
    return e


def bbuc_evidence() -> CuratedStructuralEvidence:
    ev = FV("Form 25-NSE accn 0000876661-26-000304 (2026-03-31); Form 15-12G accn 0001104659-26-042300 (2026-04-13)", "2026-04-13")
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.STALE_OR_DEREGISTERED,
        structure_evidence=ev,
        effective_date="2026-04-13",
        scope="SEC deregistration (Form 15-12G) confirmed directly.",
        revalidation_policy="Revalidate only if the entity re-registers with the SEC (unlikely) — otherwise this determination does not go stale in the normal sense.",
        ordinary_nci_at_curation=9_120_000_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    return e


def zts_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-Q accn 0001555280-26-000050, Condensed Consolidated Balance Sheets and Statements of Equity -- "
        "'Equity attributable to noncontrolling interests' and NCI share of net income both show '--' "
        "(zero) for every period presented, both current and prior year.",
        "2026-09-30",
    )
    return CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.NO_CLAIM,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Confirmed directly: 100% ownership of all consolidated subsidiaries, no outside equity "
              "holders, no redeemable/mezzanine NCI, no VIE or exchangeable-unit arrangements.",
        revalidation_policy="Revalidate if a future filing reports any nonzero NCI line item.",
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )


def pxed_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-K accn 0001193125-26-445323 -- 'Apollo Education Group, Inc., our direct wholly owned "
        "subsidiary and the direct parent of the University' and 'The University of Phoenix, Inc., our "
        "indirect wholly owned subsidiary.' Apollo Global Management/Vistria Group hold public shares at "
        "the PARENT level post-2025-10 IPO, not a subsidiary-level noncontrolling interest.",
        "2026-08-31",
    )
    return CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.NO_CLAIM,
        structure_evidence=ev,
        effective_date="2026-08-31",
        scope="Confirmed wholly-owned subsidiary chain (Apollo Education Group -> University of Phoenix). "
              "No outside ownership, no exchangeable units, no JV combinations, no redeemable preferred "
              "equity classified outside permanent equity.",
        revalidation_policy="Revalidate if a future filing reports any NCI line item, or if the post-IPO "
                             "parent-level ownership structure changes.",
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )


def acn_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-K accn 0001467373-26-000049 -- 'NoncontrollingInterestMember' appears only as an XBRL "
        "reporting dimension with zero/null values across every statement; entities named in the filing "
        "(e.g. 'Accenture Federal Services, a U.S. company and a wholly owned subsidiary') are described "
        "as wholly owned throughout.",
        "2026-08-31",
    )
    return CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.NO_CLAIM,
        structure_evidence=ev,
        effective_date="2026-08-31",
        scope="Confirmed no noncontrolling interest of any kind -- integrated global organization of "
              "wholly-owned subsidiaries.",
        revalidation_policy="Revalidate if a future filing reports any NCI line item.",
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )


def laur_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-Q accn 0000912766-26-000003 -- noncontrolling interests $0 at 2026-09-30 (was $(575) thousand "
        "at 2025-12-31, now eliminated). 'Total Laureate Education, Inc. stockholders' equity' comprises "
        "virtually the entire equity structure; the company's institutions (Mexico, Peru) are operated as "
        "wholly-owned.",
        "2026-09-30",
    )
    return CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.NO_CLAIM,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Confirmed no current-period NCI -- the small prior-period balance was eliminated, not a "
              "continuing claim.",
        revalidation_policy="Revalidate if a future filing reports a new nonzero NCI line item.",
        ordinary_nci_at_curation=None,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )


def cent_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-Q accn 0000887733-26-000022 -- 'noncontrolling interest in the Company's condensed "
        "consolidated financial statements represents the 20% interest not owned by Central in a "
        "consolidated subsidiary,' classified as permanent equity (not redeemable/mezzanine).",
        "2026-06-27",
    )
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.SIMPLE_SUBSIDIARY,
        structure_evidence=ev,
        effective_date="2026-06-27",
        scope="Single consolidated subsidiary, Central owns 80%, one outside party owns the remaining 20%, "
              "permanent-equity classification confirmed, no redeemable component.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=1_231_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.20, ownership_pct_as_of="2026-06-27", ownership_pct_is_period_average=False,
            is_majority=False, exchange_ratio_confirmed=True, entity="Central Garden & Pet consolidated subsidiary",
            status=DataStatus.POSITIVE, evidence=ev,
        )
    ]
    e.materiality_assessment = MaterialityAssessment(
        rationale="Single named 80/20 subsidiary, permanent-equity classification, no redeemable/mezzanine "
                  "component, no exchange mechanism, no indication the minority-held entity concentrates a "
                  "disproportionate share of consolidated value. $1.231M book NCI against a $2.91B market "
                  "cap (0.04%) -- upper bound (full book value) is immaterial at any plausible fair-value "
                  "multiple of book.",
        upper_bound_estimate=1_231_000.0,
        evidence=ev,
        assessed_date="2026-11-19",
    )
    return e


def oii_evidence() -> CuratedStructuralEvidence:
    ev = FV(
        "10-Q accn 0000073756-26-000154 -- Oceaneering 'consolidate[s] entities that are determined to be "
        "variable interest entities' as primary beneficiary; holds a 45% interest in one VIE located in "
        "Angola, remaining 55% owned by a local service provider, wholly consolidated.",
        "2026-09-30",
    )
    e = CuratedStructuralEvidence(
        structure_type=OwnershipStructureType.SIMPLE_SUBSIDIARY,
        structure_evidence=ev,
        effective_date="2026-09-30",
        scope="Single named VIE (Angola operation), Oceaneering holds 45%, local service provider holds "
              "55%, consolidated as primary beneficiary. No redeemable/mezzanine component.",
        revalidation_policy=DEFAULT_REVALIDATION_POLICY,
        ordinary_nci_at_curation=4_572_000.0,
        redeemable_nci_at_curation=None,
        nci_claims_verified_nonoverlapping=True,
    )
    e.common_claims = [
        CommonOwnershipClaim(
            outside_ownership_pct=0.55, ownership_pct_as_of="2026-09-30", ownership_pct_is_period_average=False,
            is_majority=True, exchange_ratio_confirmed=True, entity="Angola VIE (local service provider)",
            status=DataStatus.POSITIVE, evidence=ev,
        )
    ]
    e.materiality_assessment = MaterialityAssessment(
        rationale="Single named VIE, consolidated as primary beneficiary despite Oceaneering's own stake "
                  "being a minority 45% -- the outside party (55%) is the one with the larger claim, which "
                  "is already fully reflected in the $4.572M book NCI. No redeemable/mezzanine component. "
                  "$4.572M against a $3.39B market cap (0.13%) -- upper bound (full book value) is "
                  "immaterial at any plausible fair-value multiple of book.",
        upper_bound_estimate=4_572_000.0,
        evidence=ev,
        assessed_date="2026-11-19",
    )
    return e


ALL_CURATED_EVIDENCE = {
    "CHTR": (chtr_evidence, 13_124_102_555.0),
    "CMCSA": (cmcsa_evidence, 80_572_333_741.0),
    "HMH": (hmh_evidence, 142_710_000.0),
    "PROK": (prok_evidence, 435_344_623.0),
    "INR": (inr_evidence, 459_470_954.0),
    "FUBO": (fubo_evidence, 897_597_655.0),
    "LCID": (lcid_evidence, 1_288_609_476.0),
    "RHLD": (rhld_evidence, 1_005_479_178.0),
    "CSIQ": (csiq_evidence, 662_505_029.0),
    "INV": (inv_evidence, 73_151_299.0),
    "FIP": (fip_evidence, 272_958_736.0),
    "BBUC": (bbuc_evidence, 2_343_896_516.0),
    "ZTS": (zts_evidence, 34_768_472_699.0),
    "CENT": (cent_evidence, 2_912_433_585.0),
    "OII": (oii_evidence, 3_388_526_927.0),
    # ACN, PXED, LAUR were drafted here 2026-11-19 as NO_CLAIM based on a
    # WebFetch summary of their 10-K/10-Q that turned out to be wrong --
    # live cross-check against financial_data.db found real reported NCI
    # (ACN: $1.157B ordinary + $532.76M redeemable; PXED: $1.914M ordinary;
    # LAUR: a $699K redeemable component the fetch missed). The
    # revalidation mechanism caught the mismatch before anything shipped.
    # Deliberately NOT registered -- these three still need real research,
    # not a corrected guess. See pxed_evidence()/acn_evidence()/
    # laur_evidence() below: left in place for reference, not deleted, but
    # intentionally excluded from this dict.
}
