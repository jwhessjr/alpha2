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


_SIC_CACHE: dict[str, int | None] = {}


def get_sic(ticker: str, api_key: str) -> int | None:
    """
    Return the SEC-assigned SIC code for ticker via Intrinio's
    companies/{ticker} endpoint, or None if unavailable (no coverage, API
    failure, etc.) -- callers must degrade gracefully, never raise.

    Cached per ticker for the process lifetime -- this is a static
    regulatory classification (confirmed 2026-09-01 to match
    data.sec.gov/submissions exactly, e.g. PYPL=7389, FOA=6162), not
    something that needs re-fetching within a single run.

    Used as a cheap, SEC-sourced pre-filter for financial-firm valuation
    routing (see is_financial_sic() in av_fcff_2.py) -- deliberately NOT
    used for beta/R&D-years/industry-D&E lookups, which stay on
    Damodaran's own industry classification exactly as before. See
    docs/known_errors.md 2026-09-01.
    """
    if ticker in _SIC_CACHE:
        return _SIC_CACHE[ticker]
    sic = None
    try:
        data = _intrinio_get(f"companies/{ticker}", api_key)
        raw = data.get("sic")
        if raw is not None:
            sic = int(raw)
    except Exception as e:
        logger.warning(f"{ticker}: SIC fetch failed ({e}) -- degrading to None")
    _SIC_CACHE[ticker] = sic
    return sic


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


# ═══════════════════════════════════════════════════════════════════════════
# Intrinio-backed equivalents (2026-08-24, AV→Intrinio migration Phase 1)
#
# Each function below returns EXACTLY the same shape as its AV counterpart
# above (including get_rAndD's/get_quote's non-standard tuple returns), so
# every existing downstream caller works unchanged once a call site switches
# from get_inc_stmnt() to get_inc_stmnt_intrinio() (etc.) — see
# docs/decisions.md for the migration plan this implements.
#
# Deliberately NOT wired into any call site yet. Phase 2 (shadow-mode
# validation) calls both AV and Intrinio versions side-by-side against a
# disposable DB before Phase 3 flips the production default.
# ═══════════════════════════════════════════════════════════════════════════

_INTRINIO_BASE_URL = "https://api-v2.intrinio.com"


def _intrinio_get(path: str, api_key: str, **params) -> dict:
    """
    Fetch a single Intrinio URL with 15s timeout, up to 3 retries on network
    timeout. Mirrors _av_get()'s shape but Intrinio's error envelope is a
    flat {"error": ..., "message": ...} dict, not AV's Note/Information/Error
    Message keys — and Intrinio has no documented in-band rate-limit
    rejection to retry-with-backoff the way AV does (2,000 calls/min, 3
    sockets, per Jim 2026-08-20 — far more headroom than AV's premium tier).
    """
    params["api_key"] = api_key
    url = f"{_INTRINIO_BASE_URL}/{path.lstrip('/')}"
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "error" in data:
                raise RuntimeError(
                    f"Intrinio error on {path}: {data.get('error')} — "
                    f"{str(data.get('message', ''))[:120]}"
                )
            return data
        except RuntimeError:
            raise
        except requests.exceptions.Timeout:
            if attempt < max_attempts:
                time.sleep(5)
            else:
                raise RuntimeError(f"Intrinio timeout after {max_attempts} attempts on {path}")
        except requests.RequestException as exc:
            raise RuntimeError(f"Intrinio network error on {path}: {exc}")


def _intrinio_periods(company: str, statement_code: str, api_key: str, n: int = 20, period_type: str = "QTR") -> list[dict]:
    """
    Most-recent-first fundamental periods for company/statement_code, each
    {"id": ..., "fiscal_year": ..., "fiscal_period": ...}. Returns [] (does
    NOT raise) on no coverage — see _intrinio_period_ids()'s docstring for
    which callers treat that as fatal. This single discovery call is cheap
    regardless of n (one API call either way) — n only bounds how many
    period entries are returned in that one response, not the call count.

    period_type="FY" (added 2026-08-26) requests true fiscal-year periods --
    real, as-filed annual figures, not a synthetic 4-quarter aggregation.
    Confirmed live (AAPL): Intrinio's API accepts type="FY" directly and
    returns up to 10+ real annual periods in one discovery call, the same
    semantic as Alpha Vantage's annualReports array. Needed for moat_score.py/
    lynch_score.py/growth_screen_2.py, which all want multi-year ANNUAL
    history (moat_score.py specifically wants up to 10 years) -- the existing
    type="QTR" callers (get_inc_stmnt_intrinio() and friends) only ever
    aggregate a handful of quarters into an implicit TTM/short lookback for
    av_fcff_2.py's own needs, a different use case.
    """
    data = _intrinio_get(
        f"companies/{company}/fundamentals",
        api_key,
        statement_code=statement_code,
        type=period_type,
    )
    fundamentals = data.get("fundamentals", [])
    return [
        {"id": f["id"], "fiscal_year": f.get("fiscal_year"), "fiscal_period": f.get("fiscal_period")}
        for f in fundamentals[:n]
    ]


def _intrinio_period_ids(company: str, statement_code: str, api_key: str, n: int = 20, period_type: str = "QTR") -> list[str]:
    """
    Most-recent-first fundamental period IDs for company/statement_code.
    Thin wrapper over _intrinio_periods() for callers that only need bare
    IDs. Returns [] (does NOT raise) on no coverage — callers differ on
    whether that should be a hard failure (get_inc_stmnt_intrinio/
    get_bal_sheet_intrinio/get_cash_flow_intrinio all raise, matching their
    AV equivalents) or a graceful empty result (get_rAndD_intrinio, also
    matching its AV equivalent) — see each function's own empty-check for
    which applies.
    """
    return [p["id"] for p in _intrinio_periods(company, statement_code, api_key, n, period_type=period_type)]


def _intrinio_standardized(fundamental_id: str, api_key: str) -> dict:
    """Flattened {tag: value} for one period's standardized view."""
    data = _intrinio_get(f"fundamentals/{fundamental_id}/standardized_financials", api_key)
    out = {}
    for item in data.get("standardized_financials", []):
        tag = (item.get("data_tag") or {}).get("tag")
        if tag is not None:
            out[tag] = item.get("value")
    return out


def _intrinio_quarter_interest_expense(q: dict) -> float:
    """Prefer Intrinio's dedicated gross interest-expense tag when the filer
    reports one separately; fall back to the net combined interest-income
    tag (sign-flipped) only when the dedicated tag is absent.

    Real bug found 2026-08-25 (see docs/known_errors.md): get_inc_stmnt_intrinio()
    previously read ONLY totalinterestincome, sign-flipped. That's correct for
    filers that report one combined net interest line (e.g. AZO), but wrong
    for filers that report gross interest expense as its own tag -- confirmed
    live: VZ ($1,985,000,000), GOGO ($17,987,000), IT ($22,266,000), AOS
    ($8,100,000), ETD ($55,000) all have a real, separately-populated
    totalinterestexpense tag that this function was silently never reading,
    dropping straight to $0 on every one of them. totalinterestexpense is
    already expense-positive -- no sign flip needed when present.

    When NEITHER tag is present (confirmed live: APA, PYPL) -- a genuine
    filer-presentation gap, not a vendor-fixable data bug (verified directly
    against PYPL's real 10-Q: no interest line at all, folded into "Other
    income (expense), net") -- this correctly falls through to 0.0, same as
    before.
    """
    raw_expense = q.get("totalinterestexpense")
    if raw_expense is not None:
        return safe_float(raw_expense)
    return -safe_float(q.get("totalinterestincome"))


def get_inc_stmnt_intrinio(company: str, apiKey: str, is_financial_or_reit: bool = False) -> dict:
    """
    Intrinio-backed equivalent of get_inc_stmnt() — same return shape,
    same 5-year-annualized-from-quarters aggregation, same single-quarter
    EBIT anomaly detection.

    Field map (confirmed live 2026-08-24, see docs/decisions.md):
      ebit               -> totaloperatingincome (Intrinio's clean EBIT
                             proxy, same choice AV's operatingIncome makes
                             per this project's own known pitfall re: AV's
                             raw 'ebit' field being inflated)
      incomeBeforeTax     -> totalpretaxincome
      income_tax_expense  -> incometaxexpense
      interest_expense    -> totalinterestexpense when present (already
                             expense-positive), else -totalinterestincome
                             (NET, sign-flipped) as a fallback for filers that
                             only report one combined net interest line. See
                             _intrinio_quarter_interest_expense() docstring
                             for the 2026-08-25 bug this fixes.
      totalRevenue        -> totalrevenue
      netIncome            -> netincome

    is_financial_or_reit: when False (the FCFF/non-bank caller), a response
    that uses the bank/financial-institution standardized template
    ('totalinterestincome' present, 'totaloperatingincome' absent as a KEY,
    not merely zero) raises instead of silently returning ebit=0 for every
    quarter. Found live 2026-08-26: GRBK (Green Brick Partners, a
    homebuilder) gets Intrinio's bank template despite genuinely not being a
    bank -- Intrinio's own internal industry classification appears wrong for
    this ticker specifically. AV's data is correct (real EBIT ~$400M/quarter,
    not 0) but was never attempted, since a "successful" (non-raising)
    Intrinio call never triggers _fetch_with_fallback()'s AV fallback. When
    is_financial_or_reit=True (a real bank/REIT caller, e.g. value_bank_stock()),
    the bank template is the CORRECT, expected shape -- this check is skipped.
    """
    # Fetch only 3 years (12 quarters), not 5 (20) -- confirmed via direct
    # grep of av_fcff_2.py (2026-08-25) that no caller ever reads beyond
    # ebit[:WEALTH_GATE_MIN_YEARS] (=3, the capital-light-compounder
    # durability gate); every other field only reads index [0]. Cuts 8
    # wasted standardized_financials calls per income-statement fetch.
    period_ids = _intrinio_period_ids(company, "income_statement", apiKey, n=12)
    if not period_ids:
        raise ValueError(f"No quarterly reports found for {company} on Intrinio")
    quarters = [_intrinio_standardized(pid, apiKey) for pid in period_ids]

    if not is_financial_or_reit and quarters:
        if "totaloperatingincome" not in quarters[0] and "totalinterestincome" in quarters[0]:
            raise ValueError(
                f"{company}: Intrinio returned the bank/financial-institution "
                f"template (totalinterestincome present, totaloperatingincome "
                f"absent) for a non-financial ticker -- likely an Intrinio-side "
                f"industry misclassification, not a real zero-EBIT company."
            )

    max_quarters = min(len(quarters), 12)
    yearly_data = []
    for i in range(0, max_quarters, 4):
        block = quarters[i : i + 4]
        if len(block) < 4:
            break
        yearly_data.append(
            {
                "ebit": sum(safe_float(q.get("totaloperatingincome")) for q in block),
                "incomeBeforeTax": sum(safe_float(q.get("totalpretaxincome")) for q in block),
                "income_tax_expense": sum(safe_float(q.get("incometaxexpense")) for q in block),
                "interest_expense": sum(_intrinio_quarter_interest_expense(q) for q in block),
                "totalRevenue": sum(safe_float(q.get("totalrevenue")) for q in block),
                "netIncome": sum(safe_float(q.get("netincome")) for q in block),
            }
        )

    income_statement = {
        "ebit": [y["ebit"] for y in yearly_data],
        "incomeBeforeTax": [y["incomeBeforeTax"] for y in yearly_data],
        "income_tax_expense": [y["income_tax_expense"] for y in yearly_data],
        "interest_expense": [y["interest_expense"] for y in yearly_data],
        "totalRevenue": [y["totalRevenue"] for y in yearly_data],
        "netIncome": [y["netIncome"] for y in yearly_data],
    }

    ebit_anomaly = None
    if yearly_data and quarters:
        ttm_quarters = quarters[:4]
        q_ebits = [safe_float(q.get("totaloperatingincome")) for q in ttm_quarters]
        ttm = sum(q_ebits)
        if ttm < 0 and q_ebits:
            worst_idx = min(range(len(q_ebits)), key=lambda i: q_ebits[i])
            worst_ebit = q_ebits[worst_idx]
            normalized_ttm = ttm - worst_ebit
            if normalized_ttm > 0:
                ebit_anomaly = {
                    "quarter_date": None,  # Intrinio period date not fetched in this pass
                    "quarter_ebit": worst_ebit,
                    "normalized_ttm_ebit": normalized_ttm,
                    "quarters": list(enumerate(q_ebits)),
                }
    income_statement["ebit_anomaly"] = ebit_anomaly

    return income_statement


def _intrinio_reported_tags(fundamental_id: str, api_key: str) -> dict:
    """Flattened {tag: value} for one period's as-reported (raw XBRL) view,
    consolidated-total values only (dimensions=None — excludes segment/
    product breakdowns, e.g. VZ's Revenues also appears split by
    ServiceAndOtherMember/ProductMember dimensions under the same tag name;
    only the dimensions=None entry is the real consolidated total).

    Some tags carry a genuine duplicate dimensions=None entry (observed:
    PYPL's ProfitLoss appeared twice, once real, once a stray 0.0) — prefer
    the non-zero value per tag rather than first-seen, to avoid a stray
    zero silently overwriting a real figure depending on API response order.
    """
    data = _intrinio_get(f"fundamentals/{fundamental_id}/reported_financials", api_key)
    out: dict[str, float] = {}
    for item in data.get("reported_financials", []):
        if item.get("dimensions"):
            continue  # segment/product breakdown, not the consolidated total
        tag = (item.get("xbrl_tag") or {}).get("tag")
        if tag is None:
            continue
        val = safe_float(item.get("value"))
        if tag not in out or (out[tag] == 0.0 and val != 0.0):
            out[tag] = val
    return out


def _intrinio_lease_debt_addback(
    fundamental_id: str, api_key: str, standardized_longtermdebt: float
) -> tuple[float, float]:
    """
    Returns (current_addback, noncurrent_addback) — lease-liability amounts
    to add to short_term_debt/long_term_debt so bv_debt reflects ALL
    interest-bearing debt, not just bank/bond debt. Decided 2026-08-25 (see
    docs/decisions.md): bv_debt should include all interest-bearing debt;
    working capital should exclude it. Since working capital's formula
    already subtracts short_term_debt from total_current_liabilities, fixing
    short_term_debt/long_term_debt here fixes both at once — no changes
    needed elsewhere.

    Operating lease liability is ALWAYS added. Confirmed on every filer
    checked (AAPL, DAL, and every ticker in the field-map investigation) that
    it is never folded into a filer's own reported debt line — it always
    sits in the generic "other liabilities" bucket instead, regardless of
    whether the filer breaks lease out as its own balance-sheet caption
    (DAL) or nets everything into "Other" (AAPL). Unambiguous, no detection
    needed.

    Finance lease liability is CONDITIONALLY added. Some filers (DAL, UPS
    confirmed) already bundle it into their own combined "debt and finance
    leases" line, which flows straight into Intrinio's standardized
    longtermdebt tag — adding it again would double-count. Detected by
    comparing the as-reported combined tag
    (LongTermDebtAndCapitalLeaseObligations) against the standardized
    longtermdebt value: a close match means finance lease is already bundled
    in. CONSERVATIVE FALLBACK, deliberate: if this can't be determined
    cleanly (combined tag absent, or no close match), do NOT add finance
    lease — operating lease is the dominant term for nearly every real
    company (e.g. AAPL: $10.9B operating vs $0.7B finance liability), so a
    missed finance-lease addback is a much smaller residual error than a
    double-counted one. This is the intentional backout-friendly bias in
    this function: when uncertain, under-add rather than over-add.
    """
    tags = _intrinio_reported_tags(fundamental_id, api_key)

    op_current = tags.get("OperatingLeaseLiabilityCurrent", 0.0)
    op_noncurrent = tags.get("OperatingLeaseLiabilityNoncurrent", 0.0)

    fin_current = 0.0
    fin_noncurrent = 0.0
    combined_noncurrent = tags.get("LongTermDebtAndCapitalLeaseObligations")
    if combined_noncurrent is not None and standardized_longtermdebt > 0:
        already_bundled = (
            abs(combined_noncurrent - standardized_longtermdebt)
            < max(1.0, standardized_longtermdebt * 0.01)
        )
        if not already_bundled:
            fin_current = tags.get("FinanceLeaseLiabilityCurrent", 0.0)
            fin_noncurrent = tags.get("FinanceLeaseLiabilityNoncurrent", 0.0)
        # else: already bundled into standardized longtermdebt, add nothing.
    # else: no combined tag to compare against — conservative fallback,
    # add nothing rather than risk double-counting.

    return (op_current + fin_current, op_noncurrent + fin_noncurrent)


def get_bal_sheet_intrinio(company: str, apiKey: str, is_financial_or_reit: bool = False) -> dict:
    """
    Intrinio-backed equivalent of get_bal_sheet() — same return shape,
    same point-in-time-snapshot-per-year aggregation.

    Field map (confirmed live 2026-08-24, see docs/decisions.md):
      cash_and_equivalents      -> cashandequivalents, falling back to
                                    restrictedcash (GOLF-shaped filers that
                                    tag combined cash this way)
      total_current_assets       -> totalcurrentassets
      total_current_liabilities  -> totalcurrentliabilities
      short_term_debt             -> shorttermdebt + lease liability addback
                                    (see _intrinio_lease_debt_addback())
      long_term_debt              -> longtermdebt + lease liability addback
                                    (same)
      total_stockholders_equity   -> totalcommonequity (parent-only, matches
                                    AV's totalShareholderEquity semantics —
                                    excludes noncontrolling interest)

    LEASE-INCLUSIVE DEBT (2026-08-25, closes the Phase 1b gap): each period
    fetched also gets the as-reported view checked, adding operating (+
    conditionally finance) lease liability into short_term_debt/long_term_debt
    via _intrinio_lease_debt_addback() — see that function's docstring for
    the full detection logic and its deliberate conservative-fallback bias.

    PERIOD SELECTION (2026-08-25, efficiency fix): balance sheet is
    point-in-time, and every real downstream consumer in av_fcff_2.py only
    ever reads index [0] (current) or [1] (one year prior) — confirmed via
    direct grep, not assumption: calc_chng_wc() explicitly requires exactly
    2 years, calc_bv_debt()/the cash checks only read [0]. So this fetches
    ONLY the current period plus the period exactly one fiscal year prior —
    matched on fiscal_year/fiscal_period from the discovery response (which
    is one cheap call regardless of how many periods it lists), not just
    "4 periods back", since a gap or restated duplicate in the period
    sequence could otherwise silently misalign that offset. Cuts a full
    balance-sheet fetch from 26 API calls (1 discovery + 20 standardized +
    5 as-reported, most of it fetched-then-discarded before this fix) down
    to 5 (1 discovery + 2 standardized + 2 as-reported).

    is_financial_or_reit is accepted for signature parity with
    get_bal_sheet() but unused here — Intrinio's cashandequivalents tag has
    not shown AV's financial-firm/REIT cash-tagging quirk (docs/known_errors.md
    2026-08-02); revisit if Phase 2 finds otherwise.
    """
    periods = _intrinio_periods(company, "balance_sheet_statement", apiKey, n=20)
    if not periods:
        raise ValueError(f"No quarterly balance sheet reports found for {company} on Intrinio")

    current = periods[0]
    prior_year = None
    for p in periods[1:]:
        if (
            p.get("fiscal_period") == current.get("fiscal_period")
            and p.get("fiscal_year") is not None
            and current.get("fiscal_year") is not None
            and p["fiscal_year"] == current["fiscal_year"] - 1
        ):
            prior_year = p
            break

    selected_periods = [current] + ([prior_year] if prior_year else [])
    quarters = [_intrinio_standardized(p["id"], apiKey) for p in selected_periods]

    def _cash(q: dict) -> float:
        v = safe_float(q.get("cashandequivalents"))
        if v > 0:
            return v
        return safe_float(q.get("restrictedcash"))

    # Cash-sanity check ported from get_bal_sheet(), but LOG-ONLY here rather
    # than raising — Intrinio has not shown AV's 1000x cash-corruption
    # pattern in anything checked so far (11/11 clean 2026-08-21, 36/36
    # clean 2026-08-20). Keep watching via logs through Phase 2's shadow-mode
    # run rather than blocking on a failure mode not yet observed on this
    # vendor; promote to a raise if Phase 2 ever proves otherwise.
    latest_cash = _cash(quarters[0])
    latest_tca = safe_float(quarters[0].get("totalcurrentassets"))
    if latest_tca > 0 and latest_cash > 2 * latest_tca:
        logger.warning(
            f"{company}: Intrinio cash ({latest_cash:,.0f}) exceeds 2x total "
            f"current assets ({latest_tca:,.0f}) — would have raised under "
            f"the AV guard; log-only for Intrinio pending Phase 2 evidence."
        )
    if len(quarters) > 1:
        prior_cash = _cash(quarters[1])
        if prior_cash > 0 and latest_cash > 20 * prior_cash:
            logger.warning(
                f"{company}: Intrinio cash ({latest_cash:,.0f}) is more than "
                f"20x the prior quarter's cash ({prior_cash:,.0f}) — would "
                f"have raised under the AV guard; log-only for Intrinio "
                f"pending Phase 2 evidence."
            )

    cash_and_equivalents = []
    total_current_assets = []
    total_current_liabilities = []
    short_term_debt = []
    long_term_debt = []
    total_stockholders_equity = []

    for i, q in enumerate(quarters):
        raw_longtermdebt = safe_float(q.get("longtermdebt"))
        raw_shorttermdebt = safe_float(q.get("shorttermdebt"))
        lease_current, lease_noncurrent = _intrinio_lease_debt_addback(
            selected_periods[i]["id"], apiKey, raw_longtermdebt
        )
        cash_and_equivalents.append(_cash(q))
        total_current_assets.append(safe_float(q.get("totalcurrentassets")))
        total_current_liabilities.append(safe_float(q.get("totalcurrentliabilities")))
        short_term_debt.append(raw_shorttermdebt + lease_current)
        long_term_debt.append(raw_longtermdebt + lease_noncurrent)
        total_stockholders_equity.append(safe_float(q.get("totalcommonequity")))

    if not cash_and_equivalents:
        raise ValueError(f"Insufficient quarterly data to build balance sheet for {company} on Intrinio")

    return {
        "cash_and_equivalents": cash_and_equivalents,
        "total_current_assets": total_current_assets,
        "total_current_liabilities": total_current_liabilities,
        "short_term_debt": short_term_debt,
        "long_term_debt": long_term_debt,
        "total_stockholders_equity": total_stockholders_equity,
    }


def get_cash_flow_intrinio(company: str, apiKey: str) -> dict:
    """
    Intrinio-backed equivalent of get_cash_flow() — same return shape.

    Field map (confirmed live 2026-08-24, see docs/decisions.md):
      capex           -> -purchaseofplantpropertyandequipment. SIGN FLIP
                          REQUIRED: Intrinio reports this as a negative
                          outflow; AV's capitalExpenditures (and every
                          downstream FCFF consumer, e.g.
                          calc_capital_expenditures() in av_fcff_2.py, which
                          sums this list with NO abs()/sign-flip of its own)
                          expects a positive magnitude. Confirmed by reading
                          the FCFF formula itself (ebiat - capex + ...) —
                          getting this sign wrong would silently invert
                          capex's effect on every Intrinio-sourced valuation.
      depreciation     -> depreciationexpense + amortizationexpense summed,
                          matching AV's combined depreciationDepletionAnd-
                          Amortization field. ("Depletion" has no separate
                          Intrinio tag seen so far; omitted if absent, same
                          as AV only reporting what the filer discloses.)
      dividends_paid   -> paymentofdividends. Sign doesn't matter — every
                          call site in av_fcff_2.py wraps this in abs().
    """
    period_ids = _intrinio_period_ids(company, "cash_flow_statement", apiKey)
    quarters = [_intrinio_standardized(pid, apiKey) for pid in period_ids]

    if not quarters:
        raise ValueError(f"No quarterly cash-flow reports found for {company} on Intrinio")

    max_quarters = min(len(quarters), 20)
    depreciation = []
    capex = []
    dividends_paid = []

    for i in range(0, max_quarters, 4):
        block = quarters[i : i + 4]
        if len(block) < 4:
            break
        yearly_capex = sum(-safe_float(q.get("purchaseofplantpropertyandequipment")) for q in block)
        yearly_depr = sum(
            safe_float(q.get("depreciationexpense")) + safe_float(q.get("amortizationexpense"))
            for q in block
        )
        yearly_divs = sum(safe_float(q.get("paymentofdividends")) for q in block)

        capex.append(yearly_capex)
        depreciation.append(yearly_depr)
        dividends_paid.append(yearly_divs)

    return {
        "capex": capex,
        "depreciation": depreciation,
        "dividends_paid": dividends_paid,
    }


# ---------------------------------------------------------------------------
# Annual-history Intrinio fetchers (Phase 4, 2026-08-26) — moat_score.py/
# lynch_score.py/growth_screen_2.py want up to 10 years of true ANNUAL
# figures (AV's annualReports semantic), a different shape from the
# quarterly-aggregating fetchers above (built for av_fcff_2.py, which only
# ever reads a handful of recent years). Each function here returns a list
# of dicts using AV's OWN field names, most-recent-year-first, so the
# existing AV-shaped parsing code in each caller needs zero changes -- only
# the fetch call itself swaps to a fallback-aware wrapper. Uses Intrinio's
# native type="FY" periods (real, as-filed annual figures) rather than
# summing 4 quarters -- more accurate, and needs far fewer API calls for a
# 10-year lookback (10 FY periods vs. 40 quarterly periods).
# ---------------------------------------------------------------------------


def get_inc_stmnt_intrinio_annual(company: str, apiKey: str, years: int = 10, is_financial_or_reit: bool = False) -> list[dict]:
    """
    Up to `years` years of real annual income-statement data, shaped as a
    list of dicts matching AV's INCOME_STATEMENT annualReports entries:
    totalRevenue, grossProfit, ebit, netIncome, incomeTaxExpense,
    incomeBeforeTax, interestExpense. Most-recent-year-first.

    Field map (confirmed live 2026-08-26 against AAPL real FY periods):
      totalRevenue     -> totalrevenue
      grossProfit       -> totalgrossprofit (NOT "grossprofit" -- that tag
                          returns None; confirmed via a real AAPL FY period)
      ebit               -> totaloperatingincome (same EBIT proxy choice as
                          the quarterly fetcher, and AV's operatingIncome)
      netIncome           -> netincome
      incomeTaxExpense     -> incometaxexpense
      incomeBeforeTax       -> totalpretaxincome
      interestExpense        -> via the shared _intrinio_quarter_interest_expense()
                          helper (period-agnostic despite the name -- prefers
                          totalinterestexpense, falls back to sign-flipped
                          totalinterestincome, same logic as the quarterly path)
      commonStockSharesOutstanding -> weightedavebasicdilutedsharesos (added
                          2026-08-26 for lynch_score.py; lives on the income
                          statement in Intrinio's schema, not the balance
                          sheet where AV puts it -- see
                          get_bal_sheet_intrinio_annual() and
                          fetch_lynch_financials() for how the two get
                          reconciled back into AV's shape. Same tag
                          get_quote_intrinio()'s docstring already flagged as
                          WRONG for a point-in-time shares-outstanding proxy
                          (understated UPS's real count by ~12%) -- but here
                          it's used only to compute a PAST year's EPS
                          (net_income / weighted-average shares during that
                          year), which is the textbook-correct EPS
                          denominator, not a misuse of the tag.

    is_financial_or_reit: same bank-template guard as get_inc_stmnt_intrinio()
    (see its docstring, 2026-08-26) -- when False, a response using the bank
    template (totalinterestincome present, totaloperatingincome absent as a
    key) raises instead of silently returning ebit=0 for every year. Found
    live the same day this function was written: GRBK scored a bogus
    all-zero moat (avg_roic=0.0, avg_gross_margin=0.0, rating "None")
    through this exact path before the guard was added here too -- the
    quarterly fetcher's guard does not cover this separate annual code path.

    Raises ValueError on fewer than 2 years of coverage -- one year alone
    can never support a multi-year lookback, so this triggers the AV
    fallback the same way zero coverage does. Found live 2026-08-26: XOM
    genuinely has 20 years of Intrinio income-statement and cash-flow FY
    history but only 1 year of balance-sheet FY history -- a real, narrow
    per-statement gap, not a bug -- which silently passed the old
    "if not period_ids" check (1 is non-empty) and produced a useless
    1-year AnnualFinancials that then failed moat_score.py's own
    MIN_YEARS=3 check with a confusing downstream message instead of
    falling back to AV, which has full history.
    """
    period_ids = _intrinio_period_ids(company, "income_statement", apiKey, n=years, period_type="FY")
    if len(period_ids) < 2:
        raise ValueError(
            f"Insufficient annual income statement data for {company} on Intrinio "
            f"({len(period_ids)} year(s) available)"
        )

    annual_reports = []
    for pid in period_ids:
        q = _intrinio_standardized(pid, apiKey)
        if not is_financial_or_reit and "totaloperatingincome" not in q and "totalinterestincome" in q:
            raise ValueError(
                f"{company}: Intrinio returned the bank/financial-institution "
                f"template (totalinterestincome present, totaloperatingincome "
                f"absent) for a non-financial ticker -- likely an Intrinio-side "
                f"industry misclassification, not a real zero-EBIT company."
            )
        annual_reports.append({
            "totalRevenue": q.get("totalrevenue"),
            "grossProfit": q.get("totalgrossprofit"),
            "ebit": q.get("totaloperatingincome"),
            "netIncome": q.get("netincome"),
            "incomeTaxExpense": q.get("incometaxexpense"),
            "incomeBeforeTax": q.get("totalpretaxincome"),
            "interestExpense": _intrinio_quarter_interest_expense(q),
            "commonStockSharesOutstanding": q.get("weightedavebasicdilutedsharesos"),
        })
    return annual_reports


def get_bal_sheet_intrinio_annual(company: str, apiKey: str, years: int = 10) -> list[dict]:
    """
    Up to `years` years of real annual balance-sheet data, shaped as a list
    of dicts matching AV's BALANCE_SHEET annualReports entries:
    totalShareholderEquity, longTermDebt, shortTermDebt,
    cashAndCashEquivalentsAtCarryingValue, shortTermInvestments,
    cashAndShortTermInvestments, totalAssets, inventory,
    currentNetReceivables. Most-recent-year-first.

    Field map (confirmed live 2026-08-26 against AAPL real FY periods):
      totalShareholderEquity -> totalcommonequity
      longTermDebt             -> longtermdebt (raw, no lease addback --
                          deliberately matches what AV's own annualReports
                          already provided for moat_score.py; this is a
                          vendor swap, not a methodology change)
      shortTermDebt             -> shorttermdebt (same, raw)
      cashAndCashEquivalentsAtCarryingValue -> cashandequivalents
      shortTermInvestments       -> shortterminvestments
      cashAndShortTermInvestments -> cashandequivalents + shortterminvestments
                          (Intrinio has no distinct pre-aggregated tag the
                          way AV does; the granular sum is the best available
                          substitute -- no evidence Intrinio's granular
                          components are unreliable for financial firms the
                          way AV's were, see docs/known_errors.md 2026-08-02)
      totalAssets                -> totalassets
      inventory                  -> netinventory (added 2026-08-26 for
                          lynch_score.py's balance-sheet-discipline score;
                          0 for service companies on both vendors)
      currentNetReceivables       -> accountsreceivable (same, 2026-08-26)

    Raises ValueError on fewer than 2 years of coverage -- one year alone
    can never support a multi-year lookback, so this triggers the AV
    fallback the same way zero coverage does. Found live 2026-08-26: XOM
    genuinely has 20 years of Intrinio income-statement and cash-flow FY
    history but only 1 year of balance-sheet FY history -- a real, narrow
    per-statement gap, not a bug -- which silently passed the old
    "if not period_ids" check (1 is non-empty) and produced a useless
    1-year AnnualFinancials that then failed moat_score.py's own
    MIN_YEARS=3 check with a confusing downstream message instead of
    falling back to AV, which has full history.
    """
    period_ids = _intrinio_period_ids(company, "balance_sheet_statement", apiKey, n=years, period_type="FY")
    if len(period_ids) < 2:
        raise ValueError(
            f"Insufficient annual balance sheet data for {company} on Intrinio "
            f"({len(period_ids)} year(s) available)"
        )

    annual_reports = []
    for pid in period_ids:
        q = _intrinio_standardized(pid, apiKey)
        cash = safe_float(q.get("cashandequivalents"))
        sti = safe_float(q.get("shortterminvestments"))
        annual_reports.append({
            "totalShareholderEquity": q.get("totalcommonequity"),
            "longTermDebt": q.get("longtermdebt"),
            "shortTermDebt": q.get("shorttermdebt"),
            "cashAndCashEquivalentsAtCarryingValue": cash,
            "shortTermInvestments": sti,
            "cashAndShortTermInvestments": cash + sti,
            "totalAssets": q.get("totalassets"),
            "inventory": q.get("netinventory"),
            "currentNetReceivables": q.get("accountsreceivable"),
        })
    return annual_reports


def get_cash_flow_intrinio_annual(company: str, apiKey: str, years: int = 10) -> list[dict]:
    """
    Up to `years` years of real annual cash-flow data, shaped as a list of
    dicts matching AV's CASH_FLOW annualReports entries: operatingCashflow,
    capitalExpenditures. Most-recent-year-first.

    Field map (confirmed live 2026-08-26 against AAPL real FY periods):
      operatingCashflow  -> netcashfromoperatingactivities
      capitalExpenditures -> purchaseofplantpropertyandequipment, UNCHANGED
                          SIGN (negative, an outflow) -- matches AV's own raw
                          annualReports convention exactly (AV also stores
                          this negative; moat_score.py's calc_fcf_margin_series()
                          already adds it as a negative: `fcf = operating_cf +
                          capex`). Do NOT sign-flip here the way
                          get_cash_flow_intrinio() above does -- that
                          function serves av_fcff_2.py's different
                          convention (positive capex), not this one.

    Raises ValueError on fewer than 2 years of coverage -- one year alone
    can never support a multi-year lookback, so this triggers the AV
    fallback the same way zero coverage does. Found live 2026-08-26: XOM
    genuinely has 20 years of Intrinio income-statement and cash-flow FY
    history but only 1 year of balance-sheet FY history -- a real, narrow
    per-statement gap, not a bug -- which silently passed the old
    "if not period_ids" check (1 is non-empty) and produced a useless
    1-year AnnualFinancials that then failed moat_score.py's own
    MIN_YEARS=3 check with a confusing downstream message instead of
    falling back to AV, which has full history.
    """
    period_ids = _intrinio_period_ids(company, "cash_flow_statement", apiKey, n=years, period_type="FY")
    if len(period_ids) < 2:
        raise ValueError(
            f"Insufficient annual cash flow data for {company} on Intrinio "
            f"({len(period_ids)} year(s) available)"
        )

    annual_reports = []
    for pid in period_ids:
        q = _intrinio_standardized(pid, apiKey)
        annual_reports.append({
            "operatingCashflow": q.get("netcashfromoperatingactivities"),
            "capitalExpenditures": q.get("purchaseofplantpropertyandequipment"),
        })
    return annual_reports


def get_rAndD_intrinio(company: str, rd_years: int, apiKey: str):
    """
    Intrinio-backed equivalent of get_rAndD() — same non-standard 2-tuple
    return: (dict with a list of yearly R&D expenses, years actually
    processed).

    Field map: research_and_development -> rdexpense (confirmed clean,
    single unambiguous tag — easiest field in the whole migration).
    """
    period_ids = _intrinio_period_ids(company, "income_statement", apiKey)
    if not period_ids:
        raise ValueError(f"No quarterly reports found for {company} on Intrinio")

    num_available_years = len(period_ids) // 4
    years_to_process = min(rd_years, num_available_years)

    rd_amount = []
    for i in range(years_to_process):
        block_ids = period_ids[i * 4 : i * 4 + 4]
        year_rd = 0.0
        for pid in block_ids:
            q = _intrinio_standardized(pid, apiKey)
            year_rd += safe_float(q.get("rdexpense"))
        rd_amount.append(year_rd)

    return {"research_and_development": rd_amount}, years_to_process


def get_quote_intrinio(company: str, apiKey: str):
    """
    Intrinio-backed equivalent of get_quote() — same non-standard bare
    6-tuple return, same order: (price, sharesOutstanding, marketCap,
    company_name, dividend_yield, analyst_count).

    Field map (confirmed live 2026-08-24, see docs/decisions.md):
      price             -> data_point/adj_close_price
      marketCap          -> data_point/marketcap
      sharesOutstanding   -> DERIVED as marketCap / price, not a weighted-
                            average tag. Confirmed necessary: this project's
                            own DAL/UPS work found Intrinio's
                            weightedavebasicdilutedsharesos is a
                            trailing-quarter EPS-denominator average, not
                            current point-in-time shares — it understated
                            UPS's real share count by ~12% due to real
                            buyback activity. marketcap/price is Intrinio's
                            own real-time-consistent pair, not a proxy.
      company_name        -> companies/{ticker} -> "name"
      dividend_yield       -> data_point/trailing_dividend_yield
      analyst_count        -> NOT AVAILABLE via Intrinio on the current
                            (Starter/trial) plan — companies/{id}/data_point/
                            analyst_ratings returned an error 2026-08-21;
                            the whole Zacks package (which would carry this)
                            is separately plan-gated too, confirmed
                            2026-08-26 (see project_intrinio_zacks_
                            analyst_count_gate memory). Sourced from Yahoo
                            Finance instead (get_analyst_count_yahoo(),
                            2026-08-26) -- an unofficial, undocumented feed
                            with a known history of breaking, so this
                            degrades to 0 with a logged warning on ANY
                            failure rather than raising, exactly like this
                            field's original all-Intrinio-plans-lack-it
                            degrade did before Yahoo was wired in.

    Raises
    ------
    RuntimeError
        If price or marketCap/name data is unavailable — mirrors
        get_quote()'s RuntimeError on missing AV OVERVIEW fields (usually
        means no Intrinio coverage for the symbol).
    """
    price = safe_float(_intrinio_get(f"companies/{company}/data_point/adj_close_price", apiKey))
    if price <= 0:
        raise RuntimeError(
            f"Intrinio price data for {company} is missing or zero — "
            f"likely no Intrinio coverage for this symbol"
        )

    market_cap = safe_float(_intrinio_get(f"companies/{company}/data_point/marketcap", apiKey))
    company_info = _intrinio_get(f"companies/{company}", apiKey)
    company_name = company_info.get("name")
    if not market_cap or not company_name:
        raise RuntimeError(
            f"Intrinio company data for {company} is missing marketCap/name — "
            f"likely no Intrinio coverage for this symbol"
        )

    shares_outstanding = market_cap / price

    try:
        dividend_yield = safe_float(
            _intrinio_get(f"companies/{company}/data_point/trailing_dividend_yield", apiKey)
        )
    except Exception:
        dividend_yield = 0.0

    try:
        analyst_count = get_analyst_count_yahoo(company)
    except Exception as exc:
        analyst_count = 0
        logger.warning(
            f"{company}: analyst_count fetch failed (neither Intrinio nor AV "
            f"has this field; Yahoo Finance fallback also failed: {exc}) — "
            f"defaulting to 0."
        )

    return price, shares_outstanding, market_cap, company_name, dividend_yield, analyst_count


def get_overview_intrinio(company: str, apiKey: str) -> dict:
    """
    Intrinio-backed equivalent of AV's OVERVIEW endpoint (Phase 4,
    2026-08-26, built for lynch_score.py/growth_monitor.py; extended
    2026-08-26 same day for growth_screen_2.py) -- a current/TTM snapshot,
    not annual history. Returns an AV-shaped dict: Symbol, PERatio,
    SharesOutstanding, DividendYield, Name, MarketCapitalization.

    Field map (confirmed live 2026-08-26 against AAPL):
      Symbol               -> the `company` argument itself, echoed back
                            (matches AV's OVERVIEW, which also just echoes
                            the requested symbol) -- growth_screen_2.py's
                            "not overview or 'Symbol' not in overview"
                            no-coverage check needs this key present.
      PERatio             -> data_point/pricetoearnings (confirmed live,
                            e.g. 35.3686 for AAPL -- a real, direct trailing
                            P/E data point, not derived)
      SharesOutstanding     -> reuses get_quote_intrinio()'s marketCap/price
                            derivation (a current, real-time-consistent
                            point-in-time count -- correct here, unlike the
                            weighted-average annual tag used for historical
                            EPS elsewhere in this file)
      DividendYield          -> reuses get_quote_intrinio()'s
                            trailing_dividend_yield field
      Name                   -> reuses get_quote_intrinio()'s company_name
                            (already fetched internally to build the tuple
                            above; just wasn't surfaced until
                            growth_screen_2.py needed it)
      MarketCapitalization    -> reuses get_quote_intrinio()'s market_cap,
                            same reasoning

    Deliberately does NOT include a "Sector"/"Industry" key -- unlike AV's
    own OVERVIEW classification, every caller in this codebase that needs
    sector/industry (including the valuation table's own `industry` column,
    see av_fcff_2.py) already sources it from hg_dcflib.get_industry()'s
    Damodaran table, not any vendor's OVERVIEW endpoint. Callers that display
    sector/industry alongside this dict (growth_screen_2.py) should call
    get_industry()/replacer.industry_to_sector() directly rather than
    expecting this function to carry it.

    Raises whatever get_quote_intrinio() raises on missing price/marketCap/
    name (no Intrinio coverage for the symbol) -- PERatio itself degrades to
    None (matching AV's own behavior for loss-making companies, where
    PERatio is legitimately absent) rather than being treated as fatal.
    """
    price, shares_outstanding, market_cap, company_name, dividend_yield, _ = get_quote_intrinio(company, apiKey)

    try:
        pe_ratio = safe_float(_intrinio_get(f"companies/{company}/data_point/pricetoearnings", apiKey))
    except Exception:
        pe_ratio = None

    return {
        "Symbol": company,
        "PERatio": pe_ratio,
        "SharesOutstanding": shares_outstanding,
        "DividendYield": dividend_yield,
        "Name": company_name,
        "MarketCapitalization": market_cap,
    }


def get_quarterly_eps_intrinio(company: str, apiKey: str, quarters: int = 20) -> list[dict]:
    """
    Intrinio-backed equivalent of AV's EARNINGS endpoint's quarterlyEarnings
    array (Phase 4, 2026-08-26, built for growth_monitor.py's and
    growth_screen_2.py's negative-quarterly-EPS growth-screen gate). Returns
    an AV-shaped list of {"fiscalDateEnding": ..., "reportedEPS": ...}
    dicts, most-recent-first -- same order as AV's quarterlyEarnings and as
    _intrinio_period_ids().

    Deliberately a separate fetch from get_inc_stmnt_intrinio(): that
    function aggregates quarters into 4-quarter annual blocks and fetches
    only 12 quarters (3 years) for the FCFF wealth-gate check; this caller
    needs up to 20 individual, unaggregated quarters (5 years) to match
    AV's QUARTERS_TO_CHECK=20 window.

    Field map (confirmed live 2026-08-26 against AAPL):
      reportedEPS      -> dilutedeps, falling back to basiceps for periods/
                          filers where Intrinio's standardized template
                          omits the diluted figure (both are direct reported
                          tags, not derived here).
      fiscalDateEnding  -> synthesized as "{fiscal_year}-{fiscal_period}"
                          (e.g. "2026-Q3"), NOT a real calendar date the way
                          AV's fiscalDateEnding is -- Intrinio's period
                          discovery only exposes fiscal year/quarter labels,
                          not the underlying period-end date, at this call
                          site. Sufficient for every caller's actual use
                          (chronological sort -- this label sorts correctly
                          because fiscal_year always dominates the
                          comparison -- and human-readable display), but
                          don't treat it as an ISO date.

    Raises if Intrinio has no income_statement periods at all for the
    ticker (triggers the caller's AV fallback); a period present in the
    list but missing both EPS tags degrades that single entry's
    reportedEPS to None rather than raising, matching AV's own behavior for
    periods with an EPS gap.
    """
    periods = _intrinio_periods(company, "income_statement", apiKey, n=quarters)
    if not periods:
        raise ValueError(f"No quarterly reports found for {company} on Intrinio")

    reports = []
    for p in periods:
        q = _intrinio_standardized(p["id"], apiKey)
        val = q.get("dilutedeps")
        if val is None:
            val = q.get("basiceps")
        reports.append({
            "fiscalDateEnding": f"{p.get('fiscal_year')}-{p.get('fiscal_period')}",
            "reportedEPS": safe_float(val) if val is not None else None,
        })
    return reports


def get_daily_prices_intrinio(company: str, apiKey: str, start_date: str, end_date: str, page_size: int = 200) -> list[dict]:
    """
    Intrinio-backed equivalent of AV's TIME_SERIES_DAILY_ADJUSTED endpoint
    (Phase 4, 2026-08-26, built for drip_processor.py's dividend-detection
    and build_*.py's historical-pricing needs). Returns an AV-shaped list of
    {"date": ..., "close": ..., "dividend": ...} dicts, most-recent-first --
    same order as Intrinio's own `/securities/{id}/prices` response.

    Field map (confirmed live 2026-08-26 against AAPL):
      close      -> close (real daily close; Intrinio also offers a split/
                    dividend-adjusted `adj_close`, but every caller here
                    needs the same as-traded close AV's "4. close" gave --
                    dividend detection specifically needs the un-adjusted
                    price paid on the ex-dividend date, not a backward-
                    adjusted one)
      dividend   -> dividend (real per-day dividend amount, 0.0 on every
                    non-ex-dividend day -- same semantic as AV's "7. dividend
                    amount"; confirmed live: 2 real ex-div days found in a
                    100-day AAPL window, $0.27/share Aug-10-2026)

    Single-page fetch only (`page_size` defaults to 200, comfortably
    covering any lookback window this codebase actually uses -- the AV
    endpoint this replaces used "outputsize=compact", itself hard-capped at
    100 trading days, so no caller here has ever needed more than that in
    one response). Raises if Intrinio returns no price data at all for the
    ticker/date range (no coverage, or a date range with no trading days).
    """
    data = _intrinio_get(
        f"securities/{company}/prices",
        apiKey,
        start_date=start_date,
        end_date=end_date,
        frequency="daily",
        page_size=page_size,
    )
    prices = data.get("stock_prices", [])
    if not prices:
        raise ValueError(f"No daily price data found for {company} on Intrinio for {start_date}..{end_date}")

    return [
        {
            "date": p.get("date"),
            "close": safe_float(p.get("close")),
            "dividend": safe_float(p.get("dividend")) or 0.0,
        }
        for p in prices
    ]


def get_price_on_or_before_intrinio(company: str, apiKey: str, target_date: str, lookback_days: int = 14) -> tuple[float, str]:
    """
    Intrinio-backed helper for the "closest trading day on or before
    target_date" pattern (Phase 4, 2026-08-26) shared by all 4 build_*.py
    portfolio-construction scripts. Two of them (build_buffett_9010.py,
    build_new_value20.py) already wrote exactly this general algorithm by
    hand against AV's raw series; the other two (build_lynch_portfolios.py,
    build_value20_moat.py) hardcode a 4-date FALLBACK_DATES list near
    2026-04-01 that turns out to just be this same algorithm's result,
    manually unrolled -- both are unified into this one function's contract.

    Uses get_daily_prices_intrinio() with a lookback window ending at
    target_date; lookback_days=14 is generous for any single holiday/
    long-weekend cluster (AV's own hand-written version used a full
    "compact" 100-day window for the same purpose, so this is deliberately
    much tighter -- there's no reason to fetch 100 days to find one nearby
    trading day).

    Returns (close_price, actual_trading_date) -- actual_trading_date may
    be earlier than target_date if target_date itself wasn't a trading day.
    Raises if no day with real price data exists anywhere in the window.
    """
    start_date = (datetime.date.fromisoformat(target_date) - datetime.timedelta(days=lookback_days)).isoformat()
    series = get_daily_prices_intrinio(company, apiKey, start_date=start_date, end_date=target_date)
    for day in series:  # most-recent-first, already bounded by end_date=target_date
        if day["close"] and day["close"] > 0:
            return day["close"], day["date"]
    raise ValueError(f"No price on or before {target_date} for {company} within {lookback_days} days")


# ── Yahoo Finance (unofficial) — analyst_count only ──────────────────────────
#
# Neither Intrinio (Zacks package plan-gated, see project_intrinio_zacks_
# analyst_count_gate memory) nor AV ever provided a usable analyst_count.
# Yahoo's undocumented quoteSummary endpoint has a direct
# numberOfAnalystOpinions field (confirmed live 2026-08-26, AAPL=39) --
# but Jim has direct prior experience with this exact feed breaking
# repeatedly ("the feed kept changing... I eventually quit beating my head
# against the wall"). Deliberately isolated from every other fetcher in this
# file: never called from get_quote_intrinio()'s core price/name/marketCap
# path, only wired into that function's already-optional analyst_count slot
# (which already degraded to 0 on failure before this existed). A break here
# must never be able to threaten a valuation run.

_yahoo_session: "requests.Session | None" = None
_yahoo_crumb: str | None = None


def _yahoo_get_crumb(force_refresh: bool = False) -> tuple["requests.Session", str]:
    """
    Lazily creates and caches one requests.Session + crumb token for the
    life of the process -- re-fetching a crumb (and a fresh cookie jar) on
    every single ticker in a full-universe batch would be both slow and a
    faster route to Yahoo rate-limiting/blocking. Call with
    force_refresh=True to discard a stale cached crumb and get a new one
    (get_analyst_count_yahoo() does this once on an "Invalid Crumb" response
    before giving up).
    """
    global _yahoo_session, _yahoo_crumb
    if _yahoo_session is None or force_refresh:
        _yahoo_session = requests.Session()
        _yahoo_session.headers.update({"User-Agent": "Mozilla/5.0"})
        resp = _yahoo_session.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15)
        resp.raise_for_status()
        crumb = resp.text.strip()
        if not crumb or "error" in crumb.lower():
            raise RuntimeError(f"Yahoo Finance: could not obtain a crumb token (got: {crumb!r})")
        _yahoo_crumb = crumb
    return _yahoo_session, _yahoo_crumb


def get_analyst_count_yahoo(company: str) -> int:
    """
    Yahoo Finance (unofficial, undocumented, no API key) equivalent of an
    analyst-coverage-count field neither Intrinio nor AV ever provided.
    Field map (confirmed live 2026-08-26 against AAPL, 39 analysts):
      numberOfAnalystOpinions -> quoteSummary?modules=financialData ->
                                 financialData.numberOfAnalystOpinions.raw

    Retries once with a freshly-fetched crumb on an auth failure (a stale
    cached crumb, not necessarily a real outage) before raising. Raises
    (does not silently return 0) on any failure -- matches every other
    fetcher in this file; the caller (get_quote_intrinio()) decides how to
    degrade, same pattern already used for its dividend_yield field.
    """
    session, crumb = _yahoo_get_crumb()
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{company}"
    resp = session.get(url, params={"modules": "financialData", "crumb": crumb}, timeout=15)

    if resp.status_code == 401:
        session, crumb = _yahoo_get_crumb(force_refresh=True)
        resp = session.get(url, params={"modules": "financialData", "crumb": crumb}, timeout=15)

    resp.raise_for_status()
    data = resp.json()

    error = data.get("quoteSummary", {}).get("error")
    if error:
        raise RuntimeError(f"Yahoo Finance error for {company}: {error}")

    results = data.get("quoteSummary", {}).get("result") or []
    if not results:
        raise ValueError(f"Yahoo Finance: no quoteSummary result for {company}")

    count = results[0].get("financialData", {}).get("numberOfAnalystOpinions", {}).get("raw")
    if count is None:
        raise ValueError(f"Yahoo Finance: numberOfAnalystOpinions missing for {company}")

    return int(count)


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
