"""
Earnings whisper truth--
Price dances with hope and fear,
Worth hides in the mist.
"""

from dataclasses import dataclass
from datetime import date
import sqlite3
import hg_dcflib
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)  # Set the overall logger level

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Create a stream handler for the console
stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.WARNING)  # only show INFO and above on console
stream_handler.setFormatter(formatter)

# Create a FileHandler for the log file
file_handler = logging.FileHandler("data/value.log")
file_handler.setLevel(logging.DEBUG)  # log all messages to the file
file_handler.setFormatter(formatter)

# Add the handlers to the logger
logger.addHandler(stream_handler)
logger.addHandler(file_handler)


# ## Define the constants used in the module

EQ_PREM = hg_dcflib.get_erp()
MARGINAL_TAX_RATE = 0.26
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


# ## Class for Valuation
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

    # calcukated values in dataclass methods
    growth_rate: float
    cost_of_capital: float
    wealth_pc: float
    fcff_value: float
    terminal_value: float
    share_value: float
    margin_of_safety: float
    margin_of_safety_pc: float


# ## Functions


def create_table():
    # conn = sqlite3.connect("/Volumes/Financial Data/valuation.db")
    database = "/Volumes/Financial_Data/valuation.db"
    statements = [
        """CREATE TABLE IF NOT EXISTS valuation (
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
              wealth_pc REAL NO NULL,
              fcff_value REAL NOT NULL,
              terminal_value REAL NOT NULL,
              share_value REAL NOT NULL,
              margin_of_safety REAL NOT NULL,
              margin_of_safety_pc REAL NOT NULL,
              PRIMARY KEY (ticker, valuation_date)
              )
              ;"""
    ]
    try:
        with sqlite3.connect(database) as conn:
            cursor = conn.cursor()
            for statement in statements:
                cursor.execute(statement)
            conn.commit()
            logger.info("Table created successfully")
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to create tables: {e}")


def insert_valuation(conn, val):
    c = conn.cursor()
    c.execute(
        """
              INSERT OR REPLACE INTO valuation (ticker, valuation_date,ent_name, industry, beta, market_cap, price, shares_outstanding, risk_free_rate, eq_premium, growth_rate, cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value, margin_of_safety, margin_of_safety_pc
            )  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              """,
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


def calc_stable_beta(UNLEVERED_BETA):
    if UNLEVERED_BETA < 0.5:
        stable_beta = 0.8
    elif UNLEVERED_BETA > 1.5:
        stable_beta = 1.2
    else:
        stable_beta = 1.0

    logger.info(f"Stable beta = {stable_beta:,.3f}")

    return stable_beta


def calc_capital_expenditures(cash_flw):
    # normalize capex
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
    ) - (bal_sht["total_current_liabilities"][0] - bal_sht["short_term_debt"][0])
    prior_yr_nc_wc = (
        bal_sht["total_current_assets"][1] - bal_sht["cash_and_equivalents"][1]
    ) - (bal_sht["total_current_liabilities"][1] - bal_sht["short_term_debt"][1])
    chng_nc_wc = curr_yr_nc_wc - prior_yr_nc_wc
    return chng_nc_wc


def capitalizerAndD(COMPANY, RD_YEARS, MY_API_KEY):
    rdTable = hg_dcflib.get_rAndD(COMPANY, RD_YEARS, MY_API_KEY)
    rd_dict, years_to_process = rdTable
    logger.info(f"rdTable = {rdTable}")
    logger.info(f"rd_dict = {rd_dict}")
    logger.info(f"Years to Process = {years_to_process}")
    rd_table = {}
    rd_expense = []
    unamort_percent = []
    unamort_amt = []
    amort_percentage = 1.0 / (RD_YEARS - 1)

    # Calculate the current year's total amortization from all past R&D
    current_year_total_amortization = 0
    for year in range(1, min(years_to_process, RD_YEARS)):
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
    fcff_data = [ebiat, capex, chng_nc_wc, depreciation, fcff]
    return fcff_data


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
    # logger.info(f"Current Long Term Debt {bal_sht['short_term_debt'][0]:,.2f}")
    # logger.info(f"Long Term Debt {bal_sht['long_term_debt'][0]:,.2f}")
    # logger.info(f"Cash and Equivalents {bal_sht['cash_and_equivalents'][0]:,.2f}")
    logger.info(f"BV Debt = {bv_debt:,.2f}")
    return bv_debt


def calc_tax_rate(inc_stmnt):
    eff_tax_rate = inc_stmnt["income_tax_expense"][0] / inc_stmnt["incomeBeforeTax"][0]
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


def calc_discount_rate(inc_stmnt, bv_debt, adjusted_bv_equity, beta):
    # Discount rate for free cah flow to the firm = cost of capital
    # The cost of capital is the weighted average of the cost of equity and the cost of debt
    # Cost of equity = risk free rate + Beta(Implied Equity Risk Premium)

    cost_of_equity = RISK_FREE + (beta * EQ_PREM)
    logger.info(f"COE = {cost_of_equity:,.4}")

    try:
        int_cover = inc_stmnt["ebit"][0] / inc_stmnt["interest_expense"][0]
    except ZeroDivisionError:
        int_cover = 25  # forces default spread to the lowest level

    logger.info(f"operating Income {inc_stmnt['ebit'][0]}")
    logger.info(f"interest expense {inc_stmnt['interest_expense'][0]}")
    logger.info(f"Interest Coverage = {int_cover}")
    def_spread = hg_dcflib.get_default_spread(int_cover)
    logger.info(f"Default Spread = {def_spread}")

    # 2. Calcultate after tax cost of debt
    cost_of_debt = (RISK_FREE + def_spread) * (1 - MARGINAL_TAX_RATE)
    logger.info(f"Cost of Debt = {cost_of_debt}")
    percent_debt = bv_debt / (adjusted_bv_equity + bv_debt)
    percent_equity = 1 - percent_debt
    logger.info(f"% Debt {percent_debt:,.4f}")
    logger.info(f"% Equity {percent_equity:,.4f}")

    # 3 calcualte the weighted cost of capital
    cost_of_capital = (cost_of_debt * percent_debt) + (cost_of_equity * percent_equity)
    logger.info(f"Cost of Capital = {cost_of_capital:,.4f}")

    return cost_of_capital


def calc_expected_fcff(adjusted_ebiat, growth_rate, reinvestment_rate):
    # change this calculation to estimate the ebit and the use the reinvestment rate to calculate the expected FCFF

    value_dict = {}
    keys = ["ebiat_n", "fcff_n"]
    for k in keys:
        value_dict[k] = []

    for year in range(GROWTH_PERIOD):
        if year == 0:
            value_dict["ebiat_n"].append(adjusted_ebiat * (1 + growth_rate))
        else:
            value_dict["ebiat_n"].append(
                value_dict["ebiat_n"][year - 1] * (1 + growth_rate)
            )

        value_dict["fcff_n"].append(
            value_dict["ebiat_n"][year] * (1 - reinvestment_rate)
        )
    for val in value_dict["fcff_n"]:
        logger.info(f"Expected FCFF = {val:,.2f}")

    return value_dict["fcff_n"]


def calc_fcff_value(fcff_table, discount_rate):
    fcff_value = 0
    for year in range(GROWTH_PERIOD):
        fcff_pv = fcff_table[year] / ((1 + discount_rate) ** (year + 1))
        fcff_value += fcff_pv
        logger.info(f"Year: {year}")
    logger.info(f"FCFF Value = {fcff_value:,.2f}")
    return fcff_value


def calc_terminal_value(fcff_last, stable_cost_of_capital, growth_cost_of_capital):
    terminal_value = (fcff_last * (1 + RISK_FREE)) / (
        stable_cost_of_capital - RISK_FREE
    )
    terminal_value_pv = terminal_value / ((1 + growth_cost_of_capital) ** GROWTH_PERIOD)
    logger.info(f"Terminal Value = {terminal_value_pv:,.2f}")
    return terminal_value_pv


def calc_intrinsic_value(
    fcff_pv,
    terminal_value_pv,
    cash_and_equivalents,
    bv_debt,
    shares_outstanding,
):
    enterprise_value = fcff_pv + terminal_value_pv + cash_and_equivalents - bv_debt
    intrinsic_value = enterprise_value / shares_outstanding
    logger.info(f"Enterprise Value = {enterprise_value:,.2f}")
    logger.info(f"Intrinsic Value = {intrinsic_value:,.2f}")
    return intrinsic_value


# ## Main() Function


def main():
    inc_stmnt = income_statement(COMPANY, MY_API_KEY)
    logger.info(f"Inc Stmnt {inc_stmnt}")
    bal_sht = balance_sheet(COMPANY, MY_API_KEY)
    logger.info(f"Bal Sheet {bal_sht}")
    cash_flw = cash_flow_statement(COMPANY, MY_API_KEY)
    logger.info(f"Cash Flow {cash_flw}")
    ent_quote = enterprise_quote(COMPANY, MY_API_KEY)
    logger.info(f"Ent Quote {ent_quote}")
    valuation_date = str(date.today())
    # Add exchange to this
    price = ent_quote[0]
    shares_outstanding = ent_quote[1]
    logger.info(f"Shares Outstanding: {shares_outstanding}")
    market_cap = ent_quote[2]
    ent_name = ent_quote[3]
    stable_beta = calc_stable_beta(UNLEVERED_BETA)
    eff_tax_rate = calc_tax_rate(inc_stmnt)
    fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate)

    ebiat = fcff_data[0]
    capex = fcff_data[1]
    chng_nc_wc = fcff_data[2]
    depreciation = fcff_data[3]
    curr_yr_fcff = fcff_data[4]

    amort_schedule = capitalizerAndD(COMPANY, RD_YEARS, MY_API_KEY)
    logger.info(f"Amortization Schedule {amort_schedule}")
    adjusted_ebiat = calc_adj_ebiat(ebiat, amort_schedule)
    firm_reinvestment = calc_reinvestment(
        capex, depreciation, chng_nc_wc, amort_schedule
    )

    adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
    bv_debt = calc_bv_debt(bal_sht)
    reinvestment_rate = firm_reinvestment / adjusted_ebiat
    logger.info(f"Reinvestment rate = {reinvestment_rate:,.4f}")

    return_on_capital = calc_return_on_capital(
        adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht
    )
    growth_rate = calc_growth_rate(reinvestment_rate, return_on_capital)
    discount_rate = calc_discount_rate(
        inc_stmnt, bv_debt, adjusted_bv_equity, UNLEVERED_BETA
    )
    logger.info(f"disc rate {discount_rate:,.4}")

    fcff_table = calc_expected_fcff(adjusted_ebiat, growth_rate, reinvestment_rate)

    fcff_pv = calc_fcff_value(fcff_table, discount_rate)
    terminal_cost_of_capital = calc_discount_rate(
        inc_stmnt, bv_debt, adjusted_bv_equity, stable_beta
    )
    terminal_value_pv = calc_terminal_value(
        fcff_table[-1], terminal_cost_of_capital, discount_rate
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
    if return_on_capital > discount_rate:
        logger.info("Wealth Creator")
    else:
        logger.info("Wealth Detroyer")
    wealth_pc = return_on_capital - discount_rate
    try:
        valuation = Stock_Value(
            COMPANY,
            valuation_date,
            ent_name,
            INDUSTRY,
            UNLEVERED_BETA,
            market_cap,
            price,
            shares_outstanding,
            RISK_FREE,
            EQ_PREM,
            growth_rate,
            discount_rate,
            wealth_pc,
            fcff_pv,
            terminal_value_pv,
            intrinsic_value,
            safety_margin,
            safety_margin_pc,
        )
        logger.info(valuation)
    except Exception as e:
        logger.debug(f"An exception occured: {e}")

    # write to db
    create_table()
    conn = sqlite3.connect("/Volumes/Financial_Data/valuation.db")
    insert_valuation(conn, valuation)

    print("DONE")


if __name__ == "__main__":
    main()
