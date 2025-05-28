# %% [markdown]
# This Notebook will be used to refactor my av_fcff.py module to a more correct form
#

# %%
from dataclasses import dataclass
from typing import Optional
from datetime import date
import csv
import hg_dcflib

# %% [markdown]
# ## Define the constants used in the module

# %%
EQ_PREM = 0.0441  # Damodaran 05/01/2025
MARGINAL_TAX_RATE = 0.26
STABLE_BETA = 1.0
COMPANY = input("Input company ticker: ").upper()
GROWTH_PERIOD = int(input("Input growth period: "))
INDUSTRY = hg_dcflib.get_industry(COMPANY)
with open("/Users/jhess/Development/Alpha2/data/ApiKey.txt") as f:
    MY_API_KEY = f.readline()
with open("/Users/jhess/Development/Alpha2/data/fred_api.txt") as f:
    FRED_KEY = f.readline()
RD_YEARS = hg_dcflib.get_rAndD_years(INDUSTRY) + 1
UNLEVERED_BETA = hg_dcflib.get_beta(INDUSTRY)
RISK_FREE = hg_dcflib.get_risk_free(FRED_KEY)

# %% [markdown]
# ## Class for Valuation


# %%
@dataclass
class Stock_Value:
    valuation_date: str
    ticker: str
    industry: str
    beta: float
    price: float
    shares_outstanding: float
    risk_free_rate: float
    eq_premium: float
    #
    # calcukated values in dataclass methods
    growth_rate: float = None
    fcff_value: float = None
    terminal_value: float = None
    ent_value: float = None
    share_value: float = None
    margin_of_safety: float = None


# %% [markdown]
# ## Functions
#


# %%
def income_statement(COMPANY, MY_API_KEY):
    inc_stmnt = hg_dcflib.get_inc_stmnt(COMPANY, MY_API_KEY)
    # with open(f"data/{COMPANY}inc_stmnt.csv", "w", newline="") as f:
    #     w = csv.DictWriter(f, inc_stmnt.keys())
    #     w.writeheader()
    #     w.writerow(inc_stmnt)
    return inc_stmnt


def balance_sheet(COMPANY, MY_API_KEY):
    bal_sht = hg_dcflib.get_bal_sheet(COMPANY, MY_API_KEY)
    # with open(f"data/{COMPANY}bal_Sht.csv", "w", newline="") as f:
    #     w = csv.DictWriter(f, bal_Sht.keys())
    #     w.writeheader()
    #     w.writerow(bal_Sht)
    return bal_sht


def cash_flow_statement(COMPANY, MY_API_KEY):
    cash_flw = hg_dcflib.get_cash_flow(COMPANY, MY_API_KEY)
    # with open(f"data/{COMPANY}cashFlw.csv", "w") as f:
    #     w = csv.DictWriter(f, cash_flw.keys())
    #     w.writeheader()
    #     w.writerow(cash_flw)
    return cash_flw


def enterprise_quote(COMPANY, MY_API_KEY):
    ent_quote = hg_dcflib.get_quote(COMPANY, MY_API_KEY)
    return ent_quote


def calc_capital_expenditures(cash_flw):
    capex = (
        cash_flw["capex"][0]
        + cash_flw["capex"][1]
        + cash_flw["capex"][2]
        + cash_flw["capex"][3]
        + cash_flw["capex"][4]
    ) / 5
    return capex


def calc_chng_wc(bal_sht):
    curr_yr_nc_wc = (
        bal_sht["total_current_assets"][0] - bal_sht["cash_and_equivalents"][0]
    ) - (
        bal_sht["total_current_liabilities"][0]
        - bal_sht["short_term_debt"][0]
        - bal_sht["current_long_debt"][0]
    )
    prior_yr_nc_wc = (
        bal_sht["total_current_assets"][1] - bal_sht["cash_and_equivalents"][1]
    ) - (
        bal_sht["total_current_liabilities"][1]
        - bal_sht["short_term_debt"][1]
        - bal_sht["current_long_debt"][1]
    )
    chng_nc_wc = curr_yr_nc_wc - prior_yr_nc_wc
    return chng_nc_wc


def capitalizerAndD(COMPANY, RD_YEARS, MY_API_KEY):
    rdTable = hg_dcflib.get_rAndD(COMPANY, RD_YEARS, MY_API_KEY)
    rd_table = {}
    rd_expense = []
    unamort_percent = []
    unamort_amt = []
    curr_year_amortization = []
    amort_percentage = 1.0 / RD_YEARS
    rd_asset_value = 0
    rd_amort_amt = 0
    for year in range(RD_YEARS):
        # print(year, rdTable['researchAndDevelopment'][year])
        rd_expense.append(rdTable["research_and_development"][year])
        unamort_percent.append(1.0 - (1.0 / RD_YEARS * year))
        unamort_amt.append(rd_expense[year] * unamort_percent[year])
        if year == 0:
            curr_year_amortization.append(0.00)
        else:
            curr_year_amortization.append(rd_expense[year] * amort_percentage)

        rd_asset_value += unamort_amt[year]
        rd_amort_amt += curr_year_amortization[year]
    rd_table["rAndDExpense"] = rd_expense
    rd_table["unamortized_percent"] = unamort_percent
    rd_table["unamort_amount"] = unamort_amt
    rd_table["amort_amt"] = curr_year_amortization
    rd_table["RD_Asset_Value"] = rd_asset_value
    rd_table["RD_Amortization_Amt"] = rd_amort_amt

    return rd_table


def calc_fcff(inc_stmnt, bal_sht, cash_flw):
    ebiat = inc_stmnt["operating_income"][0] - inc_stmnt["income_tax_expense"][0]
    print(f"ebiat {ebiat:,.2f}")
    capex = calc_capital_expenditures(cash_flw)
    print(f"Capex {capex:,.2f}")
    chng_nc_wc = calc_chng_wc(bal_sht)
    print(f"Change WC {chng_nc_wc:,.2f}")
    depreciation = cash_flw["depreciation"][0]
    print(f"Depreciation {depreciation:,.2f}")
    fcff = ebiat - capex + depreciation + chng_nc_wc
    print(f"FCFF {fcff:,.2f}")
    fcff_data = [ebiat, capex, chng_nc_wc, depreciation, fcff]
    return fcff_data


def calc_reinvestment(ebiat, capex, depreciation, chng_nc_wc, amort_schedule):
    firm_reinvestment = (
        capex
        - depreciation
        + chng_nc_wc
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["RD_Amortization_Amt"]
    )
    print(f"Firm Reinvestment {firm_reinvestment:,.2f}")
    return firm_reinvestment


def calc_adj_ebiat(ebiat, amort_schedule):
    adjusted_ebiat = (
        ebiat
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["RD_Amortization_Amt"]
    )
    print(f"Adjusted ebiat {adjusted_ebiat:,.2f}")
    return adjusted_ebiat


def calc_adj_bv_equity(bal_sht, amort_schedule):
    adjusted_bv_equity = (
        bal_sht["total_stockholders_equity"][0] + amort_schedule["RD_Asset_Value"]
    )
    print(f"adjusted BV Equity = {adjusted_bv_equity:,.2f}")
    return adjusted_bv_equity


def calc_adj_bv_debt(bal_sht):
    adj_bv_debt = (
        bal_sht["current_long_debt"][0]
        + bal_sht["short_term_debt"][0]
        + bal_sht["long_term_debt"][0]
        - bal_sht["cash_and_equivalents"][0]
    )
    return adj_bv_debt


def calc_tax_rate(inc_stmnt):
    eff_tax_rate = inc_stmnt["income_tax_expense"][0] / inc_stmnt["operating_income"][0]
    print(f"Effective Tax Rate = {eff_tax_rate:,.4f}")
    return eff_tax_rate


def calc_return_on_capital(adjusted_ebiat, adjusted_bv_equity, adj_bv_debt):
    return_on_capital = adjusted_ebiat / (adjusted_bv_equity + adj_bv_debt)
    print(f"ROIC = {return_on_capital:,.4f}")
    return return_on_capital


def calc_growth_rate(reinvestment_rate, return_on_capital):
    growth_rate = reinvestment_rate * return_on_capital
    print(f"Growth Rate = {growth_rate:,.4f}")
    return growth_rate


def calc_discount_rate(
    EQ_PREM, UNLEVERED_BETA, RISK_FREE, inc_stmnt, adjusted_bv_debt, adjusted_bv_equity
):
    # Discount rate for free cah flow to the firm = cost of capital
    # The cost of capital is the weighted average of the cost of equity and the cost of debt
    # Cost of equity = risk free rate + Beta(Implied Equity Risk Premium)

    cost_of_equity = RISK_FREE + UNLEVERED_BETA * EQ_PREM
    print(f"COE = {cost_of_equity:,.4}")

    # Cost of debt = risk free rate + default spread * (1 - marginal tax rate)
    # 1. Calculate Interest Coverage

    # if inc_stmnt["interest_expense"][0] != 0:
    #      int_cover = inc_stmnt["operating_income"][0] / inc_stmnt["interest_expense"][0]
    # else:
    #     int_cover = 25

    try:
        int_cover = inc_stmnt["operating_income"][0] / inc_stmnt["interest_expense"][0]
    except ZeroDivisionError:
        int_cover = 25  # forces default spread to the lowest level

    print(f"Interest Coverage = {int_cover}")
    def_spread = hg_dcflib.get_default_spread(int_cover)
    print(f"Default Spread = {def_spread}")

    # 2. Calcultate after tax cost of debt
    cost_of_debt = (RISK_FREE + def_spread) * (1 - MARGINAL_TAX_RATE)
    print(f"Cost of Debt = {cost_of_debt}")
    percent_debt = adjusted_bv_debt / (adjusted_bv_equity + adjusted_bv_debt)
    percent_equity = 1 - percent_debt
    print(f"% Debt {percent_debt:,.4}")
    print(f"% Equity {percent_equity:,.4}")

    # 3 calcualte the weighted cost of capital
    cost_of_capital = (cost_of_debt * percent_debt) + (cost_of_equity * percent_equity)

    return cost_of_capital


def calc_pv_fcff(curr_yr_fcff, growth_rate, GROWTH_PERIOD, discount_rate):
    print("IN calc_pv")

    fcff_table = []
    pv_fcff = 0
    for year in range(GROWTH_PERIOD):
        if year == 0:
            fcff_table.append(curr_yr_fcff * (1 + growth_rate))
        else:
            fcff_table.append(fcff_table[year - 1] * (1 + growth_rate))
    for val in fcff_table:
        print(f"Expected FCFF = {val:,.2f}")

    return pv_fcff


# %% [markdown]
# ## Main() Function

# %%


def main():
    inc_stmnt = income_statement(COMPANY, MY_API_KEY)
    print(f"Inc Stmnt {inc_stmnt}")
    bal_sht = balance_sheet(COMPANY, MY_API_KEY)
    print(f"Bal Sheet {bal_sht}")
    cash_flw = cash_flow_statement(COMPANY, MY_API_KEY)
    print(f"Cash Flow {cash_flw}")
    ent_quote = enterprise_quote(COMPANY, MY_API_KEY)
    print(f"Ent Quote {ent_quote}")
    valuation_date = date.today()
    price = ent_quote[0]
    shares_outstanding = ent_quote[1]
    market_cap = ent_quote[2]
    fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw)

    ebiat = fcff_data[0]
    capex = fcff_data[1]
    chng_nc_wc = fcff_data[2]
    depreciation = fcff_data[3]
    curr_yr_fcff = fcff_data[4]

    amort_schedule = capitalizerAndD(COMPANY, RD_YEARS, MY_API_KEY)
    print(f"Amortization Schedule {amort_schedule}")
    firm_reinvestment = calc_reinvestment(
        ebiat, capex, depreciation, chng_nc_wc, amort_schedule
    )
    adjusted_ebiat = calc_adj_ebiat(ebiat, amort_schedule)
    adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
    adjusted_bv_debt = calc_adj_bv_debt(bal_sht)
    reinvestment_rate = firm_reinvestment / adjusted_ebiat
    print(f"Reinvestment rate = {reinvestment_rate:,.4f}")
    try:
        valuation = Stock_Value(
            valuation_date,
            COMPANY,
            INDUSTRY,
            UNLEVERED_BETA,
            price,
            shares_outstanding,
            RISK_FREE,
            EQ_PREM,
        )
        print(valuation)
    except Exception as e:
        print("An exception occured: ", e)

    eff_tax_rate = calc_tax_rate(inc_stmnt)
    return_on_capital = calc_return_on_capital(
        adjusted_ebiat, adjusted_bv_equity, adjusted_bv_debt
    )
    growth_rate = calc_growth_rate(reinvestment_rate, return_on_capital)
    discount_rate = calc_discount_rate(
        EQ_PREM,
        UNLEVERED_BETA,
        RISK_FREE,
        inc_stmnt,
        adjusted_bv_equity,
        adjusted_bv_debt,
    )
    print(f"disc rate {discount_rate:,.4}")

    print("ENTER pv_fcff")
    pv_fcff = calc_pv_fcff(curr_yr_fcff, growth_rate, GROWTH_PERIOD, discount_rate)

    print(pv_fcff)
    # import traceback

    # print("✅ Start valuation process")

    # try:
    #     print(f"disc rate {discount_rate:,.4f}")
    #     print(
    #         f"Inputs: {curr_yr_fcff}, {growth_rate}, {GROWTH_PERIOD}, {discount_rate}"
    #     )

    #     print("➡️ Before calling calc_pv_fcff")
    #     pv_fcff = calc_pv_fcff(curr_yr_fcff, growth_rate, GROWTH_PERIOD, discount_rate)
    #     print("✅ Successfully called calc_pv_fcff")

    #     print(pv_fcff)
    #     print(valuation.valuation_date)
    #     print(valuation.ticker)
    #     print(f"{valuation.shares_outstanding:,}")

    #     print("🎉 DONE")
    # except Exception as e:
    #     print("💥 ERROR during valuation process:")
    #     traceback.print_exc()

    print(valuation.valuation_date)
    print(valuation.ticker)
    print(f"{valuation.shares_outstanding:,}")

    print("DONE")


# %%
if __name__ == "__main__":
    main()
