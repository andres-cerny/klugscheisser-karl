"""v6 step 4: Steckbrief profile card — key facts with sources, conflicts, career table, Wikipedia intro,
Wikimedia Commons photo with author and licence (no network in tests)."""

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.facts.wikidata_import import import_wikidata_facts
from klugscheisser.mcp_server.present import age
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app, card, card_data, photo

SETTINGS = Settings(_env_file=None, auto_refresh=False, web_answer_engine="fake")
FIXTURES = Path(__file__).parent / "fixtures"
COMMONS = json.loads((FIXTURES / "commons" / "alina_mueller_p18.json").read_text())
WD_ROWS = FIXTURES / "wikidata" / "facts_sui_5.json"
QID = "Q15768448"  # Alina Müller


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _fact(s, a, kind, value, text, source="wikidata", url=None, status="confirmed", quote=None, season=None):
    s.add(
        m.Fact(
            athlete_id=a.id,
            kind=kind,
            value=value,
            value_text=text,
            source=source,
            source_url=url or f"https://www.wikidata.org/wiki/{QID}",
            quote=quote,
            status=status,
            season=season,
            extractor=source,
        )
    )


def _stint(
    s, a, team, season_from, season_to=None, league=None, source="wikidata", url=None, status="confirmed"
):
    s.add(
        m.CareerStint(
            athlete_id=a.id,
            team_name=team,
            kind="club",
            league=league,
            season_from=season_from,
            season_to=season_to,
            role="player",
            source=source,
            source_url=url or f"https://www.wikidata.org/wiki/{QID}",
            quote=None if source == "wikidata" else f"Sie spielte für {team}.",
            status=status,
        )
    )


def _alina(s: Session) -> m.Athlete:
    a = repo.resolve_athlete(s, "Alina Müller")
    a.wikidata_qid = QID
    s.flush()
    return a


def _build(s: Session) -> m.Athlete:
    a = _alina(s)
    wp_de = "https://de.wikipedia.org/wiki/Alina_M%C3%BCller"
    _fact(s, a, "birth_date", {"date": "1998-03-12"}, "12.03.1998")
    _fact(s, a, "height_cm", {"number": 170}, "170 cm", status="conflict")
    _fact(
        s,
        a,
        "height_cm",
        {"number": 168},
        "168 cm",
        source="wikipedia",
        url=wp_de,
        status="conflict",
        quote="1,68 m",
    )
    _fact(s, a, "shoots", {"text": "L"}, "links")
    _fact(s, a, "position", {"text": "Stürmerin", "code": "F"}, "Stürmerin")
    _fact(s, a, "languages", {"text": "Deutsch"}, "Deutsch")
    _fact(s, a, "languages", {"text": "Englisch"}, "Englisch")
    _fact(s, a, "college", {"text": "Northeastern University"}, "Northeastern University")
    _fact(s, a, "nickname", {"text": "Ali"}, "Ali", source="news", url="")  # no link: never shown
    _stint(s, a, "ZSC Lions Frauen", 2014, 2017, league="SWHL A")
    _stint(s, a, "ZSC Lions Women", 2014, 2017, source="wikipedia", url=wp_de)  # same stint, 2nd source
    _stint(s, a, "Northeastern Huskies", 2017, 2022, league="NCAA")
    _stint(s, a, "Boston Fleet", 2023, None, league="PWHL")
    _stint(
        s, a, "HC Geheim", 2012, 2013, source="news", url="https://example.org/geheim", status="unconfirmed"
    )
    item = m.Item(
        source_type="profile",
        platform="wikipedia",
        source_name="Wikipedia (de)",
        url=wp_de,
        title="Alina Müller",
        text="Alina Müller (* 12. März 1998 in Winterthur) ist eine Schweizer Eishockeyspielerin, die seit 2023 "
        "bei Boston Fleet in der PWHL spielt.\nKarriere\nSie begann beim EHC Winterthur.",
        content_hash="x1",
        language="de",
    )
    en = m.Item(
        source_type="profile",
        platform="wikipedia",
        source_name="Wikipedia (en)",
        url="https://en.wikipedia.org/wiki/Alina_M%C3%BCller",
        title="Alina Müller",
        text="Alina Müller (born 12 March 1998) is a Swiss ice hockey player for the Boston Fleet of the PWHL.",
        content_hash="x2",
        language="en",
    )
    s.add_all([item, en])
    s.flush()
    s.add_all(
        [m.ItemAthlete(item_id=item.id, athlete_id=a.id), m.ItemAthlete(item_id=en.id, athlete_id=a.id)]
    )
    s.flush()
    return a


def _by_label(cd: dict) -> dict[str, dict]:
    return {f["label"]: f for f in cd["fakten"]}


def test_card_data_values_sources_conflicts(seeded):
    with Session(seeded) as s:
        a = _build(s)
        cd = card_data.card_data(s, a)
        f = _by_label(cd)
        assert f["Alter"]["value"] == f"{age(date(1998, 3, 12))} Jahre (geb. 12.03.1998)"
        assert f["Alter"]["sources"][0] == {
            "label": "Wikidata",
            "url": f"https://www.wikidata.org/wiki/{QID}",
            "quote": None,
        }
        assert f["Position"]["value"] == "Stürmerin" and f["Schusshand"]["value"] == "links"
        assert f["Nation"]["value"] == "Schweiz" and f["Nation"]["sources"]
        # conflict: preferred (Wikidata) value + "abweichend" with its own source
        assert f["Grösse"]["value"] == "170 cm"
        assert [(x["value"], x["source"]["label"]) for x in f["Grösse"]["abweichend"]] == [
            ("168 cm", "Wikipedia de")
        ]
        assert f["Sprachen"]["value"] == "Deutsch, Englisch"
        assert all(p["source"]["url"] for p in f["Sprachen"]["parts"])
        assert f["College / Studium"]["value"] == "Northeastern University"
        club = f["Aktueller Verein"]
        assert club["value"] == "Boston Fleet (PWHL)" and club["note"].startswith("laut Quelle, Stand ")
        assert "Spitzname" not in f  # unsourced: not shown
        # every shown value has a link
        for x in cd["fakten"]:
            assert x["sources"] or all(p["source"] for p in x["parts"])
        # career: by season, the same stint from two sources is one row with two links, unconfirmed apart
        seasons = [(r["saison"], r["team"]) for r in cd["karriere"]]
        assert seasons == [
            ("2014/15–2017/18", "ZSC Lions Frauen"),
            ("2017/18–2022/23", "Northeastern Huskies"),
            ("seit 2023/24", "Boston Fleet"),
        ]
        assert [x["label"] for x in cd["karriere"][0]["sources"]] == ["Wikidata", "Wikipedia de"]
        assert [r["team"] for r in cd["karriere_unbestaetigt"]] == ["HC Geheim"]
        # intro: home language (de for SUI) first, verbatim first paragraph
        assert cd["intro"]["label"] == "Wikipedia de"
        assert cd["intro"]["text"].startswith("Alina Müller (* 12. März 1998 in Winterthur)")
        assert "Karriere" not in cd["intro"]["text"]


def test_number_from_newest_roster(seeded):
    with Session(seeded) as s:
        a = _alina(s)
        _fact(
            s,
            a,
            "jersey_number",
            {"number": "25", "team": "Boston Fleet"},
            "Nummer 25 (Boston Fleet)",
            season=2023,
        )
        s.flush()
        # v8: national number from the seeded Olympic roster + club number from the fact
        assert _by_label(card_data.card_data(s, a))["Nummer"]["value"] == "#25 (Schweiz) · #25 (Boston Fleet)"
        ev_old = m.Event(
            key="wm-2025", name="WM 2025", kind="tournament", starts_at=datetime(2025, 4, 9, tzinfo=UTC)
        )
        ev_new = m.Event(
            key="wm-2027", name="WM 2026", kind="tournament", starts_at=datetime(2027, 4, 9, tzinfo=UTC)
        )
        s.add_all([ev_old, ev_new])
        s.flush()
        s.add_all(
            [
                m.EventEntry(
                    event_id=ev_old.id,
                    athlete_id=a.id,
                    jersey_number=7,
                    list_type="confirmed",
                    source="SIHF",
                    source_url="https://sihf.ch/kader-2025",
                ),
                m.EventEntry(
                    event_id=ev_new.id,
                    athlete_id=a.id,
                    jersey_number=25,
                    list_type="confirmed",
                    source="SIHF",
                    source_url="https://sihf.ch/kader-2026",
                ),
            ]
        )
        s.flush()
        nr = _by_label(card_data.card_data(s, a))["Nummer"]
        assert nr["value"] == "#25 · #25 (Boston Fleet)"  # newest roster (no team given) + club
        assert (
            nr["sources"][0]["label"] == "Kader WM 2026"
            and nr["sources"][0]["url"] == "https://sihf.ch/kader-2026"
        )


def test_card_from_wikidata_fixture(seeded):
    """Real Wikidata rows (recorded fixture) give a card with sourced values only."""
    with Session(seeded) as s:
        a = _alina(s)
        import_wikidata_facts(s, [a], rows=json.loads(WD_ROWS.read_text())["results"]["bindings"])
        cd = card_data.card_data(s, a)
        f = _by_label(cd)
        assert "Alter" in f and "Position" in f and cd["karriere"]
        for x in cd["fakten"]:
            assert x["sources"] or x["parts"]


def test_commons_photo_parsing():
    assert photo.parse_p18(COMMONS["wbgetclaims"]) == "Alina Müller Boston Fleet 2025.jpg"
    meta = photo.parse_imageinfo(COMMONS["imageinfo"])
    assert meta["author"] == "Jenn G" and meta["licence"] == "CC BY-SA 2.0"
    assert (
        meta["file_page"] == "https://commons.wikimedia.org/wiki/File:Alina_M%C3%BCller_Boston_Fleet_2025.jpg"
    )
    assert meta["thumb"].startswith("https://thumb.wikimedia.org/") and "utm_" not in meta["thumb"]
    assert meta["width"] == 320
    # HTML author is reduced to text; no licence -> no photo
    raw = json.loads(json.dumps(COMMONS["imageinfo"]))
    ext = raw["query"]["pages"][0]["imageinfo"][0]["extmetadata"]
    ext["Artist"]["value"] = '<a href="//commons.wikimedia.org/wiki/User:X">Jane &amp; Co</a>'
    assert photo.parse_imageinfo(raw)["author"] == "Jane & Co"
    del ext["LicenseShortName"]
    assert photo.parse_imageinfo(raw) is None
    assert photo.parse_p18({"claims": {}}) is None
    assert photo.parse_imageinfo({"query": {"pages": [{"missing": True}]}}) is None


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


class _Client:
    def __init__(self):
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        assert timeout is not None and timeout <= photo.TIMEOUT_S
        return _Resp(COMMONS["wbgetclaims"] if "wikidata" in url else COMMONS["imageinfo"])


def test_fetch_and_cache_photo(seeded):
    client = _Client()
    meta = photo.fetch_photo(QID, client=client)
    assert meta["licence"] == "CC BY-SA 2.0"
    assert client.calls[0][1]["entity"] == QID and client.calls[1][1]["iiurlwidth"] == 320
    with Session(seeded) as s:
        a = _alina(s)
        calls = []

        def fake(qid):
            calls.append(qid)
            return meta

        assert photo.ensure_photo(s, a, fetch=fake)["author"] == "Jenn G"
        assert photo.ensure_photo(s, a, fetch=fake)["author"] == "Jenn G"
        assert calls == [QID]  # stored as a fact, not fetched again
        f = s.scalar(select(m.Fact).where(m.Fact.athlete_id == a.id, m.Fact.kind == "photo"))
        assert f.source == "wikimedia" and f.source_url == meta["file_page"]
        # a person without a photo is asked once, then not again for RETRY_DAYS; errors give no photo
        b = repo.resolve_athlete(s, "Lara Stalder")
        b.wikidata_qid = "Q15743541"
        assert photo.ensure_photo(s, b, fetch=lambda q: None) is None
        assert not photo.photo_due(s, b)

        def boom(qid):
            raise TimeoutError("slow")

        c = repo.resolve_athlete(s, "Andrea Brändli")
        c.wikidata_qid = "Q29717053"
        assert photo.ensure_photo(s, c, fetch=boom) is None
        entry = s.get(m.CacheEntry, (photo.CACHE_SOURCE, f"athlete:{c.id}:Q29717053"))
        assert entry.fetched_at < datetime.now(UTC) - timedelta(days=photo.RETRY_DAYS - 2)  # retried in a day


def test_page_renders_card_without_network(seeded, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("no network in page render")

    monkeypatch.setattr(photo, "fetch_photo", no_network)
    with Session(seeded) as s:
        a = _build(s)
        photo.store_photo(s, a, photo.parse_imageinfo(COMMONS["imageinfo"]))
        pid = a.id
        other = repo.resolve_athlete(s, "Lara Stalder")
        other.wikidata_qid = "Q15743541"
        other_id = other.id
        s.commit()
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get(f"/person/{pid}").text
        assert 'class="steckbrief"' in html
        info = html[html.index("class='pg-main'") :]
        assert info.index("<h1>") < info.index("Stand: ") < info.index('class="steckbrief"')
        assert "Foto: Jenn G, " in html and "CC BY-SA 2.0" in html and "Wikimedia Commons</a>" in html
        assert "https://commons.wikimedia.org/wiki/File:Alina_M%C3%BCller_Boston_Fleet_2025.jpg" in html
        assert "abweichend: 168 cm" in html and "laut Quelle, Stand" in html
        assert "<summary>weitere Angaben (unbestätigt)</summary>" in html and "HC Geheim" in html
        assert "«Alina Müller (* 12. März 1998 in Winterthur)" in html
        assert "instagram" not in html[html.index('class="steckbrief"') : html.index("</section>")].lower()
        # a person without a stored photo gets a lazy htmx slot, the page itself never fetches
        other_html = c.get(f"/person/{other_id}").text
        assert f'hx-get="/person/{other_id}/foto"' in other_html


def test_photo_route_lazy(seeded, monkeypatch):
    monkeypatch.setattr(photo, "fetch_photo", lambda qid: photo.parse_imageinfo(COMMONS["imageinfo"]))
    with Session(seeded) as s:
        a = _alina(s)
        pid = a.id
        s.commit()
    with TestClient(build_web_app(SETTINGS)) as c:
        frag = c.get(f"/person/{pid}/foto", headers={"HX-Request": "true"}).text
        assert frag.startswith('<figure class="sb-photo">') and "Jenn G" in frag
        assert c.get("/person/999999/foto").text == ""
    with Session(seeded) as s:
        assert photo.stored_photo(s, pid)["licence"] == "CC BY-SA 2.0"


def test_markup_is_mobile_safe(seeded):
    with Session(seeded) as s:
        a = _build(s)
        photo.store_photo(s, a, photo.parse_imageinfo(COMMONS["imageinfo"]))
        html = card.profile_card(card_data.card_data(s, a))
    assert '<div class="sb-scroll"><table class="sb-career">' in html  # wide table scrolls inside the card
    assert re.search(r'<img [^>]*loading="lazy"[^>]*width="320" height="402"', html)  # no layout jump
    assert "style=" not in html  # all layout in CSS (tokens, media queries)
    from klugscheisser.web.html import CSS

    assert "@media (max-width:420px){.sb-photo" in CSS and ".sb-scroll{overflow-x:auto" in CSS
    assert "<script" not in html and "javascript:" not in html


def test_empty_card_says_so(seeded):
    with Session(seeded) as s:
        sport = s.scalar(select(m.Sport).where(m.Sport.key == "icehockey_women"))
        a = m.Athlete(
            sport_id=sport.id, full_name="Testa Leer", name_normalized="testa leer", nationality="SUI"
        )
        s.add(a)
        s.flush()
        html = card.profile_card(card_data.card_data(s, a))
    assert "Noch keine Steckbrief-Angaben mit Quelle gespeichert." in html and "hx-get" not in html
