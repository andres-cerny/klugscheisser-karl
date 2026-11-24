"""German interviews from stored texts (facts/german_evidence.py): prefilter, the bestätigt rule, copies of club
releases, own posts, idempotent reruns, and the output in facts/german.py. The model is injected (no network)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import german
from klugscheisser.facts import german_evidence as ge

TODAY = date(2026, 10, 25)

INTERVIEW = (
    "Noemi Testová im Gespräch mit SRF. «Ich habe in Zug sehr viel gelernt und ich freue mich auf die WM», sagt "
    "Testová gegenüber SRF nach dem Training. Die Tschechin spielte zwei Saisons beim EVZ."
)
RELEASE = (
    "Klára Davosová verlängert beim HCD. «Ich bin sehr glücklich hier in Davos und wir haben noch viel vor», "
    "sagt Davosová. Der Club freut sich auf eine weitere Saison mit der Verteidigerin."
)


def _world(s: Session) -> dict:
    sport = repo.get_sport(s, "icehockey_women")
    cze = m.Team(sport_id=sport.id, key="cze-women", name="Tschechien", kind="national", country="CZE")
    sui = m.Team(sport_id=sport.id, key="sui-women", name="Schweiz", kind="national", country="SUI")
    s.add_all([cze, sui])
    s.flush()
    ev = m.Event(
        key="wch-2026",
        name="IIHF Frauen-WM 2026",
        kind="tournament",
        starts_at=datetime(2026, 11, 6, tzinfo=UTC),
    )
    s.add(ev)
    s.flush()

    def person(name, team, nat):
        a = repo.upsert_athlete(s, sport, name, nationality=nat)
        s.add(m.EventEntry(event_id=ev.id, athlete_id=a.id, team_id=team.id, role="F", source="test"))
        return a

    w = {
        "zug": person("Noemi Testová", cze, "CZE"),
        "davos": person("Klára Davosová", cze, "CZE"),
        "post": person("Petra Postová", cze, "CZE"),
        "swiss": person("Lara Schweizer", sui, "SUI"),
    }
    s.flush()
    return w


def _item(s, url, text, athletes, kind="srf", source_name="SRF", platform="web", match="name", day=12):
    it = m.Item(
        source_type="other_media",
        platform=platform,
        source_name=source_name,
        url=url,
        text=text,
        content_hash=url,
        source_kind=kind,
        published_at=datetime(2026, 3, day, 12, tzinfo=UTC),
    )
    s.add(it)
    s.flush()
    for a in athletes:
        s.add(m.ItemAthlete(item_id=it.id, athlete_id=a.id, match_kind=match))
    s.flush()
    return it


class FakeLLM:
    def __init__(self, verdicts):
        self.verdicts = verdicts  # url -> list of verdict dicts
        self.calls = 0

    def __call__(self, system, prompt, schema):
        self.calls += 1
        url = next(u for u in self.verdicts if u in prompt)
        return {"zitate": self.verdicts[url]}, {}


def _v(quote, rahmen="stellungnahme_medium", original="ja", hers=True):
    return {
        "nr": 1,
        "von_ihr": hers,
        "zitat": quote,
        "rahmen": rahmen,
        "deutsch_original": original,
        "grund": "x",
    }


def _rows(s, aid=None):
    q = select(m.Fact).where(m.Fact.kind == german.EVIDENCE_KIND)
    if aid:
        q = q.where(m.Fact.athlete_id == aid)
    return list(s.scalars(q))


def test_prefilter_quotes():
    assert ge.quote_is_german("Ich habe in Zug sehr viel gelernt und freue mich")
    assert not ge.quote_is_german("I learned a lot in Zug and I am looking forward")
    assert ge.looks_german(INTERVIEW)
    assert not ge.looks_german(
        "Testová scored twice as Czechia beat Finland 3-1 in the opening game of the EHT."
    )


def test_interview_is_confirmed_and_rerun_is_free(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.srf.ch/a", INTERVIEW, [w["zug"]])
        llm = FakeLLM({"srf.ch/a": [_v("Ich habe in Zug sehr viel gelernt und ich freue mich auf die WM")]})
        rep = ge.run(s, Settings(), llm=llm, today=TODAY)
        assert llm.calls == 1 and rep.written == 1
        (f,) = _rows(s, w["zug"].id)
        assert f.value["label"] == "bestaetigt" and f.value["date"] == "2026-03-12"
        assert f.value_text == "Aussage auf Deutsch gegenüber SRF" and f.source_url == "https://www.srf.ch/a"
        assert f.quote.startswith("Ich habe in Zug")
        h = german.hint(s, w["zug"], TODAY)
        assert h.label == german.CONFIRMED
        assert h.line().startswith(
            "Deutsch: bestätigt – Aussage auf Deutsch gegenüber SRF (12.03.2026): «Ich habe"
        )
        rep2 = ge.run(s, Settings(), llm=llm, today=TODAY)
        assert llm.calls == 1 and rep2.cached == 1 and rep2.written == 0 and len(_rows(s)) == 1
        ge.run(s, Settings(), llm=llm, today=TODAY, force=True)
        assert llm.calls == 2 and len(_rows(s)) == 1  # replaced, never duplicated


def test_club_release_and_copy_are_only_likely(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.hcd.ch/news/1", RELEASE, [w["davos"]], kind="official", source_name="HC Davos")
        _item(
            s,
            "https://www.fan-site.ch/x",
            RELEASE,
            [w["davos"]],
            kind="web",
            source_name="fan-site.ch",
            day=13,
        )
        q = "Ich bin sehr glücklich hier in Davos und wir haben noch viel vor"
        llm = FakeLLM({"hcd.ch": [_v(q)], "fan-site.ch": [_v(q, rahmen="stellungnahme_medium")]})
        ge.run(s, Settings(), llm=llm, today=TODAY)
        rows = {f.source_url: f for f in _rows(s, w["davos"].id)}
        assert {f.value["label"] for f in rows.values()} == {"wahrscheinlich"}
        assert "Club-/Verbandsmitteilung (HC Davos)" in rows["https://www.hcd.ch/news/1"].value_text
        assert "gleiches Zitat auch bei HC Davos" in rows["https://www.fan-site.ch/x"].value_text
        h = german.hint(s, w["davos"], TODAY)
        assert h.label == german.LIKELY and "1 weiterer solcher Text" in h.line()
        assert "Kein deutsches Interview" in h.line()


def test_rejected_not_hers_or_translated(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.srf.ch/b", INTERVIEW, [w["zug"]])
        _item(
            s,
            "https://www.blick.ch/c",
            INTERVIEW.replace("nach dem Training", "auf Englisch"),
            [w["zug"]],
            kind="boulevard",
            source_name="Blick",
        )
        q = "Ich habe in Zug sehr viel gelernt und ich freue mich auf die WM"
        llm = FakeLLM({"srf.ch/b": [_v(q, hers=False)], "blick.ch/c": [_v(q, original="unklar")]})
        rep = ge.run(s, Settings(), llm=llm, today=TODAY)
        assert _rows(s) == []
        reasons = sorted(d.reason.split(":")[0] for d in rep.decisions)
        assert reasons == ["nicht ihre Worte", "übersetztes Zitat"]


def test_quote_must_be_verbatim(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.srf.ch/d", INTERVIEW, [w["zug"]])
        llm = FakeLLM({"srf.ch/d": [_v("Ich spreche fliessend Deutsch und liebe die Schweiz")]})
        ge.run(s, Settings(), llm=llm, today=TODAY)
        (f,) = _rows(s)  # falls back to the prefilter's verbatim quote
        assert f.quote.startswith("Ich habe in Zug")


def test_own_german_post_is_likely_without_model(db):
    with Session(db) as s:
        w = _world(s)
        _item(
            s,
            "https://www.instagram.com/p/1",
            "Ich bin so stolz auf unser Team, wir haben es wirklich geschafft und ich freue mich auf die WM!",
            [w["post"]],
            kind="self",
            source_name="@petra",
            platform="instagram",
            match="author",
        )
        llm = FakeLLM({})
        ge.run(s, Settings(), llm=llm, today=TODAY)
        (f,) = _rows(s, w["post"].id)
        assert llm.calls == 0 and f.value["label"] == "wahrscheinlich" and "eigener Post" in f.value_text


def test_german_nations_and_free_mode(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.srf.ch/e", INTERVIEW.replace("Noemi Testová", "Lara Schweizer"), [w["swiss"]])
        _item(s, "https://www.srf.ch/f", INTERVIEW, [w["zug"]])
        llm = FakeLLM({"srf.ch/f": [_v("Ich habe in Zug sehr viel gelernt und ich freue mich auf die WM")]})
        assert w["swiss"].id not in ge.roster_persons(s, TODAY)
        rep = ge.run(s, Settings(), llm=llm, paid=False, today=TODAY)
        assert llm.calls == 0 and rep.candidates == 1 and _rows(s) == []
        assert s.scalar(select(func.count()).select_from(m.CacheEntry)) == 0  # candidate stays open


def test_unlinked_item_naming_her_in_full(db):
    with Session(db) as s:
        w = _world(s)
        _item(s, "https://www.srf.ch/g", INTERVIEW, [])
        llm = FakeLLM({"srf.ch/g": [_v("Ich habe in Zug sehr viel gelernt und ich freue mich auf die WM")]})
        rep = ge.run(s, Settings(), llm=llm, today=TODAY)
        assert rep.pairs == 1 and len(_rows(s, w["zug"].id)) == 1
