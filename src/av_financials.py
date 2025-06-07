"""
This program is used to access Alpha Vantage to extract the specific data used in the DCF analysis.
Sources: Income Ststement, Balance Sheet and Cashflow Statement
"""

import csv
import hg_dcflib


def write_csv(statement, company, name):
    with open(f"data/{company}_{name}.csv", "w") as f:
        w = csv.DictWriter(f, statement.keys())
        w.writeheader()
        w.writerow(statement)

def main():
    company = input("Enter company ticker: ")
    with open("/Users/jhess/Development/Alpha2/data/ApiKey.txt") as f:
        MY_API_KEY = f.readline()

    inc_stmnt = hg_dcflib.get_inc_stmnt(company, MY_API_KEY)
    write_csv(inc_stmnt, company,"inc_stmnt")
    bal_sht = hg_dcflib.get_bal_sheet(company, MY_API_KEY)
    write_csv(bal_sht, company, "bal_sht")
    cash_flw = hg_dcflib.get_cash_flow(company, MY_API_KEY)
    write_csv(cash_flw, company, "csh_flow")

if __name__ == "__main__":
    main()



