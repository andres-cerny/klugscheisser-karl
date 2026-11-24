"""v7: project page — briefing (Fabi's text + the learned part she approves), settings, connector address."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from klugscheisser import briefing, projects
from klugscheisser.db import models as m
from klugscheisser.domain.labels import date_de
from klugscheisser.web.html import e

SCHEDULE_KEYS = {"nightly_cap_usd", "nightly_hour", "news_poll_minutes"}
PLACEHOLDER = (
    "Wofür ist dieses Projekt? Welcher Anlass, welche Teams und Personen sind wichtig? Was willst du "
    "(z. B. «Persönliches und Geschichten vor Interviews, keine Taktik»), welcher Ton, was vermeiden?"
)


def settings_values() -> dict[str, Any]:
    from klugscheisser.config import get_settings

    cur = get_settings()
    return {key: getattr(cur, key) for key in projects.EDITABLE}


def save_settings(s: Session, p: projects.Project, form: dict[str, str]) -> list[str]:
    """Store changed values in the project's database. Returns error texts (empty = all saved)."""
    errors = []
    for key, (label, typ) in projects.EDITABLE.items():
        raw = (form.get(key) or "").strip().replace(",", ".")
        if raw == "":
            continue
        try:
            value = typ(float(raw)) if typ is int else typ(raw)
        except ValueError:
            errors.append(f"{label}: «{raw}» ist keine Zahl")
            continue
        if value < 0 or (key == "nightly_hour" and value > 23):
            errors.append(f"{label}: ungültiger Wert")
            continue
        row = s.get(m.ProjectSetting, key) or m.ProjectSetting(key=key)
        row.value = value
        s.merge(row)
    s.flush()
    projects.clear_cache(p.key)
    return errors


def page_body(s: Session, host: str, saved: str = "", errors: list[str] | None = None) -> str:
    p = projects.current()
    cur = briefing.current(s)
    sug = briefing.open_suggestion(s)
    out = [f"<h1>{e(p.name)}</h1>"]
    if not p.active:
        jobs = "<strong>Archiviert</strong>: keine Jobs, die Daten bleiben."
    elif p.scheduled_jobs:
        jobs = "Hintergrund-Jobs (News, nächtliche Aktualisierung) laufen."
    else:
        jobs = "Nur auf Anfrage: keine Hintergrund-Jobs, Personen kommen über «hinzufügen»."
    out.append(f"<p class='meta'>Eigene Datenbank für dieses Projekt. {jobs}</p>")
    if saved:
        out.append(f"<div class='progress'>{e(saved)}</div>")
    for err in errors or []:
        out.append(f"<p class='err'>{e(err)}</p>")

    # briefing
    version = (
        f"Version {cur.id} vom {e(date_de(cur.created_at, True))}"
        if cur is not None
        else "Noch kein Briefing"
    )
    out.append(
        "<h2>Briefing</h2><p class='small'>Wird bei jeder Antwort mitgegeben (Auswahl, Gewichtung, Ton). "
        "Es ist nie eine Quelle, und die Regeln (jede Aussage mit Zitat und Link) gehen immer vor. "
        f"{version}.</p>"
        "<form method='post' action='/projekt/briefing'>"
        f"<textarea class='briefing' name='text' data-mic placeholder='{e(PLACEHOLDER)}'>"
        f"{e(cur.text if cur else '')}</textarea>"
        "<p><button class='btn primary' type='submit'>Briefing speichern</button></p></form>"
    )

    # learned part
    out.append("<h2>Was wir gelernt haben</h2>")
    out.append(
        "<p class='small'>Aus deinen Fragen und Bewertungen. Kommt nur ins Briefing, wenn du es übernimmst.</p>"
    )
    if sug is not None:
        b = sug.basis or {}
        out.append(
            "<div class='proposal'><strong>Neuer Vorschlag</strong>"
            f"<div class='small'>Aus {e(b.get('fragen', 0))} Fragen seit {e(b.get('seit', ''))}, "
            f"Bewertungen: {e(', '.join(f'{k} {v}' for k, v in (b.get('bewertungen') or {}).items()) or 'keine')}"
            "</div>"
            f"<form method='post' action='/projekt/vorschlag/{sug.id}'>"
            f"<textarea name='text' data-mic rows='6'>{e(sug.text)}</textarea>"
            "<div class='yesno'><button class='btn primary' name='aktion' value='annehmen'>Übernehmen</button>"
            "<button class='btn' name='aktion' value='ablehnen'>Ablehnen</button></div>"
            "<div class='small'>Du kannst den Text vor dem Übernehmen ändern.</div></form></div>"
        )
    learned = cur.learned if cur else ""
    out.append(
        "<form method='post' action='/projekt/gelernt'>"
        f"<textarea name='learned' rows='5' data-mic placeholder='Noch nichts übernommen.'>{e(learned)}</textarea>"
        "<p><button class='btn' type='submit'>Gelerntes speichern</button></p></form>"
    )
    if sug is None:
        out.append(
            "<form method='post' action='/projekt/vorschlag'><p><button class='btn' type='submit'>"
            "Jetzt Vorschlag erstellen</button> <span class='small'>(sonst automatisch einmal pro Woche)"
            "</span></p></form>"
        )

    hist = briefing.history(s)
    if len(hist) > 1:
        rows = "".join(
            f"<li>Version {h.id} · {e(date_de(h.created_at, True))} · {e(h.created_by)}"
            f"{' · ' + e(h.note) if h.note else ''}</li>"
            for h in hist
        )
        out.append(
            f"<details><summary class='small'>Frühere Versionen</summary><ul class='lines'>{rows}</ul></details>"
        )

    # settings
    values = settings_values()
    fields = "".join(
        f"<label class='small'>{e(label)}<input type='text' inputmode='decimal' name='{key}' "
        f"value='{e(values[key])}'></label>"
        for key, (label, _) in projects.EDITABLE.items()
        if p.scheduled_jobs or key not in SCHEDULE_KEYS  # no schedules in on-demand projects
    )
    out.append(
        "<h2>Einstellungen</h2><p class='small'>Kostendeckel und Zeiten nur für dieses Projekt.</p>"
        f"<form method='post' action='/projekt/einstellungen'>{fields}"
        "<p><button class='btn' type='submit'>Einstellungen speichern</button></p></form>"
    )

    # connector
    out.append(
        "<h2>Perplexity</h2><p class='small'>Eigene Connector-Adresse pro Projekt (ein Perplexity-Space pro "
        "Projekt):</p>"
        f"<p><code>https://{e(host)}/mcp/{e(p.key)}</code></p>"
        f"<p class='small'>App-Link direkt in dieses Projekt: <code>https://{e(host)}/p/{e(p.key)}/</code></p>"
    )
    return "".join(out)
