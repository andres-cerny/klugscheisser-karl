"""Find which known athletes a text or post is about (by handle or by name)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.domain.names import normalize


@dataclass
class Link:
    athlete_id: int
    kind: str  # author | tag | mention | name
    confidence: float


class AthleteMatcher:
    def __init__(self, session: Session, jev_client=None) -> None:
        self.session = session
        self.jev_client = jev_client
        self.by_handle: dict[str, int] = {}
        for h in session.scalars(select(m.Handle).where(m.Handle.status.in_(["verified", "wahrscheinlich"]))):
            self.by_handle[h.handle.lower()] = h.athlete_id
        # only multi-word aliases: a lone surname ("Müller") is too ambiguous in news text
        self.by_name: dict[str, set[int]] = {}
        for alias in session.scalars(select(m.AthleteAlias)):
            if len(alias.alias_normalized.split()) >= 2:
                self.by_name.setdefault(alias.alias_normalized, set()).add(alias.athlete_id)
        names = sorted(self.by_name, key=len, reverse=True)
        self._rx = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b") if names else None

    def from_text(self, text: str) -> list[Link]:
        if not self._rx or not text:
            return []
        found: dict[int, Link] = {}
        for match in self._rx.finditer(normalize(text)):
            ids = self.by_name[match.group(1)]
            if len(ids) == 1:
                aid = next(iter(ids))
                found.setdefault(aid, Link(aid, "name", 0.9))
            elif (aid := self.disambiguate(sorted(ids), text)) is not None:
                found.setdefault(aid, Link(aid, "name", 0.75))
        return list(found.values())

    def from_text_fuzzy(self, text: str, min_ratio: float = 88.0) -> list[tuple[Link, str]]:
        """v12: names with a typo ("Lara Staldr") — two neighbouring words close to a stored full name (one
        person only, same first letters). Only used when the exact match found nobody. Returns (link, name)."""
        from itertools import pairwise

        from rapidfuzz import fuzz

        words = normalize(text or "").split()
        found: dict[int, tuple[Link, str]] = {}
        for first, second in pairwise(words):
            pair = f"{first} {second}"
            if len(pair) < 8:
                continue
            best: list[tuple[float, str]] = []
            for name in self.by_name:
                if len(name.split()) != 2 or name[:2] != pair[:2]:
                    continue
                score = fuzz.ratio(pair, name)
                if score >= min_ratio:
                    best.append((score, name))
            if not best:
                continue
            best.sort(reverse=True)
            top = [n for sc, n in best if sc == best[0][0]]
            ids = {i for n in top for i in self.by_name[n]}
            if len(ids) == 1:  # one person only: never guess between namesakes
                aid = next(iter(ids))
                found.setdefault(aid, (Link(aid, "name_fuzzy", 0.6), top[0]))
        return list(found.values())

    def disambiguate(self, ids: list[int], text: str) -> int | None:
        """Several persons share this name: Jev picks one from context (sport, role, team), or none."""
        if self.jev_client is None:
            return None  # without Jev: skip namesakes rather than guess
        from klugscheisser.classify.jev import choice_answer

        options = {}
        for aid in ids:
            a = self.session.get(m.Athlete, aid)
            teams = ", ".join(sorted({x.team.name for x in a.affiliations if x.team}))[:120]
            sport = a.sport.name if getattr(a, "sport", None) else ""
            options[f"p{aid}"] = f"{a.full_name}: {a.role}, {sport}, {a.nationality or ''}, {teams}".strip(
                ", "
            )
        options["none"] = "keine dieser Personen / unklar"
        question = {
            "wer": {
                "type": "choice",
                "instructions": "Um welche Person geht es in diesem Text?",
                "criteria": options,
            }
        }
        try:
            answer = self.jev_client.ask(text[:3000], question)["wer"]
        except Exception:
            return None
        pick, confidence = choice_answer(answer, list(options))
        return int(pick[1:]) if pick.startswith("p") and confidence >= 0.7 else None

    # --- v8: surnames of tier 1/2 persons (news gate) -------------------------------------------------------

    COMMON_SURNAMES = frozenset(
        [
            "muller",
            "mueller",
            "meier",
            "meyer",
            "schmid",
            "schmidt",
            "keller",
            "weber",
            "huber",
            "schneider",
            "fischer",
            "wagner",
            "frei",
            "johansson",
            "andersson",
            "karlsson",
            "nilsson",
            "eriksson",
            "larsson",
            "olsson",
            "persson",
            "svensson",
            "gustafsson",
            "smith",
            "johnson",
            "williams",
            "brown",
            "jones",
            "miller",
            "davis",
            "wilson",
            "anderson",
            "taylor",
            "thomas",
            "moore",
            "martin",
            "novak",
            "novakova",
            "svoboda",
            "svobodova",
            "dvorak",
            "dvorakova",
            "virtanen",
            "korhonen",
            "nieminen",
            "makinen",
            "hamalainen",
            "jensen",
            "nielsen",
            "hansen",
            "pedersen",
            "andersen",
            "sato",
            "suzuki",
            "takahashi",
            "tanaka",
            "watanabe",
            "ito",
            "yamamoto",
        ]
    )

    def surname_index(self) -> dict[str, int]:
        """Surname -> person, for tier 1/2 persons whose surname is unique in our database, at least 5 letters
        and not a common name. Built on first use."""
        if getattr(self, "_surnames", None) is None:
            from collections import Counter

            rows = self.session.execute(
                select(m.Athlete.id, m.Athlete.full_name, m.Athlete.tier, m.Athlete.tier_manual)
            ).all()
            last = {
                aid: normalize(name).split()[-1] for aid, name, _, _ in rows if name and " " in name.strip()
            }
            counts = Counter(last.values())
            self._surnames = {
                sur: aid
                for aid, name, tier, manual in rows
                if (manual or tier or 3) <= 2
                and (sur := last.get(aid))
                and counts[sur] == 1
                and len(sur) >= 5
                and sur not in self.COMMON_SURNAMES
            }
        return self._surnames

    def from_surnames(self, text: str, skip: set[int] | None = None) -> list[Link]:
        """Tier 1/2 persons named only by surname ("Stalder trifft doppelt"). Low confidence: the caller must
        confirm each with the identity check before linking."""
        index = self.surname_index()
        if not index or not text:
            return []
        words = set(normalize(text).split())
        return [
            Link(aid, "surname", 0.6)
            for sur, aid in index.items()
            if sur in words and aid not in (skip or set())
        ]

    def from_post(
        self, owner: str, tagged: list[str], mentions: list[str], coauthors: list[str], caption: str
    ) -> list[Link]:
        links: dict[int, Link] = {}
        if (aid := self.by_handle.get(owner.lower())) is not None:
            links[aid] = Link(aid, "author", 1.0)
        for kind, handles in (("tag", tagged), ("tag", coauthors), ("mention", mentions)):
            for h in handles:
                if (aid := self.by_handle.get(h.lower())) is not None and aid not in links:
                    links[aid] = Link(aid, kind, 0.95)
        for link in self.from_text(caption):
            links.setdefault(link.athlete_id, link)
        return list(links.values())
