"""Gap loop round 2: current status per person (injury reconciled with later nominations / games), Vorsicht only
with a stated reason, "Zu beachten" without cut-off sentences."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from klugscheisser import person_status as ps
from klugscheisser.classify.base import ClassificationResult, ClassifyInput, is_vorsicht, vorsicht_reason
from klugscheisser.classify.router import ClassifierRouter
from klugscheisser.classify.rules import RuleBasedClassifier
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.mcp_server import caution, present, tools

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
ME = 7


# --- (b) a Vorsicht flag needs a reason ------------------------------------------------------------------------


def _c(category="other", mood=None, storyline=None, topics=None, vorsicht=True):
    return m.Classification(
        relevant_p=0.9,
        relevant=True,
        category=category,
        sensitive_p=0.8,
        vorsicht=vorsicht,
        newsworthiness=3,
        mood=mood,
        storyline=storyline,
        topics=topics,
        classifier="jev",
        classifier_version="1",
    )


def test_vorsicht_without_a_reason_is_no_vorsicht():
    # Balzer: "Still thinking about this one." – Jev said sensitive, category "Anderes", mood nachdenklich
    balzer = _c("other", "nachdenklich", topics={"verletzung": 0.09, "beziehung": 0.03})
    assert vorsicht_reason(balzer) is None and not is_vorsicht(balzer)
    assert vorsicht_reason(_c("injury")) == "Verletzung"
    assert vorsicht_reason(_c("team_coach", topics={"verletzung": 0.92})) == "Verletzung"
    assert vorsicht_reason(_c("controversy")) == "Kontroverse"
    assert vorsicht_reason(_c("result", storyline="karriereende")) == "Karriereende/Pause"
    assert vorsicht_reason(_c("private_life")) == "Privatleben"
    assert vorsicht_reason(_c("result", "trauer")) is None  # a lost game is no reason for caution
    assert vorsicht_reason(_c("other", "trauer")) == "Trauer/Abschied"
    assert not is_vorsicht(_c("injury", vorsicht=False))


class _Fixed:
    name = "jev"

    def __init__(self, **kw):
        self.kw = kw

    def classify(self, item):
        return ClassificationResult(
            relevant_p=0.9, sensitive_p=0.9, newsworthiness=3, classifier="jev", **self.kw
        )


def test_router_drops_a_flag_without_reason():
    settings = Settings()
    item = ClassifyInput(text="Still thinking about this one.", source_type="tagged_post")
    assert (
        ClassifierRouter(_Fixed(category="other", mood="nachdenklich"), None, settings)
        .classify(item)
        .vorsicht
        is False
    )
    assert ClassifierRouter(_Fixed(category="injury"), None, settings).classify(item).vorsicht is True


def test_rules_give_their_extra_sensitive_words_a_reason():
    r = RuleBasedClassifier().classify(ClassifyInput(text="Sie liegt krank im Spital.", source_type="srf"))
    assert vorsicht_reason(r) == "Verletzung"
    r = RuleBasedClassifier().classify(ClassifyInput(text="Rücktritt nach der Saison.", source_type="srf"))
    assert vorsicht_reason(r) == "Karriereende/Pause"


def test_roster_line_shows_the_reason_and_the_status():
    team = {
        "team": "Schweiz",
        "kader_status": "bestätigt",
        "spielerinnen": [
            {
                "name": "A",
                "position": "Stürmerin",
                "vorsicht": [{"x": 1}],
                "vorsicht_grund": "Verletzung 12.10.",
            },
            {
                "name": "B",
                "position": "Stürmerin",
                "status_hinweis": "Verletzung 19.09., seither: [2 Tore 14.10.](u)",
            },
            {"name": "C", "position": "Stürmerin", "vorsicht": [], "vorsicht_grund": None},
        ],
    }
    text = present.render_markdown({"teams": [team]})
    assert "- A (Stürmerin) ⚠️ Verletzung 12.10.\n" in text
    assert "- B (Stürmerin) · Verletzung 19.09., seither: [2 Tore 14.10.](u)\n" in text
    assert "- C (Stürmerin)\n" in text


# --- (c) sentence splitter, short lines ------------------------------------------------------------------------


def test_first_sentence_does_not_stop_after_ordinals_dates_or_abbreviations():
    f = present.first_sentence
    assert f("In der 56. Minute traf Enzler zum 1:2. Dann glich Leemann aus.") == (
        "In der 56. Minute traf Enzler zum 1:2."
    )
    assert f("Das Team spielt vom 28. bis 31. Oktober 2026 in Södertälje. Mehr dazu.").endswith("Södertälje.")
    assert (
        f("Am 13.10. gab die SIHF das Aufgebot bekannt. Weiter.")
        == "Am 13.10. gab die SIHF das Aufgebot bekannt."
    )
    assert f("Laut Dr. Meier ist z. B. alles gut gegangen in Bern. Ja.") == (
        "Laut Dr. Meier ist z. B. alles gut gegangen in Bern."
    )
    assert f("Tor! Leemann trifft in der 59. Minute zum Ausgleich. Danach") == (
        "Tor! Leemann trifft in der 59. Minute zum Ausgleich."
    )
    assert (
        f("Die Schweiz gewinnt gegen Tschechien 3:2. Danach mehr.")
        == "Die Schweiz gewinnt gegen Tschechien 3:2."
    )


def test_too_short_lines_are_not_zu_beachten():
    assert not tools._says_something({"zusammenfassung": "Summer Recap☀️🗺️"})
    assert not tools._says_something({"zusammenfassung": "In der 56."})
    assert tools._says_something({"zusammenfassung": "Sinja Leemann mit dem Ausgleich!"})
    assert tools._says_something({"zusammenfassung": "x", "titel": "Eggli und Waser fallen aus"})


# --- (a) status: appearances, since, caution ------------------------------------------------------------------


def _game(text="", raw=None, **kw) -> m.Item:
    return m.Item(
        id=kw.pop("id", 1),
        source_type="official_result",
        platform="sihf",
        url="https://sihf.ch/g/1",
        content_hash="g",
        title="ZSC Lions – EV Zug 1:3",
        text=text,
        raw=raw or {},
        published_at=kw.pop("published_at", NOW),
    )


def _athlete(name="Luisa Waser") -> m.Athlete:
    return m.Athlete(id=ME, full_name=name, name_normalized=name.lower(), aliases=[])


def test_appearance_from_the_swiss_line_participants_and_lineup():
    a = _athlete()
    swiss = "Ort: Zürich.\nSchweizerinnen: Alina Marti (1 Tor); Luisa Waser (2 Tore); Chiara Pfosi (Torhüterin: 1 Gegentore, 60:00 Min.)."
    assert ps.appearance(_game(swiss), a) == "2 Tore"
    assert ps.appearance(_game(swiss), _athlete("Chiara Pfosi")) == "Torhüterin (1 Gegentore, 60:00 Min.)"
    raw = {
        "participants": [
            {"name": "Waser Luisa", "role": "scorer"},
            {"name": "Luisa Waser", "role": "assist"},
            {"name": "Lara Stalder", "role": "assist"},
        ],
        "lineup": {"EV Zug": ["Nina Harju", "Luisa Waser"]},
    }
    assert ps.appearance(_game(raw=raw), a) == "1 Tor, 1 Assist"
    assert (
        ps.appearance(_game(raw={"lineup": {"EV Zug": ["Nina Harju"]}}), _athlete("Nina Harju"))
        == "im Einsatz"
    )
    assert ps.appearance(_game(raw={"lineup": {"EV Zug": ["Nina Harju"]}}), a) is None  # linked ≠ played


def _sig(kind, d, short):
    return ps.Signal(kind, d, short, short, f"https://x/{short}", "SIHF")


def _status(injury_day=date(2026, 9, 19), signals=()):
    inj = _sig("verletzung", injury_day, "Verletzung") if injury_day else None
    return ps.PersonStatus(ME, "Luisa Waser", inj, sorted(signals, key=lambda s: s.day, reverse=True))


def test_since_lists_what_happened_after_the_injury_oldest_first():
    st = _status(
        signals=[
            _sig("spiel", date(2026, 8, 22), "im Einsatz 22.08."),
            _sig("spiel", date(2026, 10, 14), "2 Tore 14.10."),
            _sig("aufgebot", date(2026, 10, 13), "im Aufgebot 13.10."),
        ]
    )
    assert st.since_text() == "seither: im Aufgebot 13.10., 2 Tore 14.10."
    assert st.resolved() and not st.resolved(date(2026, 10, 14))
    assert st.since_md().startswith("seither: [im Aufgebot 13.10.](https://x/im Aufgebot 13.10.)")
    assert not _status(signals=[_sig("spiel", date(2026, 8, 22), "x")]).resolved()


def _item(iid, days_ago, category="injury", kind="media", vorsicht=True, mood=None):
    it = m.Item(
        id=iid,
        source_type="web",
        platform="web",
        source_kind=kind,
        url=f"https://example.org/{iid}",
        content_hash=str(iid),
        published_at=NOW - timedelta(days=days_ago),
        text=f"Bericht {iid}",
    )
    it.classification = _c(category, mood, vorsicht=vorsicht)
    it.athletes = [m.ItemAthlete(athlete_id=ME, match_kind="name", main=None)]
    return it


def test_caution_drops_resolved_injuries_and_says_more_for_open_ones():
    recent, old = _item(1, 7), _item(2, 30)  # 12.10. and 19.09.
    back = _status(None, [_sig("spiel", date(2026, 10, 14), "2 Tore 14.10.")])
    back.injury = _sig("verletzung", date(2026, 9, 19), "Verletzung")

    def pick(items, st):
        return [(i.id, n) for i, n in caution.select(items, ME, caution.is_caution, 21, NOW, st)]

    assert pick([recent], back) == []  # she played after the report
    assert pick([old], back) == []  # the old report is not shown for lack of news either
    still = _status(None, [_sig("spiel", date(2026, 9, 1), "im Einsatz 01.09.")])
    assert pick([recent], still) == [(1, None)]
    note = pick([old], still)[0][1]
    assert "kein Aufgebot und kein Spiel" in note
    joke = _item(3, 2, category="other", mood="nachdenklich")  # flagged, but no reason
    assert pick([joke], None) == []


# --- end to end on the test database ---------------------------------------------------------------------------


def _setup(s: Session, now: datetime, injury_kind="media"):
    sport = repo.get_sport(s, "icehockey_women")
    waser = repo.upsert_athlete(s, sport, "Luisa Waser", nationality="SUI", position="F")
    coach = repo.upsert_athlete(s, sport, "Saara Coach", role="coach")
    team = m.Team(sport_id=sport.id, key="sui-women", name="Schweiz", kind="national", country="SUI")
    s.add(team)
    s.flush()
    ev = m.Event(key="test-cup", name="Test Cup", kind="tournament", starts_at=now - timedelta(days=6))
    s.add(ev)
    s.flush()
    s.add(
        m.EventEntry(
            event_id=ev.id,
            athlete_id=waser.id,
            team_id=team.id,
            role="F",
            list_type="confirmed",
            source="SIHF",
            source_url="https://sihf.ch/aufgebot",
        )
    )
    s.add(m.EventTeam(event_id=ev.id, team_id=team.id))
    for iid, (aid, days, kind, cat, title) in enumerate(
        [
            (waser.id, 30, injury_kind, "injury", "Eggli und Waser fallen aus, Operation nötig"),
            (waser.id, 25, "fan", "injury", "Waser won't be on the ice"),  # a fan: no injury report
            (coach.id, 20, "media", "injury", "Coach mit Knieproblemen"),
        ],
        start=1,
    ):
        it = m.Item(
            source_type="web",
            platform="web",
            source_kind=kind,
            url=f"https://news.example/{iid}",
            content_hash=str(iid),
            title=title,
            text=title,
            published_at=now - timedelta(days=days),
        )
        it.classification = _c(cat, "verletzung")
        it.athletes = [m.ItemAthlete(athlete_id=aid, match_kind="name")]
        s.add(it)
    game = m.Item(
        source_type="official_result",
        platform="sihf",
        source_kind="result",
        source_name="SIHF",
        url="https://sihf.ch/game/1",
        content_hash="g1",
        title="ZSC Lions – EV Zug 1:3",
        text="PostFinance Women's League: ZSC Lions – EV Zug 1:3.\nSchweizerinnen: Luisa Waser (2 Tore).",
        published_at=now - timedelta(days=5),
    )
    game.classification = _c("result", vorsicht=False)
    game.athletes = [m.ItemAthlete(athlete_id=waser.id, match_kind="name")]
    s.add(game)
    s.flush()
    return waser, coach


def test_status_from_the_database(db):
    with Session(db) as s:
        waser, coach = _setup(s, NOW)
        states = ps.statuses(s, [waser, coach], NOW)
        assert coach.id not in states  # staff: no player status
        st = states[waser.id]
        assert st.injury.text.startswith("Eggli und Waser fallen aus")  # the fan post is no report
        # (dates without year in the current year)
        assert re.fullmatch(r"seither: im Aufgebot 13\.10\.(2026)?, 2 Tore 14\.10\.(2026)?", st.since_text())
        assert st.resolved()
        assert st.latest.short.startswith("2 Tore 14.10.")


def test_tools_show_the_status_and_drop_the_resolved_injury(db):
    now = datetime.now(UTC)
    with Session(db) as s:
        _setup(s, now)
        s.commit()
        athlete = present.render_markdown(tools.get_athlete(s, "Luisa Waser"))
        event = present.render_markdown(tools.get_event(s, "test-cup"))
        roster = present.render_markdown(tools.get_team_roster(s, "Schweiz"))
    assert "**🩺 Aktueller Stand**" in athlete
    assert "Letzte Verletzungsmeldung" in athlete and "Eggli und Waser fallen aus" in athlete
    assert "– seither: [im Aufgebot" in athlete and "[2 Tore " in athlete
    assert "**⚠️ Vorsicht**" not in athlete  # resolved: she was nominated and scored since
    line = next(x for x in event.splitlines() if x.startswith("- Luisa Waser"))
    assert "⚠️" not in line and "· Verletzung " in line and "seither: [im Aufgebot" in line
    assert "· Verletzung " in next(x for x in roster.splitlines() if x.startswith("- Luisa Waser"))


def test_open_injury_keeps_its_vorsicht_with_reason(db):
    now = datetime.now(UTC)
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Nina Test", nationality="SUI")
        team = m.Team(sport_id=sport.id, key="sui-women", name="Schweiz", kind="national", country="SUI")
        s.add(team)
        s.flush()
        ev = m.Event(key="test-cup", name="Test Cup", kind="tournament", starts_at=now - timedelta(days=40))
        s.add(ev)
        s.flush()
        s.add(m.EventEntry(event_id=ev.id, athlete_id=a.id, team_id=team.id, list_type="confirmed"))
        s.add(m.EventTeam(event_id=ev.id, team_id=team.id))
        it = m.Item(
            source_type="srf",
            platform="rss",
            source_kind="srf",
            url="https://srf.ch/1",
            content_hash="1",
            title="Nina Test fällt mit einer Knieverletzung aus",
            text="x",
            published_at=now - timedelta(days=3),
        )
        it.classification = _c("injury", "verletzung")
        it.athletes = [m.ItemAthlete(athlete_id=a.id, match_kind="name")]
        s.add(it)
        s.commit()
        event = present.render_markdown(tools.get_event(s, "test-cup"))
        athlete = present.render_markdown(tools.get_athlete(s, "Nina Test"))
    assert "- Nina Test (" in event and "⚠️ Verletzung" in event
    assert "**⚠️ Vorsicht**" in athlete
    assert "seither kein Aufgebot und kein Spiel in unseren Daten (Fitness offen)" in athlete
