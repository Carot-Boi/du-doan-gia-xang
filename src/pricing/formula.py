"""
Phase 2: the structural "giá cơ sở" (base fuel price) formula, per Nghị
định 83/2014/NĐ-CP + Nghị định 95/2021/NĐ-CP (Điều 38a) + later amendments.

    giá cơ sở = giá thế giới (quy đổi VNĐ)
                + [chi phí đưa về cảng VN + chi phí kinh doanh định mức
                   + lợi nhuận định mức + mức trích lập BOG]   <- lumped_residual_vnd
                (all of the above, x (1 + thuế nhập khẩu))
                x (1 + thuế TTĐB)         [xăng only]
                + thuế bảo vệ môi trường
                x (1 + VAT)

This module is a PURE function over its inputs plus whatever's in the
`constants` table (src/pricing/constants.py) for the given as_of_date -- it
does not read world_price_daily/cycle_summary/retail_prices itself. Callers
(calibrate.py, scripts/validate_formula.py) are responsible for pulling the
world-price average and FX rate out of the DB and passing them in.

Deliberate simplification for this phase (see the Phase 2 brief): chi phí
kinh doanh định mức, chi phí đưa về cảng, and lợi nhuận định mức are NOT
modeled as separate known figures. Their current đồng/lít values are not
confidently known, so they are lumped into ONE named parameter,
`lumped_residual_vnd`, which calibrate.py fits from real published (world
price, retail price) pairs rather than this module guessing a number.

UPDATE (post-validation follow-up): BOG (Quỹ bình ổn giá) trích lập/chi sử
dụng is NOT lumped into that fitted term. bog_actions is real, per-cycle,
per-product data MOIT already publishes (parsed in Phase 1) -- lumping it
into an "average" residual was actively wrong, because BOG swings ~100-500
đ/lít cycle to cycle (see bog_actions), while chi phí/lợi nhuận định mức are
genuinely close to fixed. Folding a swinging real number into a fitted
constant just relabels real variation as noise. `bog_net_vnd` is therefore
a separate, explicit parameter here -- calibrate.py now fits
`lumped_residual_vnd` only from the residual THAT REMAINS after already
subtracting the real bog_net_vnd for that cycle.

MEASURED RESULT of this change (run scripts/validate_formula.py to
reproduce): overall MAPE improved 1.71% -> 1.45%. It's a clear, real win for
FO_180CST_3_5S (residual spread 28.4% -> 11.7% of mean) and RON95III (48.0%
-> 21.6%) -- confirms BOG swings were genuinely a big chunk of their noise.

UPDATE (post-Phase-3 data fix): the 1.45% figure above was measured on a DB
that turned out to have retail_prices/cycle_summary/bog_actions rows
triplicated by a backfill re-run bug (see src/db/store.py's docstring) --
the duplication was uniform so it didn't change the number, but it wasn't a
clean measurement either. After fixing that bug and rebuilding the DB (also
picking up 2 previously-undiscovered recent bulletins, see
scripts/backfill.py's docstring), the same calibration now measures overall
MAPE at 1.57% on clean, deduplicated data. Re-run scripts/validate_formula.py
to reproduce.

It did NOT meaningfully fix E5RON92 or E10RON95III, whose worst errors (the
two most recent cycles, both ~-5% and ~-2 to -4%) are essentially unchanged
-- XANG_SINH_HOC's trích_lập is only ~100-200đ there, far too small to
explain a >1000đ/lít gap. Both are ethanol-blended products whose world
reference is plain RON92/RON95 -- the formula has no term at all for the
ethanol component's own (domestically-priced, not Platts-quoted) cost, which
is the leading suspect for this specific residual pattern. Left as an open
item for a later phase (would need a domestic ethanol/biofuel price series
this project doesn't have yet) rather than papering over it with a bigger
fitted constant.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from src.parser.products import RETAIL_PRODUCTS, WORLD_PRODUCTS
from src.pricing.constants import MissingConstantError, get_constant, is_gasoline

# USD/barrel -> USD/liter for RON92, RON95, DIESEL_0_05S (all quoted
# "USD/thùng" in MOIT's daily table). 158.987 L is the standard "42 US
# gallon" oil-industry barrel. Phase 2's calibration step
# (src/pricing/calibrate.py) reverse-checks this figure against MOIT's own
# published retail prices rather than trusting it blindly -- see that
# module's docstring / the Phase 2 report for what was found.
LITERS_PER_BARREL = 158.987

# USD/ton -> USD/kg for FO_180CST_3_5S (quoted "USD/tấn").
KG_PER_TON = 1000.0


class UnsupportedProductError(ValueError):
    """Raised for a retail product_code this formula doesn't know how to price."""


def _world_unit_to_retail_unit(world_price_avg_usd_per_unit: float, world_code: str) -> float:
    """Convert a world-quote average (USD/thùng or USD/tấn) to USD per the
    retail unit (liter or kg) actually sold."""
    world_unit = WORLD_PRODUCTS[world_code]["unit"]
    if world_unit == "USD/thung":
        return world_price_avg_usd_per_unit / LITERS_PER_BARREL
    if world_unit == "USD/tan":
        return world_price_avg_usd_per_unit / KG_PER_TON
    raise UnsupportedProductError(f"don't know how to convert world unit {world_unit!r}")


@dataclass
class GiaCoSoBreakdown:
    product_code: str
    as_of_date: date
    world_price_avg_usd_per_unit: float
    fx_rate_vnd_per_usd: float
    cif_vnd_per_unit: float
    import_tax_rate: float
    excise_tax_rate: float
    env_tax_vnd_per_unit: float
    vat_rate: float
    lumped_residual_vnd: float
    bog_net_vnd: float
    gia_co_so_vnd: float
    retail_unit: str  # "VND/lit" or "VND/kg"


def compute_gia_co_so(
    conn: sqlite3.Connection,
    *,
    world_price_avg_usd_per_unit: float,
    fx_rate_vnd_per_usd: float,
    product_code: str,
    as_of_date: date,
    lumped_residual_vnd: float,
    bog_net_vnd: float = 0.0,
) -> GiaCoSoBreakdown:
    """
    Compute giá cơ sở (VND/lít or VND/kg) for one retail product on one date.

    `world_price_avg_usd_per_unit` is the already-computed cycle average
    (reuse cycle_summary.avg_price_published or an equivalent
    window-averaged figure -- see calibrate.py's docstring for why this
    module does not recompute that window itself).

    `lumped_residual_vnd` stands in for (chi phí kinh doanh định mức + chi
    phí đưa về cảng + lợi nhuận định mức), per-unit. Pass 0.0 to get the
    "world-price-plus-tax-only" baseline (this is exactly what calibrate.py
    does to back out the implied residual from a real price).

    `bog_net_vnd` is the real, per-cycle Quỹ BOG effect (trích_lap minus
    chi_su_dung, đồng/lít or đồng/kg) -- kept separate from
    `lumped_residual_vnd` because it is known, published, per-cycle data
    (bog_actions), not a stable constant to fit. Defaults to 0.0 so existing
    callers that don't pass it behave exactly as before.

    Raises MissingConstantError (from src.pricing.constants) if a required
    tax constant has no row covering as_of_date -- callers must not catch
    this and silently substitute 0; a missing constant for a given date is
    a real gap in what this phase modeled (see Phase 2 report), not a
    harmless default.
    """
    if product_code not in RETAIL_PRODUCTS:
        raise UnsupportedProductError(f"unknown retail product_code {product_code!r}")
    info = RETAIL_PRODUCTS[product_code]
    world_code = info["world_reference"]

    usd_per_retail_unit = _world_unit_to_retail_unit(world_price_avg_usd_per_unit, world_code)
    cif_vnd_per_unit = usd_per_retail_unit * fx_rate_vnd_per_usd

    import_tax_rate = get_constant(conn, "import_tax_rate", product_code, as_of_date)
    vat_rate = get_constant(conn, "vat_rate", product_code, as_of_date)
    env_tax_vnd_per_unit = get_constant(conn, "env_tax_vnd", product_code, as_of_date)
    excise_tax_rate = (
        get_constant(conn, "excise_tax_rate", product_code, as_of_date) if is_gasoline(product_code) else 0.0
    )

    # Nghị định 95/2021 Điều 38a structure: import duty applies to the CIF
    # cost plus the lumped cost/profit residual plus the real BOG net effect;
    # TTĐB (gasoline only) applies multiplicatively on top of that; thuế
    # BVMT is a flat per-unit add-on; VAT applies last, to the whole thing.
    pre_excise = (cif_vnd_per_unit + lumped_residual_vnd + bog_net_vnd) * (1 + import_tax_rate)
    with_excise = pre_excise * (1 + excise_tax_rate)
    with_env = with_excise + env_tax_vnd_per_unit
    gia_co_so_vnd = with_env * (1 + vat_rate)

    return GiaCoSoBreakdown(
        product_code=product_code,
        as_of_date=as_of_date,
        world_price_avg_usd_per_unit=world_price_avg_usd_per_unit,
        fx_rate_vnd_per_usd=fx_rate_vnd_per_usd,
        cif_vnd_per_unit=cif_vnd_per_unit,
        import_tax_rate=import_tax_rate,
        excise_tax_rate=excise_tax_rate,
        env_tax_vnd_per_unit=env_tax_vnd_per_unit,
        vat_rate=vat_rate,
        lumped_residual_vnd=lumped_residual_vnd,
        bog_net_vnd=bog_net_vnd,
        gia_co_so_vnd=gia_co_so_vnd,
        retail_unit=info["unit"],
    )


def apply_vung_2_markup(vung1_price_vnd: float, markup_pct: float = 0.0) -> float:
    """
    Vùng 2 (remote/high-transport-cost pricing zone) distributors may add up
    to +2% on top of the published Vùng 1 ceiling, at their own discretion
    -- it is NOT centrally published, so it cannot be looked up from the
    constants table the way the other formula inputs are.

    This phase only has MOIT's own Vùng 1 published ceiling to validate
    against (see the Phase 2 report), so `markup_pct` defaults to 0.0 (no
    markup applied) -- a stub for a later phase to plug in this station's
    actual observed Vùng-2 markup once that data exists, rather than
    guessing a number now.
    """
    return vung1_price_vnd * (1 + markup_pct)
