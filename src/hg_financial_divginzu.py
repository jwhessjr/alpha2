"""
Locked, spreadsheet-faithful port of Damodaran's divginzu.xls ("A complete
dividend discount model that can do stable growth, 2-stage or 3-stage
valuation" -- Damodaran's own spreadsheet-library description; "your best
choice if you are analyzing financial service firms" per the same page).

PRIMARY financial-firm valuation model as of 2026-10-05 -- see
docs/decisions.md, "DivGinzu primary / EqExRet cross-check financial-firm
architecture."

This file is a LOCKED mathematical reference, same convention as
lib/hg_fcff_ginzu.py for the FCFF Ginzu port (2026-10-01) -- it must not be
modified to chase a "nicer" answer for any one ticker. HessGrp-specific data
sourcing, classification, and beta-peer-group selection all live in
lib/hg_financial_adapter.py / lib/hg_financial_classifier.py, never here.

Reverse-engineered by directly reading every non-blank cell in the
workbook's "Inputs" and "Valuation Output" sheets (xlrd -- .xls BIFF
formulas are not recoverable as text in this environment; values were
reproduced to full floating-point precision and cross-checked against the
pre-loaded JPM "Dimon in the Rough" example, Inputs!B3/B5):
  ~/Desktop/HessGrp_RR_Work_2026-09/banks/damodaran_reference/divginzu.xls

Confirmed mechanics (all directly demonstrated, not inferred from labels):

1. Growth during the fade years is NOT recomputed as ROE_t x Retention_t.
   It follows its OWN independent linear fade from the flat explicit
   growth rate (ROE_explicit x Retention_explicit) to the stable growth
   rate. Net income/EPS literally compounds off this independent growth
   path. See tests/test_hg_financial_divginzu.py::
   test_growth_does_not_equal_roe_times_retention_during_fade -- this is
   the single most non-obvious fact this module encodes and the one most
   likely to be "simplified away" by a future maintainer who hasn't read
   this docstring.

2. Dividend_t = NetIncome_t x Payout_t, same year, for every year
   (confirmed exactly at the fade boundary in the reference workbook,
   where NetIncome_t and Payout_t are both independently faded).

3. Stable payout ratio is a derived identity: 1 - stable_growth/stable_roe
   (Inputs!B40 in the reference workbook, "Stable payout ratio from
   fundamentals is =", confirmed to full precision).

4. Stable growth defaults to the risk-free rate (the workbook's own
   documented default -- "The model uses the riskfree rate as the growth
   rate in perpetuity" -- Inputs!B36, used because Inputs!B37
   "Do you want to override the default" = "No" in the reference example)
   unless the caller explicitly supplies a different value.

5. The reference workbook's "Length of high growth period" input (=10)
   produces a FIRST displayed year that is simply the current actual net
   income, unchanged -- real compounding begins at year 2 and runs for 9
   years before the terminal step. This module collapses that one-year
   "pinned to current" display convention: `high_growth_years` here means
   the number of years that actually grow (9 in the reference example,
   confirmed to reproduce every stored cell value exactly), not
   Damodaran's raw "length of high growth period" input. This is a
   cleaner encoding of the identical arithmetic, not a methodology change
   -- see docs/decisions.md for the full derivation and the exact
   reproduction numbers.

6. No Tier 1 / CET1 / RWA / regulatory-capital input, calculation, or
   output exists anywhere in this workbook. Do not add one here -- see
   docs/decisions.md, 2026-10-05.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hg_financial_fade import linear_fade, payout_retention_invariant_error


class DivGinzuInvalidInputs(Exception):
    """Raised for economically invalid stable-phase conditions. Never
    silently clamped -- see docstring instruction in
    lib/hg_financial_adapter.py and task governance in docs/decisions.md:
    "Missing/invalid data must not silently create an invented number.\""""


@dataclass
class DivGinzuInputs:
    current_net_income: float      # TTM, aggregate (not per-share) -- X_0
    shares_outstanding: float

    high_growth_years: int         # number of years that actually grow --
                                    # see module docstring point 5
    fade_years: int                # years at the back of the explicit
                                    # period that fade to stable values;
                                    # 0 = no fade (Damodaran's 2-stage
                                    # toggle answered "No")

    roe_explicit: float
    payout_explicit: float

    beta_explicit: float
    risk_free_rate: float
    erp_explicit: float

    roe_stable: float               # UNRESOLVED input -- Damodaran's own
                                     # workbook treats this as hand-typed
                                     # analyst judgment (Inputs!B32 in the
                                     # reference workbook), not a formula
                                     # output. There is no automated
                                     # source for this number. Callers
                                     # must supply it explicitly; this
                                     # module never defaults or invents it.

    growth_stable: float | None = None   # None -> defaults to risk_free_rate
                                           # (the workbook's own documented
                                           # default, module docstring point 4)
    beta_stable: float | None = None     # None -> defaults to beta_explicit
    erp_stable: float | None = None      # None -> defaults to erp_explicit

    ticker: str | None = None            # optional, diagnostic-only -- surfaced
                                           # in DivGinzuInvalidInputs messages when
                                           # supplied (2026-10-06 hardening pass);
                                           # the engine itself remains ticker-agnostic


@dataclass
class DivGinzuYearResult:
    year: int
    roe: float
    payout: float
    retention: float
    growth: float
    beta: float
    cost_of_equity: float
    net_income: float
    dividend: float
    cumulative_cost_of_equity: float
    pv_dividend: float


@dataclass
class DivGinzuDiagnostics:
    stable_growth_source: str       # "risk_free_rate_default" | "caller_supplied"
    stable_beta_source: str         # "flat_explicit" | "caller_supplied"
    stable_erp_source: str          # "flat_explicit" | "caller_supplied"
    stable_payout_derived: bool     # always True -- 1 - g_stable/roe_stable
    terminal_denominator: float     # cost_of_equity_stable - growth_stable
    warnings: list[str] = field(default_factory=list)


@dataclass
class DivGinzuResult:
    inputs: DivGinzuInputs
    years: list[DivGinzuYearResult]
    retention_stable: float
    payout_stable: float
    cost_of_equity_stable: float
    terminal_dividend: float
    terminal_value: float
    pv_terminal_value: float
    pv_explicit_dividends: float
    equity_value: float
    value_per_share: float
    terminal_value_pct_of_equity: float
    diagnostics: DivGinzuDiagnostics


def value_divginzu(inp: DivGinzuInputs) -> DivGinzuResult:
    warnings: list[str] = []

    if inp.high_growth_years <= 0:
        raise DivGinzuInvalidInputs(f"high_growth_years must be positive, got {inp.high_growth_years}")
    if inp.fade_years > inp.high_growth_years:
        raise DivGinzuInvalidInputs(
            f"fade_years ({inp.fade_years}) cannot exceed high_growth_years ({inp.high_growth_years})"
        )
    if inp.roe_stable <= 0:
        raise DivGinzuInvalidInputs(f"stable ROE must be positive, got {inp.roe_stable}")
    if not inp.shares_outstanding:
        raise DivGinzuInvalidInputs("shares_outstanding must be a positive, nonzero number")

    growth_stable = inp.growth_stable if inp.growth_stable is not None else inp.risk_free_rate
    stable_growth_source = "caller_supplied" if inp.growth_stable is not None else "risk_free_rate_default"

    if growth_stable >= inp.roe_stable:
        raise DivGinzuInvalidInputs(
            f"stable growth ({growth_stable:.4f}) must be < stable ROE ({inp.roe_stable:.4f}) "
            "for a defensible (non-negative) stable retention ratio"
        )

    retention_stable = growth_stable / inp.roe_stable
    payout_stable = 1.0 - retention_stable
    if not (0.0 <= payout_stable <= 1.0):
        raise DivGinzuInvalidInputs(f"derived stable payout ratio {payout_stable:.4f} outside [0, 1]")

    beta_stable = inp.beta_stable if inp.beta_stable is not None else inp.beta_explicit
    stable_beta_source = "caller_supplied" if inp.beta_stable is not None else "flat_explicit"
    erp_stable = inp.erp_stable if inp.erp_stable is not None else inp.erp_explicit
    stable_erp_source = "caller_supplied" if inp.erp_stable is not None else "flat_explicit"
    cost_of_equity_stable = inp.risk_free_rate + beta_stable * erp_stable

    if growth_stable >= cost_of_equity_stable:
        raise DivGinzuInvalidInputs(
            f"stable growth ({growth_stable:.4f}) must be < stable cost of equity "
            f"({cost_of_equity_stable:.4f}) -- terminal denominator would be <= 0"
        )

    retention_explicit = 1.0 - inp.payout_explicit
    growth_explicit = inp.roe_explicit * retention_explicit

    # Explicit-period invariant (2026-10-06 hardening pass): the stable
    # phase already has this guard (payout_stable above); the explicit
    # phase previously relied entirely on the adapter's own upstream
    # payout source (_bank_payout_ratio()) staying in-bounds -- a real,
    # correct dependency today, but not self-contained. Hard-fails, never
    # clamps -- see lib/hg_financial_fade.py's docstring.
    err = payout_retention_invariant_error(
        engine="DivGinzu", context="explicit period (pre-fade)",
        payout=inp.payout_explicit, retention=retention_explicit,
        roe=inp.roe_explicit, growth=growth_explicit, ticker=inp.ticker,
    )
    if err:
        raise DivGinzuInvalidInputs(err)

    roe_path = linear_fade(inp.roe_explicit, inp.roe_stable, inp.high_growth_years, inp.fade_years)
    payout_path = linear_fade(inp.payout_explicit, payout_stable, inp.high_growth_years, inp.fade_years)
    growth_path = linear_fade(growth_explicit, growth_stable, inp.high_growth_years, inp.fade_years)
    beta_path = linear_fade(inp.beta_explicit, beta_stable, inp.high_growth_years, inp.fade_years)
    erp_path = linear_fade(inp.erp_explicit, erp_stable, inp.high_growth_years, inp.fade_years)

    years: list[DivGinzuYearResult] = []
    net_income_prev = inp.current_net_income
    cumulative_coe = 1.0
    pv_explicit = 0.0

    # Year 1 is a special, non-growing leading period: Damodaran's own
    # workbook displays the current actual net income/dividend unchanged
    # as "year 1" (confirmed: Inputs!B9 x Inputs!B22(payout) reproduces
    # Inputs!B10's current aggregate dividend exactly), but still
    # discounts it by one full period of cost of equity -- it is one year
    # away, not already in hand. Real compounding (the growth_path/
    # roe_path/payout_path built above) begins at year 2. Confirmed
    # against every stored cell in the reference workbook's "Valuation
    # Output" sheet, not inferred -- see module docstring point 5 and
    # tests/test_hg_financial_divginzu.py::test_dimon_in_the_rough_exact_reproduction.
    coe_1 = inp.risk_free_rate + inp.beta_explicit * inp.erp_explicit
    dividend_1 = inp.current_net_income * inp.payout_explicit
    cumulative_coe *= (1.0 + coe_1)
    pv_dividend_1 = dividend_1 / cumulative_coe
    pv_explicit += pv_dividend_1
    years.append(DivGinzuYearResult(
        year=1, roe=inp.roe_explicit, payout=inp.payout_explicit,
        retention=retention_explicit, growth=0.0, beta=inp.beta_explicit,
        cost_of_equity=coe_1, net_income=inp.current_net_income,
        dividend=dividend_1, cumulative_cost_of_equity=cumulative_coe,
        pv_dividend=pv_dividend_1,
    ))

    for t in range(inp.high_growth_years):
        roe_t = roe_path[t]
        payout_t = payout_path[t]
        growth_t = growth_path[t]
        beta_t = beta_path[t]
        erp_t = erp_path[t]
        coe_t = inp.risk_free_rate + beta_t * erp_t

        net_income_t = net_income_prev * (1.0 + growth_t)
        dividend_t = net_income_t * payout_t
        cumulative_coe *= (1.0 + coe_t)
        pv_dividend_t = dividend_t / cumulative_coe
        pv_explicit += pv_dividend_t

        years.append(DivGinzuYearResult(
            year=t + 2, roe=roe_t, payout=payout_t, retention=1.0 - payout_t,
            growth=growth_t, beta=beta_t, cost_of_equity=coe_t,
            net_income=net_income_t, dividend=dividend_t,
            cumulative_cost_of_equity=cumulative_coe, pv_dividend=pv_dividend_t,
        ))
        net_income_prev = net_income_t

    terminal_dividend = net_income_prev * (1.0 + growth_stable) * payout_stable
    terminal_denominator = cost_of_equity_stable - growth_stable
    terminal_value = terminal_dividend / terminal_denominator
    pv_terminal_value = terminal_value / cumulative_coe

    equity_value = pv_explicit + pv_terminal_value
    value_per_share = equity_value / inp.shares_outstanding
    terminal_pct = pv_terminal_value / equity_value if equity_value else 0.0

    if terminal_pct > 0.95:
        warnings.append(f"terminal_value_pct_of_equity={terminal_pct:.1%} exceeds 95% -- review stable assumptions")
    if equity_value <= 0:
        warnings.append(f"equity_value={equity_value:,.2f} is non-positive -- divginzu has no book-equity floor")

    diagnostics = DivGinzuDiagnostics(
        stable_growth_source=stable_growth_source,
        stable_beta_source=stable_beta_source,
        stable_erp_source=stable_erp_source,
        stable_payout_derived=True,
        terminal_denominator=terminal_denominator,
        warnings=warnings,
    )

    return DivGinzuResult(
        inputs=inp, years=years, retention_stable=retention_stable,
        payout_stable=payout_stable, cost_of_equity_stable=cost_of_equity_stable,
        terminal_dividend=terminal_dividend, terminal_value=terminal_value,
        pv_terminal_value=pv_terminal_value, pv_explicit_dividends=pv_explicit,
        equity_value=equity_value, value_per_share=value_per_share,
        terminal_value_pct_of_equity=terminal_pct, diagnostics=diagnostics,
    )
