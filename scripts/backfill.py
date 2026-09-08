"""
Backfill script: discover MOIT fuel-price bulletins, fetch + archive their
raw HTML, parse them, and store structured results in the local SQLite DB.

Usage:
    python scripts/backfill.py [--db data/db/moit.sqlite3] [--limit N] [--no-brute-force]
                                [--brute-force-days N]

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
from datetime import date, timedelta
from pathlib import Path

# Bulletin titles/warnings contain Vietnamese text. On Windows, stdout is
# often opened with the system ANSI codepage (e.g. cp1258) rather than
# UTF-8 -- especially when output is redirected to a file/log rather than
# an interactive UTF-8-capable terminal -- which raises UnicodeEncodeError
# on print() the moment a warning string contains an accented character.
# Reconfiguring explicitly here makes the script robust regardless of how
# it's invoked, rather than relying on callers to set PYTHONIOENCODING.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.schema import init_db
from src.db.store import store_bulletin
from src.parser.bulletin_parser import parse_bulletin
from src.scraper.discovery import discover_all
from src.scraper.fetch import fetch_and_archive
from src.scraper.http import PoliteSession

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "db" / "moit.sqlite3"


def _bulletin_dates_already_in_db(conn) -> frozenset[date]:
    """Bulletin dates this DB already has stored, from any prior run --
    passed into discover_all() so Tier 2 doesn't re-probe Thursdays whose
    bulletin is already safely on disk. See discover_all()'s docstring for
    why this matters in practice, not just in theory."""
    rows = conn.execute("SELECT DISTINCT bulletin_date FROM bulletins WHERE bulletin_date IS NOT NULL").fetchall()
    out = set()
    for (d,) in rows:
        try:
            out.add(date.fromisoformat(d))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def run_backfill(db_path: Path, limit: int | None, use_brute_force: bool, brute_force_days: int | None = 120) -> None:
    session = PoliteSession()
    conn = init_db(str(db_path))
    already_known_dates = _bulletin_dates_already_in_db(conn)

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
            # IMPORTANT: gap_end must extend to TODAY, not just
            # max(known_dates). Confirmed by hand (2026-09-07): MOIT
            # silently stopped filing new fuel bulletins under Tier 1's
            # category sometime after 2026-07-09, so Tier 1's own
            # max(known_dates) can itself be stale by weeks -- real
            # bulletins for 13/8/2026 and 27/8/2026 exist and are fetchable
            # by direct URL, but Tier 1 never returns them, and the old
            # `gap_end = max(known_dates)` here meant Tier 2 could only ever
            # fill holes *inside* Tier 1's own range, never discover
            # anything more recent than Tier 1's last (possibly stale)
            # result. This is exactly the "site changed, discovery breaks
            # silently" risk flagged in the design doc -- it was not
            # hypothetical, it happened during this project's own
            # development. Do not revert this without re-confirming Tier 1
            # is current again (e.g. by hand-checking
            # https://moit.gov.vn/tin-tuc/thi-truong-trong-nuoc against
            # today's actual latest bulletin).
            #
            # gap_start is windowed to the last `brute_force_days` days by
            # default (not the full min(known_dates), which can be back to
            # 2022). Confirmed necessary in practice (2026-09-08): most of
            # the ~235 Thursdays since 2022 have no bulletin at all (MOIT
            # doesn't publish every single week), so "gap Thursdays" stayed
            # ~120-125 every run even after already_known_dates started
            # skipping Thursdays whose bulletin IS already stored -- there's
            # no cache of "already confirmed no bulletin here" to skip the
            # rest, so a full-history scan re-probes essentially the same
            # ~120 empty Thursdays forever. One real run at the old
            # unwindowed scope took over an hour on GitHub Actions. Old
            # bulletins from years ago are also for all practical purposes
            # never going to newly appear -- the risk this project actually
            # cares about (MOIT silently changing its URL scheme, per the
            # d96338e and f056a37 commits) is a RECENT-week phenomenon, not
            # a 2023 one. Pass --brute-force-days 0 (or a very large number)
            # for an explicit full-history rescan when actually needed (e.g.
            # after another URL-scheme change is suspected somewhere in the
            # older history too).
            gap_start_floor = min(known_dates)
            if brute_force_days:
                gap_start_floor = max(gap_start_floor, date.today() - timedelta(days=brute_force_days))
            gap_start, gap_end = gap_start_floor, max(max(known_dates), date.today())
            print(f"=== Tier 2: brute-force gap-fill probing Thursdays in [{gap_start}, {gap_end}] ===")
            bulletins2, stats2 = discover_all(
                session, brute_force_start=gap_start, brute_force_end=gap_end,
                already_known_dates=already_known_dates,
            )
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
    ap.add_argument(
        "--brute-force-days", type=int, default=120,
        help="Only Tier-2 gap-fill Thursdays within this many days of today (default 120). "
             "Pass 0 to scan the full history instead (slow -- see run_backfill()'s comment).",
    )
    args = ap.parse_args()
    run_backfill(
        args.db, args.limit, use_brute_force=not args.no_brute_force,
        brute_force_days=args.brute_force_days or None,
    )


if __name__ == "__main__":
    main()
