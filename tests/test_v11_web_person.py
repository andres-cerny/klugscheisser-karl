"""v11 person page with Elite Prospects data: Instagram · Wikipedia · Elite Prospects links on top, new Steckbrief
fields with conflicts, the Statistik section (skater and goalie, Klub / Nationalteam / Junioren, playoffs, totals,
form line), awards grouped by name with medals first, transfers in the timeline. Rows are inserted directly in the
shape of the shared data contract (docs/build-plan-v11-eliteprospects.md), not through the importer."""

import re
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app, card, card_data
from klugscheisser.web import statistik as st
from klugscheisser.web import timeline as tl

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")
EP_URL = "https://www.eliteprospects.com/player/369459/lara-stalder"
FETCHED = datetime(2026, 10, 4, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _fact(s, a, kind, value, text, source="eliteprospects", url=EP_URL, status="confirmed", season=None):
    s.add(
        m.Fact(
            athlete_id=a.id,
            kind=kind,
            value=value,
            value_text=text,
            source=source,
            source_url=url,
            status=status,
            season=season,
            extractor=source,
        )
    )


def _stat(s, a, season, league, team, kind="club", phase="regular", **v):
    s.add(
        m.SeasonStat(
            athlete_id=a.id,
            season=season,
            league=league,
            team_name=team,
            team_kind=kind,
            phase=phase,
            source="eliteprospects",
            source_url=EP_URL,
            fetched_at=FETCHED,
            **v,
        )
    )


def _ep(s, a, **data):
    s.add(m.EpProfile(athlete_id=a.id, ep_id="369459", url=EP_URL, fetched_at=FETCHED, raw={}, data=data))


def _wikipedia(s, a, lang, title):
    item = m.Item(
        source_type="profile",
        platform="wikipedia",
        source_name=f"Wikipedia ({lang})",
        url=f"https://{lang}.wikipedia.org/wiki/{title}",
        title=title,
        content_hash=f"wp-{lang}",
        language=lang,
    )
    s.add(item)
    s.flush()
    s.add(m.ItemAthlete(item_id=item.id, athlete_id=a.id))


def _stalder(s) -> m.Athlete:
    """A skater with full EP data (shaped like tests/fixtures/eliteprospects/stalder_skater.json)."""
    a = repo.resolve_athlete(s, "Lara Stalder")
    a.eliteprospects_id = "369459"
    a.position = "F"
    s.add(m.Handle(athlete_id=a.id, platform="instagram", handle="larastalder", status="verified"))
    _wikipedia(s, a, "en", "Lara_Stalder")
    _wikipedia(s, a, "de", "Lara_Stalder")
    _fact(s, a, "height_cm", {"number": 167}, "167 cm")
    _fact(
        s,
        a,
        "height_cm",
        {"number": 176},
        "176 cm",
        source="wikidata",
        url="https://www.wikidata.org/wiki/Q1",
        status="conflict",
    )
    _fact(s, a, "weight_kg", {"number": 63}, "63")
    _fact(s, a, "position", {"text": "F", "detail": ["C"]}, "Stürmerin")
    _fact(s, a, "shoots", {"text": "R"}, "R")
    _fact(s, a, "birth_place", {"text": "Luzern, SUI"}, "Luzern, SUI")
    _fact(s, a, "youth_team", {"text": "HC Luzern"}, "HC Luzern")
    _fact(
        s,
        a,
        "current_team",
        {
            "team": "EVZ Women's Team",
            "league": "SWHL A (W)",
            "season": 2026,
            "jersey": 7,
            "status": "rostered",
        },
        "EVZ Women's Team",
    )
    _fact(s, a, "contract", {"text": "28/29", "season_to": 2028}, "28/29")
    _fact(s, a, "agency", {"text": "Sports Talent AG"}, "Sports Talent AG")
    _fact(s, a, "draft", {"league": "NWHL", "year": 2016, "round": 5, "team": "Boston Pride"}, "")
    _fact(
        s,
        a,
        "family_in_sport",
        {"text": "Bruder: Dario Stalder", "relation": "Bruder"},
        "Bruder: Dario Stalder",
    )
    # awards: the same award in two seasons is one row; a medal goes first
    for season, number in ((2018, 89), (2019, 75)):
        _fact(
            s,
            a,
            "award",
            {"text": "SDHL Most Points", "season": season, "number": number, "league": "SDHL"},
            f"SDHL Most Points ({number})",
        )
    _fact(
        s,
        a,
        "award",
        {"text": "WJC-18 D1 (W) Best Defenseman", "season": 2009, "year": 2010},
        "WJC-18 D1 (W) Best Defenseman",
    )
    _fact(s, a, "medal", {"text": "OG Bronze", "metal": "bronze", "year": 2014}, "Olympia-Bronze 2014")
    # club, national and youth stats; one national "(all)" summary row that must never be counted
    _stat(
        s,
        a,
        2026,
        "SWHL A (W)",
        "EVZ Women's Team",
        games=11,
        goals=9,
        assists=8,
        points=17,
        penalty_minutes=2,
    )
    _stat(
        s, a, 2025, "SWHL A (W)", "EVZ Women's Team", games=28, goals=30, assists=34, points=64, plus_minus=21
    )
    _stat(
        s, a, 2025, "SWHL A (W)", "EVZ Women's Team", phase="playoffs", games=8, goals=5, assists=6, points=11
    )
    _stat(s, a, 2019, "SDHL (W)", "Brynäs IF", games=36, goals=42, assists=47, points=89, penalty_minutes=10)
    _stat(s, a, 2012, "NCAA (W)", "Minnesota Duluth", kind="college", games=10)  # old season: only games
    _stat(s, a, 2024, "WC (W)", "Switzerland", kind="national", games=7, goals=2, assists=4, points=6)
    _stat(s, a, 2024, "International (W)", "Switzerland (all)", kind="national", games=999, goals=999)
    _stat(s, a, 2008, "U15-Elit", "HC Luzern U15", kind="youth", games=20, goals=25, assists=10, points=35)
    _ep(
        s,
        a,
        form={
            "last5": {"GP": 5, "G": 3, "A": 4, "PTS": 7, "PM": 2},
            "last10": {"GP": 10, "G": 9, "A": 8, "PTS": 17, "PM": None},
        },
        transfers=[
            {
                "date": "2025-12-03",
                "from": "EVZ Women's Team",
                "to": "EVZ Women's Team",
                "extra": "2 years",
                "source_url": "https://www.evz.ch/news/lara-stalder-verlaengert",
            },
            {"date": "2021-05-10", "from": "Brynäs IF", "to": "EVZ Women's Team", "extra": "3 years"},
        ],
    )
    s.flush()
    return a


def _goalie(s) -> m.Athlete:
    stalder = repo.resolve_athlete(s, "Lara Stalder")
    a = m.Athlete(
        sport_id=stalder.sport_id,
        full_name="Andrea Brändli",
        name_normalized="andrea brandli",
        nationality="SUI",
        position="G",
        eliteprospects_id="200001",
    )
    s.add(a)
    s.flush()
    _fact(s, a, "shoots", {"text": "L"}, "L")
    _stat(
        s,
        a,
        2025,
        "PWHL (W)",
        "Boston Fleet",
        games=12,
        wins=8,
        losses=4,
        shutouts=2,
        save_pct=0.931,
        gaa=1.85,
    )
    _stat(s, a, 2024, "PWHL (W)", "Boston Fleet", games=10, wins=5, losses=5, saves=250, goals_against=20)
    _ep(s, a, form={"last5": {"GP": 5, "GA": 9, "SV": 140, "SVP": 0.94}})
    s.flush()
    return a


def _page(aid: int) -> str:
    with TestClient(build_web_app(SETTINGS)) as c:
        r = c.get(f"/person/{aid}")
    assert r.status_code == 200
    return r.text


def _section(html: str, sec_id: str) -> str:
    start = html.index(f"id='{sec_id}'")
    return html[start : html.index("</section>", start)]


# --- links row -------------------------------------------------------------------------------------------


def test_links_row_all_three(seeded):
    with Session(seeded) as s:
        aid = _stalder(s).id
        s.commit()
    html = _page(aid)
    row = html[html.index("<nav class='plinks'") : html.index("</nav>", html.index("<nav class='plinks'"))]
    assert (
        row.index("instagram.com/larastalder") < row.index("de.wikipedia.org") < row.index("eliteprospects")
    )
    assert "en.wikipedia.org" not in row  # German article first
    assert "https://www.eliteprospects.com/player/369459" in row and ">Elite Prospects</a>" in row
    assert row.count("target='_blank' rel='noopener'") == 3
    # moved up: no second Eliteprospects link in "Daten & Einstellungen"
    assert "Eliteprospects" not in html


def test_links_row_hides_unknown_links(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        a.eliteprospects_id = "369459"
        s.add(m.Handle(athlete_id=a.id, platform="instagram", handle="nicht_sie", status="candidate"))
        s.commit()
        aid = a.id
    html = _page(aid)
    row = html[html.index("<nav class='plinks'") : html.index("</nav>", html.index("<nav class='plinks'"))]
    assert "Elite Prospects" in row and "instagram.com" not in row and "wikipedia" not in row


# --- Steckbrief ------------------------------------------------------------------------------------------


def test_steckbrief_ep_fields_and_conflict(seeded):
    with Session(seeded) as s:
        a = _stalder(s)
        card = card_data.card_data(s, a)
        facts = {f["label"]: f for f in card["fakten"]}
        assert facts["Grösse"]["value"] == "167 cm"
        assert facts["Grösse"]["sources"][0]["label"] == "Elite Prospects"
        assert [(x["value"], x["source"]["label"]) for x in facts["Grösse"]["abweichend"]] == [
            ("176 cm", "Wikidata")
        ]
        assert facts["Gewicht"]["value"] == "63 kg"
        assert facts["Position"]["value"] == "Stürmerin (Center)"
        assert facts["Schusshand"]["value"] == "rechts"
        assert facts["Geburtsort"]["value"] == "Luzern, SUI"
        assert facts["Juniorenklub"]["value"] == "HC Luzern"
        assert facts["Aktueller Verein"]["value"] == "EVZ Women's Team (SWHL A (W)) · #7"
        assert facts["Aktueller Verein"]["note"] == "Saison 2026/27 · Stand 04.10.2026"
        assert facts["Vertrag"]["value"] == "bis 2028/29" and facts["Vertrag"]["note"] == "Stand 04.10.2026"
        assert facts["Agentur"]["value"] == "Sports Talent AG"
        assert facts["Draft"]["value"] == "2016 NWHL Runde 5 durch Boston Pride"
        assert facts["Familie im Eishockey"]["value"] == "Bruder: Dario Stalder"
        s.commit()
        aid = a.id
    html = _page(aid)
    assert "abweichend: 176 cm" in html and "Elite Prospects</a>: Stand 04.10.2026" in html
    line = re.search(r"<span class=\"ph-line\">(.*?)</span>", html).group(1)
    assert line.endswith("EVZ Women&#x27;s Team") and line.count("#7") == 1


def test_goalie_catches_label(seeded):
    with Session(seeded) as s:
        facts = {f["label"]: f for f in card_data.card_data(s, _goalie(s))["fakten"]}
        assert facts["Fanghand"]["value"] == "links" and "Schusshand" not in facts


def test_ep_pronunciation_plays_for_swiss_names(seeded):
    with Session(seeded) as s:
        a = _stalder(s)
        s.add(
            m.NamePronunciation(
                athlete_id=a.id,
                source="eliteprospects",
                label="Aufnahme (Elite Prospects)",
                mime="audio/mpeg",
                audio=b"ID3fake",
            )
        )
        s.commit()
        aid = a.id
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get(f"/person/{aid}").text
        info = c.get(f"/person/{aid}/aussprache").text
        audio = c.get(f"/person/{aid}/aussprache.wav")
    # Swiss (German-language) name: no computer voice, but the real recording is offered
    assert 'class="say"' in html and f"id='aussprache-{aid}'" in html
    assert "Aufnahme (Elite Prospects)" in info and "eliteprospects.com/player/369459" in info
    assert audio.headers["content-type"] == "audio/mpeg" and audio.content == b"ID3fake"


# --- Statistik -------------------------------------------------------------------------------------------


def test_statistik_skater(seeded):
    with Session(seeded) as s:
        a = _stalder(s)
        d = st.stats_data(s, a, now=NOW)
        assert not d["goalie"] and d["current"] == 2026
        assert [(p["group"], p["phase"]) for p in d["panes"]] == [
            ("club", "regular"),
            ("club", "playoffs"),
            ("national", "regular"),
            ("youth", "regular"),
        ]
        club = d["panes"][0]
        assert [r["saison"] for r in club["rows"]] == [
            "2026/27",
            "2025/26",
            "2019/20",
            "2012/13",
        ]  # incl. college
        swhl = next(t for t in club["liga"] if t["league"] == "SWHL A (W)")
        assert swhl["games"] == 39 and swhl["points"] == 81 and swhl["points_per_game"] == 2.08
        national = d["panes"][2]
        # the "(all)" summary row is in no table and no total
        assert [r["league"] for r in national["rows"]] == ["WC (W)"]
        assert [t["league"] for t in national["liga"]] == ["WC (W)"] and national["liga"][0]["games"] == 7
        s.commit()
        aid = a.id
    html = _page(aid)
    sec = _section(html, "statistik")
    assert "999" not in sec and "International (W)" not in sec and "(all)" not in sec
    assert re.search(r"data-g='club' data-p='regular'>", sec)  # shown first
    assert re.search(r"data-g='club' data-p='playoffs' hidden>", sec)
    assert re.search(r"data-g='national' data-p='regular' hidden>", sec)
    assert ">Klub</button>" in sec and ">Nationalteam</button>" in sec and ">Junioren</button>" in sec
    assert "onchange='statPhase(this)'> Playoffs" in sec
    for h in ("Saison", "Team", "Liga", "Sp", "T", "A", "P", "PIM", "+/-", "P/Sp"):
        assert f">{h}</th>" in sec
    # old season with games only: other cells empty ("–"), never 0
    old = re.search(r"<tr class='[^']*'><td class='s'>2012/13</td>.*?</tr>", sec).group(0)
    assert old.count("<td>–</td>") == 5 and "<td>0</td>" not in old
    assert "<td>+21</td>" in sec and "<td>2,08</td>" in sec
    assert "<b>Letzte 5 Spiele:</b> 3 T, 4 A, 7 P, +/- +2" in sec
    assert "<b>Letzte 10 Spiele:</b> 9 T, 8 A, 17 P" in sec
    assert "Quelle: Elite Prospects" in sec and "Stand 04.10.2026" in sec and EP_URL in sec
    assert html.index("id='statistik'") < html.index("id='erf-h'")
    assert "onclick=\"setFilter(this,'statistik')\"" in html


def test_statistik_current_season_highlighted(seeded):
    with Session(seeded) as s:
        d = st.stats_data(s, _stalder(s), now=NOW)
    html = st.statistik_html(d)
    assert "<tr class='cur'><td class='s'>2026/27</td>" in html
    assert "<tr class=''><td class='s'>2025/26</td>" in html


def test_statistik_goalie(seeded):
    with Session(seeded) as s:
        a = _goalie(s)
        d = st.stats_data(s, a, now=NOW)
        s.commit()
        aid = a.id
    assert d["goalie"] and [p["group"] for p in d["panes"]] == ["club"]
    total = d["panes"][0]["liga"][0]
    assert total["games"] == 22 and total["wins"] == 13 and total["shutouts"] == 2
    html = _page(aid)
    sec = _section(html, "statistik")
    for h in ("Sp", "W", "L", "SO", "Sv%", "GAA"):
        assert f">{h}</th>" in sec
    assert ">PIM</th>" not in sec and "Torhüterin" in sec
    assert "<td>93,1 %</td>" in sec and "<td>1,85</td>" in sec
    # 2024: Sv% computed from saves and goals against (250 / 270), GAA not stored -> "–"
    row24 = re.search(r"<td class='s'>2024/25</td>.*?</tr>", sec).group(0)
    assert "<td>92,6 %</td>" in row24 and row24.endswith("<td>–</td></tr>")
    assert "Letzte 5 Spiele:</b> Sv% 94,0 %, 9 Gegentore, 140 Paraden" in sec
    assert "Playoffs" not in sec  # no playoff rows: no toggle
    assert "<span class='st-one'>Klub</span>" in sec and "statTab(" not in sec  # one team kind: no tabs


def test_formatting_helpers():
    assert st.num(None) == "–" and st.num(0) == "0" and st.signed(3) == "+3" and st.signed(-2) == "-2"
    assert st.pct(0.918) == "91,8 %" and st.pct(91.8) == "91,8 %" and st.dec(None) == "–"
    assert st.is_summary("International (W)", "Switzerland (all)") and not st.is_summary(
        "WC (W)", "Switzerland"
    )
    assert st.current_season(datetime(2027, 3, 1, tzinfo=UTC)) == 2026


# --- Erfolge and timeline --------------------------------------------------------------------------------


def test_awards_grouped_medals_first(seeded):
    with Session(seeded) as s:
        a = _stalder(s)
        items = card_data.achievements(s, a)
        assert items[0]["kind"] == "medal" and items[0]["label"] == "Bronze"
        most = [x for x in items if x["text"] == "SDHL Most Points"]
        assert len(most) == 1 and most[0]["detail"] == "2019/20 (75) · 2018/19 (89)"
        assert most[0]["quelle"] == "Elite Prospects"
        assert [x["text"] for x in items].count("WJC-18 D1 (W) Best Defenseman") == 1
        s.commit()
        aid = a.id
    html = _page(aid)
    erf = _section(html, "erf-h")
    assert erf.index("Bronze") < erf.index("SDHL Most Points")
    assert "<span class='y'>2019/20 (75) · 2018/19 (89)</span>" in erf


def test_transfers_in_timeline(seeded):
    with Session(seeded) as s:
        a = _stalder(s)
        s.commit()
        aid = a.id
    html = _page(aid)
    assert "Vertragsverlängerung bei EVZ Women&#x27;s Team, 2 Jahre" in html
    assert 'href="https://www.evz.ch/news/lara-stalder-verlaengert"' in html and ">evz.ch<" in html
    # no source link: her EP page; "von Brynäs IF"
    assert "Wechsel zu EVZ Women&#x27;s Team, 3 Jahre" in html and "von Brynäs IF" in html
    assert "Dezember 2025" in html and "Mai 2021" in html


def test_transfer_entry_wording():
    x = tl.transfer_entry({"date": "2026-06-01", "from": "SDE", "to": "PWHL Detroit", "extra": "3 years"})
    assert x["title"] == "Wechsel zu PWHL Detroit, 3 Jahre" and x["sub"] == "von SDE" and x["url"] is None
    y = tl.transfer_entry({"to": {"name": "HC Ambrì-Piotta"}, "type": "loan", "extra": "PTO"}, "https://ep/x")
    assert (
        y["title"] == "Leihe: HC Ambrì-Piotta, PTO"
        and y["url"] == "https://ep/x"
        and y["src"] == "Elite Prospects"
    )


# --- no EP data: page as before --------------------------------------------------------------------------


def test_page_without_ep_data_unchanged(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Alina Müller")
        card = card_data.card_data(s, a)
        assert card["ep"] is None and card["links"] == {} and card["aufnahme"] is None
        assert st.stats_data(s, a) is None
        aid = a.id
    html = _page(aid)
    assert "<section class='stats'" not in html and "<nav class='plinks'" not in html
    assert "Elite Prospects" not in html and 'data-k="wechsel"' not in html
    assert "setFilter(this,'statistik')" not in html
    assert "Steckbrief &amp; Karriere" in html and 'id="tl-h"' in html and "Daten &amp; Einstellungen" in html


def test_career_ep_rows_with_other_sources_merged(seeded):
    """With Elite Prospects stints the career list is EP's; the same team from Wikipedia becomes a second source
    link on that row, a station EP does not know goes to "weitere Angaben"."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")

        def stint(team, frm, to, source, url):
            s.add(
                m.CareerStint(
                    athlete_id=a.id,
                    team_name=team,
                    kind="club",
                    season_from=frm,
                    season_to=to,
                    source=source,
                    source_url=url,
                    status="confirmed",
                )
            )

        stint("Brynäs IF", 2019, 2022, "eliteprospects", EP_URL)
        stint("Brynäs IF Dam", 2019, None, "wikidata", "https://www.wikidata.org/wiki/Q1")
        stint("Brynäs IF", None, None, "web", "https://example.org/bericht")
        stint("HC Irgendwo", 2010, 2011, "wikipedia", "https://de.wikipedia.org/wiki/X")
        s.commit()
        shown, more = card_data.career(s, a)
    assert [r["team"] for r in shown] == ["Brynäs IF"]
    assert {x["url"] for x in shown[0]["sources"]} == {
        EP_URL,
        "https://www.wikidata.org/wiki/Q1",
        "https://example.org/bericht",
    }
    assert [r["team"] for r in more] == ["HC Irgendwo"]


def _add_stint(s, a, team, frm, to, source, url, league=None, item_id=None):
    s.add(
        m.CareerStint(
            athlete_id=a.id,
            team_name=team,
            kind="club",
            league=league,
            season_from=frm,
            season_to=to,
            source=source,
            source_url=url,
            source_item_id=item_id,
            status="confirmed",
        )
    )


def test_career_sihf_rows_are_the_list_without_ep(seeded):
    """2026-11-01: no EP stints -> the SIHF stints (with seasons) are the list; web stints of the same team (also
    "EVZ Women’s Team" = "EV Zug") become source links, named by site and date; other stations stay, without "?"."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = m.Item(
            source_type="other_media",
            platform="web",
            source_name="EV Zug",
            url="https://www.evz.ch/news/vertrag",
            title="Vertrag",
            published_at=datetime(2025, 12, 16, 10, tzinfo=UTC),
            content_hash="x",
        )
        s.add(item)
        s.flush()
        _add_stint(s, a, "SC Reinach", 2019, 2021, "sihf", "https://www.sihf.ch/a", "SWHL A")
        _add_stint(s, a, "EV Zug", 2024, None, "sihf", "https://www.sihf.ch/a", "SWHL A")
        _add_stint(
            s, a, "EVZ Women’s Team", 2023, None, "web", "https://www.evz.ch/news/vertrag", item_id=item.id
        )
        _add_stint(s, a, "SC Reinach Damen", None, None, "web", "https://www.frauen-eishockey.com/x")
        _add_stint(s, a, "HC Luzern", None, None, "web", "https://www.frauen-eishockey.com/y")
        s.commit()
        shown, more = card_data.career(s, a)
    assert [(r["team"], r["saison"]) for r in shown] == [
        ("SC Reinach", "2019/20–2021/22"),
        ("EV Zug", "seit 2024/25"),
        ("HC Luzern", "Saison unbekannt"),
    ]
    assert [x["label"] for x in shown[0]["sources"]] == ["sihf.ch", "frauen-eishockey.com"]
    assert [x["label"] for x in shown[1]["sources"]] == ["sihf.ch", "EV Zug, 16.12.2025"]
    assert shown[2]["saison_bekannt"] is False and more == []
    html = card._career_table(shown)
    assert "?" not in html and "Saison unbekannt" in html and "HC Luzern" in html


def test_career_ep_player_rows_unchanged_by_sihf(seeded):
    """Players with EP stints look as before: SIHF stints only add source links to EP rows or go to "weitere"."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        _add_stint(s, a, "EVZ Women's Team", 2023, None, "eliteprospects", EP_URL, "SWHL A (W)")
        _add_stint(s, a, "Brynäs IF", 2019, 2022, "eliteprospects", EP_URL, "SDHL (W)")
        s.commit()
        before, more_before = card_data.career(s, a)
        _add_stint(s, a, "SC Reinach", 2009, 2011, "sihf", "https://www.sihf.ch/a", "SWHL A")
        _add_stint(s, a, "EVZ Women's Team", 2023, None, "sihf", "https://www.sihf.ch/b", "SWHL A")
        s.commit()
        after, more = card_data.career(s, a)
    strip = lambda rows: [(r["team"], r["saison"], r["liga"]) for r in rows]  # noqa: E731
    assert strip(after) == strip(before)
    assert "https://www.sihf.ch/b" in {x["url"] for x in after[-1]["sources"]}
    assert [r["team"] for r in more] == ["SC Reinach"] and more_before == []
