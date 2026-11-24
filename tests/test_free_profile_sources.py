"""Free profile sources (2026-10-25): Hockey Canada rosters, Wikipedia infobox, Wikidata weight + photos, coverage."""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors import hockeycanada as hcc
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import hockeycanada_import as hci
from klugscheisser.facts import source_facts as sf
from klugscheisser.facts import wikipedia_infobox as wi
from klugscheisser.facts.queries import facts_for

WCH25 = hcc.event_by_key("wch-2025")
WCH24 = hcc.event_by_key("wch-2024")
WCH26 = hcc.event_by_key("wch-2026")


def _page(data: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def _roster_page(players: list[dict], goalies: list[dict] | None = None) -> str:
    return _page(
        {
            "props": {
                "pageProps": {
                    "teamDataTournament": {
                        "TeamRoster_Players": players,
                        "TeamRoster_Goalies": goalies or [],
                        "TeamRoster_Staff": [{"Name": "Coach X", "RosterType": "staff", "Info": []}],
                    }
                }
            }
        }
    )


def _p(
    name,
    jersey="7",
    pos="F",
    club="EV Zug (SUI)",
    dob="05-15-1994",
    h="5'6",
    w="139",
    home="Lucerne",
    hand="R",
):
    first, last = name.split(" ", 1)
    info = [
        {"Name": "Date of Birth", "Value": dob},
        {"Name": "Height", "Value": h},
        {"Name": "Weight", "Value": w},
    ]
    info += [{"Name": "Hometown", "Value": home}, {"Name": "Shoots", "Value": hand}]
    return {
        "Id": 1,
        "Name": name,
        "FirstName": first,
        "LastName": last,
        "JerseyNumber": jersey,
        "Position": pos,
        "CurrentTeam": club,
        "RosterType": "player",
        "Info": info,
    }


# --- parsing -----------------------------------------------------------------------------------------------------


def test_units_and_dates():
    assert hcc.feet_inches_cm("5'6") == 168
    assert hcc.feet_inches_cm("4'11") == 150
    assert hcc.feet_inches_cm("5'") == 152
    assert hcc.feet_inches_cm("") is None
    assert hcc.lb_kg("139") == (139, 63)
    assert hcc.lb_kg("0") == (None, None)  # the site's "unknown"
    assert hcc.us_date("05-15-1994") == date(1994, 5, 15)
    assert hcc.us_date("15-05-1994") is None


def test_parse_teams_maps_names_and_skips_placeholders():
    html = _page(
        {
            "props": {
                "pageProps": {
                    "games": [
                        {
                            "home": {"id": 186, "name": "Switzerland", "abbr": "SUI"},
                            "away": {"id": 1290, "name": "Austria ", "abbr": "AUS"},
                        },
                        {
                            "home": {"id": 735, "name": "W26", "abbr": "W26"},
                            "away": {"id": 186, "name": "Switzerland"},
                        },
                    ]
                }
            }
        }
    )
    teams = hcc.parse_teams(html)
    assert [(t.team_id, t.code) for t in teams] == [("186", "SUI"), ("1290", "AUT")]


def test_parse_roster_players_goalies_no_staff():
    goalie = _p("Andrea Brändli", jersey="30", pos="G", h="5'6", w="168", home=", ")
    goalie["Info"].append({"Name": "Catches", "Value": "L"})
    players = hcc.parse_roster(_roster_page([_p("Lara Stalder", w="0")], [goalie]))
    assert [p.name for p in players] == ["Lara Stalder", "Andrea Brändli"]
    lara, andrea = players
    assert (lara.height_cm, lara.weight_kg, lara.shoots, lara.jersey) == (168, None, "R", 7)
    assert lara.birth_date == date(1994, 5, 15)
    assert andrea.position == "G" and andrea.shoots == "L" and andrea.hometown is None


def test_roster_label():
    assert hcc.roster_label(WCH25.roster_url(186)) == "Hockey Canada, Kader IIHF Frauen-WM 2025"
    assert hcc.roster_label("https://example.org") == "Hockey Canada"


# --- import --------------------------------------------------------------------------------------------------------


@pytest.fixture
def s(db):
    with Session(db) as session:
        sport = m.Sport(key="icehockey_women", name="Eishockey Frauen")
        session.add(sport)
        session.flush()
        yield session
        session.rollback()


def _person(s, name, nat="SUI", birth=None):
    sport = s.scalar(select(m.Sport))
    return repo.upsert_athlete(s, sport, name, nationality=nat, birth_date=birth)


def _roster(event, code, players, team_id="186"):
    return hcc.HcRoster(event, hcc.HcTeam(team_id, "Switzerland", code), event.roster_url(team_id), players)


def _players(*dicts):
    return hcc.parse_roster(_roster_page(list(dicts)))


def test_import_matches_stores_and_is_idempotent(s):
    lara = _person(s, "Lara Stalder", birth=date(1994, 5, 15))
    _person(s, "Lara Stalder", nat="SWE")  # namesake of another nation: never matched
    rosters = [_roster(WCH25, "SUI", _players(_p("Lara Stalder"), _p("Nobody Known")))]
    r = hci.import_rosters(s, rosters)
    assert r.matched == 1 and r.unmatched == ["wch-2025 SUI Nobody Known (none)"]
    facts = {f.kind: f for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id))}
    assert facts["height_cm"].value["number"] == 168 and facts["height_cm"].value["raw"] == "5'6"
    assert facts["weight_kg"].value_text == "63 kg"
    assert facts["shoots"].value_text == "rechts"
    assert facts["hometown"].value_text == "Lucerne"
    assert facts["height_cm"].valid_from == WCH25.start
    assert facts["height_cm"].source_url.endswith(
        "/national/2024-25/world-championship/stats/team-rosters/186"
    )
    assert "jersey_number" not in facts  # number and club only from the current event
    ids = sorted(f.id for f in facts.values())
    r2 = hci.import_rosters(s, rosters)
    assert r2.counts["facts_added"] == 0 and r2.counts["facts_removed"] == 0
    assert sorted(f.id for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id))) == ids
    assert lara.shoots == "R"  # empty column filled


def test_newest_event_wins_and_current_event_adds_number_and_club(s):
    lara = _person(s, "Lara Stalder")
    rosters = [
        _roster(WCH24, "SUI", _players(_p("Lara Stalder", h="5'5"))),
        _roster(WCH26, "SUI", _players(_p("Lara Stalder", h="5'7", jersey="10"))),
    ]
    hci.import_rosters(s, rosters)
    rows = {
        r.kind: r for k in ("height_cm", "jersey_number", "current_team") for r in facts_for(s, [lara.id], k)
    }
    assert rows["height_cm"].value["number"] == 170
    assert rows["jersey_number"].value_text == "Nummer 10 (Switzerland (IIHF Frauen-WM 2026))"
    assert rows["current_team"].value_text == "EV Zug (SUI) (laut Kader IIHF Frauen-WM 2026)"


def test_partial_run_keeps_rows_of_other_events(s):
    lara = _person(s, "Lara Stalder")
    hci.import_rosters(s, [_roster(WCH25, "SUI", _players(_p("Lara Stalder")))])
    # nightly: only WCh 2026, which lists her without weight
    p26 = _p("Lara Stalder", w="")
    hci.import_rosters(s, [_roster(WCH26, "SUI", _players(p26))], full=False)
    kinds = {f.kind: f for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id))}
    assert hci.event_of(kinds["weight_kg"].source_url) == "wch-2025"
    assert hci.event_of(kinds["jersey_number"].source_url) == "wch-2026"


def test_birth_date_rules(s):
    _person(s, "Rebecca Leslie", nat="CAN", birth=date(1995, 5, 8))  # stored a year off: still her
    _person(s, "Old Namesake", nat="CAN", birth=date(1980, 1, 1))
    lines = _players(_p("Rebecca Leslie", dob="05-08-1996"), _p("Old Namesake", dob="01-01-2004"))
    r = hci.import_rosters(s, [_roster(WCH25, "CAN", lines, team_id="179")])
    assert r.matched == 1 and r.unmatched == ["wch-2025 CAN Old Namesake (birth-differs)"]


def test_conflict_with_more_trusted_source_and_tolerance(s):
    a = _person(s, "Lara Stalder")
    for kind, value, text in (
        ("height_cm", {"number": 167}, "167 cm"),
        ("weight_kg", {"number": 58}, "58 kg"),
    ):
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind=kind,
                value=value,
                value_text=text,
                source="eliteprospects",
                source_url="https://ep",
                status="confirmed",
                extractor="eliteprospects",
            )
        )
    s.flush()
    hci.import_rosters(s, [_roster(WCH25, "SUI", _players(_p("Lara Stalder")))])  # 168 cm, 63 kg
    hc = {f.kind: f.status for f in s.scalars(select(m.Fact).where(m.Fact.source == "hockeycanada"))}
    assert hc["height_cm"] == "confirmed"  # 1 cm off: feet/inches rounding
    assert hc["weight_kg"] == "conflict"
    row = facts_for(s, [a.id], "weight_kg")[0]
    assert row.source == "eliteprospects" and row.conflicts[0].source == "hockeycanada"


def test_rank_puts_roster_between_ep_and_wikidata(s):
    a = _person(s, "Lara Stalder")
    for src in ("wikidata", "hockeycanada", "web"):
        s.add(
            m.Fact(
                athlete_id=a.id,
                kind="height_cm",
                value={"number": 168},
                value_text="168 cm",
                source=src,
                source_url=f"https://{src}",
                status="confirmed",
                extractor=src,
            )
        )
    s.flush()
    assert facts_for(s, [a.id], "height_cm")[0].source == "hockeycanada"


def test_nightly_only_around_the_current_event(s):
    class Settings:
        hockeycanada_nightly = True
        hockeycanada_delay_s = 0

    class Client:
        requests = 0

        def rosters(self, event, codes):
            self.requests += 1
            return [_roster(event, "SUI", [])], []

    assert hci.nightly(s, Settings(), Client(), today=date(2026, 9, 1)) == {
        "uebersprungen": "kein laufendes Turnier"
    }
    out = hci.nightly(s, Settings(), Client(), today=date(2026, 10, 25))
    assert out["kader"] == 1 and out["leer"] == 1
    Settings.hockeycanada_nightly = False
    assert "uebersprungen" in hci.nightly(s, Settings(), Client(), today=date(2026, 10, 25))


# --- Wikipedia infobox ---------------------------------------------------------------------------------------------

EN = """{{Short description|x}}
{{Infobox ice hockey biography
| name = Lara Stalder
| birth_date = {{birth date and age|df=yes|1994|5|15}}
| birth_place = [[Lucerne]], [[Switzerland]]<ref>x</ref>
| position = [[Centre (ice hockey)|Centre]]
| shoots = Right
| height_m = 1.67
| weight_kg = 63
| former_teams = {{ubl|[[Brynäs IF (women)|Brynäs IF]]|[[Linköping HC]]}}
}}
'''Lara Stalder''' is …"""
DE = """{{Infobox Eishockeyspieler
 | Geburtsort   = [[Lempäälä]], [[Finnland]]
 | Größe        = 160 cm
 | Gewicht      = 63 kg
 | Position     = Verteidiger
 | Schusshand   = Links
}}"""
FR = """{{Infobox Personnalité du hockey sur glace
 | position = Gardienne
 | attrape = gauche
 | lieu de naissance = [[San Francisco]] ([[États-Unis]])
}}"""


def test_infobox_en_de_fr():
    en = wi.specs_from(wi.infobox_fields(EN), "u")
    assert {x.kind: x.value_text for x in en} == {
        "height_cm": "167 cm",
        "weight_kg": "63 kg",
        "shoots": "rechts",
        "position": "Stürmerin",
        "birth_place": "Lucerne, Switzerland",
    }
    de = {x.kind: x.value_text for x in wi.specs_from(wi.infobox_fields(DE), "u")}
    assert de["height_cm"] == "160 cm" and de["position"] == "Verteidigerin" and de["shoots"] == "links"
    fr = {x.kind: x.value_text for x in wi.specs_from(wi.infobox_fields(FR), "u")}
    assert fr == {
        "shoots": "links (Fanghand)",
        "position": "Torhüterin",
        "birth_place": "San Francisco, États-Unis",
    }
    assert wi.infobox_fields("no infobox here") == {}


class _Resp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class _WikiHttp:
    def __init__(self):
        self.calls = 0

    def get(self, url, params):
        self.calls += 1
        assert "en.wikipedia.org" in url
        return _Resp(
            {
                "query": {
                    "normalized": [{"from": "Lara_Stalder", "to": "Lara Stalder"}],
                    "pages": [{"title": "Lara Stalder", "revisions": [{"slots": {"main": {"content": EN}}}]}],
                }
            }
        )


def test_infobox_run_stores_below_wikidata_and_keeps_model_facts(s):
    a = _person(s, "Lara Stalder")
    item = m.Item(
        source_type="profile",
        platform="wikipedia",
        url="https://en.wikipedia.org/wiki/Lara_Stalder",
        content_hash="x",
        tagged=[],
    )
    s.add(item)
    s.flush()
    s.add(m.ItemAthlete(item_id=item.id, athlete_id=a.id, match_kind="exact", confidence=1.0))
    model_fact = m.Fact(
        athlete_id=a.id,
        kind="nickname",
        value={"text": "LS"},
        value_text="LS",
        source="wikipedia",
        source_url=item.url,
        status="confirmed",
        extractor="claude",
    )
    s.add(model_fact)
    s.add(
        m.Fact(
            athlete_id=a.id,
            kind="height_cm",
            value={"number": 172},
            value_text="172 cm",
            source="wikidata",
            source_url="https://wd",
            status="confirmed",
            extractor="wikidata",
        )
    )
    s.flush()
    r = wi.run(s, [a.id], http=_WikiHttp(), sleep=lambda _: None)
    assert r.with_infobox == 1 and r.requests == 1
    rows = {f.kind: f for f in s.scalars(select(m.Fact).where(m.Fact.extractor == wi.EXTRACTOR))}
    assert rows["height_cm"].status == "conflict" and rows["weight_kg"].status == "confirmed"
    assert facts_for(s, [a.id], "height_cm")[0].source == "wikidata"
    assert s.get(m.Fact, model_fact.id) is not None
    again = wi.run(s, [a.id], http=_WikiHttp(), sleep=lambda _: None)
    assert again.counts["facts_added"] == 0


# --- Wikidata weight + photos --------------------------------------------------------------------------------------


def test_wikidata_weight_row(s):
    from klugscheisser.facts.wikidata_import import import_wikidata_facts

    a = _person(s, "Lara Stalder")
    a.wikidata_qid = "Q1"
    s.flush()
    rows = [{"p": "http://www.wikidata.org/entity/Q1", "prop": "P2067", "val": "63.0"}]
    import_wikidata_facts(s, [a], rows=rows)
    assert facts_for(s, [a.id], "weight_kg")[0].value_text == "63 kg"


def test_photos_batch(s):
    from klugscheisser.facts.wikidata_extra import photos

    a = _person(s, "Lara Stalder")
    a.wikidata_qid = "Q1"
    b = _person(s, "Nora Harju")
    s.flush()
    meta = {
        "url": "u",
        "thumb": "t",
        "author": "Jane",
        "licence": "CC BY-SA 4.0",
        "file_page": "https://commons/f",
    }
    c = photos(s, [a, b], fetch=lambda qid: meta, sleep=lambda _: None)
    assert c["photo_added"] == 1 and c["no_qid"] == 1
    assert photos(s, [a], fetch=lambda qid: None, sleep=lambda _: None)["had_photo"] == 1


def test_source_facts_agreement_rules():
    assert sf.agrees("height_cm", 168.0, 166.0) and not sf.agrees("height_cm", 168.0, 165.0)
    assert sf.agrees("birth_date", "1994", "1994-05-15")
