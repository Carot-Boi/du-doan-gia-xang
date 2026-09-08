"""
Phase 3: a live crude-oil-price proxy for the world-price days MOIT hasn't
published a bulletin for yet.

CORRECTION (2026-09-08): the original version of this module claimed "no
genuinely free, no-signup, DAILY option actually exists" and used FRED's
MONTHLY Dubai crude series (POILDUBUSDM), held flat across every day within
a month. That claim was simply wrong -- re-checked by hand after the
project owner pointed out (correctly) that day-by-day tracking is the
whole point of nowcasting a not-yet-published cycle. FRED also publishes
**daily** crude series with no signup at all:

    curl https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILBRENTEU
    curl https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO

Both confirmed live (verified 2026-09-08, latest row same-week). This
module now uses **DCOILBRENTEU** ("Crude Oil Prices: Brent - Europe"),
not WTI and not the previous Dubai series:

- Brent over WTI: Brent is the seaborne global benchmark that Asian
  refined-product markets (including Singapore, where MOIT's own "giá
  thế giới" benchmark is actually quoted -- Platts Singapore/MOPS) track
  more closely day-to-day than WTI, which is a landlocked US benchmark
  with its own idiosyncratic Cushing-storage dynamics.
- Brent over the old Dubai series: Dubai/Oman is arguably the more
  precise regional benchmark for what actually feeds Singapore refining,
  but FRED only publishes it MONTHLY -- exactly the granularity problem
  being fixed here. Brent is DAILY. Since src/pricing/bridge.py's
  crude->product bridge is refit from real historical data (not a fixed
  textbook ratio), it calibrates away most of the systematic Brent-vs-
  actual-benchmark level difference; what daily Brent buys us is real
  day-to-day MOVEMENT, which a stale monthly print structurally cannot
  provide no matter which crude it's the monthly average of.

Other sources checked and still NOT used, for the same reason as before:
API Ninjas and OilPriceAPI.com require an account signup to issue an API
key, and this agent does not create accounts on the user's behalf under
any circumstances. Nothing here requires either of them.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

import requests

FRED_SERIES_ID = "DCOILBRENTEU"
FRED_CSV_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_SERIES_ID}"


class ProxyFetchError(RuntimeError):
    """Raised when the live FRED feed can't be fetched or parsed."""


@dataclass(frozen=True)
class CrudeProxySeries:
    """A daily (trading_date -> USD/barrel Brent crude) series, sorted
    ascending by date. Deliberately dumb/immutable data holder so it's easy
    to build a synthetic one for tests without touching the network.

    Only trades on weekdays (no weekend/holiday rows) -- value_on() forward-
    fills those small gaps, which is now a genuinely minor approximation
    (a day or two, not a whole month, unlike the previous monthly series)."""

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


def _parse_fred_csv(text: str) -> list[tuple[date, float]]:
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header or len(header) < 2:
        raise ProxyFetchError(f"unexpected FRED CSV header: {header!r}")
    out: list[tuple[date, float]] = []
    for row in reader:
        if len(row) < 2:
            continue
        raw_date, raw_value = row[0], row[1]
        if raw_value in ("", "."):
            continue  # FRED's own "no observation" marker -- not an error
        try:
            d = datetime.strptime(raw_date, "%Y-%m-%d").date()
            v = float(raw_value)
        except ValueError:
            continue
        out.append((d, v))
    out.sort(key=lambda pair: pair[0])
    return out


def fetch_fred_brent_series(start: date | None = None, timeout: float = 30.0) -> CrudeProxySeries:
    """
    Fetch the live FRED "Crude Oil Prices: Brent - Europe" (DCOILBRENTEU)
    daily series. No API key required. Raises ProxyFetchError on any
    network/parse failure -- callers decide whether that's fatal or worth
    falling back on stale/cached data; this function does not silently
    swallow errors.
    """
    url = FRED_CSV_URL
    if start is not None:
        url += f"&cosd={start.isoformat()}"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "du-doan-gia-xang-bot/0.1"})
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ProxyFetchError(f"could not fetch FRED series {FRED_SERIES_ID}: {e}") from e
    rows = _parse_fred_csv(resp.text)
    if not rows:
        raise ProxyFetchError(f"FRED series {FRED_SERIES_ID} returned no usable observations")
    return CrudeProxySeries(daily=rows)
