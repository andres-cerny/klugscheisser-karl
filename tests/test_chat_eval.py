"""The fixed chat evaluation: question file loads, the checker finds missing / forbidden words and the wrong
shape, follow-ups get the earlier turn as history, runs are compared with the previous one."""

from klugscheisser import chat_eval
from klugscheisser.interfaces import AnswerBlock, AnswerResult, AnswerSentence


def _res(*sentences, rueckfrage=None, rows=()):
    blocks = [AnswerBlock(typ="text", saetze=[AnswerSentence(t) for t in sentences])] if sentences else []
    if rows:
        blocks.append(
            AnswerBlock(
                typ="tabelle", zeilen=[{"zellen": list(r), "satz": AnswerSentence("x")} for r in rows]
            )
        )
    return AnswerResult(
        frage="q", datenstand="", statements=[], stil="prosa", antwort=blocks, rueckfrage=rueckfrage
    )


def test_question_file_is_complete():
    items = chat_eval.load()
    assert len(items) == 65 and len({it["id"] for it in items}) == 65
    ids = {it["id"] for it in items}
    assert all(it.get("nach") in ids for it in items if it.get("nach"))


def test_checker():
    item = {"muss": ["13 Punkte", "WM|WC"], "darf_nicht": ["31 Jahre"], "art": "antwort"}
    assert chat_eval.check(item, _res("Sie hat 13 Punkte.", rows=[("WC (W)", "5 Spiele")])) == []
    assert chat_eval.check(item, _res("Sie ist 31 Jahre alt.")) == [
        "fehlt: 13 Punkte",
        "fehlt: WM|WC",
        "enthält: 31 Jahre",
    ]
    assert chat_eval.check({"muss": [], "darf_nicht": [], "art": "rueckfrage"}, _res(rueckfrage="Wen?")) == []
    assert chat_eval.check({"muss": [], "darf_nicht": [], "art": "nichts"}, _res("Etwas.")) == [
        "Form: antwort statt nichts"
    ]


def test_run_chains_follow_ups_and_compares(tmp_path):
    calls = []

    class Engine:
        def answer(self, q, use_cache, style, history, **context):
            calls.append((q, history))
            return _res("Lara Stalder ist die Älteste." if "Älteste" in q else "Sie hat 13 Punkte.")

    items = [
        {
            "id": "G1",
            "group": "F",
            "frage": "Wer ist die Älteste?",
            "muss": ["Stalder"],
            "darf_nicht": [],
            "art": "antwort",
        },
        {
            "id": "G2",
            "group": "F",
            "frage": "Und ihre Punkte?",
            "nach": "G1",
            "muss": ["13 Punkte"],
            "darf_nicht": [],
            "art": "antwort",
        },
    ]
    out = chat_eval.run(Engine(), items)
    assert [o.ok for o in out] == [True, True]
    assert calls[1][1][0]["frage"] == "Wer ist die Älteste?" and "Lara Stalder" in calls[1][1][0]["saetze"][0]
    chat_eval.save(out, tmp_path)
    before = chat_eval.previous_run(tmp_path)
    out[1].ok = False
    assert "schlechter als letzter Lauf: G2" in chat_eval.report(out, before)


def test_either_shape_allowed():
    item = {"muss": ["Verletzung"], "darf_nicht": [], "art": "antwort|nichts"}
    assert chat_eval.check(item, _res()) == []
    assert chat_eval.check(item, _res("Keine Verletzung bekannt.")) == []
    assert chat_eval.check(item, _res(rueckfrage="Wen?")) == ["Form: rueckfrage statt antwort|nichts"]
