"""
RESEARCH COPY of the proposed production module `lib/hg_ownership_controls.py`.
NOT applied to the production checkout.

Gap #1 fix: `fetch_current_observations()` and the curated-evidence lookup
are kept as two SEPARATE return values — never merged into one object
before `assess()` sees them. `assess()` itself (in assessment_engine.py)
is what decides, via `revalidate()`, whether the curated evidence is
still safe to use.

Gap #2 fix: explicit failure policy. `ownership_control_note()` NEVER
raises — every failure mode (unreadable database, missing schema/
malformed rows, an exception inside assess()/formatting) is caught and
turned into a clearly-labeled `OWNERSHIP_SCREEN_ERROR` note instead of
either a silent "" or an uncaught exception that could abort the calling
valuation. The one thing this function is never allowed to do is make the
caller's valuation fail because OF it.

Gap #3 fix: the returned note text comes from `user_facing_flags.
format_for_report_surface()` — the SAME shared formatter the API surface
uses — not a separate, lossier ad hoc string.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Optional

from ownership_model import DataStatus, OwnershipProfile
from assessment_engine import OverallReadiness, assess
from curated_evidence import ALL_CURATED_EVIDENCE
from user_facing_flags import format_for_report_surface

NCI_ORDINARY_TAG = "noncontrollinginterests"
NCI_REDEEMABLE_TAG = "redeemablenoncontrollinginterest"


class OwnershipScreenError(Exception):
    """Raised internally only — always caught by ownership_control_note()
    before it can propagate to the caller. Exists so the three failure
    categories below share one consistent message shape."""


def _classify_value(row) -> tuple[Optional[float], DataStatus]:
    if row is None:
        return None, DataStatus.MISSING
    value, is_explicit_zero = row
    if value is None and not is_explicit_zero:
        return None, DataStatus.MISSING
    if is_explicit_zero or value == 0.0:
        return 0.0, DataStatus.EXPLICIT_ZERO
    return (value, DataStatus.POSITIVE) if value > 0 else (value, DataStatus.NEGATIVE)


def fetch_current_observations(ticker: str, financial_data_db_path: str, today_iso: str) -> OwnershipProfile:
    """Always returns a fresh, current-only OwnershipProfile. Never
    consults the curated-evidence table — that lookup happens separately,
    in `ownership_control_note()`, so the two are never accidentally
    merged before `assess()`'s own revalidation check runs.

    Raises OwnershipScreenError (never a raw sqlite3/other exception) on
    any failure — caught unconditionally by the one caller below."""
    try:
        conn = sqlite3.connect(f"file:{financial_data_db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise OwnershipScreenError(f"database unreadable ({financial_data_db_path}): {exc}") from exc

    try:
        try:
            comp = conn.execute(
                "SELECT c.company_id, c.identity_quarantined FROM ticker_history th "
                "JOIN companies c ON th.company_id = c.company_id WHERE th.ticker=? AND th.is_current=1",
                (ticker,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise OwnershipScreenError(f"schema mismatch querying ticker_history/companies: {exc}") from exc

        if comp is None:
            return OwnershipProfile(ticker=ticker, no_company_match=True)
        company_id, identity_quarantined = comp

        try:
            fund = conn.execute(
                "SELECT fundamental_id, period_end_date, quarantined FROM fundamentals "
                "WHERE company_id=? AND statement_type='balance_sheet_statement' AND is_current=1 "
                "ORDER BY period_end_date DESC, filing_date DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise OwnershipScreenError(f"schema mismatch querying fundamentals: {exc}") from exc

        if fund is None:
            return OwnershipProfile(ticker=ticker, identity_quarantined=bool(identity_quarantined))

        try:
            fundamental_id, bs_date, fundamentals_quarantined = fund[0], fund[1], bool(fund[2])
        except (IndexError, TypeError) as exc:
            raise OwnershipScreenError(f"malformed fundamentals row for {ticker}: {exc}") from exc

        freshness_days = None
        if bs_date:
            try:
                freshness_days = (datetime.date.fromisoformat(today_iso) - datetime.date.fromisoformat(bs_date)).days
            except ValueError:
                pass  # leave freshness_days=None rather than guessing — not a hard failure

        def get_tag(tag_name):
            try:
                return conn.execute(
                    "SELECT fv.value, fv.is_explicit_zero FROM financial_values fv "
                    "JOIN financial_tags ft ON fv.tag_id = ft.tag_id "
                    "WHERE fv.fundamental_id=? AND ft.intrinio_tag=?",
                    (fundamental_id, tag_name),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                raise OwnershipScreenError(f"schema mismatch querying financial_values/financial_tags: {exc}") from exc

        ord_val, ord_status = _classify_value(get_tag(NCI_ORDINARY_TAG))
        red_val, red_status = _classify_value(get_tag(NCI_REDEEMABLE_TAG))

        return OwnershipProfile(
            ticker=ticker,
            ordinary_nci_book=ord_val, ordinary_nci_status=ord_status,
            redeemable_nci_book=red_val, redeemable_nci_status=red_status,
            balance_sheet_as_of=bs_date, balance_sheet_freshness_days=freshness_days,
            fundamentals_quarantined=fundamentals_quarantined, identity_quarantined=bool(identity_quarantined),
        )
    finally:
        conn.close()


def ownership_control_note(ticker: str, market_cap: float, financial_data_db_path: str, today_iso: str) -> str:
    """The single function the production patch calls. NEVER raises —
    every failure mode is caught and converted to an explicit
    OWNERSHIP_SCREEN_ERROR note (gap #2's failure policy). The one normal
    silent case is a confirmed, no-claim-at-all ticker (format_for_report_surface
    already returns "" for READY_NO_CLAIM, filtered out by the existing
    ` if n` guard in av_fcff_2.py's notes-chain generator, same contract
    as every other *_note() function already chained there)."""
    try:
        profile = fetch_current_observations(ticker, financial_data_db_path, today_iso)
        curated_entry = ALL_CURATED_EVIDENCE.get(ticker)
        curated = curated_entry[0]() if curated_entry is not None else None
        a = assess(profile, curated, market_cap)
        if a.overall_readiness == OverallReadiness.READY_NO_CLAIM:
            return ""  # the one normal silent case — genuinely nothing to report
        return format_for_report_surface(a)
    except OwnershipScreenError as exc:
        return format_for_report_surface(None, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — deliberately broad: this function must NEVER propagate
        return format_for_report_surface(None, error=f"unexpected assessment/formatting failure: {exc}")
