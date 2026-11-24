"""MCP server: Streamable HTTP, bearer auth, sourced and German-labelled tool results."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any

from mcp.server.lowlevel.server import Server
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from sqlalchemy import func, select
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from klugscheisser.config import Settings, get_settings
from klugscheisser.db import models
from klugscheisser.db.session import session_scope
from klugscheisser.mcp_server import tools as t
from klugscheisser.mcp_server.auth import AccessLogMiddleware, BearerTokenMiddleware
from klugscheisser.mcp_server.present import QUELLE

log = logging.getLogger("klugscheisser.tools")

# Sent once per session (clients like Perplexity read it); the per-call footer is one short line (present.RULES).
# Keep in line with the Perplexity instruction draft (gaploop/instructions_eishockey_v2.txt).
INSTRUCTIONS = (
    "Karl: verified data on Swiss women's ice hockey: players, rosters and staff, games and results, tournaments "
    "(Women's World Championship 2026 in Denmark, Euro Hockey Tour), season statistics (SIHF league tables and game sheets for the current season, Elite Prospects for the rest), "
    "Instagram, SRF and other news. Tool output is German Markdown; answer in the language of the user's "
    "question (translate the facts, keep names and «quotes» verbatim). "
    "Rules: start with the tool's 'Datenstand' line; keep every fact's Markdown link unchanged; a line "
    "'Quelle für alle Zeilen: …' is the source of every line of its block; ⚠️ Vorsicht items first; quotes "
    "verbatim; Instagram handles only from these tools; data missing here is not proof of the opposite. "
    "This data is authoritative; keep your own web search separate and marked as web. "
    "Use the cheapest tool that answers, usually one call: stats for numbers, rankings, comparisons; "
    "get_team_roster for one team's roster and staff; frage for open questions, injuries, coaches/staff and "
    "tournament questions (format, groups, schedule, history); list_events for event keys and a team's last or "
    "next games (team=…, last=N; opponent=… for the head-to-head); get_athlete for a full portrait of one person (long, not for a single number or a "
    "last game); get_event only for a whole tournament sheet; whats_new for news since a date; search for "
    "anything else. One connector per project (/mcp/eishockey; /mcp/allgemein for one-day topics of any "
    "sport); 'status' names the project."
)

# All tools only read public data (updates refresh our own cache of public sources; nothing of the user's is
# changed). Declaring this lets clients like Perplexity call them without a confirmation prompt.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)


class KarlServer(MCPServer):
    """Lists and runs only the tools of the active tool set (old 16 tools, or Phase F with MCP_NEW_TOOLS)."""

    async def list_tools(self):
        from klugscheisser.mcp_server import localize
        from klugscheisser.mcp_server.toolset_f import visible

        settings = get_settings()
        tools = [t for t in await super().list_tools() if visible(t.name, settings)]
        if not localize.active():
            return tools
        # 2026-11-24: descriptions in the words of this connector's project (/mcp/<key>; floorball …)
        return [
            t.model_copy(
                update={
                    "description": localize.text(t.description),
                    "input_schema": localize.schema(t.input_schema),
                }
            )
            for t in tools
        ]

    async def call_tool(self, name, arguments, context=None):
        from mcp.server.mcpserver.exceptions import ToolError

        from klugscheisser.mcp_server.toolset_f import visible

        if not visible(name, get_settings()):
            raise ToolError(f"Unknown tool: {name}")
        return await super().call_tool(name, arguments, context)


class _ProjectInstructions(Server):
    """Low-level server whose instructions follow the project of the request (/mcp/<key>, read at initialize)."""

    @property
    def instructions(self) -> str | None:
        from klugscheisser.mcp_server import localize

        return localize.text(self._base_instructions)

    @instructions.setter
    def instructions(self, value: str | None) -> None:
        self._base_instructions = value


mcp = KarlServer(name="klugscheisser-athlete", version="0.1.0", instructions=INSTRUCTIONS)
mcp._lowlevel_server.__class__ = _ProjectInstructions  # 2026-11-24: per-project words (floorball project)
mcp._lowlevel_server.instructions = INSTRUCTIONS


# parameter descriptions shown to the client (input schema); short on purpose, German examples like the data
Person = Annotated[str, Field(description="Name of one person, e.g. 'Lara Stalder' (spelling tolerant).")]
TeamName = Annotated[
    str,
    Field(description="Team: national team ('Schweiz', 'Kanada', 'USA') or club ('EV Zug', 'PWHL Boston')."),
]
OptTeam = Annotated[
    str | None, Field(description="Optional team filter: national team ('Schweiz') or club ('EV Zug').")
]
Since = Annotated[
    str | None,
    Field(description="Optional start date: '2026-10-01', '01.10.2026', '7d' (days) or '2w' (weeks)."),
]


def response_size(result: Any) -> int:
    """Characters sent back to the client (text blocks of a CallToolResult, a string, or the JSON of a dict)."""
    import json

    if isinstance(result, CallToolResult):
        return sum(len(c.text) for c in result.content if isinstance(c, TextContent))
    if isinstance(result, str):
        return len(result)
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return 0


ANSWER_PREVIEW_CHARS = 600


def response_text(result: Any) -> str:
    """The text a client receives (text blocks of a CallToolResult, a string, or the JSON of a dict)."""
    import json

    if isinstance(result, CallToolResult):
        return "\n".join(c.text for c in result.content if isinstance(c, TextContent))
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return ""


def answer_preview(result: Any, limit: int = ANSWER_PREVIEW_CHARS) -> str:
    """First `limit` chars of the response on one line (newlines shown as ⏎), for the server log."""
    text = " ⏎ ".join(line.strip() for line in response_text(result).splitlines() if line.strip())
    return text[:limit] + ("…" if len(text) > limit else "")


MAX_TOOL_CHARS = 12_000  # hard cap on every tool answer (24.10.: one frage sent 140,228 chars)
MORE_HINT = {
    "frage": "enger fragen (eine Person mit person='…', ein Team mit team='…', «aktuell», ein Zeitraum mit since='…')",
    "get_event": "frage('…', event='KEY') für einen Teil (Spielplan, Gruppen, Kader eines Teams)",
    "get_athlete": "frage('…', person='…') für eine bestimmte Angabe",
    "get_team_roster": "get_team_roster(team, competition) für einen bestimmten Kader",
    "list_events": "list_events(team='…', last=N) oder get_event(key)",
    "search": "search mit engerem Begriff oder since='…'",
    "whats_new": "whats_new mit since='…' oder einer Person",
    "stats": "stats mit weniger Personen oder einer Liga/Saison",
}


def cap_text(text: str, tool_name: str, limit: int = MAX_TOOL_CHARS) -> str:
    """Cut a tool answer at a line end before `limit` and say so, with how to get the rest."""
    if len(text) <= limit:
        return text
    hint = MORE_HINT.get(tool_name, "enger fragen")
    tail = f"\n\n_… gekürzt: {limit:,} von {len(text):,} Zeichen gezeigt. mehr: {hint}._".replace(",", "'")
    cut = text.rfind("\n", 0, limit - len(tail))
    if cut < limit // 2:
        cut = limit - len(tail)
    return text[:cut].rstrip() + tail


def cap_result(result: Any, tool_name: str) -> Any:
    if isinstance(result, CallToolResult):
        texts = [c for c in result.content if isinstance(c, TextContent)]
        if sum(len(c.text) for c in texts) > MAX_TOOL_CHARS and len(texts) == 1:
            return CallToolResult(
                content=[TextContent(type="text", text=cap_text(texts[0].text, tool_name))],
                is_error=result.is_error,
            )
        return result
    if isinstance(result, str):
        return cap_text(result, tool_name)
    return result


def timed(fn):
    """Log every tool call with its duration and response size (the slow ones show up in the server log)."""
    import functools
    import inspect
    import time

    from klugscheisser import calllog

    param_names = list(inspect.signature(fn).parameters)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        start = time.monotonic()
        result, failed, capped = None, True, False
        notes, token = calllog.start()
        try:
            raw = fn(*args, **kwargs)
            result = cap_result(raw, fn.__name__)
            capped = result is not raw and response_size(result) < response_size(raw)
            failed = False
            return result
        finally:
            calllog.end(token)
            dur = time.monotonic() - start
            log.info(
                "tool %s done in %.2f s, %s chars%s",
                fn.__name__,
                dur,
                response_size(result),
                " (error)" if failed else "",
            )
            if get_settings().mcp_call_log:
                log.info(
                    "%s",
                    calllog.format_line(
                        fn.__name__,
                        dur,
                        response_size(result),
                        capped,
                        failed,
                        notes,
                        calllog.summarize_args(args, kwargs, param_names),
                    ),
                )
            if not failed:
                # what the client (Perplexity) actually received, for the gap review (tool output is public data)
                log.info("tool %s answer: %s", fn.__name__, answer_preview(result))

    return wrapper


def tool(**kwargs):
    """`@mcp.tool(...)` plus the timing log."""
    return lambda fn: mcp.tool(**kwargs)(timed(fn))


@tool(
    annotations=READ_ONLY,
    description="Health and data status of the Swiss women's ice hockey (or this connector's project) athlete server: "
    "row counts, newest data timestamp and which data sources are switched on.",
)
def status() -> dict[str, Any]:
    settings = get_settings()
    with session_scope() as s:
        counts = {
            name: s.scalar(select(func.count()).select_from(model))
            for name, model in [
                ("athletes", models.Athlete),
                ("teams", models.Team),
                ("events", models.Event),
                ("handles", models.Handle),
                ("items", models.Item),
            ]
        }
        newest = s.scalar(select(func.max(models.Item.scraped_at)))
    from klugscheisser import projects

    return {
        "quelle": QUELLE,
        "projekt": projects.current().name,
        "stand": (newest or datetime.now(UTC)).isoformat(),
        "counts": counts,
        "features": settings.features,
    }


def _run(fn, *args, **kwargs) -> CallToolResult:
    """Run a tool; the client gets one compact German Markdown text (each fact with its link), no JSON copy.

    One representation on purpose: LLM clients (Perplexity) read the text; the same data again as JSON doubled
    the size and diluted answers (gap loop 2026-10-17). No structured content either, so no client gets both."""
    from klugscheisser.mcp_server.present import render_markdown
    from klugscheisser.mcp_server.tools import NotFound

    log.info("tool %s args=%s kwargs=%s", fn.__name__, [a for a in args if isinstance(a, str | int)], kwargs)
    with session_scope() as s:
        try:
            result = fn(s, *args, **kwargs)
        except (NotFound, ValueError) as exc:
            result = {"quelle": QUELLE, "fehler": str(exc)}
    return text_result(render_markdown(result))


def text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


@tool(
    annotations=READ_ONLY,
    description="Full sheet of one event (long, ~10k characters): dates, venues, groups, format, Swiss games, "
    "roster status of every team, the Swiss roster, Vorsicht and 'Zu beachten' items. Use it only for a whole "
    "tournament overview; a single tournament question (who plays when, format, history) is cheaper with "
    "frage, one team's roster with get_team_roster, event keys with list_events.",
)
def get_event(
    event: Annotated[
        str, Field(description="Event name or key, e.g. 'wch-2026', 'Frauen-WM 2026', 'eht-2026-sodertalje'.")
    ],
    posts_per_player: Annotated[
        int,
        Field(
            description="0 (default) = names only; >0 adds each player's handle and latest posts (very long)."
        ),
    ] = 0,
) -> CallToolResult:
    return _run(t.get_event, event, posts_per_player=posts_per_player)


@tool(
    annotations=READ_ONLY,
    description="Full portrait of one person (long, ~10k characters): club, position, age, verified Instagram "
    "handle and bio, Elite Prospects profile (height, contract, draft …), statistics (this season, career per "
    "league, form last 5/10 games), awards, latest posts and news, Vorsicht items, events she is rostered for, "
    "current status (newest injury report and what happened since: roster nominations, games with points). "
    "Use for 'tell me about <player>' or 'is she fit/injured?'. Not for one number (stats), a ranking (stats) "
    "or her last game (whats_new or list_events(team=club)).",
)
def get_athlete(name: Person) -> CallToolResult:
    return _run(t.get_athlete, name)


@tool(
    annotations=READ_ONLY,
    description="Season statistics per person, team and league (current season: SIHF league tables and counted "
    "game sheets, newer than the Elite Prospects import of 04.10.2026; older seasons: Elite Prospects): games, goals, assists, "
    "points, penalty minutes, +/-, per-game values; goalies: wins, shutouts, save percentage, goals against "
    "average. For numbers, rankings and comparisons (cheaper than a full portrait). All filters optional; each "
    "row is one sentence with its source link and date; an empty value means 'no data' (never 0). Newest "
    "season first unless sort_by.",
)
def stats(
    person: Annotated[
        str | list[str] | None,
        Field(description="Name, or a list of names to compare: ['Stalder', 'Müller']."),
    ] = None,
    team: Annotated[str | None, Field(description="Team, e.g. 'Northeastern', 'EV Zug', 'Schweiz'.")] = None,
    league: Annotated[
        str | list[str] | None,
        Field(
            description="League or list: 'SWHL' (Women's League), 'PWHL', 'SDHL', 'NCAA', 'Olympia', 'WM', 'EHT'."
        ),
    ] = None,
    season: Annotated[
        str | None,
        Field(description="'2026/27', 'diese Saison', 'letzte Saison' or 'seit 2022'; empty = all seasons."),
    ] = None,
    team_kind: Annotated[str | None, Field(description="club, college, nationalteam or junioren.")] = None,
    phase: Annotated[str, Field(description="regular (default), playoffs or alle.")] = "regular",
    nationality: Annotated[str | None, Field(description="Nation code, e.g. 'SUI'.")] = None,
    position: Annotated[str | None, Field(description="G (goalie), D (defence) or F (forward).")] = None,
    sort_by: Annotated[
        str | None,
        Field(
            description="Ranking: points, goals, assists, points_per_game, goals_per_game, games, save_pct, gaa, "
            "wins, shutouts, penalty_minutes, plus_minus."
        ),
    ] = None,
    group: Annotated[
        str,
        Field(description="season (one row per season, default), career (sum per person), team or league."),
    ] = "season",
    min_games: Annotated[
        int | None,
        Field(
            description="Minimum games for per-game values and save % (default 5 per season, 10 career, 3/5 tournaments)."
        ),
    ] = None,
    limit: Annotated[int, Field(description="Maximum rows (default 20).")] = 20,
) -> CallToolResult:
    from klugscheisser.mcp_server import stats_tools

    return _run(
        stats_tools.stats,
        person=person,
        team=team,
        league=league,
        season=season,
        team_kind=team_kind,
        phase=phase,
        nationality=nationality,
        position=position,
        sort_by=sort_by,
        group=group,
        min_games=min_games,
        limit=limit,
    )


@tool(
    annotations=READ_ONLY,
    description="Recent Instagram posts by or tagging one player, newest first: caption, image description, "
    "category, Vorsicht flag, link.",
)
def get_athlete_posts(
    name: Person,
    since: Since = None,
    limit: Annotated[int, Field(description="Maximum posts (default 10).")] = 10,
    include_tagged: Annotated[
        bool, Field(description="Also posts of others that tag her (default true).")
    ] = True,
) -> CallToolResult:
    return _run(t.get_athlete_posts, name, since=since, limit=limit, include_tagged=include_tagged)


@tool(
    annotations=READ_ONLY,
    description="Kader (und Staff, wo erfasst) of one team: players with position, nation and verified "
    "Instagram handle, whether the roster is provisional or confirmed, and its source. Cheapest tool for "
    "'who is in the roster of X'.",
)
def get_team_roster(
    team: TeamName,
    competition: Annotated[
        str | None,
        Field(description="Roster for one event, e.g. 'WM 2026', 'EHT Södertälje'; empty = newest roster."),
    ] = None,
) -> CallToolResult:
    return _run(t.get_team_roster, team, competition=competition)


@tool(
    annotations=READ_ONLY,
    description="Verified Instagram handles of players, by names or by team. Use only handles from these "
    "tools; never guess handles.",
)
def get_ig_handles(
    athletes: Annotated[
        list[str] | None, Field(description="Names, e.g. ['Lara Stalder', 'Alina Müller'].")
    ] = None,
    team: OptTeam = None,
) -> CallToolResult:
    return _run(t.get_ig_handles, athletes=athletes, team=team)


@tool(
    annotations=READ_ONLY,
    description="What's new about a player, team or event since a date: Instagram posts, SRF and other news, "
    "results; dated and sourced, newest first. Good for 'latest news' and a player's last game.",
)
def whats_new(
    target: Annotated[
        str, Field(description="Person, team or event, e.g. 'Lara Stalder', 'Schweiz', 'wch-2026'.")
    ],
    since: Annotated[
        str | None, Field(description="Start date: '7d' (default), '2w', '2026-10-01' or '01.10.2026'.")
    ] = None,
) -> CallToolResult:
    return _run(t.whats_new, target, since=since)


@tool(
    annotations=READ_ONLY,
    description="Full-text search in the stored documents (Instagram posts, SRF and other news, results) for "
    "what the other tools don't cover. Returns matching documents, not an answer (for an answer: frage).",
)
def search(
    query: Annotated[str, Field(description="Search words, e.g. 'Verletzung Knie', 'Comeback'.")],
    athlete: Annotated[str | None, Field(description="Optional: only documents about this person.")] = None,
    team: OptTeam = None,
    since: Since = None,
    source_type: Annotated[
        str | None,
        Field(description="Optional: athlete_post, tagged_post, srf or official_result."),
    ] = None,
    limit: Annotated[int, Field(description="Maximum hits (default 8).")] = 8,
) -> CallToolResult:
    return _run(
        t.search_tool,
        get_settings(),
        query,
        athlete=athlete,
        team=team,
        since=since,
        source_type=source_type,
        limit=limit,
    )


# search lab 2026-10-25: full text of one search card (visible with MCP_SEARCH_CARDS or MCP_NEW_TOOLS)
@tool(
    annotations=READ_ONLY,
    description="Full stored text of one document from a search card (the number in 'dokument(…)'), incl. "
    "the description of its image/video. Use it when the card's excerpt is not enough; many source links "
    "need a login (Instagram, X, paywalls). Long texts come in parts (teil=2, …).",
)
def dokument(
    id: Annotated[int, Field(description="Document id from the search card, e.g. 12345.")],
    teil: Annotated[int, Field(description="Part of a long text (default 1).")] = 1,
) -> CallToolResult:
    if get_settings().mcp_new_tools:
        from klugscheisser.mcp_server.suche import plain

        res = _run(t.dokument_tool, id, teil=teil)
        return text_result(plain(res.content[0].text))
    return _run(t.dokument_tool, id, teil=teil)


@tool(
    annotations=READ_ONLY,
    description="Update data now for a player, team or event: news immediately, Instagram in the background "
    "(returns job_id; ask refresh_status about a minute later). Cached, cost-capped. Only when the user asks "
    "for the very latest.",
)
def refresh(
    target: Annotated[str, Field(description="Person, team or event, e.g. 'Lara Stalder', 'Schweiz'.")],
) -> CallToolResult:
    from klugscheisser.jobs.refresh import refresh as do_refresh

    return _run(do_refresh, get_settings(), target)


@tool(
    annotations=READ_ONLY,
    description="Status and new items of a background refresh started with refresh or named in an answer's "
    "aktualisierung (by job_id); waits up to 60 s for the job to finish.",
)
def refresh_status(
    job_id: Annotated[int, Field(description="job_id from refresh or from an answer's update note.")],
) -> CallToolResult:
    from klugscheisser.jobs.refresh import refresh_status as do_status

    return _run(do_status, job_id, wait_s=get_settings().status_wait_s)


@tool(
    annotations=READ_ONLY,
    description="Answers one question from the stored documents: open questions ('Wer studiert?', 'Wer ist "
    "verletzt?', 'Gossip vor der WM?'), injuries, coaches and staff (head coach, assistants, goalie coach, GM, "
    "physio …, coaching changes; from the team's stored staff), tournament questions without a person "
    "(WM format, groups, schedule, who plays when, quarterfinals, relegation, history, why November), the "
    "latter from stored tournament facts (cheaper than get_event), and recent games, results, form or "
    "head-to-head of national teams or clubs ('letzte Spiele/Form von Tschechien', 'Schweiz gegen Österreich "
    "zuletzt'), from all stored games of every competition. Also in ONE call (no need to look up players one "
    "by one): connections between two teams' players ('Welche Spielerinnen von Finnland und der Schweiz spielen "
    "im selben Klub oder haben zusammen gespielt?', optionally naming a game; marks who was in its lineup), the "
    "games of a named tournament ('Resultate der Schweiz in Lahti, Spiele in Södertälje'), and the current clubs "
    "of a whole team ('Bei welchen Klubs spielen die Finninnen aktuell?'). Every statement comes with a verbatim "
    "quote and its link; unsupported statements are removed.",
)
def frage(
    frage: Annotated[str, Field(description="The question in full, in German or English.")],
    person: Annotated[
        str | None, Field(description="Optional: the question is about this one person.")
    ] = None,
    team: OptTeam = None,
    event: Annotated[str | None, Field(description="Optional event, e.g. 'wch-2026'.")] = None,
    since: Since = None,
    web: Annotated[
        bool,
        Field(description="Also read fresh web pages when stored documents are not enough (default true)."),
    ] = True,
) -> CallToolResult:
    from klugscheisser.answer.engine import answer_to_dict, build_answer_engine
    from klugscheisser.db import repo
    from klugscheisser.mcp_server.present import render_answer
    from klugscheisser.mcp_server.tools import NotFound, parse_since, resolve_event, resolve_team

    settings = get_settings()
    log.info("tool frage %r person=%s team=%s event=%s", frage, person, team, event)
    try:
        with session_scope() as s:
            person_ids, team_id, event_id, team_row, event_row = None, None, None, None, None
            if person:
                a = repo.resolve_athlete(s, person)
                if a is None:
                    raise NotFound(
                        f"Keine Person gefunden für '{person}'. Mit hinzufuegen(name='{person}', details=…) "
                        "kann sie vorgeschlagen und nach Bestätigung geladen werden."
                    )
                person_ids = [a.id]
            if team:
                team_row = resolve_team(s, team)
                team_id = team_row.id
            if event:
                event_row = resolve_event(s, event)
                event_id = event_row.id
            start = parse_since(since, 30) if since else None
            # v19: coach/staff questions → the team's stored staff (team level, not the players' documents)
            from klugscheisser import staff

            sres = staff.answer(s, frage, team_id=team_id, event_id=event_id, person_ids=person_ids)
            if sres is not None:
                from klugscheisser.mcp_server.present import datenstand

                return text_result(staff.render(sres, datenstand(s)))
            if not person:
                from klugscheisser.answer import connections, event_games, team_games
                from klugscheisser.mcp_server.present import datenstand

                # 24.10.: players of two teams who play / played in the same club (one call instead of ~20)
                text = connections.answer_text(s, frage, datenstand(s), team_row, event_row)
                # games of a tournament named by its city/key (Lahti, Södertälje): results + schedule
                if text is None:
                    text = event_games.answer_text(s, frage, datenstand(s), team_row, event_row)
                # recent games / results / form / head-to-head of teams: straight from all stored games
                if text is None:
                    text = team_games.answer_text(s, frage, datenstand(s), team_row, event_row)
                if text is not None:
                    return text_result(text)
            # tournament questions (format, schedule, history …): from the stored tournament sheet
            if not person:
                from klugscheisser import tournament

                tev = tournament.event_for_question(s, frage, event_id)
                if tev is not None:
                    from klugscheisser.mcp_server.present import datenstand

                    tres = tournament.answer(s, tev, f"{frage} {team}" if team else frage)
                    return text_result(tournament.render(tres, datenstand(s)))
        result = answer_to_dict(
            build_answer_engine(settings).answer(
                frage, person_ids=person_ids, team_id=team_id, event_id=event_id, since=start, web=web
            )
        )
    except (NotFound, ValueError) as exc:
        return text_result(f"Fehler: {exc}")
    return text_result(render_answer(result))


# v5 phase 6: add a missing person/team from the chat app (proposal → Fabi says ja → load)
WRITES = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)


def _proposal_text(req: models.AddRequest) -> str:
    from klugscheisser.adds.base import Proposal

    if req.proposal is None:
        return f"Anfrage #{req.id}: kein Vorschlag ({req.error or req.status})."
    p = Proposal.from_json(req.proposal)
    lines = [f"Anfrage #{req.id} ({p.kind}): {p.summary}"]
    for x in p.persons[:12]:
        src = ", ".join(f"[{r.label}]({r.url})" if r.url else r.label for r in x.sources[:2])
        ig = f" · Instagram @{x.instagram} ({x.instagram_check or x.instagram_source})" if x.instagram else ""
        lines.append(f"- {x.name}{' · ' + x.function_de if x.function_de else ''}{ig} · Quelle: {src}")
    if len(p.persons) > 12:
        lines.append(f"- … und {len(p.persons) - 12} weitere")
    lines += [f"ℹ️ {n}" for n in p.notes]
    if p.alternatives:
        lines.append("Andere Treffer (nicht gewählt): " + " · ".join(p.alternatives[:5]))
    lines.append(f"Geschätzte Kosten: ~{p.cost_estimate_chf:.2f} CHF.")
    if req.status == "proposed":
        lines.append(
            "Frage die Nutzerin, ob das die richtige Person/das richtige Team ist. Dann "
            f"hinzufuegen_antwort(anfrage={req.id}, antwort='ja' | 'nein' | 'praezisieren', text=Korrektur)."
            if p.found
            else "Nichts Verlässliches gefunden: nach Details fragen und hinzufuegen_antwort(antwort='praezisieren')."
        )
    return "\n".join(lines)


@tool(
    annotations=WRITES,
    description="Add a person (athlete, coach) or a whole team that is missing in this server's database. "
    "Returns a concrete proposal with sources and cost; nothing is added until the user confirms with "
    "hinzufuegen_antwort. Use it when frage/get_athlete says the person is unknown.",
)
def hinzufuegen(
    name: Annotated[str, Field(description="Name of the person or team to add.")],
    art: Annotated[str, Field(description="'person' (default) or 'team'.")] = "person",
    details: Annotated[
        str, Field(description="What identifies her/it: club, nation, role, e.g. 'Stürmerin EV Zug'.")
    ] = "",
    instagram: Annotated[str | None, Field(description="Instagram handle if the user knows it.")] = None,
) -> str:
    from klugscheisser.adds import flow

    settings = get_settings()
    kind = "team" if art.strip().lower().startswith("team") else "person"
    inp = (
        {"request": f"{name} {details}".strip()}
        if kind == "team"
        else {"name": name.strip(), "instagram": instagram, "details": details.strip()}
    )
    log.info("tool hinzufuegen %s %r", kind, name)
    with session_scope() as s:
        req = flow.propose(s, settings, kind, inp)
        return _proposal_text(req)


@tool(
    annotations=WRITES,
    description="Answer to a proposal from hinzufuegen: antwort 'ja' (add and load data: Instagram ~30-60 s, "
    "image/reel analysis a few minutes later), 'nein' (discard) or 'praezisieren' with text (new proposal).",
)
def hinzufuegen_antwort(
    anfrage: Annotated[int, Field(description="Request number from hinzufuegen.")],
    antwort: Annotated[str, Field(description="'ja', 'nein' or 'praezisieren' (the user's decision).")],
    text: Annotated[str | None, Field(description="The correction, only with 'praezisieren'.")] = None,
) -> str:
    from klugscheisser.adds import flow
    from klugscheisser.adds.load import confirm
    from klugscheisser.jobs.freshness import wait_for_job

    settings = get_settings()
    a = antwort.strip().lower()
    with session_scope() as s:
        req = s.get(models.AddRequest, anfrage)
        if req is None:
            return f"Keine Anfrage #{anfrage}."
        if a.startswith("prä") or a.startswith("prae") or a.startswith("praz"):
            if not text:
                return "Bitte die Korrektur als text mitgeben."
            return _proposal_text(flow.refine(s, settings, req, text))
        if a.startswith("n"):
            flow.reject(s, req)
            return f"Anfrage #{anfrage} verworfen. Nichts hinzugefügt."
        if not a.startswith("j"):
            return "antwort muss 'ja', 'nein' oder 'praezisieren' sein."
        if req.status != "proposed" or not (req.proposal or {}).get("found"):
            return f"Anfrage #{anfrage} ist nicht offen (Status {req.status})."
        job = confirm(s, settings, req)
        job_id = job.id
    with session_scope() as s:
        done = wait_for_job(s, job_id, settings.status_wait_s)
        return hinzufuegen_status_text(s, anfrage, finished=done is not None)


def hinzufuegen_status_text(s, anfrage: int, finished: bool) -> str:
    req = s.get(models.AddRequest, anfrage)
    job = s.get(models.Job, req.job_id) if req and req.job_id else None
    names = [s.get(models.Athlete, i).full_name for i in (req.athlete_ids or [])[:12]] if req else []
    if not finished:
        step = (job.result or {}).get("schritt", "läuft") if job else "läuft"
        return (
            f"Hinzugefügt: {', '.join(names)}. Daten werden geladen ({step}). "
            f"hinzufuegen_status(anfrage={anfrage}) aufrufen; danach frage/get_athlete benutzen."
        )
    return (
        f"Fertig: {', '.join(names)} hinzugefügt und geladen. Jetzt frage(...) oder get_athlete(...) benutzen. "
        "Bild-/Reel-Analysen kommen in den nächsten Minuten dazu."
    )


@tool(
    annotations=READ_ONLY,
    description="Status of an add request (hinzufuegen_antwort ja); waits up to 60 s for the load to finish.",
)
def hinzufuegen_status(
    anfrage: Annotated[int, Field(description="Request number from hinzufuegen.")],
) -> str:
    from klugscheisser.jobs.freshness import wait_for_job

    settings = get_settings()
    with session_scope() as s:
        req = s.get(models.AddRequest, anfrage)
        if req is None:
            return f"Keine Anfrage #{anfrage}."
        if req.job_id is None:
            return _proposal_text(req)
        done = wait_for_job(s, req.job_id, settings.status_wait_s)
        return hinzufuegen_status_text(s, anfrage, finished=done is not None)


@tool(
    annotations=READ_ONLY,
    description="Current and upcoming events with their keys: tournaments as one entry each (dates, place, "
    "number of games, next game), national-team games of the last 14 and next `days` days one by one, club "
    "games only counted per league. team adds that team's LAST played games from all stored competitions "
    "(Olympics, WM, Euro Hockey Tour, friendlies, league) with score, result and link, plus its upcoming games "
    "and tournaments. opponent (with team) gives the head-to-head: all stored meetings of the two teams and the "
    "next one. Use it for 'Form', 'letzte Resultate', 'wann zuletzt gegeneinander'. Details: get_event(key).",
)
def list_events(
    days: Annotated[int, Field(description="How many days ahead (default 21, max 120).")] = 21,
    team: Annotated[
        str | None,
        Field(description="Optional team, e.g. 'Schweiz', 'USA', 'EV Zug': its last results and next games."),
    ] = None,
    last: Annotated[
        int | None, Field(description="With team: how many of its last played games (default 5).")
    ] = None,
    since: Annotated[
        str | None,
        Field(description="With team: all its games since this date (YYYY-MM-DD) instead of last N."),
    ] = None,
    opponent: Annotated[
        str | None,
        Field(description="With team: head-to-head with this team (all stored meetings and the next one)."),
    ] = None,
) -> CallToolResult:
    return _run(t.list_events, days=days, team=team, last=last, since=since, opponent=opponent)


# Phase F read tools (suche, person, team, events): visible only with MCP_NEW_TOOLS (toolset_f.visible)
from klugscheisser.mcp_server import toolset_f  # noqa: E402

toolset_f.register(tool, READ_ONLY)


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def build_app(settings: Settings | None = None) -> ASGIApp:
    settings = settings or get_settings()
    # Phase F: the server instructions follow the tool set (read when a client connects)
    mcp._lowlevel_server.instructions = toolset_f.instructions(settings, INSTRUCTIONS)
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*",
            "localhost:*",
            "[::1]:*",
            *settings.mcp_allowed_hosts,
            *[f"{h}:443" for h in settings.mcp_allowed_hosts],
        ],
        allowed_origins=[
            "http://127.0.0.1:*",
            "http://localhost:*",
            *[f"https://{h}" for h in settings.mcp_allowed_hosts],
            *settings.mcp_allowed_origins,
        ],
    )
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host=settings.mcp_host,
    )
    app.add_route("/healthz", healthz)
    from klugscheisser.web import web_routes

    app.router.routes.extend(web_routes(settings))  # own web app (search, person/event pages, question box)
    wrapped: ASGIApp = app
    if settings.mcp_auth_token is not None:
        wrapped = BearerTokenMiddleware(app, settings.mcp_auth_token.get_secret_value())
    from klugscheisser.web.project_switch import ProjectMiddleware

    return AccessLogMiddleware(ProjectMiddleware(wrapped))  # v7: /p/<key>/…, /mcp/<key>


def serve() -> None:
    import uvicorn

    from klugscheisser import projects
    from klugscheisser.config import CurrentSettings
    from klugscheisser.jobs.refresh import recover_stale_jobs
    from klugscheisser.media.worker import start_background_worker

    settings = CurrentSettings()  # v7: follows the project of each request / job
    for p in projects.active_projects():
        with projects.use(p):
            try:
                recover_stale_jobs()
            except Exception:
                logging.getLogger(__name__).exception("project %s: database not ready", p.key)
    start_background_worker(settings)  # image/reel analysis queue (no-op when no Gemini/Claude key)
    from klugscheisser.jobs.nightly import start_news_poller, start_nightly_worker

    start_nightly_worker(settings)  # v5: nightly refresh for prepared events
    start_news_poller(settings)  # v5: news for everyone, every 30 min
    from klugscheisser.jobs.gamewatch import start_game_watch

    start_game_watch(settings)  # 2026-10-31: IIHF games right after the final buzzer (GAME_WATCH)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # uvicorn's own access log would print ?token=… in clear; ours redacts it
    uvicorn.run(
        build_app(settings),
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level="info",
        access_log=False,
        # behind Caddy / cloudflared on the same machine: trust their X-Forwarded-For/-Proto (real client IP
        # for the login brake, https for secure cookies) — only from localhost
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
