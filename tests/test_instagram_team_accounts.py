"""Official Instagram team accounts (INSTAGRAM_TEAM_ACCOUNTS, default @swissicehockeywomen): read nightly, only
new posts, stored as official items linked to the women's national team, counted in the nightly plan and the
Apify caps; tags by the team account become "team_tags" evidence. Offline, no paid call."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.instagram.base import FetchResult, InstagramPost
from klugscheisser.config import Settings
from klugscheisser.db import models as m
from klugscheisser.db import repo
from klugscheisser.db.session import session_scope
from klugscheisser.domain.labels import TZ
from klugscheisser.ingest import costs
from klugscheisser.ingest.instagram import TEAM_SOURCE, ingest_instagram_teams, team_for_account
from klugscheisser.ingest.runner import build_context
from klugscheisser.jobs import nightly as nj
from klugscheisser.jobs.refresh import touch

TEAM = "swissicehockeywomen"
# one team account (the default list has the league/club accounts too, see test_default_team_accounts)
SETTINGS = Settings(
    _env_file=None, instagram_source="fixture", x_team_accounts=[], instagram_team_accounts=[TEAM]
)
NIGHT = datetime(2026, 10, 12, 5, 0, tzinfo=TZ)


def post(pid, caption, owner=TEAM, tagged=(), days_ago=1):
    return InstagramPost(
        post_id=pid,
        shortcode=pid,
        url=f"https://www.instagram.com/p/{pid}/",
        owner_username=owner,
        owner_full_name="Swiss Ice Hockey Women" if owner == TEAM else "Someone",
        published_at=datetime.now(UTC) - timedelta(days=days_ago),
        caption=caption,
        tagged=list(tagged),
    )


class FakeSource:
    def __init__(self, posts, name="fixture", cost=0.0):
        self.name, self.posts, self.cost, self.calls = name, posts, cost, []

    def fetch_posts(self, handles, since, limit=12, max_usd=None):
        self.calls.append({"handles": handles, "since": since, "limit": limit, "max_usd": max_usd})
        return FetchResult(posts={h: list(self.posts) for h in handles}, cost_usd=self.cost, source=self.name)


@pytest.fixture
def world(db):
    """The women's national team as in seeds/teams.yaml, but seeded before it had its instagram_handle; one
    player with a verified handle and one with a mere candidate."""
    with Session(db) as s:
        sport = repo.get_sport(s, "icehockey_women")
        s.add(m.Team(sport_id=sport.id, key="sui-women", name="Schweiz", short_name="SUI", kind="national"))
        lara = repo.upsert_athlete(s, sport, "Lara Testalder", nationality="SUI")
        s.add(m.Handle(athlete=lara, platform="instagram", handle="lara_t", status="verified"))
        nina = repo.upsert_athlete(s, sport, "Nina Kandidat", nationality="SUI")
        s.add(
            m.Handle(
                athlete=nina,
                platform="instagram",
                handle="nina.k",
                status="candidate",
                evidence=[{"kind": "name_in_handle", "detail": "Name im Handle"}],
            )
        )
        s.commit()
    return db


def run(source, settings=SETTINGS, accounts=None):
    with session_scope() as s:
        return ingest_instagram_teams(build_context(s, settings), source, accounts)


def test_default_team_accounts():
    # national team + Swiss league/club/federation accounts (replace most Apify "mentions", 2026-10-30)
    assert Settings(_env_file=None).instagram_team_accounts == [
        TEAM,
        "postfinance_womensleague",
        "fr_gotteron_ladies",
        "lionsfrauen",
        "scb_frauen_official",
        "swissicehockeyfederation",
    ]


def test_team_resolved_from_seed_when_db_lacks_handle(world):
    with session_scope() as s:
        team = team_for_account(s, "SwissIceHockeyWomen")
        assert team.key == "sui-women" and team.instagram_handle == TEAM
        assert team_for_account(s, "unknown_account") is None


def test_posts_stored_as_official_team_items(world):
    src = FakeSource(
        [
            post("T1", "Trainingslager in Vierumäki 🇨🇭", tagged=["lara_t"]),
            post("T2", "Kader für die Euro Hockey Tour: Nina Kandidat dabei", tagged=["nina.k"]),
            post("F1", "Fan-Post", owner="fan_account", tagged=[TEAM]),
        ]
    )
    r = run(src)
    assert (r.handles, r.posts_seen, r.items_new, r.team_posts_new) == (1, 3, 3, 2)
    assert src.calls[0]["handles"] == [TEAM] and src.calls[0]["limit"] == SETTINGS.instagram_team_max_posts
    first_since = src.calls[0]["since"]
    assert first_since < datetime.now(UTC) - timedelta(days=SETTINGS.instagram_first_run_days - 1)
    with session_scope() as s:
        team = repo.get_team(s, "sui-women")
        t1 = s.scalar(select(m.Item).where(m.Item.url.like("%/T1/")))
        assert t1.source_kind == "official" and t1.author == TEAM
        assert team.id in {t.team_id for t in t1.teams}
        assert {ia.athlete.full_name for ia in t1.athletes} == {"Lara Testalder"}
        t2 = s.scalar(select(m.Item).where(m.Item.url.like("%/T2/")))
        assert "Nina Kandidat" in {ia.athlete.full_name for ia in t2.athletes}  # named in the caption
        fan = s.scalar(select(m.Item).where(m.Item.url.like("%/F1/")))
        assert fan.source_kind != "official" and team.id not in {t.team_id for t in fan.teams}
        acc = s.get(m.Account, ("instagram", TEAM))
        assert acc.kind == "official" and acc.display_name == "Swiss Ice Hockey Women"
        assert s.get(m.CacheEntry, (TEAM_SOURCE, TEAM)) is not None


def test_incremental_and_not_twice_a_night(world):
    src = FakeSource([post("T1", "Spieltag")])
    run(src)
    assert run(src).handles == 0 and len(src.calls) == 1  # fetched a moment ago: skipped
    with session_scope() as s:
        entry = s.get(m.CacheEntry, (TEAM_SOURCE, TEAM))
        entry.fetched_at = datetime.now(UTC) - timedelta(days=1)
        last = entry.fetched_at
    r = run(src)
    assert len(src.calls) == 2 and src.calls[1]["since"] == last - timedelta(days=1)  # only newer posts
    assert r.items_new == 0 and r.team_posts_new == 0  # same post again: no duplicate
    with session_scope() as s:
        assert len(s.scalars(select(m.Item)).all()) == 1


def test_team_tags_evidence_once_per_account(world):
    src = FakeSource(
        [post("T1", "Gratulation", tagged=["nina.k"]), post("T2", "Nochmals", tagged=["nina.k"])]
    )
    r = run(src)
    assert r.team_tags == 1
    with session_scope() as s:
        h = s.scalar(select(m.Handle).where(m.Handle.handle == "nina.k"))
        tags = [e for e in h.evidence if e["kind"] == "team_tags"]
        assert len(tags) == 1 and tags[0]["detail"] == f"markiert von @{TEAM}"
        # name_in_handle + team_tags = 0.48: still a candidate (never scraped); one more signal is needed
        assert h.confidence == pytest.approx(1 - 0.8 * 0.65) and h.status == "candidate"


def test_paid_fetch_inside_apify_cap(world):
    paid = FakeSource([post("T1", "Spieltag")], name="apify", cost=0.01)
    r = run(paid)
    assert r.cost_usd == pytest.approx(0.01) and paid.calls[0]["max_usd"] is not None
    with session_scope() as s:
        assert costs.spent_today(s, "apify") == pytest.approx(0.01)
        s.get(m.CacheEntry, (TEAM_SOURCE, TEAM)).fetched_at = datetime.now(UTC) - timedelta(days=1)
        costs.record(s, "apify", 1, 1.99, "earlier today")
    capped = SETTINGS.model_copy(update={"apify_daily_cost_cap_usd": 2.0})
    r = run(paid, capped)
    assert r.skipped_budget == [TEAM] and len(paid.calls) == 1


# --- nightly plan ----------------------------------------------------------------------------------------


def test_plan_counts_team_account_in_estimate_and_cycle_guard(world):
    price = SETTINGS.apify_cost_per_post_usd
    with session_scope() as s:
        p = nj.plan(s, SETTINGS, NIGHT)
        assert p.ig_team == {TEAM: pytest.approx(12 * price)}  # first read: up to 12 posts
        assert p.apify_usd == pytest.approx(12 * price)
        assert p.out()["instagram_teamaccounts_usd"] == {f"@{TEAM}": round(12 * price, 3)}
        none = SETTINGS.model_copy(update={"instagram_team_accounts": []})
        assert nj.plan(s, none, NIGHT).apify_usd == 0 and nj.plan(s, none, NIGHT).ig_team == {}
        # read before, no posts stored: one result
        touch(s, TEAM_SOURCE, TEAM)
        assert nj.plan(s, SETTINGS, NIGHT).ig_team[TEAM] == pytest.approx(price)
        # the Apify cycle guard sees it
        costs.record(s, "apify", 1, 17.999, "cycle")
        tight = SETTINGS.model_copy(update={"apify_cycle_cap_usd": 18.0})
        assert any("Apify-Monat" in x for x in nj.plan(s, tight, NIGHT).problems)


def test_nightly_step_reads_team_accounts(world, monkeypatch):
    src = FakeSource([post("T1", "Spieltag")])
    monkeypatch.setattr("klugscheisser.collectors.instagram.sources.get_instagram_source", lambda st: src)
    with session_scope() as s:
        p = nj.plan(s, SETTINGS, NIGHT)
    out = nj._instagram(p, SETTINGS)
    assert out["team_posts_new"] == 1 and src.calls[0]["handles"] == [TEAM]
