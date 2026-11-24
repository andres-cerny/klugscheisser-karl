"""Is this text about THIS person (not a namesake)? One Jev question, shared by web pages, news articles,
search hits and podcast/video links (v8; before only the answer engine asked it).

Phrasing calibrated 2026-10-03: right page 0.98, namesakes 0.03 (see tests/test_v3_answer.py).
"""

from __future__ import annotations

import logging

from klugscheisser.db import models as m

log = logging.getLogger(__name__)
ABOUT_MIN = 0.8


def _spec(a: m.Athlete):
    from klugscheisser.sports import get_sport

    try:
        return get_sport(a.sport.key) if a.sport else None
    except KeyError:
        return None


def _men(a: m.Athlete) -> bool:
    """A men's sport (floorball project, 2026-11-24): German words for a man."""
    spec = _spec(a)
    return bool(spec and spec.men)


def _noun(a: m.Athlete) -> str:
    spec = _spec(a)
    if a.role == "coach":
        if spec and spec.men:  # "Unihockeyspieler" → "Unihockeytrainer"
            return spec.person_de.removesuffix("spieler") + "trainer"
        return "Eishockeytrainerin" if (a.sport and "women" in a.sport.key) else "Trainerin"
    try:
        return spec.person_de
    except Exception:
        return "Sportlerin"


def about_question(a: m.Athlete, mode: str = "about") -> dict:
    """mode "about": the page is about her (profiles, web pages). mode "meant": the name in the text refers to
    her and not to a namesake (news, match reports, lists: she need not be the main subject)."""
    noun = _noun(a)
    men = _men(a)
    nation = {"SUI": "Schweizer"}.get(a.nationality or "", "")
    year = f" (geboren {a.birth_date.year})" if a.birth_date else ""
    if mode == "meant":
        clubs = sorted({x.team.name for x in a.affiliations if x.team})[:3]
        team = f", Teams: {', '.join(clubs)}" if clubs else ""
        return {
            "about": {
                "type": "noul",
                "instructions": f"Ist mit «{a.full_name}» (oder {'seinem' if men else 'ihrem'} Nachnamen) in diesem Text "
                f"{'der' if men else 'die'} {nation} {noun} "
                f"{a.full_name}{year}{team} gemeint?".replace("  ", " "),
                "criteria": {
                    "true": "der Text meint diese Person (auch wenn sie nur am Rand vorkommt)",
                    "false": "eine andere Person mit gleichem (Nach-)Namen, z. B. aus einer anderen Sportart",
                },
            }
        }
    return {
        "about": {
            "type": "noul",
            "instructions": f"Diese Seite handelt {'vom' if men else 'von der'} {nation} {noun} {a.full_name}{year}.".replace(
                "  ", " "
            ),
            "criteria": {
                "true": f"die Seite beschreibt {'diesen' if men else 'diese'} {noun}",
                "false": "die Seite beschreibt eine andere Person mit gleichem Namen",
            },
        }
    }


def about_probability(
    settings, a: m.Athlete, title: str, text: str, jev=None, mode: str = "about"
) -> float | None:
    """Jev probability that title+text is about this person; None when Jev is off or failed."""
    if jev is None:
        if not settings.features["jev"]:
            return None
        from klugscheisser.classify.jev import JevClient

        jev = JevClient(settings)
    from klugscheisser.classify.jev import noul_probability

    try:
        return noul_probability(jev.ask(f"{title}\n{text[:4000]}", about_question(a, mode))["about"])
    except Exception as exc:
        log.warning("identity check failed: %s", exc)
        return None


def mentions_name(a: m.Athlete, title: str, text: str) -> bool:
    from klugscheisser.domain.names import name_variants, normalize

    body = normalize(f"{title} {text}")
    names = name_variants(a.full_name) | {x.alias_normalized for x in a.aliases}
    return any(v and v in body for v in names)


def is_about(settings, a: m.Athlete, title: str, text: str, jev=None, need_name: bool = True) -> bool:
    """Name must appear (unless need_name=False, e.g. surname-only hits), then Jev ≥ ABOUT_MIN. Without Jev:
    at least the sport must appear."""
    if need_name and not mentions_name(a, title, text):
        return False
    p = about_probability(settings, a, title, text, jev)
    if p is None:
        if settings.features["jev"]:
            return False  # Jev on but failed: never guess
        from klugscheisser.domain.sport import sport_verdict

        return sport_verdict(f"{title} {text}").looks_hockey()
    return p >= ABOUT_MIN
