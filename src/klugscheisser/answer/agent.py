"""v14: second stage of the web chat — an agent that searches deeper when the fast answer is not enough
(docs/build-plan-v14-agent.md).

Jev judges (is the fast answer enough? which documents matter? is the material enough?), the model only plans
the searches and writes (Claude Sonnet or Gemini, llm/text.py). All results are documents with their source;
the writer's checks (verbatim quotes, `quote_in`, Jev support per sentence) stay unchanged — the agent can search more and combine, never invent."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.llm.text import add_usage
from klugscheisser.llm.words import fill

log = logging.getLogger(__name__)

TOOLS = ["datenpaket", "spielberichte", "statistik", "dokumente"]
JEV_USD_PER_M = 0.042
MAX_DOCS = 25
DATA_PACKAGE_PERSONS = 45

PLAN_SYSTEM = (
    "Du planst die Recherche für eine Frage zu {sport_topic}. Eine erste, schnelle Suche hat die Frage "
    "nicht (genug) beantwortet. Gib 2 bis 4 Suchvarianten für unsere Dokumentensuche (andere Wörter, Synonyme, "
    "Fachbegriffe, deutsch und englisch, Namen ausgeschrieben, z. B. '200. Länderspiel Stalder', 'Stalder caps "
    "Switzerland') und wähle die Werkzeuge:\n"
    "- datenpaket: alles Gespeicherte zu den Personen (Profil, Länderspiele, Debüt, Position, Team, Vertrag …)\n"
    "- spielberichte: gespeicherte Spielberichte mit Toren und Assists dieser Personen\n"
    "- statistik: Saison- und Karrierestatistik (Spiele, Tore, Punkte, Nationalteam)\n"
    "- dokumente: Dokumentensuche mit den Suchvarianten (Artikel, Posts, Wikipedia)\n"
    "Wähle alle, die helfen können. Beantworte die Frage NICHT."
)
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "suchen": {"type": "array", "items": {"type": "string"}},
        "werkzeuge": {"type": "array", "items": {"type": "string", "enum": TOOLS}},
    },
    "required": ["suchen", "werkzeuge"],
}
GATE_QUESTION = {
    "type": "noul",
    "instructions": "Beantwortet die Antwort die Frage zumindest zu einem wesentlichen Teil mit konkreten Angaben?",
    "criteria": {
        "true": "die Antwort enthält konkrete Angaben (Namen, Zahlen, Fakten), die das Gefragte betreffen",
        "false": "keine Antwort, nur 'nichts gefunden', nur Allgemeines oder etwas anderes als gefragt",
    },
}
ENOUGH_QUESTION = {
    "type": "noul",
    "instructions": "Enthalten diese Auszüge Angaben, mit denen sich die Frage zumindest teilweise beantworten lässt?",
    "criteria": {
        "true": "mindestens ein Auszug nennt etwas, das direkt zur Frage gehört",
        "false": "die Auszüge handeln von anderem oder sind zu allgemein",
    },
}


@dataclass
class AgentRun:
    rounds: int = 0
    searches: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    docs: list = field(default_factory=list)
    llm_usage: dict = field(default_factory=lambda: {"input": 0, "output": 0})  # planner calls (llm/text.py)
    jev_tokens: int = 0

    def cost(self, settings) -> float:
        from klugscheisser.llm.text import cost_usd

        return cost_usd(settings, self.llm_usage) + self.jev_tokens / 1e6 * JEV_USD_PER_M


# --- Jev judgements -------------------------------------------------------------------------------------------


def _jev(settings, state: str, question: dict, run: AgentRun, jev=None) -> float | None:
    """One yes/no judgement; None when Jev is off or fails (the caller decides the default)."""
    if jev is not None:
        return jev(state, question)
    if not settings.features["jev"]:
        return None
    from klugscheisser.classify.jev import JevClient, noul_probability

    try:
        client = JevClient(settings)
        ans = client.ask(state, {"q": question})
        run.jev_tokens += int(client.usage.get("input_tokens", 0))
        return noul_probability(ans["q"])
    except Exception as exc:
        log.warning("agent jev failed: %s", str(exc)[:160])
        return None


def shown_text(result) -> str:
    parts = []
    for b in result.antwort or []:
        parts += [x.text for x in b.saetze]
        parts += [" | ".join(z["zellen"]) for z in b.zeilen]
    return "\n".join(parts)


def needs_agent(settings, question: str, result, run: AgentRun, jev=None) -> bool:
    """Stage 2 when stage 1 has no answer, or Jev says it does not answer the question. Answers computed from
    data (stats tables, direct answers: no model involved) and ask-backs are kept."""
    if result.rueckfrage:
        return False
    text = shown_text(result)
    if not text.strip():
        return True
    if result.statements and all(getattr(st, "berechnet", False) for st in result.statements):
        return False  # computed straight from our data
    p = _jev(settings, f"Frage: {question}\nAntwort: {text[:1500]}", GATE_QUESTION, run, jev)
    return p is not None and p < 0.5


# --- tools (each returns documents with their source) ---------------------------------------------------------


GROUP_KINDS = ("appearances", "current_team", "position", "jersey_number", "national_team_debut", "college")


def group_tables(s: Session, athletes: list) -> list:
    """Context for a group (team/event page, "the national team"): ONE computed table document per field —
    "Länderspiele: Alina Müller 94 (Wikipedia); …; ohne Angabe: …" — so a list question cites one document
    instead of collecting 40. Each value keeps its source in the basis links."""
    from klugscheisser.answer.cross import _computed_doc
    from klugscheisser.facts.catalog import FACT_KINDS
    from klugscheisser.facts.queries import facts_for

    athletes = [a for a in athletes if a.role == "athlete"]  # player tables: no coaches/staff
    ids = [a.id for a in athletes]
    names = {a.id: a.full_name for a in athletes}
    label_de = {"appearances": "Länderspiele/Einsätze"}
    out = []
    ep_games = _national_games(s, ids)
    if ep_games:
        values = "; ".join(f"{names[i]}: {n}" for i, n in sorted(ep_games.items(), key=lambda kv: -kv[1]))
        body = (
            f"Gruppe ({len(ids)} Personen) – Nationalteam-Spiele laut Elite Prospects (nur dort erfasste "
            f"Turniere und Saisons, unvollständig; keine offizielle Länderspielzahl): {values}."
        )
        missing = [names[i] for i in ids if i not in ep_games]
        if missing:
            body += f" Ohne Elite-Prospects-Daten ({len(missing)}): {', '.join(missing)}."
        basis = [{"quelle": "Elite Prospects", "url": u} for u in _ep_urls(s, list(ep_games))]
        out.append(
            _computed_doc(
                body, "Gruppe, Nationalteam-Spiele (Elite Prospects)", list(map(names.get, ep_games)), basis
            )
        )
    for kind in GROUP_KINDS:
        rows = facts_for(s, ids, kind)
        if not rows:
            continue
        have = {r.athlete_id for r in rows}
        missing = [names[i] for i in ids if i not in have]
        title = label_de.get(kind, FACT_KINDS.get(kind, {}).get("de", kind))
        values = "; ".join(f"{r.athlete_name}: {r.value_text}" for r in rows)
        body = f"Gruppe ({len(ids)} Personen) – {title} laut gespeicherten Daten: {values}."
        if missing:
            body += f" Ohne gespeicherte Angabe ({len(missing)}): {', '.join(missing)}."
        basis = [{"quelle": r.source, "url": r.source_url} for r in rows if r.source_url]
        doc = _computed_doc(body, f"Gruppe, {title}", [r.athlete_name for r in rows], basis)
        out.append(doc)
    return out


def _national_games(s: Session, ids: list[int]) -> dict[int, int]:
    """Regular-season national-team games per person in Elite Prospects (summary "(all)" rows left out)."""
    from klugscheisser.stats import queries as sq

    out = {}
    for aid in ids:
        rows = sq.season_rows(s, aid, team_kind="national")
        games = sum(r.games or 0 for r in rows)
        if games:
            out[aid] = games
    return out


def _ep_urls(s: Session, ids: list[int]) -> list[str]:
    return [u for u in s.scalars(select(m.EpProfile.url).where(m.EpProfile.athlete_id.in_(ids))) if u]


def data_package(s: Session, athletes: list) -> list:
    """Everything stored about the persons. One to three persons: their full profile (base data, squads,
    Instagram numbers, all profile fields, form). A group: one table document per field (`group_tables`)."""
    from klugscheisser.answer.engine import fact_docs

    persons = athletes[:DATA_PACKAGE_PERSONS]
    if len(persons) <= 3:
        return fact_docs(s, persons)
    return group_tables(s, persons)


def match_reports(s: Session, athletes: list, limit: int = 12) -> list:
    """Stored official match reports (goals with scorer and assists) in which these persons appear."""
    from klugscheisser.answer.engine import Doc, _prefix

    names = {a.full_name for a in athletes}
    if not names:
        return []
    items = s.scalars(
        select(m.Item)
        .where(m.Item.source_type == "official_result")
        .order_by(m.Item.published_at.desc().nulls_last())
        .limit(200)
    )
    out = []
    for it in items:
        raw = it.raw or {}
        goals = raw.get("goals") or []
        lines = []
        for g in goals:
            people = [g.get("scorer"), *(g.get("assists") or [])]
            if names & {p for p in people if p}:
                assists = ", ".join(g.get("assists") or []) or "ohne Assist"
                lines.append(
                    f"{g.get('time', '?')} {g.get('score', '')} Tor {g.get('scorer')} ({assists}), {g.get('team', '')}"
                )
        if not lines:
            continue
        when = f"{it.published_at:%d.%m.%Y}" if it.published_at else "?"
        title = f"{raw.get('home', '?')} – {raw.get('away', '?')} {raw.get('score', '')}"
        label = f"[Spielbericht {title}, {when}]"
        out.append(
            Doc(
                key="",
                text=f"Spiel {title} am {when}: " + "; ".join(lines) + ".",
                meta=f"{label} · offizieller Spielbericht · über: {', '.join(sorted(names & {x for g in goals for x in [g.get('scorer'), *(g.get('assists') or [])] if x}))}",
                url=it.url,
                quelle=label,
                zeile_prefix=_prefix("official", None),
                item_id=it.id,
            )
        )
        if len(out) >= limit:
            break
    return out


def stats_docs(s: Session, athletes: list) -> list:
    """Career totals per league and national-team games (Elite Prospects) as one document per person."""
    from klugscheisser.answer.engine import Doc, _prefix
    from klugscheisser.stats import queries as sq

    out = []
    for a in athletes[:15]:
        rows = sq.totals(s, a.id, by="league")
        if not rows:
            continue
        nat = sq.totals(s, a.id, by="team_kind", team_kind="national")
        parts = [
            f"{r['league']}: {r.get('games') or '–'} Spiele, {r.get('goals') if r.get('goals') is not None else '–'} Tore, "
            f"{r.get('points') if r.get('points') is not None else '–'} Punkte ({r['von']}–{r['bis']})"
            for r in rows[:8]
        ]
        if nat:
            parts.append(
                f"Nationalteam total laut Elite Prospects (unvollständig, nur erfasste Turniere): "
                f"{nat[0].get('games')} Spiele"
            )
        ep = s.get(m.EpProfile, a.id)
        label = f"[Elite Prospects, Statistik {a.full_name}]"
        out.append(
            Doc(
                key="",
                text=f"{a.full_name}, Karriere: " + "; ".join(parts) + ".",
                meta=f"{label} · Statistik · über: {a.full_name}",
                url=ep.url if ep else "",
                quelle=label,
                zeile_prefix=_prefix("profile", None),
            )
        )
    return out


def search_docs(s: Session, settings, queries: list[str], athletes: list) -> list:
    from klugscheisser.answer.engine import candidate_items, item_doc, search_terms

    seen, out, terms = set(), [], search_terms(*queries[:4])
    for q in queries[:4]:
        for it in candidate_items(s, settings, q, athletes, None):
            if it.id not in seen:
                seen.add(it.id)
                out.append(item_doc(it, terms))
    return out


# --- the loop -------------------------------------------------------------------------------------------------

MIN_ROUND_USD = 0.005  # a round needs at least this much of the answer budget (llm/budget.py)


def _budget_left() -> float:
    from klugscheisser.answer.engine import _budget_left as left

    return left()


def plan(
    ask, question: str, rules_q: str, athletes: list, found: list[str], run: AgentRun
) -> tuple[list, list]:
    names = ", ".join(a.full_name for a in athletes[:20]) or "-"
    seen = "\n".join(f"- {t}" for t in found[:8]) or "- (noch nichts)"
    prompt = (
        f"Frage: {question}\nGelesen als: {rules_q}\nPersonen im Fokus: {names}\n"
        f"Bisher gefunden (reicht nicht):\n{seen}"
    )
    try:
        out, usage = ask(fill(PLAN_SYSTEM), prompt, PLAN_SCHEMA)
    except Exception as exc:
        log.warning("agent plan failed: %s", str(exc)[:160])
        return [rules_q], list(TOOLS)
    add_usage(run.llm_usage, usage)
    queries = [str(x).strip() for x in (out or {}).get("suchen") or [] if str(x).strip()][:4] or [rules_q]
    tools = [t for t in (out or {}).get("werkzeuge") or [] if t in TOOLS] or list(TOOLS)
    return queries, tools


def collect(engine, s: Session, question: str, rules_q: str, athletes: list, ask, jev=None) -> AgentRun:
    """Up to `agent_max_rounds` rounds: plan → fetch → Jev sorts → Jev: enough? The best documents are in
    `run.docs` for the writer."""
    from klugscheisser.answer.engine import passage, rerank, search_terms

    st = engine.settings
    run, pool, seen_keys = AgentRun(), [], set()
    for _ in range(max(1, st.agent_max_rounds)):
        if run.cost(st) >= st.agent_max_usd or _budget_left() < MIN_ROUND_USD:  # + the answer's budget
            break
        run.rounds += 1
        found = [d.text[:120] for d in run.docs[:8]]
        queries, tools = plan(ask, question, rules_q, athletes, found, run)
        run.searches += queries
        run.tools += [t for t in tools if t not in run.tools]
        new = []
        if run.rounds == 1:  # our own data is local and free: always in the first round
            new += data_package(s, athletes) + match_reports(s, athletes)
            if "statistik" in tools:
                new += stats_docs(s, athletes)
        if "dokumente" in tools:
            new += search_docs(s, st, queries, athletes)
        for d in new:
            key = (d.item_id, d.text[:200])
            if key not in seen_keys and d.url:
                seen_keys.add(key)
                pool.append(d)
        # Jev reads long documents where the words of the question and of all search variants occur
        terms = search_terms(question, rules_q, *run.searches)
        ranked = rerank(st, f"{question} {rules_q}", list(pool), terms)
        run.docs = ranked[:MAX_DOCS]
        excerpt = "\n".join(f"- {d.meta[:80]}: {passage(d.text, terms, 300)}" for d in run.docs[:8])
        enough = _jev(st, f"Frage: {question}\nAuszüge:\n{excerpt}", ENOUGH_QUESTION, run, jev)
        if enough is None or enough >= 0.5:
            break
    return run
