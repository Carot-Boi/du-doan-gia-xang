"""
Backfill script: discover MOIT fuel-price bulletins, fetch + archive their
raw HTML, parse them, and store structured results in the local SQLite DB.

Usage:
    python scripts/backfill.py [--db data/db/moit.sqlite3] [--limit N] [--no-brute-force]

Designed to be safely re-run: bulletins are keyed by URL (UNIQUE), so
re-running just refreshes already-known bulletins' parse results and picks
up any new ones. This is intentional -- the same code path is meant to be
reusable later as a "check for this week's new bulletin" job (a future
phase), not a one-shot script to be thrown away after this backfill.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.schema import init_db
from src.db.store import store_bulletin
from src.parser.bulletin_parser import parse_bulletin
from src.scraper.discovery import discover_all
from src.scraper.fetch import fetch_and_archive
from src.scraper.http import PoliteSession

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "moit.sqlite3"


def run_backfill(db_path: Path, limit: int | None, use_brute_force: bool) -> None:
    session = PoliteSession()
    conn = init_db(str(db_path))

    print("=== Tier 1: discovering bulletins via category listing API ===")
    bulletins, stats = discover_all(
        session,
        brute_force_start=None,
        brute_force_end=None,
    )
    print(f"Tier 1 (category_api) found: {stats.get('tier1_found', 0)} candidate URLs")
    if stats.get("tier1_error"):
        print(f"  Tier 1 ERROR: {stats['tier1_error']}")

    if use_brute_force and bulletins:
        known_dates = [b.publish_time.date() for b in bulletins if b.publish_time]
        if known_dates:
            gap_start, gap_end = min(known_dates), max(known_dates)
            print(f"=== Tier 2: brute-force gap-fill probing Thursdays in [{gap_start}, {gap_end}] ===")
            bulletins2, stats2 = discover_all(session, brute_force_start=gap_start, brute_force_end=gap_end)
            print(f"Tier 2 (brute_force) probed {stats2.get('tier2_probed', 0)} candidate Thursdays, "
                  f"found {stats2.get('tier2_found', 0)} additional bulletins")
            existing = {b.url for b in bulletins}
            bulletins = bulletins + [b for b in bulletins2 if b.url not in existing and b.discovery_method == "brute_force"]

    bulletins.sort(key=lambda b: b.publish_time or date.min)
    if limit:
        bulletins = bulletins[-limit:]

    print(f"\n=== Fetching, parsing, and storing {len(bulletins)} bulletins ===")

    fetched_ok = 0
    fetch_failed = 0
    parse_failed = 0
    parse_partial = 0
    validation_failures = 0
    dates_seen = []
    method_counts = Counter()

    for i, b in enumerate(bulletins, 1):
        method_counts[b.discovery_method] += 1
        print(f"[{i}/{len(bulletins)}] ({b.discovery_method}) {b.url}")
        fr = fetch_and_archive(session, b.url)
        if fr.error or fr.html is None:
            print(f"    FETCH FAILED: {fr.error}")
            fetch_failed += 1
            continue
        fetched_ok += 1

        parsed = parse_bulletin(fr.html, b.url)
        if parsed.parse_status == "failed":
            parse_failed += 1
            print(f"    PARSE FAILED: {parsed.warnings}")
        elif parsed.parse_status == "partial":
            parse_partial += 1
            print(f"    parsed with warnings: {parsed.warnings}")

        for cs in parsed.cycle_summary:
            if cs.get("validation_ok") is False:
                validation_failures += 1

        result = store_bulletin(
            conn, url=b.url, raw_html_path=fr.raw_html_path, http_status=fr.http_status,
            discovery_method=b.discovery_method, parsed=parsed,
        )
        if parsed.bulletin_date:
            dates_seen.append(parsed.bulletin_date)
        print(f"    stored: {result}")

    print("\n=== Backfill summary ===")
    print(f"Discovery method breakdown: {dict(method_counts)}")
    print(f"Bulletins attempted: {len(bulletins)}")
    print(f"Fetched OK: {fetched_ok}  |  Fetch failed: {fetch_failed}")
    print(f"Parse failed: {parse_failed}  |  Parse partial (warnings): {parse_partial}")
    print(f"Cycle-average validation failures: {validation_failures}")
    if dates_seen:
        print(f"Bulletin date range covered: {min(dates_seen)} .. {max(dates_seen)}")
    else:
        print("No bulletin dates successfully parsed.")
    print(f"Database: {db_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--limit", type=int, default=None, help="Only process the N most recent discovered bulletins")
    ap.add_argument("--no-brute-force", action="store_true", help="Skip the Tier-2 brute-force gap fill")
    args = ap.parse_args()
    run_backfill(args.db, args.limit, use_brute_force=not args.no_brute_force)


if __name__ == "__main__":
    main()
