"""2026-10-25: whole Wikipedia articles (was: cut at 6,000 chars), the backfill for cut profiles, dokument parts."""

import copy

import httpx
import respx
from sqlalchemy.orm import Session
from test_v3_profiles import ALINA, DE_API, WD, polite

from klugscheisser.collectors.profiles import enabled_profile_collectors
from klugscheisser.collectors.profiles.text import cap_text
from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.embed import chunks as C
from klugscheisser.ingest.profiles import backfill_full_wikipedia, cut_wikipedia_profiles
from klugscheisser.ingest.store import content_hash
from klugscheisser.mcp_server.cards import DOC_PART, doc_parts, dokument
from klugscheisser.mcp_server.server import MAX_TOOL_CHARS
from klugscheisser.seed.loader import load_all_seeds

URL = "https://de.wikipedia.org/wiki/Alina_M%C3%BCller"
SENT = "In der Saison {y} spielte sie für die Northeastern University und erzielte viele Tore. "
LONG = "Alina Müller ist eine Schweizer Eishockeyspielerin.\n\n" + "".join(
    SENT.format(y=y) for y in range(1900, 2160)
)  # ~23,000 characters, the end: "Saison 2159"


def mock_long() -> None:
    extract = copy.deepcopy(ALINA["extract_de"])
    extract["query"]["pages"][0]["extract"] = LONG
    respx.get(WD).mock(return_value=httpx.Response(200, json=ALINA["wbgetentities"]))
    respx.get(DE_API).mock(return_value=httpx.Response(200, json=extract))
    respx.get(url__regex=r"https://de\.wikipedia\.org/api/rest_v1/page/summary/.*").mock(
        return_value=httpx.Response(200, json=ALINA["summary_de"])
    )


@respx.mock
def test_wikipedia_article_is_stored_whole():
    mock_long()
    (doc,) = WikipediaCollector(polite(), languages=["de"]).collect(
        "Alina Müller", "icehockey_women", {"wikidata_qid": "Q15768448"}
    )
    assert len(doc.text) > 20_000 and "Saison 2159" in doc.text and not doc.text.endswith(" […]")
    st = Settings(_env_file=None)
    wiki = next(c for c in enabled_profile_collectors(st) if c.name == "wikipedia")
    assert wiki.max_chars == st.profiles_wikipedia_max_chars == 100_000
    college = next(c for c in enabled_profile_collectors(st) if c.name != "wikipedia")
    assert college.max_chars == st.profiles_max_chars == 6000  # college bio pages keep their cap


def _cut_item(s: Session) -> m.Item:
    alina = repo.resolve_athlete(s, "Alina Müller")
    old = cap_text(LONG, 6000)
    item = m.Item(
        source_type="profile",
        platform="wikipedia",
        source_name="Wikipedia (de)",
        url=URL,
        title="Alina Müller",
        text=old,
        content_hash=content_hash(old, "Alina Müller"),
        raw={"collector": "wikipedia", "language": "de"},
        tagged=[],
        is_deleted_at_source=False,
    )
    item.athletes.append(m.ItemAthlete(athlete_id=alina.id, match_kind="profile", confidence=1.0))
    s.add(item)
    s.flush()
    return item


@respx.mock
def test_backfill_full_wikipedia_dry_run_then_update(db):
    mock_long()
    with Session(db) as s:
        load_all_seeds(s)
        item = _cut_item(s)
        s.commit()
        old_text, old_hash, old_body = item.text, item.content_hash, C.body_hash(C.doc_body(item))
        assert [i.id for i in cut_wikipedia_profiles(s)] == [item.id]
        collector = WikipediaCollector(polite(), languages=["de"])

        r = backfill_full_wikipedia(s, collector, dry_run=True)
        assert (r.candidates, r.longer, r.unchanged, r.missing) == (1, 1, 0, 0)
        assert r.chars_before == len(old_text) and r.chars_after == len(LONG.strip())
        assert item.text == old_text and item.content_hash == old_hash  # dry run: nothing changed

        r = backfill_full_wikipedia(s, collector)
        s.commit()
        s.refresh(item)
        assert r.longer == 1 and not r.errors
        assert "Saison 2159" in item.text and not item.text.endswith(" […]")
        assert item.content_hash == content_hash(item.text, item.title) != old_hash
        assert C.body_hash(C.doc_body(item)) != old_body  # reembed-chunks sees a changed document
        assert item.raw["language"] == "de" and item.raw["full_text_at"]
        assert [ia.athlete.full_name for ia in item.athletes] == ["Alina Müller"]  # same item, same links

        # second run: nothing is cut any more
        assert backfill_full_wikipedia(s, collector).candidates == 0


@respx.mock
def test_backfill_skips_missing_articles(db):
    respx.get(DE_API).mock(
        return_value=httpx.Response(
            200, json={"query": {"pages": [{"title": "Alina Müller", "missing": True}]}}
        )
    )
    with Session(db) as s:
        load_all_seeds(s)
        item = _cut_item(s)
        r = backfill_full_wikipedia(s, WikipediaCollector(polite(), languages=["de"]))
        assert (r.candidates, r.longer, r.missing) == (1, 0, 1)
        assert item.text.endswith(" […]")


def test_doc_parts_fit_the_tool_cap_and_lose_nothing():
    body = " ".join(SENT.format(y=y).strip() for y in range(1900, 2600))  # ~60,000 chars, one line
    parts = doc_parts(body)
    assert len(parts) >= 6 and all(len(p) <= DOC_PART for p in parts)
    assert all(p.endswith(".") for p in parts)  # cut after a sentence
    assert " ".join(parts) == body
    assert doc_parts("kurz.") == ["kurz."]


def test_dokument_part_of_a_long_bio_is_not_cut_by_the_tool_cap(db):
    with Session(db) as s:
        load_all_seeds(s)
        item = _cut_item(s)
        item.text = " ".join(SENT.format(y=y).strip() for y in range(1900, 2600))
        s.flush()
        first = dokument(s, item.id)["dokument_text"]
        last = dokument(s, item.id, teil=99)["dokument_text"]
        assert len(first) < MAX_TOOL_CHARS and "Teil 1/" in first and "teil=2" in first
        assert "Saison 2599" in last and "weiter mit" not in last
