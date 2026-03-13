"""
This library is a collection of functions used in the Hess Group DCF model.

"""

import os
import sys
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
import logging

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

DELAY = 1.0 / 5  # 0.2 seconds between calls


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


# Read statements from Alpha Vantage


def get_jsonparsed_data(url):
    time.sleep(DELAY)
    response = requests.get(url)
    response.raise_for_status()
    return response.json()


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


def get_inc_stmnt(company: str, apiKey: str) -> dict:
    time.sleep(DELAY)
    """Return annualized ebit, tax expense and interest expense
       from the quarterly reports of a ticker.

    The API returns up to 20 recent quarters; we aggregate them into at most five years.
    """
    url = (
        f"https://www.alphavantage.co/query?"
        f"function=INCOME_STATEMENT&symbol={company}&apikey={apiKey}"
    )
    resp = requests.get(url)
    data = resp.json()

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

        ebit = sum(safe_float(q["ebit"]) for q in quarter_block)
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


def get_bal_sheet(company, apiKey):
    time.sleep(DELAY)
    url = (
        f"https://www.alphavantage.co/query?"
        f"function=BALANCE_SHEET&symbol={company}&apikey={apiKey}"
    )
    resp = requests.get(url)
    resp.raise_for_status()
    data = resp.json()

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
        cash_and_equivalents.append(safe_float(q["cashAndShortTermInvestments"]))
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
    time.sleep(DELAY)
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
    resp = requests.get(url)
    data = resp.json()

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
    time.sleep(DELAY)
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
    resp = requests.get(url)
    data = resp.json()
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
    sharesOutstanding = safe_float(data["SharesOutstanding"])
    marketCap = safe_float(data["MarketCapitalization"])
    company_name = data["Name"]
    entQuote = price, sharesOutstanding, marketCap, company_name
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
    indName = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/indname.xlsx",
        sheet_name="US by Industry",
    )

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
    beta = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/betas.xlsx",
        sheet_name="Industry Averages",
        skiprows=9,
    )

    for index, row in beta.iterrows():
        try:
            if industry in row["Industry Name"]:
                unleveredBeta = row["Unlevered beta corrected for cash"]
            else:
                continue
        except TypeError:
            continue

    logger.info(f"Beta {unleveredBeta}")
    return unleveredBeta


def get_default_spread(intCover):
    defaultSpread = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/defaultSpread.xlsx"
    )

    # for col in defaultSpread.columns:
    #     print(col)

    for index in defaultSpread.index:
        if (
            intCover > defaultSpread["GT"][index]
            and intCover < defaultSpread["LT"][index]
        ):
            return defaultSpread["Spread"][index]
        else:
            continue
    # print(defa
    # ultSpread)
    # print(defaultSpread.index)


def get_rAndD_years(industry):
    amortYears = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/RD_Amortization.xlsx",
        sheet_name="Amort Years",
    )

    for index, row in amortYears.iterrows():
        try:
            if industry == row["Industry"]:
                rAndD_years = row["Years"]
                logger.info(f"Years = {rAndD_years}")
            else:
                continue
        except TypeError:
            continue
        except AttributeError:
            continue

    return rAndD_years
