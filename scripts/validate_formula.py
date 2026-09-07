"""
Phase 2 backtest/report: run the giá cơ sở formula (src/pricing/formula.py)
across every usable historical (bulletin, product) pair in the DB, using a
lumped_residual_vnd fitted per-product from the SAME calibration set (see
src/pricing/calibrate.py), and report how well it reproduces MOIT's own
published retail ceiling prices.

This is deliberately an in-sample backtest (the fitted residual is derived
from the same 34 rows it's then scored against) -- with only 9 usable
bulletins in the DB right now (see the "usable" count printed below), a
train/test split would leave too little of either to mean much. The point
of this script is to answer "does the structural formula, with ONE fitted
number per product, get anywhere close to reality" -- not to claim
predictive accuracy on unseen future cycles.

Usage:
    python scripts/validate_formula.py [--db data/db/moit.sqlite3]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.schema import init_db
from src.pricing.calibrate import collect_calibration_rows, fitted_residual_for_product, summarize_residuals
from src.pricing.constants import seed_constants
from src.pricing.formula import compute_gia_co_so

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "moit.sqlite3"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = ap.parse_args()

    conn = init_db(str(args.db))
    seed_constants(conn)

    usable, excluded = collect_calibration_rows(conn)

    print("=== Calibration set ===")
    print(f"Usable (bulletin, product) pairs: {len(usable)}")
    bulletins_covered = sorted({r.bulletin_date for r in usable})
    print(f"Bulletins covered: {len(bulletins_covered)}  ({bulletins_covered[0]} .. {bulletins_covered[-1]})")
    print(f"Excluded pairs: {len(excluded)}")
    for e in excluded:
        print(f"  - {e.bulletin_date} {e.retail_product_code} (from world {e.world_product_code}): {e.reason}")

    print("\n=== Fitted lumped_residual_vnd per product (mean of implied residuals) ===")
    stats = summarize_residuals(usable)
    fitted: dict[str, float] = {}
    for s in stats:
        fitted[s.product_code] = fitted_residual_for_product(usable, s.product_code)
        stdev_str = f"{s.stdev:.1f}" if s.stdev is not None else "n/a (n=1)"
        rel_spread_pct = (s.spread / s.mean * 100) if s.mean else float("nan")
        print(
            f"  {s.product_code:15s} n={s.n:2d}  mean={s.mean:8.1f}  stdev={stdev_str:>8s}  "
            f"min={s.minimum:8.1f}  max={s.maximum:8.1f}  spread={s.spread:7.1f} ({rel_spread_pct:5.1f}% of mean)"
        )

    print("\n=== Backtest: formula-predicted vs MOIT-published retail price ===")
    header = f"{'date':10s} {'product':13s} {'published':>10s} {'predicted':>10s} {'error':>8s} {'error%':>7s}"
    print(header)
    print("-" * len(header))

    abs_errors = []
    pct_errors = []
    worst = []
    for r in usable:
        residual = fitted[r.retail_product_code]
        pred = compute_gia_co_so(
            conn,
            world_price_avg_usd_per_unit=r.world_price_avg,
            fx_rate_vnd_per_usd=r.fx_rate,
            product_code=r.retail_product_code,
            as_of_date=r.this_cycle_date,
            lumped_residual_vnd=residual,
            bog_net_vnd=r.bog_net_vnd,
        ).gia_co_so_vnd
        error = pred - r.published_retail_price
        pct = error / r.published_retail_price * 100
        abs_errors.append(abs(error))
        pct_errors.append(abs(pct))
        worst.append((abs(pct), r.bulletin_date, r.retail_product_code, r.published_retail_price, pred, error, pct))
        print(
            f"{str(r.bulletin_date):10s} {r.retail_product_code:13s} {r.published_retail_price:10.0f} "
            f"{pred:10.1f} {error:8.1f} {pct:6.2f}%"
        )

    print("\n=== Summary ===")
    mae = sum(abs_errors) / len(abs_errors)
    mape = sum(pct_errors) / len(pct_errors)
    print(f"Mean absolute error: {mae:.1f} VND/lit(or kg)")
    print(f"Mean absolute percentage error: {mape:.2f}%")

    worst.sort(reverse=True)
    print("\nWorst 5 by |error%|:")
    for abs_pct, d, prod, pub, pred, err, pct in worst[:5]:
        print(f"  {d} {prod:13s} published={pub:8.0f} predicted={pred:8.1f} error={err:7.1f} ({pct:+.2f}%)")


if __name__ == "__main__":
    main()
