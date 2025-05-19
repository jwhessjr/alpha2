"""
This library is a collection of functions used in the Hess Group DCF model.

"""

import json
from urllib.request import urlopen

import certifi
import pandas as pd
import requests
from bs4 import BeautifulSoup

# import logging


def safe_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0
# Read statements from Alpha Vantage


def get_jsonparsed_data(url):
    response = urlopen(url, cafile=certifi.where())
    data = response.read().decode("utf-8")
    return json.loads(data)


# Function to get the income statement and extract the required fields


def get_inc_stmnt(company, apiKey):
    url = f"https://www.alphavantage.co/query?function=INCOME_STATEMENT&symbol={company}&apikey={apiKey}"

    response = requests.get(url, timeout=20)
    data = response.json()
    incStmnt = data.get("quarterlyReports", [])
    incomeStatement = {}
    operatingIncome = []
    incomeTaxExpense = []
    interestExpense = []
    indx = 0
    for year in range(5):
        for qtr in range(indx + 4):
            yearOperatingIncome = (
                safe_float(incStmnt[indx]["incomeBeforeTax"])
                + safe_float(incStmnt[indx + 1]["incomeBeforeTax"])
                + safe_float(incStmnt[indx + 2]["incomeBeforeTax"])
                + safe_float(incStmnt[indx + 3]["incomeBeforeTax"])
            )

            yearTaxExpense = (
                safe_float(incStmnt[indx]["incomeTaxExpense"])
                + safe_float(incStmnt[indx + 1]["incomeTaxExpense"])
                + safe_float(incStmnt[indx + 2]["incomeTaxExpense"])
                + safe_float(incStmnt[indx + 3]["incomeTaxExpense"])
            )

            yearInterestExpense = (
                safe_float(incStmnt[indx]["interestExpense"])
                + safe_float(incStmnt[indx + 1]["interestExpense"])
                + safe_float(incStmnt[indx + 2]["interestExpense"])
                + safe_float(incStmnt[indx + 3]["interestExpense"])
            )
        operatingIncome.append(yearOperatingIncome)
        # print(f"Net Income = {yearoperatingIncome}")
        # print(f"Interest Income = {yearIntIncome}")
        # totalRevenue.append(yearTotalRevenue)
        # print(f"Total Revenue = {yearTotalRevenue}")
        # incomeBeforeTax.append(yearIncBeforeTax)
        # print(f"Income Before Tax = {yearIncBeforeTax}")
        incomeTaxExpense.append(yearTaxExpense)
        interestExpense.append(yearInterestExpense)
        # print(f"Tax Expense = {yearTaxExpense}")
        # ebit.append(yearEbit)
        # print(f"EBIT = {yearEbit}")
        indx += 4
        if indx > 16:
            break
    incomeStatement["operating_income"] = operatingIncome
    incomeStatement["income_tax_expense"] = incomeTaxExpense
    incomeStatement["interest_expense"] = interestExpense
    # print("incomeTaxExpense")
    # print(incomeStatement["operatingIncome"])

    return incomeStatement


# Function to get the balance sheet and extract the required fields


def get_bal_sheet(company, apiKey):
    url = f"https://www.alphavantage.co/query?function=BALANCE_SHEET&symbol={company}&apikey={apiKey}"

    data = get_jsonparsed_data(url)
    balSheet = data.get("quarterlyReports", [])
    balSht = {}
    cashAndEquivalents = [
        safe_float(balSheet[0]["cashAndShortTermInvestments"]),
        safe_float(balSheet[4]["cashAndShortTermInvestments"]),
        safe_float(balSheet[8]["cashAndShortTermInvestments"]),
        safe_float(balSheet[12]["cashAndShortTermInvestments"]),
        safe_float(balSheet[16]["cashAndShortTermInvestments"]),
    ]
    currentAssets = [
        safe_float(balSheet[0]["totalCurrentAssets"]),
        safe_float(balSheet[4]["totalCurrentAssets"]),
        safe_float(balSheet[8]["totalCurrentAssets"]),
        safe_float(balSheet[12]["totalCurrentAssets"]),
        safe_float(balSheet[16]["totalCurrentAssets"]),
    ]
    # totalAssets = [
    #     qrtrlyData[0]["totalAssets"],
    #     qrtrlyData[4]["totalAssets"],
    #     qrtrlyData[8]["totalAssets"],
    #     qrtrlyData[12]["totalAssets"],
    #     qrtrlyData[16]["totalAssets"],
    # ]
    # accountsPayable = [
    #     qrtrlyData[0]["currentAccountsPayables"],
    #     qrtrlyData[4]["currentAccountsPayables"],
    #     qrtrlyData[8]["currentAccountsPayables"],
    #     qrtrlyData[12]["currentAccountsPayables"],
    #     qrtrlyData[16]["currentAccountsPayables"],
    # ]
    stockholdersEquity = [
        safe_float(balSheet[0]["totalShareholderEquity"]),
        safe_float(balSheet[4]["totalShareholderEquity"]),
        safe_float(balSheet[8]["totalShareholderEquity"]),
        safe_float(balSheet[12]["totalShareholderEquity"]),
        safe_float(balSheet[16]["totalShareholderEquity"]),
    ]
    currentLiabilities = [
        safe_float(balSheet[0]["totalCurrentLiabilities"]),
        safe_float(balSheet[4]["totalCurrentLiabilities"]),
        safe_float(balSheet[8]["totalCurrentLiabilities"]),
        safe_float(balSheet[12]["totalCurrentLiabilities"]),
        safe_float(balSheet[16]["totalCurrentLiabilities"]),
    ]
    currentLongDebt = [
        safe_float(balSheet[0]["currentLongTermDebt"]),
        safe_float(balSheet[4]["currentLongTermDebt"]),
        safe_float(balSheet[8]["currentLongTermDebt"]),
        safe_float(balSheet[12]["currentLongTermDebt"]),
        # safe_float(balSheet[16]["currentLongTermDebt"]),

    ]
    shortTermDebt = [
        safe_float(balSheet[0]["shortTermDebt"]),
        safe_float(balSheet[4]["shortTermDebt"]),
        safe_float(balSheet[8]["shortTermDebt"]),
        safe_float(balSheet[12]["shortTermDebt"]),
        safe_float(balSheet[16]["shortTermDebt"]),
    ]
    longTermDebt = [
        safe_float(balSheet[0]["longTermDebt"]),
        safe_float(balSheet[4]["longTermDebt"]),
        safe_float(balSheet[8]["longTermDebt"]),
        safe_float(balSheet[12]["longTermDebt"]),
        safe_float(balSheet[16]["longTermDebt"]),
    ]
    balSht["cash_and_equivalents"] = cashAndEquivalents
    balSht["total_current_assets"] = currentAssets
    # balSht["totalAssets"] = totalAssets
    # balSht["accountsPayable"] = accountsPayable
    balSht["current_long_debt"] = currentLongDebt
    balSht["short_term_debt"] = shortTermDebt
    balSht["long_term_debt"] = longTermDebt
    balSht["total_current_liabilities"] = currentLiabilities
    # balSht["totalLiabilities"] = liabilities
    balSht["total_stockholders_equity"] = stockholdersEquity
    return balSht


# Function to get the cash flow statement and extract the required fields


def get_cash_flow(company, apiKey):
    url = f"https://www.alphavantage.co/query?function=CASH_FLOW&symbol={company}&apikey={apiKey}"

    data = get_jsonparsed_data(url)
    cashFlw = data.get("quarterlyReports", [])
    cashFlow = {}
    depreciation = []
    capex = []
    # acquisition = []
    # stockBuyBack = []
    # dividends = []
    indx = 0
    for year in range(5):
        for qtr in range(indx + 4):
            yearCapex = (
                safe_float(cashFlw[indx]["capitalExpenditures"])
                + safe_float(cashFlw[indx + 1]["capitalExpenditures"])
                + safe_float(cashFlw[indx + 2]["capitalExpenditures"])
                + safe_float(cashFlw[indx + 3]["capitalExpenditures"])
            )

            yearDeprec = (
                safe_float(
                    cashFlw[indx]["depreciationDepletionAndAmortization"])
                + safe_float(cashFlw[indx + 1]
                             ["depreciationDepletionAndAmortization"])
                + safe_float(cashFlw[indx + 2]
                             ["depreciationDepletionAndAmortization"])
                + safe_float(cashFlw[indx + 3]
                             ["depreciationDepletionAndAmortization"])
            )
            # yearAcquisition = (
            #     qrtrlyData[indx]["acquisitionsNet"]
            #     + qrtrlyData[indx + 1]["acquisitionsNet"]
            #     + qrtrlyData[indx + 2]["acquisitionsNet"]
            #     + qrtrlyData[indx + 3]["acquisitionsNet"]
            # )
            # yearStockBuyBack = (
            #     qrtrlyData[indx]["paymentForRepurchaseOfCommonStock"]
            #     + qrtrlyData[indx + 1]["paymentForRepurchaseOfCommonStock"]
            #     + qrtrlyData[indx + 2]["paymentForRepurchaseOfCommonStock"]
            #     + qrtrlyData[indx + 3]["paymentForRepurchaseOfCommonStock"]
            # )
            # yearDividends = (
            #     qrtrlyData[indx]["dividendPayout"]
            #     + qrtrlyData[indx + 1]["dividendPayout"]
            #     + qrtrlyData[indx + 2]["dividendPayout"]
            #     + qrtrlyData[indx + 3]["dividendPayout"]
            # )
        capex.append(yearCapex)
        depreciation.append(yearDeprec)
        # acquisition.append(yearAcquisition)
        # stockBuyBack.append(yearStockBuyBack)
        # dividends.append(yearDividends)
        indx += 4
        if indx > 16:
            break

    cashFlow["depreciation"] = depreciation
    cashFlow["capex"] = capex
    # cshFlw["acquisition"] = acquisition
    # cshFlw["stockBuyBack"] = stockBuyBack
    # cshFlw["dividendsPaid"] = dividends

    return cashFlow

# function to retrieve R&D expense so we can capitalize it


def get_rAndD(company, rd_years, apiKey):
    url = f"https://www.alphavantage.co/query?function=INCOME_STATEMENT&symbol={company}&apikey={apiKey}"

    response = requests.get(url, timeout=20)
    data = response.json()
    rdExpense = data.get("quarterlyReports", [])
    rdTable = {}
    rd_Amount = []
    indx = 0
    for year in range(rd_years):
        for qtr in range(indx + 4):
            try:
                yearRDExpense = (
                    safe_float(rdExpense[indx]["researchAndDevelopment"])
                    + safe_float(rdExpense[indx + 1]["researchAndDevelopment"])
                    + safe_float(rdExpense[indx + 2]["researchAndDevelopment"])
                    + safe_float(rdExpense[indx + 3]["researchAndDevelopment"])
                )

                rd_Amount.append(yearRDExpense)
                indx += 4
                if indx > 16:
                    break
            except ValueError:
                yearRDExpense = 0.00
                rd_Amount.append(yearRDExpense)

                indx += 4
                if indx > 16:
                    break
    rdTable["research_and_development"] = rd_Amount
    # print("Type RD Amount", type(rd_Amount[0]))
    # print("rdTable", rdTable)

    return rdTable


# Function to get the current share price, shares outstanding, and market cap


def get_quote(company, apiKey):
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
    entQuote = price, sharesOutstanding, marketCap
    return entQuote


def get_risk_free(FRED_KEY):
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "GS10",
        "api_key": FRED_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1
    }
    # Fetch data
    response = requests.get(url, params=params, timeout=20)

    if response.status_code != 200:
        print(f"Error: Received status code {response.status_code}")
        return None

    # Parse JSON response
    data = response.json()
    RISK_FREE = safe_float(data["observations"][0]["value"]) / 100
    print(RISK_FREE)
    return RISK_FREE


def get_industry(company):
    indName = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/indname.xlsx", sheet_name="US by industry")
    for index, row in indName.iterrows():
        try:
            if company == row["Exchange:Ticker"].split(":")[1]:
                industry = row["Industry Group"]
                print(f"Industry Group {industry}")
            else:
                continue
        except TypeError:
            continue
        except AttributeError:
            continue
    return industry


def get_beta(industry):

    beta = pd.read_excel(
        "/Users/jhess/Development/Alpha2/data/betas.xlsx", sheet_name="Industry Averages", skiprows=9)

    for index, row in beta.iterrows():
        try:

            if industry in row["Industry Name"]:
                unleveredBeta = row["Unlevered beta corrected for cash"]
            else:
                continue
        except TypeError:
            continue
    print(f"Beta {unleveredBeta}")
    return unleveredBeta


def get_default_spread(intCover):
    defaultSpread = pd.read_excel(
        "/Users/jhess/Documents/Investing/Damodaran Reference/defaultSpread.xlsx"
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
        sheet_name="Amort Years",)

    for index, row in amortYears.iterrows():
        try:
            if industry == row["Industry"]:
                rAndD_years = row["Years"]
                print(f"Years = {rAndD_years}")
            else:
                continue
        except TypeError:
            continue
        except AttributeError:
            continue

    return rAndD_years
