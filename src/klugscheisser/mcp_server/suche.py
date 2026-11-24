"""Phase F (2026-10-25): the read tools of the new Perplexity tool set (switch MCP_NEW_TOOLS, see toolset_f.py).

No new search or answer logic here: every tool calls the functions the old tools and the chat use.

- `suche`: the evidence pack the chat writer would get, but Perplexity writes the answer (no Sonnet here):
  1. "Daten": model-free material from Karl's tables: the same shortcuts `frage` runs (staff, connections, games
     of a tournament, a team's games, tournament sheet), else the computed documents of the cross step
     (`cross.prosa_inputs`: facts across persons, relations, games), plus the persons' current status (injury
     report and what happened since) and stored profile facts;
  2. "Dokumente": evidence cards from the shared document search (`tools.search_tool`, cards on).
- `person`, `team`, `events`: the old portrait / roster / events tools, in parts (`teil`) instead of cut off.

Output: plain German Markdown without emojis (the old line prefixes like "📸 (von ihr selbst)" keep their words),
every fact with its source link and date."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from klugscheisser.config import Settings
from klugscheisser.mcp_server import cards
from klugscheisser.mcp_server.present import RULES, datenstand

PART_CHARS = 9000  # person/team/events: characters per part (the tool cap is 12k; nothing is cut off)
ONE_PART_MAX = 11000  # up to this a text stays one part
MATERIAL_MAX = 3500  # suche: characters of "Daten" before the cards
DOC_LINE_MAX = 500  # one material line (longer texts: dokument(id) when it is a stored document)
SUCHE_MAX_HITS = 20

_EMOJI = re.compile(cards._EMOJI.pattern[:-2] + "\u2300-\u23ff\u2b00-\u2bff\ufe0f\u200d]+")


def plain(text: str) -> str:
    """No emojis (each old prefix keeps its words: "📸 (von ihr selbst)" -> "(von ihr selbst)"), lines kept."""
    out = []
    for line in text.splitlines():
        t = _EMOJI.sub("", line)
        lead = len(t) - len(t.lstrip(" "))
        t = " " * lead + re.sub(r" {2,}", " ", t.strip())
        t = re.sub(r"^(\s*(?:- )?)\*\* +", r"\1**", t)  # "**📊 Statistik**" -> "**Statistik**"
        out.append(t.rstrip())
    return "\n".join(out)


def _without_head(text: str) -> list[str]:
    """A shortcut's text without its own Datenstand line and the per-call footer (suche has them once)."""
    lines = text.splitlines()
    skip = (RULES, "_Aussage, «Zitat» und Link gehören zusammen._")
    return [
        x.replace("frage('", "suche('").replace('frage("', 'suche("')
        for x in lines
        if not x.startswith("**Datenstand:**") and x.strip() not in skip
    ]


def _link(label: str, url: str) -> str:
    label = (label or "Quelle").strip("[] ")
    return f"[{label}]({url})" if url else label


def doc_line(d) -> str:
    """One material line: the stored/computed text, its source(s) with date, Vorsicht first."""
    body = cards.clean(d.text)
    more = ""
    if len(body) > DOC_LINE_MAX:
        body = body[:DOC_LINE_MAX].rsplit(" ", 1)[0] + " […]"
        if d.item_id:
            more = f" · Volltext: dokument({d.item_id})"
    if d.computed and d.basis:
        src = "berechnet aus: " + ", ".join(_link(b["quelle"], b["url"]) for b in d.basis[:4])
        if len(d.basis) > 4:
            src += f" +{len(d.basis) - 4}"
    else:
        src = "Quelle: " + _link(d.quelle, d.url)
    when = d.extra.get("published_at") if isinstance(d.extra, dict) else None
    if when is not None:
        from klugscheisser.retrieval.docs import age_de

        src += f" · {age_de(when)}"
    return f"- {'Vorsicht: ' if d.vorsicht else ''}{body} · {src}{more}"


def material(
    s: Session, frage: str, person_ids: list[int] | None, team_row, event_row
) -> tuple[list[str], list[int]]:
    """Model-free data for the question (see module doc). Returns the lines and the persons in scope."""
    from klugscheisser import staff, tournament
    from klugscheisser.answer import connections, cross, event_games, team_games
    from klugscheisser.answer.engine import (
        INJURY_Q,
        STRUCTURED,
        fact_docs,
        scope_athletes,
        status_docs,
    )
    from klugscheisser.answer.lookups import direct_lookup

    ds = datenstand(s)
    team_id = team_row.id if team_row is not None else None
    event_id = event_row.id if event_row is not None else None
    text = german_text(s, frage, person_ids, team_row)
    # the same model-free shortcuts as frage, in its order
    sres = (
        None
        if text is not None
        else staff.answer(s, frage, team_id=team_id, event_id=event_id, person_ids=person_ids)
    )
    if text is not None:
        pass
    elif sres is not None:
        text = staff.render(sres, ds)
    elif not person_ids:
        text = (
            connections.answer_text(s, frage, ds, team_row, event_row)
            or event_games.answer_text(s, frage, ds, team_row, event_row)
            or team_games.answer_text(s, frage, ds, team_row, event_row)
        )
        if text is None:
            tev = tournament.event_for_question(s, frage, event_id)
            if tev is not None:
                q = f"{frage} {team_row.name}" if team_row is not None else frage
                text = tournament.render(tournament.answer(s, tev, q), ds)
    lines = _without_head(text) if text else []
    athletes, note = scope_athletes(s, frage, person_ids, team_id, event_id)
    athletes = [a for a in athletes if a is not None]
    docs: list = []
    # cross step without its direct lookups: those expect the chat interpreter's question templates and fire on
    # free text too ("… 2026 das Kreuzband" -> all Olympic games); games and staff came from the shortcuts above
    if text is None and direct_lookup(s, frage, athletes) is None:
        pi = cross.prosa_inputs(s, frage, athletes)
        if pi is not None:
            docs += [d for d in pi.docs if d.computed or not d.item_id]  # stored documents come as cards
            note = pi.scope_note or note
    if text is None and (0 < len(athletes) <= 3 or INJURY_Q.search(frage)):
        docs += status_docs(s, athletes)  # injury report + what happened since (roster, games)
    if text is None and STRUCTURED.search(frage):  # age, number, position, followers …
        docs += fact_docs(s, athletes)
    seen: set[str] = set()
    doc_lines = []
    for d in docs:
        line = doc_line(d)
        if line not in seen:
            seen.add(line)
            doc_lines.append(line)
    if note and doc_lines:
        lines.append(f"_{note}_")
    lines += doc_lines
    out, size = [], 0
    for i, line in enumerate(lines):
        if size + len(line) > MATERIAL_MAX:
            out.append(f"_… {len(lines) - i} weitere Zeilen nicht gezeigt: enger fragen (person=…, team=…)._")
            break
        out.append(line)
        size += len(line) + 1
    return out, [a.id for a in athletes]


def german_text(s: Session, frage: str, person_ids: list[int] | None, team_row) -> str | None:
    """'Wer spricht Deutsch bei Tschechien?': the Deutsch labels of the asked persons, of the named national
    teams' newest tournament rosters, or of all other nations' rosters (facts/german.py; GERMAN_HINTS)."""
    from klugscheisser.config import get_settings
    from klugscheisser.db import models as m
    from klugscheisser.facts import german

    if not get_settings().german_hints or not german.wants_german(frage):
        return None
    if person_ids:
        hs = german.hints(s, [a for a in (s.get(m.Athlete, i) for i in person_ids) if a is not None])
        rows = [
            f"- {s.get(m.Athlete, aid).full_name}: {h.line()}"
            + (f" [{h.sources[0][0]}]({h.sources[0][1]})" if h.sources and h.sources[0][1] else "")
            for aid, h in hs.items()
            if h.line()
        ]
        return "\n".join(["**Deutsch als Interviewsprache**", *rows]) if rows else None
    from klugscheisser.answer.games import teams_in

    # "Deutsch" itself names Germany: German-speaking nations are never a scope here (they are not labelled)
    teams = (
        [team_row]
        if team_row is not None
        else [
            t
            for t in teams_in(s, frage)
            if t.kind == "national" and (t.country or "") not in german.GERMAN_NATIONS
        ]
    )
    lines = german.roster_lines(s, [t.id for t in teams] or None)
    return (
        "\n".join(["**Deutsch als Interviewsprache** (andere Nationen, neuestes Turnier-Aufgebot)", *lines])
        if lines
        else None
    )


def suche(
    s: Session,
    settings: Settings,
    frage: str,
    person: str | None = None,
    team: str | None = None,
    seit: str | None = None,
    quelle: str | None = None,
    anzahl: int = 8,
    neueste: bool = False,
) -> str:
    """The evidence pack for one question: Daten (from Karl's tables) + Dokumente (cards). No model writes."""
    from klugscheisser.db import repo
    from klugscheisser.mcp_server.tools import NotFound, resolve_team, search_tool

    anzahl = max(1, min(int(anzahl or 8), SUCHE_MAX_HITS))
    person_ids, team_row = None, None
    if person:
        a = repo.resolve_athlete(s, person)
        if a is None:
            raise NotFound(
                f"Keine Person gefunden für '{person}'. Andere Schreibweise versuchen oder ohne person suchen."
            )
        person_ids = [a.id]
    if team:
        team_row = resolve_team(s, team)
    data, scope_ids = material(s, frage, person_ids, team_row, None)
    result = search_tool(
        s,
        settings,
        frage,
        athlete=person,
        team=team,
        since=seit,
        source_type=quelle,
        limit=anzahl,
        cards=True,
        card_chars=cards.TARGET_CHARS - min(len("\n".join(data)), MATERIAL_MAX) // 2,
        newest=neueste,
    )
    ids = list(dict.fromkeys([*(result.get("athlete_ids") or []), *(person_ids or []), *scope_ids[:5]]))
    out = [f"**Datenstand:** {datenstand(s, ids or None)}"]
    akt = result.get("aktualisierung")
    if isinstance(akt, dict) and akt.get("hinweis"):
        out.append(f"_{akt['hinweis']}_")
    if data:
        out += ["", "**Daten aus Karls Tabellen** (berechnet/gespeichert, mit Quelle):", *data, ""]
    out.append("**Dokumente**")
    result["fuss"] = footer(result.get("fuss") or "", anzahl)
    rendered = cards.render(result)
    if neueste and rendered:
        rendered[0] = rendered[0].replace("nach Relevanz sortiert", "neueste zuerst")
    out += rendered
    out += ["", RULES]
    return plain("\n".join(out))


def footer(old: str, anzahl: int) -> str:
    """The cards footer with the Phase F tool names (keeps "Gezeigt: …" and a widened-date note)."""
    head = old.strip("_").split(" Mehr: ")[0].strip()
    more = (
        f"Mehr: dokument(id) für den ganzen Text; andere Wörter, person/team/seit oder anzahl (jetzt {anzahl}); "
        "Zahlen: stats; Spiele: events; Porträt: person."
    )
    return f"_{head} {more}_" if head else f"_{more}_"


# --- person / team / events: the old tools' data, in parts --------------------------------------------------


def parts(text: str, size: int = PART_CHARS) -> list[str]:
    """Split at line ends into parts of at most `size` characters (a longer single line is cut)."""
    out, cur, n = [], [], 0
    for line in text.splitlines():
        while len(line) > size:
            out.append("\n".join([*cur, line[:size]]) if cur else line[:size])
            cur, n, line = [], 0, line[size:]
        if n + len(line) + 1 > size and cur:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out or [""]


def paged(text: str, teil: int, call: str) -> str:
    """Part `teil` of a long tool text. Every part starts with the Datenstand line; a note names the next call.
    `call` has a `{teil}` placeholder, e.g. "person(name='Lara Stalder', teil={teil})"."""
    text = plain(text)
    head = ""
    if text.startswith("**Datenstand:**"):
        head, _, text = text.partition("\n")
    body = text.strip("\n")
    ps = [body] if len(body) <= ONE_PART_MAX else parts(body)
    teil = min(max(1, int(teil or 1)), len(ps))
    lines = [head] if head else []
    if len(ps) > 1:
        nxt = f" – weiter mit {call.format(teil=teil + 1)}" if teil < len(ps) else " (letzter Teil)"
        lines.append(f"_Teil {teil}/{len(ps)}{nxt}_")
    lines.append(ps[teil - 1])
    if len(ps) > 1 and teil < len(ps):
        lines.append(f"\n_Fortsetzung: {call.format(teil=teil + 1)}_")
    return "\n".join(lines)


def _rendered(s: Session, fn, *args, **kwargs) -> str:
    from klugscheisser.mcp_server.present import render_markdown

    return render_markdown(fn(s, *args, **kwargs))


def _q(v: Any) -> str:
    return repr(v) if isinstance(v, str) else str(v)


def person(s: Session, name: str, teil: int = 1) -> str:
    from klugscheisser.mcp_server import tools as t

    return paged(_rendered(s, t.get_athlete, name), teil, f"person(name={_q(name)}, teil={{teil}})")


def team(s: Session, name: str, wettbewerb: str | None = None, teil: int = 1) -> str:
    from klugscheisser.config import get_settings
    from klugscheisser.mcp_server import tools as t

    w = f", wettbewerb={_q(wettbewerb)}" if wettbewerb else ""
    kp = get_settings().team_key_players
    return paged(
        _rendered(s, t.get_team_roster, name, competition=wettbewerb, key_players=kp),
        teil,
        f"team(name={_q(name)}{w}, teil={{teil}})",
    )


def events(
    s: Session,
    turnier: str | None = None,
    team: str | None = None,
    gegner: str | None = None,
    letzte: int | None = None,
    seit: str | None = None,
    tage: int = 21,
    teil: int = 1,
) -> str:
    """turnier: the whole tournament sheet (in parts); else the event list / a team's last and next games /
    head-to-head (old list_events)."""
    from klugscheisser.mcp_server import tools as t

    if turnier:
        return paged(
            _rendered(s, t.get_event, turnier), teil, f"events(turnier={_q(turnier)}, teil={{teil}})"
        )
    args = {"tage": tage, "team": team, "gegner": gegner, "letzte": letzte, "seit": seit}
    call = ", ".join(f"{k}={_q(v)}" for k, v in args.items() if v is not None)
    return paged(
        _rendered(s, t.list_events, days=tage, team=team, last=letzte, since=seit, opponent=gegner),
        teil,
        f"events({call}, teil={{teil}})",
    )


__all__ = ["doc_line", "events", "material", "paged", "parts", "person", "plain", "suche", "team"]
