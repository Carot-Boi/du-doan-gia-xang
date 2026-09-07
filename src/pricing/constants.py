"""
Phase 2: giá cơ sở formula constants, stored in the `constants` table
(schema in src/db/schema.py) rather than as Python literals, so that a
historical bulletin is scored against the tax/fee regime that was actually
in force on ITS OWN date -- not whatever is true today.

Only the constants explicitly given as "known" in the Phase 2 brief are
seeded here. Two things are deliberately NOT stored as legal constants:

- The barrel/ton -> liter/kg physical unit-conversion factors
  (LITERS_PER_BARREL, KG_PER_TON) live in formula.py as plain module
  constants, not in this table, because they are physics, not policy -- they
  don't have an "effective_from/effective_to".
- "chi phí kinh doanh định mức", "chi phí đưa xăng dầu về cảng VN", and the
  BOG trích lập/chi sử dụng are NOT stored here as separate constants at
  all. Per the brief, their exact current đồng/lít figures are not
  confidently known, so rather than guess numbers, this phase lumps them
  (together with lợi nhuận định mức) into ONE fitted "residual/markup" term
  that calibrate.py derives from real published prices. See formula.py's
  `lumped_residual_vnd` parameter.

Seeding is idempotent: `constants` has a UNIQUE(constant_key, product_code,
effective_from) constraint, and seed_constants() uses INSERT OR IGNORE, so
re-running it (e.g. every time validate_formula.py starts) never creates
duplicates.
"""

from __future__ import annotations

import sqlite3
from datetime import date

# Retail products that are gasoline (xăng) for the purposes of thuế TTĐB,
# which -- unlike import duty, thuế BVMT, and VAT -- is NEVER applied to
# diesel, FO, or kerosene. Kept as an explicit allowlist here rather than
# inferring from RETAIL_PRODUCTS unit/label, so the tax-applicability rule
# is visible in one place.
GASOLINE_RETAIL_CODES = {"E5RON92", "RON95III", "E10RON95III"}

# (constant_key, product_code, value, unit, effective_from, effective_to, source, note)
# product_code = "ALL" applies to every product unless a more specific row
# for the same constant_key exists (see get_constant()'s fallback order).
_SEED_ROWS = [
    (
        "import_tax_rate", "ALL", 0.0, "rate",
        "2026-01-01", "2026-09-30",
        "Nghị định 72/2026/NĐ-CP; gia hạn bởi Nghị quyết 25/2026/NQ-CP và Nghị quyết 34/2026/NQ-CP",
        "Thuế nhập khẩu ưu đãi xăng/dầu/nguyên liệu = 0%. effective_from is a "
        "conservative lower bound covering this project's Phase-2 calibration "
        "window (2026-04 .. 2026-07) -- the exact original effective date of "
        "Nghị định 72/2026 itself was NOT independently verified in this phase; "
        "do not rely on this row for dates before 2026-01-01.",
    ),
    (
        "excise_tax_rate", "E5RON92", 0.0, "rate",
        "2026-04-16", "2026-09-30",
        "Nghị quyết 19/2026/QH16 (12/4/2026); gia hạn Khoản 2 Điều 3 Nghị quyết 34/2026/NQ-CP",
        "Thuế TTĐB xăng = 0%. Only ever looked up for gasoline products -- see "
        "is_gasoline() / GASOLINE_RETAIL_CODES.",
    ),
    (
        "excise_tax_rate", "RON95III", 0.0, "rate",
        "2026-04-16", "2026-09-30",
        "Nghị quyết 19/2026/QH16; gia hạn Nghị quyết 34/2026/NQ-CP",
        "Thuế TTĐB xăng = 0%.",
    ),
    (
        "excise_tax_rate", "E10RON95III", 0.0, "rate",
        "2026-04-16", "2026-09-30",
        "Nghị quyết 19/2026/QH16; gia hạn Nghị quyết 34/2026/NQ-CP",
        "Thuế TTĐB xăng = 0%.",
    ),
    (
        "env_tax_vnd", "ALL", 0.0, "VND/lit_or_kg",
        "2026-04-16", "2026-09-30",
        "Nghị quyết 19/2026/QH16; gia hạn Nghị quyết 34/2026/NQ-CP",
        "Thuế bảo vệ môi trường = 0 đồng/lít (kg cho FO) cho xăng (trừ etanol), "
        "dầu điêzen, dầu hỏa, dầu madút, nhiên liệu bay -- i.e. every retail "
        "product this project tracks.",
    ),
    (
        "vat_rate", "ALL", 0.0, "rate",
        "2026-04-16", "2026-09-30",
        'Nghị quyết 19/2026/QH16 + Nghị quyết 34/2026/NQ-CP (chế độ "không kê khai, '
        'tính nộp thuế GTGT nhưng được khấu trừ thuế GTGT đầu vào")',
        "NUANCE (flagged, not a clean 0% statutory rate): this period's regime is "
        '"no output VAT declared, but input VAT stays creditable" -- not literally '
        "the same thing as a 0% VAT rate. For this formula, which only needs the "
        "VAT multiplier applied at the retail-ceiling-price step, we treat the "
        "effective consumer-facing VAT contribution as 0 for this window. This is "
        "an assumption; revisit if evidence shows MOIT's own gia-co-so calculation "
        "handles it differently.",
    ),
    (
        "profit_margin_vnd", "ALL", 300.0, "VND/lit_or_kg",
        "2014-01-01", None,
        "Thông báo liên Bộ Công Thương - Tài chính (giá trị phổ biến được công bố nhiều kỳ)",
        "Lợi nhuận định mức = 300 đồng/lít (or /kg). Treated as a stable default "
        "across this DB's whole historical range -- NOT independently verified "
        "per-cycle. Stored with an effective-date range (open-ended effective_to) "
        "so a later phase can split it into sub-periods if evidence says otherwise.",
    ),
]


def seed_constants(conn: sqlite3.Connection) -> int:
    """Idempotently insert the known constants. Returns rows actually inserted."""
    cur = conn.cursor()
    inserted = 0
    for row in _SEED_ROWS:
        cur.execute(
            """INSERT OR IGNORE INTO constants
                (constant_key, product_code, value, unit, effective_from, effective_to, source, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            row,
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


class MissingConstantError(LookupError):
    """Raised when a formula input has no constant on record for that date.

    Deliberately NOT silently defaulted to 0 -- a missing constant means
    either the calibration window was mis-scoped (e.g. a bulletin predates
    the zero-tax regime) or a real historical rate is simply not modeled
    yet. Both are worth surfacing to the caller, not hiding.
    """


def get_constant(
    conn: sqlite3.Connection,
    constant_key: str,
    product_code: str,
    as_of_date: date,
) -> float:
    """
    Look up a constant's value effective on `as_of_date`, product-specific
    row first, falling back to the product_code='ALL' row for the same key.

    Raises MissingConstantError if neither is on record for that date --
    callers must not silently substitute 0, since for some keys (e.g.
    import_tax_rate before 2026) that would be a real wrong answer, not a
    harmless default.
    """
    as_of = as_of_date.isoformat() if isinstance(as_of_date, date) else str(as_of_date)
    for pc in (product_code, "ALL"):
        cur = conn.execute(
            """SELECT value FROM constants
               WHERE constant_key = ? AND product_code = ?
                 AND (effective_from IS NULL OR effective_from <= ?)
                 AND (effective_to IS NULL OR effective_to >= ?)
               ORDER BY effective_from DESC LIMIT 1""",
            (constant_key, pc, as_of, as_of),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
    raise MissingConstantError(
        f"no '{constant_key}' constant on record for product={product_code!r} as_of={as_of} "
        f"(checked product-specific and 'ALL' rows)"
    )


def is_gasoline(product_code: str) -> bool:
    return product_code in GASOLINE_RETAIL_CODES
