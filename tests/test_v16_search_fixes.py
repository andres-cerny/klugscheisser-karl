"""v16: the three search questions the eval still missed (S3, S4, S7): failed sentences rewritten closer to their
quote, record questions to the documents instead of the stats table, long documents read where the search
words occur."""

from klugscheisser.answer import engine as eng
from klugscheisser.answer.engine import Doc, GeminiAnswerEngine
from klugscheisser.config import Settings

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")


def _doc(text: str) -> Doc:
    return Doc(
        key="",
        text=text,
        meta="[Glice Rink, 21.02.2023] · Art: Medienbericht · über: Lara Stalder",
        url="https://youtube/x",
        quelle="[Glice Rink, 21.02.2023]",
        zeile_prefix="",
        item_id=1,
    )


def test_failed_sentence_is_rewritten_to_what_the_quote_says():
    prompts = []

    def llm(system, prompt, schema):
        prompts.append(prompt)
        if schema is eng.SCHEMA_REPAIR:
            assert (
                "Lara Stalder hat auf Kunsteis trainiert." in prompt
                and "«Lara Stalder on Glice Hockey»" in prompt
            )
            return {
                "saetze": [
                    {"nr": 1, "satz": "Ein Video mit dem Titel «Lara Stalder on Glice Hockey» zeigt sie."}
                ]
            }, {
                "input": 100,
                "output": 20,
            }
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Lara Stalder hat auf Kunsteis trainiert.",
                            "belege": [
                                {"dok": "D1", "zitat": "Lara Stalder on Glice Hockey", "kategorie": "anderes"}
                            ],
                        }
                    ],
                }
            ],
            "luecken": [],
        }, {"input": 1000, "output": 40}

    e = GeminiAnswerEngine(
        SETTINGS, llm=llm, support_checker=lambda pairs: [0.9 if "Video" in a else 0.1 for _z, a in pairs]
    )
    out = e._write_prosa(
        "Hat sie auf Kunsteis trainiert?", [_doc("Lara Stalder on Glice Hockey - YouTube")], []
    )
    assert (
        out.antwort[0]
        .saetze[0]
        .text.endswith("Ein Video mit dem Titel «Lara Stalder on Glice Hockey» zeigt sie.")
    )
    assert out.nicht_sicher == [] and out.usage["input"] == 1100 and len(prompts) == 2


def test_no_repair_when_a_sentence_holds_or_the_rewrite_still_fails():
    calls = []

    def llm(system, prompt, schema):
        calls.append(schema)
        if schema is eng.SCHEMA_REPAIR:
            return {"saetze": [{"nr": 1, "satz": "Sie trainierte auf Kunsteis."}]}, {}
        return {
            "antwort": [
                {
                    "typ": "text",
                    "saetze": [
                        {
                            "satz": "Sie trainierte auf Kunsteis.",
                            "belege": [
                                {"dok": "D1", "zitat": "Lara Stalder on Glice Hockey", "kategorie": "anderes"}
                            ],
                        }
                    ],
                }
            ],
            "luecken": [],
        }, {}

    e = GeminiAnswerEngine(SETTINGS, llm=llm, support_checker=lambda pairs: [0.1] * len(pairs))
    out = e._write_prosa("q", [_doc("Lara Stalder on Glice Hockey")], [])
    assert out.antwort == [] and len(out.nicht_sicher) == 1  # still not supported → stays "nicht sicher"
    good = GeminiAnswerEngine(SETTINGS, llm=llm, support_checker=lambda pairs: [0.9] * len(pairs))
    calls.clear()
    good._write_prosa("q", [_doc("Lara Stalder on Glice Hockey")], [])
    assert eng.SCHEMA_REPAIR not in calls  # a supported answer needs no repair call


def test_record_questions_skip_the_stats_table():
    assert eng.RECORD.search("Welchen Punkterekord hat Alina Müller aufgestellt?")
    assert eng.RECORD.search("Is she the all-time leader?") and eng.RECORD.search("Allzeit-Leaderin")
    assert not eng.RECORD.search("Wie viele Punkte hat Alina Müller diese Saison?")


def test_long_document_read_where_the_search_words_are():
    text = "Fran Rider is a Canadian ice hockey executive. " + "Organised tournaments. " * 200
    text += "In May 2018 she pledged her brain to a concussion research centre."
    terms = eng.search_terms("Welche Spielerin vermacht ihr Gehirn der Forschung?", "brain donation research")
    assert "brain" in terms and "spiele" not in terms
    p = eng.passage(text, terms)
    assert p.startswith("Fran Rider is a Canadian") and "pledged her brain" in p and len(p) <= 803
    assert eng.passage("short text", terms) == "short text"
    assert eng.passage(text, ["nothing"]) == text[:800]  # no search word: the start as before
