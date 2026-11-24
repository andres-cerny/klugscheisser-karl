"""SEARCH_NAMES: a person named only in the MCP search question brings her own documents as candidates."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.retrieval import docs
from klugscheisser.retrieval.search import Filters

SETTINGS = Settings(_env_file=None, auto_refresh=False)
OFF = SETTINGS.model_copy(update={"search_names": False})


def add_item(s, url, text, *, person=None, days_ago=0, source_type="srf"):
    item = m.Item(
        source_type=source_type,
        platform="rss",
        source_name="SRF",
        url=url,
        title=text[:40],
        text=text,
        content_hash=url,
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
    )
    s.add(item)
    s.flush()
    item.classification = m.Classification(
        relevant_p=0.9,
        relevant=True,
        category="other",
        sensitive_p=0.1,
        newsworthiness=2,
        classifier="t",
        classifier_version="1",
    )
    if person is not None:
        item.athletes.append(m.ItemAthlete(athlete_id=person.id))
    s.flush()
    return item


@pytest.fixture
def world(db):
    docs._NAME_MATCHERS.clear()  # names of earlier tests must not be cached
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        people = {}
        for name in ["Lara Testalder", "Andrea Bränztli", "Alina Mustermüller", "Nina Gleich", "Eva Müller"]:
            a = repo.upsert_athlete(s, sport, name)
            a.tier = 1
            people[name] = a
        repo.upsert_athlete(s, sport, "Sara Müller").tier = 1  # 'Müller' twice: never a surname match
        for i in range(30):  # newer posts that use the question's word, about nobody
            add_item(s, f"https://noise/{i}", f"Operation geglückt, sagt der Trainer {i}.", days_ago=i)
        # her own post: no name in the text, older than all the noise
        add_item(
            s,
            "https://lara-own",
            "Danke für alles nach der Operation, bald wieder auf dem Eis!",
            person=people["Lara Testalder"],
            days_ago=40,
            source_type="athlete_post",
        )
        add_item(
            s, "https://other", "Andrea Bränztli nach Operation zurück.", person=people["Andrea Bränztli"]
        )
        s.commit()
    yield db
    docs._NAME_MATCHERS.clear()


def names(s, query):
    return sorted(s.get(m.Athlete, i).full_name for i in docs.named_persons(s, query))


def test_names_full_surname_accents_several_and_typo(world):
    with Session(world) as s:
        assert names(s, "Lara Testalder Operation") == ["Lara Testalder"]
        assert names(s, "lara testalder") == ["Lara Testalder"]  # case
        assert names(s, "Testalder Saisonstart") == ["Lara Testalder"]  # unique surname of a tier 1 person
        assert names(s, "Branztli Shutout") == ["Andrea Bränztli"]  # accents / umlauts folded
        assert names(s, "Braenztli Shutout") == ["Andrea Bränztli"]
        assert names(s, "Testalder und Bränztli Olympia") == ["Andrea Bränztli", "Lara Testalder"]
        assert names(s, "Lara Testaldr Verletzung") == ["Lara Testalder"]  # small typo
        assert names(s, "Kreuzbandriss Saison vorbei") == []


def test_ambiguous_common_and_word_surnames_are_not_persons(world):
    with Session(world) as s:
        assert names(s, "Müller Verletzung") == []  # two Müllers in the database
        assert names(s, "gleich viele Tore wie letztes Jahr") == []  # an everyday word, not Nina Gleich
        assert names(s, "Nina Gleich Tore") == ["Nina Gleich"]  # the full name still counts
        assert names(s, "Eva Müller Tore") == ["Eva Müller"]


def test_own_post_without_name_becomes_candidate(world):
    with Session(world) as s:
        q = "Lara Testalder Operation"
        before, _, _ = docs.named_candidates(s, OFF, q, Filters(), cap=12)
        after, why, named = docs.named_candidates(s, SETTINGS, q, Filters(), cap=12)
        assert "https://lara-own" not in {i.url for i in before}
        assert "https://lara-own" in {i.url for i in after}
        own = next(i for i in after if i.url == "https://lara-own")
        assert "person" in why[own.id]
        assert len(after) <= 12 and named
        # a topic result about someone else stays among the candidates
        assert "https://other" in {i.url for i in after}


def test_find_documents_scores_own_post_and_marks_it(world):
    with Session(world) as s:
        hits = docs.find_documents(
            s, SETTINGS, "Lara Testalder Operation", Filters(), cap=12, top_n=40, min_score=0.0
        )
        own = next(h for h in hits if h.item.url == "https://lara-own")
        assert "person" in own.scores["gefunden"]


def test_not_used_with_person_filter_or_switch_off(world):
    with Session(world) as s:
        andrea = repo.resolve_athlete(s, "Andrea Bränztli")
        f = Filters(athlete_ids=[andrea.id])
        items, _why, named = docs.named_candidates(s, SETTINGS, "Lara Testalder Operation", f, cap=12)
        assert named == [] and "https://lara-own" not in {i.url for i in items}  # the given filter wins
        _, why_off, named_off = docs.named_candidates(s, OFF, "Lara Testalder Operation", Filters(), cap=12)
        assert named_off == [] and not any("person" in w for w in why_off.values())


def test_name_index_is_cached_per_database(world):
    with Session(world) as s:
        first = docs._name_matcher(s)
        assert docs._name_matcher(s) is first


def test_suche_tool_gets_names_too(world, monkeypatch):
    """Phase F `suche` reaches find_documents through search_tool: the name in its question counts too."""
    from klugscheisser.mcp_server import suche as F
    from klugscheisser.mcp_server import tools as t

    monkeypatch.setattr(t, "freshen", lambda s, athletes: {"status": "test"})
    seen = []
    real = docs.named_candidates

    def spy(*a, **kw):
        out = real(*a, **kw)
        seen.append(out[2])
        return out

    monkeypatch.setattr(docs, "named_candidates", spy)
    st = SETTINGS.model_copy(update={"retrieval_vector_candidates": False})
    with Session(world) as s:
        lara = repo.resolve_athlete(s, "Lara Testalder")
        F.suche(s, st, "Testalder Operation")
        assert seen and seen[-1] == [lara.id]
        F.suche(s, st, "Operation", person="Andrea Bränztli")  # person given: no name pass
        assert seen[-1] == []
