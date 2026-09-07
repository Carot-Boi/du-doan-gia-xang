"""SQLite schema for Phase 1 (data foundation only — no prediction logic here)."""

import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bulletins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    bulletin_date TEXT,               -- ISO date, the "ngay DD/MM/YYYY" the bulletin itself is about
    effective_at TEXT,                -- ISO datetime the new retail prices take effect
    fetch_timestamp TEXT NOT NULL,    -- when we fetched it (UTC ISO)
    http_status INTEGER,
    raw_html_path TEXT NOT NULL,
    title TEXT,
    title_variant TEXT,               -- which known slug/title phrasing this bulletin uses
    parse_status TEXT NOT NULL,       -- ok | partial | failed
    parse_warnings TEXT,              -- newline-joined warning strings, for humans to skim
    discovery_method TEXT             -- which discovery tier found this URL (category_api | brute_force | manual)
);

-- Append-only by design: if a later bulletin restates a date we already
-- have, we insert a NEW row rather than overwrite. MOIT has been observed
-- to republish/duplicate content, and a differing value for a date we've
-- already stored is itself useful signal (possible revision), not noise
-- to discard.
CREATE TABLE IF NOT EXISTS world_price_daily (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL,
    quote_date TEXT NOT NULL,
    price REAL NOT NULL,
    unit TEXT NOT NULL,
    source_bulletin_id INTEGER NOT NULL REFERENCES bulletins(id),
    conflicts_with_prior INTEGER NOT NULL DEFAULT 0  -- 1 if a prior row for the same (product_code, quote_date) had a different price
);
CREATE INDEX IF NOT EXISTS idx_world_price_daily_product_date
    ON world_price_daily(product_code, quote_date);

CREATE TABLE IF NOT EXISTS cycle_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_id INTEGER NOT NULL REFERENCES bulletins(id),
    product_code TEXT NOT NULL,
    prev_cycle_date TEXT,
    this_cycle_date TEXT,
    avg_price_published REAL,
    delta_published REAL,
    pct_change_published REAL,
    computed_avg REAL,               -- what our own since-last-cycle average calc produced, if checkable
    validation_ok INTEGER,           -- 1 / 0 / NULL (NULL = not enough same-bulletin data to check)
    validation_note TEXT
);

CREATE TABLE IF NOT EXISTS retail_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_id INTEGER NOT NULL REFERENCES bulletins(id),
    product_code TEXT NOT NULL,
    price_vnd INTEGER,
    delta_vnd INTEGER,
    unit TEXT,
    effective_at TEXT
);

CREATE TABLE IF NOT EXISTS bog_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bulletin_id INTEGER NOT NULL REFERENCES bulletins(id),
    product_code TEXT NOT NULL,
    trich_lap_vnd INTEGER,
    chi_su_dung_vnd INTEGER,
    unit TEXT
);

-- Phase 2: pricing-formula constants (Nghi dinh 83/2014 + 95/2021 Dieu 38a +
-- later amendments). Keyed by (constant_key, product_code, effective_from)
-- rather than a single "current value" table, because the whole point of
-- this table is that historical bulletins must be scored against the
-- constants that were actually in force on their own date, not today's.
-- product_code = 'ALL' means the constant applies uniformly across products
-- (e.g. import duty, VAT); a specific retail product_code overrides that
-- for products where the rate genuinely differs (e.g. thue TTDB only
-- applies to gasoline, never diesel/FO/kerosene -- see
-- src/pricing/formula.py's is_gasoline() gate, which skips the lookup
-- entirely for non-gasoline products rather than requiring a stored 0 row).
-- effective_from/effective_to are inclusive ISO dates; NULL effective_to
-- means "still in effect as of when this row was seeded".
CREATE TABLE IF NOT EXISTS constants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    constant_key TEXT NOT NULL,
    product_code TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT,
    effective_from TEXT,
    effective_to TEXT,
    source TEXT,
    note TEXT,
    UNIQUE(constant_key, product_code, effective_from)
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: str) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn
