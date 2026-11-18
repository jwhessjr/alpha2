#!/usr/bin/env python3
"""
composite_score.py — 100-point composite value/quality scorer for portfolio selection.

Joins valuation, moat_scores, lynch_scores, and the Intrinio 40-quarter
financial-statement repository (/Volumes/Financial_Data/financial_data.db, via
lib/financial_data_repository_reader.py) to produce a single ranked score per
ticker based on the HessGroupLLC Value Portfolio framework.

Score structure:
  Tier 1 — Quality & Profitability  50 pts
  Tier 2 — Valuation                30 pts
  Tier 3 — Durability               20 pts
  Total                            100 pts

Binary gates (must pass all to be scored):
  - operating_cf > 0
  - margin_of_safety_pc > 0 and share_value > 0
  - interest_coverage >= 2.0 (skipped if data unavailable)
  - wealth_pc > 0  (ROIC > WACC for FCFF firms; ROE > cost of equity for FCFE firms)

Usage:
  python3 composite_score.py           # score all tickers in valuation.db
  python3 composite_score.py --ticker AAPL
  python3 composite_score.py --min-score 65   # report only Qualified+
"""

import sys
import json
import sqlite3
import argparse
import logging
from datetime import date
from pathlib import Path

_lib = Path(__file__).parent.parent / "lib"
if str(_lib) not in sys.path:
    sys.path.insert(0, str(_lib))
from config import VALUATION_DB
from financial_data_repository_reader import fetch_snapshot_fields_bulk

logging.basicConfig(level=logging.WARNING)
log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Table setup
# ---------------------------------------------------------------------------

def ensure_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS composite_scores (
            ticker                  TEXT PRIMARY KEY,
            score_date              TEXT NOT NULL,
            gate_pass               INTEGER NOT NULL DEFAULT 0,
            gate_fail_reason        TEXT NOT NULL DEFAULT '',
            tier1_score             INTEGER NOT NULL DEFAULT 0,
            tier2_score             INTEGER NOT NULL DEFAULT 0,
            tier3_score             INTEGER NOT NULL DEFAULT 0,
            total_score             INTEGER NOT NULL DEFAULT 0,
            gross_margin_score      INTEGER NOT NULL DEFAULT 0,
            roic_score              INTEGER NOT NULL DEFAULT 0,
            fcf_margin_score        INTEGER NOT NULL DEFAULT 0,
            roa_score               INTEGER NOT NULL DEFAULT 0,
            earnings_stability_score INTEGER NOT NULL DEFAULT 0,
            mos_score               INTEGER NOT NULL DEFAULT 0,
            earnings_yield_score    INTEGER NOT NULL DEFAULT 0,
            price_fcf_score         INTEGER NOT NULL DEFAULT 0,
            moat_score_pts          INTEGER NOT NULL DEFAULT 0,
            debt_equity_score       INTEGER NOT NULL DEFAULT 0,
            asset_turnover_score    INTEGER NOT NULL DEFAULT 0,
            margin_of_safety_pc     REAL,
            avg_roic                REAL,
            avg_gross_margin        REAL,
            avg_fcf_margin          REAL,
            moat_rating             TEXT,
            ebit_ev                 REAL,
            price_fcf               REAL,
            roa                     REAL,
            debt_to_equity          REAL,
            designation             TEXT NOT NULL DEFAULT '',
            notes                   TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.commit()

# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _score_gross_margin(avg_gm):
    if avg_gm is None: return 0
    if avg_gm >= 0.40: return 10
    if avg_gm >= 0.25: return 6
    if avg_gm >= 0.10: return 3
    return 0

def _score_roic(avg_roic):
    if avg_roic is None: return 0
    if avg_roic >= 0.15: return 10
    if avg_roic >= 0.10: return 7
    if avg_roic >= 0.05: return 4
    return 0

def _score_fcf_margin(avg_fcf):
    if avg_fcf is None: return 0
    if avg_fcf >= 0.15: return 10
    if avg_fcf >= 0.08: return 6
    if avg_fcf >= 0.03: return 3
    return 0

def _score_roa(roa, roa_prior, accruals_positive):
    pts = 0
    if roa is not None and roa > 0:      pts += 4
    if roa is not None and roa_prior is not None and roa > roa_prior: pts += 3
    if accruals_positive == 1:           pts += 3
    return pts

def _score_earnings_stability(pos5):
    if pos5 is None: return 0
    if pos5 == 5: return 10
    if pos5 == 4: return 5
    return 0

def _score_mos(mos_pc):
    if mos_pc is None: return 0
    if mos_pc >= 0.40: return 15
    if mos_pc >= 0.20: return 10
    if mos_pc >= 0.10: return 5
    return 0

def _score_ebit_ev(ebit_ev):
    if ebit_ev is None: return 0
    if ebit_ev >= 0.08: return 10
    if ebit_ev >= 0.05: return 6
    if ebit_ev >= 0.03: return 3
    return 0

def _score_price_fcf(price_fcf):
    if price_fcf is None or price_fcf <= 0: return 0
    if price_fcf <= 12:  return 5
    if price_fcf <= 18:  return 3
    if price_fcf <= 25:  return 1
    return 0

def _score_moat(moat_rating):
    if moat_rating == "Wide":        return 10
    if moat_rating == "Narrow":      return 6
    if moat_rating == "Questionable":return 2
    return 0

def _score_debt_equity(de):
    if de is None: return 0
    if de < 0.5:  return 5
    if de < 1.0:  return 3
    if de < 2.0:  return 1
    return 0

def _score_asset_turnover(trend):
    if trend is None: return 0
    if trend > 0.01:   return 5   # improving
    if trend > -0.01:  return 2   # stable
    return 0                      # declining

def designation(total):
    if total >= 80: return "Premier"
    if total >= 65: return "Qualified"
    if total >= 50: return "Marginal"
    return "Exclude"

# ---------------------------------------------------------------------------
# Score one ticker row
# ---------------------------------------------------------------------------

def score_row(v: dict, m: dict, fd: dict) -> dict:
    today = str(date.today())
    result = {
        "ticker":    v["ticker"],
        "score_date": today,
        "gate_pass": 0, "gate_fail_reason": "",
        "tier1_score": 0, "tier2_score": 0, "tier3_score": 0, "total_score": 0,
        "gross_margin_score": 0, "roic_score": 0, "fcf_margin_score": 0,
        "roa_score": 0, "earnings_stability_score": 0,
        "mos_score": 0, "earnings_yield_score": 0, "price_fcf_score": 0,
        "moat_score_pts": 0, "debt_equity_score": 0, "asset_turnover_score": 0,
        "margin_of_safety_pc": v.get("margin_of_safety_pc"),
        "avg_roic":        m.get("avg_roic"),
        "avg_gross_margin":m.get("avg_gross_margin"),
        "avg_fcf_margin":  m.get("avg_fcf_margin"),
        "moat_rating":     m.get("moat_rating"),
        "ebit_ev": None, "price_fcf": None,
        "roa":             fd.get("roa"),
        "debt_to_equity":  fd.get("debt_to_equity"),
        "designation": "Exclude", "notes": "",
    }

    # ── Binary gates ──────────────────────────────────────────────────────
    fails = []
    op_cf = fd.get("operating_cf")
    if op_cf is not None and op_cf <= 0:
        fails.append("negative operating CF")

    mos_pc     = v.get("margin_of_safety_pc") or 0
    share_val  = v.get("share_value") or 0
    if mos_pc <= 0 or share_val <= 0:
        fails.append("no margin of safety or negative share value")

    ic = fd.get("interest_coverage")
    if ic is not None and ic < 2.0:
        fails.append(f"interest coverage {ic:.1f}x < 2.0x")

    # Wealth creator gate: ROIC > WACC (FCFF) or ROE > cost of equity (FCFE)
    wealth_pc = v.get("wealth_pc")
    if wealth_pc is not None and wealth_pc <= 0:
        fails.append("wealth destroyer (ROIC < WACC)")

    if fails:
        result["gate_fail_reason"] = "; ".join(fails)
        return result

    result["gate_pass"] = 1

    # ── Tier 1 — Quality & Profitability (50 pts) ─────────────────────────
    gm_sc  = _score_gross_margin(m.get("avg_gross_margin"))
    ri_sc  = _score_roic(m.get("avg_roic"))
    fcf_sc = _score_fcf_margin(m.get("avg_fcf_margin"))
    roa_sc = _score_roa(fd.get("roa"), fd.get("roa_prior"), fd.get("accruals_positive"))
    es_sc  = _score_earnings_stability(fd.get("positive_earnings_5yr"))
    tier1  = gm_sc + ri_sc + fcf_sc + roa_sc + es_sc

    result.update({
        "gross_margin_score":       gm_sc,
        "roic_score":               ri_sc,
        "fcf_margin_score":         fcf_sc,
        "roa_score":                roa_sc,
        "earnings_stability_score": es_sc,
        "tier1_score":              tier1,
    })

    # ── Tier 2 — Valuation (30 pts) ───────────────────────────────────────
    mos_sc = _score_mos(mos_pc)

    # EBIT / Enterprise Value (Greenblatt earnings yield)
    ebit        = fd.get("ebit")
    total_debt  = fd.get("total_debt") or 0.0
    cash        = fd.get("cash") or 0.0
    market_cap  = v.get("market_cap") or 0.0
    ebit_ev     = None
    ev          = market_cap + total_debt - cash
    if ebit and ev and ev > 0:
        ebit_ev = ebit / ev
    ey_sc = _score_ebit_ev(ebit_ev)

    # Price / FCF per share
    price  = v.get("price") or 0.0
    fcf    = fd.get("fcf")
    shares = fd.get("shares_outstanding")
    price_fcf = None
    if fcf and shares and shares > 0 and price > 0:
        fcf_per_share = fcf / shares
        if fcf_per_share > 0:
            price_fcf = price / fcf_per_share
    pfcf_sc = _score_price_fcf(price_fcf)

    tier2 = mos_sc + ey_sc + pfcf_sc
    result.update({
        "mos_score":           mos_sc,
        "earnings_yield_score":ey_sc,
        "price_fcf_score":     pfcf_sc,
        "tier2_score":         tier2,
        "ebit_ev":             round(ebit_ev, 4) if ebit_ev else None,
        "price_fcf":           round(price_fcf, 2) if price_fcf else None,
    })

    # ── Tier 3 — Durability (20 pts) ──────────────────────────────────────
    mo_sc  = _score_moat(m.get("moat_rating"))
    de_sc  = _score_debt_equity(fd.get("debt_to_equity"))
    at_sc  = _score_asset_turnover(fd.get("asset_turnover_trend"))
    tier3  = mo_sc + de_sc + at_sc

    result.update({
        "moat_score_pts":     mo_sc,
        "debt_equity_score":  de_sc,
        "asset_turnover_score": at_sc,
        "tier3_score":        tier3,
    })

    total = tier1 + tier2 + tier3
    result["total_score"] = total
    result["designation"] = designation(total)
    return result

# ---------------------------------------------------------------------------
# DB upsert
# ---------------------------------------------------------------------------

def upsert(conn: sqlite3.Connection, r: dict):
    conn.execute("""
        INSERT INTO composite_scores (
            ticker, score_date, gate_pass, gate_fail_reason,
            tier1_score, tier2_score, tier3_score, total_score,
            gross_margin_score, roic_score, fcf_margin_score,
            roa_score, earnings_stability_score,
            mos_score, earnings_yield_score, price_fcf_score,
            moat_score_pts, debt_equity_score, asset_turnover_score,
            margin_of_safety_pc, avg_roic, avg_gross_margin, avg_fcf_margin,
            moat_rating, ebit_ev, price_fcf, roa, debt_to_equity,
            designation, notes
        ) VALUES (
            :ticker, :score_date, :gate_pass, :gate_fail_reason,
            :tier1_score, :tier2_score, :tier3_score, :total_score,
            :gross_margin_score, :roic_score, :fcf_margin_score,
            :roa_score, :earnings_stability_score,
            :mos_score, :earnings_yield_score, :price_fcf_score,
            :moat_score_pts, :debt_equity_score, :asset_turnover_score,
            :margin_of_safety_pc, :avg_roic, :avg_gross_margin, :avg_fcf_margin,
            :moat_rating, :ebit_ev, :price_fcf, :roa, :debt_to_equity,
            :designation, :notes
        )
        ON CONFLICT(ticker) DO UPDATE SET
            score_date              = excluded.score_date,
            gate_pass               = excluded.gate_pass,
            gate_fail_reason        = excluded.gate_fail_reason,
            tier1_score             = excluded.tier1_score,
            tier2_score             = excluded.tier2_score,
            tier3_score             = excluded.tier3_score,
            total_score             = excluded.total_score,
            gross_margin_score      = excluded.gross_margin_score,
            roic_score              = excluded.roic_score,
            fcf_margin_score        = excluded.fcf_margin_score,
            roa_score               = excluded.roa_score,
            earnings_stability_score= excluded.earnings_stability_score,
            mos_score               = excluded.mos_score,
            earnings_yield_score    = excluded.earnings_yield_score,
            price_fcf_score         = excluded.price_fcf_score,
            moat_score_pts          = excluded.moat_score_pts,
            debt_equity_score       = excluded.debt_equity_score,
            asset_turnover_score    = excluded.asset_turnover_score,
            margin_of_safety_pc     = excluded.margin_of_safety_pc,
            avg_roic                = excluded.avg_roic,
            avg_gross_margin        = excluded.avg_gross_margin,
            avg_fcf_margin          = excluded.avg_fcf_margin,
            moat_rating             = excluded.moat_rating,
            ebit_ev                 = excluded.ebit_ev,
            price_fcf               = excluded.price_fcf,
            roa                     = excluded.roa,
            debt_to_equity          = excluded.debt_to_equity,
            designation             = excluded.designation,
            notes                   = excluded.notes
    """, r)
    conn.commit()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Composite value/quality scorer")
    parser.add_argument("--ticker",    metavar="SYM", help="Score a single ticker")
    parser.add_argument("--min-score", type=int, default=0, metavar="N",
                        help="Print only tickers with total_score >= N")
    parser.add_argument("--db", default=str(VALUATION_DB), metavar="PATH")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: {db_path} not found", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_table(conn)

    # Load lookup tables into dicts for fast join
    moat_map = {r["ticker"]: dict(r) for r in
                conn.execute("SELECT * FROM moat_scores").fetchall()}

    if args.ticker:
        valuation_rows = conn.execute(
            "SELECT * FROM valuation WHERE ticker=?", (args.ticker.upper(),)
        ).fetchall()
    else:
        valuation_rows = conn.execute("SELECT * FROM valuation ORDER BY ticker").fetchall()

    fd_map = fetch_snapshot_fields_bulk([r["ticker"] for r in valuation_rows])

    total_tickers = len(valuation_rows)
    scored = passed = 0
    results = []

    for vrow in valuation_rows:
        v  = dict(vrow)
        m  = moat_map.get(v["ticker"], {})
        fd = fd_map.get(v["ticker"], {})
        r  = score_row(v, m, fd)
        upsert(conn, r)
        scored += 1
        if r["gate_pass"]:
            passed += 1
        results.append(r)

    conn.close()

    # Print results meeting min-score threshold
    qualifying = [r for r in results if r["gate_pass"] and r["total_score"] >= args.min_score]
    qualifying.sort(key=lambda x: -x["total_score"])

    if qualifying:
        print(f"\n{'Ticker':<8} {'Total':>5} {'T1':>4} {'T2':>4} {'T3':>4} "
              f"{'Moat':<10} {'MoS%':>6} {'EBIT/EV':>8} {'P/FCF':>6} {'Designation'}")
        print("-" * 90)
        for r in qualifying:
            mos   = f"{r['margin_of_safety_pc']:.0%}" if r['margin_of_safety_pc'] else "—"
            ey    = f"{r['ebit_ev']:.1%}"              if r['ebit_ev']             else "—"
            pfcf  = f"{r['price_fcf']:.1f}x"          if r['price_fcf']           else "—"
            moat  = r['moat_rating'] or "—"
            print(f"{r['ticker']:<8} {r['total_score']:>5} {r['tier1_score']:>4} "
                  f"{r['tier2_score']:>4} {r['tier3_score']:>4} "
                  f"{moat:<10} {mos:>6} {ey:>8} {pfcf:>6} {r['designation']}")

    print(f"\nScored {scored}/{total_tickers} tickers | {passed} passed gates")
    print(f"Premier (80+): {sum(1 for r in results if r['total_score'] >= 80 and r['gate_pass'])}")
    print(f"Qualified (65-79): {sum(1 for r in results if 65 <= r['total_score'] < 80 and r['gate_pass'])}")
    print(f"  DB: {db_path}")

    print(f"\n__RESULT__:{json.dumps({'tool':'composite_score','total':total_tickers,'scored':scored,'passed_gates':passed})}")


if __name__ == "__main__":
    main()
