"""
Phase 3: a live crude-oil-price proxy for the world-price days MOIT hasn't
published a bulletin for yet.

RESEARCH RESULT (tested for real this phase, not just read about -- see
scripts/predict_next_cycle.py's printed report and the Phase 3 writeup for
the live numbers):

- **FRED (St. Louis Fed), series `POILDUBUSDM`** ("Global price of Dubai
  Crude"): genuinely free, no signup, no API key at all. Verified live:
      curl https://fred.stlouisfed.org/graph/fredgraph.csv?id=POILDUBUSDM
  returns HTTP 200 with real CSV data (confirmed during this phase, latest
  row was 2026-07). Dubai crude is also the economically *right* benchmark
  here -- closer to what feeds Singapore refined-product prices than
  WTI/Brent would be. The one real limitation: it's **monthly**, not daily.

- **API Ninjas** (api-ninjas.com/api/oilprice) and **OilPriceAPI.com**: both
  confirmed (by reading their own docs) to offer a free, no-credit-card
  tier with daily-or-better granularity. NOT integrated here, on purpose:
  both require creating an account (email signup) to issue an API key, and
  this agent does not create accounts on the user's behalf under any
  circumstances, including when explicitly asked to -- that's a hard rule,
  not a judgment call made for this project. If the project owner wants
  either of these, they need to sign up themselves and drop the resulting
  key into `.env` (see `.env.example` at the repo root for the expected
  variable names) -- nothing in this module requires that to work, so nothing
  breaks if it's never done.

- **dev.mem.gov.om** (Oman): confirmed dead in an earlier research pass
  (expired SSL certificate, both http and https). Not retried here.

DECISION: no genuinely free, no-signup, DAILY option actually exists right
now. Rather than force a bad choice, this project uses FRED's monthly Dubai
print as a coarse anchor, held flat (forward-filled) across the days within
that month -- i.e. explicitly a step function, not a real daily signal.
Every place that consumes this (src/pricing/bridge.py,
scripts/predict_next_cycle.py) is labelled with the resulting wider
uncertainty rather than presenting a falsely-precise daily number.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime

import requests

FRED_SERIES_ID = "POILDUBUSDM"
FRED_CSV_URL = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={FRED_SERIES_ID}"


class ProxyFetchError(RuntimeError):
    """Raised when the live FRED feed can't be fetched or parsed."""


@dataclass(frozen=True)
class CrudeProxySeries:
    """A monthly (month_start_date -> USD/barrel Dubai crude) series, sorted
    ascending by date. Deliberately dumb/immutable data holder so it's easy
    to build a synthetic one for tests without touching the network."""

    monthly: list[tuple[date, float]]  # [(YYYY-MM-01, value), ...] ascending

    def as_dict(self) -> dict[str, float]:
        """Keyed by 'YYYY-MM' string, for joining against other monthly data."""
        return {d.strftime("%Y-%m"): v for d, v in self.monthly}

    def latest(self) -> tuple[date, float] | None:
        return self.monthly[-1] if self.monthly else None

    def value_on(self, d: date) -> float | None:
        """
        The proxy's value for calendar day `d`, using hold-flat (forward
        fill) from the most recent month whose start is <= d. If `d` is
        before every month we have, falls back to the EARLIEST known value
        (backward fill) rather than returning None, since a caller building
        a continuous daily series generally still wants *something* rather
        than a hole. Returns None only if the series is empty.

        This is the one place the "monthly, not daily" limitation actually
        bites: every day within a given month gets the identical value, a
        step function rather than real daily movement. Callers (bridge.py,
        predict_next_cycle.py) must not present the result as more granular
        than it is.
        """
        if not self.monthly:
            return None
        best: float | None = None
        for month_start, value in self.monthly:
            if month_start <= d:
                best = value
            else:
                break
        if best is not None:
            return best
        return self.monthly[0][1]  # d is before all known months -> backward fill


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


def fetch_fred_dubai_series(start: date | None = None, timeout: float = 30.0) -> CrudeProxySeries:
    """
    Fetch the live FRED "Global price of Dubai Crude" (POILDUBUSDM) series.
    No API key required. Raises ProxyFetchError on any network/parse
    failure -- callers decide whether that's fatal or worth falling back on
    stale/cached data; this function does not silently swallow errors.
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
    return CrudeProxySeries(monthly=rows)
