"""ui-loop round 3: "Auf einen Blick" on top of the person page — the 10-second view before going on air.

Built only from data the page already holds (Steckbrief facts, season stats, Anlässe, timeline entries, Erfolge);
no extra query, no model. Every value carries its source chip (the same sources the sections below show)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlparse

from klugscheisser.web.html import e, safe_url

STATUS_DE = {"confirmed": "Kader bestätigt", "provisional": "Kader provisorisch"}


def chip(label: str | None, url: str | None, title: str | None = None) -> str:
    """The quiet source chip used everywhere (a link when the source has one)."""
    if not label:
        return ""
    if not url:
        return f"<span class='src'>{e(label)}</span>"
    return (
        f"<a class='src' href='{safe_url(url)}' target='_blank' rel='noopener' title='{e(title or label)}'>"
        f"{e(label)}</a>"
    )


def _tile(label: str, value: str, sub: str = "", sources: str = "", href: str | None = None) -> str:
    value_html = f"<a href='{e(href)}'>{e(value)}</a>" if href else e(value)
    sub_html = f"<span class='gl-s'>{e(sub)}</span>" if sub else ""
    src_html = f"<span class='gl-src'>{sources}</span>" if sources else ""
    return (
        f"<div class='gl-t'><span class='gl-l'>{e(label)}</span><span class='gl-v'>{value_html}</span>"
        f"{sub_html}{src_html}</div>"
    )


def _fact(card: dict[str, Any], label: str) -> dict[str, Any] | None:
    return next((f for f in card.get("fakten") or [] if f["label"] == label), None)


def club_name(p: dict[str, Any], card: dict[str, Any]) -> tuple[str, str] | None:
    """(team, league) the club tile shows, or None."""
    f = _fact(card, "Aktueller Verein")
    if f:
        head = f["value"].split(" · ")[0]
        m = re.match(r"^(.*?)\s*\((.*)\)$", head)
        return (m.group(1), m.group(2)) if m else (head, "")
    clubs = (p.get("athletin") or {}).get("vereine_laut_quellen") or []
    if clubs and clubs[0].get("url"):
        return clubs[0]["team"], ""
    return None


def club_tile(p: dict[str, Any], card: dict[str, Any]) -> str:
    """Current club (Steckbrief fact 'Aktueller Verein', else the newest club from the sources)."""
    f = _fact(card, "Aktueller Verein")
    if f:
        team, league = club_name(p, card) or ("", "")
        srcs = " ".join(chip(x.get("label"), x.get("url"), x.get("quote")) for x in f["sources"] if x)
        return _tile("Aktueller Verein", team, league, srcs)
    clubs = (p.get("athletin") or {}).get("vereine_laut_quellen") or []
    if clubs and clubs[0].get("url"):
        v = clubs[0]
        return _tile("Verein laut Quellen", v["team"], v.get("saison") or "", chip(v.get("quelle"), v["url"]))
    return ""


def _same_team(a: str | None, b: str | None) -> bool:
    norm = lambda x: re.sub(r"\W+", " ", (x or "").casefold()).strip()  # noqa: E731
    return bool(norm(a)) and norm(a) == norm(b)


def season_tile(stats: dict[str, Any] | None, club: str | None = None) -> str:
    """This season's line (newest club season with games, regular season; the row with the most games). When the
    club tile already names the same team (`club`), club and league are not repeated (ui-loop round 5)."""
    if not stats:
        return ""
    from klugscheisser.web.statistik import SOURCE_DE, dec, pct

    pane = next((x for x in stats["panes"] if x["group"] == "club" and x["phase"] == "regular"), None)
    if not pane or not pane["rows"]:
        return ""
    played = [r for r in pane["rows"] if r.get("games")]
    if not played:
        return ""
    # the newest season with games (early in a season the current row is often still empty)
    newest = max(r["season"] for r in played)
    r = max((x for x in played if x["season"] == newest), key=lambda x: x["games"])
    if stats["goalie"]:
        bits = [
            f"{r['games']} Sp",
            f"{pct(r['save_pct'])} Sv" if r.get("save_pct") is not None else None,
            f"{dec(r['gaa'])} GAA" if r.get("gaa") is not None else None,
        ]
    else:
        bits = [f"{r['games']} Sp"] + [
            f"{r[k]} {lbl}"
            for k, lbl in (("goals", "T"), ("assists", "A"), ("points", "P"))
            if r.get(k) is not None
        ]
    q = stats.get("quelle") or {}
    label = "Diese Saison" if newest == stats.get("current") else f"Saison {r['saison']}"
    sub = (
        ""
        if _same_team(r.get("team"), club)
        else " · ".join(x for x in (r.get("team"), r.get("league")) if x)
    )
    if label == "Diese Saison":
        sub = " · ".join(x for x in (r["saison"], sub) if x)
    src_label = SOURCE_DE.get(r.get("source") or "", q.get("label"))
    src = chip(src_label, r.get("quelle_link") or q.get("url"))
    return _tile(label, " · ".join(b for b in bits if b), sub, src)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt and not dt.tzinfo else dt


def event_tile(p: dict[str, Any], now: datetime) -> str:
    """The next tournament she is in (roster status + the roster's source)."""
    upcoming = sorted(
        (x for x in p.get("anlaesse") or [] if (w := _when(x.get("start"))) and w > now),
        key=lambda x: _when(x["start"]),
    )
    if not upcoming:
        return ""
    ev = upcoming[0]
    sub = " · ".join(x for x in (ev.get("datum"), STATUS_DE.get(ev.get("kader_status") or "")) if x)
    label = ev.get("quelle") or ""
    if (
        not label or len(label) > 24
    ):  # long roster notes ("Annahme: …") make a poor chip: the site's name instead
        label = (urlparse(ev.get("url") or "").hostname or "Quelle").removeprefix("www.")
    src = chip(label, ev.get("url"), ev.get("quelle")) if ev.get("url") else ""
    return _tile("Nächster Anlass", ev["name"], sub, src, href=f"/event/{quote(ev['key'])}")


def medal_tile(erfolge: list[dict[str, Any]]) -> str:
    medals = [x for x in erfolge if x.get("metal")]
    if not medals:
        return ""
    x = medals[0]
    more = f"+ {len(medals) - 1} weitere" if len(medals) > 1 else ""
    return _tile("Medaille", x["text"], more, chip(x.get("quelle"), x.get("url")))


def newest_rows(items: list[dict[str, Any]], now: datetime, n: int = 2) -> str:
    """The newest dated things about her (not events: those have their own tile), as linked rows."""
    from klugscheisser.domain.labels import local

    past = sorted(
        (x for x in items if x["when"] and x["when"] <= now and x["k"] != "event"),
        key=lambda x: x["when"],
        reverse=True,
    )[:n]
    rows = []
    for x in past:
        warn = "<span class='k v'>Vorsicht</span>" if x["vorsicht"] else ""
        target = " target='_blank' rel='noopener'" if x["extern"] else ""
        href = safe_url(x["url"]) if x["extern"] else e(x["url"])
        src = f"<span class='src'>{e(x['src'])}</span>" if x["src"] else ""
        rows.append(
            f"<a class='gl-n' href='{href}'{target}><span class='d'>{e(local(x['when']).strftime('%d.%m.'))}</span>"
            f"<span class='t'>{warn}<span class='k'>{e(x['label'])}</span>{e(x['title'])}</span>{src}</a>"
        )
    return "".join(rows)


def glance_html(
    p: dict[str, Any],
    card: dict[str, Any],
    items: list[dict[str, Any]],
    erfolge: list[dict[str, Any]],
    stats: dict[str, Any] | None,
    now: datetime | None = None,
) -> str:
    """Tiles (club, this season, next event, a medal) + the two newest entries. Nothing when there is too little
    to show (the sections below say what is missing)."""
    now = now or datetime.now(UTC)
    tiles = [
        t
        for t in (
            club_tile(p, card),
            season_tile(stats, (club_name(p, card) or ("", ""))[0]),
            event_tile(p, now),
            medal_tile(erfolge),
        )
        if t
    ]
    news = newest_rows(items, now)
    if len(tiles) + (1 if news else 0) < 2:
        return ""
    news_html = f"<div class='gl-news'><span class='gl-l'>Neueste Einträge</span>{news}</div>" if news else ""
    return (
        "<section class='glance' aria-labelledby='gl-h' data-sec='glance'>"
        "<h2 id='gl-h' class='gl-h'>Auf einen Blick</h2>"
        f"<div class='gl-grid'>{''.join(tiles)}</div>{news_html}</section>"
    )
