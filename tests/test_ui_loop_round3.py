"""ui-loop round 3 (2026-10-17): "Auf einen Blick", Steckbrief career newest first, search rows with photo or
initials, login / 404 pages (MCP-side paths keep the plain 404)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.db import repo
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.web import build_web_app
from klugscheisser.web import card as sb
from klugscheisser.web import glance as gl
from klugscheisser.web.html import initials
from klugscheisser.web.views import _result_rows

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
SETTINGS = Settings(_env_file=None, web_answer_engine="fake")


def _card() -> dict:
    return {
        "fakten": [
            {
                "label": "Aktueller Verein",
                "value": "SC Bern Frauen (SWHL A (W)) · #29",
                "sources": [
                    {
                        "label": "Elite Prospects",
                        "url": "https://www.eliteprospects.com/player/1",
                        "quote": None,
                    }
                ],
            }
        ]
    }


def _stats() -> dict:
    row = {
        "season": 2026,
        "saison": "2026/27",
        "team": "SC Bern Frauen",
        "league": "SWHL A (W)",
        "games": 4,
        "goals": None,
        "assists": None,
        "points": None,
        "save_pct": 0.894,
        "gaa": 2.06,
        "source": "eliteprospects",
        "quelle_link": "https://www.eliteprospects.com/player/1",
    }
    return {
        "goalie": True,
        "panes": [{"group": "club", "phase": "regular", "rows": [row]}],
        "quelle": {"label": "Elite Prospects", "url": "https://www.eliteprospects.com/player/1"},
        "current": 2026,
    }


def _page() -> dict:
    return {
        "athletin": {"id": 1, "name": "Saskia Maurer"},
        "anlaesse": [
            {
                "key": "wch-2026",
                "name": "IIHF Frauen-WM 2026",
                "datum": "06.11.2026",
                "start": (NOW + timedelta(days=20)).isoformat(),
                "kader_status": "provisional",
                "quelle": "Annahme: letztes bekanntes Aufgebot",
                "url": "https://www.sihf.ch/kader",
            },
            {
                "key": "oly-2026",
                "name": "Olympia 2026",
                "datum": "05.02.2026",
                "start": (NOW - timedelta(days=200)).isoformat(),
                "kader_status": "confirmed",
                "quelle": "SIHF",
                "url": "https://www.sihf.ch/oly",
            },
        ],
    }


def _items() -> list[dict]:
    return [
        {
            "when": NOW - timedelta(days=i + 1),
            "k": "news",
            "label": "News",
            "title": f"Bericht {i}",
            "src": "SRF",
            "url": f"https://www.srf.ch/{i}",
            "extern": True,
            "vorsicht": i == 1,
        }
        for i in range(4)
    ] + [
        {
            "when": NOW + timedelta(days=20),
            "k": "event",
            "label": "Anlass",
            "title": "WM",
            "src": "SIHF",
            "url": "/event/wch-2026",
            "extern": False,
            "vorsicht": False,
        }
    ]


def test_glance_tiles_each_with_its_source():
    erfolge = [
        {
            "metal": "bronze",
            "text": "Olympia 2026: Bronze",
            "quelle": "Elite Prospects",
            "url": "https://www.eliteprospects.com/player/1",
        }
    ]
    html = gl.glance_html(_page(), _card(), _items(), erfolge, _stats(), now=NOW)
    assert "Auf einen Blick" in html and "data-sec='glance'" in html
    assert "SC Bern Frauen" in html and "SWHL A (W)" in html  # team and league apart
    assert "4 Sp · 89,4 % Sv · 2,06 GAA" in html and "Diese Saison" in html
    assert "IIHF Frauen-WM 2026" in html and "/event/wch-2026" in html and "Kader provisorisch" in html
    assert ">sihf.ch<" in html  # a long roster note becomes the site's name as chip label
    assert "Olympia 2026: Bronze" in html
    # the two newest entries (not the event), the Vorsicht one marked, each linked to its source
    assert "Bericht 0" in html and "Bericht 1" in html and "Bericht 2" not in html
    assert html.count("class='k v'>Vorsicht") == 1
    assert html.count("class='src'") >= 6  # club, season, event, medal, 2 rows
    assert "data-k=" not in html  # the filter chips never pick rows out of the glance


def test_glance_stays_away_when_there_is_too_little():
    assert gl.glance_html({"athletin": {"id": 1, "name": "X"}}, {"fakten": []}, [], [], None, now=NOW) == ""


def test_career_newest_first_older_behind_a_button():
    rows = [
        {
            "saison": f"20{10 + i}/{11 + i}",
            "team": f"Team {i}",
            "sources": [{"label": "EP", "url": "https://ep/x"}],
        }
        for i in range(9)
    ]
    html = sb._career_table(rows)
    assert html.startswith('<div class="sb-scroll"><table class="sb-career">')
    assert html.index("Team 8") < html.index("Team 0")  # newest first
    assert html.count("<tr class='x'>") == 3 and "Frühere Stationen anzeigen" in html
    assert html.count(">EP</a>") == 9  # every station keeps its source
    assert "Frühere" not in sb._career_table(rows[:7])  # one hidden row is not worth a button


def test_initials():
    assert initials("Saskia Maurer") == "SM"
    assert initials("Margot Huot-Marchand") == "MH"
    assert initials("Cher") == "CH"


def test_search_rows_show_photo_or_initials_and_the_fact_line():
    res = {
        "personen": [
            {
                "id": 1,
                "name": "Lara Stalder",
                "info": "x",
                "fakten": "Stürmerin · SUI · Brynäs IF",
                "badge": "Tier 1",
                "foto": "https://upload.wikimedia.org/a.jpg",
                "foto_credit": "Foto: A, CC BY-SA 4.0, Wikimedia Commons",
            },
            {
                "id": 2,
                "name": "Alina Müller",
                "info": "Stürmerin · SUI",
                "fakten": "Stürmerin · SUI",
                "badge": "",
                "foto": None,
            },
        ],
        "teams": [],
        "anlaesse": [],
    }
    rows = _result_rows(res, "x")["personen"]
    assert "upload.wikimedia.org/a.jpg" in rows[0] and "CC BY-SA 4.0" in rows[0] and "Tier 1" in rows[0]
    assert "class='av ini'" in rows[1] and ">AM<" in rows[1]
    assert '<span class="nm"><span class="t">Lara Stalder</span>' in rows[0]  # the name stays the first text


def test_unknown_address_gets_the_404_page_but_mcp_paths_stay_plain(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    with TestClient(build_web_app(SETTINGS)) as c:
        r = c.get("/gibt-es-nicht")
        assert r.status_code == 404 and "Nicht gefunden" in r.text and "Zur Suche" in r.text
        assert "href='/'>Zur Startseite</a>" in r.text  # round 5: a clear way back home
        assert (
            'class="mainnav"' not in r.text and "Projekt wechseln" not in r.text
        )  # nothing that needs a login
        for path in ("/.well-known/oauth-protected-resource", "/mcp/irgendwas", "/healthz/x"):
            r = c.get(path)
            assert r.status_code == 404 and r.text == "Not Found", path
        assert c.post("/gibt-es-nicht").status_code in (404, 405)  # only GET has a page
        missing = c.get("/person/99999999")
        assert (
            missing.status_code == 404
            and "Keine Person" in missing.text
            and 'class="mainnav"' in missing.text
        )


def test_login_page_is_bare_and_explains(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    app = build_web_app(Settings(_env_file=None, web_password="richtig-lang-123", web_answer_engine="fake"))
    with TestClient(app) as c:
        html = c.get("/login").text
        assert "class='gate'" in html or 'class="gate"' in html
        assert "jede Aussage mit Quelle" in html and 'autocomplete="current-password"' in html
        assert 'class="mainnav"' not in html and "Projekt wechseln" not in html
        assert (
            c.get("/irgendwo", follow_redirects=False).status_code == 404
        )  # no redirect loop for unknown paths


def test_person_page_has_glance_before_the_steckbrief(db):
    with Session(db) as s:
        load_all_seeds(s)
        sport = repo.get_sport(s, "icehockey_women")
        a = repo.upsert_athlete(s, sport, "Testa Blick", nationality="SUI", position="F")
        s.commit()
        aid = a.id
    with TestClient(build_web_app(SETTINGS)) as c:
        html = c.get(f"/person/{aid}").text
        main = (
            html[html.index("class='info-main'") :]
            if "class='info-main'" in html
            else html[html.index('class="info-main"') :]
        )
        assert 'data-sec="steckbrief"' in main  # the Steckbrief now lives in the main column
        assert "class='ph-photo' data-ini=" in html or 'class="ph-photo" data-ini="TB"' in html


def test_season_tile_does_not_repeat_the_club_tile():
    html = gl.glance_html(_page(), _card(), _items(), [], _stats(), now=NOW)
    season = html[html.index("Diese Saison") :]
    season = season[: season.index("</div>")]
    assert "2026/27" in season and "SC Bern Frauen" not in season and "SWHL" not in season
    other = gl.season_tile(_stats(), club="EV Zug")  # another club: team and league stay
    assert "SC Bern Frauen" in other and "SWHL A (W)" in other


def test_season_tile_falls_back_to_the_last_season_with_games():
    st = _stats()
    empty = dict(st["panes"][0]["rows"][0], season=2027, saison="2027/28", games=None)
    st["panes"][0]["rows"].append(empty)
    st["current"] = 2027
    html = gl.season_tile(st)
    assert "Saison 2026/27" in html and "4 Sp" in html and "Diese Saison" not in html
    assert ">Elite Prospects<" in html  # the line keeps its source


def test_new_chat_button_is_labelled_on_the_phone():
    from klugscheisser.web.person import chat_aside

    html = chat_aside("Chat", "zu X", "", "/person/1?neu=1")
    assert "<span class='ibl'>Neuer Chat</span>" in html
