"""v12 Part I (dictation that shows the words while speaking) and Part H (chat replay report, question set)."""

import json
import re
import shutil
import subprocess

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient
from typer.testing import CliRunner

from klugscheisser import replay
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence, Statement
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app
from klugscheisser.web.html import CSS, js

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


# --- Part I: dictation -------------------------------------------------------------------------------------


def test_dictation_script_and_styles():
    script = js()
    # continuous until the button or ~3 s of silence, interim results live, de-CH default, en-US switch
    for needle in (
        "r.continuous=true",
        "r.interimResults=true",
        "MIC_SILENCE_MS=3000",
        "de:'de-CH'",
        "en:'en-US'",
        "localStorage.setItem('micLang'",
        # "I'm listening": red pulse from audiostart, stronger on speech, flash per result, vibration
        "r.onaudiostart=listening",
        "r.onspeechstart=",
        "r.onresult=",
        "flash()",
        "navigator.vibrate",
        "Ich höre zu …",
        # second mic stream only behind a flag that is off
        "const MIC_ANALYSER=false",
        "createAnalyser",
        # errors → short German hint; no recognition → no button
        "'no-speech':",
        "'not-allowed':",
        "if(!SR)return",
    ):
        assert needle in script, needle
    for needle in (".micbox.on .mic{", "@keyframes micpulse", ".mic-lang{", ".mic-live{", ".mic-lvl"):
        assert needle in CSS, needle
    assert "prefers-reduced-motion" in CSS


def test_dictation_on_chat_page(db):
    with TestClient(build_web_app(SETTINGS)) as c:
        page = c.get("/chat").text
    assert "addMics" in page and "micJoin" in page and 'name="frage"' in page


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_mic_join_drops_repeated_words():
    """Android Chrome repeats earlier words in later final results; the field shows them once."""
    fn = re.search(r"function micJoin\(parts\)\{.*?return out\.join\(' '\)\}", js(), re.S).group(0)
    cases = [
        ["wer ist", "wer ist die älteste"],
        ["wer ist ", " die älteste", "die älteste"],
        ["Who is", "the oldest"],
        ["", "  "],
    ]
    code = fn + f"console.log(JSON.stringify({json.dumps(cases)}.map(micJoin)))"
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, check=True).stdout
    assert json.loads(out) == ["wer ist die älteste", "wer ist die älteste", "Who is the oldest", ""]


# --- Part H: replay ----------------------------------------------------------------------------------------


def _answer(text: str, **kw) -> dict:
    res = AnswerResult(frage="", datenstand="heute", statements=[], luecken=[text], kosten_usd=0.002, **kw)
    from dataclasses import asdict

    return asdict(res)


@pytest.fixture
def chats(db):
    """Two chats: a person chat with a rated answer and a follow-up, and a general one with an add request."""
    with Session(db) as s:
        load_all_seeds(s)
        lara = repo.resolve_athlete(s, "Lara Stalder")
        c1 = m.Chat(title="Team", athlete_id=lara.id)
        c2 = m.Chat(title="Allgemein")
        s.add_all([c1, c2])
        s.flush()
        s.add_all(
            [
                m.ChatMessage(chat_id=c1.id, role="user", text="Für welches Team spielt sie aktuell?"),
                m.ChatMessage(
                    chat_id=c1.id,
                    role="assistant",
                    text="Sie spielt beim EV Zug.",
                    answer=_answer("alt", personen=["Lara Stalder"], person_ids=[lara.id]),
                    rating="schlecht",
                    rating_reason="zu knapp",
                ),
                m.ChatMessage(chat_id=c2.id, role="user", text="Füge Hanna Olsson hinzu"),
                m.ChatMessage(chat_id=c2.id, role="assistant", text="Vorschlag …"),
                m.ChatMessage(chat_id=c1.id, role="user", text="und seit wann?"),
                m.ChatMessage(chat_id=c1.id, role="assistant", text="Keine Angabe.", answer=_answer("alt2")),
                m.ChatMessage(chat_id=c2.id, role="user", text="Wer ist die älteste?"),
            ]
        )
        s.commit()
        return {"lara": lara.id, "c1": c1.id, "c2": c2.id}


def _snapshot(db) -> tuple:
    with Session(db) as s:
        return (
            s.scalar(select(func.count(m.ChatMessage.id))),
            s.scalar(select(func.count(m.Chat.id))),
            tuple(s.scalars(select(m.Chat.updated_at).order_by(m.Chat.id))),
            tuple(s.scalars(select(m.ChatMessage.rating).order_by(m.ChatMessage.id))),
        )


def test_collect_questions_with_history_and_rating(chats, db):
    with Session(db) as s:
        qs = replay.collect_questions(s, "eishockey")
    assert [q.question for q in qs] == [
        "Für welches Team spielt sie aktuell?",
        "und seit wann?",
        "Wer ist die älteste?",
    ]  # the add request is not an engine question
    first, follow, open_ = qs
    assert first.scope == {"person_ids": [chats["lara"]]} and first.history == []
    assert (
        first.rating == "schlecht" and first.rating_reason == "zu knapp" and first.context == "Lara Stalder"
    )
    assert follow.history == [
        {
            "frage": "Für welches Team spielt sie aktuell?",
            "antwort": "Sie spielt beim EV Zug.",
            "personen": ["Lara Stalder"],
            "person_ids": [chats["lara"]],
        }
    ]
    assert follow.scope == {"person_ids": [chats["lara"]]}  # follow-up keeps the previous turn's persons
    assert open_.stored is None and open_.rating is None
    with Session(db) as s:
        assert [q.question for q in replay.collect_questions(s, limit=1)] == ["Wer ist die älteste?"]


def test_fake_replay_writes_nothing_and_reports_both(chats, db):
    before = _snapshot(db)
    engine = replay.ReplayFakeEngine(SETTINGS)
    rows = replay.replay_project("eishockey", engine, ("liste", "prosa"))
    assert _snapshot(db) == before
    assert len(rows) == 3 and len(engine.calls) == 6
    assert all(c["use_cache"] is False for c in engine.calls)
    assert [c["style"] for c in engine.calls[:2]] == ["liste", "prosa"]
    assert engine.calls[2]["history"][0]["frage"] == "Für welches Team spielt sie aktuell?"
    assert rows[0].answers["prosa"].result.stil == "prosa" and rows[0].answers["liste"].result.stil == "liste"
    html = replay.report_html(rows, ("liste", "prosa"), note="Testmodus")
    assert "Alt (Liste)" in html and "Neu (Prosa)" in html
    assert "rate-schlecht" in html and "«zu knapp»" in html  # Fabi's rating next to the old answer
    assert "Folgefrage: 1 frühere Fragen" in html and "Damals gespeicherte Antwort" in html
    assert html.count("class='rp-q'") == 3 and "Testmodus" in html and "Kosten $" in html


def test_engine_without_style_answers_list_only():
    class Old:
        def __init__(self):
            self.kw = []

        def answer(self, question, person_ids=None, team_id=None, event_id=None, history=None):
            self.kw.append((person_ids, history))
            return AnswerResult(frage=question, datenstand="", statements=[])

    eng = Old()
    q = replay.ReplayQuestion("x", 1, 1, "Wer?", "", "", {"person_ids": [3]}, [])
    assert replay.answer_once(eng, q, "liste").result is not None
    prosa = replay.answer_once(eng, q, "prosa")
    assert prosa.result is None and "style" in prosa.error and len(eng.kw) == 1


def test_cli_dry_run_makes_no_engine_call(chats, monkeypatch, tmp_path):
    from klugscheisser import cli

    def boom(*a, **k):
        raise AssertionError("no engine call without --yes")

    monkeypatch.setattr("klugscheisser.answer.engine.build_answer_engine", boom)
    monkeypatch.setattr(replay, "answer_once", boom)
    out = tmp_path / "r.html"
    res = CliRunner().invoke(cli.app, ["chat-replay", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert "3 Fragen × 2 Stile" in res.output and "geschätzt $" in res.output
    assert "Nichts ausgeführt" in res.output and not out.exists()


def test_cli_fake_run_writes_report(chats, db, tmp_path):
    from klugscheisser import cli

    before = _snapshot(db)
    out = tmp_path / "r.html"
    res = CliRunner().invoke(cli.app, ["chat-replay", "--fake", "--styles", "liste,prosa", "--out", str(out)])
    assert res.exit_code == 0, res.output
    html = out.read_text()
    assert "Für welches Team spielt sie aktuell?" in html and "Neu (Prosa)" in html and "Testmodus" in html
    assert _snapshot(db) == before
    bad = CliRunner().invoke(cli.app, ["chat-replay", "--fake", "--styles", "kurz"])
    assert bad.exit_code != 0


def test_estimate():
    est = replay.estimate(10, ("liste", "prosa"), 0.002)
    assert est.answers == 20 and est.calls == 40 and est.usd == pytest.approx(0.05)
    assert replay.estimate(4, ("liste",), 0.0).usd == pytest.approx(4 * replay.FALLBACK_COST_USD)


# --- Part H: question set ----------------------------------------------------------------------------------


def _st(quelle: str = "[SRF, 03.10.]") -> Statement:
    return Statement(
        aussage="a", zitat="z", item_id=1, url="https://x", quelle=quelle, quelle_link="", zeile_prefix=""
    )


def _prosa(sentences, table=None, quellen=None, **kw) -> AnswerResult:
    blocks = [AnswerBlock(typ="text", saetze=[AnswerSentence(text=t, quellen=q) for t, q in sentences])]
    if table:
        blocks.append(
            AnswerBlock(
                typ="tabelle",
                spalten=["Spielerin", "Geboren"],
                zeilen=[
                    {"zellen": cells, "satz": AnswerSentence(text=" | ".join(cells), quellen=q)}
                    for cells, q in table
                ],
            )
        )
    return AnswerResult(
        frage="",
        datenstand="",
        statements=[_st()],
        stil="prosa",
        antwort=blocks,
        quellen=quellen or [_st()],
        **kw,
    )


def test_question_set_loads():
    items = {x.id: x for x in replay.load_question_set()}
    assert {"1", "1-folge", "1b", "2", "3", "4", "5", "6"} <= set(items)
    assert items["1-folge"].folgefrage_von == "1b-vorher" and items["1b"].tabelle is True
    assert items["2"].rueckfrage == "erlaubt" and items["4"].nur_eigene_quellen and items["5"].nichts_gefunden
    assert items["6"].kanal == "mcp" and items["6"].stil == "liste"
    assert all(x.sprache == "de" for x in items.values())


def test_question_set_order_is_checked(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text("fragen:\n - {id: a, frage: x, folgefrage_von: b}\n - {id: b, frage: y}\n")
    with pytest.raises(ValueError, match="vorher"):
        replay.load_question_set(p)


def test_checker_with_fake_results():
    items = {x.id: x for x in replay.load_question_set()}
    calc = [_st("[Berechnet aus gespeicherten Daten, 04.10.]")]
    good = _prosa(
        [("Jessica Lutz ist die älteste Spielerin, sie ist am 24.05.1989 geboren.", [1])], quellen=calc
    )
    assert replay.check_result(items["1"], good, live=True) == []
    bad = _prosa([("Jessica Lutz is the oldest player of the team, she was born in 1989.", [])])
    probs = replay.check_result(items["1"], bad)
    assert any("ohne Quelle" in p for p in probs) and any("Deutsch" in p for p in probs)
    assert any("berechnete Quelle" in p for p in probs)
    # 1b: a table with a row without source and a row pointing to a missing source
    table = _prosa(
        [("Der Kader hat 23 Spielerinnen.", [1])],
        table=[
            (["Jessica Lutz", "24.05.1989"], [1]),
            (["Alina Müller", "12.03.1998"], []),
            (["X", "Y"], [5]),
        ],
        quellen=calc,
    )
    probs = replay.check_result(items["1b"], table)
    assert any("Tabellenzeile ohne Quelle" in p for p in probs) and any("nicht gibt" in p for p in probs)
    assert any("Tabelle erwartet" in p for p in replay.check_result(items["1b"], good))
    # 2: a question back is fine
    ask = AnswerResult(
        frage="", datenstand="", statements=[], stil="prosa", rueckfrage="Welches Spiel meinst du?"
    )
    assert replay.check_result(items["2"], ask) == []
    assert "unerwartete Rückfrage" in replay.check_result(items["3"], ask)
    # 4: sensitive only from own posts
    foreign = _prosa([("Sie ist mit einem Spieler zusammen.", [1])], quellen=[_st("[Blick, 01.10.]")])
    assert any("heikles Thema" in p for p in replay.check_result(items["4"], foreign))
    own = _prosa(
        [("Sie zeigt auf Instagram ein Foto mit ihrem Freund.", [1])],
        quellen=[_st("[Instagram @lara, 2.9.]")],
    )
    assert replay.check_result(items["4"], own) == []
    # 5: nothing found + closest sources
    none = _prosa([("Dazu habe ich nichts gefunden.", [])], naechste_quellen=[{"titel": "t", "url": "u"}])
    assert replay.check_result(items["5"], none) == []
    assert any(
        "nächsten Quellen" in p
        for p in replay.check_result(items["5"], _prosa([("Dazu habe ich nichts gefunden.", [])]))
    )
    # 6: MCP keeps the line format
    lines = AnswerResult(frage="", datenstand="", statements=[_st()])
    assert replay.check_result(items["6"], lines) == []
    assert replay.check_result(items["6"], good)


def test_run_question_set_with_fake_engine(chats):
    engine = replay.ReplayFakeEngine(SETTINGS)
    out = replay.run_question_set(engine)
    assert len(out) == len(replay.load_question_set()) and all(r is not None for _, r, _ in out)
    follow = next(c for c in engine.calls if c["frage"] == "And who is the oldest in the olympic team")
    assert follow["history"][0]["frage"] == "Who has the most ig followers in the team"
    lara_q = next(c for c in engine.calls if c["frage"] == "Hat Lara Stalder einen Freund?")
    assert lara_q["person_ids"] == [chats["lara"]] and lara_q["style"] == "prosa"
    assert next(c for c in engine.calls if c["frage"].startswith("Who from"))["use_cache"] is False
