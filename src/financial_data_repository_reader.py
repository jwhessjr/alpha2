"""
financial_data_repository_reader.py — read-only adapter over the Intrinio
40-quarter financial-statement repository (/Volumes/Financial_Data/financial_data.db).

Retires moat_score.py's own live Alpha Vantage / Intrinio annual-statement
fetch (hg_dcflib.get_*_intrinio_annual() + AV annualReports fallback) as
the source for its 10-year AnnualFinancials series, and retires
valuation.db's `financial_data` TABLE as the downstream snapshot cache
every other consumer (composite_score.py, av_fcff_2.py's auto-rescore,
lib/replacer.py, build_new_value20.py) previously read. See
~/Desktop/HessGrp_RR_Work_2026-10/legacy_financial_table_retirement/
00_completion_record.md for the full migration record, field-mapping
table, and population comparison this module's deployment was based on.

Design, stated once here rather than repeated at every call site:
  - Every DB connection opened here is read-only (SQLite URI `mode=ro`) —
    this module cannot write to financial_data.db even by accident.
  - A fiscal year's FLOW figure (revenue, gross profit, EBIT/operating
    income, net income, tax expense, interest expense, operating CF,
    capex) is the SUM of that year's four quarters -- included ONLY if
    all four quarters (Q1-Q4) are present, current (is_current=1),
    non-quarantined, and not validation_status='rejected'. A year missing
    even one quarter is NEVER partially summed or interpolated -- it is
    simply absent from the returned series, exactly matching "do not
    invent annual periods from incomplete quarters."
  - A fiscal year's POINT-IN-TIME figure (equity, debt, cash, total
    assets) is that year's `FY`-period balance-sheet fundamental --
    confirmed live (AAPL, company_id=1) that balance_sheet_statement
    fiscal-year-end snapshots are tagged fiscal_period='FY', NOT 'Q4', in
    this repository -- unlike income_statement/cash_flow_statement, where
    Q4 is a genuine distinct flow period requiring its own value (or the
    Stage 1 Q4-reconstruction mechanism). A balance sheet has no separate
    "Q4" snapshot distinct from the FY-end snapshot; querying for 'Q4'
    here returns nothing, which is what the first draft of this module
    did incorrectly before being caught and fixed. Never averaged or
    summed across quarters either way.
  - interest_expense is NOT required for a fiscal year to count as
    complete -- confirmed live that some companies (e.g. AAPL) report no
    distinct interest-expense tag at all (nets interest income/expense,
    or simply doesn't break it out). A year missing interest_expense
    still counts as complete; interest_expense for that year is None,
    and interest_coverage downstream correctly becomes None too (already
    handled gracefully by every caller — see composite_score.py's
    existing `if ic is not None` guard).
  - Shares outstanding is the AVERAGE of the four quarters'
    weightedavebasicsharesos (income-statement, flow-period-average) --
    financial_data.db has no point-in-time share-count tag at all (see
    the field-mapping table). This is a DOCUMENTED DEFINITIONAL CHANGE
    from the legacy table's balance-sheet commonStockSharesOutstanding,
    not a missing-data substitution -- flagged explicitly, not hidden.
  - Pretax income (income_before_tax) is DERIVED as net_income +
    tax_expense (the standard GAAP identity) because financial_data.db
    has no direct incomebeforetax tag -- a documented derivation from two
    directly-available tags, not a guess.
  - cash is cashandequivalents PLUS shortterminvestments (0 if a company
    reports no distinct short-term-investments line that year), restoring
    moat_score.py's original "cash + STI" field definition (its own
    _cash_and_sti(), now deleted) rather than narrowing it to cash alone.
    financial_data.db has NO pre-aggregated "cash and short-term
    investments" tag at all -- confirmed live (grepped every intrinio_tag
    containing "cash") -- so the old function's core job (preferring a
    granular sum over Alpha Vantage's own pre-summed field, which had a
    confirmed bug silently dropping the STI component -- docs/
    known_errors.md 2026-07-31) is structurally moot here: there is only
    one path, the granular sum, because there is nothing else to prefer
    it over. The old function's separate financial-firm/REIT carve-out
    (trust AV's pre-tagged value instead, docs/known_errors.md 2026-08-02)
    is likewise moot for the same reason -- it existed only to choose
    between two AV-specific fields, neither of which this repository has.
    short_term_investments is OPTIONAL (like interest_expense) -- it never
    gates a fiscal year's completeness, since most non-financial companies
    genuinely hold none.
  - Every ticker resolution goes through ticker_history.is_current=1 and
    checks companies.identity_quarantined -- the same identity-resolution
    discipline already used in lib/assessment_engine.py's ownership-
    controls work (2026-11-16/17).
  - No fallback to Alpha Vantage, no fallback to a live Intrinio API call,
    no fallback to the legacy valuation.db.financial_data table. A ticker
    this module cannot resolve, or that has fewer complete fiscal years
    than the caller's own minimum, returns None/empty -- the caller's
    EXISTING missing-data behavior (moat_score.py's `ValueError` for
    insufficient data) is what surfaces this, not a new behavior invented
    here.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

FINANCIAL_DATA_REPOSITORY_DB_PATH = "/Volumes/Financial_Data/financial_data.db"

# ── Field-mapping table (also reproduced in the completion record) ─────────
# AnnualFinancials field  | statement_type           | intrinio_tag                          | basis
# revenue                 | income_statement         | totalrevenue                          | sum of 4 quarters
# gross_profit             | income_statement         | totalgrossprofit                       | sum of 4 quarters
# ebit                     | income_statement         | totaloperatingincome                   | sum of 4 quarters (EBIT proxy per CLAUDE.md)
# net_income               | income_statement         | netincome                               | sum of 4 quarters
# tax_expense               | income_statement         | incometaxexpense                        | sum of 4 quarters
# income_before_tax          | income_statement         | DERIVED = net_income + tax_expense       | derived, not a raw tag
# interest_expense           | income_statement         | totalinterestexpense                    | sum of 4 quarters
# equity                     | balance_sheet_statement  | totalcommonequity                       | Q4 (FYE) snapshot
# long_term_debt              | balance_sheet_statement  | longtermdebt                            | Q4 (FYE) snapshot
# short_term_debt              | balance_sheet_statement  | shorttermdebt                           | Q4 (FYE) snapshot
# cash                         | balance_sheet_statement  | cashandequivalents + shortterminvestments | Q4 (FYE) snapshot; STI optional, 0 if absent that year (see module docstring)
# total_assets                 | balance_sheet_statement  | totalassets                             | Q4 (FYE) snapshot
# shares                       | income_statement         | weightedavebasicsharesos                | AVERAGE of 4 quarters (DEFINITIONAL CHANGE -- see module docstring)
# operating_cf                 | cash_flow_statement      | netcashfromoperatingactivities          | sum of 4 quarters
# capex                        | cash_flow_statement      | purchaseofplantpropertyandequipment     | sum of 4 quarters (already negative-signed, same convention as AV)
# eps                          | NOT fetched -- computed by the caller exactly as before (net_income/shares, scale-invariant fallback)

#: Required for a fiscal year to count as "complete" -- every field here
#: must have all 4 quarters present.
_FLOW_TAGS_REQUIRED = {
    "income_statement": {
        "revenue": "totalrevenue",
        "gross_profit": "totalgrossprofit",
        "ebit": "totaloperatingincome",
        "net_income": "netincome",
        "tax_expense": "incometaxexpense",
    },
    "cash_flow_statement": {
        "operating_cf": "netcashfromoperatingactivities",
        "capex": "purchaseofplantpropertyandequipment",
    },
}
#: NOT required for completeness -- confirmed some companies (AAPL) have
#: zero coverage. Populated with None for a year if any quarter is missing,
#: never excludes the year.
_FLOW_TAGS_OPTIONAL = {
    "income_statement": {
        "interest_expense": "totalinterestexpense",
    },
}
#: Point-in-time balance-sheet fields use the FY-period fundamental
#: directly (confirmed live -- see module docstring), not 'Q4'.
_POINT_IN_TIME_TAGS = {
    "balance_sheet_statement": {
        "equity": "totalcommonequity",
        "long_term_debt": "longtermdebt",
        "short_term_debt": "shorttermdebt",
        "cash": "cashandequivalents",
        "total_assets": "totalassets",
    },
}
#: Optional point-in-time field -- never gates completeness. Summed into
#: `cash` (0 if absent that year) to restore moat_score.py's original
#: "cash + short-term investments" definition -- see module docstring.
_POINT_IN_TIME_TAGS_OPTIONAL = {
    "balance_sheet_statement": {
        "short_term_investments": "shortterminvestments",
    },
}
_SHARES_TAG = ("income_statement", "weightedavebasicsharesos")


@dataclass
class AnnualFinancialsResult:
    """Mirrors moat_score.AnnualFinancials's field shape exactly (most-
    recent-year first) so the two are drop-in compatible at the call site.
    `years` reflects only COMPLETE fiscal years (all tags present for all
    4 quarters) -- never a partially-populated year."""
    ticker: str
    years: int
    revenue: list[float]
    gross_profit: list[float]
    ebit: list[float]
    net_income: list[float]
    eps: list[float]
    tax_expense: list[float]
    income_before_tax: list[float]
    equity: list[float]
    long_term_debt: list[float]
    short_term_debt: list[float]
    cash: list[float]
    total_assets: list[float]
    interest_expense: list[float]
    shares: list[float]
    operating_cf: list[float]
    capex: list[float]
    fiscal_years: list[int]  # new, diagnostic -- not on the legacy dataclass; purely additive
    source: str = "financial_data_repository"  # provenance marker, purely additive


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_company_id(conn: sqlite3.Connection, ticker: str) -> Optional[int]:
    """The only sanctioned ticker->company_id path -- via ticker_history
    (is_current=1), checking companies.identity_quarantined. Returns None
    (never a guessed/ambiguous match) if unresolved or identity-quarantined."""
    row = conn.execute(
        "SELECT c.company_id, c.identity_quarantined FROM ticker_history th "
        "JOIN companies c ON c.company_id = th.company_id "
        "WHERE th.ticker = ? AND th.is_current = 1",
        (ticker,),
    ).fetchone()
    if row is None or row["identity_quarantined"]:
        return None
    return row["company_id"]


def _quarterly_values(
    conn: sqlite3.Connection, company_id: int, statement_type: str, intrinio_tag: str,
    periods: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4"),
) -> dict[int, dict[str, float]]:
    """Returns {fiscal_year: {fiscal_period: value}} for one tag, current/
    non-quarantined/non-rejected rows only, restricted to `periods`
    (Q1-Q4 for flow statements; ('FY',) for balance-sheet point-in-time
    lookups -- see module docstring for why balance sheets use FY, not
    Q4). A period present here is confirmed real evidence -- a period
    ABSENT here (because it's missing, quarantined, or rejected) is
    never backfilled or guessed."""
    placeholders = ",".join("?" for _ in periods)
    rows = conn.execute(
        f"""SELECT f.fiscal_year, f.fiscal_period, v.value
           FROM fundamentals f
           JOIN financial_values v ON v.fundamental_id = f.fundamental_id
           JOIN financial_tags t ON t.tag_id = v.tag_id
           WHERE f.company_id = ? AND f.statement_type = ? AND t.intrinio_tag = ?
             AND f.is_current = 1 AND f.quarantined = 0
             AND f.fiscal_period IN ({placeholders})
             AND v.validation_status != 'rejected'
             AND v.value IS NOT NULL""",
        (company_id, statement_type, intrinio_tag, *periods),
    ).fetchall()
    out: dict[int, dict[str, float]] = {}
    for r in rows:
        out.setdefault(r["fiscal_year"], {})[r["fiscal_period"]] = r["value"]
    return out


def fetch_annual_financials_from_repository(
    ticker: str, years: int = 10, db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH
) -> Optional[AnnualFinancialsResult]:
    """The production entry point. Returns None if the ticker cannot be
    resolved at all, or has zero complete fiscal years -- the caller
    (moat_score.py) is responsible for turning that into its own existing
    missing-data behavior (a ValueError, matching what the live-fetch path
    already raised for insufficient data). Never raises here itself, and
    never falls back to any other data source."""
    conn = _connect(db_path)
    try:
        company_id = resolve_company_id(conn, ticker)
        if company_id is None:
            return None

        flow_by_field: dict[str, dict[int, dict[str, float]]] = {}
        for stmt, fields in _FLOW_TAGS_REQUIRED.items():
            for field, tag in fields.items():
                flow_by_field[field] = _quarterly_values(conn, company_id, stmt, tag)
        optional_by_field: dict[str, dict[int, dict[str, float]]] = {}
        for stmt, fields in _FLOW_TAGS_OPTIONAL.items():
            for field, tag in fields.items():
                optional_by_field[field] = _quarterly_values(conn, company_id, stmt, tag)

        pit_by_field: dict[str, dict[int, dict[str, float]]] = {}
        for stmt, fields in _POINT_IN_TIME_TAGS.items():
            for field, tag in fields.items():
                pit_by_field[field] = _quarterly_values(conn, company_id, stmt, tag, periods=("FY",))

        pit_optional_by_field: dict[str, dict[int, dict[str, float]]] = {}
        for stmt, fields in _POINT_IN_TIME_TAGS_OPTIONAL.items():
            for field, tag in fields.items():
                pit_optional_by_field[field] = _quarterly_values(conn, company_id, stmt, tag, periods=("FY",))

        shares_stmt, shares_tag = _SHARES_TAG
        shares_by_year = _quarterly_values(conn, company_id, shares_stmt, shares_tag)

        # A fiscal year is COMPLETE if every REQUIRED flow field has all 4
        # quarters, every point-in-time field has an FY row, and shares has
        # all 4 quarters (needed for the average). Optional fields
        # (interest_expense) never gate completeness. No partial years.
        candidate_years = set(flow_by_field["revenue"].keys())
        for field_map in flow_by_field.values():
            candidate_years &= {y for y, qs in field_map.items() if len(qs) == 4}
        for field_map in pit_by_field.values():
            candidate_years &= {y for y, qs in field_map.items() if "FY" in qs}
        candidate_years &= {y for y, qs in shares_by_year.items() if len(qs) == 4}

        complete_years = sorted(candidate_years, reverse=True)[:years]
        if not complete_years:
            return None

        def flow_sum(field, y):
            return sum(flow_by_field[field][y].values())

        def optional_flow_sum(field, y):
            qs = optional_by_field[field].get(y)
            if qs is None or len(qs) != 4:
                return None  # partial/absent optional coverage -> explicitly None, never 0
            return sum(qs.values())

        def pit_fy(field, y):
            return pit_by_field[field][y]["FY"]

        def pit_fy_optional(field, y):
            qs = pit_optional_by_field[field].get(y)
            return qs["FY"] if qs and "FY" in qs else None

        def shares_avg(y):
            qs = shares_by_year[y]
            return sum(qs.values()) / len(qs)

        revenue = [flow_sum("revenue", y) for y in complete_years]
        gross_profit = [flow_sum("gross_profit", y) for y in complete_years]
        ebit = [flow_sum("ebit", y) for y in complete_years]
        net_income = [flow_sum("net_income", y) for y in complete_years]
        tax_expense = [flow_sum("tax_expense", y) for y in complete_years]
        income_before_tax = [net_income[i] + tax_expense[i] for i in range(len(complete_years))]
        interest_expense = [optional_flow_sum("interest_expense", y) for y in complete_years]
        equity = [pit_fy("equity", y) for y in complete_years]
        long_term_debt = [pit_fy("long_term_debt", y) for y in complete_years]
        short_term_debt = [pit_fy("short_term_debt", y) for y in complete_years]
        cash = [pit_fy("cash", y) + (pit_fy_optional("short_term_investments", y) or 0.0)
                for y in complete_years]
        total_assets = [pit_fy("total_assets", y) for y in complete_years]
        shares = [shares_avg(y) for y in complete_years]
        operating_cf = [flow_sum("operating_cf", y) for y in complete_years]
        capex = [flow_sum("capex", y) for y in complete_years]

        eps = []
        for i in range(len(complete_years)):
            sh = shares[i]
            if sh and sh > 0:
                eps.append(net_income[i] / sh)
            elif net_income[i] != 0:
                eps.append(net_income[i])
            else:
                eps.append(0.0)

        return AnnualFinancialsResult(
            ticker=ticker, years=len(complete_years),
            revenue=revenue, gross_profit=gross_profit, ebit=ebit, net_income=net_income,
            eps=eps, tax_expense=tax_expense, income_before_tax=income_before_tax,
            equity=equity, long_term_debt=long_term_debt, short_term_debt=short_term_debt,
            cash=cash, total_assets=total_assets, interest_expense=interest_expense,
            shares=shares, operating_cf=operating_cf, capex=capex,
            fiscal_years=complete_years,
        )
    finally:
        conn.close()


# ── Shared snapshot-field computation, extracted from moat_score.py's own
# upsert_financial_data() so BOTH the (now-retired) legacy-table writer and
# every in-memory reader use IDENTICAL derivation logic -- never
# duplicated. Pure function: takes an AnnualFinancials-SHAPED object
# (works for both the legacy dataclass and AnnualFinancialsResult above),
# returns the same 14-field dict every reader (composite_score.py,
# av_fcff_2.py's auto-rescore, build_new_value20.py) needs. Does NOT
# write anywhere. ──────────────────────────────────────────────────────

def compute_snapshot_fields(fin) -> dict:
    n = fin.years

    def _r(lst, i=0):
        return lst[i] if i < len(lst) else None

    revenue = _r(fin.revenue)
    ebit = _r(fin.ebit)
    net_income = _r(fin.net_income)
    interest_exp = _r(fin.interest_expense)
    total_assets = _r(fin.total_assets)
    lt_debt = _r(fin.long_term_debt)
    st_debt = _r(fin.short_term_debt)
    total_debt = (lt_debt or 0.0) + (st_debt or 0.0)
    cash = _r(fin.cash)
    equity = _r(fin.equity)
    shares = _r(fin.shares)
    op_cf = _r(fin.operating_cf)
    capex = _r(fin.capex)
    fcf = (op_cf or 0.0) + (capex or 0.0)

    roa = (net_income / total_assets) if total_assets and total_assets > 0 else None
    ni_prior = _r(fin.net_income, 1)
    ta_prior = _r(fin.total_assets, 1)
    roa_prior = (ni_prior / ta_prior) if (ni_prior is not None and ta_prior and ta_prior > 0) else None

    ie_abs = abs(interest_exp) if interest_exp else None
    interest_coverage = (ebit / ie_abs) if (ebit and ie_abs and ie_abs > 0) else None

    debt_to_equity = (total_debt / equity) if (equity and equity > 0) else None

    at_series = []
    for i in range(n):
        ta = fin.total_assets[i]
        rv = fin.revenue[i]
        at_series.append((rv / ta) if ta and ta > 0 else None)
    asset_turnover = at_series[0] if at_series else None
    valid_at = [(i, v) for i, v in enumerate(at_series) if v is not None]
    if len(valid_at) >= 2:
        xs = [p[0] for p in valid_at]
        ys = [p[1] for p in valid_at]
        xm = sum(xs) / len(xs)
        ym = sum(ys) / len(ys)
        num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
        den = sum((x - xm) ** 2 for x in xs)
        asset_turnover_trend = -(num / den) if den != 0 else 0.0
    else:
        asset_turnover_trend = None

    pos5 = sum(1 for i in range(min(5, n)) if fin.net_income[i] > 0)
    accruals_positive = int(op_cf > net_income) if (op_cf is not None and net_income is not None) else None

    return {
        "ticker": fin.ticker,
        "revenue": revenue, "ebit": ebit, "net_income": net_income,
        "interest_expense": interest_exp, "total_assets": total_assets,
        "total_debt": total_debt, "cash": cash, "equity": equity,
        "shares_outstanding": shares, "operating_cf": op_cf, "capex": capex, "fcf": fcf,
        "roa": roa, "roa_prior": roa_prior, "interest_coverage": interest_coverage,
        "debt_to_equity": debt_to_equity, "asset_turnover": asset_turnover,
        "asset_turnover_trend": asset_turnover_trend,
        "positive_earnings_5yr": pos5, "accruals_positive": accruals_positive,
        "data_years": n,
    }


def fetch_snapshot_fields(ticker: str, db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH) -> Optional[dict]:
    """Convenience: resolve -> fetch -> compute in one call, for readers
    that only need the flat 14-field snapshot (composite_score.py,
    av_fcff_2.py's auto-rescore, replacer.py's OCF gate), not the full
    multi-year series moat_score.py's own ROIC/margin/FCF/EPS scoring
    needs. Returns None if the ticker can't be resolved or has zero
    complete fiscal years -- callers must treat None as "missing," never
    as zero."""
    fin = fetch_annual_financials_from_repository(ticker, years=5, db_path=db_path)
    if fin is None:
        return None
    return compute_snapshot_fields(fin)


def fetch_snapshot_fields_bulk(tickers: list[str], db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH) -> dict[str, dict]:
    """Bulk convenience for composite_score.py/av_fcff_2.py's auto-rescore,
    which previously did one `SELECT * FROM financial_data` and indexed
    the result by ticker. Opens ONE read-only connection for the whole
    batch rather than one per ticker. Tickers that can't be resolved or
    have no complete fiscal years are simply absent from the result dict
    -- callers already use `.get(ticker, {})`, so a missing entry degrades
    to the exact same "no data" behavior as before."""
    out: dict[str, dict] = {}
    for ticker in tickers:
        snap = fetch_snapshot_fields(ticker, db_path=db_path)
        if snap is not None:
            out[ticker] = snap
    return out


# ── Field-specific quarterly/point-in-time primitives ──────────────────────
# Added for the DCF-engine migration (legacy_financial_table_retirement's
# sibling task, dcf_financial_repository_migration) -- av_fcff_2.py's own
# income_statement()/balance_sheet()/cash_flow_statement()/research_and_
# development() wrappers need a TTM-rolling-quarter shape (income/cash-flow:
# most recent N quarters blocked into non-overlapping 4-quarter sums, NOT
# fiscal-year-aligned -- confirmed required by docs/decisions.md 2026-10-01,
# "HessGrp's canonical flow-variable methodology is quarterly-derived
# trailing-12-month periods, not fiscal-year statements") and a point-in-
# time year-over-year shape (balance sheet: current period + the SAME
# relative fiscal_period in up to N-1 prior fiscal years), both DIFFERENT
# from moat_score.py's own FY-aligned annual design above.
#
# CRITICAL DESIGN RULE, the actual subject of this migration's field-
# availability fix: period discovery (which fiscal_year/fiscal_period
# periods exist at all) is NEVER gated by any specific tag's presence --
# it queries `fundamentals` directly, independent of `financial_values`.
# Each field/tag is then read independently per discovered period. A
# company missing gross_profit/ebit (e.g. a bank or REIT, confirmed common
# in the legacy_financial_table_retirement population comparison) must
# never lose access to cash/debt/operating_cf/shares -- those are read
# from the SAME periods via a SEPARATE lookup per tag, not blocked by
# gross_profit's absence. This corrects the all-or-nothing intersection
# `fetch_annual_financials_from_repository()` above deliberately still
# uses for moat_score.py's OWN multi-year-series needs (unchanged, still
# correct for that caller) -- the DCF engine's needs are different enough
# (single-field-at-a-time access, TTM/point-in-time rather than annual-FY)
# to warrant these new, separate functions rather than retrofitting the
# existing one.
# ─────────────────────────────────────────────────────────────────────────

def _recent_periods(conn: sqlite3.Connection, company_id: int, statement_type: str, limit: int = 20) -> list:
    """Field-INDEPENDENT period discovery: every (fiscal_year,
    fiscal_period) this company has a current, non-quarantined
    `fundamentals` row for, most-recent first by period_end_date -- a
    period with zero populated tags still appears here, since this reads
    `fundamentals` directly, never joining through `financial_values`.
    Never raises; returns [] if the company/statement_type has no
    fundamentals on file at all."""
    return conn.execute(
        """SELECT fundamental_id, fiscal_year, fiscal_period, period_end_date
           FROM fundamentals
           WHERE company_id = ? AND statement_type = ? AND is_current = 1 AND quarantined = 0
           ORDER BY period_end_date DESC
           LIMIT ?""",
        (company_id, statement_type, limit),
    ).fetchall()


def _values_for_fundamentals(conn: sqlite3.Connection, fundamental_ids: list) -> dict:
    """{fundamental_id: {intrinio_tag: value}}, current/non-rejected/non-null
    values only. Field-independent: a fundamental missing a given tag
    simply has no key for it in its sub-dict -- never affects any other
    fundamental's or any other tag's availability."""
    if not fundamental_ids:
        return {}
    placeholders = ",".join("?" for _ in fundamental_ids)
    rows = conn.execute(
        f"""SELECT v.fundamental_id, t.intrinio_tag, v.value
           FROM financial_values v
           JOIN financial_tags t ON t.tag_id = v.tag_id
           WHERE v.fundamental_id IN ({placeholders})
             AND v.validation_status != 'rejected' AND v.value IS NOT NULL""",
        fundamental_ids,
    ).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["fundamental_id"], {})[r["intrinio_tag"]] = r["value"]
    return out


def fetch_quarterly_ttm(
    ticker: str, statement_type: str, tags: dict, n_quarters: int = 12,
    db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH,
) -> Optional[dict]:
    """
    TTM-rolling-quarter fetch for income_statement/cash_flow_statement --
    the shape av_fcff_2.py's income_statement()/cash_flow_statement()/
    research_and_development() wrappers need. Mirrors
    hg_dcflib.get_inc_stmnt_intrinio()'s own mechanic exactly: takes the
    most recent `n_quarters` REAL quarters (never fabricated), blocks them
    into non-overlapping groups of 4 (most-recent block = index 0, a true
    trailing-12-month figure, NOT fiscal-year-aligned), sums each tag
    within each block.

    Returns None only if the ticker can't be resolved or has ZERO periods
    on file for this statement_type at all -- the caller's own existing
    ValueError ("No quarterly reports found...") is what should surface
    this, not a new error type.

    `tags`: {field_name: intrinio_tag}. Returned `fields[field_name]` is
    that field's own list of block sums, independent of every OTHER
    field's availability (the field-specific-availability fix this
    migration exists to make) -- a field truncates at its own first
    incomplete block from the front (never a fabricated/partial block, and
    never skips a gap to keep going), but one field's truncation can never
    shorten or block another field's list. A field with literally no
    coverage in these periods returns an empty list, not an error, not 0.0.

    `latest_values`: the raw {intrinio_tag: value} dict for the single
    most recent quarter's fundamental, for callers that need to inspect
    tag PRESENCE directly (e.g. av_fcff_2.py's bank-template-misclassification
    guard: "totalinterestincome present, totaloperatingincome absent as a
    KEY" -- distinguishable here from "present with value 0.0" because this
    dict only contains tags that actually had a non-null row).
    """
    conn = _connect(db_path)
    try:
        company_id = resolve_company_id(conn, ticker)
        if company_id is None:
            return None
        periods = _recent_periods(conn, company_id, statement_type, limit=n_quarters)
        if not periods:
            return None
        fundamental_ids = [p["fundamental_id"] for p in periods]
        values = _values_for_fundamentals(conn, fundamental_ids)

        n_blocks = len(periods) // 4
        field_blocks: dict = {}
        for field, tag in tags.items():
            series = []
            for b in range(n_blocks):
                block = periods[b * 4:(b + 1) * 4]
                block_vals = []
                complete = True
                for p in block:
                    v = values.get(p["fundamental_id"], {}).get(tag)
                    if v is None:
                        complete = False
                        break
                    block_vals.append(v)
                if not complete:
                    break
                series.append(sum(block_vals))
            field_blocks[field] = series

        latest = periods[0]
        return {
            "periods": [(p["fiscal_year"], p["fiscal_period"], p["period_end_date"]) for p in periods],
            "fields": field_blocks,
            "latest_period": (latest["fiscal_year"], latest["fiscal_period"], latest["period_end_date"]),
            "latest_values": values.get(latest["fundamental_id"], {}),
        }
    finally:
        conn.close()


def fetch_point_in_time_series(
    ticker: str, statement_type: str, tags: dict, n_periods: int = 5,
    db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH,
) -> Optional[dict]:
    """
    Point-in-time, year-over-year fetch for balance_sheet_statement -- the
    shape av_fcff_2.py's balance_sheet() wrapper needs. Mirrors
    hg_dcflib.get_bal_sheet_intrinio()'s own mechanic: picks the single
    most recent period as "current" (across Q1/Q2/Q3/FY -- this repository
    tags a fiscal-year-end snapshot 'FY', not 'Q4'; see this module's own
    top docstring for the confirmed-live reason), then matches the SAME
    relative fiscal_period in up to `n_periods - 1` prior fiscal years
    (year-over-year, never a different quarter) -- never a different
    quarter standing in for a missing one.

    Returns None only if the ticker can't be resolved or has ZERO periods
    on file for this statement_type at all.

    `tags`: {field_name: intrinio_tag}. Returned `fields[field_name]` is a
    list aligned to `periods` (index 0 = current), one entry per matched
    period -- `None` where that SPECIFIC field has no value at that
    SPECIFIC period (never fabricated, never defaults to 0 here -- the
    caller applies the existing `safe_float()` zero-default at the point
    it already does for every other vendor field, preserving the exact
    existing missing-single-field convention). A field missing at one
    period never shortens the list or affects any other field.
    """
    conn = _connect(db_path)
    try:
        company_id = resolve_company_id(conn, ticker)
        if company_id is None:
            return None
        all_periods = _recent_periods(conn, company_id, statement_type, limit=40)
        if not all_periods:
            return None

        current = all_periods[0]
        target_period = current["fiscal_period"]
        current_fy = current["fiscal_year"]
        by_year_period = {(p["fiscal_year"], p["fiscal_period"]): p for p in all_periods}

        selected = [current]
        for offset in range(1, n_periods):
            match = by_year_period.get((current_fy - offset, target_period))
            if match is None:
                break
            selected.append(match)

        fundamental_ids = [p["fundamental_id"] for p in selected]
        values = _values_for_fundamentals(conn, fundamental_ids)

        field_series: dict = {}
        for field, tag in tags.items():
            field_series[field] = [
                values.get(p["fundamental_id"], {}).get(tag) for p in selected
            ]

        return {
            "periods": [(p["fiscal_year"], p["fiscal_period"], p["period_end_date"]) for p in selected],
            "fields": field_series,
        }
    finally:
        conn.close()


def fetch_lynch_annual_reports(
    ticker: str, years: int = 10, db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH,
) -> Optional[dict]:
    """Aligned fiscal-year annual statement inputs; no network or cash-flow
    dependency. Despite the name, also serves growth_monitor.py's and
    growth_screen_2.py's identical annual-statement need (both migrated
    2026-11-18) -- the field set is a strict superset of what either
    caller needs, so this is reuse, not a near-duplicate second function.

    Flows require Q1-Q4; balance-sheet values are FY snapshots for the
    SAME fiscal year. Stop at a missing year rather than compressing time
    in CAGR calculations. Annual weighted-average diluted shares are the
    mean of quarterly observations (basic shares only if diluted coverage
    is incomplete); this is a proxy for the former provider FY denominator.
    operatingIncome/incomeBeforeTax/incomeTaxExpense (added 2026-11-18 for
    growth_screen_2.py's ROIC reporting field, never a screen gate for any
    caller) are optional the same way grossProfit already was. Optional
    missing fields remain None; the caller retains its existing
    single-field defaults and records those gaps explicitly.
    """
    conn = _connect(db_path)
    try:
        cid = resolve_company_id(conn, ticker)
        if cid is None:
            return None
        snapshots = conn.execute(
            "SELECT fundamental_id,fiscal_year,period_end_date,quarantined FROM fundamentals "
            "WHERE company_id=? AND statement_type='balance_sheet_statement' "
            "AND fiscal_period='FY' AND is_current=1 ORDER BY fiscal_year DESC", (cid,)
        ).fetchall()
        if not snapshots or snapshots[0]['quarantined']:
            return None
        by_year = {r['fiscal_year']: r for r in snapshots if not r['quarantined']}
        pit = _values_for_fundamentals(conn, [r['fundamental_id'] for r in by_year.values()])
        tags = {'totalRevenue': 'totalrevenue', 'netIncome': 'netincome',
                'grossProfit': 'totalgrossprofit', 'diluted': 'weightedavebasicdilutedsharesos',
                'basic': 'weightedavebasicsharesos',
                # Added 2026-11-18 for growth_monitor.py's/growth_screen_2.py's
                # ROIC reporting field (never a gate for either caller) --
                # same optional-field treatment as grossProfit below, never
                # part of the completeness check two lines down.
                'operatingIncome': 'totaloperatingincome', 'incomeBeforeTax': 'totalpretaxincome',
                'incomeTaxExpense': 'incometaxexpense'}
        flows = {f: _quarterly_values(conn, cid, 'income_statement', tag) for f, tag in tags.items()}
        inc, bal, fiscal_years, gaps = [], [], [], set()
        for year in range(snapshots[0]['fiscal_year'], snapshots[0]['fiscal_year']-years, -1):
            if year not in by_year:
                break
            if any(set(flows[f].get(year, {})) != {'Q1','Q2','Q3','Q4'} for f in ('totalRevenue','netIncome')):
                break
            def annual(field):
                qs = flows[field].get(year, {})
                return sum(qs.values()) if set(qs) == {'Q1','Q2','Q3','Q4'} else None
            diluted, basic = annual('diluted'), annual('basic')
            shares = diluted/4 if diluted is not None else basic/4 if basic is not None else None
            if diluted is None:
                gaps.add('historical shares use basic proxy' if basic is not None else 'historical shares unavailable; legacy EPS fallback applies')
            income = {f: annual(f) for f in ('totalRevenue','netIncome','grossProfit',
                                              'operatingIncome','incomeBeforeTax','incomeTaxExpense')}
            income['commonStockSharesOutstanding'] = shares
            raw = pit.get(by_year[year]['fundamental_id'], {})
            mapping = {'totalShareholderEquity': 'totalcommonequity', 'longTermDebt':'longtermdebt',
                       'shortTermDebt':'shorttermdebt', 'cashAndCashEquivalentsAtCarryingValue':'cashandequivalents',
                       'shortTermInvestments':'shortterminvestments', 'inventory':'netinventory',
                       'currentNetReceivables':'accountsreceivable'}
            balance = {f: raw.get(tag) for f,tag in mapping.items()}
            cash, sti = raw.get('cashandequivalents'), raw.get('shortterminvestments')
            balance['cashAndShortTermInvestments'] = cash+(sti or 0.0) if cash is not None else None
            for field,value in {**income,**balance}.items():
                if value is None:
                    gaps.add(field+' missing; existing scorer default applies')
            inc.append(income); bal.append(balance); fiscal_years.append(year)
        if not fiscal_years:
            return None
        return {'income':inc,'balance':bal,'fiscal_years':fiscal_years,
                'notes':'; '.join(sorted(gaps)), 'source':'financial_data_repository'}
    finally:
        conn.close()


def fetch_quarterly_eps_series(
    ticker: str, n_quarters: int = 20, db_path: str = FINANCIAL_DATA_REPOSITORY_DB_PATH,
) -> Optional[list[dict]]:
    """Up to `n_quarters` individual, UNAGGREGATED quarterly EPS observations,
    most-recent-first -- the shape growth_monitor.py's/growth_screen_2.py's
    negative-quarterly-EPS growth-screen gate needs (added 2026-11-18,
    migrating them off hg_dcflib.get_quarterly_eps_intrinio()). Deliberately
    separate from fetch_quarterly_ttm()/fetch_lynch_annual_reports(): those
    both SUM quarters into TTM blocks or fiscal years; this caller needs each
    quarter's own reported figure, not a rolling or annual aggregate.

    Field map (confirmed live against real repository data):
      reportedEPS -> dilutedeps, falling back to basiceps for a period where
                     the diluted tag is absent -- same per-period fallback
                     get_quarterly_eps_intrinio() already used.
      fiscalDateEnding -> period_end_date (the real calendar date this
                          repository already stores -- strictly better than
                          the live function's own synthesized
                          "{fiscal_year}-{fiscal_period}" label, which was
                          only ever a stand-in for a date Intrinio's live
                          discovery call didn't expose at that call site).

    Returns None only if the ticker can't be resolved or has zero
    income_statement periods on file at all. A period present but missing
    BOTH eps tags gets reportedEPS=None (never fabricated, never skipped --
    matches the live function's own per-period degrade, so callers' existing
    None-handling needs no changes).
    """
    conn = _connect(db_path)
    try:
        company_id = resolve_company_id(conn, ticker)
        if company_id is None:
            return None
        periods = _recent_periods(conn, company_id, "income_statement", limit=n_quarters)
        if not periods:
            return None
        values = _values_for_fundamentals(conn, [p["fundamental_id"] for p in periods])
        out = []
        for p in periods:
            tag_values = values.get(p["fundamental_id"], {})
            eps = tag_values.get("dilutedeps")
            if eps is None:
                eps = tag_values.get("basiceps")
            out.append({"fiscalDateEnding": p["period_end_date"], "reportedEPS": eps})
        return out
    finally:
        conn.close()
