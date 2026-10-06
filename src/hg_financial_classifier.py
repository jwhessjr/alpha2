"""
Financial-firm economic-subtype classification -- a layer SEPARATE from the
valuation engines (lib/hg_financial_divginzu.py, lib/hg_financial_eqexret.py)
and from the beta-peer-group lookup needed to feed them, per Jim's explicit
instruction: "classification/routing is a separate concern from the
valuation engines themselves."

Built to resolve the COF/RJF misclassification confirmed across six
separate audits this week (2026-10-02 through 2026-10-05):
  - COF (SIC 6021, a true depository bank, identical SIC to JPM/BAC/USB)
    was routed into insurance-style NI normalization purely because its
    Damodaran-industry string contains the substring "insurance"
    ("Financial Svcs. (Non-bank & Insurance)").
  - RJF (SIC 6211, a broker/dealer) was routed through the identical bank
    Excess Return function as JPM, with no subtype distinction anywhere.

Per Jim's explicit instruction: "Do not solve classification using
company-name strings or bucket labels." SIC is therefore the PRIMARY
classifier for economic subtype -- it is a structured, SEC-assigned code,
not a brittle keyword match. Damodaran's industry string is used only as a
SECONDARY signal, strictly for beta-peer-group selection within the
BANK_DEPOSITORY subtype (Money Center vs. Regional), never to override SIC's
subtype determination. This directly implements the hybrid approach
recommended in the 2026-10-02 SIC classification audit and confirmed
workable (zero depository/insurer classification errors in a 23-ticker
sample) in that audit.

SIC ranges (SEC Division H, "Finance, Insurance, and Real Estate",
confirmed against the SEC's own SIC code list):
  6020-6036  National/state commercial banks, savings institutions -> BANK_DEPOSITORY
  6060-6062  Credit unions -> BANK_DEPOSITORY (deposit-taking, same economics)
  6099       Functions related to depository banking -> BANK_DEPOSITORY
  6141-6159  Personal/consumer credit, agricultural credit -> CONSUMER_FINANCE
  6199       Finance services, NEC -> CONSUMER_FINANCE (2026-10-06 addition --
             AXP/SYF-shaped card issuers/consumer lenders carry this generic
             SIC; confirmed live for both tickers, see docs/decisions.md)
  6211       Security brokers, dealers, and flotation companies -> ambiguous,
             see _resolve_sic_6211() -- shared with asset managers (e.g. BLK)
  6221-6221  Commodity contracts brokers -> BROKER_DEALER
  6282       Investment advice -> ASSET_MANAGER
  6726       Investment offices, NEC -> ASSET_MANAGER
  6311-6321  Life insurance, accident & health insurance -> INSURER
  6324, 6331, 6351, 6361, 6411  Hospital/medical service plans, fire/marine/
             casualty insurance, surety, title insurance, insurance agents -> INSURER
  other 6000-6799 -> OTHER_FINANCIAL (no bank/broker/asset-manager/insurer
             match confirmed, but still inside the SEC financial division)
  outside 6000-6799, or SIC unavailable -> see classify_financial_firm()

NOTE ON COF specifically (2026-10-06 production-routing pass): COF's SIC is
6021 -- a genuine depository-bank SIC, identical to JPM/BAC/USB/PNC/WFC/C,
confirmed across six separate audits this week. COF is correctly classified
BANK_DEPOSITORY here, NOT CONSUMER_FINANCE -- relabeling it would contradict
real, repeatedly-verified SEC data. COF already reaches the required
"never bank-model, never insurer-normalized" outcome today via the existing
mechanism: BANK_DEPOSITORY + an unresolved peer-beta bucket (its Damodaran
industry isn't one of the two confirmed bank-specific strings) -> structured
unsupported, same end state a CONSUMER_FINANCE label would produce, reached
honestly instead of via a factually incorrect SIC relabel.

SIC 6211 is genuinely ambiguous -- shared by true brokers (GS/MS/SCHW/RJF,
confirmed) and at least one true asset manager (BLK, confirmed) -- resolved
via an explicit, exact-match secondary Damodaran-industry lookup
(_resolve_sic_6211()), never a substring/keyword heuristic. Falls back to
OTHER_FINANCIAL (not a guessed BROKER_DEALER/ASSET_MANAGER) if the
Damodaran industry string doesn't exactly match one of the two known
values -- "do not guess" applies here exactly as it does to the bank
peer-beta bucket.

This module does NOT decide a valuation model. It answers two questions
only: "what kind of financial firm is this" and, for depository banks
specifically, "which Damodaran beta peer group applies."
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FinancialSubtype(str, Enum):
    BANK_DEPOSITORY = "BANK_DEPOSITORY"
    CONSUMER_FINANCE = "CONSUMER_FINANCE"
    BROKER_DEALER = "BROKER_DEALER"
    ASSET_MANAGER = "ASSET_MANAGER"
    INSURER = "INSURER"
    OTHER_FINANCIAL = "OTHER_FINANCIAL"
    NOT_FINANCIAL = "NOT_FINANCIAL"
    UNKNOWN = "UNKNOWN"              # SIC unavailable and no other signal


_BANK_DEPOSITORY_SIC_RANGES = ((6020, 6036), (6060, 6062), (6099, 6099))
_CONSUMER_FINANCE_SIC_RANGES = ((6141, 6159), (6199, 6199))
_BROKER_DEALER_SIC_RANGES = ((6211, 6211), (6221, 6221))   # 6211 is coarse/pure-SIC
                                                             # only -- see _resolve_sic_6211()
                                                             # for the real, industry-aware resolution
                                                             # classify_financial_firm() actually applies.
_ASSET_MANAGER_SIC_RANGES = ((6282, 6282), (6726, 6726))
_INSURER_SIC_RANGES = ((6311, 6321), (6324, 6324), (6331, 6331), (6351, 6351), (6361, 6361), (6411, 6411))
SIC_FINANCIAL_RANGE = (6000, 6799)

# Explicit, exact-match secondary discriminator for the genuinely ambiguous
# SIC 6211 (2026-10-06) -- confirmed live: GS/MS/SCHW/RJF all carry this SIC
# and are true brokers; BLK also carries this SIC but is a true asset
# manager. No substring/keyword heuristic -- an unrecognized Damodaran
# industry string for a 6211 company resolves to OTHER_FINANCIAL (ambiguous,
# unresolved), never a guessed BROKER_DEALER or ASSET_MANAGER.
AMBIGUOUS_SIC_6211 = 6211
_SIC_6211_SECONDARY_MAPPING: dict[str, FinancialSubtype] = {
    "Brokerage & Investment Banking": FinancialSubtype.BROKER_DEALER,
    "Investments & Asset Management": FinancialSubtype.ASSET_MANAGER,
}

# Exact Damodaran industry-string bank buckets this project's reference
# data (reference_data/indname_2026.xlsx / betas_2026.xls) carries. These
# two strings are the ONLY ones treated as a confirmed bank peer-beta
# group -- no substring/keyword matching, per Jim's explicit instruction.
# Confirmed from betas_2026.xls, "Industry Averages" sheet, 2026-10-05:
#   "Bank (Money Center)"  -- 15 firms, levered beta 0.761046
#   "Banks (Regional)"     -- 568 firms, levered beta 0.398477
MONEY_CENTER_BUCKET = "Bank (Money Center)"
REGIONAL_BUCKET = "Banks (Regional)"
BANK_BETA_BUCKETS = (MONEY_CENTER_BUCKET, REGIONAL_BUCKET)


def _sic_in_ranges(sic: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    return any(lo <= sic <= hi for lo, hi in ranges)


def classify_sic(sic: int | None) -> FinancialSubtype:
    """Classify purely from SIC -- no Damodaran industry string involved.
    Returns UNKNOWN if sic is None (caller decides what to do; this module
    never guesses)."""
    if sic is None:
        return FinancialSubtype.UNKNOWN
    if not (SIC_FINANCIAL_RANGE[0] <= sic <= SIC_FINANCIAL_RANGE[1]):
        return FinancialSubtype.NOT_FINANCIAL
    if _sic_in_ranges(sic, _BANK_DEPOSITORY_SIC_RANGES):
        return FinancialSubtype.BANK_DEPOSITORY
    if _sic_in_ranges(sic, _CONSUMER_FINANCE_SIC_RANGES):
        return FinancialSubtype.CONSUMER_FINANCE
    if _sic_in_ranges(sic, _BROKER_DEALER_SIC_RANGES):
        return FinancialSubtype.BROKER_DEALER
    if _sic_in_ranges(sic, _ASSET_MANAGER_SIC_RANGES):
        return FinancialSubtype.ASSET_MANAGER
    if _sic_in_ranges(sic, _INSURER_SIC_RANGES):
        return FinancialSubtype.INSURER
    return FinancialSubtype.OTHER_FINANCIAL


def _resolve_sic_6211(damodaran_industry: str | None) -> tuple[FinancialSubtype, str]:
    """
    SIC 6211 ("Security brokers, dealers, and flotation companies") is
    genuinely ambiguous at the pure-SIC level -- confirmed live 2026-10-06:
    GS, MS, SCHW, RJF all carry this SIC and are true brokers; BLK also
    carries this exact SIC but is a true asset manager.

    Resolved via an explicit, exact-match lookup against the Damodaran
    industry string (_SIC_6211_SECONDARY_MAPPING) -- never a substring or
    keyword heuristic, per Jim's explicit instruction. Returns
    (FinancialSubtype.OTHER_FINANCIAL, "sic_6211_ambiguous_unresolved") --
    not a guessed BROKER_DEALER/ASSET_MANAGER -- when the industry string
    is missing or doesn't exactly match a known value.
    """
    if damodaran_industry in _SIC_6211_SECONDARY_MAPPING:
        return _SIC_6211_SECONDARY_MAPPING[damodaran_industry], "resolved"
    return FinancialSubtype.OTHER_FINANCIAL, "sic_6211_ambiguous_unresolved"


@dataclass
class FinancialClassification:
    ticker: str
    sic: int | None
    damodaran_industry: str | None
    subtype: FinancialSubtype
    subtype_source: str             # "sic" | "unknown_no_sic" | "sic_6211_secondary:resolved"
                                     # | "sic_6211_secondary:sic_6211_ambiguous_unresolved"
    peer_beta_bucket: str | None    # only set for BANK_DEPOSITORY; one of
                                     # BANK_BETA_BUCKETS, or None if unresolved
    peer_beta_status: str           # "resolved" | "damodaran_bucket_not_bank_specific"
                                     # | "damodaran_industry_unavailable" | "not_applicable"
    notes: list[str]


def classify_financial_firm(ticker: str, sic: int | None, damodaran_industry: str | None) -> FinancialClassification:
    """
    Primary classification by SIC. Damodaran industry string is consulted
    for exactly two purposes, both explicit exact-match lookups, never
    substring/keyword matching: (1) resolving the genuinely ambiguous SIC
    6211 (see _resolve_sic_6211()) and (2) resolving the beta peer-group
    bucket for BANK_DEPOSITORY tickers. Neither ever overrides a SIC-based
    subtype that classify_sic() already resolved unambiguously. This is
    the exact mechanism that fixes COF (SIC 6021 -> BANK_DEPOSITORY
    regardless of its Damodaran bucket containing the word "insurance"),
    correctly leaves RJF/GS/MS/SCHW (SIC 6211 -> BROKER_DEALER via the
    secondary lookup) out of the bank path, and correctly distinguishes
    BLK (SIC 6211 -> ASSET_MANAGER via the same lookup) from those four.

    Does NOT invent a peer-beta fallback when the Damodaran bucket isn't
    one of the two confirmed bank-specific strings (MONEY_CENTER_BUCKET /
    REGIONAL_BUCKET) -- this is the confirmed, still-open "missing
    classification" gap from the 2026-10-04 bank-beta audit (COF/ALLY land
    in neither bucket). None of that audit's six candidate fallbacks
    (fail valuation / default to Regional / infer from SIC / infer from
    size / combined bank beta / other) were found to be evidence-
    supported, so none is implemented here -- peer_beta_bucket stays None
    and peer_beta_status explains exactly why, for the adapter layer to
    surface rather than silently guess around.
    """
    notes: list[str] = []
    subtype = classify_sic(sic)
    subtype_source = "sic" if sic is not None else "unknown_no_sic"
    if sic is None:
        notes.append(f"SIC unavailable for {ticker} -- classification is UNKNOWN, not guessed from Damodaran industry")

    if sic == AMBIGUOUS_SIC_6211:
        subtype, resolution_status = _resolve_sic_6211(damodaran_industry)
        subtype_source = f"sic_6211_secondary:{resolution_status}"
        if resolution_status != "resolved":
            notes.append(
                f"{ticker}: SIC 6211 is ambiguous (shared by true brokers and at least one "
                f"true asset manager, BLK) -- Damodaran industry ({damodaran_industry!r}) doesn't "
                "exactly match a known resolving value; classified OTHER_FINANCIAL, not guessed"
            )

    peer_beta_bucket: str | None = None
    if subtype != FinancialSubtype.BANK_DEPOSITORY:
        peer_beta_status = "not_applicable"
    elif damodaran_industry is None:
        peer_beta_status = "damodaran_industry_unavailable"
        notes.append(f"{ticker} is BANK_DEPOSITORY by SIC but Damodaran industry lookup failed -- no peer beta available")
    elif damodaran_industry in BANK_BETA_BUCKETS:
        peer_beta_bucket = damodaran_industry
        peer_beta_status = "resolved"
    else:
        peer_beta_status = "damodaran_bucket_not_bank_specific"
        notes.append(
            f"{ticker} is BANK_DEPOSITORY by SIC (sic={sic}) but its Damodaran industry "
            f"({damodaran_industry!r}) is not {MONEY_CENTER_BUCKET!r} or {REGIONAL_BUCKET!r} -- "
            "no evidence-supported fallback exists (2026-10-04 bank-beta audit); "
            "do not invent one here"
        )

    return FinancialClassification(
        ticker=ticker, sic=sic, damodaran_industry=damodaran_industry,
        subtype=subtype, subtype_source=subtype_source,
        peer_beta_bucket=peer_beta_bucket, peer_beta_status=peer_beta_status,
        notes=notes,
    )
