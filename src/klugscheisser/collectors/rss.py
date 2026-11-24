"""News via RSS/Atom (SRF, Blick, 20 Minuten, Watson, NZZ). Only women's ice hockey items are kept; for those
the public article page is fetched once for an excerpt, but only for feeds with ``fetch_article: true``
(robots.txt and the outlet's terms allow it; see docs/sources.md, "News feeds"). Otherwise the feed teaser is used.

Excerpt extraction works per outlet without per-outlet code: JSON-LD ``articleBody`` first (Watson and many
CMSs), then SRF's article container, then the paragraphs of the page's ``<article>`` (Blick), then all paragraphs.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import httpx
import yaml

from klugscheisser.interfaces import FeedConfig

USER_AGENT = "klugscheisser-athlete/0.1 (+https://github.com/andres-cerny/klugscheisser_athlete)"
EXCERPT_MAX = 4000

FEEDS_SEED = Path("seeds/feeds.yaml")


def load_feeds(path: Path = FEEDS_SEED) -> list[FeedConfig]:
    return [FeedConfig(**entry) for entry in yaml.safe_load(path.read_text()) or []]


DEFAULT_FEEDS = load_feeds()

# Women's terms as used by SRF, Blick, 20 Minuten, Watson and NZZ.
WOMEN = re.compile(
    r"(frauen-eishockey|frauen-nationalteam|schweizerinnen|frauen-nati|nationalteam der frauen|"
    r"women'?s league|women'?s hockey|frauen-wm|pwhl|sdhl|eishockey-frauen|frauen-hockey|hockey-frauen|"
    r"hockey-nati der frauen|hockeyanerin|nati-spielerin|nationalspielerin|eishockeyspielerin|"
    r"spielerin|torhüterin|stürmerin|verteidigerin|frauenteam|frauen-team|deb-frauen|"
    # v5: French, Italian, Swedish, Finnish, Czech, Danish, Norwegian, Polish, English
    r"hockey féminin|hockey feminin|féminine|joueuse|hockeyeuse|gardienne|attaquante|suissesses|"
    r"hockey femminile|nazionale femminile|giocatrice|rossocrociate|"
    r"damkronorna|damhockey|damlandslaget|dam-vm|"
    r"naisleijonat|naisten|"
    r"hokejistky|hokejistka|ženy|ženská|ženský|"
    r"kvindelandsholdet|kvinder|kvinnelandslaget|kvinner|"
    r"reprezentantki|hokeistki|"
    r"women'?s|female)",
    re.IGNORECASE,
)
# Ice hockey context: domain/sport.py. Broad sport feeds also carry women's football, skiing etc., so a women's
# term alone is not enough, and the URL section only counts when the text names no sport (outlets file women's
# football stories under /eishockey/).
_BOILERPLATE = re.compile(
    r"^(SRF-Livestream|Einige Elemente|Bei Google Play|In der App|Header|\d{2}\.\d{2}\.\d{4}, \d{2}:\d{2}$|"
    r"Publiziert|Aktualisiert|Das könnte dich auch interessieren|Mehr zum Thema|Hol dir jetzt|"
    r"Wir schliessen in Liveticker|Du hast einen Fehler gefunden|Teilen$|Mehr anzeigen|\(Auf https?://\S+ lesen\))",
    re.IGNORECASE,
)
_ARTICLE_TYPES = {"NewsArticle", "Article", "ReportageNewsArticle", "AnalysisNewsArticle", "BlogPosting"}
# Sentence end; a period after a bare 1-3 digit number is a German ordinal ("in der 14. Minute"), not an end.
_SENTENCE_END = re.compile(r"(?<![\s(]\d)(?<![\s(]\d\d)(?<![\s(]\d\d\d)[.!?…][»«\"')]*(?=\s)")


@dataclass
class Article:
    feed: str
    source_type: str
    url: str
    source_kind: str
    title: str
    summary: str
    published_at: datetime | None
    excerpt: str | None = None
    fetch_article: bool = False
    language: str = "de"
    canonical_url: str | None = (
        None  # where the outlet really files the article (after redirects / canonical)
    )

    @property
    def text(self) -> str:
        return self.excerpt or self.summary


def strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


def _date(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")  # RSS and Atom (Watson)
    if parsed:
        return datetime(*parsed[:6], tzinfo=UTC)
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _summary(entry) -> str:
    text = strip_html(entry.get("summary", ""))
    return "" if _BOILERPLATE.match(text) else text  # Watson Atom content is only "(Auf … lesen)"


def parse_feed(content: bytes | str, feed: FeedConfig) -> list[Article]:
    parsed = feedparser.parse(content)
    out = []
    for e in parsed.entries:
        link = (e.get("link") or "").strip()
        title = strip_html(e.get("title", ""))
        if not link or not title or link.endswith("/undefined"):  # 20 Minuten emits empty placeholder items
            continue
        if feed.section_filter and feed.section_filter not in link:
            continue
        link = clean_link(link)
        out.append(
            Article(
                feed=feed.name,
                source_type=feed.source_type,
                url=link,
                source_kind=feed.source_kind,
                fetch_article=feed.fetch_article,
                title=title,
                summary=_summary(e),
                published_at=_date(e),
                language=feed.language,
            )
        )
    return out


_TRACKING = re.compile(r"[?&](rts_source|utm_[a-z]+|wt_mc|at_medium|at_campaign)=[^&#]*", re.I)


def clean_link(link: str) -> str:
    """Drop tracking parameters (RTS adds ?rts_source=rss_t, which its robots.txt disallows)."""
    out = _TRACKING.sub("", link)
    if "?" not in out and "&" in out:
        out = out.replace("&", "?", 1)
    return out


def _split_paragraphs(fragment: str) -> list[str]:
    """HTML text (e.g. a JSON-LD articleBody with <br>/<p>) -> paragraph texts."""
    parts = re.split(r"<(?:/p|br\s*/?|/h[1-6]|/li)>|\n{2,}", fragment, flags=re.I)
    return [strip_html(p) for p in parts]


def _json_ld_body(page: str) -> list[str] | None:
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                kinds = node.get("@type")
                kinds = set(kinds) if isinstance(kinds, list) else {kinds}
                body = node.get("articleBody")
                if kinds & _ARTICLE_TYPES and isinstance(body, str) and body.strip():
                    return _split_paragraphs(body)
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return None


def _article_container(page: str) -> str | None:
    m = re.search(r'<article class="articlepage__article.*?</article>', page, re.S)  # SRF
    if m:
        return m.group(0)
    blocks = re.findall(r"<article\b.*?</article>", page, re.S | re.I)  # Blick and most CMSs
    return max(blocks, key=len) if blocks else None


def _truncate(text: str) -> str:
    if len(text) <= EXCERPT_MAX:
        return text
    cut = text[:EXCERPT_MAX]
    ends = list(_SENTENCE_END.finditer(cut))
    if ends:
        return cut[: ends[-1].end()] + " […]"  # never cut mid-sentence
    return cut[: cut.rfind(" ")] + " […]"


def extract_article_excerpt(page: str) -> str | None:
    """Article text of an outlet's page: JSON-LD articleBody, else the <article> paragraphs."""
    page = re.sub(r"<!--.*?-->", "", page, flags=re.S)
    paras = _json_ld_body(page)
    if not paras:
        body = _article_container(page) or page
        paras = [strip_html(p) for p in re.findall(r"<p\b[^>]*>(.*?)</p>", body, re.S | re.I)]
    paras = [
        p
        for p in paras
        if len(p) > 40 and not _BOILERPLATE.match(p) and "Schweizer Radio und Fernsehen" not in p
    ]
    text = "\n".join(paras)
    return _truncate(text) if text else None


def is_womens_hockey(article: Article) -> bool:
    """Women's term somewhere + the text (or, when it names no sport, the URL section) is about ice hockey.
    A text with more cues for another sport (Fussball, FIFA, ÖFB, Elfmeter …) is rejected whatever the URL says.
    """
    from klugscheisser.domain.sport import sport_verdict
    from klugscheisser.sports import current_spec

    url = article.canonical_url or article.url
    # 2026-11-24: a men's project (floorball) has no women's filter, only the sport check
    if not current_spec().men and not WOMEN.search(f"{url} {article.title} {article.text}"):
        return False
    return sport_verdict(f"{article.title}\n{article.text}", url).names_hockey


_CANONICAL = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]*>', re.I)
_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)


def canonical_url(page: str, final_url: str | None = None) -> str | None:
    """The page's <link rel="canonical">, else the URL after redirects."""
    tag = _CANONICAL.search(page or "")
    href = _HREF.search(tag.group(0)) if tag else None
    return (href.group(1).strip() if href else None) or final_url


class RssCollector:
    def __init__(
        self, feeds: list[FeedConfig] | None = None, client: httpx.Client | None = None, delay_s: float = 1.0
    ) -> None:
        self.feeds = feeds or DEFAULT_FEEDS
        self.http = client or httpx.Client(
            timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )
        self.delay_s = delay_s
        self.errors: list[str] = []
        self.canonical: dict[str, str] = {}  # feed URL -> canonical URL, filled by fetch_excerpt

    def articles(self) -> list[Article]:
        """Articles of all feeds. One failing feed does not stop the others; its error goes to ``errors``.
        Raises only if every feed failed."""
        out = []
        self.errors = []
        for feed in self.feeds:
            try:
                r = self.http.get(feed.url)
                r.raise_for_status()
                out.extend(parse_feed(r.content, feed))
            except Exception as exc:
                self.errors.append(f"feed {feed.name}: {exc}")
        if self.feeds and len(self.errors) == len(self.feeds):
            raise RuntimeError("; ".join(self.errors))
        return out

    def fetch_excerpt(self, url: str) -> str | None:
        time.sleep(self.delay_s)
        r = self.http.get(url)
        if r.status_code != 200:
            return None
        # the page's own URL, also when unchanged: a checked URL section can be trusted for team links
        # (SRF's feed says /sport/eishockey/… for pages that redirect to /sport/fussball/…)
        self.canonical[url] = canonical_url(r.text, str(r.url)) or url
        return extract_article_excerpt(r.text)
