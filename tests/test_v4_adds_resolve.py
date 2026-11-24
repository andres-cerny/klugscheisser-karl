"""v4 P2: resolvers for adding persons and teams (recorded Wikidata/Wikipedia fixtures, fake Jev/LLM/web)."""

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, unquote

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.adds.base import Proposal
from klugscheisser.adds.person import FABI_ONLY, WikiPersonResolver, clean_handle, contradiction
from klugscheisser.adds.resolvers import get_person_resolver, get_team_resolver
from klugscheisser.adds.team import TeamRosterResolver, interpret_rules
from klugscheisser.adds.wiki import WikiClient, countries_in, parse_roster_html, pick_roster_section
from klugscheisser.collectors.instagram.base import InstagramProfile
from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.interfaces import WebResult

FIX = Path("tests/fixtures/adds")
HANNA_HOCKEY, HANNA_FOOTBALL, LARA = "Q19972549", "Q97723108", "Q30293"
SWEDEN_TEAM, FROLUNDA_WOMEN = "Q174641", "Q121392101"


class WikiFixtures:
    """httpx handler answering Wikidata/Wikipedia requests from tests/fixtures/adds."""

    def __init__(self) -> None:
        self.search = json.loads((FIX / "wikidata_search.json").read_text())
        self.rows = json.loads((FIX / "sparql_persons.json").read_text()) + json.loads(
            (FIX / "sparql_teams.json").read_text()
        )
        self.summaries = json.loads((FIX / "wikipedia_summaries.json").read_text())
        self.sections = json.loads((FIX / "wikipedia_sections.json").read_text())
        self.html = {
            ("en", "Sweden women's national ice hockey team", "9"): "roster_sweden_en.html",
            ("en", "Frölunda HC (women)", "4"): "roster_frolunda_en.html",
        }
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url, params = request.url, request.url.params
        self.requests.append(str(url))
        if url.host == "www.wikidata.org":
            hits = self.search.get(f"{params['language']}|{params['search']}", [])
            return httpx.Response(200, json={"search": hits, "success": 1})
        if url.host == "query.wikidata.org":
            query = parse_qs(request.content.decode())["query"][0]
            var, values = re.search(r"VALUES \?(\w+) \{([^}]*)\}", query).groups()
            qids = set(re.findall(r"wd:(Q\d+)", values))
            rows = [r for r in self.rows if var in r and r[var]["value"].rsplit("/", 1)[-1] in qids]
            return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": rows}})
        lang = url.host.split(".")[0]
        if "/api/rest_v1/page/summary/" in url.path:
            title = unquote(url.path.rsplit("/", 1)[-1]).replace("_", " ")
            data = self.summaries.get(f"{lang}|{title}")
            return httpx.Response(200, json=data) if data else httpx.Response(404, json={})
        if url.path == "/w/api.php" and params.get("action") == "parse":
            page = params["page"]
            if params.get("prop") == "sections":
                data = self.sections.get(f"{lang}|{page}")
                if data is None:
                    return httpx.Response(200, json={"error": {"code": "missingtitle"}})
                return httpx.Response(200, json={"parse": data})
            name = self.html.get((lang, page, params.get("section")))
            text = (FIX / name).read_text() if name else ""
            return httpx.Response(200, json={"parse": {"title": page, "text": text}})
        return httpx.Response(404, json={})


class FakeJev:
    """Answers the fit question with fixed probabilities; records what it was asked."""

    def __init__(self, probabilities: dict[str, float]) -> None:
        self.probabilities = probabilities
        self.asked: list[tuple[str, dict]] = []
        self.usage = {"input_tokens": 0}

    def ask(self, state: str, questions: dict) -> dict:
        self.asked.append((state, questions))
        self.usage["input_tokens"] += 1000
        options = list(questions["fit"]["criteria"])
        probs = {o: self.probabilities.get(o, 0.0) for o in options}
        return {"fit": {"probabilities": probs}}


class FakeWeb:
    name = "fake"

    def __init__(self, results: list[WebResult]) -> None:
        self.results = results
        self.queries: list[str] = []

    def search(self, query: str, limit: int = 5) -> list[WebResult]:
        self.queries.append(query)
        return self.results[:limit]


class FakeInstagram:
    name = "apify"

    def __init__(self, profiles: list[InstagramProfile]) -> None:
        self.profiles = profiles
        self.asked: list[list[str]] = []

    def fetch_profiles(self, handles: list[str]) -> list[InstagramProfile]:
        self.asked.append(handles)
        return [p for p in self.profiles if p.username in handles]


class FakeLlm:
    name, model = "fake", "fake-1"

    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list[str] = []

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        self.calls.append(user)
        return self.answer


@pytest.fixture
def wiki():
    handler = WikiFixtures()
    http = PoliteClient(
        client=httpx.Client(transport=httpx.MockTransport(handler)), min_delay_s=0, sleep=lambda s: None
    )
    client = WikiClient(http)
    client.fixtures = handler
    return client


def settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def person_resolver(wiki, jev=None, web=None, instagram=None, **kw) -> WikiPersonResolver:
    return WikiPersonResolver(settings(**kw), wiki=wiki, jev=jev, web_search=web, instagram_source=instagram)


# --- helpers --------------------------------------------------------------------------------------------


def test_clean_handle_and_countries():
    assert clean_handle("@Hanna.Olsson") == "hanna.olsson"
    assert clean_handle("https://www.instagram.com/laragutofficial/") == "laragutofficial"
    assert clean_handle("kein handle!") is None
    assert countries_in("die Schwedin") == ["SWE"]
    assert countries_in("Schweizer Skirennfahrerin") == ["SUI"]


def test_roster_section_choice():
    sections = [
        {"index": "4", "line": "2024–25 roster"},
        {"index": "5", "line": "Team captaincy history"},
        {"index": "6", "line": "Head coaches"},
        {"index": "9", "line": "Current roster"},
    ]
    assert pick_roster_section(sections)["index"] == "9"
    assert pick_roster_section(sections[:3])["index"] == "4"


def test_roster_parse_strips_captain_marker():
    page = (
        "<p>Head coach: Jane Doe</p><table class='wikitable'><tr><th>No.</th><th>Pos.</th><th>Name</th></tr>"
        "<tr><td>71</td><td>D</td><td><a href='/wiki/Anna_Kjellbin' title='Anna Kjellbin'>Anna Kjellbin</a>"
        " – C</td></tr></table>"
    )
    roster = parse_roster_html(page)
    assert [(r.name, r.number, r.position, r.role) for r in roster.rows] == [
        ("Anna Kjellbin", 71, "D", "athlete"),
        ("Jane Doe", None, None, "coach"),
    ]


# --- persons --------------------------------------------------------------------------------------------


def test_unique_match_new_sport(db, wiki):
    with Session(db) as s:
        p = person_resolver(wiki).propose(s, "Lara Gut-Behrami", details="Schweizer Skirennfahrerin")
    assert p.found and p.kind == "person"
    person = p.persons[0]
    assert person.wikidata_qid == LARA
    assert person.nationality == "SUI"  # Wikidata also lists LIE; Fabi's details decide
    assert (person.sport_key, person.sport_name_de, person.person_de) == (
        "ski_alpine_women",
        "Ski alpin Frauen",
        "Skirennfahrerin",
    )
    assert person.birth_date == date(1991, 4, 27)
    assert person.instagram == "laragutofficial" and person.instagram_source == "wikidata"
    assert person.wikipedia_url == "https://de.wikipedia.org/wiki/Lara_Gut-Behrami"
    assert [x.label for x in person.sources] == [f"Wikidata {LARA}", "Wikipedia (de)"]
    assert p.summary.startswith("Lara Gut-Behrami (SUI), Skirennfahrerin, geb. 27.04.1991 (laut Wikidata).")
    assert p.cost_estimate_chf > 0 and p.resolver_cost_usd == 0
    assert p.notes == []


def test_namesakes_without_jev_are_alternatives(db, wiki):
    with Session(db) as s:
        p = person_resolver(wiki).propose(s, "Hanna Olsson", details="schwedische Eishockeyspielerin")
    assert not p.found and p.persons == []
    assert "Mehrere Personen heissen «Hanna Olsson»" in p.summary and "präzisieren" in p.summary
    assert any(HANNA_HOCKEY in a for a in p.alternatives)
    assert any(HANNA_FOOTBALL in a and "Fussballspielerin" in a for a in p.alternatives)


def test_jev_picks_the_right_namesake(db, wiki):
    jev = FakeJev({HANNA_HOCKEY: 0.93, HANNA_FOOTBALL: 0.03, "keiner": 0.04})
    with Session(db) as s:
        p = person_resolver(wiki, jev=jev).propose(
            s, "Hanna Olsson", details="schwedische Eishockeyspielerin"
        )
        ledger = s.scalars(select(m.CostLedger).where(m.CostLedger.source == "jev")).all()
    assert p.found
    person = p.persons[0]
    assert (person.wikidata_qid, person.fit) == (HANNA_HOCKEY, 0.93)
    assert (person.sport_key, person.position, person.nationality) == ("icehockey_women", "F", "SWE")
    assert person.eliteprospects_id == "316516"
    assert "Hanna Olsson (SWE), Eishockeyspielerin, geb. 20.01.1999, Stürmerin (laut Wikidata)" in p.summary
    assert "Instagram @holsson (laut Wikidata" in p.summary
    assert all(HANNA_HOCKEY not in a for a in p.alternatives) and p.alternatives
    state, questions = jev.asked[0]
    criteria = questions["fit"]["criteria"]
    assert questions["fit"]["type"] == "choice" and "keiner" in criteria and HANNA_HOCKEY in criteria
    assert "schwedische Eishockeyspielerin" in questions["fit"]["instructions"]
    assert "Wikipedia:" in state  # summary of the top candidate goes into the context
    assert len(ledger) == 1 and p.resolver_cost_usd == pytest.approx(ledger[0].cost_usd, abs=1e-4)


def test_jev_unsure_gives_fabi_only_proposal(db, wiki):
    jev = FakeJev({HANNA_HOCKEY: 0.55, "keiner": 0.45})
    with Session(db) as s:
        p = person_resolver(wiki, jev=jev).propose(
            s, "Hanna Olsson", details="schwedische Eishockeyspielerin, Jahrgang 2003"
        )
    assert p.found and p.notes[0] == FABI_ONLY
    person = p.persons[0]
    assert person.wikidata_qid is None and person.fit is None
    assert (person.sport_key, person.nationality) == ("icehockey_women", "SWE")
    assert person.sources[0].label == "von Fabi angegeben (Jahrgang 2003)"
    assert "nur Angaben von Fabi" in p.summary
    assert any("unsicher" in n for n in p.notes)


def test_refine_excludes_previous_candidate(db, wiki):
    first = FakeJev({HANNA_HOCKEY: 0.9})
    with Session(db) as s:
        previous = person_resolver(wiki, jev=first).propose(s, "Hanna Olsson", details="Schwedin")
        assert previous.persons[0].wikidata_qid == HANNA_HOCKEY
        second = FakeJev({HANNA_FOOTBALL: 0.88, HANNA_HOCKEY: 0.1})
        p = person_resolver(wiki, jev=second).propose(
            s,
            "Hanna Olsson",
            details="Schwedin",
            refine="nein, die Fussballerin, Jahrgang 1986",
            previous=Proposal.from_json(previous.to_json()),
        )
    _, questions = second.asked[0]
    assert HANNA_HOCKEY not in questions["fit"]["criteria"]
    assert "Jahrgang 1986" in questions["fit"]["instructions"]
    assert p.persons[0].wikidata_qid == HANNA_FOOTBALL
    assert p.persons[0].sport_key == "football_women"


def test_refine_changes_outcome_without_jev(db, wiki):
    with Session(db) as s:
        p = person_resolver(wiki).propose(
            s, "Lara Gut-Behrami", details="Schweizer Skirennfahrerin", refine="nein, die Eishockeyspielerin"
        )
    # only candidate contradicts the corrected details -> no Wikidata match, Fabi-only proposal
    assert p.found and p.persons[0].wikidata_qid is None and p.notes[0] == FABI_ONLY


def test_contradiction_rules(wiki):
    resolver = person_resolver(wiki)
    cands = {c.qid: c for c in resolver.candidates("Hanna Olsson", [])}
    hockey, football = cands[HANNA_HOCKEY], cands[HANNA_FOOTBALL]
    assert contradiction(hockey, "schwedische Eishockeyspielerin") is None
    assert "Sport" in contradiction(football, "schwedische Eishockeyspielerin")
    assert "Jahrgang" in contradiction(hockey, "Jahrgang 2001")
    assert "Nation" in contradiction(hockey, "die Kanadierin")


def test_coach_of_womens_team(db, wiki):
    with Session(db) as s:
        p = person_resolver(wiki).propose(s, "Colin Muller", details="Trainer der Schweizer Frauen-Nati")
    person = p.persons[0]
    assert p.found and person.wikidata_qid == "Q16540791"
    assert (person.role, person.function_de, person.sport_key) == ("coach", "Trainer", "icehockey_women")
    assert person.team is None and person.position is None  # P54 lists the clubs he played for
    assert p.summary.startswith(
        "Colin Muller (SUI), Trainer (Eishockey Frauen), geb. 01.12.1963 (laut Wikidata)."
    )
    assert any("Rolle Trainer/in nur von Fabi" in n for n in p.notes)


def test_fabi_handle_without_apify_is_not_checked(db, wiki):
    with Session(db) as s:
        p = person_resolver(wiki).propose(
            s, "Lara Gut-Behrami", instagram="@lara.fabi", details="Skirennfahrerin"
        )
    person = p.persons[0]
    assert (person.instagram, person.instagram_source, person.instagram_check) == (
        "lara.fabi",
        "fabi",
        "nicht geprüft",
    )
    assert "Instagram @lara.fabi (von Fabi angegeben; nicht geprüft)" in p.summary
    assert any("@laragutofficial" in n for n in p.notes)  # Wikidata knows a different handle
    assert person.sources[-1].label == "Instagram @lara.fabi von Fabi angegeben"


def test_fabi_handle_with_apify_profile_check(db, wiki):
    profile = InstagramProfile(
        username="laragutofficial",
        full_name="Lara Gut-Behrami",
        biography="Ski alpin 🇨🇭 Olympiasiegerin",
        followers=251234,
    )
    ig = FakeInstagram([profile])
    with Session(db) as s:
        p = person_resolver(wiki, instagram=ig).propose(
            s, "Lara Gut-Behrami", instagram="laragutofficial", details="Skirennfahrerin"
        )
        ledger = s.scalars(select(m.CostLedger).where(m.CostLedger.source == "apify")).all()
    assert ig.asked == [["laragutofficial"]]
    assert p.persons[0].instagram_check == "Profilname stimmt, Bio nennt Ski, 251'234 Follower"
    assert len(ledger) == 1 and p.resolver_cost_usd == pytest.approx(ledger[0].cost_usd)


def test_existing_person_by_qid_and_by_name(db, wiki):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        lara = repo.upsert_athlete(s, sport, "Lara Gut-Behrami", wikidata_qid=LARA)
        hanna = repo.upsert_athlete(s, sport, "Hanna Olsson", birth_date=date(1999, 1, 20))
        p1 = person_resolver(wiki).propose(s, "Lara Gut-Behrami", details="Skirennfahrerin")
        p2 = person_resolver(wiki, jev=FakeJev({HANNA_HOCKEY: 0.95})).propose(
            s, "Hanna Olsson", details="Eishockey"
        )
    assert p1.persons[0].existing_id == lara.id
    assert f"Bereits in unserer Datenbank (ID {lara.id})" in p1.summary
    assert p2.persons[0].existing_id == hanna.id


def test_existing_namesake_with_other_birth_date_is_not_matched(db, wiki):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        repo.upsert_athlete(s, sport, "Hanna Olsson", birth_date=date(2004, 5, 5))
        p = person_resolver(wiki, jev=FakeJev({HANNA_HOCKEY: 0.95})).propose(s, "Hanna Olsson")
    assert p.persons[0].existing_id is None


def test_nothing_found(db, wiki):
    web = FakeWeb([])
    with Session(db) as s:
        p = person_resolver(wiki, web=web).propose(s, "Zoe Quxbaumer")
    assert not p.found and p.persons == []
    assert "nichts Verlässliches" in p.summary
    assert web.queries == ["Zoe Quxbaumer"]


def test_fabi_only_with_web_hints(db, wiki):
    web = FakeWeb([WebResult(url="https://example.org/zoe", title="Zoe Quxbaumer – U18", snippet="…")])
    with Session(db) as s:
        p = person_resolver(wiki, web=web).propose(
            s, "Zoe Quxbaumer", instagram="zoe.qux", details="Schweizer Eishockeyspielerin, U18"
        )
    assert p.found and p.notes[0] == FABI_ONLY
    assert web.queries == ["Zoe Quxbaumer Schweizer Eishockeyspielerin, U18"]
    assert any("Websuche (nicht geprüft): Zoe Quxbaumer – U18" in n for n in p.notes)
    person = p.persons[0]
    assert (person.sport_key, person.nationality, person.instagram_source) == (
        "icehockey_women",
        "SUI",
        "fabi",
    )
    assert [x.label for x in person.sources] == [
        "von Fabi angegeben",
        "Instagram @zoe.qux von Fabi angegeben",
    ]


# --- teams ----------------------------------------------------------------------------------------------


def test_interpret_rules():
    r = interpret_rules("Füge das schwedische Frauen-Nationalteam hinzu")
    assert (r.kind, r.country, r.gender, r.sport, r.query) == ("national", "SWE", "female", "ice hockey", "")
    r = interpret_rules("add Frölunda women")
    assert (r.kind, r.query, r.gender) == ("club", "Frölunda", "female")
    assert interpret_rules("SC Bern Frauen").query == "SC Bern"


def test_national_team_roster_with_coach(db, wiki):
    with Session(db) as s:
        p = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(
            s, "Füge das schwedische Frauen-Nationalteam hinzu"
        )
    assert p.found and p.kind == "team"
    team = p.team
    assert (team.name, team.kind, team.country, team.wikidata_qid) == (
        "Schwedische Eishockeynationalmannschaft der Frauen",
        "national",
        "SWE",
        SWEDEN_TEAM,
    )
    assert team.roster_source.url.endswith("Sweden_women%27s_national_ice_hockey_team#2026_Olympics_roster")
    players = [x for x in p.persons if x.role == "athlete"]
    coaches = [x for x in p.persons if x.role == "coach"]
    assert len(players) == 8
    ebba = players[0]
    assert (ebba.name, ebba.jersey_number, ebba.position, ebba.birth_date, ebba.team, ebba.nationality) == (
        "Ebba Svensson Träff",
        1,
        "G",
        date(2005, 11, 27),
        "Linköping HC",
        "SWE",
    )
    assert ebba.wikipedia_url == "https://en.wikipedia.org/wiki/Ebba_Svensson_Tr%C3%A4ff"
    assert ebba.sport_key == "icehockey_women" and ebba.sources == [team.roster_source]
    assert [(c.name, c.function_de) for c in coaches] == [("Ulf Lundberg", "Cheftrainer/in")]
    assert "8 Spielerinnen, 1 Trainer/Staff" in p.summary
    assert "Zum Beispiel: Ebba Svensson Träff, Linnéa Andersson" in p.summary
    assert "2026 Olympics roster" in p.summary
    assert (
        p.notes[0]
        == "Kader von Wikipedia («2026 Olympics roster»), Stand 12 January 2026 – kann veraltet sein."
    )
    assert p.cost_estimate_chf > 0


def test_national_team_without_staff(db, wiki):
    with Session(db) as s:
        p = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(
            s, "Swedish women's national team", include_staff=False
        )
    assert p.found and all(x.role == "athlete" for x in p.persons) and len(p.persons) == 8


def test_club_roster_via_llm(db, wiki):
    llm = FakeLlm(
        {"query": "Frölunda", "kind": "club", "country": "SWE", "gender": "female", "sport": "ice hockey"}
    )
    with Session(db) as s:
        p = get_team_resolver(settings(), wiki=wiki, llm=llm).propose(s, "add Frölunda women")
    assert llm.calls == ["add Frölunda women"]
    assert p.found and p.team.wikidata_qid == FROLUNDA_WOMEN and p.team.kind == "club"
    assert p.team.league == "Nationella Damhockeyligan"
    players = [x for x in p.persons if x.role == "athlete"]
    staff = [x for x in p.persons if x.role != "athlete"]
    assert len(players) == 6 and len(staff) == 6
    assert {x.position for x in players} == {"D", "F"}  # C and LW map to F
    brodt = next(x for x in players if x.name == "Sydney Brodt")
    assert (brodt.nationality, brodt.jersey_number, brodt.team) == ("USA", 88, "Frölunda HC (women)")
    holst = next(x for x in staff if x.name == "Erika Holst")
    assert holst.role == "coach" and holst.wikidata_qid == "Q274487"
    assert [s.label for s in holst.sources][-1] == f"Wikidata {FROLUNDA_WOMEN}"
    assert next(x for x in staff if x.name == "Jay Hasson").function_de == "Materialwart/in"
    assert "Stand 19 December 2024" in p.notes[0]


def test_club_roster_rules_and_cap(db, wiki):
    with Session(db) as s:
        p = TeamRosterResolver(settings(add_max_team_persons=5), wiki=wiki, llm=None).propose(
            s, "add Frölunda women"
        )
    assert p.found and p.team.wikidata_qid == FROLUNDA_WOMEN
    assert len(p.persons) == 5 and any("Obergrenze" in n for n in p.notes)


def test_team_existing_persons_marked(db, wiki):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        ebba = repo.upsert_athlete(s, sport, "Ebba Svensson Träff", birth_date=date(2005, 11, 27))
        p = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(s, "schwedisches Frauen-Nationalteam")
    assert p.persons[0].existing_id == ebba.id
    assert "1 davon sind schon in unserer Datenbank" in p.summary


def test_team_from_db_roster_without_network(db, wiki):
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women", "Eishockey Frauen")
        team = m.Team(
            sport_id=sport.id, key="sui-women", name="Schweiz Frauen", kind="national", country="SUI"
        )
        event = m.Event(key="wch-2026-sui-fin", name="WM 2026: SUI–FIN")
        s.add_all([team, event])
        s.flush()
        for i, name in enumerate(["Alina Müller", "Lara Stalder"], start=1):
            a = repo.upsert_athlete(s, sport, name, nationality="SUI")
            s.add(
                m.EventEntry(
                    event_id=event.id, athlete_id=a.id, team_id=team.id, jersey_number=i, source="IIHF-Kader"
                )
            )
        s.flush()
        p = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(s, "Schweizer Frauen-Nationalteam")
    assert p.found and p.team.existing_id == team.id
    assert [x.name for x in p.persons] == ["Alina Müller", "Lara Stalder"]
    assert all(x.existing_id for x in p.persons) and p.cost_estimate_chf == 0
    assert wiki.fixtures.requests == []


def test_team_not_found(db, wiki):
    with Session(db) as s:
        p = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(s, "SC Bern Frauen")
        q = TeamRosterResolver(settings(), wiki=wiki, llm=None).propose(
            s, "Füge das lettische Nationalteam hinzu"
        )
    assert not p.found and p.persons == [] and "Kein passendes Team" in p.summary
    assert not q.found


def test_team_proposal_json_roundtrip(db, wiki):
    with Session(db) as s:
        p = get_team_resolver(settings(), wiki=wiki, llm=None).propose(s, "schwedisches Frauen-Nationalteam")
    again = Proposal.from_json(json.loads(json.dumps(p.to_json())))
    assert again == p


def test_person_factory_injection(db, wiki):
    resolver = get_person_resolver(settings(), wiki=wiki, jev=None, web_search=None, instagram_source=None)
    with Session(db) as s:
        assert resolver.propose(s, "Lara Gut-Behrami").persons[0].wikidata_qid == LARA


@pytest.mark.network
def test_live_wikidata_namesakes(db):
    with Session(db) as s:
        p = get_person_resolver(settings(), jev=None, web_search=None, instagram_source=None).propose(
            s, "Hanna Olsson", details="schwedische Eishockeyspielerin"
        )
    assert not p.found and any(HANNA_HOCKEY in a for a in p.alternatives)
