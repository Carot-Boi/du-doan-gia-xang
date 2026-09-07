"""
Phase 3 tests for src/pricing/bridge.py.

fit_linear()'s regression math is checked against a small synthetic dataset
with a KNOWN exact answer (constructed so the OLS solution is exact, not
approximate) -- reproducible offline, independent of any live API. The
DB-integration path (fit_bridge_for_product / fit_all_bridges) is checked
against a hand-built in-memory world_price_daily + a synthetic
CrudeProxySeries, again with no network access.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.db.schema import init_db
from src.pricing.bridge import (
    BridgeFitError,
    fit_all_bridges,
    fit_bridge_for_product,
    fit_linear,
    monthly_avg_by_product,
)
from src.proxy.crude_proxy import CrudeProxySeries


def test_fit_linear_recovers_exact_line():
    """y = 5 + 2x exactly, no noise -- OLS must recover a=5, b=2 exactly
    (up to floating point)."""
    xs = [10.0, 20.0, 30.0, 40.0, 50.0]
    ys = [5 + 2 * x for x in xs]
    a, b = fit_linear(xs, ys)
    assert a == pytest.approx(5.0, abs=1e-9)
    assert b == pytest.approx(2.0, abs=1e-9)


def test_fit_linear_matches_independently_computed_ols():
    """y = 3 + 0.5x plus noise (not exactly on the line, unlike the
    exact-line test above) -- checked against a value independently
    computed by hand from the textbook OLS formulas
    (b = cov(x,y)/var(x), a = ybar - b*xbar), not just "close to the true
    generating line" (which noise makes an unreliable check on its own)."""
    xs = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    noise = [1.0, -1.0, 2.0, -2.0, 1.0, -1.0]
    ys = [3 + 0.5 * x + n for x, n in zip(xs, noise)]
    a, b = fit_linear(xs, ys)
    assert a == pytest.approx(3.571428571428571, abs=1e-9)
    assert b == pytest.approx(0.47714285714285715, abs=1e-9)


def test_fit_linear_requires_at_least_two_points():
    with pytest.raises(BridgeFitError):
        fit_linear([1.0], [2.0])


def test_fit_linear_rejects_zero_variance_x():
    with pytest.raises(BridgeFitError):
        fit_linear([5.0, 5.0, 5.0], [1.0, 2.0, 3.0])


def test_fit_linear_mismatched_lengths_raises():
    with pytest.raises(ValueError):
        fit_linear([1.0, 2.0], [1.0])


@pytest.fixture()
def conn():
    c = init_db(":memory:")
    c.execute(
        "INSERT INTO bulletins (id, url, fetch_timestamp, raw_html_path, parse_status) "
        "VALUES (1, 'https://example.test/x', '2026-01-01T00:00:00', '/dev/null', 'ok')"
    )
    c.commit()
    yield c
    c.close()


def _insert_daily(conn, product_code: str, quote_date: str, price: float, bulletin_id: int = 1):
    conn.execute(
        "INSERT INTO world_price_daily (product_code, quote_date, price, unit, source_bulletin_id) "
        "VALUES (?, ?, ?, 'USD/thung', ?)",
        (product_code, quote_date, price, bulletin_id),
    )


def test_monthly_avg_by_product_groups_by_calendar_month(conn):
    _insert_daily(conn, "RON92", "2026-01-05", 100.0)
    _insert_daily(conn, "RON92", "2026-01-10", 110.0)
    _insert_daily(conn, "RON92", "2026-02-01", 200.0)
    conn.commit()

    result = monthly_avg_by_product(conn, "RON92")
    assert result == {"2026-01": pytest.approx(105.0), "2026-02": pytest.approx(200.0)}


def test_fit_bridge_for_product_matches_manual_ols(conn):
    # Product price = 10 + 1.5 * crude, exactly, across 3 months.
    _insert_daily(conn, "RON92", "2026-01-15", 10 + 1.5 * 80.0)
    _insert_daily(conn, "RON92", "2026-02-15", 10 + 1.5 * 90.0)
    _insert_daily(conn, "RON92", "2026-03-15", 10 + 1.5 * 100.0)
    conn.commit()

    crude = CrudeProxySeries(
        monthly=[
            (date(2026, 1, 1), 80.0),
            (date(2026, 2, 1), 90.0),
            (date(2026, 3, 1), 100.0),
        ]
    )

    fit = fit_bridge_for_product(conn, "RON92", crude)
    assert fit.n == 3
    assert fit.intercept == pytest.approx(10.0, abs=1e-6)
    assert fit.slope == pytest.approx(1.5, abs=1e-6)
    assert fit.residual_std == pytest.approx(0.0, abs=1e-6)
    assert fit.r == pytest.approx(1.0, abs=1e-6)
    assert fit.predict(120.0) == pytest.approx(10 + 1.5 * 120.0, abs=1e-6)


def test_fit_bridge_for_product_raises_with_insufficient_overlap(conn):
    _insert_daily(conn, "RON92", "2026-01-15", 100.0)
    conn.commit()
    crude = CrudeProxySeries(monthly=[(date(2026, 1, 1), 80.0)])
    with pytest.raises(BridgeFitError):
        fit_bridge_for_product(conn, "RON92", crude)


def test_fit_all_bridges_skips_products_without_enough_data(conn):
    _insert_daily(conn, "RON92", "2026-01-15", 100.0)
    _insert_daily(conn, "RON92", "2026-02-15", 110.0)
    # DIESEL_0_05S has only one month -> should be skipped, not raise.
    _insert_daily(conn, "DIESEL_0_05S", "2026-01-15", 90.0)
    conn.commit()

    crude = CrudeProxySeries(monthly=[(date(2026, 1, 1), 80.0), (date(2026, 2, 1), 85.0)])
    bridges = fit_all_bridges(conn, crude, world_products=["RON92", "DIESEL_0_05S"])
    assert "RON92" in bridges
    assert "DIESEL_0_05S" not in bridges


def test_crude_proxy_series_value_on_forward_fills():
    series = CrudeProxySeries(monthly=[(date(2026, 1, 1), 80.0), (date(2026, 3, 1), 100.0)])
    assert series.value_on(date(2026, 1, 15)) == pytest.approx(80.0)  # within Jan
    assert series.value_on(date(2026, 2, 20)) == pytest.approx(80.0)  # Feb has no data -> hold Jan flat
    assert series.value_on(date(2026, 3, 5)) == pytest.approx(100.0)  # into March
    assert series.value_on(date(2026, 4, 1)) == pytest.approx(100.0)  # beyond latest -> hold flat
    assert series.value_on(date(2025, 12, 1)) == pytest.approx(80.0)  # before earliest -> backward fill


def test_crude_proxy_series_value_on_empty_series_returns_none():
    series = CrudeProxySeries(monthly=[])
    assert series.value_on(date(2026, 1, 1)) is None
