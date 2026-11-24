"""v4: overview of all data sources (page "Quellen") and the per-person "Datenquellen" box.

Facts here mirror docs/sources.md; keep both in sync when a source changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.domain.labels import TZ
from klugscheisser.handles.service import STATUS_LABEL_DE, reason_de


@dataclass
class SourceInfo:
    group: str  # "Instagram", "News", "Resultate", "Profile", "Web", "KI"
    name: str
    url: str
    takes: str  # what we take from it (German)
    active: bool
    terms: str  # terms / robots note (German)
    emoji: str = ""


def general_sources(settings: Settings) -> list[SourceInfo]:
    from klugscheisser.collectors.rss import DEFAULT_FEEDS

    f = settings.features
    ep_pause = _ep_pause()
    out = [
        SourceInfo(
            "Instagram",
            "Instagram via Apify (instagram-scraper)",
            "https://apify.com/apify/instagram-scraper",
            "Eigene Posts und Posts, in denen die Person markiert ist; Profil (Name, Bio). Keine Medien gespeichert.",
            f["apify"],
            "Nur öffentliche Profile; nur geprüfte Handles (verifiziert/wahrscheinlich). Tageslimit "
            f"${settings.apify_daily_cost_cap_usd:.2f}.",
            "📸",
        ),
        SourceInfo(
            "X",
            "X API (offiziell, pay-per-use)",
            "https://docs.x.com/x-api/getting-started/pricing",
            "Eigene Posts der Personen (nur geprüfte Handles) und offizielle Team-Accounts; Text, Datum, Link.",
            f.get("x", False),
            "Offizielle API; Anzeige mit @Handle, unverändertem Text, Datum und Link. Tageslimit "
            f"${settings.x_daily_cost_cap_usd:.2f}.",
            "𝕏",
        ),
        SourceInfo(
            "Instagram",
            "Meta Graph API (Business Discovery)",
            "https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery",
            "Eigene Posts von Business-/Creator-Konten (schneller, offiziell).",
            f["graph_business_discovery"],
            "Offizielle API; noch nicht eingerichtet.",
            "📸",
        ),
        SourceInfo(
            "Instagram",
            "Bright Data Web Scraper API (Instagram Posts + Profiles)",
            "https://docs.brightdata.com/products/scrapers/instagram/introduction",
            "Eigene Posts (inkl. Kollaborationen) und Profile; nach Graph, vor Apify. Keine markierten Posts.",
            f.get("brightdata", False),
            f"Gratis-Kontingent {settings.brightdata_free_records_per_month} Records/Monat; Karl stoppt bei "
            f"{settings.brightdata_monthly_record_cap} Records pro Monat.",
            "📸",
        ),
    ]
    for feed in DEFAULT_FEEDS:
        out.append(
            SourceInfo(
                "News",
                feed.name,
                feed.url,
                "Ganzer Artikel"
                if feed.fetch_article
                else (
                    "Ganzer Artikel (nur Testmodus)" if settings.news_fetch_all_articles else "Titel + Teaser"
                ),
                True,
                "Artikelseite erlaubt (robots.txt + AGB)"
                if feed.fetch_article
                else "AGB verbieten automatisches Lesen der Artikel: nur RSS-Teaser"
                + (" (Testmodus: ganze Artikel)" if settings.news_fetch_all_articles else ""),
                {"srf": "📺", "boulevard": "🍿", "media": "📰"}.get(str(feed.source_kind), "📰"),
            )
        )
    out += [
        SourceInfo(
            "Resultate",
            "SIHF (PostFinance Women's League)",
            "https://www.sihf.ch",
            "Resultate, Torschützinnen, Spielberichte",
            settings.sihf_data_enabled,
            "Nur persönlicher, nicht-kommerzieller Gebrauch.",
            "🏒",
        ),
        SourceInfo(
            "Resultate",
            "IIHF (WM)",
            "https://www.iihf.com",
            "Live-Stand und Resultate der WM",
            settings.iihf_realtime_enabled,
            "Keine Weiterverwendung ohne Zustimmung der IIHF (privater PoC).",
            "🏒",
        ),
        SourceInfo(
            "Resultate",
            "PWHL",
            "https://www.thepwhl.com",
            "Resultate der PWHL",
            settings.pwhl_enabled,
            "AGB verbieten automatisierte Skripte (privater PoC, geringe Menge).",
            "🏒",
        ),
        SourceInfo(
            "Resultate",
            "SDHL (stats.swehockey.se)",
            "https://stats.swehockey.se",
            "Resultate der SDHL",
            settings.sdhl_enabled,
            "Keine AGB gefunden; © Svenska Ishockeyförbundet.",
            "🏒",
        ),
        SourceInfo(
            "Resultate",
            "Leijonat (finnischer Verband, Nationalteams)",
            "https://tulospalvelu.leijonat.fi",
            "Spielplan und Resultate der Frauen-Nationalteams (Euro Hockey Tour), Torschützinnen",
            settings.leijonat_enabled,
            "Keine AGB und keine robots.txt gefunden; wenige Abfragen, 1 pro Sekunde.",
            "🏒",
        ),
        SourceInfo(
            "Resultate",
            "SIHF Frauen-Nationalteam (sihf.ch)",
            "https://www.sihf.ch/de/nationalteams/womens-national-team/programm/",
            "Programm und Resultate der Schweizerinnen (auch Testspiele); nur wenn keine andere Quelle das Spiel hat",
            settings.sihf_nt_enabled,
            "Nur persönlicher, nicht-kommerzieller Gebrauch; /umbraco/ (robots.txt) wird nie abgefragt.",
            "🏒",
        ),
        SourceInfo(
            "Profile",
            "Wikidata",
            "https://www.wikidata.org",
            "Personen, Geburtsdatum, Nation, Teams, Instagram-Handle (nur als Hinweis)",
            True,
            "CC0, frei nutzbar.",
            "🪪",
        ),
        SourceInfo(
            "Profile",
            "Wikipedia (de/en/fr)",
            "https://www.wikipedia.org",
            "Profiltext und Kader-Abschnitte",
            settings.profiles_enabled,
            "CC BY-SA; Quelle wird immer verlinkt.",
            "🪪",
        ),
        SourceInfo(
            "Profile",
            "College-Seiten (Sidearm)",
            "https://www.sidearmsports.com",
            "Spielerinnen-Bio von US-College-Teams",
            settings.profiles_enabled and settings.profiles_college_enabled,
            "robots.txt: Crawl-delay 30 s wird eingehalten.",
            "🪪",
        ),
        SourceInfo(
            "Profile",
            "EliteProspects",
            "https://www.eliteprospects.com",
            "Profilseiten der Spielerinnen: Steckbrief, Statistik pro Saison, Auszeichnungen, Transfers. "
            "Keine Fotos, keine Prognosen, keine Scouting-Berichte.",
            settings.ep_enabled and ep_pause is None,
            "Höflich: 1 Seite alle 6–9 s, nachts, max "
            f"{settings.ep_daily_page_cap}/Tag, {_ep_who(settings)}; Nutzungsbedingungen verbieten Scraping — privates Projekt, Ondřej trägt das Risiko "
            "(2026-10-04); robots.txt erlaubt /player/." + (f" Zurzeit {ep_pause}." if ep_pause else ""),
            "🪪",
        ),
        SourceInfo(
            "Web",
            "Brave Search API",
            "https://brave.com/search/api/",
            "Websuche für Lücken; Seiten nur gelesen, wenn robots.txt und AGB es erlauben; Namensprüfung mit Jev",
            settings.brave_api_key is not None,
            f"Bezahlt pro Anfrage; Limit {settings.brave_daily_cap}/Tag.",
            "🌐",
        ),
        SourceInfo(
            "KI",
            "Jev (TypeSafe)",
            "https://typesafe.ai",
            "Einordnung, Relevanz, heikle Themen, Namensgleichheit, Prüfung der Zitate",
            f["jev"],
            "Bekommt nur Texte aus den Quellen.",
            "🧠",
        ),
        SourceInfo(
            "KI",
            "Claude Sonnet (Anthropic)",
            "https://www.anthropic.com",
            "Antworten mit wörtlichen Zitaten, Fragen lesen, Fakten, Übersetzungen",
            f["claude"] and _text_provider(settings) == "claude",
            "Bezahlt pro Token.",
            "🧠",
        ),
        SourceInfo(
            "KI",
            "Google Gemini",
            "https://ai.google.dev",
            "Bild-/Reel-Analyse, Suche (Embeddings), Computerstimme"
            + (", Antworten mit wörtlichen Zitaten" if _text_provider(settings) == "gemini" else ""),
            f["gemini"],
            "Bezahlt pro Token.",
            "🧠",
        ),
    ]
    return out


def _text_provider(settings: Settings) -> str:
    from klugscheisser.llm.text import provider

    return provider(settings)


def _ep_who(settings: Settings) -> str:
    """Who the polite EP fetch reads (EP_SCOPE)."""
    from klugscheisser.collectors.eliteprospects.fetch import parse_scope

    kind, key = parse_scope(settings.ep_scope)
    if kind == "event":
        return f"nur die Spielerinnen der Kader von {key}, jede einmal pro Nacht"
    return "nur geänderte (Tier 1 täglich, Tier 2 wöchentlich, nach Spielen)"


def _ep_pause() -> str | None:
    """v11: "pausiert bis …" while Elite Prospects blocked us (source_state); None otherwise or without DB."""
    from klugscheisser.collectors.eliteprospects.fetch import pause_notice
    from klugscheisser.db.session import session_scope

    try:
        with session_scope() as s:
            return pause_notice(s)
    except Exception:
        return None


def person_sources(s: Session, athlete: m.Athlete) -> list[dict[str, Any]]:
    """Where the data about this person comes from, with links and counts."""
    rows: list[dict[str, Any]] = []
    for h in athlete.handles:
        if h.platform == "x":
            status = STATUS_LABEL_DE.get(h.status, "abgelehnt" if h.status == "rejected" else "ungeprüft")
            rows.append(
                {
                    "name": f"X @{h.handle}",
                    "url": f"https://x.com/{h.handle}",
                    "info": f"{status}" + (f" ({reason_de(h)})" if reason_de(h) else ""),
                    "aktiv": h.usable,
                }
            )
            continue
        if h.platform != "instagram":
            continue
        status = STATUS_LABEL_DE.get(h.status, "abgelehnt" if h.status == "rejected" else "ungeprüft")
        rows.append(
            {
                "name": f"Instagram @{h.handle}",
                "url": f"https://www.instagram.com/{h.handle}/",
                "info": f"{status}" + (f" ({reason_de(h)})" if reason_de(h) else ""),
                "aktiv": h.usable,
            }
        )
    if athlete.wikidata_qid:
        rows.append(
            {
                "name": f"Wikidata {athlete.wikidata_qid}",
                "url": f"https://www.wikidata.org/wiki/{athlete.wikidata_qid}",
                "info": "Stammdaten",
                "aktiv": True,
            }
        )
    if athlete.eliteprospects_id:
        ep = s.get(m.EpProfile, athlete.id)
        rows.append(
            {
                "name": "EliteProspects",
                "url": ep.url if ep else f"https://www.eliteprospects.com/player/{athlete.eliteprospects_id}",
                "info": f"Profilseite gelesen am {ep.fetched_at.astimezone(TZ).strftime('%d.%m.%Y')}"
                if ep
                else "Profilseite noch nicht gelesen",
                "aktiv": ep is not None,
            }
        )
    counts = s.execute(
        select(m.Item.platform, m.Item.source_name, func.count(), func.max(m.Item.url))
        .join(m.ItemAthlete)
        .where(m.ItemAthlete.athlete_id == athlete.id, m.Item.platform.in_(["wikipedia", "college", "web"]))
        .group_by(m.Item.platform, m.Item.source_name)
    ).all()
    labels = {"wikipedia": "Wikipedia", "college": "College-Seite", "web": "Webseiten"}
    for platform, source_name, n, url in counts:
        name = labels.get(platform, platform)
        if source_name and source_name.casefold() not in name.casefold():
            name = f"{name} ({source_name})"
        elif source_name:
            name = source_name
        rows.append(
            {
                "name": name,
                "url": url if n == 1 else None,
                "info": f"{n} Dokument{'e' if n != 1 else ''}",
                "aktiv": True,
            }
        )
    by_kind = s.execute(
        select(m.Item.platform, func.count())
        .join(m.ItemAthlete)
        .where(
            m.ItemAthlete.athlete_id == athlete.id, m.Item.platform.not_in(["wikipedia", "college", "web"])
        )
        .group_by(m.Item.platform)
    ).all()
    names = {"instagram": "📸 Instagram-Posts", "rss": "📰 News-Artikel"}
    for platform, n in by_kind:
        rows.append(
            {"name": names.get(platform, f"🏒 Resultate ({platform.upper()})"), "url": None, "info": str(n)}
        )
    if athlete.notes and "Hinzugefügt von Fabi" in athlete.notes:
        rows.append({"name": "Hinzugefügt", "url": None, "info": athlete.notes.splitlines()[-1]})
    return rows
