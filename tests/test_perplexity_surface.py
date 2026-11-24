"""What Perplexity sees of Karl (gap loop round 2, 2026-10-19): server instructions in the user's language, the
rules once in the instructions instead of a long footer per call, a description for every tool parameter, one
source line per block when every line has the same link, and a team's last games in list_events."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import present

DEB = "[DEB, Staff (07.10.2026)](https://www.deb-online.de/frauen-nationalmannschaft/)"


def _tools():
    from klugscheisser.mcp_server.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_instructions_answer_in_the_users_language_and_carry_the_rules_once():
    from klugscheisser.mcp_server.server import INSTRUCTIONS

    assert "in German" not in INSTRUCTIONS
    assert "language of the user's question" in INSTRUCTIONS
    for rule in ("Datenstand", "link unchanged", "Quelle für alle Zeilen", "Vorsicht", "not proof"):
        assert rule in INSTRUCTIONS, rule
    # cheapest tool per question type
    for hint in (
        "stats for numbers",
        "get_event only for",
        "get_team_roster for one team",
        "list_events for",
    ):
        assert hint in INSTRUCTIONS, hint
    assert len(present.RULES) < 60  # the per-call footer is one short line


def test_every_tool_parameter_has_a_description():
    tools = _tools()
    missing = [
        (name, p)
        for name, t in tools.items()
        for p, spec in t.input_schema.get("properties", {}).items()
        if not spec.get("description")
    ]
    assert missing == []
    assert "quelle_link" not in tools["stats"].description  # old JSON field names are gone
    assert "Staff" in tools["get_team_roster"].description
    assert "LAST played games" in tools["list_events"].description  # #31: last N from all stored games
    assert "opponent" in tools["list_events"].description


def test_one_source_per_block_when_all_lines_share_the_link():
    staff = [f"- Person {i}, Funktion {i} {DEB}" for i in range(13)]
    text = "\n".join(
        ["**Datenstand:** heute", "**Deutschland** – Kader", "", "**Trainer und Staff**", *staff]
    )
    out = present.one_source_per_block(text)
    assert out.count("deb-online.de") == 1
    lines = out.splitlines()
    i = lines.index("**Trainer und Staff**")
    assert lines[i + 1] == f"_Quelle für alle Zeilen: {DEB}_"
    assert lines[i + 2] == "- Person 0, Funktion 0"


def test_lines_with_different_links_or_without_a_link_keep_theirs():
    mixed = "\n".join(
        [
            "**Erfolge**",
            *(f"- Titel {i} {DEB}" for i in range(4)),
            "- MVP [web](https://evz.ch/mvp)",
        ]
    )
    assert present.one_source_per_block(mixed) == mixed
    bare = "\n".join(["**Staff**", *(f"- Person {i} {DEB}" for i in range(4)), "- Person ohne Beleg"])
    assert present.one_source_per_block(bare) == bare
    two = "\n".join(["**Staff**", *(f"- Person {i} {DEB}" for i in range(2))])
    assert present.one_source_per_block(two) == two  # too few to be worth it
    more = "\n".join(["**Staff**", *(f"- Person {i} {DEB}" for i in range(4)), "- … und 3 weitere"])
    assert present.one_source_per_block(more).count("deb-online.de") == 1


def test_render_markdown_names_a_shared_source_once_and_ends_with_the_short_footer():
    link = "[Elite Prospects, Stand 04.10.2026](https://www.eliteprospects.com/player/1/x)"
    result = {
        "datenstand": "heute",
        "profil": {
            f"f{i}": {"feld": f"Feld {i}", "wert": f"Wert {i}", "quelle_link": link} for i in range(6)
        },
    }
    text = present.render_markdown(result)
    assert text.count("eliteprospects.com") == 1
    assert "_Quelle für alle Zeilen: [Elite Prospects" in text
    assert text.endswith(present.RULES)
    assert "Emoji + Aussage + Link" not in text


@pytest.fixture
def games(db):
    now = datetime.now(UTC)
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        usa = m.Team(sport_id=sport.id, key="usa-women", name="USA", kind="national", country="USA")
        cze = m.Team(sport_id=sport.id, key="cze-women", name="Tschechien", kind="national", country="CZE")
        s.add_all([usa, cze])
        comp = m.Competition(sport_id=sport.id, key="rivalry", name="Rivalry Series", kind="tournament")
        s.add(comp)
        s.flush()
        s.add(m.Event(key="rivalry", name="Rivalry Series", kind="tournament", competition_id=comp.id))
        for key, days, comp_id in [
            ("usa-g-old", -400, None),  # older than a year: listed too (#31: all stored games)
            ("usa-g-90", -90, None),  # a friendly three months ago
            ("usa-g-40", -40, comp.id),  # a tournament game
            ("usa-g-next", 3, None),
        ]:
            s.add(
                m.Event(
                    key=key,
                    name="USA – Tschechien",
                    kind="game",
                    home_team_id=usa.id,
                    away_team_id=cze.id,
                    starts_at=now + timedelta(days=days),
                    competition_id=comp_id,
                    status="final" if days < 0 else None,
                    score="3:1" if days < 0 else None,
                )
            )
        s.commit()
    return db


def test_list_events_with_team_lists_its_last_games_also_older_and_tournament_games(games):
    from klugscheisser.mcp_server import tools

    with Session(games) as s:
        out = tools.list_events(s, team="USA")
        assert [g["key"] for g in out["spiele_letzte"]] == ["usa-g-40", "usa-g-90", "usa-g-old"]
        assert [g["key"] for g in out["spiele_naechste"]] == ["usa-g-next"]
        plain = tools.list_events(s)  # without team: national-team games of the last 14 days only
        assert plain["spiele_letzte"] == []
        assert "die letzten 3" in out["mehr"]
        assert [g["key"] for g in tools.list_events(s, team="USA", last=2)["spiele_letzte"]] == [
            "usa-g-40",
            "usa-g-90",
        ]
