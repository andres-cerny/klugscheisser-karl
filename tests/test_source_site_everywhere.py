"""2026-11-01: web-type sources are cited by site + article date everywhere (profile box, MCP profile fields,
German hints, chat profile documents, awards, "Alles" view), never as a bare "Web"/"News"; 'sihf' is "sihf.ch".
"Aktueller Verein" prefers a team that plays in the running season, then on equal start season EP/SIHF over web."""

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer import engine
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import german
from klugscheisser.facts.source_site import cite_label, cite_name
from klugscheisser.mcp_server import stats_tools
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import card_data
from klugscheisser.web import timeline as tl
from klugscheisser.web.statistik import current_season

EVZ_URL = "https://www.evz.ch/news/teams/women/2026/09/kader"
EP_URL = "https://www.eliteprospects.com/player/1/x"


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _item(s, url=EVZ_URL, name="EV Zug", when=datetime(2026, 9, 25, 10, tzinfo=UTC)) -> m.Item:
    item = m.Item(
        source_type="other_media",
        platform="web",
        source_name=name,
        url=url,
        title="Kader",
        published_at=when,
        content_hash=url,
    )
    s.add(item)
    s.flush()
    return item


def _stint(s, a, team, frm, source, url, league=None, item_id=None, to=None):
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


def _fact(s, a, kind, text, source, url, item_id=None, value=None):
    s.add(
        m.Fact(
            athlete_id=a.id,
            kind=kind,
            value=value or {},
            value_text=text,
            source=source,
            source_url=url,
            source_item_id=item_id,
            status="confirmed",
            extractor=source,
        )
    )


def test_cite_label_and_name():
    assert cite_label("web", "https://www.srf.ch/x", "SRF, 01.10.2026") == "SRF, 01.10.2026"
    assert cite_label("news", "https://www.srf.ch/x") == "srf.ch"  # no item info: at least the domain
    assert cite_label("sihf", "https://www.sihf.ch/x") == "sihf.ch"
    assert cite_label("eliteprospects", EP_URL) == "Elite Prospects"
    assert cite_name("web", "https://www.evz.ch/x", "[EV Zug, 16.12.2025]") == "EV Zug"
    assert cite_name("web", "https://www.evz.ch/x") == "evz.ch"
    assert cite_name("wikidata", "https://www.wikidata.org/wiki/Q1") == "Wikidata"


def test_current_club_plays_now_and_sihf_beat_web_on_equal_season(seeded):
    """Luisa Waser case: web 'EHC Sursee' (2024, open, EVZ archive page of 25.09.2026) vs SIHF 'EV Zug' (2024,
    open) with games for "EVZ Women's Team" this season -> EV Zug; Sursee stays "abweichend", the old web stint
    "EVZ Women’s Team" is the same club and is not repeated."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = _item(s)
        _stint(s, a, "EHC Sursee", 2024, "web", EVZ_URL, item_id=item.id)
        _stint(s, a, "EVZ Women’s Team", 2023, "web", EVZ_URL, item_id=item.id)
        _stint(s, a, "EV Zug", 2024, "sihf", "https://www.sihf.ch/a", "SWHL A (W)")
        s.add(
            m.SeasonStat(
                athlete_id=a.id,
                season=current_season(),
                league="SWHL A (W)",
                team_name="EVZ Women's Team",
                team_kind="club",
                phase="regular",
                source="sihf",
                source_url="https://www.sihf.ch/t",
                games=8,
            )
        )
        s.commit()
        club = card_data._club(s, a)
    assert club["value"] == "EV Zug (SWHL A (W))"
    assert [x["label"] for x in club["sources"]] == ["sihf.ch"]
    assert [(x["value"], x["source"]["label"]) for x in club["abweichend"]] == [
        ("EHC Sursee", "EV Zug, 25.09.2026")
    ]


def test_current_club_equal_season_without_stats_prefers_sihf(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        _stint(s, a, "EHC Sursee", 2024, "web", "https://www.frauen-eishockey.com/x")
        _stint(s, a, "EV Zug", 2024, "sihf", "https://www.sihf.ch/a", "SWHL A (W)")
        s.commit()
        club = card_data._club(s, a)
    assert club["value"].startswith("EV Zug")
    assert [x["value"] for x in club["abweichend"]] == ["EHC Sursee"]
    assert club["abweichend"][0]["source"]["label"] == "frauen-eishockey.com"


def test_current_club_newer_web_stint_still_wins_without_stats(seeded):
    """Without games this season the newest start season decides as before (a fresh transfer news)."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        _stint(s, a, "EV Zug", 2024, "sihf", "https://www.sihf.ch/a", "SWHL A (W)")
        _stint(s, a, "Boston Fleet", 2026, "web", "https://www.thepwhl.com/x", "PWHL")
        s.commit()
        club = card_data._club(s, a)
    assert club["value"] == "Boston Fleet (PWHL)"


def test_ep_current_team_keeps_newest_other_team_as_abweichend(seeded):
    """EP players look as before: EP's current team is the value; a web stint of the same newest season at another
    team stays "abweichend" (now with site + date), even when EP's own stint ranks first."""
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = _item(s, "https://www.thepwhl.com/news/x", "PWHL", datetime(2026, 4, 15, tzinfo=UTC))
        _fact(
            s,
            a,
            "current_team",
            "Seattle Torrent",
            "eliteprospects",
            EP_URL,
            value={"team": "Seattle Torrent"},
        )
        _stint(s, a, "Seattle Torrent", 2026, "eliteprospects", EP_URL, "PWHL (W)")
        _stint(s, a, "Boston Fleet", 2026, "web", item.url, "PWHL", item_id=item.id)
        _stint(s, a, "Brynäs IF", 2023, "wikipedia", "https://en.wikipedia.org/wiki/X", "SDHL")
        s.commit()
        club = card_data._current_team(s, a, None)
    assert club["value"].startswith("Seattle Torrent")
    assert [(x["value"], x["source"]["label"]) for x in club["abweichend"]] == [
        ("Boston Fleet (PWHL)", "PWHL, 15.04.2026")
    ]


def test_profile_box_mcp_and_chat_name_the_site(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = _item(s, when=datetime(2023, 12, 1, 9, tzinfo=UTC))
        _fact(s, a, "height_cm", "159 cm", "web", EVZ_URL, item_id=item.id, value={"cm": 159})
        s.commit()
        box = {f["label"]: f for f in card_data.key_facts(s, a)}
        mcp = stats_tools.profile_fields(s, a, None)
        docs = engine._profile_fact_docs(s, a)
    assert [x["label"] for x in box["Grösse"]["sources"]] == ["EV Zug, 01.12.2023"]
    assert mcp["height_cm"]["quelle"] == "EV Zug, 01.12.2023"
    assert mcp["height_cm"]["quelle_link"] == f"[EV Zug, 01.12.2023]({EVZ_URL})"
    assert any(d.quelle == "[EV Zug, 01.12.2023, Profil Lara Stalder]" for d in docs)
    assert not any("Web" in d.quelle for d in docs)


def test_german_hint_sources_name_the_site(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = _item(s)
        _stint(s, a, "EV Zug", 2025, "web", EVZ_URL, item_id=item.id)
        _stint(s, a, "EV Zug", 2024, "sihf", "https://www.sihf.ch/a", "SWHL A (W)")
        s.commit()
        h = german.hints(s, [a], nations={a.id: "CZE"})[a.id]
    labels = [lbl for lbl, _ in h.sources]
    assert labels[0] == "sihf.ch"  # SIHF ranks before article sources
    assert "Web" not in labels and "SIHF" not in labels


def test_award_source_and_timeline_tag(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Lara Stalder")
        item = _item(s, "https://www.zugerzeitung.ch/x", "Zuger Zeitung", datetime(2026, 9, 13, tzinfo=UTC))
        _fact(s, a, "award", "Sportlerin des Jahres", "web", item.url, item_id=item.id, value={"year": 2026})
        s.commit()
        rows = card_data.achievements(s, a)
    assert "Zuger Zeitung, 13.09.2026" in [r["quelle"] for r in rows]  # seeds add roster medals ("Kader")
    assert tl._kind({"source_type": "web", "quelle": "[SRF]", "url": "https://srf.ch/x"}) == (
        "news",
        "Webseite",
    )
    assert tl._kind({"source_type": "profile", "quelle": "[X]", "url": "https://x.ch"})[1] == "Profil"
    assert tl._kind({"source_type": "web", "url": "https://de.wikipedia.org/wiki/X"})[1] == "Wikipedia"
