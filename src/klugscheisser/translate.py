"""v8: German translations for quotes and summaries from sources that are neither German nor English.

Fabi reads German and English. The original stays (it is the proof, the link points to it); the German text is
shown below it, marked "automatisch übersetzt". Made once per text (v17: Claude Sonnet, or Gemini Flash-Lite
with LLM_PROVIDER=gemini; several texts per call), stored in `translations`, cost in the ledger as
"llm:claude-translate" / "llm:gemini-translate".
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)
READABLE = {"de", "en", "gsw"}
COST_SOURCE = "llm:gemini-translate"  # with Gemini; see cost_source()
BATCH = 25
LANG_DE = {
    "cs": "Tschechisch",
    "fi": "Finnisch",
    "sv": "Schwedisch",
    "da": "Dänisch",
    "fr": "Französisch",
    "it": "Italienisch",
    "ja": "Japanisch",
    "no": "Norwegisch",
    "nb": "Norwegisch",
    "sk": "Slowakisch",
    "hu": "Ungarisch",
    "ru": "Russisch",
    "pl": "Polnisch",
    "rm": "Rätoromanisch",
}
SYSTEM = (
    "Du übersetzt kurze Texte (Zitate aus Wikipedia, Zeitungen, Social Media über Sportlerinnen) ins Deutsche. "
    "Wörtlich und vollständig, nichts weglassen, nichts hinzufügen, keine Erklärungen. Namen, Vereine und Zahlen "
    "unverändert. Gib für jeden Text genau eine Übersetzung zurück, in derselben Reihenfolge."
)


def system() -> str:
    """SYSTEM of the current project: "über Sportler" in men's projects (floorball), else unchanged."""
    from klugscheisser.sports import current_spec

    return SYSTEM.replace("über Sportlerinnen)", "über Sportler)") if current_spec().men else SYSTEM


SCHEMA = {
    "type": "object",
    "properties": {"uebersetzungen": {"type": "array", "items": {"type": "string"}}},
    "required": ["uebersetzungen"],
}


def key_of(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def needs(lang: str | None) -> bool:
    return bool(lang) and lang.split("-")[0].lower() not in READABLE


def lang_of_url(url: str | None) -> str | None:
    host = urlsplit(url or "").hostname or ""
    if host.endswith("wikipedia.org"):
        return host.split(".")[0]
    return None


# outlets whose language the URL gives away (French/Italian Switzerland, Nordic and Czech media)
DOMAIN_LANG = {
    "rts.ch": "fr", "lematin.ch": "fr", "24heures.ch": "fr", "letemps.ch": "fr", "tdg.ch": "fr",
    "lenouvelliste.ch": "fr", "arcinfo.ch": "fr", "laliberte.ch": "fr", "lequipe.fr": "fr",
    "planetehockey.com": "fr", "rsi.ch": "it", "laregione.ch": "it", "cdt.ch": "it", "ticinonews.ch": "it",
    "rtr.ch": "rm", "yle.fi": "fi", "iltalehti.fi": "fi", "is.fi": "fi", "hs.fi": "fi", "jatkoaika.com": "fi",
    "svt.se": "sv", "expressen.se": "sv", "aftonbladet.se": "sv", "dn.se": "sv", "hockeysverige.se": "sv",
    "idnes.cz": "cs", "isport.blesk.cz": "cs", "blesk.cz": "cs", "denik.cz": "cs", "irozhlas.cz": "cs",
    "ct24.ceskatelevize.cz": "cs", "sport.cz": "cs", "hokej.cz": "cs", "dr.dk": "da", "tv2.dk": "da",
    "bt.dk": "da", "ekstrabladet.dk": "da",
}  # fmt: skip


def guess_lang(url: str | None) -> str | None:
    """Language from the URL: Wikipedia subdomain, known outlets, or a /fr/ /it/ path (e.g. 20min.ch/fr/)."""
    if lang := lang_of_url(url):
        return lang
    parts = urlsplit(url or "")
    host = (parts.hostname or "").removeprefix("www.")
    for domain, lang in DOMAIN_LANG.items():
        if host == domain or host.endswith("." + domain):
            return lang
    first = parts.path.strip("/").split("/", 1)[0].lower()
    return first if first in ("fr", "it", "fi", "sv", "cs", "da", "ja") else None


def item_lang(item: m.Item) -> str | None:
    """Language of a stored document: its own field, the X post's language, or a guess from the URL."""
    return (
        item.language
        or ((item.raw or {}).get("lang") if item.platform == "x" else None)
        or (item.raw or {}).get("language")
        or guess_lang(item.url)
    )


def cached(s: Session, text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    row = s.get(m.Translation, key_of(text))
    return row.text_de if row else None


def label(lang: str | None) -> str:
    return LANG_DE.get((lang or "").split("-")[0].lower(), (lang or "").upper() or "Originalsprache")


def cost_source(settings) -> str:
    from klugscheisser.llm import text as llm_text

    return llm_text.cost_source(settings, "translate")


def _model_call(settings):
    def call(texts: list[str]) -> tuple[list[str], dict]:
        from klugscheisser.llm import text as llm_text

        numbered = "\n\n".join(f"[{i + 1}] {t}" for i, t in enumerate(texts))
        out, usage = llm_text.generate(
            settings, system(), f"Texte:\n{numbered}", schema=SCHEMA, role="answer", temperature=0.0
        )
        return list(out.get("uebersetzungen") or []), usage

    return call


def ensure(s: Session, settings, texts: Iterable[tuple[str, str | None]], llm=None, budget_left=None) -> int:
    """Translate (text, lang) pairs that need it and are not cached yet. Returns the number made.
    `budget_left()`: USD a caller's run may still spend (deep --cap); checked before every batch, nothing more
    is translated once it is used up (2026-11-24: tier 2 with cap $3 spent $4.08 because this step ignored it)."""
    from klugscheisser.ingest import costs

    todo: dict[str, tuple[str, str | None]] = {}
    for text, lang in texts:
        if text and text.strip() and needs(lang):
            k = key_of(text)
            if k not in todo and s.get(m.Translation, k) is None:
                todo[k] = (text.strip(), lang)
    if not todo:
        return 0
    from klugscheisser.llm import text as llm_text

    if llm is None and not llm_text.available(settings):
        return 0
    call = llm or _model_call(settings)
    model_name = llm_text.model(settings, "answer")
    made = 0
    items = list(todo.items())
    for i in range(0, len(items), BATCH):
        chunk = items[i : i + BATCH]
        if budget_left is not None and budget_left() <= 0:
            log.info("translation: budget used up, %s texts left untranslated", len(items) - i)
            break
        try:
            out, tokens = call([t for _, (t, _) in chunk])
        except Exception as exc:
            log.warning("translation failed: %s", str(exc)[:200])
            break
        if len(out) != len(chunk):
            log.warning("translation: %s texts, %s answers - skipped", len(chunk), len(out))
            continue
        for (k, (_, lang)), de in zip(chunk, out, strict=True):
            if de.strip():
                s.merge(m.Translation(key=k, source_lang=lang, text_de=de.strip(), model=model_name))
                made += 1
        usd = llm_text.cost_usd(settings, tokens)
        costs.record(s, cost_source(settings), len(chunk), usd, f"{len(chunk)} Texte")
        s.flush()
    return made


def german(s: Session, settings, text: str | None, lang: str | None, create: bool = True) -> str | None:
    """German version of one text (None when not needed or not available)."""
    if not text or not needs(lang):
        return None
    hit = cached(s, text)
    if hit is None and create:
        ensure(s, settings, [(text, lang)])
        hit = cached(s, text)
    return hit


def backfill(
    s: Session, settings, athlete_ids: list[int] | None = None, echo=print, budget_left=None
) -> dict:
    """Quotes of stored facts (incl. fun facts), card intros and document summaries/titles that need German."""
    from klugscheisser.mcp_server.present import first_sentence
    from klugscheisser.web.card_data import first_paragraph

    pairs: list[tuple[str, str | None]] = []
    fq = select(m.Fact).where(m.Fact.quote.is_not(None))
    if athlete_ids:
        fq = fq.where(m.Fact.athlete_id.in_(athlete_ids))
    facts = list(s.scalars(fq))
    for f in facts:
        lang = lang_of_url(f.source_url)
        if lang is None and f.source_item_id:
            item = s.get(m.Item, f.source_item_id)
            lang = item_lang(item) if item else None
        pairs.append((f.quote, lang))
    iq = select(m.Item).where(m.Item.text.is_not(None))
    if athlete_ids:
        iq = iq.join(m.ItemAthlete).where(m.ItemAthlete.athlete_id.in_(athlete_ids))
    for item in s.scalars(iq):
        lang = item_lang(item)
        if not needs(lang):
            continue
        pairs.append((first_sentence(item.text), lang))
        if item.title:
            pairs.append((item.title, lang))
        if item.platform == "wikipedia" and (intro := first_paragraph(item.text)):
            pairs.append((intro, lang))
    n = ensure(s, settings, pairs, budget_left=budget_left)
    echo(f"Übersetzungen: {n} neu ({len(pairs)} Texte geprüft)")
    return {"neu": n, "geprueft": len(pairs)}


def fact_lang(s: Session, f: m.Fact) -> str | None:
    lang = lang_of_url(f.source_url)
    if lang is None and f.source_item_id:
        item = s.get(m.Item, f.source_item_id)
        lang = item_lang(item) if item else None
    return lang


def for_facts(s: Session, settings, facts: list[m.Fact], create: bool = True) -> dict[str, str]:
    """{quote: German} for the facts whose quote needs a translation (made now when `create`)."""
    pairs = [(f.quote, fact_lang(s, f)) for f in facts if f.quote]
    pairs = [(q, lang) for q, lang in pairs if needs(lang)]
    if create and pairs:
        try:
            ensure(s, settings, pairs)
        except Exception as exc:
            log.warning("fact translation failed: %s", exc)
    return {q: de for q, _ in pairs if (de := cached(s, q))}
