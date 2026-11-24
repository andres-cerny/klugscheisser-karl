import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from klugscheisser.answer.engine import GeminiAnswerEngine, norm_quote, quote_in
from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.config import Settings
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/instagram")
SETTINGS = Settings(_env_file=None, auto_refresh=False, answer_support_threshold=0.6)


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        testa = repo.upsert_athlete(
            s, repo.get_sport(s, "icehockey_women"), "Testa Beispiel", nationality="SUI"
        )
        add_handle_candidate(s, testa, "instagram", "testa.beispiel", {"kind": "wikidata"})
        ingest_instagram(build_context(s, SETTINGS), FixtureInstagramSource(FIX), [testa])
        s.commit()
        return db, testa.id


def key_with(prompt: str, needle: str) -> str:
    for block in prompt.split("\n\n["):
        if needle in block:
            return re.search(r"(D\d+)\]", "[" + block).group(1)
    raise AssertionError(f"{needle} not in prompt")


def fake_model(system, prompt, schema):
    injury = key_with(prompt, "Kreuzband")
    bio = key_with(prompt, "EVZ Women")
    sponsor = key_with(prompt, "Rabatt")
    return {
        "statements": [
            {
                "aussage": "Sie hat sich das Kreuzband im linken Knie gerissen.",
                "dok": injury,
                "zitat": "Kreuzband im linken Knie gerissen",
                "kategorie": "gesundheit",
            },
            {
                "aussage": "Sie spielt beim EVZ.",
                "dok": bio,
                "zitat": "EVZ Women's Team",
                "kategorie": "anderes",
            },
            {
                "aussage": "Sie studiert Medizin.",
                "dok": bio,
                "zitat": "studiert Medizin in Zürich",
                "kategorie": "anderes",
            },  # invented quote
            {
                "aussage": "Sie hat 3 Tore geschossen.",
                "dok": "D99",
                "zitat": "drei Tore",
                "kategorie": "anderes",
            },
            {
                "aussage": "Sie ist mit dem Sponsor liiert.",
                "dok": sponsor,
                "zitat": "zusammen mit @testa.beispiel",
                "kategorie": "beziehung",
            },  # relationship from a third-party post
            {
                "aussage": "Sie ist die beste Stürmerin der Liga.",
                "dok": bio,
                "zitat": "Nati",
                "kategorie": "anderes",
            },  # too short to be a quote
        ],
        "luecken": ["Studium nicht erwähnt"],
    }, {"input": 4000, "output": 300}


def support(pairs):
    return [0.2 if "Sie spielt beim EVZ" in a else 0.9 for _, a in pairs]


def test_quote_normalisation():
    assert quote_in("«Kreuzband im linken Knie gerissen»", "Leider … Kreuzband im  linken Knie gerissen. Die")
    assert not quote_in("Kreuzband im rechten Knie", "Kreuzband im linken Knie gerissen")
    assert norm_quote("„Okay super weitermachen!“") == "okay super weitermachen"


def test_engine_keeps_only_verified_statements(world):
    _db, testa_id = world
    engine = GeminiAnswerEngine(SETTINGS, llm=fake_model, support_checker=support)
    result = engine.answer("Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False)
    texts = [st.aussage for st in result.statements]
    # 07.10.: a relationship statement from a third-party post is kept (marked Vorsicht, Fabi decides)
    assert texts == ["Sie hat sich das Kreuzband im linken Knie gerissen.", "Sie ist mit dem Sponsor liiert."]
    assert result.statements[1].vorsicht
    injury = result.statements[0]
    assert injury.vorsicht and injury.url == "https://www.instagram.com/p/SYN1/"
    assert injury.quelle_link.startswith("[Instagram @testa.beispiel") and injury.zeile_prefix.startswith(
        "📸"
    )
    assert [st.aussage for st in result.unbestaetigt] == ["Sie spielt beim EVZ."]  # Jev: not supported
    assert result.unbestaetigt[0].url == "https://www.instagram.com/testa.beispiel/"  # bio link from DB
    assert result.verworfen == 3  # invented quote, invented doc, too short
    assert result.luecken == ["Studium nicht erwähnt"]
    assert result.datenstand.startswith("Instagram:") and result.kosten_usd > 0


def test_engine_finds_person_from_question_text(world):
    _db, _ = world
    engine = GeminiAnswerEngine(SETTINGS, llm=fake_model, support_checker=lambda p: [0.9] * len(p))
    result = engine.answer("Hat sich Testa Beispiel verletzt?", web=False)
    assert result.personen == ["Testa Beispiel"]
    assert any("Kreuzband" in st.zitat for st in result.statements)


def test_engine_honest_when_nothing_found(world):
    engine = GeminiAnswerEngine(
        SETTINGS,
        llm=lambda *a: ({"statements": [], "luecken": ["nichts gefunden"]}, {}),
        support_checker=lambda p: [],
    )
    result = engine.answer("Wer spielt Schach?", web=False)
    assert result.statements == [] and result.luecken


def test_web_namesake_pages_are_never_quoted(world):
    from klugscheisser.interfaces import WebPage, WebResult

    _db, testa_id = world

    class Web:
        def search(self, query, limit=5):
            return [
                WebResult("https://example.org/historikerin", "Testa Beispiel, Historikerin", ""),
                WebResult("https://example.org/spielerin", "Testa Beispiel, Eishockey", ""),
            ]

        def fetch(self, url):
            text = (
                "Testa Beispiel studierte Geschichte in Basel."
                if "historikerin" in url
                else "Testa Beispiel studiert Sportwissenschaft in Bern und spielt Eishockey."
            )
            return WebPage(url=url, title=url.rsplit("/", 1)[-1], text=text, allowed=True)

    def model(system, prompt, schema):
        if "Sportwissenschaft" not in prompt:
            return {"statements": [], "luecken": ["Studium unbekannt"]}, {}
        key = key_with(prompt, "Sportwissenschaft")
        return {
            "statements": [
                {
                    "aussage": "Sie studiert Sportwissenschaft in Bern.",
                    "dok": key,
                    "zitat": "studiert Sportwissenschaft in Bern",
                    "kategorie": "anderes",
                }
            ],
            "luecken": [],
        }, {}

    engine = GeminiAnswerEngine(
        SETTINGS.model_copy(update={"answer_web_search": True}),  # web search is off by default since v5
        llm=model,
        support_checker=lambda p: [0.95] * len(p),
        web_search=Web(),
        identity_checker=lambda athlete, title, text: "Eishockey" in text,
    )
    result = engine.answer("Was studiert Testa Beispiel?", person_ids=[testa_id], web=True)
    assert [st.url for st in result.statements] == ["https://example.org/spielerin"]
    assert result.statements[0].zeile_prefix.startswith("🌐 (Web)")
