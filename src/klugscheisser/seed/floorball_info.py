"""The floorball research file (seeds/floorball/event.yaml, nested `event:` block) -> the tournament info format of
seeds/events/<event>.yaml (what tournament.py and seed/loader.load_event_info read).

The research file names teams in English ("Switzerland"), gives Swiss and local kick-off times, the knock-out
bracket with placeholders ("A1", "Winner 36") and plain-text facts with their source URLs. Every line here keeps a
source: the URL in the text, else the section's source. Pure function, no database.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")

NATION_CODES = {
    "switzerland": "SUI",
    "sweden": "SWE",
    "finland": "FIN",
    "czechia": "CZE",
    "czech republic": "CZE",
    "germany": "GER",
    "austria": "AUT",
    "norway": "NOR",
    "latvia": "LAT",
    "slovakia": "SVK",
    "slovenia": "SLO",
    "estonia": "EST",
    "singapore": "SGP",
    "thailand": "THA",
    "denmark": "DEN",
    "japan": "JPN",
    "philippines": "PHI",
    "canada": "CAN",
    "usa": "USA",
    "united states": "USA",
    "poland": "POL",
    "hungary": "HUN",
    "italy": "ITA",
    "spain": "ESP",
    "netherlands": "NED",
    "australia": "AUS",
    "great britain": "GBR",
    "russia": "RUS",
}

STAGE_DE = [  # most specific first: "5th-8th place semi-final" is a placement game, not a semifinal
    (
        r"(\d+)(?:st|nd|rd|th)?\s*-\s*(\d+)(?:st|nd|rd|th)?\s+place(?:ment)?\s+semi",
        r"Platzierungsspiel \1–\2",
    ),
    (r"(\d+)(?:st|nd|rd|th)?\s*-\s*(\d+)(?:st|nd|rd|th)?\s+placement", r"Platzierungsrunde \1–\2"),
    (r"(\d+)(?:st|nd|rd|th) place", r"Spiel um Platz \1"),
    (r"bronze", "Spiel um Bronze"),
    (r"semi-?\s?finals?(?:\s+(\d))?", "Halbfinal"),
    (r"quarter-?\s?finals?", "Viertelfinal"),
    (r"^playoff", "Playoff (Achtelfinal-Runde)"),
    (r"gold medal|\bfinal\b", "Final"),
    (r"^group\s+([a-d])\b", r"Gruppe \1"),
    (r"^group\b", "Gruppe"),
]
_STAGE_IN_TEXT = re.compile(
    r"(\d+(?:st|nd|rd|th)?-\d+(?:st|nd|rd|th)? place semi-final|\d+(?:st|nd|rd|th) place match|Bronze medal match|"
    r"Gold medal (?:match|game)|Semi-?final|Quarter-?final|Final|Group [A-D]\b|Group)",
)


def stages_de(text: str) -> str:
    """English stage names inside a text to German ('1998 Group A: …; 2000 Semi-final: …')."""
    return _STAGE_IN_TEXT.sub(lambda mt: stage_de(mt.group(0)), text or "")


SITE_NAMES = {
    "floorball.sport": "IFF (floorball.sport)",
    "app.floorball.sport": "IFF-App",
    "freeoflimits.fi": "Veranstalter WFC 2026 (freeoflimits.fi)",
    "tulospalvelu.salibandy.fi": "Offizieller Resultatdienst (Torneopal)",
    "salibandy.api.torneopal.com": "Offizieller Resultatdienst (Torneopal)",
    "unihockey.ch": "Swiss Unihockey (unihockey.ch)",
    "en.wikipedia.org": "Wikipedia",
}
URL = re.compile(r"https?://[^\s)\],;'\"]+")


def code(name: Any) -> str | None:
    """'Switzerland' / 'SUI' -> 'SUI'; placeholders ('A1', 'Winner 36') -> None."""
    s = str(name or "").strip()
    if re.fullmatch(r"[A-Z]{3}", s):
        return s
    return NATION_CODES.get(s.lower())


def stage_de(stage: str) -> str:
    s = (stage or "").strip()
    for rx, de in STAGE_DE:
        if mt := re.search(rx, s, re.I):
            return mt.expand(de) if "\\" in de else de
    return s


def _placeholder_de(x: str) -> str:
    return re.sub(r"^Winner\s+", "Sieger Spiel ", re.sub(r"^Loser\s+", "Verlierer Spiel ", str(x)))


class _Sources:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self.by_url: dict[str, str] = {}

    def add(
        self, url: str | None, name: str | None = None, key: str | None = None, when: str | None = None
    ) -> str:
        if not url:
            return ""
        url = url.rstrip(".")
        if url in self.by_url:
            return self.by_url[url]
        host = urlsplit(url).netloc.removeprefix("www.")
        key = key or f"s{len(self.items) + 1}"
        kind = (
            "web"
            if "wikipedia" in host
            else ("media" if host.endswith("unihockey.ch") and "/news/" in url else "official")
        )
        self.items[key] = {"name": name or SITE_NAMES.get(host, host), "url": url, "kind": kind}
        if when:
            self.items[key]["date"] = when
        self.by_url[url] = key
        return key

    def from_text(self, text: str, default: str) -> str:
        m = URL.search(text or "")
        return self.add(m.group(0)) if m else default


def _clean(text: str) -> str:
    """Drop the URL from a fact text (it is the line's link); keep the rest."""
    t = URL.sub("", str(text))
    t = re.sub(r"\(\s*\)|\(\s*,\s*", "(", t)
    return re.sub(r"\s{2,}", " ", t).strip(" ,;")


def normalize(raw: dict, event_key: str, checked: str | None = None) -> dict:
    ev = raw.get("event") or {}
    src = _Sources()
    iff = src.add(ev.get("iff_event_page"), "IFF, Men's WFC 2026", "iff")
    official = src.add(ev.get("official_site"), "Veranstalter WFC 2026 (freeoflimits.fi)", "official")
    results = src.add(ev.get("official_results_service"), "Offizieller Resultatdienst (Torneopal)", "results")
    default = iff or official or results
    country = code(ev.get("country"))
    from klugscheisser.tournament import NATION_DE

    host = f"{NATION_DE.get(country, ev.get('country'))} ({ev.get('city')})"
    dates = ev.get("dates") or {}
    name = ev.get("german") or ev.get("name") or event_key
    year = str(dates.get("start", ""))[:4]
    info: dict[str, Any] = {
        "event": event_key,
        "name": f"Unihockey-WM {year} der Männer ({ev.get('city')})" if year else name,
        "aliases": [event_key, "unihockey-wm", "wm", "weltmeisterschaft", "wfc", ev.get("short") or ""],
        "starts": str(dates.get("start")),
        "ends": str(dates.get("end")),
        "host": host,
        "city": ev.get("city"),
        "checked": checked,
        "default_source": default,
        "schedule_source": results or default,
        "games_source": "Torneopal",
        "time_note": "Schweizer Zeit = Ortszeit Finnland minus 1 Stunde",
        "ko_start_note": "ab den Playoffs am 09.12. bzw. Viertelfinal ab 10.12.",
        "facts": [],
    }
    facts = info["facts"]

    def fact(topics: list[str], text: Any, source: str | None = None) -> None:
        if not text:
            return
        t = str(text)
        facts.append({"topic": topics, "text": _clean(t), "src": src.from_text(t, source or default)})

    fact(
        ["uebersicht"],
        f"{ev.get('german') or name}: {ev.get('name')}, {dates.get('start')} bis {dates.get('end')} in {ev.get('city')}.",
        default,
    )
    if ev.get("organisers"):
        fact(
            ["uebersicht"],
            "Organisation: " + ", ".join(map(str, ev["organisers"])) + ".",
            official or default,
        )
    if ev.get("defending_champion"):
        fact(
            ["uebersicht", "geschichte"],
            f"Titelverteidiger: {NATION_DE.get(code(ev['defending_champion']) or '', ev['defending_champion'])} (WM 2024 in Malmö).",
            default,
        )
    if ev.get("claim"):
        fact(["uebersicht"], ev["claim"], src.add(ev.get("claim_source")) or default)
    if m := ev.get("mascot"):
        fact(
            ["uebersicht"],
            f"Maskottchen {m.get('name')}: {m.get('what')}",
            src.add(m.get("source"), when=str(m.get("retrieved") or "")),
        )
    for n in ev.get("next_events") or []:
        when = n.get("date")
        fact(
            ["zukunft", "geschichte"],
            f"{n.get('event')}: {n.get('city') or ''} {n.get('country') or ''}".strip(),
            src.add(n.get("source"), when=str(when) if when else None),
        )

    venues = []
    for v in raw.get("venues") or []:
        if isinstance(v, dict) and v.get("name"):
            venues.append(
                {
                    "city": ev.get("city"),
                    "arena": v["name"],
                    "rounds": v.get("use", ""),
                    "src": src.add(v.get("source")) or default,
                }
            )
            if v.get("note"):
                fact(["ort"], f"{v['name']}: {v['note']}", src.add(v.get("source")) or default)
    info["venues"] = venues

    fmt = raw.get("format") or {}
    fsrc = src.from_text(str(fmt.get("progression_source") or ""), default)
    for k in ("teams", "groups", "match_length"):
        if fmt.get(k):
            label = {"teams": "Teams", "groups": "Gruppen", "match_length": "Spielzeit"}[k]
            fact(["modus", "uebersicht"] if k != "match_length" else ["modus"], f"{label}: {fmt[k]}", fsrc)
    for p in fmt.get("progression") or []:
        fact(["modus", "viertelfinal"], p, fsrc)
    for k in ("group_ballot", "schedule_published"):
        b = fmt.get(k)
        if isinstance(b, dict):
            what = "Gruppenauslosung" if k == "group_ballot" else "Spielplan publiziert"
            fact(
                ["gruppen" if k == "group_ballot" else "spielplan"],
                f"{what}: {b.get('date')}" + (f", {b['place']}" if b.get("place") else ""),
                src.add(b.get("source"), when=str(b.get("date"))),
            )
    for path, text in (fmt.get("swiss_paths") or {}).items():
        fact(["viertelfinal", "spielplan"], f"Weg der Schweiz als {path}: {text}", fsrc)
    mg = fmt.get("medal_games") or {}
    for k, label in (("semifinals", "Halbfinals"), ("bronze", "Spiel um Bronze"), ("final", "Final")):
        if mg.get(k):
            fact(["viertelfinal", "spielplan"], f"{label}: {mg[k]}", fsrc)
    if fmt.get("schedule_changes_note"):
        fact(["spielplan", "viertelfinal"], fmt["schedule_changes_note"], fsrc)

    groups = raw.get("groups") or {}
    gsrc = src.add(groups.get("source")) or default
    info["groups"] = {
        g: {"teams": [c for c in (code(t) for t in teams) if c], "city": ev.get("city")}
        for g, teams in groups.items()
        if re.fullmatch(r"[A-Z]", str(g)) and isinstance(teams, list)
    }
    for k in ("pots_note", "canada"):
        fact(["gruppen", "teams"], groups.get(k), gsrc)
    for q in groups.get("qualification_facts") or []:
        fact(["teams"], q, gsrc)

    ref = raw.get("referees") or {}
    rsrc = src.add(ref.get("source")) or default
    if ref.get("pairs"):
        fact(["schiedsrichter"], "Schiedsrichter-Paare: " + "; ".join(ref["pairs"]), rsrc)
    if ref.get("head_of_referees"):
        fact(["schiedsrichter"], f"Chef der Schiedsrichter: {ref['head_of_referees']}", rsrc)

    sch = raw.get("schedule") or {}
    ssrc = src.from_text(str(sch.get("source") or ""), results or default)
    for k in ("opening_game", "finland_norway_note"):
        fact(["spielplan", "uebersicht"] if k == "opening_game" else ["spielplan"], sch.get(k), ssrc)
    games, playoffs = [], []
    for g in sch.get("games") or []:
        h, a = code(g.get("home")), code(g.get("away"))
        swiss = str(g.get("time_swiss_CET") or "")
        day = str(g.get("date"))
        url = g.get("result_url")
        gsrc_key = src.add(url, "Offizieller Resultatdienst (Torneopal)") if url else ssrc
        if h and a:
            utc = None
            if swiss:
                local = datetime.fromisoformat(f"{day}T{swiss}").replace(tzinfo=ZURICH)
                utc = local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
            games.append(
                {
                    "key": f"torneopal-{g['torneopal_match_id']}"
                    if g.get("torneopal_match_id")
                    else f"{event_key}-{day}-{h}-{a}",
                    "utc": utc or f"{day}T00:00:00Z",
                    "home": h,
                    "away": a,
                    "venue": g.get("venue"),
                    "city": ev.get("city"),
                    "url": url,
                    "source": "Torneopal",
                    "group": re.sub(r"^Group\s+", "", str(g.get("stage") or "")),
                }
            )
        else:
            what = f"{stage_de(str(g.get('stage') or ''))}: {_placeholder_de(g.get('home'))} – {_placeholder_de(g.get('away'))}"
            if g.get("game_no_inferred"):
                what += f" (Spiel {g['game_no_inferred']})"
            playoffs.append(
                {
                    "day": day,
                    "time": swiss,
                    "what": what,
                    "arena": g.get("venue"),
                    "src": gsrc_key,
                    # 2026-11-24: for a team's possible path (tournament.sec_schedule): slots + game number
                    "slots": [str(g.get("home") or ""), str(g.get("away") or "")],
                    "no": g.get("game_no_inferred"),
                    "stage": str(g.get("stage") or ""),
                }
            )
    info["games"], info["playoffs"] = games, playoffs

    past = raw.get("past_wfc") or {}
    psrc = past.get("sources") or {}
    wiki = src.add(psrc.get("finals_and_4th"), "Wikipedia: Men's World Floorball Championship", "wiki_wfc")
    medals = src.add(psrc.get("medals_unihockey_ch"), "Swiss Unihockey: Medaillen der Männer", "medals_sui")
    src.add(psrc.get("medal_table_iff"))
    hist, notes = [], []
    for e in past.get("editions") or []:
        g, s_, b = code(e.get("gold")), code(e.get("silver")), code(e.get("bronze"))
        extra = []
        if e.get("final"):
            extra.append(f"Final {e['final']}")
        if e.get("bronze_game"):
            extra.append(f"Bronzespiel {e['bronze_game']}")
        if e.get("fourth"):
            extra.append(f"4. {NATION_DE.get(code(e['fourth']) or '', e['fourth'])}")
        if e.get("swiss_coach"):
            extra.append(f"Schweizer Trainer: {e['swiss_coach']}")
        if e.get("note"):
            extra.append(_clean(e["note"]))
        hist.append(
            {
                "year": int(e["year"]),
                "host": e.get("host", ""),
                "gold": g or e.get("gold"),
                "silver": s_ or e.get("silver"),
                "bronze": b or e.get("bronze"),
                "sui": f"{e['switzerland']}." if e.get("switzerland") else "?",
                "extra": "; ".join(extra),
                "src": wiki or default,
            }
        )
    info["history"] = hist
    if mt := past.get("medal_table"):
        notes.append(
            {
                "text": "Ewige Medaillentabelle (Gold-Silber-Bronze): "
                + ", ".join(
                    f"{NATION_DE.get(c, c)} {v.get('gold', 0)}-{v.get('silver', 0)}-{v.get('bronze', 0)}"
                    for c, v in mt.items()
                ),
                "src": medals or default,
            }
        )
    for f in past.get("facts") or []:
        notes.append({"text": _clean(f), "src": src.from_text(f, default)})
    summ = past.get("switzerland_summary") or {}
    for k in ("medals", "hosted_note", "medal_drought"):
        if summ.get(k):
            notes.append({"text": f"Schweiz: {summ[k]}", "src": medals or default})
    info["history_notes"] = notes
    info["history_source"] = wiki or default
    info["sources"] = src.items
    return info


# --- rosters (research format: rosters/<CODE>.yaml) -----------------------------------------------------------

POSITIONS = (
    ("goal", "G"),
    ("torh", "G"),
    ("keeper", "G"),
    ("defen", "D"),
    ("verteid", "D"),
    ("forward", "F"),
    ("center", "F"),
    ("centre", "F"),
    ("wing", "F"),
    ("stürm", "F"),
)
STAFF_ROLES = (
    ("head_coach", "Head Coach"),
    ("assistants", "Assistant Coach"),
    ("goalkeeper_coach", "Goalkeeper Coach"),
    ("athletic_coach", "Athletic Coach"),
    ("physios", "Physiotherapist"),
    ("team_manager", "Team Manager"),
    ("equipment", "Equipment Manager"),
    ("head_of_national_teams_men", "Head of National Teams"),
)


def position(value: Any) -> str | None:
    low = str(value or "").lower()
    for kw, c in POSITIONS:
        if low.startswith(kw) or low.split(" ")[0].startswith(kw):
            return c
    return None


def _league(value: Any) -> tuple[str | None, str | None]:
    """'L-UPL (SUI)' -> ('L-UPL', 'SUI'); 'SSL? (SWE)' -> ('SSL', 'SWE'); 'SSL (Svenska Superligan)' -> ('SSL', None)."""
    s = str(value or "").strip()
    if not s:
        return None, None
    mt = re.match(r"^(.*?)\??\s*\(([^)]*)\)\s*$", s)
    if not mt:
        return s.rstrip("?") or None, None
    inner = mt.group(2).strip()
    return mt.group(1).strip(" ?") or None, (inner if re.fullmatch(r"[A-Z]{3}", inner) else code(inner))


COACH_ROLES = (
    ("head", "Head Coach"),
    ("förbundskapten", "Head Coach"),
    ("assist", "Assistant Coach"),
    ("goal", "Goalkeeper Coach"),
    ("physio", "Physiotherapist"),
    ("manager", "Team Manager"),
    ("equipment", "Equipment Manager"),
    ("athletic", "Athletic Coach"),
    ("analyst", "Video Analyst"),
    ("doctor", "Team Doctor"),
)


def _role(value: Any) -> str:
    low = str(value or "").lower()
    for kw, role in COACH_ROLES:
        if kw in low:
            return role
    return re.sub(r"\s*\(.*\)$", "", str(value or "Staff")).strip().title()[:60] or "Staff"


def _url(value: Any) -> str | None:
    s = str(value or "")
    return s if s.startswith("http") else None


# 2026-11-24: the research rosters' `notes` mix real stories with notes about the research itself ("profile rows
# start at World Games 2025 (incomplete)", "spelled 'Phillip' on floorball.de", "Swiss club not named by LFS").
# Only the story parts become fun facts; a note with nothing else is none.
RESEARCH_NOTE = re.compile(
    r"\b(spelled|spelling|schreibt|writes|not (named|found|verified|among|in (the )?[\w ]{0,30}(list|call-?up))|"
    r"nicht (genannt|gefunden|verifiziert)|unknown|unbekannt|relationship|verwandtschaft|profile (rows?|stats|data|"
    r"exists)|in (the )?profile|senior (rows?|data)|listed|lists? (him|club|as)|announcement lists|caption|"
    r"own count|eigene zählung|incomplete|unvollständig|registration history|retrieved|checked|check the|"
    r"birth.?date|from .{0,40}(page|table)|wikipedia .{0,40}table|confirms|league (level )?(not )?verified|"
    r"added for clarity|reason not given|possibly|swiss connection|swiss club|^plays (in|for)|lists|club named|"
    r"probably the same|same surname)\b",
    re.I,
)
_NOTE_REF = re.compile(
    r"\s*\((?:same source|per that page|announcement|federation [\w ]+|all per [^)]*|[^)]*unverified[^)]*)\)|\s+as of retrieval",
    re.I,
)


def note_fun_fact(note: str) -> str | None:
    """The story parts of one research note (clauses split at ';' and sentence ends), without the research
    remarks; None when nothing is left."""
    text = _clean(str(note))
    text = re.sub(r"\s*[-–]\s*unverified\b[^;.]*", "", text, flags=re.I)
    text = _NOTE_REF.sub("", text)
    text = re.sub(r"\(\s*,?\s*[^()]*?\s*\)?$", lambda mt: "" if len(mt.group(0)) < 4 else mt.group(0), text)
    text = re.sub(r"\b(vs|ca|St|Nr|No|incl|approx|Dr|Jr)\.", "\\1\x00", text)  # abbreviations end no sentence
    parts = [x.replace("\x00", ".") for x in re.split(r";\s*|(?<=[\w)'][\w)'])\.\s+(?=[^\W\d_])", text)]
    keep = []
    for part in parts:
        part = part.strip(" .,-–")
        if not part or RESEARCH_NOTE.search(part) or len(re.findall(r"\w+", part)) < 2:
            continue
        keep.append(part)
    return "; ".join(keep) if keep else None


def is_research_roster(raw: dict) -> bool:
    return bool(raw.get("code")) and not raw.get("event")


def _staff(raw: dict, url: str | None) -> list[dict]:
    coaches = raw.get("coaches") or {}
    out = []
    if isinstance(coaches, list):
        for c in coaches:
            if isinstance(c, dict) and c.get("name"):
                item = {
                    "name": c["name"],
                    "role": _role(c.get("role")),
                    "source_url": _url(c.get("source")) or url,
                }
                if code(c.get("nationality")):
                    item["nationality"] = code(c["nationality"])
                out.append(item)
        return out
    csrc = _url(coaches.get("source")) or url
    for key, role in STAFF_ROLES:
        v = coaches.get(key)
        for person in v if isinstance(v, list) else [v]:
            if not person:
                continue
            name = person.get("name") if isinstance(person, dict) else str(person)
            if not name:
                continue
            item = {"name": name, "role": role, "source_url": csrc}
            if isinstance(person, dict) and code(person.get("nationality")):
                item["nationality"] = code(person["nationality"])
            out.append(item)
    return out


def normalize_roster(raw: dict, event_key: str, season: str = "2026-27") -> dict:
    """Research roster -> the seed roster format of seed/loader.load_roster_data (+ per-player funfacts from the
    `notes` and national-team totals as facts, each with its own source link when the line has one, else the
    announcement's)."""
    ann = raw.get("announced") or {}
    url = _url(ann.get("source_url")) or _url(raw.get("source_url"))
    when = str(ann.get("date") or raw.get("retrieved") or "")
    official = "official" in str(raw.get("status") or "")
    nat = raw["code"]
    cap = raw.get("captain")
    cap_name = cap.get("name") if isinstance(cap, dict) else (cap if isinstance(cap, str) else None)
    players = []
    for p in raw.get("players") or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        league, country = _league(p.get("league"))
        country = code(p.get("club_country") or p.get("country_of_club")) or country
        purl = _url(p.get("source")) or url
        entry: dict[str, Any] = {
            "name": p["name"],
            "pos": position(p.get("position")),
            "club": re.sub(r",\s*[^,]+$", "", str(p["club"])) if p.get("club") else None,
            "league": league,
            "birth": str(p["birth_date"]) if p.get("birth_date") else None,
            "birth_place": p.get("birthplace"),
        }
        if not country and league:
            from klugscheisser.answer.club_country import league_country

            country = league_country(league, p.get("club"))
        # 2026-11-24: the club's country always goes along (before, a club in the player's own country got none
        # and the loader's default made Danish, German, Japanese … clubs Swiss). Without league or country the
        # player's nation is the best guess (`country_guess`: a later explicit value wins).
        if p.get("club"):
            entry["country"] = country or nat
            entry["country_guess"] = not country
        if p.get("number"):
            entry["number"] = p["number"]
        if cap_name and p["name"] == cap_name:
            entry["captain"] = "C"
        facts = []
        if p.get("caps") is not None:
            bits = [f"{p['caps']} Länderspiele"]
            if p.get("goals") is not None:
                bits.append(
                    f"{p['goals']} Tore, {p.get('assists', '?')} Assists ({p.get('points', '?')} Punkte)"
                )
            as_of = p.get("caps_as_of") or when
            facts.append(
                {
                    "kind": "appearances",
                    "text": f"Nationalteam: {', '.join(bits)} (Stand {as_of})",
                    "source_url": _url(p.get("caps_source")) or purl,
                    "date": str(as_of),
                }
            )
        if p.get("wfc_debut") is True or p.get("wfc_debutant") is True:
            facts.append(
                {
                    "kind": "national_team_event",
                    "text": "WM-Debüt an der Unihockey-WM 2026 (erste A-WM)",
                    "source_url": url,
                    "date": when,
                }
            )
        if p.get("previous_wfc_tournaments") is not None:
            facts.append(
                {
                    "kind": "national_team_event",
                    "text": f"Frühere A-WM-Teilnahmen vor 2026: {p['previous_wfc_tournaments']}",
                    "source_url": purl,
                    "date": when,
                }
            )
        if facts:
            entry["facts"] = facts
        if p.get("notes"):
            ffs = []
            for note in p["notes"] if isinstance(p["notes"], list) else [p["notes"]]:
                note = str(note)
                text = note_fun_fact(note)
                if not text:
                    continue  # a research note (spelling, sources, what was not found): no fun fact
                m_ = URL.search(note)
                ffs.append(
                    {
                        "text": text,
                        "source_url": m_.group(0).rstrip(".,;") if m_ else purl,
                        "source": None if m_ else ann.get("source_title"),
                        "date": when,
                        "unverified": "unverified" in note.lower() or bool(p.get("unverified")),
                    }
                )
            if ffs:
                entry["funfacts"] = ffs
        players.append(entry)
    pool = [
        *((raw.get("wider_candidate_pool") or {}).get("players") or []),
        *(raw.get("reserves") or []),
    ]
    return {
        "event": event_key,
        "team": nat,
        "list_type": "confirmed" if official else "provisional",
        "season": season,
        "source": (ann.get("source_title") or "Kader")[:200],
        "source_url": url,
        "source_date": when,
        "players": players,
        "staff": _staff(raw, url),
        "also_called_up": [
            {
                "name": p["name"],
                "pos": position(p.get("position")),
                "birth": str(p["birth_date"]) if p.get("birth_date") else None,
            }
            for p in pool
            if isinstance(p, dict) and p.get("name")
        ],
        "squad_facts": [str(f) for f in raw.get("squad_facts") or []],
    }


def add_history_detail(info: dict, detail: dict) -> None:
    """wfc_history_detail.yaml -> head-to-head lines (topic "bilanz"), IFF records ("rekorde"), MVP / best goalie
    per edition (history `extra`) and Swiss WFC games per year (topic "bilanz", only lines with a source)."""
    from klugscheisser.tournament import NATION_DE

    sources = info.setdefault("sources", {})
    facts = info.setdefault("facts", [])

    def src(url: str | None, name: str | None = None) -> str:
        if not url:
            return info.get("history_source") or info.get("default_source") or ""
        for k, v in sources.items():
            if v.get("url") == url:
                return k
        key = f"h{len(sources) + 1}"
        host = urlsplit(url).netloc.removeprefix("www.")
        sources[key] = {
            "name": name or SITE_NAMES.get(host, host),
            "url": url,
            "kind": "web" if "wiki" in host else "official",
        }
        return key

    by_year = {int(e["year"]): e for e in detail.get("editions") or [] if e.get("year")}
    for nat, h in (detail.get("head_to_head") or {}).items():
        if not isinstance(h, dict):
            continue
        if not h.get("played"):
            text = f"Schweiz gegen {NATION_DE.get(nat, nat)}: an Männer-WMs noch nie gegeneinander gespielt"
        else:
            text = (
                f"Schweiz gegen {NATION_DE.get(nat, nat)} an Männer-WMs: {h.get('played')} Spiele, "
                f"{h.get('w')} Siege, {h.get('d')} Unentschieden, {h.get('l')} Niederlagen, Tore {h.get('gf')}:{h.get('ga')}"
            )
        if h.get("games"):
            text += " – " + "; ".join(stages_de(str(g)) for g in h["games"])
        # compiled from the per-edition game lists (Wikipedia/IFF): cited with the WFC history page
        facts.append(
            {
                "topic": ["bilanz"],
                "text": text,
                "src": src(None),
                "opponent": nat,
                "played": int(h.get("played") or 0),
            }
        )
    rec = detail.get("iff_records") or {}
    rsrc = src(rec.get("source"), "IFF, Men's WFC records")
    labels = {
        "all_time_points": "Meiste WM-Punkte (ewig)",
        "swiss_in_all_time_top10": "Schweizer in den ewigen Top 10",
        "most_assists_all_time": "Meiste Assists (ewig)",
        "single_tournament_points": "Meiste Punkte an einer WM",
        "single_tournament_assists": "Meiste Assists an einer WM",
        "single_tournament_goals": "Meiste Tore an einer WM",
    }
    for k, label in labels.items():
        v = rec.get(k)
        if v:
            facts.append(
                {
                    "topic": ["rekorde", "geschichte"],
                    "text": f"{label}: {', '.join(v) if isinstance(v, list) else v}",
                    "src": rsrc,
                }
            )
    for row in info.get("history") or []:
        e = by_year.get(int(row["year"]))
        if not e:
            continue
        bits = []
        for k, label in (("mvp", "MVP"), ("best_goalkeeper", "bester Torhüter"), ("top_scorer", "Topskorer")):
            v = e.get(k)
            if isinstance(v, dict) and v.get("name"):
                bits.append(f"{label} {v['name']} ({v.get('country', '')})".replace(" ()", ""))
        if bits:
            row["extra"] = "; ".join([x for x in (row.get("extra"), ", ".join(bits)) if x])
        games = [
            f"{stage_de(str(g.get('stage') or ''))}: Schweiz – {NATION_DE.get(g.get('opponent'), g.get('opponent'))} {g.get('score')}"
            + (f" ({g['ot_pso']})" if g.get("ot_pso") else "")
            for g in e.get("switzerland_games") or []
        ]
        if games:
            facts.append(
                {
                    "topic": ["bilanz"],
                    "text": f"WM {e['year']}, Spiele der Schweiz: " + "; ".join(games),
                    "src": src(
                        next((u for u in e.get("sources") or [] if "wikipedia" in u), None)
                        or next(iter(e.get("sources") or []), None)
                    ),
                    "year": int(e["year"]),
                    "opponents": sorted({str(g.get("opponent")) for g in e.get("switzerland_games") or []}),
                }
            )
