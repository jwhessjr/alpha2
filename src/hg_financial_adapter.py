"""
HessGrp data -> (DivGinzuInputs, EqExRetInputs) -> both locked engines ->
cross-model diagnostic, for financial firms. This is the HessGrp-specific
glue layer; the two engines (lib/hg_financial_divginzu.py,
lib/hg_financial_eqexret.py) and the classifier (lib/hg_financial_classifier.py)
stay pure and reusable. Mirrors the lib/hg_fcff_ginzu.py +
lib/hg_fcff_ginzu_adapter.py separation already established for the FCFF
Ginzu port (2026-10-01).

ARCHITECTURE (approved 2026-10-05, docs/decisions.md):
  PRIMARY:   DivGinzu (lib/hg_financial_divginzu.py)
  SECONDARY: corrected EqExRet (lib/hg_financial_eqexret.py), a flagged
             cross-check only -- never blended/averaged into the primary
             number.

This is a PARALLEL, net-new path. `scripts/av_fcff_2.py`'s existing
`value_bank_stock()` / `_value_bank_stock_detail()` are UNCHANGED and
remain the production default -- nothing here is wired into
`value_stock()`'s routing yet. Call `build_financial_valuation()` directly
for controlled ticker tests.

------------------------------------------------------------------------
STABLE-PHASE ROE -- RESOLVED 2026-10-05
------------------------------------------------------------------------
Both Damodaran spreadsheets require a STABLE-PHASE ROE as a direct input.
Confirmed from both workbooks (Inputs!B32 in divginzu.xls, Inputs!B25 in
eqexret.xls): this is hand-typed analyst judgment, never a formula output.
Flagged "NORMALIZED ROE DECISION REQUIRED" in the 2026-10-04 Excess Return
audit; resolved by a dedicated audit the same week and approved by Jim:

  Stable ROE = Damodaran's own industry-average ROE for the ticker's
  confirmed financial peer bucket (lib/hg_financial_stable_roe.py).

NOT stable cost of equity (the prior placeholder, removed), NOT a
company-specific premium, NOT a moat adjustment, NOT an arbitrary fixed
spread. See lib/hg_financial_stable_roe.py's own docstring for full
provenance and docs/decisions.md, 2026-10-05, "Stable-ROE: Damodaran
industry-average ROE" for the approval record.
------------------------------------------------------------------------
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

# Resolved relative to this file, not a hardcoded path into the other
# (primary HessGrp) checkout -- mirrors the fix applied to the primary
# lib/hg_financial_adapter.py copy. This Alpha2 tree uses a flat src/
# layout (no lib/ vs scripts/ split), so both av_fcff_2.py and hg_dcflib.py
# already live alongside this file in src/ -- one insert covers both.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pandas as pd

import av_fcff_2 as prod
import hg_dcflib
from hg_financial_classifier import (
    FinancialSubtype, classify_financial_firm, FinancialClassification,
)
from hg_financial_divginzu import DivGinzuInputs, DivGinzuResult, value_divginzu, DivGinzuInvalidInputs
from hg_financial_eqexret import EqExRetInputs, EqExRetResult, value_eqexret, EqExRetInvalidInputs
from hg_financial_stable_roe import get_stable_roe, StableRoeUnavailable

BETAS_WORKBOOK_PATH = str(_REPO_ROOT / "reference_data" / "betas_2026.xls")


class HgFinancialAdapterUnsupported(Exception):
    """Raised when a ticker genuinely cannot be routed through these
    engines without a methodological guess -- a correct, reportable
    outcome (same convention as lib/hg_fcff_ginzu_adapter.py's
    GinzuAdapterUnsupported), not a bug to silently work around."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass
class CrossModelDiagnostic:
    divginzu_iv: float
    eqexret_iv: float
    absolute_difference: float
    percentage_divergence: float    # (eqexret - divginzu) / divginzu
    divginzu_terminal_pct: float
    eqexret_terminal_pct: float
    current_roe: float
    stable_roe: float
    current_cost_of_equity: float
    stable_cost_of_equity: float
    current_payout: float
    stable_payout: float
    primary_warnings: list[str]
    cross_check_warnings: list[str]
    divergence_flag: str | None      # descriptive only, never a hard reject --
                                       # see docs/decisions.md 2026-10-05,
                                       # "divergence policy"


@dataclass
class HgFinancialValuation:
    ticker: str
    classification: FinancialClassification
    beta_peer_levered: float | None
    beta_peer_source: str
    risk_free_rate: float
    risk_free_source: str
    equity_risk_premium: float
    erp_source: str
    stable_roe: float
    stable_roe_source: str
    stable_roe_bucket: str
    stable_roe_vintage: str
    stable_roe_override_applied: bool
    divginzu: DivGinzuResult
    eqexret: EqExRetResult
    diagnostics: CrossModelDiagnostic


def _peer_levered_beta(damodaran_bucket: str) -> float:
    """Look up Damodaran's current average LEVERED beta for a confirmed
    bank peer-group bucket (Money Center / Regional) -- the policy
    approved 2026-10-05: use this number DIRECTLY as cost-of-equity beta,
    never unlever/relever by the subject bank's own D/E. Reuses the exact
    reference file (reference_data/betas_2026.xls, "Industry Averages"
    sheet) the 2026-10-04 beta audit's own scratch script already used --
    not a new data source."""
    betas = pd.read_excel(BETAS_WORKBOOK_PATH, sheet_name="Industry Averages", skiprows=9, engine="xlrd")
    row = betas[betas["Industry Name"] == damodaran_bucket]
    if row.empty:
        raise HgFinancialAdapterUnsupported(
            "peer_beta_lookup_failed", f"No row for {damodaran_bucket!r} in {BETAS_WORKBOOK_PATH}"
        )
    return float(row["Beta "].iloc[0])


def _risk_free_and_erp(valuation_date: str, attempt_live: bool) -> dict:
    """Canonical market-input sourcing, identical pattern to
    lib/hg_fcff_ginzu_adapter.py's _risk_free_and_erp() (2026-10-01) --
    reused rather than duplicated. FRED DGS10 for risk-free, Damodaran's
    published Implied ERP for ERP, each independently run through
    hg_dcflib's live -> cached -> fallback hierarchy."""
    rf = hg_dcflib.get_risk_free_with_hierarchy(
        prod.FRED_KEY, valuation_date=valuation_date,
        fallback_constant=prod.RISK_FREE, attempt_live=attempt_live,
    )
    erp = hg_dcflib.get_erp_with_hierarchy(
        valuation_date=valuation_date, fallback_constant=prod.EQ_PREM, attempt_live=attempt_live,
    )
    return {
        "risk_free_rate": rf["value"], "risk_free_source": rf["source"],
        "equity_risk_premium": erp["value"], "erp_source": erp["source"],
    }


def build_financial_valuation(
    ticker: str, growth_period: int = prod.GROWTH_PERIOD, attempt_live_market_data: bool = True,
    stable_roe_override: float | None = None,
) -> HgFinancialValuation:
    """
    The HessGrp data -> (DivGinzuInputs, EqExRetInputs) -> both engines ->
    cross-model diagnostic boundary, for one financial-firm ticker.

    Raises HgFinancialAdapterUnsupported for: non-financial tickers (SIC
    outside the financial division and no override); a confirmed
    BANK_DEPOSITORY ticker whose Damodaran industry bucket isn't one of
    the two confirmed bank-specific strings (the COF/ALLY-class gap --
    see lib/hg_financial_classifier.py's docstring; no fallback is
    invented -- NOT resolved by the 2026-10-05 stable-ROE decision, which
    answered a different question); insufficient balance-sheet/dividend
    history; or any DivGinzuInvalidInputs/EqExRetInvalidInputs the
    engines themselves raise for economically invalid stable-phase
    conditions.

    `stable_roe_override`, when supplied, is recorded in diagnostics
    (`stable_roe_override_applied=True`) rather than applied silently --
    see lib/hg_financial_stable_roe.py's get_stable_roe().
    """
    sic = hg_dcflib.get_sic(ticker, prod.INTRINIO_KEY)
    try:
        damodaran_industry = hg_dcflib.get_industry(ticker)
    except Exception:
        damodaran_industry = None

    classification = classify_financial_firm(ticker, sic, damodaran_industry)

    if classification.subtype == FinancialSubtype.NOT_FINANCIAL:
        raise HgFinancialAdapterUnsupported("not_financial", f"{ticker} SIC={sic} is outside the financial division")
    if classification.subtype == FinancialSubtype.UNKNOWN:
        raise HgFinancialAdapterUnsupported("sic_unavailable", f"{ticker} has no SIC on record")
    if classification.subtype != FinancialSubtype.BANK_DEPOSITORY:
        raise HgFinancialAdapterUnsupported(
            "subtype_not_yet_supported",
            f"{ticker} classified as {classification.subtype.value} -- only BANK_DEPOSITORY is wired to "
            "these engines so far (task scope); broker/asset-manager/insurer/consumer-finance peer-beta "
            "and normalization policy is explicitly out of scope for this pass",
        )
    if classification.peer_beta_status != "resolved":
        raise HgFinancialAdapterUnsupported(
            "peer_beta_unresolved",
            f"{ticker}: {classification.peer_beta_status} -- "
            + "; ".join(classification.notes) if classification.notes else classification.peer_beta_status,
        )

    beta_peer = _peer_levered_beta(classification.peer_beta_bucket)

    inc_stmnt = prod.income_statement(ticker, prod.MY_API_KEY, is_financial_or_reit=True)
    bal_sht = prod.balance_sheet(ticker, prod.MY_API_KEY, is_financial_or_reit=True)
    cash_flw = prod.cash_flow_statement(ticker, prod.MY_API_KEY)
    ent_quote = prod.enterprise_quote(ticker, prod.MY_API_KEY)

    shares_outstanding = ent_quote[1]

    if len(bal_sht["total_stockholders_equity"]) < 2:
        raise HgFinancialAdapterUnsupported("insufficient_balance_sheet_history", f"{ticker} needs 2 years of book equity")

    net_income = inc_stmnt["netIncome"][0]
    bv_equity_curr = bal_sht["total_stockholders_equity"][0]
    bv_equity_prior = bal_sht["total_stockholders_equity"][1]

    # Beginning-of-period ROE -- the first confirmed gap (2026-10-04 Excess
    # Return audit, reconfirmed from the spreadsheet itself 2026-10-05):
    # Damodaran's own reference models use ROE_t = NetIncome_t / BookEquity_(t-1).
    roe_current = net_income / bv_equity_prior if bv_equity_prior else 0.0

    # Reuse HessGrp's existing payout-ratio sourcing exactly as-is -- no
    # duplicate provider logic (instruction #21). This is the same
    # dividends+buybacks-as-total-shareholder-return convention already
    # established 2026-09-13 (docs/known_errors.md) and matches both
    # spreadsheets' own "current dividends" input concept.
    payout_current = prod._bank_payout_ratio(net_income, bv_equity_curr, bv_equity_prior, cash_flw)

    valuation_date = prod.date.today()
    market = _risk_free_and_erp(valuation_date, attempt_live_market_data)
    risk_free_rate = market["risk_free_rate"]
    erp = market["equity_risk_premium"]
    cost_of_equity_current = risk_free_rate + beta_peer * erp

    # Same bucket as the beta lookup above, by construction (instruction
    # #6's "beta bucket and ROE bucket must be the same economic peer
    # bucket whenever the source taxonomy supports it" -- here it always
    # does, since both reference files share the identical bucket
    # vocabulary; there is no code path where they could diverge).
    try:
        stable_roe_result = get_stable_roe(classification.peer_beta_bucket, override=stable_roe_override)
    except StableRoeUnavailable as e:
        raise HgFinancialAdapterUnsupported("stable_roe_unavailable", str(e)) from e
    stable_roe = stable_roe_result.stable_roe
    stable_roe_source = stable_roe_result.source

    fade_years = max(1, growth_period // 2) if growth_period > 1 else 0

    try:
        divginzu_result = value_divginzu(DivGinzuInputs(
            current_net_income=net_income, shares_outstanding=shares_outstanding,
            high_growth_years=growth_period, fade_years=fade_years,
            roe_explicit=roe_current, payout_explicit=payout_current,
            beta_explicit=beta_peer, risk_free_rate=risk_free_rate, erp_explicit=erp,
            roe_stable=stable_roe, ticker=ticker,
        ))
    except DivGinzuInvalidInputs as e:
        raise HgFinancialAdapterUnsupported("divginzu_invalid_inputs", str(e)) from e

    try:
        eqexret_result = value_eqexret(EqExRetInputs(
            current_book_equity=bv_equity_curr, shares_outstanding=shares_outstanding,
            high_growth_years=growth_period, fade_years=fade_years,
            roe_explicit=roe_current, payout_explicit=payout_current,
            beta_explicit=beta_peer, risk_free_rate=risk_free_rate, erp_explicit=erp,
            roe_stable=stable_roe, growth_stable=risk_free_rate, ticker=ticker,
        ))
    except EqExRetInvalidInputs as e:
        raise HgFinancialAdapterUnsupported("eqexret_invalid_inputs", str(e)) from e

    divginzu_iv = divginzu_result.value_per_share
    eqexret_iv = eqexret_result.value_per_share
    abs_diff = eqexret_iv - divginzu_iv
    pct_divergence = abs_diff / divginzu_iv if divginzu_iv else 0.0

    divergence_flag = None
    if abs(pct_divergence) > 0.50:
        divergence_flag = f"LARGE divergence: eqexret is {pct_divergence:+.1%} vs divginzu -- reported, not blocked (no threshold approved, see docs/decisions.md)"
    elif abs(pct_divergence) > 0.20:
        divergence_flag = f"Moderate divergence: eqexret is {pct_divergence:+.1%} vs divginzu"

    diagnostics = CrossModelDiagnostic(
        divginzu_iv=divginzu_iv, eqexret_iv=eqexret_iv,
        absolute_difference=abs_diff, percentage_divergence=pct_divergence,
        divginzu_terminal_pct=divginzu_result.terminal_value_pct_of_equity,
        eqexret_terminal_pct=eqexret_result.terminal_value_pct_of_equity,
        current_roe=roe_current, stable_roe=stable_roe,
        current_cost_of_equity=cost_of_equity_current,
        stable_cost_of_equity=divginzu_result.cost_of_equity_stable,
        current_payout=payout_current, stable_payout=divginzu_result.payout_stable,
        primary_warnings=list(divginzu_result.diagnostics.warnings),
        cross_check_warnings=list(eqexret_result.diagnostics.warnings),
        divergence_flag=divergence_flag,
    )

    return HgFinancialValuation(
        ticker=ticker, classification=classification,
        beta_peer_levered=beta_peer, beta_peer_source=f"damodaran_bucket:{classification.peer_beta_bucket}",
        risk_free_rate=risk_free_rate, risk_free_source=market.get("risk_free_source", "unknown"),
        equity_risk_premium=erp, erp_source=market.get("erp_source", "unknown"),
        stable_roe=stable_roe, stable_roe_source=stable_roe_source,
        stable_roe_bucket=stable_roe_result.bucket, stable_roe_vintage=stable_roe_result.source_vintage,
        stable_roe_override_applied=stable_roe_result.override_applied,
        divginzu=divginzu_result, eqexret=eqexret_result, diagnostics=diagnostics,
    )
