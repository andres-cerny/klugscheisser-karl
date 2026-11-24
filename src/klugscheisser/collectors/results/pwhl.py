"""PWHL via the HockeyTech feed that thepwhl.com itself uses (lscluster.hockeytech.com).

- Seasons:  feed=modulekit&view=seasons
- Schedule: feed=modulekit&view=schedule&season_id={id}
- Game summary: feed=gc&tab=gamesummary&game_id={id}
- Public game page: https://www.thepwhl.com/en/stats/game-center/{gameId}

The key is the public one embedded in thepwhl.com (documented in open-source projects), not officially
issued; PWHL terms forbid automated collection. On for the private PoC only, low volume, and only for
teams with Swiss players (settings.pwhl_teams).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta

from klugscheisser.collectors.results.base import KnownFinal, PoliteHttp, in_window, ot_suffix, wants
from klugscheisser.interfaces import GameResult, Participant

log = logging.getLogger(__name__)

FEED = "https://lscluster.hockeytech.com/feed/index.php"
GAME_URL = "https://www.thepwhl.com/en/stats/game-center/{game_id}"
PERIOD_DE = {"1": "1. Drittel", "2": "2. Drittel", "3": "3. Drittel"}


def _name(p: dict | None) -> str | None:
    if not p or not p.get("last_name"):
        return None
    return " ".join(filter(None, [p.get("first_name"), p.get("last_name")]))


def active_seasons(seasons: list[dict], since: date, until: date) -> list[dict]:
    """Seasons whose date range overlaps [since, until]."""
    out = []
    for s in seasons:
        try:
            start, end = date.fromisoformat(s["start_date"]), date.fromisoformat(s["end_date"])
        except (KeyError, ValueError):
            continue
        if start <= until and end >= since:
            out.append(s)
    return out


def _status(g: dict) -> str:
    if str(g.get("final")) == "1":
        return "final"
    return "live" if str(g.get("started")) == "1" else "scheduled"


def parse_schedule(data: dict, competition: str) -> list[GameResult]:
    games = []
    for g in (data.get("SiteKit") or {}).get("Schedule") or []:
        st = _status(g)
        start = g.get("GameDateISO8601") or g.get("date_time_played")
        score = None
        if st != "scheduled":
            sfx = ot_suffix(
                0, shootout=str(g.get("shootout")) == "1", overtime=str(g.get("overtime")) not in ("0", "")
            )
            score = f"{g.get('home_goal_count')}:{g.get('visiting_goal_count')}{sfx}"
        games.append(
            GameResult(
                source="PWHL",
                sport="icehockey_women",
                competition=competition,
                external_id=str(g["game_id"]),
                url=GAME_URL.format(game_id=g["game_id"]),
                status=st,
                starts_at=datetime.fromisoformat(start.replace("Z", "+00:00")) if start else None,
                home=g.get("home_team_name"),
                away=g.get("visiting_team_name"),
                score=score,
                venue=(g.get("venue_name") or "").split(" | ")[0] or None,
                raw={"home_code": g.get("home_team_code"), "away_code": g.get("visiting_team_code")},
            )
        )
    return games


def apply_summary(game: GameResult, data: dict) -> GameResult:
    gs = (data.get("GC") or {}).get("Gamesummary") or {}
    home_id = str((gs.get("home") or {}).get("id"))
    teams = {home_id: game.home, str((gs.get("visitor") or {}).get("id")): game.away}
    parts: list[Participant] = []
    goals = []
    h = a = 0
    ordered = sorted(
        gs.get("goals") or [], key=lambda x: (int(x.get("period_id") or 0), int(x.get("s") or 0))
    )
    for g in ordered:
        is_home = str(g.get("home")) == "1"
        h, a = (h + 1, a) if is_home else (h, a + 1)
        team = teams.get(str(g.get("team_id")))
        tags = [
            t
            for t, k in (("PP", "power_play"), ("SH", "short_handed"), ("EN", "empty_net"))
            if g.get(k) == "1"
        ]
        period = PERIOD_DE.get(str(g.get("period_id")), "Verlängerung")
        detail = f"{h}:{a}, {g.get('time')} {period}" + (f" ({', '.join(tags)})" if tags else "")
        scorer = _name(g.get("goal_scorer"))
        assists = [n for n in (_name(g.get("assist1_player")), _name(g.get("assist2_player"))) if n]
        if scorer:
            parts.append(Participant(scorer, "scorer", team, detail))
        parts += [Participant(n, "assist", team, detail) for n in assists]
        goals.append(
            {"score": f"{h}:{a}", "time": g.get("time"), "team": team, "scorer": scorer, "assists": assists}
        )
    for side, team in (("home", game.home), ("visitor", game.away)):
        agg: dict[str, dict] = {}
        for gk in (gs.get("goalies") or {}).get(side) or []:
            n = _name(gk)
            x = agg.setdefault(n, {"saves": 0, "sa": 0, "secs": 0})
            x["saves"] += int(gk.get("saves") or 0)
            x["sa"] += int(gk.get("shots_against") or 0)
            x["secs"] += int(gk.get("secs") or 0)
        for n, x in agg.items():
            if n and x["secs"] > 0:
                parts.append(Participant(n, "goalie", team, f"{x['saves']} Paraden bei {x['sa']} Schüssen"))
        lineup = gs.get(f"{side}_team_lineup") or {}
        for p in lineup.get("players") or []:
            if n := _name(p):
                parts.append(Participant(n, "lineup", team))
        for p in lineup.get("goalies") or []:
            if (n := _name(p)) and int(p.get("seconds") or 0) > 0:
                parts.append(Participant(n, "lineup", team))
    gbp = gs.get("goalsByPeriod") or {}
    keys = sorted(set(gbp.get("home") or {}) | set(gbp.get("visitor") or {}), key=lambda k: int(k))
    game.period_scores = [f"{gbp['home'].get(k, 0)}:{gbp['visitor'].get(k, 0)}" for k in keys]
    meta = gs.get("meta") or {}
    if meta.get("home_goal_count") is not None:
        sfx = ot_suffix(len(keys), shootout=str(meta.get("shootout")) == "1")
        game.score = f"{meta['home_goal_count']}:{meta['visiting_goal_count']}{sfx}"
    if str(meta.get("final")) == "1":
        game.status = "final"
    game.participants = parts
    shots = gs.get("totalShots") or {}
    game.raw = {
        **game.raw,
        "goals": goals,
        "shots": f"{shots.get('home')}:{shots.get('visitor')}" if shots else None,
        "stars": [_name(x) for x in gs.get("mvps") or []],
        "end_local": meta.get("end_time"),
        "timezone": meta.get("timezone"),
        "detail": True,
    }
    return game


class PwhlCollector(KnownFinal):
    name = "pwhl"
    source_label = "PWHL"

    def __init__(
        self,
        key: str = "694cfeed58c932ee",
        teams: list[str] | None = None,
        delay_s: float = 1.0,
        max_details: int = 20,
        horizon_days: int = 60,
        http: PoliteHttp | None = None,
    ) -> None:
        self.key = key
        self.default_teams = teams
        self.max_details = max_details
        self.horizon_days = horizon_days
        self.http = http or PoliteHttp(delay_s)
        self.known_final = set()

    def _params(self, **kw) -> dict:
        return {**kw, "key": self.key, "client_code": "pwhl", "fmt": "json"}

    def collect(self, since: datetime | None = None, teams: list[str] | None = None) -> list[GameResult]:
        teams = teams or self.default_teams
        now = datetime.now(UTC)
        since_d = (since or now - timedelta(days=14)).date()
        until = now + timedelta(days=self.horizon_days)
        try:
            seasons = self.http.json(FEED, self._params(feed="modulekit", view="seasons"))
            active = active_seasons(
                (seasons.get("SiteKit") or {}).get("Seasons") or [], since_d, until.date()
            )
        except Exception as exc:
            log.warning("pwhl: seasons failed: %s", exc)
            return []
        out, details = [], 0
        for season in active:
            try:
                data = self.http.json(
                    FEED, self._params(feed="modulekit", view="schedule", season_id=season["season_id"])
                )
                games = parse_schedule(data, f"PWHL {season.get('season_name', '')}".strip())
            except Exception as exc:
                log.warning("pwhl: schedule %s failed: %s", season.get("season_id"), exc)
                continue
            for game in games:
                if not wants(teams, game.home, game.away, game.raw["home_code"], game.raw["away_code"]):
                    continue
                if not in_window(game, since or now - timedelta(days=14)):
                    continue
                if game.starts_at and game.starts_at > until:
                    continue
                need = game.status == "live" or (
                    game.status == "final" and not self.is_known_final(game.external_id)
                )
                if need and details < self.max_details:
                    details += 1
                    try:
                        summary = self.http.json(
                            FEED, self._params(feed="gc", tab="gamesummary", game_id=game.external_id)
                        )
                        apply_summary(game, summary)
                    except Exception as exc:
                        log.warning("pwhl: summary %s failed: %s", game.external_id, exc)
                out.append(game)
        return out
