"""
Phase 2: fit the lumped_residual_vnd term (see formula.py) from real,
already-published (world price average, retail ceiling price) pairs in the
DB, and report how much it varies -- the central validation question for
this phase.

For each usable (bulletin, product) pair:

    lumped_residual = actual_published_retail_price
                       - gia_co_so(world_avg, fx, product, date, lumped_residual_vnd=0)

i.e. call the formula with the residual zeroed out (so it returns pure
CIF + tax), then whatever gap remains between that and the real published
retail ceiling IS the implied residual -- the combined chi phí kinh doanh
định mức + chi phí đưa về cảng + lợi nhuận định mức + BOG net effect that
this phase deliberately doesn't try to model as separate figures.

Data sourcing notes (why this isn't a single SQL join):

- The world-price AVERAGE is read straight from cycle_summary.avg_price_published
  -- the brief is explicit that the [prev_cycle_date, this_cycle_date) window
  rule is already implemented and validated in
  src/parser/bulletin_parser.py's validate_cycle_averages(), so this module
  reuses that already-computed figure rather than re-deriving the window.

- cycle_summary's product_code is a WORLD code (RON92/RON95/DIESEL_0_05S/
  FO_180CST_3_5S); retail_prices' product_code is a RETAIL code (E5RON92/
  RON95III/E10RON95III/...). They're joined via
  src.parser.products.RETAIL_PRODUCTS[*]['world_reference'].

- The FX rate is the one thing NOT available from cycle_summary (MOIT's
  prose doesn't publish a cycle-average FX rate) -- but the archived raw
  HTML across the three fully-clean bulletins (25/6, 2/7, 9/7/2026) happens
  to cover world_price_daily back to 2025-06-26, so VCB_SELL quotes for
  every bulletin's [prev_cycle_date, this_cycle_date) window are on hand in
  the DB regardless of which bulletin's own page originally supplied them.
  get_fx_rate_avg() below applies the exact same window rule to VCB_SELL
  that validate_cycle_averages() already validated for world prices -- this
  is the one piece of window logic this module does have to run itself,
  since cycle_summary simply has no FX column to reuse.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime

from src.parser.products import BOG_TO_RETAIL_PRODUCTS, RETAIL_PRODUCTS
from src.pricing.constants import MissingConstantError
from src.pricing.formula import compute_gia_co_so

# Which VCB rate to use for converting a USD cost into VND. Fuel importers
# BUY foreign currency from banks to pay overseas suppliers, so the bank's
# SELL rate (what the importer pays per USD) is the economically correct
# side -- not the BUY rate (what the bank pays the importer, irrelevant
# here). This is an assumption, not something MOIT's bulletins state
# explicitly; flagged for correction if evidence says otherwise.
FX_RATE_CODE = "VCB_SELL"


def _parse_date(v) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    return datetime.strptime(v, "%Y-%m-%d").date()


def get_fx_rate_avg(conn: sqlite3.Connection, prev_date: date, this_date: date) -> float | None:
    """
    Average VCB_SELL over [prev_date, this_date) -- the same "previous
    cycle's date included, current cycle's date excluded" window rule
    validate_cycle_averages() already established for world prices, applied
    here to FX because cycle_summary has no FX column of its own to reuse.
    Returns None if no quotes are on record in that window.
    """
    cur = conn.execute(
        """SELECT price FROM world_price_daily
           WHERE product_code = ? AND quote_date >= ? AND quote_date < ?""",
        (FX_RATE_CODE, prev_date.isoformat(), this_date.isoformat()),
    )
    prices = [r[0] for r in cur.fetchall()]
    if not prices:
        return None
    return sum(prices) / len(prices)


def get_bog_net_by_retail_product(conn: sqlite3.Connection, bulletin_id: int) -> dict[str, float]:
    """
    Real, per-cycle BOG net effect (trich_lap - chi_su_dung, VND/lit or
    VND/kg) for one bulletin, keyed by RETAIL product code (not the raw
    BOG-section code) via BOG_TO_RETAIL_PRODUCTS. "Xăng sinh học" fans out to
    BOTH E5RON92 and E10RON95-III with the same figure -- see that mapping's
    docstring for why. Returns {} (not an error) if this bulletin's BOG
    section wasn't parsed -- an absent row means "treat as 0", not "missing
    data to exclude on", since a genuinely-zero BOG cycle is common and looks
    identical to an unparsed one from this table alone.
    """
    cur = conn.execute(
        "SELECT product_code, trich_lap_vnd, chi_su_dung_vnd FROM bog_actions WHERE bulletin_id = ?",
        (bulletin_id,),
    )
    out: dict[str, float] = {}
    for bog_code, trich_lap, chi_su_dung in cur.fetchall():
        net = (trich_lap or 0) - (chi_su_dung or 0)
        for retail_code in BOG_TO_RETAIL_PRODUCTS.get(bog_code, []):
            out[retail_code] = net
    return out


@dataclass
class CalibrationRow:
    bulletin_id: int
    bulletin_date: date
    world_product_code: str
    retail_product_code: str
    prev_cycle_date: date | None
    this_cycle_date: date | None
    world_price_avg: float
    fx_rate: float
    published_retail_price: float
    bog_net_vnd: float  # real, known BOG effect for this bulletin+product -- NOT fitted
    baseline_gia_co_so: float  # formula with lumped_residual_vnd = 0, bog_net_vnd applied
    implied_lumped_residual: float  # what's left to explain AFTER real BOG is accounted for


@dataclass
class ExcludedRow:
    bulletin_id: int
    bulletin_date: date
    world_product_code: str
    retail_product_code: str
    reason: str


def collect_calibration_rows(conn: sqlite3.Connection) -> tuple[list[CalibrationRow], list[ExcludedRow]]:
    """
    Walk every cycle_summary row that has a matching retail_prices row (via
    world_reference) and a usable FX window, run the formula with
    lumped_residual_vnd=0, and back out the implied residual.

    Returns (usable_rows, excluded_rows) -- excluded rows are NOT silently
    dropped; each carries a reason (missing FX data, this_cycle_date before
    a required constant's effective_from, etc.) so the backtest report can
    say plainly what was left out and why, per the Phase 2 brief's
    insistence on being honest about what the calibration set actually was.
    """
    world_to_retail: dict[str, list[str]] = {}
    for code, info in RETAIL_PRODUCTS.items():
        world_to_retail.setdefault(info["world_reference"], []).append(code)

    cur = conn.execute(
        """SELECT cs.bulletin_id, b.bulletin_date, cs.product_code AS world_code,
                  cs.prev_cycle_date, cs.this_cycle_date, cs.avg_price_published
           FROM cycle_summary cs
           JOIN bulletins b ON b.id = cs.bulletin_id
           ORDER BY b.bulletin_date, cs.product_code"""
    )
    cs_rows = cur.fetchall()

    cur = conn.execute("SELECT bulletin_id, product_code, price_vnd FROM retail_prices")
    rp_by_bid: dict[int, dict[str, float]] = {}
    for bid, pcode, price_vnd in cur.fetchall():
        rp_by_bid.setdefault(bid, {})[pcode] = price_vnd

    usable: list[CalibrationRow] = []
    excluded: list[ExcludedRow] = []

    for row in cs_rows:
        bid, bdate_raw, world_code, prev_raw, this_raw, avg_pub = row
        bdate = _parse_date(bdate_raw)
        prev_date, this_date = _parse_date(prev_raw), _parse_date(this_raw)
        retail_codes = world_to_retail.get(world_code, [])
        bog_net_by_product = get_bog_net_by_retail_product(conn, bid)

        for retail_code in retail_codes:
            published = rp_by_bid.get(bid, {}).get(retail_code)
            if published is None or avg_pub is None:
                continue  # not an error -- this bulletin just didn't yield both halves

            if prev_date is None or this_date is None or prev_date >= this_date:
                excluded.append(
                    ExcludedRow(bid, bdate, world_code, retail_code, "prev_cycle_date/this_cycle_date missing or out of order")
                )
                continue

            fx = get_fx_rate_avg(conn, prev_date, this_date)
            if fx is None:
                excluded.append(
                    ExcludedRow(bid, bdate, world_code, retail_code, "no VCB_SELL daily quotes in this bulletin's cycle window")
                )
                continue

            as_of = this_date  # constants are looked up as of the NEW cycle's effective date
            bog_net = bog_net_by_product.get(retail_code, 0.0)
            try:
                baseline = compute_gia_co_so(
                    conn,
                    world_price_avg_usd_per_unit=avg_pub,
                    fx_rate_vnd_per_usd=fx,
                    product_code=retail_code,
                    as_of_date=as_of,
                    lumped_residual_vnd=0.0,
                    bog_net_vnd=bog_net,
                )
            except MissingConstantError as e:
                excluded.append(ExcludedRow(bid, bdate, world_code, retail_code, f"missing constant: {e}"))
                continue

            usable.append(
                CalibrationRow(
                    bulletin_id=bid,
                    bulletin_date=bdate,
                    world_product_code=world_code,
                    retail_product_code=retail_code,
                    prev_cycle_date=prev_date,
                    this_cycle_date=this_date,
                    world_price_avg=avg_pub,
                    fx_rate=fx,
                    published_retail_price=published,
                    bog_net_vnd=bog_net,
                    baseline_gia_co_so=baseline.gia_co_so_vnd,
                    implied_lumped_residual=published - baseline.gia_co_so_vnd,
                )
            )

    return usable, excluded


@dataclass
class ResidualStats:
    product_code: str
    n: int
    mean: float
    stdev: float | None
    minimum: float
    maximum: float
    spread: float  # max - min


def summarize_residuals(rows: list[CalibrationRow]) -> list[ResidualStats]:
    """Per-product summary of the implied lumped residual -- the key
    validation output: a tight cluster supports "Model A + residual"; a
    wide spread means the lumped term is hiding real variation (most likely
    BOG swings, since trích lập/chi sử dụng changes every cycle) that a
    later phase should model explicitly instead of lumping."""
    by_product: dict[str, list[float]] = {}
    for r in rows:
        by_product.setdefault(r.retail_product_code, []).append(r.implied_lumped_residual)

    out = []
    for product_code, values in sorted(by_product.items()):
        out.append(
            ResidualStats(
                product_code=product_code,
                n=len(values),
                mean=statistics.mean(values),
                stdev=statistics.stdev(values) if len(values) > 1 else None,
                minimum=min(values),
                maximum=max(values),
                spread=max(values) - min(values),
            )
        )
    return out


def fitted_residual_for_product(rows: list[CalibrationRow], product_code: str) -> float:
    """The single fitted lumped_residual_vnd to use for a product going
    forward: the mean of its implied residuals across the calibration set."""
    values = [r.implied_lumped_residual for r in rows if r.retail_product_code == product_code]
    if not values:
        raise ValueError(f"no calibration rows for product {product_code!r}")
    return statistics.mean(values)
