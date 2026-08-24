"""
config.py — Central configuration for all HessGrp scripts.

Import from here instead of redefining paths and constants in each script:

    from config import PORTFOLIO_DB, VALUATION_DB, DATA_DIR, ETF_EXCLUSIONS, AV_DELAY
"""

import os
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

HESSGRP      = Path.home() / "HessGrp"
DATA_DIR     = HESSGRP / "data"
PORTFOLIO_DB = DATA_DIR / "portfolio.db"
VALUATION_DB = Path(os.environ.get("VALUATION_DB", "/Volumes/Financial_Data/valuation.db"))

# ── Alpha Vantage ─────────────────────────────────────────────────────────────

AV_KEY   = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
AV_DELAY = 0.90   # seconds between calls (~67/min, under 75/min premium limit)

# ── FRED (St. Louis Fed) ──────────────────────────────────────────────────────

FRED_KEY = os.environ.get("FRED_API_KEY", "")

# ── Intrinio ──────────────────────────────────────────────────────────────────


def _load_intrinio_key() -> str:
    """
    INTRINIO_API_KEY lives in ~/.hessgrp_credentials (plain KEY=value, no
    export prefix) rather than being exported via .zshenv like AV's key —
    mirrors the loader already duplicated in compare_data_providers.py and
    ebit_spot_check.py.
    """
    cred_path = Path.home() / ".hessgrp_credentials"
    if cred_path.exists():
        for line in cred_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("INTRINIO_API_KEY="):
                return line.split("=", 1)[1].strip()
    return os.environ.get("INTRINIO_API_KEY", "")


INTRINIO_KEY = _load_intrinio_key()
INTRINIO_BASE_URL = "https://api-v2.intrinio.com"

# ── Portfolio evaluation exclusions ──────────────────────────────────────────

# Tickers excluded from DCF-based buy/sell evaluation (ETFs, index funds, cash)
ETF_EXCLUSIONS: frozenset[str] = frozenset({
    "VOO", "VGSH", "IWM", "SPY", "QQQ", "VTI", "BND", "AGG", "CASH",
    "VTSAX", "VBTLX", "VTIAX", "FWGIX", "BIL",
})

# ── Scoring thresholds ────────────────────────────────────────────────────────

SELL_THRESHOLD  = 0.20   # price > 120% of intrinsic value → SELL
WATCH_THRESHOLD = 0.10   # price > 110% of intrinsic value → WATCH
