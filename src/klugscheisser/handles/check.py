"""v8: check social handles of tier 1/2 persons before we scrape them.

- Instagram "wahrscheinlich": Jev reads the stored profile (name, bio, followers) and the captions we already
  hold and answers "is this the person's own account?". Yes (≥ YES) adds evidence that, with name + bio,
  makes the handle verified; a clear no (≤ NO) sets it back to candidate (not scraped, not shown). In between
  nothing changes.
- Facebook: one Brave query `site:facebook.com "Name"` per person; a result that looks like a page of its own
  (not a post, photo or group) and passes the same Jev check becomes a facebook handle ("wahrscheinlich"; or
  verified when Jev is very sure). Only those pages are scraped later (text only).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.handles.service import add_evidence, add_handle_candidate, reason_de

log = logging.getLogger(__name__)
YES = 0.85
NO = 0.15
FB_SURE = 0.95

# facebook.com/<page> or /people/<name>/<id>, not posts, photos, groups, events, hashtags …
_FB_NOT_PAGE = re.compile(
    r"/(posts|photos?|videos?|groups|events|hashtag|story\.php|watch|reel|permalink)\b", re.I
)


@dataclass
class CheckReport:
    checked: int = 0
    verified: int = 0
    demoted: int = 0
    unchanged: int = 0
    facebook_found: int = 0
    facebook_queries: int = 0
    notes: list[str] = field(default_factory=list)


def person_line(a: m.Athlete) -> str:
    from klugscheisser.sports import get_sport

    try:
        noun = get_sport(a.sport.key).person_de
    except Exception:
        noun = "Sportlerin"
    if a.role == "coach":
        try:
            spec = get_sport(a.sport.key)
        except Exception:
            spec = None
        noun = (
            f"Trainer:in im {spec.name_de}"
            if spec is not None and spec.family != "hockey"
            else "Trainer:in im Fraueneishockey"
        )
    clubs = sorted({x.team.name for x in a.affiliations if x.team})[:4]
    bits = [
        f"{noun} {a.full_name}",
        a.nationality or "",
        f"geboren {a.birth_date.year}" if a.birth_date else "",
    ]
    if clubs:
        bits.append("Teams: " + ", ".join(clubs))
    return ", ".join(b for b in bits if b)


def owner_probability(jev, a: m.Athlete, platform: str, account: str, state: str) -> float:
    """Jev: is this account the person's own (not a fan, team or namesake account)?"""
    from klugscheisser.classify.jev import noul_probability

    question = {
        "own": {
            "type": "noul",
            "instructions": f"Gehört dieser {platform}-Account ({account}) der Person: {person_line(a)}?",
            "criteria": {
                "true": "eigener Account genau dieser Person",
                "false": "andere Person mit gleichem Namen, Fan-Account, Team-/Vereinsseite oder unklar",
            },
        }
    }
    return noul_probability(jev.ask(state[:4000], question)["own"])


def instagram_state(s: Session, h: m.Handle) -> str:
    p = h.profile or {}
    captions = list(
        s.scalars(
            select(m.Item.text)
            .join(m.ItemAthlete, m.ItemAthlete.item_id == m.Item.id)
            .where(
                m.ItemAthlete.athlete_id == h.athlete_id,
                m.Item.platform == "instagram",
                m.Item.source_type == "athlete_post",
                m.Item.text.is_not(None),
            )
            .order_by(m.Item.published_at.desc().nulls_last())
            .limit(5)
        )
    )
    lines = [
        f"Instagram @{h.handle}",
        f"Profilname: {p.get('full_name') or '–'}",
        f"Bio: {p.get('biography') or '–'}",
        f"Kategorie: {p.get('category') or '–'} · Follower: {p.get('followers') or '–'} · "
        f"Posts: {p.get('posts_count') or '–'} · verifiziert: {'ja' if p.get('verified_badge') else 'nein'}",
    ]
    lines += [f"Post: {c[:300]}" for c in captions]
    return "\n".join(lines)


def check_instagram(s: Session, jev, athlete_ids: list[int], report: CheckReport) -> None:
    handles = s.scalars(
        select(m.Handle).where(
            m.Handle.athlete_id.in_(athlete_ids),
            m.Handle.platform == "instagram",
            m.Handle.status == "wahrscheinlich",
        )
    ).all()
    for h in handles:
        if not h.profile:
            report.notes.append(f"@{h.handle}: noch kein Profil geladen, später")
            continue
        report.checked += 1
        try:
            p = owner_probability(jev, h.athlete, "Instagram", f"@{h.handle}", instagram_state(s, h))
        except Exception as exc:
            report.notes.append(f"@{h.handle}: Jev-Fehler {exc}")
            continue
        url = f"https://www.instagram.com/{h.handle}/"
        if p >= YES:
            add_evidence(h, {"kind": "jev_profile", "detail": f"Jev: eigener Account ({p:.2f})", "url": url})
            report.verified += h.status == "verified"
            report.unchanged += h.status != "verified"
        elif p <= NO:
            h.evidence = [
                *h.evidence,
                {"kind": "jev_doubt", "detail": f"Jev: eher nicht ihr Account ({p:.2f})"},
            ]
            h.status = "candidate"
            report.demoted += 1
        else:
            report.unchanged += 1
    s.flush()


def facebook_page(url: str) -> str | None:
    """The page slug of a facebook.com result URL that looks like a page of its own, else None."""
    parts = urlsplit(url)
    if not parts.netloc.lower().endswith("facebook.com") or _FB_NOT_PAGE.search(parts.path):
        return None
    segs = [x for x in parts.path.split("/") if x]
    if not segs:
        return None
    if segs[0] == "people" and len(segs) >= 3:
        return f"people/{segs[1]}/{segs[2]}"
    if segs[0] in ("profile.php", "pages", "public", "login", "share"):
        return None
    return segs[0]


def find_facebook(s: Session, jev, web, athlete_ids: list[int], report: CheckReport) -> None:
    from klugscheisser.domain.names import name_variants, normalize

    for aid in athlete_ids:
        a = s.get(m.Athlete, aid)
        if any(h.platform == "facebook" for h in a.handles):
            continue
        report.facebook_queries += 1
        try:
            results = web.search(
                f'site:facebook.com "{a.full_name}"', limit=10, keep_social=True, country="ALL"
            )
        except Exception as exc:
            report.notes.append(f"{a.full_name}: Brave-Fehler {exc}")
            continue
        names = name_variants(a.full_name)
        for r in results:
            slug = facebook_page(r.url)
            if slug is None or not any(n in normalize(r.title) for n in names):
                continue
            state = f"Facebook-Seite {r.url}\nTitel: {r.title}\nAusschnitt: {r.snippet}"
            try:
                p = owner_probability(jev, a, "Facebook", r.url, state)
            except Exception as exc:
                report.notes.append(f"{a.full_name}: Jev-Fehler {exc}")
                break
            if p < YES:
                continue
            h = add_handle_candidate(
                s,
                a,
                "facebook",
                slug,
                {"kind": "jev_profile", "detail": f"Jev: eigene Seite ({p:.2f})", "url": r.url},
            )
            add_evidence(h, {"kind": "search_hit", "detail": "Brave site:facebook.com", "url": r.url})
            if p >= FB_SURE:
                add_evidence(
                    h, {"kind": "profile_name", "detail": f"Seitentitel «{r.title[:80]}»", "url": r.url}
                )
            report.facebook_found += 1
            report.notes.append(f"{a.full_name}: Facebook {slug} ({reason_de(h)}, {h.status})")
            break
    s.flush()
