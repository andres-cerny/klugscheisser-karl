"""Find which known teams a text is about: whole words, explicit aliases, women's context for national cues.

Aliases come from seeds/team_aliases.yaml (plus multi-word club names from the DB). Rules:
- whole-word matching on normalised text ("Bernhard" and "Anzug" never match "Bern" / "Zug");
- ambiguous aliases (city names) count only with an ice hockey context (domain/sport.py: specific hockey cues,
  or the URL section when the text names no sport) or a linked player name;
- national-team aliases need a women's context in the text, unless the alias itself is women-specific
  ("Damkronorna", "Frauen-Nati", "Smile Japan"), so the men's "Nati" does not link the women's team;
- national-team and city aliases need a hockey cue in the text, a canonical (page-checked) /eishockey/ URL or a
  named player; a feed URL alone is not enough (outlets misfile women's football under /eishockey/);
- multi-sport tickers ("… +++ …", "Fussball, Eishockey, Boxen …") link no team at all;
- a text about another sport (Fussball, FIFA, ÖFB, Elfmeter … outweigh the hockey cues) links no national team
  and no ambiguous alias: "Schweizerinnen", "Nati" and "Bern" are shared by every sport. Explicit club names
  ("ZSC Lions Frauen") still link.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize

log = logging.getLogger(__name__)

# women's words per language, on normalised text (accents stripped, lower case)
_WOMEN = re.compile(
    r"\b("
    r"\w*frauen\w*|damen\w*|women\w*|woman|ladies|female|girls"  # de / en
    r"|feminin\w*|femmes?"  # fr
    r"|femminil\w*|donne|ragazze"  # it
    r"|dam|damer\w*|dam(lag|landslag|kronor|hockey|allsvensk)\w*|kvinn\w*"  # sv
    r"|nais(et|ten|leijon\w*)"  # fi
    r"|zen|zeny|zensk\w*|hokejistk\w*|reprezentantk\w*"  # cs
    r"|kvinde\w*"  # da
    r"|pwhl|sdhl"  # women's leagues
    r")\b"
)
# German feminine plurals (Spielerinnen, Schweizerinnen) — but not gewinnen / beginnen / drinnen
_INNEN_NOT = ("winnen", "beginnen", "sinnen", "spinnen", "drinnen", "entrinnen", "zerrinnen", "binnen")


def has_women_context(norm: str) -> bool:
    if _WOMEN.search(norm):
        return True
    return any(t.endswith("innen") and len(t) > 7 and not t.endswith(_INNEN_NOT) for t in norm.split())


@dataclass(frozen=True)
class TeamLink:
    team_id: int
    how: str  # alias | national | ambiguous
    confidence: float


@dataclass(frozen=True)
class _Entry:
    team_id: int
    ambiguous: bool
    needs_women: bool
    national: bool = False


class TeamMatcher:
    def __init__(self, session: Session, athlete_matcher=None) -> None:
        from klugscheisser.seed.loader import load_team_aliases

        self.session = session
        self._athlete_matcher = athlete_matcher
        from klugscheisser.sports import current_spec

        teams = {t.key: t for t in session.scalars(select(m.Team))}
        self.men = men = current_spec().men
        self.by_alias: dict[str, list[_Entry]] = {}
        for key, aliases in load_team_aliases(session).items():
            team = teams[key]
            for a in aliases:
                norm = normalize(a.alias)
                if not norm:
                    continue
                women_alias = a.women_specific or has_women_context(norm)
                # 2026-11-24: a men's project (floorball) links its national teams without a women's context
                needs_women = team.kind == "national" and not women_alias and not men
                self._add(norm, _Entry(team.id, a.ambiguous, needs_women, team.kind == "national"))
        # clubs without YAML aliases: their DB name (one word = ambiguous); never national team names
        for team in teams.values():
            if team.kind == "national":
                continue
            norm = normalize(team.name)
            if norm and norm not in self.by_alias:
                self._add(norm, _Entry(team.id, len(norm.split()) < 2, False))
        names = sorted(self.by_alias, key=len, reverse=True)
        self._rx = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None

    def _add(self, norm: str, entry: _Entry) -> None:
        entries = self.by_alias.setdefault(norm, [])
        if all(e.team_id != entry.team_id for e in entries):
            entries.append(entry)

    def _player_named(self, text: str) -> bool:
        if self._athlete_matcher is None:
            from klugscheisser.domain.matcher import AthleteMatcher

            self._athlete_matcher = AthleteMatcher(self.session)
        return bool(self._athlete_matcher.from_text(text))

    def from_text(self, text: str, url: str | None = None, url_checked: bool = False) -> list[TeamLink]:
        """``url_checked``: ``url`` is the outlet's canonical URL (read from the article page), so its section
        (``/eishockey/``) can be trusted. A feed URL alone is not enough for a national-team or city link:
        SRF and Blick file women's football under ``/sport/eishockey/``."""
        from klugscheisser.domain.sport import is_ticker, sport_verdict

        blob = f"{url or ''}\n{text or ''}"
        if not self._rx or not blob.strip() or is_ticker(text or "", url):
            return []  # multi-sport tickers: no story in them belongs to one team
        norm = normalize(blob)
        women: bool | None = None
        verdict = None
        hockey: bool | None = None
        found: dict[int, TeamLink] = {}
        for match in self._rx.finditer(norm):
            for e in self.by_alias[match.group(1)]:
                if e.needs_women:
                    women = has_women_context(norm) if women is None else women
                    if not women:
                        continue
                elif self.men and e.national:
                    women = has_women_context(norm) if women is None else women
                    if women:
                        continue  # the women's team of the men's project's sport
                if e.national or e.ambiguous:
                    verdict = verdict or sport_verdict(text or "", url)
                    if verdict.is_other or verdict.sport == "mixed":
                        continue  # women's football "Nati" / "Schweizerinnen" / "Bern"
                    if hockey is None:
                        hockey = (
                            verdict.is_hockey and (verdict.source == "body" or url_checked)
                        ) or self._player_named(text or "")
                    if not hockey:
                        continue
                link = _link(e)
                if e.team_id not in found or found[e.team_id].confidence < link.confidence:
                    found[e.team_id] = link
        return list(found.values())


def _link(e: _Entry) -> TeamLink:
    if e.ambiguous:
        return TeamLink(e.team_id, "ambiguous", 0.7)
    if e.needs_women:
        return TeamLink(e.team_id, "national", 0.8)
    return TeamLink(e.team_id, "alias", 0.9)


def relink_teams(session: Session, since: datetime | None = None) -> int:
    """Run the TeamMatcher over stored non-Instagram items and add missing item_teams rows. Returns rows added."""
    matcher = TeamMatcher(session)
    q = select(m.Item).where(m.Item.platform != "instagram").order_by(m.Item.id)
    if since is not None:
        q = q.where(func.coalesce(m.Item.published_at, m.Item.scraped_at) >= since)
    added = 0
    for item in session.scalars(q):
        text = "\n".join(filter(None, [item.title, item.text]))
        if not text:
            continue
        existing = {t.team_id for t in item.teams}
        checked = (item.raw or {}).get("canonical_url")
        for link in matcher.from_text(text, url=checked or item.url, url_checked=bool(checked)):
            if link.team_id not in existing:
                item.teams.append(m.ItemTeam(team_id=link.team_id))
                existing.add(link.team_id)
                added += 1
    session.flush()
    return added
