"""
Phase 3: predict the NEXT (not-yet-announced) MOIT pricing cycle for every
retail product this project tracks.

Phases 1-2 only validated the structural formula against cycles MOIT had
ALREADY published. This script is the first thing in the project that
produces an actual forward-looking number: for each retail product, it
predicts the giá cơ sở for the currently-open (unpublished) cycle by
estimating the world-price average for that cycle's window from a live
crude-oil proxy (src/proxy/crude_proxy.py) run through a crude->product
bridge (src/pricing/bridge.py), then feeding that into the SAME
compute_gia_co_so() formula Phase 2 validated -- nothing about the formula
itself is reimplemented or changed here.

HOW THE WINDOW IS BUILT (see build_day_estimates()):
  - window = [last_known_cycle_date, cycle_end), business days only,
    following exactly the same "previous date included, current date
    excluded" rule validate_cycle_averages() established for HISTORICAL
    cycles (see src/parser/bulletin_parser.py / the README). cycle_end is
    ASSUMED to be the next Thursday on/after today -- clearly an
    assumption, since real cycles have not always landed exactly 7 days
    apart (holidays etc).
  - "known" days: any day in that window MOIT has already published a real
    world_price_daily quote for (possible if a new bulletin landed since
    this script's DB was last refreshed, but predicts nothing new the
    formula didn't already validate for that day). Zero uncertainty.
  - "nowcast" days: business days from the window start through TODAY that
    aren't already known -- estimated via the live crude proxy + bridge.
    Some real uncertainty (the bridge's fit residual).
  - "forecast" days: business days strictly after today through the
    assumed cycle end -- also estimated via the crude proxy + bridge, but
    since the proxy itself has no real future data, this reduces to a
    naive random walk (today's/latest known proxy value held flat). Wider
    uncertainty band than nowcast, on purpose.

TWO OTHER EXPLICIT, PERMANENT ASSUMPTIONS (not solvable in this phase, see
the Phase 3 report):
  - FX rate: held flat at the most recent real cycle's own [prev, this)
    VCB_SELL average (falling back to the single latest daily VCB_SELL
    quote on record if that's unavailable). There is no live daily FX feed
    in scope for this phase.
  - bog_net_vnd: defaulted to the PREVIOUS cycle's actual, real BOG action
    for that product (src.pricing.calibrate.get_bog_net_by_retail_product).
    The next cycle's real BOG decision is fundamentally unknowable ahead of
    time (it's a discretionary regulator action, not derivable from world
    prices) -- this was established as a permanent blind spot in the Phase
    2 design and is NOT something this phase attempts to solve. Every
    product's prediction carries this caveat, not just some of them.

Usage:
    python scripts/predict_next_cycle.py [--db data/db/moit.sqlite3] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.schema import init_db
from src.parser.products import RETAIL_PRODUCTS
from src.pricing.bridge import BRIDGE_WORLD_PRODUCTS, BridgeFit, fit_all_bridges
from src.pricing.calibrate import (
    collect_calibration_rows,
    fitted_residual_for_product,
    get_bog_net_by_retail_product,
    get_fx_rate_avg,
)
from src.pricing.constants import MissingConstantError, seed_constants
from src.pricing.formula import compute_gia_co_so
from src.proxy.crude_proxy import CrudeProxySeries, ProxyFetchError, fetch_yahoo_brent_series

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "moit.sqlite3"

# KEROSENE and RON95III are both excluded from prediction -- both are
# effectively discontinued (see their "note" in src/parser/products.py for
# what each is based on), and predicting a discontinued product's next
# "cycle" isn't meaningful. Confirmed as a REAL bug, not hypothetical:
# RON95III was still in this list until 2026-09-08, and the dashboard used
# it as the flagship/headline number -- meaning the most prominent price on
# the whole site was a phantom prediction for a product MOIT hasn't
# actually priced since 2026-05-28, being silently compared by readers
# against the real, currently-published E10RON95III number instead.
_DISCONTINUED_RETAIL_PRODUCTS = {"KEROSENE", "RON95III"}
PREDICTABLE_RETAIL_PRODUCTS = [code for code in RETAIL_PRODUCTS if code not in _DISCONTINUED_RETAIL_PRODUCTS]


def next_thursday_on_or_after(d: date) -> date:
    """The assumed next cycle-end date. If `d` is itself a Thursday, `d` is
    returned (the simplest reading of "next Thursday from today" when today
    IS Thursday) -- an assumption, stated plainly in the printed report."""
    days_ahead = (3 - d.weekday()) % 7  # Monday=0 ... Thursday=3
    return d + timedelta(days=days_ahead)


def business_days(start: date, end_exclusive: date) -> list[date]:
    """Mon-Fri days in [start, end_exclusive) -- same half-open convention
    as the project's established cycle-average window rule, restricted to
    weekdays because Singapore's market doesn't trade weekends (see
    README's "Blank weekend/holiday rows" note)."""
    out = []
    d = start
    while d < end_exclusive:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _parse_date(v) -> date:
    return v if isinstance(v, date) else datetime.strptime(v, "%Y-%m-%d").date()


def get_last_known_cycle_date(conn) -> tuple[date, int]:
    """The most recent this_cycle_date across cycle_summary (and its
    bulletin_id) -- the real boundary the not-yet-published next cycle's
    window starts from."""
    row = conn.execute(
        "SELECT this_cycle_date, bulletin_id FROM cycle_summary "
        "WHERE this_cycle_date IS NOT NULL ORDER BY this_cycle_date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("no cycle_summary rows on record -- nothing to predict a next cycle from")
    return _parse_date(row[0]), row[1]


def get_flat_fx_assumption(conn, last_bulletin_id: int) -> tuple[float, str]:
    """Flat FX assumption for the whole new-cycle window: the last real
    cycle's own [prev_cycle_date, this_cycle_date) VCB_SELL average, falling
    back to the single most recent daily VCB_SELL quote on record if that
    window has no data. Returns (rate, description-of-source)."""
    row = conn.execute(
        "SELECT prev_cycle_date, this_cycle_date FROM cycle_summary "
        "WHERE bulletin_id = ? AND prev_cycle_date IS NOT NULL AND this_cycle_date IS NOT NULL LIMIT 1",
        (last_bulletin_id,),
    ).fetchone()
    if row is not None:
        prev_d, this_d = _parse_date(row[0]), _parse_date(row[1])
        fx = get_fx_rate_avg(conn, prev_d, this_d)
        if fx is not None:
            return fx, f"last real cycle's own VCB_SELL average, window [{prev_d}, {this_d})"
    fallback = conn.execute(
        "SELECT price, quote_date FROM world_price_daily WHERE product_code = 'VCB_SELL' "
        "ORDER BY quote_date DESC LIMIT 1"
    ).fetchone()
    if fallback is not None:
        return fallback[0], f"latest single VCB_SELL quote on record ({fallback[1]})"
    raise RuntimeError("no VCB_SELL data on record at all -- cannot assume an FX rate")


@dataclass
class DayEstimate:
    day: date
    source: str  # "known" | "nowcast" | "forecast"
    world_price: float


def build_day_estimates(
    window_days: list[date],
    today: date,
    known_by_date: dict[date, float],
    crude_series: CrudeProxySeries,
    bridge: BridgeFit | None,
) -> list[DayEstimate]:
    out: list[DayEstimate] = []
    for d in window_days:
        if d in known_by_date:
            out.append(DayEstimate(d, "known", known_by_date[d]))
            continue
        if bridge is None:
            continue  # no way to estimate this day for this product
        crude_val = crude_series.value_on(d)
        if crude_val is None:
            continue
        product_val = bridge.predict(crude_val)
        source = "nowcast" if d <= today else "forecast"
        out.append(DayEstimate(d, source, product_val))
    return out


@dataclass
class ProductPrediction:
    retail_product_code: str
    world_product_code: str
    predicted_vnd: float
    low_vnd: float
    high_vnd: float
    known_days: int
    nowcast_days: int
    forecast_days: int
    window_days_total: int
    bog_net_vnd: float
    lumped_residual_vnd: float
    world_price_avg: float
    unit: str


@dataclass
class PredictionRun:
    today: date
    last_cycle_date: date
    cycle_end_assumed: date
    fx_rate: float
    fx_source: str
    predictions: list[ProductPrediction] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # products skipped + why


def predict_products(
    conn,
    as_of_today: date,
    crude_series: CrudeProxySeries,
    bridges: dict[str, BridgeFit],
) -> PredictionRun:
    """Core computation, isolated from argv/printing/network so it's
    testable with injected fixture crude_series + an in-memory DB and no
    live internet access."""
    last_cycle_date, last_bulletin_id = get_last_known_cycle_date(conn)
    cycle_end = next_thursday_on_or_after(as_of_today)
    if cycle_end <= last_cycle_date:
        # today is already past the assumed cycle end (stale DB / test edge
        # case) -- push the window out one more week so it's still a
        # non-empty, forward-looking window rather than raising.
        cycle_end = next_thursday_on_or_after(last_cycle_date + timedelta(days=1))

    window_days = business_days(last_cycle_date, cycle_end)
    fx_rate, fx_source = get_flat_fx_assumption(conn, last_bulletin_id)

    bog_net_by_product = get_bog_net_by_retail_product(conn, last_bulletin_id)
    usable_calibration, _excluded = collect_calibration_rows(conn)

    run = PredictionRun(
        today=as_of_today,
        last_cycle_date=last_cycle_date,
        cycle_end_assumed=cycle_end,
        fx_rate=fx_rate,
        fx_source=fx_source,
    )

    for retail_code in PREDICTABLE_RETAIL_PRODUCTS:
        info = RETAIL_PRODUCTS[retail_code]
        world_code = info["world_reference"]

        bridge = bridges.get(world_code)
        if bridge is None:
            run.notes.append(f"{retail_code}: skipped -- no usable crude->{world_code} bridge (insufficient history)")
            continue

        known_rows = conn.execute(
            "SELECT quote_date, price FROM world_price_daily WHERE product_code = ? "
            "AND quote_date >= ? AND quote_date < ?",
            (world_code, last_cycle_date.isoformat(), cycle_end.isoformat()),
        ).fetchall()
        known_by_date = {_parse_date(qd): price for qd, price in known_rows}

        day_estimates = build_day_estimates(window_days, as_of_today, known_by_date, crude_series, bridge)
        if not day_estimates:
            run.notes.append(f"{retail_code}: skipped -- could not estimate any day in the window")
            continue

        world_price_avg = sum(e.world_price for e in day_estimates) / len(day_estimates)
        known_days = sum(1 for e in day_estimates if e.source == "known")
        nowcast_days = sum(1 for e in day_estimates if e.source == "nowcast")
        forecast_days = sum(1 for e in day_estimates if e.source == "forecast")

        try:
            lumped_residual = fitted_residual_for_product(usable_calibration, retail_code)
        except ValueError:
            run.notes.append(f"{retail_code}: skipped -- no Phase 2 calibration data for this product")
            continue

        bog_net = bog_net_by_product.get(retail_code, 0.0)

        try:
            central = compute_gia_co_so(
                conn,
                world_price_avg_usd_per_unit=world_price_avg,
                fx_rate_vnd_per_usd=fx_rate,
                product_code=retail_code,
                as_of_date=cycle_end,
                lumped_residual_vnd=lumped_residual,
                bog_net_vnd=bog_net,
            )
        except MissingConstantError as e:
            run.notes.append(f"{retail_code}: skipped -- {e}")
            continue

        # Uncertainty band: the bridge's own historical fit residual (how
        # far actual daily product prices strayed from the fitted line),
        # widened for the forecast portion of the window since those days
        # have no real crude signal behind them at all (pure hold-flat).
        # This is a simple, honestly-labelled heuristic, not a rigorous
        # confidence interval -- see the module docstring / Phase 3 report.
        base_band = bridge.residual_std or 0.0
        total_days = len(day_estimates)
        forecast_fraction = forecast_days / total_days if total_days else 0.0
        band = base_band * (1.0 + 0.5 * forecast_fraction)

        low = compute_gia_co_so(
            conn,
            world_price_avg_usd_per_unit=world_price_avg - band,
            fx_rate_vnd_per_usd=fx_rate,
            product_code=retail_code,
            as_of_date=cycle_end,
            lumped_residual_vnd=lumped_residual,
            bog_net_vnd=bog_net,
        ).gia_co_so_vnd
        high = compute_gia_co_so(
            conn,
            world_price_avg_usd_per_unit=world_price_avg + band,
            fx_rate_vnd_per_usd=fx_rate,
            product_code=retail_code,
            as_of_date=cycle_end,
            lumped_residual_vnd=lumped_residual,
            bog_net_vnd=bog_net,
        ).gia_co_so_vnd

        run.predictions.append(
            ProductPrediction(
                retail_product_code=retail_code,
                world_product_code=world_code,
                predicted_vnd=central.gia_co_so_vnd,
                low_vnd=min(low, high),
                high_vnd=max(low, high),
                known_days=known_days,
                nowcast_days=nowcast_days,
                forecast_days=forecast_days,
                window_days_total=total_days,
                bog_net_vnd=bog_net,
                lumped_residual_vnd=lumped_residual,
                world_price_avg=world_price_avg,
                unit=central.retail_unit,
            )
        )

    return run


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--today", type=str, default=None, help="Override 'today' as YYYY-MM-DD (default: real current date)")
    args = ap.parse_args()

    conn = init_db(str(args.db))
    seed_constants(conn)

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    print(f"=== Phase 3: next-cycle prediction (run as of {today.isoformat()}) ===\n")

    print("--- Step 1: live crude-oil proxy ---")
    try:
        crude_series = fetch_yahoo_brent_series()
    except ProxyFetchError as e:
        print(f"FATAL: could not fetch the crude proxy (Yahoo Finance BZ=F): {e}")
        sys.exit(1)
    latest = crude_series.latest()
    print(f"Yahoo Finance Brent front-month futures (BZ=F): {len(crude_series.daily)} daily points fetched, "
          f"near-real-time, no API key.")
    if latest:
        print(f"  Latest observation: {latest[0].isoformat()} = {latest[1]:.2f} USD/bbl")
    print("  Near-real-time (today's live intraday price included, not a week-stale settlement print --")
    print("  see src/proxy/crude_proxy.py docstring for why FRED's own daily series was replaced).")

    print("--- Step 2: crude -> refined-product bridge fit (OLS on daily pairs) ---")
    bridges = fit_all_bridges(conn, crude_series)
    for code in BRIDGE_WORLD_PRODUCTS:
        b = bridges.get(code)
        if b is None:
            print(f"  {code:15s} -- no usable bridge (insufficient overlapping days)")
            continue
        r_str = f"{b.r:.3f}" if b.r is not None else "n/a"
        std_str = f"{b.residual_std:.2f}" if b.residual_std is not None else "n/a"
        print(
            f"  {code:15s} n={b.n:4d} days ({b.months[0]}..{b.months[-1]})  "
            f"price = {b.intercept:8.2f} + {b.slope:6.3f} * crude   r={r_str}  resid_std={std_str}"
        )
    print()

    print("--- Step 3: predicting the next (unpublished) cycle ---")
    run = predict_products(conn, today, crude_series, bridges)
    print(f"  Last known real cycle date : {run.last_cycle_date.isoformat()}")
    print(f"  Assumed next cycle end     : {run.cycle_end_assumed.isoformat()}  (assumption: next Thursday on/after today)")
    print(f"  FX rate assumed            : {run.fx_rate:,.1f} VND/USD  ({run.fx_source})")
    print()

    if not run.predictions:
        print("  No products could be predicted -- see notes below.")
    for p in run.predictions:
        pct_known = 100.0 * p.known_days / p.window_days_total if p.window_days_total else 0.0
        pct_nowcast = 100.0 * p.nowcast_days / p.window_days_total if p.window_days_total else 0.0
        pct_forecast = 100.0 * p.forecast_days / p.window_days_total if p.window_days_total else 0.0
        print(f"  {p.retail_product_code} (world ref: {p.world_product_code})")
        print(f"    predicted giá cơ sở : {p.predicted_vnd:,.0f} {p.unit}   range [{p.low_vnd:,.0f} .. {p.high_vnd:,.0f}]")
        print(
            f"    window composition  : {p.window_days_total} business days -- "
            f"known {p.known_days} ({pct_known:.0f}%), nowcast {p.nowcast_days} ({pct_nowcast:.0f}%), "
            f"forecast {p.forecast_days} ({pct_forecast:.0f}%)"
        )
        print(f"    world price avg used: {p.world_price_avg:.2f}   lumped_residual_vnd: {p.lumped_residual_vnd:,.1f}")
        print(
            f"    BOG caveat          : bog_net_vnd={p.bog_net_vnd:,.0f} assumed = PREVIOUS cycle's real value "
            f"(next cycle's actual BOG decision is fundamentally unknowable -- see module docstring)"
        )
        if p.retail_product_code in ("E5RON92", "E10RON95III"):
            print(
                "    KNOWN BIAS CAVEAT   : this product has underpredicted the real published price by "
                "~4-5% in EVERY one of the last 4 cycles checked (Jun-Aug 2026), not just noise -- see "
                "formula.py's module docstring. The real price is likely to land noticeably HIGHER than "
                "the number above; treat this prediction as a floor, not a best estimate, until that gap "
                "is understood (suspected: unmodeled domestic ethanol-blend cost)."
            )
        print()

    if run.notes:
        print("  Notes (products not predicted):")
        for n in run.notes:
            print(f"    - {n}")


if __name__ == "__main__":
    main()
