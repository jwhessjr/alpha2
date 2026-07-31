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
    global _default_spread_df
    if _default_spread_df is None:
        _default_spread_df = pd.read_excel(_DATA_DIR / "defaultSpread.xlsx")
    return _default_spread_df


def _get_rd_amort() -> pd.DataFrame:
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
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# Read statements from Alpha Vantage


def get_jsonparsed_data(url):
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
        headers = {"User-Agent": "hg-dcf-model/1.0 research@example.com"}
        resp = requests.get(url, headers=headers, timeout=15)
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


# Function to get the balance sheet and extract the required fields


# def get_bal_sheet(company, apiKey):
#     url = f"https://www.alphavantage.co/query?function=BALANCE_SHEET&symbol={company}&apikey={apiKey}"

#     data = get_jsonparsed_data(url)
#     balSheet = data.get("quarterlyReports", [])
#     balSht = {}
#     cashAndEquivalents = [
#         safe_float(balSheet[0]["cashAndShortTermInvestments"]),
#         safe_float(balSheet[4]["cashAndShortTermInvestments"]),
#         safe_float(balSheet[8]["cashAndShortTermInvestments"]),
#         safe_float(balSheet[12]["cashAndShortTermInvestments"]),
#         safe_float(balSheet[16]["cashAndShortTermInvestments"]),
#     ]
#     currentAssets = [
#         safe_float(balSheet[0]["totalCurrentAssets"]),
#         safe_float(balSheet[4]["totalCurrentAssets"]),
#         safe_float(balSheet[8]["totalCurrentAssets"]),
#         safe_float(balSheet[12]["totalCurrentAssets"]),
#         safe_float(balSheet[16]["totalCurrentAssets"]),
#     ]

#     stockholdersEquity = [
#         safe_float(balSheet[0]["totalShareholderEquity"]),
#         safe_float(balSheet[4]["totalShareholderEquity"]),
#         safe_float(balSheet[8]["totalShareholderEquity"]),
#         safe_float(balSheet[12]["totalShareholderEquity"]),
#         safe_float(balSheet[16]["totalShareholderEquity"]),
#     ]
#     currentLiabilities = [
#         safe_float(balSheet[0]["totalCurrentLiabilities"]),
#         safe_float(balSheet[4]["totalCurrentLiabilities"]),
#         safe_float(balSheet[8]["totalCurrentLiabilities"]),
#         safe_float(balSheet[12]["totalCurrentLiabilities"]),
#         safe_float(balSheet[16]["totalCurrentLiabilities"]),
#     ]
#     currentLongDebt = [
#         safe_float(balSheet[0]["currentLongTermDebt"]),
#         safe_float(balSheet[4]["currentLongTermDebt"]),
#         safe_float(balSheet[8]["currentLongTermDebt"]),
#         safe_float(balSheet[12]["currentLongTermDebt"]),
#         safe_float(balSheet[16]["currentLongTermDebt"]),
#     ]
#     shortTermDebt = [
#         safe_float(balSheet[0]["shortTermDebt"]),
#         safe_float(balSheet[4]["shortTermDebt"]),
#         safe_float(balSheet[8]["shortTermDebt"]),
#         safe_float(balSheet[12]["shortTermDebt"]),
#         safe_float(balSheet[16]["shortTermDebt"]),
#     ]
#     longTermDebt = [
#         safe_float(balSheet[0]["longTermDebt"]),
#         safe_float(balSheet[4]["longTermDebt"]),
#         safe_float(balSheet[8]["longTermDebt"]),
#         safe_float(balSheet[12]["longTermDebt"]),
#         safe_float(balSheet[16]["longTermDebt"]),
#     ]
#     balSht["cash_and_equivalents"] = cashAndEquivalents
#     balSht["total_current_assets"] = currentAssets
#     # balSht["totalAssets"] = totalAssets
#     # balSht["accountsPayable"] = accountsPayable
#     balSht["current_long_debt"] = currentLongDebt
#     balSht["short_term_debt"] = shortTermDebt
#     balSht["long_term_debt"] = longTermDebt
#     balSht["total_current_liabilities"] = currentLiabilities
#     # balSht["totalLiabilities"] = liabilities
#     balSht["total_stockholders_equity"] = stockholdersEquity
#     return balSht


def _q_cash_and_sti(q: dict) -> float:
    """
    Cash + short-term investments for one AV BALANCE_SHEET quarterly report.

    AV's own pre-aggregated "cashAndShortTermInvestments" field is not
    reliably the sum of its two components — confirmed for GOOG (2026-07-31):
    it returned a value identical to cashAndCashEquivalentsAtCarryingValue
    alone, silently dropping $186.6B of shortTermInvestments. That left
    short-term investments trapped inside "non-cash working capital",
    inflating calc_chng_wc()'s reinvestment estimate by roughly the same
    amount and pinning the reinvestment rate at its 100% cap. Same class of
    problem as AV's `ebit` field (see _q_ebit() in av_fcff_2.py) — prefer the
    granular components over AV's own pre-summed convenience field.
    """
    granular = safe_float(q.get("cashAndCashEquivalentsAtCarryingValue")) + safe_float(
        q.get("shortTermInvestments")
    )
    if granular > 0:
        return granular
    return safe_float(q.get("cashAndShortTermInvestments"))


def get_bal_sheet(company, apiKey):
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
        cash_and_equivalents.append(_q_cash_and_sti(q))
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
    _sleep_with_jitter()
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


# function to retrieve R&D expense so we can capitalize it


def get_erp():
    # URL of the page
    url = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/home.htm"  # Replace with the correct full URL if deeper than homepage

    # Fetch the page
    response = requests.get(url)
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


def get_rAndD(company, rd_years, apiKey):
    _sleep_with_jitter()
    """
    Fetches R&D expenses for a specified number of years from Alpha Vantage.

    Args:
        company (str): The company symbol.
        rd_years (int): The number of years to fetch R&D data for.
        apiKey (str): The Alpha Vantage API key.

    Returns:
        dict: A dictionary containing a list of yearly R&D expenses.
    """
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


def get_risk_free(FRED_KEY):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "GS10",
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    # Fetch data
    response = requests.get(url, params=params)

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
    "NasdaqCM",
    "NasdaqNM",
    "NYSEMKT",
    "NYSEARCA",
    "BATS",
}


def get_industry(company):
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


def get_beta(industry):
    beta = _get_betas()

    unleveredBeta = 1.0  # market-average default if industry not found
    for index, row in beta.iterrows():
        try:
            if industry in row["Industry Name"]:
                unleveredBeta = row["Unlevered beta corrected for cash"]
                break
        except TypeError:
            continue

    logger.info(f"Beta {unleveredBeta}")
    return unleveredBeta


def get_industry_de(industry):
    """
    Industry-average D/E ratio from the same "Industry Averages" sheet get_beta()
    already reads — used to cap (never raise) a company's stable-phase D/E in
    calc_levered_beta(). See docs/decisions.md "Stable-phase capital structure"
    for why this only ever caps down, not up.
    """
    beta = _get_betas()

    industryDE = 1.0  # market-average-ish default if industry not found
    for index, row in beta.iterrows():
        try:
            if industry in row["Industry Name"]:
                industryDE = row["D/E Ratio"]
                break
        except TypeError:
            continue

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
