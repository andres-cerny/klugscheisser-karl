"""v5 P4: Wikipedia by home language, home-language rosters, women's team coaches, Wikidata bulk seeding.

Recorded fixtures (tests/fixtures/v5wiki, trimmed, 2026-10-03): sv + cs national-team roster sections,
Colin Muller (Q16540791) Wikidata rows, bulk SPARQL results (Swiss alpine skiers, Swedish hockey players).
Small synthetic API answers are used where only the request flow matters.
"""

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from klugscheisser.adds.person import CoachedTeam, WikiPersonResolver, parse_coached
from klugscheisser.adds.team import TeamEntity, TeamRosterResolver, _position
from klugscheisser.adds.wiki import (
    WikiClient,
    parse_roster_html,
    parse_text_date,
    pick_roster_section,
)
from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector, language_order
from klugscheisser.collectors.wikidata import (
    WikidataCollector,
    WikidataPlayer,
    WikidataTeamMembership,
    parse_memberships,
    parse_players,
    split_active,
)
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.profiles import ingest_profiles
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_wikidata_players, seed_wikidata_bulk
from klugscheisser.sports import citizenship_qids, get_sport

FIX = Path("tests/fixtures/v5wiki")
ADDS = Path("tests/fixtures/adds")
TODAY = date(2026, 10, 3)


def polite(transport=None) -> PoliteClient:
    client = httpx.Client(transport=transport) if transport else None
    return PoliteClient(client=client, min_delay_s=0, sleep=lambda s: None)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


# --- 1. Wikipedia by home language ------------------------------------------------------------------


def test_language_order_home_language_first():
    assert language_order("SWE") == ["sv", "de", "en", "fr"]
    assert language_order("FIN") == ["fi", "sv", "de", "en", "fr"]
    assert language_order("SUI") == ["de", "en", "fr", "it"]
    assert language_order("CZE")[:3] == ["cs", "de", "en"]
    assert language_order("NOR")[0] == "nb"
    assert language_order("CAN") == ["en", "fr", "de"]
    assert language_order("AND")[:2] == ["ca", "es"]
    assert language_order("JPN") == ["de", "en", "fr"]  # Japanese is not read
    assert language_order(None) == ["de", "en", "fr"]
    assert language_order("SUI", trailing=False) == ["de", "en", "fr"]


class WikiApi:
    """Wikidata sitelinks + Wikipedia extract/summary/search per host, from small dicts."""

    def __init__(self, sitelinks: dict[str, str], articles: dict[str, tuple[str, str]], search=None):
        self.sitelinks = sitelinks  # site -> title
        self.articles = articles  # host -> (title, extract)
        self.search = search or {}  # host -> [titles]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url, params = request.url, request.url.params
        if url.host == "www.wikidata.org":
            wanted = params["sitefilter"].split("|")
            links = {s: {"site": s, "title": t} for s, t in self.sitelinks.items() if s in wanted}
            return httpx.Response(200, json={"entities": {params["ids"]: {"sitelinks": links}}})
        title, extract = self.articles.get(url.host, ("", ""))
        if "/rest_v1/page/summary/" in url.path:
            return httpx.Response(
                200,
                json={
                    "type": "standard",
                    "title": title,
                    "extract": extract[:100],
                    "timestamp": "2026-09-01T10:00:00Z",
                    "content_urls": {
                        "desktop": {"page": f"https://{url.host}/wiki/{title.replace(' ', '_')}"}
                    },
                },
            )
        if params.get("list") == "search":
            hits = [{"title": t} for t in self.search.get(url.host, [])]
            return httpx.Response(200, json={"query": {"search": hits}})
        if params.get("prop", "").startswith("extracts"):
            if not title:
                return httpx.Response(200, json={"query": {"pages": [{"missing": True}]}})
            return httpx.Response(200, json={"query": {"pages": [{"title": title, "extract": extract}]}})
        return httpx.Response(404, json={})


SWEDE_ARTICLES = {
    "sv.wikipedia.org": ("Hanna Olsson", "Hanna Olsson, född 1999, är en svensk ishockeyspelare (forward)."),
    "de.wikipedia.org": ("Hanna Olsson", "Hanna Olsson (* 1999) ist eine schwedische Eishockeyspielerin."),
    "en.wikipedia.org": ("Hanna Olsson", "Hanna Olsson (born 1999) is a Swedish ice hockey player."),
    "fr.wikipedia.org": ("Hanna Olsson", "Hanna Olsson est une joueuse suédoise de hockey sur glace."),
    "fi.wikipedia.org": ("Hanna Olsson", "Hanna Olsson on ruotsalainen jääkiekkoilija."),
}


def test_wikipedia_with_qid_reads_home_language_first_and_caps():
    api = WikiApi(
        {
            "svwiki": "Hanna Olsson",
            "dewiki": "Hanna Olsson",
            "enwiki": "Hanna Olsson",
            "fiwiki": "Hanna Olsson",
        },
        SWEDE_ARTICLES,
    )
    collector = WikipediaCollector(polite(httpx.MockTransport(api)), max_languages=3)
    docs = collector.collect("Hanna Olsson", "icehockey_women", {"wikidata_qid": "Q1", "nationality": "SWE"})
    assert [d.language for d in docs] == ["sv", "de", "en"]  # fi is linked but not a Swedish home language
    sv = docs[0]
    assert sv.source_name == "Wikipedia (sv)"
    assert sv.url == "https://sv.wikipedia.org/wiki/Hanna_Olsson"
    assert sv.text.startswith("Hanna Olsson, född 1999")
    sitefilter = api.requests[0].url.params["sitefilter"]
    assert sitefilter == "svwiki|dewiki|enwiki|frwiki"


def test_wikipedia_norwegian_is_no_wikipedia():
    api = WikiApi(
        {"nowiki": "Kari Nordmann"},
        {"no.wikipedia.org": ("Kari Nordmann", "Kari Nordmann er en norsk alpinist.")},
    )
    docs = WikipediaCollector(polite(httpx.MockTransport(api))).collect(
        "Kari Nordmann", "ski_alpine_women", {"wikidata_qid": "Q2", "nationality": "NOR"}
    )
    assert [(d.language, d.source_name) for d in docs] == [("nb", "Wikipedia (nb)")]
    assert docs[0].url.startswith("https://no.wikipedia.org/")
    assert "nowiki" in api.requests[0].url.params["sitefilter"]


def test_wikipedia_search_fallback_tries_home_language_first_and_rejects_namesakes():
    articles = {
        "sv.wikipedia.org": ("Hanna Olsson", SWEDE_ARTICLES["sv.wikipedia.org"][1]),
        "de.wikipedia.org": ("Hanna Olsson (Politikerin)", "Hanna Olsson ist eine schwedische Politikerin."),
    }
    api = WikiApi(
        {},
        articles,
        search={"sv.wikipedia.org": ["Hanna Olsson"], "de.wikipedia.org": ["Hanna Olsson (Politikerin)"]},
    )
    docs = WikipediaCollector(polite(httpx.MockTransport(api)), languages=["de"]).collect(
        "Hanna Olsson", "icehockey_women", {"nationality": "SWE"}
    )
    assert [d.language for d in docs] == ["sv"]  # the German politician is a namesake
    assert api.requests[0].url.host == "sv.wikipedia.org"
    assert "ishockey" in api.requests[0].url.params["srsearch"]


def test_ingest_profiles_keeps_the_language(db):
    api = WikiApi({"svwiki": "Hanna Olsson", "enwiki": "Hanna Olsson"}, SWEDE_ARTICLES)
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        hanna = repo.upsert_athlete(s, sport, "Hanna Olsson", nationality="SWE", wikidata_qid="Q19972549")
        ctx = build_context(s, settings())
        report = ingest_profiles(ctx, [hanna], [WikipediaCollector(polite(httpx.MockTransport(api)))])
        s.commit()
        assert report.by_source == {"Wikipedia (sv)": 1, "Wikipedia (en)": 1}
        items = s.scalars(select(m.Item).where(m.Item.platform == "wikipedia")).all()
        assert sorted(i.raw["language"] for i in items) == ["en", "sv"]
        sv = next(i for i in items if i.raw["language"] == "sv")
        assert sv.source_name == "Wikipedia (sv)" and sv.url.startswith("https://sv.wikipedia.org/")


# --- 2. Rosters from the home language --------------------------------------------------------------


def test_swedish_roster_section_takes_only_the_newest_table():
    html = (FIX / "roster_sweden_sv.html").read_text()
    roster = parse_roster_html(html, lambda label: _position(label, "sv"))
    names = [r.name for r in roster.rows]
    assert roster.as_of == "VM-laget 2019"
    assert "Cecilia Andersson" not in names  # 2006 Olympic team in the same section
    assert names[:2] == ["Sara Grahn", "Julia Åberg"]
    grahn, waxin = roster.rows[0], roster.rows[3]
    assert (grahn.number, grahn.position, grahn.team) == (1, "G", "Luleå HF")  # MV = målvakt
    assert (waxin.name, waxin.position) == ("Mina Waxin", "D")  # B = back
    assert roster.rows[1].wikipedia_title is None  # red link


def test_czech_roster_with_text_dates_and_coach_line():
    html = (FIX / "roster_czechia_cs.html").read_text()
    roster = parse_roster_html(html, lambda label: _position(label, "cs"))
    players = [r for r in roster.rows if r.role == "athlete"]
    first = players[0]
    assert (first.name, first.number, first.position) == ("Viktorie Švejdová", 1, "G")
    assert first.birth_date == date(2002, 6, 24)  # "24. června 2002"
    assert players[1].name == "Aneta Tejralová"  # without the hidden sort key and "– A"
    assert players[1].birth_date == date(1996, 1, 4)
    coach = next(r for r in roster.rows if r.role == "coach")
    assert (coach.name, coach.function_de) == ("Carla MacLeodová", "Cheftrainer/in")
    assert roster.as_of == "2022"


def test_swedish_club_roster_positions_from_group_titles():
    html = (FIX / "roster_brynas_sv.html").read_text()  # sv: Brynäs IF Dam, "Truppen 2025/2026"
    roster = parse_roster_html(html, lambda label: _position(label, "sv"))
    assert [(r.name, r.position) for r in roster.rows] == [
        ("Ena Nystrøm", "G"),
        ("Klára Peslarová", "G"),
        ("Anna Andersson", "D"),
        ("Oona Koukkula", "D"),
        ("Jenny Antonsson", "F"),
        ("Tilda Edsman", "F"),
    ]
    assert roster.rows[0].birth_date == date(2000, 4, 28)  # "28 april 2000 (26 år)"
    assert roster.rows[0].number == 31


def test_finnish_and_german_headers_and_positions():
    html = """
    <table class="wikitable"><tr><th>Nro</th><th>Pelipaikka</th><th>Nimi</th><th>Syntymäaika</th>
    <th>Seura</th></tr>
    <tr><td>18</td><td>MV</td><td><a href="/wiki/Anni_Keisala" title="Anni Keisala">Anni Keisala</a></td>
    <td>12. maaliskuuta 1997</td><td>HPK</td></tr>
    <tr><td>4</td><td>P</td><td>Ronja Savolainen</td><td>29. marraskuuta 1997</td><td>Luleå HF</td></tr>
    <tr><td>12</td><td>H</td><td>Petra Nieminen</td><td>4. toukokuuta 1999</td><td>Luleå HF</td></tr>
    </table><p>Päävalmentaja: Juuso Toivola</p>
    """
    roster = parse_roster_html(html, lambda label: _position(label, "fi"))
    assert [(r.name, r.position, r.birth_date) for r in roster.rows[:3]] == [
        ("Anni Keisala", "G", date(1997, 3, 12)),
        ("Ronja Savolainen", "D", date(1997, 11, 29)),
        ("Petra Nieminen", "F", date(1999, 5, 4)),
    ]
    assert roster.rows[0].team == "HPK" and roster.rows[0].number == 18
    assert (roster.rows[3].name, roster.rows[3].role, roster.rows[3].function_de) == (
        "Juuso Toivola",
        "coach",
        "Cheftrainer/in",
    )
    de = parse_roster_html(
        "<table><tr><th>Nr.</th><th>Pos.</th><th>Name</th><th>Geburtsdatum</th><th>Verein</th></tr>"
        "<tr><td>29</td><td>T</td><td>Andrea Brändli</td><td>11. März 1997</td><td>Ohio State</td></tr></table>",
        lambda label: _position(label, "de"),
    )
    assert (de.rows[0].position, de.rows[0].birth_date) == ("G", date(1997, 3, 11))


def test_parse_text_date_languages():
    assert parse_text_date("24.\xa0června 2002 (20\xa0let)") == date(2002, 6, 24)
    assert parse_text_date("12 mars 1999") == date(1999, 3, 12)
    assert parse_text_date("4. heinäkuuta 2001") == date(2001, 7, 4)
    assert parse_text_date("March 12, 1999") == date(1999, 3, 12)
    assert parse_text_date("12.3.1999") == date(1999, 3, 12)
    assert parse_text_date("1999-03-12") == date(1999, 3, 12)
    assert parse_text_date("31. února 2000") is None  # no such day
    assert parse_text_date("") is None


def test_pick_roster_section_multilingual():
    sv = pick_roster_section(
        [
            {"index": "1", "line": "Historik"},
            {"index": "2", "line": "Tidigare spelare"},
            {"index": "3", "line": "Nuvarande trupp"},
        ]
    )
    assert sv["index"] == "3" and sv["score"] > 10_000
    cs = pick_roster_section([{"index": "5", "line": "Hráčky"}, {"index": "6", "line": "Trenéři"}])
    assert cs["index"] == "5" and cs["score"] == 1
    fi = pick_roster_section(
        [{"index": "7", "line": "Joukkue MM-kisoissa 2025"}, {"index": "9", "line": "Valmentajia"}]
    )
    assert fi["index"] == "7" and fi["score"] == 2025


class TeamWiki:
    """Wikidata sitelinks + Wikipedia sections/section HTML for the team roster flow."""

    def __init__(self, sitelinks: dict[str, str], sections: dict[str, list[dict]], html: dict[str, str]):
        self.sitelinks, self.sections, self.html = sitelinks, sections, html
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, params = request.url, request.url.params
        self.requests.append(f"{url.host} {params.get('action')} {params.get('prop')}")
        if url.host == "www.wikidata.org":
            wanted = params["sitefilter"].split("|")
            links = {s: {"title": t} for s, t in self.sitelinks.items() if s in wanted}
            return httpx.Response(200, json={"entities": {params["ids"]: {"sitelinks": links}}})
        lang = url.host.split(".")[0]
        if params.get("prop") == "sections":
            secs = self.sections.get(lang)
            if secs is None:
                return httpx.Response(200, json={"error": {"code": "missingtitle"}})
            return httpx.Response(200, json={"parse": {"title": params["page"], "sections": secs}})
        name = self.html.get(f"{lang}|{params.get('section')}")
        text = name and Path(name).read_text()
        return httpx.Response(200, json={"parse": {"title": params["page"], "text": text or ""}})


SWEDEN = TeamEntity(
    qid="Q174641",
    label="Sweden women's national ice hockey team",
    instances=["women's national ice hockey team"],
    nations=["SWE"],
    enwiki="https://en.wikipedia.org/wiki/Sweden_women%27s_national_ice_hockey_team",
    dewiki="https://de.wikipedia.org/wiki/Schwedische_Eishockeynationalmannschaft_der_Frauen",
)
EN_SECTIONS = [{"index": "9", "line": "2026 Olympics roster", "anchor": "2026_Olympics_roster"}]


def resolver(handler) -> TeamRosterResolver:
    return TeamRosterResolver(settings(), wiki=WikiClient(polite(httpx.MockTransport(handler))), llm=None)


def test_team_roster_prefers_a_current_home_language_roster():
    handler = TeamWiki(
        {"svwiki": "Sveriges damlandslag i ishockey"},
        {"sv": [{"index": "7", "line": "Nuvarande trupp"}], "en": EN_SECTIONS, "de": []},
        {"sv|7": str(FIX / "roster_sweden_sv.html"), "en|9": str(ADDS / "roster_sweden_en.html")},
    )
    roster, source, section, lang = resolver(handler).wikipedia_roster(SWEDEN, "SWE")
    assert lang == "sv" and section == "Nuvarande trupp"
    assert source.label == "Wikipedia (sv): Sveriges damlandslag i ishockey – Nuvarande trupp"
    assert source.url.startswith("https://sv.wikipedia.org/wiki/Sveriges_damlandslag_i_ishockey#")
    assert roster.rows[0].name == "Sara Grahn"
    assert handler.requests[0].startswith("www.wikidata.org")  # sitelink of the home language first


def test_team_roster_undated_home_section_loses_to_newer_english_roster():
    handler = TeamWiki(
        {"svwiki": "Sveriges damlandslag i ishockey"},
        {"sv": [{"index": "7", "line": "Laguppställningar (urval)"}], "en": EN_SECTIONS, "de": []},
        {"sv|7": str(FIX / "roster_sweden_sv.html"), "en|9": str(ADDS / "roster_sweden_en.html")},
    )
    roster, _source, section, lang = resolver(handler).wikipedia_roster(SWEDEN, "SWE")
    assert (lang, section) == ("en", "2026 Olympics roster")
    assert roster.rows


def test_team_roster_czech_only_on_cswiki():
    czechia = TeamEntity(
        qid="Q1137285", label="Czech Republic women's national ice hockey team", nations=["CZE"]
    )
    handler = TeamWiki(
        {"cswiki": "Česká ženská hokejová reprezentace"},
        {"cs": [{"index": "5", "line": "Hráčky"}]},
        {"cs|5": str(FIX / "roster_czechia_cs.html")},
    )
    roster, source, _section, lang = resolver(handler).wikipedia_roster(czechia, "CZE")
    assert lang == "cs" and source.label.startswith("Wikipedia (cs): Česká ženská hokejová reprezentace")
    assert roster.rows[0].birth_date == date(2002, 6, 24)
    assert resolver(handler).roster_languages("SUI") == ["de", "fr", "it", "en"]


# --- 3. Coaches of women's teams ----------------------------------------------------------------------


class ColinWiki:
    """Recorded Wikidata answers for 'Colin Muller' (search, entity rows, coached-teams rows)."""

    def __init__(self) -> None:
        self.data = json.loads((FIX / "colin_muller.json").read_text())

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, params = request.url, request.url.params
        if url.host == "www.wikidata.org":
            hits = self.data["search"].get(f"{params['language']}|{params['search']}", [])
            return httpx.Response(200, json={"search": hits})
        if url.host == "query.wikidata.org":
            query = parse_qs(request.content.decode())["query"][0]
            rows = self.data["coached"] if "P1344" in query else self.data["persons"]
            return httpx.Response(200, json={"results": {"bindings": rows}})
        return httpx.Response(404, json={})  # Wikipedia summaries: not needed


def test_colin_muller_is_coach_of_the_swiss_women_team(db):
    wiki = WikiClient(polite(httpx.MockTransport(ColinWiki())))
    r = WikiPersonResolver(settings(), wiki=wiki, web_search=None, jev=None, instagram_source=None)
    with Session(db) as s:
        proposal = r.propose(s, "Colin Muller")
    assert proposal.found
    colin = proposal.persons[0]
    assert (colin.wikidata_qid, colin.role) == ("Q16540791", "coach")
    assert colin.sport_key == "icehockey_women" and colin.sport_name_de == "Eishockey Frauen"
    assert colin.function_de == "Cheftrainer Frauen-Nationalteam Schweiz"
    assert "Männer" not in proposal.summary
    assert "Cheftrainer Frauen-Nationalteam Schweiz (Eishockey Frauen)" in proposal.summary
    assert any(src.label.startswith("Wikidata Q676135") for src in colin.sources)  # the team


def test_parse_coached_three_wikidata_paths():
    e = "http://www.wikidata.org/entity/"
    rows = [
        # P286 head coach on a men's club
        {"p": {"value": e + "Q1"}, "team": {"value": e + "Q10"}, "teamEn": {"value": "EV Zug"}},
        # P6087 on the person: assistant coach of a women's club
        {
            "p": {"value": e + "Q2"},
            "team": {"value": e + "Q20"},
            "teamEn": {"value": "ZSC Lions Frauen"},
            "role": {"value": "assistant coach"},
            "when": {"value": "2025-08-01T00:00:00Z"},
        },
    ]
    rows = [{k: v["value"] for k, v in r.items()} for r in rows]
    rows[0]["role"] = "head coach"
    found = parse_coached(rows)
    zug = found["Q1"][0]
    assert not zug.women and zug.function_de(False) == "Cheftrainer EV Zug"
    zsc = found["Q2"][0]
    assert zsc.women and zsc.function_de(True) == "Assistenztrainerin Frauen ZSC Lions Frauen"
    national = CoachedTeam(
        "Q676135", "Switzerland women's national ice hockey team", women=True, national=True
    )
    national.nation = "SUI"
    assert national.function_de(None) == "Trainer/in Frauen-Nationalteam Schweiz"


# --- 4. Wikidata bulk seeding -------------------------------------------------------------------------


def bulk_fixture(name: str, sport_key: str, nation: str) -> list[WikidataPlayer]:
    sport = get_sport(sport_key)
    players = parse_players(json.loads((FIX / f"bulk_{name}_players.json").read_text()), nation, sport)
    parse_memberships(json.loads((FIX / f"bulk_{name}_memberships.json").read_text()), players)
    return list(players.values())


def test_ski_alpine_sports_registered():
    women, men = get_sport("ski_alpine_women"), get_sport("ski_alpine_men")
    assert (women.name_de, women.person_de) == ("Ski alpin Frauen", "Skirennfahrerin")
    assert (men.name_de, men.person_de, men.wikidata_gender) == (
        "Ski alpin Männer",
        "Skirennfahrer",
        "Q6581097",
    )
    assert women.wikidata_occupation == men.wikidata_occupation == "Q4144610"  # alpine skier


def test_split_active_filters_old_and_retired():
    players = [
        WikidataPlayer("Q1", "Aktive", "SUI", birth_date=date(1999, 1, 1)),
        WikidataPlayer("Q2", "Zu alt", "SUI", birth_date=date(1979, 12, 31)),
        WikidataPlayer("Q3", "Rücktritt 2020", "SUI", birth_date=date(1990, 1, 1), work_end=date(2020, 1, 1)),
        WikidataPlayer("Q4", "Rücktritt 2024", "SUI", birth_date=date(1990, 1, 1), work_end=date(2024, 1, 1)),
        WikidataPlayer(
            "Q5", "Ohne Geburtsdatum", "SUI", instagram=["x"], teams=[WikidataTeamMembership("Q9", "EVZ")]
        ),
    ]
    active, count = split_active(players, "SUI", TODAY)
    assert [p.qid for p in active] == ["Q1", "Q4", "Q5"]
    assert (count.found, count.active, count.born_before, count.retired) == (5, 3, 1, 1)
    assert (count.no_birth_date, count.with_instagram, count.with_current_team) == (1, 1, 1)


def test_collect_bulk_queries_and_chunks():
    queries: list[str] = []
    players = json.loads((FIX / "bulk_hockey_women_swe_players.json").read_text())
    members = json.loads((FIX / "bulk_hockey_women_swe_memberships.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(request.content.decode())["query"][0]
        queries.append(query)
        return httpx.Response(200, json=members if "VALUES ?p" in query else players)

    collector = WikidataCollector(httpx.Client(transport=httpx.MockTransport(handler)))
    found = collector.collect_bulk(get_sport("icehockey_women"), "SWE", "Q34", chunk=2)
    assert len(found) == 5 and len(queries) == 1 + 3  # 5 persons, memberships in chunks of 2
    assert "wd:Q11774891" in queries[0] and "wd:Q34" in queries[0] and ">= 1980" in queries[0]
    assert "wd:Q41466" in queries[1]  # only ice hockey teams (or teams without sport)
    men = WikidataCollector(httpx.Client(transport=httpx.MockTransport(handler)))
    queries.clear()
    men.collect_bulk(get_sport("ski_alpine_men"), "AUT", "Q40")
    assert "wd:Q4144610" in queries[0] and "wd:Q6581097" in queries[0] and "wd:Q186222" in queries[1]
    queries.clear()
    men.collect_bulk(get_sport("icehockey_women"), "DEN", citizenship_qids("DEN"))
    assert "VALUES ?country { wd:Q35 wd:Q756617 }" in queries[0]  # Danish players: Kingdom of Denmark


def test_load_bulk_players_into_their_sport(db):
    skiers = bulk_fixture("ski_women_sui", "ski_alpine_women", "SUI")
    hockey = bulk_fixture("hockey_women_swe", "icehockey_women", "SWE")
    with Session(db) as s:
        stats = load_wikidata_players(s, skiers, sport_key="ski_alpine_women")
        load_wikidata_players(s, hockey)
        s.commit()
        assert stats.athletes == 4 and stats.handles == sum(len(p.instagram) + len(p.x) for p in skiers)
        lara = s.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == "Q30293"))
        assert lara.full_name == "Lara Gut-Behrami" and lara.sport.key == "ski_alpine_women"
        assert lara.sport.name == "Ski alpin Frauen" and lara.birth_date == date(1991, 4, 27)
        handles = sorted((h.platform, h.handle, h.evidence[0]["kind"]) for h in lara.handles)
        assert handles == [("instagram", "laragutofficial", "wikidata"), ("x", "laragut", "wikidata")]
        ebba = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Ebba Berglund"))
        assert ebba.sport.key == "icehockey_women" and ebba.position == "D" and ebba.nationality == "SWE"
        teams = sorted(a.team.name for a in ebba.affiliations)
        assert teams == ["HV71", "MoDo Hockey (women)"]  # open memberships only
        assert all(a.source == "Wikidata" for a in ebba.affiliations)


def test_bulk_never_merges_a_namesake(db):
    with Session(db) as s:
        hockey = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        same = repo.upsert_athlete(s, hockey, "Linnéa Andersson")  # roster entry without QID
        other_sport = repo.upsert_athlete(s, hockey, "Katja Grossmann")  # a hockey player of that name
        swe = bulk_fixture("hockey_women_swe", "icehockey_women", "SWE")
        ski = bulk_fixture("ski_women_sui", "ski_alpine_women", "SUI")
        load_wikidata_players(s, [p for p in swe if p.name == "Linnéa Andersson"])
        load_wikidata_players(
            s, [p for p in ski if p.name == "Katja Grossmann"], sport_key="ski_alpine_women"
        )
        s.commit()
        assert same.wikidata_qid == "Q110712879"  # same sport, no QID yet: merged
        assert other_sport.wikidata_qid is None  # the skier is someone else
        skier = s.scalar(select(m.Athlete).where(m.Athlete.wikidata_qid == "Q65953102"))
        assert skier.id != other_sport.id and skier.sport.key == "ski_alpine_women"
        load_wikidata_players(
            s, [p for p in ski if p.name == "Katja Grossmann"], sport_key="ski_alpine_women"
        )
        assert len(s.scalars(select(m.Athlete).where(m.Athlete.full_name == "Katja Grossmann")).all()) == 2


class FakeBulkCollector:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def collect_bulk(self, sport, nationality, country_qids, min_birth_year=1980):
        self.calls.append((sport.key, nationality, country_qids, min_birth_year))
        return bulk_fixture("ski_women_sui", sport.key, nationality)


def test_seed_wikidata_bulk_dry_run_writes_nothing(db):
    fake = FakeBulkCollector()
    counts, stats = seed_wikidata_bulk(None, "ski_alpine_men", ["SUI", "aut"], collector=fake, dry_run=True)
    assert fake.calls == [
        ("ski_alpine_men", "SUI", ("Q39",), 1980),
        ("ski_alpine_men", "AUT", ("Q40",), 1980),
    ]
    assert [c.nation for c in counts] == ["SUI", "AUT"] and counts[0].active == 4
    assert stats.athletes == 0
    with pytest.raises(ValueError):
        seed_wikidata_bulk(None, "ski_alpine_men", ["XYZ"], collector=fake, dry_run=True)


def test_cli_wikidata_bulk_dry_run(monkeypatch):
    from klugscheisser import cli
    from klugscheisser.collectors import wikidata

    fake = FakeBulkCollector()
    monkeypatch.setattr(
        wikidata.WikidataCollector, "collect_bulk", lambda self, *a, **kw: fake.collect_bulk(*a, **kw)
    )

    def no_db():
        raise AssertionError("dry run must not open the database")

    monkeypatch.setattr("klugscheisser.db.session.session_scope", no_db)
    result = CliRunner().invoke(
        cli.app, ["seed", "wikidata-bulk", "--sport", "ski_alpine_women", "--nations", "SUI,AUT", "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert re.search(r"SUI: 4 found \(alive, born 1980\+ or unknown\), 4 active", result.output)
    assert "ski_alpine_women: 8 active persons (dry run, nothing written)" in result.output
    bad = CliRunner().invoke(cli.app, ["seed", "wikidata-bulk", "--sport", "curling", "--dry-run"])
    assert bad.exit_code != 0


# --- live checks (RUN_NETWORK_TESTS=1) ----------------------------------------------------------------


@pytest.mark.network
def test_live_colin_muller_coaches_women_team():
    r = WikiPersonResolver(settings(), wiki=WikiClient(), web_search=None, jev=None, instagram_source=None)
    cands = r.candidates("Colin Muller", [])
    colin = next(c for c in cands if c.qid == "Q16540791")
    assert colin.coached_team is not None and colin.coached_team.women


@pytest.mark.network
def test_live_bulk_dry_run_counts():
    counts, _ = seed_wikidata_bulk(None, "icehockey_women", ["SUI"], dry_run=True)
    assert counts[0].active > 20


def test_wikidata_national_team_membership_maps_to_our_national_team(db):
    from sqlalchemy.orm import Session

    from klugscheisser.db import models as m
    from klugscheisser.db import repo
    from klugscheisser.seed.loader import _team_by_name, load_all_seeds

    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        team = _team_by_name(s, sport, "Switzerland women's national ice hockey team", None, "SUI")
        assert (
            team.key == "sui-women" and s.query(m.Team).filter(m.Team.name.like("Switzerland%")).count() == 0
        )
