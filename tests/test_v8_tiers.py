"""v8 phase 1: rosters from Wikipedia (numbers, head coaches), tiers, badge, manual tier."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser import tiers
from klugscheisser.collectors.rosters_wikipedia import parse, to_seed
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds

WIKITEXT = """
==Group A==
===Canada===
Head coach: [[Troy Ryan]]<ref>{{Cite web|title=Team roster: Canada|url=https://www.iihf.com/pdf/852/ihw8520can_33_1_0}}</ref>

{| class="wikitable sortable"
!No.
!Pos.
!Name
!Height
!Weight
!Birthdate
!Team
|-
| 2 || D || align="left"|{{sortname|Sophie|Jaques}} || {{convert|1.72|m|ftin|abbr=on}} || x || {{birth date and age2|2026|2|5|2000|10|16|df=y}} || align="left"|{{flagicon|CAN}} [[Vancouver Goldeneyes]]
|-
| 16 || F || align="left" |{{sortname|Kateřina|Mrázová|dab=ice hockey}} – '''[[Captain (ice hockey)#Alternate captains|A]]''' || x || x || {{birth date and age2|2026|2|5|1992|10|19|df=y}} || align="left" |{{flagicon|SWI}} [[EV Zug]]
|}
===Czech Republic===
Head coach: {{flagicon|CAN}} [[Carla MacLeod]]<ref>{{Cite web|url=https://www.iihf.com/pdf/852/ihw8520cze_33_1_0}}</ref>

{| class="wikitable"
!No.
|-
| 29 || G || align="left"|{{Sortname|Klára|Peslarová}} || x || x || {{birth date and age2|2026|2|5|1997|1|28|df=y}} || {{flagicon|SWE}} [[Brynäs IF]]
|}
"""


def test_parse_wikipedia_roster():
    can, cze = parse(WIKITEXT)
    assert (can.country, can.head_coach, cze.head_coach, cze.head_coach_country) == (
        "CAN",
        "Troy Ryan",
        "Carla MacLeod",
        "CAN",
    )
    assert can.official_url == "https://www.iihf.com/pdf/852/ihw8520can_33_1_0"
    jaques, mrazova = can.players
    assert (jaques.number, jaques.pos, jaques.birth, jaques.club) == (
        2,
        "D",
        date(2000, 10, 16),
        "Vancouver Goldeneyes",
    )
    assert mrazova.captain == "A" and mrazova.wiki == "Kateřina Mrázová (ice hockey)"
    assert mrazova.club_country == "SUI"  # Wikipedia's "SWI" typo
    seed = to_seed(cze, "oly-2026", "Some page", "2025-26", qids={"Klára Peslarová": "Q26209334"})
    assert seed["team"] == "cze-women" and seed["players"][0]["qid"] == "Q26209334"
    assert seed["staff"] == [
        {"name": "Carla MacLeod", "role": "Head coach", "nationality": "CAN", "wiki": "Carla MacLeod"}
    ]
    assert to_seed(can, "oly-2026", "P", "2025-26")["staff"][0]["nationality"] == "CAN"  # no flag: own nation


def test_seeded_rosters_numbers_coaches_and_wm_assumption(db):
    with Session(db) as s:
        load_all_seeds(s)
        oly = s.scalar(select(m.Event).where(m.Event.key == "oly-2026"))
        entries = s.scalars(select(m.EventEntry).where(m.EventEntry.event_id == oly.id)).all()
        assert len(entries) == 240  # 10 teams × (23 players + head coach)
        coaches = {e.athlete.full_name: e for e in entries if e.role == "Head coach"}
        assert (
            coaches["Colin Muller"].athlete.role == "coach" and coaches["Colin Muller"].jersey_number is None
        )
        stalder = repo.resolve_athlete(s, "Lara Stalder")
        assert any(e.jersey_number == 7 for e in entries if e.athlete_id == stalder.id)
        captain = s.scalar(select(m.Fact).where(m.Fact.athlete_id == stalder.id, m.Fact.kind == "captaincy"))
        assert captain is not None and "Captain (Schweiz" in captain.value_text
        lahti = s.scalar(select(m.Event).where(m.Event.key == "eht-2026-lahti"))
        miller = s.scalar(
            select(m.EventEntry).where(
                m.EventEntry.event_id == lahti.id, m.EventEntry.role.ilike("head coach")
            )
        )
        assert miller.athlete.full_name == "Shannon Miller" and miller.athlete.nationality is None
        # Alina Marti withdrew: no Lahti entry, but she was called up (national-team affiliation)
        marti = repo.resolve_athlete(s, "Alina Marti")
        assert (
            s.scalar(
                select(m.EventEntry).where(
                    m.EventEntry.event_id == lahti.id, m.EventEntry.athlete_id == marti.id
                )
            )
            is None
        )
        assert any(x.team.key == "sui-women" and x.season == "2026-27" for x in marti.affiliations)


def test_tiers_from_rosters_and_manual_choice(db):
    with Session(db) as s:
        load_all_seeds(s)
        counts = tiers.recompute(s)
        assert counts[1] >= 35 and counts[2] >= 150  # seeds only (no Wikidata stints in tests)
        stalder = repo.resolve_athlete(s, "Lara Stalder")
        miller = repo.resolve_athlete(s, "Shannon Miller")
        knight = repo.upsert_athlete(
            s, repo.get_sport(s, "icehockey_women"), "Testa Knight", nationality="USA"
        )
        usa_coach = repo.resolve_athlete(s, "John Wroblewski")
        france = s.scalar(
            select(m.Athlete)
            .join(m.EventEntry, m.EventEntry.athlete_id == m.Athlete.id)
            .join(m.Team, m.Team.id == m.EventEntry.team_id)
            .where(m.Team.key == "fra-women")
        )
        tiers.recompute(s)
        assert (stalder.tier, miller.tier, usa_coach.tier) == (1, 1, 2)  # staff count too
        assert knight.tier == 3  # USA, but on no roster and no national stint
        assert france.tier == 3  # Olympia only, France is not at the WM
        tiers.set_manual(s, knight, 1)
        tiers.recompute(s)
        assert tiers.effective(knight) == 1 and knight.tier == 3  # manual wins, automatic value kept
        tiers.set_manual(s, knight, None)
        assert tiers.effective(knight) == 3
        assert tiers.badge(1, 64) == "Tier 1 · 64 Quellen" and tiers.badge(3, 5) == ""


def test_badge_on_person_page_and_manual_switch(db):
    from klugscheisser.web.routes import build_web_app

    with Session(db) as s:
        load_all_seeds(s)
        tiers.recompute(s)
        aid = repo.resolve_athlete(s, "Lara Stalder").id
        s.commit()
    app = build_web_app(Settings(_env_file=None, web_answer_engine="fake", auto_refresh=False))
    with TestClient(app) as c:
        html = c.get(f"/person/{aid}").text
        assert "class='tier tier-1'" in html and "Tier 1 · " in html and "Tiefe der Suche" in html
        assert "Tier 1" in c.get("/suche", params={"q": "Lara Stal"}).text
        r = c.post(f"/person/{aid}/tier", data={"tier": "3"}, follow_redirects=False)
        assert r.status_code == 303
        assert "class='tier tier-1'" not in c.get(f"/person/{aid}").text
        c.post(f"/person/{aid}/tier", data={"tier": "auto"})
        assert "class='tier tier-1'" in c.get(f"/person/{aid}").text


def test_club_numbers_only_when_our_player_is_on_the_roster(db):
    from klugscheisser import numbers
    from klugscheisser.adds.base import Proposal, ProposedPerson, ProposedTeam, SourceRef
    from klugscheisser.web import card_data

    class FakeResolver:
        def propose(self, s, request, include_staff=True):
            team = ProposedTeam(
                name="X", kind="club", roster_source=SourceRef("Wikipedia (de): EVZ", "https://w/evz")
            )
            if "EVZ" in request:
                return Proposal(
                    kind="team",
                    found=True,
                    summary="ok",
                    team=team,
                    persons=[
                        ProposedPerson(name="Lara Stalder", jersey_number=7),
                        ProposedPerson(name="Someone Else", jersey_number=9),
                    ],
                )
            return Proposal(
                kind="team",
                found=True,
                summary="men",
                team=team,
                persons=[ProposedPerson(name="Max Muster", jersey_number=7)],
            )

    with Session(db) as s:
        load_all_seeds(s)
        stalder = repo.resolve_athlete(s, "Lara Stalder")
        # the Lahti seed already gives her EV Zug (2026-27) via the club alias
        r = numbers.club_numbers(
            s, Settings(_env_file=None), [stalder.id], resolver=FakeResolver(), echo=lambda *_: None
        )
        assert r.numbers == 1 and r.clubs_with_roster == 1
        value = next(x for x in card_data.card_data(s, stalder)["fakten"] if x and x["label"] == "Nummer")[
            "value"
        ]
        assert "#7 (Schweiz)" in value and "#7 (EVZ Women's Team)" in value
