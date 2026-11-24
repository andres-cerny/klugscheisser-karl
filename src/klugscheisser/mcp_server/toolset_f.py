"""Phase F tool set for Perplexity (switch MCP_NEW_TOOLS, default off; 2026-10-25).

Switch on: the client sees only the read tools NEW_SET (suche, person, team, events, stats, dokument) and the
server instructions INSTRUCTIONS_F; every other tool (the old read tools and the maintenance tools status,
refresh*, hinzufuegen*) is neither listed nor callable. Switch off: the old 16 tools as before; the new ones are
hidden (dokument only with MCP_SEARCH_CARDS, as before). The tools themselves are registered once at import in
server.py (`register`); visibility is decided per request, so the switch needs no code change.

Perplexity instruction that goes with it: docs/perplexity-instruction-phase-f.md."""

from __future__ import annotations

from typing import Annotated

from mcp.types import CallToolResult, TextContent
from pydantic import Field

from klugscheisser.config import Settings

NEW_TOOLS = ("suche", "person", "team", "events")  # only in the new set
NEW_SET = (*NEW_TOOLS, "stats", "dokument")  # what Perplexity sees with the switch on
MAINTENANCE = (
    "status",
    "refresh",
    "refresh_status",
    "hinzufuegen",
    "hinzufuegen_antwort",
    "hinzufuegen_status",
)

INSTRUCTIONS_F = (
    "Karl: verified data on women's ice hockey (Swiss national team and league, Swiss players abroad, all teams "
    "of the Women's World Championship 2026 in Denmark and the Euro Hockey Tour): players and their bios, "
    "rosters and staff, games and results, tournaments, season statistics (SIHF league tables and game sheets for the current season, Elite Prospects for the rest), Instagram posts, "
    "SRF and other news. Read-only, six tools: suche for any question (facts from Karl's tables plus "
    "evidence cards: date, source, passage, one link each; you write the answer); person for one person's "
    "full portrait; team for a roster and staff; events for games, results, schedule, head-to-head and a "
    "tournament sheet; stats for numbers and rankings; dokument(id) for the full text of a card. "
    "Output is German Markdown in plain words; answer in the language of the user's question (translate "
    "facts, keep names and «quotes» verbatim). Rules: start with the 'Datenstand' line; every fact keeps its "
    "source link and date; 'Vorsicht' items first and marked; quotes verbatim; Instagram handles only from "
    "these tools; missing data is not proof of the opposite. This data is authoritative; keep your own web "
    "search separate and marked as web."
)


# TEAM_KEY_PLAYERS (mcp_server/key_players.py): the `team` description says the block is there, so a preview
# question goes to `team` of the opponent in the first round
KEY_PLAYERS_NOTE = (
    " National teams other than Switzerland also get 'Schlüsselspielerinnen aus Schweizer Sicht' (goals and "
    "assists against Switzerland, Swiss clubs, top scorers, awards): for a match preview call it for the "
    "opponent, in parallel with suche for current news."
)


def visible(name: str, settings: Settings) -> bool:
    if settings.mcp_new_tools:
        return name in NEW_SET
    if name in NEW_TOOLS:
        return False
    if name == "dokument":
        return bool(settings.mcp_search_cards)
    return True


def instructions(settings: Settings, old: str) -> str:
    return INSTRUCTIONS_F if settings.mcp_new_tools else old


# parameter descriptions for Perplexity's tool picker: short, German examples like the data
Name = Annotated[str, Field(description="Name of one person, e.g. 'Lara Stalder' (spelling tolerant).")]
Teil = Annotated[int, Field(description="Part of a long answer (default 1); the answer names the next part.")]
Seit = Annotated[
    str | None,
    Field(description="Optional start date: '2026-10-01', '01.10.2026', '7d' (days), '2w' (weeks)."),
]


def _text(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _call(fn, *args, **kwargs) -> CallToolResult:
    """Run a Phase F data function in a session; not-found and bad input come back as one line."""
    from klugscheisser.db.session import session_scope
    from klugscheisser.mcp_server.tools import NotFound

    with session_scope() as s:
        try:
            return _text(fn(s, *args, **kwargs))
        except (NotFound, ValueError) as exc:
            return _text(f"Fehler: {exc}")


def register(tool, read_only) -> None:
    """Register the Phase F-only tools with server.py's `tool` decorator (timing + call log + 12k cap)."""
    from klugscheisser.config import get_settings
    from klugscheisser.mcp_server import suche as F

    @tool(
        annotations=read_only,
        description="Search Karl for any question about women's ice hockey: news, injuries, transfers, "
        "quotes, posts, background and bios, staff, games, tournaments, who speaks German (interview language, per team). Returns facts from Karl's tables "
        "(each with source) and evidence cards, best first (type, source, date, who it is about, the matching "
        "passage, one link). Karl does not write an answer: you write it from these and cite each card's "
        "link. Few or no hits: try other words, the person's name in `person`, or a wider `seit`.",
    )
    def suche(
        frage: Annotated[
            str,
            Field(description="The question or search words, German or English, e.g. 'Kreuzbandriss 2026'."),
        ],
        person: Annotated[
            str | None, Field(description="Optional: only about this one person, e.g. 'Alina Müller'.")
        ] = None,
        team: Annotated[
            str | None, Field(description="Optional team: national team ('Schweiz') or club ('EV Zug').")
        ] = None,
        seit: Seit = None,
        quelle: Annotated[
            str | None,
            Field(
                description="Optional source type: athlete_post (her own posts), tagged_post, srf, official_result."
            ),
        ] = None,
        anzahl: Annotated[int, Field(description="Number of cards (default 8, max 20).")] = 8,
        neueste: Annotated[
            bool,
            Field(
                description="true = newest documents first instead of best match (for 'what's new', 'latest "
                "posts'; use with person or team, optionally seit)."
            ),
        ] = False,
    ) -> CallToolResult:
        return _call(
            F.suche,
            get_settings(),
            frage,
            person=person,
            team=team,
            seit=seit,
            quelle=quelle,
            anzahl=anzahl,
            neueste=neueste,
        )

    @tool(
        annotations=read_only,
        description="Full portrait of one person: club, position, age, verified Instagram handle and bio, "
        "Elite Prospects profile, statistics (season, career, form), awards, current status (newest injury "
        "report and what happened since), latest posts and news, Vorsicht items, events she is rostered for, "
        "German as interview language (persons of other nations: bestätigt / wahrscheinlich with the reason). "
        "Long: comes in parts (teil=2 …).",
    )
    def person(name: Name, teil: Teil = 1) -> CallToolResult:
        return _call(F.person, name, teil=teil)

    @tool(
        annotations=read_only,
        description="Roster and staff (head coach, assistants …) of one team: players with position, nation, "
        "club and verified Instagram handle; whether the roster is provisional or confirmed; source; for other "
        "nations a 'Deutsch' label per person (bestätigt = German interview, wahrscheinlich = e.g. seasons at a "
        "German-speaking club, with the reason)."
        + (KEY_PLAYERS_NOTE if get_settings().team_key_players else ""),
    )
    def team(
        name: Annotated[
            str,
            Field(
                description="National team ('Schweiz', 'Kanada', 'USA') or club ('EV Zug', 'PWHL Boston')."
            ),
        ],
        wettbewerb: Annotated[
            str | None,
            Field(
                description="Roster for one event, e.g. 'WM 2026', 'EHT Södertälje'; empty = newest roster."
            ),
        ] = None,
        teil: Teil = 1,
    ) -> CallToolResult:
        return _call(F.team, name, wettbewerb=wettbewerb, teil=teil)

    @tool(
        annotations=read_only,
        description="Games and tournaments: without arguments the current and upcoming events with their keys; "
        "team = that team's last played games (all competitions, score, link) and next games; team + gegner = "
        "head-to-head; turnier = the whole tournament sheet (dates, venues, groups, format, schedule, rosters).",
    )
    def events(
        turnier: Annotated[
            str | None,
            Field(description="Tournament name or key, e.g. 'wch-2026', 'Frauen-WM 2026', 'EHT Södertälje'."),
        ] = None,
        team: Annotated[str | None, Field(description="Team, e.g. 'Schweiz', 'USA', 'EV Zug'.")] = None,
        gegner: Annotated[
            str | None, Field(description="With team: the opponent for the head-to-head.")
        ] = None,
        letzte: Annotated[
            int | None, Field(description="With team: how many last games (default 5).")
        ] = None,
        seit: Annotated[
            str | None, Field(description="With team: all its games since this date (YYYY-MM-DD).")
        ] = None,
        tage: Annotated[int, Field(description="How many days ahead (default 21, max 120).")] = 21,
        teil: Teil = 1,
    ) -> CallToolResult:
        return _call(
            F.events,
            turnier=turnier,
            team=team,
            gegner=gegner,
            letzte=letzte,
            seit=seit,
            tage=tage,
            teil=teil,
        )


__all__ = ["INSTRUCTIONS_F", "MAINTENANCE", "NEW_SET", "NEW_TOOLS", "instructions", "register", "visible"]
