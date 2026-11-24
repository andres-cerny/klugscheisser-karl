"""v11 part A: Elite Prospects page parser and import (facts, awards, season stats, stints, pronunciation,
handle candidates). No network: the pronunciation download uses a fake HTTP client."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.eliteprospects.parse import parse, save_pct, team_kind
from klugscheisser.db import models as m
from klugscheisser.ep_import import (
    SOURCE,
    handle_from_url,
    import_profiles,
    load_dir,
    value_key,
)
from klugscheisser.facts import queries
from klugscheisser.seed.loader import load_all_seeds
from klugscheisser.stats import queries as stats_q

FIXTURES = Path(__file__).parent / "fixtures" / "eliteprospects"
FILES = {
    "stalder": "stalder_skater",
    "brandli": "brandli_goalie",
    "meriguet": "meriguet_youth",
    "rochat": "rochat_retired",
    "mueller": "mueller_pwhl",
}


def _raw(key: str) -> dict:
    return json.loads((FIXTURES / f"{FILES[key]}.json").read_text())


class FakeResponse:
    def __init__(self, content: bytes, status: int = 200):
        self.content, self.status_code = content, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, content: bytes = b"ID3fake-mp3"):
        self.content, self.calls = content, []

    def get(self, url):
        self.calls.append(url)
        return FakeResponse(self.content)


@pytest.fixture
def seeded(db):
    with Session(db) as s:
        load_all_seeds(s)
        s.commit()
    return db


def _athlete(s: Session, raw: dict, ep_id: bool = True) -> m.Athlete:
    sport = s.scalar(select(m.Sport).where(m.Sport.key == "icehockey_women"))
    first, last = raw["name"].split(" ", 1)
    a = m.Athlete(
        sport_id=sport.id,
        full_name=raw["name"],
        first_name=first,
        last_name=last,
        name_normalized=raw["name"].lower(),
        nationality="SUI",
        eliteprospects_id=raw["eliteprospects_id"] if ep_id else None,
    )
    s.add(a)
    s.flush()
    return a


def _load(s: Session, tmp_path: Path, *keys: str) -> dict[str, m.Athlete]:
    """Persons for these fixtures, their pages written as raw files with the new ids, loaded via load_dir."""
    out = {}
    for key in keys:
        raw = _raw(key)
        a = _athlete(s, raw)
        raw["athlete_id"] = a.id
        (tmp_path / f"{a.id}.json").write_text(json.dumps(raw))
        out[key] = a
    counts = load_dir(s, tmp_path)
    assert counts["loaded"] == len(keys)
    return out


def _facts(s: Session, a: m.Athlete, kind: str, source: str = SOURCE) -> list[m.Fact]:
    return list(
        s.scalars(
            select(m.Fact)
            .where(m.Fact.athlete_id == a.id, m.Fact.kind == kind, m.Fact.source == source)
            .order_by(m.Fact.id)
        )
    )


def _snapshot(s: Session) -> dict:
    facts = sorted(
        (f.id, f.athlete_id, f.kind, json.dumps(f.value, sort_keys=True), f.value_text, f.status, f.season)
        for f in s.scalars(select(m.Fact))
    )
    stats = sorted(
        (
            r.id,
            r.athlete_id,
            r.season,
            r.league,
            r.team_name,
            r.phase,
            r.games,
            r.points,
            r.save_pct,
            r.team_kind,
        )
        for r in s.scalars(select(m.SeasonStat))
    )
    stints = sorted(
        (st.id, st.team_name, st.kind, st.season_from, st.season_to, st.quote, st.team_id)
        for st in s.scalars(select(m.CareerStint))
    )
    handles = sorted((h.id, h.handle, h.status, json.dumps(h.evidence)) for h in s.scalars(select(m.Handle)))
    data = sorted((p.athlete_id, json.dumps(p.data, sort_keys=True)) for p in s.scalars(select(m.EpProfile)))
    return {"facts": facts, "stats": stats, "stints": stints, "handles": handles, "data": data}


# --- parser -------------------------------------------------------------------------------------------------


def test_parse_skater_stalder():
    r = parse(_raw("stalder"))
    p = r.profile
    assert (r.athlete_id, r.ep_id) == (34, "369459")
    assert r.url == "https://www.eliteprospects.com/player/369459/lara-stalder"
    assert (p.place_of_birth, p.birth_country, p.place_of_birth_raw) == ("Luzern", "SUI", "Luzern, SUI")
    assert (p.height_cm, p.weight_kg, p.position, p.detailed_position) == (167, 63, "F", ["C"])
    assert (p.shoots, p.catches, p.youth_team, p.status) == ("R", False, "HC Luzern", "active")
    assert (p.contract, p.contract_season_to) == ("27/28", 2027)
    assert p.date_of_birth.isoformat() == "1994-05-15"
    assert r.latest["team"] == "EVZ Women's Team" and r.latest["jersey_number"] == 7
    assert r.latest["season"] == 2026
    assert set(r.links) == {"instagram", "x", "facebook", "linkedin", "youtube"}
    assert r.form["last5"]["points"] == 7 and r.form["last10"]["games"] == 10
    assert [(d.league, d.year, d.round, d.overall, d.team) for d in r.drafts] == [
        ("NWHL", 2016, 5, 20, "Boston Pride")
    ]
    assert [(x.relation, x.name) for x in r.relatives] == [("Bruder", "Dario Stalder")]
    # the projected 2026/27 row (no team) is dropped, never a fact
    assert r.projected_dropped == 1
    assert all(row.team for row in r.seasons)
    most = next(a for a in r.awards if a.name == "SDHL (W) Most Points" and a.season == 2021)
    assert (most.number, most.detail, most.league) == ("89", None, "SDHL (W)")
    medal = next(a for a in r.awards if a.name == "Olympic (W) Bronze Medal")
    assert (medal.medal, medal.event, medal.season) == ("bronze", "Olympische Spiele", 2013)
    assert not next(a for a in r.awards if a.name == "SDHL (W) SM-silver Medal").medal  # not an IIHF medal
    assert next(a for a in r.awards if a.name == "LKA (W) Champion").detail == "ZSC Lions Frauen"
    ext = r.transfers[-1]
    assert (ext.type, ext.extra, ext.from_team, ext.to_team) == (
        "extension",
        "2 years",
        "EVZ Women's Team",
        "EVZ Women's Team",
    )
    assert ext.source.startswith("https://www.evz.ch/")


def test_parse_goalie_brandli():
    r = parse(_raw("brandli"))
    assert r.is_goalie and r.profile.shoots == "L" and r.profile.catches
    sdhl = next(x for x in r.seasons if x.team == "Frölunda HC" and x.phase == "regular")
    assert (sdhl.games, sdhl.wins, sdhl.losses, sdhl.shutouts, sdhl.goals_against, sdhl.saves) == (
        22,
        17,
        5,
        4,
        33,
        571,
    )
    assert sdhl.save_pct == 0.945 and sdhl.gaa == 1.49 and sdhl.toi == "1329:31"
    assert sdhl.goals is None and sdhl.points is None  # goalie: no skater values, never 0
    po = next(x for x in r.seasons if x.team == "Frölunda HC" and x.phase == "playoffs")
    assert (po.games, po.save_pct, po.postseason_type) == (12, 0.918, "playoffs")
    # dressed but did not play: GP 0 stays 0, missing values stay None
    wc = next(x for x in r.seasons if x.season == 2015 and x.league == "WC (W)")
    assert wc.games == 0 and wc.save_pct is None and wc.wins is None
    # GP only (old season): the rest is "no data"
    old = next(x for x in r.seasons if x.season == 2009)
    assert old.games == 1 and old.goals is None and old.gaa is None
    assert r.form["last5"]["save_pct"] == 0.9184 and r.form["last5"]["saves"] == 135
    assert save_pct(".918") == 0.918 and save_pct("1.000") == 1.0 and save_pct(None) is None
    assert r.drafts[0].team == "PWHL Detroit" and r.drafts[0].overall == 15
    assert r.links == {}  # empty strings are no links


def test_parse_youth_meriguet_kinds():
    r = parse(_raw("meriguet"))
    assert r.projected_dropped == 2 and all(x.team for x in r.seasons)
    assert r.profile.contract == "Junior" and r.profile.contract_season_to is None
    assert r.profile.secondary_nationality == "France"
    kinds = {(x.team, x.league): x.team_kind for x in r.seasons}
    assert kinds[("Genève Future Hockey U18", "U18-Elit")] == "youth"
    assert kinds[("Switzerland U18", "WJC-18 (W)")] == "national"
    assert kinds[("Switzerland Olympics", "OG (W)")] == "national"
    assert kinds[("Lausanne HC Féminin", "SWHL A (W)")] == "club"
    agg = next(x for x in r.seasons if x.team == "Switzerland (all)")
    assert agg.aggregate and agg.team_kind == "national" and agg.league == "International (W)"


def test_team_kind_rules():
    assert team_kind("Northeastern Univ.", "NCAA (W)") == "college"
    assert team_kind("EHC Winterthur U15", "Mini Top") == "youth"
    assert team_kind("EHC Kloten U17", "Novizen Elite") == "youth"
    assert team_kind("Switzerland", "EHT (W)") == "national"
    assert team_kind("Switzerland U16", "EWIHC") == "national"
    assert team_kind("Czechia U18 (women) (all)", "International-Jr (W)") == "national"
    assert team_kind("Canada (AMHL)", "AMHL") == "youth"
    assert team_kind("Eisbären Juniors Berlin", "DFEL (W)") == "club"
    assert team_kind("HC Slavia Praha", "Czechia (W)") == "club"  # a country's league, not its national team
    assert team_kind("Stanstead College Varsity", "CAHS (W)") == "youth"


def test_parse_retired_rochat_and_pwhl_mueller():
    r = parse(_raw("rochat"))
    assert r.profile.status == "retired" and r.profile.place_of_birth == "Summit"
    assert (r.profile.birth_region, r.profile.birth_country) == ("NJ", "USA")
    assert r.latest["season"] == 1995 and r.form == {} and r.drafts == []
    assert all(a.medal is None for a in r.awards)  # division I medals are not WM medals
    mu = parse(_raw("mueller"))
    assert [(d.league, d.year, d.round, d.overall, d.team) for d in mu.drafts] == [
        ("PWHL", 2023, 1, 3, "Boston Fleet")
    ]
    assert [(x.relation, x.name, x.relation_en) for x in mu.relatives] == [
        ("Bruder", "Mirco Müller", "Brother")
    ]
    ham = mu.transfers[-1]
    assert (ham.to_team, ham.date.isoformat(), ham.extra) == ("PWHL Hamilton", "2026-06-09", "3 years")
    assert "thepwhl.com" in ham.source
    assert mu.links == {"instagram": "https://www.instagram.com/alinamueller_/"}
    assert mu.profile.pronunciation_url.endswith("alina_muller.mp3")


def test_relatives_lists():
    from klugscheisser.collectors.eliteprospects.parse import relatives

    html = (
        'Father: <a href="/staff.php?staff=1">Magnus Hallin</a><br>\r\nBrothers: <a href="/p?1">Elias Hallin</a>, '
        '<a href="/p?2">Hugo Hallin</a><br>Goalie profile: <a href="/p?3">Klára H</a>'
    )
    assert [(x.relation, x.name) for x in relatives(html)] == [
        ("Vater", "Magnus Hallin"),
        ("Bruder", "Elias Hallin"),
        ("Bruder", "Hugo Hallin"),
    ]


# --- import -------------------------------------------------------------------------------------------------


def test_import_writes_facts_stats_stints(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "stalder", "brandli", "mueller")
        counts = import_profiles(s, http=FakeHttp())
        assert counts["persons"] == 3 and not counts.get("errors")
        lara = p["stalder"]
        url = "https://www.eliteprospects.com/player/369459/lara-stalder"
        one = {
            k: _facts(s, lara, k)
            for k in (
                "height_cm",
                "weight_kg",
                "position",
                "shoots",
                "birth_place",
                "youth_team",
                "current_team",
                "jersey_number",
                "contract",
            )
        }
        assert one["height_cm"][0].value == {"number": 167} and one["weight_kg"][0].value_text == "63 kg"
        assert one["position"][0].value == {"text": "Stürmerin", "code": "F", "detail": "C"}
        assert one["position"][0].value_text == "Stürmerin (Center)"
        assert one["shoots"][0].value_text == "rechts"
        assert one["birth_place"][0].value_text == "Luzern (SUI)"
        assert one["current_team"][0].value_text == "EVZ Women's Team (SWHL A, 2026/27)"
        assert one["jersey_number"][0].value == {"number": 7, "team": "EVZ Women's Team"}
        assert one["contract"][0].value == {"text": "27/28", "season_to": 2027}
        assert one["contract"][0].value_text == "2027/28"
        for facts in one.values():
            f = facts[0]
            assert (f.source, f.extractor, f.source_url, f.status, f.confidence) == (
                SOURCE,
                SOURCE,
                url,
                "confirmed",
                1.0,
            )
        award = next(f for f in _facts(s, lara, "award") if f.value_text.startswith("SDHL Most Points (89)"))
        assert award.value_text == "SDHL Most Points (89), 2021/22"
        assert award.value == {
            "text": "SDHL Most Points",
            "year": 2022,
            "season": 2021,
            "number": "89",
            "league": "SDHL (W)",
        }
        assert any(f.value_text == "SDHL SM-Silber, 2017/18" for f in _facts(s, lara, "award"))
        medal = _facts(s, lara, "medal")
        assert [(f.value["metal"], f.value["year"], f.value_text) for f in medal] == [
            ("bronze", 2014, "Olympische Spiele 2014: Bronze"),
            ("bronze", 2026, "Olympische Spiele 2026: Bronze"),
        ]
        fam = _facts(s, lara, "family_in_sport")[0]
        assert fam.value == {"text": "Bruder: Dario Stalder", "relation": "Bruder", "person": "Dario Stalder"}
        assert queries.facts_for(s, [lara.id], "family_in_sport")[0].vorsicht  # sensitive kind
        draft = _facts(s, lara, "draft")[0]
        assert draft.value_text == "Gedraftet von Boston Pride (NWHL-Draft 2016, Runde 5, Nr. 20)"

        # goalie season stats: values as on the page, empty stays empty
        andrea = p["brandli"]
        rows = {
            (r.season, r.team_name, r.phase): r
            for r in s.scalars(select(m.SeasonStat).where(m.SeasonStat.athlete_id == andrea.id))
        }
        fro = rows[(2025, "Frölunda HC", "regular")]
        assert (fro.games, fro.wins, fro.save_pct, fro.gaa, fro.toi, fro.goals, fro.team_kind) == (
            22,
            17,
            0.945,
            1.49,
            "1329:31",
            None,
            "club",
        )
        assert (fro.source, fro.source_url) == (
            SOURCE,
            "https://www.eliteprospects.com/player/295222/andrea-brandli",
        )
        assert rows[(2025, "Frölunda HC", "playoffs")].games == 12
        assert rows[(2018, "Ohio State Univ.", "regular")].team_kind == "college"
        assert all(r.team_name for r in rows.values())
        assert _facts(s, andrea, "shoots")[0].value_text == "links (Fanghand)"
        # shared stats queries work on the rows; the "(all)" rows can be left out by league
        clubs = stats_q.totals(s, andrea.id, by="team", team_kind="club")
        assert any(t["team"] == "Frölunda HC" for t in clubs)
        nat = [
            r
            for r in stats_q.season_rows(s, andrea.id, team_kind="national")
            if not r.league.startswith("International")
        ]
        assert all("(all)" not in r.team_name for r in nat)

        # stints: per team over consecutive seasons, open when it reaches the current season
        stints = {
            (st.team_name, st.season_from): st
            for st in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == p["mueller"].id))
        }
        ham = stints[("PWHL Hamilton", 2026)]
        assert ham.season_to is None and ham.kind == "club" and ham.source == SOURCE
        assert "Transfer 09.06.2026: Boston Fleet → PWHL Hamilton (3 years)" in ham.quote
        assert "thepwhl.com" in ham.quote
        neu = stints[("Northeastern Univ.", 2018)]
        assert (neu.kind, neu.season_to) == ("college", 2022)
        assert "College Commitment" in neu.quote and "twitter.com" in neu.quote
        nati = stints[("Switzerland", 2013)]
        assert nati.kind == "national" and nati.season_to is None
        assert nati.team_id == s.scalar(select(m.Team.id).where(m.Team.key == "sui-women"))
        assert stints[("Switzerland U18", 2012)].season_to == 2015
        # v12: the renamed team (PWHL Boston -> Boston Fleet) is ONE stint through EP's team id, from–until,
        # under its newest name, with the earlier name and the transfer in the quote
        boston = stints[("Boston Fleet", 2023)]
        assert (boston.ep_team_id, boston.season_from, boston.season_to) == ("37963", 2023, 2025)
        assert "Früher: PWHL Boston" in boston.quote and "Transfer 30.10.2023" in boston.quote
        assert ("PWHL Boston", 2023) not in stints
        assert all(st.status == "confirmed" for st in stints.values())

        prof = s.get(m.EpProfile, p["mueller"].id)
        assert set(prof.data) >= {"form", "career_totals", "highlights", "links", "latest", "transfers"}
        assert prof.data["latest"]["team"] == "PWHL Hamilton" and prof.imported_at is not None
        assert prof.data["form"]["last5"]["goals"] == 3
        assert prof.data["transfers"][-1]["source"].startswith("https://www.thepwhl.com/")
        assert any(t["league"] == "PWHL (W)" for t in prof.data["career_totals"])
        assert "iconUrl" not in json.dumps(prof.data) and "imageUrl" not in json.dumps(prof.data)


def test_import_is_idempotent(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "stalder", "mueller", "rochat")
        other = s.get(m.Athlete, p["rochat"].id)
        s.add(
            m.Fact(
                athlete_id=p["stalder"].id,
                kind="height_cm",
                value={"number": 166},
                value_text="166 cm",
                source="wikidata",
                source_url="https://www.wikidata.org/wiki/Q15743541",
                status="confirmed",
            )
        )
        s.flush()
        first = import_profiles(s, http=FakeHttp())
        before = _snapshot(s)
        second = import_profiles(s, http=FakeHttp())
        assert _snapshot(s) == before
        assert not second.get("facts_added") and second.get("facts_removed", 0) == 0
        assert not second.get("season_stats_added") and not second.get("stints_added")
        assert first["facts_added"] > 50 and first["conflicts"] == 1 and not second.get("conflicts")
        # loading the same files again changes nothing either
        assert load_dir(s, tmp_path)["unchanged"] == 3
        # retired: no current team, no jersey, closed stints
        assert not _facts(s, other, "current_team") and not _facts(s, other, "jersey_number")
        assert all(
            st.season_to is not None
            for st in s.scalars(select(m.CareerStint).where(m.CareerStint.athlete_id == other.id))
        )


def test_conflicts_marked_and_restored(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "mueller")
        a = p["mueller"]

        def fact(kind, value, text, source="wikidata", status="confirmed"):
            f = m.Fact(
                athlete_id=a.id,
                kind=kind,
                value=value,
                value_text=text,
                source=source,
                source_url="https://example.org",
                status=status,
            )
            s.add(f)
            s.flush()
            return f

        tall = fact("height_cm", {"number": 176}, "176 cm")
        same_pos = fact("position", {"code": "F", "text": "Stürmerin"}, "Stürmerin", source="web")
        place = fact("birth_place", {"text": "Lengnau"}, "Lengnau")
        hand = fact("shoots", {"text": "links"}, "links", source="web")
        draft = fact("draft", {"team": "Boston Fleet", "year": 2023}, "Gedraftet von Boston Fleet (2023)")
        story = fact("college", {"text": "Northeastern"}, "Northeastern")  # not an EP-first kind
        unconf = fact("height_cm", {"number": 150}, "150 cm", source="web", status="unconfirmed")
        import_profiles(s, http=FakeHttp())
        assert (tall.status, place.status) == ("conflict", "conflict")
        assert (same_pos.status, hand.status, draft.status, story.status) == ("confirmed",) * 4
        assert unconf.status == "unconfirmed"
        row = queries.facts_for(s, [a.id], "height_cm")[0]
        assert row.source == SOURCE and row.value_text == "167 cm"
        assert [c.value_text for c in row.conflicts] == ["176 cm"]  # the other value stays visible
        assert sorted(s.get(m.EpProfile, a.id).data["conflicts"]) == sorted([tall.id, place.id])
        # the other source corrects its value -> agrees -> restored on the next import
        tall.value, tall.value_text = {"number": 167}, "167 cm"
        s.flush()
        counts = import_profiles(s, http=FakeHttp())
        assert counts["restored"] == 1 and tall.status == "confirmed" and place.status == "conflict"
        # a conflict that someone else set is left alone
        assert s.get(m.EpProfile, a.id).data["conflicts"] == [place.id]
        # other sources' facts are never deleted
        assert s.get(m.Fact, story.id) is not None and s.get(m.Fact, unconf.id) is not None


def test_award_dedupe_against_other_sources(seeded, tmp_path):
    with Session(seeded) as s:
        raw = _raw("mueller")
        a = _athlete(s, raw)
        raw["athlete_id"] = a.id
        (tmp_path / f"{a.id}.json").write_text(json.dumps(raw))
        load_dir(s, tmp_path)
        rookie = m.Fact(
            athlete_id=a.id,
            kind="award",
            value={"text": "PWHL All-Rookie Team", "year": 2024},
            value_text="PWHL All-Rookie Team",
            source="web",
            source_url="https://x.org",
            status="confirmed",
        )
        bronze = m.Fact(
            athlete_id=a.id,
            kind="medal",
            value={"text": "Olympische Bronzemedaille (2014)", "metal": "bronze", "year": 2014},
            value_text="Olympische Bronzemedaille (2014)",
            source="wikidata",
            source_url="https://www.wikidata.org/wiki/Q15768448",
            status="confirmed",
        )
        s.add_all([rookie, bronze])
        s.flush()
        counts = import_profiles(s, http=FakeHttp())
        assert counts["award_duplicates"] == 2
        texts = [f.value_text for f in _facts(s, a, "award")]
        assert not any("All-Rookie" in t for t in texts)  # not a second visible row
        assert [f.value["year"] for f in _facts(s, a, "medal")] == [2026]  # 2014 is Wikidata's
        dup = s.get(m.EpProfile, a.id).data["duplicates"]
        assert {d["same_as_fact_id"] for d in dup} == {rookie.id, bronze.id}
        # Erfolge: medals first, each medal once
        from klugscheisser.web.card_data import achievements

        erfolge = achievements(s, a)
        medals = [x for x in erfolge if x["kind"] == "medal"]
        assert [x["year"] for x in medals] == [2026, 2014] and erfolge[: len(medals)] == medals
        assert counts["award_duplicates"] == import_profiles(s, http=FakeHttp())["award_duplicates"]


def test_handle_candidates_with_evidence(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "stalder", "mueller")
        mu = p["mueller"]
        known = m.Handle(
            athlete_id=mu.id,
            platform="instagram",
            handle="alinamueller_",
            status="verified",
            confidence=0.9,
            evidence=[{"kind": "wikidata", "detail": "Wikidata Q15768448"}],
        )
        s.add(known)
        s.flush()
        import_profiles(s, http=FakeHttp())
        import_profiles(s, http=FakeHttp())
        assert known.status == "verified"
        assert [e["kind"] for e in known.evidence] == ["wikidata", "eliteprospects"]  # appended once
        lara = {
            h.platform: h for h in s.scalars(select(m.Handle).where(m.Handle.athlete_id == p["stalder"].id))
        }
        assert set(lara) == {"instagram", "x"}
        assert (lara["instagram"].handle, lara["x"].handle) == ("lara722", "lara72122")
        for h in lara.values():
            assert h.status == "candidate" and not h.usable
            assert h.evidence == [
                {
                    "kind": "eliteprospects",
                    "detail": "auf dem Elite-Prospects-Profil verlinkt",
                    "url": "https://www.eliteprospects.com/player/369459/lara-stalder",
                }
            ]
    assert handle_from_url("x", "https://x.com/@Foo_Bar") == "foo_bar"
    assert handle_from_url("instagram", "https://www.instagram.com/p/abc/") is None


def test_pronunciation_download_and_wikimedia_wins(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "mueller", "brandli", "stalder")
        mu, an = p["mueller"], p["brandli"]
        s.add(
            m.NamePronunciation(
                athlete_id=mu.id,
                source="tts",
                label="Computerstimme (Deutsch)",
                mime="audio/wav",
                audio=b"RIFF",
                hint="MÜ-ler",
                language="de-CH",
            )
        )
        s.add(
            m.NamePronunciation(
                athlete_id=an.id,
                source="wikimedia",
                label="Aufnahme (Wikimedia Commons)",
                mime="audio/ogg",
                audio=b"OggS",
            )
        )
        s.flush()
        http = FakeHttp(b"ID3-alina")
        counts = import_profiles(s, http=http)
        assert http.calls == ["https://files.eliteprospects.com/layout/player_audio/alina_muller.mp3"]
        assert counts["audio"] == 1 and counts["audio_skipped_wikimedia"] == 1
        row = s.get(m.NamePronunciation, mu.id)
        assert (row.source, row.mime, row.label, row.audio) == (
            SOURCE,
            "audio/mpeg",
            "Aufnahme (Elite Prospects)",
            b"ID3-alina",
        )
        assert row.source_url.endswith("alina_muller.mp3") and row.hint == "MÜ-ler"  # hint kept
        assert s.get(m.NamePronunciation, an.id).source == "wikimedia"
        assert s.get(m.NamePronunciation, p["stalder"].id) is None  # no recording on her page
        import_profiles(s, http=http)
        assert len(http.calls) == 1  # stored once, never fetched again
        # --no-audio: nothing is downloaded
        s.delete(row)
        s.flush()
        silent = FakeHttp()
        import_profiles(s, http=silent, audio=False)
        assert silent.calls == []


def test_nothing_written_for_other_persons(seeded, tmp_path):
    with Session(seeded) as s:
        p = _load(s, tmp_path, "stalder")
        bystander = _athlete(s, {"name": "Evelina Raselli", "eliteprospects_id": "1"})
        # a raw file whose id belongs to someone else in this database (e.g. another project's ids)
        stray = _raw("mueller")
        stray["athlete_id"] = bystander.id
        (tmp_path / f"{bystander.id}.json").write_text(json.dumps(stray))
        counts = load_dir(s, tmp_path)
        assert counts["other_person"] == 1 and s.get(m.EpProfile, bystander.id) is None
        import_profiles(s, athlete_ids=[p["stalder"].id], http=FakeHttp())
        for model in (m.Fact, m.SeasonStat, m.CareerStint, m.Handle):
            assert not s.scalars(select(model).where(model.athlete_id == bystander.id)).all()
        assert {f.athlete_id for f in s.scalars(select(m.Fact).where(m.Fact.source == SOURCE))} == {
            p["stalder"].id
        }


def test_value_keys():
    assert value_key("birth_place", {"text": "Prag 2"}) == value_key("birth_place", {"text": "Praha"})
    assert value_key("birth_place", {"text": "Wald ZH"}) == "wald"
    assert value_key("birth_place", {"text": "Själevads församling"}) == value_key(
        "birth_place", {"text": "Själevad"}
    )
    assert value_key("birth_place", {"text": "Schweiz"}) is None  # a country is not a birth place
    assert value_key("shoots", {"text": "L"}) == value_key("shoots", {"text": "links"}) == "L"
    assert value_key("position", {"text": "Center"}) == "F"
