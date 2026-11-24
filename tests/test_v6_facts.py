"""v6 step 2: fact layer — Wikidata import, verified extraction from documents, fact queries."""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts import queries
from klugscheisser.facts.extract import extract_facts, parse_season, select_documents
from klugscheisser.facts.teams import TeamResolver, team_key
from klugscheisser.facts.wikidata_import import import_wikidata_facts, season_from_date, season_to_date
from klugscheisser.seed.loader import load_all_seeds

SETTINGS = Settings(_env_file=None, auto_refresh=False)
FIXTURE = Path(__file__).parent / "fixtures" / "wikidata" / "facts_sui_5.json"
QIDS = {
    "Lara Stalder": "Q15743541",
    "Alina Müller": "Q15768448",
    "Christine Meier": "Q16229749",
    "Evelina Raselli": "Q1381662",
    "Andrea Brändli": "Q29717053",
}


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _person(s: Session, name: str, nationality: str = "SUI") -> m.Athlete:
    exact = [x.athlete for x in repo.find_athletes(s, name) if x.how == "exact"]
    a = exact[0] if exact else None
    if a is None:
        sport = s.scalar(select(m.Sport).where(m.Sport.key == "icehockey_women"))
        first, last = name.split(" ", 1)
        a = m.Athlete(
            sport_id=sport.id,
            full_name=name,
            first_name=first,
            last_name=last,
            name_normalized=name.lower(),
            nationality=nationality,
        )
        s.add(a)
        s.flush()
    return a


def _with_qids(s: Session) -> dict[str, m.Athlete]:
    out = {}
    for name, qid in QIDS.items():
        a = _person(s, name)
        a.wikidata_qid = qid
        out[name] = a
    s.flush()
    return out


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text())["results"]["bindings"]


def _team_id(s: Session, key: str) -> int:
    return s.scalar(select(m.Team.id).where(m.Team.key == key))


# --- Wikidata -------------------------------------------------------------------------------------------


def test_season_mapping():
    assert parse_season("2019/20") == 2019
    assert parse_season("2019–2020") == 2019 and parse_season("seit 2021") == 2021
    assert parse_season("") is None and parse_season("Saison unbekannt") is None
    assert season_from_date(date(2019, 1, 1), 9) == 2019  # year precision: from 2019/20
    assert season_from_date(date(2019, 3, 1), 11) == 2018  # spring: still 2018/19
    assert season_to_date(date(2019, 1, 1), 9) == 2018  # left in 2019: last season 2018/19
    assert season_to_date(date(2019, 10, 1), 11) == 2019


def test_wikidata_import_from_recorded_fixture(seeded):
    with Session(seeded) as s:
        p = _with_qids(s)
        stats = import_wikidata_facts(s, list(p.values()), rows=_rows())
        assert stats["persons"] == 5 and stats["with_data"] == 5
        # Linköpings FC is a football club in Wikidata (P641 = football): never attached as hockey stint
        assert stats["skipped_other_sport"] == 1
        lara = p["Lara Stalder"]
        facts = {f.kind: f for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id))}
        assert facts["birth_date"].value == {"date": "1994-05-15"}
        assert facts["birth_date"].value_text == "15.05.1994"
        assert facts["height_cm"].value["number"] == 166
        assert facts["position"].value == {"text": "Stürmerin", "code": "F"}
        assert facts["shoots"].value_text == "rechts"
        assert facts["birth_place"].value_text == "Luzern"
        for f in facts.values():
            if f.source == "roster":
                continue  # v8: captaincy from the seeded Olympic roster
            assert f.source == "wikidata" and f.quote is None and f.confidence == 1.0
            assert f.source_url == "https://www.wikidata.org/wiki/Q15743541"
        stints = {
            st.team_name: st
            for st in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == lara.id))
        }
        assert "Linköpings FC" not in stints
        umd = stints["Minnesota Duluth Bulldogs women's ice hockey"]
        assert (umd.kind, umd.season_from, umd.season_to) == ("college", 2013, 2016)
        nati = stints["Switzerland women's national ice hockey team"]
        assert nati.kind == "national" and nati.team_id == _team_id(s, "sui-women")
        brynas = stints["Brynäs IF Dam"]
        assert (brynas.kind, brynas.season_from, brynas.season_to) == ("club", 2019, None)
        assert all(st.quote is None and st.source == "wikidata" for st in stints.values())

        alina = p["Alina Müller"]
        zsc = s.scalar(
            select(m.CareerStint).where(
                m.CareerStint.athlete_id == alina.id, m.CareerStint.team_name == "ZSC Lions Frauen"
            )
        )
        assert zsc.team_id == _team_id(s, "zsc-frauen") and zsc.season_from is None
        draft = s.scalar(select(m.Fact).where(m.Fact.athlete_id == alina.id, m.Fact.kind == "draft"))
        assert draft.value["team"] == "Boston Fleet" and draft.value["year"] == 2023
        award = s.scalar(select(m.Fact).where(m.Fact.athlete_id == alina.id, m.Fact.kind == "award"))
        assert award.value_text == "Winterthurer Sportler/in des Jahres (2019)"
        # awards without a label in our languages are skipped (no "Q133310431" as text)
        assert not s.scalar(select(m.Fact).where(m.Fact.athlete_id == lara.id, m.Fact.kind == "award"))
        assert (
            s.scalar(
                select(m.Fact.value_text).where(
                    m.Fact.athlete_id == p["Andrea Brändli"].id, m.Fact.kind == "position"
                )
            )
            == "Torhüterin"
        )

        n_facts = s.scalar(select(func.count(m.Fact.id)))
        n_stints = s.scalar(select(func.count(m.CareerStint.id)))
        # idempotent: raw rows again, and WikiClient-style flat rows
        again = import_wikidata_facts(s, list(p.values()), rows=_rows())
        flat = [{k: v["value"] for k, v in r.items()} for r in _rows()]
        again2 = import_wikidata_facts(s, list(p.values()), rows=flat)
        assert again["facts"] == again["stints"] == again2["facts"] == again2["stints"] == 0
        assert s.scalar(select(func.count(m.Fact.id))) == n_facts
        assert s.scalar(select(func.count(m.CareerStint.id))) == n_stints


def test_wikidata_jersey_matches_and_fetch_chunks(seeded):
    calls = []

    class FakeWiki:
        def sparql(self, query):
            calls.append(query)
            return [
                {
                    "p": "http://www.wikidata.org/entity/Q15743541",
                    "prop": "P54",
                    "val": "http://www.wikidata.org/entity/Q1",
                    "valLabel": "EV Zug Frauen",
                    "valEn": "EVZ Women's Team",
                    "jersey": "10",
                    "matches": "150",
                    "start": "2021-08-15T00:00:00Z",
                    "startPrec": "11",
                    "sports": "Q41466",
                }
            ]

    with Session(seeded) as s:
        lara = _with_qids(s)["Lara Stalder"]
        import_wikidata_facts(s, [lara], wiki=FakeWiki())
        assert len(calls) == 1 and "wd:Q15743541" in calls[0]
        st = s.scalar(select(m.CareerStint).where(m.CareerStint.athlete_id == lara.id))
        assert st.team_id == _team_id(s, "evz-women") and st.season_from == 2021
        kinds = {f.kind: f for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id))}
        assert kinds["jersey_number"].value == {"number": "10", "team": "EVZ Women's Team"}
        assert kinds["appearances"].value == {"number": 150, "team": "EVZ Women's Team"}


def test_team_resolver(seeded):
    with Session(seeded) as s:
        r = TeamResolver(s)
        assert r.resolve("ZSC Lions") == _team_id(s, "zsc-frauen")
        assert r.resolve("Switzerland women's national ice hockey team", country="SUI") == _team_id(
            s, "sui-women"
        )
        assert r.resolve("Switzerland men's national ice hockey team", country="SUI") is None
        assert r.resolve("Switzerland women's national under-18 ice hockey team", country="SUI") is None
        assert r.resolve("HC Unbekannt") is None
    assert team_key("ZSC Lions Frauen") == team_key("ZSC Lions Women") == "zsc lions"
    assert team_key("Linköping HC Dam") == team_key("Linköping HC")


# --- extraction -----------------------------------------------------------------------------------------

WIKI_TEXT = (
    "Lara Stalder (* 15. Mai 1994 in Luzern) ist eine Schweizer Eishockeyspielerin. "
    "Sie ist 1,70 m gross und spielt als Stürmerin. Von 2013 bis 2017 spielte sie für die "
    "Minnesota Duluth Bulldogs in der NCAA. 2017 wechselte sie zu Linköping HC in die SDHL, wo sie "
    "zwei Saisons blieb. Seit 2019 spielt sie für Brynäs IF und wurde dort Captain. "
    "Neben dem Eishockey arbeitet sie gelegentlich als Expertin."
)


def _item(s, a, url, text, source_type="profile", platform="wikipedia", main=None, others=()):
    item = m.Item(
        source_type=source_type,
        platform=platform,
        source_name="Wikipedia (de)" if platform == "wikipedia" else "SRF",
        url=url,
        title=None,
        text=text,
        content_hash=url,
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
    )
    s.add(item)
    s.flush()
    item.athletes.append(m.ItemAthlete(athlete_id=a.id, main=main))
    for o in others:
        item.athletes.append(m.ItemAthlete(athlete_id=o.id, main=True))
    s.flush()
    return item


def _fake_llm(calls):
    def llm(system, prompt, schema):
        calls.append(prompt)
        assert "D1" in prompt and "Lara Stalder" in prompt
        return (
            {
                "fakten": [
                    {
                        "art": "birth_date",
                        "wert": "15. Mai 1994",
                        "datum": "1994-05-15",
                        "zahl": "",
                        "dok": "D1",
                        "zitat": "Lara Stalder (* 15. Mai 1994 in Luzern)",
                    },
                    {
                        "art": "height_cm",
                        "wert": "170 cm",
                        "datum": "",
                        "zahl": "170",
                        "dok": "D1",
                        "zitat": "Sie ist 1,70 m gross",
                    },
                    # not verbatim (paraphrase) -> dropped
                    {
                        "art": "position",
                        "wert": "Stürmerin",
                        "datum": "",
                        "zahl": "",
                        "dok": "D1",
                        "zitat": "sie spielt im Sturm",
                    },
                    # Jev does not support it (the text says "gelegentlich") -> unconfirmed
                    {
                        "art": "job",
                        "wert": "TV-Expertin bei SRF",
                        "datum": "",
                        "zahl": "",
                        "dok": "D1",
                        "zitat": "arbeitet sie gelegentlich als Expertin",
                    },
                    # unknown document -> dropped
                    {
                        "art": "nickname",
                        "wert": "Stali",
                        "datum": "",
                        "zahl": "",
                        "dok": "D9",
                        "zitat": "x y z",
                    },
                ],
                "stationen": [
                    {
                        "team": "Linköping HC",
                        "liga": "SDHL",
                        "von": "2017/18",
                        "bis": "2018/19",
                        "rolle": "player",
                        "art": "club",
                        "dok": "D1",
                        "zitat": "2017 wechselte sie zu Linköping HC in die SDHL",
                    },
                    {
                        "team": "Brynäs IF",
                        "liga": "SDHL",
                        "von": "2019/20",
                        "bis": "",
                        "rolle": "captain",
                        "art": "club",
                        "dok": "D1",
                        "zitat": "Seit 2019 spielt sie für Brynäs IF und wurde dort Captain",
                    },
                ],
            },
            {"input": 4000, "output": 600},
        )

    return llm


def _support(pairs):
    return [0.2 if "TV-Expertin" in statement else 0.95 for _quote, statement in pairs]


def test_extract_checks_quotes_support_conflicts_and_is_idempotent(seeded):
    with Session(seeded) as s:
        lara = _with_qids(s)["Lara Stalder"]
        import_wikidata_facts(s, [lara], rows=_rows())
        doc = _item(s, lara, "https://de.wikipedia.org/wiki/Lara_Stalder", WIKI_TEXT)
        # news where she is only mentioned and a page that does not name her: not used
        other = _person(s, "Alina Müller")
        _item(
            s,
            lara,
            "https://srf.ch/a",
            "Alina Müller trifft. Auch Stalder spielte.",
            "srf",
            "rss",
            main=False,
        )
        _item(s, lara, "https://srf.ch/b", "Ein Bericht über ein ganz anderes Team.", "srf", "rss", main=True)
        assert {i.url for i in select_documents(s, lara)} == {doc.url, "https://srf.ch/b"}
        assert other  # (kept for the mention case)

        calls: list[str] = []
        r = extract_facts(s, SETTINGS, lara, llm=_fake_llm(calls), support=_support)
        assert len(calls) == 1 and r["docs"] == 1  # the page without her name was not sent
        assert r["dropped"] == 2 and r["unconfirmed"] == 1 and r["facts"] == 3 and r["stints"] == 2
        assert r["cost_usd"] == pytest.approx(4000 / 1e6 * 0.25 + 600 / 1e6 * 1.5)
        ledger = s.scalar(select(m.CostLedger).where(m.CostLedger.source == "llm:gemini-facts"))
        assert ledger and ledger.cost_usd == pytest.approx(r["cost_usd"])

        extracted = list(
            s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id, m.Fact.source == "wikipedia"))
        )
        by_kind = {f.kind: f for f in extracted}
        assert set(by_kind) == {"birth_date", "height_cm", "job"}
        for f in extracted:
            assert f.source_item_id == doc.id and f.source_url == doc.url and f.quote in WIKI_TEXT
        assert by_kind["birth_date"].status == "confirmed"  # same as Wikidata
        assert by_kind["job"].status == "unconfirmed" and by_kind["job"].confidence == 0.2
        # 170 cm (Wikipedia) vs 166 cm (Wikidata): both kept, Wikidata stays confirmed
        assert by_kind["height_cm"].status == "conflict" and by_kind["height_cm"].value["number"] == 170
        wd_height = s.scalar(
            select(m.Fact).where(
                m.Fact.athlete_id == lara.id, m.Fact.kind == "height_cm", m.Fact.source == "wikidata"
            )
        )
        assert wd_height.status == "confirmed"

        stints = {
            st.team_name: st
            for st in s.scalars(
                select(m.CareerStint).where(
                    m.CareerStint.athlete_id == lara.id, m.CareerStint.source == "wikipedia"
                )
            )
        }
        assert (stints["Linköping HC"].season_from, stints["Linköping HC"].season_to) == (2017, 2018)
        assert stints["Brynäs IF"].role == "captain" and stints["Brynäs IF"].season_to is None
        assert stints["Brynäs IF"].quote.startswith("Seit 2019")

        # rerun: documents are marked, no second model call, nothing duplicated
        n = s.scalar(select(func.count(m.Fact.id)))
        r2 = extract_facts(s, SETTINGS, lara, llm=_fake_llm(calls), support=_support)
        assert len(calls) == 1 and r2["docs"] == 0 and s.scalar(select(func.count(m.Fact.id))) == n


def test_conflict_without_wikidata_and_no_support_check(seeded):
    with Session(seeded) as s:
        a = _person(s, "Lara Stalder")
        _item(s, a, "https://de.wikipedia.org/wiki/Lara_Stalder", "Lara Stalder ist 168 cm gross.")
        _item(
            s,
            a,
            "https://srf.ch/portrait",
            "Porträt: Lara Stalder misst 172 Zentimeter.",
            "srf",
            "rss",
            main=True,
        )

        def llm(system, prompt, schema):
            return {
                "fakten": [
                    {
                        "art": "height_cm",
                        "wert": "168 cm",
                        "datum": "",
                        "zahl": "168",
                        "dok": "D1",
                        "zitat": "Lara Stalder ist 168 cm gross",
                    },
                    {
                        "art": "height_cm",
                        "wert": "172 cm",
                        "datum": "",
                        "zahl": "172",
                        "dok": "D2",
                        "zitat": "Lara Stalder misst 172 Zentimeter",
                    },
                ],
                "stationen": [],
            }, {}

        # without a support check nothing is stored and the documents stay open
        r0 = extract_facts(s, SETTINGS, a, llm=llm, support=lambda pairs: [None] * len(pairs))
        assert r0["error"] and r0["facts"] == 0 and len(select_documents(s, a)) == 2

        r = extract_facts(s, SETTINGS, a, llm=llm, support=lambda pairs: [0.9] * len(pairs))
        assert r["conflicts"] == 2
        rows = queries.facts_for(s, [a.id], "height_cm")
        assert len(rows) == 1 and rows[0].status == "conflict"
        assert {rows[0].value_text, rows[0].conflicts[0].value_text} == {"168 cm", "172 cm"}


# --- queries --------------------------------------------------------------------------------------------


def _fact(s, a, kind, text, source="wikipedia", status="confirmed", conf=0.9, value=None):
    f = m.Fact(
        athlete_id=a.id,
        kind=kind,
        value=value or {"text": text},
        value_text=text,
        source=source,
        source_url=f"https://example.org/{source}/{a.id}",
        quote=None if source == "wikidata" else f"Zitat {text}",
        confidence=conf,
        status=status,
    )
    s.add(f)
    s.flush()
    return f


def test_facts_for_prefers_confirmed_then_wikidata(seeded):
    with Session(seeded) as s:
        a, b, c = _person(s, "Lara Stalder"), _person(s, "Alina Müller"), _person(s, "Test Person")
        _fact(s, a, "birth_place", "Lucerne", source="wikipedia", conf=0.99)
        _fact(s, a, "birth_place", "Luzern", source="wikidata", conf=1.0)
        _fact(s, b, "birth_place", "Winterthur", status="unconfirmed", conf=0.3)
        _fact(s, b, "birth_place", "Lengnau", source="news", conf=0.7)
        _fact(s, c, "birth_place", "Bern", status="unconfirmed", conf=0.3)
        rows = queries.facts_for(s, [c.id, a.id, b.id], "birth_place")
        assert [(r.athlete_name, r.value_text) for r in rows] == [
            ("Lara Stalder", "Luzern"),
            ("Alina Müller", "Lengnau"),
        ]
        assert rows[0].source == "wikidata" and rows[1].quote == "Zitat Lengnau" and rows[1].source_url
        with_unconfirmed = queries.facts_for(s, [c.id], "birth_place", include_unconfirmed=True)
        assert with_unconfirmed[0].status == "unconfirmed"
        assert queries.facts_for(s, [], "birth_place") == []
        _fact(s, a, "injury", "Knieverletzung", source="news")
        assert queries.facts_for(s, [a.id], "injury")[0].vorsicht


def _stint(s, a, team, frm, to, team_id=None, source="wikipedia", status="confirmed"):
    st = m.CareerStint(
        athlete_id=a.id,
        team_id=team_id,
        team_name=team,
        season_from=frm,
        season_to=to,
        source=source,
        source_url=f"https://example.org/{a.id}/{team}",
        quote=None if source == "wikidata" else f"{team} {frm}",
        confidence=1.0,
        status=status,
    )
    s.add(st)
    s.flush()
    return st


def test_played_together_and_stints(seeded):
    with Session(seeded) as s:
        zsc = _team_id(s, "zsc-frauen")
        a, b, c, d = (
            _person(s, n, "SUI") for n in ("Lara Stalder", "Alina Müller", "Anna Svensson", "Emma Nilsson")
        )
        _stint(s, a, "ZSC Lions Frauen", 2015, 2018, team_id=zsc)
        _stint(s, a, "Linköping HC", 2017, 2018)
        _stint(s, a, "HC Lugano", None, None)  # no seasons: never "together"
        _stint(s, b, "ZSC Lions", 2018, 2020, team_id=zsc)
        _stint(s, c, "Linköping HC Dam", 2018, None)  # open end = until now
        _stint(s, c, "HC Lugano", None, None)
        _stint(s, d, "ZSC Lions Frauen", 2019, 2021, team_id=zsc)  # after Lara left
        _stint(s, d, "Linköping HC", 2010, 2012, status="unconfirmed")

        pairs = queries.played_together(s, [a.id], [b.id, c.id, d.id])
        got = {(p.b.athlete_name, p.team_name, p.season_from, p.season_to) for p in pairs}
        assert got == {
            ("Alina Müller", "ZSC Lions Frauen", 2018, 2018),
            ("Anna Svensson", "Linköping HC", 2018, 2018),
        }
        for p in pairs:
            assert p.a.source_url and p.b.source_url and p.a.quote and p.b.quote
        # both sides open: overlap without end
        _stint(s, a, "Brynäs IF", 2019, None)
        _stint(s, d, "Brynäs IF Dam", 2022, None)
        open_pair = [p for p in queries.played_together(s, [d.id], [a.id]) if "Bryn" in p.team_name]
        assert open_pair[0].season_from == 2022 and open_pair[0].season_to is None

        rows = queries.stints_for(s, [a.id])
        assert [r.team_name for r in rows][:2] == ["ZSC Lions Frauen", "Linköping HC"]
        assert rows[-1].team_name == "HC Lugano" and rows[-1].seasons == "Saison unbekannt"
        assert rows[0].seasons == "2015/16–2018/19"
        assert queries.stints_for(s, [d.id])[-1].team_name == "Brynäs IF Dam"  # unconfirmed one hidden
        assert len(queries.stints_for(s, [d.id], include_unconfirmed=True)) == 3


def test_backfill_swiss_first_limit_and_quota_stop(seeded):
    from klugscheisser.facts.backfill import backfill_documents, backfill_wikidata
    from klugscheisser.facts.extract import DailyQuotaExceeded

    with Session(seeded) as s:
        lara = _with_qids(s)["Lara Stalder"]
        swede = _person(s, "Anna Svensson", "SWE")
        _item(s, swede, "https://sv.wikipedia.org/wiki/Anna_Svensson", "Anna Svensson är en svensk spelare.")
        _item(s, lara, "https://de.wikipedia.org/wiki/Lara_Stalder", WIKI_TEXT)
        s.commit()
        lara_id = lara.id

    class FakeWiki:
        def sparql(self, query):
            return [{k: v["value"] for k, v in r.items()} for r in _rows()]

    lines: list[str] = []
    w = backfill_wikidata(["SUI"], echo=lines.append, wiki=FakeWiki())
    n = w["persons"]  # v8: the seeded rosters bring QIDs for most Swiss players
    assert n >= 5 and w["facts"] > 10 and lines[0].startswith(f"Wikidata {n}/{n}")

    calls: list[str] = []
    r = backfill_documents(SETTINGS, None, limit=1, echo=lines.append, llm=_fake_llm(calls), support=_support)
    assert r["persons"] == 1 and len(calls) == 1 and "Lara Stalder" in calls[0]  # Swiss first
    with Session(seeded) as s:
        n_stints = select(func.count(m.CareerStint.id)).where(m.CareerStint.source == "wikipedia")
        assert s.scalar(n_stints) == 2
        assert s.scalar(select(func.count(m.Fact.id)).where(m.Fact.athlete_id == lara_id)) > 5

    def quota(system, prompt, schema):
        raise DailyQuotaExceeded("429 GenerateRequestsPerDay")

    r2 = backfill_documents(SETTINGS, None, echo=lines.append, llm=quota, support=_support)
    assert r2["stopped"] == "quota" and r2["persons"] == 0
    assert any("daily quota" in line for line in lines)


# --- live (one person, paid: Gemini Flash-Lite + Jev) ---------------------------------------------------


@pytest.mark.network
def test_live_extraction_one_person(seeded):
    """Validates the prompt on a real Wikipedia page. Keys come from the main checkout's .env (never printed)."""
    from dotenv import dotenv_values

    from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector

    env_file = os.environ.get("FACTS_ENV_FILE", ".env")
    env = dotenv_values(env_file)
    if not env.get("GEMINI_API_KEY") or not env.get("TYPESAFE_API_KEY"):
        pytest.skip("needs GEMINI_API_KEY and TYPESAFE_API_KEY")
    settings = Settings(
        _env_file=None,
        auto_refresh=False,
        gemini_api_key=env["GEMINI_API_KEY"],
        typesafe_api_key=env["TYPESAFE_API_KEY"],
    )
    with Session(seeded) as s:
        lara = _with_qids(s)["Lara Stalder"]
        import_wikidata_facts(s, [lara])
        page = WikipediaCollector().document("de", "Lara Stalder")
        assert page is not None
        _item(s, lara, page.url, page.text)
        r = extract_facts(s, settings, lara)
        print("\nLIVE", {k: v for k, v in r.items()})
        for f in s.scalars(select(m.Fact).where(m.Fact.athlete_id == lara.id, m.Fact.source != "wikidata")):
            print("FACT", f.kind, f.status, round(f.confidence, 2), f.value_text, "|", f.quote)
        for st in s.scalars(
            select(m.CareerStint).where(
                m.CareerStint.athlete_id == lara.id, m.CareerStint.source != "wikidata"
            )
        ):
            print("STINT", st.team_name, st.season_from, st.season_to, st.role, st.status, "|", st.quote)
        assert r["facts"] + r["stints"] > 0 and r["cost_usd"] < 0.01
        s.commit()
