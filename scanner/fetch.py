"""Polite HTTP access: conditional GETs, bounded retries, robots.txt respect."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {500, 502, 503, 504, 522, 524}

# Values that churn on every render (CSRF tokens, build ids, cache-busting
# query strings, timestamps). They are scrubbed before hashing so "did this
# page really change?" does not answer "yes" on every single request.
_VOLATILE = (
    re.compile(r'(?i)(csrf|nonce|token|_build|buildid|requestid|sessionid)"?\s*[:=]\s*"?[\w\-]{6,}'),
    re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"(?i)\b[0-9a-f]{32,}\b"),
    re.compile(r"\?v=\d+"),
    re.compile(r"\b\d{10,13}\b"),
)


def content_hash(text: str) -> str:
    normalized = text or ""
    for pattern in _VOLATILE:
        normalized = pattern.sub("", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return hashlib.sha256(normalized.encode("utf-8", "replace")).hexdigest()


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    text: str = ""
    headers: dict = field(default_factory=dict)
    error: str | None = None
    not_modified: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None and (self.not_modified or self.status == 200)

    @property
    def etag(self) -> str:
        return self.headers.get("ETag", "")

    @property
    def last_modified(self) -> str:
        return self.headers.get("Last-Modified", "")

    @property
    def hash(self) -> str:
        return content_hash(self.text) if self.text else ""


class Fetcher:
    def __init__(self, cfg, session: requests.Session | None = None):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    # -- robots ---------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        parser = self._robots[origin]
        if parser is None:  # unreachable robots.txt -> proceed, as crawlers do
            return True
        return parser.can_fetch(self.cfg.user_agent, url)

    def _load_robots(self, origin: str):
        try:
            response = self.session.get(f"{origin}/robots.txt", timeout=self.cfg.timeout)
        except requests.RequestException as exc:
            log.warning("could not read robots.txt for %s: %s", origin, exc)
            return None
        if response.status_code >= 400:
            log.info("no usable robots.txt at %s (HTTP %s)", origin, response.status_code)
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    # -- fetching -------------------------------------------------------
    def get(self, url: str, etag: str = "", last_modified: str = "") -> FetchResult:
        if not self.allowed(url):
            return FetchResult(url=url, error="disallowed by robots.txt")

        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        last_error = "unknown error"
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                response = self.session.get(url, headers=headers, timeout=self.cfg.timeout)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.warning("fetch %s failed (attempt %s/%s): %s",
                            url, attempt, self.cfg.max_retries, last_error)
            else:
                if response.status_code == 304:
                    return FetchResult(url=url, status=304, headers=dict(response.headers),
                                       not_modified=True)
                if response.status_code in RETRY_STATUSES:
                    last_error = f"HTTP {response.status_code}"
                    log.warning("fetch %s returned %s (attempt %s/%s)",
                                url, response.status_code, attempt, self.cfg.max_retries)
                else:
                    # 2xx and 4xx alike are final: retrying a 403/404 just
                    # annoys the server and delays the alert we owe the user.
                    result = FetchResult(url=url, status=response.status_code,
                                         text=response.text, headers=dict(response.headers))
                    if response.status_code != 200:
                        result.error = f"HTTP {response.status_code}"
                    return result

            if attempt < self.cfg.max_retries:
                time.sleep(2 ** attempt + random.uniform(0, 1))

        return FetchResult(url=url, error=last_error)
