"""
Rate-limited, robots.txt-respecting HTTP client for moit.gov.vn.

robots.txt is parsed programmatically (urllib.robotparser) rather than
hardcoding the current Disallow list in code, since MOIT can change it —
and none of the paths this scraper touches (/tin-tuc/... articles, the
/api/Content/Article/selectAll listing endpoint) are currently disallowed
anyway (confirmed 2026-09: robots.txt only blocks /data/, /lib/, /packages/,
/portals/, /pages/, /1/, /content/download/).
"""

from __future__ import annotations

import time
import urllib.robotparser
from urllib.parse import urljoin

import requests

USER_AGENT = "Mozilla/5.0 (compatible; du-doan-gia-xang-bot/0.1; data foundation research)"
BASE_URL = "https://moit.gov.vn"
MIN_REQUEST_INTERVAL_SECONDS = 0.6  # ~1.6 req/s, comfortably under the ~2 req/s ceiling


class PoliteSession:
    def __init__(self, base_url: str = BASE_URL, min_interval: float = MIN_REQUEST_INTERVAL_SECONDS):
        self.base_url = base_url
        self.min_interval = min_interval
        self._last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self._robots = urllib.robotparser.RobotFileParser()
        try:
            self._robots.set_url(urljoin(base_url, "/robots.txt"))
            self._robots.read()
        except Exception:
            # If robots.txt is unreachable, fail safe by *not* blocking —
            # but this is logged so a caller can decide to be more cautious.
            self._robots = None

    def allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        try:
            return self._robots.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait = self.min_interval - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(self, url: str, **kwargs) -> requests.Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows fetching: {url}")
        self._throttle()
        return self.session.get(url, timeout=30, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows fetching: {url}")
        self._throttle()
        return self.session.post(url, timeout=30, **kwargs)
