"""
HessGrp <-> Ginzu FCFF adapter (2026-10-01).

Input-preparation / HessGrp-data-glue layer ONLY. Gathers a ticker's real
company data via HessGrp's existing production fetchers (scripts/av_fcff_2.py,
lib/hg_dcflib.py) and HessGrp's current revenue-growth forecast methodology
(calc_hessgrp_growth_path(), commit 83032e4), assembles it into the
validated Ginzu dataclasses (lib/hg_fcff_ginzu.py), and calls the UNMODIFIED
Ginzu valuation mathematics (value_company()). Does not reimplement any
valuation formula -- every number that reaches value_company() either comes
from a HessGrp fetcher/forecast or from Ginzu's own literal default.

EXPERIMENTAL / PARALLEL PATH -- does not touch, call, or alter the existing
HessGrp FCFF path (_value_stock_fcff()/_value_stock_detail_fcff() in
av_fcff_2.py). That path remains the default and is completely unaffected by
this module's existence. See docs/decisions.md 2026-10-01 ("Ginzu FCFF core
-- parallel integration") for the full architecture and every judgment call
made while building this adapter.

KNOWN, DELIBERATE GAPS (not bugs -- see Phase 10/11 of the integration
report for the full reasoning):
  - Operating leases: HessGrp has no lease-capitalization data source today.
    has_operating_leases is always False; lease_status is always
    "not_supplied" in diagnostics (never a fabricated zero commitment).
  - Employee options: same gap. has_options_outstanding always False;
    option_status always "not_supplied".
  - NOL carryforward: HessGrp has no NOL-balance data source today.
    nol_override always False (Ginzu's own no-NOL default applies);
    nol_status always "not_supplied".
  - Failure probability: HessGrp has no failure-probability data source.
    failure_probability_override always False (Ginzu's own 0% default
    applies); failure_status always "not_supplied".
  - Cyclical-EBIT-normalization / Adaptive Growth: Ginzu has no equivalent
    mechanism (growth is an independent revenue input in Ginzu, not solved
    for to bridge EBIT). build_ginzu_valuation() DETECTS this case
    (cyclical_diag["applied"] True) and returns an explicit "unsupported"
    result rather than guessing at a translation -- see
    GinzuAdapterUnsupported and docs/decisions.md.

R&D PARTIAL-HISTORY CAPITALIZATION (2026-10-01, fixed -- was a real
implementation gap, not a deliberate one): the 12-company controlled
validation found this adapter previously required a FULL, contiguous
amortization_years+1-year R&D history or applied zero capitalization --
no partial credit, unlike the old HessGrp FCFF path's graceful degrade.
This was the primary driver of AAPL's and JNJ's valuation differences
from the old path. Fixed via research_and_development_ttm_periods()
(HessGrp's established quarterly-derived, valuation-date-aligned 12-month
period construction -- same methodology get_rAndD_intrinio()/get_rAndD()
already use, just not capped at 5 years, and each period tagged with its
anchor quarter's real year instead of a bare position) and
_partial_rd_capitalization() (the identical calc_rd_capitalization()
formula, applied to whichever real ages actually have data -- never
assumes contiguity, never fabricates a missing period). Full, contiguous
history still goes through the unmodified, locked calc_rd_capitalization()
directly -- zero behavior change for companies that already had complete
history.

CORRECTION, same day: an earlier version of this fix switched the R&D
history source to Intrinio's true fiscal-year (FY) periods -- Jim
explicitly rejected that period-basis change and it was reversed.
HessGrp's canonical flow-variable methodology is quarterly-derived
trailing-12-month periods, not fiscal-year statements, even where FY data
happen to be directly available. The partial-history/provenance behavior
(the actual fix) was kept; only the period-construction basis was
restored. See docs/decisions.md 2026-10-01 ("R&D partial-history
capitalization") and docs/known_errors.md for the AAPL/JNJ reconciliation.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_scripts = str(Path(__file__).parent.parent / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)
_lib = str(Path(__file__).parent)
if _lib not in sys.path:
    sys.path.insert(0, _lib)

import av_fcff_2
import hg_dcflib
from hg_fcff_ginzu import (
    GinzuInputs, GinzuRDInputs, GinzuRDResult, GinzuForecastAssumptions, GinzuCostOfCapitalInputs,
    GinzuValuationResult, calc_rd_capitalization, value_company,
)

# Damodaran "Mature Market ERP +" constant, 'Country equity risk premiums'!B1
# in fcffsimpleginzu.xlsx -- a dated workbook literal, NOT the same series as
# HessGrp's EQ_PREM (implied US ERP). Confirmed directly from the workbook
# in ginzu_stable_period_cost_of_capital_audit.md (2026-10-01). This is
# Ginzu's own number, sourced once from the validated reference workbook,
# not re-derived or approximated here. Needs the same periodic-refresh
# discipline the workbook itself would need if Damodaran updates it --
# flagged in diagnostics as a static/dated constant, not a live fetch.
MATURE_MARKET_ERP_CONSTANT = 0.0423
MATURE_MARKET_ERP_AS_OF = "fcffsimpleginzu.xlsx snapshot used throughout this session's Ginzu sandbox work, 2026-09/10"


def research_and_development_ttm_periods(ticker: str, max_years: int, api_key: str, intrinio_key: str) -> list[tuple[int, float]]:
    """HessGrp's established quarterly-derived, valuation-date-aligned 12-
    month R&D history, not capped at 5 years, each period tagged with its
    anchor quarter's real year (Intrinio primary, AV fallback on failure)
    -- see hg_dcflib.get_rAndD_ttm_periods_intrinio()/get_rAndD_ttm_periods_av()'s
    docstrings. Reuses av_fcff_2's own _fetch_with_fallback() unmodified --
    same provider-switch convention every other HessGrp fetcher already
    uses, not a new mechanism.

    CORRECTION (2026-10-01, same day): an earlier version of this function
    fetched Intrinio true fiscal-year (FY) periods instead -- Jim
    explicitly rejected that period-basis change. HessGrp's canonical flow-
    variable methodology is quarterly-derived trailing-12-month periods,
    not fiscal-year statements, even where FY data happen to be directly
    available. See docs/decisions.md 2026-10-01."""
    return av_fcff_2._fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_rAndD_ttm_periods_intrinio(ticker, max_years, intrinio_key),
        lambda: hg_dcflib.get_rAndD_ttm_periods_av(ticker, max_years, api_key),
        "R&D (quarterly-derived 12-month periods)",
    )


def _partial_rd_capitalization(current_year_rd_expense: float, ages_and_values: dict[int, float], amortization_years: int) -> "GinzuRDResult":
    """2026-10-01 (12-company controlled validation, AAPL/JNJ R&D-history
    sufficiency gap): applies calc_rd_capitalization()'s EXACT per-year
    formula (see that function's docstring in lib/hg_fcff_ginzu.py -- not
    redesigned here, just not required to be exactly `amortization_years`
    contiguous entries) to whichever real prior-year ages actually have
    data. Full, contiguous history never reaches this function -- that
    case still goes through the unmodified, locked calc_rd_capitalization()
    directly, so full-history companies see zero behavior change.

    ages_and_values: {age (1=one year ago, ..., amortization_years=oldest
    year still in the window): rd_expense}. A missing age simply never
    appears as a key -- contributes nothing. Never zero-filled, averaged,
    or interpolated for a missing age -- "missing means missing" (Jim's
    explicit instruction, 2026-10-01).
    """
    n = amortization_years
    unamortized_value_sum = 0.0
    amortization_this_year_sum = 0.0
    for age, rd_expense_i in ages_and_values.items():
        year_i = -age
        unamortized_fraction_i = (n + year_i) / n
        unamortized_value_sum += rd_expense_i * unamortized_fraction_i
        amortization_this_year_sum += rd_expense_i / n

    research_asset_value = current_year_rd_expense + unamortized_value_sum
    ebit_adjustment = current_year_rd_expense - amortization_this_year_sum

    return GinzuRDResult(
        research_asset_value=research_asset_value,
        amortization_this_year=amortization_this_year_sum,
        ebit_adjustment=ebit_adjustment,
        tax_effect=0.0,  # filled in by the caller once marginal_tax_rate is known, same convention calc_rd_capitalization()'s own callers use
    )


class GinzuAdapterUnsupported(Exception):
    """Raised when a ticker genuinely cannot be routed through the Ginzu
    core without a methodological guess -- per Jim's explicit instruction,
    this is a correct, reportable outcome, not a bug to work around."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass
class HgGinzuDiagnostics:
    ticker: str
    industry: str
    sic: int | None

    valuation_date: str             # ISO date -- market inputs are selected relative to this

    risk_free_rate: float
    risk_free_series: str           # "DGS10" -- 2026-10-01 canonical choice, see docs/decisions.md
    risk_free_observation_date: str | None   # None iff source == "fallback"
    risk_free_source: str           # "live" | "cached" | "fallback"
    risk_free_failure_code: str | None       # None iff source == "live"
    risk_free_failure_detail: str | None

    equity_risk_premium: float
    erp_type: str                   # "Damodaran Implied ERP" -- never Country ERP, see module docstring
    erp_as_of_date: str | None      # None iff source == "fallback"
    erp_source: str                 # "live" | "cached" | "fallback"
    erp_failure_code: str | None    # None iff source == "live" -- tracked independently of risk_free_failure_code
    erp_failure_detail: str | None

    mature_market_erp_constant: float
    mature_market_erp_source: str  # static, dated workbook literal -- see module docstring

    rd_amortization_years: int
    rd_history_source: str         # "hessgrp_fetcher" | "not_applicable_zero_life"
    rd_capitalization_applied: bool
    rd_history_status: str         # "full" | "partial" | "none" -- 2026-10-01, see module docstring
    rd_history_years_required: int
    rd_history_years_available: int
    rd_history_years_used: int
    rd_history_missing_years: list[int]   # anchor years of the 12-month periods absent from the window, [] if none/not determinable
    rd_research_asset_value: float | None      # None iff rd_history_status == "none"
    rd_amortization_this_year: float | None    # None iff rd_history_status == "none" -- populated for
                                                # BOTH full and partial status, since the partial case's
                                                # rd_result never reaches GinzuValuationResult.rd_result
                                                # (has_rd_to_capitalize=False, folded into ebit_ltm/
                                                # book_equity_ltm directly instead -- see module docstring)

    growth_path: list[float]
    growth_source: str             # calc_hessgrp_growth_path()'s own diag["source"]

    sales_to_capital: float
    sales_to_capital_source: str   # "ginzu_global_industry_default"

    lease_status: str              # always "not_supplied" -- see module docstring
    option_status: str             # always "not_supplied"
    nol_status: str                # always "not_supplied"
    failure_status: str            # always "not_supplied"

    cyclical_normalization_applicable: bool  # always False when this diagnostics object exists
                                              # (True case raises GinzuAdapterUnsupported before construction)


@dataclass
class HgGinzuValuation:
    ticker: str
    result: GinzuValuationResult
    diagnostics: HgGinzuDiagnostics


def _risk_free_and_erp(valuation_date, attempt_live: bool) -> dict:
    """Canonical market-input sourcing for the Ginzu path only (2026-10-01,
    see docs/decisions.md "Canonical market inputs: DGS10 + Damodaran
    Implied ERP, Ginzu path only"): FRED DGS10 (not GS10 -- the old
    HessGrp FCFF path keeps GS10 via av_fcff_2.RISK_FREE, untouched) for
    risk-free, Damodaran's published Implied ERP (not Country ERP) for
    ERP, each independently run through hg_dcflib's current-retrieval ->
    cached -> fallback-constant hierarchy with full, non-conflated failure
    diagnostics. Fallback constants are the SAME existing module-level
    numbers the old path already used (av_fcff_2.RISK_FREE/EQ_PREM
    defaults, 0.0425/0.0472) -- reused as the final safety net per Jim's
    explicit "do not remove the existing fallback constants" instruction,
    not new numbers invented for this task.

    attempt_live=False skips the live network call entirely (falls
    straight to cached/fallback) -- used by tests and by any caller that
    wants a fast, deterministic run; this replaces the old parameter's
    "did not call refresh_market_data()" meaning with the equivalent
    concept under the new hierarchy."""
    rf = hg_dcflib.get_risk_free_with_hierarchy(
        av_fcff_2.FRED_KEY, valuation_date=valuation_date,
        fallback_constant=av_fcff_2.RISK_FREE, attempt_live=attempt_live,
    )
    erp = hg_dcflib.get_erp_with_hierarchy(
        valuation_date=valuation_date,
        fallback_constant=av_fcff_2.EQ_PREM, attempt_live=attempt_live,
    )
    return {
        "risk_free_rate": rf["value"], "risk_free_source": rf["source"],
        "risk_free_observation_date": rf["observation_date"],
        "risk_free_failure_code": rf["failure_code"], "risk_free_failure_detail": rf["failure_detail"],
        "equity_risk_premium": erp["value"], "erp_source": erp["source"],
        "erp_as_of_date": erp["as_of_date"],
        "erp_failure_code": erp["failure_code"], "erp_failure_detail": erp["failure_detail"],
    }


def build_ginzu_valuation(
    ticker: str, growth_period: int = 5, db_path: str | None = None,
    refresh_market_data: bool = True,
) -> HgGinzuValuation:
    """The HessGrp company data -> GinzuFCFFInputs -> Ginzu math -> result
    boundary, end to end, for one non-financial, non-REIT, non-cyclical
    ticker. Raises GinzuAdapterUnsupported for cyclical tickers, financial/
    REIT tickers, or insufficient R&D history -- callers (e.g. a sample-run
    loop) should catch this and record it as "unsupported", not a crash.
    """
    MY_API_KEY = av_fcff_2.MY_API_KEY
    INTRINIO_KEY = av_fcff_2.INTRINIO_KEY

    industry = hg_dcflib.get_industry(ticker)
    sic = hg_dcflib.get_sic(ticker, INTRINIO_KEY)
    if sic is not None and av_fcff_2.is_financial_sic(sic):
        raise GinzuAdapterUnsupported("financial_or_reit_sic", f"SIC {sic}")
    if av_fcff_2.is_reit(industry):
        raise GinzuAdapterUnsupported("reit_industry", industry)
    if av_fcff_2.is_financial_firm(industry):
        raise GinzuAdapterUnsupported("financial_firm_industry", industry)

    inc_stmnt = av_fcff_2.income_statement(ticker, MY_API_KEY)
    bal_sht = av_fcff_2.balance_sheet(ticker, MY_API_KEY)
    ent_quote = av_fcff_2.enterprise_quote(ticker, MY_API_KEY)
    price, shares_outstanding, market_cap = ent_quote[0], ent_quote[1], ent_quote[2]

    raw_ebit = inc_stmnt["ebit"][0]
    revenue = inc_stmnt["totalRevenue"][0]
    effective_tax_rate = av_fcff_2.calc_tax_rate(inc_stmnt)
    marginal_tax_rate = av_fcff_2.MARGINAL_TAX_RATE
    bv_debt = av_fcff_2.calc_bv_debt(bal_sht)
    book_equity = bal_sht["total_stockholders_equity"][0]
    cash = bal_sht["cash_and_equivalents"][0]

    # --- Cyclical normalization: no Ginzu-core equivalent, STOP per Phase 7 ---
    annual_reports = []
    try:
        annual_reports = av_fcff_2.annual_income_statement(ticker, MY_API_KEY)
        _, cyclical_diag = av_fcff_2.calc_cyclical_normalized_ebit(
            ticker, raw_ebit, revenue, annual_reports
        )
    except Exception as exc:
        cyclical_diag = {"applied": False, "reason": f"fetch_failed: {exc}"}
    if cyclical_diag.get("applied"):
        raise GinzuAdapterUnsupported(
            "cyclical_normalization_no_ginzu_equivalent",
            "Adaptive Growth fired for this ticker; Ginzu has no EBIT-normalization "
            "concept (growth is an independent revenue input, not solved to bridge "
            "EBIT) -- see docs/decisions.md 2026-10-01.",
        )

    # --- R&D: amortization life from HessGrp, history from HessGrp's
    # established quarterly-derived, valuation-date-aligned 12-month
    # period construction (2026-10-01 fix, corrected same day -- see
    # research_and_development_ttm_periods()/_partial_rd_capitalization()'s
    # docstrings and docs/decisions.md 2026-10-01), capitalization math
    # from Ginzu (full-history case: the unmodified, locked
    # calc_rd_capitalization(); partial-history case: the algebraically
    # identical _partial_rd_capitalization() above, applied only to the
    # real ages that actually have data).
    rd_n = hg_dcflib.get_rAndD_years(industry)
    rd_inputs = None
    rd_result = None
    rd_history_source = "not_applicable_zero_life"
    rd_history_status = "none"
    rd_history_years_required = int(rd_n) if rd_n else 0
    rd_history_years_available = 0
    rd_history_years_used = 0
    rd_history_missing_years: list[int] = []

    if rd_n and rd_n > 0:
        n = int(rd_n)
        history_years = n + 1
        ttm_history = research_and_development_ttm_periods(ticker, history_years, MY_API_KEY, INTRINIO_KEY)
        rd_history_source = "hessgrp_fetcher"
        if ttm_history:
            current_period_anchor, current_rd = ttm_history[0]
            # Real age from each 12-month period's real anchor-quarter
            # year -- a gap shifts nothing, because position in the list
            # is never used as age. First (most-recent) occurrence of a
            # given age wins if a vendor ever returns a duplicate anchor
            # year (defensive; shouldn't happen with real data).
            ages_seen: dict[int, float] = {}
            for anchor, val in ttm_history[1:]:
                age = current_period_anchor - anchor
                if 1 <= age <= n and age not in ages_seen:
                    ages_seen[age] = val

            rd_history_years_available = len(ages_seen)
            rd_history_years_used = rd_history_years_available
            rd_history_missing_years = sorted(
                current_period_anchor - age for age in range(1, n + 1) if age not in ages_seen
            )

            if rd_history_years_available == n:
                # Full, contiguous history -- EXACT prior code path, zero
                # behavior change for companies that already had complete
                # history (Jim's explicit "no regression" requirement).
                ordered_values = [ages_seen[age] for age in range(1, n + 1)]
                rd_inputs = GinzuRDInputs(
                    amortization_years=n,
                    current_year_rd_expense=current_rd,
                    prior_years_rd_expense=ordered_values,
                )
                rd_history_status = "full"
            elif rd_history_years_available > 0:
                rd_result = _partial_rd_capitalization(current_rd, ages_seen, n)
                rd_result.tax_effect = rd_result.ebit_adjustment * marginal_tax_rate
                rd_history_status = "partial"
            else:
                rd_history_status = "none"  # current year exists, but zero valid prior ages in-window

    has_rd = rd_inputs is not None or rd_result is not None
    rd_capital_fold_in = 0.0
    adjusted_ebit_base = raw_ebit
    if rd_inputs is not None:
        rd_result = calc_rd_capitalization(rd_inputs)
        adjusted_ebit_base = raw_ebit + rd_result.ebit_adjustment
    elif rd_result is not None:
        # Partial-history case: fold the adjustment in here (adapter/
        # input-preparation layer) rather than through value_company()'s
        # own has_rd_to_capitalize path, since calc_rd_capitalization()
        # itself hard-requires exactly `amortization_years` contiguous
        # entries (by design -- it's the locked, validated engine, not to
        # be modified for this). ebit_ltm/book_equity_ltm below get this
        # same adjustment folded in directly; has_rd_to_capitalize stays
        # False so value_company() does not attempt to recompute it.
        adjusted_ebit_base = raw_ebit + rd_result.ebit_adjustment
        rd_capital_fold_in = rd_result.research_asset_value
    operating_margin_year1 = adjusted_ebit_base / revenue if revenue else 0.0

    # --- Growth: HessGrp's current production methodology (commit 83032e4) ---
    growth_path, growth_diag = av_fcff_2.calc_hessgrp_growth_path(
        ticker, industry, annual_reports, growth_period=growth_period
    )
    if growth_period != 5 or len(growth_path) != 5:
        raise GinzuAdapterUnsupported(
            "growth_path_length_mismatch",
            f"value_company()'s stepped-growth extension requires exactly 5 years, got {len(growth_path)}",
        )

    # --- Sales/Capital: Ginzu's own default source (Global industry avg) ---
    sales_to_capital = hg_dcflib.get_industry_sales_to_capital(industry)

    # --- Cost of capital ---
    # valuation_date computed once, here, and reused for both market-input
    # selection (below) and GinzuInputs -- Jim's explicit instruction: "Do
    # not substitute an arbitrary market-data date for valuation_date."
    valuation_date = av_fcff_2.date.today()
    market = _risk_free_and_erp(valuation_date, attempt_live=refresh_market_data)
    risk_free = market["risk_free_rate"]
    eq_prem = market["equity_risk_premium"]
    unlevered_beta = hg_dcflib.get_beta(industry)
    interest_expense = inc_stmnt["interest_expense"][0]
    int_cover = av_fcff_2.calc_interest_coverage(raw_ebit, interest_expense, bv_debt, risk_free)
    def_spread = hg_dcflib.get_default_spread(int_cover)
    pre_tax_cost_of_debt = risk_free + def_spread

    # Partial-history R&D is folded directly into ebit_ltm/book_equity_ltm
    # here (adapter/input-preparation layer), NOT via has_rd_to_capitalize,
    # since calc_rd_capitalization() itself requires exactly
    # `amortization_years` contiguous entries -- see the R&D section above.
    # Full-history/no-R&D cases are completely unaffected: ebit_ltm/
    # book_equity_ltm stay raw, has_rd_to_capitalize drives
    # value_company()'s own (unmodified) internal computation exactly as
    # before.
    ebit_ltm_for_ginzu = raw_ebit
    book_equity_ltm_for_ginzu = book_equity
    if rd_inputs is None and rd_result is not None:
        ebit_ltm_for_ginzu = raw_ebit + rd_result.ebit_adjustment
        book_equity_ltm_for_ginzu = book_equity + rd_result.research_asset_value

    ginzu_inputs = GinzuInputs(
        valuation_date=valuation_date,
        company_name=ticker,
        country="United States",
        industry_us=industry,
        industry_global=industry,
        revenue_ltm=revenue,
        ebit_ltm=ebit_ltm_for_ginzu,
        interest_expense_ltm=interest_expense,
        book_equity_ltm=book_equity_ltm_for_ginzu,
        book_debt_ltm=bv_debt,
        cash_ltm=cash,
        non_operating_assets_ltm=0.0,
        minority_interest_ltm=0.0,
        shares_outstanding=shares_outstanding,
        stock_price=price,
        effective_tax_rate=effective_tax_rate,
        marginal_tax_rate=marginal_tax_rate,
        has_rd_to_capitalize=rd_inputs is not None,
        has_operating_leases=False,   # not_supplied, see module docstring
        has_options_outstanding=False,  # not_supplied, see module docstring
    )

    coc_inputs = GinzuCostOfCapitalInputs(
        risk_free_rate=risk_free,
        unlevered_beta=unlevered_beta,
        equity_risk_premium=eq_prem,
        pre_tax_cost_of_debt=pre_tax_cost_of_debt,
        market_value_equity=shares_outstanding * price,
        market_value_debt=bv_debt,
        market_value_preferred=0.0,
        mature_market_erp_constant=MATURE_MARKET_ERP_CONSTANT,
    )

    assumptions = GinzuForecastAssumptions(
        revenue_growth_year1=growth_path[0],
        revenue_growth_years2_5=growth_path[-1],
        operating_margin_year1=operating_margin_year1,
        target_operating_margin=operating_margin_year1,
        margin_convergence_year=5,
        sales_to_capital_years1_5=sales_to_capital,
        sales_to_capital_years6_10=sales_to_capital,
        reinvestment_lag_override=False,
        reinvestment_lag=1,
        tax_rate_converges_to_marginal=True,
        nol_override=False,
        failure_probability_override=False,
        stable_growth_override=False,
        risk_free_persists_override=False,
        stable_cost_of_capital_override=False,
        stable_roc_override=False,
        revenue_growth_years1_5_path=growth_path,
    )

    result = value_company(ginzu_inputs, assumptions, coc_inputs, rd_inputs=rd_inputs)

    diagnostics = HgGinzuDiagnostics(
        ticker=ticker,
        industry=industry,
        sic=sic,
        valuation_date=valuation_date.isoformat(),
        risk_free_rate=risk_free,
        risk_free_series="DGS10",
        risk_free_observation_date=market["risk_free_observation_date"],
        risk_free_source=market["risk_free_source"],
        risk_free_failure_code=market["risk_free_failure_code"],
        risk_free_failure_detail=market["risk_free_failure_detail"],
        equity_risk_premium=eq_prem,
        erp_type="Damodaran Implied ERP",
        erp_as_of_date=market["erp_as_of_date"],
        erp_source=market["erp_source"],
        erp_failure_code=market["erp_failure_code"],
        erp_failure_detail=market["erp_failure_detail"],
        mature_market_erp_constant=MATURE_MARKET_ERP_CONSTANT,
        mature_market_erp_source=MATURE_MARKET_ERP_AS_OF,
        rd_amortization_years=int(rd_n) if rd_n else 0,
        rd_history_source=rd_history_source,
        rd_capitalization_applied=has_rd,
        rd_history_status=rd_history_status,
        rd_history_years_required=rd_history_years_required,
        rd_history_years_available=rd_history_years_available,
        rd_history_years_used=rd_history_years_used,
        rd_history_missing_years=rd_history_missing_years,
        rd_research_asset_value=rd_result.research_asset_value if rd_result is not None else None,
        rd_amortization_this_year=rd_result.amortization_this_year if rd_result is not None else None,
        growth_path=growth_path,
        growth_source=growth_diag.get("source", "unknown") if growth_diag else "unknown",
        sales_to_capital=sales_to_capital,
        sales_to_capital_source="ginzu_global_industry_default",
        lease_status="not_supplied",
        option_status="not_supplied",
        nol_status="not_supplied",
        failure_status="not_supplied",
        cyclical_normalization_applicable=False,
    )

    return HgGinzuValuation(ticker=ticker, result=result, diagnostics=diagnostics)
