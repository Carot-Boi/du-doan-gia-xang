"""
Parser for a single MOIT fuel-price bulletin page.

Design notes (the WHY, not the WHAT):

- All page text is run through unicodedata.normalize('NFC', ...) before any
  regex/substring matching. MOIT's CMS content was evidently assembled from
  mixed sources: some paragraphs use precomposed Vietnamese accents (NFC,
  e.g. U+1EE9 for "ứ"), others use decomposed combining-mark sequences (NFD,
  e.g. "a" + U+0323 COMBINING DOT BELOW for "ậ"). A literal Vietnamese
  string in this file's source code is NFC, so an un-normalized search
  against NFD page text silently fails to match — this was discovered
  empirically while parsing the 09/7/2026 bulletin fixture (the "Trích lập
  Quỹ bình ổn giá xăng dầu" heading uses NFD, the surrounding paragraphs
  use NFC) and is not something the original research pass flagged.

- Bullet-list lines ("- Xăng E5RON92: ...") must be matched with a
  line-start anchor (^ with re.MULTILINE), not a bare "-" search, because
  product names themselves contain hyphens (e.g. "E10RON95-III"). A naive
  "-" bullet regex greedily matches from inside the product name of the
  *previous* line instead of the next real bullet.

- Numeric cells in the daily world-price table are parsed by stripping the
  separator character and treating the digit string positionally, rather
  than assuming a decimal-vs-thousands convention. Both the correct
  convention (comma-decimal for USD prices, dot-thousands for VCB rates)
  and the observed bug (some rows swap them) place exactly three digits
  after the separator, so digit-extraction recovers the right value either
  way without needing to detect which convention a given row used.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup

from src.parser.products import (
    BOG_PRODUCTS,
    RETAIL_PRODUCTS,
    WORLD_PRODUCTS,
    WORLD_TABLE_COLUMN_ORDER,
)

BLANK_CELL_VALUES = {"", "-", "‐", "—", "−", "–"}


class ParseWarning(str):
    """Marker type so callers can distinguish soft warnings from hard data."""


@dataclass
class ParsedBulletin:
    url: str
    title: str | None = None
    title_variant: str | None = None
    bulletin_date: date | None = None
    effective_at: datetime | None = None
    world_price_daily: list[dict] = field(default_factory=list)
    cycle_summary: list[dict] = field(default_factory=list)
    retail_prices: list[dict] = field(default_factory=list)
    bog_actions: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    parse_status: str = "ok"  # ok | partial | failed

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _get_text(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    return soup


def normalize_price_cell(raw: str) -> float | None:
    """Parse a world-price table cell (X92/X95/DauHoa/DO/FO) into USD, 3dp."""
    raw = (raw or "").strip()
    if raw in BLANK_CELL_VALUES or raw == "":
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if digits == "":
        return None
    return int(digits) / 1000.0


def normalize_vcb_cell(raw: str) -> int | None:
    """Parse a VCB exchange-rate cell into whole VND."""
    raw = (raw or "").strip()
    if raw in BLANK_CELL_VALUES or raw == "":
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if digits == "":
        return None
    return int(digits)


def normalize_vnd_amount(raw: str) -> int | None:
    """Parse a VND prose amount (e.g. '19.191', '539') -> whole dong."""
    raw = (raw or "").strip()
    if raw in BLANK_CELL_VALUES or raw == "":
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if digits == "":
        return None
    return int(digits)


def normalize_decimal_comma(raw: str) -> float | None:
    """Parse a prose number using Vietnamese comma-decimal (e.g. '94,948')."""
    raw = (raw or "").strip()
    if raw == "":
        return None
    return float(raw.replace(".", "").replace(",", ".")) if "," in raw else float(raw.replace(".", ""))


def parse_quote_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", raw)
    if not m:
        return None
    d, mo, y = m.groups()
    y = int(y)
    if y < 100:
        y += 2000
    try:
        return date(y, int(mo), int(d))
    except ValueError:
        return None


def parse_world_price_table(soup: BeautifulSoup, bulletin: ParsedBulletin) -> None:
    tables = soup.find_all("table")
    if not tables:
        bulletin.warn("no <table> found for world price history")
        return
    table = tables[0]
    rows = table.find_all("tr")
    if not rows:
        bulletin.warn("world price table has no rows")
        return

    header_cells = [
        unicodedata.normalize("NFC", c.get_text(strip=True)) for c in rows[0].find_all(["td", "th"])
    ]
    expected = ["TT", "Ngày"] + [WORLD_PRODUCTS[c]["label"] for c in WORLD_TABLE_COLUMN_ORDER]
    expected_width = len(expected)

    # The first <table> on the page is not always the world-price history
    # table: some bulletins instead lead with (or only contain) a BOG
    # trich-lap/chi-su-dung HISTORY table ("TT | Ky dieu hanh | Mat hang",
    # 5 products x 2 (trich/chi) = 12 cells/row). That table's cells are
    # numeric-looking and would happily (mis)parse positionally as world
    # prices if we let them, corrupting the DB with values like RON95=4.0.
    # Reject on a structural mismatch (wrong header width or first two
    # labels wrong) rather than continuing "defensively" - a soft content
    # mismatch (label drift) still proceeds with a warning, since that is
    # not evidence of a different table entirely.
    header_shape_ok = (
        len(header_cells) == expected_width
        and header_cells[0] == "TT"
        and header_cells[1] in ("Ngày", "Ngay")
    )
    if not header_shape_ok:
        bulletin.warn(
            f"first <table> on page is not the world-price history table "
            f"(header={header_cells!r}); skipping world-price extraction for this bulletin"
        )
        return
    if header_cells != expected:
        bulletin.warn(f"world price table header labels drifted from expected: {header_cells!r}")
        # shape matches (same column count / TT+Ngay lead) so still proceed positionally

    for tr in rows[1:]:
        cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) != expected_width:
            if any(c for c in cells):
                bulletin.warn(f"skipping malformed world price row (wrong cell count {len(cells)}): {cells!r}")
            continue

        row_number_raw, date_raw = cells[0], cells[1]
        quote_date = parse_quote_date(date_raw)
        if quote_date is None:
            bulletin.warn(f"skipping world price row with unparseable date: {date_raw!r}")
            continue

        for i, product_code in enumerate(WORLD_TABLE_COLUMN_ORDER):
            cell_raw = cells[2 + i]
            if product_code in ("VCB_BUY", "VCB_SELL"):
                value = normalize_vcb_cell(cell_raw)
                unit = "VND/USD"
            else:
                value = normalize_price_cell(cell_raw)
                unit = "USD/thung" if product_code != "FO_180CST_3_5S" else "USD/tan"
            if value is None:
                continue  # weekend/holiday/discontinued-product blank cell, not an error
            bulletin.world_price_daily.append(
                {
                    "product_code": product_code,
                    "quote_date": quote_date,
                    "price": value,
                    "unit": unit,
                    "source_row_number": row_number_raw,
                }
            )


def parse_bulletin_date(text: str, url: str) -> date | None:
    m = re.search(r"ngày\s*(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if m:
        d, mo, y = (int(x) for x in m.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    m2 = re.search(r"ngay-(\d{1,2})-(\d{1,2})-(\d{4})", url)
    if m2:
        d, mo, y = (int(x) for x in m2.groups())
        try:
            return date(y, mo, d)
        except ValueError:
            pass
    return None


def parse_effective_at(text: str, fallback_date: date | None) -> datetime | None:
    m = re.search(
        r"(\d{1,2})\s*giờ\s*(\d{2})['’]?\s*ng[aà]y\s*(\d{1,2})\s*th[aá]ng\s*(\d{1,2})\s*n[aă]m\s*(\d{4})",
        text,
    )
    if not m:
        return None
    hh, mm, d, mo, y = (int(x) for x in m.groups())
    try:
        return datetime(y, mo, d, hh, mm)
    except ValueError:
        return None


_CYCLE_SUMMARY_SEGMENT_RE = re.compile(
    r"(?P<value>[\d.,]+)\s*(?P<unit>USD/th[uù]ng|USD/t[aấ]n)\s*(?P<phrase>[^;()]+?)\s*"
    r"\((?P<changeword>t[aă]ng|gi[aả]m)\s*(?P<delta>[\d.,]+)\s*(?:USD/th[uù]ng|USD/t[aấ]n)[^,]*,"
    r"\s*tương đương\s*(?P<pctword>t[aă]ng|gi[aả]m)\s*(?P<pct>[\d.,]+)%\)",
    re.I,
)


def _classify_world_phrase(phrase: str) -> str | None:
    p = phrase.lower()
    if "ron92" in p.replace(" ", ""):
        return "RON92"
    if "ron95" in p.replace(" ", ""):
        return "RON95"
    if "điêzen" in p or "diesel" in p:
        return "DIESEL_0_05S"
    if "mazut" in p or "madút" in p or "madut" in p:
        return "FO_180CST_3_5S"
    if "hỏa" in p or "hoả" in p or "hoa" in p:
        return "KEROSENE"
    return None


def parse_cycle_summary(text: str, bulletin: ParsedBulletin) -> tuple[date | None, date | None]:
    m = re.search(
        r"bình quân giữa kỳ điều hành giá ngày\s*(\d{1,2}/\d{1,2}/\d{4})\s*"
        r"và kỳ điều hành ngày\s*(\d{1,2}/\d{1,2}/\d{4})\s*là:\s*(.+?)\.\s*(?:\n|[A-ZĐ])",
        text,
        re.S,
    )
    if not m:
        bulletin.warn("cycle-summary sentence not found (prose wording may have changed)")
        return None, None

    def _to_date(s: str) -> date:
        d, mo, y = (int(x) for x in s.split("/"))
        return date(y, mo, d)

    prev_date, this_date = _to_date(m.group(1)), _to_date(m.group(2))
    segment_text = m.group(3)

    for sm in _CYCLE_SUMMARY_SEGMENT_RE.finditer(segment_text):
        gd = sm.groupdict()
        product_code = _classify_world_phrase(gd["phrase"])
        if product_code is None:
            bulletin.warn(f"could not classify cycle-summary product phrase: {gd['phrase']!r}")
            continue
        avg_price = normalize_decimal_comma(gd["value"])
        delta = normalize_decimal_comma(gd["delta"])
        pct = normalize_decimal_comma(gd["pct"])
        if gd["changeword"].lower().startswith(("gi",)):
            delta = -abs(delta) if delta is not None else None
        if gd["pctword"].lower().startswith(("gi",)):
            pct = -abs(pct) if pct is not None else None
        bulletin.cycle_summary.append(
            {
                "product_code": product_code,
                "prev_cycle_date": prev_date,
                "this_cycle_date": this_date,
                "avg_price_published": avg_price,
                "delta_published": delta,
                "pct_change_published": pct,
                "validation_ok": None,  # filled in by validate_cycle_averages()
            }
        )
    return prev_date, this_date


_RETAIL_LINE_RE = re.compile(
    r"^-\s*([^:]+):\s*không cao hơn\s*([\d.,]+)\s*đồng/(lít|kg)\s*"
    r"\((t[aă]ng|gi[aả]m)\s*([\d.,]+)\s*đồng/(?:lít|kg)",
    re.I | re.M,
)


def _resolve_retail_product(name_text: str) -> str | None:
    name_norm = re.sub(r"\s+", " ", name_text).strip()
    # Longest-alias-first so "E10RON95-III" is matched before a bare "RON95-III".
    candidates = sorted(
        ((code, alias) for code, info in RETAIL_PRODUCTS.items() for alias in info["aliases"]),
        key=lambda t: -len(t[1]),
    )
    for code, alias in candidates:
        if alias.lower() in name_norm.lower():
            return code
    return None


def parse_retail_prices(text: str, bulletin: ParsedBulletin, effective_at: datetime | None) -> None:
    for m in _RETAIL_LINE_RE.finditer(text):
        name_text, price_raw, unit, changeword, delta_raw = m.groups()
        product_code = _resolve_retail_product(name_text)
        if product_code is None:
            bulletin.warn(f"could not resolve retail product name: {name_text!r}")
            continue
        price_vnd = normalize_vnd_amount(price_raw)
        delta_vnd = normalize_vnd_amount(delta_raw)
        if changeword.lower().startswith("gi") and delta_vnd is not None:
            delta_vnd = -delta_vnd
        bulletin.retail_prices.append(
            {
                "product_code": product_code,
                "price_vnd": price_vnd,
                "delta_vnd": delta_vnd,
                "unit": f"VND/{unit}",
                "effective_at": effective_at,
            }
        )


_BOG_LINE_RE = re.compile(r"^-\s*([^:]+):\s*([\d.,]+)\s*đồng/(lít|kg)", re.M)


def _resolve_bog_product(name_text: str) -> str | None:
    name_norm = re.sub(r"\s+", " ", name_text).strip().lower()
    for code, info in BOG_PRODUCTS.items():
        for alias in info["aliases"]:
            if alias.lower() in name_norm:
                return code
    return None


def parse_bog_actions(text: str, bulletin: ParsedBulletin) -> None:
    idx_trich = text.find("Trích lập Quỹ bình ổn giá xăng dầu")
    idx_chi = text.find("Chi sử dụng Quỹ bình ổn giá xăng dầu")
    if idx_trich == -1 or idx_chi == -1 or idx_chi <= idx_trich:
        bulletin.warn("could not locate BOG (Quỹ bình ổn giá) trích lập/chi sử dụng sections")
        return
    idx_end = text.find("Giá bán xăng dầu", idx_chi)
    if idx_end == -1:
        idx_end = idx_chi + 1000  # generous bound; defensive against heading drift

    trich_map = {}
    for name_text, amount_raw, unit in _BOG_LINE_RE.findall(text[idx_trich:idx_chi]):
        code = _resolve_bog_product(name_text)
        if code is None:
            bulletin.warn(f"could not resolve BOG (trích lập) product: {name_text!r}")
            continue
        trich_map[code] = (normalize_vnd_amount(amount_raw), f"VND/{unit}")

    chi_map = {}
    for name_text, amount_raw, unit in _BOG_LINE_RE.findall(text[idx_chi:idx_end]):
        code = _resolve_bog_product(name_text)
        if code is None:
            bulletin.warn(f"could not resolve BOG (chi sử dụng) product: {name_text!r}")
            continue
        chi_map[code] = (normalize_vnd_amount(amount_raw), f"VND/{unit}")

    for code in set(trich_map) | set(chi_map):
        trich_val, trich_unit = trich_map.get(code, (None, None))
        chi_val, chi_unit = chi_map.get(code, (None, None))
        bulletin.bog_actions.append(
            {
                "product_code": code,
                "trich_lap_vnd": trich_val,
                "chi_su_dung_vnd": chi_val,
                "unit": trich_unit or chi_unit,
            }
        )


def validate_cycle_averages(bulletin: ParsedBulletin, prev_date: date | None, this_date: date | None) -> None:
    """
    Cross-check MOIT's published cycle-average prose against the daily
    table in the same bulletin.

    Empirically reproduced rule (verified exactly, to 3 decimals, against
    the 09/7/2026 bulletin for RON92/RON95/DO): average the non-blank daily
    quotes for dates in [prev_date, this_date) — i.e. the PREVIOUS cycle's
    own date is INCLUDED and the current cycle's date is excluded (quotes
    run through the day before this bulletin). This differs subtly from a
    plausible-sounding but incorrect description of "strictly between both
    dates" (excluding prev_date too) — that version does NOT reproduce the
    published numbers; including prev_date does, exactly.

    Mismatches are recorded, never raised — a validation failure is useful
    signal (methodology may have changed for a given cycle) rather than a
    bug to crash the pipeline over.
    """
    if prev_date is None or this_date is None:
        return
    by_product: dict[str, list[tuple[date, float]]] = {}
    for row in bulletin.world_price_daily:
        by_product.setdefault(row["product_code"], []).append((row["quote_date"], row["price"]))

    for entry in bulletin.cycle_summary:
        code = entry["product_code"]
        quotes = by_product.get(code, [])
        window = [p for d, p in quotes if prev_date <= d < this_date]
        if not window:
            entry["validation_ok"] = None  # not enough data in this bulletin's own table to check
            entry["validation_note"] = "no daily quotes in window within this bulletin's table"
            continue
        computed_avg = sum(window) / len(window)
        published = entry["avg_price_published"]
        ok = published is not None and abs(computed_avg - published) < 0.01
        entry["validation_ok"] = ok
        entry["computed_avg"] = round(computed_avg, 3)
        if not ok:
            bulletin.warn(
                f"cycle-average validation FAILED for {code}: "
                f"published={published} computed={round(computed_avg, 3)} "
                f"(n={len(window)} quotes in [{prev_date}, {this_date}))"
            )


def detect_title_variant(url: str) -> str:
    if "mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau" in url:
        return "mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau"
    if "thong-tin-ve-viec-dieu-hanh-gia-xang-dau" in url:
        return "thong-tin-ve-viec-dieu-hanh-gia-xang-dau"
    return "unknown"


def parse_bulletin(html: str, url: str) -> ParsedBulletin:
    bulletin = ParsedBulletin(url=url, title_variant=detect_title_variant(url))
    try:
        soup = _get_text(html)
    except Exception as e:  # pragma: no cover - defensive
        bulletin.parse_status = "failed"
        bulletin.warn(f"failed to parse HTML: {e}")
        return bulletin

    text = unicodedata.normalize("NFC", soup.get_text("\n"))

    title_el = soup.find("h1") or soup.find("title")
    bulletin.title = title_el.get_text(strip=True) if title_el else None

    bulletin.bulletin_date = parse_bulletin_date(text, url)
    if bulletin.bulletin_date is None:
        bulletin.warn("could not determine bulletin date from prose or URL")

    bulletin.effective_at = parse_effective_at(text, bulletin.bulletin_date)

    parse_world_price_table(soup, bulletin)
    prev_date, this_date = parse_cycle_summary(text, bulletin)
    parse_retail_prices(text, bulletin, bulletin.effective_at)
    parse_bog_actions(text, bulletin)
    validate_cycle_averages(bulletin, prev_date, this_date)

    if not bulletin.world_price_daily and not bulletin.cycle_summary:
        bulletin.parse_status = "failed"
    elif bulletin.warnings:
        bulletin.parse_status = "partial"
    else:
        bulletin.parse_status = "ok"

    return bulletin
