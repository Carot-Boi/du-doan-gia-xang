"""
Phase 3: the crude-oil -> Singapore refined-product "bridge".

The chosen crude proxy (src/proxy/crude_proxy.py -- Yahoo Finance's
near-real-time DAILY Brent futures print, see that module's docstring for
why Brent/daily/Yahoo) tracks CRUDE oil, not the refined-product quotes
(RON92, RON95, DIESEL_0_05S,
FO_180CST_3_5S) MOIT's own formula actually uses. Those move together but
not 1:1 -- refining margins ("crack spreads") widen and narrow with
refinery utilization, seasonal demand, etc. This module fits the simplest
honest bridge between them: product_price =~ a + b * crude_price, one
(a, b) pair per world product code, by plain ordinary least squares (no ML
dependency -- ~15 lines of arithmetic is enough for a straight line).

DAILY PAIRS, NOT MONTHLY AVERAGES (changed 2026-09-08): the crude proxy
used to be monthly (FRED/POILDUBUSDM), so pairing it against
world_price_daily's individual daily rows would have just repeated the
same crude value across every day in a month (pseudo-replication, not more
information) -- hence the old design averaged both sides to one point per
calendar month first. Now that the crude proxy is itself daily (and
near-real-time -- see crude_proxy.py), that workaround is gone: each
world_price_daily row is
paired directly against the crude proxy's value for that SAME calendar
date (via CrudeProxySeries.value_on(), which forward-fills the crude
series' own weekend/holiday gaps only -- a day or two, not a month). This
is real information, not repetition, and gives roughly 20-25x more fitted
points than the old monthly version (hundreds of daily rows vs ~14 monthly
ones) -- a materially better fit, not just a cosmetic change.

`months` on BridgeFit is kept as a human-readable summary (which calendar
months the underlying daily points fall in) for a transparent report, even
though the fit itself is no longer computed by averaging into those
months.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date

from src.proxy.crude_proxy import CrudeProxySeries

# The four world product codes this phase's bridge covers -- deliberately
# excludes KEROSENE (dropped as a state-priced product from 29/4/2026 and
# VCB_BUY/VCB_SELL (FX rates, not oil products; not a crude-tracking series).
BRIDGE_WORLD_PRODUCTS = ["RON92", "RON95", "DIESEL_0_05S", "FO_180CST_3_5S"]


class BridgeFitError(ValueError):
    """Raised when there isn't enough overlapping data to fit a bridge."""


@dataclass(frozen=True)
class BridgeFit:
    world_product_code: str
    n: int  # number of overlapping (crude, product) DAILY points used
    intercept: float  # a
    slope: float  # b
    r: float | None  # Pearson correlation, None if undefined (n<2 or zero variance)
    residual_std: float | None  # population stdev of (actual - predicted), product's own unit; None if n<2
    months: list[str]  # distinct 'YYYY-MM' months the fitted daily points fall in, for a transparent report

    def predict(self, crude_price: float) -> float:
        return self.intercept + self.slope * crude_price


def fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """
    Plain ordinary least squares for y = a + b*x. Returns (intercept, slope).
    No numpy/sklearn -- this is a single straight-line fit, not worth a
    heavy dependency for.
    """
    n = len(xs)
    if n != len(ys):
        raise ValueError(f"xs and ys must be the same length (got {n} vs {len(ys)})")
    if n < 2:
        raise BridgeFitError(f"need at least 2 points to fit a line, got {n}")
    xbar = sum(xs) / n
    ybar = sum(ys) / n
    var_x = sum((x - xbar) ** 2 for x in xs)
    if var_x == 0:
        raise BridgeFitError("x has zero variance across all points; cannot fit a slope")
    cov_xy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    b = cov_xy / var_x
    a = ybar - b * xbar
    return a, b


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    xbar, ybar = sum(xs) / n, sum(ys) / n
    var_x = sum((x - xbar) ** 2 for x in xs)
    var_y = sum((y - ybar) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    cov_xy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    return cov_xy / ((var_x**0.5) * (var_y**0.5))


def daily_pairs_by_product(
    conn: sqlite3.Connection, world_product_code: str, crude_series: CrudeProxySeries
) -> tuple[list[str], list[float], list[float]]:
    """
    For every world_price_daily row of `world_product_code`, look up the
    crude proxy's forward-filled value for that SAME date. Returns
    (dates, crude_values, product_values), all three the same length and
    in the same order -- a row is skipped only if the crude series has no
    value at all for that date (i.e. entirely before the series starts).

    Every row on record is used, including ones flagged
    conflicts_with_prior -- they're still real observed quotes for that
    date; adjudicating which one is "right" isn't this module's job.
    """
    cur = conn.execute(
        "SELECT quote_date, price FROM world_price_daily WHERE product_code = ? ORDER BY quote_date",
        (world_product_code,),
    )
    dates: list[str] = []
    crude_values: list[float] = []
    product_values: list[float] = []
    for quote_date, price in cur.fetchall():
        d = date.fromisoformat(quote_date)
        crude_val = crude_series.value_on(d)
        if crude_val is None:
            continue
        dates.append(quote_date)
        crude_values.append(crude_val)
        product_values.append(price)
    return dates, crude_values, product_values


def fit_bridge_for_product(
    conn: sqlite3.Connection, world_product_code: str, crude_series: CrudeProxySeries
) -> BridgeFit:
    """Fit one product's crude -> product-price bridge from every
    world_price_daily row that has a matching crude-proxy value. Raises
    BridgeFitError if fewer than 2 usable points exist."""
    dates, xs, ys = daily_pairs_by_product(conn, world_product_code, crude_series)
    if len(dates) < 2:
        raise BridgeFitError(
            f"{world_product_code}: only {len(dates)} day(s) with both a world-price quote and a crude-proxy "
            f"value -- need at least 2 to fit a line"
        )

    a, b = fit_linear(xs, ys)
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    residual_std = statistics.pstdev(resid) if len(resid) > 1 else None
    r = _pearson_r(xs, ys)
    months = sorted({d[:7] for d in dates})

    return BridgeFit(
        world_product_code=world_product_code,
        n=len(dates),
        intercept=a,
        slope=b,
        r=r,
        residual_std=residual_std,
        months=months,
    )


def fit_all_bridges(
    conn: sqlite3.Connection, crude_series: CrudeProxySeries, world_products: list[str] = BRIDGE_WORLD_PRODUCTS
) -> dict[str, BridgeFit]:
    """Fit bridges for every product in `world_products`, skipping (not
    raising for) any product with too little overlap -- a caller can decide
    whether a missing bridge is fatal for what it's trying to predict."""
    out: dict[str, BridgeFit] = {}
    for code in world_products:
        try:
            out[code] = fit_bridge_for_product(conn, code, crude_series)
        except BridgeFitError:
            continue
    return out
