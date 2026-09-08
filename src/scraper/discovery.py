"""
Discovery: find candidate bulletin URLs before fetching+parsing them.

Two tiers, tried in order. Tier 1 is a real listing API discovered by
reverse-engineering the site's CMS (VHV), not a documented endpoint — so it
is wrapped defensively and Tier 2 exists as a guaranteed-to-work fallback
if Tier 1 ever breaks (site redesign, endpoint renamed, etc).
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from src.scraper.http import BASE_URL, PoliteSession

CATEGORY_LISTING_API = f"{BASE_URL}/api/Content/Article/selectAll"
THI_TRUONG_TRONG_NUOC_CATEGORY_ID = "5238202"

# Both known title-slug variants (per the research brief). Treated as
# not-fully-enumerable -- Tier 1 doesn't need this list (it matches on
# keyword substring), but Tier 2's brute-force prober does.
KNOWN_SLUG_VARIANTS = [
    "mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau",
    "thong-tin-ve-viec-dieu-hanh-gia-xang-dau",
]

# Confirmed via the 3/9/2026 bulletin (found only by following a related-
# articles link on minhbach.moit.gov.vn, since neither Tier 1's category nor
# the old brute-force pattern below caught it): MOIT moved this bulletin
# under a NEW "thong-bao" path segment
# (/tin-tuc/thong-bao/mot-so-thong-tin-ve-viec-dieu-hanh-gia-xang-dau-ngay-3-9.html)
# -- previously always bare /tin-tuc/{slug}-... -- and dropped the year
# suffix entirely for it (:"ngay-3-9", not "ngay-3-9-2026"). Both are
# real, observed site-structure changes, not guesses; kept as extra
# candidate variants (not replacements) since older bulletins still use the
# old bare-path + explicit-year form.
KNOWN_CATEGORY_PREFIXES = ["", "thong-bao/"]

BULLETIN_URL_KEYWORD = "dieu-hanh-gia-xang-dau"


@dataclass
class DiscoveredBulletin:
    url: str
    publish_time: datetime | None
    title: str | None
    discovery_method: str


def _normalize_article_url(rewrite_url: str) -> str:
    rewrite_url = rewrite_url.lstrip("/")
    return f"{BASE_URL}/{rewrite_url}"


def discover_via_category_api(
    session: PoliteSession,
    category_id: str = THI_TRUONG_TRONG_NUOC_CATEGORY_ID,
    items_per_page: int = 100,
    max_pages: int = 50,
) -> list[DiscoveredBulletin]:
    """
    Tier 1 (primary): page through the "Thi truong trong nuoc" category via
    the site's own AJAX listing API and keep articles whose rewriteURL
    contains the fuel-bulletin keyword.

    This endpoint was found by decoding the base64-encoded widget config
    embedded in https://moit.gov.vn/tin-tuc/thi-truong-trong-nuoc (a
    VHV-CMS "Content.Listing" module) to recover its `service` field
    ("Content.Article.selectAll") and `categoryId`, then confirming by
    direct POST that itemsPerPage/pageNo/orderBy are real, working
    pagination params -- unlike the page's own `?page=N` query string,
    which returns HTTP 200 but silently ignores N. It is intentionally
    NOT documented anywhere on the site; if MOIT's CMS is ever swapped out
    this will start returning errors/garbage, hence the try/except here
    handing control back to the Tier 2 fallback.
    """
    found: list[DiscoveredBulletin] = []
    page_no = 1
    while page_no <= max_pages:
        try:
            resp = session.post(
                CATEGORY_LISTING_API,
                data={
                    "categoryId": category_id,
                    "pageNo": str(page_no),
                    "itemsPerPage": str(items_per_page),
                    "orderBy": "publishTime DESC",
                    "type": "Article.News",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            raise RuntimeError(f"Tier-1 category API failed on page {page_no}: {e}") from e

        items = payload.get("items", {})
        if not items:
            break

        for item in items.values():
            rewrite_url = item.get("rewriteURL", "")
            if BULLETIN_URL_KEYWORD not in rewrite_url:
                continue
            pt = item.get("publishTime")
            publish_dt = datetime.fromtimestamp(pt, tz=timezone.utc) if pt else None
            found.append(
                DiscoveredBulletin(
                    url=_normalize_article_url(rewrite_url),
                    publish_time=publish_dt,
                    title=item.get("title"),
                    discovery_method="category_api",
                )
            )

        total_items = payload.get("totalItems", 0)
        if page_no * items_per_page >= total_items:
            break
        page_no += 1

    # de-dup (same URL can theoretically appear across pages if the
    # underlying data shifts between requests)
    seen = set()
    deduped = []
    for b in found:
        if b.url in seen:
            continue
        seen.add(b.url)
        deduped.append(b)
    return deduped


def _thursdays_between(start: date, end: date) -> list[date]:
    d = start
    while d.weekday() != 3:  # Monday=0 ... Thursday=3
        d += timedelta(days=1)
    out = []
    while d <= end:
        out.append(d)
        d += timedelta(days=7)
    return out


def _looks_like_bulletin_page(html: str) -> bool:
    text = unicodedata.normalize("NFC", html)
    return "Giá thành phẩm xăng dầu thế giới" in text and "Quỹ bình ổn giá xăng dầu" in text


def discover_via_brute_force(
    session: PoliteSession,
    start: date,
    end: date,
    slug_variants: list[str] = KNOWN_SLUG_VARIANTS,
    category_prefixes: list[str] = KNOWN_CATEGORY_PREFIXES,
    extra_delay: float = 0.0,
) -> list[DiscoveredBulletin]:
    """
    Tier 2 (fallback): probe candidate Thursday-dated URLs directly.

    Kept as a clearly separate function/path (never silently blended into
    Tier 1's results) so a backfill report can state plainly which tier
    found which bulletins, per the brief's instruction not to pad Tier-1
    results with brute-force hits without saying so.

    Slower and uglier than Tier 1 by design -- it exists purely as a safety
    net in case the category-API endpoint ever stops working, and tries
    both zero-padded and non-padded day/month for both known slugs (the
    site is inconsistent about padding, e.g. "ngay-09-7-2026" vs
    "ngay-9-4-2026"), both known category prefixes (see
    KNOWN_CATEGORY_PREFIXES's docstring), and both with/without the year
    suffix -- MOIT has published at least one recent bulletin
    ("...-ngay-3-9.html") with no year at all.
    """
    found: list[DiscoveredBulletin] = []
    for d in _thursdays_between(start, end):
        candidates = set()
        for prefix in category_prefixes:
            for slug in slug_variants:
                for day_fmt in (f"{d.day:02d}", str(d.day)):
                    for month_fmt in (f"{d.month:02d}", str(d.month)):
                        base = f"{BASE_URL}/tin-tuc/{prefix}{slug}-ngay-{day_fmt}-{month_fmt}"
                        candidates.add(f"{base}-{d.year}.html")
                        candidates.add(f"{base}.html")

        for url in candidates:
            try:
                resp = session.get(url)
            except PermissionError:
                continue
            except Exception:
                continue
            if extra_delay:
                time.sleep(extra_delay)
            if resp.status_code == 200 and _looks_like_bulletin_page(resp.text):
                found.append(
                    DiscoveredBulletin(
                        url=url,
                        publish_time=datetime(d.year, d.month, d.day, tzinfo=timezone.utc),
                        title=None,
                        discovery_method="brute_force",
                    )
                )
    return found


def discover_all(
    session: PoliteSession,
    brute_force_start: date | None = None,
    brute_force_end: date | None = None,
) -> tuple[list[DiscoveredBulletin], dict]:
    """
    Run Tier 1, then Tier 2 only over any gaps Tier 1 didn't cover (if a
    date range is given). Returns (bulletins, stats) where stats records
    which tier contributed what, for an honest backfill report.
    """
    stats = {"tier1_found": 0, "tier2_found": 0, "tier2_probed": 0}
    tier1 = []
    try:
        tier1 = discover_via_category_api(session)
        stats["tier1_found"] = len(tier1)
    except Exception as e:
        stats["tier1_error"] = str(e)

    all_bulletins = list(tier1)

    if brute_force_start and brute_force_end:
        known_dates = {b.publish_time.date() for b in tier1 if b.publish_time}
        gap_thursdays = [d for d in _thursdays_between(brute_force_start, brute_force_end) if d not in known_dates]
        if gap_thursdays:
            gap_start, gap_end = min(gap_thursdays), max(gap_thursdays)
            tier2 = discover_via_brute_force(session, gap_start, gap_end)
            stats["tier2_probed"] = len(gap_thursdays)
            stats["tier2_found"] = len(tier2)
            existing_urls = {b.url for b in all_bulletins}
            all_bulletins.extend(b for b in tier2 if b.url not in existing_urls)

    return all_bulletins, stats
