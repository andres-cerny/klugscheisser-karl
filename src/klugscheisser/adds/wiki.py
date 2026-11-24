"""Shared helpers for the add resolvers: Wikimedia API access, countries, sports, roster tables.

All Wikimedia requests go through one `PoliteClient` (identified User-Agent, one request at a time, a short
pause per host). Wikidata/Wikipedia APIs are not crawled pages, so robots.txt is not checked (see
collectors/profiles/http.py).
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import quote, unquote

from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector, wiki_host
from klugscheisser.collectors.wikidata import ENDPOINT as SPARQL_ENDPOINT
from klugscheisser.collectors.wikidata import USER_AGENT

WIKIDATA_API = "https://www.wikidata.org/w/api.php"

# --- countries --------------------------------------------------------------------------------------------

# IOC code -> (English name as used in Wikipedia team titles, German name, pattern matching free text)
COUNTRIES: dict[str, tuple[str, str, str]] = {
    "SUI": ("Switzerland", "Schweiz", r"schweiz|swiss|switzerland|\bSUI\b"),
    "SWE": ("Sweden", "Schweden", r"schwed|swed|\bSWE\b"),
    "FIN": ("Finland", "Finnland", r"finn|finland|\bFIN\b"),
    "CAN": ("Canada", "Kanada", r"kanad|canad|\bCAN\b"),
    "USA": ("United States", "USA", r"\bUSA\b|\bUS-|amerikan|american|united states"),
    "CZE": ("Czech Republic", "Tschechien", r"tschech|czech|\bCZE\b"),
    "GER": ("Germany", "Deutschland", r"deutsch|german|\bGER\b"),
    "AUT": ("Austria", "Österreich", r"österreich|oesterreich|austria|\bAUT\b"),
    "JPN": ("Japan", "Japan", r"japan|\bJPN\b"),
    "DEN": ("Denmark", "Dänemark", r"dänisch|dänin|dänemark|danish|denmark|\bDEN\b"),
    "NOR": ("Norway", "Norwegen", r"norweg|norway|\bNOR\b"),
    "FRA": ("France", "Frankreich", r"französ|frankreich|french|france|\bFRA\b"),
    "ITA": ("Italy", "Italien", r"italien|italy|\bITA\b"),
    "SVK": ("Slovakia", "Slowakei", r"slowak|slovak|\bSVK\b"),
    "HUN": ("Hungary", "Ungarn", r"ungar|hungar|\bHUN\b"),
    "CHN": ("China", "China", r"chines|china|\bCHN\b"),
    "KOR": ("South Korea", "Südkorea", r"korea|\bKOR\b"),
    "GBR": ("Great Britain", "Grossbritannien", r"brit|england|\bGBR\b"),
    "NED": ("Netherlands", "Niederlande", r"niederl|dutch|netherlands|holländ|\bNED\b"),
    "POL": ("Poland", "Polen", r"polnisch|polin\b|polen|polish|poland|\bPOL\b"),
    "LAT": ("Latvia", "Lettland", r"lett|latvia|\bLAT\b"),
    "SLO": ("Slovenia", "Slowenien", r"slowen|sloven|\bSLO\b"),
}
# the IOC codes only in capitals: "den Club" is no DEN, "who can" no CAN
_COUNTRY_RE = {
    ioc: re.compile(re.sub(r"\\b([A-Z]{3})\\b", r"(?-i:\\b\1\\b)", pattern), re.I)
    for ioc, (_, _, pattern) in COUNTRIES.items()
}

# ISO 3166 alpha-3 -> IOC where they differ (fallback when Wikidata has no IOC code, P984)
ISO_TO_IOC = {
    "CHE": "SUI",
    "DEU": "GER",
    "DNK": "DEN",
    "NLD": "NED",
    "SVN": "SLO",
    "LVA": "LAT",
    "GRC": "GRE",
    "PRT": "POR",
    "HRV": "CRO",
}


def countries_in(text: str) -> list[str]:
    """IOC codes of all countries mentioned in free text ("die Schwedin" -> ["SWE"])."""
    # "Schweizer" must not also count as "Schwed…" and vice versa: the patterns are disjoint by design
    return [ioc for ioc, rx in _COUNTRY_RE.items() if rx.search(text or "")]


def ioc_code(ioc: str | None, iso: str | None) -> str | None:
    if ioc:
        return ioc.upper()
    if iso:
        return ISO_TO_IOC.get(iso.upper(), iso.upper())
    return None


# --- sports -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SportInfo:
    base: str  # key prefix: "icehockey" -> "icehockey_women"
    name_de: str  # "Eishockey"
    person_f: str
    person_m: str
    label_pattern: str  # matched against English occupation/sport labels from Wikidata
    text_pattern: str  # matched against Fabi's free text (German/English)


SPORTS: list[SportInfo] = [
    SportInfo(
        "icehockey",
        "Eishockey",
        "Eishockeyspielerin",
        "Eishockeyspieler",
        r"ice hockey",
        r"eishockey|(?<!uni)hockey",  # not "Unihockey" (floorball)
    ),
    SportInfo(
        "ski_alpine",
        "Ski alpin",
        "Skirennfahrerin",
        "Skirennfahrer",
        r"alpine ski",
        r"ski ?alpin|alpine ski|skirennfahr|abfahrt|slalom",
    ),
    SportInfo(
        "ski_crosscountry",
        "Skilanglauf",
        "Skilangläuferin",
        "Skilangläufer",
        r"cross-country ski",
        r"langlauf|langläuf|cross-country",
    ),
    SportInfo("biathlon", "Biathlon", "Biathletin", "Biathlet", r"biathl", r"biathl"),
    SportInfo(
        "ski_jumping", "Skispringen", "Skispringerin", "Skispringer", r"ski jump", r"skisprung|skispring"
    ),
    SportInfo("snowboard", "Snowboard", "Snowboarderin", "Snowboarder", r"snowboard", r"snowboard"),
    SportInfo(
        "freestyle", "Freestyle-Ski", "Freestyle-Skierin", "Freestyle-Skier", r"freestyle ski", r"freestyle"
    ),
    SportInfo("curling", "Curling", "Curlerin", "Curler", r"curl", r"curling|curler"),
    SportInfo(
        "floorball",
        "Unihockey",
        "Unihockeyspielerin",
        "Unihockeyspieler",
        r"floorball",
        r"unihockey|floorball",
    ),
    SportInfo(
        "football",
        "Fussball",
        "Fussballspielerin",
        "Fussballspieler",
        r"association football|soccer|footballer",
        r"fussball|fußball|soccer|football",
    ),
    SportInfo("tennis", "Tennis", "Tennisspielerin", "Tennisspieler", r"tennis", r"tennis"),
    SportInfo("handball", "Handball", "Handballspielerin", "Handballspieler", r"handball", r"handball"),
    SportInfo(
        "volleyball", "Volleyball", "Volleyballspielerin", "Volleyballspieler", r"volleyball", r"volleyball"
    ),
    SportInfo(
        "basketball", "Basketball", "Basketballspielerin", "Basketballspieler", r"basketball", r"basketball"
    ),
    SportInfo(
        "athletics",
        "Leichtathletik",
        "Leichtathletin",
        "Leichtathlet",
        r"athletics|sprinter|runner|hurdler|jumper|thrower|heptathl|decathl",
        r"leichtathlet|sprint|hürden|athletics",
    ),
    SportInfo("swimming", "Schwimmen", "Schwimmerin", "Schwimmer", r"swim", r"schwimm|swim"),
    SportInfo(
        "cycling", "Radsport", "Radrennfahrerin", "Radrennfahrer", r"cycl", r"radrennfahr|radsport|cycling"
    ),
    SportInfo(
        "figure_skating",
        "Eiskunstlauf",
        "Eiskunstläuferin",
        "Eiskunstläufer",
        r"figure skat",
        r"eiskunstl|figure skat",
    ),
    SportInfo("triathlon", "Triathlon", "Triathletin", "Triathlet", r"triathl", r"triathl"),
]
_COACH = re.compile(r"coach|trainer|manager", re.I)


def sport_from_labels(labels: list[str]) -> SportInfo | None:
    text = " | ".join(labels)
    return next((s for s in SPORTS if re.search(s.label_pattern, text, re.I)), None)


def sports_in(text: str) -> list[SportInfo]:
    return [s for s in SPORTS if re.search(s.text_pattern, text or "", re.I)]


def sport_fields(info: SportInfo | None, female: bool = True) -> tuple[str | None, str | None, str | None]:
    """(sport_key, sport_name_de, person_de) — the registered SportSpec when one exists."""
    if info is None:
        return None, None, None
    key = f"{info.base}_{'women' if female else 'men'}"
    try:
        from klugscheisser.sports import get_sport

        spec = get_sport(key)
        return spec.key, spec.name_de, spec.person_de
    except KeyError:
        return (
            key,
            f"{info.name_de} {'Frauen' if female else 'Männer'}",
            info.person_f if female else info.person_m,
        )


def is_coach_label(label: str) -> bool:
    return bool(_COACH.search(label or ""))


# --- dates ------------------------------------------------------------------------------------------------


def date_de(d: date | None) -> str:
    """Birth dates always with year: 12.03.1999 (format of domain/labels.date_de)."""
    return d.strftime("%d.%m.%Y") if d else "unbekannt"


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.lstrip("+")[:10])
    except ValueError:
        return None


def year_in(text: str) -> int | None:
    """'Jahrgang 2001' / 'geb. 2001' / 'born 2001' -> 2001."""
    m = re.search(
        r"(?:jahrgang|jg\.?|geb(?:oren|\.)?|born|\*)\s*(?:\d{1,2}\.\s*\d{1,2}\.\s*)?((?:19|20)\d\d)",
        text or "",
        re.I,
    )
    return int(m.group(1)) if m else None


# --- Wikimedia client -------------------------------------------------------------------------------------


def wiki_title(url: str | None) -> str | None:
    """'https://de.wikipedia.org/wiki/Hanna_Olsson' -> 'Hanna Olsson'."""
    if not url or "/wiki/" not in url:
        return None
    return unquote(url.rsplit("/wiki/", 1)[1]).replace("_", " ")


def wiki_url(lang: str, title: str) -> str:
    return f"https://{wiki_host(lang)}/wiki/{quote(title.replace(' ', '_'))}"


class WikiClient:
    """Wikidata search + SPARQL, Wikipedia summaries, sections and section HTML."""

    def __init__(self, client: PoliteClient | None = None) -> None:
        self.client = client or PoliteClient(min_delay_s=0.5)
        self.wikipedia = WikipediaCollector(client=self.client)

    def _json(self, url: str, params: dict) -> dict:
        r = self.client.get(url, check_robots=False, params=params)
        r.raise_for_status()
        return r.json()

    def search_entities(
        self, query: str, languages: tuple[str, ...] = ("de", "en"), limit: int = 8
    ) -> list[dict]:
        """wbsearchentities per language, merged in order, each QID once."""
        seen: dict[str, dict] = {}
        for lang in languages:
            data = self._json(
                WIKIDATA_API,
                {
                    "action": "wbsearchentities",
                    "search": query,
                    "language": lang,
                    "uselang": lang,
                    "type": "item",
                    "limit": limit,
                    "format": "json",
                },
            )
            for hit in data.get("search", []):
                if hit["id"] not in seen:
                    seen[hit["id"]] = hit
                elif not seen[hit["id"]].get("description") and hit.get("description"):
                    seen[hit["id"]]["description"] = hit["description"]
        return list(seen.values())

    def sparql(self, query: str) -> list[dict[str, str]]:
        # POST: the query service rejects long GET queries (403); one query per proposal, no pacing needed
        r = self.client.http.post(
            SPARQL_ENDPOINT,
            data={"query": query},
            headers={"Accept": "application/sparql-results+json", "User-Agent": USER_AGENT},
            timeout=60,
        )
        r.raise_for_status()
        return [{k: v["value"] for k, v in row.items()} for row in r.json()["results"]["bindings"]]

    def sitelinks(self, qid: str, languages: list[str] | tuple[str, ...]) -> dict[str, str]:
        """{"sv": "Sveriges damlandslag i ishockey", ...}: article titles of a Wikidata item per language."""
        return self.wikipedia.sitelinks(qid, languages)

    def summary(self, lang: str, title: str) -> dict:
        try:
            return self.wikipedia.summary(lang, title)
        except Exception:  # a missing summary is no reason to fail the proposal
            return {}

    def sections(self, lang: str, title: str) -> tuple[str, list[dict]]:
        """(resolved title, [{"index", "line", "level"}]) of an article."""
        data = self._json(
            f"https://{wiki_host(lang)}/w/api.php",
            {
                "action": "parse",
                "page": title,
                "prop": "sections",
                "redirects": 1,
                "format": "json",
                "formatversion": 2,
            },
        )
        parse = data.get("parse") or {}
        return parse.get("title") or title, parse.get("sections") or []

    def section_html(self, lang: str, title: str, index: str) -> str:
        data = self._json(
            f"https://{wiki_host(lang)}/w/api.php",
            {
                "action": "parse",
                "page": title,
                "section": index,
                "prop": "text",
                "redirects": 1,
                "disablelimitreport": 1,
                "disableeditsection": 1,
                "format": "json",
                "formatversion": 2,
            },
        )
        return (data.get("parse") or {}).get("text") or ""


# --- roster tables ----------------------------------------------------------------------------------------


@dataclass
class RosterRow:
    name: str
    number: int | None = None
    position: str | None = None
    birth_date: date | None = None
    team: str | None = None
    nationality: str | None = None
    role: str = "athlete"  # athlete | coach | staff
    function_de: str | None = None
    wikipedia_title: str | None = None  # linked article (None for red links)


@dataclass
class Roster:
    rows: list[RosterRow] = field(default_factory=list)
    as_of: str | None = None  # "19 December 2024", "12 January 2026"


@dataclass
class _Cell:
    text: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (title, href)
    bday: str | None = None
    sort_value: str | None = None
    header: bool = False


class _TableParser(HTMLParser):
    """Collects tables as rows of cells, and the plain text outside tables (for 'Head coach: X')."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[_Cell]]] = []
        self.captions: list[str] = []  # <caption> text per table ("" when none)
        self._in_caption = False
        self.outside: list[str] = []
        self._depth = 0
        self._row: list[_Cell] | None = None
        self._cell: _Cell | None = None
        self._skip = 0  # inside <style>, <sup class=reference>, hidden spans
        self._skip_tags: list[str] = []
        self._in_bday = False
        self._link: tuple[str, str] | None = None
        self._outside_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs_list: list) -> None:
        attrs = {k: v or "" for k, v in attrs_list}
        if self._skip:
            if tag in ("style", "sup", "span") and tag == self._skip_tags[-1]:
                self._skip += 1
                self._skip_tags.append(tag)
            return
        if tag == "style" or (tag == "sup" and "reference" in attrs.get("class", "")):
            self._skip, self._skip_tags = 1, [tag]
            return
        if tag == "span" and "display:none" in attrs.get("style", "").replace(" ", ""):
            # hidden "(1999-03-12)" next to the visible date; keep the bday inside it
            self._skip, self._skip_tags = 1, [tag]
            return
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
                self.captions.append("")
                self._flush_outside()
        elif self._depth == 1 and tag == "caption":
            self._in_caption = True
        elif self._depth == 1 and tag == "tr":
            self._row = []
            self.tables[-1].append(self._row)
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = _Cell(header=tag == "th" and attrs.get("scope") != "row")
            self._row.append(self._cell)
        elif tag == "span" and self._cell is not None:
            if "data-sort-value" in attrs and self._cell.sort_value is None:
                self._cell.sort_value = attrs["data-sort-value"]
            if "bday" in attrs.get("class", ""):
                self._in_bday = True
        elif tag == "a":
            title = attrs.get("title", "")
            if (
                "redlink=1" in attrs.get("href", "")
                or "page does not exist" in title
                or "Seite nicht vorhanden" in title
            ):
                title = ""
            self._link = (title, attrs.get("href", ""))
            if self._cell is not None:
                self._cell.links.append(self._link)
        elif tag in ("p", "dd", "li", "br", "div") and self._depth == 0:
            self._flush_outside()

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag == self._skip_tags[-1]:
                self._skip -= 1
                self._skip_tags.pop()
            return
        if tag == "table":
            self._depth -= 1
            self._row = self._cell = None
        elif tag == "caption":
            self._in_caption = False
        elif self._depth == 1 and tag in ("td", "th"):
            self._cell = None
        elif tag == "span":
            self._in_bday = False
        elif tag == "a":
            self._link = None
        elif tag in ("p", "dd", "li", "div") and self._depth == 0:
            self._flush_outside()

    def handle_data(self, data: str) -> None:
        if self._skip:
            # hidden "(1999-03-12)" next to the visible birth date
            iso = re.search(r"\d{4}-\d\d-\d\d", data)
            if iso and self._skip_tags[0] == "span" and self._cell is not None and self._cell.bday is None:
                self._cell.bday = iso.group(0)
            return
        if self._in_caption and self._depth == 1 and self.captions:
            self.captions[-1] = " ".join((self.captions[-1] + " " + data).split())
            return
        if self._cell is not None:
            if self._in_bday and self._cell.bday is None:
                self._cell.bday = data.strip()
            self._cell.text += data
        elif self._depth == 0:
            self._outside_buf.append(data)

    def _flush_outside(self) -> None:
        text = " ".join("".join(self._outside_buf).split())
        if text:
            self.outside.append(text)
        self._outside_buf = []

    def close(self) -> None:
        super().close()
        self._flush_outside()


# column headers in en/de and the home languages of the hockey nations (sv, fi, cs, da/nb, it, fr, sl, sk, pl)
_COLUMNS = {
    "number": re.compile(
        r"^(no\.?|nr\.?|nro\.?|#|number|nummer|numero|číslo|č\.|numer|n°|n\.|št\.|broj|dres)$", re.I
    ),
    "position": re.compile(
        r"^(pos\.?|position|pelipaikka|pp\.?|pozice|post|pozycja|pozícia|ruolo|poste|položaj|pos\.)$", re.I
    ),
    "name": re.compile(
        r"^(name|player|spieler(in)?|spielerinnen|athlete|staff|namn|spelare|nimi|pelaaja|jméno|hráč(ka|ky)?|"
        r"navn|spiller|nome|giocatri?ce|giocatore|nom|joueuse|joueur|ime|igralka|igralec|meno|hráčka|"
        r"zawodniczka|zawodnik|imię i nazwisko|nazwisko)$",
        re.I,
    ),
    "birth": re.compile(
        r"^(birth ?date|date of birth|geburtsdatum|geboren|born|age|alter|född|födelsedatum|ålder|syntynyt|"
        r"syntymäaika|ikä|datum narození|narozen|věk|født|alder|data di nascita|nascita|date de naissance|"
        r"né|datum rojstva|dátum narodenia|data urodzenia)",
        re.I,
    ),
    "team": re.compile(
        r"^(team|club|verein|klub|current team|aktueller verein|klubb|lag|seura|joukkue|tým|oddíl|squadra|"
        r"équipe|klubas)",
        re.I,
    ),
    "nationality": re.compile(r"^(nat\.?|nationality|nation)$", re.I),
    "role": re.compile(r"^(role|funktion|rolle|job|title|roll|rooli|tehtävä|funkce|fonction)$", re.I),
}
_COACH_WORD = r"coach|trainer|tränare|valmentaja|trenér|træner|trener|allenat"
_STAFF_LINE = re.compile(
    r"(?P<role>(?:[^\W\d_]+[ -]){0,3}(?:coach(?:es)?|\w*trainer(?:in|innen)?|\w*tränare|\w*valmentaja|"
    r"trenér(?:ka|ky|i)?|\w*træner|\w*trener|allenatore|manager|therapist|physio\w*|doctor|arzt|ärztin|"
    r"betreuer(?:in)?|materialwart(?:in)?|director|sportchef(?:in)?))\s*:\s*(?P<names>[^.;\n]+)",
    re.I,
)
_STAFF_DE = {
    "head coach": "Cheftrainer/in",
    "cheftrainer": "Cheftrainer/in",
    "cheftrainerin": "Cheftrainer/in",
    "huvudtränare": "Cheftrainer/in",
    "förbundskapten": "Cheftrainer/in",
    "päävalmentaja": "Cheftrainer/in",
    "hlavní trenér": "Cheftrainer/in",
    "hlavní trenérka": "Cheftrainer/in",
    "cotrainer": "Assistenztrainer/in",
    "co-trainer": "Assistenztrainer/in",
    "assistenztrainer": "Assistenztrainer/in",
    "assisterande tränare": "Assistenztrainer/in",
    "assisterande tränare (förbundskapten)": "Assistenztrainer/in",
    "apuvalmentaja": "Assistenztrainer/in",
    "asistent trenéra": "Assistenztrainer/in",
    "asistentka trenéra": "Assistenztrainer/in",
    "asistenti trenéra": "Assistenztrainer/in",
    "målvaktstränare": "Torhütertrainer/in",
    "maalivahtivalmentaja": "Torhütertrainer/in",
    "trenér brankářů": "Torhütertrainer/in",
    "assistant coach": "Assistenztrainer/in",
    "assistant coaches": "Assistenztrainer/in",
    "goaltending coach": "Torhütertrainer/in",
    "goalie coach": "Torhütertrainer/in",
    "conditioning coach": "Athletiktrainer/in",
    "strength and conditioning coach": "Athletiktrainer/in",
    "video coach": "Videotrainer/in",
    "skills coach": "Skills-Trainer/in",
    "general manager": "General Manager",
    "team manager": "Teammanager/in",
    "physical therapist": "Physiotherapeut/in",
    "physiotherapist": "Physiotherapeut/in",
    "equipment manager": "Materialwart/in",
    "team doctor": "Teamarzt/-ärztin",
}
_AS_OF = re.compile(
    r"(?:as of|stand:?|updated)\s+([0-9]{1,2}\.? [A-Za-zäöüÄÖÜ]+\.? [0-9]{4}|[A-Za-z]+ [0-9]{1,2}, [0-9]{4})",
    re.I,
)
_ANNOUNCED = re.compile(r"(?:announced|named|bekanntgegeben)\s+on\s+([0-9]{1,2} [A-Za-z]+ [0-9]{4})", re.I)


def _column_map(header: list[_Cell]) -> dict[str, int]:
    cols: dict[str, int] = {}
    for i, cell in enumerate(header):
        label = " ".join(cell.text.split())
        for key, rx in _COLUMNS.items():
            if key not in cols and rx.search(label):
                cols[key] = i
    return cols


def _number(text: str) -> int | None:
    m = re.search(r"\d{1,3}", text or "")
    return int(m.group(0)) if m else None


def _nat(cell: _Cell) -> str | None:
    if cell.sort_value and re.match(r"^[A-Z]{3}", cell.sort_value):
        return cell.sort_value[:3]
    text = cell.text.strip()
    return text[:3] if re.fullmatch(r"[A-Z]{3}", text) else None


def _person_name(cell: _Cell) -> tuple[str, str | None]:
    text = " ".join(htmllib.unescape(cell.text).split())
    text = re.sub(r"\s*(?:[–-]\s*[CA]|\((?:C|A|captain|Captain)\))\s*$", "", text).strip(" –-*†")
    title = next((t for t, href in cell.links if t and "/wiki/" in href), None)
    return text, title


# month names in roster birth-date columns (en, de, sv, fi, cs, da/nb, it, fr); Finnish forms are prefixes
_MONTHS: dict[str, int] = {}
for _number_, _names in enumerate(
    (
        "january januar jänner januari tammikuu ledna leden gennaio janvier",
        "february februar februari helmikuu února únor febbraio février",
        "march märz mars maaliskuu března březen marts marzo",
        "april huhtikuu dubna duben aprile avril",
        "may mai maj toukokuu května květen maggio",
        "june juni kesäkuu června červen giugno juin",
        "july juli heinäkuu července červenec luglio juillet",
        "august augusti elokuu srpna srpen agosto août",
        "september syyskuu září settembre septembre",
        "october oktober lokakuu října říjen ottobre octobre",
        "november marraskuu listopadu listopad novembre",
        "december dezember joulukuu prosince prosinec dicembre décembre",
    ),
    start=1,
):
    for _name in _names.split():
        _MONTHS.setdefault(_name, _number_)
_TEXT_DATE = re.compile(r"\b(\d{1,2})\.?\s*([^\W\d_]{3,})\.?\s+((?:19|20)\d\d)\b")
_TEXT_DATE_EN = re.compile(r"\b([A-Za-z]{3,})\.?\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")
_NUM_DATE = re.compile(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*((?:19|20)\d\d)\b")


def _month(word: str) -> int | None:
    word = word.lower().rstrip(".")
    if word in _MONTHS:
        return _MONTHS[word]
    # Finnish partitive ("maaliskuuta") and abbreviations ("Mar", "Okt") by prefix
    hits = {
        n
        for name, n in _MONTHS.items()
        if word.startswith(name) or (len(word) >= 3 and name.startswith(word))
    }
    return hits.pop() if len(hits) == 1 else None


def parse_text_date(text: str | None) -> date | None:
    """Birth dates as written in roster tables: '24. června 2002', '12 mars 1999', 'March 12, 1999',
    '12.3.1999', '1999-03-12'. None when unclear."""
    text = " ".join((text or "").replace("\xa0", " ").split())
    if not text:
        return None
    if iso := re.search(r"\b((?:19|20)\d\d-\d\d-\d\d)\b", text):
        return parse_date(iso.group(1))
    candidates = []
    if m := _NUM_DATE.search(text):
        candidates.append((m.group(3), m.group(2), m.group(1)))
    if (m := _TEXT_DATE.search(text)) and (month := _month(m.group(2))):
        candidates.append((m.group(3), month, m.group(1)))
    if (m := _TEXT_DATE_EN.search(text)) and (month := _month(m.group(1))):
        candidates.append((m.group(3), month, m.group(2)))
    for year, month, day in candidates:
        try:
            return date(int(year), int(month), int(day))
        except ValueError:
            continue
    return None


def _is_coach_role(text: str) -> bool:
    return re.search(_COACH_WORD, text or "", re.I) is not None


def _staff_from_text(lines: list[str]) -> list[RosterRow]:
    rows: list[RosterRow] = []
    for line in lines:
        for m in _STAFF_LINE.finditer(line):
            role = m.group("role").strip().lower()
            function = _STAFF_DE.get(role, m.group("role").strip())
            for name in re.split(r",| and | und | och | ja | a | og | e | et ", m.group("names")):
                name = re.sub(r"\[\d+\]", "", name).strip()
                if 3 <= len(name) <= 60 and len(name.split()) >= 2:
                    rows.append(
                        RosterRow(
                            name=name,
                            role="coach" if _is_coach_role(role) else "staff",
                            function_de=function,
                        )
                    )
    return rows


def _years(text: str) -> list[int]:
    return [int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", text or "")]


def _header_row(table: list[list[_Cell]]) -> tuple[int | None, str]:
    """(index of the column header row, text of title rows above it such as 'VM-laget 2019')."""
    titles: list[str] = []
    for i, row in enumerate(table):
        if not row or not all(c.header for c in row):
            if titles or i > 2:
                break
            continue
        if "name" in _column_map(row):
            return i, " ".join(titles)
        titles.append(" ".join(" ".join(c.text for c in row).split()))
    return None, " ".join(titles)


_ROSTER_INTRO = re.compile(
    r"(?:soupiska|roster|kader|trupp|joukkue|squad|laguppställning)[^.\n]{0,90}?\b((?:19|20)\d\d)\b", re.I
)


def parse_roster_html(page: str, position_code=None) -> Roster:
    """Players (number, position, name, birth date, team) from the wikitables of a roster section, plus
    coaches/staff from 'Head coach: X' lines and staff tables. `position_code(label) -> code|None` maps
    positions to the sport's codes (G/D/F for ice hockey); unmapped labels are kept as they are.

    Column headers are recognised in en/de and the hockey nations' languages (Nr, Pos/Pelipaikka/Pozice,
    Namn/Spelare/Nimi/Jméno, ...). When one section holds rosters of several years (sv: 'OS-laget 2006',
    'VM-laget 2019'), only the newest one is taken; its title becomes `as_of`."""
    parser = _TableParser()
    parser.feed(page)
    parser.close()
    roster = Roster()
    seen: set[str] = set()
    tables = []
    for i, table in enumerate(parser.tables):
        header_i, title = _header_row(table)
        if header_i is None:
            continue
        label = " ".join(x for x in (parser.captions[i] if i < len(parser.captions) else "", title) if x)
        tables.append((table, header_i, label, max(_years(label), default=None)))
    newest = max((year for *_, year in tables if year), default=None)
    newest_label = None
    for table, header_i, label, year in tables:
        if newest is not None and year is not None and year < newest:
            continue  # an older year's roster in the same section
        if year is not None and newest_label is None:
            newest_label = label
        cols = _column_map(table[header_i])
        staff_table = "role" in cols and "number" not in cols
        # one table per position group, titled "Målvakter" / "Backar" / "Forwards" (sv club rosters)
        group_position = position_code(label) if position_code and label and "position" not in cols else None
        for row in table[header_i + 1 :]:
            if len(row) <= cols["name"] or all(c.header for c in row):
                continue
            name, title = _person_name(row[cols["name"]])
            if not name or len(name) > 60 or name.lower() in seen:
                continue
            item = RosterRow(name=name, wikipedia_title=title)
            if "number" in cols and len(row) > cols["number"]:
                cell = row[cols["number"]]
                item.number = _number(cell.text)
            if "position" in cols and len(row) > cols["position"]:
                pos = " ".join(row[cols["position"]].text.split())
                item.position = (position_code(pos) if position_code else None) or pos or None
            elif group_position:
                item.position = group_position
            if "birth" in cols and len(row) > cols["birth"]:
                cell = row[cols["birth"]]
                item.birth_date = parse_date(cell.bday) or parse_text_date(cell.text)
            if "team" in cols and len(row) > cols["team"]:
                item.team = " ".join(row[cols["team"]].text.split()) or None
            if "nationality" in cols and len(row) > cols["nationality"]:
                item.nationality = _nat(row[cols["nationality"]])
            if staff_table:
                function = " ".join(row[cols["role"]].text.split())
                item.role = "coach" if _is_coach_role(function) else "staff"
                item.function_de = _STAFF_DE.get(function.lower(), function)
            seen.add(name.lower())
            roster.rows.append(item)
    for staff in _staff_from_text(parser.outside):
        if staff.name.lower() not in seen:
            seen.add(staff.name.lower())
            roster.rows.append(staff)
    text = " ".join(parser.outside)
    if m := (_AS_OF.search(text) or _ANNOUNCED.search(text)):
        roster.as_of = m.group(1)
    elif newest_label:
        roster.as_of = newest_label if len(newest_label) <= 40 else str(newest)
    elif m := _ROSTER_INTRO.search(text):
        roster.as_of = m.group(1)
    return roster


_ROSTER_SECTION = re.compile(
    r"roster|kader|squad|current team|aktuelle[rs]? (?:team|mannschaft)|trupp|laguppställning|joukkue|"
    r"pelaajat|pelaajisto|kokoonpano|soupiska|hráčky|hráči|sestava|kádr|spillertrupp|rosa|effectif|kadra",
    re.I,
)
_NOT_ROSTER = re.compile(
    r"captain|history|geschichte|famous|notable|former|ehemalige|head coaches|trainer|historik|historia|"
    r"historie|tidigare|kända|entiset|tunnetut|bývalé|bývalí|známé|tränare|valmentaj|trenéř|flest|"
    r"ennätys|rekord",
    re.I,
)
_CURRENT = re.compile(r"current|aktuell|nuvarande|nykyinen|aktuální|attuale|actuel|nåværende|nuværende", re.I)


def roster_section_score(line: str) -> int:
    """'Current roster' (any language) > newest season/tournament roster > undated roster section."""
    years = _years(line)
    return (10_000 if _CURRENT.search(line) else 0) + (max(years) if years else 1)


def pick_roster_section(sections: list[dict]) -> dict | None:
    """The most current roster section: 'Current roster' over the newest season/tournament roster. The returned
    section carries its `score` (see `roster_section_score`)."""
    best, best_score = None, -1
    for s in sections:
        line = htmllib.unescape(re.sub(r"<[^>]+>", "", s.get("line", "")))
        if not _ROSTER_SECTION.search(line) or _NOT_ROSTER.search(line):
            continue
        score = roster_section_score(line)
        if score > best_score:
            best, best_score = {**s, "line": line, "score": score}, score
    return best


# position abbreviations per Wikipedia language (B is a defender in Swedish but a goalie in Czech)
POSITION_ABBR: dict[str, dict[str, str]] = {
    "sv": {"M": "G", "MV": "G", "B": "D", "F": "F", "C": "F", "LW": "F", "RW": "F"},
    "fi": {"MV": "G", "M": "G", "P": "D", "PL": "D", "H": "F", "HY": "F", "KH": "F", "LH": "F", "OH": "F"},
    "cs": {"B": "G", "BR": "G", "O": "D", "Ú": "F", "U": "F", "UT": "F"},
    "sk": {"B": "G", "BR": "G", "O": "D", "Ú": "F", "U": "F"},
    "de": {"T": "G", "TH": "G", "TW": "G", "V": "D", "VT": "D", "S": "F", "ST": "F"},
    "da": {"M": "G", "MV": "G", "B": "D", "F": "F", "A": "F"},
    "nb": {"K": "G", "M": "G", "B": "D", "F": "F", "A": "F"},
    "it": {"P": "G", "PO": "G", "DIF": "D", "ATT": "F", "A": "F", "D": "D"},
    "fr": {"GB": "G", "DÉF": "D", "DEF": "D", "AT": "F", "ATT": "F"},
}
