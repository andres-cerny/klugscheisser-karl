"""Phase F: the new Perplexity tool set behind MCP_NEW_TOOLS (mcp_server/toolset_f.py, mcp_server/suche.py)."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_search_jev_path import add_item, world  # noqa: F401  (fixture)

from klugscheisser.config import Settings
from klugscheisser.db import models as m

SETTINGS = Settings(_env_file=None, auto_refresh=False, retrieval_vector_candidates=False)
OLD_READ = {
    "get_event",
    "get_athlete",
    "get_athlete_posts",
    "get_team_roster",
    "get_ig_handles",
    "whats_new",
    "search",
    "frage",
    "list_events",
    "stats",
}


def _names(monkeypatch, **switches) -> set[str]:
    from klugscheisser.mcp_server import server

    st = SETTINGS.model_copy(update=switches)
    monkeypatch.setattr(server, "get_settings", lambda: st)
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_switch_off_lists_the_old_tools_only(monkeypatch):
    from klugscheisser.mcp_server.toolset_f import MAINTENANCE, NEW_TOOLS

    names = _names(monkeypatch, mcp_new_tools=False, mcp_search_cards=False)
    assert names >= OLD_READ and set(MAINTENANCE) <= names
    assert not names & set(NEW_TOOLS) and "dokument" not in names
    assert "dokument" in _names(monkeypatch, mcp_new_tools=False, mcp_search_cards=True)  # as before


def test_switch_on_lists_only_the_six_read_tools(monkeypatch):
    names = _names(monkeypatch, mcp_new_tools=True)
    assert names == {"suche", "person", "team", "events", "stats", "dokument"}


def test_switch_on_hidden_tools_cannot_be_called(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from klugscheisser.mcp_server import server

    st = SETTINGS.model_copy(update={"mcp_new_tools": True})
    monkeypatch.setattr(server, "get_settings", lambda: st)
    for name, args in (("refresh", {"target": "x"}), ("frage", {"frage": "x"}), ("status", {})):
        with pytest.raises(ToolError, match="Unknown tool"):
            asyncio.run(server.mcp.call_tool(name, args))


def test_descriptions_are_short_and_name_no_old_tools(monkeypatch):
    from klugscheisser.mcp_server import server
    from klugscheisser.mcp_server.toolset_f import INSTRUCTIONS_F, instructions

    st = SETTINGS.model_copy(update={"mcp_new_tools": True})
    monkeypatch.setattr(server, "get_settings", lambda: st)
    tools = asyncio.run(server.mcp.list_tools())
    old = ("get_athlete", "frage", "list_events", "whats_new", "get_team_roster", "search(")
    for t in tools:
        assert len(t.description) < 600, t.name
        assert not any(o in t.description for o in old), t.name
    assert not any(o in INSTRUCTIONS_F for o in old)
    assert instructions(st, "OLD") == INSTRUCTIONS_F
    assert instructions(SETTINGS, "OLD") == "OLD"
    suche = next(t for t in tools if t.name == "suche")
    assert set(suche.input_schema["properties"]) == {
        "frage",
        "person",
        "team",
        "seit",
        "quelle",
        "anzahl",
        "neueste",
    }


def _suche(s, frage, **kw):
    from klugscheisser.mcp_server import suche as F
    from klugscheisser.mcp_server import tools as t

    t.freshen = lambda s, athletes: {"status": "test"}
    return F.suche(s, SETTINGS, frage, **kw)


def test_suche_output_shape(world):  # noqa: F811
    with Session(world) as s:
        knie = s.scalar(select(m.Item).where(m.Item.url == "https://knie"))
        knie.text = "📸 Kreuzbandriss im Knie: die Saison ist vorbei. " + "Reha. " * 300
        s.flush()
        text = _suche(s, "Kreuzbandriss Knie")
        assert text.startswith("**Datenstand:**")
        assert "**Dokumente**" in text and "📸" not in text
        blocks = [b for b in text.split("\n\n") if b.startswith("**[")]
        assert blocks
        for b in blocks:  # one link per card, last; type, source and date on the meta line
            lines = b.strip().splitlines()
            assert b.count("http") == 1 and lines[-1].startswith("Link: ")
            assert lines[1].startswith("Typ: ") and "Quelle: " in lines[1]
        knie_block = next(b for b in blocks if "Kreuzbandriss" in b)
        assert f"dokument({knie.id})" in knie_block
        # the footer names the new tools, not the old ones
        assert "get_athlete" not in text and "list_events" not in text and "events" in text
        s.rollback()


def test_suche_person_filter_status_and_newest(world):  # noqa: F811
    with Session(world) as s:
        text = _suche(s, "Neuigkeiten", person="Lara Testalder", neueste=True, anzahl=2)
        assert "neueste zuerst" in text and "athlete=Lara Testalder" in text
        blocks = [b for b in text.split("\n\n") if b.startswith("**[")]
        assert len(blocks) == 2 and "https://lara-new" in blocks[0]  # newest first, not by score
        with pytest.raises(Exception, match="Keine Person"):
            _suche(s, "x", person="Niemand Unbekannt")
        s.rollback()


def test_material_lines_keep_source_and_vorsicht():
    from klugscheisser.answer.engine import Doc
    from klugscheisser.mcp_server.suche import doc_line

    d = Doc(
        key="", text="Lara: geboren 1994.", meta="", url="https://w", quelle="[Wikidata Q1]", zeile_prefix=""
    )
    assert doc_line(d) == "- Lara: geboren 1994. · Quelle: [Wikidata Q1](https://w)"
    c = Doc(
        key="",
        text="X, aktueller Stand: Verletzung offen.",
        meta="",
        url="https://a",
        quelle="[Berechnet]",
        zeile_prefix="",
        computed=True,
        basis=[{"quelle": "SRF", "url": "https://a"}, {"quelle": "SIHF", "url": "https://b"}],
        vorsicht=True,
    )
    line = doc_line(c)
    assert line.startswith("- Vorsicht: X") and "[SRF](https://a), [SIHF](https://b)" in line


def test_paged_and_plain():
    from klugscheisser.mcp_server.suche import paged, plain

    assert plain("**📊 Statistik**\n- 📸 (von ihr selbst) Text ⏱") == "**Statistik**\n- (von ihr selbst) Text"
    long = "**Datenstand:** heute\n" + "\n".join(f"- Zeile {i} " + "x" * 90 for i in range(200))
    p1 = paged(long, 1, "person(name='A', teil={teil})")
    p3 = paged(long, 3, "person(name='A', teil={teil})")
    assert p1.startswith("**Datenstand:** heute\n_Teil 1/3 – weiter mit person(name='A', teil=2)_")
    assert p3.startswith("**Datenstand:** heute\n_Teil 3/3 (letzter Teil)_") and "Fortsetzung" not in p3
    assert all(len(paged(long, i, "x{teil}")) < 9500 for i in (1, 2, 3))
    short = paged("**Datenstand:** heute\n- eine Zeile", 1, "x{teil}")
    assert short == "**Datenstand:** heute\n- eine Zeile"


def test_person_tool(world):  # noqa: F811
    from klugscheisser.mcp_server import suche as F
    from klugscheisser.mcp_server import tools as t

    t.freshen = lambda s, athletes: {"status": "test"}
    with Session(world) as s:
        out = F.person(s, "Lara Testalder")
        assert out.startswith("**Datenstand:**") and "Lara Testalder" in out
        s.rollback()
