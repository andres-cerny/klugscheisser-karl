"""v9 UI (design "Variant C"): medals from Wikidata and the Erfolge block, the dated timeline, "Nächste Fragen",
person / team / event pages with the chat next to them, no emoji labels."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts.wikidata_import import import_wikidata_facts, medal_metal
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app, card_data
from klugscheisser.web import timeline as tl

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")
MEDAL_ROWS = Path(__file__).parent / "fixtures" / "wikidata" / "medal_rows.json"
EMOJI_LABELS = (
    "✨",
    "🏒",
    "📸",
    "🪪",
    "🎚️",
    "🔄",
    "⤵️",
    "✅",
    "🎉",
    "ℹ️",
    "➡️",
    "🖼️",
    "🔤",
    "⚠️",
    "🌐",
    "💬",
    "🔊",
    "🐢",
)


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _medalist(s: Session) -> m.Athlete:
    a = repo.resolve_athlete(s, "Lara Stalder")
    a.wikidata_qid = "Q90000001"
    s.flush()
    import_wikidata_facts(s, [a], rows=json.loads(MEDAL_ROWS.read_text())["results"]["bindings"])
    return a


def test_medal_metal_only_for_medals():
    assert (
        medal_metal("Olympic bronze medal") == "bronze"
        and medal_metal(None, "Olympische Goldmedaille") == "gold"
    )
    assert medal_metal("Silbermedaille an der WM") == "silver"
    assert medal_metal("Guldhjälmen") is None and medal_metal("MVP PostFinance Women's League") is None


def test_medal_import_and_erfolge(seeded):
    with Session(seeded) as s:
        a = _medalist(s)
        medals = s.scalars(select(m.Fact).where(m.Fact.athlete_id == a.id, m.Fact.kind == "medal")).all()
        # award received (P166) and participant in with ranking 3 (P1344) name the same medal: stored once;
        # ranking 6 is no medal
        assert [f.value["metal"] for f in medals] == ["bronze"] and medals[0].value["year"] == 2014
        assert all(f.source_url == "https://www.wikidata.org/wiki/Q90000001" for f in medals)
        assert not any("2019" in f.value_text for f in medals)
        award = s.scalar(select(m.Fact).where(m.Fact.athlete_id == a.id, m.Fact.kind == "award"))
        assert award.value_text == "Guldhjälmen (2020)"
        s.add(  # near-identical web fact: shown once
            m.Fact(
                athlete_id=a.id,
                kind="award",
                value={"text": "Guldhjälmen (2020)"},
                value_text="Guldhjälmen (2020)",
                source="web",
                source_url="https://www.example.ch/guldhjalmen",
                confidence=0.9,
                status="confirmed",
            )
        )
        s.flush()
        items = card_data.achievements(s, a)
        assert items[0]["kind"] == "medal" and items[0]["label"] == "Bronze" and items[0]["year"] == 2014
        assert sum(1 for x in items if x["kind"] == "medal") == 1
        assert [x["text"] for x in items].count("Guldhjälmen (2020)") == 1
        assert all(x["url"] for x in items)
        s.commit()
        aid = a.id
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get(f"/person/{aid}").text
    assert "id='erf-h'>Erfolge</h2>" in html and "<span class='ml bronze'>Bronze</span>" in html
    assert html.index("Bronze") < html.index("Guldhjälmen")


def test_no_medal_without_statement(seeded):
    with Session(seeded) as s:
        a = repo.resolve_athlete(s, "Alina Müller")
        assert not [x for x in card_data.achievements(s, a) if x["kind"] == "medal"]


def test_timeline_groups_upcoming_and_months():
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    p = {
        "vorsicht": [],
        "instagram": [
            {
                "url": "https://www.instagram.com/p/A/",
                "quelle": "[Instagram @lara, 30.09.]",
                "account": "@lara",
                "zeile_prefix": "📸 (von ihr selbst) 🎉 (Freude)",
                "zusammenfassung": "Spielszenen Schweiz – Schweden",
                "bildbeschreibung": "Zweikämpfe",
                "published_at": "2026-09-30T10:00:00+00:00",
            }
        ],
        "news": [
            {
                "url": "https://www.srf.ch/a",
                "quelle": "[SRF, 03.10.]",
                "titel": "Nati siegt",
                "published_at": "2026-10-03T10:00:00+00:00",
            }
        ],
        "medien": [
            {
                "url": "https://open.spotify.com/x",
                "quelle": "[EVZ, 18.09.2025]",
                "source_type": "media_link",
                "titel": "Hertitalk",
                "published_at": "2025-09-18T10:00:00+00:00",
            }
        ],
        "anlaesse": [
            {
                "key": "wch-2026",
                "name": "IIHF Frauen-WM 2026",
                "start": now + timedelta(days=33),
                "kader_status": "provisional",
                "quelle": "SIHF",
                "url": "https://www.sihf.ch/x",
            }
        ],
    }
    items = tl.entries(p)
    groups = tl.grouped(items, now)
    assert [g for g, _ in groups] == ["Demnächst", "Oktober 2026", "September 2026", "September 2025"]
    social = next(x for x in items if x["k"] == "social")
    assert social["sub"] == "von ihr selbst · Freude · Bild: Zweikämpfe" and social["src"] == "@lara"
    assert next(x for x in items if x["k"] == "podcasts")["label"] == "Podcast"
    html = tl.timeline_html(items, now)
    assert html.count('target="_blank" rel="noopener"') == 4  # every row opens its source
    counts = dict((k, n) for k, _, n in tl.counts(items, erfolge=2, funfacts=3))
    assert counts["alle"] == 4 + 2 + 3 and counts["social"] == 1 and counts["podcasts"] == 1


def test_person_questions_from_her_data():
    class F:
        value_text = "In ihrer Kindheit lieferte sie sich Duelle mit ihrem Bruder Dario."

    now = datetime(2026, 10, 4, tzinfo=UTC)
    p = {
        "vorsicht": [{"titel": "Nach Schreckmoment in der Nati", "published_at": "2026-08-24"}],
        "anlaesse": [{"name": "Euro Hockey Tour Lahti", "start": now - timedelta(days=5)}],
    }
    qs = tl.person_questions(p, [F()], now)
    assert qs == [
        "Was ist bekannt zu «Nach Schreckmoment in der Nati»?",
        "Mehr dazu: «In ihrer Kindheit lieferte sie sich …»",
        "Wie lief Euro Hockey Tour Lahti?",
    ]
    assert len(tl.person_questions({}, [], now)) == 2  # defaults only, nothing invented


def test_person_page_layout_and_no_emoji_labels(seeded):
    with Session(seeded) as s:
        aid = repo.resolve_athlete(s, "Lara Stalder").id
        s.commit()
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get(f"/person/{aid}").text
        chat = c.get(f"/person/{aid}", params={"tab": "chat"}).text
    assert (
        "<title>Lara Stalder · Klugscheisser Karl</title>" in html
        and '<a class="logo" href="/"' in html
        and '<span class="logo-t">Klugscheisser Karl</span>' in html
    )
    assert (
        "class='pg' data-view='info'" in html and "class='pg' data-view='chat'" in chat
    )  # ?tab=chat deep link
    assert "setFilter(this,'alle')" in html and "Steckbrief &amp; Karriere" in html
    assert "Nächste Fragen" in html and "onclick='askNext(this)'" in html
    assert 'aria-label="Chat vergrössern"' in html and "Daten &amp; Einstellungen" in html
    assert html.index("Daten &amp; Einstellungen") > html.index(
        'aria-labelledby="tl-h"'
    )  # admin at the bottom
    assert not [x for x in EMOJI_LABELS if x in html]


def test_team_and_event_pages_with_chat(seeded):
    with Session(seeded) as s:
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        tid = sui.id
    with TestClient(build_web_app(SETTINGS)) as c:
        team = c.get(f"/team/{tid}").text
        event = c.get("/event/wch-2026").text
        r = c.post(
            "/chat",
            data={
                "frage": "Wer ist verletzt?",
                "team": str(tid),
                "eingebettet": "1",
                "vorschlag": tl.TEAM_QUESTIONS,
            },
            headers={"HX-Request": "true"},
        )
        again = c.get(f"/team/{tid}").text
    for html in (team, event):
        assert "setView(this,'kader')" in html and "Wer hat die meisten Länderspiele?" in html
        assert "class='roster'" in html and "Stand: " in html
        assert not [x for x in EMOJI_LABELS if x in html]
    assert 'name="team" value="' + str(tid) + '"' in team and 'name="event" value="wch-2026"' in event
    # 03.11.2026: every WCh list is official, no "Provisorisch" tag (with a provisional list it is said once)
    assert "<span class='tag'>Provisorisch</span>" not in event and "Bestätigter Kader." in event
    assert "Wer spielt im Ausland?" in r.text  # the hints survive the new chat
    assert "Wer ist verletzt?</div>" in again  # the team page shows its latest chat
