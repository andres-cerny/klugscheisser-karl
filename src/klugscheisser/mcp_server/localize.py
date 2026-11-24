"""2026-11-24 (floorball project): MCP texts per project.

The server instructions and the tool / parameter descriptions are written once (registered at import) for the
ice hockey project. One MCP endpoint serves every project (/mcp/<key> sets the current project per request), so
the texts are adapted when a client reads them (initialize, tools/list): the phrases below are replaced by the
current project's words. The ice hockey project gets the texts unchanged (no replacement at all)."""

from __future__ import annotations

import copy
from typing import Any

from klugscheisser import projects
from klugscheisser.projects import Wording
from klugscheisser.sports import DEFAULT_SPORT, current_spec


def _pairs() -> list[tuple[str, str]]:
    """(ice hockey phrase, the current project's phrase), most specific first."""
    w = projects.wording()
    p = projects.current()
    spec = current_spec()
    ev = p.main_event
    pairs = [
        (
            "the Swiss women's ice hockey (or this connector's project) athlete server",
            f"the athlete server for {w.topic_en}",
        ),
        ("Swiss women's ice hockey", w.topic_en),
        ("women's ice hockey", f"{w.gender_en} {w.sport_en}"),
    ]
    if spec.family != "hockey":
        club, club2 = "'SV Wiler-Ersigen'", "'IBF Falun'"
        pairs += [
            (
                " (Women's World Championship 2026 in Denmark, Euro Hockey Tour)",
                f" ({w.worlds_de} 2026, key '{ev}')",
            ),
            (
                "all teams of the Women's World Championship 2026 in Denmark and the Euro Hockey Tour",
                f"all teams of the {w.worlds_de} 2026 (key '{ev}')",
            ),
            (
                "season statistics (SIHF league tables and game sheets for the current season, Elite Prospects for "
                "the rest)",
                "season statistics (where stored, each with its source)",
            ),
            (
                " (current season: SIHF league tables and counted game sheets, newer than the Elite Prospects import "
                "of 04.10.2026; older seasons: Elite Prospects)",
                " (as stored, each row with its source)",
            ),
            ("Elite Prospects profile (height, contract, draft …), ", ""),
            ("Elite Prospects profile, ", ""),
            (
                "'SWHL' (Women's League), 'PWHL', 'SDHL', 'NCAA', 'Olympia', 'WM', 'EHT'",
                f"e.g. 'WM', '{w.league_de}'",
            ),
            ("'wch-2026', 'Frauen-WM 2026', 'eht-2026-sodertalje'", f"'{ev}', '{w.worlds_de} 2026'"),
            ("'wch-2026', 'Frauen-WM 2026', 'EHT Södertälje'", f"'{ev}', '{w.worlds_de} 2026'"),
            ("'wch-2026'", f"'{ev}'"),
            ("'WM 2026', 'EHT Södertälje'", "'WM 2026'"),
            ("(Olympics, WM, Euro Hockey Tour, friendlies, league)", "(WM, friendlies, league)"),
            ("('Schweiz', 'Kanada', 'USA')", "('Schweiz', 'Schweden', 'Finnland')"),
            ("'Schweiz', 'USA', 'EV Zug'", f"'Schweiz', 'Schweden', {club}"),
            ("'Northeastern', 'EV Zug', 'Schweiz'", f"{club}, 'Schweiz'"),
            ("'EV Zug', 'PWHL Boston'", f"{club}, {club2}"),
            ("'Stürmerin EV Zug'", f"'{spec.position_labels_de.get('F', 'Stürmer')} {club[1:-1]}'"),
            ("'EV Zug'", club),
            (", e.g. 'Lara Stalder' (spelling tolerant)", " (spelling tolerant)"),
            ("e.g. 'Lara Stalder', 'Schweiz', ", "e.g. 'Schweiz', "),
            ("e.g. 'Lara Stalder', ", "e.g. "),
            ("['Lara Stalder', 'Alina Müller']", "['Vorname Nachname', …]"),
            ("['Stalder', 'Müller']", "['Name 1', 'Name 2']"),
            (", e.g. 'Alina Müller'", ""),
            ("'Resultate der Schweiz in Lahti, Spiele in Södertälje'", "'Resultate der Schweiz an der WM'"),
            (", history, why November)", ", history)"),
        ]
    if spec.men:
        pairs += [
            ("Schlüsselspielerinnen", f"Schlüssel{w.players.lower()}"),
            ("Welche Spielerinnen", f"Welche {w.players}"),
            ("die Finninnen", "die Finnen"),
            ("events she is rostered for", "events he is rostered for"),
            ("is she fit/injured?", "is he fit/injured?"),
            ("or her last game", "or his last game"),
            ("that tag her", "that tag him"),
            ("her/it", "him/it"),
            ("(her own posts)", "(his own posts)"),
        ]
    return [(a, b) for a, b in pairs if a != b]


def active() -> bool:
    """False for the ice hockey project (its texts are the originals)."""
    return not (
        projects.wording() == Wording() and (projects.current().sport or DEFAULT_SPORT) == DEFAULT_SPORT
    )


def text(value: str | None) -> str | None:
    """One MCP text (instructions or a description) in the current project's words."""
    if not value or not active():
        return value
    for old, new in _pairs():
        value = value.replace(old, new)
    return value


def schema(value: dict[str, Any]) -> dict[str, Any]:
    """A tool's input schema with every parameter `description` in the current project's words (a copy)."""
    if not active():
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                k: (text(v) if k == "description" and isinstance(v, str) else walk(v))
                for k, v in node.items()
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(copy.deepcopy(value))
