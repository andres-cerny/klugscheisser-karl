"""ui-loop round 4 (2026-10-17): orientation on long pages (Steckbrief chip, Erfolge capped on wide screens,
event page: jump bar to each roster, teams without roster as one compact block), forgiving search for
"Frauen/Damen/Ladies/Women's" and filler words in event names, "Meinten Sie" prefers tier 1."""

import pytest
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import person as wp
from klugscheisser.web import roster as wr
from klugscheisser.web import search as ws
from klugscheisser.web import timeline as tl

# --- person page --------------------------------------------------------------------------------------


def _erfolge(n: int) -> list[dict]:
    return [
        {"text": f"Erfolg {i}", "url": f"https://ep.example/{i}", "quelle": "Elite Prospects", "metal": None,
         "label": "Auszeichnung"}
        for i in range(n)
    ]  # fmt: skip


def test_erfolge_phone_four_wide_six_then_all():
    html = wp.erfolge_html(_erfolge(12))
    assert html.count("<a class='' ") == 4  # always visible
    assert html.count("<a class='xp' ") == 2  # from the wide layout on (round 5: 6 instead of 8)
    assert html.count("<a class='x' ") == 6  # behind "Alle 12 Erfolge" everywhere
    assert "erf-all erf-w'" in html and "Alle 12 Erfolge" in html
    six = wp.erfolge_html(_erfolge(6))
    assert "erf-w" not in six and "Alle 6 Erfolge" in six  # phone only: the wide screen shows all six
    assert "erf-w" in wp.erfolge_html(_erfolge(7))
    assert "Alle" not in wp.erfolge_html(_erfolge(3))
    assert all(f"href='https://ep.example/{i}'" in html for i in range(12))  # each keeps its source link


def test_chips_follow_the_page_order_with_steckbrief():
    keys = [k for k, _, _ in tl.counts([], erfolge=2, funfacts=1, seasons=3, facts=14)]
    assert keys[:4] == ["alle", "steckbrief", "statistik", "erfolge"]
    n = {k: c for k, _, c in tl.counts([], erfolge=2, funfacts=1, seasons=3, facts=14)}
    assert n["steckbrief"] == 14 and n["alle"] == 3  # facts and seasons are not part of "Alles"


# --- event page ---------------------------------------------------------------------------------------


def _team(i: int, name: str, group: str, players: int) -> dict:
    return {
        "id": i,
        "team": name,
        "schweiz": name == "Schweiz",
        "gruppe": group,
        "kader_status": "confirmed" if players else None,
        "kader_quelle": None,
        "spielerinnen": [
            {"id": 100 * i + k, "name": f"Spielerin {i}-{k}", "position": "Stürmerin", "nummer": k + 1}
            for k in range(players)
        ],
    }


def _event(teams: list[dict]) -> dict:
    return {
        "anlass": {"name": "Testturnier", "key": "test-2026", "von": None, "ort": "Bern", "url": None},
        "stand": {},
        "kader_stand": None,
        "lineup_status": "Bestätigter Kader.",
        "teams": teams,
    }


def test_event_page_teams_without_roster_in_one_block():
    html = wr.event_body(
        _event([_team(1, "Schweiz", "A", 2), _team(2, "Kanada", "B", 0), _team(3, "USA", "A", 0)]), aside=""
    )
    assert html.count("class='team-h'") == 1  # only the roster gets a big heading
    block = html[html.index("class='ev-rest'") :]
    assert "Weitere Teams" in block and "2 · Kader noch nicht erfasst" in block
    assert block.index("Gruppe A") < block.index("href='/team/3'") < block.index("Gruppe B")
    assert "href='/team/2'" in block
    assert "Springen zu" not in html  # one roster: no jump bar


def test_event_page_jump_bar_with_several_rosters():
    html = wr.event_body(
        _event([_team(1, "Schweiz", "A", 2), _team(2, "Kanada", "A", 3), _team(3, "Japan", "B", 0)]), aside=""
    )
    nav = html[html.index("aria-label='Springen zu'") : html.index("</nav>")]
    assert "href='#team-1'" in nav and "href='#team-2'" in nav and "href='#evr-h'" in nav
    assert "id='team-1'" in html and "id='team-2'" in html and "id='evr-h'" in html


def test_event_page_all_without_roster_says_teams():
    html = wr.event_body(_event([_team(2, "Kanada", "", 0), _team(3, "USA", "", 0)]), aside="")
    assert ">Teams</h2>" in html and "Weitere Teams" not in html and "Gruppe" not in html


# --- search -------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(engine):
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE "
                + ", ".join(x.name for x in m.Base.metadata.sorted_tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    with Session(engine) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        for key, name in [
            ("hc-davos-ladies-r4", "HC Davos Ladies"),
            ("ev-zug-r4", "EV Zug"),
            ("ev-zug-damen-r4", "EV Zug Damen"),
            ("lausanne-feminin-r4", "Lausanne HC Féminin"),
        ]:
            s.add(m.Team(sport_id=sport.id, key=key, name=name, kind="club", country="SUI"))
        s.commit()
    return engine


def _keys(hits) -> list[str]:
    return [h.team.key for h in hits]


def test_women_words_in_any_language_find_the_womens_team(seeded):
    names = lambda q: [h.team.name for h in ws.find_teams(s, q)]  # noqa: E731
    with Session(seeded) as s:
        assert names("Davos Damen")[:1] == ["HC Davos Ladies"]
        assert names("Lausanne Frauen")[:1] == ["Lausanne HC Féminin"]
        zug = _keys(ws.find_teams(s, "EV Zug Frauen"))
        assert "ev-zug-damen-r4" in zug and "ev-zug-r4" not in zug  # the men's team has no such word
        assert "ev-zug-r4" in _keys(ws.find_teams(s, "EV Zug"))  # without the word: both


def test_event_names_with_filler_words_and_womens(seeded):
    with Session(seeded) as s:
        for q in ("Championnat du monde", "Women's World Championship", "Mondial féminin", "Frauen WM"):
            assert "wch-2026" in [ev.key for ev in ws.find_events(s, q)], q


def test_did_you_mean_prefers_tier_one_on_equal_spelling(seeded):
    with Session(seeded) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Zora Mekkan", wikidata_qid="Q-r4-a")
        b = repo.upsert_athlete(s, sport, "Zora Mekkon", wikidata_qid="Q-r4-b")
        a.tier, b.tier = 3, 1
        s.commit()
        labels = [x["label"] for x in ws.did_you_mean(s, "Zora Mekkun")]
        assert labels.index("Zora Mekkon") < labels.index("Zora Mekkan")  # same distance: tier 1 first


# --- round 5 (2026-10-18) -----------------------------------------------------------------------------


def test_steckbrief_keeps_only_a_link_line_for_the_events():
    p = {
        "athletin": {"id": 1, "name": "Saskia Maurer"},
        "anlaesse": [
            {
                "key": "wch-2026",
                "name": "IIHF Frauen-WM 2026",
                "datum": "06.11.2026",
                "kader_status": "confirmed",
            },
            {"key": "oly 2026", "name": "Olympia 2026", "datum": "05.02.2026", "kader_status": None},
        ],
    }
    card = {"id": 1, "name": "Saskia Maurer", "fakten": [], "karriere": [], "karriere_unbestaetigt": [], "intro": None,
            "foto": None}  # fmt: skip
    html = wp.drawer_html(p, card)
    assert (
        "<h2>Anlässe</h2>" not in html and "Kader bestätigt" not in html
    )  # dates/status: glance + Zeitleiste
    assert "href='/event/wch-2026'" in html and "href='/event/oly%202026'" in html  # still reachable
    assert html.count("ev-line") == 1
    # every Anlass still has its dated row in the Zeitleiste
    assert [x["title"] for x in tl.entries(p) if x["k"] == "event"] == ["IIHF Frauen-WM 2026", "Olympia 2026"]


def test_brief_chips_from_the_same_source_carry_the_surname():
    from klugscheisser.web import brief as wb

    ep = "https://www.eliteprospects.com/player/"
    html = wb.pair_chips(ep + "1", "Lara Müller", ep + "2", "Alina Meier")
    assert ">EP · Müller<" in html and ">EP · Meier<" in html
    assert f"href='{ep}1'" in html and f"href='{ep}2'" in html  # links stay
    mixed = wb.pair_chips(ep + "1", "Lara Müller", "https://de.wikipedia.org/wiki/X", "Alina Meier")
    assert ">Elite Prospects<" in mixed and ">Wikipedia<" in mixed and "Müller" not in mixed
