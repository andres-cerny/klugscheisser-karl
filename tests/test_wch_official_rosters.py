"""2026-10-28: official WCh rosters replace the provisional / camp lists (kaderwatch fixes, parsers of the federation
pages, IIHF roster mirror on hockeycanada.ca, roster history, one-off catch-up). Fake HTTP only, no network."""

import json
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser import tournament
from klugscheisser.db import models as m
from klugscheisser.jobs import roster_parsers as rp
from klugscheisser.jobs import roster_watch as rw
from klugscheisser.seed.loader import load_all_seeds, load_roster_data, roster_history

WATCH = {t: spec for t, spec in tournament.load_info("wch-2026")["roster_watch"]["teams"].items()}


@pytest.fixture
def wch(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
        return s.scalar(select(m.Event.id).where(m.Event.key == "wch-2026"))


class FakeResponse:
    def __init__(self, text, status=200, ctype="text/html"):
        self.text, self.status_code, self.headers = text, status, {"content-type": ctype}
        self.content = text.encode() if isinstance(text, str) else text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.pages.get(url) or FakeResponse("<html></html>")


# --- announcement patterns on the real link texts (28.10.2026) -------------------------------------------------


@pytest.mark.parametrize(
    "code, text, url, hit",
    [
        (
            "CAN",
            "Women’s Worlds roster set",
            "https://www.hockeycanada.ca/en-ca/news/womens-worlds-roster-set-2026-27-nwt",
            True,
        ),
        (
            "CAN",
            "Women’s camp roster named",
            "https://www.hockeycanada.ca/en-ca/news/training-camp-roster-2026-27-nwt",
            False,
        ),
        (
            "SWE",
            "Här är Tre Kronor dams lag till Lidl Hockey Games och VM",
            "https://www.swehockey.se/toppnyheter/haer-aer-tre-kronor-dams-lag-till-lidl-hockey-games-och-vm/",
            True,
        ),
        ("SWE", "Tre Kronor herrs trupp till NOCCO Hockey Games", "https://www.swehockey.se/x/", False),
        (
            "JPN",
            "2026.10.21 日本代表 2026女子世界選手権および事前合宿",
            "https://www.jihf.or.jp/news/detail.php?id=2434",
            True,
        ),
        (
            "JPN",
            "2026.09.25 日本代表 女子U18 Euro Hockey Tour Tournament",
            "https://www.jihf.or.jp/news/detail.php?id=2425",
            False,
        ),
        (
            "SUI",
            "Roster-Update: Captain Lara Stalder verpasst die WM verletzungsbedingt, Dalessi rückt ins Aufgebot",
            "https://www.sihf.ch/de/news/a-frauen/202627/roster-update-captain-lara-stalder-verpasst-die-wm-verletzungsbedingt/",
            False,
        ),
        (
            "SUI",
            "Das WM-Kader der Frauen steht",
            "https://www.sihf.ch/de/news/a-frauen/202627/das-wm-kader-der-frauen-steht/",
            True,
        ),
        (
            "CZE",
            "Ženy: Lvice čeká generálka na světový šampionát, hrát budou ve Švédsku",
            "https://www.ceskyhokej.cz/novinky/9955-zeny-lvice",
            False,
        ),
        (
            "CZE",
            "Ženy: Trenér Idalski zveřejnil nominaci na mistrovství světa v Dánsku",
            "https://www.ceskyhokej.cz/novinky/9990-zeny-nominace-ms",
            True,
        ),
        (
            "USA",
            "U.S. Women’s National Team October Training Camp Set For Plymouth, Mich.",
            "https://teamusa.usahockey.com/news_article/show/1368591",
            False,
        ),
        (
            "USA",
            "U.S. Women's National Team Roster Named for 2026 IIHF Women's World Championship",
            "https://teamusa.usahockey.com/news_article/show/1369999",
            True,
        ),
        (
            "DEN",
            "U18-kvindelandsholdet udtaget til træningslejr og VM i Italien",
            "https://ishockey.dk/15898-2/",
            False,
        ),
        (
            "DEN",
            "Kvindelandsholdet udtaget til VM på hjemmebane",
            "https://ishockey.dk/kvindelandsholdet-udtaget-til-vm/",
            True,
        ),
    ],
)
def test_announcement_patterns(code, text, url, hit):
    spec = WATCH[code]
    assert bool(rw.matching([(url, text)], spec["pattern"], spec.get("exclude"))) is hit


def test_matching_keeps_the_headline_of_an_image_link():
    url = "https://www.hockeycanada.ca/en-ca/news/womens-worlds-roster-set-2026-27-nwt"
    assert rw.matching([(url, ""), (url, "Women’s Worlds roster set")], WATCH["CAN"]["pattern"]) == [
        (url, "Women’s Worlds roster set")
    ]


# --- parsers of the official pages (formats as published) ------------------------------------------------------


def test_parse_swehockey():
    text = (
        "Truppen\nMålvakter\n1 Ebba Svensson Träff, Linköping HC\n30 Emma Söderberg, SDE HF\nBackar\n"
        + "\n".join(f"{10 + i} Back Spelare{i}, Brynäs IF" for i in range(7))
        + "\nForwards\n"
        + "\n".join(f"{40 + i} Forward Spelare{i}, HV71" for i in range(14))
        + "\nSpelschema Lidl Hockey Games\n28 oktober"
    )
    ps = rp.parse_swehockey(text)
    assert len(ps) == 23 and ps[0] == {
        "name": "Ebba Svensson Träff",
        "pos": "G",
        "number": 1,
        "club_text": "Linköping HC",
    }
    assert [p["pos"] for p in ps].count("D") == 7 and ps[-1]["pos"] == "F"


def test_parse_jihf_text_positions_from_group_sizes():
    rows = [
        ("GK", "HALLORAN, Rei", "2001/03/22", "25", "Timra IK (SWE)"),
        ("", "MASUHARA, Miyuu", "2001/10/04", "25", "Sabres St. Pölten(AUT)"),
        ("DF", "SHIGA, Aoi", "1999/07/04", "27", "Luleå HF(SWE)"),
        ("FW", "NORO, Rio", "2004/05/15", "22", "Daishin"),
        ("", "NORO, Riri", "2004/05/15", "22", "Daishin"),
    ]
    text = "PLAYER\n" + "\n".join(
        f"{pos:8} 漢字 {n}   {b}   {a}     {c}      会社" for pos, n, b, a, c in rows
    )
    text = (
        text.replace("SHIGA, Aoi", "SHIGA, Aoi").replace("GK", "GK")
        + "\n(２)\n(1)\n(2)\nSTAFF\nHC 大久保 OKUBO, Tomohito 1976/9/22 50 x"
    )
    ps = rp.parse_jihf_text(text)
    assert [(p["name"], p["pos"]) for p in ps] == [
        ("Rei Halloran", "G"),
        ("Miyuu Masuhara", "G"),
        ("Aoi Shiga", "D"),
        ("Rio Noro", "F"),
        ("Riri Noro", "F"),
    ]
    assert ps[0]["birth"] == "2001-03-22" and ps[0]["club_text"] == "Timra IK (SWE)"
    assert rp.parse_jihf_text(text.replace("(2)\nSTAFF", "(3)\nSTAFF")) == []  # sizes do not add up: no guess


def test_parse_deb_team_block_printed_twice():
    block = "Goalies\n35\nSandra\nAbstreiter\nVerteidigung\n10\nEmilija\nBirka\nSturm\n42\nMarie-Kristin\nSchmid\n"
    text = "Kader für die IIHF Women’s World Championship 2026\n" + block + block + "Staff\nRonja\nJenike\nGM"
    assert rp.parse_deb_team(text) == [
        {"name": "Sandra Abstreiter", "pos": "G", "number": 35},
        {"name": "Emilija Birka", "pos": "D", "number": 10},
        {"name": "Marie-Kristin Schmid", "pos": "F", "number": 42},
    ]


def test_parse_oehv():
    text = (
        "Der Kader im Überblick:\nTOR\nADAMITSCH Anja | 24 Jahre alt | KSV Highlanders\nVERTEIDIGUNG\n"
        "MÖRTL Emma | 22 | Sabres St. Pölten\nANGRIFF\nSCHAFZAHL Theresa | 26 | Seattle Torrent (USA)\nZurück zu Aktuelles"
    )
    assert rp.parse_oehv(text) == [
        {"name": "Anja Adamitsch", "pos": "G", "club_text": "KSV Highlanders"},
        {"name": "Emma Mörtl", "pos": "D", "club_text": "Sabres St. Pölten"},
        {"name": "Theresa Schafzahl", "pos": "F", "club_text": "Seattle Torrent (USA)"},
    ]


# --- seeds: the official lists of 28.10.2026 --------------------------------------------------------------------


def test_seeded_official_lists(db, wch):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        want = {"can-women": 25, "swe-women": 24, "jpn-women": 23, "ger-women": 25, "aut-women": 25}
        # 03.11.2026: the last five official lists (SUI 01.11., USA 31.10., DEN 29.10., FIN 31.10., CZE 01.11.)
        want |= {"sui-women": 25, "usa-women": 25, "den-women": 24, "fin-women": 25, "cze-women": 25}
        for key, n in want.items():
            team = s.scalar(select(m.Team).where(m.Team.key == key))
            st = tournament.roster_status(s, ev, team)
            assert st["status"] == "offiziell" and st["anzahl"] == n, key
        can = s.scalar(select(m.Team).where(m.Team.key == "can-women"))
        names = {e.athlete.full_name for e in rw._players(s, ev, can)}
        assert "Brianne Jenner" not in names and "Sarah Fillier" in names
        sui = s.scalar(select(m.Team).where(m.Team.key == "sui-women"))
        st = tournament.roster_status(s, ev, sui)
        names = {e.athlete.full_name for e in rw._players(s, ev, sui)}
        assert "Lara Stalder" not in names and "Elisa Dalessi" in names
        assert not names & {"Alizée Aymon", "Alizee Aymon", "Elena Gaberell", "Jade Surdez"}
        assert any(a["name"] == "Lara Stalder" and "Knie" in a["reason"] for a in st["fehlen"])


# --- kaderwatch: IIHF mirror, history, protection, seed precedence ----------------------------------------------

HC = "https://www.hockeycanada.ca/en-ca/team-canada/women/national/2026-27/world-championship/stats"


def _next(data: dict) -> str:
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def _mirror_pages(players: list[dict]) -> dict:
    schedule = {
        "props": {
            "pageProps": {
                "games": [
                    {"home": {"id": 182, "name": "United States"}, "away": {"id": 179, "name": "Canada"}}
                ]
            }
        }
    }
    roster = {
        "props": {
            "pageProps": {
                "teamDataTournament": {
                    "TeamRoster_Players": [
                        {
                            "Name": p["name"],
                            "Position": p["pos"],
                            "JerseyNumber": str(p.get("number", "")),
                            "RosterType": "player",
                        }
                        for p in players
                    ]
                }
            }
        }
    }
    return {
        f"{HC}/schedule": FakeResponse(_next(schedule)),
        f"{HC}/team-rosters/182": FakeResponse(_next(roster)),
        f"{HC}/team-rosters/179": FakeResponse(_next({"props": {"pageProps": {"teamDataTournament": {}}}})),
    }


def _usa(s):
    return s.scalar(select(m.Team).where(m.Team.key == "usa-women"))


def test_mirror_roster_replaces_provisional_list_and_keeps_history(db, wch, as_provisional):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        usa = _usa(s)
        as_provisional(s, "usa-women")  # as before USA Hockey's list (31.10.2026)
        old = sorted(rw._players(s, ev, usa), key=lambda e: e.athlete.full_name)
        assert len(old) == 25 and old[0].list_type == "provisional"
        keep = [
            {"name": e.athlete.full_name, "pos": e.role or "F", "number": 10 + i}
            for i, e in enumerate(old[:20])
        ]
        client = FakeClient(_mirror_pages(keep))
        report = rw.run(s, client=client, today=date(2026, 11, 2))
        s.commit()
        change = report["kader"]["USA"]
        assert change["status"] == "übernommen" and change["alt"] == 25 and change["neu"] == 20
        assert sorted(change["weg"]) == sorted(e.athlete.full_name for e in old[20:])
        st = tournament.roster_status(s, ev, usa)
        assert st["status"] == "offiziell" and st["anzahl"] == 20 and st["datum"] == "2026-11-02"
        assert st["quelle"].startswith("Kaderwatch: IIHF-Turnierkader") and "team-rosters/182" in st["url"]
        assert "5 Spielerinnen der früheren provisorischen Liste" in st["nicht_im_offiziellen_kader"]
        gone = roster_history(s, ev, usa)
        assert {h["name"] for h in gone if h["role"] in ("G", "D", "F")} == {
            e.athlete.full_name for e in old[20:]
        }
        assert all(s.get(m.Athlete, h["athlete_id"]) is not None for h in gone)  # athletes are never deleted
        staff = [
            e
            for e in s.scalars(
                select(m.EventEntry).where(m.EventEntry.event_id == ev.id, m.EventEntry.team_id == usa.id)
            )
            if e.role not in ("G", "D", "F", None)
        ]
        assert staff  # staff lines stay
        # next night: same list → nothing touched; the older seed (31.10.) never replaces it
        again = rw.run(s, client=client, today=date(2026, 11, 3))
        assert again["kader"]["USA"]["status"] == "unverändert"
        load_all_seeds(s)
        s.commit()
        assert tournament.roster_status(s, ev, usa)["anzahl"] == 20
        # a late replacement on the official list: one out, one in
        swap = [*keep[:-1], {"name": old[20].athlete.full_name, "pos": old[20].role or "F", "number": 99}]
        late = rw.run(s, client=FakeClient(_mirror_pages(swap)), today=date(2026, 11, 4))
        s.commit()
        assert late["kader"]["USA"]["status"] == "übernommen"
        assert late["kader"]["USA"]["weg"] == [keep[-1]["name"]]
        st = tournament.roster_status(s, ev, usa)
        assert any(keep[-1]["name"] in g for g in st["aus_offiziellem_kader_gestrichen"])


def test_official_list_losing_many_players_is_reported_not_applied(db, wch):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        usa = _usa(s)
        old = sorted(rw._players(s, ev, usa), key=lambda e: e.athlete.full_name)
        first = [{"name": e.athlete.full_name, "pos": e.role or "F"} for e in old[:23]]
        rw.run(s, client=FakeClient(_mirror_pages(first)), today=date(2026, 11, 2))
        odd = first[:10] + [{"name": f"Neue Spielerin{i}", "pos": "F"} for i in range(13)]
        r = rw.run(s, client=FakeClient(_mirror_pages(odd)), today=date(2026, 11, 3))
        assert r["kader"]["USA"]["status"].startswith("NICHT übernommen")
        assert tournament.roster_status(s, ev, usa)["anzahl"] == 23
        names = {e.athlete.full_name for e in rw._players(s, ev, usa)}
        assert names == {p["name"] for p in first}


def test_misspelled_name_matches_the_player_already_on_the_list(db, wch):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        swe = s.scalar(select(m.Team).where(m.Team.key == "swe-women"))
        old = rw._players(s, ev, swe)
        pairs = rw.resolve(
            s, [{"name": "Josefine Holngren", "pos": "D"}, {"name": "Lisa Ljungblom", "pos": "F"}], old
        )
        got = {s.get(m.Athlete, i).full_name for _p, i in pairs}
        assert got == {"Josefine Holmgren", "Lina Ljungblom"}


def test_newer_confirmed_seed_replaces_watcher_list_older_does_not(db, wch):
    with Session(db) as s:
        ev = s.get(m.Event, wch)
        usa = _usa(s)
        old = sorted(rw._players(s, ev, usa), key=lambda e: e.athlete.full_name)
        rw.run(
            s,
            client=FakeClient(_mirror_pages([{"name": e.athlete.full_name, "pos": "F"} for e in old[:23]])),
            today=date(2026, 11, 2),
        )
        seed = {
            "event": "wch-2026",
            "team": "usa-women",
            "list_type": "confirmed",
            "season": "2026-27",
            "source": "x",
            "source_url": "https://example.org/x",
        }
        players = [{"name": e.athlete.full_name, "pos": "F"} for e in old[:20]]
        load_roster_data(s, {**seed, "source_date": "2026-11-01", "players": players})
        assert tournament.roster_status(s, ev, usa)["anzahl"] == 23  # older than the watcher's list: skipped
        load_roster_data(s, {**seed, "source_date": "2026-11-05", "players": players})
        assert tournament.roster_status(s, ev, usa)["anzahl"] == 20


def test_blocked_site_is_named_and_not_worked_around(db, wch, as_provisional):
    pages = {WATCH["FIN"]["url"]: FakeResponse("<title>Just a moment...</title>", status=403)}
    client = FakeClient(pages)
    with Session(db) as s:
        as_provisional(s, "fin-women")
        r = rw.run(s, client=client, today=date(2026, 11, 2))
        assert "Bot-Schutz" in r["fehler"]["FIN"]
        assert client.calls.count(WATCH["FIN"]["url"]) == 1  # one polite request, no retry / other agent


def test_update_all_dry_run_lists_changes_per_team(db):
    with Session(db) as s:
        load_all_seeds(s)
        ev = s.scalar(select(m.Event).where(m.Event.key == "wch-2026"))
        can = s.scalar(select(m.Team).where(m.Team.key == "can-women"))
        # put the old camp list back to see the catch-up
        extra = s.scalar(select(m.Athlete).where(m.Athlete.full_name == "Brianne Jenner"))
        s.add(
            m.EventEntry(
                event_id=ev.id, athlete_id=extra.id, team_id=can.id, role="F", list_type="provisional"
            )
        )
        s.flush()
        out = rw.update_all(s, "wch-2026", web=False)
        row = next(t for t in out["teams"] if t["code"] == "CAN")
        assert row["vorher"].startswith("26") and row["nachher"] == "25 (offiziell)"
        assert row["weg"] == ["Brianne Jenner"] and row["datum"] == "2026-10-19"
        assert "hockeycanada.ca" in row["url"]
        s.rollback()
