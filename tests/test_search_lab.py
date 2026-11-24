"""Search lab 2026-10-25: passage vectors (RETRIEVAL_CHUNKS), pairwise Jev (JEV_PAIRWISE), document cards."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_search_jev_path import add_item, world  # noqa: F401  (fixture)

from klugscheisser.classify.jev import JevPairReranker, reader_context
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.embed import chunks as C
from klugscheisser.retrieval import docs
from klugscheisser.retrieval.search import Filters

SETTINGS = Settings(_env_file=None, auto_refresh=False)


def test_split_short_document_is_one_passage():
    assert [p.text for p in C.split("Kurzer Text.")] == ["Kurzer Text."]


def test_split_long_document_overlaps_and_covers_everything():
    body = " ".join(f"Satz Nummer {i} handelt von Eishockey in der Schweiz." for i in range(200))
    parts = C.split(body)
    assert len(parts) > 3
    assert all(len(p.text) <= C.SIZE + 5 for p in parts)
    assert parts[0].start == 0
    for a, b in pairwise(parts):
        assert b.start < a.start + len(a.text)  # overlap: next passage starts inside the previous one
        assert b.start > a.start
    assert body.rstrip().endswith(parts[-1].text[-30:])


def test_embed_text_has_title_and_header():
    item = m.Item(
        title="Saisonende",
        source_name="SRF",
        source_type="srf",
        platform="rss",
        published_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    t = C.embed_text(item, C.Passage(0, 0, "Kreuzbandriss."), names=["Lara Testalder"])
    assert t.startswith(
        "title: Saisonende | text: Quelle: SRF (srf) | Datum: 2026-10-02 | Personen: Lara Testalder"
    )
    assert t.endswith("Kreuzbandriss.")


class StubChunkEmbedder:
    chunked = True
    model = "stub-chunk-768"
    dim = 768

    def embed_query(self, text):
        return [1.0] + [0.0] * 767


def test_chunk_vectors_give_candidates_and_their_best_passage(world):  # noqa: F811
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        for ord_, (text, vec) in enumerate(
            [
                ("Anfang des Porträts.", [0.0, 1.0] + [0.0] * 766),
                ("Kreuzbandriss im Knie.", [1.0] + [0.0] * 767),
            ]
        ):
            s.add(
                m.ItemChunk(
                    item_id=knie.id,
                    model="stub-chunk-768",
                    ord=ord_,
                    start=0,
                    text=text,
                    text_hash="h",
                    vector=vec,
                )
            )
        s.flush()
        passages: dict = {}
        _, why = docs.candidate_items(
            s, "torn ACL", Filters(), cap=30, embedder=StubChunkEmbedder(), passages=passages
        )
        assert "vector" in why[knie.id]
        assert passages[knie.id] == "Kreuzbandriss im Knie."  # the nearest passage, not the doc start
        s.rollback()


def test_query_embedder_chunk_switch(monkeypatch):
    on = Settings(_env_file=None, retrieval_chunks=True, gemini_api_key="x")
    monkeypatch.setattr(docs, "chunks_ready", lambda _s: False)
    assert not getattr(docs.query_embedder(on), "chunked", False)  # no passage vectors yet: document vectors
    monkeypatch.setattr(docs, "chunks_ready", lambda _s: True)
    assert getattr(docs.query_embedder(on), "chunked", False)
    assert docs.query_embedder(Settings(_env_file=None, retrieval_chunks=True, gemini_api_key=None)) is None


def test_doc_card_fields(world):  # noqa: F811
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        card = docs.doc_card(knie, ["kreuzb"], now=knie.published_at + timedelta(days=3))
        assert card["quelle"] == "SRF" and card["über"] == "Lara Testalder"
        assert card["datum"].endswith("(vor 3 Tagen)")
        assert "Kreuzbandriss" in card["text"]
        assert docs.doc_card(knie, [], best="Nur dieser Abschnitt.")["text"] == "Nur dieser Abschnitt."


class FakeJevClient:
    def __init__(self, fail_on: str | None = None):
        self.states, self.usage, self.fail_on = [], {"input_tokens": 0}, fail_on

    def ask(self, state, questions):
        self.states.append((state, questions))
        text = state["dokument"].get("text", "")
        if self.fail_on and self.fail_on in text:
            raise RuntimeError("Jev HTTP 500")
        hit = "Kreuzband" in text
        out = {"rel": {"score": 3.0 if hit else 0.0}}
        if "beleg" in questions:
            out["beleg"] = {"noul": 0.9 if hit else 0.05}
        return out


def test_pair_reranker_one_request_per_document_with_json_state():
    client = FakeJevClient()
    rr = JevPairReranker(SETTINGS, client=client, workers=4, reader_context=True, evidence=True)
    out = rr.judge("Kreuzbandriss", [{"titel": "a", "text": "Kreuzbandriss"}, "Training in Davos"])
    assert [round(x["score"], 2) for x in out] == [1.0, 0.0]
    assert out[0]["beleg"] == 0.9
    assert len(client.states) == 2
    state, questions = client.states[0]
    assert state["frage"] == "Kreuzbandriss" and state["nutzerin"] == reader_context()
    assert not any(c.startswith(("0:", "1:")) for c in questions["rel"]["criteria"])  # descriptive levels


def test_pair_reranker_falls_back_per_document():
    rr = JevPairReranker(SETTINGS, client=FakeJevClient(fail_on="Davos"), workers=2, evidence=False)
    out = rr.judge("Training Davos", ["Kreuzband", "Training in Davos"])
    assert out[1].get("fallback") == 1.0 and out[1]["score"] > 0  # word overlap for the failed one
    assert rr.errors == 1


def test_find_documents_noul_cut_off(world):  # noqa: F811
    st = SETTINGS.model_copy(update={"rerank_noul_min": 0.5})
    rr = JevPairReranker(st, client=FakeJevClient(), workers=4)
    with Session(world) as s:
        hits = docs.find_documents(s, st, "Kreuzband Knie", Filters(), reranker=rr)
        assert [h.item.url for h in hits] == ["https://knie"]  # only the document with evidence
        assert hits[0].scores["beleg"] == 0.9
        s.rollback()
    _ = add_item


def test_reader_context_and_card_line_in_chunk_mode(world):  # noqa: F811
    from klugscheisser.classify.jev import JevReranker

    class Client:
        usage = None

        def ask(self, state, questions):
            self.state, self.questions = state, questions
            return {k: {"score": 1.0} for k in questions}

    c = Client()
    JevReranker(SETTINGS, client=c, reader_context=True).score("Kreuzband", ["a", "b"])
    assert (
        c.state.startswith(f"Nutzerin: {reader_context()}")
        and "der Nutzerin" in c.questions["c0"]["instructions"]
    )
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        docs.preload_cards(s, [knie])
        line = docs.card_line(docs.doc_card(knie, ["kreuzb"], width=docs.CARD_LINE_PASSAGE))
        assert "| SRF (" in line and "über: Lara Testalder" in line and len(line) <= 800


def test_store_chunks_skips_unchanged_text(world):  # noqa: F811
    class Emb:
        model = "stub-chunk-768"
        calls = 0

        def embed_documents(self, texts):
            Emb.calls += 1
            return [[1.0] + [0.0] * 767 for _ in texts]

    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        assert C.store_chunks(s, Emb(), knie) == 1
        assert C.store_chunks(s, Emb(), knie) == 0  # same text hash: no new call
        knie.text = knie.text + " Neu."
        assert C.store_chunks(s, Emb(), knie) == 1 and Emb.calls == 2
        s.rollback()


def test_search_cards_shape(world):  # noqa: F811
    from klugscheisser.mcp_server import cards
    from klugscheisser.mcp_server import tools as t
    from klugscheisser.mcp_server.present import render_markdown

    st = SETTINGS.model_copy(update={"mcp_search_cards": True, "retrieval_vector_candidates": False})
    t.freshen = lambda s, athletes: {"status": "test"}
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        knie.text = "📸 Kreuzbandriss im Knie: die Saison ist vorbei. https://t.co/xyz " + "Reha. " * 300
        knie.source_kind = "self"
        knie.platform = "instagram"
        s.flush()
        text = render_markdown(t.search_tool(s, st, "Kreuzbandriss Knie"))
        block = next(b for b in text.split("\n\n") if b.startswith("**[") and "Kreuzbandriss" in b)
        assert block.count("http") == 1 and block.strip().splitlines()[-1] == "Link: https://knie"
        assert "Typ: Instagram, eigener Account" in block and "📸" not in block
        assert f"Volltext: dokument({knie.id})" in block  # long text: excerpt + full text on request
        short = [b for b in text.split("\n\n") if b.startswith("**[") and "Lara Testalder trifft" in b]
        assert not short or "Volltext" not in short[0]  # short documents are shown in full
        doc = render_markdown(t.dokument_tool(s, knie.id))
        assert "Teil 1/1" in doc and "Reha." in doc and doc.count("Link: https://knie") == 1
        assert len(cards.clean("🏒 a  b\nc")) == 5
        s.rollback()
