"""
Damodaran financial-industry stable-ROE lookup -- the approved automated
source for the one input both lib/hg_financial_divginzu.py and
lib/hg_financial_eqexret.py require but cannot derive from a formula
(confirmed from both reference spreadsheets: stable ROE is hand-typed
analyst judgment, never a formula output).

APPROVED RULE (docs/decisions.md, 2026-10-05, "Stable-ROE: Damodaran
industry-average ROE"): Stable ROE for automated financial-firm valuation
= Damodaran's own industry-average ROE for the ticker's confirmed
financial peer bucket. NOT stable cost of equity, NOT a company-specific
premium, NOT a moat adjustment, NOT an arbitrary fixed spread.

Data source: reference_data/industry_roe_2026.xlsx -- a small, static,
versioned HessGrp reference file, NOT a runtime parse of divginzu.xls.
Extracted once, by hand, via
~/Desktop/HessGrp_RR_Work_2026-09/banks/scratch/extract_industry_roe.py
from divginzu.xls's own "US Industry averages" sheet (SHA-256
e7556693afa15aa286387f9cde08768fb914eb3ea64b35a0ae88ab79c1124d93,
workbook Last Saved 2026-02-16) -- the same underlying Damodaran dataset
vintage as reference_data/betas_2026.xls (confirmed: both files' levered
beta figures for every bucket match byte-for-byte). Every row in the
reference file carries its own provenance columns (source file, sheet,
hash, vintage, extraction date) -- see that file directly for the exact
values, this module never duplicates them as inline constants.

Uses the exact same Damodaran bucket strings
lib/hg_financial_classifier.py already uses for beta peer-group selection
-- this module does not invent a second classification vocabulary.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

# Resolved relative to this file (lib/hg_financial_stable_roe.py), not a
# hardcoded absolute repo path -- this file is new as of 2026-10-05 and
# does not yet exist in every checkout of this codebase (e.g. the primary
# /Users/jhess/HessGrp working directory, deliberately untouched by this
# implementation pass), so a relative resolution is the only form that
# works correctly in the isolated worktree it was built in today.
INDUSTRY_ROE_WORKBOOK_PATH = str(Path(__file__).resolve().parent.parent / "reference_data" / "industry_roe_2026.xlsx")


class StableRoeUnavailable(Exception):
    """Raised when no Damodaran industry-average ROE exists for the
    requested bucket. Never silently substituted with stable cost of
    equity, historical company ROE, 10%, peer beta, or any other
    fallback constant -- a missing/invalid bucket is a controlled stop,
    not a guess."""


@dataclass
class StableRoeResult:
    bucket: str
    stable_roe: float
    source: str              # "damodaran_industry_average"
    source_file: str
    source_vintage: str
    number_of_firms: float
    override_applied: bool = False


_cache: pd.DataFrame | None = None


def _load() -> pd.DataFrame:
    global _cache
    if _cache is None:
        _cache = pd.read_excel(INDUSTRY_ROE_WORKBOOK_PATH, sheet_name="Industry Stable ROE", engine="openpyxl")
    return _cache


def get_stable_roe(damodaran_bucket: str, override: float | None = None) -> StableRoeResult:
    """
    Look up Damodaran's industry-average ROE for `damodaran_bucket`.

    `override`, when supplied (not None), is used INSTEAD of the looked-up
    value -- but the lookup still runs first (so source metadata for the
    bucket is still available/reported), and the result's
    `override_applied` flag is set so every caller can see an override
    occurred. No silent override behavior: a caller that wants the
    Damodaran default simply never passes this argument.

    Raises StableRoeUnavailable if the bucket has no row in the reference
    dataset -- this is a controlled stop, not a fallback point.
    """
    df = _load()
    row = df[df["Industry Name"] == damodaran_bucket]
    if row.empty:
        raise StableRoeUnavailable(
            f"No Damodaran industry-average ROE on record for bucket {damodaran_bucket!r} "
            f"in {INDUSTRY_ROE_WORKBOOK_PATH} -- do not substitute stable COE, a fixed "
            "constant, or any other fallback; refresh the reference dataset or escalate."
        )
    r = row.iloc[0]
    stable_roe = float(r["ROE"]) if override is None else float(override)
    return StableRoeResult(
        bucket=damodaran_bucket, stable_roe=stable_roe, source="damodaran_industry_average",
        source_file=str(r["Damodaran Source File"]), source_vintage=str(r["Data Vintage"]),
        number_of_firms=float(r["Number of firms"]), override_applied=override is not None,
    )
