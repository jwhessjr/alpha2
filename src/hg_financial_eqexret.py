"""
Locked, spreadsheet-faithful port of Damodaran's eqexret.xls ("Estimates
the value of equity in a bank by discounting expected excess returns to
equity investors over time and adding them to book value of equity" --
Damodaran's own spreadsheet-library description).

SECONDARY, flagged cross-check as of 2026-10-05 -- see docs/decisions.md,
"DivGinzu primary / EqExRet cross-check financial-firm architecture." This
is Damodaran's own third-named option for financial-firm valuation
(finfirm09.pdf, "Valuing Financial Service Firms", April 2009) and, unlike
divginzu, has a structural book-equity floor -- equity value can never fall
below current book equity, because that figure is added unconditionally,
not discounted. That property is this model's entire reason for being
retained alongside divginzu rather than replaced by it.

This file is a LOCKED mathematical reference, same convention as
lib/hg_fcff_ginzu.py for the FCFF Ginzu port (2026-10-01) -- it must not be
modified to chase a "nicer" answer for any one ticker. HessGrp-specific data
sourcing, classification, and beta-peer-group selection all live in
lib/hg_financial_adapter.py / lib/hg_financial_classifier.py, never here.

Reverse-engineered by directly reading every non-blank cell in the
workbook's "Inputs" and "Excess Return Valuation" sheets (xlrd -- .xls BIFF
formulas are not recoverable as text in this environment; every equation
below reproduces the workbook's own stored numbers to full floating-point
precision):
  ~/Desktop/HessGrp_RR_Work_2026-09/banks/damodaran_reference/eqexret.xls

Confirmed mechanics (all directly demonstrated, not inferred from labels):

1. Book equity rolls forward ADDITIVELY via retained earnings, not via a
   separately-tracked growth rate:
     NetIncome_t    = ROE_t x BeginningBV_t
     EquityCost_t   = CostOfEquity_t x BeginningBV_t
     ExcessReturn_t = NetIncome_t - EquityCost_t
     Dividend_t     = Payout_t x NetIncome_t
     RetainedEarn_t = NetIncome_t - Dividend_t
     BeginningBV_(t+1) = BeginningBV_t + RetainedEarn_t
   This is the one structural difference from divginzu's independent
   growth-rate fade (see lib/hg_financial_divginzu.py docstring point 1) --
   here growth is always implicitly ROE_t x Retention_t, every year,
   including fade years, because it is never computed as a separate input
   at all.

2. ROE, cost of equity, and payout ratio are independently faded linearly
   across the back portion of the explicit forecast window (Damodaran's
   "gradually adjust your inputs during the second half" toggle) --
   confirmed identical fade mechanism to divginzu's ROE/payout fade, just
   without the separate growth-rate fade divginzu also has.

3. The terminal excess return is computed on the book-equity base AS IT
   STANDS at the end of the explicit forecast window (i.e. after the
   period's own retained earnings have already compounded it), using the
   stable-phase ROE and cost of equity:
     TerminalExcessReturn = (ROE_stable - CostOfEquity_stable) x BeginningBV_terminal
     TerminalValue = TerminalExcessReturn / (CostOfEquity_stable - StableGrowth)
   discounted by the SAME cumulative cost-of-equity factor as the last
   explicit year (not a separate N+1 discount period) -- confirmed exactly
   against the reference workbook's own stored "Terminal Value" and
   "Present Value" cells.

4. Stable payout ratio is a derived identity: 1 - stable_growth/stable_roe
   (Inputs!B32 in the reference workbook, "Stable payout ratio from
   fundamentals is =", confirmed to full precision) -- identical identity
   to divginzu's.

5. Unlike divginzu, the reference workbook has NO risk-free-rate default
   for stable growth -- it is a plain, required, directly-entered input
   (Inputs!B30) with no override toggle. This module therefore requires
   growth_stable explicitly; it is lib/hg_financial_adapter.py's job (not
   this locked engine's) to decide what value to supply, documented and
   flagged there.

6. No Tier 1 / CET1 / RWA / regulatory-capital input, calculation, or
   output exists anywhere in this workbook. Do not add one here -- see
   docs/decisions.md, 2026-10-05.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from hg_financial_fade import linear_fade, payout_retention_invariant_error


class EqExRetInvalidInputs(Exception):
    """Raised for economically invalid stable-phase conditions. Never
    silently clamped -- see docs/decisions.md, 2026-10-05: "Missing/invalid
    data must not silently create an invented number.\""""


@dataclass
class EqExRetInputs:
    current_book_equity: float
    shares_outstanding: float

    high_growth_years: int
    fade_years: int                # 0 = no fade

    roe_explicit: float
    payout_explicit: float

    beta_explicit: float
    risk_free_rate: float
    erp_explicit: float

    roe_stable: float                # UNRESOLVED input -- see module
                                      # docstring; Damodaran's own workbook
                                      # treats this as hand-typed analyst
                                      # judgment, not a formula output.
                                      # Callers must supply it explicitly.
    growth_stable: float             # Required -- no risk-free-rate
                                      # default exists in this workbook
                                      # (module docstring point 5).

    beta_stable: float | None = None   # None -> defaults to beta_explicit
    erp_stable: float | None = None    # None -> defaults to erp_explicit

    ticker: str | None = None          # optional, diagnostic-only -- surfaced
                                         # in EqExRetInvalidInputs messages when
                                         # supplied (2026-10-06 hardening pass);
                                         # the engine itself remains ticker-agnostic


@dataclass
class EqExRetYearResult:
    year: int
    roe: float
    payout: float
    retention: float
    beta: float
    cost_of_equity: float
    beginning_book_equity: float
    net_income: float
    equity_cost: float
    excess_return: float
    dividend: float
    retained_earnings: float
    cumulative_cost_of_equity: float
    pv_excess_return: float


@dataclass
class EqExRetDiagnostics:
    stable_beta_source: str         # "flat_explicit" | "caller_supplied"
    stable_erp_source: str          # "flat_explicit" | "caller_supplied"
    stable_payout_derived: bool     # always True -- 1 - g_stable/roe_stable
    terminal_denominator: float     # cost_of_equity_stable - growth_stable
    warnings: list[str] = field(default_factory=list)


@dataclass
class EqExRetResult:
    inputs: EqExRetInputs
    years: list[EqExRetYearResult]
    retention_stable: float
    payout_stable: float
    cost_of_equity_stable: float
    beginning_book_equity_terminal: float
    terminal_excess_return: float
    terminal_value: float
    pv_terminal_value: float
    pv_explicit_excess_returns: float
    equity_value: float
    value_per_share: float
    terminal_value_pct_of_equity: float
    diagnostics: EqExRetDiagnostics


def value_eqexret(inp: EqExRetInputs) -> EqExRetResult:
    warnings: list[str] = []

    if inp.high_growth_years <= 0:
        raise EqExRetInvalidInputs(f"high_growth_years must be positive, got {inp.high_growth_years}")
    if inp.fade_years > inp.high_growth_years:
        raise EqExRetInvalidInputs(
            f"fade_years ({inp.fade_years}) cannot exceed high_growth_years ({inp.high_growth_years})"
        )
    if inp.roe_stable <= 0:
        raise EqExRetInvalidInputs(f"stable ROE must be positive, got {inp.roe_stable}")
    if not inp.shares_outstanding:
        raise EqExRetInvalidInputs("shares_outstanding must be a positive, nonzero number")
    if inp.growth_stable >= inp.roe_stable:
        raise EqExRetInvalidInputs(
            f"stable growth ({inp.growth_stable:.4f}) must be < stable ROE ({inp.roe_stable:.4f}) "
            "for a defensible (non-negative) stable retention ratio"
        )

    retention_stable = inp.growth_stable / inp.roe_stable
    payout_stable = 1.0 - retention_stable
    if not (0.0 <= payout_stable <= 1.0):
        raise EqExRetInvalidInputs(f"derived stable payout ratio {payout_stable:.4f} outside [0, 1]")

    beta_stable = inp.beta_stable if inp.beta_stable is not None else inp.beta_explicit
    stable_beta_source = "caller_supplied" if inp.beta_stable is not None else "flat_explicit"
    erp_stable = inp.erp_stable if inp.erp_stable is not None else inp.erp_explicit
    stable_erp_source = "caller_supplied" if inp.erp_stable is not None else "flat_explicit"
    cost_of_equity_stable = inp.risk_free_rate + beta_stable * erp_stable

    if inp.growth_stable >= cost_of_equity_stable:
        raise EqExRetInvalidInputs(
            f"stable growth ({inp.growth_stable:.4f}) must be < stable cost of equity "
            f"({cost_of_equity_stable:.4f}) -- terminal denominator would be <= 0"
        )

    # Explicit-period invariant (2026-10-06 hardening pass) -- same
    # rationale and shared helper as lib/hg_financial_divginzu.py's
    # identical guard; see that module for the full explanation of why
    # this wasn't previously self-contained.
    retention_explicit = 1.0 - inp.payout_explicit
    growth_explicit_implied = inp.roe_explicit * retention_explicit
    err = payout_retention_invariant_error(
        engine="EqExRet", context="explicit period (pre-fade)",
        payout=inp.payout_explicit, retention=retention_explicit,
        roe=inp.roe_explicit, growth=growth_explicit_implied, ticker=inp.ticker,
    )
    if err:
        raise EqExRetInvalidInputs(err)

    roe_path = linear_fade(inp.roe_explicit, inp.roe_stable, inp.high_growth_years, inp.fade_years)
    payout_path = linear_fade(inp.payout_explicit, payout_stable, inp.high_growth_years, inp.fade_years)
    beta_path = linear_fade(inp.beta_explicit, beta_stable, inp.high_growth_years, inp.fade_years)
    erp_path = linear_fade(inp.erp_explicit, erp_stable, inp.high_growth_years, inp.fade_years)

    years: list[EqExRetYearResult] = []
    beginning_bv = inp.current_book_equity
    cumulative_coe = 1.0
    pv_explicit = 0.0
    for t in range(inp.high_growth_years):
        roe_t = roe_path[t]
        payout_t = payout_path[t]
        beta_t = beta_path[t]
        erp_t = erp_path[t]
        coe_t = inp.risk_free_rate + beta_t * erp_t

        net_income_t = roe_t * beginning_bv
        equity_cost_t = coe_t * beginning_bv
        excess_return_t = net_income_t - equity_cost_t
        dividend_t = payout_t * net_income_t
        retained_earnings_t = net_income_t - dividend_t

        cumulative_coe *= (1.0 + coe_t)
        pv_excess_return_t = excess_return_t / cumulative_coe
        pv_explicit += pv_excess_return_t

        years.append(EqExRetYearResult(
            year=t + 1, roe=roe_t, payout=payout_t, retention=1.0 - payout_t,
            beta=beta_t, cost_of_equity=coe_t, beginning_book_equity=beginning_bv,
            net_income=net_income_t, equity_cost=equity_cost_t,
            excess_return=excess_return_t, dividend=dividend_t,
            retained_earnings=retained_earnings_t,
            cumulative_cost_of_equity=cumulative_coe, pv_excess_return=pv_excess_return_t,
        ))
        beginning_bv = beginning_bv + retained_earnings_t

    beginning_bv_terminal = beginning_bv
    terminal_net_income = inp.roe_stable * beginning_bv_terminal
    terminal_equity_cost = cost_of_equity_stable * beginning_bv_terminal
    terminal_excess_return = terminal_net_income - terminal_equity_cost
    terminal_denominator = cost_of_equity_stable - inp.growth_stable
    terminal_value = terminal_excess_return / terminal_denominator
    pv_terminal_value = terminal_value / cumulative_coe

    equity_value = inp.current_book_equity + pv_explicit + pv_terminal_value
    value_per_share = equity_value / inp.shares_outstanding
    terminal_pct = pv_terminal_value / equity_value if equity_value else 0.0

    if terminal_pct > 0.95:
        warnings.append(f"terminal_value_pct_of_equity={terminal_pct:.1%} exceeds 95% -- review stable assumptions")

    diagnostics = EqExRetDiagnostics(
        stable_beta_source=stable_beta_source,
        stable_erp_source=stable_erp_source,
        stable_payout_derived=True,
        terminal_denominator=terminal_denominator,
        warnings=warnings,
    )

    return EqExRetResult(
        inputs=inp, years=years, retention_stable=retention_stable,
        payout_stable=payout_stable, cost_of_equity_stable=cost_of_equity_stable,
        beginning_book_equity_terminal=beginning_bv_terminal,
        terminal_excess_return=terminal_excess_return, terminal_value=terminal_value,
        pv_terminal_value=pv_terminal_value, pv_explicit_excess_returns=pv_explicit,
        equity_value=equity_value, value_per_share=value_per_share,
        terminal_value_pct_of_equity=terminal_pct, diagnostics=diagnostics,
    )
