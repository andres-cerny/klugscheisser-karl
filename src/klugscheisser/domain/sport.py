"""Which sport is a text about? One decision for the news filter, team links and the rules classifier.

Why: generic words (Nati, WM, Spiel, Team, Saison, Goalie) are shared by every team sport, and outlets file
stories under the wrong URL section (SRF and Blick put women's FOOTBALL national-team stories under
``/sport/eishockey/…``; SRF redirects them to ``/sport/fussball/…``). So the decision weighs evidence:

1. Only *specific* cues count: words that name the sport or belong to it alone (Eishockey, Puck, Powerplay,
   IIHF, PWHL … / Fussball, FIFA, UEFA, ÖFB, SFV, Elfmeter, Abseits, Women's Euro …). Generic words are
   counted separately (``weak``) and never decide on their own.
2. The body (title + text) decides: more cues for another sport than for ice hockey -> that sport, whatever
   the URL says. A tie with cues on both sides is ``mixed``.
3. The URL section (``/eishockey/``, ``/fussball/``) only decides when the body names no sport at all.
4. Sport lists in boilerplate ("News aus der Welt des Sports: Fussball, Eishockey, Boxen, Tennis und mehr")
   are removed first: an enumeration of sports is not about any of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


def _rx(*parts: str) -> re.Pattern:
    return re.compile("|".join(parts), re.IGNORECASE)


# Words for "a sport" used in enumerations ("Fussball, Eishockey, Boxen, Tennis und mehr").
_SPORT_WORD = (
    r"(?:fu(?:ss|ß)ball|eishockey|hockey|unihockey|handball|basketball|volleyball|tennis|boxen|golf|ski|"
    r"skifahren|ski alpin|formel 1|motorsport|radsport|rad|leichtathletik|schwingen|curling|rudern|"
    r"schwimmen|turnen|reiten|football|soccer|ice hockey|cycling|athletics|skiing|basket)"
)
_ENUMERATION = re.compile(
    rf"\b{_SPORT_WORD}(?:\s*(?:,|/|&|\bund\b|\band\b|\bet\b|\be\b|\boder\b)\s*{_SPORT_WORD}){{2,}}\b"
    r"(?:\s*(?:,|und|and)\s*(?:mehr|more|vieles mehr|co\.?))?",
    re.IGNORECASE,
)

# Ice hockey. "hockey" alone counts unless it is another hockey (Unihockey, Landhockey, field hockey …).
HOCKEY_STRONG = _rx(
    r"(?<!uni)(?<!uni-)(?<!land)(?<!feld)(?<!roll)(?<!rollen)(?<!inline)(?<!inline-)(?<!street)"
    r"(?<!field )(?<!roller )(?<!floor)(?<!street )hockey",
    r"ishockey",  # sv / da / no
    r"j[aä][aä]kiekko",
    r"\bl[aä]tk[aä]",
    r"\bhokej",
    r"\bhokei",
    r"\bpuck",
    r"\bpower-?play",
    r"\bbox-?play",
    r"penalty-?killing",
    r"\bunterzahl-?spiel",  # (plain "in Unterzahl" is also football after a red card)
    r"\b(schluss|mittel|start|anfangs)drittel",
    r"\b(im|ins|nach dem|das) (ersten|zweiten|dritten|letzten|erste|zweite|dritte|letzte) drittel",
    r"\b(1\.|2\.|3\.) drittel",
    r"\biihf\b",
    r"\bsihf\b",
    r"swiss ice hockey",
    r"\bö?ehv\b",
    r"\böehv",
    r"\bdeb-",  # Deutscher Eishockey-Bund: DEB-Frauen, DEB-Auswahl
    r"\bpwhl\b",
    r"\bsdhl\b",
    r"\bswhl\b",
    r"\bnhl\b",
    r"\bkhl\b",
    r"\bahl\b",
    r"\bechl\b",
    r"\bwomen'?s league\b",  # Swiss women's hockey league (football's is the "Women's Super League")
    r"\bnational league\b",
    r"\bswiss league\b",
    r"stanley cup",
    r"\bkronorna\b",  # Tre Kronor / Damkronorna
    r"\bdamkronor",
    r"\b(nais)?leijon(at|ien|ia)\b",
    r"smile japan",
    r"\beis(halle|stadion|feld|fläche)",
    r"\b(auf dem|aufs|auf das) eis\b",
    r"\bon the ice\b",
    r"\bsur la glace\b",
    r"\bsul ghiaccio\b",
    # French / Italian (RTS, RSI): only words without a football/handball meaning
    r"\bpalets?\b",  # puck
    r"\bpatinoire",
    r"\btiers-temps\b",
    r"\b(premier|deuxième|second|troisième) tiers\b",
    r"\bghiaccio\b",  # sul ghiaccio, pista di ghiaccio
    r"\bpista del ghiaccio\b",
    r"\bweht\b",  # Women's Euro Hockey Tour
    r"\beht\b",
    r"\bshannon miller\b",  # Swiss women's national ice hockey coach
    r"\bshutout",
    r"\bempty-?nett?er",
    r"\bempty net",
    r"\bface-?off",
    r"🏒",
)

# Generic team-sport words: reported, but never enough on their own.
HOCKEY_WEAK = _rx(
    r"\bnati\b",
    r"\bwm\b",
    r"world championship",
    r"\bolymp",
    r"\bgoalie",
    r"\btendy",
    r"torhüter",
    r"verteidig",
    r"stürm",
    r"\bteam\b",
    r"\bsaison",
    r"\bseason",
    r"spiel",
    r"\bgame\b",
    r"\bliga\b",
    r"\bleague\b",
    r"\bncaa\b",
    r"🥅",
)

# Other sports: cues specific enough to say "this is NOT ice hockey".
OTHER_STRONG: dict[str, re.Pattern] = {
    "football": _rx(
        r"fu(ss|ß)ball",
        r"\bsoccer\b",
        r"(?<!american )\bfootball\b",
        r"\bfootballeuse",
        r"\bcalcio\b",
        r"\bcalciatric",
        r"\bfotboll",
        r"\bfodbold",
        r"\bfotball",
        r"\bfotbal",
        r"\bjalkapallo",
        r"piłk[ai] nożn",
        r"\bfifa\b",
        r"\bfifa-",
        r"\buefa\b",
        r"\buefa-",
        r"\böfb\b",
        r"\böfb-",
        r"\bsfv\b",
        r"\bsfv-",
        r"\basf\b",
        r"\bdfb\b",
        r"\bdfb-",
        r"women'?s (euro(?! hockey)|super league|champions league|world cup|european qualifiers|nations league)",
        r"\bweuro",
        r"\bawsl\b",
        r"\bnwsl\b",
        r"(?<!hockey )champions league",
        r"\bnations league",
        r"\bsuper league\b",
        r"\bchallenge league\b",
        r"frauen-bundesliga",
        r"\belfmeter",
        r"\babseits",
        r"\beckball",
        r"\bfreistoss",
        r"\bfreistoß",
        r"\bkopfball",
        r"\bflanke",
        r"\bnachspielzeit",
        r"\bhalbzeit",
        r"\bclean sheet",
        r"\bwankdorf",
        r"\bletzigrund",
        r"stade de suisse",
        r"st\.? jakob-park",
        r"\bjoggeli\b",
        r"\bkybunpark\b",
        r"\bstockhorn arena\b",
        # the 2027 Women's World Cup is in Brazil; ice hockey world championships never are
        r"\b(wm|weltmeisterschaft|world cup|coupe du monde|mondiali)\b[^.\n]{0,60}\bbrasilien|"
        r"\bbrasilien\b[^.\n]{0,20}\b2027\b|\bbrazil 2027\b",
        # Swiss women's football national team: coaches and players by full name (no surname-only:
        # Kathrin Lehmann played both sports, other surnames have hockey namesakes)
        r"\blia w(ä|ae)lti\b",
        r"\bcaptain w(ä|ae)lti\b",
        r"\bana-maria crnogor",
        r"\bramona bachmann\b",
        r"\briola xhemaili\b",
        r"\bg(é|e)raldine reuteler\b",
        r"\bcoumba sow\b",
        r"\blivia peng\b",
        r"\bnadine riesen\b",
        r"\bsydney schertenleib\b",
        r"\biman beney\b",
        r"\bsvenja f(ö|oe)lmli\b",
        r"\balisha lehmann\b",
        r"\bpia sundhage\b",
        r"\brafel navarro\b",
        r"\bnavarro-team\b",
    ),
    # no women's ice hockey European championship exists, so an "EM" is another sport
    "other": _rx(
        r"\bem\b(?!-?\s?hockey)",
        r"\bfrauen-em\b",
        r"europameisterschaft",
        r"\beuro 20\d\d\b",
        r"championnat d'europe",
        r"\beuropei\b",
    ),
    "unihockey": _rx(
        r"unihockey",
        r"\bfloorball",
        r"innebandy",
        r"salibandy",
        r"field hockey",
        r"landhockey",
        r"feldhockey",
    ),
    "handball": _rx(r"handball"),
    "basketball": _rx(r"basketball", r"\bwnba\b"),
    "volleyball": _rx(r"volleyball", r"beachvolley"),
    "tennis": _rx(r"\btennis", r"\bwta\b", r"\batp\b", r"grand slam", r"wimbledon", r"roland garros"),
    "ski": _rx(
        r"riesenslalom",
        r"\bslalom",
        r"\bsuper-g\b",
        r"\bski alpin",
        r"\bskirennen",
        r"\bskifahrerin",
        r"\bski-weltcup",
        r"\bswiss-ski\b",
        r"\bbiathlon",
        r"\blanglauf",
    ),
    "curling": _rx(r"\bcurling"),
    "figure_skating": _rx(r"eiskunstlauf", r"figure skating", r"patinage artistique"),
    "cycling": _rx(r"\bradrennen", r"tour de france", r"\bradsport"),
    "motorsport": _rx(r"formel 1", r"\bformula 1\b", r"\bgrand prix\b", r"\bf1\b"),
}

_URL_HOCKEY = re.compile(r"/(eishockey|hockey|ice-?hockey|hockey-sur-glace|ishockey)(/|-|$)", re.I)
_URL_OTHER = {
    "football": re.compile(r"/(fussball|football|soccer|calcio|fotboll|fodbold|fotball)(/|-|$)", re.I),
    "ski": re.compile(r"/(ski|ski-alpin|wintersport/ski)(/|-|$)", re.I),
    "tennis": re.compile(r"/tennis(/|-|$)", re.I),
    "unihockey": re.compile(r"/(unihockey|floorball)(/|-|$)", re.I),
    "handball": re.compile(r"/handball(/|-|$)", re.I),
}


# 2026-11-24, floorball project ("unihockey"): the same check with floorball as the home sport. Ice hockey is then
# "another sport", but only with cues that floorball does not share (floorball has Powerplay, Boxplay, Drittel,
# Goalie, Shutout too: those are generic words there).
FLOORBALL_STRONG = _rx(
    r"unihockey",
    r"\bfloorball",
    r"innebandy",
    r"salibandy",
    r"\bflorbal",
    r"\bunihockeyaner",
    r"\bswiss unihockey\b",
    r"\biff\b",
    r"\bwfc\b",
    r"\bupl\b",  # Unihockey Prime League
    r"prime league",
    r"\bf-?liiga\b",
    r"\bsuperligan\b",
    r"\bsalibandyliiga\b",
    r"\bsuperliga\b",
    r"\beuro floorball tour\b",
    r"\beft\b",
    r"\bchampions cup\b",
    r"\bunihockey-?schl(ä|ae)ger",
    r"\bkleinfeld\b",
    r"\bgrossfeld\b",
)
ICEHOCKEY_ONLY = _rx(
    r"eishockey",
    r"ice hockey",
    r"(?<!inne)(?<!inne-)\bishockey",
    r"j[aä][aä]kiekko",
    r"\bl[aä]tk[aä]",
    r"\bhokej",
    r"\bpuck",
    r"\biihf\b",
    r"\bsihf\b",
    r"swiss ice hockey",
    r"\bnhl\b",
    r"\bkhl\b",
    r"\bahl\b",
    r"\bpwhl\b",
    r"\bsdhl\b",
    r"stanley cup",
    r"\btre kronor\b",
    r"\beis(halle|stadion|feld|fläche)",
    r"\b(auf dem|aufs|auf das) eis\b",
    r"\bon the ice\b",
    r"\bpatinoire",
    r"🏒",
)
FLOORBALL_WEAK = _rx(
    HOCKEY_WEAK.pattern, r"\bpower-?play", r"\bbox-?play", r"drittel", r"\bshutout", r"\bhockey\b"
)
FLOORBALL_OTHER: dict[str, re.Pattern] = {
    **{k: v for k, v in OTHER_STRONG.items() if k != "unihockey"},
    "icehockey": ICEHOCKEY_ONLY,
    "field_hockey": _rx(r"field hockey", r"landhockey", r"feldhockey", r"hallenhockey"),
}
_URL_FLOORBALL = re.compile(r"/(unihockey|floorball|innebandy|salibandy|florbal)(/|-|$)", re.I)
_URL_FLOORBALL_OTHER = {
    **{k: v for k, v in _URL_OTHER.items() if k != "unihockey"},
    "icehockey": _URL_HOCKEY,
}


@dataclass(frozen=True)
class SportCues:
    """The cues of a project's home sport ("hockey" for ice hockey, "unihockey" for floorball)."""

    home: str
    strong: re.Pattern
    weak: re.Pattern
    other: dict[str, re.Pattern]
    url_home: re.Pattern
    url_other: dict[str, re.Pattern]


ICEHOCKEY_CUES = SportCues("hockey", HOCKEY_STRONG, HOCKEY_WEAK, OTHER_STRONG, _URL_HOCKEY, _URL_OTHER)
FLOORBALL_CUES = SportCues(
    "unihockey", FLOORBALL_STRONG, FLOORBALL_WEAK, FLOORBALL_OTHER, _URL_FLOORBALL, _URL_FLOORBALL_OTHER
)
CUES = {"hockey": ICEHOCKEY_CUES, "unihockey": FLOORBALL_CUES}


def project_cues() -> SportCues:
    """Cues of the current project's sport; projects without one (Allgemein) and other families: ice hockey."""
    try:
        from klugscheisser.sports import current_spec

        return CUES.get(current_spec().family, ICEHOCKEY_CUES)
    except Exception:  # no projects file / settings in a bare unit test
        return ICEHOCKEY_CUES


def strip_enumerations(text: str) -> str:
    """Remove lists of sports ("Fussball, Eishockey, Boxen, Tennis und mehr"): they are about no sport."""
    return _ENUMERATION.sub(" ", text or "")


@dataclass(frozen=True)
class SportVerdict:
    """``sport``: "hockey", another sport key ("football", "ski" …), "mixed" or "unknown".
    ``source``: what decided it — "body", "url" or "none"."""

    sport: str
    source: str
    hockey: int = 0
    weak: int = 0
    other: dict[str, int] = field(default_factory=dict)
    url_sport: str | None = None
    home: str = (
        "hockey"  # the project's sport ("unihockey" in the floorball project); `hockey` counts its cues
    )

    @property
    def is_hockey(self) -> bool:
        """About the project's sport (ice hockey; floorball in the floorball project)."""
        return self.sport == self.home

    @property
    def is_other(self) -> bool:
        """Clearly another sport (not hockey, not mixed, not unknown)."""
        return self.sport not in (self.home, "mixed", "unknown")

    @property
    def names_hockey(self) -> bool:
        """Ice hockey, or a mix in which hockey has as many cues as any other sport."""
        return self.sport in (self.home, "mixed")

    def looks_hockey(self) -> bool:
        """Lenient check for short texts (bios): ice hockey, or generic sport words and nothing else."""
        return self.is_hockey or (self.sport == "unknown" and self.weak > 0)


def url_sport(url: str | None, cues: SportCues | None = None) -> str | None:
    if not url:
        return None
    cues = cues or project_cues()
    path = re.sub(r"^[a-z]+://[^/]+", "", url.strip(), flags=re.I)
    if cues.url_home.search(path):
        return cues.home
    for sport, rx in cues.url_other.items():
        if rx.search(path):
            return sport
    return None


def sport_verdict(text: str, url: str | None = None, cues: SportCues | None = None) -> SportVerdict:
    """`cues`: the home sport (default: the current project's sport; ice hockey for Eishockey and Allgemein)."""
    cues = cues or project_cues()
    home = cues.home
    body = strip_enumerations(text or "")
    hockey = len(cues.strong.findall(body))
    weak = len(cues.weak.findall(body))
    other = {k: n for k, rx in cues.other.items() if (n := len(rx.findall(body)))}
    from_url = url_sport(url, cues)
    top, top_n = max(other.items(), key=lambda kv: kv[1]) if other else (None, 0)
    if hockey or top_n:
        if hockey > top_n:
            sport = home
        elif top_n > hockey:
            sport = top
        else:  # cues for both: the URL may break the tie, but only towards a sport the body names
            sport = from_url if from_url in ({home} | set(other)) else "mixed"
        return SportVerdict(sport, "body", hockey, weak, other, from_url, home)
    if from_url:
        return SportVerdict(from_url, "url", hockey, weak, other, from_url, home)
    return SportVerdict("unknown", "none", hockey, weak, other, from_url, home)


# Multi-sport tickers ("Brasilianer ersetzt Ocon bei Haas +++ Schweizerinnen gegen Österreich in Bern"): one item,
# many stories, so nothing in it can be pinned to one team.
_TICKER = re.compile(r"\+\+\+|sport-?news-?ticker|sport-?ticker|news-?ticker|ticker-?sport", re.IGNORECASE)


def is_ticker(text: str, url: str | None = None) -> bool:
    """A sports ticker / round-up of several stories ("+++", "Sportnews-Ticker"): never links teams.
    (A plain sport list alone is not enough: site navigation lists sports too; strip_enumerations handles it.)"""
    return bool(_TICKER.search(f"{url or ''}\n{text or ''}"))


def is_hockey(text: str, url: str | None = None) -> bool:
    return sport_verdict(text, url).is_hockey
