"""v3 P2: Wikipedia articles and college bio pages as raw profile documents (offline, respx)."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.collectors.profiles.college import (
    CollegeSite,
    SidearmRosterCollector,
    extract_bio,
    find_player,
    parse_roster,
)
from klugscheisser.collectors.profiles.http import Disallowed, PoliteClient
from klugscheisser.collectors.profiles.text import cap_text
from klugscheisser.collectors.profiles.wikipedia import WikipediaCollector, title_matches
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.ingest.profiles import athlete_hints, ingest_profiles
from klugscheisser.ingest.runner import build_context
from klugscheisser.seed.loader import load_all_seeds

FIX = Path("tests/fixtures/profiles")
ALINA = json.loads((FIX / "wikipedia_alina_mueller.json").read_text())
LAURA = json.loads((FIX / "wikipedia_laura_zimmermann_search.json").read_text())
WD = "https://www.wikidata.org/w/api.php"
DE_API = "https://de.wikipedia.org/w/api.php"
EN_API = "https://en.wikipedia.org/w/api.php"
HC = "https://goholycross.com"
HC_ROSTER = f"{HC}/sports/womens-ice-hockey/roster"
HC_BIO = f"{HC_ROSTER}/naemi-herzig/17403"
HC_SITE = CollegeSite(
    team="College of the Holy Cross",
    aliases=["Holy Cross", "Holy Cross Crusaders"],
    source_name="Holy Cross Athletics",
    roster_url=HC_ROSTER,
)
SIDEARM_ROBOTS = (
    "User-agent: *\nDisallow: /common/\nDisallow: /services/\nDisallow: /images/\nAllow: /\nCrawl-delay: 30\n"
)


class FakeClock:
    """Records sleeps instead of waiting; time advances only by sleeping."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def __call__(self) -> float:
        return self.now


def polite(clock: FakeClock | None = None) -> PoliteClient:
    clock = clock or FakeClock()
    return PoliteClient(min_delay_s=0, sleep=clock.sleep, clock=clock)


def _api(extract: dict):
    """Action API: the extract for page queries, an empty result for searches (no namesake articles)."""

    def handler(request):
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json={"query": {"search": []}})
        return httpx.Response(200, json=extract)

    return handler


def mock_alina() -> None:
    respx.get(WD).mock(return_value=httpx.Response(200, json=ALINA["wbgetentities"]))
    for api, key in ((DE_API, "extract_de"), (EN_API, "extract_en")):
        respx.get(api).mock(side_effect=_api(ALINA[key]))
    respx.get(url__regex=r"https://de\.wikipedia\.org/api/rest_v1/page/summary/.*").mock(
        return_value=httpx.Response(200, json=ALINA["summary_de"])
    )
    respx.get(url__regex=r"https://en\.wikipedia\.org/api/rest_v1/page/summary/.*").mock(
        return_value=httpx.Response(200, json=ALINA["summary_en"])
    )


def mock_holycross(robots: str = SIDEARM_ROBOTS):
    respx.get(f"{HC}/robots.txt").mock(return_value=httpx.Response(200, text=robots))
    roster = respx.get(HC_ROSTER).mock(
        return_value=httpx.Response(200, text=(FIX / "sidearm_holycross_roster.html").read_text())
    )
    bio = respx.get(HC_BIO).mock(
        return_value=httpx.Response(200, text=(FIX / "sidearm_holycross_bio.html").read_text())
    )
    return roster, bio


# --- Wikipedia ------------------------------------------------------------------------------------


@respx.mock
def test_wikipedia_via_qid_one_doc_per_language():
    mock_alina()
    docs = WikipediaCollector(polite(), languages=["de", "en", "fr"]).collect(
        "Alina Müller", "icehockey_women", {"wikidata_qid": "Q15768448"}
    )
    assert [d.language for d in docs] == ["de", "en"]  # no frwiki sitelink in the fixture -> no fr doc
    de, en = docs
    assert de.source_name == "Wikipedia (de)" and en.source_name == "Wikipedia (en)"
    assert de.url == "https://de.wikipedia.org/wiki/Alina_M%C3%BCller"
    assert (de.source_kind, de.source_type, de.platform) == ("media", "profile", "wikipedia")
    assert de.text.startswith(
        "Alina Müller (* 12. März 1998 in Lengnau AG) ist eine Schweizer Eishockeyspielerin"
    )
    assert "Northeastern" in en.text
    assert de.published_at.isoformat().startswith("2026-06-27T14:40:50")
    assert "\n\n\n" not in de.text


@respx.mock
def test_wikipedia_text_capped_after_full_sentence():
    mock_alina()
    docs = WikipediaCollector(polite(), languages=["de"], max_chars=600).collect(
        "Alina Müller", "icehockey_women", {"wikidata_qid": "Q15768448"}
    )
    text = docs[0].text
    assert len(text) <= 604 and text.endswith(" […]")
    assert text[: -len(" […]")].rstrip().endswith((".", "!", "?", "»", ")"))


def test_cap_text_never_cuts_mid_sentence():
    text = "Erster Satz ist hier. Zweiter Satz in der 14. Minute geht weiter. Dritter Satz kommt nie ganz."
    capped = cap_text(text, 75)
    assert capped == "Erster Satz ist hier. Zweiter Satz in der 14. Minute geht weiter. […]"
    assert cap_text("kurz.", 100) == "kurz."


def test_title_matches_name_with_qualifier_only():
    assert title_matches("Laura Zimmermann (Eishockeyspielerin)", "Laura Zimmermann")
    assert title_matches("Alina Mueller", "Alina Müller")
    assert not title_matches("Operation Libero", "Laura Zimmermann")
    assert not title_matches("Women’s League 2021/22", "Laura Zimmermann")


@respx.mock
def test_wikipedia_fallback_search_rejects_namesakes_and_disambiguation():
    def api(request):
        params = request.url.params
        if params.get("list") == "search":
            key = "search_hockey" if "Eishockey" in params["srsearch"] else "search_plain"
            return httpx.Response(200, json=LAURA[key])
        title = params["titles"]
        if title == "Laura Zimmermann":
            return httpx.Response(200, json=LAURA["extract_disambiguation"])
        if title == "Laura Zimmermann (Triathletin)":
            return httpx.Response(200, json=LAURA["extract_triathletin"])
        # the activist: same name, Swiss, no ice hockey
        page = {"title": title, "extract": "Laura Zimmermann ist eine Schweizer Politaktivistin."}
        return httpx.Response(200, json={"query": {"pages": [page]}})

    route = respx.get(DE_API).mock(side_effect=api)
    summary = respx.get(url__regex=r".*/api/rest_v1/page/summary/.*")
    docs = WikipediaCollector(polite(), languages=["de"]).collect("Laura Zimmermann", "icehockey_women", {})
    assert docs == []
    titles = [c.request.url.params.get("titles") for c in route.calls if c.request.url.params.get("titles")]
    assert "Laura Zimmermann (Triathletin)" in titles
    assert not summary.called  # nothing accepted -> no summary fetched


@respx.mock
def test_wikipedia_fallback_accepts_hockey_article():
    search = {"query": {"search": [{"title": "Alina Müller", "snippet": "Eishockeyspielerin"}]}}

    def api(request):
        if request.url.params.get("list") == "search":
            return httpx.Response(200, json=search)
        return httpx.Response(200, json=ALINA["extract_de"])

    respx.get(DE_API).mock(side_effect=api)
    respx.get(url__regex=r"https://de\.wikipedia\.org/api/rest_v1/page/summary/.*").mock(
        return_value=httpx.Response(200, json=ALINA["summary_de"])
    )
    docs = WikipediaCollector(polite(), languages=["de"]).collect("Alina Müller", "icehockey_women", {})
    assert len(docs) == 1 and docs[0].title == "Alina Müller"


# --- Sidearm college pages ------------------------------------------------------------------------


def test_sidearm_roster_parsing_and_matching():
    entries = parse_roster((FIX / "sidearm_holycross_roster.html").read_text(), HC_ROSTER)
    assert all("/coaches/" not in e.url and "/staff/" not in e.url for e in entries)
    hit = find_player(entries, "Naemi Herzig")
    assert hit.url == HC_BIO and hit.name == "Naemi Herzig"
    assert find_player(entries, "Alina Müller") is None
    # umlaut written as "ae" on US sites
    baechler = [*entries, type(hit)(name="Alessia Baechler", url=f"{HC_ROSTER}/alessia-baechler/1")]
    assert find_player(baechler, "Alessia Bächler").name == "Alessia Baechler"


def test_sidearm_bio_extraction_classic_and_nuxt_layout():
    fields, lines = extract_bio((FIX / "sidearm_holycross_bio.html").read_text())
    assert ("Hometown", "St. Gallen, Switzerland") in fields and ("Class", "Sophomore") in fields
    assert "• Majoring in Economics" in lines
    assert "• Played for EV Zug under head coach Daniela Diaz" in lines

    fields, lines = extract_bio((FIX / "sidearm_stcloud_bio.html").read_text())
    assert ("Previous Team", "EV Bomo Thun (SWHL)") in fields
    assert ("Hometown", "Ringgenberg, Switzerland") in fields
    assert any("majoring in Exercise Science" in ln for ln in lines)
    assert lines[0] == "CAREER NOTES"


@respx.mock
def test_sidearm_collector_respects_crawl_delay():
    roster, bio = mock_holycross()
    clock = FakeClock()
    collector = SidearmRosterCollector(sites=[HC_SITE], client=polite(clock))
    docs = collector.collect("Naemi Herzig", "icehockey_women", {"teams": ["College of the Holy Cross"]})
    assert len(docs) == 1
    doc = docs[0]
    assert doc.url == HC_BIO and doc.source_name == "Holy Cross Athletics"
    assert (doc.source_kind, doc.source_type) == ("profile", "profile")
    assert doc.text.startswith("Position: Forward\nHeight: 5-5\nClass: Sophomore\nHometown: St. Gallen")
    assert "Majoring in Economics" in doc.text
    assert roster.call_count == 1 and bio.call_count == 1
    assert clock.sleeps == [30.0, 30.0]  # robots.txt -> roster -> bio, 30 s apart

    # no college in the hints, other sport: no requests at all
    assert collector.collect("Naemi Herzig", "icehockey_women", {"teams": ["EVZ Women's Team"]}) == []
    assert collector.collect("Naemi Herzig", "ski_alpine_women", {"teams": ["Holy Cross"]}) == []
    assert roster.call_count == 1


@respx.mock
def test_robots_disallow_is_respected():
    roster, bio = mock_holycross(robots="User-agent: *\nDisallow: /sports/\n")
    collector = SidearmRosterCollector(sites=[HC_SITE], client=polite())
    with pytest.raises(Disallowed):
        collector.collect("Naemi Herzig", "icehockey_women", {"teams": ["Holy Cross"]})
    assert not roster.called and not bio.called


@respx.mock
def test_robots_unreadable_or_crawl_delay_too_long_means_no_fetch():
    respx.get(f"{HC}/robots.txt").mock(return_value=httpx.Response(403))
    page = respx.get(HC_ROSTER).mock(return_value=httpx.Response(200, text=""))
    client = polite()
    assert client.allowed(HC_ROSTER) == (False, "robots.txt nicht lesbar")

    respx.get("https://slow.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 120\n")
    )
    ok, why = client.allowed("https://slow.example/page")
    assert not ok and "Crawl-delay" in why
    assert not page.called


# --- ingest ---------------------------------------------------------------------------------------


@pytest.fixture
def world(db):
    with Session(db) as s:
        load_all_seeds(s)
        alina = repo.resolve_athlete(s, "Alina Müller")
        alina.wikidata_qid = "Q15768448"
        s.commit()
    return db


@respx.mock
def test_ingest_profiles_idempotent_and_linked(world):
    mock_alina()
    roster, bio = mock_holycross()
    with Session(world) as s:
        alina = repo.resolve_athlete(s, "Alina Müller")
        naemi = repo.resolve_athlete(s, "Naemi Herzig")
        assert "Holy Cross Crusaders" in athlete_hints(naemi)["teams"]
        assert athlete_hints(alina)["wikidata_qid"] == "Q15768448"

        def collectors():
            client = polite()
            return [
                WikipediaCollector(client, languages=["de", "en"]),
                SidearmRosterCollector(sites=[HC_SITE], client=client),
            ]

        ctx = build_context(s, Settings(_env_file=None))
        report = ingest_profiles(ctx, [alina, naemi], collectors())
        s.commit()
        assert not report.errors
        assert report.items_new == 3 and report.docs == 3
        assert report.by_source == {"Wikipedia (de)": 1, "Wikipedia (en)": 1, "Holy Cross Athletics": 1}

        items = s.scalars(select(m.Item).where(m.Item.source_type == "profile").order_by(m.Item.id)).all()
        assert len(items) == 3
        by_url = {i.url: i for i in items}
        hc = by_url[HC_BIO]
        assert [(ia.athlete.full_name, ia.match_kind, ia.confidence) for ia in hc.athletes] == [
            ("Naemi Herzig", "profile", 1.0)
        ]
        assert hc.source_kind == "profile" and hc.author == "Holy Cross Athletics" and hc.platform == "web"
        de = by_url["https://de.wikipedia.org/wiki/Alina_M%C3%BCller"]
        assert de.platform == "wikipedia" and de.source_kind == "media" and de.source_name == "Wikipedia (de)"
        assert [ia.athlete_id for ia in de.athletes] == [alina.id]
        assert de.published_at is not None and de.classification is not None

        # second run within max_age_days: nothing fetched
        report = ingest_profiles(build_context(s, Settings(_env_file=None)), [alina, naemi], collectors())
        assert report.fresh == 2 and report.docs == 0
        assert roster.call_count == 1 and bio.call_count == 1

        # forced re-run (max_age 0): same documents, no new items, no duplicate links
        report = ingest_profiles(
            build_context(s, Settings(_env_file=None)), [alina, naemi], collectors(), max_age_days=0
        )
        s.commit()
        assert report.docs == 3 and report.items_new == 0 and report.items_changed == 0
        assert s.scalar(select(func.count()).select_from(m.Item).where(m.Item.source_type == "profile")) == 3
        assert s.scalar(select(func.count()).select_from(m.ItemAthlete)) == 3


@respx.mock
def test_ingest_profiles_source_error_does_not_mark_fresh(world):
    respx.get(WD).mock(return_value=httpx.Response(503))
    with Session(world) as s:
        alina = repo.resolve_athlete(s, "Alina Müller")
        ctx = build_context(s, Settings(_env_file=None))
        report = ingest_profiles(ctx, [alina], [WikipediaCollector(polite(), languages=["de"])])
        assert report.errors and report.docs == 0
        assert s.get(m.CacheEntry, ("profiles", f"athlete:{alina.id}")) is None


def test_profiles_for_update_off(world):
    from klugscheisser.ingest.profiles import profiles_for_update

    with Session(world) as s:
        settings = Settings(_env_file=None, profiles_enabled=False)
        assert profiles_for_update(s, settings, []) == {"status": "aus"}
