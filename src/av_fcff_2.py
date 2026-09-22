"""
Earnings whisper truth--
Price dances with hope and fear,
Worth hides in the mist.

S&P 500 / Russell 2000 batch valuation using FCFF DCF model.
Outputs results to value_<index>_YYYYMMDD.xlsx, sorted by margin of safety.


NOTE: Alpha Vantage free tier allows ~25 API requests/day (5/min).
      Each stock requires ~4-5 calls; use --limit N to cap the number of stocks.
      Usage: python av_fcff_2.py [--limit N] [--growth N]
"""

import argparse
from dataclasses import dataclass
from datetime import date
import sqlite3
import sys as _sys
from pathlib import Path as _Path
# This file is intentionally kept identical to
# /Users/jhess/Development/Alpha2/src/av_fcff_2.py (see docs/known_errors.md,
# 2026-07-14 — that copy is actively used by stock_analysis.py, not orphaned).
# hg_dcflib.py has its own separate, manually-synced copy in each location
# (per CLAUDE.md), so we APPEND ~/HessGrp/lib/ rather than inserting at the
# front — this makes it a fallback for logging_setup.py (which only exists
# in HessGrp/lib), without shadowing a same-directory hg_dcflib.py copy.
_sys.path.append(str(_Path.home() / "HessGrp" / "lib"))
import hg_dcflib
from av_fetcher import av_fetch
from config import INTRINIO_KEY
import json
import logging
import os
import sys
import time
import traceback
import io
import pandas as pd
import requests

from logging_setup import make_logger, LONG_FMT

if getattr(sys, "frozen", False):
    _log_dir = os.path.join(os.path.dirname(sys.executable), "data")
else:
    _log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(_log_dir, exist_ok=True)

logger = make_logger(__name__, os.path.join(_log_dir, "value.log"),
                     stream_level=logging.WARNING, fmt=LONG_FMT)


# ---------------------------------------------------------------------------
# Constants (fetched once at startup)
# ---------------------------------------------------------------------------

MY_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY")
FRED_KEY = os.environ.get("FRED_API_KEY")
if not MY_API_KEY:
    raise EnvironmentError(
        "ALPHA_VANTAGE_API_KEY is not set. Run: export ALPHA_VANTAGE_API_KEY='your_key'"
    )
if not FRED_KEY:
    raise EnvironmentError(
        "FRED_API_KEY is not set. Run: export FRED_API_KEY='your_key'"
    )

MARGINAL_TAX_RATE = 0.26
GROWTH_PERIOD = 5  # high-growth years; override with --growth N
TRANSITION_PERIOD = 5
# Years, after the explicit high-growth period, over which growth and
# reinvestment rate fade linearly toward their stable-phase values instead
# of jumping straight from the explicit rate to STABLE_GROWTH -- the
# standard Damodaran 3-stage structure. Fixed 2026-09-11 (external DCF
# review, finding #6: see docs/known_errors.md). Not CLI-configurable
# (unlike GROWTH_PERIOD) -- keeping this fixed avoids compounding an
# already-large change with a second new knob; revisit if a real need
# for a different transition length surfaces.

# AV->Intrinio migration Phase 3 (2026-08-24): "intrinio" is now the
# production default for the 5 fetch wrappers below, with automatic
# per-call fallback to AV if an Intrinio fetch fails for a given ticker
# (see _fetch_with_fallback()) -- not just an available alternative via
# --provider av. AV's own subscription runs regardless through April 2027,
# and Phase 2's real 3-way comparison against SEC EDGAR ground truth found
# each vendor fails on different tickers for different reasons, so combining
# both should reduce total nightly failures versus either alone. Full
# rationale: docs/decisions.md, "Data provider: Intrinio becomes primary".
DATA_PROVIDER = "intrinio"

# DB path — override via $VALUATION_DB env var or --db argument
DEFAULT_DB = os.environ.get("VALUATION_DB", "/Volumes/Financial_Data/valuation.db")

# Deferred to main() so a slow/failed network call at startup doesn't block
# argument parsing or prevent a scheduled job from reporting a clean error.
# Values below are used as fallbacks if the live fetch fails.
EQ_PREM: float = 0.0472    # Damodaran Jan 2026 US ERP fallback
RISK_FREE: float = 0.0425  # approximate 10-yr Treasury fallback
STABLE_GROWTH: float = 0.030  # long-run US nominal GDP growth rate (Damodaran ceiling: <= risk-free)
EQUITY_OVERRIDE: float | None = None  # set via --equity-override; bypasses AV balance sheet equity pull

# Moat-gated stable-phase ROIC blend (decided 2026-08-01, see docs/decisions.md
# and docs/known_errors.md). weight=0 (moat_rating "None" or no moat_scores
# row) reproduces pure g/WACC convergence — Damodaran's own conservative
# default, no persistent excess returns. weight=1 (Wide moat, sustained
# >= MOAT_CONFIDENCE_YEARS) assumes full ROIC persistence. Narrow/Questionable
# interpolate. Confidence in a moat rating is throttled by years_above_wacc
# (scripts/moat_score.py) so one good quarter can't buy a decade of credit.
MOAT_CONFIDENCE_YEARS = 5
MOAT_BASE_WEIGHT = {"Wide": 1.0, "Narrow": 0.5, "Questionable": 0.15, "None": 0.0}

# Capital-light-compounder ROIC gate (decided 2026-08-10, see docs/decisions.md
# and docs/known_errors.md). calc_return_on_capital()'s denominator (equity +
# debt - cash) goes negative for cash-rich, heavy-buyback companies (AZO,
# EXPE, etc.), producing a spurious sign-flipped ROIC. Rather than exclude the
# whole category, a company with positive earnings but non-positive invested
# capital must clear all three gates below to be treated as a wealth creator;
# failing any of them degrades to the same flagged-skip treatment as negative
# book equity. See calc_gated_return_on_capital().
WEALTH_GATE_MIN_YEARS = 3               # years of positive EBIT required
WEALTH_GATE_MIN_INTEREST_COVERAGE = 4.0  # EBIT / interest expense
WEALTH_GATE_MIN_YEARS_ABOVE_WACC = 3     # corroborating moat_scores track record

# ROIC corroboration flag (decided 2026-08-20, docs/decisions.md) — for the
# normal positive-invested-capital case, the current-period return_on_capital
# is used as-is with no check against the company's own longer-run history.
# Found live: NUTX (implied ROIC ~121% vs. moat_scores' own -15.3% 10-year
# average) and NRC (~151% vs. 47.7%) both hit the 30% growth-rate cap on a
# single strong quarter with no reference to their track record. An absolute
# spread, not a ratio, since NUTX's own average is negative (a ratio is
# undefined/meaningless there). Flags via `notes`, never corrects the
# computed number — see calc_gated_return_on_capital()'s docstring.
ROIC_CORROBORATION_MAX_SPREAD = 0.50    # percentage points above moat_scores' avg_roic
ROIC_CORROBORATION_MIN_DATA_YEARS = 3   # avg_roic needs enough history to be a credible baseline

# Terminal value dominance flag (decided 2026-08-27, see docs/decisions.md).
# Calibrated empirically against the real ~2,400-ticker universe, not chosen
# arbitrarily: terminal_value/market_cap has a median of 0.42x and a 99th
# percentile of 4.63x across the whole database, then a clean cliff to a
# small set of genuine outliers (15 tickers, all >5x) -- 5.0x sits right at
# that cliff. Same "flag, don't silently exclude/correct" pattern as
# ROIC_CORROBORATION above and replacer.py's staleness annotation -- see
# terminal_value_dominance_note()'s docstring for the full rationale,
# including why this deliberately self-clears on every revaluation rather
# than needing a manual reset.
TV_MARKET_CAP_MAX_RATIO = 5.0

# Cyclical EBIT normalization -- Damodaran's "relative average over time"
# method ("Ups and Downs: Valuing Cyclical and Commodity Companies", Sept
# 2009, NYU Stern -- verified against the primary-source PDF, decided
# 2026-09-22, see docs/decisions.md). Found live on PARR/DHT/CSTM: a single
# strong quarter/year drives TTM EBIT 2-3x above the company's own long-run
# ROIC average, then compounds through 5-10yr growth. Unlike every other
# guard in this file (flag-only), this one actually replaces the EBIT
# feeding calc_adj_ebit() -- see calc_cyclical_normalized_ebit()'s docstring
# for why that departure from the "flag, never correct" house pattern is
# deliberate here. No industry-level cyclical/commodity gate is used --
# investigated reference_data/betas.xlsx's "Standard deviation in operating
# income (last 10 years)" column and found it fails to flag 2 of 3
# confirmed-bad tickers (DHT/CSTM score below the 94-industry p75), since
# industry-group averages smooth out individual small-cap volatility.
# Applied per-ticker instead, universally -- a no-op by construction for any
# company whose current margin already tracks its own history.
CYCLICAL_MARGIN_MIN_YEARS = 5      # Damodaran's stated window floor -- deliberately above this file's usual 3yr floor (WEALTH_GATE_MIN_YEARS/ROIC_CORROBORATION_MIN_DATA_YEARS), since 3 years can't be trusted to span a full cycle
CYCLICAL_MARGIN_MAX_YEARS = 10     # Damodaran's stated ceiling
# Calibrated 2026-09-22 against a live 65-ticker sample of the real
# universe, comparing each ticker's live TTM margin to its own 10yr FY-
# history average (same inputs this function actually receives). General
# population: p50=1.10x, p75=1.39x, p90=1.91x, p95=3.39x, p99=8.23x. The 3
# confirmed-bad tickers: PARR 6.83x (between p95-p99, unambiguous), DHT
# 1.81x, CSTM 1.75x (both between p75-p90). Controls: AAPL 1.17x, MSFT
# 1.18x, KO 1.19x, PG 1.09x, META 0.97x (all near/below 1.0x -- META's
# multi-year structural margin recovery is old enough that its own 10yr
# average already reflects it, so the mechanism correctly does not fire).
# K=1.6 sits between the general population's p75/p90, catches all 3
# confirmed cases with room to spare, clears every control. See
# docs/decisions.md.
CYCLICAL_MARGIN_RATIO_K = 1.6
CYCLICAL_MARGIN_ABSOLUTE_SPREAD = 0.15  # pp fallback when avg_margin's sign is unstable/near zero -- same reasoning as ROIC_CORROBORATION_MAX_SPREAD; between the small sign-mismatch sample's p50 (9.2%) and p75 (30.2%)


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class Stock_Value:
    ticker: str
    valuation_date: str
    ent_name: str
    industry: str
    cik: str
    beta: float
    market_cap: float
    price: float
    shares_outstanding: float
    risk_free_rate: float
    eq_premium: float
    growth_rate: float
    cost_of_capital: float
    wealth_pc: float
    fcff_value: float
    terminal_value: float
    share_value: float
    margin_of_safety: float
    margin_of_safety_pc: float
    target_price: float
    earnings_yield: float = 0.0
    dividend_yield: float = 0.0
    notes: str = ""
    analyst_count: int = 0


# ---------------------------------------------------------------------------
# S&P 500 ticker list
# ---------------------------------------------------------------------------


def get_sp500_tickers() -> list:
    """
    Return the current S&P 500 constituent list.

    Reads from data/sp500_tickers.csv if available (produced by ticker_lists).
    Falls back to a live Wikipedia fetch if the file is not found.
    """
    csv_path = os.path.join(_log_dir, "sp500_tickers.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        logger.info(f"Loaded {len(tickers)} S&P 500 tickers from {csv_path}")
        return tickers

    # ── Fallback: live fetch from Wikipedia ────────────────────────────────
    logger.warning("sp500_tickers.csv not found — fetching live from Wikipedia")
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    tables = pd.read_html(io.StringIO(resp.text), header=0)
    df = tables[0]
    tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
    logger.info(f"Fetched {len(tickers)} S&P 500 tickers from Wikipedia")
    return tickers


def get_russell2000_tickers() -> list:
    """
    Return the current Russell 2000 constituent list.

    Priority:
      1. data/russell2000_tickers.csv (produced by ticker_lists.py — most accurate)
      2. Live iShares IWM CSV (requires no-auth access — may be blocked)
      3. valuation.db full ticker universe (fallback when iShares is unavailable)
    """
    csv_path = os.path.join(_log_dir, "russell2000_tickers.csv")
    if os.path.exists(csv_path):
        df = pd.read_csv(csv_path)
        tickers = df["Ticker"].dropna().astype(str).str.strip().tolist()
        logger.info(f"Loaded {len(tickers)} Russell 2000 tickers from {csv_path}")
        return tickers

    # ── Fallback 1: live fetch from iShares ───────────────────────────────
    logger.warning("russell2000_tickers.csv not found — trying live iShares fetch")
    try:
        url = (
            "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/"
            "1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.ishares.com/",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        if resp.text.lstrip().startswith("<"):
            raise ValueError("iShares returned HTML instead of CSV — direct download is blocked")
        lines = resp.text.splitlines()
        header_idx = next(
            (i for i, line in enumerate(lines) if "Ticker" in line and "Name" in line),
            None,
        )
        if header_idx is None:
            raise ValueError("Could not locate Ticker header row in iShares CSV")
        df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
        tickers = (
            df["Ticker"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.replace(".", "-", regex=False)
            .pipe(lambda s: s[s.str.match(r"^[A-Z]{1,5}(-[A-Z]+)?$")])
            .tolist()
        )
        logger.info(f"Fetched {len(tickers)} Russell 2000 tickers from iShares")
        return tickers
    except Exception as e:
        logger.warning(f"iShares live fetch failed ({e}) — falling back to valuation.db universe")

    # ── Fallback 2: use all tickers already in valuation.db ───────────────
    db_path = os.environ.get("VALUATION_DB", DEFAULT_DB)
    if not os.path.exists(db_path):
        raise RuntimeError(
            f"russell2000_tickers.csv missing, iShares blocked, and valuation.db not found at {db_path}. "
            "Run ticker_lists.py to generate the CSV file."
        )
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT DISTINCT ticker FROM valuation ORDER BY ticker").fetchall()
    conn.close()
    tickers = [r[0] for r in rows]
    logger.warning(
        f"Using {len(tickers)} tickers from valuation.db as Russell 2000 proxy. "
        "Run ticker_lists.py to refresh russell2000_tickers.csv for an accurate constituent list."
    )
    print(
        f"\n  NOTE: iShares CSV unavailable. Running against {len(tickers)} tickers already in "
        "valuation.db.\n  For a fresh Russell 2000 list, run: python3 ticker_lists.py\n"
    )
    return tickers


# ---------------------------------------------------------------------------
# Filings-driven ticker list
# ---------------------------------------------------------------------------


def get_tickers_from_filings(path: str) -> list[str]:
    """
    Return a de-duplicated, order-preserved list of tickers from a filings file.

    Supported formats (detected by extension):
      .xlsx  — legacy sec_monitor output (header on row 4, "Ticker" column)
      .json  — sec_daily_index output: {"tickers": ["AAPL", "JBL", ...]}
               or a plain JSON array: ["AAPL", "JBL", ...]
      .txt   — one ticker per line, blank lines and # comments ignored
    """
    import json as _json

    ext = os.path.splitext(path)[1].lower()

    if ext == ".xlsx":
        df = pd.read_excel(path, header=3)   # row 4 (0-indexed row 3) is the header
        tickers = df["Ticker"].dropna().str.strip().str.upper().unique().tolist()

    elif ext == ".json":
        with open(path, "r") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            raw = data.get("tickers", [])
        elif isinstance(data, list):
            raw = data
        else:
            raise ValueError(f"Unrecognised JSON structure in {path}")
        tickers = list(dict.fromkeys(t.strip().upper() for t in raw if t.strip()))

    elif ext == ".txt":
        with open(path, "r") as f:
            lines = f.readlines()
        tickers = list(dict.fromkeys(
            line.strip().upper()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ))

    else:
        raise ValueError(f"Unsupported filings file format: {ext}  (expected .xlsx, .json, or .txt)")

    logger.info(f"Loaded {len(tickers)} tickers from filings file: {path}")
    return tickers


# ---------------------------------------------------------------------------
# Financial statement helpers
# ---------------------------------------------------------------------------


def _fetch_with_fallback(ticker, intrinio_fn, av_fn, label):
    """
    Phase 3 (2026-08-24): when DATA_PROVIDER == "intrinio" (the production
    default), try Intrinio first and automatically fall back to AV if the
    Intrinio call fails for this ticker -- not just AV being reachable via
    --provider av. Phase 2's real 3-way comparison against SEC EDGAR found
    each vendor fails on different tickers for different reasons (Intrinio:
    genuine coverage gaps on foreign-domiciled names; AV: the cash-corruption/
    coverage pattern that motivated the migration), so combining both
    automatically should reduce total nightly failures versus either alone.
    See docs/decisions.md, "Data provider: Intrinio becomes primary".

    When DATA_PROVIDER == "av" explicitly (manual/shadow-mode AV-only runs),
    no fallback applies -- calls AV directly, unchanged from Phase 1/2.
    """
    if DATA_PROVIDER != "intrinio":
        return av_fn()
    try:
        return intrinio_fn()
    except Exception as exc:
        logger.warning(
            f"{ticker}: Intrinio {label} fetch failed ({exc}) — falling back to AV."
        )
        return av_fn()


def income_statement(ticker, api_key, is_financial_or_reit: bool = False):
    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_inc_stmnt_intrinio(ticker, INTRINIO_KEY, is_financial_or_reit=is_financial_or_reit),
        lambda: hg_dcflib.get_inc_stmnt(ticker, api_key),
        "income statement",
    )


def annual_income_statement(ticker, api_key, years: int = CYCLICAL_MARGIN_MAX_YEARS):
    """Up to `years` years of annual (revenue, ebit) pairs, most-recent-first.
    Feeds calc_cyclical_normalized_ebit() -- see its docstring. Intrinio-
    primary via hg_dcflib.get_inc_stmnt_intrinio_annual() (already returns
    this exact shape); AV-fallback normalizes AV's raw annualReports through
    hg_dcflib.annual_ebit_proxy() so the caller never needs to know which
    provider served a given ticker."""
    def _av_annual():
        raw = av_fetch("INCOME_STATEMENT", symbol=ticker).get("annualReports", [])
        return [
            {
                "totalRevenue": hg_dcflib.safe_float(row.get("totalRevenue")),
                "ebit": hg_dcflib.annual_ebit_proxy(row),
            }
            for row in raw[:years]
        ]

    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_inc_stmnt_intrinio_annual(ticker, INTRINIO_KEY, years=years),
        _av_annual,
        "annual income statement (cyclical normalization)",
    )


def annual_cash_flow_statement(ticker, api_key, years: int = CYCLICAL_MARGIN_MAX_YEARS):
    """Up to `years` years of annual capex history, most-recent-first,
    POSITIVE-magnitude sign convention (matches calc_capital_expenditures()'s
    existing contract -- the current-year figure calc_reinvestment() already
    expects). Feeds calc_cyclical_normalized_reinvestment() -- see its
    docstring. 2026-09-22 follow-up to annual_income_statement() (same
    fetch/fallback template).

    SIGN FLIP, explicit and isolated to this one function: Intrinio's
    get_cash_flow_intrinio_annual() deliberately returns capitalExpenditures
    RAW/NEGATIVE (see its own docstring) -- that's the correct convention
    for its other consumer, moat_score.py, but the OPPOSITE of what every
    capex figure in THIS file expects (calc_capital_expenditures(),
    calc_reinvestment()'s capex parameter). The flip happens once, here,
    at the vendor boundary -- never inside the calc functions themselves.
    AV's own annual capitalExpenditures is already positive (matches AV's
    quarterly convention, summed with no flip by calc_capital_expenditures()
    today) -- the AV branch below does NOT flip."""
    def _av_annual():
        raw = av_fetch("CASH_FLOW", symbol=ticker).get("annualReports", [])
        return [
            {"capitalExpenditures": hg_dcflib.safe_float(row.get("capitalExpenditures"))}
            for row in raw[:years]
        ]

    def _intrinio_annual():
        raw = hg_dcflib.get_cash_flow_intrinio_annual(ticker, INTRINIO_KEY, years=years)
        return [
            {"capitalExpenditures": -hg_dcflib.safe_float(row.get("capitalExpenditures"))}
            for row in raw
        ]

    return _fetch_with_fallback(
        ticker,
        _intrinio_annual,
        _av_annual,
        "annual cash flow (cyclical reinvestment normalization)",
    )


def balance_sheet(ticker, api_key, is_financial_or_reit: bool = False):
    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_bal_sheet_intrinio(ticker, INTRINIO_KEY, is_financial_or_reit=is_financial_or_reit),
        lambda: hg_dcflib.get_bal_sheet(ticker, api_key, is_financial_or_reit=is_financial_or_reit),
        "balance sheet",
    )


def cash_flow_statement(ticker, api_key):
    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_cash_flow_intrinio(ticker, INTRINIO_KEY),
        lambda: hg_dcflib.get_cash_flow(ticker, api_key),
        "cash flow statement",
    )


def research_and_development(ticker, rd_years, api_key):
    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_rAndD_intrinio(ticker, rd_years, INTRINIO_KEY),
        lambda: hg_dcflib.get_rAndD(ticker, rd_years, api_key),
        "R&D",
    )


# Populated by prefetch_quotes() before a batch run — see that function's
# docstring and docs/known_errors.md (2026-07-22) for why quote and
# fundamentals calls are deliberately kept out of the same time window.
# Keyed by (ticker, DATA_PROVIDER) so a shadow-mode comparison that runs both
# providers within one process (e.g. a Phase 2 diff script) can't serve a
# cached AV quote back for an Intrinio-provider call or vice versa. A
# fallback-to-AV quote (Phase 3) is cached under the "intrinio" provider key
# it was requested under, not "av" -- it's what enterprise_quote() actually
# returned for that (ticker, DATA_PROVIDER) combination.
_QUOTE_CACHE: dict = {}


def enterprise_quote(ticker, api_key):
    cache_key = (ticker, DATA_PROVIDER)
    if cache_key in _QUOTE_CACHE:
        return _QUOTE_CACHE[cache_key]
    return _fetch_with_fallback(
        ticker,
        lambda: hg_dcflib.get_quote_intrinio(ticker, INTRINIO_KEY),
        lambda: hg_dcflib.get_quote(ticker, api_key),
        "quote",
    )


def prefetch_quotes(tickers: list, api_key: str) -> None:
    """
    Fetch GLOBAL_QUOTE/OVERVIEW (via hg_dcflib.get_quote) for every ticker in
    one contiguous batch, before any fundamentals calls (INCOME_STATEMENT/
    BALANCE_SHEET/CASH_FLOW) begin, and cache the results in _QUOTE_CACHE.

    Alpha Vantage support confirmed (2026-07-22) that interleaving GLOBAL_QUOTE
    (real-time, entitlement-gated) with fundamentals calls for the same symbol
    within the same short window can trip a per-minute micro-throttle separate
    from the account's headline RPM cap — even when the overall request rate is
    far under that cap (our own logs showed ~11-12 req/min, well under the 75/
    min premium limit, still failing ~37% of tickers). Every one of our 6
    duplicated valuation paths calls the shared enterprise_quote() wrapper
    above, so caching there fixes all 6 without touching any of them — see
    value_bank_stock, _value_stock_fcff, value_reit_stock,
    _value_bank_stock_detail, _value_reit_stock_detail, _value_stock_detail_fcff.

    Per-ticker failures here are logged and simply left out of the cache —
    enterprise_quote() falls back to a live call for anything not cached, so a
    prefetch miss degrades to the old (interleaved) behavior for that one
    ticker rather than blocking the whole run.
    """
    total = len(tickers)
    bar_width = 40
    start_time = time.time()
    for idx, ticker in enumerate(tickers, 1):
        try:
            _QUOTE_CACHE[(ticker, DATA_PROVIDER)] = _fetch_with_fallback(
                ticker,
                lambda t=ticker: hg_dcflib.get_quote_intrinio(t, INTRINIO_KEY),
                lambda t=ticker: hg_dcflib.get_quote(t, api_key),
                "quote",
            )
        except Exception as e:
            # Both Intrinio and its AV fallback failed (or DATA_PROVIDER=="av"
            # and AV itself failed) -- logged and left out of the cache;
            # enterprise_quote()'s own fallback-aware live call covers this
            # ticker later, same degrade-gracefully behavior as before.
            logger.warning(f"Prefetch quote failed for {ticker}: {e}")
        filled = int(bar_width * idx / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        elapsed = int(time.time() - start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        print(f"\r  quotes {idx}/{total} [{bar}] {h:02d}:{m:02d}:{s:02d}", end="", flush=True)
    print()


def get_excluded_tickers() -> set:
    """
    Load the permanently-excluded ticker set from data/excluded_tickers.json.

    Found 2026-07-23: this file was previously read only by Iggy's SKILL.md
    orchestration (hess_group/scheduled/iggy-valuation-update/SKILL.md), and
    only to strip excluded tickers from the *next day's* retry file — never to
    skip them within the same run. Every batch run was still attempting (and,
    since the 2026-07-22 second-pass retry queue, re-attempting) every
    excluded ticker before that filtering ever kicked in. This wires the same
    file directly into the batch loop so excluded tickers are skipped before
    the first attempt, not just before tomorrow's retry.

    Resolves the file the same way hg_dcflib.py resolves reference_data/: try
    the path relative to this script's own location first (so a duplicated
    copy in Development/Alpha2 would be preferred there), falling back to the
    fixed ~/HessGrp/data/ path since this file is HessGrp-specific and has no
    Alpha2 counterpart today. See docs/known_errors.md 2026-07-14 entry for
    the same fallback pattern used for logging_setup.py/hg_dcflib.py.
    """
    candidates = [
        _Path(os.path.abspath(__file__)).parent.parent / "data" / "excluded_tickers.json",
        _Path.home() / "HessGrp" / "data" / "excluded_tickers.json",
    ]
    for path in candidates:
        if path.exists():
            try:
                with open(path) as f:
                    return set(t.upper() for t in json.load(f).get("tickers", []))
            except Exception as e:
                logger.warning(f"Could not load excluded_tickers.json at {path}: {e}")
    return set()


def get_moat_weight(ticker: str, db_path: str | None = None) -> float:
    """
    Blend weight toward full stable-phase ROIC persistence, gated on
    scripts/moat_score.py's moat_rating and years_above_wacc — see
    MOAT_BASE_WEIGHT/MOAT_CONFIDENCE_YEARS above and docs/decisions.md
    "Moat-gated stable-phase ROIC assumption" (decided 2026-08-01).

    Missing moat_scores row, missing table, or any read failure all degrade
    to weight 0.0 — i.e. pure WACC-convergence, the same behavior as before
    this feature existed. A missing/stale moat score should never make a
    valuation *more* optimistic than the conservative default.
    """
    try:
        conn = sqlite3.connect(db_path or DEFAULT_DB, timeout=10)
        row = conn.execute(
            "SELECT moat_rating, years_above_wacc FROM moat_scores WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        conn.close()
    except Exception as e:
        logger.debug(f"{ticker}: could not read moat_scores ({e}) — moat weight 0.0")
        return 0.0

    if row is None:
        return 0.0

    moat_rating, years_above_wacc = row
    base = MOAT_BASE_WEIGHT.get(moat_rating, 0.0)
    confidence = min(years_above_wacc / MOAT_CONFIDENCE_YEARS, 1.0) if years_above_wacc else 0.0
    return base * confidence


def get_moat_corroboration(
    ticker: str, db_path: str | None = None
) -> tuple[str | None, int | None, float | None, int | None]:
    """
    Fetch (moat_rating, years_above_wacc, avg_roic, data_years) from
    moat_scores — the raw fields behind get_moat_weight(), used by
    calc_gated_return_on_capital()'s Gate 3 (see docs/decisions.md
    "Capital-light compounder ROIC gate") and its ROIC-corroboration flag
    (docs/decisions.md, decided 2026-08-20). moat_score.py's own avg_roic
    already skips non-positive-invested-capital years
    (scripts/moat_score.py: score_roic()), so it's reused directly rather
    than inventing a second capital-efficiency heuristic here.

    Same failure contract as get_moat_weight(): missing row, missing table,
    or any read failure all degrade to (None, None, None, None) — never more
    optimistic than "gate fails"/"no corroboration" on a data gap.
    """
    try:
        conn = sqlite3.connect(db_path or DEFAULT_DB, timeout=10)
        row = conn.execute(
            "SELECT moat_rating, years_above_wacc, avg_roic, data_years "
            "FROM moat_scores WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        conn.close()
    except Exception as e:
        logger.debug(f"{ticker}: could not read moat_scores ({e}) — no gate corroboration")
        return None, None, None, None

    if row is None:
        return None, None, None, None
    return row[0], row[1], row[2], row[3]


# ---------------------------------------------------------------------------
# Industry classification helpers
# ---------------------------------------------------------------------------

_FINANCIAL_KEYWORDS = {
    "bank",
    "banks",
    "financial services",
    "insurance",
    "brokerage",
    "investment banking",
    "thrift",
    "savings",
    "credit",
    "mortgage",
    "asset management",
}

_INSURANCE_KEYWORDS = {"insurance", "reinsurance", "surety", "title insurance"}

_REIT_KEYWORDS = {"reit", "real estate investment trust"}
_REIT_INDUSTRY_PREFIXES = ("retail (reit", "r.e.i.t.")

# Growth rate floors by REIT sub-type (AV industry string keyword → rate).
# Reflects contractual lease escalators and structural growth independent of
# retained earnings. None = mortgage REIT; flag for manual review.
_REIT_SUBTYPE_GROWTH: dict[str, float | None] = {
    "specialty":   0.020,  # broad bucket (data centers, self-storage, etc.) — conservative default
    "industrial":  0.010,
    "residential": 0.010,
    "healthcare":  0.015,
    "diversified": 0.005,
    "hotel":       0.000,
    "retail":      0.000,  # conservative; net lease overrides below
    "office":      0.000,
    "mortgage":    None,   # not property income — AFFO DDM not applicable
}

# Ticker-level overrides for known sub-types where the industry keyword is too coarse.
# Tower REITs and data centers: CPI escalators + colocation growth → 3%.
# Net lease REITs: contractual annual escalators (1–2%) → 1.5%.
_REIT_TICKER_GROWTH_OVERRIDE: dict[str, float] = {
    "SBAC": 0.030, "AMT": 0.030, "CCI": 0.030,          # tower
    "EQIX": 0.030, "DLR": 0.030, "CONE": 0.030,          # data center
    "O":    0.015, "NNN": 0.015, "STOR": 0.015,           # net lease
    "ELS":  0.020, "SUI": 0.020,                           # manufactured housing
    "WY":   0.015, "PCH": 0.015,                           # timber (biological growth proxy)
}


def reit_subtype_growth(ticker: str, industry: str) -> float | None:
    """Return DDM growth rate floor for a REIT based on sub-type.

    Returns None for mortgage REITs (AFFO DDM not applicable — flag for manual review).
    Ticker-level override takes precedence over industry keyword match.
    """
    if ticker in _REIT_TICKER_GROWTH_OVERRIDE:
        return _REIT_TICKER_GROWTH_OVERRIDE[ticker]
    low = industry.lower()
    for keyword, rate in _REIT_SUBTYPE_GROWTH.items():
        if keyword in low:
            return rate
    return 0.010  # unknown sub-type — conservative fallback


def is_financial_firm(industry: str) -> bool:
    """Return True if the industry is a financial firm requiring FCFE valuation."""
    low = industry.lower()
    return any(kw in low for kw in _FINANCIAL_KEYWORDS)


def is_insurance_firm(industry: str) -> bool:
    """Return True for insurance companies that need normalized NI."""
    low = industry.lower()
    return any(kw in low for kw in _INSURANCE_KEYWORDS)


def is_reit(industry: str) -> bool:
    """Return True for REITs (pass-through entities requiring AFFO DDM).
    Excludes real estate services/development/brokerage firms — those use FCFF."""
    low = industry.lower()
    return (
        any(kw in low for kw in _REIT_KEYWORDS)
        or any(low.startswith(p) for p in _REIT_INDUSTRY_PREFIXES)
    )


# SIC 6000-6799 = SEC/EDGAR Division H, "Finance, Insurance, and Real
# Estate" -- the standard regulatory classification range covering
# depository institutions (6000s), non-depository credit institutions
# (6100s, e.g. mortgage bankers), security/commodity brokers and
# exchanges (6200s), insurance carriers/agents (6300s-6400s), real
# estate (6500s), and holding/investment offices (6700s).
SIC_FINANCIAL_RANGE = (6000, 6799)


def is_financial_sic(sic: int) -> bool:
    """Return True if sic falls in the SEC's own Finance/Insurance/Real-Estate
    division. Used only as a cheap pre-filter (see value_stock()) to decide
    whether a ticker is even a *candidate* for bank/insurance/REIT-style
    valuation at all -- it never forces that routing on its own. Confirmed
    live 2026-09-01: CBOE and CME both carry SIC 6200 (in-range) but value
    correctly under plain FCFF (exchanges take fee income, not deposits) --
    an in-range SIC narrows which tickers need the finer-grained
    Damodaran-bucket + Intrinio-tag-magnitude check, it doesn't decide the
    outcome by itself. See docs/known_errors.md 2026-09-01.
    """
    return SIC_FINANCIAL_RANGE[0] <= sic <= SIC_FINANCIAL_RANGE[1]


def _normalized_net_income(net_income_list: list) -> tuple[float, int]:
    """
    Return (normalized_NI, years_used).
    Uses up to 5 years of NI, excluding any years where NI < 0
    (catastrophe or reserve-charge years) if at least 2 positive years exist.
    Falls back to simple average if most years are negative.
    """
    positives = [ni for ni in net_income_list if ni > 0]
    if len(positives) >= 2:
        avg = sum(positives) / len(positives)
        return avg, len(positives)
    # If fewer than 2 positive years, use all available years
    avg = sum(net_income_list) / len(net_income_list)
    return avg, len(net_income_list)


# ---------------------------------------------------------------------------
# Calculation functions
# ---------------------------------------------------------------------------


def calc_stable_beta(unlevered_beta):
    if unlevered_beta < 0.5:
        stable_beta = 0.8
    elif unlevered_beta > 1.5:
        stable_beta = 1.2
    else:
        stable_beta = 1.0
    logger.info(f"Stable beta = {stable_beta:,.3f}")
    return stable_beta


def calc_capital_expenditures(cash_flw):
    """
    Current-year (TTM) capex only.

    Reverted 2026-09-13 (same reasoning, and same precedent, as the R&D-
    expense averaging revert in calc_reinvestment() on 2026-09-11): Ginzu's
    own Master Inputs sheet takes capex (B14) as a single raw fed-in figure,
    never averaged -- confirmed by reading its formulas directly, same
    procedure used for the R&D revert. 5-year averaging was added 2026-09-10
    (external review, finding #4) on internal-consistency grounds, but a
    live GOOG comparison (independent-data Ginzu run vs. our engine, 2026-
    09-11/13) found it was diluting a real, structural capex ramp: GOOG's
    TTM capex was $132.4B, but the 5-year average our engine fed Ginzu was
    only $60.3B (blended against 4-year-old figures as low as $28B) --
    exactly the same averaging-masks-a-real-trend failure mode already found
    and reverted for R&D on AAPL. This directly explained most of the gap
    between our engine's growth rate (10.6%) and Ginzu's own native growth
    rate on identical data (23.9%) for GOOG.
    """
    return cash_flw["capex"][0]


def calc_depreciation(cash_flw):
    """
    Current-year (TTM) depreciation only.

    Reverted 2026-09-13, same reasoning as calc_capital_expenditures() above
    -- added 2026-09-10 (external review, finding #4) as a 5-year average to
    mirror capex, but Ginzu feeds depreciation (B15) as a single raw current-
    year figure too, and the same GOOG capex investigation found
    depreciation similarly diluted (TTM $25.2B vs. 5-year average $16.2B).
    """
    return cash_flw["depreciation"][0]


def calc_chng_wc(bal_sht, inc_stmnt=None):
    """
    Current-year (TTM) change in non-cash working capital only.

    Reverted 2026-09-13 to current-year-only -- was a 5-year average of
    deltas, added 2026-09-10 (external review, finding #4). Same reasoning
    and precedent as calc_capital_expenditures()/calc_depreciation()'s
    2026-09-13 revert: read Ginzu's own Valuation Model formula directly
    ('Valuation Model'!D10) and confirmed it never averages working-capital
    change either -- always a single current-year figure, same as every
    other reinvestment component.

    Negative-value override, matching Ginzu's D10 formula exactly:
    `=IF(B20<0, (B18-C18)*(B19/B18), B20)`. If the raw current-year delta is
    negative (working capital released, which would otherwise add to cash
    flow), Ginzu doesn't trust the isolated swing at face value -- it
    re-derives the change as (dollar revenue growth) x (current non-cash-
    WC-to-revenue ratio), i.e. assumes working capital scales
    proportionally with revenue growth rather than accepting a one-off
    release. Requires inc_stmnt (totalRevenue[0]/[1]) to apply -- degrades
    to the raw (negative) delta, unadjusted, if inc_stmnt is omitted or
    lacks a prior-year revenue figure, rather than guessing.
    """
    n = len(bal_sht["total_current_assets"])
    if n < 2:
        raise ValueError("Insufficient balance sheet history (need 2 years) to compute working capital change")

    def _nc_wc(i):
        return (bal_sht["total_current_assets"][i] - bal_sht["cash_and_equivalents"][i]) - (
            bal_sht["total_current_liabilities"][i] - bal_sht["short_term_debt"][i]
        )

    curr_nc_wc = _nc_wc(0)
    chng_nc_wc = curr_nc_wc - _nc_wc(1)

    if chng_nc_wc < 0 and inc_stmnt is not None:
        revenue = inc_stmnt.get("totalRevenue", [])
        if len(revenue) >= 2 and revenue[0] != 0:
            chng_nc_wc = (revenue[0] - revenue[1]) * (curr_nc_wc / revenue[0])

    return chng_nc_wc


def capitalizerAndD(ticker, rd_years, api_key):
    """
    Damodaran's R&D-capitalization amortization schedule: R&D expense is
    treated as a capitalized asset, amortized straight-line over rd_years.
    Each prior year's R&D vintage contributes 1/rd_years to this year's
    amortization charge -- e.g. rd_years=3 means each of the 3 most recent
    prior years' R&D still has some unamortized balance, each contributing
    an equal 1/3 share to this year's Current_Year_Amortization.

    Fixed 2026-09-11 (external DCF review + a live Ginzu comparison, see
    docs/known_errors.md): amort_percentage was 1/(rd_years-1), not
    1/rd_years -- confirmed against Damodaran's own Ginzu R&D-converter
    formulas directly (its 'Amortization this year' column divides by the
    amortization period itself, not one less than it). This overstated
    Current_Year_Amortization (and RD_Asset_Value, since both use the same
    amort_percentage) by exactly rd_years/(rd_years-1) -- 50% for
    rd_years=3, 25% for rd_years=5, milder for longer amortization periods
    -- for every R&D-capitalizing company in the universe, understating
    adjusted_ebit, adjusted_bv_equity, ROIC, growth, and IV. Live-verified:
    GOOG/MSFT (rd_years=3) IV understated ~13.6%/~10.4% by this alone;
    AAPL (rd_years=5) ~8.9%.
    """
    rd_years = int(rd_years)
    if rd_years <= 1:
        # No R&D amortization for this industry — skip API call and return zeroed schedule
        return {
            "rAndDExpense": [0.0],
            "unamortized_percent": [0.0],
            "unamort_amount": [0.0],
            "RD_Asset_Value": 0.0,
            "Current_Year_Amortization": 0.0,
        }

    rdTable = research_and_development(ticker, rd_years, api_key)
    rd_dict, years_to_process = rdTable
    logger.info(f"rdTable = {rdTable}")

    if years_to_process == 0:
        # Same zero-filled shape as the rd_years <= 1 branch above -- a
        # vendor returning fewer than 4 quarters of income-statement history
        # (thin data, not necessarily "no R&D") must degrade the same safe
        # way, not leave rAndDExpense as an empty list. calc_adj_ebit() and
        # calc_reinvestment() both unconditionally index amort_schedule[...][0]
        # -- an empty list crashes with "list index out of range" instead of
        # a clear message. Confirmed live 2026-08-26: CMCL/CMRE/TNK (real
        # zero-R&D companies -- shipping/mining, not thin data) hit exactly
        # this path after get_rAndD_intrinio() started raising instead of
        # silently returning empty (see docs/known_errors.md 2026-08-26).
        return {
            "rAndDExpense": [0.0],
            "unamortized_percent": [0.0],
            "unamort_amount": [0.0],
            "RD_Asset_Value": 0.0,
            "Current_Year_Amortization": 0.0,
        }

    rd_table = {}
    rd_expense = []
    unamort_percent = []
    unamort_amt = []
    amort_percentage = 1.0 / rd_years

    # min(years_to_process, rd_years) never actually caps anything here --
    # research_and_development()'s own fetchers already enforce
    # years_to_process = min(rd_years, num_available_years), so
    # years_to_process <= rd_years always holds. Kept for defensive clarity
    # in case that invariant ever changes upstream.
    current_year_total_amortization = 0
    for year in range(1, min(years_to_process, rd_years)):
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
    chng_nc_wc = calc_chng_wc(bal_sht, inc_stmnt)
    logger.info(f"Change WC {chng_nc_wc:,.2f}")
    depreciation = calc_depreciation(cash_flw)
    logger.info(f"Depreciation {depreciation:,.2f}")
    fcff = ebiat - capex + depreciation - chng_nc_wc
    logger.info(f"FCFF {fcff:,.2f}")
    return [ebiat, capex, chng_nc_wc, depreciation, fcff]


def calc_reinvestment(capex, depreciation, chng_nc_wc, amort_schedule):
    """
    Net new capitalized R&D investment (rAndDExpense - amortization, the R&D
    equivalent of capex - depreciation) uses the CURRENT YEAR's R&D expense
    only -- matching Damodaran's own Ginzu FCFF spreadsheet, which is
    literally where this codebase's whole R&D-capitalization methodology
    came from (see docs/ginzu_comparison_procedure.md): Ginzu's R&D
    converter feeds current-year R&D expense (its F7 cell) directly into
    the reinvestment-rate numerator, never an average.

    2026-09-11 revert (external DCF review finding #4 had averaged this
    over 5 years, 2026-09-10, on internal-consistency grounds -- matching
    the newly-averaged capex/depreciation/chng_nc_wc in the same formula).
    A live 3-ticker Ginzu comparison (AAPL/GOOG/MSFT) traced and quantified
    this specific averaging as the dominant driver of a ~21-23% IV gap
    against Ginzu for R&D-heavy companies whose R&D spend is trending up
    (AAPL's 5yr R&D history: $25.3B -> $29.4B -> $30.9B -> $33.4B ->
    $42.9B -- the average sits well below the current year, materially
    understating reinvestment/growth for exactly this common case). Jim's
    call: if it isn't part of Ginzu, it isn't part of Damodaran's
    philosophy -- revert. See docs/known_errors.md 2026-09-11.

    Current_Year_Amortization is NOT averaged -- it's already a
    schedule-based figure built from a multi-year straight-line
    amortization of the capitalized R&D asset (see capitalizerAndD()), not
    a raw single-year snapshot -- smoothed by construction already, and
    this is exactly how Ginzu's own R&D converter computes its
    amortization too.

    calc_adj_ebit() also uses amort_schedule["rAndDExpense"][0] (this
    year's actual R&D expense) -- that function restates THIS YEAR's
    income statement onto an R&D-capitalized basis, so it needs this
    year's actual figure regardless of what this function does.
    """
    firm_reinvestment = (
        capex
        - depreciation
        + chng_nc_wc
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["Current_Year_Amortization"]
    )
    logger.info(f"Firm Reinvestment {firm_reinvestment:,.2f}")
    return firm_reinvestment


def calc_adj_ebit(raw_ebit, amort_schedule):
    """
    R&D capitalization adjustment at the pre-tax EBIT level, per Damodaran's
    procedure: add back R&D expense (already expensed within reported EBIT)
    and subtract the capitalized R&D asset's amortization for the year --
    both pre-tax figures, so the adjustment must happen before any tax rate
    is applied, not after.

    Fixed 2026-09-10 (external review flagged this, confirmed against the
    code directly before acting on it): the prior version -- calc_adj_ebiat()
    -- applied this same adjustment to EBIAT (already after-tax) instead of
    EBIT, mixing pre-tax R&D/amortization figures into an after-tax base.
    Every call site then "reconstructed" an EBIT by dividing that mixed
    figure through (1 - eff_tax_rate) -- which doesn't undo the mixing, it
    compounds it, since R&D/amortization were never tax-affected in the
    first place. Net effect: adjusted EBIT (and everything downstream --
    ROIC, growth rate, FCFF, terminal value) was overstated for any
    R&D-capitalizing company, worse the larger R&D is relative to EBIT.
    See docs/known_errors.md 2026-09-10.
    """
    adjusted_ebit = (
        raw_ebit
        + amort_schedule["rAndDExpense"][0]
        - amort_schedule["Current_Year_Amortization"]
    )
    logger.info(f"Adjusted EBIT {adjusted_ebit:,.2f}")
    return adjusted_ebit


def calc_cyclical_normalized_ebit(
    ticker: str,
    raw_ebit: float,
    current_revenue: float,
    annual_reports: list,
    min_years: int = CYCLICAL_MARGIN_MIN_YEARS,
    max_years: int = CYCLICAL_MARGIN_MAX_YEARS,
    k: float = CYCLICAL_MARGIN_RATIO_K,
    absolute_spread: float = CYCLICAL_MARGIN_ABSOLUTE_SPREAD,
) -> tuple:
    """
    Returns (ebit_to_use, diagnostics). Damodaran's "relative average over
    time" method ("Ups and Downs: Valuing Cyclical and Commodity Companies",
    Sept 2009) -- average this ticker's OWN operating margin (EBIT/revenue)
    over up to max_years, apply that average margin to CURRENT revenue.
    Demonstrated by Damodaran on Toyota (1998-2008 avg pre-tax margin
    applied to 2009 revenue) rather than his rejected method #1 (average raw
    earnings -- understates a firm that's grown in scale) or #3 (sector
    average margin -- not used here, no reliable per-ticker industry
    classification exists, see docs/decisions.md).

    Deliberately NOT gated by industry (see docs/decisions.md) -- an
    industry-volatility classification was investigated and rejected: it
    fails to flag 2 of 3 confirmed-bad tickers (DHT, CSTM) whose
    company-specific volatility is far higher than their broad industry
    group's average. Applied per-ticker, universally, instead -- a no-op by
    construction for stable-margin companies (current margin ~= own
    average), so no gate is needed for the common case.

    ebit_to_use is raw_ebit unchanged unless diagnostics["applied"] is True.
    When years_used < min_years, normalization is skipped entirely (not
    partially applied) -- not enough history to distinguish a real cycle
    from noise; matches this file's existing MIN_YEARS-style floors
    (ROIC_CORROBORATION_MIN_DATA_YEARS) rather than inventing a
    partial-credit scheme. No flag fires in this case -- an unremarkable
    young company shouldn't get a permanent "insufficient data" scar on
    every valuation.

    Ratio test used when avg_margin and current_margin share a sign (both
    positive or both negative); falls back to an absolute percentage-point
    spread when the sign is unstable or avg_margin is near zero -- same
    reasoning as ROIC_CORROBORATION_MAX_SPREAD's own absolute-vs-ratio
    choice (a ratio is undefined/meaningless when the denominator can flip
    sign).

    UNLIKE every other anomaly guard in this file (terminal_value_dominance_
    note, low_growth_rate_note, extreme_reinvestment_rate_note,
    high_growth_rate_note, calc_gated_return_on_capital's ROIC
    corroboration), this is not flag-only -- when triggered, the returned
    ebit_to_use (normalized_ebit) becomes the TARGET the model is steered
    toward. Deliberate: those guards ask "is this correctly-computed result
    unusual?"; this asks "is the EBIT input itself even the right number to
    feed in?" -- the same category calc_adj_ebit()'s own R&D adjustment
    already occupies (an input correction, not a result flag). Every
    trigger is still surfaced -- via `notes` in the batch path
    (cyclical_ebit_normalization_note()) and via detail-dict keys in the
    Excel report -- so it is never a SILENT correction, only a
    non-flag-gated one.

    2026-09-22 update: the caller no longer instantly substitutes this
    return value as the explicit period's starting EBIT by default --
    calc_adaptive_growth_rate() (Damodaran's own named alternative,
    "Adaptive Growth") steers the real, current EBIT toward this target via
    a solved growth rate instead, so the trajectory doesn't jump straight
    to the average on day one. Instant substitution (what this function's
    return value used to feed directly) is now only the FALLBACK, used
    when no real growth rate can bridge the two (sign mismatch -- see
    calc_adaptive_growth_rate()'s docstring). See docs/known_errors.md
    2026-09-22 for why (PARR's overcorrection to a negative IV under pure
    instant substitution) and docs/decisions.md for the full decision.

    A real, accepted limitation: this cannot distinguish a genuine temporary
    cyclical spike (PARR/DHT/CSTM) from a genuine permanent structural
    margin improvement -- Damodaran's formula has no way to tell the two
    apart, and neither does this implementation. Live-verified against META
    (real, well-documented multi-year margin recovery, not a data anomaly)
    rather than assumed safe -- see docs/known_errors.md 2026-09-22.
    """
    margins = []
    for r in annual_reports[:max_years]:
        rev = r.get("totalRevenue")
        ebit = r.get("ebit")
        if rev and rev > 0 and ebit is not None:
            margins.append(ebit / rev)

    years_used = len(margins)
    if years_used < min_years or not current_revenue or current_revenue <= 0:
        return raw_ebit, {
            "applied": False,
            "reason": "insufficient_history" if years_used < min_years else "no_current_revenue",
            "years_used": years_used,
        }

    avg_margin = sum(margins) / years_used
    current_margin = raw_ebit / current_revenue
    same_sign = (avg_margin >= 0) == (current_margin >= 0)

    triggered = False
    if same_sign and abs(avg_margin) > 0.001:
        ratio = current_margin / avg_margin
        triggered = ratio > k or ratio < (1.0 / k)
    else:
        triggered = abs(current_margin - avg_margin) > absolute_spread

    if not triggered:
        return raw_ebit, {
            "applied": False,
            "reason": "within_normal_range",
            "years_used": years_used,
            "avg_margin": avg_margin,
            "current_margin": current_margin,
        }

    normalized_ebit = avg_margin * current_revenue
    logger.info(
        f"{ticker}: cyclical EBIT normalization applied -- current margin "
        f"{current_margin:.1%} vs. {years_used}yr avg {avg_margin:.1%}, "
        f"using ${normalized_ebit:,.0f} instead of raw TTM ${raw_ebit:,.0f}"
    )
    return normalized_ebit, {
        "applied": True,
        "years_used": years_used,
        "avg_margin": avg_margin,
        "current_margin": current_margin,
        "raw_ebit": raw_ebit,
        "normalized_ebit": normalized_ebit,
    }


def calc_cyclical_normalized_reinvestment(
    ticker: str,
    current_capex: float,
    current_revenue: float,
    capex_annual_reports: list,
    ebit_annual_reports: list,
    cyclical_diag: dict,
) -> tuple[float, dict]:
    """
    Returns (capex_to_use, reinvest_diag). Extends Damodaran's cyclical-
    normalization method ("Ups and Downs: Valuing Cyclical and Commodity
    Companies", Sept 2009 -- *"we also have to normalize return on
    capital, reinvestment and cost of financing"*) from EBIT alone to
    capex, the reinvestment-side analogue -- fixes an "incomplete
    normalization" gap found live 2026-09-22: DHT/CSTM's reinvestment_rate
    was still built from today's real, unnormalized capex, which for a
    company at a cyclical peak is very likely also elevated, exactly
    parallel to the EBIT distortion calc_cyclical_normalized_ebit() fixes.

    STRICTLY GATED behind cyclical_diag["applied"] -- the EBIT-margin
    trigger calc_cyclical_normalized_ebit() already computed for THIS
    ticker, THIS run. Never has its own independent trigger logic. This is
    deliberate: a UNIVERSAL version of this exact technique (average
    capex/depreciation/working-capital-change over a multi-year window,
    applied to every ticker) was already tried in this codebase and
    reverted (docs/known_errors.md, introduced 2026-09-10, reverted
    2026-09-11 to 13) -- GOOG's real TTM capex was $132.4B, a 5yr average
    was $60.3B, a 54% understatement that masked GOOG's real, structural
    AI-infrastructure capex ramp, not a cyclical blip. Gating on the SAME
    calibrated test already used for EBIT (rather than a separately-
    derived capex trigger) means a structurally-scaling company can only
    reach this function if it ALSO independently trips the EBIT margin
    test -- confirmed live GOOG/AAPL/MSFT never do.

    Gating alone is necessary but not fully sufficient, though --
    calc_cyclical_normalized_ebit()'s own docstring already admits it
    "cannot distinguish a genuine temporary cyclical spike... from a
    genuine permanent structural margin improvement." A ticker that trips
    the EBIT gate for a STRUCTURAL reason (real scale-up, not a cyclical
    peak) would also get its capex wrongly normalized here -- the GOOG
    story again, just entered through a different door. Mitigated by a
    same-direction corroboration check, mirroring calc_gated_return_on_
    capital()'s existing ROIC-corroboration pattern (flag, don't silently
    exclude -- no ground truth exists to auto-decide this): if the capex
    margin ratio and the EBIT margin ratio move in the SAME direction
    relative to 1.0 (both compressed together, or both elevated together
    -- the genuine cyclical-peak signature, e.g. PARR/DHT/CSTM), normalize.
    If they move in OPPOSITE directions (EBIT compressed while capex
    margin EXPANDED -- real capacity build-out ahead of earnings catching
    up, the GOOG/META signature), do NOT normalize capex -- capex_to_use
    stays current_capex UNCHANGED, but reinvest_diag carries the
    disagreement so it's visible in notes/Excel, never silent. This never
    blocks the (already-verified-sound) EBIT normalization itself.

    Reuses cyclical_diag["years_used"] rather than an independently-
    derived capex window -- the EBIT gate's own verdict and window are
    what justified triggering at all; a separately-computed capex window
    could silently decouple the normalization from the trigger that
    justified it. capex_annual_reports/ebit_annual_reports are paired by
    index (both come from the same years_used-bounded window, revenue
    for the margin ratio always taken from ebit_annual_reports -- the
    exact series calc_cyclical_normalized_ebit() already used) -- degrades
    to a no-op (not partial-credit) if capex history is shorter than
    years_used, same "insufficient data -> skip, don't guess" philosophy
    calc_cyclical_normalized_ebit() already uses for its own min_years
    floor.

    Scope: capex only. Working-capital-change normalization is deferred
    (get_bal_sheet_intrinio_annual() doesn't currently expose the
    totalCurrentAssets/totalCurrentLiabilities fields calc_chng_wc() would
    need -- a real, unreviewed fetcher gap, not wired here) -- see
    docs/decisions.md. Depreciation is never normalized -- it reflects
    PAST capex already amortized over a useful life, smoothed by
    construction; normalizing it again would double-smooth.
    """
    if not cyclical_diag or not cyclical_diag.get("applied"):
        return current_capex, {"applied": False, "reason": "ebit_gate_not_triggered"}

    years_used = cyclical_diag.get("years_used", 0)
    paired = list(zip(capex_annual_reports[:years_used], ebit_annual_reports[:years_used]))
    capex_margins = []
    for capex_row, ebit_row in paired:
        capex = capex_row.get("capitalExpenditures")
        rev = ebit_row.get("totalRevenue")
        if capex is not None and rev and rev > 0:
            capex_margins.append(capex / rev)

    if len(capex_margins) < years_used or not current_revenue or current_revenue <= 0:
        return current_capex, {
            "applied": False,
            "reason": "insufficient_capex_history",
            "years_used": len(capex_margins),
        }

    avg_capex_margin = sum(capex_margins) / len(capex_margins)
    current_capex_margin = current_capex / current_revenue
    normalized_capex = avg_capex_margin * current_revenue

    ebit_avg_margin = cyclical_diag.get("avg_margin")
    ebit_current_margin = cyclical_diag.get("current_margin")
    ebit_ratio = (ebit_current_margin - ebit_avg_margin) if ebit_avg_margin is not None else 0.0
    capex_ratio = current_capex_margin - avg_capex_margin
    same_direction = (ebit_ratio >= 0) == (capex_ratio >= 0)

    if not same_direction:
        return current_capex, {
            "applied": False,
            "reason": "direction_mismatch_possible_structural_change",
            "years_used": years_used,
            "avg_capex_margin": avg_capex_margin,
            "current_capex_margin": current_capex_margin,
            "normalized_capex": normalized_capex,
        }

    logger.info(
        f"{ticker}: cyclical reinvestment normalization applied -- current "
        f"capex margin {current_capex_margin:.1%} vs. {years_used}yr avg "
        f"{avg_capex_margin:.1%}, using ${normalized_capex:,.0f} instead of "
        f"raw capex ${current_capex:,.0f}"
    )
    return normalized_capex, {
        "applied": True,
        "years_used": years_used,
        "avg_capex_margin": avg_capex_margin,
        "current_capex_margin": current_capex_margin,
        "raw_capex": current_capex,
        "normalized_capex": normalized_capex,
    }


def calc_adaptive_growth_rate(
    raw_ebit: float,
    normalized_ebit: float,
    growth_period: int,
    transition_period: int,
    stable_growth: float,
    tolerance: float = 1.0,
    max_iterations: int = 100,
) -> float | None:
    """
    Damodaran's "Adaptive Growth" compromise ("Ups and Downs: Valuing
    Cyclical and Commodity Companies", Sept 2009, NYU Stern) -- his own
    stated alternative to instant substitution (calc_cyclical_normalized_
    ebit()'s default behavior): *"allow earnings to follow the current
    cycle for the short term, and use the growth rate as a mechanism to
    bring us back to normalcy."* Rather than replacing the explicit
    period's starting EBIT outright, solve for a single flat growth_rate
    that -- fed through calc_expected_fcff()'s existing, UNMODIFIED 3-stage
    fade -- carries raw_ebit (today's real, current earnings) to
    normalized_ebit (the long-run target) by the time the model actually
    enters stable phase.

    Solves for ebit_n[-1] == normalized_ebit at the END of growth_period +
    transition_period, NOT at the end of growth_period alone. This
    distinction matters and was verified by hand, not assumed: a
    closed-form CAGR targeting growth_period alone overshoots once fed
    through the transition fade (which interpolates the RATE, not the
    EBIT LEVEL) -- for PARR's real numbers, targeting growth_period alone
    gives g=-31.9%, which then keeps dragging EBIT down through the
    transition years to a trough ~47% BELOW the normalized target before
    recovering. Targeting the true stable-phase entry point (g=-25.4% for
    PARR) produces a monotonic path landing within ~3% of the target
    throughout the tail. See docs/known_errors.md 2026-09-22 for the full
    hand-verification.

    Returns None when no real bridging rate exists: raw_ebit and
    normalized_ebit must share the same sign (and both be non-zero). Proof:
    every year's effective rate is a convex combination of growth_rate and
    stable_growth, both > -1 by construction, so (1+rate) never crosses
    zero -- sign is preserved through the entire trajectory, meaning a real
    geometric bridge between the two endpoints can only exist when they
    already share a sign. Callers must fall back to instant substitution
    in this case (a genuine trough -- current EBIT negative, reverting to a
    positive historical average -- is exactly Damodaran's own Toyota-shaped
    example, but the fallback, not this solver, is the right tool for it;
    instant substitution is itself one of Damodaran's three named methods,
    not a workaround).

    Implemented as bisection over calc_expected_fcff() itself (called with
    placeholder eff_tax_rate=0.0/reinvestment_rate=0.0 -- ebit_n depends
    only on growth_rate/growth_period/transition_period/stable_growth,
    confirmed by reading calc_expected_fcff() directly), not a closed-form
    CAGR -- the mixed flat-then-fading rate makes this genuinely non-
    closed-form once transition_period > 0.
    """
    if raw_ebit == 0 or normalized_ebit == 0 or (raw_ebit > 0) != (normalized_ebit > 0):
        return None

    def trajectory_end(g: float) -> float:
        _, ebit_n, _, _, _ = calc_expected_fcff(
            raw_ebit, 0.0, g, 0.0, growth_period,
            transition_period=transition_period, stable_growth=stable_growth,
            stable_reinvestment_rate=0.0,
        )
        return ebit_n[-1]

    lo, hi = -0.999, 20.0
    f_lo = trajectory_end(lo) - normalized_ebit
    f_hi = trajectory_end(hi) - normalized_ebit
    if f_lo == 0.0:
        return lo
    if f_hi == 0.0:
        return hi
    if (f_lo > 0) == (f_hi > 0):
        return None  # bracket failed to span the target -- degenerate, fall back

    mid = lo
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        f_mid = trajectory_end(mid) - normalized_ebit
        if abs(f_mid) < tolerance:
            return mid
        if (f_mid > 0) == (f_lo > 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return mid


def calc_adj_bv_equity(bal_sht, amort_schedule):
    if EQUITY_OVERRIDE is not None:
        base_equity = EQUITY_OVERRIDE
        logger.info(f"equity override active: using {base_equity:,.0f} instead of AV balance sheet")
    else:
        base_equity = bal_sht["total_stockholders_equity"][0]
    adjusted_bv_equity = base_equity + amort_schedule["RD_Asset_Value"]
    logger.info(f"adjusted BV Equity = {adjusted_bv_equity:,.2f}")
    return adjusted_bv_equity


def calc_bv_debt(bal_sht):
    bv_debt = bal_sht["short_term_debt"][0] + bal_sht["long_term_debt"][0]
    logger.info(f"BV Debt = {bv_debt:,.2f}")
    return bv_debt


def calc_tax_rate(inc_stmnt):
    income_before_tax = inc_stmnt["incomeBeforeTax"][0]
    if income_before_tax <= 0:
        # Loss-making company: effective rate is meaningless; use marginal rate
        logger.info(
            f"Negative/zero pre-tax income — using marginal tax rate {MARGINAL_TAX_RATE:.4f}"
        )
        return MARGINAL_TAX_RATE
    eff_tax_rate = inc_stmnt["income_tax_expense"][0] / income_before_tax
    # Clamp to [0, marginal rate] to prevent sign-flip in FCFF projections
    eff_tax_rate = min(max(eff_tax_rate, 0.0), MARGINAL_TAX_RATE)
    logger.info(f"Effective Tax Rate = {eff_tax_rate:,.4f}")
    return eff_tax_rate


def calc_return_on_capital(adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht):
    return_on_capital = adjusted_ebiat / (
        adjusted_bv_equity + bv_debt - bal_sht["cash_and_equivalents"][0]
    )
    logger.info(f"ROIC = {return_on_capital:,.4f}")
    return return_on_capital


def calc_gated_return_on_capital(
    ticker: str,
    adjusted_ebiat: float,
    adjusted_bv_equity: float,
    bv_debt: float,
    bal_sht: dict,
    inc_stmnt: dict,
    db_path: str | None = None,
) -> tuple[float | None, str]:
    """
    Guarded wrapper around calc_return_on_capital() — see docs/decisions.md
    "Capital-light compounder ROIC gate" (decided 2026-08-10) and
    docs/known_errors.md for the full writeup.

    calc_return_on_capital()'s denominator (equity + debt - cash) goes
    negative for cash-rich, heavy-buyback companies (confirmed live: AZO,
    EXPE, CCSI, PRDO, INOD, and others), producing a spurious, sign-flipped
    ROIC that misclassifies genuinely profitable businesses as wealth
    destroyers and collapses the moat-gated terminal value to zero.

    Returns (return_on_capital, notes):
    - invested_capital > 0: passthrough to calc_return_on_capital(), with a
      new corroboration check (decided 2026-08-20, docs/decisions.md) — if
      the result exceeds moat_scores' own avg_roic by more than
      ROIC_CORROBORATION_MAX_SPREAD (and avg_roic has at least
      ROIC_CORROBORATION_MIN_DATA_YEARS of history to be a credible
      baseline), notes carries a flag but the computed return_on_capital is
      returned UNCHANGED — this is an annotation, not a correction, same
      "flag, don't silently exclude/correct" pattern as sector balance and
      the staleness warning (docs/decisions.md). Missing/thin moat data
      never triggers the flag — consistent with every other gate in this
      function degrading to "no signal" rather than "more optimistic."
    - adjusted_ebiat <= 0 (regardless of invested capital): (None, reason) —
      a real operating loss is a real signal; never overridden by this gate.
    - adjusted_ebiat > 0 and invested_capital <= 0 (the ambiguous case): must
      clear all three gates to be treated as a wealth creator —
        1. Durability: positive EBIT in each of the last WEALTH_GATE_MIN_YEARS
           years (inc_stmnt["ebit"] already carries up to 5 years, no new
           fetch).
        2. Interest coverage >= WEALTH_GATE_MIN_INTEREST_COVERAGE (mirrors
           the existing int_cover pattern used for get_default_spread()).
        3. Corroboration from moat_score.py's own ROIC series, which already
           skips non-positive-invested-capital years: a Wide/Narrow moat
           rating with >= WEALTH_GATE_MIN_YEARS_ABOVE_WACC years above WACC.
      All three pass: returns moat_score.py's avg_roic (reusing its
      already-guarded computation rather than inventing a second heuristic).
      Any gate fails: (None, reason naming the failed gate(s)).

    Callers must treat a None result by writing a flagged, zeroed Stock_Value
    with notes=<reason> instead of continuing the DCF -- this is now the ONLY
    negative-book-equity-adjacent skip path (see docs/known_errors.md
    2026-08-25: a separate, cruder `adjusted_bv_equity < 0` guard used to fire
    before this function ever ran, unconditionally killing the DCF for any
    negative-book-equity ticker regardless of invested capital -- removed so
    every such ticker gets a real shot at the three-gate test above instead
    of being assumed to fail it). docs/decisions.md's existing "downstream
    consumers filter on non-empty notes" rule already makes a None-result row
    correctly invisible to replacer.py's candidate queries and skips
    portfolio_monitor.py's elimination check — no consumer-side changes
    needed.
    """
    cash = bal_sht["cash_and_equivalents"][0]
    invested_capital = adjusted_bv_equity + bv_debt - cash

    if invested_capital > 0:
        roc = calc_return_on_capital(adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht)
        _, _, moat_avg_roic, data_years = get_moat_corroboration(ticker, db_path)
        if (
            moat_avg_roic is not None
            and data_years is not None
            and data_years >= ROIC_CORROBORATION_MIN_DATA_YEARS
            and (roc - moat_avg_roic) > ROIC_CORROBORATION_MAX_SPREAD
        ):
            return roc, (
                f"Current ROIC ({roc:.1%}) exceeds moat_scores' "
                f"{data_years}yr average ({moat_avg_roic:.1%}) by more than "
                f"{ROIC_CORROBORATION_MAX_SPREAD:.0%} -- growth rate and "
                "wealth_pc are computed from the uncorroborated current "
                "figure; verify before trusting this as sustainable."
            )
        return roc, ""

    if adjusted_ebiat <= 0:
        return None, (
            "ROIC undefined -- negative/zero invested capital "
            f"({invested_capital:,.0f}) and non-positive earnings "
            f"(EBIAT {adjusted_ebiat:,.0f})"
        )

    # Ambiguous case: positive earnings, non-positive invested capital.
    ebit_hist = inc_stmnt.get("ebit", [])
    durable = (
        len(ebit_hist) >= WEALTH_GATE_MIN_YEARS
        and all(e > 0 for e in ebit_hist[:WEALTH_GATE_MIN_YEARS])
    )

    try:
        coverage = inc_stmnt["ebit"][0] / inc_stmnt["interest_expense"][0]
    except ZeroDivisionError:
        coverage = float("inf")  # no debt burden -- can't fail a coverage test
    covered = coverage >= WEALTH_GATE_MIN_INTEREST_COVERAGE

    moat_rating, years_above_wacc, moat_avg_roic, _ = get_moat_corroboration(ticker, db_path)
    corroborated = (
        moat_rating in ("Wide", "Narrow")
        and years_above_wacc is not None
        and years_above_wacc >= WEALTH_GATE_MIN_YEARS_ABOVE_WACC
        and moat_avg_roic is not None
    )

    if durable and covered and corroborated:
        logger.info(
            f"{ticker}: capital-light compounder -- standard ROIC undefined "
            f"(invested capital {invested_capital:,.0f}), cleared via "
            f"{WEALTH_GATE_MIN_YEARS}yr EBIT durability, {coverage:.1f}x "
            f"interest coverage, and moat_score.py's {moat_rating} rating "
            f"({years_above_wacc}yr above WACC) -- using moat_score's own "
            f"avg_roic ({moat_avg_roic:.1%}) in place of the undefined ratio"
        )
        return moat_avg_roic, ""

    reasons = []
    if not durable:
        reasons.append(f"< {WEALTH_GATE_MIN_YEARS}yr positive EBIT history")
    if not covered:
        reasons.append(f"interest coverage {coverage:.1f}x < {WEALTH_GATE_MIN_INTEREST_COVERAGE}x")
    if not corroborated:
        reasons.append("no corroborating Wide/Narrow moat rating with sufficient track record")
    return None, (
        f"ROIC undefined -- negative invested capital ({invested_capital:,.0f}), "
        "failed gate: " + "; ".join(reasons)
    )


def terminal_value_dominance_note(terminal_value_pv: float, market_cap: float) -> str:
    """
    Flag (never filter) when a DCF's terminal value dominates market cap by
    an extreme multiple -- decided 2026-08-27 after investigating why
    Value 10/20's top replacement candidates kept surfacing implausible
    93-99% margins of safety (see docs/decisions.md "Terminal value
    dominance flag" for the full investigation).

    Same "flag, don't silently exclude/correct" pattern as
    calc_gated_return_on_capital()'s ROIC-corroboration note and
    replacer.py's staleness annotation -- share_value/margin_of_safety/rank
    are returned completely unchanged; this only ever appends to `notes`.

    Deliberately NOT a permanent classification. `notes` is rebuilt from
    scratch on every single valuation run (this function is called fresh
    each time, off that run's own terminal_value/market_cap), so a ticker
    that trips this today and later stops -- a one-time event rolls out of
    the trailing-financials window, a company's distress eases, a vendor
    fixes a data bug -- simply stops carrying the note the very next time
    it's revalued. No separate reset step, expiry timer, or manual review
    queue is needed; the flag only ever reflects the most recent valuation.
    (It does depend on the ticker actually continuing to get revalued on
    the normal cadence -- an excluded/stale ticker keeps whatever note its
    last real valuation carried, same as every other field on that row.)

    A live cross-vendor + SEC-filing investigation of 9 flagged tickers
    (2026-08-27) found this doesn't distinguish *why* a ticker trips it --
    3 were genuine Ben Graham-style deep-value candidates where the model
    math itself is just unstable for that capital structure (not a data
    problem), 1 was a real one-time corporate event making trailing
    financials temporarily meaningless (also not a data problem, self-
    resolves in future quarters), and 2 were confirmed vendor-specific data
    bugs. All 9 were correctly worth a second look either way -- that's the
    intended behavior, not a limitation to fix.
    """
    if not market_cap or market_cap <= 0 or terminal_value_pv is None:
        return ""
    ratio = terminal_value_pv / market_cap
    if ratio <= TV_MARKET_CAP_MAX_RATIO:
        return ""
    return (
        f"Terminal value (${terminal_value_pv:,.0f}) is {ratio:.1f}x current "
        f"market cap (${market_cap:,.0f}) -- DCF result is dominated by the "
        "perpetuity-growth terminal period rather than near-term cash flows; "
        "verify before trusting this valuation."
    )


def low_growth_rate_note(growth_rate: float, risk_free_rate: float) -> str:
    """
    Flag (never filter) when a DCF's modeled growth rate doesn't even keep
    pace with the risk-free rate -- decided 2026-09-09 after HG (Hamilton
    Insurance Group) surfaced as a replacement candidate with a 1.4%
    modeled growth rate that Jim caught as not even beating inflation,
    while the candidate-screening report treated the low growth as a
    *virtue* (less terminal-value risk) rather than the quality concern it
    actually is. Same investigation also found the real bug behind HG's
    number specifically (_bank_payout_ratio() summing multiple years of
    dividends against one year's net income, see docs/known_errors.md
    2026-09-09) -- this flag is a second, independent layer: even a
    correctly-computed low growth rate is worth a second look, the same way
    a correctly-computed dominant terminal value is.

    Symmetric to terminal_value_dominance_note()'s ceiling (growth_rate
    capped at 30% when the model's own reinvestment math implies more) --
    this is the floor side of the same idea, which had no equivalent check
    before this. Same "flag, don't silently exclude" pattern as every other
    notes annotation in this file -- share_value/margin_of_safety/rank are
    never touched, this only ever appends to `notes`.

    Tied to RISK_FREE (recomputed fresh every run from FRED, see main())
    rather than a hardcoded inflation guess, matching how every other
    macro-sensitive check in this codebase already uses the run's own live
    inputs (ERP, risk-free rate) instead of a fixed constant that goes
    stale.

    Deliberately NOT wired into the REIT path (value_reit_stock() /
    _value_reit_stock_detail()) -- REITs already have their own explicit
    subtype_floor mechanism (_reit_growth_rate()) precisely because low
    retention-driven growth is structural and expected there (90%+ income
    distribution requirement, growth mostly comes from acquisitions funded
    outside retained earnings). Applying this flag to REITs would be noise,
    not signal -- see docs/known_errors.md 2026-09-09.
    """
    if growth_rate is None or risk_free_rate is None:
        return ""
    if growth_rate >= risk_free_rate:
        return ""
    return (
        f"Growth rate ({growth_rate:.1%}) is below the risk-free rate "
        f"({risk_free_rate:.1%}) -- this business is not compounding value "
        "fast enough to beat holding cash; verify this is a genuine quality "
        "read, not a fragile input, before trusting it as an attractive "
        "candidate."
    )


def extreme_reinvestment_rate_note(reinvestment_rate: float) -> str:
    """
    Flag (never filter) when the explicit-period reinvestment rate falls
    outside [0, 1] -- added 2026-09-10 alongside removing the hard clamp
    that used to force it into that band (`min(max(firm_reinvestment /
    adjusted_ebiat, 0.0), 1.0)`, both in _value_stock_fcff() and
    _value_stock_detail_fcff()).

    External review (ChatGPT, checked directly against the code before
    acting) flagged that the clamp silently substitutes a fundamentally
    different company for the one being valued whenever reinvestment truly
    falls outside [0,1] -- e.g. a company reinvesting 140% of NOPAT
    (negative FCFF while growing aggressively) got clamped to RR=100%,
    understating both its real growth rate and its real near-term cash
    burn; a mature company with negative reinvestment (depreciation +
    working-capital release exceeding capex, returning capital) got
    clamped to RR=0%, hiding that it's shrinking its capital base.

    Both are legitimate economic states, not data errors -- Damodaran's own
    framework allows reinvestment rates outside [0,1] for exactly these
    cases. The clamp wasn't a sanity check, it was silently swapping in a
    different growth story. Same "flag, don't filter" pattern as
    terminal_value_dominance_note()/low_growth_rate_note(): the raw ratio
    now flows through unmodified into growth_rate and the FCFF projections
    (which still have their own separate safety net -- the adjusted_ebiat==0
    raise above this call, untouched by this change), and this note just
    surfaces the unusual case for review rather than silently distorting
    the number to look "normal." The 30% growth-rate cap referenced here
    originally was itself converted from a hard clamp to a flag on
    2026-09-13 -- see high_growth_rate_note() below, same reasoning.

    Deliberately NOT applied to calc_stable_reinvestment_rate() (the
    terminal/stable-phase rate) -- that's a separate formula
    (stable_growth / stable_cost_of_capital) where staying in [0,1] is the
    correct Damodaran terminal-phase assumption itself (ROIC converges to
    WACC, no permanent excess returns), not an artificial clamp on a
    genuine outlier. See docs/known_errors.md 2026-09-10.
    """
    if reinvestment_rate is None:
        return ""
    if 0.0 <= reinvestment_rate <= 1.0:
        return ""
    return (
        f"Reinvestment rate ({reinvestment_rate:.1%}) falls outside the "
        "normal [0%, 100%] band -- this reflects either aggressive "
        "growth funded beyond NOPAT (negative FCFF) or a mature business "
        "releasing capital (negative reinvestment); verify this is a "
        "genuine read before trusting the resulting growth rate and FCFF."
    )


def high_growth_rate_note(growth_rate: float, cap: float = 0.30) -> str:
    """
    Flag (never cap) when the explicit-period growth rate exceeds `cap`.

    Changed 2026-09-13 from a hard `min(growth_rate, 0.30)` clamp to a flag
    -- Ginzu's own growth-rate formula (`Valuation Model!D15`) has no cap at
    all, confirmed by reading it directly. Same "flag, don't filter/
    silently correct" pattern as terminal_value_dominance_note() and
    low_growth_rate_note() (this is the ceiling to that function's floor --
    low_growth_rate_note()'s own docstring already anticipated this
    symmetry before this note existed).

    The unattended, ~2,300-ticker automated-screening context this system
    runs in is a real difference from Ginzu's interactive, one-company-at-
    a-time use case, where a human filling out the spreadsheet would
    immediately notice and reject an absurd growth rate. Silently capping
    masked that signal entirely (an implied 80% growth rate and a 31% one
    looked identical downstream); silently removing the cap with no flag
    would let a data-anomaly-driven number flow straight into a screen or
    replacement-candidate report with nobody watching. This flag is the
    middle path: pass the real number through, but make it visible.

    Only wired into the batch path (_value_stock_fcff()) -- the detail path
    (_value_stock_detail_fcff(), used for the single-ticker Excel report a
    human already reviews directly) has no `notes` field to flag through;
    removing its cap without a flag is correct there, the report itself is
    the review step. See docs/known_errors.md 2026-09-13.
    """
    if growth_rate is None or growth_rate <= cap:
        return ""
    return (
        f"Growth rate ({growth_rate:.1%}) exceeds {cap:.0%} -- Damodaran's "
        "own Ginzu model has no cap here, so this is passed through "
        "uncapped, but a rate this high usually reflects either a genuinely "
        "extraordinary competitive advantage or a data anomaly (a single "
        "distorted quarter feeding the reinvestment-rate/ROC inputs); "
        "verify before trusting it."
    )


def cyclical_ebit_normalization_note(diag: dict) -> str:
    """Flag when cyclical EBIT normalization actually fired -- unlike the
    other *_note() functions in this file, this documents a correction
    that already happened (see calc_cyclical_normalized_ebit()'s and
    calc_adaptive_growth_rate()'s docstrings for why this is not flag-
    only), not a result to independently verify.

    Two distinct modes, branched on here (2026-09-22): the primary path is
    Damodaran's "Adaptive Growth" (calc_adaptive_growth_rate() found a real
    bridging growth rate -- explicit period starts from real current EBIT,
    tapers to the normalized target by stable phase); the fallback is his
    other named method, instant substitution (used only when no real
    bridging rate exists -- raw/normalized EBIT don't share a sign, a
    genuine trough case). growth_rate is explicitly called out in both
    messages since this is the first place in this file it can diverge
    from reinvestment_rate x return_on_capital -- never a silent
    divergence.

    Only wired into the batch path (_value_stock_fcff()) -- the detail path
    has no `notes` field; it gets the same information as new detail-dict
    keys in the Excel report instead, same precedent as
    high_growth_rate_note()'s own batch-only wiring (see its docstring)."""
    if not diag or not diag.get("applied"):
        return ""
    if diag.get("adaptive_growth_fallback") == "instant_substitution":
        return (
            f"EBIT normalized using Damodaran's relative-margin method: TTM "
            f"operating margin ({diag['current_margin']:.1%}) diverges from the "
            f"{diag['years_used']}yr average margin ({diag['avg_margin']:.1%}) "
            f"beyond the cyclical-normalization threshold -- used the average "
            f"margin applied to current revenue (${diag['normalized_ebit']:,.0f}) "
            f"instead of raw TTM EBIT (${diag['raw_ebit']:,.0f}) via instant "
            "substitution (Damodaran's Adaptive Growth method was not usable here "
            "-- current and normalized EBIT don't share a sign); verify this "
            "reflects genuine cyclical mean reversion, not a real structural "
            "change in the business, before trusting the resulting growth rate."
        )
    return (
        f"EBIT normalized using Damodaran's Adaptive Growth method: TTM "
        f"operating margin ({diag['current_margin']:.1%}) diverges from the "
        f"{diag['years_used']}yr average margin ({diag['avg_margin']:.1%}) "
        f"beyond the cyclical-normalization threshold -- the explicit period "
        f"starts from the real, current EBIT (${diag['raw_ebit']:,.0f}) and "
        f"tapers via a solved growth rate ({diag.get('adaptive_growth_rate', 0):.1%}/yr) "
        f"to the normalized target (${diag['normalized_ebit']:,.0f}) by the time "
        "the model enters stable phase, rather than substituting the normalized "
        "figure outright -- growth_rate no longer equals reinvestment_rate x "
        "return_on_capital for this ticker as a result; verify this reflects "
        "genuine cyclical mean reversion, not a real structural change in the "
        "business, before trusting the resulting valuation."
    )


def cyclical_reinvestment_normalization_note(diag: dict) -> str:
    """Flag when calc_cyclical_normalized_reinvestment() actually swapped
    in the margin-normalized capex -- see its docstring for the gating/
    same-direction-guard design (2026-09-22). Like
    cyclical_ebit_normalization_note(), this documents a correction that
    already happened, not a result to independently verify. Also surfaces
    the direction-mismatch case (flag, no correction applied) so a
    reviewer can see the guard fired, not just silence.

    Only wired into the batch path -- same precedent as
    cyclical_ebit_normalization_note()'s own batch-only wiring."""
    if not diag:
        return ""
    if diag.get("reason") == "direction_mismatch_possible_structural_change":
        return (
            f"Reinvestment normalization NOT applied despite the EBIT gate "
            f"firing: capex margin ({diag['current_capex_margin']:.1%} vs. "
            f"{diag['years_used']}yr avg {diag['avg_capex_margin']:.1%}) moved "
            "in the OPPOSITE direction from the EBIT margin -- capex expanding "
            "while earnings compress is the signature of a real structural "
            "change (capacity build-out ahead of earnings), not a cyclical "
            "peak; using raw current capex, verify this ticker's reinvestment "
            "story directly before trusting the reinvestment rate."
        )
    if not diag.get("applied"):
        return ""
    return (
        f"Reinvestment normalized using Damodaran's relative-margin method "
        f"(same technique as the EBIT normalization above, gated on the same "
        f"trigger): capex margin ({diag['current_capex_margin']:.1%}) vs. the "
        f"{diag['years_used']}yr average ({diag['avg_capex_margin']:.1%}) -- "
        f"used the average margin applied to current revenue "
        f"(${diag['normalized_capex']:,.0f}) instead of raw capex "
        f"(${diag['raw_capex']:,.0f}); verify this reflects genuine cyclical "
        "mean reversion, not a real structural change, before trusting the "
        "resulting reinvestment rate."
    )


def calc_growth_rate(reinvestment_rate, return_on_capital):
    growth_rate = reinvestment_rate * return_on_capital
    logger.info(f"Growth Rate = {growth_rate:,.4f}")
    return growth_rate


def _bank_growth_rate(roe: float, retention_ratio: float) -> float:
    """Shared by value_bank_stock()/_value_bank_stock_detail() — identical
    formula in both, extracted per the av_fcff_2.py consolidation plan."""
    return min(roe * retention_ratio, 0.30)


def _reit_growth_rate(roe: float, retention_ratio: float, subtype_floor: float):
    """Shared by value_reit_stock()/_value_reit_stock_detail() — identical
    formula in both, extracted per the av_fcff_2.py consolidation plan.

    Returns (growth_rate, retained_growth) — retained_growth is its own
    Excel report field in the detail path, not just an intermediate.
    subtype_floor itself is NOT computed here: batch resolves it before any
    data fetch (to skip mortgage REITs early), detail resolves it after —
    each call site keeps its own gating exactly where it already is.
    """
    retained_growth = min(roe * retention_ratio, 0.15)
    return max(retained_growth, subtype_floor), retained_growth


def calc_reit_effective_retention_ratio(retention_ratio, affo, net_equity_issued, net_new_debt):
    """
    Extends the AFFO-retention ratio to include capital raised externally
    (net new equity + net new debt), matching Damodaran's own REIT-specific
    growth framework rather than the plain retention x ROE formula this
    system originally used unmodified for REITs.

    Added 2026-09-13, found comparing our REIT/AFFO model against
    Damodaran's own methodology: because REITs must distribute 90%+ of
    taxable income, real REIT growth overwhelmingly comes from acquisitions
    funded by newly issued equity/debt, not retained earnings -- a plain
    retention-ratio formula misses that channel entirely. Confirmed live on
    O (Realty Income, a REIT famous for exactly this growth-via-capital-
    markets model): the old formula alone produced a growth rate stuck at
    its 1.5% net-lease-escalator floor, well below O's long, well-
    documented mid-single-digit AFFO/share growth track record.

    net_equity_issued/net_new_debt: positive = net capital raised (adds to
    reinvestment), negative = net capital returned/repaid (reduces it) --
    same signed convention as get_cash_flow_intrinio()'s buybacks/
    net_new_debt fields (buybacks is reused directly here: for a REIT, a
    positive value means net equity issuance, not net repurchase).
    net_new_debt already nets issuance against repayment, so routine debt
    refinancing (REITs continuously roll maturing debt) doesn't get
    mistaken for real balance-sheet growth.

    Existing guardrails (the 15% cap and subtype-floor max() in
    _reit_growth_rate(), called with this function's output) are
    unchanged and still apply -- a REIT doing an unusually large one-year
    capital raise doesn't produce an unbounded growth rate.
    """
    if affo <= 0:
        return retention_ratio
    return retention_ratio + (net_equity_issued + net_new_debt) / affo


def calc_levered_beta(unlevered_beta, bv_debt, market_cap_equity, tax_rate, de_cap=None):
    """
    de_cap (optional): caps the D/E ratio used for re-levering at this value if
    the company's own D/E exceeds it — never raises D/E if the company's own is
    already lower. Used only for the stable/terminal-phase beta (pass the
    industry-average D/E via hg_dcflib.get_industry_de()) so an over-levered
    company's assumed perpetual leverage is capped toward a typical level,
    while an already-conservative company's IV is never inflated by assuming
    more leverage than it actually carries. See docs/decisions.md "Stable-phase
    capital structure" (decided 2026-07-22) — never pass de_cap for the
    explicit-period beta, which must always use the company's actual own D/E.
    """
    de_ratio = bv_debt / market_cap_equity if market_cap_equity > 0 else 0.0
    if de_cap is not None:
        de_ratio = min(de_ratio, de_cap)
    levered_beta = unlevered_beta * (1 + (1 - tax_rate) * de_ratio)
    logger.info(f"Levered beta = {levered_beta:,.4f} (unlevered {unlevered_beta:,.4f}, D/E {de_ratio:,.4f})")
    return levered_beta


def calc_interest_coverage(raw_ebit, interest_expense, bv_debt, risk_free):
    """
    Interest coverage ratio for the synthetic-rating cost-of-debt lookup
    (hg_dcflib.get_default_spread()).

    Added 2026-09-14. Replaces the old `try: ebit/interest_expense except
    ZeroDivisionError: int_cover = 25` pattern, which had two real problems:

    1. interest_expense == 0 doesn't always mean "no debt burden" -- it's
       also what a company with genuinely no disclosed gross interest
       expense line produces (confirmed live for AAPL; already documented
       for APA/PYPL in _intrinio_quarter_interest_expense()'s docstring --
       "a genuine filer-presentation gap... folded into Other income
       (expense), net"). A flat `int_cover = 25` fallback assumes top-tier
       (Aaa/AAA) credit regardless of the company's actual debt load.
       Damodaran's own prescribed remedy (his stated practice for companies
       that don't cleanly disclose interest expense): don't treat this as
       infinite/assumed coverage, IMPUTE a gross interest expense as
       `bv_debt x an assumed cost of debt`, bootstrapped from the same
       top-tier spread the old fallback assumed, then compute real coverage
       from that. Live-verified 2026-09-14: no-op for AAPL and APA (both
       independently strong enough to land in the same top bucket either
       way), but a real, correctly-directional effect for PYPL (imputed
       coverage 8.7x lands in the A1/A+ bucket instead of Aaa/AAA, IV
       -1.13%) -- confirms the fix only moves cases that actually need
       moving.

    2. A NEGATIVE interest_expense (the sign-flipped net-interest-income
       fallback in _intrinio_quarter_interest_expense(), used when a filer
       only reports one combined net interest line, e.g. AZO) previously
       divided straight through with no guard -- a negative numerator/
       denominator combination can produce a coverage ratio that looks
       superficially plausible while being computed from two wrong-signed
       inputs (confirmed live: INTC, currently negative EBIT / negative
       interest_expense = a coincidentally positive-looking 5.9x that means
       nothing), or for a positive-EBIT company, a genuinely negative
       coverage ratio that would fall into defaultSpread's worst bucket
       (GT=-100000, 19% spread) regardless of the company's real credit
       quality. Any non-positive interest_expense now routes to the same
       imputed-interest-expense path as the zero case.

    If bv_debt is also non-positive (genuinely no debt, not just unreported
    interest), this correctly degrades to the same `25` sentinel the old
    fallback always used -- a debt-free company legitimately deserves the
    top-tier bucket, no imputation needed or possible.

    See docs/known_errors.md 2026-09-14.
    """
    if interest_expense and interest_expense > 0:
        return raw_ebit / interest_expense
    if bv_debt <= 0:
        return 25
    bootstrap_cost_of_debt = risk_free + hg_dcflib.get_default_spread(25)
    imputed_interest_expense = bv_debt * bootstrap_cost_of_debt
    return raw_ebit / imputed_interest_expense


def calc_discount_rate(inc_stmnt, bv_debt, market_cap_equity, beta, risk_free, eq_prem, de_cap=None):
    # Re-lever the industry (unlevered) beta to this company's own capital
    # structure before computing cost of equity — see docs/known_errors.md
    # (2026-07-14: previously used the raw unlevered beta directly in CAPM).
    # de_cap: pass hg_dcflib.get_industry_de(industry) when computing the
    # stable/terminal-phase rate (beta = stable_beta) only — never for the
    # explicit-period call. See docs/decisions.md "Stable-phase capital
    # structure" (decided 2026-07-22).
    levered_beta = calc_levered_beta(beta, bv_debt, market_cap_equity, MARGINAL_TAX_RATE, de_cap=de_cap)
    cost_of_equity = risk_free + (levered_beta * eq_prem)
    logger.info(f"COE = {cost_of_equity:,.4f}")

    int_cover = calc_interest_coverage(
        inc_stmnt["ebit"][0], inc_stmnt["interest_expense"][0], bv_debt, risk_free
    )

    logger.info(f"Interest Coverage = {int_cover}")
    def_spread = hg_dcflib.get_default_spread(int_cover)
    logger.info(f"Default Spread = {def_spread}")

    cost_of_debt = (risk_free + def_spread) * (1 - MARGINAL_TAX_RATE)
    logger.info(f"Cost of Debt = {cost_of_debt}")
    total_capital = market_cap_equity + bv_debt
    percent_debt = bv_debt / total_capital if total_capital > 0 else 0.5
    percent_equity = 1 - percent_debt

    cost_of_capital = (cost_of_debt * percent_debt) + (cost_of_equity * percent_equity)
    logger.info(f"Cost of Capital = {cost_of_capital:,.4f}")
    return cost_of_capital


def calc_expected_fcff(
    adjusted_ebit, eff_tax_rate, growth_rate, reinvestment_rate, growth_period,
    transition_period=0, stable_growth=None, stable_reinvestment_rate=None,
):
    """
    Projects EBIT/EBIAT/FCFF year by year. Applying growth to EBIT (not
    EBIAT or FCFF) avoids sign errors when the current FCFF is negative due
    to high reinvestment.

    Three-stage fade (2026-09-11, external DCF review finding #6: see
    docs/known_errors.md) -- when transition_period > 0, growth_period
    years of constant explicit growth_rate/reinvestment_rate are followed
    by transition_period years where both fade LINEARLY toward
    stable_growth/stable_reinvestment_rate, reaching those targets exactly
    at the last transition year. This replaces the prior abrupt jump
    straight from the explicit rate to STABLE_GROWTH at the terminal
    boundary -- worst for tickers hitting the 30% growth cap (a 30%->3%
    overnight drop) -- with a gradual deceleration, the standard Damodaran
    3-stage structure. Default transition_period=0 preserves the exact
    prior two-stage behavior (the `else` branch below is never reached).

    Returns (fcff_n, ebit_n, ebiat_n, reinv_n, growth_n) -- reinv_n and
    growth_n are the per-year RATES actually used (constant for the first
    growth_period years, fading thereafter) -- not dollar amounts. Callers
    needing the terminal-year EBIT (for calc_terminal_value()) or the full
    trajectory for display don't need their own separate projection loop.
    Consolidates what used to be two independent implementations of this
    same math (the batch path via this function, the detail path via its
    own inline loop) -- see docs/known_errors.md 2026-07-31's rule on
    duplicated valuation-path math.
    """
    ebit_n = []
    ebiat_n = []
    fcff_n = []
    reinv_n = []
    growth_n = []
    total_years = growth_period + transition_period
    for year in range(total_years):
        if year < growth_period:
            g = growth_rate
            rr = reinvestment_rate
        else:
            fade_year = year - growth_period + 1  # 1..transition_period
            fraction = fade_year / transition_period
            g = growth_rate + (stable_growth - growth_rate) * fraction
            rr = reinvestment_rate + (stable_reinvestment_rate - reinvestment_rate) * fraction
        if year == 0:
            ebit_n.append(adjusted_ebit * (1 + g))
        else:
            ebit_n.append(ebit_n[year - 1] * (1 + g))
        ebiat = ebit_n[year] * (1 - eff_tax_rate)
        ebiat_n.append(ebiat)
        reinv_n.append(rr)
        growth_n.append(g)
        fcff_n.append(ebiat * (1 - rr))
        logger.info(
            f"Expected FCFF year {year + 1} = {fcff_n[year]:,.2f} "
            f"(growth={g:.4f}, reinvestment={rr:.4f})"
        )
    return fcff_n, ebit_n, ebiat_n, reinv_n, growth_n


def calc_fading_discount_rates(discount_rate, stable_cost_of_capital, growth_period, transition_period):
    """
    Per-year discount rate: flat `discount_rate` for growth_period years,
    then fades LINEARLY toward stable_cost_of_capital over transition_period
    years, reaching it exactly at the last year -- same shape as
    calc_expected_fcff()'s growth/reinvestment 3-stage fade.

    Added 2026-09-13: read Ginzu's own 'Valuation Model'!D47 formula
    directly (`=IF(year<B58/2, D31, D58+((D31-D58)/(B58/2))*(B58-year))`)
    and confirmed it fades the discount rate the same way, over the same
    transition window, that finding #6 (2026-09-11) already fades growth/
    reinvestment -- our engine deliberately left the discount rate flat at
    the time, a documented scope decision, not an oversight, but a real,
    identified divergence from Ginzu flagged as the leading suspect for
    MSFT's unexplained residual gap in the 2026-09-11 Ginzu comparison. See
    docs/known_errors.md 2026-09-13.

    transition_period=0 returns a flat list (identical to every call site's
    prior behavior) -- this is a strict extension, not a behavior change,
    for any caller not using the 3-stage fade.
    """
    rates = []
    total_years = growth_period + transition_period
    for year in range(total_years):
        if year < growth_period:
            rates.append(discount_rate)
        else:
            fade_year = year - growth_period + 1  # 1..transition_period
            fraction = fade_year / transition_period
            rates.append(discount_rate + (stable_cost_of_capital - discount_rate) * fraction)
    return rates


def _cumulative_discount_factors(discount_rates):
    """
    Cumulative product of (1+rate) through each year -- the correct
    multi-year discount factor when the rate varies by year, matching
    Ginzu's own 'Cumulated Cost of Capital' row (D48:R48). Takes a per-year
    list (see calc_fading_discount_rates()); callers handle the flat-rate
    case themselves (a simple (1+rate)**year, no list needed).
    """
    factors = []
    running = 1.0
    for rate in discount_rates:
        running *= 1 + rate
        factors.append(running)
    return factors


def calc_fcff_value(fcff_table, discount_rates, growth_period=None):
    """
    discount_rates: a single flat rate (float) applied to every year --
    preserves every existing caller's exact prior behavior -- or a per-year
    list (see calc_fading_discount_rates()), one entry per fcff_table year.
    growth_period is accepted for backward compatibility but ignored --
    len(fcff_table) is always the authoritative year count.
    """
    n = len(fcff_table)
    if isinstance(discount_rates, (int, float)):
        fcff_value = sum(fcff_table[year] / ((1 + discount_rates) ** (year + 1)) for year in range(n))
    else:
        factors = _cumulative_discount_factors(discount_rates)
        fcff_value = sum(fcff_table[year] / factors[year] for year in range(n))
    logger.info(f"FCFF Value = {fcff_value:,.2f}")
    return fcff_value


def calc_stable_reinvestment_rate(stable_growth, stable_cost_of_capital):
    """
    Stable-phase reinvestment rate: stable_growth / stable_cost_of_capital,
    i.e. ROIC converges to WACC in stable growth (no permanent excess
    returns) — the same assumption already used in value_bank_stock()'s and
    value_reit_stock()'s terminal value. See docs/known_errors.md 2026-07-31.
    """
    if stable_cost_of_capital <= 0:
        return 0.0
    return min(max(stable_growth / stable_cost_of_capital, 0.0), 1.0)


def calc_stable_phase_reinvestment_rate(
    stable_cost_of_capital, stable_growth, moat_weight=0.0, explicit_roic=None
):
    """
    The stable-phase reinvestment rate -- shared by calc_terminal_value()
    (the terminal Gordon-growth FCFF) and calc_expected_fcff()'s 3-stage
    fade (the target the transition years fade toward), single source of
    truth so the two can't drift apart. Extracted 2026-09-11 (finding #6)
    from what used to be an if/else duplicated inline in
    calc_terminal_value() alone; logic itself unchanged from 2026-08-01's
    moat-gated blend.
    """
    if moat_weight and explicit_roic is not None:
        assumed_stable_roic = stable_cost_of_capital + moat_weight * (explicit_roic - stable_cost_of_capital)
        if assumed_stable_roic > 0:
            return min(max(stable_growth / assumed_stable_roic, 0.0), 1.0)
        return 1.0
    return calc_stable_reinvestment_rate(stable_growth, stable_cost_of_capital)


def calc_terminal_value(
    ebit_last, stable_cost_of_capital, growth_cost_of_capital,
    stable_growth, growth_period, moat_weight=0.0, explicit_roic=None
):
    """
    Terminal value at the end of the explicit high-growth period, discounted
    back to present.

    growth_cost_of_capital: a single flat rate (float, discounted as
    (1+rate)**growth_period -- every caller's behavior before 2026-09-13) or
    a per-year list (see calc_fading_discount_rates()), discounted by the
    cumulative product of (1+rate) through the last year -- matching
    Ginzu's own terminal-value formula, `=D59/MAX(D48:R48)`.

    Terminal-year FCFF is recomputed at the stable-phase reinvestment rate
    (see calc_stable_reinvestment_rate()) rather than carrying forward the
    explicit period's — typically much higher — reinvestment rate. The
    previous version grew the last explicit year's FCFF by stable_growth
    directly, with no adjustment to reinvestment even though growth had just
    dropped from (say) double digits to 3% — understating terminal FCFF (and
    hence terminal value, usually 70-90%+ of total DCF value) for any company
    whose explicit reinvestment rate exceeds what stable growth actually
    requires, which is true for nearly every profitable growth company. See
    docs/known_errors.md 2026-07-31.

    moat_weight / explicit_roic (optional): blend the stable-phase
    reinvestment rate toward full ROIC persistence (weight=1) instead of
    pure WACC-convergence (weight=0, the default — identical to the
    2026-07-31 behavior). See get_moat_weight() and docs/known_errors.md
    2026-08-01 "Moat-gated stable-phase ROIC assumption".

    Terminal-year EBIAT is taxed at MARGINAL_TAX_RATE, not the explicit
    period's (possibly much lower) effective tax rate -- fixed 2026-09-10
    per Damodaran's stated practice that a firm's tax rate should transition
    toward the marginal rate by the stable-growth phase (NOLs, R&D credits,
    and other current-period tax advantages don't persist forever). No
    longer takes eff_tax_rate as a parameter -- it was only ever used for
    this one calculation, and using the current-period rate here was the
    bug. Also keeps this consistent with the stable-phase WACC, which
    already assumes MARGINAL_TAX_RATE for the debt tax shield (see
    calc_discount_rate()) -- previously the terminal discount rate assumed
    a marginal-tax regime while the terminal cash flow it discounted did
    not. See docs/known_errors.md 2026-09-10.
    """
    stable_reinv_rate = calc_stable_phase_reinvestment_rate(
        stable_cost_of_capital, stable_growth, moat_weight, explicit_roic
    )
    terminal_ebit = ebit_last * (1 + stable_growth)
    terminal_ebiat = terminal_ebit * (1 - MARGINAL_TAX_RATE)
    fcff_terminal = terminal_ebiat * (1 - stable_reinv_rate)
    # Gordon Growth requires cost of capital > growth rate — otherwise this
    # denominator is zero or negative and terminal value is undefined. Not a
    # theoretical concern: a real, unguarded ZeroDivisionError hit CLMB, OSW,
    # RCKY during the 2026-07-31 Russell 2000 triage. See docs/known_errors.md.
    if stable_cost_of_capital <= stable_growth:
        raise ValueError(
            f"Stable-phase cost of capital ({stable_cost_of_capital:.4f}) is at or "
            f"below the stable growth rate ({stable_growth:.4f}) — terminal value "
            f"is undefined (requires cost of capital > growth)."
        )
    terminal_value = fcff_terminal / (stable_cost_of_capital - stable_growth)
    if isinstance(growth_cost_of_capital, (int, float)):
        cumulative_factor = (1 + growth_cost_of_capital) ** growth_period
    else:
        cumulative_factor = _cumulative_discount_factors(growth_cost_of_capital)[-1]
    terminal_value_pv = terminal_value / cumulative_factor
    logger.info(f"Stable reinvestment rate = {stable_reinv_rate:,.4f}")
    logger.info(f"Terminal Value = {terminal_value_pv:,.2f}")
    return terminal_value_pv


def calc_intrinsic_value(
    fcff_pv, terminal_value_pv, cash_and_equivalents, bv_debt, shares_outstanding
):
    enterprise_value = fcff_pv + terminal_value_pv + cash_and_equivalents - bv_debt
    intrinsic_value = enterprise_value / shares_outstanding
    logger.info(f"Enterprise Value = {enterprise_value:,.2f}")
    logger.info(f"Intrinsic Value = {intrinsic_value:,.2f}")
    return intrinsic_value


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------


def create_table(conn):
    schema_sql = """CREATE TABLE IF NOT EXISTS valuation (
              ticker TEXT NOT NULL,
              valuation_date TEXT NOT NULL,
              ent_name TEXT NOT NULL,
              industry TEXT NOT NULL,
              cik TEXT NOT NULL DEFAULT '',
              beta REAL NOT NULL,
              market_cap REAL NOT NULL,
              price REAL NOT NULL,
              shares_outstanding REAL NOT NULL,
              risk_free_rate REAL NOT NULL,
              eq_premium REAL NOT NULL,
              growth_rate REAL NOT NULL,
              cost_of_capital REAL NOT NULL,
              wealth_pc REAL NOT NULL,
              fcff_value REAL NOT NULL,
              terminal_value REAL NOT NULL,
              share_value REAL NOT NULL,
              margin_of_safety REAL NOT NULL,
              margin_of_safety_pc REAL NOT NULL,
              target_price REAL NOT NULL DEFAULT 0,
              earnings_yield REAL NOT NULL DEFAULT 0,
              dividend_yield REAL NOT NULL DEFAULT 0,
              notes TEXT NOT NULL DEFAULT '',
              analyst_count INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (ticker)
              );"""
    try:
        # Check if table exists with old (ticker, valuation_date) composite PK
        # and migrate if needed so that each ticker has only one row.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='valuation'"
        ).fetchone()
        if row and "PRIMARY KEY (ticker, valuation_date)" in row[0]:
            logger.info("Migrating valuation table to single-ticker primary key ...")
            # DDL (ALTER/CREATE/DROP) bypasses Python's automatic transaction
            # management, so we disable it temporarily and use explicit SQL
            # BEGIN/COMMIT/ROLLBACK to guarantee atomicity.
            orig_isolation = conn.isolation_level
            conn.isolation_level = None
            try:
                conn.execute("BEGIN EXCLUSIVE")
                conn.execute("ALTER TABLE valuation RENAME TO valuation_old")
                conn.execute(schema_sql)
                # Explicit destination column list — cik and target_price are
                # new columns not present in the old table; they receive their
                # DEFAULT values ('', 0) automatically.
                conn.execute("""
                    INSERT INTO valuation (
                        ticker, valuation_date, ent_name, industry, beta, market_cap,
                        price, shares_outstanding, risk_free_rate, eq_premium, growth_rate,
                        cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value,
                        margin_of_safety, margin_of_safety_pc)
                    SELECT ticker, valuation_date, ent_name, industry, beta, market_cap,
                           price, shares_outstanding, risk_free_rate, eq_premium, growth_rate,
                           cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value,
                           margin_of_safety, margin_of_safety_pc
                    FROM (
                        SELECT *, ROW_NUMBER() OVER (PARTITION BY ticker ORDER BY valuation_date DESC) rn
                        FROM valuation_old
                    ) WHERE rn = 1
                """)
                conn.execute("DROP TABLE valuation_old")
                conn.execute("COMMIT")
                logger.info("Migration complete.")
            except Exception as exc:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise RuntimeError(f"Migration failed and was rolled back: {exc}") from exc
            finally:
                conn.isolation_level = orig_isolation
        else:
            conn.execute(schema_sql)
            conn.commit()
            logger.info("Table created successfully")
        # Add columns to existing tables that pre-date these fields
        for col_def in (
            "target_price REAL NOT NULL DEFAULT 0",
            "cik TEXT NOT NULL DEFAULT ''",
            "earnings_yield REAL NOT NULL DEFAULT 0",
            "dividend_yield REAL NOT NULL DEFAULT 0",
            "notes TEXT NOT NULL DEFAULT ''",
            "analyst_count INTEGER NOT NULL DEFAULT 0",
        ):
            try:
                conn.execute(f"ALTER TABLE valuation ADD COLUMN {col_def}")
                conn.commit()
                logger.info(f"Added column {col_def.split()[0]} to existing table")
            except sqlite3.OperationalError:
                pass  # column already exists
    except sqlite3.OperationalError as e:
        logger.warning(f"Failed to create tables: {e}")


def insert_valuation(conn, val):
    c = conn.cursor()
    c.execute(
        """INSERT OR REPLACE INTO valuation
        (ticker, valuation_date, ent_name, industry, cik, beta, market_cap, price,
            shares_outstanding, risk_free_rate, eq_premium, growth_rate,
            cost_of_capital, wealth_pc, fcff_value, terminal_value, share_value,
            margin_of_safety, margin_of_safety_pc, target_price, earnings_yield,
            dividend_yield, notes, analyst_count)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            val.ticker,
            val.valuation_date,
            val.ent_name,
            val.industry,
            val.cik,
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
            val.target_price,
            val.earnings_yield,
            val.dividend_yield,
            val.notes,
            val.analyst_count,
        ),
    )
    conn.commit()


def _rescore_tickers(db_path: str, tickers: list) -> None:
    """Re-score composite_scores for the given tickers immediately after valuation.

    Keeps composite scores in sync with valuations on a per-ticker basis so
    a full composite_score.py run is only needed after a bulk refresh.
    Errors are logged and swallowed — a scoring failure never aborts a valuation.
    """
    if not tickers:
        return
    try:
        from composite_score import (
            score_row, upsert, ensure_table as ensure_score_table,
        )
    except ImportError:
        logger.warning("composite_score not importable; skipping auto-rescore")
        return
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        ensure_score_table(conn)
        moat_map = {r["ticker"]: dict(r)
                    for r in conn.execute("SELECT * FROM moat_scores").fetchall()}
        fd_map   = {r["ticker"]: dict(r)
                    for r in conn.execute("SELECT * FROM financial_data").fetchall()}
        for ticker in tickers:
            vrow = conn.execute(
                "SELECT * FROM valuation WHERE ticker=?", (ticker,)
            ).fetchone()
            if not vrow:
                continue
            r = score_row(dict(vrow), moat_map.get(ticker, {}), fd_map.get(ticker, {}))
            upsert(conn, r)
            logger.info(
                f"Composite score updated: {ticker} → {r['total_score']} ({r['designation']})"
            )
        conn.close()
        label = tickers[0] if len(tickers) == 1 else f"{len(tickers)} tickers"
        print(f"  Composite score(s) updated: {label}")
    except Exception as e:
        logger.warning(f"Composite score auto-update failed: {e}")


# ---------------------------------------------------------------------------
# Bank / financial-firm valuation  (FCFE equity DCF)
# ---------------------------------------------------------------------------


def _bank_payout_ratio(
    net_income: float, bv_equity_curr: float, bv_equity_prior: float, cash_flw: dict
) -> float:
    """
    Determine payout ratio for a bank using the most reliable source available.

    Priority:
      1. Dividends + buybacks from cash flow (most direct; sum of quarterly outflows)
      2. Equity-change method (net income minus equity retained on balance sheet)
      3. Fallback: 40% payout (typical for well-run regional bank)

    Method 1 fixed 2026-09-13 to include buybacks, not just dividends --
    found while checking whether the bank/FCFE model matches Damodaran's own
    framework for financial firms, which treats total cash returned to
    shareholders (dividends + repurchases) as the FCFE proxy, not dividends
    alone. Dividends-only silently passed this method's own [0.05, 0.95]
    sanity check even for companies returning most of their capital via
    buybacks instead, so Method 2 (which would have implicitly captured
    buybacks via the balance-sheet equity change) never got a chance to run.
    Confirmed live on RJF: dividends-only payout 18.6% vs. true total payout
    ~91% (TTM buybacks $1,672M vs. dividends $428M against $2,306M net
    income) -- understated FCFE by roughly 5x in the explicit period,
    IV $44.47 vs. a corrected $111.52 (Morningstar: $187). See
    docs/known_errors.md 2026-09-13.

    AOCI swings (unrealized bond gains/losses) inflate the equity-change figure,
    so if that method would imply retention > 80% we prefer the dividend method.

    cash_flw["dividends_paid"] is ALREADY one already-annualized figure per
    year (both get_cash_flow() and get_cash_flow_intrinio() sum each year's
    4 quarters before appending -- confirmed by reading both), most-recent-
    first, with up to 5 years of history. Method 1 must use only index [0]
    (the most recent year) -- summing the whole list sums MULTIPLE YEARS of
    dividends against a single year's net_income, inflating payout by
    roughly Nx (N = years of history fetched). Fixed 2026-09-09: found live
    on HG (Hamilton Insurance Group) -- 3 years of dividends [$472.3M,
    $212.0M, $127.9M] were being summed to $812.2M and divided by one
    year's net income ($862.8M), producing a 94.1% payout ratio (5.9%
    retention, growth_rate=1.45%) -- when the correct single-year payout is
    54.75% (45.25% retention, growth_rate≈11.2%), a difference material
    enough to significantly change intrinsic value. This bug is old (present
    in the original AV-only code, not something the Intrinio migration
    introduced) and affects every bank/insurance-routed ticker with more
    than one year of dividend history fetched -- likely the real
    explanation behind several previously-flagged-but-never-investigated
    "bad-looking" bank valuations (ALRS, HASI) from the 2026-08-31 12-ticker
    classification sample. See docs/known_errors.md 2026-09-09.
    """
    payout = None

    # --- Method 1: actual dividends + buybacks paid (most recent year only) ---
    div_history = cash_flw.get("dividends_paid", [])
    divs = abs(div_history[0]) if div_history and div_history[0] else 0.0
    buyback_history = cash_flw.get("buybacks", [])
    buybacks = abs(buyback_history[0]) if buyback_history and buyback_history[0] else 0.0
    total_returned = divs + buybacks
    if net_income > 0 and total_returned > 0:
        payout_from_total = total_returned / net_income
        if 0.05 <= payout_from_total <= 0.95:
            payout = payout_from_total
            logger.info(
                f"Payout ratio from dividends+buybacks: {payout:.4f} "
                f"(dividends {divs:,.0f}, buybacks {buybacks:,.0f})"
            )

    # --- Method 2: equity-change (only use if dividends unavailable/unreliable) ---
    if payout is None and net_income > 0:
        equity_change = bv_equity_curr - bv_equity_prior
        retention = equity_change / net_income
        if 0.0 <= retention <= 0.80:  # AOCI likely not distorting
            payout = 1.0 - retention
            logger.info(f"Payout ratio from equity change: {payout:.4f}")

    # --- Method 3: fallback ---
    if payout is None:
        payout = 0.40
        logger.info(f"Payout ratio: using fallback {payout:.4f}")

    return payout


def value_bank_stock(ticker: str, growth_period: int):
    """
    Excess Return model for banks and financial firms (Damodaran, "Valuing
    Financial Service Firms", April 2009) — replaces the FCFE/dividend-
    discount model 2026-09-17. See docs/known_errors.md 2026-09-17.

    Key differences from FCFF:
    - Value = book equity + PV(excess returns), not a discounted cash-flow stream
    - Excess return = (ROE − Cost of Equity) × book equity
    - Reinvestment (g / ROE) still grows book equity, not a payout stream
    - Discounts at Cost of Equity, not WACC
    - No debt/cash adjustment — we work at the equity level throughout
    - No R&D capitalisation (not applicable to financials)
    - Terminal value is zero by construction (no Gordon Growth term)
    """
    logger.info(f"Valuing {ticker} as financial firm (Excess Return)")
    try:
        industry = hg_dcflib.get_industry(ticker)
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY, is_financial_or_reit=True)
        bal_sht = balance_sheet(ticker, MY_API_KEY, is_financial_or_reit=True)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        valuation_date = str(date.today())
        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]
        dividend_yield = ent_quote[4]
        analyst_count = int(ent_quote[5])
        cik = hg_dcflib.get_cik(ticker)

        reported_net_income = inc_stmnt["netIncome"][0]
        if len(bal_sht["total_stockholders_equity"]) < 2:
            raise ValueError("Insufficient balance sheet history (need 2 years) for bank Excess Return model")
        bv_equity_curr = bal_sht["total_stockholders_equity"][0]
        bv_equity_prior = bal_sht["total_stockholders_equity"][1]

        # Insurance firms: normalize NI over available years to smooth
        # underwriting cycles and catastrophe years.
        if is_insurance_firm(industry):
            net_income, ni_years = _normalized_net_income(inc_stmnt["netIncome"])
            logger.info(
                f"Insurance: normalized NI over {ni_years} years = {net_income:,.0f}  (TTM = {reported_net_income:,.0f})"
            )
        else:
            net_income = reported_net_income
            ni_years = 1

        # --- ROE and growth ---
        roe = net_income / bv_equity_curr if bv_equity_curr != 0 else 0.0
        logger.info(f"ROE = {roe:.4f}")

        payout_ratio = _bank_payout_ratio(
            reported_net_income, bv_equity_curr, bv_equity_prior, cash_flw
        )
        retention_ratio = 1.0 - payout_ratio
        growth_rate = _bank_growth_rate(roe, retention_ratio)
        logger.info(
            f"Growth rate = {growth_rate:.4f}  (ROE={roe:.4f} × retention={retention_ratio:.4f})"
        )

        # --- Cost of equity (no WACC — debt is operational for banks) ---
        bv_debt = calc_bv_debt(bal_sht)
        levered_beta = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        cost_of_equity = RISK_FREE + (levered_beta * EQ_PREM)
        logger.info(f"Cost of Equity = {cost_of_equity:.4f}")

        # --- Excess Return model (Damodaran, "Valuing Financial Service
        # Firms", April 2009) — replaces the FCFE/dividend-discount model,
        # 2026-09-17. Book equity is a more meaningful anchor for a financial
        # firm than a payout-ratio-driven cash flow stream: dividend payout
        # for a regulated financial firm is set by regulatory capital
        # requirements, not reinvestment needs — exactly why
        # _bank_payout_ratio() produced two real bugs this month (multi-year
        # dividend summing, 2026-09-09; PYPL payout fragility). growth_rate
        # (still ROE × retention, via _bank_growth_rate()/_bank_payout_ratio()
        # above) now only grows book equity, not a cash-flow stream. ROE and
        # cost of equity are held flat through the explicit period — same
        # treatment the old model already gave growth_rate/cost_of_equity. A
        # 3-stage Ginzu-style fade is deliberately out of scope here — see
        # docs/known_errors.md 2026-09-17.
        #
        #   value of equity = book value of equity + PV(excess returns)
        #   excess_return_t = (ROE − cost of equity) × book equity_(t-1)
        #
        # Terminal value is zero BY CONSTRUCTION: ROE is assumed to converge
        # to cost of equity beyond the explicit period (the same "competitive
        # equilibrium" philosophy the old stable phase used, reached directly
        # here instead of via a Gordon Growth formula) — no terminal-value
        # division, no CLMB/OSW/RCKY-class division-by-zero risk
        # (docs/known_errors.md 2026-08-03) for this model.
        bv_equity_n = []
        excess_return_n = []
        bv_equity_prev = bv_equity_curr
        for year in range(growth_period):
            bv_equity_t = bv_equity_prev * (1 + growth_rate)
            excess_return_t = (roe - cost_of_equity) * bv_equity_prev
            bv_equity_n.append(bv_equity_t)
            excess_return_n.append(excess_return_t)
            logger.info(f"Excess return year {year + 1} = {excess_return_t:,.2f}")
            bv_equity_prev = bv_equity_t

        excess_return_pv = sum(
            excess_return_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period)
        )

        equity_value = bv_equity_curr + excess_return_pv
        intrinsic_value = equity_value / shares_outstanding  # both in consistent units

        safety_margin = float(intrinsic_value - price)
        safety_margin_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0.0
        )
        wealth_pc = roe - cost_of_equity
        target_price = intrinsic_value * (1 + cost_of_equity)

        logger.info(f"Intrinsic value = {intrinsic_value:.2f}  Price = {price:.2f}")

        return Stock_Value(
            ticker=ticker,
            valuation_date=valuation_date,
            ent_name=ent_name,
            industry=industry,
            cik=cik,
            beta=levered_beta,
            market_cap=market_cap,
            price=price,
            shares_outstanding=shares_outstanding,
            risk_free_rate=RISK_FREE,
            eq_premium=EQ_PREM,
            growth_rate=growth_rate,
            cost_of_capital=cost_of_equity,  # equity rate, not WACC
            wealth_pc=wealth_pc,
            fcff_value=excess_return_pv,  # PV of excess returns
            terminal_value=0.0,  # zero by construction — see comment above
            share_value=intrinsic_value,
            margin_of_safety=safety_margin,
            margin_of_safety_pc=safety_margin_pc,
            notes=" | ".join(
                n for n in (
                    terminal_value_dominance_note(0.0, market_cap),
                    low_growth_rate_note(growth_rate, RISK_FREE),
                ) if n
            ),
            target_price=target_price,
            earnings_yield=0.0,  # Excess Return model — EBIT/EV not applicable for banks
            dividend_yield=dividend_yield,
            analyst_count=analyst_count,
        )

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Single-stock valuation
# ---------------------------------------------------------------------------


def value_stock(ticker: str, growth_period: int, db_path: str | None = None):
    """
    Route to the correct valuation model based on industry:
      - SIC pre-filter → SEC's own SIC code, when available and outside the
        Finance/Insurance/Real-Estate division, sends the ticker straight to
        FCFF regardless of Damodaran's bucket. Never forces bank/insurance/
        REIT routing on its own -- an in-range SIC just falls through to the
        checks below unchanged. See is_financial_sic() and
        docs/known_errors.md 2026-09-01.
      - REITs → skipped (FCFF/FCFE not applicable; FFO/AFFO model pending Phase 2)
      - Financial firms (banks, insurance, etc.) → FCFE equity DCF
      - All others → FCFF firm DCF
    """
    try:
        industry = hg_dcflib.get_industry(ticker)
    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        return None

    sic = hg_dcflib.get_sic(ticker, INTRINIO_KEY)
    if sic is not None and not is_financial_sic(sic):
        return _value_stock_fcff(ticker, growth_period, industry, db_path)

    if is_reit(industry):
        return value_reit_stock(ticker, growth_period)

    if is_financial_firm(industry):
        return value_bank_stock(ticker, growth_period)

    return _value_stock_fcff(ticker, growth_period, industry, db_path)


def _value_stock_fcff(ticker: str, growth_period: int, industry: str, db_path: str | None = None):
    """
    FCFF DCF valuation for non-financial firms.
    Returns a Stock_Value dataclass or None if any step fails.
    """
    logger.info(f"Valuing {ticker} ...")
    try:
        rd_years = hg_dcflib.get_rAndD_years(industry) + 1
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        valuation_date = str(date.today())
        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]
        dividend_yield = ent_quote[4]
        analyst_count = int(ent_quote[5])
        cik = hg_dcflib.get_cik(ticker)

        stable_beta = calc_stable_beta(unlevered_beta)
        eff_tax_rate = calc_tax_rate(inc_stmnt)
        fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate)

        ebiat = fcff_data[0]
        capex = fcff_data[1]
        chng_nc_wc = fcff_data[2]
        depreciation = fcff_data[3]

        amort_schedule = capitalizerAndD(ticker, rd_years, MY_API_KEY)
        logger.info(f"Amortization Schedule {amort_schedule}")

        # Cyclical EBIT normalization (Damodaran's "relative average over
        # time" method, 2026-09-22) — must run BEFORE calc_adj_ebit(), on
        # the raw TTM figure, same as the R&D adjustment that follows it.
        # Wrapped in its own try/except that degrades to the raw TTM figure
        # on any failure — a thin annual-history fetch failing must never
        # add a new failure mode to the nightly ~2,300-ticker batch. See
        # calc_cyclical_normalized_ebit()'s docstring.
        try:
            annual_reports = annual_income_statement(ticker, MY_API_KEY)
            cyclical_ebit, cyclical_diag = calc_cyclical_normalized_ebit(
                ticker, inc_stmnt["ebit"][0], inc_stmnt["totalRevenue"][0], annual_reports
            )
        except Exception as exc:
            logger.warning(f"{ticker}: cyclical EBIT normalization skipped ({exc}) — using raw TTM EBIT.")
            cyclical_ebit, cyclical_diag = inc_stmnt["ebit"][0], {"applied": False, "reason": f"fetch_failed: {exc}"}

        # Pre-tax adjusted EBIT — used as the base for projections so that
        # growth is applied to EBIT rather than EBIAT or FCFF. Adjust at the
        # EBIT level (2026-09-10 fix), then derive EBIAT from it — not the
        # other way around. See calc_adj_ebit()'s docstring.
        #
        # Adaptive Growth (Damodaran's own named alternative to instant
        # substitution, 2026-09-22 — see calc_adaptive_growth_rate()'s
        # docstring): when cyclical normalization triggers, the explicit
        # period starts from the REAL, current EBIT (not the normalized
        # figure) and instead solves for a growth rate that carries it to
        # the normalized target by the time the model enters stable phase.
        # Falls back to instant substitution only when no real bridging
        # rate exists (raw/normalized EBIT don't share a sign — a genuine
        # trough case).
        adaptive_growth_rate = None
        adjusted_ebit = calc_adj_ebit(inc_stmnt["ebit"][0], amort_schedule)
        if cyclical_diag.get("applied"):
            adjusted_normalized_ebit = calc_adj_ebit(cyclical_diag["normalized_ebit"], amort_schedule)
            adaptive_growth_rate = calc_adaptive_growth_rate(
                adjusted_ebit, adjusted_normalized_ebit, growth_period, TRANSITION_PERIOD, STABLE_GROWTH
            )
            cyclical_diag["adaptive_growth_rate"] = adaptive_growth_rate
            if adaptive_growth_rate is None:
                adjusted_ebit = adjusted_normalized_ebit
                cyclical_diag["adaptive_growth_fallback"] = "instant_substitution"
        adjusted_ebiat = adjusted_ebit * (1 - eff_tax_rate)

        # Cyclical reinvestment (capex) normalization (2026-09-22 follow-up
        # to the EBIT fix above) — gated behind the SAME cyclical_diag the
        # EBIT normalization already computed; never an independent
        # trigger. See calc_cyclical_normalized_reinvestment()'s docstring
        # for the gating design and the same-direction guard.
        reinvest_capex = capex
        reinvest_diag = {"applied": False, "reason": "ebit_gate_not_triggered"}
        if cyclical_diag.get("applied"):
            try:
                capex_annual = annual_cash_flow_statement(
                    ticker, MY_API_KEY, years=cyclical_diag["years_used"]
                )
                reinvest_capex, reinvest_diag = calc_cyclical_normalized_reinvestment(
                    ticker, capex, inc_stmnt["totalRevenue"][0],
                    capex_annual, annual_reports, cyclical_diag,
                )
            except Exception as exc:
                logger.warning(f"{ticker}: cyclical reinvestment normalization skipped ({exc}) — using raw capex.")

        firm_reinvestment = calc_reinvestment(
            reinvest_capex, depreciation, chng_nc_wc, amort_schedule
        )
        adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
        bv_debt = calc_bv_debt(bal_sht)

        # Sanity check: if |EBIT| dwarfs the market cap by more than 10×,
        # the quarterly working-capital data is almost certainly corrupted.
        if market_cap > 0 and abs(adjusted_ebit) > 10 * market_cap:
            raise ValueError(
                f"Adjusted EBIT ({adjusted_ebit:,.0f}) is > 10× market cap "
                f"({market_cap:,.0f}) — likely bad WC data, skipping."
            )

        if adjusted_ebiat == 0:
            raise ValueError(
                f"Adjusted EBIAT is zero for {ticker} — cannot compute reinvestment rate."
            )
        # Not clamped to [0,1] -- a company can legitimately reinvest more
        # than 100% of NOPAT (negative FCFF, aggressive growth) or less than
        # 0% (mature, releasing capital). See extreme_reinvestment_rate_note()
        # docstring and docs/known_errors.md 2026-09-10.
        reinvestment_rate = firm_reinvestment / adjusted_ebiat
        logger.info(f"Reinvestment rate = {reinvestment_rate:,.4f}")

        return_on_capital, roc_notes = calc_gated_return_on_capital(
            ticker, adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht, inc_stmnt, db_path
        )
        if return_on_capital is None:
            logger.warning(f"{ticker}: {roc_notes}")
            return Stock_Value(
                ticker=ticker, valuation_date=valuation_date, ent_name=ent_name,
                industry=industry, cik=cik,
                beta=calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE),
                market_cap=market_cap,
                price=price, shares_outstanding=shares_outstanding,
                risk_free_rate=RISK_FREE, eq_premium=EQ_PREM,
                growth_rate=0.0, cost_of_capital=0.0, wealth_pc=0.0,
                fcff_value=0.0, terminal_value=0.0, share_value=0.0,
                margin_of_safety=0.0, margin_of_safety_pc=0.0, target_price=0.0,
                earnings_yield=0.0, dividend_yield=dividend_yield,
                notes=roc_notes,
                analyst_count=analyst_count,
            )
        # Uncapped 2026-09-13 -- Ginzu has no cap here either; see
        # high_growth_rate_note(), wired into `notes` below instead.
        growth_rate = calc_growth_rate(reinvestment_rate, return_on_capital)
        # Adaptive Growth override (2026-09-22): the first place in this
        # file growth_rate deliberately diverges from reinvestment_rate x
        # return_on_capital -- see calc_adaptive_growth_rate()'s docstring
        # and docs/decisions.md for why this is safe (return_on_capital,
        # wealth_pc, and calc_terminal_value()'s moat blending never read
        # growth_rate, confirmed by reading every call site).
        if adaptive_growth_rate is not None:
            growth_rate = adaptive_growth_rate

        levered_beta = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        discount_rate = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, unlevered_beta, RISK_FREE, EQ_PREM
        )
        logger.info(f"disc rate {discount_rate:,.4f}")

        terminal_cost_of_capital = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, stable_beta, RISK_FREE, EQ_PREM,
            de_cap=hg_dcflib.get_industry_de(industry),
        )
        moat_weight = get_moat_weight(ticker, db_path)
        # 3-stage fade target (2026-09-11, finding #6) -- growth/reinvestment
        # fade toward this over TRANSITION_PERIOD years instead of jumping
        # straight to it at the terminal boundary. See calc_expected_fcff()
        # and calc_stable_phase_reinvestment_rate()'s docstrings.
        stable_reinv_rate_target = calc_stable_phase_reinvestment_rate(
            terminal_cost_of_capital, STABLE_GROWTH, moat_weight, return_on_capital
        )
        total_explicit_years = growth_period + TRANSITION_PERIOD
        fcff_table, ebit_n, _, _, _ = calc_expected_fcff(
            adjusted_ebit, eff_tax_rate, growth_rate, reinvestment_rate, growth_period,
            transition_period=TRANSITION_PERIOD, stable_growth=STABLE_GROWTH,
            stable_reinvestment_rate=stable_reinv_rate_target,
        )
        # Discount-rate fade (2026-09-13, see calc_fading_discount_rates()
        # docstring) -- same transition window as the growth/reinvestment
        # fade above, replacing the prior flat discount_rate throughout.
        discount_rates = calc_fading_discount_rates(
            discount_rate, terminal_cost_of_capital, growth_period, TRANSITION_PERIOD
        )
        fcff_pv = calc_fcff_value(fcff_table, discount_rates)

        ebit_last = ebit_n[-1]
        terminal_value_pv = calc_terminal_value(
            ebit_last,
            terminal_cost_of_capital,
            discount_rates,
            STABLE_GROWTH,
            total_explicit_years,
            moat_weight=moat_weight,
            explicit_roic=return_on_capital,
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
        safety_margin_pc = (1 - (price / intrinsic_value)) if intrinsic_value != 0 else 0.0
        wealth_pc = return_on_capital - discount_rate
        target_price = intrinsic_value * (1 + discount_rate)
        _cash = bal_sht["cash_and_equivalents"][0]
        _ev = market_cap + bv_debt - _cash
        earnings_yield = adjusted_ebit / _ev if _ev > 0 else 0.0

        if return_on_capital > discount_rate:
            logger.info("Wealth Creator")
        else:
            logger.info("Wealth Destroyer")

        # Write the row even when the model produces a non-positive intrinsic
        # value rather than skipping it — a skip is indistinguishable in
        # valuation.db from "not yet attempted" or "rate-limited," which both
        # (a) erases why the ticker has no usable value and (b) causes every
        # future batch run to re-attempt it from scratch, burning an AV call
        # every night on a company whose negative EBIT isn't going to
        # un-happen tomorrow (same self-perpetuating-retry shape as the BK
        # bug in docs/known_errors.md). Flagging via `notes` instead lets a
        # query filter these out cheaply while preserving the audit trail —
        # see docs/known_errors.md 2026-07-31 "Negative FCFF intrinsic values".
        notes = (
            "Model produced non-positive intrinsic value — negative/deteriorating "
            "fundamentals; DCF result may not be economically meaningful"
            if intrinsic_value <= 0 else roc_notes
        )
        notes = " | ".join(
            n for n in (
                notes,
                terminal_value_dominance_note(terminal_value_pv, market_cap),
                low_growth_rate_note(growth_rate, RISK_FREE),
                extreme_reinvestment_rate_note(reinvestment_rate),
                high_growth_rate_note(growth_rate),
                cyclical_ebit_normalization_note(cyclical_diag),
                cyclical_reinvestment_normalization_note(reinvest_diag),
            ) if n
        )

        return Stock_Value(
            ticker=ticker,
            valuation_date=valuation_date,
            ent_name=ent_name,
            industry=industry,
            cik=cik,
            beta=levered_beta,
            market_cap=market_cap,
            price=price,
            shares_outstanding=shares_outstanding,
            risk_free_rate=RISK_FREE,
            eq_premium=EQ_PREM,
            growth_rate=growth_rate,
            cost_of_capital=discount_rate,
            wealth_pc=wealth_pc,
            fcff_value=fcff_pv,
            terminal_value=terminal_value_pv,
            share_value=intrinsic_value,
            margin_of_safety=safety_margin,
            margin_of_safety_pc=safety_margin_pc,
            target_price=target_price,
            earnings_yield=earnings_yield,
            dividend_yield=dividend_yield,
            analyst_count=analyst_count,
            notes=notes,
        )

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# REIT valuation  (AFFO-based DDM)
# ---------------------------------------------------------------------------

def value_reit_stock(ticker: str, growth_period: int):
    """
    AFFO-based dividend discount model for REITs.

    Why not FCFF/FCFE:
    - REITs distribute 90%+ of income → retention ≈ 0 → FCFF growth = 0
    - Near-zero corporate tax distorts FCFF
    - Growth driven by acquisitions, not retained earnings

    Model:
    - AFFO = Net Income + D&A − CapEx  (AV CapEx = recurring, not acquisitions)
    - Payout = dividends_paid / AFFO
    - Growth = ROE × retention, capped at 15%
    - Discount at Cost of Equity (no WACC — REIT leverage is structural)
    """
    logger.info(f"Valuing {ticker} as REIT (AFFO DDM)")
    try:
        industry = hg_dcflib.get_industry(ticker)

        subtype_floor = reit_subtype_growth(ticker, industry)
        if subtype_floor is None:
            logger.warning(
                f"Skipping {ticker}: Mortgage REIT — AFFO DDM not applicable; manual review required"
            )
            return None

        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY, is_financial_or_reit=True)
        bal_sht   = balance_sheet(ticker, MY_API_KEY, is_financial_or_reit=True)
        cash_flw  = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        valuation_date    = str(date.today())
        price             = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap        = ent_quote[2]
        ent_name          = ent_quote[3]
        dividend_yield    = ent_quote[4]
        analyst_count     = int(ent_quote[5])
        cik               = hg_dcflib.get_cik(ticker)

        net_income     = inc_stmnt["netIncome"][0]
        da             = cash_flw["depreciation"][0] if cash_flw["depreciation"] else 0.0
        capex          = abs(cash_flw["capex"][0])           if cash_flw["capex"]          else 0.0
        dividends_paid = abs(cash_flw["dividends_paid"][0])  if cash_flw["dividends_paid"] else 0.0
        bv_equity      = bal_sht["total_stockholders_equity"][0]

        ffo  = net_income + da
        affo = max(ffo - capex, ffo * 0.80)  # floor at 80% of FFO to absorb edge cases

        payout_ratio   = min(dividends_paid / affo, 1.0) if affo > 0 else 0.85
        retention_ratio = 1.0 - payout_ratio

        roe             = net_income / bv_equity if bv_equity > 0 else 0.0
        # Growth from external capital raised (net new equity + net new
        # debt), on top of AFFO retention -- see
        # calc_reit_effective_retention_ratio()'s docstring, 2026-09-13.
        net_equity_issued = cash_flw["buybacks"][0] if cash_flw.get("buybacks") else 0.0
        net_new_debt_val  = cash_flw["net_new_debt"][0] if cash_flw.get("net_new_debt") else 0.0
        effective_retention_ratio = calc_reit_effective_retention_ratio(
            retention_ratio, affo, net_equity_issued, net_new_debt_val
        )
        # Use the higher of the retention-based rate and the sub-type floor.
        # The floor captures contractual lease escalators and structural growth
        # that exists independent of retained earnings (e.g. CPI escalators on
        # tower leases, 5G colocation, biological timber growth).
        growth_rate, retained_growth = _reit_growth_rate(roe, effective_retention_ratio, subtype_floor)
        logger.info(
            f"AFFO={affo:,.0f}  payout={payout_ratio:.3f}  ROE={roe:.4f}  "
            f"effective_retention={effective_retention_ratio:.4f}  "
            f"retained_g={retained_growth:.4f}  subtype_floor={subtype_floor:.4f}  "
            f"g={growth_rate:.4f}"
        )

        bv_debt = calc_bv_debt(bal_sht)
        levered_beta = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        cost_of_equity = RISK_FREE + (levered_beta * EQ_PREM)

        affo_n, div_n = [], []
        for year in range(growth_period):
            a = affo * (1 + growth_rate) ** (year + 1)
            affo_n.append(a)
            div_n.append(a * payout_ratio)

        div_pv = sum(div_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period))

        stable_beta             = calc_stable_beta(unlevered_beta)
        stable_levered_beta     = calc_levered_beta(stable_beta, bv_debt, market_cap, MARGINAL_TAX_RATE, de_cap=hg_dcflib.get_industry_de(industry))
        stable_cost_of_equity   = RISK_FREE + (stable_levered_beta * EQ_PREM)
        stable_growth           = STABLE_GROWTH
        # shared helper guards non-positive denominators and clamps to [0,1] —
        # see docs/known_errors.md 2026-08-01.
        stable_reinv            = calc_stable_reinvestment_rate(stable_growth, stable_cost_of_equity)
        stable_div              = div_n[-1] * (1 + stable_growth) * (1 - stable_reinv)
        # Gordon Growth requires cost of equity > growth rate — see
        # docs/known_errors.md 2026-08-03 (CLMB/OSW/RCKY division-by-zero fix).
        if stable_cost_of_equity <= stable_growth:
            raise ValueError(
                f"Stable-phase cost of equity ({stable_cost_of_equity:.4f}) is at or "
                f"below the stable growth rate ({stable_growth:.4f}) — terminal value "
                f"is undefined (requires cost of equity > growth)."
            )
        terminal_value          = stable_div / (stable_cost_of_equity - stable_growth)
        terminal_value_pv       = terminal_value / (1 + cost_of_equity) ** growth_period

        equity_value    = div_pv + terminal_value_pv
        intrinsic_value = equity_value / shares_outstanding
        # Write the row even when AFFO/growth-vs-CoE math produces a
        # non-positive IV rather than skipping — see the matching comment in
        # _value_stock_fcff() and docs/known_errors.md 2026-07-31 "Negative
        # FCFF intrinsic values" for why a skip is worse than a flagged row.
        notes = ""
        if intrinsic_value <= 0:
            logger.warning(
                f"{ticker}: AFFO model produced non-positive IV "
                f"({intrinsic_value:.2f}) — negative AFFO or growth ≥ CoE"
            )
            notes = (
                "AFFO model produced non-positive intrinsic value — negative "
                "AFFO or growth ≥ cost of equity; DCF result may not be "
                "economically meaningful"
            )
        notes = " | ".join(
            n for n in (notes, terminal_value_dominance_note(terminal_value_pv, market_cap)) if n
        )
        safety_margin   = float(intrinsic_value - price)
        safety_margin_pc = (1 - price / intrinsic_value) if intrinsic_value != 0 else 0.0
        wealth_pc       = roe - cost_of_equity
        target_price    = intrinsic_value * (1 + cost_of_equity)

        logger.info(f"Intrinsic value = {intrinsic_value:.2f}  Price = {price:.2f}")

        return Stock_Value(
            ticker=ticker,
            valuation_date=valuation_date,
            ent_name=ent_name,
            industry=industry,
            cik=cik,
            beta=levered_beta,
            market_cap=market_cap,
            price=price,
            shares_outstanding=shares_outstanding,
            risk_free_rate=RISK_FREE,
            eq_premium=EQ_PREM,
            growth_rate=growth_rate,
            cost_of_capital=cost_of_equity,
            wealth_pc=wealth_pc,
            fcff_value=div_pv,
            terminal_value=terminal_value_pv,
            share_value=intrinsic_value,
            margin_of_safety=safety_margin,
            margin_of_safety_pc=safety_margin_pc,
            target_price=target_price,
            earnings_yield=0.0,
            dividend_yield=dividend_yield,
            analyst_count=analyst_count,
            notes=notes,
        )

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Single-stock detailed valuation (for Excel output)
# ---------------------------------------------------------------------------


def _stock_value_from_detail(d: dict) -> Stock_Value:
    """Build a Stock_Value dataclass from a value_stock_detail dict for DB insertion."""
    if d["model"] == "ExcessReturn":
        cost_of_capital = d["cost_of_equity"]
        wealth_pc = d["roe"] - d["cost_of_equity"]
        fcff_value = d["excess_return_pv"]
    elif d["model"] == "AFFO":
        cost_of_capital = d["cost_of_equity"]
        wealth_pc = d["roe"] - d["cost_of_equity"]
        fcff_value = d["div_pv"]
    else:
        cost_of_capital = d["discount_rate"]
        wealth_pc = d["return_on_capital"] - d["discount_rate"]
        fcff_value = d["fcff_pv"]

    return Stock_Value(
        ticker=d["ticker"],
        valuation_date=d["valuation_date"],
        ent_name=d["ent_name"],
        industry=d["industry"],
        cik=d.get("cik", ""),
        beta=d["beta"],
        market_cap=d["market_cap"],
        price=d["price"],
        shares_outstanding=d["shares_outstanding"],
        risk_free_rate=d["risk_free"],
        eq_premium=d["eq_prem"],
        growth_rate=d["growth_rate"],
        cost_of_capital=cost_of_capital,
        wealth_pc=wealth_pc,
        fcff_value=fcff_value,
        terminal_value=d["terminal_value_pv"],
        share_value=d["intrinsic_value"],
        margin_of_safety=d["margin_of_safety"],
        margin_of_safety_pc=d["margin_of_safety_pc"],
        target_price=d["target_price"],
        earnings_yield=d.get("earnings_yield", 0.0),
        analyst_count=d.get("analyst_count", 0),
    )


def value_stock_detail(ticker: str, growth_period: int, db_path: str | None = None) -> dict | None:
    """
    Route to the correct detail valuation for the Excel report:
      - SIC pre-filter → same as value_stock(), see is_financial_sic()
      - REITs          → skipped (FFO/AFFO model pending Phase 2)
      - Financial firms → FCFE bank detail
      - All others      → FCFF detail
    """
    try:
        industry = hg_dcflib.get_industry(ticker)
    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        return None

    sic = hg_dcflib.get_sic(ticker, INTRINIO_KEY)
    if sic is not None and not is_financial_sic(sic):
        return _value_stock_detail_fcff(ticker, growth_period, industry, db_path)

    if is_reit(industry):
        return _value_reit_stock_detail(ticker, growth_period, industry)

    if is_financial_firm(industry):
        return _value_bank_stock_detail(ticker, growth_period, industry)
    return _value_stock_detail_fcff(ticker, growth_period, industry, db_path)


def _value_bank_stock_detail(
    ticker: str, growth_period: int, industry: str
) -> dict | None:
    """Excess Return detail dict for bank/financial firms (used for Excel output).
    See value_bank_stock()'s docstring / docs/known_errors.md 2026-09-17."""
    try:
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY, is_financial_or_reit=True)
        bal_sht = balance_sheet(ticker, MY_API_KEY, is_financial_or_reit=True)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]
        analyst_count = int(ent_quote[5])
        cik = hg_dcflib.get_cik(ticker)

        reported_net_income = inc_stmnt["netIncome"][0]
        bv_equity_curr = bal_sht["total_stockholders_equity"][0]
        bv_equity_prior = bal_sht["total_stockholders_equity"][1]
        equity_change = bv_equity_curr - bv_equity_prior

        if is_insurance_firm(industry):
            net_income, ni_years = _normalized_net_income(inc_stmnt["netIncome"])
            logger.info(
                f"Insurance: normalized NI over {ni_years} years = {net_income:,.0f}  (TTM = {reported_net_income:,.0f})"
            )
        else:
            net_income = reported_net_income
            ni_years = 1

        roe = net_income / bv_equity_curr if bv_equity_curr != 0 else 0.0
        payout_ratio = _bank_payout_ratio(
            reported_net_income, bv_equity_curr, bv_equity_prior, cash_flw
        )
        retention_ratio = 1.0 - payout_ratio
        growth_rate = _bank_growth_rate(roe, retention_ratio)

        bv_debt = calc_bv_debt(bal_sht)
        levered_beta = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        cost_of_equity = RISK_FREE + (levered_beta * EQ_PREM)

        # Excess Return model — see value_bank_stock()'s docstring /
        # docs/known_errors.md 2026-09-17 for the full rationale. Terminal
        # value is zero by construction (ROE converges to cost of equity
        # beyond the explicit period) — no Gordon Growth term needed.
        bv_equity_n, excess_return_n = [], []
        bv_equity_prev = bv_equity_curr
        for year in range(growth_period):
            bv_equity_t = bv_equity_prev * (1 + growth_rate)
            excess_return_t = (roe - cost_of_equity) * bv_equity_prev
            bv_equity_n.append(bv_equity_t)
            excess_return_n.append(excess_return_t)
            bv_equity_prev = bv_equity_t

        excess_return_pv = sum(
            excess_return_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period)
        )

        equity_value = bv_equity_curr + excess_return_pv
        intrinsic_value = equity_value / shares_outstanding
        margin_of_safety = float(intrinsic_value - price)
        margin_of_safety_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0
        )

        return {
            "model": "ExcessReturn",
            "ticker": ticker,
            "ent_name": ent_name,
            "industry": industry,
            "cik": cik,
            "valuation_date": str(date.today()),
            # --- Inputs ---
            "net_income": net_income,
            "reported_net_income": reported_net_income,
            "ni_years": ni_years,
            "bv_equity": bv_equity_curr,
            "bv_equity_prior": bv_equity_prior,
            "equity_change": equity_change,
            "roe": roe,
            "retention_ratio": retention_ratio,
            "payout_ratio": payout_ratio,
            # --- Growth phase ---
            "growth_period": growth_period,
            "growth_rate": growth_rate,
            "beta": levered_beta,
            "risk_free": RISK_FREE,
            "eq_prem": EQ_PREM,
            "cost_of_equity": cost_of_equity,
            # --- Year-by-year projections ---
            "bv_equity_n": bv_equity_n,
            "excess_return_n": excess_return_n,
            # --- Valuation ---
            "excess_return_pv": excess_return_pv,
            "terminal_value_pv": 0.0,  # zero by construction — see comment above
            "equity_value": equity_value,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "intrinsic_value": intrinsic_value,
            "margin_of_safety": margin_of_safety,
            "margin_of_safety_pc": margin_of_safety_pc,
            "target_price": intrinsic_value * (1 + cost_of_equity),
        }

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


def _value_reit_stock_detail(
    ticker: str, growth_period: int, industry: str
) -> dict | None:
    """AFFO DDM detail dict for REITs (used for Excel output)."""
    try:
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY, is_financial_or_reit=True)
        bal_sht   = balance_sheet(ticker, MY_API_KEY, is_financial_or_reit=True)
        cash_flw  = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        price              = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap         = ent_quote[2]
        ent_name           = ent_quote[3]
        cik                = hg_dcflib.get_cik(ticker)

        net_income     = inc_stmnt["netIncome"][0]
        da             = cash_flw["depreciation"][0] if cash_flw["depreciation"] else 0.0
        capex          = abs(cash_flw["capex"][0])           if cash_flw["capex"]          else 0.0
        dividends_paid = abs(cash_flw["dividends_paid"][0])  if cash_flw["dividends_paid"] else 0.0
        bv_equity      = bal_sht["total_stockholders_equity"][0]

        ffo  = net_income + da
        affo = max(ffo - capex, ffo * 0.80)

        payout_ratio    = min(dividends_paid / affo, 1.0) if affo > 0 else 0.85
        retention_ratio = 1.0 - payout_ratio
        roe             = net_income / bv_equity if bv_equity > 0 else 0.0
        subtype_floor   = reit_subtype_growth(ticker, industry)
        if subtype_floor is None:
            logger.warning(f"Skipping {ticker}: Mortgage REIT — AFFO DDM not applicable")
            return None
        # Growth from external capital raised, on top of AFFO retention --
        # see calc_reit_effective_retention_ratio()'s docstring, 2026-09-13.
        net_equity_issued = cash_flw["buybacks"][0] if cash_flw.get("buybacks") else 0.0
        net_new_debt_val  = cash_flw["net_new_debt"][0] if cash_flw.get("net_new_debt") else 0.0
        effective_retention_ratio = calc_reit_effective_retention_ratio(
            retention_ratio, affo, net_equity_issued, net_new_debt_val
        )
        growth_rate, retained_growth = _reit_growth_rate(roe, effective_retention_ratio, subtype_floor)
        bv_debt         = calc_bv_debt(bal_sht)
        levered_beta    = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        cost_of_equity  = RISK_FREE + (levered_beta * EQ_PREM)

        affo_n, div_n = [], []
        for year in range(growth_period):
            a = affo * (1 + growth_rate) ** (year + 1)
            affo_n.append(a)
            div_n.append(a * payout_ratio)

        div_pv = sum(div_n[y] / (1 + cost_of_equity) ** (y + 1) for y in range(growth_period))

        stable_beta           = calc_stable_beta(unlevered_beta)
        stable_levered_beta   = calc_levered_beta(stable_beta, bv_debt, market_cap, MARGINAL_TAX_RATE, de_cap=hg_dcflib.get_industry_de(industry))
        stable_cost_of_equity = RISK_FREE + (stable_levered_beta * EQ_PREM)
        stable_growth         = STABLE_GROWTH
        # shared helper guards non-positive denominators and clamps to [0,1] —
        # see docs/known_errors.md 2026-08-01.
        stable_reinv          = calc_stable_reinvestment_rate(stable_growth, stable_cost_of_equity)
        stable_div            = div_n[-1] * (1 + stable_growth) * (1 - stable_reinv)
        # Gordon Growth requires cost of equity > growth rate — see
        # docs/known_errors.md 2026-08-03 (CLMB/OSW/RCKY division-by-zero fix).
        if stable_cost_of_equity <= stable_growth:
            raise ValueError(
                f"Stable-phase cost of equity ({stable_cost_of_equity:.4f}) is at or "
                f"below the stable growth rate ({stable_growth:.4f}) — terminal value "
                f"is undefined (requires cost of equity > growth)."
            )
        terminal_value_undiscounted = stable_div / (stable_cost_of_equity - stable_growth)
        terminal_value_pv     = terminal_value_undiscounted / (1 + cost_of_equity) ** growth_period

        equity_value     = div_pv + terminal_value_pv
        intrinsic_value  = equity_value / shares_outstanding
        margin_of_safety = float(intrinsic_value - price)
        margin_of_safety_pc = (1 - price / intrinsic_value) if intrinsic_value != 0 else 0.0

        return {
            "model": "AFFO",
            "ticker": ticker,
            "ent_name": ent_name,
            "industry": industry,
            "cik": cik,
            "valuation_date": str(date.today()),
            # --- Inputs ---
            "net_income": net_income,
            "da": da,
            "capex": capex,
            "dividends_paid": dividends_paid,
            "ffo": ffo,
            "affo": affo,
            "bv_equity": bv_equity,
            "roe": roe,
            "payout_ratio": payout_ratio,
            "retention_ratio": retention_ratio,
            "retained_growth": retained_growth,
            "subtype_floor": subtype_floor,
            # --- Growth phase ---
            "growth_period": growth_period,
            "growth_rate": growth_rate,
            "beta": levered_beta,
            "stable_beta": stable_beta,
            "risk_free": RISK_FREE,
            "eq_prem": EQ_PREM,
            "cost_of_equity": cost_of_equity,
            # --- Year-by-year projections ---
            "affo_n": affo_n,
            "div_n": div_n,
            # --- Stable phase ---
            "stable_growth": stable_growth,
            "stable_reinv": stable_reinv,
            "stable_div": stable_div,
            "stable_cost_of_equity": stable_cost_of_equity,
            "terminal_value_undiscounted": terminal_value_undiscounted,
            "terminal_value_pv": terminal_value_pv,
            # --- Valuation ---
            "div_pv": div_pv,
            "equity_value": equity_value,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "intrinsic_value": intrinsic_value,
            "margin_of_safety": margin_of_safety,
            "margin_of_safety_pc": margin_of_safety_pc,
            "target_price": intrinsic_value * (1 + cost_of_equity),
        }

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


def _value_stock_detail_fcff(
    ticker: str, growth_period: int, industry: str, db_path: str | None = None
) -> dict | None:
    """FCFF detail dict for non-financial firms (used for Excel output)."""
    try:
        rd_years = hg_dcflib.get_rAndD_years(industry) + 1
        unlevered_beta = hg_dcflib.get_beta(industry)

        inc_stmnt = income_statement(ticker, MY_API_KEY)
        bal_sht = balance_sheet(ticker, MY_API_KEY)
        cash_flw = cash_flow_statement(ticker, MY_API_KEY)
        ent_quote = enterprise_quote(ticker, MY_API_KEY)

        price = ent_quote[0]
        shares_outstanding = ent_quote[1]
        market_cap = ent_quote[2]
        ent_name = ent_quote[3]
        cik = hg_dcflib.get_cik(ticker)

        stable_beta = calc_stable_beta(unlevered_beta)
        eff_tax_rate = calc_tax_rate(inc_stmnt)
        fcff_data = calc_fcff(inc_stmnt, bal_sht, cash_flw, eff_tax_rate)

        ebiat = fcff_data[0]
        capex = fcff_data[1]
        chng_nc_wc = fcff_data[2]
        depreciation = fcff_data[3]

        amort_schedule = capitalizerAndD(ticker, rd_years, MY_API_KEY)

        try:
            annual_reports = annual_income_statement(ticker, MY_API_KEY)
            cyclical_ebit, cyclical_diag = calc_cyclical_normalized_ebit(
                ticker, inc_stmnt["ebit"][0], inc_stmnt["totalRevenue"][0], annual_reports
            )
        except Exception as exc:
            logger.warning(f"{ticker}: cyclical EBIT normalization skipped ({exc}) — using raw TTM EBIT.")
            cyclical_ebit, cyclical_diag = inc_stmnt["ebit"][0], {"applied": False, "reason": f"fetch_failed: {exc}"}

        # Adaptive Growth (2026-09-22) -- see the matching batch-path
        # comment and calc_adaptive_growth_rate()'s docstring.
        adaptive_growth_rate = None
        adjusted_ebit = calc_adj_ebit(inc_stmnt["ebit"][0], amort_schedule)
        if cyclical_diag.get("applied"):
            adjusted_normalized_ebit = calc_adj_ebit(cyclical_diag["normalized_ebit"], amort_schedule)
            adaptive_growth_rate = calc_adaptive_growth_rate(
                adjusted_ebit, adjusted_normalized_ebit, growth_period, TRANSITION_PERIOD, STABLE_GROWTH
            )
            cyclical_diag["adaptive_growth_rate"] = adaptive_growth_rate
            if adaptive_growth_rate is None:
                adjusted_ebit = adjusted_normalized_ebit
                cyclical_diag["adaptive_growth_fallback"] = "instant_substitution"
        adjusted_ebiat = adjusted_ebit * (1 - eff_tax_rate)

        # Cyclical reinvestment (capex) normalization (2026-09-22 follow-up)
        # -- see the matching batch-path comment and
        # calc_cyclical_normalized_reinvestment()'s docstring.
        reinvest_capex = capex
        reinvest_diag = {"applied": False, "reason": "ebit_gate_not_triggered"}
        if cyclical_diag.get("applied"):
            try:
                capex_annual = annual_cash_flow_statement(
                    ticker, MY_API_KEY, years=cyclical_diag["years_used"]
                )
                reinvest_capex, reinvest_diag = calc_cyclical_normalized_reinvestment(
                    ticker, capex, inc_stmnt["totalRevenue"][0],
                    capex_annual, annual_reports, cyclical_diag,
                )
            except Exception as exc:
                logger.warning(f"{ticker}: cyclical reinvestment normalization skipped ({exc}) — using raw capex.")

        firm_reinvestment = calc_reinvestment(
            reinvest_capex, depreciation, chng_nc_wc, amort_schedule
        )
        adjusted_bv_equity = calc_adj_bv_equity(bal_sht, amort_schedule)
        bv_debt = calc_bv_debt(bal_sht)

        if market_cap > 0 and abs(adjusted_ebit) > 10 * market_cap:
            raise ValueError(
                f"Adjusted EBIT ({adjusted_ebit:,.0f}) is > 10× market cap "
                f"({market_cap:,.0f}) — likely bad WC data, skipping."
            )

        if adjusted_ebiat == 0:
            raise ValueError(
                f"Adjusted EBIAT is zero for {ticker} — cannot compute reinvestment rate."
            )
        # Not clamped to [0,1] -- see extreme_reinvestment_rate_note()
        # docstring and docs/known_errors.md 2026-09-10.
        reinvestment_rate = firm_reinvestment / adjusted_ebiat
        _extreme_rr_note = extreme_reinvestment_rate_note(reinvestment_rate)
        if _extreme_rr_note:
            logger.warning(f"{ticker}: {_extreme_rr_note}")
        return_on_capital, roc_notes = calc_gated_return_on_capital(
            ticker, adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht, inc_stmnt, db_path
        )
        if return_on_capital is None:
            raise ValueError(roc_notes)
        if roc_notes:
            logger.warning(f"{ticker}: {roc_notes}")
        # Uncapped 2026-09-13 -- Ginzu has no cap here either; this path has
        # no `notes` field (it's the single-ticker Excel report, already
        # human-reviewed directly), see high_growth_rate_note()'s docstring.
        growth_rate = calc_growth_rate(reinvestment_rate, return_on_capital)
        # Adaptive Growth override (2026-09-22) -- see the matching
        # batch-path comment and calc_adaptive_growth_rate()'s docstring.
        if adaptive_growth_rate is not None:
            growth_rate = adaptive_growth_rate

        # Compute discount rate components inline to capture intermediates
        levered_beta = calc_levered_beta(unlevered_beta, bv_debt, market_cap, MARGINAL_TAX_RATE)
        cost_of_equity = RISK_FREE + (levered_beta * EQ_PREM)
        int_cover = calc_interest_coverage(
            inc_stmnt["ebit"][0], inc_stmnt["interest_expense"][0], bv_debt, RISK_FREE
        )
        def_spread = hg_dcflib.get_default_spread(int_cover)
        cost_of_debt_pretax = RISK_FREE + def_spread
        cost_of_debt_aftertax = cost_of_debt_pretax * (1 - MARGINAL_TAX_RATE)
        # WACC weights use market value of equity, book value of debt (Damodaran's
        # prescribed methodology — market debt is rarely observable, market equity
        # is trivial: price x shares). Previously weighted by book equity here,
        # diverging from calc_discount_rate() (used by the batch path, and by this
        # same function's own stable-phase rate two lines below) — see
        # docs/known_errors.md 2026-08-01.
        total_capital = market_cap + bv_debt
        percent_debt = bv_debt / total_capital if total_capital > 0 else 0.5
        percent_equity = 1 - percent_debt
        discount_rate = (cost_of_debt_aftertax * percent_debt) + (
            cost_of_equity * percent_equity
        )

        # Stable phase
        stable_levered_beta = calc_levered_beta(stable_beta, bv_debt, market_cap, MARGINAL_TAX_RATE, de_cap=hg_dcflib.get_industry_de(industry))
        stable_cost_of_equity = RISK_FREE + (stable_levered_beta * EQ_PREM)
        stable_cost_of_capital = calc_discount_rate(
            inc_stmnt, bv_debt, market_cap, stable_beta, RISK_FREE, EQ_PREM,
            de_cap=hg_dcflib.get_industry_de(industry),
        )
        stable_growth = STABLE_GROWTH
        moat_weight = get_moat_weight(ticker, db_path)
        stable_reinv_rate = calc_stable_phase_reinvestment_rate(
            stable_cost_of_capital, stable_growth, moat_weight, return_on_capital
        )

        # Year-by-year FCFF projections -- 3-stage fade (2026-09-11, external
        # DCF review finding #6: see docs/known_errors.md). growth_period
        # years of constant growth_rate/reinvestment_rate, then
        # TRANSITION_PERIOD years fading linearly toward
        # stable_growth/stable_reinv_rate, replacing the prior abrupt jump
        # straight to STABLE_GROWTH at the terminal boundary. See
        # calc_expected_fcff()'s docstring.
        total_explicit_years = growth_period + TRANSITION_PERIOD
        fcff_n, ebit_n, ebiat_n, reinv_rate_n, growth_n = calc_expected_fcff(
            adjusted_ebit, eff_tax_rate, growth_rate, reinvestment_rate, growth_period,
            transition_period=TRANSITION_PERIOD, stable_growth=stable_growth,
            stable_reinvestment_rate=stable_reinv_rate,
        )
        # Dollar reinvestment per year (ebiat * rate) -- the Excel report's
        # capex/WC breakdown rows split this proportionally; kept as a
        # separate dollar-amount array from reinv_rate_n (the rate) since
        # the two aren't interchangeable.
        reinv_n = [ebiat_n[y] * reinv_rate_n[y] for y in range(total_explicit_years)]

        # Discount-rate fade (2026-09-13, see calc_fading_discount_rates()
        # docstring) -- same transition window as the growth/reinvestment
        # fade above, replacing the prior flat discount_rate throughout.
        discount_rates = calc_fading_discount_rates(
            discount_rate, stable_cost_of_capital, growth_period, TRANSITION_PERIOD
        )
        discount_factors = _cumulative_discount_factors(discount_rates)
        fcff_pv = sum(fcff_n[y] / discount_factors[y] for y in range(total_explicit_years))

        # Terminal-year FCFF is recomputed at the stable-phase reinvestment
        # rate rather than carrying forward the explicit period's (typically
        # much higher) reinvestment rate — see calc_terminal_value() and
        # docs/known_errors.md 2026-07-31.
        # Display-only intermediates for the Excel report (stable_fcff,
        # terminal_value_undiscounted) — the authoritative terminal_value_pv
        # below comes from the shared calc_terminal_value(), which
        # recomputes the same values internally. See docs/known_errors.md
        # 2026-08-01 "FCFF terminal-value consolidation".
        # Taxed at MARGINAL_TAX_RATE, not eff_tax_rate -- matches the
        # authoritative calc_terminal_value() call below (2026-09-10 fix,
        # see its docstring); kept in sync here since this block is
        # display-only but must show the same figures. By construction the
        # fade above lands exactly on stable_growth/stable_reinv_rate at
        # ebit_n[-1], so this is now a seamless continuation, not a jump.
        terminal_ebit = ebit_n[-1] * (1 + stable_growth)
        terminal_ebiat = terminal_ebit * (1 - MARGINAL_TAX_RATE)
        stable_fcff = terminal_ebiat * (1 - stable_reinv_rate)
        # Gordon Growth requires cost of capital > growth rate. This block is
        # display-only (see comment above), but it still executes before the
        # authoritative calc_terminal_value() call below and would still
        # raise an unguarded ZeroDivisionError first — see docs/known_errors.md
        # 2026-08-03 (CLMB/OSW/RCKY division-by-zero fix).
        if stable_cost_of_capital <= stable_growth:
            raise ValueError(
                f"Stable-phase cost of capital ({stable_cost_of_capital:.4f}) is at or "
                f"below the stable growth rate ({stable_growth:.4f}) — terminal value "
                f"is undefined (requires cost of capital > growth)."
            )
        terminal_value_undiscounted = stable_fcff / (
            stable_cost_of_capital - stable_growth
        )
        terminal_value_pv = calc_terminal_value(
            ebit_n[-1], stable_cost_of_capital, discount_rates,
            stable_growth, total_explicit_years, moat_weight=moat_weight,
            explicit_roic=return_on_capital,
        )

        cash = bal_sht["cash_and_equivalents"][0]
        enterprise_value = fcff_pv + terminal_value_pv + cash - bv_debt
        intrinsic_value = enterprise_value / shares_outstanding
        margin_of_safety = float(intrinsic_value - price)
        margin_of_safety_pc = (
            1 - (price / intrinsic_value) if intrinsic_value != 0 else 0
        )

        # Non-cash working capital (current year)
        curr_nc_wc = (
            bal_sht["total_current_assets"][0] - bal_sht["cash_and_equivalents"][0]
        ) - (bal_sht["total_current_liabilities"][0] - bal_sht["short_term_debt"][0])
        revenue = inc_stmnt["totalRevenue"][0]
        wc_pct_revenue = curr_nc_wc / revenue if revenue != 0 else 0

        # Normalized valuation: if a single outlier quarter drove TTM EBIT negative,
        # rerun the model using TTM EBIT with that quarter excluded.  The result is
        # labeled "normalized" and shown alongside the GAAP figure in reports.
        ebit_anomaly = inc_stmnt.get("ebit_anomaly")
        norm_intrinsic_value = None
        norm_adjusted_ebit = None
        norm_return_on_capital = None
        norm_growth_rate = None
        if ebit_anomaly and adjusted_ebit < 0:
            norm_ttm_ebit_raw = ebit_anomaly["normalized_ttm_ebit"]
            # Adjust at the EBIT level, then derive EBIAT -- same 2026-09-10
            # fix as the main adjusted_ebit/adjusted_ebiat calc above; see
            # calc_adj_ebit()'s docstring.
            norm_adjusted_ebit_raw = calc_adj_ebit(norm_ttm_ebit_raw, amort_schedule)
            norm_adjusted_ebiat = norm_adjusted_ebit_raw * (1 - eff_tax_rate)
            if norm_adjusted_ebiat > 0:
                norm_adjusted_ebit = norm_adjusted_ebit_raw
                # Not clamped -- same 2026-09-10 fix as the main
                # reinvestment_rate calc above.
                norm_reinv_rate = firm_reinvestment / norm_adjusted_ebiat
                norm_return_on_capital, norm_roc_notes = calc_gated_return_on_capital(
                    ticker, norm_adjusted_ebiat, adjusted_bv_equity, bv_debt, bal_sht, inc_stmnt, db_path
                )
                if norm_return_on_capital is None:
                    # Same undefined-ROIC gate as the main path (see
                    # calc_gated_return_on_capital()) -- the normalized figure
                    # is a supplementary display value, not a hard-blocking
                    # path, so skip it gracefully rather than raising and
                    # losing the already-computed GAAP result above.
                    logger.warning(f"{ticker}: normalized valuation skipped -- {norm_roc_notes}")
                else:
                    if norm_roc_notes:
                        logger.warning(f"{ticker}: normalized valuation -- {norm_roc_notes}")
                    # Uncapped 2026-09-13, same reasoning as the GAAP
                    # growth_rate above.
                    norm_growth_rate = calc_growth_rate(norm_reinv_rate, norm_return_on_capital)
                    # 3-stage fade applied here too (2026-09-11, finding #6),
                    # matching the main GAAP path -- own fade target since
                    # norm_return_on_capital can differ from the GAAP
                    # return_on_capital used above.
                    norm_stable_reinv_rate = calc_stable_phase_reinvestment_rate(
                        stable_cost_of_capital, stable_growth, moat_weight, norm_return_on_capital
                    )
                    norm_fcff_table, norm_ebit_n, _, _, _ = calc_expected_fcff(
                        norm_adjusted_ebit, eff_tax_rate, norm_growth_rate,
                        norm_reinv_rate, growth_period,
                        transition_period=TRANSITION_PERIOD, stable_growth=stable_growth,
                        stable_reinvestment_rate=norm_stable_reinv_rate,
                    )
                    # Reuses the same discount_rates fade computed above --
                    # it depends only on discount_rate/stable_cost_of_capital/
                    # growth_period/TRANSITION_PERIOD, none of which differ
                    # between the GAAP and normalized paths.
                    norm_fcff_pv = calc_fcff_value(norm_fcff_table, discount_rates)
                    norm_ebit_last = norm_ebit_n[-1]
                    norm_tv_pv = calc_terminal_value(
                        norm_ebit_last, stable_cost_of_capital,
                        discount_rates, stable_growth, total_explicit_years,
                        moat_weight=moat_weight, explicit_roic=norm_return_on_capital,
                    )
                    norm_ev = norm_fcff_pv + norm_tv_pv + cash - bv_debt
                    norm_intrinsic_value = norm_ev / shares_outstanding

        return {
            "model": "FCFF",
            "ticker": ticker,
            "ent_name": ent_name,
            "industry": industry,
            "cik": cik,
            "valuation_date": str(date.today()),
            # --- Inputs ---
            "raw_ttm_ebit": inc_stmnt["ebit"][0],
            "cyclical_normalization_applied": cyclical_diag.get("applied", False),
            "cyclical_avg_margin": cyclical_diag.get("avg_margin"),
            "cyclical_current_margin": cyclical_diag.get("current_margin"),
            "cyclical_years_used": cyclical_diag.get("years_used"),
            "cyclical_normalized_ebit": cyclical_diag.get("normalized_ebit"),
            "adaptive_growth_rate": cyclical_diag.get("adaptive_growth_rate"),
            "cyclical_normalization_mode": (
                "instant_substitution" if cyclical_diag.get("adaptive_growth_fallback")
                else "adaptive_growth" if cyclical_diag.get("applied")
                else None
            ),
            "cyclical_reinvestment_normalization_applied": reinvest_diag.get("applied", False),
            "cyclical_avg_capex_margin": reinvest_diag.get("avg_capex_margin"),
            "cyclical_current_capex_margin": reinvest_diag.get("current_capex_margin"),
            "cyclical_normalized_capex": reinvest_diag.get("normalized_capex"),
            "cyclical_reinvestment_direction_mismatch": (
                reinvest_diag.get("reason") == "direction_mismatch_possible_structural_change"
            ),
            "adjusted_ebit": adjusted_ebit,
            "interest_expense": inc_stmnt["interest_expense"][0],
            "capex": capex,
            "reinvest_capex_used": reinvest_capex,
            "depreciation": depreciation,
            "eff_tax_rate": eff_tax_rate,
            "revenue": revenue,
            "curr_nc_wc": curr_nc_wc,
            "chng_nc_wc": chng_nc_wc,
            "bv_debt": bv_debt,
            "adjusted_bv_equity": adjusted_bv_equity,
            "cash": cash,
            # --- Growth phase parameters ---
            "growth_period": total_explicit_years,
            "growth_rate": growth_rate,
            "reinvestment_rate": reinvestment_rate,
            "return_on_capital": return_on_capital,
            "beta": levered_beta,
            "stable_beta": stable_beta,
            "risk_free": RISK_FREE,
            "eq_prem": EQ_PREM,
            "marginal_tax_rate": MARGINAL_TAX_RATE,
            # --- Discount rate components ---
            "cost_of_equity": cost_of_equity,
            "cost_of_debt_pretax": cost_of_debt_pretax,
            "cost_of_debt_aftertax": cost_of_debt_aftertax,
            "percent_debt": percent_debt,
            "percent_equity": percent_equity,
            "discount_rate": discount_rate,
            "wc_pct_revenue": wc_pct_revenue,
            # --- Stable phase ---
            "stable_growth": stable_growth,
            "stable_reinv_rate": stable_reinv_rate,
            "stable_fcff": stable_fcff,
            "stable_cost_of_equity": stable_cost_of_equity,
            "stable_cost_of_capital": stable_cost_of_capital,
            "terminal_value_undiscounted": terminal_value_undiscounted,
            "terminal_value_pv": terminal_value_pv,
            # --- Year-by-year projections ---
            "ebit_n": ebit_n,
            "ebiat_n": ebiat_n,
            "reinv_n": reinv_n,
            "reinvestment_rate_n": reinv_rate_n,
            "growth_n": growth_n,
            "fcff_n": fcff_n,
            # --- Valuation ---
            "fcff_pv": fcff_pv,
            "price": price,
            "shares_outstanding": shares_outstanding,
            "market_cap": market_cap,
            "intrinsic_value": intrinsic_value,
            "enterprise_value": enterprise_value,
            "margin_of_safety": margin_of_safety,
            "margin_of_safety_pc": margin_of_safety_pc,
            "target_price": intrinsic_value * (1 + discount_rate),
            "earnings_yield": adjusted_ebit / (market_cap + bv_debt - cash) if (market_cap + bv_debt - cash) > 0 else 0.0,
            # Anomaly detection — single outlier quarter driving negative TTM EBIT
            "ebit_anomaly": ebit_anomaly,
            "norm_intrinsic_value": norm_intrinsic_value,
            "norm_adjusted_ebit": norm_adjusted_ebit,
            "norm_return_on_capital": norm_return_on_capital,
            "norm_growth_rate": norm_growth_rate,
        }

    except Exception as e:
        logger.warning(f"Skipping {ticker}: {e}")
        logger.debug(traceback.format_exc())
        return None


# ---------------------------------------------------------------------------
# Excel single-stock report
# ---------------------------------------------------------------------------


def _generate_xlsx_bank(
    ws,
    d,
    gp,
    label,
    val_dollar,
    val_pct,
    section_header,
    BLUE_FILL,
    YELLOW_FILL,
    GREEN_FILL,
):
    """Populate the worksheet for a bank / financial firm (Excess Return model)."""
    from openpyxl.styles import Font

    r = 1
    ws.cell(
        row=r, column=1, value=f"{d['ent_name']} ({d['ticker']}) — Excess Return Valuation"
    ).font = Font(bold=True, size=13)
    r += 1
    ws.cell(
        row=r,
        column=1,
        value=f"Bank / Financial Firm  |  {d['valuation_date']}  |  Industry: {d['industry']}",
    )
    r += 1
    cik_val = d.get("cik", "")
    cik_cell = ws.cell(
        row=r,
        column=1,
        value=f"SEC EDGAR CIK: {cik_val}" if cik_val else "SEC EDGAR CIK: —",
    )
    cik_cell.font = Font(color="0563C1", underline="single")
    if cik_val:
        cik_cell.hyperlink = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_val}&type=10-K&dateb=&owner=include&count=10"
    r += 2

    # ---- Inputs --------------------------------------------------------
    section_header(r, 1, "Inputs")
    r += 1
    ni_label = (
        f"Net Income (normalized, {d['ni_years']}yr avg)"
        if d.get("ni_years", 1) > 1
        else "Net Income (TTM)"
    )
    inputs = [
        (ni_label, d["net_income"], "dollar"),
    ]
    if d.get("ni_years", 1) > 1:
        inputs.append(
            ("  Reported Net Income (TTM)", d["reported_net_income"], "dollar")
        )
    inputs += [
        ("Book Value of Equity (current)", d["bv_equity"], "dollar"),
        ("Book Value of Equity (prior yr)", d["bv_equity_prior"], "dollar"),
        ("Change in Book Equity", d["equity_change"], "dollar"),
        ("Return on Equity (ROE)", d["roe"], "pct"),
        ("Equity Retention Ratio (drives book-equity growth)", d["retention_ratio"], "pct"),
        ("Risk-free Rate", d["risk_free"], "pct"),
        ("Equity Risk Premium", d["eq_prem"], "pct"),
        ("Beta", d["beta"], "num"),
        ("Cost of Equity", d["cost_of_equity"], "pct"),
        ("Excess Return Spread (ROE − Cost of Equity)", d["roe"] - d["cost_of_equity"], "pct"),
    ]
    for lbl, v, fmt in inputs:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        elif fmt == "pct":
            val_pct(r, 2, v, BLUE_FILL)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "0.00"
            c.fill = BLUE_FILL
        r += 1

    r += 1

    # ---- Year-by-year Excess Return table ------------------------------
    # ROE and Cost of Equity are held flat through the explicit period
    # (same treatment growth_rate/cost_of_equity got under the old FCFE
    # model) -- terminal value is zero by construction (ROE assumed to
    # converge to cost of equity beyond this window), so there is no
    # separate stable-phase section. See docs/known_errors.md 2026-09-17.
    section_header(r, 1, f"Projected Excess Returns  (growth period = {gp} years)")
    for i in range(gp):
        ws.cell(row=r, column=2 + i, value=f"Year {i + 1}").font = Font(bold=True)
    r += 1

    table = [
        ("Book Value of Equity (start of year)", [d["bv_equity"]] + d["bv_equity_n"][:-1], "dollar"),
        ("Return on Equity (ROE)", [d["roe"]] * gp, "pct"),
        ("Cost of Equity", [d["cost_of_equity"]] * gp, "pct"),
        ("Excess Return (= (ROE − CoE) × BVE)", d["excess_return_n"], "dollar"),
        (
            "Cumulated CoE",
            [(1 + d["cost_of_equity"]) ** (y + 1) for y in range(gp)],
            "num",
        ),
        (
            "Present Value of Excess Return",
            [d["excess_return_n"][y] / (1 + d["cost_of_equity"]) ** (y + 1) for y in range(gp)],
            "dollar",
        ),
    ]
    for lbl, values, fmt in table:
        label(r, 1, lbl)
        for i, v in enumerate(values):
            if fmt == "dollar":
                val_dollar(r, 2 + i, v)
            elif fmt == "pct":
                val_pct(r, 2 + i, v)
            else:
                c = ws.cell(row=r, column=2 + i, value=v)
                c.number_format = "0.0000"
        r += 1

    r += 1

    # ---- Valuation summary --------------------------------------------
    section_header(r, 1, "Valuation")
    r += 1
    valuation = [
        ("Book Value of Equity (today)", d["bv_equity"], "dollar"),
        ("PV of Excess Returns", d["excess_return_pv"], "dollar"),
        ("Equity Value", d["equity_value"], "dollar"),
        ("÷ Shares Outstanding", d["shares_outstanding"], "num"),
        ("Value of Equity per Share", d["intrinsic_value"], "dollar"),
        ("Target Price (1-yr)", d["target_price"], "dollar"),
        ("Stock Price", d["price"], "dollar"),
        ("Margin of Safety ($)", d["margin_of_safety"], "dollar"),
        ("Margin of Safety (%)", d["margin_of_safety_pc"], "pct"),
    ]
    for lbl, v, fmt in valuation:
        label(r, 1, lbl)
        fill = (
            GREEN_FILL
            if lbl
            in (
                "Value of Equity per Share",
                "Target Price (1-yr)",
                "Stock Price",
                "Margin of Safety ($)",
                "Margin of Safety (%)",
            )
            else None
        )
        if fmt == "dollar":
            val_dollar(r, 2, v, fill)
        elif fmt == "pct":
            val_pct(r, 2, v, fill)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "#,##0"
            if fill:
                c.fill = fill
        r += 1


def _generate_xlsx_reit(
    ws, d, gp, label, val_dollar, val_pct, section_header,
    BLUE_FILL, YELLOW_FILL, GREEN_FILL,
):
    """Populate the worksheet for a REIT (AFFO DDM model)."""
    from openpyxl.styles import Font

    r = 1
    ws.cell(row=r, column=1,
            value=f"{d['ent_name']} ({d['ticker']}) — AFFO Valuation").font = Font(bold=True, size=13)
    r += 1
    ws.cell(row=r, column=1,
            value=f"REIT / AFFO DDM  |  {d['valuation_date']}  |  Industry: {d['industry']}")
    r += 1
    cik_val = d.get("cik", "")
    cik_cell = ws.cell(row=r, column=1,
                       value=f"SEC EDGAR CIK: {cik_val}" if cik_val else "SEC EDGAR CIK: —")
    cik_cell.font = Font(color="0563C1", underline="single")
    if cik_val:
        cik_cell.hyperlink = (
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
            f"&CIK={cik_val}&type=10-K&dateb=&owner=include&count=10"
        )
    r += 2

    # ---- Inputs --------------------------------------------------------
    section_header(r, 1, "Inputs")
    r += 1
    inputs = [
        ("Net Income (GAAP TTM)",            d["net_income"],      "dollar"),
        ("Depreciation & Amortization",      d["da"],              "dollar"),
        ("Recurring CapEx",                  d["capex"],           "dollar"),
        ("FFO  (Net Income + D&A)",          d["ffo"],             "dollar"),
        ("AFFO  (FFO − CapEx)",              d["affo"],            "dollar"),
        ("Dividends Paid",                   d["dividends_paid"],  "dollar"),
        ("Book Value of Equity",             d["bv_equity"],       "dollar"),
        ("Return on Equity (ROE)",           d["roe"],             "pct"),
        ("Payout Ratio (Dividends / AFFO)",  d["payout_ratio"],    "pct"),
        ("Equity Retention Ratio",           d["retention_ratio"], "pct"),
        ("Risk-free Rate",                   d["risk_free"],       "pct"),
        ("Equity Risk Premium",              d["eq_prem"],         "pct"),
        ("Beta",                             d["beta"],            "num"),
        ("Cost of Equity",                   d["cost_of_equity"],  "pct"),
    ]
    for lbl, v, fmt in inputs:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        elif fmt == "pct":
            val_pct(r, 2, v, BLUE_FILL)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "0.00"
            c.fill = BLUE_FILL
        r += 1

    r += 1

    # ---- Parameters: High Growth vs Stable ----------------------------
    section_header(r, 1, "Parameters")
    label(r, 2, "High Growth", bold=True)
    label(r, 3, "Stable", bold=True)
    r += 1
    params = [
        ("Growth Rate",    d["growth_rate"],  "pct", d["stable_growth"]),
        ("Payout Ratio",   d["payout_ratio"], "pct", 1 - d["stable_reinv"]),
        ("Beta",           d["beta"],         "num", d["stable_beta"]),
        ("Cost of Equity", d["cost_of_equity"], "pct", d["stable_cost_of_equity"]),
    ]
    for lbl, hg, fmt, st in params:
        label(r, 1, lbl)
        if fmt == "pct":
            val_pct(r, 2, hg, YELLOW_FILL)
            val_pct(r, 3, st, YELLOW_FILL)
        else:
            c = ws.cell(row=r, column=2, value=hg); c.number_format = "0.00"; c.fill = YELLOW_FILL
            c2 = ws.cell(row=r, column=3, value=st); c2.number_format = "0.00"; c2.fill = YELLOW_FILL
        r += 1

    r += 1

    # ---- Year-by-year AFFO projections --------------------------------
    section_header(r, 1, f"Projected Dividends  (growth period = {gp} years)")
    for col in range(1, gp + 1):
        label(r, col + 2, f"Year {col}", bold=True)
    r += 1

    rows_proj = [
        ("AFFO",            d["affo_n"]),
        ("Dividends",       d["div_n"]),
    ]
    for row_lbl, vals in rows_proj:
        label(r, 1, row_lbl)
        for col, v in enumerate(vals, 1):
            val_dollar(r, col + 2, v, None)
        r += 1

    # PV of dividends row
    label(r, 1, "PV of Dividends (sum)")
    val_dollar(r, 2, d["div_pv"], GREEN_FILL)
    r += 2

    # ---- Stable phase -------------------------------------------------
    section_header(r, 1, "Stable Phase")
    r += 1
    stable_rows = [
        ("Growth Rate in Stable Phase",          d["stable_growth"],                "pct"),
        ("Reinvestment Rate in Stable Phase",     d["stable_reinv"],                 "pct"),
        ("Cost of Equity in Stable Phase",        d["stable_cost_of_equity"],        "pct"),
        ("Stable Dividend",                       d["stable_div"],                   "dollar"),
        ("Terminal Value (undiscounted)",          d["terminal_value_undiscounted"],  "dollar"),
        ("PV of Terminal Value",                  d["terminal_value_pv"],            "dollar"),
    ]
    for lbl, v, fmt in stable_rows:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        else:
            val_pct(r, 2, v, BLUE_FILL)
        r += 1

    r += 1

    # ---- Valuation summary --------------------------------------------
    section_header(r, 1, "Valuation Summary")
    r += 1
    val_rows = [
        ("PV of Dividends (growth period)",  d["div_pv"],            "dollar"),
        ("PV of Terminal Value",             d["terminal_value_pv"], "dollar"),
        ("Equity Value",                     d["equity_value"],      "dollar"),
        ("Intrinsic Value / Share",          d["intrinsic_value"],   "dollar"),
        ("Current Price",                    d["price"],             "dollar"),
        ("Margin of Safety ($)",             d["margin_of_safety"],  "dollar"),
        ("Margin of Safety (%)",             d["margin_of_safety_pc"], "pct"),
    ]
    for lbl, v, fmt in val_rows:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, GREEN_FILL)
        else:
            val_pct(r, 2, v, GREEN_FILL)
        r += 1


def generate_xlsx(d: dict, output_path: str) -> None:
    """Write a Damodaran-style FCFF valuation worksheet for a single stock."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = d["ticker"]

    # ---- helpers --------------------------------------------------------
    BLUE_FILL = PatternFill("solid", fgColor="DDEEFF")
    YELLOW_FILL = PatternFill("solid", fgColor="FFFACD")
    GREEN_FILL = PatternFill("solid", fgColor="D4EDDA")
    HEADER_FONT = Font(bold=True)

    FMT_DOLLAR = "#,##0.00"
    FMT_PCT = "0.00%"

    def label(row, col, text, bold=False):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=bold)
        return c

    def val_dollar(row, col, v, fill=None):
        c = ws.cell(row=row, column=col, value=v)
        c.number_format = FMT_DOLLAR
        c.alignment = Alignment(horizontal="right")
        if fill:
            c.fill = fill
        return c

    def val_pct(row, col, v, fill=None):
        c = ws.cell(row=row, column=col, value=v)
        c.number_format = FMT_PCT
        c.alignment = Alignment(horizontal="right")
        if fill:
            c.fill = fill
        return c

    def section_header(row, col, text):
        c = ws.cell(row=row, column=col, value=text)
        c.font = Font(bold=True, underline="single")
        return c

    gp = d["growth_period"]

    # ---- Column widths --------------------------------------------------
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 14
    for col in range(4, 4 + gp):
        ws.column_dimensions[get_column_letter(col)].width = 12

    if d.get("model") == "AFFO":
        _generate_xlsx_reit(
            ws, d, gp, label, val_dollar, val_pct, section_header,
            BLUE_FILL, YELLOW_FILL, GREEN_FILL,
        )
        wb.save(output_path)
        print(f"Saved: {output_path}")
        return

    if d.get("model") == "ExcessReturn":
        _generate_xlsx_bank(
            ws,
            d,
            gp,
            label,
            val_dollar,
            val_pct,
            section_header,
            BLUE_FILL,
            YELLOW_FILL,
            GREEN_FILL,
        )
        wb.save(output_path)
        print(f"Saved: {output_path}")
        return

    _generate_xlsx_fcff(
        ws, d, gp, label, val_dollar, val_pct, section_header,
        BLUE_FILL, YELLOW_FILL, GREEN_FILL,
    )
    wb.save(output_path)
    print(f"Saved: {output_path}")


def _generate_xlsx_fcff(
    ws, d, gp, label, val_dollar, val_pct, section_header,
    BLUE_FILL, YELLOW_FILL, GREEN_FILL,
):
    """Populate the worksheet for a standard non-financial firm (FCFF model)."""
    from openpyxl.styles import Font

    HEADER_FONT = Font(bold=True)

    # ====================================================================
    # TITLE
    # ====================================================================
    r = 1
    ws.cell(row=r, column=1, value=f"{d['ent_name']} ({d['ticker']})").font = Font(
        bold=True, size=13
    )
    r += 1
    ws.cell(
        row=r,
        column=1,
        value=f"FCFF Valuation  |  {d['valuation_date']}  |  Industry: {d['industry']}",
    )
    r += 1
    cik_val = d.get("cik", "")
    cik_cell = ws.cell(
        row=r,
        column=1,
        value=f"SEC EDGAR CIK: {cik_val}" if cik_val else "SEC EDGAR CIK: —",
    )
    cik_cell.font = Font(color="0563C1", underline="single")
    if cik_val:
        cik_cell.hyperlink = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik_val}&type=10-K&dateb=&owner=include&count=10"
    r += 2

    # ====================================================================
    # SECTION 1 — INPUTS
    # ====================================================================
    section_header(r, 1, "Inputs")
    r += 1

    inputs = [
        ("Raw TTM EBIT (before adjustments)", d["raw_ttm_ebit"], "dollar"),
        ("Cyclical Nyr Avg Operating Margin", d["cyclical_avg_margin"], "percent"),
        ("Cyclical Current TTM Operating Margin", d["cyclical_current_margin"], "percent"),
        ("Cyclical Normalization Applied?", "Yes" if d["cyclical_normalization_applied"] else "No", "text"),
        ("Cyclically Normalized EBIT", d["cyclical_normalized_ebit"], "dollar"),
        ("Adjusted EBIT", d["adjusted_ebit"], "dollar"),
        ("Adjusted Interest Expense", d["interest_expense"], "dollar"),
        ("Raw Capital Spending (current year)", d["capex"], "dollar"),
        ("Cyclical Nyr Avg Capex Margin", d["cyclical_avg_capex_margin"], "percent"),
        ("Cyclical Current Capex Margin", d["cyclical_current_capex_margin"], "percent"),
        ("Cyclical Reinvestment Normalization Applied?", "Yes" if d["cyclical_reinvestment_normalization_applied"] else "No", "text"),
        ("Cyclically Normalized Capex", d["cyclical_normalized_capex"], "dollar"),
        ("Capex Used in Reinvestment Calc", d["reinvest_capex_used"], "dollar"),
        ("Adjusted Depreciation & Amort'n", d["depreciation"], "dollar"),
        ("Tax Rate on Income", d["eff_tax_rate"], "pct"),
        ("Current Revenues", d["revenue"], "dollar"),
        ("Current Non-cash Working Capital", d["curr_nc_wc"], "dollar"),
        ("Chg. Working Capital", d["chng_nc_wc"], "dollar"),
        ("Adjusted Book Value of Debt", d["bv_debt"], "dollar"),
        ("Adjusted Book Value of Equity", d["adjusted_bv_equity"], "dollar"),
    ]
    for lbl, v, fmt in inputs:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, BLUE_FILL)
        else:
            val_pct(r, 2, v, BLUE_FILL)
        r += 1

    r += 1  # blank

    # ====================================================================
    # SECTION 2 — PARAMETERS: HIGH GROWTH vs STABLE
    # ====================================================================
    section_header(r, 1, "Parameters")
    label(r, 2, "High Growth", bold=True)
    label(r, 3, "Stable", bold=True)
    r += 1

    params = [
        (
            "Length of Explicit Period (high-growth + fade)",
            d["growth_period"], "int", "Forever",
        ),
        ("Growth Rate (initial / at fade start)", d["growth_rate"], "pct", d["stable_growth"]),
        ("Beta used for stock", d["beta"], "num", d["stable_beta"]),
        ("Risk-free Rate", d["risk_free"], "pct", d["risk_free"]),
        ("Equity Risk Premium", d["eq_prem"], "pct", d["eq_prem"]),
        (
            "Pre-tax Cost of Debt",
            d["cost_of_debt_pretax"],
            "pct",
            d["cost_of_debt_pretax"],
        ),
        (
            "Effective Tax Rate (cash flow)",
            d["eff_tax_rate"],
            "pct",
            d["marginal_tax_rate"],
        ),
        (
            "Marginal Tax Rate (cost of debt)",
            d["marginal_tax_rate"],
            "pct",
            d["marginal_tax_rate"],
        ),
        ("Return on Capital", d["return_on_capital"], "pct", d["return_on_capital"]),
        ("Reinvestment Rate", d["reinvestment_rate"], "pct", d["stable_reinv_rate"]),
        ("Debt / (Debt + Equity)", d["percent_debt"], "pct", d["percent_debt"]),
    ]
    for lbl, hg_val, fmt, st_val in params:
        label(r, 1, lbl)
        if fmt == "pct":
            val_pct(r, 2, hg_val, YELLOW_FILL)
            val_pct(r, 3, st_val, YELLOW_FILL) if isinstance(
                st_val, float
            ) else ws.cell(row=r, column=3, value=st_val)
        elif fmt == "int":
            ws.cell(row=r, column=2, value=hg_val).fill = YELLOW_FILL
            ws.cell(row=r, column=3, value=st_val)
        else:
            c = ws.cell(row=r, column=2, value=hg_val)
            c.number_format = "0.00"
            c.fill = YELLOW_FILL
            c2 = ws.cell(row=r, column=3, value=st_val)
            c2.number_format = "0.00"
        r += 1

    r += 1  # blank

    # ====================================================================
    # SECTION 3 — COST OF CAPITAL OUTPUT
    # ====================================================================
    section_header(r, 1, "Cost of Capital — Output")
    r += 1

    coc_rows = [
        ("Cost of Equity", d["cost_of_equity"], "pct"),
        ("Equity / (Debt + Equity)", d["percent_equity"], "pct"),
        ("After-tax Cost of Debt", d["cost_of_debt_aftertax"], "pct"),
        ("Debt / (Debt + Equity)", d["percent_debt"], "pct"),
        ("Cost of Capital (WACC)", d["discount_rate"], "pct"),
    ]
    for lbl, v, fmt in coc_rows:
        label(r, 1, lbl)
        val_pct(r, 2, v, GREEN_FILL)
        r += 1

    r += 1

    label(r, 1, "Working Capital as % of Revenue")
    val_pct(r, 2, d["wc_pct_revenue"])
    r += 2

    # ====================================================================
    # SECTION 4 — YEAR-BY-YEAR FCFF TABLE
    # ====================================================================
    section_header(r, 1, f"Projected FCFF  (explicit period = {gp} years, incl. {TRANSITION_PERIOD}yr fade to stable growth)")
    year_cols = list(range(1, gp + 1))
    for i, yr in enumerate(year_cols):
        ws.cell(row=r, column=2 + i, value=f"Year {yr}").font = HEADER_FONT
    r += 1

    net_capex = d["capex"] - d["depreciation"]
    # Proportion of reinvestment attributable to net capex vs WC change
    total_reinv0 = (
        net_capex + d["chng_nc_wc"] if (net_capex + d["chng_nc_wc"]) != 0 else 1
    )
    capex_frac = net_capex / total_reinv0
    wc_frac = d["chng_nc_wc"] / total_reinv0

    # Per-year growth/reinvestment rate -- was a single flat rate repeated
    # across every column; now varies during the 3-stage fade's transition
    # years (2026-09-11, finding #6), so this reads the actual per-year
    # arrays instead of assuming one constant rate for the whole period.
    _cumulated_growth = []
    _acc = 1.0
    for _g in d["growth_n"]:
        _acc *= (1 + _g)
        _cumulated_growth.append(_acc - 1)

    table_rows = [
        ("Expected Growth Rate", d["growth_n"], "pct"),
        ("Cumulated Growth", _cumulated_growth, "pct"),
        ("Reinvestment Rate", d["reinvestment_rate_n"], "pct"),
        ("EBIT", d["ebit_n"], "dollar"),
        ("Tax Rate (cash flow)", [d["eff_tax_rate"]] * gp, "pct"),
        ("EBIT × (1 − tax rate)", d["ebiat_n"], "dollar"),
        (
            "− (CapEx − Depreciation)",
            [-r_v * capex_frac for r_v in d["reinv_n"]],
            "dollar",
        ),
        ("− Chg. Working Capital", [-r_v * wc_frac for r_v in d["reinv_n"]], "dollar"),
        ("Free Cash Flow to Firm", d["fcff_n"], "dollar"),
        ("Cost of Capital", [d["discount_rate"]] * gp, "pct"),
        (
            "Cumulated Cost of Capital",
            [(1 + d["discount_rate"]) ** (y + 1) for y in range(gp)],
            "num",
        ),
        (
            "Present Value",
            [d["fcff_n"][y] / (1 + d["discount_rate"]) ** (y + 1) for y in range(gp)],
            "dollar",
        ),
    ]

    for lbl, values, fmt in table_rows:
        label(r, 1, lbl)
        for i, v in enumerate(values):
            if fmt == "dollar":
                val_dollar(r, 2 + i, v)
            elif fmt == "pct":
                val_pct(r, 2 + i, v)
            else:
                c = ws.cell(row=r, column=2 + i, value=v)
                c.number_format = "0.0000"
        r += 1

    r += 1

    # ====================================================================
    # SECTION 5 — STABLE PHASE
    # ====================================================================
    section_header(r, 1, "Stable Phase")
    r += 1

    stable_rows = [
        ("Growth Rate in Stable Phase", d["stable_growth"], "pct"),
        ("Reinvestment Rate in Stable Phase", d["stable_reinv_rate"], "pct"),
        ("FCFF in Stable Phase", d["stable_fcff"], "dollar"),
        ("Cost of Equity in Stable Phase", d["stable_cost_of_equity"], "pct"),
        ("Cost of Capital in Stable Phase", d["stable_cost_of_capital"], "pct"),
        (
            "Value at End of Growth Phase (TV)",
            d["terminal_value_undiscounted"],
            "dollar",
        ),
        ("PV of Terminal Value", d["terminal_value_pv"], "dollar"),
    ]
    for lbl, v, fmt in stable_rows:
        label(r, 1, lbl)
        if fmt == "dollar":
            val_dollar(r, 2, v, YELLOW_FILL)
        else:
            val_pct(r, 2, v, YELLOW_FILL)
        r += 1

    r += 1

    # ====================================================================
    # SECTION 6 — VALUATION SUMMARY
    # ====================================================================
    section_header(r, 1, "Valuation")
    r += 1

    valuation_rows = [
        ("PV of FCFF in High Growth Phase", d["fcff_pv"], "dollar"),
        ("PV of Terminal Value of Firm", d["terminal_value_pv"], "dollar"),
        ("Value of Operating Assets", d["fcff_pv"] + d["terminal_value_pv"], "dollar"),
        ("+ Cash & Non-operating Assets", d["cash"], "dollar"),
        ("Value of Firm", d["enterprise_value"] + d["bv_debt"], "dollar"),
        ("− Market Value of Debt", d["bv_debt"], "dollar"),
        ("Market Value of Equity", d["enterprise_value"], "dollar"),
        ("÷ Shares Outstanding (000s)", d["shares_outstanding"], "num"),
        ("Value of Equity per Share", d["intrinsic_value"], "dollar"),
        ("Target Price (1-yr)", d["target_price"], "dollar"),
        ("Stock Price", d["price"], "dollar"),
        ("Margin of Safety ($)", d["margin_of_safety"], "dollar"),
        ("Margin of Safety (%)", d["margin_of_safety_pc"], "pct"),
    ]
    for lbl, v, fmt in valuation_rows:
        label(r, 1, lbl)
        fill = (
            GREEN_FILL
            if lbl
            in (
                "Value of Equity per Share",
                "Target Price (1-yr)",
                "Stock Price",
                "Margin of Safety ($)",
                "Margin of Safety (%)",
            )
            else None
        )
        if fmt == "dollar":
            val_dollar(r, 2, v, fill)
        elif fmt == "pct":
            val_pct(r, 2, v, fill)
        else:
            c = ws.cell(row=r, column=2, value=v)
            c.number_format = "#,##0"
            if fill:
                c.fill = fill
        r += 1


# ---------------------------------------------------------------------------
# XLSX report generation
# ---------------------------------------------------------------------------


def generate_summary_xlsx(
    valuations: list, output_path: str, index_label: str = "S&P 500"
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Valuation Summary"

    DARK_FILL = PatternFill("solid", fgColor="343A40")
    GREEN_FILL = PatternFill("solid", fgColor="D4EDDA")
    YELLOW_FILL = PatternFill("solid", fgColor="FFF3CD")
    RED_FILL = PatternFill("solid", fgColor="F8D7DA")
    HEADER_FONT = Font(bold=True, color="FFFFFF")
    THIN = Side(style="thin", color="CCCCCC")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

    # Metadata rows
    today = date.today().strftime("%B %d, %Y")
    ws.cell(
        row=1, column=1, value=f"{index_label} FCFF Valuation — {today}"
    ).font = Font(bold=True, size=13)
    ws.cell(
        row=2,
        column=1,
        value=f"Risk-free rate: {RISK_FREE * 100:.2f}%  |  ERP: {EQ_PREM * 100:.2f}%  |  "
        f"Stable growth: {STABLE_GROWTH * 100:.1f}%  |  "
        f"Growth period: {GROWTH_PERIOD} yrs  |  Stocks valued: {len(valuations)}",
    ).font = Font(italic=True, color="666666")

    # Header row
    headers = [
        "Ticker",
        "CIK",
        "Company",
        "Industry",
        "Price",
        "Intrinsic Value",
        "Target Price\n(1-yr)",
        "MoS ($)",
        "MoS (%)",
        "Growth Rate",
        "Cost of Capital",
        "Excess Return\n(ROIC-WACC)",
        "Unlevered Beta",
        "Market Cap ($B)",
    ]
    HDR_ROW = 4
    for col, hdr in enumerate(headers, 1):
        c = ws.cell(row=HDR_ROW, column=col, value=hdr)
        c.font = HEADER_FONT
        c.fill = DARK_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = BORDER

    # Column widths / number formats shared by both tabs
    col_widths = [8, 12, 28, 20, 10, 14, 14, 10, 10, 12, 14, 16, 13, 14]
    num_fmts = [
        None,
        None,
        None,
        None,
        '"$"#,##0.00',
        '"$"#,##0.00',
        '"$"#,##0.00',
        '"$"#,##0.00',
        "0.0%",
        "0.0%",
        "0.0%",
        "0.0%",
        "0.000",
        "#,##0.00",
    ]

    def _write_tab(sheet, rows):
        """Header row + data rows + column widths + freeze pane for one tab
        — shared by the main Valuation Summary and Value Creators sheets."""
        for col, hdr in enumerate(headers, 1):
            c = sheet.cell(row=HDR_ROW, column=col, value=hdr)
            c.font = HEADER_FONT
            c.fill = DARK_FILL
            c.alignment = Alignment(horizontal="center", wrap_text=True)
            c.border = BORDER
        sheet.row_dimensions[HDR_ROW].height = 30

        for row_idx, v in enumerate(rows, HDR_ROW + 1):
            if v.margin_of_safety >= 1:
                row_fill = GREEN_FILL
            elif v.margin_of_safety >= 0:
                row_fill = YELLOW_FILL
            else:
                row_fill = RED_FILL

            row_data = [
                v.ticker,
                v.cik,
                v.ent_name,
                v.industry,
                v.price,
                v.share_value,
                v.target_price,
                v.margin_of_safety,
                v.margin_of_safety_pc,
                v.growth_rate,
                v.cost_of_capital,
                v.wealth_pc,
                v.beta,
                v.market_cap / 1e9,
            ]
            for col, (val, fmt) in enumerate(zip(row_data, num_fmts), 1):
                c = sheet.cell(row=row_idx, column=col, value=val)
                c.fill = row_fill
                c.border = BORDER
                if fmt:
                    c.number_format = fmt
                c.alignment = Alignment(horizontal="left" if col <= 4 else "right")

        for col, w in enumerate(col_widths, 1):
            sheet.column_dimensions[get_column_letter(col)].width = w
        sheet.freeze_panes = sheet.cell(row=HDR_ROW + 1, column=1)

    _write_tab(ws, valuations)

    # ----------------------------------------------------------------
    # Tab 2 — Value Creators with positive MoS, sorted by Excess Return desc
    # ----------------------------------------------------------------
    creators = sorted(
        [v for v in valuations if v.wealth_pc > 0 and v.margin_of_safety > 0],
        key=lambda v: v.wealth_pc,
        reverse=True,
    )

    vc = wb.create_sheet(title="Value Creators")

    vc.cell(
        row=1,
        column=1,
        value=f"{index_label} — Value Creators & Undervalued Stocks — {today}",
    ).font = Font(bold=True, size=13)
    vc.cell(
        row=2,
        column=1,
        value=f"{len(creators)} of {len(valuations)} stocks (ROIC > WACC or MoS > $0) — sorted by Excess Return (ROIC − WACC) descending",
    ).font = Font(italic=True, color="666666")

    _write_tab(vc, creators)

    wb.save(output_path)
    print(f"Saved: {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _write_market_data_status(erp_status: dict, risk_free_status: dict) -> None:
    """
    Record refresh_market_data()'s outcome to data/market_data_fetch_status.json
    so Iggy's nightly valuation report can surface a fetch failure to Jim (see
    iggy-valuation-update SKILL.md's "Notes for Addie" section).

    get_erp()/get_risk_free() have no timeout or retry (unlike hg_dcflib's AV
    fetchers) — a hang or failure here previously only produced a logger.warning
    line nobody would see until this file existed. Best-effort: a failure to
    write this status file is logged but never blocks a valuation run.
    """
    status_path = _Path(os.path.abspath(__file__)).parent.parent / "data" / "market_data_fetch_status.json"
    try:
        status_path.parent.mkdir(parents=True, exist_ok=True)
        with open(status_path, "w") as f:
            json.dump({
                "date": date.today().isoformat(),
                "erp": erp_status,
                "risk_free": risk_free_status,
                "eq_prem_used": EQ_PREM,
                "risk_free_used": RISK_FREE,
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not write market_data_fetch_status.json: {e}")


def refresh_market_data():
    """
    Fetch live ERP and risk-free rate, falling back to the module-level
    constants on any failure. Deferred from module level so a slow/failed
    network call doesn't block argument parsing or import. Callers that
    import this module as a library (e.g. stock_analysis.py) must call
    this explicitly — it does not run automatically except via main().

    Always records the outcome via _write_market_data_status(), even on
    success — see that function's docstring for why.
    """
    global EQ_PREM, RISK_FREE
    erp_status = {"ok": True, "error": None}
    risk_free_status = {"ok": True, "error": None}
    try:
        _erp = hg_dcflib.get_erp()
        if _erp is None:
            erp_status = {"ok": False, "error": "get_erp() returned None (no Implied ERP % parsed)"}
            logger.warning(f"ERP returned None; using fallback {EQ_PREM:.4f}")
        else:
            EQ_PREM = _erp
            logger.info(f"ERP: {EQ_PREM:.4f}")
    except Exception as e:
        erp_status = {"ok": False, "error": str(e)}
        logger.warning(f"ERP fetch failed ({e}); using fallback {EQ_PREM:.4f}")
    try:
        _rf = hg_dcflib.get_risk_free(FRED_KEY)
        if _rf is None:
            risk_free_status = {"ok": False, "error": "get_risk_free() returned None (non-200 FRED response)"}
            logger.warning(f"Risk-free rate returned None; using fallback {RISK_FREE:.4f}")
        else:
            RISK_FREE = _rf
            logger.info(f"Risk-free: {RISK_FREE:.4f}")
    except Exception as e:
        risk_free_status = {"ok": False, "error": str(e)}
        logger.warning(f"Risk-free rate fetch failed ({e}); using fallback {RISK_FREE:.4f}")

    _write_market_data_status(erp_status, risk_free_status)


def main():
    parser = argparse.ArgumentParser(
        description="HessGrp FCFF/FCFE valuation engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python av_fcff_2.py --ticker AAPL\n"
            "  python av_fcff_2.py --index sp500 --limit 50\n"
            "  python av_fcff_2.py --filings ~/HessGrp/data/pending_valuations_2026-04-22.json\n"
            "  python av_fcff_2.py              # interactive menu"
        ),
    )
    parser.add_argument("--ticker",   metavar="SYM", action="append",
                        help="Value one or more stocks (repeat for multiple: --ticker AAPL --ticker JBL). Excel output + DB update.")
    parser.add_argument("--index",    choices=["sp500", "r2000"],
                        help="Batch valuation across S&P 500 or Russell 2000")
    parser.add_argument("--filings",  metavar="FILE",
                        help="Tickers from sec_daily_index JSON/xlsx/txt output")
    parser.add_argument("--limit",    type=int, default=None, metavar="N",
                        help="Cap batch to first N tickers")
    parser.add_argument("--growth",   type=int, default=GROWTH_PERIOD, metavar="N",
                        help=f"High-growth period in years (default {GROWTH_PERIOD})")
    parser.add_argument("--db",       default=DEFAULT_DB, metavar="PATH",
                        help="Path to valuation.db (default: $VALUATION_DB or /Volumes/Financial_Data/valuation.db)")
    parser.add_argument("--equity-override", type=float, default=None, metavar="DOLLARS",
                        help="Override shareholders' equity (in dollars) for all tickers in this run. "
                             "Use when AV balance sheet data is known to be incorrect (e.g., PPG Q1 2026). "
                             "Example: --equity-override 8104000000")
    parser.add_argument("--provider", choices=["av", "intrinio"], default="intrinio",
                        help="Fundamentals data source (default: intrinio, with automatic fallback "
                             "to AV per-ticker if an Intrinio fetch fails — see docs/decisions.md, "
                             "'Data provider: Intrinio becomes primary'). Pass --provider av to force "
                             "AV only, no fallback, e.g. for a manual AV-side comparison run.")
    args = parser.parse_args()

    growth_period = args.growth

    global EQUITY_OVERRIDE, DATA_PROVIDER
    if args.equity_override is not None:
        EQUITY_OVERRIDE = args.equity_override
        print(f"  equity override active: ${EQUITY_OVERRIDE:,.0f}")
    DATA_PROVIDER = args.provider
    if DATA_PROVIDER == "intrinio":
        print("  data provider: Intrinio (primary), AV fallback on per-ticker failure")
    else:
        print("  data provider: AV only (--provider av — no Intrinio fallback)")
    db_path       = args.db

    # ---- Fetch market reference data (deferred from module level) --------
    print("Fetching market reference data (ERP, risk-free rate)...")
    refresh_market_data()
    print(f"  ERP: {EQ_PREM:.4f}   Risk-free: {RISK_FREE:.4f}")

    # ---- Determine run mode ---------------------------------------------
    tickers      = []
    single_stock = False
    ticker       = None

    if args.ticker:
        ticker_list = [t.strip().upper() for t in args.ticker]
        if len(ticker_list) == 1:
            ticker       = ticker_list[0]
            index_label  = ticker
            single_stock = True
        else:
            tickers     = ticker_list
            index_label = "+".join(ticker_list[:3]) + ("..." if len(ticker_list) > 3 else "")
            print(f"\nTicker-list mode: valuing {len(tickers)} stocks")

    elif args.filings:
        if not os.path.exists(args.filings):
            print(f"Error: filings file not found: {args.filings}", file=sys.stderr)
            sys.exit(1)
        tickers     = get_tickers_from_filings(args.filings)
        index_label = "filings"
        print(f"\nFilings mode: valuing {len(tickers)} stocks from {args.filings}")

    elif args.index:
        if args.index == "sp500":
            tickers     = get_sp500_tickers()
            index_label = "sp500"
        else:
            tickers     = get_russell2000_tickers()
            index_label = "r2000"

    else:
        # Interactive fallback — used when launched from hess_menu
        print("\nSelect index to value:")
        print("  1. S&P 500")
        print("  2. Russell 2000")
        print("  3. Single stock")
        while True:
            choice = input("Choice [1/2/3]: ").strip()
            if choice == "1":
                tickers     = get_sp500_tickers()
                index_label = "sp500"
                break
            elif choice == "2":
                tickers     = get_russell2000_tickers()
                index_label = "r2000"
                break
            elif choice == "3":
                ticker       = input("Enter ticker symbol: ").strip().upper()
                index_label  = ticker
                single_stock = True
                break
            else:
                print("Please enter 1, 2, or 3.")

    # ---- Single-stock path: Excel output + DB update --------------------
    if single_stock:
        output_file = os.path.join(
            _log_dir,
            f"value_{index_label}_{date.today().strftime('%Y%m%d')}.xlsx",
        )
        detail = value_stock_detail(ticker, growth_period, db_path)
        if detail:
            generate_xlsx(detail, output_file)
            try:
                db_conn = sqlite3.connect(db_path, timeout=30)
                db_conn.execute("PRAGMA journal_mode=WAL")
                create_table(db_conn)
                insert_valuation(db_conn, _stock_value_from_detail(detail))
                db_conn.close()
                print(f"Valuation for {ticker} saved to database.")
                _rescore_tickers(db_path, [ticker])
            except Exception as e:
                logger.warning(f"DB write failed for {ticker}: {e}")
        else:
            try:
                industry = hg_dcflib.get_industry(ticker)
            except Exception:
                industry = ""
            print(f"Valuation failed for {ticker}.")
        return

    # ---- Batch path: Excel output ---------------------------------------
    if args.limit:
        tickers = tickers[:args.limit]

    # Skip permanently-excluded tickers before the first attempt, not just
    # before tomorrow's Iggy-level retry filtering — see get_excluded_tickers()
    # docstring and docs/known_errors.md 2026-07-23. Batch modes only: a
    # deliberate multi-ticker request (e.g. --ticker A --ticker B) still goes
    # through this filter since it's the same "many tickers, one run" pattern
    # this exists to protect; true single-stock mode is handled separately
    # above and is never filtered, so a one-off re-check of an excluded ticker
    # (exactly how the 2026-07-23 exclusions were themselves verified) still works.
    excluded = get_excluded_tickers()
    if excluded:
        already_excluded = [t for t in tickers if t.upper() in excluded]
        if already_excluded:
            print(
                f"  Skipping {len(already_excluded)} permanently-excluded ticker(s) "
                f"(see data/excluded_tickers.json): {', '.join(already_excluded)}"
            )
            tickers = [t for t in tickers if t.upper() not in excluded]

    print(
        f"Prefetching quotes for {len(tickers)} tickers (separate batch — "
        f"see docs/known_errors.md 2026-07-22 AV support guidance)..."
    )
    prefetch_quotes(tickers, MY_API_KEY)

    print(
        f"Valuing {len(tickers)} {index_label.upper()} stocks (growth period = {growth_period} years) ..."
    )

    # Optionally write to DB
    try:
        db_conn = sqlite3.connect(db_path, timeout=30)
        db_conn.execute("PRAGMA journal_mode=WAL")
        create_table(db_conn)
    except Exception:
        db_conn = None
        logger.warning("Database unavailable; skipping DB writes.")

    valuations = []
    valued_tickers = []
    failed_tickers = []
    total = len(tickers)
    bar_width = 40
    start_time = time.time()
    for idx, ticker in enumerate(tickers, 1):
        result = value_stock(ticker, growth_period, db_path)
        if result:
            valuations.append(result)
            valued_tickers.append(result.ticker)
            if db_conn:
                try:
                    insert_valuation(db_conn, result)
                except Exception as e:
                    logger.warning(f"DB insert failed for {ticker}: {e}")
        else:
            failed_tickers.append(ticker)

        filled = int(bar_width * idx / total)
        bar = "#" * filled + "-" * (bar_width - filled)
        elapsed = int(time.time() - start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        print(f"\r  {idx}/{total} [{bar}] {h:02d}:{m:02d}:{s:02d}", end="", flush=True)

    print()  # newline after progress bar

    # Second pass: retry tickers that failed on the first pass, after one
    # deliberate cool-off. AV support confirmed (2026-07-22) that a longer
    # cool-off clears transient per-minute micro-throttles better than our
    # existing fast in-place retries (5s/15s) alone — every retry sequence in
    # the run that prompted this fix exhausted both in-place retries without
    # ever succeeding. Rather than stretching the in-place backoff to AV's
    # suggested 60-90s ceiling (which would multiply added runtime across
    # every failing ticker on the first pass), we pay one 60s cool-off ONCE
    # here and then reuse the same fast in-place retries for the smaller
    # failed-only subset — cheaper, and this is also exactly the workflow
    # TASK-109/114/115 already proved out manually (re-run the stale subset
    # after time has passed), just automated into a single run.
    if failed_tickers:
        print(
            f"\n{len(failed_tickers)} ticker(s) failed on first pass — "
            f"retrying after a 60s cool-off..."
        )
        time.sleep(60)
        retry_total = len(failed_tickers)
        for idx, ticker in enumerate(failed_tickers, 1):
            result = value_stock(ticker, growth_period, db_path)
            if result:
                valuations.append(result)
                valued_tickers.append(result.ticker)
                if db_conn:
                    try:
                        insert_valuation(db_conn, result)
                    except Exception as e:
                        logger.warning(f"DB insert failed for {ticker}: {e}")
            print(f"\r  retry {idx}/{retry_total}", end="", flush=True)
        print()
        still_failed = [t for t in failed_tickers if t not in valued_tickers]
        if still_failed:
            print(
                f"{len(still_failed)} ticker(s) still failed after retry pass: "
                f"{', '.join(still_failed)}"
            )

    if db_conn:
        db_conn.close()

    _rescore_tickers(db_path, valued_tickers)

    valuations.sort(key=lambda v: v.margin_of_safety, reverse=True)

    _index_display = {"sp500": "S&P 500", "r2000": "Russell 2000"}.get(
        index_label, index_label
    )
    output_file = os.path.join(
        _log_dir,
        f"value_{index_label}_{date.today().strftime('%Y%m%d')}.xlsx",
    )
    generate_summary_xlsx(valuations, output_file, _index_display)
    print(f"Done. {len(valuations)}/{len(tickers)} stocks valued successfully.")


if __name__ == "__main__":
    main()
