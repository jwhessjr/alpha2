"""
This library is a collection of functions used in the Hess Group DCF model.

"""

import os
import sys
import datetime
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
import random
import logging
from pathlib import Path

# Reference data directory — shared Damodaran tables used by all valuation functions.
# Eliminates per-call Excel reads; each DataFrame is loaded once per process.
# Deliberately separate from data/ (generated output, cleaned by Oscar nightly) so
# reference/source files can never be mistaken for stale duplicates and archived.
# When frozen (compiled binary in ~/HessGrp/), data lives next to the executable.
# When running from source, data lives in the dev tree.
if getattr(sys, "frozen", False):
    _DATA_DIR = Path(os.path.dirname(sys.executable)) / "reference_data"
else:
    _DATA_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent / "reference_data"

_indname_df: pd.DataFrame | None = None
_betas_df: pd.DataFrame | None = None
_default_spread_df: pd.DataFrame | None = None
_rd_amort_df: pd.DataFrame | None = None

# Damodaran annual data files — downloaded once per calendar year and cached locally.
# Filenames follow the pattern <name>_YYYY<ext> (e.g. indname_2025.xlsx, betas_2025.xls).
_DAMODARAN_SOURCES = {
    "indname": {
        "url": "https://pages.stern.nyu.edu/~adamodar/pc/datasets/indname.xlsx",
        "ext": ".xlsx",
    },
    "betas": {
        "url": "https://pages.stern.nyu.edu/~adamodar/pc/datasets/betas.xls",
        "ext": ".xls",
    },
}

_DAMODARAN_HEADERS = {"User-Agent": "hg-dcf-model/1.0 jhess2@gmail.com"}


def _get_damodaran_file(name: str) -> Path:
    """Return path to a cached Damodaran reference file, downloading a fresh
    copy for the current year if one does not already exist.  Falls back to
    the most recent cached year file, then to the legacy bare filename."""
    info = _DAMODARAN_SOURCES[name]
    ext = info["ext"]
    year = datetime.date.today().year
    year_file = _DATA_DIR / f"{name}_{year}{ext}"

    if not year_file.exists():
        try:
            resp = requests.get(info["url"], headers=_DAMODARAN_HEADERS, timeout=30)
            resp.raise_for_status()
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            year_file.write_bytes(resp.content)
            logging.getLogger(__name__).info(
                f"Downloaded {name} {year} data ({len(resp.content):,} bytes) → {year_file.name}"
            )
            return year_file
        except Exception as exc:
            logging.getLogger(__name__).warning(
                f"Could not download {name} from Damodaran ({exc}); looking for cached copy"
            )

    if year_file.exists():
        return year_file

    # Most recent year-stamped file (either extension)
    candidates = sorted(
        p for p in _DATA_DIR.glob(f"{name}_[0-9][0-9][0-9][0-9]*") if p.suffix in (".xls", ".xlsx")
    )
    if candidates:
        fallback = candidates[-1]
        logging.getLogger(__name__).warning(
            f"Using cached {fallback.name} for {name} (current-year download failed)"
        )
        return fallback

    # Legacy bare filename
    for legacy_ext in (".xlsx", ".xls"):
        legacy = _DATA_DIR / f"{name}{legacy_ext}"
        if legacy.exists():
            logging.getLogger(__name__).warning(f"Using legacy {legacy.name} for {name}")
            return legacy

    raise FileNotFoundError(
        f"No {name} data file found in {_DATA_DIR}. "
        f"Check network access to {info['url']}"
    )


def _get_indname() -> pd.DataFrame:
    """Cached loader for Damodaran's industry-name-by-exchange table, used by
    get_industry(). Downloaded/refreshed yearly via _get_damodaran_file()."""
    global _indname_df
    if _indname_df is None:
        path = _get_damodaran_file("indname")
        # Sheet was "US by Industry" in older files; current file uses "By industry"
        xl = pd.ExcelFile(path)
        sheet = next(
            (s for s in xl.sheet_names if "industry" in s.lower() and "by" in s.lower()),
            xl.sheet_names[0],
        )
        _indname_df = xl.parse(sheet)
    return _indname_df


def _get_betas() -> pd.DataFrame:
    """Cached loader for Damodaran's industry unlevered-beta / D-E-ratio table,
    used by get_beta() and get_industry_de(). Downloaded/refreshed yearly via
    _get_damodaran_file()."""
    global _betas_df
    if _betas_df is None:
        path = _get_damodaran_file("betas")
        _betas_df = pd.read_excel(
            path,
            sheet_name="Industry Averages",
            skiprows=9,
        )
    return _betas_df


def _get_default_spread() -> pd.DataFrame:
    """Cached loader for Damodaran's interest-coverage default-spread bucket
    table, used by get_default_spread(). Deliberately does NOT go through
    _get_damodaran_file()'s yearly download/fallback pipeline — this file is
    git-tracked and manually curated with no download URL/auto-refresh, unlike
    indname/betas (see docs/known_errors.md, reference_data/ .gitignore notes).
    """
    global _default_spread_df
    if _default_spread_df is None:
        _default_spread_df = pd.read_excel(_DATA_DIR / "defaultSpread.xlsx")
    return _default_spread_df


def _get_rd_amort() -> pd.DataFrame:
    """Cached loader for Damodaran's R&D amortization-period-by-industry table,
    used by get_rAndD_years(). Deliberately does NOT go through
    _get_damodaran_file()'s yearly download/fallback pipeline — same reasoning
    as _get_default_spread() above (git-tracked, manually curated, no
    auto-refresh)."""
    global _rd_amort_df
    if _rd_amort_df is None:
        _rd_amort_df = pd.read_excel(
            _DATA_DIR / "RD_Amortization.xlsx", sheet_name="Amort Years"
        )
    return _rd_amort_df

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # Set the overall logger level

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Create a stream handler for the console
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.WARNING)  # only show INFO and above on console
stream_handler.setFormatter(formatter)

# Resolve log directory relative to the executable (PyInstaller) or source file,
# and create it automatically if it does not exist.
if getattr(sys, "frozen", False):
    _log_base = os.path.dirname(sys.executable)
else:
    _log_base = os.path.dirname(os.path.abspath(__file__))
_log_dir = os.path.join(_log_base, "data")
os.makedirs(_log_dir, exist_ok=True)

# Create a FileHandler for the log file
file_handler = logging.FileHandler(os.path.join(_log_dir, "value.log"))
file_handler.setLevel(logging.DEBUG)  # log all messages to the file
file_handler.setFormatter(formatter)

# Add the handlers to the logger
logger.addHandler(stream_handler)
logger.addHandler(file_handler)

DELAY = 0.90  # 0.90 s base delay between calls — ~67 calls/min, safely under AV's 75/min limit
DELAY_JITTER = 0.40  # randomized 0-0.4s added on top of DELAY, so calls don't land on a
                     # perfectly periodic schedule that can repeatedly brush the same
                     # offset in AV's rolling 60-second rate-limit window
_RATE_LIMIT_BACKOFFS = [5, 15]  # seconds to wait before each retry of an AV in-band error


def _sleep_with_jitter():
    time.sleep(DELAY + random.uniform(0, DELAY_JITTER))


def _av_get(url: str) -> dict:
    """
    Fetch a single Alpha Vantage URL with:
      - 15-second timeout (avoids indefinite hangs)
      - Up to 3 attempts on network timeouts (5-second pause between retries)
      - Up to 3 attempts on AV's in-band rate-limit / error responses
        ('Note', 'Information', 'Error Message' keys in the JSON body), with
        backoff — AV's own "Error Message" text for these rejections literally
        says "Please retry", and AV support confirmed premium-tier rejections
        at our pacing are typically a rolling-60-second-window brush rather
        than a hard cap, so a short backoff often clears it.

    Raises RuntimeError only after retries on in-band errors are exhausted,
    or after network timeouts are exhausted, so callers can handle both
    consistently.
    """
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not data:
                # A genuinely successful AV response is never an empty JSON
                # object for any endpoint we call — this is another shape of
                # transient rate-limit rejection (confirmed 2026-07-17: 9
                # tickers hit a downstream KeyError on 'SharesOutstanding'
                # from get_quote() after OVERVIEW silently returned {}).
                raise RuntimeError("AV returned an empty response (likely transient rate-limit)")
            if "Note" in data:
                raise RuntimeError(f"AV rate-limit: {data['Note']}")
            if "Information" in data:
                raise RuntimeError(f"AV access/info: {data['Information']}")
            if "Error Message" in data:
                raise RuntimeError(f"AV error: {data['Error Message']}")
            return data
        except RuntimeError as e:
            if attempt < max_attempts:
                backoff = _RATE_LIMIT_BACKOFFS[attempt - 1]
                logger.warning(
                    f"AV in-band error on attempt {attempt}/{max_attempts} ({e}), "
                    f"retrying in {backoff}s..."
                )
                time.sleep(backoff)
            else:
                raise
        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                logger.warning(
                    f"Timeout on attempt {attempt}/{max_attempts}, retrying in 5 s..."
                )
                time.sleep(5)
            else:
                raise RuntimeError(f"Timed out after {max_attempts} attempts: {url}")


def safe_float(val):
    """Coerce val to a float, returning 0.0 for None/missing/unparseable input
    instead of raising — used throughout this file for AV JSON fields that are
    sometimes the string "None" or absent entirely."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# Read statements from Alpha Vantage


def get_jsonparsed_data(url):
    """Thin wrapper: jitter-sleep then delegate to _av_get(). Only ever called
    internally from get_quote() in this file — no external caller today."""
    _sleep_with_jitter()
    return _av_get(url)


# ---------------------------------------------------------------------------
# SEC EDGAR CIK lookup  (single HTTP fetch, then cached for the run)
# ---------------------------------------------------------------------------

_cik_map: dict[str, str] = {}  # ticker.upper() → zero-padded CIK string
_cik_map_loaded = False


def _load_cik_map() -> None:
    """Download the full SEC EDGAR ticker→CIK mapping (once per process)."""
    global _cik_map, _cik_map_loaded
    if _cik_map_loaded:
        return
    try:
        url = "https://www.sec.gov/files/company_tickers.json"
        resp = requests.get(url, headers=_DAMODARAN_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        for entry in data.values():
            ticker = str(entry.get("ticker", "")).upper()
            cik_str = str(entry.get("cik_str", "")).zfill(10)
            if ticker:
                _cik_map[ticker] = cik_str
        logger.info(f"Loaded {len(_cik_map)} CIK entries from SEC EDGAR")
    except Exception as e:
        logger.warning(f"Could not load SEC CIK map: {e}")
    _cik_map_loaded = True


def get_cik(ticker: str) -> str:
    """Return the zero-padded 10-digit CIK for *ticker*, or '' if not found."""
    _load_cik_map()
    return _cik_map.get(ticker.upper(), "")


# Function to get the income statement and extract the required fields


def _q_ebit(q: dict) -> float:
    """
    Return operating income (EBIT proxy) for one quarterly report row.

    AV's 'ebit' field = incomeBeforeTax + interestExpense, which inflates EBIT
    for companies with large non-operating income (investment gains, interest
    income on large cash piles). 'operatingIncome' is the correct field for FCFF.

    Fall back to 'ebit' when:
      - operatingIncome is absent or zero, OR
      - operatingIncome and ebit have opposite signs (AV data error indicator —
        e.g., AV NHC Q1 2026: operatingIncome = -$104M, ebit = +$45M, SEC = +$32M)
    """
    ebit_val = safe_float(q.get("ebit", 0) or 0)
    oi = q.get("operatingIncome")
    if oi not in (None, "None", ""):
        val = safe_float(oi)
        if val is not None and val != 0.0:
            # If signs differ, one of them is wrong — trust ebit as the lesser evil
            if ebit_val != 0 and (val > 0) != (ebit_val > 0):
                return ebit_val
            return val
    return ebit_val


def get_inc_stmnt(company: str, apiKey: str) -> dict:
    """Return annualized ebit, tax expense and interest expense
       from the quarterly reports of a ticker.

    The API returns up to 20 recent quarters; we aggregate them into at most five years.
    """
    _sleep_with_jitter()
    url = (
        f"https://www.alphavantage.co/query?"
        f"function=INCOME_STATEMENT&symbol={company}&apikey={apiKey}"
    )
    data = _av_get(url)

    # The API returns the most recent quarter first.
    quarterly_reports = data.get("quarterlyReports", [])

    if not quarterly_reports:
        raise ValueError(f"No quarterly reports found for {company}")

    # We’ll aggregate at most 5 years (20 quarters).
    max_quarters = min(len(quarterly_reports), 20)
    yearly_data = []

    for i in range(0, max_quarters, 4):  # step by four quarters
        quarter_block = quarterly_reports[i : i + 4]
        if len(quarter_block) < 4:
            break  # incomplete year at the end of the list

        ebit = sum(_q_ebit(q) for q in quarter_block)
        incomeBeforeTax = sum(safe_float(q["incomeBeforeTax"]) for q in quarter_block)
        tax_exp = sum(safe_float(q["incomeTaxExpense"]) for q in quarter_block)
        int_exp = sum(safe_float(q["interestExpense"]) for q in quarter_block)
        revenue = sum(safe_float(q["totalRevenue"]) for q in quarter_block)
        net_income = sum(safe_float(q["netIncome"]) for q in quarter_block)

        yearly_data.append(
            {
                "ebit": ebit,
                "incomeBeforeTax": incomeBeforeTax,
                "income_tax_expense": tax_exp,
                "interest_expense": int_exp,
                "totalRevenue": revenue,
                "netIncome": net_income,
            }
        )

    # Build the result dictionary with separate lists
    income_statement = {
        "ebit": [y["ebit"] for y in yearly_data],
        "incomeBeforeTax": [y["incomeBeforeTax"] for y in yearly_data],
        "income_tax_expense": [y["income_tax_expense"] for y in yearly_data],
        "interest_expense": [y["interest_expense"] for y in yearly_data],
        "totalRevenue": [y["totalRevenue"] for y in yearly_data],
        "netIncome": [y["netIncome"] for y in yearly_data],
    }

    # Detect single-quarter outlier driving a negative TTM EBIT.
    # If removing the worst quarter flips TTM positive, flag it so callers can
    # compute a normalized valuation alongside the GAAP result.
    ebit_anomaly = None
    if yearly_data and quarterly_reports:
        ttm_quarters = quarterly_reports[:4]
        q_ebits = [
            (q.get("fiscalDateEnding", ""), _q_ebit(q))
            for q in ttm_quarters
        ]
        ttm = sum(e for _, e in q_ebits)
        if ttm < 0:
            worst_idx = min(range(len(q_ebits)), key=lambda i: q_ebits[i][1])
            worst_date, worst_ebit = q_ebits[worst_idx]
            normalized_ttm = ttm - worst_ebit
            if normalized_ttm > 0:
                ebit_anomaly = {
                    "quarter_date": worst_date,
                    "quarter_ebit": worst_ebit,
                    "normalized_ttm_ebit": normalized_ttm,
                    "quarters": q_ebits,
                }
    income_statement["ebit_anomaly"] = ebit_anomaly

    return income_statement


_FINANCIAL_OR_REIT_KEYWORDS = {
    "bank", "banks", "financial services", "insurance", "reinsurance",
    "brokerage", "investment banking", "thrift", "savings", "credit",
    "mortgage", "asset management", "reit", "real estate investment trust",
}
_FINANCIAL_OR_REIT_PREFIXES = ("retail (reit", "r.e.i.t.")


def is_financial_or_reit_industry(industry: str) -> bool:
    """
    True for banks/insurers/brokerages/REITs and similar — mirrors
    av_fcff_2.py's is_financial_firm()/is_reit() keyword sets (kept as a
    separate, small, duplicated classifier here rather than importing the
    3000+-line av_fcff_2.py from this lower-level library — see
    docs/known_errors.md 2026-08-02 for why this classification matters for
    _q_cash_and_sti()).
    """
    low = industry.lower()
    return (
        any(kw in low for kw in _FINANCIAL_OR_REIT_KEYWORDS)
        or any(low.startswith(p) for p in _FINANCIAL_OR_REIT_PREFIXES)
    )


def _q_cash_and_sti(q: dict, is_financial_or_reit: bool = False) -> float:
    """
    Cash + short-term investments for one AV BALANCE_SHEET quarterly report.

    AV's own pre-aggregated "cashAndShortTermInvestments" field is not
    reliably the sum of its two components — confirmed for GOOG (2026-07-31):
    it returned a value identical to cashAndCashEquivalentsAtCarryingValue
    alone, silently dropping $186.6B of shortTermInvestments. That left
    short-term investments trapped inside "non-cash working capital",
    inflating calc_chng_wc()'s reinvestment estimate by roughly the same
    amount and pinning the reinvestment rate at its 100% cap. Same class of
    problem as AV's `ebit` field (see _q_ebit() in this same file) — prefer the
    granular components over AV's own pre-summed convenience field.

    2026-08-02: a 63-ticker empirical audit (docs/known_errors.md) found this
    "prefer the granular sum" behavior is correct for non-financial companies
    but wrong for financial firms/REITs — for those, AV's granular
    "shortTermInvestments" field is frequently either drawn from a
    non-current-qualified SEC concept representing the company's entire
    investment portfolio (not truly short-term — common for banks/insurers
    with unclassified balance sheets), or untraceable to any real SEC figure
    at all (~28% of a fresh financial/REIT sample, vs. 0% for non-financials).
    Pass is_financial_or_reit=True to trust AV's pre-tagged field instead.
    """
    if is_financial_or_reit:
        return safe_float(q.get("cashAndShortTermInvestments"))
    granular = safe_float(q.get("cashAndCashEquivalentsAtCarryingValue")) + safe_float(
        q.get("shortTermInvestments")
    )
    if granular > 0:
        return granular
    return safe_float(q.get("cashAndShortTermInvestments"))


def get_bal_sheet(company, apiKey, is_financial_or_reit: bool = False):
    """
    Fetch and snapshot (not summed) AV quarterly balance-sheet data for company.

    Unlike get_inc_stmnt()/get_cash_flow(), this takes point-in-time quarterly
    snapshots rather than annualizing/summing across quarters, since balance
    sheet figures are stock (as-of-date) values, not flow (period) values.

    is_financial_or_reit: pass True for banks/insurers/REITs — see
    _q_cash_and_sti()'s docstring for why the cash-field handling differs.

    Returns
    -------
    dict
        Keys include 'cash_and_equivalents', 'total_current_assets',
        'total_current_liabilities', 'current_long_debt', 'short_term_debt',
        'long_term_debt', 'total_stockholders_equity' — each a list of
        snapshot values across the quarters processed.

    Raises
    ------
    ValueError
        If no quarterly reports are returned for the ticker.
    """
    _sleep_with_jitter()
    url = (
        f"https://www.alphavantage.co/query?"
        f"function=BALANCE_SHEET&symbol={company}&apikey={apiKey}"
    )
    data = _av_get(url)

    quarterly_reports = data.get("quarterlyReports", [])
    if not quarterly_reports:
        logger.debug(
            f"Balance sheet raw response keys for {company}: {list(data.keys())}"
        )
        logger.debug(f"Balance sheet raw response: {data}")
        raise ValueError(f"No quarterly balance sheet reports found for {company}")

    # Sanity check: a raw AV cash figure that's wildly larger than the same
    # quarter's own total current assets, or that jumped implausibly from
    # the prior quarter, is a data ingestion error, not a real cash
    # position — cash is a subset of current assets by definition, so any
    # multiple above ~1x already indicates a problem. Confirmed live
    # 2026-08-20: GOLF (Acushnet) reported $67.9B cash vs. $1.27B total
    # current assets (53x) and a 1,314x jump from the prior quarter's
    # $51.7M; ZYME (Zymeworks) reported $179.4B cash, a 734x jump from the
    # prior quarter's $244M. Left unguarded, this silently produces a
    # share_value many multiples of a sane DCF result (see
    # docs/known_errors.md 2026-08-20) — the existing negative-invested-
    # capital gate (2026-08-10) doesn't reliably catch it, since a company
    # with a real moat rating can pass that gate's corroboration check even
    # when the underlying cash figure is garbage.
    latest_cash = _q_cash_and_sti(quarterly_reports[0], is_financial_or_reit)
    latest_tca = safe_float(quarterly_reports[0].get("totalCurrentAssets"))
    if latest_tca > 0 and latest_cash > 2 * latest_tca:
        raise ValueError(
            f"{company}: reported cash ({latest_cash:,.0f}) exceeds 2x total "
            f"current assets ({latest_tca:,.0f}) for the latest quarter — "
            f"likely an AV data ingestion error, not a real cash position."
        )
    if len(quarterly_reports) > 1:
        prior_cash = _q_cash_and_sti(quarterly_reports[1], is_financial_or_reit)
        if prior_cash > 0 and latest_cash > 20 * prior_cash:
            raise ValueError(
                f"{company}: reported cash ({latest_cash:,.0f}) is more than "
                f"20x the prior quarter's cash ({prior_cash:,.0f}) — likely "
                f"an AV data ingestion error, not a real cash position."
            )

    # Balance sheet is point-in-time, so we take one snapshot per year
    # (the last quarter of each annual block) rather than summing.
    # Step through in blocks of 4, take the first quarter of each block
    # (most recent quarter of that fiscal year).
    max_quarters = min(len(quarterly_reports), 20)

    cash_and_equivalents = []
    total_current_assets = []
    total_current_liabilities = []
    short_term_debt = []
    long_term_debt = []
    total_stockholders_equity = []

    for i in range(0, max_quarters, 4):
        block = quarterly_reports[i : i + 4]
        if len(block) < 4:
            break  # incomplete year, skip

        q = block[0]  # most recent quarter of this annual period
        cash_and_equivalents.append(_q_cash_and_sti(q, is_financial_or_reit))
        total_current_assets.append(safe_float(q["totalCurrentAssets"]))
        total_current_liabilities.append(safe_float(q["totalCurrentLiabilities"]))
        short_term_debt.append(safe_float(q["shortTermDebt"]))
        long_term_debt.append(safe_float(q["longTermDebt"]))
        total_stockholders_equity.append(safe_float(q["totalShareholderEquity"]))

    if not cash_and_equivalents:
        raise ValueError(
            f"Insufficient quarterly data to build balance sheet for {company}"
        )

    return {
        "cash_and_equivalents": cash_and_equivalents,
        "total_current_assets": total_current_assets,
        "total_current_liabilities": total_current_liabilities,
        "short_term_debt": short_term_debt,
        "long_term_debt": long_term_debt,
        "total_stockholders_equity": total_stockholders_equity,
    }


# Function to get the cash flow statement and extract the required fields


def get_cash_flow(company: str, apiKey: str) -> dict:
    """
    Return annualized depreciation and cap‑ex from the quarterly cash‑flow data.

    Parameters
    ----------
    company : str
        Ticker symbol.
    apiKey : str
        AlphaVantage API key.

    Returns
    -------
    dict
        Keys: 'depreciation', 'capex' (each a list of up to 5 yearly values).
    """
    _sleep_with_jitter()
    url = (
        f"https://www.alphavantage.co/query?"
        f"function=CASH_FLOW&symbol={company}&apikey={apiKey}"
    )
    data = _av_get(url)

    quarterly_reports = data.get("quarterlyReports", [])
    if not quarterly_reports:
        raise ValueError(f"No quarterly cash‑flow reports found for {company}")

    # We’ll aggregate at most 5 years (20 quarters).
    max_quarters = min(len(quarterly_reports), 20)

    depreciation = []
    capex = []
    dividends_paid = []

    # Step through the list in blocks of four quarters.
    for i in range(0, max_quarters, 4):
        block = quarterly_reports[i : i + 4]
        if len(block) < 4:
            break  # incomplete year at the end

        yearly_capex = sum(safe_float(q["capitalExpenditures"]) for q in block)
        yearly_depr = sum(
            safe_float(q["depreciationDepletionAndAmortization"]) for q in block
        )
        yearly_divs = sum(safe_float(q["dividendPayout"]) for q in block)

        capex.append(yearly_capex)
        depreciation.append(yearly_depr)
        dividends_paid.append(yearly_divs)

    return {
        "capex": capex,
        "depreciation": depreciation,
        "dividends_paid": dividends_paid,
    }


def get_rAndD(company, rd_years, apiKey):
    """
    Fetches R&D expenses for a specified number of years from Alpha Vantage.

    Args:
        company (str): The company symbol.
        rd_years (int): The number of years to fetch R&D data for.
        apiKey (str): The Alpha Vantage API key.

    Returns:
        tuple[dict, int]: (dict with a list of yearly R&D expenses, number of
        years actually processed) — note the non-standard 2-tuple return, unlike
        most other fetchers in this file which return a plain dict.
    """
    _sleep_with_jitter()
    url = f"https://www.alphavantage.co/query?function=INCOME_STATEMENT&symbol={company}&apikey={apiKey}"

    rd_table = {}
    data = _av_get(url)
    rdExpense = data.get("quarterlyReports", [])

    if not rdExpense:
        logger.debug("No quarterly reports found.")
        return {"research_and_development": []}, 0
    rd_Amount = []

    # We need to process quarters in chunks of 4 for each year.
    # The number of available years is the length of the list divided by 4.
    num_available_years = len(rdExpense) // 4
    years_to_process = min(rd_years, num_available_years)

    for i in range(years_to_process):
        start_index = i * 4
        end_index = start_index + 4

        # Get the slice of the list for the current year's quarters
        quarters = rdExpense[start_index:end_index]

        # Calculate the sum of R&D expenses for the year
        yearRDExpense = 0.0
        for quarter in quarters:
            try:
                # Use .get() with a default value to prevent KeyError
                rd_val = safe_float(quarter.get("researchAndDevelopment", "0"))
                yearRDExpense += rd_val
            except ValueError:
                # If safe_float fails, just add 0 and continue.
                pass

        rd_Amount.append(yearRDExpense)

    rd_table["research_and_development"] = rd_Amount
    rdTable = rd_table, years_to_process
    return rdTable


# Function to get the current share price, shares outstanding, and market cap


def get_quote(company, apiKey):
    """
    Fetch current price + company overview data (GLOBAL_QUOTE + OVERVIEW).

    Returns
    -------
    tuple
        Non-standard bare positional 6-tuple (not a dict, unlike most other
        fetchers in this file): (price, sharesOutstanding, marketCap,
        company_name, dividend_yield, analyst_count). Callers must unpack in
        exactly this order.

    Raises
    ------
    RuntimeError
        If the OVERVIEW response is missing SharesOutstanding/
        MarketCapitalization/Name — usually means no AV coverage for the symbol.
    """
    # ADD exchange to this extract and add it to the database
    url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={company}&apikey={apiKey}"
    data = get_jsonparsed_data(url)
    data = data.get("Global Quote", [])
    # print(data)
    price = safe_float(data["05. price"])
    url = f"https://www.alphavantage.co/query?function=OVERVIEW&symbol={company}&apikey={apiKey}"
    data = get_jsonparsed_data(url)
    # print(data)
    if "SharesOutstanding" not in data or "MarketCapitalization" not in data or "Name" not in data:
        raise RuntimeError(
            f"AV OVERVIEW response for {company} is missing required fields "
            f"(SharesOutstanding/MarketCapitalization/Name) — likely no AV coverage for this symbol"
        )
    sharesOutstanding = safe_float(data["SharesOutstanding"])
    marketCap = safe_float(data["MarketCapitalization"])
    company_name = data["Name"]
    dividend_yield = safe_float(data.get("DividendYield", 0))
    analyst_count = int(
        safe_float(data.get("AnalystRatingStrongBuy", 0))
        + safe_float(data.get("AnalystRatingBuy", 0))
        + safe_float(data.get("AnalystRatingHold", 0))
        + safe_float(data.get("AnalystRatingSell", 0))
        + safe_float(data.get("AnalystRatingStrongSell", 0))
    )
    entQuote = price, sharesOutstanding, marketCap, company_name, dividend_yield, analyst_count
    return entQuote


def _get_with_retry(url: str, params: dict | None = None, max_attempts: int = 3, timeout: int = 15) -> "requests.Response":
    """
    Shared timeout+retry wrapper for get_erp()/get_risk_free()'s network
    calls — mirrors _av_get()'s network-retry behavior (15s timeout, up to
    3 attempts, 5s pause between) but without AV's in-band rate-limit-response
    handling, which doesn't apply to these two non-AV endpoints (Damodaran's
    ERP page, FRED's API). Added because both functions previously made a
    bare, unbounded requests.get() call — a hang here stalls
    refresh_market_data() (and therefore the whole nightly batch, which calls
    it once at the very start) indefinitely, with nothing to catch it.

    Raises requests.exceptions.Timeout after retries are exhausted. Does not
    inspect status codes or body content — callers keep their own post-fetch
    handling exactly as before.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return requests.get(url, params=params, timeout=timeout)
        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                logger.warning(
                    f"Timeout on attempt {attempt}/{max_attempts} for {url}, retrying in 5s..."
                )
                time.sleep(5)
            else:
                raise


def get_erp():
    # URL of the page
    url = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/home.htm"  # Replace with the correct full URL if deeper than homepage

    # Fetch the page
    response = _get_with_retry(url)
    response.raise_for_status()  # Raises an error if the request failed

    # Parse the HTML
    soup = BeautifulSoup(response.text, "html.parser")

    # Find the paragraph containing the ERP info
    paragraphs = soup.find_all("p")
    for p in paragraphs:
        if "Implied ERP" in p.get_text():
            text = p.get_text()
            break
    else:
        raise ValueError("Couldn't find the paragraph with Implied ERP")

    # Use regex to extract the first percentage value
    match = re.search(r"(\d+\.\d+)%", text)
    if match:
        implied_erp = safe_float(match.group(1)) / 100
        # print(f"Implied ERP: {implied_erp}%")
        logger.info(f"Implied ERP {implied_erp}")
        return implied_erp
    else:
        # print("Couldn't extract Implied ERP value")
        logger.debug("Couldn't extract ERP %s")


def get_risk_free(FRED_KEY):
    """
    Fetch the latest 10-year Treasury yield (FRED series GS10) as a decimal.

    Returns None (does not raise) on a non-200 HTTP response — unlike most
    other fetchers in this file, which raise on failure. Callers must check
    for None and apply their own fallback.
    """
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "GS10",
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    # Fetch data
    try:
        response = _get_with_retry(url, params=params)
    except requests.exceptions.Timeout:
        logger.debug(f"Timed out fetching risk-free rate after retries: {url}")
        return None

    if response.status_code != 200:
        logger.debug(f"Error: Received status code {response.status_code}")
        return None

    # Parse JSON response
    data = response.json()
    RISK_FREE = safe_float(data["observations"][0]["value"]) / 100
    logger.info(f"Risk Free Rate {RISK_FREE}")
    return RISK_FREE


_US_EXCHANGES = {
    "NYSE",
    "NasdaqGS",
    "NasdaqGM",
    "NasdaqCM",
    "NasdaqNM",
    "NYSEMKT",
    "NYSEARCA",
    "BATS",
}


def get_industry(company):
    """
    Look up company's Damodaran industry group from the indname table.

    Prefers a US-exchange match (_US_EXCHANGES) and stops immediately once
    found; a non-US match is kept as a fallback but the scan continues in
    case a US match appears later in the table.

    Raises
    ------
    ValueError
        If no matching row is found for the ticker at all.
    """
    indName = _get_indname()

    industry = None
    for index, row in indName.iterrows():
        try:
            parts = row["Exchange:Ticker"].split(":")
            if len(parts) < 2 or parts[1] != company:
                continue
            matched_industry = row["Industry Group"]
            exchange = parts[0]
            if exchange in _US_EXCHANGES:
                # US exchange match — use it immediately and stop
                industry = matched_industry
                logger.info(f"Industry Group {industry}")
                break
            else:
                # Non-US match — keep as fallback but continue looking
                industry = matched_industry
                logger.info(f"Industry Group {industry}")
        except (TypeError, AttributeError):
            continue
        except Exception as e:
            logger.debug(f"Error reading industry {e}")

    if industry is None:
        raise ValueError(f"Industry not found for {company}")
    return industry


def _lookup_beta_row(industry):
    """Shared row-scan for get_beta()/get_industry_de(): substring match
    against "Industry Name" in Damodaran's industry-averages table, first
    match wins per row order. Returns None if no row matches (or the table
    isn't scannable), leaving the market-average default to each caller."""
    beta = _get_betas()

    for index, row in beta.iterrows():
        try:
            if industry in row["Industry Name"]:
                return row
        except TypeError:
            continue
    return None


def get_beta(industry):
    """
    Look up industry's unlevered beta from Damodaran's industry-averages table.

    Substring match against "Industry Name" (first match wins, per row order).
    Defaults to 1.0 (does not raise) if industry isn't found — a market-average
    assumption, unlike get_industry()'s raise-on-not-found behavior.
    """
    row = _lookup_beta_row(industry)
    unleveredBeta = row["Unlevered beta corrected for cash"] if row is not None else 1.0

    logger.info(f"Beta {unleveredBeta}")
    return unleveredBeta


def get_industry_de(industry):
    """
    Industry-average D/E ratio from the same "Industry Averages" sheet get_beta()
    already reads — used to cap (never raise) a company's stable-phase D/E in
    calc_levered_beta(). See docs/decisions.md "Stable-phase capital structure"
    for why this only ever caps down, not up.
    """
    row = _lookup_beta_row(industry)
    industryDE = row["D/E Ratio"] if row is not None else 1.0

    logger.info(f"Industry D/E {industryDE}")
    return industryDE


def get_default_spread(intCover):
    """
    Look up Damodaran's default-spread bucket for an interest coverage ratio.

    The source table has deliberate tiny gaps between buckets (e.g. LT=7.499999
    for one row, GT=7.50 for the next) so adjacent ranges never double-match.
    A ratio landing exactly on one of these round boundaries (7.5, 2.5, 2.0,
    etc. — not rare; confirmed for CBT/CNK/EL 2026-07-17) falls in the gap and
    previously returned None with no fallback, crashing calc_discount_rate()
    downstream ("unsupported operand type(s) for +: 'float' and 'NoneType'").
    """
    defaultSpread = _get_default_spread()

    for index in defaultSpread.index:
        if (
            intCover > defaultSpread["GT"][index]
            and intCover < defaultSpread["LT"][index]
        ):
            return defaultSpread["Spread"][index]

    # Below the lowest bucket or above the highest — clamp to the extreme rating.
    min_gt_idx = defaultSpread["GT"].idxmin()
    max_lt_idx = defaultSpread["LT"].idxmax()
    if intCover <= defaultSpread["GT"][min_gt_idx]:
        return defaultSpread["Spread"][min_gt_idx]
    if intCover >= defaultSpread["LT"][max_lt_idx]:
        return defaultSpread["Spread"][max_lt_idx]

    # Landed exactly in a gap between two buckets — match inclusively.
    for index in defaultSpread.index:
        if (
            intCover >= defaultSpread["GT"][index]
            and intCover <= defaultSpread["LT"][index]
        ):
            return defaultSpread["Spread"][index]

    # Should be unreachable given the clamping above — never return None.
    logger.warning(
        f"get_default_spread: intCover={intCover} matched no bucket even after "
        f"clamping — using worst-case (D2/D) spread"
    )
    return defaultSpread["Spread"][min_gt_idx]


def get_rAndD_years(industry):
    amortYears = _get_rd_amort()

    rAndD_years = 0  # default: no R&D amortization if industry not found
    for index, row in amortYears.iterrows():
        try:
            if industry == row["Industry"]:
                rAndD_years = row["Years"]
                logger.info(f"Years = {rAndD_years}")
                break
        except (TypeError, AttributeError):
            continue

    return rAndD_years
