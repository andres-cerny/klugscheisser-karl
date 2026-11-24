"""v6 step 7: name pronunciation — Wikimedia recording first, otherwise a stored computer voice; IPA + hint."""

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser import pronounce
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds

ON = Settings(_env_file=None, gemini_api_key="x", llm_daily_cost_cap_usd=1.0)


def _person(s, name="Klára Peslarová", nat="CZE", qid="Q1"):
    if s.scalar(select(m.Sport.id).limit(1)) is None:  # seed once per test (re-seeding takes seconds)
        load_all_seeds(s)
    sport = repo.get_sport(s, "icehockey_women")
    a = repo.upsert_athlete(s, sport, name, nationality=nat)
    a.wikidata_qid = qid
    s.flush()
    return a


def _wikidata(audio_file: str | None, ipa: str | None):
    def handler(request: httpx.Request) -> httpx.Response:
        if "wikidata.org" in request.url.host:
            claims = {}
            if audio_file:
                claims["P443"] = [{"mainsnak": {"datavalue": {"value": audio_file}}}]
            if ipa:
                claims["P898"] = [{"mainsnak": {"datavalue": {"value": ipa}}}]
            return httpx.Response(200, json={"entities": {"Q1": {"claims": claims}}})
        if request.url.path.endswith(".mp3"):
            return httpx.Response(200, content=b"MP3DATA", headers={"content-type": "audio/mpeg"})
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_language_from_nationality(db):
    with Session(db) as s:
        assert pronounce.language(_person(s)) == "cs"
        assert pronounce.language(_person(s, "Lara Stalder", "SUI", "Q2")) == "de-CH"
        assert pronounce.language(_person(s, "Hilary Knight", "USA", "Q3")) == "en"


def test_wikimedia_recording_first(db, monkeypatch):
    monkeypatch.setattr(
        pronounce, "write_hint", lambda *a: ("KLAA-ra PESCH-la-ro-wa", {"input": 1, "output": 1})
    )
    with Session(db) as s:
        a = _person(s)
        row = pronounce.info(s, ON, a, client=_wikidata("Cs-Klára Peslarová.ogg", "ˈklaːra ˈpɛslaːrovaː"))
        assert row.source == "wikimedia" and row.mime == "audio/mpeg" and row.audio == b"MP3DATA"
        assert row.source_url.startswith("https://commons.wikimedia.org/wiki/File:")
        assert row.ipa == "ˈklaːra ˈpɛslaːrovaː" and row.ipa_source_url.endswith("/Q1")
        assert row.hint == "KLAA-ra PESCH-la-ro-wa"
        assert pronounce.audio(s, ON, a) == (b"MP3DATA", "audio/mpeg")


def test_computer_voice_made_once_and_stored(db, monkeypatch):
    calls = []

    def fake_tts(settings, name, lang):
        calls.append((name, lang))
        return b"WAV", "audio/wav", {"input": 20, "output": 250}

    monkeypatch.setattr(pronounce, "tts", fake_tts)
    monkeypatch.setattr(pronounce, "write_hint", lambda *a: ("", {"input": 0, "output": 0}))
    with Session(db) as s:
        a = _person(s)
        pronounce.info(s, ON, a, client=_wikidata(None, None))
        assert pronounce.audio(s, ON, a)[0] == b"WAV"
        assert pronounce.audio(s, ON, a)[0] == b"WAV"
        assert calls == [("Klára Peslarová", "cs")]  # only the name is sent, once
        row = s.get(m.NamePronunciation, a.id)
        assert row.label == "Computerstimme (Tschechisch)"


def test_no_key_no_voice(db):
    with Session(db) as s:
        a = _person(s)
        pronounce.info(s, Settings(_env_file=None), a, client=_wikidata(None, None))
        try:
            pronounce.audio(s, Settings(_env_file=None), a)
            raise AssertionError("expected Unavailable")
        except pronounce.Unavailable:
            pass


def test_buttons_on_person_page(db):
    from klugscheisser.web.routes import build_web_app

    with Session(db) as s:
        aid = _person(s).id
        sui = _person(s, "Lara Stalder", "SUI", "Q2").id
        s.commit()
    app = build_web_app(Settings(_env_file=None, web_answer_engine="fake"))
    with TestClient(app) as c:
        html = c.get(f"/person/{aid}").text
        assert (
            '<h1>Klára Peslarová<span class="say"' in html
            and 'class="say"' in html
            and f"aussprache-{aid}" in html
        )
        assert 'class="say"' not in c.get(f"/person/{sui}").text  # German-language name: no computer voice
