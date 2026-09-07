"""
Regression test against the 09/7/2026 bulletin, whose values were quoted
verbatim in the original research brief. This both validates the parser's
correctness on real data and guards against future regressions.

Fixture: tests/fixtures/bulletin_20260709.html (raw HTML as fetched from
https://moit.gov.vn/tin-tuc/mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau-ngay-09-7-2026.html)
"""

import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.parser.bulletin_parser import (
    normalize_price_cell,
    normalize_vcb_cell,
    parse_bulletin,
)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "bulletin_20260709.html"
FIXTURE_URL = "https://moit.gov.vn/tin-tuc/mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau-ngay-09-7-2026.html"


def _parse_fixture():
    html = FIXTURE_PATH.read_text(encoding="utf-8")
    return parse_bulletin(html, FIXTURE_URL)


def test_parse_status_ok():
    b = _parse_fixture()
    assert b.parse_status == "ok"
    assert b.warnings == []


def test_bulletin_date_and_effective_at():
    b = _parse_fixture()
    assert b.bulletin_date == date(2026, 7, 9)
    assert b.effective_at == datetime(2026, 7, 9, 15, 0)


def test_daily_world_price_table_known_values():
    """Spot-check specific daily rows against the brief's example values."""
    b = _parse_fixture()
    by_key = {(r["product_code"], r["quote_date"]): r["price"] for r in b.world_price_daily}

    # First row of the table (26/2/26): X92=79,280 -> 79.280
    assert by_key[("RON92", date(2026, 2, 26))] == 79.280
    assert by_key[("RON95", date(2026, 2, 26))] == 81.740

    # The row explicitly quoted in the brief with mixed '.' decimal
    # separator bug: "119.460  125.820  ...  26,142  26,361" for 8/4/26.
    assert by_key[("RON92", date(2026, 4, 8))] == 119.460
    assert by_key[("RON95", date(2026, 4, 8))] == 125.820

    # Last row of the table (8/7/26).
    assert by_key[("RON92", date(2026, 7, 8))] == 94.780
    assert by_key[("RON95", date(2026, 7, 8))] == 96.790
    assert by_key[("DIESEL_0_05S", date(2026, 7, 8))] == 120.070


def test_vcb_rate_mixed_separator_bug_row():
    """Same 8/4/26 row: VCB columns use ',' where the site normally uses '.' —
    both must resolve to the same whole-VND value regardless of which
    separator character was used."""
    b = _parse_fixture()
    by_key = {(r["product_code"], r["quote_date"]): r["price"] for r in b.world_price_daily}
    assert by_key[("VCB_BUY", date(2026, 4, 8))] == 26142
    assert by_key[("VCB_SELL", date(2026, 4, 8))] == 26361
    # A normal (non-buggy) row for comparison.
    assert by_key[("VCB_BUY", date(2026, 2, 26))] == 25780
    assert by_key[("VCB_SELL", date(2026, 2, 26))] == 26260


def test_weekend_rows_are_blank_not_error():
    b = _parse_fixture()
    keys = {(r["product_code"], r["quote_date"]) for r in b.world_price_daily}
    # 28/2/26 and 1/3/26 are the weekend rows shown as "-" in the table.
    assert ("RON92", date(2026, 2, 28)) not in keys
    assert ("RON92", date(2026, 3, 1)) not in keys


def test_kerosene_column_goes_blank_partway_through():
    b = _parse_fixture()
    keys = {(r["product_code"], r["quote_date"]) for r in b.world_price_daily}
    assert ("KEROSENE", date(2026, 2, 26)) in keys  # present early on
    assert ("KEROSENE", date(2026, 7, 8)) not in keys  # discontinued by the end of the table


def test_duplicate_row_number_does_not_break_parsing():
    """TT=107 appears twice (12/6/26 and 13/6/26) in the real table — the
    parser must key on quote_date, never on the row-number column."""
    b = _parse_fixture()
    keys = {(r["product_code"], r["quote_date"]) for r in b.world_price_daily}
    assert ("RON92", date(2026, 6, 12)) in keys
    assert ("RON92", date(2026, 6, 13)) not in keys  # that date's row is all blank/'-'


def test_cycle_summary_published_averages_match_brief():
    b = _parse_fixture()
    by_product = {c["product_code"]: c for c in b.cycle_summary}

    ron92 = by_product["RON92"]
    assert ron92["avg_price_published"] == 94.948
    assert ron92["delta_published"] == -3.218
    assert ron92["pct_change_published"] == -3.28

    ron95 = by_product["RON95"]
    assert ron95["avg_price_published"] == 97.468
    assert ron95["delta_published"] == -2.624
    assert ron95["pct_change_published"] == -2.62

    diesel = by_product["DIESEL_0_05S"]
    assert diesel["avg_price_published"] == 116.456
    assert diesel["delta_published"] == 4.656
    assert diesel["pct_change_published"] == 4.16

    fo = by_product["FO_180CST_3_5S"]
    assert fo["avg_price_published"] == 428.324
    assert fo["delta_published"] == -4.486
    assert fo["pct_change_published"] == -1.04


def test_cycle_summary_validation_passes_for_all_products():
    """The since-last-cycle average, reproduced from the same bulletin's own
    daily table, must match the published average to within rounding for
    every product that has data in the table."""
    b = _parse_fixture()
    for c in b.cycle_summary:
        assert c["validation_ok"] is True, f"{c['product_code']}: {c}"


def test_retail_prices_match_brief():
    b = _parse_fixture()
    by_product = {r["product_code"]: r for r in b.retail_prices}

    assert by_product["E10RON95III"]["price_vnd"] == 20003
    assert by_product["E10RON95III"]["delta_vnd"] == -412

    assert by_product["E5RON92"]["price_vnd"] == 19191
    assert by_product["E5RON92"]["delta_vnd"] == -539

    assert by_product["DIESEL_0_05S"]["price_vnd"] == 21745
    assert by_product["DIESEL_0_05S"]["delta_vnd"] == 569

    assert by_product["FO_180CST_3_5S"]["price_vnd"] == 13735
    assert by_product["FO_180CST_3_5S"]["delta_vnd"] == -318


def test_bog_actions_match_brief():
    b = _parse_fixture()
    by_product = {r["product_code"]: r for r in b.bog_actions}

    assert by_product["XANG_SINH_HOC"]["trich_lap_vnd"] == 200
    assert by_product["XANG_SINH_HOC"]["chi_su_dung_vnd"] == 0

    assert by_product["DIESEL_0_05S"]["trich_lap_vnd"] == 0
    assert by_product["DIESEL_0_05S"]["chi_su_dung_vnd"] == 0

    assert by_product["FO_180CST_3_5S"]["trich_lap_vnd"] == 200
    assert by_product["FO_180CST_3_5S"]["chi_su_dung_vnd"] == 0


def test_normalize_price_cell_handles_both_decimal_conventions():
    assert normalize_price_cell("79,280") == 79.280
    assert normalize_price_cell("119.460") == 119.460
    assert normalize_price_cell("-") is None
    assert normalize_price_cell("") is None
    assert normalize_price_cell("  -  ") is None


def test_normalize_vcb_cell_handles_both_decimal_conventions():
    assert normalize_vcb_cell("25.780") == 25780
    assert normalize_vcb_cell("26,142") == 26142
    assert normalize_vcb_cell("-") is None
