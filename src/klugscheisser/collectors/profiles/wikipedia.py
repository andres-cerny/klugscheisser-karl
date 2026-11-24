"""Wikipedia articles about a person as raw documents, home language first (v5 P4).

Languages per person: the home language(s) of her nationality (sv for a Swede, fi + sv for a Finn), then the
configured base languages (de, en, fr), at most ``max_languages`` articles. A Swiss gets de, en, fr, it (German
and English before the other national languages). Japanese articles are not read.

With a Wikidata QID: sitelinks (wbgetentities) -> article title per language -> REST summary (canonical URL,
revision timestamp, page type) + plain-text extract of the whole article (Action API ``prop=extracts``),
stored whole (``max_chars`` is only a safety limit for giant pages, 100k; it was 6,000 until 2026-10-25). One ProfileDoc per language; its ``language`` and
``source_name`` ("Wikipedia (sv)") name the language.

Without a QID: full-text search per language (same order) for the quoted name; only an article whose title is
the name (optionally with a qualifier in brackets), which is not a disambiguation page and whose introduction
mentions the sport is accepted. Namesakes (a triathlete, a politician) are rejected.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote

from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.collectors.profiles.text import cap_text
from klugscheisser.domain.names import name_variants, normalize
from klugscheisser.interfaces import ProfileDoc

log = logging.getLogger(__name__)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
LANGUAGES = ("de", "en", "fr")
MAX_LANGUAGES = 4
MAX_CHARS = 100_000  # safety limit only: a whole article is stored (Settings.profiles_wikipedia_max_chars)

# IOC code -> Wikipedia languages of the home country, most used first. JPN: none (Japanese is not read).
HOME_LANGUAGES: dict[str, tuple[str, ...]] = {
    "SWE": ("sv",),
    "FIN": ("fi", "sv"),
    "CZE": ("cs",),
    "DEN": ("da",),
    "NOR": ("nb",),
    "ITA": ("it",),
    "FRA": ("fr",),
    "SLO": ("sl",),
    "CRO": ("hr",),
    "SVK": ("sk",),
    "POL": ("pl",),
    "AUT": ("de",),
    "GER": ("de",),
    "LIE": ("de",),
    "CAN": ("en", "fr"),
    "USA": ("en",),
    "GBR": ("en",),
    "ESP": ("es",),
    "AND": ("ca", "es"),
    "BEL": ("fr", "nl"),
    "NED": ("nl",),
    "HUN": ("hu",),
    "SUI": ("de", "fr", "it"),
    "JPN": (),
}
# a Swiss reads German first, then English, then the other national languages
_LEAD_LANGUAGES = {"SUI": ("de",)}
# language code -> Wikipedia subdomain where they differ (Norwegian Bokmål is no.wikipedia.org)
WIKI_SUBDOMAIN = {"nb": "no"}


def wiki_host(lang: str) -> str:
    return f"{WIKI_SUBDOMAIN.get(lang, lang)}.wikipedia.org"


def wiki_site(lang: str) -> str:
    """Wikidata sitelink key: "svwiki"; "nowiki" for nb."""
    return f"{WIKI_SUBDOMAIN.get(lang, lang)}wiki"


def home_languages(nationality: str | None) -> tuple[str, ...]:
    return HOME_LANGUAGES.get((nationality or "").upper(), ())


def language_order(
    nationality: str | None, base: tuple[str, ...] | list[str] = LANGUAGES, trailing: bool = True
) -> list[str]:
    """Wikipedia languages to read for a person: home language(s) first, then ``base`` (de, en, fr).

    SWE -> sv, de, en, fr; FIN -> fi, sv, de, en, fr; SUI -> de, en, fr, it; JPN / unknown -> de, en, fr.
    ``trailing=False`` leaves out the home languages that come after the base ones (fr/it for a Swiss)."""
    home = home_languages(nationality)
    lead = _LEAD_LANGUAGES.get((nationality or "").upper(), home)
    order: list[str] = []
    for lang in (*lead, *base, *(home if trailing else ())):
        if lang not in order:
            order.append(lang)
    return order


# search words and the pattern an accepted article must contain, per sport and language
_HOCKEY_TERMS = {
    "de": "Eishockey",
    "en": "ice hockey",
    "fr": "hockey sur glace",
    "it": "hockey su ghiaccio",
    "sv": "ishockey",
    "fi": "jääkiekko",
    "cs": "lední hokej",
    "da": "ishockey",
    "nb": "ishockey",
    "sl": "hokej na ledu",
    "hr": "hokej na ledu",
    "sk": "ľadový hokej",
    "pl": "hokej na lodzie",
    "es": "hockey sobre hielo",
    "ca": "hoquei sobre gel",
    "nl": "ijshockey",
    "hu": "jégkorong",
}
_SKI_TERMS = {
    "de": "Ski Alpin",
    "en": "alpine skier",
    "fr": "ski alpin",
    "it": "sci alpino",
    "sv": "alpin skidåkare",
    "fi": "alppihiihtäjä",
    "cs": "alpské lyžování",
    "da": "alpint skiløb",
    "nb": "alpinist",
    "sl": "alpsko smučanje",
    "hr": "alpsko skijanje",
    "sk": "alpské lyžovanie",
    "pl": "narciarstwo alpejskie",
    "es": "esquí alpino",
    "ca": "esquí alpí",
    "nl": "alpineskiër",
    "hu": "alpesi síző",
}
# 2026-11-24: floorball (Unihockey) project
_FLOORBALL_TERMS = {
    "de": "Unihockey",
    "en": "floorball",
    "fr": "floorball",
    "it": "floorball",
    "sv": "innebandy",
    "fi": "salibandy",
    "cs": "florbal",
    "da": "floorball",
    "nb": "innebandy",
    "sl": "floorball",
    "hr": "floorball",
    "sk": "florbal",
    "pl": "unihokej",
    "es": "floorball",
    "ca": "floorball",
    "nl": "floorball",
    "hu": "floorball",
}
SPORT_TERMS: dict[str, dict[str, str]] = {
    "icehockey_women": _HOCKEY_TERMS,
    "ski_alpine_women": _SKI_TERMS,
    "ski_alpine_men": _SKI_TERMS,
    "floorball_men": _FLOORBALL_TERMS,
    "floorball_women": _FLOORBALL_TERMS,
}
_HOCKEY = re.compile(
    r"eishockey|ice hockey|hockey sur glace|hockeyeuse|hockey player|hockey su ghiaccio|hockeista|ishockey|"
    r"jääkiek|lední hokej|hokejist|hokej na l|ľadov|hockey sobre hielo|hoquei|ijshockey|jégkorong",
    re.I,
)
_SKI = re.compile(
    r"ski ?alpin|alpine ski|skirennfahrer|skirennläufer|skieuse|skieur alpin|sci alpino|sciatric|sciatore|"
    r"alpin skidåk|alppihiiht|sjezd|alpsk|alpejsk|alpinist|alpint|esquí alp|esquiador|alpineski|alpesi sí",
    re.I,
)
_FLOORBALL = re.compile(
    r"unihockey|floorball|innebandy|salibandy|florbal|unihokej|florbol|saalihoki",  # florbal(ista), florbols (lv)
    re.I,
)
SPORT_PATTERNS: dict[str, re.Pattern] = {
    "icehockey_women": _HOCKEY,
    "ski_alpine_women": _SKI,
    "ski_alpine_men": _SKI,
    "floorball_men": _FLOORBALL,
    "floorball_women": _FLOORBALL,
}
# trailing sections without prose (empty in plain-text extracts)
_TAIL = re.compile(
    r"\n(?:Weblinks|Einzelnachweise|Literatur|Siehe auch|References|External links|See also|Notes|"
    r"Notes et références|Références|Liens externes|Voir aussi|Referenser|Externa länkar|Se även|Lähteet|"
    r"Aiheesta muualla|Katso myös|Reference|Externí odkazy|Související články|Odkazy|Note|Collegamenti esterni|"
    r"Voci correlate|Referanser|Eksterne lenker|Se også|Kilder|Eksterne henvisninger|Viri|Zunanje povezave|"
    r"Przypisy|Linki zewnętrzne|Referencias|Enlaces externos|Referències|Enllaços externs|Externe links|"
    r"Bronnen|Jegyzetek|Források|Izvori|Vanjske poveznice|Referencie|Externé odkazy)\s*\n.*\Z",
    re.S,
)


def clean_extract(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _TAIL.sub("", text + "\n").strip()


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def title_matches(title: str, full_name: str) -> bool:
    """'Laura Zimmermann (Eishockeyspielerin)' matches 'Laura Zimmermann'; 'Operation Libero' does not."""
    base = normalize(re.sub(r"\s*\(.*?\)\s*$", "", title))
    return base in name_variants(full_name)


class WikipediaCollector:
    name = "wikipedia"

    def __init__(
        self,
        client: PoliteClient | None = None,
        languages: tuple[str, ...] | list[str] = LANGUAGES,
        max_chars: int = MAX_CHARS,
        max_languages: int = MAX_LANGUAGES,
    ) -> None:
        self.client = client or PoliteClient(min_delay_s=0.5)
        self.languages = tuple(languages)  # base languages for everybody, after the home language(s)
        self.max_chars = max_chars
        self.max_languages = max_languages

    def languages_for(self, nationality: str | None) -> list[str]:
        return language_order(nationality, self.languages)

    # --- API calls --------------------------------------------------------------------------------

    def _json(self, url: str, params: dict | None = None) -> dict:
        r = self.client.get(url, check_robots=False, params=params)
        r.raise_for_status()
        return r.json()

    def sitelinks(self, qid: str, languages: tuple[str, ...] | list[str] | None = None) -> dict[str, str]:
        """{"de": "Alina Müller", ...} for the given languages (default: the base languages), in that order."""
        languages = tuple(languages or self.languages)
        data = self._json(
            WIKIDATA_API,
            {
                "action": "wbgetentities",
                "ids": qid,
                "props": "sitelinks",
                "sitefilter": "|".join(wiki_site(lang) for lang in languages),
                "format": "json",
            },
        )
        links = (data.get("entities", {}).get(qid) or {}).get("sitelinks") or {}
        return {lang: links[wiki_site(lang)]["title"] for lang in languages if wiki_site(lang) in links}

    def summary(self, lang: str, title: str) -> dict:
        url = f"https://{wiki_host(lang)}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'), safe='')}"
        return self._json(url)

    def extract(self, lang: str, title: str) -> dict:
        data = self._json(
            f"https://{wiki_host(lang)}/w/api.php",
            {
                "action": "query",
                "prop": "extracts|info|pageprops",
                "explaintext": 1,
                "exsectionformat": "plain",
                "inprop": "url",
                "redirects": 1,
                "titles": title,
                "format": "json",
                "formatversion": 2,
            },
        )
        pages = data.get("query", {}).get("pages") or [{}]
        return pages[0]

    def search(self, lang: str, query: str, limit: int = 5) -> list[str]:
        data = self._json(
            f"https://{wiki_host(lang)}/w/api.php",
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "format": "json",
                "formatversion": 2,
            },
        )
        return [hit["title"] for hit in data.get("query", {}).get("search", [])]

    # --- documents --------------------------------------------------------------------------------

    def document(self, lang: str, title: str, page: dict | None = None) -> ProfileDoc | None:
        page = page or self.extract(lang, title)
        if page.get("missing") or "disambiguation" in (page.get("pageprops") or {}):
            return None
        summary = self.summary(lang, page.get("title") or title)
        if summary.get("type") == "disambiguation":
            return None
        text = clean_extract(page.get("extract") or "") or (summary.get("extract") or "").strip()
        if not text:
            return None
        url = (
            ((summary.get("content_urls") or {}).get("desktop") or {}).get("page")
            or page.get("fullurl")
            or f"https://{wiki_host(lang)}/wiki/{quote(title.replace(' ', '_'))}"
        )
        return ProfileDoc(
            url=url,
            title=page.get("title") or title,
            text=cap_text(text, self.max_chars),
            source_name=f"Wikipedia ({lang})",
            source_kind="media",
            source_type="profile",
            platform="wikipedia",
            language=lang,
            published_at=_parse_ts(summary.get("timestamp") or page.get("touched")),
        )

    def _search_fallback(self, lang: str, full_name: str, sport: str) -> ProfileDoc | None:
        pattern = SPORT_PATTERNS.get(sport)
        if pattern is None:
            return None
        term = SPORT_TERMS.get(sport, {}).get(lang, "")
        titles = [
            t for t in self.search(lang, f'"{full_name}" {term}'.strip()) if title_matches(t, full_name)
        ]
        if not titles:  # the sport word can push the article out of the top hits
            titles = [t for t in self.search(lang, f'"{full_name}"') if title_matches(t, full_name)]
        for title in titles[:3]:
            page = self.extract(lang, title)
            if "disambiguation" in (page.get("pageprops") or {}):
                continue
            if not pattern.search((page.get("extract") or "")[:2000]):
                log.info("wikipedia %s: %r is a namesake (no %s)", lang, title, sport)
                continue
            return self.document(lang, title, page)
        return None

    def collect(self, full_name: str, sport: str, hints: dict) -> list[ProfileDoc]:
        """Articles in the person's languages (home language first), at most ``max_languages``."""
        qid = hints.get("wikidata_qid")
        languages = self.languages_for(hints.get("nationality"))
        docs: list[ProfileDoc] = []
        if qid:
            for lang, title in self.sitelinks(qid, languages).items():
                if len(docs) >= self.max_languages:
                    break
                if doc := self.document(lang, title):
                    docs.append(doc)
            return docs
        # without a QID each language costs up to two searches and namesakes are likelier: only the lead home
        # language(s) and the base languages are searched (a Swiss: de, en, fr; not it), at most max_languages
        search = language_order(hints.get("nationality"), self.languages, trailing=False)
        for lang in search[: self.max_languages]:
            if doc := self._search_fallback(lang, full_name, sport):
                docs.append(doc)
        return docs
