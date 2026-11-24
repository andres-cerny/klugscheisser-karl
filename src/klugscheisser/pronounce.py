"""v6 step 7: how to pronounce a player's name — 🔊 normal, 🐢 slow, IPA, German-readable hint.

1. A recording on Wikimedia Commons (Wikidata "pronunciation audio", P443), labelled with its Commons link.
2. Otherwise a computer voice (Gemini TTS) in the person's home language (from the nationality): only the name
   is sent, the language goes into the speech config. Made on first click and stored. 🐢 = slower playback.
3. IPA from Wikidata (P898) or the stored Wikipedia intro, with its link; the German hint ("PESCH-la-ro-wa") is
   written by the text model (v17: Claude Sonnet; Gemini with LLM_PROVIDER=gemini) and labelled
   "automatisch erzeugt, ohne Gewähr".

Nothing is generated in advance: only names someone actually clicks cost anything (cost cap: LLM daily cap).
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.ingest import costs

log = logging.getLogger(__name__)
COST_SOURCE = "llm:gemini-tts"
USER_AGENT = "klugscheisser-athlete/0.1 (private research PoC)"

LANGUAGE_DE = {
    "de-CH": "Schweizer Hochdeutsch",
    "de": "Deutsch",
    "en": "Englisch",
    "fr": "Französisch",
    "it": "Italienisch",
    "sv": "Schwedisch",
    "fi": "Finnisch",
    "cs": "Tschechisch",
    "sk": "Slowakisch",
    "da": "Dänisch",
    "nb": "Norwegisch",
    "sl": "Slowenisch",
    "hr": "Kroatisch",
    "pl": "Polnisch",
    "hu": "Ungarisch",
    "lv": "Lettisch",
    "ja": "Japanisch",
    "zh": "Chinesisch",
    "ko": "Koreanisch",
    "nl": "Niederländisch",
    "ru": "Russisch",
    "es": "Spanisch",
    "pt": "Portugiesisch",
}
ENGLISH = {"USA", "CAN", "GBR", "AUS", "NZL", "IRL"}
# German-language names get no computer voice: tested 2026-10-03 with five voices, de-DE and de-CH, the TTS
# model often read "Lara Stalder" / "Alina Müller" the English way. Fabi says these names natively anyway.
GERMAN_SPEAKING = {"SUI", "GER", "AUT", "LIE"}


def offered(nationality: str | None) -> bool:
    """Whether the 🔊 buttons are shown (a Wikimedia recording would still be used if one exists)."""
    return (nationality or "").upper() not in GERMAN_SPEAKING


IPA_CHARS = "ˈˌːəɛɔʃʒθðŋɲʎʁøœɐɪʊɑæʌɒɯɤʂʐɕʑçɟɣχ"
_IPA_RE = re.compile(r"[\[/]([^\]/\n]{2,80}?)[\]/]")


def language(a: m.Athlete) -> str:
    from klugscheisser.collectors.profiles.wikipedia import home_languages

    nat = (a.nationality or "").upper()
    if nat == "SUI":
        return "de-CH"
    if nat in ENGLISH:
        return "en"
    home = home_languages(nat)
    return home[0] if home else "en"


def language_de(code: str | None) -> str:
    return LANGUAGE_DE.get(code or "", code or "unbekannt")


# --- sources ------------------------------------------------------------------------------------------


def _claims(qid: str, client: httpx.Client) -> dict:
    r = client.get(
        "https://www.wikidata.org/w/api.php",
        params={"action": "wbgetentities", "ids": qid, "props": "claims", "format": "json"},
    )
    r.raise_for_status()
    return (r.json().get("entities", {}).get(qid) or {}).get("claims", {})


def _values(claims: dict, prop: str) -> list[str]:
    out = []
    for c in claims.get(prop, []):
        v = ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")
        if isinstance(v, str):
            out.append(v)
    return out


def commons_audio(filename: str, client: httpx.Client) -> tuple[bytes, str] | None:
    """The Commons file, preferably the MP3 transcode (Safari cannot play Ogg)."""
    name = filename.replace(" ", "_")
    h = hashlib.md5(name.encode()).hexdigest()
    mp3 = f"https://upload.wikimedia.org/wikipedia/commons/transcoded/{h[0]}/{h[:2]}/{quote(name)}/{quote(name)}.mp3"
    for url, mime in (
        (mp3, "audio/mpeg"),
        (f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}", ""),
    ):
        try:
            r = client.get(url)
            if r.status_code == 200 and r.content:
                return r.content, mime or r.headers.get("content-type", "audio/ogg").split(";")[0]
        except httpx.HTTPError:
            continue
    return None


def ipa_from_wikipedia(s: Session, a: m.Athlete) -> tuple[str, str] | None:
    """IPA in brackets/slashes in a stored Wikipedia text about her (e.g. «[ˈklaːra ˈpɛslaːrovaː]»)."""
    items = s.scalars(
        select(m.Item)
        .join(m.ItemAthlete)
        .where(m.ItemAthlete.athlete_id == a.id, m.Item.platform == "wikipedia")
        .order_by(m.Item.id)
    )
    for item in items:
        for mt in _IPA_RE.finditer((item.text or "")[:1500]):
            cand = mt.group(1).strip()
            if any(ch in IPA_CHARS for ch in cand):
                return cand, item.url
    return None


# --- computer voice and hint ------------------------------------------------------------------------------


def _client(settings: Settings):
    from google import genai

    return genai.Client(api_key=settings.gemini_api_key.get_secret_value())


# language -> BCP-47 code for the voice. German names use de-CH: in tests (2026-10-03) de-DE sometimes
# produced an English reading ("ˈlɑː.ɹə ˈstɑːl.dɚ"), de-CH did not.
VOICE_CODES = {
    "de-CH": "de-CH",
    "de": "de-CH",
    "en": "en-US",
    "fr": "fr-FR",
    "it": "it-IT",
    "sv": "sv-SE",
    "fi": "fi-FI",
    "cs": "cs-CZ",
    "sk": "sk-SK",
    "da": "da-DK",
    "nb": "nb-NO",
    "sl": "sl-SI",
    "hr": "hr-HR",
    "pl": "pl-PL",
    "hu": "hu-HU",
    "lv": "lv-LV",
    "ja": "ja-JP",
    "zh": "cmn-CN",
    "ko": "ko-KR",
    "nl": "nl-NL",
    "ru": "ru-RU",
    "es": "es-ES",
    "pt": "pt-PT",
}


def tts(settings: Settings, name: str, lang: str) -> tuple[bytes, str, dict]:
    """Only the name is sent: any instruction in the text is read aloud by the TTS model (tested). The language
    goes into the speech config instead. Slow playback is done by the browser."""
    from google.genai import types

    def call(code: str | None):
        client = _client(settings)  # keep a reference: the SDK closes the client when it is collected
        return client.models.generate_content(
            model=settings.gemini_tts_model,
            contents=name,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    language_code=code,
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=settings.gemini_tts_voice)
                    ),
                ),
            ),
        )

    code = VOICE_CODES.get(lang)
    try:
        r = call(code)
    except Exception:
        if code is None:
            raise
        r = call(None)  # language not offered by the voice: let it choose
    part = r.candidates[0].content.parts[0].inline_data
    u = r.usage_metadata
    usage = {"input": u.prompt_token_count or 0, "output": u.candidates_token_count or 0}
    return part.data, part.mime_type or "audio/wav", usage


HINT_SYSTEM = (
    "Schreibe eine Aussprachehilfe für deutschsprachige Sportmoderatorinnen: den Namen in deutscher Umschrift, "
    "Silben mit Bindestrich getrennt, betonte Silbe in GROSSBUCHSTABEN, Vor- und Nachname mit Leerzeichen "
    "(z. B. 'KLAA-ra PESCH-la-ro-wa'). Beachte die Betonung der Sprache: Tschechisch, Slowakisch, Ungarisch, "
    "Finnisch, Lettisch und Isländisch immer auf der ersten Silbe; Polnisch auf der vorletzten; lange Vokale "
    "(á, é) sind keine Betonung. Nur die Umschrift, nichts sonst."
)


def write_hint(settings: Settings, name: str, lang: str, ipa: str | None) -> tuple[str, dict]:
    """The hint is text: written by the text model (llm/text.py, v17: Claude Sonnet or Gemini)."""
    from klugscheisser.llm import text as llm_text

    prompt = f"Name: {name}\nSprache: {language_de(lang)}" + (f"\nLautschrift (IPA): [{ipa}]" if ipa else "")
    text, usage = llm_text.generate(
        settings, HINT_SYSTEM, prompt, role="answer", temperature=0.1, max_tokens=1000
    )
    return (text or "").strip().splitlines()[0][:200] if (text or "").strip() else "", usage


def _record(s: Session, settings: Settings, usage: dict, tts_call: bool, note: str) -> None:
    if tts_call:
        cost = (
            usage["input"] / 1e6 * settings.gemini_tts_input_usd_per_m
            + usage["output"] / 1e6 * settings.gemini_tts_output_usd_per_m
        )
        costs.record(s, COST_SOURCE, 1, cost, note)
    else:
        from klugscheisser.llm import text as llm_text

        costs.record(s, llm_text.cost_source(settings, "answer"), 1, llm_text.cost_usd(settings, usage), note)


class Unavailable(Exception):
    """No recording and no computer voice (no key or the daily cap is reached)."""


# --- public -----------------------------------------------------------------------------------------------


def info(
    s: Session, settings: Settings, a: m.Athlete, client: httpx.Client | None = None
) -> m.NamePronunciation:
    """The stored entry; the first call looks for a recording and IPA and writes the hint."""
    row = s.get(m.NamePronunciation, a.id)
    if row is not None:
        return row
    lang = language(a)
    row = m.NamePronunciation(
        athlete_id=a.id, language=lang, source="tts", label=f"Computerstimme ({language_de(lang)})"
    )
    own = client is None
    client = client or httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    try:
        if a.wikidata_qid:
            try:
                claims = _claims(a.wikidata_qid, client)
                ipas = _values(claims, "P898")
                if ipas:
                    row.ipa = ipas[0][:200]
                    row.ipa_source_url = f"https://www.wikidata.org/wiki/{a.wikidata_qid}"
                files = _values(claims, "P443")
                if files and (got := commons_audio(files[0], client)) is not None:
                    row.audio, row.mime = got
                    row.source = "wikimedia"
                    row.label = "Aufnahme (Wikimedia Commons)"
                    row.source_url = (
                        f"https://commons.wikimedia.org/wiki/File:{quote(files[0].replace(' ', '_'))}"
                    )
            except httpx.HTTPError as exc:
                log.warning("wikidata pronunciation lookup failed for %s: %s", a.wikidata_qid, exc)
    finally:
        if own:
            client.close()
    if row.ipa is None and (found := ipa_from_wikipedia(s, a)) is not None:
        row.ipa, row.ipa_source_url = found
    from klugscheisser.llm import text as llm_text

    if (
        llm_text.available(settings)
        and costs.remaining(s, llm_text.cost_source(settings, "answer"), settings.llm_daily_cost_cap_usd) > 0
    ):
        try:
            row.hint, usage = write_hint(settings, a.full_name, lang, row.ipa)
            _record(s, settings, usage, False, f"aussprache-hinweis {a.id}")
        except Exception as exc:
            log.warning("pronunciation hint failed for %s: %s", a.id, str(exc)[:160])
    s.add(row)
    s.flush()
    return row


def audio(s: Session, settings: Settings, a: m.Athlete) -> tuple[bytes, str]:
    """The recording or the computer voice (made on first request, then stored). 🐢 = the browser plays it
    slower (the TTS model reads any speed instruction aloud)."""
    row = info(s, settings, a)
    if row.audio:
        return row.audio, row.mime
    if not offered(a.nationality):
        raise Unavailable("Deutschsprachiger Name: keine Computerstimme.")
    if not settings.features.get("gemini"):
        raise Unavailable("Keine Computerstimme (kein Gemini-Schlüssel).")
    if costs.remaining(s, COST_SOURCE, settings.llm_daily_cost_cap_usd) <= 0:
        raise Unavailable("Tageslimit für die Computerstimme erreicht.")
    data, mime, usage = tts(settings, a.full_name, row.language or "en")
    _record(s, settings, usage, True, f"aussprache {a.id}")
    row.audio, row.mime = data, mime
    s.flush()
    return data, mime
