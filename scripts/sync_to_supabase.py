"""
Phase 4: push the local SQLite DB (the pipeline's real source of truth,
unchanged since Phase 1) to the public Supabase Postgres project that
backs docs/index.html (the static dashboard) and the weekly email alert.

Two very different sync strategies for two very different kinds of data,
matching the append-only vs replace-on-reparse distinction already
established in src/db/store.py's docstrings:

  - bulletins / world_price_daily / cycle_summary / retail_prices /
    bog_actions: MIRRORED. This project's whole dataset is small (a few
    thousand rows total), so instead of diffing, every run simply wipes
    and reloads these 5 tables in Postgres from the current SQLite
    contents. Simple, always correct, cheap at this scale -- no
    upsert/conflict logic to get subtly wrong.

  - prediction_runs / predictions: APPENDED, never wiped. Each run is a
    forward-looking snapshot of "what did we predict, as of when" -- see
    supabase/schema.sql's comment. Deleting old ones would throw away the
    dashboard's ability to show how a prediction evolved as the cycle
    got closer to being real.

Requires SUPABASE_DB_URL (the Postgres "Connection string" from Supabase
project settings, with the password filled in) as an environment variable
-- see .env.example. Never logs or prints this value.

Usage:
    python scripts/sync_to_supabase.py [--db data/db/moit.sqlite3] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg2
import psycopg2.extras

from src.db.schema import init_db
from src.parser.products import RETAIL_PRODUCTS
from src.pricing.bridge import fit_all_bridges
from src.pricing.constants import seed_constants
from src.proxy.crude_proxy import ProxyFetchError, fetch_fred_brent_series
from scripts.predict_next_cycle import predict_products

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "moit.sqlite3"

# Products known (per formula.py's module docstring) to systematically
# underpredict -- mirrored into the DB so the dashboard can render the same
# caveat predict_next_cycle.py prints to the console, instead of the
# warning only existing as a CLI-only side effect.
KNOWN_BIAS_PRODUCTS = {"E5RON92", "E10RON95III"}

MIRRORED_TABLES_IN_DELETE_ORDER = [
    "bog_actions",
    "retail_prices",
    "cycle_summary",
    "world_price_daily",
    "bulletins",
]


def _rows(sqlite_conn, table: str) -> list[dict]:
    cur = sqlite_conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def mirror_bulletins(pg_cur, sqlite_conn) -> int:
    rows = _rows(sqlite_conn, "bulletins")
    psycopg2.extras.execute_values(
        pg_cur,
        """INSERT INTO bulletins
            (id, url, bulletin_date, effective_at, fetch_timestamp, http_status,
             title, title_variant, parse_status, parse_warnings, discovery_method)
           VALUES %s""",
        [
            (
                r["id"], r["url"], r["bulletin_date"], r["effective_at"], r["fetch_timestamp"],
                r["http_status"], r["title"], r["title_variant"], r["parse_status"],
                r["parse_warnings"], r["discovery_method"],
            )
            for r in rows
        ],
    )
    return len(rows)


def mirror_world_price_daily(pg_cur, sqlite_conn) -> int:
    rows = _rows(sqlite_conn, "world_price_daily")
    if not rows:
        return 0
    psycopg2.extras.execute_values(
        pg_cur,
        """INSERT INTO world_price_daily
            (product_code, quote_date, price, unit, source_bulletin_id, conflicts_with_prior)
           VALUES %s""",
        [
            (r["product_code"], r["quote_date"], r["price"], r["unit"], r["source_bulletin_id"],
             bool(r["conflicts_with_prior"]))
            for r in rows
        ],
    )
    return len(rows)


def mirror_cycle_summary(pg_cur, sqlite_conn) -> int:
    rows = _rows(sqlite_conn, "cycle_summary")
    if not rows:
        return 0
    psycopg2.extras.execute_values(
        pg_cur,
        """INSERT INTO cycle_summary
            (bulletin_id, product_code, prev_cycle_date, this_cycle_date, avg_price_published,
             delta_published, pct_change_published, computed_avg, validation_ok, validation_note)
           VALUES %s""",
        [
            (
                r["bulletin_id"], r["product_code"], r["prev_cycle_date"], r["this_cycle_date"],
                r["avg_price_published"], r["delta_published"], r["pct_change_published"],
                r["computed_avg"], None if r["validation_ok"] is None else bool(r["validation_ok"]),
                r["validation_note"],
            )
            for r in rows
        ],
    )
    return len(rows)


def mirror_retail_prices(pg_cur, sqlite_conn) -> int:
    rows = _rows(sqlite_conn, "retail_prices")
    if not rows:
        return 0
    psycopg2.extras.execute_values(
        pg_cur,
        """INSERT INTO retail_prices (bulletin_id, product_code, price_vnd, delta_vnd, unit, effective_at)
           VALUES %s""",
        [(r["bulletin_id"], r["product_code"], r["price_vnd"], r["delta_vnd"], r["unit"], r["effective_at"])
         for r in rows],
    )
    return len(rows)


def mirror_bog_actions(pg_cur, sqlite_conn) -> int:
    rows = _rows(sqlite_conn, "bog_actions")
    if not rows:
        return 0
    psycopg2.extras.execute_values(
        pg_cur,
        """INSERT INTO bog_actions (bulletin_id, product_code, trich_lap_vnd, chi_su_dung_vnd, unit)
           VALUES %s""",
        [(r["bulletin_id"], r["product_code"], r["trich_lap_vnd"], r["chi_su_dung_vnd"], r["unit"]) for r in rows],
    )
    return len(rows)


def mirror_all(pg_conn, sqlite_conn) -> dict:
    counts = {}
    with pg_conn.cursor() as cur:
        for table in MIRRORED_TABLES_IN_DELETE_ORDER:
            cur.execute(f"DELETE FROM {table}")
        counts["bulletins"] = mirror_bulletins(cur, sqlite_conn)
        counts["world_price_daily"] = mirror_world_price_daily(cur, sqlite_conn)
        counts["cycle_summary"] = mirror_cycle_summary(cur, sqlite_conn)
        counts["retail_prices"] = mirror_retail_prices(cur, sqlite_conn)
        counts["bog_actions"] = mirror_bog_actions(cur, sqlite_conn)
    pg_conn.commit()
    return counts


def append_prediction_run(pg_conn, sqlite_conn, today: date) -> int | None:
    """Runs the SAME predict_products() logic predict_next_cycle.py's CLI
    uses (imported, not reimplemented) and appends one new snapshot. Returns
    the new run's id, or None if the live crude proxy couldn't be fetched
    (a network hiccup shouldn't block the mirror sync above, which already
    committed)."""
    try:
        crude_series = fetch_fred_brent_series(start=date(2023, 1, 1))
    except ProxyFetchError as e:
        print(f"  WARNING: skipping prediction snapshot -- crude proxy fetch failed: {e}")
        return None

    bridges = fit_all_bridges(sqlite_conn, crude_series)
    run = predict_products(sqlite_conn, today, crude_series, bridges)

    with pg_conn.cursor() as cur:
        cur.execute(
            """INSERT INTO prediction_runs
                (today, last_cycle_date, cycle_end_assumed, fx_rate, fx_source)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (run.today, run.last_cycle_date, run.cycle_end_assumed, run.fx_rate, run.fx_source),
        )
        run_id = cur.fetchone()[0]

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO predictions
                (run_id, retail_product_code, world_product_code, predicted_vnd, low_vnd, high_vnd,
                 known_days, nowcast_days, forecast_days, window_days_total, bog_net_vnd,
                 lumped_residual_vnd, world_price_avg, unit, known_bias_caveat)
               VALUES %s""",
            [
                (
                    run_id, p.retail_product_code, p.world_product_code, p.predicted_vnd, p.low_vnd, p.high_vnd,
                    p.known_days, p.nowcast_days, p.forecast_days, p.window_days_total, p.bog_net_vnd,
                    p.lumped_residual_vnd, p.world_price_avg, p.unit,
                    p.retail_product_code in KNOWN_BIAS_PRODUCTS,
                )
                for p in run.predictions
            ],
        )
    pg_conn.commit()
    return run_id


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--today", type=str, default=None)
    args = ap.parse_args()

    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        print("FATAL: SUPABASE_DB_URL is not set (see .env.example).")
        sys.exit(1)

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if args.today else date.today()

    sqlite_conn = init_db(str(args.db))
    seed_constants(sqlite_conn)

    pg_conn = psycopg2.connect(db_url)
    try:
        print("=== Mirroring bulletins/world_price_daily/cycle_summary/retail_prices/bog_actions ===")
        counts = mirror_all(pg_conn, sqlite_conn)
        for table, n in counts.items():
            print(f"  {table:20s} {n} rows")

        print("\n=== Appending a new prediction snapshot ===")
        run_id = append_prediction_run(pg_conn, sqlite_conn, today)
        if run_id is not None:
            print(f"  prediction_runs.id = {run_id}")
    finally:
        pg_conn.close()


if __name__ == "__main__":
    main()
