"""Fetch a bulletin URL, archive the raw bytes to disk, return path + metadata."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.scraper.http import PoliteSession

DEFAULT_RAW_HTML_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "raw_html"


@dataclass
class FetchResult:
    url: str
    http_status: int | None
    raw_html_path: str
    fetched_at: str
    html: str | None
    error: str | None = None


def _slug_hash(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def _date_from_url(url: str) -> str:
    m = re.search(r"ngay-(\d{1,2})-(\d{1,2})-(\d{4})\.html", url)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return "unknown-date"


def fetch_and_archive(
    session: PoliteSession,
    url: str,
    raw_html_dir: Path = DEFAULT_RAW_HTML_DIR,
) -> FetchResult:
    """
    Fetch `url` and write its raw bytes to disk BEFORE any parsing happens,
    so parsing can be re-run later (e.g. after a parser bugfix) without
    re-hitting the network. A sidecar .json carries fetch metadata
    (url, timestamp, http status) next to the .html file.
    """
    raw_html_dir.mkdir(parents=True, exist_ok=True)
    fetched_at = datetime.now(timezone.utc).isoformat()
    base_name = f"{_date_from_url(url)}_{_slug_hash(url)}"
    html_path = raw_html_dir / f"{base_name}.html"
    meta_path = raw_html_dir / f"{base_name}.json"

    try:
        resp = session.get(url)
    except Exception as e:
        meta_path.write_text(
            json.dumps({"url": url, "fetched_at": fetched_at, "http_status": None, "error": str(e)}, indent=2),
            encoding="utf-8",
        )
        return FetchResult(url=url, http_status=None, raw_html_path=str(html_path), fetched_at=fetched_at, html=None, error=str(e))

    html_path.write_bytes(resp.content)
    meta_path.write_text(
        json.dumps(
            {"url": url, "fetched_at": fetched_at, "http_status": resp.status_code, "content_length": len(resp.content)},
            indent=2,
        ),
        encoding="utf-8",
    )

    if resp.status_code != 200:
        return FetchResult(
            url=url, http_status=resp.status_code, raw_html_path=str(html_path),
            fetched_at=fetched_at, html=None, error=f"HTTP {resp.status_code}",
        )

    return FetchResult(
        url=url, http_status=resp.status_code, raw_html_path=str(html_path),
        fetched_at=fetched_at, html=resp.text, error=None,
    )
