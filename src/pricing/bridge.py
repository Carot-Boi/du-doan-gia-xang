"""
Phase 3: the crude-oil -> Singapore refined-product "bridge".

The chosen crude proxy (src/proxy/crude_proxy.py -- FRED's monthly Dubai
crude print, see that module's docstring for why) tracks CRUDE oil, not the
refined-product quotes (RON92, RON95, DIESEL_0_05S, FO_180CST_3_5S) MOIT's
own formula actually uses. Those move together but not 1:1 -- refining
margins ("crack spreads") widen and narrow with refinery utilization,
seasonal demand, etc. This module fits the simplest honest bridge between
them: product_price =~ a + b * crude_price, one (a, b) pair per world
product code, by plain ordinary least squares (no ML dependency -- ~15 lines
of arithmetic is enough for a straight line).

WHY MONTHLY AVERAGES, NOT DAILY ROWS: the crude proxy is monthly
(FRED/POILDUBUSDM), so pairing it against world_price_daily's individual
daily rows would just repeat the same crude value across every day in a
month (spurious inflation of the sample size -- pseudo-replication, not
more information). Instead this module averages world_price_daily to ONE
number per calendar month and regresses THAT against the matching FRED
month. Honest sample size for the fit is therefore "how many months
overlap between world_price_daily's history and FRED's", not "how many
daily rows exist" -- see fit_product_bridges()'s docstring for what that
number actually was when this was run.

BE HONEST ABOUT THINNESS: Phase 1's clean daily data only goes back to
2025-06-26 (~9 recent bulletins' worth of daily tables were parseable) and
FRED only overlaps within that same span, so n is on the order of a dozen
monthly points per product -- thin. r and the residual spread are reported
alongside the fit so a caller (predict_next_cycle.py) can size its
uncertainty band honestly instead of pretending this is precise.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass

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
    n: int  # number of overlapping (crude, product) monthly points used
    intercept: float  # a
    slope: float  # b
    r: float | None  # Pearson correlation, None if undefined (n<2 or zero variance)
    residual_std: float | None  # population stdev of (actual - predicted), product's own unit; None if n<2
    months: list[str]  # which 'YYYY-MM' months were actually used, for a transparent report

    def predict(self, crude_price: float) -> float:
        return self.intercept + self.slope * crude_price


def fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """
    Plain ordinary least squares for y = a + b*x. Returns (intercept, slope).
    No numpy/sklearn -- this is a single straight-line fit on a handful of
    points, not worth a heavy dependency for.
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


def monthly_avg_by_product(conn: sqlite3.Connection, world_product_code: str) -> dict[str, float]:
    """Average world_price_daily's price for one product, grouped by
    calendar month ('YYYY-MM'). Uses every row on record for that product --
    the append-only conflicts_with_prior rows are included deliberately
    (they're still real observed quotes for that date; excluding them isn't
    this phase's job to adjudicate)."""
    cur = conn.execute(
        "SELECT quote_date, price FROM world_price_daily WHERE product_code = ? ORDER BY quote_date",
        (world_product_code,),
    )
    by_month: dict[str, list[float]] = defaultdict(list)
    for quote_date, price in cur.fetchall():
        by_month[quote_date[:7]].append(price)
    return {ym: statistics.mean(prices) for ym, prices in by_month.items()}


def fit_bridge_for_product(
    conn: sqlite3.Connection, world_product_code: str, crude_series: CrudeProxySeries
) -> BridgeFit:
    """Fit one product's crude -> product-price bridge from whatever
    overlapping months exist between world_price_daily and the crude proxy.
    Raises BridgeFitError if fewer than 2 overlapping months exist."""
    product_monthly = monthly_avg_by_product(conn, world_product_code)
    crude_monthly = crude_series.as_dict()

    months = sorted(m for m in product_monthly if m in crude_monthly)
    if len(months) < 2:
        raise BridgeFitError(
            f"{world_product_code}: only {len(months)} overlapping month(s) between "
            f"world_price_daily and the crude proxy series -- need at least 2 to fit a line"
        )

    xs = [crude_monthly[m] for m in months]
    ys = [product_monthly[m] for m in months]
    a, b = fit_linear(xs, ys)
    resid = [y - (a + b * x) for x, y in zip(xs, ys)]
    residual_std = statistics.pstdev(resid) if len(resid) > 1 else None
    r = _pearson_r(xs, ys)

    return BridgeFit(
        world_product_code=world_product_code,
        n=len(months),
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
