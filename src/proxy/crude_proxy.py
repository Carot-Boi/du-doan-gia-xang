"""
Phase 3: a live crude-oil-price proxy for the world-price days MOIT hasn't
published a bulletin for yet.

SECOND CORRECTION (2026-09-08, same day as the first): the FRED-based
version of this module (DCOILBRENTEU, "daily") was still wrong in a way
that mattered a lot -- FRED's daily oil series themselves lag real trading
by close to a WEEK (confirmed by hand: on 2026-09-08, FRED's latest
DCOILBRENTEU point was still 2026-09-01's 96.02). The project owner caught
this directly: crude had visibly kept climbing (WTI ~85-86 at the last
real MOIT bulletin's cycle, ~93 as of today) while this module's own
forward-filled value was stuck a week behind, at one point making a
next-cycle PREDICTION move in the opposite direction of where crude
actually was -- not just imprecise, actively backwards.

NEW SOURCE: Yahoo Finance's chart endpoint for the front-month futures
contract (unofficial, undocumented by Yahoo, but widely used, free, no
signup, no API key):

    https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?interval=1d&range=2y

Verified live (2026-09-08): returns near-real-time intraday price
(`meta.regularMarketPrice`, updated within the trading session -- not a
settlement print from a week ago) AND 505 daily closes spanning 2 years,
which is MORE history than FRED's Brent series gave us, not less. BZ=F is
the Brent Crude front-month futures contract; CL=F (WTI) is available the
same way if ever needed.

This is an unofficial endpoint with no SLA or documented stability
guarantee -- Yahoo could change or block it without notice. Accepted
trade-off: it is measurably, materially more accurate for THIS project's
actual use (nowcasting the next few days) than a "genuinely documented"
source that is a week stale. If this endpoint ever breaks, ProxyFetchError
surfaces it loudly (see fetch_yahoo_brent_series()) rather than silently
falling back to stale data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

YAHOO_SYMBOL = "BZ=F"  # Brent Crude front-month futures
YAHOO_CHART_URL = f"https://query1.finance.yahoo.com/v8/finance/chart/{YAHOO_SYMBOL}"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; du-doan-gia-xang-bot/0.1)"}


class ProxyFetchError(RuntimeError):
    """Raised when the live proxy feed can't be fetched or parsed."""


@dataclass(frozen=True)
class CrudeProxySeries:
    """A daily (trading_date -> USD/barrel Brent crude) series, sorted
    ascending by date. Deliberately dumb/immutable data holder so it's easy
    to build a synthetic one for tests without touching the network.

    Only trades on weekdays (no weekend/holiday rows) -- value_on() forward-
    fills those small gaps."""

    daily: list[tuple[date, float]]  # [(YYYY-MM-DD, value), ...] ascending

    def as_dict(self) -> dict[str, float]:
        """Keyed by ISO 'YYYY-MM-DD' string, for exact-date joins against
        world_price_daily rows (see src/pricing/bridge.py)."""
        return {d.isoformat(): v for d, v in self.daily}

    def latest(self) -> tuple[date, float] | None:
        return self.daily[-1] if self.daily else None

    def value_on(self, d: date) -> float | None:
        """
        The proxy's value for calendar day `d`, using hold-flat (forward
        fill) from the most recent trading day whose date is <= d -- covers
        weekends/holidays the crude market itself doesn't trade. If `d` is
        before every date we have, falls back to the EARLIEST known value
        (backward fill) rather than returning None. Returns None only if
        the series is empty.
        """
        if not self.daily:
            return None
        best: float | None = None
        for day, value in self.daily:
            if day <= d:
                best = value
            else:
                break
        if best is not None:
            return best
        return self.daily[0][1]  # d is before all known days -> backward fill


def fetch_yahoo_brent_series(range_: str = "2y", timeout: float = 30.0) -> CrudeProxySeries:
    """
    Fetch Yahoo Finance's daily-close history for Brent front-month futures
    (BZ=F), PLUS today's near-real-time intraday price appended as the
    latest point if the market is still trading today's session (so a
    same-day price move -- exactly what caught the previous bug -- shows up
    immediately instead of waiting for tomorrow's daily close).

    No API key required. Raises ProxyFetchError on any network/parse
    failure -- callers decide whether that's fatal or worth falling back on
    stale/cached data; this function does not silently swallow errors.
    """
    try:
        resp = requests.get(
            YAHOO_CHART_URL, params={"interval": "1d", "range": range_}, timeout=timeout, headers=_HEADERS,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise ProxyFetchError(f"could not fetch Yahoo Finance chart for {YAHOO_SYMBOL}: {e}") from e

    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
        gmtoffset = meta.get("gmtoffset", 0)
        tz = timezone(timedelta(seconds=gmtoffset))
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProxyFetchError(f"unexpected Yahoo Finance chart response shape for {YAHOO_SYMBOL}: {e}") from e

    rows: dict[date, float] = {}
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        d = datetime.fromtimestamp(ts, tz=tz).date()
        rows[d] = float(close)  # later (more complete) daily bars overwrite earlier partial ones for the same date

    # Append/overwrite with today's live intraday price, if present and
    # newer than the last daily close -- this is the whole point: don't
    # wait for a settlement print to see a same-day move.
    live_price = meta.get("regularMarketPrice")
    live_time = meta.get("regularMarketTime")
    if live_price is not None and live_time is not None:
        live_date = datetime.fromtimestamp(live_time, tz=tz).date()
        rows[live_date] = float(live_price)

    if not rows:
        raise ProxyFetchError(f"Yahoo Finance chart for {YAHOO_SYMBOL} returned no usable observations")

    return CrudeProxySeries(daily=sorted(rows.items()))
