"""
Earnings whisper truth--
Price dances with hope and fear,
Worth hides in the mist.

S&P 500 / Russell 2000 batch valuation using FCFF DCF model.
Outputs results to value_<index>_YYYYMMDD.xlsx, sorted by margin of safety.

NOTE: Alpha Vantage free tier allows ~25 API requests/day (5/min).
      Each stock requires ~4-5 calls; use --limit N to cap the number of stocks.
      Usage: python av_fcff_2.py [--limit N] [--growth N]
"""

from dataclasses import dataclass
from datetime import date
import sqlite3
import hg_dcflib
import logging
import os
import sys
import time
import traceback
import io
import pandas as pd
import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.WARNING)
stream_handler.setFormatter(formatter)

file_handler = logging.FileHandler("data/value.log")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(formatter)

logger.addHandler(stream_handler)
logger.addHandler(file_handler)


# ---------------------------------------------------------------------------
# Constants (fetched once at startup)
# ---------------------------------------------------------------------------

MY_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")
FRED_KEY = os.environ.get("FRED_API_KEY")
if not MY_API_KEY:
    raise EnvironmentError(
        "ALPHA_VANTAGE_API_KEY is not set. Run: export ALPHA_VANTAGE_API_KEY='your_key'"
    )
if not FRED_KEY:
    raise EnvironmentError(
        "FRED_API_KEY is not set. Run: export FRED_API_KEY='your_key'"
    )

MARGINAL_TAX_RATE = 0.26
GROWTH_PERIOD = 5  # high-growth years; override with --growth N
EQ_PREM = hg_dcflib.get_erp()
RISK_FREE = hg_dcflib.get_risk_free(FRED_KEY)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class Stock_Value:
    ticker: str
    valuation_date: str
    ent_name: str
    industry: str
    beta: float
    market_cap: float
    price: float
    shares_outstanding: float
    risk_free_rate: float
    eq_premium: float
    growth_rate: float
    cost_of_capital: float
    wealth_pc: float
    fcff_value: float
    terminal_value: float
    share_value: float
    margin_of_safety: float
    margin_of_safety_pc: float


# ---------------------------------------------------------------------------
# S&P 500 ticker list
# ---------------------------------------------------------------------------


def get_sp500_tickers() -> list:
    """Fetch current S&P 500 constituents from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text), header=0)
    df = tables[0]
    tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
    logger.info(f"Fetched {len(tickers)} S&P 500 tickers")
    return tickers


def get_russell2000_tickers() -> list:
    """Fetch current Russell 2000 constituents from the iShares IWM ETF holdings CSV."""
    url = (
        "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
        "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
    )
    headers = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    # iShares CSV has several metadata rows before the column headers.
    # Find the header row by looking for a line that contains "Ticker".
    lines = resp.text.splitlines()
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("Ticker"))
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), header=0)
    df = df[df["Asset Class"] == "Equity"]
    tickers = (
        df["Ticker"].dropna().str.strip().str.replace(".", "-", regex=False).tolist()
    )
    logger.info(f"Fetched {len(tickers)} Russell 2000 tickers")
    return tickers


# ---------------------------------------------------------------------------
# Financial statement helpers
# ---------------------------------------------------------------------------


def income_statement(ticker, api_key):
    return hg_dcflib.get_inc_stmnt(ticker, api_key)


def balance_sheet(ticker, api_key):
    return hg_dcflib.get_bal_sheet(ticker, api_key)


def cash_flow_statement(ticker, api_key):
    return hg_dcflib.get_cash_flow(ticker, api_key)


def enterprise_quote(ticker, api_key):
    return hg_dcflib.get_quote(ticker, api_key)


# ---------------------------------------------------------------------------
# Industry classification helpers
# ---------------------------------------------------------------------------

_FINANCIAL_KEYWORDS = {
    "bank",
    "banks",
    "financial services",
    "insurance",
    "brokerage",
    "investment banking",
    "thrift",
    "savings",
    "credit",
    "mortgage",
    "asset management",
}

_INSURANCE_KEYWORDS = {"insurance", "reinsurance", "surety", "title insurance"}


def is_financial_firm(industry: str) -> bool:
    """Return True if the industry is a financial firm requiring FCFE valuation."""
    low = industry.lower()
    return any(kw in low for kw in _FINANCIAL_KEYWORDS)


def is_insurance_firm(industry: str) -> bool:
    """Return True for insurance companies that need normalized NI."""
    low = industry.lower()
    return any(kw in low for kw in _INSURANCE_KEYWORDS)


def _normalized_net_income(net_income_list: list) -> tuple[float, int]:
    """
    Return (normalized_NI, years_used).
    Uses up to 5 years of NI, excluding any years where NI < 0
    (catastrophe or reserve-charge years) if at least 2 positive years exist.
    Falls back to simple average if most years are negative.
    """
    positives = [ni for ni in net_income_list if ni > 0]
    if len(positives) >= 2:
        avg = sum(positives) / len(positives)
        return avg, len(positives)
    # If fewer than 2 positive years, use all available years
    avg = sum(net_income_list) / len(net_income_list)
    return avg, len(net_income_list)


# ---------------------------------------------------------------------------
# Calculation functions
# ---------------------------------------------------------------------------


def calc_stable_beta(unlevered_beta):
    if unlevered_beta < 0.5:
        stable_beta = 0.8
    elif unlevered_beta > 1.5:
        stable_beta = 1.2
    else:
        stable_beta = 1.0
    logger.info(f"Stable beta = {stable_beta:,.3f}")
    return stable_beta


def calc_capital_expenditures(cash_flw):
    capex_years = cash_flw["capex"][:5]
    return sum(capex_years) / len(capex_years)


def calc_chng_wc(bal_sht):
    curr_yr_nc_wc = (
        bal_sht["total_current_assets"][0] - bal_sht["cash_and_equivalents"][0]
    ) - (bal_sht["total_current_liabilities"][0] - bal_sht["short_term_debt"][0])
    prior_yr_nc_wc = (
        bal_sht["total_current_assets"][1] - bal_sht["cash_and_equivalents"][1]
    ) - (bal_sht["total_current_liabilities"][1] - bal_sht["short_term_debt"][1])
    return curr_yr_nc_wc - prior_yr_nc_wc


def capitalizerAndD(ticker, rd_years, api_key):
    rdTable = hg_dcflib.get_rAndD(ticker, rd_years, api_key)
    rd_dict, years_to_process = rdTable
    logger.info(f"rdTable = {rdTable}")
    rd_table = {}
    rd_expense = []
    unamort_percent = []
    unamort_amt = []
    amort_percentage = 1.0 / (rd_years - 1)

    current_year_total_amortization = 0
    for year in range(1, min(years_to_process, rd_years)):
        current_year_total_amortization += (
            rd_dict["research_and_development"][year] * amort_percentage
        )

    rd_asset_value = 0
    for year in range(years_to_process):
        expense = rd_dict["research_and_development"][year]
        percent_unamort = 1.0 - (amort_percentage * year)
        unamort = expense * percent_unamort
        rd_expense.append(expense)
        unamort_percent.append(percent_unamort)
        unamort_amt.append(unamort)
        rd_asset_value += unamort

    rd_table["rAndDExpense"] = rd_expense
    rd_table["unamortized_percent"] = unamort_percent
    rd_table["unamort_amount"] = unamort_amt
    rd_table["RD_Asset_Value"] = rd_asset_value
    rd_table["Current_Year_Amortization"] = current_year_total_amortization
    return rd_table


def calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate):
    ebiat = inc_stmnt["ebit"][0] * (1 - eff_tax_rate)
    logger.info(f"ebiat {ebiat:,.2f}")
    capex = calc_capital_expenditures(cash_flw)
    logger.info(f"Capex {capex:,.2f}")
    chng_nc_wc = calc_chng_wc(bal_sht)
    logger.info(f"Change WC {chng_nc_wc:,.2f}")
    depreciation = cash_flw["depreciation"][0]
    logger.info(f"Depreciation {depreciation:,.2f}")
    fcff = ebiat - capex + depreciation - chng_nc_wc
    logger.info(f"FCFF {fcff:,.2f}")
    return [ebiat, capex, chng_nc_wc, depreciation, fcff]


def calc_reinvestment(capex, depreciation, chng_nc_wc, amort_schedule):
    firm_reinvestment = (
        capex
        - depreciation
        + chng_nc_wc
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["Current_Year_Amortization"]
    )
    logger.info(f"Firm Reinvestment {firm_reinvestment:,.2f}")
    return firm_reinvestment


def calc_adj_ebiat(ebiat, amort_schedule):
    adjusted_ebiat = (
        ebiat
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["Current_Year_Amortization"]
    )
    logger.info(f"Adjusted ebiat {adjusted_ebiat:,.2f}")
    return adjusted_ebiat


def calc_adj_bv_equity(bal_sht, amort_schedule):
    adjusted_bv_equity = (
        bal_sht["total_stockholders_equity"][0] + amort_schedule["RD_Asset_Value"]
    )
    logger.info(f"adjusted BV Equity = {adjusted_bv_equity:,.2f}")
    return adjusted_bv_equity


def calc_bv_debt(bal_sht):
    bv_debt = bal_sht["short_term_debt"][0] + bal_sht["long_term_debt"][0]
    logger.info(f"BV Debt = {bv_debt:,.2f}")
    return bv_debt


def calc_tax_rate(inc_stmnt):
    income_before_tax = inc_stmnt["incomeBeforeTax"][0]
    if income_before_tax <= 0:
        # Loss-making company: effective rate is meaningless; use marginal rate
        logger.info(
            f"Negative/zero pre-tax income — using marginal tax rate {MARGINAL_TAX_RATE:.4f}"
        )
        return MARGINAL_TAX_RATE
    eff_tax_rate = inc_stmnt["income_tax_expense"][0] / income_before_tax
    # Clamp to [0, marginal rate] to prevent sign-flip in FCFF projections
    eff_tax_rate = min(max(eff_tax_rate, 0.0), MARGINAL_TAX_RATE)
    logger.info(f"Effective Tax Rate = {eff_tax_rate:,.4f}")
    return eff_tax_rate


def calc_return_on_capital(adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht):
    return_on_capital = adjusted_ebiat / (
        adjusted_bv_equity + bv_debt - bal_sht["cash_and_equivalents"][0]
    )
    logger.info(f"ROIC = {return_on_capital:,.4f}")
    return return_on_capital


def calc_growth_rate(reinvestment_rate, return_on_capital):
    growth_rate = reinvestment_rate * return_on_capital
    logger.info(f"Growth Rate = {growth_rate:,.4f}")
    return growth_rate


def calc_discount_rate(inc_stmnt, bv_debt, market_cap_equity, beta, risk_free, eq_prem):
    cost_of_equity = risk_free + (beta * eq_prem)
    logger.info(f"COE = {cost_of_equity:,.4f}")

    try:
        int_cover = inc_stmnt["ebit"][0] / inc_stmnt["interest_expense"][0]
    except ZeroDivisionError:
        int_cover = 25

    logger.info(f"Interest Coverage = {int_cover}")
    def_spread = hg_dcflib.get_default_spread(int_cover)
    logger.info(f"Default Spread = {def_spread}")

    cost_of_debt = (risk_free + def_spread) * (1 - MARGINAL_TAX_RATE)
    logger.info(f"Cost of Debt = {cost_of_debt}")
    total_capital = market_cap_equity + bv_debt
    percent_debt = bv_debt / total_capital if total_capital > 0 else 0.5
    percent_equity = 1 - percent_debt

    cost_of_capital = (cost_of_debt * percent_debt) + (cost_of_equity * percent_equity)
    logger.info(f"Cost of Capital = {cost_of_capital:,.4f}")
    return cost_of_capital


def calc_expected_fcff(
    adjusted_ebit, eff_tax_rate, growth_rate, reinvestment_rate, growth_period
):
    # Grow EBIT each year, then derive EBIAT and FCFF.
    # Applying growth to EBIT (not EBIAT or FCFF) avoids sign errors when
    # the current FCFF is negative due to high reinvestment.
    ebit_n = []
    fcff_n = []
    for year in range(growth_period):
        if year == 0:
            ebit_n.append(adjusted_ebit * (1 + growth_rate))
        else:
            ebit_n.append(ebit_n[year - 1] * (1 + growth_rate))
        ebiat = ebit_n[year] * (1 - eff_tax_rate)
        fcff_n.append(ebiat * (1 - reinvestment_rate))
        logger.info(f"Expected FCFF year {year + 1} = {fcff_n[year]:,.2f}")
    return fcff_n


def calc_fcff_value(fcff_table, discount_rate, growth_period):
    fcff_value = 0
    for year in range(growth_period):
        fcff_pv = fcff_table[year] / ((1 + discount_rate) ** (year + 1))
        fcff_value += fcff_pv
    logger.info(f"FCFF Value = {fcff_value:,.2f}")
    return fcff_value


def calc_terminal_value(
    fcff_last, stable_cost_of_capital, growth_cost_of_capital, risk_free, growth_period
):
    terminal_value = (fcff_last * (1 + risk_free)) / (
        stable_cost_of_capital - risk_free
    )
    terminal_value_pv = terminal_value / ((1 + growth_cost_of_capital) ** growth_period)
    logger.info(f"Terminal Value = {terminal_value_pv:,.2f}")
    return terminal_value_pv


def calc_intrinsic_value(
    fcff_pv, terminal_value_pv, cash_and_equivalents, bv_debt, shares_outstanding
):
    enterprise_value = fcff_pv + terminal_value_pv + cash_and_equivalents - bv_debt
    intrinsic_value = enterprise_value / shares_outstanding
    logger.info(f"Enterprise Value = {enterprise_value:,.2f}")
    logger.info(f"Intrinsic Value = {intrinsic_value:,.2f}")
    return intrinsic_value


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def create_table(conn):
    schema_sql = """CREATE TABLE IF NOT EXISTS valuation (
              ticker TEXT NOT NULL,
              valuation_date TEXT NOT NULL,
              ent_name TEXT NOT NULL,
              industry TEXT NOT NULL,
              beta REAL NOT NULL,
              market_cap REAL NOT NULL,
              price REAL NOT NULL,
              shares_outstanding REAL NOT NULL,
              risk_free_rate REAL NOT NULL,
              eq_premium REAL NOT NULL,
              growth_rate REAL NOT NULL,
              cost_of_capital REAL NOT NULL,
              wealth_pc REAL NOT NULL,
              fcff_value REAL NOT NULL,
              terminal_value REAL NOT NULL,
              share_value REAL NOT NULL,
              margin_of_safety REAL NOT NULL,
              margin_of_safety_pc REAL NOT NULL,
              PRIMARY KEY (ticker)
              );"""
    try:
        # Check if table exists with old (ticker, valuation_date) composite PK
        # and migrate if needed so that each ticker has only one row.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='valuation'"
        ).fetchone()
        if row and "PRIMARY KEY (ticker, valuation_date)" in row[0]:
            logger.info("Migrating valuation table to single-ticker primary key ...")
            conn.execute("ALTER TABLE valuation RENAME TO valuation_old")
            conn.execute(schema_sql)
            # Keep only the most recent row per ticker
            conn.execute("""
                INSERT INTO valuation
                SELECT ticker, valuation_date, ent_name, industry, beta, market_cap,
                       price, shares_outstanding, risk_free_rate, eq_premium, growth_rate,
                       cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value,
                       margin_of_safety, margin_of_safety_pc
                FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY valuation_date DESC) rn
                    FROM valuation_old
                ) WHERE rn = 1
            """)
            conn.execute("DROP TABLE valuation_old")
            conn.commit()
            logger.info("Migration complete.")
        else:
            conn.execute(schema_sql)
            conn.commit()
            logger.info("Table created successfully")
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to create tables: {e}")


def insert_valuation(conn, val):
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO valuation
           (ticker, valuation_date, ent_name, industry, beta, market_cap, price,
            shares_outstanding, risk_free_rate, eq_premium, growth_rate,
            cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value,
            margin_of_safety, margin_of_safety_pc)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            val.ticker,
            val.valuation_date,
            val.ent_name,
            val.industry,
            val.beta,
            val.market_cap,
            val.price,
            val.shares_outstanding,
            val.risk_free_rate,
            val.eq_premium,
            val.growth_rate,
            val.cost_of_capital,
            val.wealth_pc,
            val.fcff_value,
            val.terminal_value,
            val.share_value,
            val.margin_of_safety,
            val.margin_of_safety_pc,
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Bank / financial-firm valuation  (FCFE equity DCF)
# ---------------------------------------------------------------------------


def _bank_payout_ratio(
    net_income: float, bv_equity_curr: float, bv_equity_prior: float, cash_flw: dict
) -> float:
    """
    Determine payout ratio for a bank using the most reliable source available.

    Priority:
      1. Dividends paid from cash flow (most direct; sum of quarterly outflows)
      2. Equity-change method (net income minus equity retained on balance sheet)
      3. Fallback: 40% payout (typical for well-run regional bank)

    AOCI swings (unrealized bond gains/losses) inflate the equity-change figure,
    so if that method would imply retention > 80% we prefer the dividend method.
    """
    payout = None

    # --- Method 1: actual dividends paid ---
    divs = sum(abs(v) for v in cash_flw.get("dividends_paid", []) if v)
    if net_income > 0 and divs > 0:
        payout_from_divs = divs / net_income
        if 0.05 <= payout_from_divs <= 0.95:
            payout = payout_from_divs
            logger.info(f"Payout ratio from dividends paid: {payout:.4f}")

    # --- Method 2: equity-change (only use if dividends unavailable/unreliable) ---
    if payout is None and net_income > 0:
        equity_change = bv_equity_curr - bv_equity_prior
        retention = equity_change / net_income
        if 0.0 <= retention <= 0.80:  # AOCI likely not distorting
            payout = 1.0 - retention
            logger.info(f"Payout ratio from equity change: {payout:.4f}")

    # --- Method 3: fallback ---
    if payout is None:
        payout = 0.40
        logger.info(f"Payout ratio: using fallback {payout:.4f}")

    return payout


def value_bank_stock(ticker: str, growth_period: int):
    """
    FCFE-based equity DCF for banks and financial firms.

    Key differences from FCFF:
    - Starts with Net Income, not EBIT
    - Reinvestment = equity retained to support asset growth (g / ROE)
    - Discounts at Cost of Equity, not WACC
    - No debt/cash adjustment — we work at the equity level throughout
    - No R&D capitalisation (not applicable to financials)
    """
    logger.info(f"Valuing {ticker} as financial firm (FCFE)")
    try:
        industry = hg_dcflib.get_industry(ticker)
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        valuation_date = str(date.today())
        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]

        reported_net_income = inc_stmnt["netIncome"][0]
        bv_equity_curr = bal_sht["total_stockholders_equity"][0]
        bv_equity_prior = bal_sht["total_stockholders_equity"][1]

        # Insurance firms: normalize NI over available years to smooth
        # underwriting cycles and catastrophe years.
        if is_insurance_firm(industry):
            net_income, ni_years = _normalized_net_income(inc_stmnt["netIncome"])
            logger.info(
                f"Insurance: normalized NI over {ni_years} years = {net_income:,.0f}  (TTM = {reported_net_income:,.0f})"
            )
        else:
            net_income = reported_net_income
            ni_years = 1

        # --- ROE and growth ---
        roe = net_income / bv_equity_curr if bv_equity_curr != 0 else 0.0
        logger.info(f"ROE = {roe:.4f}")

        payout_ratio = _bank_payout_ratio(
            reported_net_income, bv_equity_curr, bv_equity_prior, cash_flw
        )
        retention_ratio = 1.0 - payout_ratio
        growth_rate = min(roe * retention_ratio, 0.30)
        logger.info(
            f"Growth rate = {growth_rate:.4f}  (ROE={roe:.4f} × retention={retention_ratio:.4f})"
        )

        # --- Cost of equity (no WACC — debt is operational for banks) ---
        cost_of_equity = RISK_FREE + (unlevered_beta * EQ_PREM)
        logger.info(f"Cost of Equity = {cost_of_equity:.4f}")

        # --- Project FCFE ---
        # Grow net income; FCFE = net income retained as dividends (payout fraction)
        payout_ratio = 1.0 - retention_ratio
        ni_n = []
        fcfe_n = []
        for year in range(growth_period):
            ni = net_income * (1 + growth_rate) ** (year + 1)
            ni_n.append(ni)
            fcfe_n.append(ni * payout_ratio)
            logger.info(f"FCFE year {year + 1} = {fcfe_n[-1]:,.2f}")

        fcfe_pv = sum(
            fcfe_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period)
        )

        # --- Stable phase ---
        # In stable phase ROE converges to cost of equity (competitive equilibrium)
        stable_beta = calc_stable_beta(unlevered_beta)
        stable_cost_of_equity = RISK_FREE + (stable_beta * EQ_PREM)
        stable_growth = RISK_FREE
        stable_reinv = stable_growth / stable_cost_of_equity  # stable ROE = stable CoE
        stable_fcfe = fcfe_n[-1] * (1 + stable_growth) * (1 - stable_reinv)
        terminal_value = stable_fcfe / (stable_cost_of_equity - stable_growth)
        terminal_value_pv = terminal_value / (1 + cost_of_equity) ** growth_period

        equity_value = fcfe_pv + terminal_value_pv
        intrinsic_value = equity_value / shares_outstanding  # both in consistent units

        safety_margin = float(intrinsic_value - price)
        safety_margin_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0.0
        )
        wealth_pc = roe - cost_of_equity

        logger.info(f"Intrinsic value = {intrinsic_value:.2f}  Price = {price:.2f}")

        return Stock_Value(
            ticker=ticker,
            valuation_date=valuation_date,
            ent_name=ent_name,
            industry=industry,
            beta=unlevered_beta,
            market_cap=market_cap,
            price=price,
            shares_outstanding=shares_outstanding,
            risk_free_rate=RISK_FREE,
            eq_premium=EQ_PREM,
            growth_rate=growth_rate,
            cost_of_capital=cost_of_equity,  # equity rate, not WACC
            wealth_pc=wealth_pc,
            fcff_value=fcfe_pv,  # PV of FCFE
            terminal_value=terminal_value_pv,
            share_value=intrinsic_value,
            margin_of_safety=safety_margin,
            margin_of_safety_pc=safety_margin_pc,
        )

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Single-stock valuation
# ---------------------------------------------------------------------------


def value_stock(ticker: str, growth_period: int):
    """
    Route to the correct valuation model based on industry:
      - Financial firms (banks, insurance, etc.) → FCFE equity DCF
      - All others → FCFF firm DCF
    """
    try:
        industry = hg_dcflib.get_industry(ticker)
    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        return None

    if is_financial_firm(industry):
        return value_bank_stock(ticker, growth_period)

    return _value_stock_fcff(ticker, growth_period, industry)


def _value_stock_fcff(ticker: str, growth_period: int, industry: str):
    """
    FCFF DCF valuation for non-financial firms.
    Returns a Stock_Value dataclass or None if any step fails.
    """
    logger.info(f"Valuing {ticker} ...")
    try:
        rd_years = hg_dcflib.get_rAndD_years(industry) + 1
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        valuation_date = str(date.today())
        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]

        stable_beta = calc_stable_beta(unlevered_beta)
        eff_tax_rate = calc_tax_rate(inc_stmnt)
        fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate)

        ebiat = fcff_data[0]
        capex = fcff_data[1]
        chng_nc_wc = fcff_data[2]
        depreciation = fcff_data[3]

        amort_schedule = capitalizerAndD(ticker, rd_years, MY_API_KEY)
        logger.info(f"Amortization Schedule {amort_schedule}")

        adjusted_ebiat = calc_adj_ebiat(ebiat, amort_schedule)
        # Pre-tax adjusted EBIT — used as the base for projections so that
        # growth is applied to EBIT rather than EBIAT or FCFF.
        adjusted_ebit = (
            adjusted_ebiat / (1 - eff_tax_rate) if eff_tax_rate < 1 else adjusted_ebiat
        )
        firm_reinvestment = calc_reinvestment(
            capex, depreciation, chng_nc_wc, amort_schedule
        )
        adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
        bv_debt = calc_bv_debt(bal_sht)

        # Sanity check: if |EBIT| dwarfs the market cap by more than 10×,
        # the quarterly working-capital data is almost certainly corrupted.
        if market_cap > 0 and abs(adjusted_ebit) > 10 * market_cap:
            raise ValueError(
                f"Adjusted EBIT ({adjusted_ebit:,.0f}) is > 10× market cap "
                f"({market_cap:,.0f}) — likely bad WC data, skipping."
            )

        reinvestment_rate = min(max(firm_reinvestment / adjusted_ebiat, 0.0), 1.0)
        logger.info(f"Reinvestment rate = {reinvestment_rate:,.4f}")

        return_on_capital = calc_return_on_capital(
            adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht
        )
        growth_rate = min(calc_growth_rate(reinvestment_rate, return_on_capital), 0.30)

        discount_rate = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, unlevered_beta, RISK_FREE, EQ_PREM
        )
        logger.info(f"disc rate {discount_rate:,.4f}")

        fcff_table = calc_expected_fcff(
            adjusted_ebit, eff_tax_rate, growth_rate, reinvestment_rate, growth_period
        )
        fcff_pv = calc_fcff_value(fcff_table, discount_rate, growth_period)

        terminal_cost_of_capital = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, stable_beta, RISK_FREE, EQ_PREM
        )
        terminal_value_pv = calc_terminal_value(
            fcff_table[-1],
            terminal_cost_of_capital,
            discount_rate,
            RISK_FREE,
            growth_period,
        )
        intrinsic_value = calc_intrinsic_value(
            fcff_pv,
            terminal_value_pv,
            bal_sht["cash_and_equivalents"][0],
            bv_debt,
            shares_outstanding,
        )

        safety_margin = float(intrinsic_value - price)
        logger.info(f"Safety Margin: {safety_margin:,.2f}")
        safety_margin_pc = 1 - (price / intrinsic_value)
        wealth_pc = return_on_capital - discount_rate

        if return_on_capital > discount_rate:
            logger.info("Wealth Creator")
        else:
            logger.info("Wealth Destroyer")

        return Stock_Value(
            ticker=ticker,
            valuation_date=valuation_date,
            ent_name=ent_name,
            industry=industry,
            beta=unlevered_beta,
            market_cap=market_cap,
            price=price,
            shares_outstanding=shares_outstanding,
            risk_free_rate=RISK_FREE,
            eq_premium=EQ_PREM,
            growth_rate=growth_rate,
            cost_of_capital=discount_rate,
            wealth_pc=wealth_pc,
            fcff_value=fcff_pv,
            terminal_value=terminal_value_pv,
            share_value=intrinsic_value,
            margin_of_safety=safety_margin,
            margin_of_safety_pc=safety_margin_pc,
        )

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Single-stock detailed valuation (for Excel output)
# ---------------------------------------------------------------------------


def value_stock_detail(ticker: str, growth_period: int) -> dict | None:
    """
    Route to the correct detail valuation for the Excel report:
      - Financial firms → FCFE bank detail
      - All others      → FCFF detail
    """
    try:
        industry = hg_dcflib.get_industry(ticker)
    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        return None

    if is_financial_firm(industry):
        return _value_bank_stock_detail(ticker, growth_period, industry)
    return _value_stock_detail_fcff(ticker, growth_period, industry)


def _value_bank_stock_detail(
    ticker: str, growth_period: int, industry: str
) -> dict | None:
    """FCFE detail dict for bank/financial firms (used for Excel output)."""
    try:
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]

        reported_net_income = inc_stmnt["netIncome"][0]
        bv_equity_curr = bal_sht["total_stockholders_equity"][0]
        bv_equity_prior = bal_sht["total_stockholders_equity"][1]
        equity_change = bv_equity_curr - bv_equity_prior

        if is_insurance_firm(industry):
            net_income, ni_years = _normalized_net_income(inc_stmnt["netIncome"])
            logger.info(
                f"Insurance: normalized NI over {ni_years} years = {net_income:,.0f}  (TTM = {reported_net_income:,.0f})"
            )
        else:
            net_income = reported_net_income
            ni_years = 1

        roe = net_income / bv_equity_curr if bv_equity_curr != 0 else 0.0
        payout_ratio = _bank_payout_ratio(
            reported_net_income, bv_equity_curr, bv_equity_prior, cash_flw
        )
        retention_ratio = 1.0 - payout_ratio
        growth_rate = roe * retention_ratio

        cost_of_equity = RISK_FREE + (unlevered_beta * EQ_PREM)

        ni_n, fcfe_n = [], []
        for year in range(growth_period):
            ni = net_income * (1 + growth_rate) ** (year + 1)
            ni_n.append(ni)
            fcfe_n.append(ni * payout_ratio)

        fcfe_pv = sum(
            fcfe_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period)
        )

        stable_beta = calc_stable_beta(unlevered_beta)
        stable_cost_of_equity = RISK_FREE + (stable_beta * EQ_PREM)
        stable_growth = RISK_FREE
        stable_reinv = stable_growth / stable_cost_of_equity
        stable_fcfe = fcfe_n[-1] * (1 + stable_growth) * (1 - stable_reinv)
        terminal_value_undiscounted = stable_fcfe / (
            stable_cost_of_equity - stable_growth
        )
        terminal_value_pv = (
            terminal_value_undiscounted / (1 + cost_of_equity) ** growth_period
        )

        equity_value = fcfe_pv + terminal_value_pv
        intrinsic_value = equity_value / shares_outstanding
        margin_of_safety = float(intrinsic_value - price)
        margin_of_safety_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0
        )

        return {
            "model": "FCFE",
            "ticker": ticker,
            "ent_name": ent_name,
            "industry": industry,
            "valuation_date": str(date.today()),
            # --- Inputs ---
            "net_income": net_income,
            "reported_net_income": reported_net_income,
            "ni_years": ni_years,
            "bv_equity": bv_equity_curr,
            "bv_equity_prior": bv_equity_prior,
            "equity_change": equity_change,
            "roe": roe,
            "retention_ratio": retention_ratio,
            "payout_ratio": payout_ratio,
            # --- Growth phase ---
            "growth_period": growth_period,
            "growth_rate": growth_rate,
            "beta": unlevered_beta,
            "stable_beta": stable_beta,
            "risk_free": RISK_FREE,
            "eq_prem": EQ_PREM,
            "cost_of_equity": cost_of_equity,
            # --- Year-by-year projections ---
            "ni_n": ni_n,
            "fcfe_n": fcfe_n,
            # --- Stable phase ---
            "stable_growth": stable_growth,
            "stable_reinv": stable_reinv,
            "stable_fcfe": stable_fcfe,
            "stable_cost_of_equity": stable_cost_of_equity,
            "terminal_value_undiscounted": terminal_value_undiscounted,
            "terminal_value_pv": terminal_value_pv,
            # --- Valuation ---
            "fcfe_pv": fcfe_pv,
            "equity_value": equity_value,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "intrinsic_value": intrinsic_value,
            "margin_of_safety": margin_of_safety,
            "margin_of_safety_pc": margin_of_safety_pc,
        }

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


def _value_stock_detail_fcff(
    ticker: str, growth_period: int, industry: str
) -> dict | None:
    """FCFF detail dict for non-financial firms (used for Excel output)."""
    try:
        rd_years = hg_dcflib.get_rAndD_years(industry) + 1
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]

        stable_beta = calc_stable_beta(unlevered_beta)
        eff_tax_rate = calc_tax_rate(inc_stmnt)
        fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate)

        ebiat = fcff_data[0]
        capex = fcff_data[1]
        chng_nc_wc = fcff_data[2]
        depreciation = fcff_data[3]

        amort_schedule = capitalizerAndD(ticker, rd_years, MY_API_KEY)
        adjusted_ebiat = calc_adj_ebiat(ebiat, amort_schedule)
        adjusted_ebit = (
            adjusted_ebiat / (1 - eff_tax_rate) if eff_tax_rate < 1 else adjusted_ebiat
        )
        firm_reinvestment = calc_reinvestment(
            capex, depreciation, chng_nc_wc, amort_schedule
        )
        adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
        bv_debt = calc_bv_debt(bal_sht)

        if market_cap > 0 and abs(adjusted_ebit) > 10 * market_cap:
            raise ValueError(
                f"Adjusted EBIT ({adjusted_ebit:,.0f}) is > 10× market cap "
                f"({market_cap:,.0f}) — likely bad WC data, skipping."
            )

        reinvestment_rate = min(max(firm_reinvestment / adjusted_ebiat, 0.0), 1.0)
        return_on_capital = calc_return_on_capital(
            adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht
        )
        growth_rate = min(calc_growth_rate(reinvestment_rate, return_on_capital), 0.30)

        # Compute discount rate components inline to capture intermediates
        cost_of_equity = RISK_FREE + (unlevered_beta * EQ_PREM)
        try:
            int_cover = inc_stmnt["ebit"][0] / inc_stmnt["interest_expense"][0]
        except ZeroDivisionError:
            int_cover = 25
        def_spread = hg_dcflib.get_default_spread(int_cover)
        cost_of_debt_pretax = RISK_FREE + def_spread
        cost_of_debt_aftertax = cost_of_debt_pretax * (1 - MARGINAL_TAX_RATE)
        percent_debt = bv_debt / (adjusted_bv_equity + bv_debt)
        percent_equity = 1 - percent_debt
        discount_rate = (cost_of_debt_aftertax * percent_debt) + (
            cost_of_equity * percent_equity
        )

        # Stable phase
        stable_cost_of_equity = RISK_FREE + (stable_beta * EQ_PREM)
        stable_cost_of_capital = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, stable_beta, RISK_FREE, EQ_PREM
        )
        stable_growth = RISK_FREE
        stable_reinv_rate = (
            stable_growth / return_on_capital if return_on_capital != 0 else 0
        )

        # Year-by-year FCFF projections
        ebit_n, ebiat_n, reinv_n, fcff_n = [], [], [], []
        for year in range(growth_period):
            e = adjusted_ebit * (1 + growth_rate) ** (year + 1)
            eb = e * (1 - eff_tax_rate)
            r = eb * reinvestment_rate
            ebit_n.append(e)
            ebiat_n.append(eb)
            reinv_n.append(r)
            fcff_n.append(eb - r)

        fcff_pv = sum(
            fcff_n[y] / (1 + discount_rate) ** (y + 1) for y in range(growth_period)
        )

        stable_fcff = fcff_n[-1] * (1 + stable_growth)
        terminal_value_undiscounted = stable_fcff / (
            stable_cost_of_capital - stable_growth
        )
        terminal_value_pv = (
            terminal_value_undiscounted / (1 + discount_rate) ** growth_period
        )

        cash = bal_sht["cash_and_equivalents"][0]
        enterprise_value = fcff_pv + terminal_value_pv + cash - bv_debt
        intrinsic_value = enterprise_value / shares_outstanding
        margin_of_safety = float(intrinsic_value - price)
        margin_of_safety_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0
        )

        # Non-cash working capital (current year)
        curr_nc_wc = (
            bal_sht["total_current_assets"][0] - bal_sht["cash_and_equivalents"][0]
        ) - (bal_sht["total_current_liabilities"][0] - bal_sht["short_term_debt"][0])
        revenue = inc_stmnt["totalRevenue"][0]
        wc_pct_revenue = curr_nc_wc / revenue if revenue != 0 else 0

        return {
            "model": "FCFF",
            "ticker": ticker,
            "ent_name": ent_name,
            "industry": industry,
            "valuation_date": str(date.today()),
            # --- Inputs ---
            "normalized_ebit": inc_stmnt["ebit"][0],
            "adjusted_ebit": adjusted_ebit,
            "interest_expense": inc_stmnt["interest_expense"][0],
            "capex": capex,
            "depreciation": depreciation,
            "eff_tax_rate": eff_tax_rate,
            "revenue": revenue,
            "curr_nc_wc": curr_nc_wc,
            "chng_nc_wc": chng_nc_wc,
            "bv_debt": bv_debt,
            "adjusted_bv_equity": adjusted_bv_equity,
            "cash": cash,
            # --- Growth phase parameters ---
            "growth_period": growth_period,
            "growth_rate": growth_rate,
            "reinvestment_rate": reinvestment_rate,
            "return_on_capital": return_on_capital,
            "beta": unlevered_beta,
            "stable_beta": stable_beta,
            "risk_free": RISK_FREE,
            "eq_prem": EQ_PREM,
            "marginal_tax_rate": MARGINAL_TAX_RATE,
            # --- Discount rate components ---
            "cost_of_equity": cost_of_equity,
            "cost_of_debt_pretax": cost_of_debt_pretax,
            "cost_of_debt_aftertax": cost_of_debt_aftertax,
            "percent_debt": percent_debt,
            "percent_equity": percent_equity,
            "discount_rate": discount_rate,
            "wc_pct_revenue": wc_pct_revenue,
            # --- Stable phase ---
            "stable_growth": stable_growth,
            "stable_reinv_rate": stable_reinv_rate,
            "stable_fcff": stable_fcff,
            "stable_cost_of_equity": stable_cost_of_equity,
            "stable_cost_of_capital": stable_cost_of_capital,
            "terminal_value_undiscounted": terminal_value_undiscounted,
            "terminal_value_pv": terminal_value_pv,
            # --- Year-by-year projections ---
            "ebit_n": ebit_n,
            "ebiat_n": ebiat_n,
            "reinv_n": reinv_n,
            "fcff_n": fcff_n,
            # --- Valuation ---
            "fcff_pv": fcff_pv,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "intrinsic_value": intrinsic_value,
            "enterprise_value": enterprise_value,
            "margin_of_safety": margin_of_safety,
            "margin_of_safety_pc": margin_of_safety_pc,
        }

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Excel single-stock report
# ---------------------------------------------------------------------------


def _generate_xlsx_bank(
    ws,
    d,
    gp,
    label,
    val_dollar,
    val_pct,
    section_header,
    BLUE_FILL,
    YELLOW_FILL,
    GREEN_FILL,
):
    """Populate the worksheet for a bank / financial firm (FCFE model)."""
    from openpyxl.styles import Font

    r = 1
    ws.cell(
        row=r, column=1, value=f"{d['ent_name']} ({d['ticker']}) — FCFE Valuation"
    ).font = Font(bold=True, size=13)
    r += 1
    ws.cell(
        row=r,
        column=1,
        value=f"Bank / Financial Firm  |  {d['valuation_date']}  |  Industry: {d['industry']}",
    )
    r += 2

    # ---- Inputs --------------------------------------------------------
    section_header(r, 1, "Inputs")
    r += 1
    ni_label = (
        f"Net Income (normalized, {d['ni_years']}yr avg)"
        if d.get("ni_years", 1) > 1
        else "Net Income (TTM)"
    )
    inputs = [
        (ni_label, d["net_income"], "dollar"),
    ]
    if d.get("ni_years", 1) > 1:
        inputs.append(
            ("  Reported Net Income (TTM)", d["reported_net_income"], "dollar")
        )
    inputs += [
        ("Book Value of Equity (current)", d["bv_equity"], "dollar"),
        ("Book Value of Equity (prior yr)", d["bv_equity_prior"], "dollar"),
        ("Change in Book Equity", d["equity_change"], "dollar"),
        ("Return on Equity (ROE)", d["roe"], "pct"),
        ("Equity Retention Ratio", d["retention_ratio"], "pct"),
        ("Payout Ratio (FCFE / Net Income)", d["payout_ratio"], "pct"),
        ("Risk-free Rate", d["risk_free"], "pct"),
        ("Equity Risk Premium", d["eq_prem"], "pct"),
        ("Beta", d["beta"], "num"),
        ("Cost of Equity", d["cost_of_equity"], "pct"),
    ]
    for lbl, v, fmt in inputs:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        elif fmt == "pct":
            val_pct(r, 2, v, BLUE_FILL)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "0.00"
            c.fill = BLUE_FILL
        r += 1

    r += 1

    # ---- Parameters: High Growth vs Stable ----------------------------
    section_header(r, 1, "Parameters")
    label(r, 2, "High Growth", bold=True)
    label(r, 3, "Stable", bold=True)
    r += 1
    params = [
        ("Growth Rate", d["growth_rate"], "pct", d["stable_growth"]),
        ("Payout Ratio", d["payout_ratio"], "pct", 1 - d["stable_reinv"]),
        ("Beta", d["beta"], "num", d["stable_beta"]),
        ("Cost of Equity", d["cost_of_equity"], "pct", d["stable_cost_of_equity"]),
    ]
    for lbl, hg, fmt, st in params:
        label(r, 1, lbl)
        if fmt == "pct":
            val_pct(r, 2, hg, YELLOW_FILL)
            val_pct(r, 3, st, YELLOW_FILL)
        else:
            c = ws.cell(row=r, column=2, value=hg)
            c.number_format = "0.00"
            c.fill = YELLOW_FILL
            c2 = ws.cell(row=r, column=3, value=st)
            c2.number_format = "0.00"
        r += 1

    r += 1

    # ---- Year-by-year FCFE table --------------------------------------
    section_header(r, 1, f"Projected FCFE  (growth period = {gp} years)")
    for i in range(gp):
        ws.cell(row=r, column=2 + i, value=f"Year {i + 1}").font = Font(bold=True)
    r += 1

    table = [
        ("Expected Growth Rate", [d["growth_rate"]] * gp, "pct"),
        ("Net Income", d["ni_n"], "dollar"),
        ("FCFE (= NI × payout)", d["fcfe_n"], "dollar"),
        ("Cost of Equity", [d["cost_of_equity"]] * gp, "pct"),
        (
            "Cumulated CoE",
            [(1 + d["cost_of_equity"]) ** (y + 1) for y in range(gp)],
            "num",
        ),
        (
            "Present Value of FCFE",
            [d["fcfe_n"][y] / (1 + d["cost_of_equity"]) ** (y + 1) for y in range(gp)],
            "dollar",
        ),
    ]
    for lbl, values, fmt in table:
        label(r, 1, lbl)
        for i, v in enumerate(values):
            if fmt == "dollar":
                val_dollar(r, 2 + i, v)
            elif fmt == "pct":
                val_pct(r, 2 + i, v)
            else:
                c = ws.cell(row=r, column=2 + i, value=v)
                c.number_format = "0.0000"
        r += 1

    r += 1

    # ---- Stable phase -------------------------------------------------
    section_header(r, 1, "Stable Phase")
    r += 1
    stable = [
        ("Growth Rate in Stable Phase", d["stable_growth"], "pct"),
        ("Reinvestment Rate in Stable Phase", d["stable_reinv"], "pct"),
        ("FCFE in Stable Phase", d["stable_fcfe"], "dollar"),
        ("Cost of Equity in Stable Phase", d["stable_cost_of_equity"], "pct"),
        ("Terminal Value (undiscounted)", d["terminal_value_undiscounted"], "dollar"),
        ("PV of Terminal Value", d["terminal_value_pv"], "dollar"),
    ]
    for lbl, v, fmt in stable:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, YELLOW_FILL)
        else:
            val_pct(r, 2, v, YELLOW_FILL)
        r += 1

    r += 1

    # ---- Valuation summary --------------------------------------------
    section_header(r, 1, "Valuation")
    r += 1
    valuation = [
        ("PV of FCFE in High Growth Phase", d["fcfe_pv"], "dollar"),
        ("PV of Terminal Value", d["terminal_value_pv"], "dollar"),
        ("Equity Value", d["equity_value"], "dollar"),
        ("÷ Shares Outstanding", d["shares_outstanding"], "num"),
        ("Value of Equity per Share", d["intrinsic_value"], "dollar"),
        ("Stock Price", d["price"], "dollar"),
        ("Margin of Safety ($)", d["margin_of_safety"], "dollar"),
        ("Margin of Safety (%)", d["margin_of_safety_pc"], "pct"),
    ]
    for lbl, v, fmt in valuation:
        label(r, 1, lbl)
        fill = (
            GREEN_FILL
            if lbl
            in (
                "Value of Equity per Share",
                "Stock Price",
                "Margin of Safety ($)",
                "Margin of Safety (%)",
            )
            else None
        )
        if fmt == "dollar":
            val_dollar(r, 2, v, fill)
        elif fmt == "pct":
            val_pct(r, 2, v, fill)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "#,##0"
            if fill:
                c.fill = fill
        r += 1


def generate_xlsx(d: dict, output_path: str) -> None:
    """Write a Damodaran-style FCFF valuation worksheet for a single stock."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = d["ticker"]

    # ---- helpers --------------------------------------------------------
    BLUE_FILL = PatternFill("solid", fgColor="DDEEFF")
    YELLOW_FILL = PatternFill("solid", fgColor="FFFACD")
    GREEN_FILL = PatternFill("solid", fgColor="D4EDDA")
    HEADER_FONT = Font(bold=True)

    FMT_DOLLAR = "#,##0.00"
    FMT_PCT = "0.00%"

    def label(row, col, text, bold=False):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=bold)
        return c

    def val_dollar(row, col, v, fill=None):
        c = ws.cell(row=row, column=col, value=v)
        c.number_format = FMT_DOLLAR
        c.alignment = Alignment(horizontal="right")
        if fill:
            c.fill = fill
        return c

    def val_pct(row, col, v, fill=None):
        c = ws.cell(row=row, column=col, value=v)
        c.number_format = FMT_PCT
        c.alignment = Alignment(horizontal="right")
        if fill:
            c.fill = fill
        return c

    def section_header(row, col, text):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=True, underline="single")
        return c

    gp = d["growth_period"]

    # ---- Column widths --------------------------------------------------
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 14
    for col in range(4, 4 + gp):
        ws.column_dimensions[get_column_letter(col)].width = 12

    if d.get("model") == "FCFE":
        _generate_xlsx_bank(
            ws,
            d,
            gp,
            label,
            val_dollar,
            val_pct,
            section_header,
            BLUE_FILL,
            YELLOW_FILL,
            GREEN_FILL,
        )
        wb.save(output_path)
        print(f"Saved: {output_path}")
        return

    # ====================================================================
    # TITLE
    # ====================================================================
    r = 1
    ws.cell(row=r, column=1, value=f"{d['ent_name']} ({d['ticker']})").font = Font(
        bold=True, size=13
    )
    r += 1
    ws.cell(
        row=r,
        column=1,
        value=f"FCFF Valuation  |  {d['valuation_date']}  |  Industry: {d['industry']}",
    )
    r += 2

    # ====================================================================
    # SECTION 1 — INPUTS
    # ====================================================================
    section_header(r, 1, "Inputs")
    r += 1

    inputs = [
        ("Normalized EBIT (before adjustments)", d["normalized_ebit"], "dollar"),
        ("Adjusted EBIT", d["adjusted_ebit"], "dollar"),
        ("Adjusted Interest Expense", d["interest_expense"], "dollar"),
        ("Adjusted Capital Spending (avg 5yr)", d["capex"], "dollar"),
        ("Adjusted Depreciation & Amort'n", d["depreciation"], "dollar"),
        ("Tax Rate on Income", d["eff_tax_rate"], "pct"),
        ("Current Revenues", d["revenue"], "dollar"),
        ("Current Non-cash Working Capital", d["curr_nc_wc"], "dollar"),
        ("Chg. Working Capital", d["chng_nc_wc"], "dollar"),
        ("Adjusted Book Value of Debt", d["bv_debt"], "dollar"),
        ("Adjusted Book Value of Equity", d["adjusted_bv_equity"], "dollar"),
    ]
    for lbl, v, fmt in inputs:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        else:
            val_pct(r, 2, v, BLUE_FILL)
        r += 1

    r += 1  # blank

    # ====================================================================
    # SECTION 2 — PARAMETERS: HIGH GROWTH vs STABLE
    # ====================================================================
    section_header(r, 1, "Parameters")
    label(r, 2, "High Growth", bold=True)
    label(r, 3, "Stable", bold=True)
    r += 1

    params = [
        ("Length of High Growth Period", d["growth_period"], "int", "Forever"),
        ("Growth Rate", d["growth_rate"], "pct", d["stable_growth"]),
        ("Beta used for stock", d["beta"], "num", d["stable_beta"]),
        ("Risk-free Rate", d["risk_free"], "pct", d["risk_free"]),
        ("Equity Risk Premium", d["eq_prem"], "pct", d["eq_prem"]),
        (
            "Pre-tax Cost of Debt",
            d["cost_of_debt_pretax"],
            "pct",
            d["cost_of_debt_pretax"],
        ),
        (
            "Effective Tax Rate (cash flow)",
            d["eff_tax_rate"],
            "pct",
            d["marginal_tax_rate"],
        ),
        (
            "Marginal Tax Rate (cost of debt)",
            d["marginal_tax_rate"],
            "pct",
            d["marginal_tax_rate"],
        ),
        ("Return on Capital", d["return_on_capital"], "pct", d["return_on_capital"]),
        ("Reinvestment Rate", d["reinvestment_rate"], "pct", d["stable_reinv_rate"]),
        ("Debt / (Debt + Equity)", d["percent_debt"], "pct", d["percent_debt"]),
    ]
    for lbl, hg_val, fmt, st_val in params:
        label(r, 1, lbl)
        if fmt == "pct":
            val_pct(r, 2, hg_val, YELLOW_FILL)
            val_pct(r, 3, st_val, YELLOW_FILL) if isinstance(
                st_val, float
            ) else ws.cell(row=r, column=3, value=st_val)
        elif fmt == "int":
            ws.cell(row=r, column=2, value=hg_val).fill = YELLOW_FILL
            ws.cell(row=r, column=3, value=st_val)
        else:
            c = ws.cell(row=r, column=2, value=hg_val)
            c.number_format = "0.00"
            c.fill = YELLOW_FILL
            c2 = ws.cell(row=r, column=3, value=st_val)
            c2.number_format = "0.00"
        r += 1

    r += 1  # blank

    # ====================================================================
    # SECTION 3 — COST OF CAPITAL OUTPUT
    # ====================================================================
    section_header(r, 1, "Cost of Capital — Output")
    r += 1

    coc_rows = [
        ("Cost of Equity", d["cost_of_equity"], "pct"),
        ("Equity / (Debt + Equity)", d["percent_equity"], "pct"),
        ("After-tax Cost of Debt", d["cost_of_debt_aftertax"], "pct"),
        ("Debt / (Debt + Equity)", d["percent_debt"], "pct"),
        ("Cost of Capital (WACC)", d["discount_rate"], "pct"),
    ]
    for lbl, v, fmt in coc_rows:
        label(r, 1, lbl)
        val_pct(r, 2, v, GREEN_FILL)
        r += 1

    r += 1

    label(r, 1, "Working Capital as % of Revenue")
    val_pct(r, 2, d["wc_pct_revenue"])
    r += 2

    # ====================================================================
    # SECTION 4 — YEAR-BY-YEAR FCFF TABLE
    # ====================================================================
    section_header(r, 1, f"Projected FCFF  (growth period = {gp} years)")
    year_cols = list(range(1, gp + 1))
    for i, yr in enumerate(year_cols):
        ws.cell(row=r, column=2 + i, value=f"Year {yr}").font = HEADER_FONT
    r += 1

    net_capex = d["capex"] - d["depreciation"]
    # Proportion of reinvestment attributable to net capex vs WC change
    total_reinv0 = (
        net_capex + d["chng_nc_wc"] if (net_capex + d["chng_nc_wc"]) != 0 else 1
    )
    capex_frac = net_capex / total_reinv0
    wc_frac = d["chng_nc_wc"] / total_reinv0

    table_rows = [
        ("Expected Growth Rate", [d["growth_rate"]] * gp, "pct"),
        (
            "Cumulated Growth",
            [(1 + d["growth_rate"]) ** (y + 1) - 1 for y in range(gp)],
            "pct",
        ),
        ("Reinvestment Rate", [d["reinvestment_rate"]] * gp, "pct"),
        ("EBIT", d["ebit_n"], "dollar"),
        ("Tax Rate (cash flow)", [d["eff_tax_rate"]] * gp, "pct"),
        ("EBIT × (1 − tax rate)", d["ebiat_n"], "dollar"),
        (
            "− (CapEx − Depreciation)",
            [-r_v * capex_frac for r_v in d["reinv_n"]],
            "dollar",
        ),
        ("− Chg. Working Capital", [-r_v * wc_frac for r_v in d["reinv_n"]], "dollar"),
        ("Free Cash Flow to Firm", d["fcff_n"], "dollar"),
        ("Cost of Capital", [d["discount_rate"]] * gp, "pct"),
        (
            "Cumulated Cost of Capital",
            [(1 + d["discount_rate"]) ** (y + 1) for y in range(gp)],
            "num",
        ),
        (
            "Present Value",
            [d["fcff_n"][y] / (1 + d["discount_rate"]) ** (y + 1) for y in range(gp)],
            "dollar",
        ),
    ]

    for lbl, values, fmt in table_rows:
        label(r, 1, lbl)
        for i, v in enumerate(values):
            if fmt == "dollar":
                val_dollar(r, 2 + i, v)
            elif fmt == "pct":
                val_pct(r, 2 + i, v)
            else:
                c = ws.cell(row=r, column=2 + i, value=v)
                c.number_format = "0.0000"
        r += 1

    r += 1

    # ====================================================================
    # SECTION 5 — STABLE PHASE
    # ====================================================================
    section_header(r, 1, "Stable Phase")
    r += 1

    stable_rows = [
        ("Growth Rate in Stable Phase", d["stable_growth"], "pct"),
        ("Reinvestment Rate in Stable Phase", d["stable_reinv_rate"], "pct"),
        ("FCFF in Stable Phase", d["stable_fcff"], "dollar"),
        ("Cost of Equity in Stable Phase", d["stable_cost_of_equity"], "pct"),
        ("Cost of Capital in Stable Phase", d["stable_cost_of_capital"], "pct"),
        (
            "Value at End of Growth Phase (TV)",
            d["terminal_value_undiscounted"],
            "dollar",
        ),
        ("PV of Terminal Value", d["terminal_value_pv"], "dollar"),
    ]
    for lbl, v, fmt in stable_rows:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, YELLOW_FILL)
        else:
            val_pct(r, 2, v, YELLOW_FILL)
        r += 1

    r += 1

    # ====================================================================
    # SECTION 6 — VALUATION SUMMARY
    # ====================================================================
    section_header(r, 1, "Valuation")
    r += 1

    valuation_rows = [
        ("PV of FCFF in High Growth Phase", d["fcff_pv"], "dollar"),
        ("PV of Terminal Value of Firm", d["terminal_value_pv"], "dollar"),
        ("Value of Operating Assets", d["fcff_pv"] + d["terminal_value_pv"], "dollar"),
        ("+ Cash & Non-operating Assets", d["cash"], "dollar"),
        ("Value of Firm", d["enterprise_value"] + d["bv_debt"], "dollar"),
        ("− Market Value of Debt", d["bv_debt"], "dollar"),
        ("Market Value of Equity", d["enterprise_value"], "dollar"),
        ("÷ Shares Outstanding (000s)", d["shares_outstanding"], "num"),
        ("Value of Equity per Share", d["intrinsic_value"], "dollar"),
        ("Stock Price", d["price"], "dollar"),
        ("Margin of Safety ($)", d["margin_of_safety"], "dollar"),
        ("Margin of Safety (%)", d["margin_of_safety_pc"], "pct"),
    ]
    for lbl, v, fmt in valuation_rows:
        label(r, 1, lbl)
        fill = (
            GREEN_FILL
            if lbl
            in (
                "Value of Equity per Share",
                "Stock Price",
                "Margin of Safety ($)",
                "Margin of Safety (%)",
            )
            else None
        )
        if fmt == "dollar":
            val_dollar(r, 2, v, fill)
        elif fmt == "pct":
            val_pct(r, 2, v, fill)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "#,##0"
            if fill:
                c.fill = fill
        r += 1

    wb.save(output_path)
    print(f"Saved: {output_path}")


# ---------------------------------------------------------------------------
# XLSX report generation
# ---------------------------------------------------------------------------


def generate_summary_xlsx(
    valuations: list, output_path: str, index_label: str = "S&P 500"
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Valuation Summary"

    DARK_FILL = PatternFill("solid", fgColor="343A40")
    GREEN_FILL = PatternFill("solid", fgColor="D4EDDA")
    YELLOW_FILL = PatternFill("solid", fgColor="FFF3CD")
    RED_FILL = PatternFill("solid", fgColor="F8D7DA")
    HEADER_FONT = Font(bold=True, color="FFFFFF")
    THIN = Side(style="thin", color="CCCCCC")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    # Metadata rows
    today = date.today().strftime("%B %d, %Y")
    ws.cell(
        row=1, column=1, value=f"{index_label} FCFF Valuation — {today}"
    ).font = Font(bold=True, size=13)
    ws.cell(
        row=2,
        column=1,
        value=f"Risk-free rate: {RISK_FREE * 100:.2f}%  |  ERP: {EQ_PREM * 100:.2f}%  |  "
        f"Growth period: {GROWTH_PERIOD} yrs  |  Stocks valued: {len(valuations)}",
    ).font = Font(italic=True, color="666666")

    # Header row
    headers = [
        "Ticker",
        "Company",
        "Industry",
        "Price",
        "Intrinsic Value",
        "MoS ($)",
        "MoS (%)",
        "Growth Rate",
        "Cost of Capital",
        "Excess Return\n(ROIC-WACC)",
        "Unlevered Beta",
        "Market Cap ($B)",
    ]
    HDR_ROW = 4
    for col, hdr in enumerate(headers, 1):
        c = ws.cell(row=HDR_ROW, column=col, value=hdr)
        c.font = HEADER_FONT
        c.fill = DARK_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = BORDER

    ws.row_dimensions[HDR_ROW].height = 30

    # Data rows
    for row_idx, v in enumerate(valuations, HDR_ROW + 1):
        if v.margin_of_safety >= 1:
            row_fill = GREEN_FILL
        elif v.margin_of_safety >= 0:
            row_fill = YELLOW_FILL
        else:
            row_fill = RED_FILL

        row_data = [
            v.ticker,
            v.ent_name,
            v.industry,
            v.price,
            v.share_value,
            v.margin_of_safety,
            v.margin_of_safety_pc,
            v.growth_rate,
            v.cost_of_capital,
            v.wealth_pc,
            v.beta,
            v.market_cap / 1e9,
        ]
        num_fmts = [
            None,
            None,
            None,
            '"$"#,##0.00',
            '"$"#,##0.00',
            '"$"#,##0.00',
            "0.0%",
            "0.0%",
            "0.0%",
            "0.0%",
            "0.000",
            "#,##0.00",
        ]
        for col, (val, fmt) in enumerate(zip(row_data, num_fmts), 1):
            c = ws.cell(row=row_idx, column=col, value=val)
            c.fill = row_fill
            c.border = BORDER
            if fmt:
                c.number_format = fmt
            if col <= 3:
                c.alignment = Alignment(horizontal="left")
            else:
                c.alignment = Alignment(horizontal="right")

    # Column widths
    col_widths = [8, 28, 20, 10, 14, 10, 10, 12, 14, 16, 13, 14]
    for col, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = w

    # Freeze header
    ws.freeze_panes = ws.cell(row=HDR_ROW + 1, column=1)

    # ----------------------------------------------------------------
    # Tab 2 — Value Creators with positive MoS, sorted by Excess Return desc
    # ----------------------------------------------------------------
    creators = sorted(
        [v for v in valuations if v.wealth_pc > 0 and v.margin_of_safety > 0],
        key=lambda v: v.wealth_pc,
        reverse=True,
    )

    vc = wb.create_sheet(title="Value Creators")

    vc.cell(
        row=1,
        column=1,
        value=f"{index_label} — Value Creators & Undervalued Stocks — {today}",
    ).font = Font(bold=True, size=13)
    vc.cell(
        row=2,
        column=1,
        value=f"{len(creators)} of {len(valuations)} stocks (ROIC > WACC or MoS > $0) — sorted by Excess Return (ROIC − WACC) descending",
    ).font = Font(italic=True, color="666666")

    for col, hdr in enumerate(headers, 1):
        c = vc.cell(row=HDR_ROW, column=col, value=hdr)
        c.font = HEADER_FONT
        c.fill = DARK_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = BORDER

    vc.row_dimensions[HDR_ROW].height = 30

    for row_idx, v in enumerate(creators, HDR_ROW + 1):
        row_fill = (
            GREEN_FILL
            if v.margin_of_safety >= 1
            else (YELLOW_FILL if v.margin_of_safety >= 0 else RED_FILL)
        )
        row_data = [
            v.ticker,
            v.ent_name,
            v.industry,
            v.price,
            v.share_value,
            v.margin_of_safety,
            v.margin_of_safety_pc,
            v.growth_rate,
            v.cost_of_capital,
            v.wealth_pc,
            v.beta,
            v.market_cap / 1e9,
        ]
        for col, (val, fmt) in enumerate(zip(row_data, num_fmts), 1):
            c = vc.cell(row=row_idx, column=col, value=val)
            c.fill = row_fill
            c.border = BORDER
            if fmt:
                c.number_format = fmt
            c.alignment = Alignment(horizontal="left" if col <= 3 else "right")

    for col, w in enumerate(col_widths, 1):
        vc.column_dimensions[get_column_letter(col)].width = w

    vc.freeze_panes = vc.cell(row=HDR_ROW + 1, column=1)

    wb.save(output_path)
    print(f"Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    # Parse simple CLI args: --limit N  --growth N
    args = sys.argv[1:]
    limit = None
    growth_period = GROWTH_PERIOD

    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--growth" and i + 1 < len(args):
            growth_period = int(args[i + 1])
            i += 2
        else:
            i += 1

    tickers = []
    single_stock = False

    print("\nSelect index to value:")
    print("  1. S&P 500")
    print("  2. Russell 2000")
    print("  3. Single stock")
    while True:
        choice = input("Choice [1/2/3]: ").strip()
        if choice == "1":
            tickers = get_sp500_tickers()
            index_label = "sp500"
            break
        elif choice == "2":
            tickers = get_russell2000_tickers()
            index_label = "r2000"
            break
        elif choice == "3":
            ticker = input("Enter ticker symbol: ").strip().upper()
            index_label = ticker
            single_stock = True
            break
        else:
            print("Please enter 1, 2, or 3.")

    # ---- Single-stock path: Excel output --------------------------------
    if single_stock:
        output_file = (
            f"/Users/jhess/Development/Alpha2/data/outputs/"
            f"value_{index_label}_{date.today().strftime('%Y%m%d')}.xlsx"
        )
        detail = value_stock_detail(ticker, growth_period)
        if detail:
            generate_xlsx(detail, output_file)
        else:
            print(f"Valuation failed for {ticker}.")
        return

    # ---- Batch path: HTML output ----------------------------------------
    if limit:
        tickers = tickers[:limit]

    print(
        f"Valuing {len(tickers)} {index_label.upper()} stocks (growth period = {growth_period} years) ..."
    )

    # Optionally write to DB
    try:
        db_conn = sqlite3.connect("/Volumes/Financial_Data/valuation.db", timeout=30)
        db_conn.execute("PRAGMA journal_mode=WAL")
        create_table(db_conn)
    except Exception:
        db_conn = None
        logger.warning("Database unavailable; skipping DB writes.")

    valuations = []
    total = len(tickers)
    bar_width = 40
    start_time = time.time()
    for idx, ticker in enumerate(tickers, 1):
        result = value_stock(ticker, growth_period)
        if result:
            valuations.append(result)
            if db_conn:
                try:
                    insert_valuation(db_conn, result)
                except Exception as e:
                    logger.warning(f"DB insert failed for {ticker}: {e}")

        filled = int(bar_width * idx / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        elapsed = int(time.time() - start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        print(f"\r  {idx}/{total} [{bar}] {h:02d}:{m:02d}:{s:02d}", end="", flush=True)

    print()  # newline after progress bar

    if db_conn:
        db_conn.close()

    valuations.sort(key=lambda v: v.margin_of_safety, reverse=True)

    _index_display = {"sp500": "S&P 500", "r2000": "Russell 2000"}.get(
        index_label, index_label
    )
    output_file = f"/Users/jhess/Development/Alpha2/data/outputs/value_{index_label}_{date.today().strftime('%Y%m%d')}.xlsx"
    generate_summary_xlsx(valuations, output_file, _index_display)
    print(f"Done. {len(valuations)}/{len(tickers)} stocks valued successfully.")


if __name__ == "__main__":
    main()
