"""

    This Module calculates a value for a common stock using a Discounted Cash Flow.  The specific cash flow calculated is a Free Cash Flow to the Firm.  The valuation is then calcuated by adding back the cash and equivalents to the enterprise value and subtracting out debt.

"""

import csv
import hg_dcflib

EQ_PREM = 0.0441    # Damodaran 05/01/2025
MARGINAL_TAX_RATE = 0.26
STABLE_BETA = 1.0


def calcCapitalExpenditures(cash_flw):
    capex = (cash_flw["capex"][0] + cash_flw["capex"][1] + cash_flw["capex"]
             [2] + cash_flw["capex"][3] + cash_flw["capex"][4]) / 5

    return (capex)


def capitalizerAndD(company, rd_years, MY_API_KEY):
    rdTable = hg_dcflib.get_rAndD(company, rd_years, MY_API_KEY)
    rd_table = {}
    rd_expense = []
    unamort_percent = []
    unamort_amt = []
    curr_year_amortization = []
    amort_percentage = 1.0 / rd_years
    rd_asset_value = 0
    rd_amort_amt = 0
    for year in range(rd_years):
        # print(year, rdTable['researchAndDevelopment'][year])
        rd_expense.append(rdTable["researchAndDevelopment"][year])
        unamort_percent.append(1.0 - (1.0 / rd_years * year))
        unamort_amt.append(rd_expense[year] * unamort_percent[year])
        if year == 0:
            curr_year_amortization.append(0.00)
        else:
            curr_year_amortization.append(
                rd_expense[year] * amort_percentage)

        rd_asset_value += unamort_amt[year]
        rd_amort_amt += curr_year_amortization[year]
    rd_table["rAndDExpense"] = rd_expense
    rd_table["unamortized_percent"] = unamort_percent
    rd_table["unamort_amount"] = unamort_amt
    rd_table["amort_amt"] = curr_year_amortization
    rd_table["RD_Asset_Value"] = rd_asset_value
    rd_table["RD_Amortization_Amt"] = rd_amort_amt

    return rd_table
    # Integrate RandD.py into this code


def main():
    company = input("Input company ticker: ").upper()
    print(company)

    industry = hg_dcflib.get_industry(company)
    print(f"industry: {industry}")
    with open("data/ApiKey.txt") as f:
        MY_API_KEY = f.readline()
    rd_years = hg_dcflib.get_rAndD_years(industry) + 1
    unlevered_beta = hg_dcflib.get_beta(industry)
    with open("data/fred_api.txt") as f:
        FRED_KEY = f.readline()
    risk_free = hg_dcflib.get_risk_free(FRED_KEY)
    print(risk_free)
    growth_period = int(input("Input growth period: "))
    # long term a company can't grow faster than the economy in which it operates
    stable_growth = risk_free  # CAGR last 10 years
    # the unlevered beta for the industry in which the firm opeerates

    inc_stmnt = hg_dcflib.get_inc_stmnt(company, MY_API_KEY)

    with open(f"data/{company}inc_stmnt.csv", "w", newline="") as f:
        w = csv.DictWriter(f, inc_stmnt.keys())
        w.writeheader()
        w.writerow(inc_stmnt)

    bal_Sht = hg_dcflib.get_bal_sheet(company, MY_API_KEY)

    with open(f"data/{company}bal_Sht.csv", "w", newline="") as f:
        w = csv.DictWriter(f, bal_Sht.keys())
        w.writeheader()
        w.writerow(bal_Sht)

    cash_flw = hg_dcflib.get_cash_flow(company, MY_API_KEY)

    with open(f"data/{company}cashFlw.csv", "w") as f:
        w = csv.DictWriter(f, cash_flw.keys())
        w.writeheader()
        w.writerow(cash_flw)

    amort_schedule = capitalizerAndD(company, rd_years, MY_API_KEY)
    print("Amortization Schedule", amort_schedule)

    ent_quote = hg_dcflib.get_quote(company, MY_API_KEY)
    print(ent_quote)
    price = ent_quote[0]
    shares_outstanding = ent_quote[1]
    market_cap = ent_quote[2]

    ebit = inc_stmnt["operatingIncome"][0] + \
        amort_schedule["rAndDExpense"][0] - \
        amort_schedule["RD_Amortization_Amt"]
    capex = calcCapitalExpenditures(cash_flw)
    deprec = (cash_flw["depreciation"][0])
    inc_tax = inc_stmnt["incomeTaxExpense"][0]
    eff_tax_rate = inc_tax / ebit
    book_value_debt = (bal_Sht["shortTermDebt"][0]) + (bal_Sht["longTermDebt"]
                                                       [0] + bal_Sht["shortTermDebt"][1] + bal_Sht["longTermDebt"][1])/2
    print("book_value_debt")
    print(bal_Sht["shortTermDebt"][0], bal_Sht["longTermDebt"][0])
    book_value_equity = ((bal_Sht["totalStockholdersEquity"][0] +
                          bal_Sht["totalStockholdersEquity"][1])/2) + amort_schedule["RD_Asset_Value"]
    cash = (bal_Sht["cashAndEquivalents"][0])

    # Calculate Current Year Non Cash Working Capital
    # Calculate a moving average change in working capital
    curr_year_working_cap = (
        bal_Sht["totalCurrentAssets"][0] - bal_Sht["cashAndEquivalents"][0]
    ) - (bal_Sht["totalCurrentLiabilities"][0] - bal_Sht["shortTermDebt"][0] - bal_Sht["currentLongDebt"][0])
    print(f"Current Working Capital = {curr_year_working_cap:,.2f}")

    # Calculate Prior Year Non Cash Working Capitalß
    prior_year_working_cap = (
        bal_Sht["totalCurrentAssets"][1] - bal_Sht["cashAndEquivalents"][1]
    ) - (bal_Sht["totalCurrentLiabilities"][1] - bal_Sht["shortTermDebt"][1] - bal_Sht["currentLongDebt"][1])
    print(f"Prior Year Working Capital = {prior_year_working_cap:,.2f}")

    # Calculate Change in Non Cash Working Capital
    change_working_cap = curr_year_working_cap - prior_year_working_cap

    # inc_stmnt.to_csv(f"{company}_incomeStatement.csv")
    # bal_Sht.to_csv(f"{company}_balanceSheet.csv")
    # cash_flw.to_csv(f"{company}_cashflowStatement.csv")

    # Calculate Free Cash Flow to the Firm
    ebiat = ebit - inc_tax
    fcff = ebiat - capex + deprec - change_working_cap
    print(f"Operating Income = {ebit:,.2f}")
    print(f"EBIAT = {ebiat:,.2f}")
    print(f"Capex = {capex:,.2f}")
    print(f"Depreciation = {deprec:,.2f}")
    print(f"Change in Working Capital = {change_working_cap:,.2f}")
    print(f"Free Cash Flow to Firm = {fcff:,.2f}")
    print(f"Effective Tax Rate = {eff_tax_rate}")
    print(f"Income Tax = {inc_tax:,.2f}")

    # Calculate Griwth Rate in Operating Income
    firmReinvestment = capex - deprec + change_working_cap + \
        amort_schedule["rAndDExpense"][0] - \
        amort_schedule["RD_Amortization_Amt"]
    print(f"Reinvestment = {firmReinvestment:,.2f}")
    reinvestmentRate = firmReinvestment / (ebit * (1 - eff_tax_rate))
    print(f"Reinvestment Rate = {reinvestmentRate}")
    print(book_value_debt, book_value_equity, cash)
    ROC = (ebit * (1 - eff_tax_rate)) / \
        (book_value_debt + book_value_equity - cash)
    print(f"Return on Capital = {ROC}")
    expGrowthebit = reinvestmentRate * ROC
    print(f"Expected Growth in Op Income = {expGrowthebit}")

    # Calculate Expected Free Cash Flow to Firm
    expectedFCFF = []
    for year in range(growth_period):
        if year == 0:
            expectedFCFF.append(fcff * (1 + expGrowthebit))
        else:
            expectedFCFF.append(expectedFCFF[year - 1] * (1 + expGrowthebit))
    for fcff in expectedFCFF:
        print(f"Expected FCFF = {fcff:,.2f}")

    # print(f"Expected FCFF = {expectedFCFF}")

    # Calculate Cost of Capital -> Discount Rate
    # 1. Calculate Interest COverage
    intCover = ebit / inc_stmnt["incomeTaxExpense"][0]
    print(f"Interest Coverage = {intCover}")
    defSpread = hg_dcflib.get_default_spread(intCover)
    print(f"Default Spread = {defSpread}")
    # 2. Calcultate after tax cost of debt
    costOfDebt = (risk_free + defSpread) * (1 - MARGINAL_TAX_RATE)
    print(f"Cost of Debt = {costOfDebt}")
    # 3. Calculate cost of equity
    costOfEquity = risk_free + (unlevered_beta * EQ_PREM)
    print(f"Cost of Equity = {costOfEquity}")
    totalCap = book_value_debt + market_cap
    percentDebt = book_value_debt / totalCap
    percentEquity = market_cap / totalCap
    costOfCapital = (costOfDebt * percentDebt) + (costOfEquity * percentEquity)
    print(f"Cost of Capital = {costOfCapital}")
    # Calculate the termnal value of the firm
    # 1.  After Taxx Operating Income in the year following the last growth year
    #       = Cash flow in year n + 1 / (discount rate - perpetual growth rate)
    #       cash flow in year n+1 = cash flow in year n * (1 + perpetual growth rate)
    #       discount rate = cost of capital recalculs=ated to reflect stable period cost of equity (beta changes)
    stableCostOfEquity = risk_free + (STABLE_BETA * EQ_PREM)
    stableCostOfCapital = (costOfDebt * percentDebt) + (
        stableCostOfEquity * percentEquity
    )
    terminalFCFF = (expectedFCFF[-1] * (1 + stable_growth)) / (
        stableCostOfCapital - stable_growth
    )
    print(f"Terminal Cash Flow to Firm = {terminalFCFF:,.2f}")

    # Calculate Firm Value = present value of growth period free cash flows + present value of the terminal cash flow
    firmValue = 0
    for year in range(growth_period):
        presentValue = expectedFCFF[year] / ((1 + costOfCapital) ** (year + 1))
        firmValue += presentValue
        if year == growth_period - 1:
            presentValueOfTerminalValue = terminalFCFF / (
                (1 + costOfCapital) ** (year + 1)
            )
            firmValue += presentValueOfTerminalValue
    print(f"Firm Value = {firmValue:,.2f}")

    # Calculate the value of equity = firm value - debt + cash
    equityValue = firmValue - book_value_debt + cash
    print(f"Debt = {book_value_debt:,.2f}")
    print(f"Cash = {cash:,.2f}")
    print(f"Equity Value = {equityValue:,.2f}")
    print(f"Shares Outstanding = {shares_outstanding:,.2f}")

    valuePerShare = equityValue / shares_outstanding
    print(f"Value per share = {valuePerShare:,.2f}")
    print(f"Price per share = {price:,.2f}")
    print(f"Over/Under Value = {(price / valuePerShare)-1:,.2f}")


if __name__ == "__main__":
    main()
