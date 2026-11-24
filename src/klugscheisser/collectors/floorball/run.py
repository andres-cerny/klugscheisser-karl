"""One entry point over the floorball adapters, for `klugscheisser -p unihockey seed floorball`.

    enrich(session, athlete_ids, sources=("swissunihockey", "iff"), dry_run=True, max_requests=300) -> dict

Sources:
- "swissunihockey": profile fields (federation_id "su:<id>", position if empty), season stats (source
  "swissunihockey") and career stints (source "federation") of every athlete found at Swiss Unihockey. Swiss
  athletes first, then the others (foreign players in the L-UPL). Costs ~3–16 list requests + 2 per player.
- "swissunihockey_nati": national team career totals of the current Swiss pool (not stored, returned as text).
- "iff": WFC games from Wikipedia/IFF (counts only; use `iff.fetch_wfc_games` / `iff.seed_games` for the data).
- "ssl": Swedish SSL regular-season stats of all seasons since 2016/17 (ssl.se API, ~20 requests), matched by
  name (+ birth year): season stats (source "ssl"), career stints (source "league"), empty position/birth date.
- "fliiga": Finnish F-Liiga stats since 2020/21 (fliiga.com; list ~18 requests + 1 per candidate player).
- "stints": after the league sources, end open club stations that a later federation/league/EP station
  contradicts (`leagues.close_outdated_open_stints`); runs automatically when "ssl" or "fliiga" is asked for.

Never commits; flushes when not a dry run. Unknown source names are reported, not raised.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.collectors.floorball import fliiga, iff, leagues, ssl_se, swissunihockey
from klugscheisser.collectors.floorball.common import CappedHttp, RequestCapReached
from klugscheisser.db import models as m

SOURCES = ("swissunihockey", "swissunihockey_nati", "iff", "ssl", "fliiga", "stints")


def enrich(
    session: Session,
    athlete_ids: list[int],
    sources: tuple[str, ...] = ("swissunihockey", "iff"),
    dry_run: bool = True,
    max_requests: int = 300,
    http: CappedHttp | None = None,
    wiki_http: CappedHttp | None = None,
) -> dict:
    out: dict = {}
    unknown = [s for s in sources if s not in SOURCES]
    if unknown:
        out["unbekannte_quellen"] = unknown
    athletes = (
        list(session.scalars(select(m.Athlete).where(m.Athlete.id.in_(athlete_ids)))) if athlete_ids else []
    )
    athletes.sort(key=lambda a: (a.nationality != "SUI", a.full_name))
    injected = http
    http = http or CappedHttp(delay_s=1.5, max_requests=max_requests)
    if "swissunihockey" in sources:
        res = swissunihockey.import_for_athletes(session, athletes, dry_run=dry_run, http=http)
        res.pop("found", None)
        swiss_missing = {a.full_name for a in athletes if a.nationality == "SUI"}
        res["not_found_sui"] = [n for n in res.pop("not_found", []) if n.split(" (")[0] in swiss_missing]
        out.update({f"swissunihockey_{k}": v for k, v in res.items()})
    if "swissunihockey_nati" in sources:
        names = [a.full_name for a in athletes if a.nationality == "SUI"]
        try:
            totals = swissunihockey.national_totals_for(http, names)
        except RequestCapReached as exc:
            totals, out["swissunihockey_nati_stopped"] = {}, str(exc)
        except Exception as exc:
            totals, out["swissunihockey_nati_error"] = {}, str(exc)
        out["swissunihockey_nati"] = {
            name: "; ".join(
                f"{t.selection}: {t.games} Sp., {t.goals} T, {t.assists} A, {t.points} P ({t.url})"
                for t in ts
            )
            for name, ts in totals.items()
        }
    if "iff" in sources:
        wiki_http = wiki_http or CappedHttp(delay_s=1.0, max_requests=10)
        for year in (2026, 2024):
            games = iff.fetch_wfc_games(year, wiki_http)
            out[f"iff_wfc_{year}_spiele"] = len(games)
        out.update(iff.import_for_athletes(session, athletes, dry_run=dry_run))
    clubs: dict[str, set[str]] = {}
    if {"ssl", "fliiga"} & set(sources):
        try:
            from klugscheisser import projects

            clubs = leagues.roster_clubs(projects.current().seeds_dir)
        except Exception:  # no project / seeds: match by name, birth year and nationality only
            clubs = {}
    for name, mod in (("ssl", ssl_se), ("fliiga", fliiga)):
        if name not in sources:
            continue
        lhttp = injected or CappedHttp(delay_s=1.5, max_requests=max_requests)  # own budget per league site
        try:
            if name == "ssl":
                players, report = ssl_se.fetch_players(lhttp)
            else:
                players, report = fliiga.fetch_players(lhttp, [a.full_name for a in athletes])
            report["requests"] = lhttp.requests
            res = leagues.import_players(
                session,
                [a for a in athletes if (a.role or "athlete") == "athlete"],
                players,
                source=mod.SOURCE,
                league=mod.LEAGUE,
                country=mod.COUNTRY,
                dry_run=dry_run,
                clubs=clubs,
            )
        except RequestCapReached as exc:
            report, res = {"gestoppt": str(exc)}, {}
        except Exception as exc:  # network / format: the other sources still run
            report, res = {"fehler": str(exc)[:200]}, {}
        out[f"{name}_quelle"] = report
        out.update({f"{name}_{k}": v for k, v in res.items()})
    if {"ssl", "fliiga", "stints"} & set(sources):
        out["stints_beendet"] = leagues.close_outdated_open_stints(session, athlete_ids, dry_run=dry_run)
    out["floorball_requests"] = http.requests
    return out
