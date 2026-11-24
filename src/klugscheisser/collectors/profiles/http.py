"""Polite HTTP for profile pages: identified User-Agent, robots.txt per host, Crawl-delay per host.

robots.txt is read once per host and client. 404 means "no rules" (allowed); 401/403, server errors and network
errors mean "not allowed" (conservative). A Crawl-delay above ``max_crawl_delay_s`` makes the host off-limits
instead of blocking the update job for minutes. The robots.txt request itself counts towards the Crawl-delay.

Wikimedia APIs (Wikidata, Wikipedia REST/Action API) are not pages: their robots.txt rules address crawlers,
API use is governed by the Wikimedia API etiquette (identified User-Agent, no parallel requests). They go
through ``get(..., check_robots=False)``.
"""

from __future__ import annotations

import time
import urllib.robotparser
from collections.abc import Callable
from urllib.parse import urlsplit

import httpx

USER_AGENT = "klugscheisser-athlete/0.1 (+https://github.com/andres-cerny/klugscheisser_athlete)"


class Disallowed(Exception):
    """robots.txt (or a too long Crawl-delay) forbids fetching this URL."""


class PoliteClient:
    def __init__(
        self,
        client: httpx.Client | None = None,
        min_delay_s: float = 1.0,
        max_crawl_delay_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.http = client or httpx.Client(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self.min_delay_s = min_delay_s
        self.max_crawl_delay_s = max_crawl_delay_s
        self.sleep = sleep
        self.clock = clock
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._delay: dict[str, float] = {}
        self._last: dict[str, float] = {}

    @staticmethod
    def _host(url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}"

    def _wait(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            remaining = self._delay.get(host, self.min_delay_s) - (self.clock() - last)
            if remaining > 0:
                self.sleep(remaining)
        self._last[host] = self.clock()

    def _parser(self, host: str) -> urllib.robotparser.RobotFileParser | None:
        """Parsed robots.txt; None = host not allowed at all."""
        if host in self._robots:
            return self._robots[host]
        rp: urllib.robotparser.RobotFileParser | None = urllib.robotparser.RobotFileParser()
        self._wait(host)
        try:
            r = self.http.get(f"{host}/robots.txt")
            if r.status_code == 404:
                rp.parse([])
            elif r.status_code == 200:
                rp.parse(r.text.splitlines())
            else:
                rp = None
        except httpx.HTTPError:
            rp = None
        if rp is not None:
            delay = rp.crawl_delay(USER_AGENT)
            if delay is not None:
                self._delay[host] = max(float(delay), self.min_delay_s)
        self._robots[host] = rp
        return rp

    def crawl_delay(self, url: str) -> float:
        return self._delay.get(self._host(url), self.min_delay_s)

    def allowed(self, url: str) -> tuple[bool, str | None]:
        host = self._host(url)
        rp = self._parser(host)
        if rp is None:
            return False, "robots.txt nicht lesbar"
        if self.crawl_delay(url) > self.max_crawl_delay_s:
            return False, f"Crawl-delay {self.crawl_delay(url):.0f}s zu hoch"
        if not rp.can_fetch(USER_AGENT, url):
            return False, "robots.txt verbietet"
        return True, None

    def get(self, url: str, check_robots: bool = True, **kwargs) -> httpx.Response:
        if check_robots:
            ok, why = self.allowed(url)
            if not ok:
                raise Disallowed(f"{url}: {why}")
        self._wait(self._host(url))
        return self.http.get(url, **kwargs)
