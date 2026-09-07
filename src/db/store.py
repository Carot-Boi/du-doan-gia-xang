"""Insert helpers implementing the storage rules from the Phase-1 spec."""

import sqlite3
from datetime import date, datetime, timezone

from src.parser.bulletin_parser import ParsedBulletin


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def upsert_bulletin_record(
    conn: sqlite3.Connection,
    *,
    url: str,
    raw_html_path: str,
    http_status: int | None,
    discovery_method: str,
    parsed: ParsedBulletin,
) -> int:
    """
    Insert or refresh the bulletins row for this URL.

    `url` is UNIQUE, so a re-run of the backfill against an already-fetched
    bulletin updates its parse result in place rather than creating a
    duplicate bulletins row -- but see store_bulletin() below: the
    child-table rows (world_price_daily etc.) it inserts are NOT re-linked
    to a pre-existing bulletin id on re-run within the same process unless
    the caller passes that id back in, since backfill.py always calls this
    first and uses the returned id for every child insert in the same pass.
    """
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("SELECT id FROM bulletins WHERE url = ?", (url,))
    row = cur.fetchone()
    warnings_joined = "\n".join(parsed.warnings) if parsed.warnings else None

    if row:
        bulletin_id = row["id"]
        conn.execute(
            """UPDATE bulletins SET
                bulletin_date=?, effective_at=?, fetch_timestamp=?, http_status=?,
                raw_html_path=?, title=?, title_variant=?, parse_status=?,
                parse_warnings=?, discovery_method=?
               WHERE id=?""",
            (
                _iso(parsed.bulletin_date), _iso(parsed.effective_at), now, http_status,
                raw_html_path, parsed.title, parsed.title_variant, parsed.parse_status,
                warnings_joined, discovery_method, bulletin_id,
            ),
        )
    else:
        cur = conn.execute(
            """INSERT INTO bulletins
                (url, bulletin_date, effective_at, fetch_timestamp, http_status,
                 raw_html_path, title, title_variant, parse_status, parse_warnings, discovery_method)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                url, _iso(parsed.bulletin_date), _iso(parsed.effective_at), now, http_status,
                raw_html_path, parsed.title, parsed.title_variant, parsed.parse_status,
                warnings_joined, discovery_method,
            ),
        )
        bulletin_id = cur.lastrowid
    return bulletin_id


def store_world_price_daily(conn: sqlite3.Connection, bulletin_id: int, rows: list[dict]) -> tuple[int, int]:
    """
    Append-only insert. Returns (inserted_count, conflict_count).

    A "conflict" is a new row whose price differs from a PRIOR bulletin's
    stored price for the same (product_code, quote_date) — flagged, not
    overwritten, per spec: this is signal that MOIT revised a historical
    quote, not something to silently discard.
    """
    inserted, conflicts = 0, 0
    for r in rows:
        quote_date_iso = _iso(r["quote_date"])
        cur = conn.execute(
            """SELECT price FROM world_price_daily
               WHERE product_code=? AND quote_date=? AND source_bulletin_id != ?
               ORDER BY id DESC LIMIT 1""",
            (r["product_code"], quote_date_iso, bulletin_id),
        )
        prior = cur.fetchone()
        conflict = 0
        if prior is not None and abs(prior["price"] - r["price"]) > 1e-9:
            conflict = 1
            conflicts += 1
        conn.execute(
            """INSERT INTO world_price_daily
                (product_code, quote_date, price, unit, source_bulletin_id, conflicts_with_prior)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (r["product_code"], quote_date_iso, r["price"], r["unit"], bulletin_id, conflict),
        )
        inserted += 1
    return inserted, conflicts


def store_cycle_summary(conn: sqlite3.Connection, bulletin_id: int, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO cycle_summary
                (bulletin_id, product_code, prev_cycle_date, this_cycle_date,
                 avg_price_published, delta_published, pct_change_published,
                 computed_avg, validation_ok, validation_note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                bulletin_id, r["product_code"], _iso(r.get("prev_cycle_date")), _iso(r.get("this_cycle_date")),
                r.get("avg_price_published"), r.get("delta_published"), r.get("pct_change_published"),
                r.get("computed_avg"),
                None if r.get("validation_ok") is None else int(bool(r.get("validation_ok"))),
                r.get("validation_note"),
            ),
        )


def store_retail_prices(conn: sqlite3.Connection, bulletin_id: int, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO retail_prices
                (bulletin_id, product_code, price_vnd, delta_vnd, unit, effective_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (bulletin_id, r["product_code"], r.get("price_vnd"), r.get("delta_vnd"), r.get("unit"), _iso(r.get("effective_at"))),
        )


def store_bog_actions(conn: sqlite3.Connection, bulletin_id: int, rows: list[dict]) -> None:
    for r in rows:
        conn.execute(
            """INSERT INTO bog_actions
                (bulletin_id, product_code, trich_lap_vnd, chi_su_dung_vnd, unit)
               VALUES (?, ?, ?, ?, ?)""",
            (bulletin_id, r["product_code"], r.get("trich_lap_vnd"), r.get("chi_su_dung_vnd"), r.get("unit")),
        )


def store_bulletin(
    conn: sqlite3.Connection,
    *,
    url: str,
    raw_html_path: str,
    http_status: int | None,
    discovery_method: str,
    parsed: ParsedBulletin,
) -> dict:
    """Store everything extracted from one parsed bulletin in a single transaction."""
    bulletin_id = upsert_bulletin_record(
        conn, url=url, raw_html_path=raw_html_path, http_status=http_status,
        discovery_method=discovery_method, parsed=parsed,
    )
    inserted, conflicts = store_world_price_daily(conn, bulletin_id, parsed.world_price_daily)
    store_cycle_summary(conn, bulletin_id, parsed.cycle_summary)
    store_retail_prices(conn, bulletin_id, parsed.retail_prices)
    store_bog_actions(conn, bulletin_id, parsed.bog_actions)
    conn.commit()
    return {
        "bulletin_id": bulletin_id,
        "world_price_rows_inserted": inserted,
        "world_price_conflicts": conflicts,
        "cycle_summary_rows": len(parsed.cycle_summary),
        "retail_price_rows": len(parsed.retail_prices),
        "bog_action_rows": len(parsed.bog_actions),
    }
