"""Floorball project (2026-11-24): MCP texts, UI labels and add defaults come from the project, not from women's ice
hockey. The ice hockey project keeps its texts unchanged."""

import asyncio
from pathlib import Path

import pytest

from klugscheisser import projects

REPO_PROJECTS = Path(__file__).resolve().parents[1] / "projects.yaml"


@pytest.fixture
def uni(monkeypatch):
    """The repo's projects.yaml (eishockey, allgemein, unihockey); nothing here opens a project database."""
    monkeypatch.setenv("PROJECTS_FILE", str(REPO_PROJECTS))
    assert projects.get("unihockey") is not None
    yield
    projects.clear_cache()


def _surface() -> str:
    from klugscheisser.mcp_server.server import mcp

    tools = asyncio.run(mcp.list_tools())
    lines = [mcp._lowlevel_server.instructions or ""]
    for t in tools:
        lines.append(t.description or "")
        lines += [p.get("description") or "" for p in t.input_schema.get("properties", {}).values()]
    return "\n".join(lines)


def test_mcp_texts_follow_the_project(uni):
    from klugscheisser.mcp_server import localize, toolset_f
    from klugscheisser.mcp_server.server import INSTRUCTIONS, mcp

    with projects.use("eishockey"):
        assert mcp._lowlevel_server.instructions == INSTRUCTIONS  # unchanged for ice hockey
        assert not localize.active()
        ice = _surface()
    assert "Swiss women's ice hockey" in ice and "'wch-2026'" in ice
    with projects.use("unihockey"):
        text = _surface()
        f = localize.text(toolset_f.INSTRUCTIONS_F)
    assert "men's floorball" in text and "men's floorball" in f
    for gone in (
        "women's ice hockey",
        "Women's World Championship",
        "Euro Hockey Tour",
        "SIHF",
        "Elite Prospects",
    ):
        assert gone not in text and gone not in f, gone
    for gone in ("'wch-2026'", "EV Zug", "PWHL", "Lara Stalder", "Welche Spielerinnen", "events she is"):
        assert gone not in text, gone
    assert "'wfc-2026'" in text and "Welche Spieler von Finnland" in text


def test_mcp_connector_per_project_lists_floorball_tools(uni, db):
    from starlette.testclient import TestClient

    from klugscheisser.config import Settings
    from klugscheisser.mcp_server.server import build_app

    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "mcp-protocol-version": "2025-06-18",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    app = build_app(Settings(_env_file=None))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        uni_init = c.post("/mcp/unihockey", json=init, headers=headers).json()["result"]
        ice_init = c.post("/mcp/eishockey", json=init, headers=headers).json()["result"]
        tools = c.post("/mcp/unihockey", json=listing, headers=headers).json()["result"]["tools"]
        ice_tools = c.post("/mcp/eishockey", json=listing, headers=headers).json()["result"]["tools"]
    assert "men's floorball" in uni_init["instructions"]
    assert "Swiss women's ice hockey" in ice_init["instructions"]
    frage = next(t for t in tools if t["name"] == "frage")
    assert "Welche Spieler von" in frage["description"]
    assert "'wfc-2026'" in frage["inputSchema"]["properties"]["event"]["description"]
    ice_frage = next(t for t in ice_tools if t["name"] == "frage")
    assert "'wch-2026'" in ice_frage["inputSchema"]["properties"]["event"]["description"]


def test_ui_labels_say_spieler(uni):
    from klugscheisser.mcp_server import key_players
    from klugscheisser.web import brief, html, routes

    with projects.use("unihockey"):
        assert brief._pos()["G"] == "Torhüter"
        assert [p for p, _ in brief._pos_group()] == ["Torhüter", "Verteidiger", "Stürmer"]
        assert "schwedischer Stürmer" in html.add_dialog()
        assert "Fakten zu Spieler, Teams" in routes.login_form("/")
        assert key_players._w("Schlüsselspielerinnen") == "Schlüsselspieler"
    with projects.use("eishockey"):
        assert brief._pos()["G"] == "Torhüterin"
        assert "schwedische Stürmerin" in html.add_dialog()
        assert "Fakten zu Spielerinnen, Teams" in routes.login_form("/")
        assert key_players._w("Schlüsselspielerinnen") == "Schlüsselspielerinnen"


def test_team_and_person_adds_default_to_the_project_sport(uni):
    from klugscheisser.adds.team import interpret_rules
    from klugscheisser.adds.wiki import sports_in

    assert [x.base for x in sports_in("Unihockey Nationalteam")] == ["floorball"]  # not ice hockey
    with projects.use("unihockey"):
        req = interpret_rules("das schwedische Nationalteam")
        assert (req.gender, req.sport) == ("male", "floorball")
        assert interpret_rules("das schwedische Frauen-Nationalteam").gender == "female"
    with projects.use("eishockey"):
        req = interpret_rules("das schwedische Nationalteam")
        assert (req.gender, req.sport) == ("female", "ice hockey")


def test_wikipedia_terms_for_floorball():
    from klugscheisser.collectors.profiles.wikipedia import SPORT_PATTERNS, SPORT_TERMS

    assert (
        SPORT_TERMS["floorball_men"]["sv"] == "innebandy"
        and SPORT_TERMS["floorball_men"]["de"] == "Unihockey"
    )
    pat = SPORT_PATTERNS["floorball_men"]
    assert pat.search("är en svensk innebandyspelare") and pat.search("je český florbalista")
    assert not pat.search("ist eine Schweizer Eishockeyspielerin")
