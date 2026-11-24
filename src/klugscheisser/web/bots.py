"""Is a web request from a bot or a headless browser? Then a page view starts no paid update.

19.10.2026: a headless Chrome (our own UI probe) opened 80 person pages in 90 s, which started 84 Apify jobs
($0.34, 15-min queue). Real browsers behave as before; `WEB_REFRESH_FOR_BOTS=true` turns this check off.
Only the user agent and the `Sec-CH-UA` client hints are read (no IP lists to keep up to date).
"""

from __future__ import annotations

import re

from starlette.requests import Request

# HeadlessChrome (Puppeteer/Playwright default), automation tools, crawlers, HTTP libraries, link previews
_BOT_UA = re.compile(
    r"headless|phantomjs|playwright|puppeteer|selenium|webdriver|lighthouse|"
    r"bot\b|bot/|crawler|spider|slurp|crawl|"
    r"^curl/|^wget/|python-|^python|httpx|aiohttp|^go-http|^java/|okhttp|^node|axios|libwww|^ruby|"
    r"facebookexternalhit|embedly|preview|whatsapp|telegram|slack|discord|skype|"
    r"perplexity|chatgpt|gptbot|claude|anthropic|bytespider|ahrefs|semrush|mj12|petal",
    re.IGNORECASE,
)


def is_bot_user_agent(ua: str | None) -> bool:
    """True for an empty user agent, a headless browser or a known bot / tool; False for normal browsers."""
    if not ua or not ua.strip():
        return True
    return bool(_BOT_UA.search(ua))


def is_bot_request(request: Request) -> bool:
    h = request.headers
    if is_bot_user_agent(h.get("user-agent")):
        return True
    return "headless" in h.get("sec-ch-ua", "").lower()  # newer headless Chrome sends 'HeadlessChrome' here
