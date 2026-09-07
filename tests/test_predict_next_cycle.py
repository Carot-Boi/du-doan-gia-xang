"""
Phase 3 tests for scripts/predict_next_cycle.py.

No live internet access required: the crude proxy is a hand-built
CrudeProxySeries fixture (not a live FRED fetch), and everything else comes
from an in-memory sqlite DB seeded directly by this file -- reproducible
offline, like tests/test_bridge.py and tests/test_formula.py.

The fixture DB models one already-published bulletin (2026-07-09, the "last
known real cycle") with three months of world_price_daily history behind it
(2026-05, 2026-06, 2026-07 -- one row per month per product, enough for
bridge.fit_bridge_for_product's n>=2 requirement) so predict_products() has
something real to build a bridge from and a real prior cycle to inherit
FX/BOG assumptions from.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.db.schema import init_db
from src.pricing.bridge import fit_all_bridges
from src.pricing.constants import seed_constants
from src.proxy.crude_proxy import CrudeProxySeries
from scripts.predict_next_cycle import (
    business_days,
    get_flat_fx_assumption,
    get_last_known_cycle_date,
    next_thursday_on_or_after,
    predict_products,
)

# World product -> monthly average prices used to build the fixture (chosen
# so the crude->product line has a real, nonzero residual -- not a
# trivially perfect fit).
_WORLD_MONTHLY = {
    "RON92": {"2026-05": 79.0, "2026-06": 91.0, "2026-07": 99.0},
    "RON95": {"2026-05": 84.0, "2026-06": 96.0, "2026-07": 104.0},
    "DIESEL_0_05S": {"2026-05": 150.0, "2026-06": 145.0, "2026-07": 160.0},
    "FO_180CST_3_5S": {"2026-05": 600.0, "2026-06": 620.0, "2026-07": 650.0},
}
_MONTH_TO_DAY = {"2026-05": "2026-05-15", "2026-06": "2026-06-15", "2026-07": "2026-07-02"}

_CRUDE_SERIES = CrudeProxySeries(
    monthly=[(date(2026, 5, 1), 70.0), (date(2026, 6, 1), 80.0), (date(2026, 7, 1), 90.0)]
)

_PREV_CYCLE_DATE = date(2026, 7, 2)
_LAST_CYCLE_DATE = date(2026, 7, 9)


@pytest.fixture()
def conn():
    c = init_db(":memory:")
    seed_constants(c)

    c.execute(
        "INSERT INTO bulletins (id, url, bulletin_date, fetch_timestamp, raw_html_path, parse_status) "
        "VALUES (1, 'https://example.test/bulletin-2026-07-09', '2026-07-09', '2026-07-09T00:00:00', '/dev/null', 'ok')"
    )

    for world_code, months in _WORLD_MONTHLY.items():
        for ym, price in months.items():
            c.execute(
                "INSERT INTO world_price_daily (product_code, quote_date, price, unit, source_bulletin_id) "
                "VALUES (?, ?, ?, 'USD/thung', 1)",
                (world_code, _MONTH_TO_DAY[ym], price),
            )
        # cycle_summary row for the last cycle, using its own July monthly value
        # as the published cycle average (consistent since that month has
        # exactly one underlying daily row in this fixture).
        c.execute(
            "INSERT INTO cycle_summary (bulletin_id, product_code, prev_cycle_date, this_cycle_date, avg_price_published) "
            "VALUES (1, ?, ?, ?, ?)",
            (world_code, _PREV_CYCLE_DATE.isoformat(), _LAST_CYCLE_DATE.isoformat(), months["2026-07"]),
        )

    # VCB_SELL quotes covering the [prev_cycle_date, this_cycle_date) FX window.
    for d, rate in [("2026-07-02", 26100.0), ("2026-07-03", 26120.0), ("2026-07-08", 26150.0)]:
        c.execute(
            "INSERT INTO world_price_daily (product_code, quote_date, price, unit, source_bulletin_id) "
            "VALUES ('VCB_SELL', ?, ?, 'VND/USD', 1)",
            (d, rate),
        )

    # Retail ceiling prices: baseline (residual=0) + a chosen residual, so
    # fitted_residual_for_product() recovers a known number deterministically.
    from src.pricing.formula import compute_gia_co_so

    fx_avg = (26100.0 + 26120.0 + 26150.0) / 3
    retail_residuals = {
        "E5RON92": 2500.0,
        "RON95III": 2600.0,
        "E10RON95III": 2700.0,
        "DIESEL_0_05S": 2400.0,
        "FO_180CST_3_5S": 3000.0,
    }
    world_ref = {
        "E5RON92": "RON92",
        "RON95III": "RON95",
        "E10RON95III": "RON95",
        "DIESEL_0_05S": "DIESEL_0_05S",
        "FO_180CST_3_5S": "FO_180CST_3_5S",
    }
    for retail_code, residual in retail_residuals.items():
        baseline = compute_gia_co_so(
            c,
            world_price_avg_usd_per_unit=_WORLD_MONTHLY[world_ref[retail_code]]["2026-07"],
            fx_rate_vnd_per_usd=fx_avg,
            product_code=retail_code,
            as_of_date=_LAST_CYCLE_DATE,
            lumped_residual_vnd=0.0,
        )
        c.execute(
            "INSERT INTO retail_prices (bulletin_id, product_code, price_vnd, unit, effective_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (retail_code, baseline.gia_co_so_vnd + residual, baseline.retail_unit, _LAST_CYCLE_DATE.isoformat()),
        )

    # A real BOG action for the last cycle -- predict_products() should pick
    # this up as the "previous cycle's actual BOG" default.
    c.execute(
        "INSERT INTO bog_actions (bulletin_id, product_code, trich_lap_vnd, chi_su_dung_vnd) VALUES (1, 'XANG_SINH_HOC', 100, 0)"
    )

    c.commit()
    yield c
    c.close()


def test_next_thursday_on_or_after():
    assert next_thursday_on_or_after(date(2026, 7, 9)) == date(2026, 7, 9)  # already Thursday
    assert next_thursday_on_or_after(date(2026, 7, 10)) == date(2026, 7, 16)  # Friday -> next Thu
    assert next_thursday_on_or_after(date(2026, 7, 6)) == date(2026, 7, 9)  # Monday -> this week's Thu


def test_business_days_excludes_weekends_and_end():
    days = business_days(date(2026, 7, 9), date(2026, 7, 16))  # Thu .. next Thu (excl)
    assert days == [date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13), date(2026, 7, 14), date(2026, 7, 15)]
    assert date(2026, 7, 11) not in days  # Saturday
    assert date(2026, 7, 12) not in days  # Sunday
    assert date(2026, 7, 16) not in days  # window end is exclusive


def test_get_last_known_cycle_date(conn):
    d, bulletin_id = get_last_known_cycle_date(conn)
    assert d == _LAST_CYCLE_DATE
    assert bulletin_id == 1


def test_get_flat_fx_assumption_uses_last_cycle_window(conn):
    fx, source = get_flat_fx_assumption(conn, last_bulletin_id=1)
    assert fx == pytest.approx((26100.0 + 26120.0 + 26150.0) / 3)
    assert "last real cycle" in source


def test_predict_products_runs_and_returns_sane_values(conn):
    """Core computation with a mix of nowcast + forecast days (today falls
    inside the window, before the assumed cycle end) -- no known days,
    since this fixture's world_price_daily has nothing IN the new window."""
    today = date(2026, 7, 10)  # Friday, inside [2026-07-09, 2026-07-16)
    bridges = fit_all_bridges(conn, _CRUDE_SERIES)
    assert set(bridges.keys()) == {"RON92", "RON95", "DIESEL_0_05S", "FO_180CST_3_5S"}

    run = predict_products(conn, today, _CRUDE_SERIES, bridges)

    assert run.last_cycle_date == _LAST_CYCLE_DATE
    assert run.cycle_end_assumed == date(2026, 7, 16)
    assert not run.notes, f"expected every product to be predictable, got notes: {run.notes}"

    predicted_codes = {p.retail_product_code for p in run.predictions}
    assert predicted_codes == {"E5RON92", "RON95III", "E10RON95III", "DIESEL_0_05S", "FO_180CST_3_5S"}

    for p in run.predictions:
        assert p.predicted_vnd > 0
        assert p.low_vnd <= p.predicted_vnd <= p.high_vnd
        assert p.known_days == 0  # nothing in world_price_daily falls inside the NEW window
        assert p.nowcast_days == 2  # 07-09, 07-10 (<=today)
        assert p.forecast_days == 3  # 07-13, 07-14, 07-15 (>today)
        assert p.window_days_total == 5

    # BOG: only E5RON92/E10RON95III have a bog_actions row (XANG_SINH_HOC) in
    # this fixture; every other product falls back to 0, per the documented
    # "absent row means treat as 0" semantics -- not a missing-data error.
    by_code = {p.retail_product_code: p for p in run.predictions}
    assert by_code["E5RON92"].bog_net_vnd == pytest.approx(100.0)
    assert by_code["E10RON95III"].bog_net_vnd == pytest.approx(100.0)
    assert by_code["DIESEL_0_05S"].bog_net_vnd == pytest.approx(0.0)


def test_predict_products_all_nowcast_when_today_is_the_assumed_cycle_end(conn):
    """When 'today' itself is a Thursday, cycle_end == today (see
    next_thursday_on_or_after()), so the window [last_cycle_date, today)
    excludes today itself and every window day is <= today -- zero
    forecast days, by construction."""
    today = date(2026, 7, 23)  # a Thursday, one week past the assumed 07-16 end
    assert today.weekday() == 3
    bridges = fit_all_bridges(conn, _CRUDE_SERIES)
    run = predict_products(conn, today, _CRUDE_SERIES, bridges)
    assert run.cycle_end_assumed == today
    for p in run.predictions:
        assert p.forecast_days == 0
        assert p.nowcast_days == p.window_days_total


def test_predict_products_uncertainty_band_widens_with_more_forecast_days(conn):
    """Same product, two different 'today's -- the run with a bigger
    forecast fraction should have a wider (or equal) band, never narrower,
    since forecast days carry no real crude signal at all."""
    bridges = fit_all_bridges(conn, _CRUDE_SERIES)

    mostly_nowcast = predict_products(conn, date(2026, 7, 15), _CRUDE_SERIES, bridges)
    mostly_forecast = predict_products(conn, date(2026, 7, 9), _CRUDE_SERIES, bridges)

    a = next(p for p in mostly_nowcast.predictions if p.retail_product_code == "DIESEL_0_05S")
    b = next(p for p in mostly_forecast.predictions if p.retail_product_code == "DIESEL_0_05S")
    assert (b.high_vnd - b.low_vnd) >= (a.high_vnd - a.low_vnd)
