from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.team_matcher import TeamMatcher, has_women_context, relink_teams
from klugscheisser.seed.loader import load_all_seeds, load_team_aliases


@pytest.fixture
def s(db):
    with Session(db) as session:
        load_all_seeds(session)
        session.commit()
        yield session


def keys(s, text, url=None):
    matcher = TeamMatcher(s)
    return sorted(s.get(m.Team, link.team_id).key for link in matcher.from_text(text, url=url))


def test_aliases_load_for_all_wch_teams(s):
    aliases = load_team_aliases(s)
    wch = {k for k in s.scalars(select(m.Team.key).where(m.Team.kind == "national"))}
    assert wch <= set(aliases)
    assert {"evz-women", "zsc-frauen", "scb-frauen"} <= set(aliases)


def test_club_short_names_match_whole_words(s):
    assert keys(s, "Der EVZ gewinnt das Derby.") == ["evz-women"]
    assert keys(s, "Die ZSC Lions Frauen und der SCB trennen sich 2:2.") == ["scb-frauen", "zsc-frauen"]
    assert keys(s, "SC Bern Frauen verlieren gegen EV Zug Women") == ["evz-women", "scb-frauen"]


def test_no_substring_matches(s):
    assert keys(s, "Bernhard kommt im Anzug ins Stadion, Eishockey ist sein Hobby.") == []
    assert keys(s, "EVZOO und ZSCHOKKE") == []


def test_ambiguous_city_needs_hockey_context(s):
    assert keys(s, "Der Zug nach Zug hatte Verspätung.") == []
    assert keys(s, "Zug gewinnt das Eishockey-Spiel gegen Davos.") == ["evz-women", "hcd-ladies"]


def test_ambiguous_city_with_linked_player(s):
    player = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Lara Stalder"))
    assert player is not None
    assert keys(s, "Lara Stalder zieht nach Zug.") == ["evz-women"]


def test_women_specific_national_aliases(s):
    assert keys(s, "Damkronorna vann mot Kanada.") == ["swe-women"]
    assert keys(s, "Naisleijonat voitti.") == ["fin-women"]
    assert keys(s, "Smile Japan qualified.") == ["jpn-women"]
    assert keys(s, "Die Schweizerinnen gewinnen im Eishockey 3:1.") == ["sui-women"]
    assert keys(s, "České hokejistky porazily Němky.") == ["cze-women"]
    assert keys(s, "Die DEB-Frauen verlieren.") == ["ger-women"]


def test_mens_nati_does_not_link_womens_team(s):
    assert keys(s, "Die Nati der Männer verliert, Trainer Patrick Fischer ist enttäuscht.") == []
    assert keys(s, "Patrick Fischer bietet für die Nati 25 Spieler auf.") == []
    assert keys(s, "Team USA beats Team Canada in overtime.") == []
    assert keys(s, "Die Schweizer Nati will gewinnen und beginnen stark.") == []


def test_national_cue_with_womens_context(s):
    assert keys(s, "Frauen-Eishockey: Die Nati gewinnt gegen Schweden.") == ["sui-women"]
    assert keys(s, "Team USA beats Team Canada in the women's hockey final.") == ["can-women", "usa-women"]
    # a feed URL section alone is no hockey context (outlets file women's football under /eishockey/)
    url = "https://www.srf.ch/sport/eishockey/frauen-eishockey/nati-siegt"
    assert keys(s, "Die Nati siegt 3:1.", url=url) == []


def test_women_context_words():
    assert has_women_context("die spielerinnen jubeln")
    assert has_women_context("ms zen v hokeji")
    assert not has_women_context("wir wollen gewinnen und beginnen")
    assert not has_women_context("damit ist alles gesagt")


def _item(s, url, text, platform="rss"):
    item = m.Item(
        url=url,
        text=text,
        title=None,
        content_hash=url,
        source_type="srf",
        platform=platform,
        published_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    s.add(item)
    s.flush()
    return item


def test_relink_is_idempotent_and_skips_instagram(s):
    news = _item(s, "https://example.ch/a", "Eishockey: Der EVZ schlägt den SCB, die Schweizerinnen jubeln.")
    insta = _item(s, "https://instagram.com/p/x", "EVZ!", platform="instagram")
    news.teams.append(m.ItemTeam(team_id=s.scalar(select(m.Team.id).where(m.Team.key == "evz-women"))))
    s.flush()
    assert relink_teams(s) == 2
    assert relink_teams(s) == 0
    assert sorted(t.team.key for t in news.teams) == ["evz-women", "scb-frauen", "sui-women"]
    assert insta.teams == []
    assert s.scalar(select(func.count()).select_from(m.ItemTeam)) == 3
    assert relink_teams(s, since=datetime(2027, 1, 1, tzinfo=UTC)) == 0
