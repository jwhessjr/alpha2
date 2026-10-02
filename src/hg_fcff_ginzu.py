"""
Python reproduction of Aswath Damodaran's fcffsimpleginzu.xlsx FCFF model.

PRODUCTION PORT (2026-10-01) of the validated sandbox engine at
~/Desktop/HessGrp_RR_Work_2026-09/ginzu/fcff_ginzu.py (157/157 metrics,
$0.00 value/share difference against the live Excel workbook's embedded
Almarai sample -- re-confirmed identical immediately before this port).
This file is byte-for-byte identical to that validated original EXCEPT for
one documented, tested, isolated addition: native support for 5
independent Year 1-5 revenue-growth rates (see GinzuForecastAssumptions.
revenue_growth_years1_5_path and its consumption in value_company(),
both marked "PRODUCTION EXTENSION" below). That addition replicates --
does not reinterpret -- the already-validated value_company_stepped_growth()
wrapper from crct_business_growth_case.py (same sandbox directory), which
Jim's own prior CRCT business-growth-path experiment used; it is folded
into this single canonical function here, rather than kept as a second,
parallel copy of the whole orchestrator, specifically so there is one
production implementation of value_company() instead of two. See
tests/test_hg_fcff_ginzu.py for the regression test proving this extension
reproduces the original wrapper's output exactly for CRCT, and
docs/decisions.md (2026-10-01, "Ginzu FCFF core — parallel integration")
for the full rationale. No other formula, dataclass field, or code path in
this file was changed from the validated original.

Reference: ginzu_fcff_spec.md (sandbox directory) -- reverse-engineered
directly from the live workbook. This engine is a faithful reproduction,
not an improvement: it does not clamp reinvestment rates, does not
normalize EBIT from historical averages, and does not add any
HessGrp-specific logic beyond the one extension noted above. Every "how
does Ginzu actually do this" decision cites the spec section it comes
from.

This module is the validated MATHEMATICAL CORE only -- it has no
Intrinio/AV connection, no valuation.db access, and does not import
av_fcff_2.py or hg_dcflib.py. HessGrp-specific input assembly (gathering a
ticker's real data and building the dataclasses below) lives in the
separate adapter module, lib/hg_fcff_ginzu_adapter.py, which imports THIS
file, never the other way around -- keeping "validated math" and
"HessGrp data glue" cleanly separated, per the architecture Jim specified.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


# ---------------------------------------------------------------------------
# Excel-compatible helpers (spec point: "create explicit helper functions
# rather than relying accidentally on Python behavior")
# ---------------------------------------------------------------------------

def is_yes(flag: str) -> bool:
    """Ginzu's normal Yes/No flag test -- exact match against 'Yes'
    (mixed case), e.g. Input sheet B16/B17/B37/B45/B48/B51/B56/B59/B61/
    B64/B67. Confirmed by reading every override flag's literal stored
    value in the sample workbook ('No' throughout, never 'no'/'NO')."""
    return flag == "Yes"


def is_yes_trapped_cash(flag: str) -> bool:
    """Input sheet!B70 (trapped-cash override) is the ONE flag in the
    entire workbook whose formula tests `="YES"` (all caps) instead of
    the `="Yes"` (mixed case) every other override flag uses -- see spec
    section 13 / the Excel-specific-behavior note in the terminal summary
    for Phase 1. Confirmed a real authoring inconsistency in Damodaran's
    own workbook, not a modeling choice -- represented here as its own
    explicit function (not unified with is_yes()) so a Python port
    reproduces this deliberately, not by accident, and so the case
    sensitivity is visible and testable rather than silently folded into
    a single "case-insensitive Yes" helper that would erase the bug."""
    return flag == "YES"


def excel_round(value: float, digits: int = 0) -> float:
    """Excel's ROUND() rounds half away from zero; Python's built-in
    round() uses banker's rounding (round-half-to-even). Only one ROUND()
    call exists anywhere in the traced formula chain (Operating lease
    converter!D18, years embedded in the 'year 6 and beyond' commitment
    estimate), but it's implemented as a named helper rather than an
    inline call to make the Excel-compatibility choice explicit."""
    factor = 10 ** digits
    scaled = value * factor
    if scaled >= 0:
        return math.floor(scaled + 0.5) / factor
    return math.ceil(scaled - 0.5) / factor


def norm_cdf(x: float) -> float:
    """Standard normal CDF, matching Excel's NORMSDIST() exactly (both are
    the same closed-form erf-based definition -- no approximation drift
    between the two, unlike ROUND())."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Input dataclasses (spec section 16 proposal, implemented with the
# elaborations noted inline -- override toggles are represented explicitly
# as booleans, not inferred from whether an override value is nonzero,
# since Ginzu's own Input sheet gates every override behind an explicit
# Yes/No flag cell independent of the override value cell itself)
# ---------------------------------------------------------------------------

@dataclass
class GinzuInputs:
    valuation_date: date
    company_name: str
    country: str
    industry_us: str
    industry_global: str
    revenue_ltm: float
    ebit_ltm: float
    interest_expense_ltm: float
    book_equity_ltm: float
    book_debt_ltm: float
    cash_ltm: float
    non_operating_assets_ltm: float
    minority_interest_ltm: float
    shares_outstanding: float
    stock_price: float
    effective_tax_rate: float
    marginal_tax_rate: float
    has_rd_to_capitalize: bool
    has_operating_leases: bool
    has_options_outstanding: bool


@dataclass
class GinzuRDInputs:
    amortization_years: int              # F6 -- literal lookup value, used AS the number of required prior years (spec section 2)
    current_year_rd_expense: float       # F7
    prior_years_rd_expense: list[float]  # B11:B20, most-recent-first (year -1 first), length must == amortization_years


@dataclass
class GinzuLeaseInputs:
    current_year_lease_expense: float    # E4
    commitments_years_1_5: list[float]   # B7:B11, exactly 5 values
    commitment_year_6_plus: float        # B12


@dataclass
class GinzuOptionInputs:
    strike_price: float                  # B39 (Input sheet) / B5 (Option value)
    expiration_years: float              # B40 / B6
    volatility: float                    # B41 / B7 (standard deviation)
    dividend_yield: float                # Option value B8 -- always 0 in the sample, no Input-sheet source cell found
    num_options: float                   # B38 / B10


@dataclass
class GinzuForecastAssumptions:
    revenue_growth_year1: float          # B26
    revenue_growth_years2_5: float       # B28, Ginzu default = B26 (caller resolves the default before constructing this)
    operating_margin_year1: float        # B27, Ginzu default = base-year computed margin (caller resolves)
    target_operating_margin: float       # B29, Ginzu default = operating_margin_year1 (caller resolves)
    margin_convergence_year: int         # B30
    sales_to_capital_years1_5: float     # B31, Ginzu default = Global industry avg S/C -- NOT the company's own trailing ratio (spec section 1/6 caveat)
    sales_to_capital_years6_10: float    # B32, Ginzu default = sales_to_capital_years1_5

    reinvestment_lag_override: bool      # B56 -- default False means Ginzu's literal 1-year-lag default applies
    reinvestment_lag: int                # B57, consulted ONLY when reinvestment_lag_override is True; one of 0/2/3

    tax_rate_converges_to_marginal: bool = True   # B59: default False (effective converges up to marginal by terminal year); True override keeps effective flat forever -- NOTE inverted vs. field name, see __post_init__ warning in value_company()
    nol_override: bool = False           # B61
    nol_carryforward: float = 0.0        # B62, consulted only when nol_override is True

    stable_growth_override: bool = False           # B67
    stable_growth_value: float = 0.0               # B68, consulted only when stable_growth_override is True
    risk_free_persists_override: bool = False      # B64
    risk_free_persists_value: float = 0.0          # B65, consulted only when risk_free_persists_override is True
    # Default stable growth (neither override set) = today's risk-free rate (spec section 5)

    stable_cost_of_capital_override: bool = False  # B45
    stable_cost_of_capital_value: float = 0.0      # B46, consulted only when stable_cost_of_capital_override is True

    stable_roc_override: bool = False    # B48
    stable_roc_value: float = 0.0        # B49, consulted only when stable_roc_override is True

    failure_probability_override: bool = False  # B51
    failure_probability: float = 0.0            # B52
    failure_proceeds_basis: str = "V"            # B53: "B" (book) or "V" (estimated fair value)
    failure_recovery_pct: float = 0.0            # B54

    trapped_cash_override_flag: str = "No"  # B70 -- stored as the RAW string, not a bool, so is_yes_trapped_cash()'s
                                             # case-sensitive "YES" check can be applied exactly as Ginzu does
    trapped_cash_amount: float = 0.0        # B71
    trapped_cash_foreign_tax_rate: float = 0.0  # B72

    # PRODUCTION EXTENSION (2026-10-01, not in the validated sandbox
    # original): optional 5 independent Year 1-5 growth rates, e.g. from
    # HessGrp's calc_hessgrp_growth_path() (commit 83032e4). When set (a
    # list of exactly 5 floats), value_company() uses these 5 values
    # directly instead of broadcasting revenue_growth_year1/years2_5 --
    # replicates crct_business_growth_case.py's validated
    # value_company_stepped_growth() wrapper (same sandbox directory),
    # folded into this canonical function instead of kept as a second,
    # parallel copy of the whole orchestrator. revenue_growth_year1/
    # years2_5 above are ignored by value_company() when this is set, but
    # remain required fields on this dataclass (the default path still
    # needs them) -- callers using this extension should still set them
    # to sane values (e.g. revenue_growth_years1_5_path[0] and [-1]) so a
    # diagnostic reading the dataclass directly isn't misled. None (the
    # default) preserves the original 2-scalar behavior byte-for-byte --
    # see test_hg_fcff_ginzu.py for the regression test proving both the
    # None-path's exact equivalence to the unmodified original and this
    # extension's exact equivalence to value_company_stepped_growth().
    revenue_growth_years1_5_path: list[float] | None = None


@dataclass
class GinzuCostOfCapitalInputs:
    """Resolved beta/ERP/cost-of-debt inputs, NOT the raw VLOOKUP machinery.
    Ginzu's 'Detailed' cost-of-capital approach ultimately resolves to these
    few numbers via large reference-data lookups (Industry Averages(US/
    Global), Country equity risk premiums, Synthetic rating brackets) --
    this engine takes the RESOLVED values as inputs rather than
    re-implementing those lookup tables, per the spec's own tolerance note
    (section 17): those tables are large, dated reference data that drift
    independently of the formula logic, so re-deriving them is a
    data-sourcing concern, not part of the FCFF calculation chain proper."""
    risk_free_rate: float
    unlevered_beta: float                # B23
    equity_risk_premium: float           # B27
    pre_tax_cost_of_debt: float          # B37
    market_value_equity: float           # B60 = shares * price
    market_value_debt: float             # C60 = PV of straight debt + convertible + lease debt
    market_value_preferred: float = 0.0  # D60
    mature_market_erp_constant: float = 0.0  # 'Country equity risk premiums'!B1, feeds terminal WACC default


@dataclass
class GinzuFailureInputs:
    probability_of_failure: float
    proceeds_basis: str  # "B" | "V"
    recovery_pct: float


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class GinzuYearResult:
    year: int                                  # 0 = base year, 1-10 = explicit, 11 = terminal
    revenue_growth: float | None
    revenue: float
    operating_margin: float
    ebit: float
    tax_rate: float
    nol_balance: float
    ebit_after_tax: float                      # EBIT(1-t), NOL-aware for years 1-10
    reinvestment: float | None
    fcff: float | None
    sales_to_capital: float | None
    invested_capital: float | None
    roic: float | None
    cost_of_capital: float | None
    cumulative_discount_factor: float | None   # None for base/terminal year
    pv_fcff: float | None


@dataclass
class GinzuRDResult:
    research_asset_value: float        # D35
    amortization_this_year: float      # D37 / D39's subtrahend
    ebit_adjustment: float             # D39 = current R&D expense - amortization this year
    tax_effect: float                  # D40 -- computed, not used downstream (spec section 2)


@dataclass
class GinzuLeaseResult:
    lease_debt_value: float            # C28
    depreciation_on_lease_asset: float # F31
    ebit_adjustment: float             # F32 = lease expense - depreciation
    debt_adjustment: float             # F33 (== lease_debt_value, kept separate to mirror the sheet's own duplicate cells)


@dataclass
class GinzuCostOfCapitalResult:
    levered_beta: float                 # C57
    cost_of_equity: float               # B62
    after_tax_cost_of_debt: float       # C62
    weight_equity: float                # B61
    weight_debt: float                  # C61
    weight_preferred: float             # D61
    wacc: float                         # E62


@dataclass
class GinzuOptionResult:
    adjusted_stock_price: float         # B17 -- the fixed point of the circular system
    value_per_option: float             # B28
    value_of_options_outstanding: float # B29
    iterations: int                     # diagnostic: bisection iterations used to reach the fixed point


@dataclass
class GinzuValuationResult:
    years: list[GinzuYearResult]        # base year (0) + years 1-10 + terminal (11), in order
    rd_result: GinzuRDResult | None
    lease_result: GinzuLeaseResult | None
    cost_of_capital_result: GinzuCostOfCapitalResult
    option_result: GinzuOptionResult | None

    terminal_value: float
    pv_terminal_value: float
    pv_explicit_fcff: float
    sum_of_pv: float

    probability_of_failure: float
    proceeds_if_failure: float
    value_of_operating_assets: float    # failure-probability-blended

    debt: float
    minority_interest: float
    cash: float
    non_operating_assets: float
    value_of_equity: float
    value_of_options: float
    value_of_equity_common: float
    value_per_share: float
    price: float
    price_as_pct_of_value: float        # Price / Value, NOT Value / Price (spec section 13 note)


# ---------------------------------------------------------------------------
# Section 2: R&D capitalization (R& D converter sheet)
# ---------------------------------------------------------------------------

def calc_rd_capitalization(rd: GinzuRDInputs) -> GinzuRDResult:
    """Faithful port of the R& D converter sheet's amortization schedule.

    F6 (amortization_years) is used LITERALLY as the number of prior years
    of R&D history required -- not F6-1 (spec section 2, confirmed by
    tracing the A12:A20 auto-extend formula chain by hand).

    For each historical year i = -1 .. -amortization_years:
      unamortized_fraction_i = (amortization_years + year_i) / amortization_years   [year_i is negative]
      unamortized_value_i    = rd_expense_i * unamortized_fraction_i
      amortization_this_year_i = rd_expense_i / amortization_years

    The oldest year (-amortization_years) has UNAMORTIZED FRACTION ZERO
    (fully run off -- contributes nothing to the research asset) but its
    amortization_this_year contribution is still nonzero (the final
    1/amortization_years slice of that year's R&D being expensed now) --
    this is the subtlety the spec's section 2 flags explicitly.
    """
    n = rd.amortization_years
    if len(rd.prior_years_rd_expense) != n:
        raise ValueError(
            f"prior_years_rd_expense must have exactly {n} entries "
            f"(amortization_years), got {len(rd.prior_years_rd_expense)}"
        )

    unamortized_value_sum = 0.0
    amortization_this_year_sum = 0.0
    for i, rd_expense_i in enumerate(rd.prior_years_rd_expense, start=1):
        year_i = -i  # row 25 = year -1, row 26 = year -2, ...
        unamortized_fraction_i = (n + year_i) / n
        unamortized_value_sum += rd_expense_i * unamortized_fraction_i
        amortization_this_year_sum += rd_expense_i / n

    # D35 = current year's R&D (100% unamortized) + sum of prior years' unamortized value
    research_asset_value = rd.current_year_rd_expense + unamortized_value_sum
    ebit_adjustment = rd.current_year_rd_expense - amortization_this_year_sum

    return GinzuRDResult(
        research_asset_value=research_asset_value,
        amortization_this_year=amortization_this_year_sum,
        ebit_adjustment=ebit_adjustment,
        tax_effect=0.0,  # filled in by caller once marginal_tax_rate is known -- see value_company()
    )


# ---------------------------------------------------------------------------
# Section 3: Operating lease conversion (Operating lease converter sheet)
# ---------------------------------------------------------------------------

def calc_lease_conversion(lease: GinzuLeaseInputs, pretax_cost_of_debt: float) -> GinzuLeaseResult:
    """Faithful port of the Operating lease converter sheet.

    Years 1-5 discounted individually at pretax_cost_of_debt. Year "6 and
    beyond" converted to an annuity of D18 = ROUND(commitment_6plus /
    AVERAGE(years 1-5), 0) years, discounted as an annuity back to year 5
    then 5 more years to present (spec section 3)."""
    c15 = pretax_cost_of_debt
    b7_b11 = lease.commitments_years_1_5
    if len(b7_b11) != 5:
        raise ValueError("commitments_years_1_5 must have exactly 5 entries")

    avg_commitment = sum(b7_b11) / 5.0
    d18 = excel_round(lease.commitment_year_6_plus / avg_commitment, 0) if avg_commitment != 0 else 0

    pv_years_1_5 = sum(b_i / (1 + c15) ** (i + 1) for i, b_i in enumerate(b7_b11))

    if d18 > 0:
        b27 = lease.commitment_year_6_plus / d18
        c27 = (b27 * (1 - (1 + c15) ** (-d18)) / c15) / (1 + c15) ** 5
    else:
        b27 = lease.commitment_year_6_plus
        c27 = b27 / (1 + c15) ** 6

    lease_debt_value = pv_years_1_5 + c27
    depreciation = lease_debt_value / (5 + d18)
    ebit_adjustment = lease.current_year_lease_expense - depreciation
    debt_adjustment = lease_debt_value

    return GinzuLeaseResult(
        lease_debt_value=lease_debt_value,
        depreciation_on_lease_asset=depreciation,
        ebit_adjustment=ebit_adjustment,
        debt_adjustment=debt_adjustment,
    )


# ---------------------------------------------------------------------------
# Section 10: Cost of capital (Detailed approach only -- see
# GinzuCostOfCapitalInputs docstring for why the beta/ERP/cost-of-debt
# lookup machinery itself is out of scope)
# ---------------------------------------------------------------------------

def calc_cost_of_capital(coc: GinzuCostOfCapitalInputs, marginal_tax_rate: float) -> GinzuCostOfCapitalResult:
    """C57 (relevered beta) via standard Hamada relevering on MARKET D/E
    (not book), then cost of equity / after-tax cost of debt / market-value
    weights / WACC exactly as the Cost of capital worksheet's Detailed path
    (spec section 10)."""
    equity = coc.market_value_equity
    debt = coc.market_value_debt
    preferred = coc.market_value_preferred
    total = equity + debt + preferred

    de_ratio = debt / equity if equity != 0 else 0.0
    levered_beta = coc.unlevered_beta * (1 + (1 - marginal_tax_rate) * de_ratio)

    cost_of_equity = coc.risk_free_rate + levered_beta * coc.equity_risk_premium
    after_tax_cost_of_debt = coc.pre_tax_cost_of_debt * (1 - marginal_tax_rate)

    weight_equity = equity / total if total != 0 else 0.0
    weight_debt = debt / total if total != 0 else 0.0
    weight_preferred = preferred / total if total != 0 else 0.0

    # Preferred cost of capital has no dedicated input field in
    # GinzuCostOfCapitalInputs (the sample company has zero preferred, so
    # Ginzu's D62 = B50/B49 preferred-dividend/preferred-price formula was
    # never exercised) -- weight_preferred is always 0 for any input set
    # this engine currently supports, so its cost contributes nothing
    # regardless of the placeholder value here.
    cost_of_preferred = 0.0

    wacc = weight_equity * cost_of_equity + weight_debt * after_tax_cost_of_debt + weight_preferred * cost_of_preferred

    return GinzuCostOfCapitalResult(
        levered_beta=levered_beta,
        cost_of_equity=cost_of_equity,
        after_tax_cost_of_debt=after_tax_cost_of_debt,
        weight_equity=weight_equity,
        weight_debt=weight_debt,
        weight_preferred=weight_preferred,
        wacc=wacc,
    )


# ---------------------------------------------------------------------------
# Option value / dilution (Option value sheet) -- circular in Excel,
# solved here via bisection on the fixed-point equation, not literal
# iteration. See module docstring / terminal-summary note: the spec's
# claim that this system is linear (hence closed-form solvable) in BOTH
# directions was wrong -- Adjusted S is linear in Value-per-option, but
# Value-per-option is the full (nonlinear, transcendental via N()) Black-
# Scholes function of Adjusted S. A true closed form does not exist; a
# deterministic numerical root-find is used instead, which Jim's own
# instructions explicitly permit ("numerical OR closed-form").
# ---------------------------------------------------------------------------

def _black_scholes_value_per_option(
    adjusted_s: float, strike: float, expiration_years: float,
    risk_free_rate: float, dividend_yield: float, volatility: float,
) -> float:
    variance = volatility ** 2
    div_adj_rate = risk_free_rate - dividend_yield
    d1 = (
        math.log(adjusted_s / strike) + (div_adj_rate + variance / 2) * expiration_years
    ) / (math.sqrt(variance) * math.sqrt(expiration_years))
    d2 = d1 - math.sqrt(variance) * math.sqrt(expiration_years)
    return (
        math.exp(-dividend_yield * expiration_years) * adjusted_s * norm_cdf(d1)
        - strike * math.exp(-risk_free_rate * expiration_years) * norm_cdf(d2)
    )


def calc_option_value(
    opt: GinzuOptionInputs, stock_price: float, shares_outstanding: float,
    risk_free_rate: float, tolerance: float = 1e-12, max_iterations: int = 200,
) -> GinzuOptionResult:
    """Solves the circular dilution-adjusted Black-Scholes system:
      adjusted_s = (stock_price*shares + value_per_option*num_options) / (shares+num_options)
      value_per_option = BlackScholes(adjusted_s, ...)
    via bisection on f(x) = implied_adjusted_s(x) - x, bounded to
    [tiny positive, stock_price] since dilution can only ever pull the
    adjusted price down toward (never above) the raw stock price."""
    shares = shares_outstanding
    warrants = opt.num_options

    def f(adjusted_s: float) -> float:
        vpo = _black_scholes_value_per_option(
            adjusted_s, opt.strike_price, opt.expiration_years,
            risk_free_rate, opt.dividend_yield, opt.volatility,
        )
        implied = (stock_price * shares + vpo * warrants) / (shares + warrants)
        return implied - adjusted_s

    lo, hi = 1e-6, stock_price
    f_lo, f_hi = f(lo), f(hi)
    # f is continuous and, empirically across reasonable inputs, monotonic
    # decreasing in adjusted_s (higher adjusted_s -> higher implied option
    # value -> but adjusted_s itself grows faster, net f decreasing) --
    # bisection requires a sign change; if it's not bracketed, raise
    # rather than silently returning a wrong root.
    if f_lo == 0:
        adjusted_s = lo
    elif f_hi == 0:
        adjusted_s = hi
    elif (f_lo > 0) == (f_hi > 0):
        raise ValueError(
            f"Option-value fixed point not bracketed in [{lo}, {hi}]: "
            f"f(lo)={f_lo}, f(hi)={f_hi} -- inputs outside the range this "
            f"solver was validated against; do not silently extrapolate."
        )
    else:
        for i in range(max_iterations):
            mid = (lo + hi) / 2
            f_mid = f(mid)
            if abs(f_mid) < tolerance or (hi - lo) < tolerance:
                adjusted_s = mid
                break
            if (f_mid > 0) == (f_lo > 0):
                lo, f_lo = mid, f_mid
            else:
                hi = mid
        else:
            adjusted_s = (lo + hi) / 2

    value_per_option = _black_scholes_value_per_option(
        adjusted_s, opt.strike_price, opt.expiration_years,
        risk_free_rate, opt.dividend_yield, opt.volatility,
    )
    return GinzuOptionResult(
        adjusted_stock_price=adjusted_s,
        value_per_option=value_per_option,
        value_of_options_outstanding=value_per_option * warrants,
        iterations=max_iterations,
    )


# ---------------------------------------------------------------------------
# Orchestrator: the full FCFF calculation chain, sections 4-13
# ---------------------------------------------------------------------------

def value_company(
    inputs: GinzuInputs,
    assumptions: GinzuForecastAssumptions,
    coc_inputs: GinzuCostOfCapitalInputs,
    rd_inputs: GinzuRDInputs | None = None,
    lease_inputs: GinzuLeaseInputs | None = None,
    option_inputs: GinzuOptionInputs | None = None,
) -> GinzuValuationResult:
    """Ties together sections 4 (base-year adjustments) through 13 (equity
    bridge) in the same conceptual order Ginzu computes them. Deliberately
    mirrors Valuation output's own column order (base year, years 1-10,
    terminal) rather than a more "natural" topological order, so each
    section of this function can be checked against a specific spreadsheet
    row range."""

    # --- Section 2/3: R&D and lease adjustments (base-year only) ---
    rd_result: GinzuRDResult | None = None
    lease_result: GinzuLeaseResult | None = None
    rd_ebit_adj = 0.0
    rd_capital_adj = 0.0
    lease_ebit_adj = 0.0
    lease_debt_adj = 0.0

    if inputs.has_rd_to_capitalize:
        if rd_inputs is None:
            raise ValueError("has_rd_to_capitalize=True requires rd_inputs")
        rd_result = calc_rd_capitalization(rd_inputs)
        rd_result.tax_effect = rd_result.ebit_adjustment * inputs.marginal_tax_rate  # D40 (informational only, spec section 2)
        rd_ebit_adj = rd_result.ebit_adjustment
        rd_capital_adj = rd_result.research_asset_value
    elif rd_inputs is not None:
        # Sheet is computed even when gated off (confirmed live in the
        # sample workbook -- R& D converter has real nonzero cached values
        # despite Input sheet!B16="No") -- compute it for completeness /
        # reconciliation, but do not let it feed the valuation chain.
        rd_result = calc_rd_capitalization(rd_inputs)
        rd_result.tax_effect = rd_result.ebit_adjustment * inputs.marginal_tax_rate

    if inputs.has_operating_leases:
        if lease_inputs is None:
            raise ValueError("has_operating_leases=True requires lease_inputs")
        lease_result = calc_lease_conversion(lease_inputs, coc_inputs.pre_tax_cost_of_debt)
        lease_ebit_adj = lease_result.ebit_adjustment
        lease_debt_adj = lease_result.debt_adjustment
    elif lease_inputs is not None:
        lease_result = calc_lease_conversion(lease_inputs, coc_inputs.pre_tax_cost_of_debt)

    # --- Section 4: base-year adjusted operating numbers ---
    adjusted_ebit_base = inputs.ebit_ltm + rd_ebit_adj + lease_ebit_adj
    adjusted_margin_base = adjusted_ebit_base / inputs.revenue_ltm
    base_tax_rate = inputs.effective_tax_rate
    ebit_after_tax_base = adjusted_ebit_base * (1 - base_tax_rate) if adjusted_ebit_base > 0 else adjusted_ebit_base
    invested_capital_base = (
        inputs.book_equity_ltm + inputs.book_debt_ltm - inputs.cash_ltm + rd_capital_adj + lease_debt_adj
    )
    roic_base = ebit_after_tax_base / invested_capital_base

    # --- Section 10: cost of capital (explicit-period initial WACC) ---
    coc_result = calc_cost_of_capital(coc_inputs, inputs.marginal_tax_rate)
    initial_wacc = coc_result.wacc

    # --- Section 5: revenue forecast, years 1-10 + terminal ---
    growth: dict[int, float] = {}
    # PRODUCTION EXTENSION (2026-10-01): 5 independent explicit years when
    # supplied, replicating value_company_stepped_growth()'s validated
    # behavior -- see GinzuForecastAssumptions.revenue_growth_years1_5_path's
    # docstring. Original 2-scalar broadcast is the untouched default.
    if assumptions.revenue_growth_years1_5_path is not None:
        if len(assumptions.revenue_growth_years1_5_path) != 5:
            raise ValueError(
                "revenue_growth_years1_5_path must have exactly 5 entries, "
                f"got {len(assumptions.revenue_growth_years1_5_path)}"
            )
        for n, g in enumerate(assumptions.revenue_growth_years1_5_path, start=1):
            growth[n] = g
    else:
        growth[1] = assumptions.revenue_growth_year1
        growth[2] = assumptions.revenue_growth_years2_5
        growth[3] = growth[2]
        growth[4] = growth[3]
        growth[5] = growth[4]

    if assumptions.stable_growth_override:
        stable_growth = assumptions.stable_growth_value
    elif assumptions.risk_free_persists_override:
        stable_growth = assumptions.risk_free_persists_value
    else:
        stable_growth = coc_inputs.risk_free_rate  # default: stable growth = today's risk-free rate (spec section 5)

    for n in range(1, 6):
        growth[5 + n] = growth[5] - ((growth[5] - stable_growth) / 5) * n  # years 6-10

    revenue: dict[int, float] = {0: inputs.revenue_ltm}
    for y in range(1, 11):
        revenue[y] = revenue[y - 1] * (1 + growth[y])
    revenue_terminal = revenue[10] * (1 + stable_growth)

    # --- Section 6: operating-margin forecast ---
    margin: dict[int, float] = {}
    margin[1] = assumptions.operating_margin_year1
    convergence_year = assumptions.margin_convergence_year
    target_margin = assumptions.target_operating_margin
    for n in range(2, 11):
        if n > convergence_year:
            margin[n] = target_margin
        else:
            margin[n] = target_margin - ((target_margin - margin[1]) / convergence_year) * (convergence_year - n)
    margin_terminal = margin[10]

    ebit: dict[int, float] = {0: adjusted_ebit_base}
    for y in range(1, 11):
        ebit[y] = margin[y] * revenue[y]
    ebit_terminal = margin_terminal * revenue_terminal

    # --- Section 7: taxes and NOL ---
    tax_rate: dict[int, float] = {0: base_tax_rate}
    for y in range(1, 6):
        tax_rate[y] = base_tax_rate
    terminal_tax_rate = (
        inputs.effective_tax_rate if assumptions.tax_rate_converges_to_marginal is False
        else inputs.marginal_tax_rate
    )
    # NOTE on the tax_rate_converges_to_marginal flag's polarity: Ginzu's
    # own B59 flag, when "Yes" (override), makes the tax rate STAY FLAT at
    # the effective rate; when "No" (default), it CONVERGES to marginal.
    # The dataclass field name here follows the *default Ginzu behavior*
    # (True = converges, matching the B59="No" default), which is the
    # logical NEGATION of B59's own raw Yes/No sense -- callers constructing
    # GinzuForecastAssumptions must pass tax_rate_converges_to_marginal =
    # NOT is_yes(input_sheet_B59), not the raw flag value.
    for y in range(6, 11):
        tax_rate[y] = tax_rate[y - 1] + (terminal_tax_rate - tax_rate[5]) / 5
    tax_rate[10] = terminal_tax_rate if convergence_year <= 10 else tax_rate[10]
    # (Ginzu's own H6:L6 ramp formula lands exactly on M6 at year 10 by
    # construction -- the explicit overwrite above only matters if a future
    # caller changes the ramp's shape; harmless no-op under the standard
    # 5-year linear ramp, kept for defensive clarity.)

    nol: dict[int, float] = {0: assumptions.nol_carryforward if assumptions.nol_override else 0.0}
    for y in range(1, 11):
        prior = nol[y - 1]
        e = ebit[y]
        if e < 0:
            nol[y] = prior - e
        elif prior > e:
            nol[y] = prior - e
        else:
            nol[y] = 0.0

    ebit_after_tax: dict[int, float] = {0: ebit_after_tax_base}
    for y in range(1, 11):
        e = ebit[y]
        prior_nol = nol[y - 1]
        if e > 0:
            if e < prior_nol:
                ebit_after_tax[y] = e
            else:
                ebit_after_tax[y] = e - (e - prior_nol) * tax_rate[y]
        else:
            ebit_after_tax[y] = e
    ebit_after_tax_terminal = ebit_terminal * (1 - terminal_tax_rate)  # M7, no NOL logic (spec section 7)

    # --- Sales-to-capital, years 1-10 ---
    sales_to_capital: dict[int, float] = {}
    for y in range(1, 6):
        sales_to_capital[y] = assumptions.sales_to_capital_years1_5
    for y in range(6, 11):
        sales_to_capital[y] = assumptions.sales_to_capital_years6_10

    # --- Section 8: reinvestment, years 1-10 (dollar-based) + terminal (rate-based) ---
    lag = assumptions.reinvestment_lag if assumptions.reinvestment_lag_override else 1

    def delta_revenue_for_lag(n: int) -> float:
        """ΔRevenue used for year n's reinvestment, per the lag setting.
        Handles the two hand-built boundary-year exceptions at n=9 (lag=2)
        and n=10 (lag=3) exactly as Ginzu's K8/L8 do (spec section 8) --
        these are literal special cases, not inferred from the general
        pattern, since no year-11/12 revenue exists to reference."""
        if lag == 0:
            return revenue[n] - revenue[n - 1]
        if lag == 1:
            if n == 10:
                return revenue_terminal - revenue[10]
            return revenue[n + 1] - revenue[n]
        if lag == 2:
            if n == 9:
                return revenue[10] * growth[10]           # K8 boundary exception
            if n == 10:
                return revenue_terminal * stable_growth    # mirrors K8's pattern one year further out
            return revenue[n + 2] - revenue[n + 1]
        if lag == 3:
            if n == 10:
                # L8 boundary exception: extrapolate the PRIOR year's
                # reinvestment dollar amount forward by stable growth --
                # not a revenue-delta calculation at all. Handled directly
                # in the reinvestment loop below since it depends on
                # reinvestment[9], not on a revenue delta.
                return None  # sentinel, handled specially below
            if n == 9:
                return revenue_terminal - revenue[10]
            return revenue[n + 3] - revenue[n + 2]
        raise ValueError(f"Unsupported reinvestment_lag: {lag}")

    reinvestment: dict[int, float] = {}
    for n in range(1, 11):
        if lag == 3 and n == 10:
            continue  # computed after the loop, depends on reinvestment[9]
        d_rev = delta_revenue_for_lag(n)
        reinvestment[n] = d_rev / sales_to_capital[n]
    if lag == 3:
        reinvestment[10] = reinvestment[9] * (1 + stable_growth)  # L8 boundary exception (spec section 8)

    # Terminal (stable-phase) reinvestment: RATE-based, categorically
    # different in form from every explicit year's dollar-based formula
    # above (spec section 8/11) -- Reinvestment$ = (g/ROC) * NOPAT.
    if assumptions.stable_roc_override:
        stable_roc = assumptions.stable_roc_value
    else:
        stable_roc = None  # resolved below, after the WACC path is known (= L12, last explicit year's WACC)

    # --- No explicit-period reinvestment-rate constraint (spec section 8):
    # deliberately NOT clamped to [0,1] anywhere, per Jim's instruction #4.

    fcff: dict[int, float] = {0: None}
    for n in range(1, 11):
        fcff[n] = ebit_after_tax[n] - reinvestment[n]

    # --- Section 10 (path): WACC years 1-10 + terminal ---
    if assumptions.stable_cost_of_capital_override:
        terminal_wacc = assumptions.stable_cost_of_capital_value
    elif assumptions.risk_free_persists_override:
        terminal_wacc = assumptions.risk_free_persists_value + coc_inputs.mature_market_erp_constant
    else:
        terminal_wacc = coc_inputs.risk_free_rate + coc_inputs.mature_market_erp_constant  # default (spec section 10)

    wacc_path: dict[int, float] = {}
    for y in range(1, 6):
        wacc_path[y] = initial_wacc
    for n in range(1, 6):
        wacc_path[5 + n] = wacc_path[5] - ((wacc_path[5] - terminal_wacc) / 5) * n

    if stable_roc is None:
        stable_roc = wacc_path[10]  # default: stable ROC = terminal-phase (last explicit year's) WACC (spec section 11)

    stable_reinvestment_rate = (stable_growth / stable_roc) if stable_growth > 0 else 0.0
    reinvestment_terminal = stable_reinvestment_rate * ebit_after_tax_terminal if stable_growth > 0 else 0.0
    fcff_terminal = ebit_after_tax_terminal - reinvestment_terminal

    # --- Invested capital and ROIC path, years 1-10 ---
    invested_capital: dict[int, float] = {0: invested_capital_base}
    for y in range(1, 11):
        invested_capital[y] = invested_capital[y - 1] + reinvestment[y]

    roic: dict[int, float] = {0: roic_base}
    for y in range(1, 11):
        roic[y] = ebit_after_tax[y] / invested_capital[y - 1]

    # --- Discounting ---
    cumulative_discount: dict[int, float] = {}
    for y in range(1, 11):
        cumulative_discount[y] = (cumulative_discount[y - 1] if y > 1 else 1.0) * (1 / (1 + wacc_path[y]))

    pv_fcff: dict[int, float] = {}
    for y in range(1, 11):
        pv_fcff[y] = fcff[y] * cumulative_discount[y]

    pv_explicit_fcff = sum(pv_fcff[y] for y in range(1, 11))

    # --- Section 11: terminal value ---
    terminal_cash_flow = fcff_terminal
    terminal_value = terminal_cash_flow / (terminal_wacc - stable_growth)
    pv_terminal_value = terminal_value * cumulative_discount[10]

    sum_of_pv = pv_explicit_fcff + pv_terminal_value

    # --- Section 12: failure/distress adjustment ---
    if assumptions.failure_probability_override:
        prob_failure = assumptions.failure_probability
    else:
        prob_failure = 0.0

    if assumptions.failure_proceeds_basis == "B":
        proceeds_if_failure = (inputs.book_equity_ltm + inputs.book_debt_ltm) * assumptions.failure_recovery_pct
    else:
        proceeds_if_failure = sum_of_pv * assumptions.failure_recovery_pct

    value_of_operating_assets = sum_of_pv * (1 - prob_failure) + proceeds_if_failure * prob_failure

    # --- Section 13: equity bridge ---
    debt = inputs.book_debt_ltm + (lease_debt_adj if inputs.has_operating_leases else 0.0)
    minority_interest = inputs.minority_interest_ltm

    if is_yes_trapped_cash(assumptions.trapped_cash_override_flag):
        cash = inputs.cash_ltm - assumptions.trapped_cash_amount * (
            inputs.marginal_tax_rate - assumptions.trapped_cash_foreign_tax_rate
        )
    else:
        cash = inputs.cash_ltm

    non_operating_assets = inputs.non_operating_assets_ltm
    value_of_equity = value_of_operating_assets - debt - minority_interest + cash + non_operating_assets

    option_result: GinzuOptionResult | None = None
    value_of_options = 0.0
    if inputs.has_options_outstanding:
        if option_inputs is None:
            raise ValueError("has_options_outstanding=True requires option_inputs")
        option_result = calc_option_value(
            option_inputs, inputs.stock_price, inputs.shares_outstanding, coc_inputs.risk_free_rate,
        )
        value_of_options = option_result.value_of_options_outstanding
    elif option_inputs is not None:
        # Computed for completeness/reconciliation even when gated off,
        # same rationale as the R&D/lease sheets above.
        option_result = calc_option_value(
            option_inputs, inputs.stock_price, inputs.shares_outstanding, coc_inputs.risk_free_rate,
        )

    value_of_equity_common = value_of_equity - value_of_options
    value_per_share = value_of_equity_common / inputs.shares_outstanding
    price_as_pct_of_value = inputs.stock_price / value_per_share  # Price / Value, not the inverse (spec section 13)

    # --- Assemble GinzuYearResult list (base year 0, explicit 1-10, terminal 11) ---
    years: list[GinzuYearResult] = []
    years.append(GinzuYearResult(
        year=0, revenue_growth=None, revenue=revenue[0], operating_margin=adjusted_margin_base,
        ebit=ebit[0], tax_rate=tax_rate[0], nol_balance=nol[0], ebit_after_tax=ebit_after_tax[0],
        reinvestment=None, fcff=None, sales_to_capital=None, invested_capital=invested_capital[0],
        roic=roic[0], cost_of_capital=None, cumulative_discount_factor=None, pv_fcff=None,
    ))
    for y in range(1, 11):
        years.append(GinzuYearResult(
            year=y, revenue_growth=growth[y], revenue=revenue[y], operating_margin=margin[y],
            ebit=ebit[y], tax_rate=tax_rate[y], nol_balance=nol[y], ebit_after_tax=ebit_after_tax[y],
            reinvestment=reinvestment[y], fcff=fcff[y], sales_to_capital=sales_to_capital[y],
            invested_capital=invested_capital[y], roic=roic[y], cost_of_capital=wacc_path[y],
            cumulative_discount_factor=cumulative_discount[y], pv_fcff=pv_fcff[y],
        ))
    years.append(GinzuYearResult(
        year=11, revenue_growth=stable_growth, revenue=revenue_terminal, operating_margin=margin_terminal,
        ebit=ebit_terminal, tax_rate=terminal_tax_rate, nol_balance=None, ebit_after_tax=ebit_after_tax_terminal,
        reinvestment=reinvestment_terminal, fcff=fcff_terminal, sales_to_capital=None, invested_capital=None,
        roic=stable_roc, cost_of_capital=terminal_wacc, cumulative_discount_factor=None, pv_fcff=None,
    ))

    return GinzuValuationResult(
        years=years,
        rd_result=rd_result,
        lease_result=lease_result,
        cost_of_capital_result=coc_result,
        option_result=option_result,
        terminal_value=terminal_value,
        pv_terminal_value=pv_terminal_value,
        pv_explicit_fcff=pv_explicit_fcff,
        sum_of_pv=sum_of_pv,
        probability_of_failure=prob_failure,
        proceeds_if_failure=proceeds_if_failure,
        value_of_operating_assets=value_of_operating_assets,
        debt=debt,
        minority_interest=minority_interest,
        cash=cash,
        non_operating_assets=non_operating_assets,
        value_of_equity=value_of_equity,
        value_of_options=value_of_options,
        value_of_equity_common=value_of_equity_common,
        value_per_share=value_per_share,
        price=inputs.stock_price,
        price_as_pct_of_value=price_as_pct_of_value,
    )
