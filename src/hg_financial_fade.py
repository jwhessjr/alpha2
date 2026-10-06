"""
Shared linear-fade utility for the financial-firm DivGinzu/EqExRet engines
(lib/hg_financial_divginzu.py, lib/hg_financial_eqexret.py).

Both of Damodaran's spreadsheets fade scalar inputs (ROE, cost of equity,
payout ratio, and -- divginzu only -- growth) linearly across the back
portion of the explicit forecast window, landing exactly on the stable-phase
value in the final explicit year. Confirmed by direct cell-value
reproduction against both downloaded workbooks (2026-10-05):

  ~/Desktop/HessGrp_RR_Work_2026-09/banks/damodaran_reference/eqexret.xls
  ~/Desktop/HessGrp_RR_Work_2026-09/banks/damodaran_reference/divginzu.xls

See docs/decisions.md, 2026-10-05, "DivGinzu primary / EqExRet cross-check
financial-firm architecture" for the full derivation.
"""
from __future__ import annotations


def linear_fade(explicit_value: float, stable_value: float, total_years: int, fade_years: int) -> list[float]:
    """
    Returns a list of length total_years.

    The first (total_years - fade_years) entries equal explicit_value
    (flat -- Damodaran's "high growth" phase). The last fade_years entries
    linearly interpolate from explicit_value toward stable_value, landing
    EXACTLY on stable_value in the final entry -- this is Damodaran's
    "gradually adjust your inputs during the second half" toggle, confirmed
    from both workbooks' own stored cell values (not inferred from labels).

    fade_years=0 reproduces the toggle's "No" answer: flat explicit_value
    for the whole period, no transition to stable at all.
    """
    if fade_years < 0:
        raise ValueError(f"fade_years cannot be negative, got {fade_years}")
    if fade_years > total_years:
        raise ValueError(f"fade_years ({fade_years}) cannot exceed total_years ({total_years})")
    if fade_years == 0:
        return [explicit_value] * total_years

    flat_years = total_years - fade_years
    path = [explicit_value] * flat_years
    step = (stable_value - explicit_value) / fade_years
    for i in range(1, fade_years + 1):
        path.append(explicit_value + step * i)
    return path


_TOLERANCE = 1e-9


def payout_retention_invariant_error(
    *, engine: str, context: str, payout: float, retention: float,
    roe: float | None = None, growth: float | None = None,
    ticker: str | None = None, period: str | None = None,
) -> str | None:
    """
    Shared [0, 1] invariant check for a payout/retention pair, reused by
    both lib/hg_financial_divginzu.py and lib/hg_financial_eqexret.py
    (2026-10-06 hardening pass) so the bound and its message format can't
    drift between the two engines.

    Returns None if both payout and retention are within [0, 1] (a small
    floating-point tolerance is allowed for values that land exactly on
    the identity payout = 1 - retention). Returns a fully-formatted error
    string -- never raises itself -- if either is out of bounds; the
    caller (each engine's own value_*() function) raises ITS OWN existing
    exception type with this string, so existing callers that catch
    DivGinzuInvalidInputs/EqExRetInvalidInputs specifically are unaffected
    by this shared helper's existence.

    Never clamps, never substitutes a fallback -- a violation is always a
    hard stop, per docs/decisions.md 2026-10-06: "Missing/invalid data
    must not silently create an invented number."
    """
    lo, hi = -_TOLERANCE, 1.0 + _TOLERANCE
    if lo <= payout <= hi and lo <= retention <= hi:
        return None

    parts = [
        f"{engine}: payout/retention invariant violated ({context})",
        f"payout={payout!r}", f"retention={retention!r}",
    ]
    if roe is not None:
        parts.append(f"roe={roe!r}")
    if growth is not None:
        parts.append(f"growth={growth!r}")
    if ticker is not None:
        parts.append(f"ticker={ticker!r}")
    if period is not None:
        parts.append(f"period={period!r}")
    parts.append("payout and retention must both lie in [0, 1] -- not clamped, not substituted")
    return " | ".join(parts)
