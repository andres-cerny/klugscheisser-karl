"""v12 Part A/E/G: written answers in the web chat (prosa). Gemini writes, every sentence and table row is checked
(document exists, verbatim quote, sensitive rule, Jev support), confidence in words, follow-ups, cache, rendering.
Fake Gemini/Jev only."""

import re
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.answer import cache, cross
from klugscheisser.answer import engine as eng
from klugscheisser.answer.engine import Doc, GeminiAnswerEngine
from klugscheisser.chat import service as chat_svc
from klugscheisser.collectors.instagram.sources import FixtureInstagramSource
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.handles.service import add_handle_candidate
from klugscheisser.ingest.instagram import ingest_instagram
from klugscheisser.ingest.runner import build_context
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence, Statement
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import views

FIX = Path("tests/fixtures/instagram")
SETTINGS = Settings(_env_file=None, auto_refresh=False, answer_support_threshold_prosa=0.6)


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


def beleg(dok: str, zitat: str, kategorie: str = "anderes") -> dict:
    return {"dok": dok, "zitat": zitat, "kategorie": kategorie}


def prosa_model(seen: dict | None = None):
    def model(system, prompt, schema):
        if seen is not None:
            seen.update(system=system, prompt=prompt, schema=schema)
        injury = key_with(prompt, "Kreuzband")
        game = key_with(prompt, "Saisonstart")
        bio = key_with(prompt, "EVZ Women")
        sponsor = key_with(prompt, "Rabatt")
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Sie hat sich das Kreuzband im linken Knie gerissen.",
                            "belege": [beleg(injury, "Kreuzband im linken Knie gerissen", "gesundheit")],
                        },
                        {
                            "satz": f"Sie spielt beim EVZ und gewann zum Saisonstart 3:1 [{bio}, {game}].",
                            "belege": [
                                beleg(bio, "EVZ Women's Team"),
                                beleg(game, "3:1 Sieg zum Saisonstart"),
                            ],
                        },
                        {
                            "satz": "Sie studiert Medizin.",
                            "belege": [beleg(bio, "studiert Medizin in Zürich")],
                        },
                        {"satz": "Sie hat drei Tore geschossen.", "belege": [beleg("D99", "drei Tore")]},
                        {
                            "satz": "Sie ist mit dem Sponsor liiert.",
                            "belege": [
                                beleg(sponsor, "entwickelt zusammen mit @testa.beispiel", "beziehung")
                            ],
                        },
                        {
                            "satz": "Sie ist Nati-Spielerin.",
                            "belege": [beleg(bio, "Eishockey 🏒 EVZ Women's Team")],
                        },
                    ],
                },
                {
                    "typ": "tabelle",
                    "spalten": ["Spiel", "Resultat"],
                    "zeilen": [
                        {
                            "zellen": ["Saisonstart", "3:1 Sieg"],
                            "belege": [beleg(game, "3:1 Sieg zum Saisonstart")],
                        },
                        {
                            "zellen": ["Finale", "5:0"],
                            "belege": [beleg(game, "5:0 Sieg im Finale gegen Lugano")],
                        },
                        {
                            "zellen": ["Training", "verletzt"],
                            "belege": [beleg(injury, "im Training erwischt")],
                        },
                    ],
                },
            ],
            "luecken": [f"Ob sie studiert, sagt {bio} nicht."],
            "rueckfrage": "",
            "naechste_fragen": [
                "Wann kommt sie zurück?",
                "Wer ist ihr Trainer?",
                "Wie lief die Saison?",
                "4?",
            ],
        }, {"input": 4000, "output": 300}

    return model


def support(pairs):
    low = ("Sie ist Nati-Spielerin.", "Spiel: Training")
    return [0.2 if any(x in a for x in low) else 0.9 for _, a in pairs]


def prosa_engine(model=None, sup=support):
    return GeminiAnswerEngine(SETTINGS, llm=model or prosa_model(), support_checker=sup)


def all_text(res: AnswerResult) -> str:
    parts = [x.text for b in res.antwort for x in b.saetze]
    parts += [c for b in res.antwort for r in b.zeilen for c in r["zellen"]] + [
        x.text for x in res.nicht_sicher
    ]
    return " ".join(parts + res.luecken + res.naechste_fragen + [res.rueckfrage or ""])


# --- writer + checks ----------------------------------------------------------------------------------------


def test_prosa_schema_checks_and_numbered_sources(world):
    _db, testa_id = world
    seen: dict = {}
    res = prosa_engine(prosa_model(seen)).answer(
        "Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False, style="prosa"
    )
    assert seen["system"].startswith(eng.SYSTEM_PROSA[:60]) and seen["schema"] is eng.SCHEMA_PROSA
    assert "deutsch" in seen["system"] and "rueckfrage" in seen["system"]
    assert res.stil == "prosa" and [b.typ for b in res.antwort] == ["text", "tabelle"]
    text, table = res.antwort
    # invented quote, unknown document, invented table row: dropped; the relationship sentence from a
    # third-party post is kept since 07.10. (marked Vorsicht, attributed)
    assert res.verworfen == 3
    shown = [x for x in text.saetze if "Sponsor" not in x.text]
    sponsor = [x for x in text.saetze if "Sponsor" in x.text]
    assert [x.text for x in shown] == [
        "Laut Instagram @testa.beispiel: Sie hat sich das Kreuzband im linken Knie gerissen.",
        "Sie spielt beim EVZ und gewann zum Saisonstart 3:1.",  # "[D3, D4]" removed
    ]
    assert [x.ton for x in shown] == ["vorsicht", "sicher"]
    assert sponsor and sponsor[0].ton == "vorsicht" and "berichtet:" in sponsor[0].text
    assert text.saetze[0].quellen == [1] and text.saetze[1].quellen == [2, 3]
    # table: the failing row is removed, the kept row reuses source 3 (same document + quote)
    assert table.spalten == ["Spiel", "Resultat"]
    assert [r["zellen"] for r in table.zeilen] == [["Saisonstart", "3:1 Sieg"]]
    zitate = [q.zitat for q in res.quellen]
    assert table.zeilen[0]["satz"].quellen == [zitate.index("3:1 Sieg zum Saisonstart") + 1]
    # low Jev support: not shown as fact, quotes still numbered (after the shown ones)
    assert [(x.text, x.ton, x.quellen) for x in res.nicht_sicher] == [
        ("Sie ist Nati-Spielerin.", "unsicher", [zitate.index("Eishockey 🏒 EVZ Women's Team") + 1]),
        ("Training | verletzt", "unsicher", [zitate.index("im Training erwischt") + 1]),
    ]
    assert zitate[0] == "Kreuzband im linken Knie gerissen" and zitate[-2:] == [
        "Eishockey 🏒 EVZ Women's Team",
        "im Training erwischt",
    ]
    assert {"EVZ Women's Team", "3:1 Sieg zum Saisonstart"} <= set(zitate) and len(zitate) == 6
    assert res.quellen[0].vorsicht and res.quellen[0].url == "https://www.instagram.com/p/SYN1/"
    assert res.statements == res.quellen[:4] and res.unbestaetigt == res.quellen[4:]
    assert res.naechste_fragen == ["Wann kommt sie zurück?", "Wer ist ihr Trainer?", "Wie lief die Saison?"]
    assert res.luecken[0].startswith("Ob sie studiert, sagt Instagram-Bio @testa.beispiel")
    assert not re.search(r"\bD\d+\b", all_text(res))  # internal ids never reach the user
    assert res.kosten_usd > 0 and res.rueckfrage is None


def test_prosa_row_support_text_and_two_document_context():
    seen = {}

    def sup(pairs):
        seen["pairs"] = pairs
        return [0.9] * len(pairs)

    a = Doc(
        key="",
        text="Jessica Lutz ist am 24.05.1989 geboren.",
        meta="[Wikidata] · über: Jessica Lutz",
        url="https://w",
        quelle="[Wikidata Q1]",
        zeile_prefix="",
    )
    b = Doc(
        key="",
        text="Lutz spielt beim ZSC Lions.",
        meta="[SRF, 03.10.] · über: Jessica Lutz",
        url="https://s",
        quelle="[SRF, 03.10.]",
        zeile_prefix="",
    )

    def model(system, prompt, schema):
        return {
            "antwort": [
                {
                    "typ": "tabelle",
                    "spalten": ["Spielerin", "Geboren", "Club"],
                    "zeilen": [
                        {
                            "zellen": ["Jessica Lutz", "24.05.1989", "ZSC Lions"],
                            "belege": [
                                beleg("D1", "Jessica Lutz ist am 24.05.1989 geboren"),
                                beleg("D2", "Lutz spielt beim ZSC Lions"),
                            ],
                        }
                    ],
                }
            ],
            "luecken": [],
        }, {}

    e = GeminiAnswerEngine(SETTINGS, llm=model, support_checker=sup)
    out = e._write_prosa("Kader mit Club?", [a, b], ["Jessica Lutz"])
    ((z, aussage),) = seen["pairs"]
    assert aussage == "Spielerin: Jessica Lutz | Geboren: 24.05.1989 | Club: ZSC Lions"
    assert z == "Jessica Lutz ist am 24.05.1989 geboren / Lutz spielt beim ZSC Lions"
    row = out.antwort[0].zeilen[0]["satz"]
    assert (
        row.quellen == [1, 2] and row.ton == "sicher" and row.text == "Jessica Lutz | 24.05.1989 | ZSC Lions"
    )


def test_support_scores_use_both_excerpts_for_two_documents(monkeypatch):
    seen = {}

    class FakeJev:
        def __init__(self, settings):
            self.usage = {}

        def ask(self, state, questions):
            seen["state"] = state
            return {k: {"noul": True, "probabilities": {"true": 0.8, "false": 0.2}} for k in questions}

    monkeypatch.setattr("klugscheisser.classify.jev.JevClient", FakeJev)
    a = Doc(
        key="D1", text="AAA Jessica Lutz geboren 1989 AAA", meta="[A]", url="u", quelle="[A]", zeile_prefix=""
    )
    b = Doc(key="D2", text="BBB Lutz spielt beim ZSC BBB", meta="[B]", url="v", quelle="[B]", zeile_prefix="")
    claim = eng._Claim(
        0,
        "Lutz (1989) spielt beim ZSC.",
        [(a, "Jessica Lutz geboren 1989", "anderes"), (b, "Lutz spielt beim ZSC", "anderes")],
    )
    e = GeminiAnswerEngine(Settings(_env_file=None, typesafe_api_key="k"))
    assert e._support_scores([claim.support_item()]) == [pytest.approx(0.8, abs=0.05)]
    assert "AAA Jessica" in seen["state"] and "BBB Lutz" in seen["state"] and "[A] + [B]" in seen["state"]


def test_sensitive_from_every_source_marked_and_attributed():
    """Ondřej 07.10.: relationships/health from every source, so Fabi sees who says it — always Vorsicht and
    attributed: her own post "Laut Instagram @x: …", a third party "Instagram @fan berichtet: …"."""
    own = Doc(
        key="",
        text="Ich bin seit einem Jahr mit Max verlobt.",
        meta="[Instagram @x]",
        url="https://o",
        quelle="[Instagram @x, 01.10.]",
        zeile_prefix="",
        self_authored=True,
    )
    other = Doc(
        key="",
        text="Gerüchte: Sie ist mit Max verlobt, sagt ein Fan.",
        meta="[Fan]",
        url="https://f",
        quelle="[Instagram @fan, 01.10.]",
        zeile_prefix="",
    )

    def model(system, prompt, schema):
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Sie ist verlobt.",
                            "belege": [beleg("D1", "seit einem Jahr mit Max verlobt", "beziehung")],
                        },
                        {
                            "satz": "Sie ist mit Max verlobt.",
                            "belege": [beleg("D2", "Sie ist mit Max verlobt", "beziehung")],
                        },
                    ],
                }
            ],
            "luecken": [],
        }, {}

    out = GeminiAnswerEngine(SETTINGS, llm=model, support_checker=lambda p: [0.9] * len(p))._write_prosa(
        "Ist sie verlobt?", [own, other], ["X"]
    )
    assert out.dropped == 0
    s1, s2 = out.antwort[0].saetze
    assert s1.ton == "vorsicht" and s1.text == "Laut Instagram @x: Sie ist verlobt."
    assert s2.ton == "vorsicht" and s2.text == "Instagram @fan berichtet: Sie ist mit Max verlobt."
    assert out.quellen[0].vorsicht and out.quellen[1].vorsicht


# --- Part E: confidence wording ---------------------------------------------------------------------------


def _doc(quelle, url, **kw):
    return Doc(key="", text="x", meta=quelle, url=url, quelle=quelle, zeile_prefix="", **kw)


def test_tone_from_real_checks_and_downgrade_wording():
    srf, blick = _doc("[SRF, 03.10.]", "https://srf"), _doc("[Blick, 03.10.]", "https://blick", vorsicht=True)
    wiki, calc = _doc("[Wikidata Q42]", "https://wd"), _doc("[Kader nach Alter]", "", computed=True)
    assert eng._tone([(srf, "q", "anderes")]) == "eine_quelle"
    assert eng._tone([(srf, "q", "anderes"), (wiki, "q", "anderes")]) == "sicher"
    assert eng._tone([(srf, "q", "anderes"), (srf, "q2", "anderes")]) == "eine_quelle"  # same source twice
    assert eng._tone([(srf, "q", "anderes"), (blick, "q", "anderes")]) == "vorsicht"
    assert eng._tone([(calc, "q", "anderes")]) == "sicher"
    assert eng._worded("Sie traf zweimal.", "eine_quelle", srf) == "Laut SRF: Sie traf zweimal."
    assert eng._worded("Sie ist 37.", "eine_quelle", wiki) == "Laut Wikidata: Sie ist 37."
    assert (
        eng._worded("Sie wechselt nach Zug.", "vorsicht", blick) == "Blick berichtet: Sie wechselt nach Zug."
    )
    assert eng._worded("Laut SRF traf sie zweimal.", "eine_quelle", srf) == "Laut SRF traf sie zweimal."
    assert eng._worded("Blick berichtet, dass sie wechselt.", "vorsicht", blick).startswith(
        "Blick berichtet, dass"
    )
    assert eng._worded("Sie traf zweimal.", "sicher", srf) == "Sie traf zweimal."
    assert eng.source_name(_doc("[Instagram-Bio @x, Stand 03.10.]", "u")) == "Instagram-Bio @x"


def test_nothing_found_says_so_with_closest_sources(world):
    _db, testa_id = world
    e = prosa_engine(lambda *a: ({"antwort": [], "luecken": ["Kein Wort zum Studium."]}, {}))
    res = e.answer("Was studiert Testa Beispiel?", person_ids=[testa_id], web=False, style="prosa")
    assert res.antwort == [] and res.statements == [] and res.naechste_quellen
    html = views.answer_html(res)
    assert "Dazu habe ich nichts gefunden." in html and "Am nächsten dran" in html
    assert chat_svc.answer_summary(res) == "Dazu habe ich nichts gefunden."


def test_sentence_about_missing_information_is_a_gap_not_a_fact(world):
    """ "Die Dokumente enthalten keine …" is no claim (Jev would rightly reject it): it goes to luecken."""
    _db, testa_id = world
    out = {
        "antwort": [
            {
                "typ": "text",
                "saetze": [
                    {"satz": "Die Dokumente enthalten keine Aussagen der Trainerin.", "belege": []},
                ],
            }
        ],
        "luecken": [],
    }
    res = prosa_engine(lambda *a: (out, {})).answer(
        "Was sagte die Trainerin?", person_ids=[testa_id], web=False, style="prosa"
    )
    assert res.antwort == [] and res.nicht_sicher == [] and res.verworfen == 0
    assert res.luecken == ["Die Dokumente enthalten keine Aussagen der Trainerin."]


# --- Part B/D consumers: ask-back, stats, cross documents ------------------------------------------------------


def test_ask_back_short_circuits_without_model(world, monkeypatch):
    _db, testa_id = world
    monkeypatch.setattr(
        cross,
        "ask_back",
        lambda s, q, a, h=None: cross.AskBack("Welches Spiel meinst du?", ["Spiel A?", "Spiel B?"]),
    )

    def no_model(*a):
        raise AssertionError("no model call for an ask-back")

    res = prosa_engine(no_model).answer("Wer mit wem?", person_ids=[testa_id], style="prosa")
    assert res.stil == "prosa" and res.rueckfrage == "Welches Spiel meinst du?"
    assert (
        res.rueckfrage_vorschlaege == ["Spiel A?", "Spiel B?"] and res.antwort == [] and res.kosten_usd == 0
    )
    html = views.answer_html(res, uid="m1")
    assert "class='rueckfrage'" in html and "Dazu habe ich nichts gefunden" not in html
    assert html.count("onclick='askNext(this)'") == 2 and "data-q='Spiel A?'" in html
    # not cached: the next time the same question is asked again
    key = cache.key("Wer mit wem?", [testa_id], None, None, None, True, None, "prosa", eng.PROSA_VERSION)
    assert cache.get(key) is None


def test_relation_answer_is_built_from_data_without_model(world, monkeypatch):
    """ "Wer kannte sich als Teamkollegin?": the count, the complete pair list as a table and the rest —
    straight from the computed document, no model call, one numbered source."""
    _db, testa_id = world
    computed = Doc(
        key="D1",
        text="Ergebnis: 2 Paare …",
        meta="über: Testa Beispiel",
        url="https://ep/1",
        quelle="[Berechnet aus gespeicherten Daten]",
        zeile_prefix="",
        computed=True,
        extra={
            "direct": {
                "summary": "Ergebnis: 2 Paare",
                "rows": [["A und B", "EV Zug (2026/27)", "x"], ["C und D", "Frölunda HC (2025/26)", "y"]],
                "unknown": "Die übrigen 3 haben nie zusammengespielt",
            }
        },
    )
    monkeypatch.setattr(
        cross,
        "prosa_inputs",
        lambda s, q, a, h=None: cross.ProsaInputs(docs=[computed], athlete_ids=[testa_id]),
    )
    res = prosa_engine(lambda *a: pytest.fail("no model call")).answer(
        "Welche Spielerinnen der Schweiz und Schwedens kannten sich als Teamkolleginnen?", style="prosa"
    )
    assert res.rueckfrage is None, res.rueckfrage
    first, table, rest = res.antwort
    assert first.saetze[0].text == "Ergebnis: 2 Paare." and table.typ == "tabelle"
    assert [z["zellen"] for z in table.zeilen] == [
        ["A und B", "EV Zug (2026/27)"],
        ["C und D", "Frölunda HC (2025/26)"],
    ]
    assert rest.saetze[0].text.startswith("Die übrigen 3") and len(res.quellen) == 1 and res.kosten_usd == 0
    assert "<table" in views.answer_html(res)


def test_computed_only_sentences_have_a_lower_jev_bar(world, monkeypatch):
    """0.5 is below the normal bar (0.6) but above the bar for sentences resting only on computed documents."""
    _db, testa_id = world
    computed = Doc(
        key="",
        text="Ergebnis: 26 Paare; beteiligt sind 25 von 46 Spielerinnen.",
        meta="über: Testa Beispiel",
        url="https://www.eliteprospects.com/player/1",
        quelle="[Berechnet aus gespeicherten Daten]",
        zeile_prefix="",
        computed=True,
    )
    monkeypatch.setattr(
        cross,
        "prosa_inputs",
        lambda s, q, a, h=None: cross.ProsaInputs(docs=[computed], athlete_ids=[testa_id]),
    )
    out = {
        "antwort": [
            {
                "typ": "text",
                "saetze": [
                    {
                        "satz": "25 Spielerinnen kennen sich.",
                        "belege": [beleg("D1", "beteiligt sind 25 von 46 Spielerinnen")],
                    }
                ],
            }
        ],
        "luecken": [],
    }
    res = prosa_engine(lambda *a: (out, {}), sup=lambda pairs: [0.5 for _ in pairs]).answer(
        "Welche Spielerinnen kennen sich aus einem Team?", style="prosa"
    )
    assert [x.text for b in res.antwort for x in b.saetze] == ["25 Spielerinnen kennen sich."]
    assert res.nicht_sicher == []


def test_cross_documents_are_written_with_computed_source(world, monkeypatch):
    _db, testa_id = world
    computed = Doc(
        key="",
        text="Olympia-Kader Schweiz 2026 nach Geburtsdatum: Jessica Lutz 24.05.1989; Testa Beispiel 01.02.2001.",
        meta="[Olympia-Kader Schweiz 2026 nach Geburtsdatum, Stand 04.10.] · über: Testa Beispiel",
        url="",
        quelle="[Olympia-Kader Schweiz 2026 nach Geburtsdatum, Stand 04.10.]",
        zeile_prefix="🧮 (berechnet)",
        computed=True,
        basis=[
            {"quelle": "Wikidata Q1", "url": "https://www.wikidata.org/wiki/Q1"},
            {"quelle": "IIHF", "url": ""},
        ],
    )
    seen = {}
    monkeypatch.setattr(
        cross,
        "prosa_inputs",
        lambda s, q, a, h=None: cross.ProsaInputs(
            docs=[computed], athlete_ids=[testa_id], scope_note="Olympia-Kader"
        ),
    )
    monkeypatch.setattr(eng, "candidate_items", lambda *a, **k: pytest.fail("no document search"))

    def model(system, prompt, schema):
        seen["prompt"] = prompt
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Jessica Lutz ist die Älteste.",
                            "belege": [beleg("D1", "Jessica Lutz 24.05.1989")],
                        }
                    ],
                }
            ],
            "luecken": [],
            "naechste_fragen": ["Wer ist die Jüngste?"],
        }, {}

    res = prosa_engine(model).answer("Who from the swiss olympic team was the oldest", style="prosa")
    assert "berechnet aus gespeicherten Daten" in seen["prompt"]
    (st,) = res.antwort[0].saetze
    assert st.text == "Jessica Lutz ist die Älteste." and st.ton == "sicher" and st.quellen == [1]
    q = res.quellen[0]
    assert q.berechnet and q.url == "https://www.wikidata.org/wiki/Q1" and len(q.basis) == 2
    assert res.scope_hinweis == "Olympia-Kader" and res.person_ids == [testa_id]
    assert res.personen == ["Testa Beispiel"]
    html = views.answer_html(res, uid="m2")
    assert "Berechnet aus gespeicherten Daten" in html and "Grundlage:" in html
    assert "href='https://www.wikidata.org/wiki/Q1'" in html and "Verstanden als: Olympia-Kader" in html
    assert "data-q='Wer ist die Jüngste?'" in html


def test_cross_scope_without_documents_searches_within_game_window(world, monkeypatch):
    """A recognised game with no computed documents: the usual search runs with the game's players and its date
    window (since from the cross step, later documents dropped)."""
    from datetime import UTC, datetime

    _db, testa_id = world
    since, until = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 5, tzinfo=UTC)
    monkeypatch.setattr(
        cross,
        "prosa_inputs",
        lambda s, q, a, h=None: cross.ProsaInputs(
            docs=[], athlete_ids=[testa_id], scope_note="Spiel A–B", since=since, until=until
        ),
    )
    seen = {}

    def items(s, settings, q, athletes, since_):
        seen["ids"], seen["since"] = [a.id for a in athletes], since_
        return []

    monkeypatch.setattr(eng, "candidate_items", items)
    res = prosa_engine(lambda *a: ({"antwort": [], "luecken": ["kein Spielbericht"]}, {})).answer(
        "Wer hat im Spiel A gegen B getroffen?", style="prosa"
    )
    assert seen == {"ids": [testa_id], "since": since}
    assert res.scope_hinweis == "Spiel A–B" and res.person_ids == [testa_id]
    late = Doc(
        key="",
        text="x",
        meta="",
        url="",
        quelle="",
        zeile_prefix="",
        extra={"published_at": datetime(2026, 10, 9, tzinfo=UTC)},
    )
    assert eng._after(late, until) and not eng._after(late, datetime(2026, 10, 10, tzinfo=UTC))


def test_stats_questions_render_without_model(world, monkeypatch):
    _db, testa_id = world
    rows = [
        Statement(
            f"{i}. Spielerin {i}: {10 - i} Tore",
            f"Spielerin {i} {10 - i} G",
            None,
            "https://ep",
            "[Elite Prospects]",
            "[Elite Prospects](https://ep)",
            "📊",
            support=1.0,
        )
        for i in (1, 2)
    ]
    monkeypatch.setattr(
        cross,
        "try_stats",
        lambda s, q, a: (rows, [], [], 0, {}, "Statistik: Tore", "2 Statistikzeilen", [testa_id]),
    )
    res = prosa_engine(lambda *a: pytest.fail("no model")).answer("Wer hat die meisten Tore?", style="prosa")
    (table,) = res.antwort
    assert table.typ == "tabelle" and [r["satz"].quellen for r in table.zeilen] == [[1], [2]]
    assert res.quellen == rows and res.statements == rows and res.scope_hinweis == "Statistik: Tore"


# --- liste stays, cache, style selection ------------------------------------------------------------------


def test_liste_unchanged_and_cache_key_per_style(world):
    _db, testa_id = world
    calls = []

    def model(system, prompt, schema):
        calls.append(schema)
        if schema is eng.SCHEMA:
            injury = key_with(prompt, "Kreuzband")
            return {
                "statements": [
                    {
                        "aussage": "Sie hat sich das Kreuzband gerissen.",
                        "dok": injury,
                        "zitat": "Kreuzband im linken Knie gerissen",
                        "kategorie": "gesundheit",
                    }
                ],
                "luecken": [],
            }, {}
        return prosa_model()(system, prompt, schema)

    e = prosa_engine(model)
    liste = e.answer("Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False)
    assert (
        liste.stil == "liste"
        and liste.antwort == []
        and [st.aussage for st in liste.statements] == ["Sie hat sich das Kreuzband gerissen."]
    )
    again = e.answer("Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False)
    assert again.aus_cache and len(calls) == 1
    prosa = e.answer("Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False, style="prosa")
    assert prosa.stil == "prosa" and not prosa.aus_cache and calls[-1] is eng.SCHEMA_PROSA
    cached = e.answer("Was ist mit Testa Beispiel los?", person_ids=[testa_id], web=False, style="prosa")
    assert cached.aus_cache and len(calls) == 2
    assert cached.antwort == prosa.antwort and cached.quellen == prosa.quellen  # rebuilt from JSON
    base = ("q", [1], None, None, None, True, 3)
    assert cache.key(*base) == cache.key(*base, style="liste")  # list keys as before
    assert cache.key(*base) != cache.key(*base, style="prosa", version=eng.PROSA_VERSION)
    assert cache.key(*base, style="prosa", version="prosa-0") != cache.key(*base, style="prosa", version="x")


def test_answer_style_from_project_setting(world):
    db, _ = world
    with Session(db) as s:
        assert chat_svc.answer_style(s, Settings(_env_file=None)) == "liste"
        assert chat_svc.answer_style(s, Settings(_env_file=None, web_answer_style="prosa")) == "prosa"
        s.add(m.ProjectSetting(key="web_answer_style", value="prosa"))
        s.flush()
        assert chat_svc.answer_style(s, Settings(_env_file=None)) == "prosa"
        s.get(m.ProjectSetting, "web_answer_style").value = "liste"
        assert chat_svc.answer_style(s, Settings(_env_file=None, web_answer_style="prosa")) == "liste"
        s.get(m.ProjectSetting, "web_answer_style").value = "kaputt"
        assert chat_svc.answer_style(s, Settings(_env_file=None)) == "liste"


def prosa_result(question: str = "Wer ist die Älteste?") -> AnswerResult:
    q1 = Statement(
        "Jessica Lutz ist die Älteste.",
        "Jessica Lutz 24.05.1989",
        None,
        "https://wd/Q1",
        "[Kader nach Alter]",
        "[Kader nach Alter](https://wd/Q1)",
        "🧮",
        support=0.9,
        berechnet=True,
        basis=[{"quelle": "Wikidata Q1", "url": "https://wd/Q1"}],
    )
    q2 = Statement(
        "<b>x</b>",
        "Fan <script>alert(1)</script> sagt",
        3,
        "https://blick",
        "[Blick, 03.10.]",
        "[Blick, 03.10.](https://blick)",
        "📰 (Medien)",
        vorsicht=True,
        zitat_de="Fan sagt",
    )
    q3 = Statement("Unsicher.", "vielleicht verletzt", 4, "https://u", "[U, 01.10.]", "[U](https://u)", "")
    return AnswerResult(
        frage=question,
        datenstand="SRF: 04.10.",
        statements=[q1, q2],
        unbestaetigt=[q3],
        luecken=["Kein Wort zum Club."],
        verworfen=2,
        kosten_usd=0.0012,
        personen=["Jessica Lutz"],
        person_ids=[9],
        scope_hinweis="Olympia-Kader Schweiz",
        durchsucht="23 Personen · 40 Dokumente geprüft",
        stil="prosa",
        antwort=[
            AnswerBlock(
                "text", saetze=[AnswerSentence("Jessica Lutz ist die <Älteste>.", [1], 0.9, "sicher")]
            ),
            AnswerBlock(
                "tabelle",
                spalten=["Spielerin", "Club <i>"],
                zeilen=[
                    {
                        "zellen": ["Lutz", "<script>x</script>"],
                        "satz": AnswerSentence("Lutz | x", [1, 2], 0.8, "vorsicht"),
                    }
                ],
            ),
        ],
        quellen=[q1, q2, q3],
        nicht_sicher=[AnswerSentence("Sie ist verletzt.", [3], 0.3, "unsicher")],
        naechste_fragen=["Und die Jüngste?"],
    )


def test_prosa_rendering_markers_table_sources_and_escaping():
    html = views.answer_html(prosa_result(), uid="m7")
    assert html.startswith("<div class='answer prosa'>")
    assert "Datenstand: SRF: 04.10. · Verstanden als: Olympia-Kader Schweiz" in html
    assert "href='#m7-q1'" in html and "id='m7-q1'" in html and "id='m7-q2'" in html
    assert "<table>" in html and "class='atab'" in html and "<th scope='col'>Club &lt;i&gt;</th>" in html
    assert "<script" not in html and "&lt;script&gt;" in html and "&lt;Älteste&gt;" in html
    quellen = html.split("<h2 class='qh'>Quellen</h2>")[1].split("</ol>")[0]
    assert "[1]" in quellen and "[2]" in quellen and "id='m7-q3'" not in quellen  # unsure source: collapsed
    assert "Berechnet aus gespeicherten Daten" in quellen and "Vorsicht" in quellen
    assert "Automatisch übersetzt: «Fan sagt»" in quellen
    assert (
        "<details class='amore'><summary>Nicht sicher belegt (1)</summary>" in html and "id='m7-q3'" in html
    )
    assert "Was die Quellen nicht sagen (1)" in html and "Kein Wort zum Club." in html
    assert "<summary>Durchsucht</summary>" in html and "Kosten $0.001" in html and "2 Sätze verworfen" in html
    assert "class='nextq'" in html and "data-q='Und die Jüngste?'" in html
    assert "Vorsicht</h2>" not in html and "Unbestätigt" not in html  # no liste headings in prosa
    # the liste view stays as it was
    liste = views.answer_html(AnswerResult("q", "heute", prosa_result().statements))
    assert "<ul class='lines'>" in liste and "class='answer prosa'" not in liste


# --- Part G: follow-ups -------------------------------------------------------------------------------------


def test_history_carries_previous_prosa_sentences_into_writer(world):
    db, testa_id = world
    with Session(db) as s:
        chat = chat_svc.create_chat(s, "Wer ist die Älteste?")
        chat_svc.add_question(s, chat, "Wer ist die Älteste?")
        chat_svc.add_answer(s, chat, prosa_result())
        s.commit()
        s.refresh(chat)
        past = chat_svc.history(chat, 4)
        assert past[-1]["saetze"] == [
            "Jessica Lutz ist die <Älteste>.",
            "Spielerin: Lutz, Club <i>: <script>x</script>",
        ]
        assert past[-1]["person_ids"] == [9] and past[-1]["antwort"].startswith("Jessica Lutz ist die")
        back = chat_svc.result_from_json(chat.messages[1].answer)
        assert back.antwort[1].zeilen[0]["satz"].quellen == [1, 2] and back.quellen[0].berechnet
    block = eng.history_block(past, prosa=True)
    assert "    - Jessica Lutz ist die <Älteste>." in block and "Personen: Jessica Lutz" in block
    assert "Kurzfassung" not in block and "Kurzfassung" in eng.history_block(past)  # liste unchanged
    seen: dict = {}
    prosa_engine(prosa_model(seen)).answer(
        "und Testa Beispiel?", person_ids=[testa_id], history=past, style="prosa"
    )
    assert "Bisheriges Gespräch (nur Kontext, keine Quelle):" in seen["prompt"]
    assert (
        "    - Jessica Lutz ist die <Älteste>." in seen["prompt"]
        and "Frage: und Testa Beispiel?" in seen["prompt"]
    )


# --- web chat + MCP wiring ----------------------------------------------------------------------------------


class RecordingEngine:
    def __init__(self):
        self.kwargs = []

    def answer(self, question, **kw):
        self.kwargs.append(kw)
        res = prosa_result(question)
        if kw.get("style") != "prosa":
            res.stil = "liste"
        return res


def test_web_chat_passes_project_style(world, monkeypatch):
    db, _ = world
    from klugscheisser.web import build_web_app

    rec = RecordingEngine()
    monkeypatch.setattr("klugscheisser.web.engine.get_answer_engine", lambda settings: rec)
    with TestClient(build_web_app(Settings(_env_file=None, web_answer_engine="fake"))) as c:
        r = c.post("/chat", data={"frage": "Wer ist die Älteste?"}, follow_redirects=False)
        chat_id = int(r.headers["location"].rsplit("/", 1)[1])
        html = c.post(f"/chat/{chat_id}/antwort", headers={"HX-Request": "true"}).text
        assert "style" not in rec.kwargs[-1] and "class='answer prosa'" not in html  # default: liste
        with Session(db) as s:
            s.add(m.ProjectSetting(key="web_answer_style", value="prosa"))
            s.commit()
        c.post(
            f"/chat/{chat_id}/nachricht", data={"frage": "und die Jüngste?"}, headers={"HX-Request": "true"}
        )
        html = c.post(f"/chat/{chat_id}/antwort", headers={"HX-Request": "true"}).text
        assert rec.kwargs[-1]["style"] == "prosa" and "class='answer prosa'" in html
        assert rec.kwargs[-1]["history"][-1]["frage"] == "Wer ist die Älteste?"
        msg_id = re.search(r'id="rate-(\d+)"', html).group(1)
        assert f"href='#m{msg_id}-q1'" in html


def test_mcp_frage_stays_liste(world, monkeypatch):
    db, _ = world
    from klugscheisser.mcp_server import server

    with Session(db) as s:
        s.add(m.ProjectSetting(key="web_answer_style", value="prosa"))
        s.commit()
    rec = RecordingEngine()
    monkeypatch.setattr("klugscheisser.answer.engine.build_answer_engine", lambda settings: rec)
    server.frage("Wer ist die Älteste?")
    assert rec.kwargs and rec.kwargs[-1].get("style", "liste") == "liste"
