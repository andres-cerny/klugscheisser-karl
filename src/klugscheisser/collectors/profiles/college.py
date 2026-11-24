"""Player bio pages on US college athletics sites (Sidearm Sports platform) as raw documents.

Which site belongs to which team comes from ``seeds/college_sites.yaml`` (team name -> roster URL). For an athlete
whose teams (affiliations) include such a college: roster page -> find the player by name -> bio page -> fields
(Hometown, Class, Previous Team, Major …) + bio text (career notes, "Personal: … majoring in …").

Both Sidearm layouts are handled: the classic one (``sidearm-roster-player-*`` classes) and the newer Nuxt one
(``<dt>/<dd>`` fields, ``s-text-paragraph-longform`` bio, JSON-LD Person as fallback).
Every request goes through PoliteClient: robots.txt is respected, Crawl-delay (30 s on these sites) is kept.
"""

from __future__ import annotations

import html as htmllib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import yaml

from klugscheisser.collectors.profiles.http import PoliteClient
from klugscheisser.collectors.profiles.text import cap_text, html_to_lines
from klugscheisser.collectors.rss import strip_html
from klugscheisser.domain.names import name_variants, normalize
from klugscheisser.interfaces import ProfileDoc

log = logging.getLogger(__name__)

COLLEGE_SEED = Path("seeds/college_sites.yaml")
SPORTS = {"icehockey_women"}  # the seeded roster URLs are women's ice hockey rosters


@dataclass
class CollegeSite:
    team: str
    source_name: str
    roster_url: str
    aliases: list[str] = field(default_factory=list)

    def matches(self, team_name: str) -> bool:
        return normalize(team_name) in {normalize(n) for n in [self.team, *self.aliases]}


@dataclass
class RosterEntry:
    name: str
    url: str


def load_sites(path: Path = COLLEGE_SEED) -> list[CollegeSite]:
    if not path.exists():
        return []
    return [CollegeSite(**entry) for entry in yaml.safe_load(path.read_text()) or []]


# --- roster ---------------------------------------------------------------------------------------

_PLAYER_LINK = re.compile(
    r'<a\b[^>]*?href="(?P<href>(?:https?://[^/"]+)?/sports/[^"/]+/roster/(?!coaches/|staff/)[^"/]+/\d+)"[^>]*>',
    re.I,
)
_LABEL = re.compile(r'(?:aria-label|title)="([^"]+)"')
_LABEL_SUFFIX = re.compile(
    r"\s*(?:-\s*View (?:Full Bio|Profile)|jersey number \d+ full bio|full bio)\s*$", re.I
)


def parse_roster(page: str, base_url: str) -> list[RosterEntry]:
    """Player links of a Sidearm roster page (coaches and staff excluded), name from aria-label/title."""
    found: dict[str, RosterEntry] = {}
    for mt in _PLAYER_LINK.finditer(page):
        url = urljoin(base_url, mt.group("href"))
        label = _LABEL.search(mt.group(0))
        name = _LABEL_SUFFIX.sub("", htmllib.unescape(label.group(1))).strip() if label else ""
        entry = found.get(url)
        if entry is None:
            slug = url.rstrip("/").split("/")[-2]
            found[url] = RosterEntry(name=name or slug.replace("-", " "), url=url)
        elif name and entry.name == entry.url.rstrip("/").split("/")[-2].replace("-", " "):
            entry.name = name
    return list(found.values())


def find_player(entries: list[RosterEntry], full_name: str) -> RosterEntry | None:
    """Exact name match in any spelling (Bächler = Baechler = Bachler); the URL slug as second chance."""
    wanted = name_variants(full_name)
    for e in entries:
        if name_variants(e.name) & wanted:
            return e
    for e in entries:
        slug = normalize(e.url.rstrip("/").split("/")[-2])
        if slug in wanted:
            return e
    return None


# --- bio page -------------------------------------------------------------------------------------

_CLASSIC_FIELD = re.compile(
    r'<span class="sidearm-roster-player-field-label">\s*([^<]+?)\s*</span>\s*<span[^>]*>\s*([^<]*?)\s*</span>',
    re.I,
)
_DT_DD = re.compile(r"<dt\b[^>]*>\s*([^<]+?)\s*:?\s*</dt>\s*<dd\b[^>]*>\s*([^<]*?)\s*</dd>", re.I)
_CLASSIC_BIO = re.compile(r'<section id="sidearm-roster-player-bio"[^>]*>(.*?)</section>', re.S | re.I)
_PROSE_START = re.compile(r'<div class="s-text-paragraph-longform[^"]*"[^>]*>', re.I)
_SKIP_LINES = {"biography", "bio"}


def _balanced_div(page: str, start: int) -> str:
    """Inner HTML of the <div> whose opening tag ends at ``start``."""
    depth = 1
    for mt in re.finditer(r"<(/?)div\b[^>]*>", page[start:], re.I):
        depth += -1 if mt.group(1) else 1
        if depth == 0:
            return page[start : start + mt.start()]
    return page[start:]


def _json_ld_person(page: str) -> str | None:
    for mt in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S | re.I):
        try:
            data = json.loads(mt.group(1))
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("@type") == "Person" and node.get("description"):
                return strip_html(node["description"])
    return None


def extract_bio(page: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(fields as (label, value), bio lines) of a Sidearm player page, verbatim."""
    fields = [(lab, val) for lab, val in _CLASSIC_FIELD.findall(page) if val]
    if not fields:
        fields = [(strip_html(lab).rstrip(":"), strip_html(val)) for lab, val in _DT_DD.findall(page) if val]
    seen: set[str] = set()
    fields = [(lab, val) for lab, val in fields if not (lab in seen or seen.add(lab))]

    lines: list[str] = []
    if mt := _CLASSIC_BIO.search(page):
        lines = html_to_lines(mt.group(1))
    elif mt := _PROSE_START.search(page):
        lines = html_to_lines(_balanced_div(page, mt.end()))
    if not lines and (desc := _json_ld_person(page)):
        lines = [desc]
    lines = [ln for ln in lines if ln.lower() not in _SKIP_LINES]
    return fields, lines


def bio_text(fields: list[tuple[str, str]], lines: list[str]) -> str:
    head = "\n".join(f"{lab}: {val}" for lab, val in fields)
    return "\n\n".join(part for part in (head, "\n".join(lines)) if part)


# --- collector ------------------------------------------------------------------------------------


class SidearmRosterCollector:
    name = "college"

    def __init__(
        self,
        sites: list[CollegeSite] | None = None,
        client: PoliteClient | None = None,
        max_chars: int = 6000,
    ) -> None:
        self.sites = load_sites() if sites is None else sites
        self.client = client or PoliteClient()
        self.max_chars = max_chars
        self._rosters: dict[str, list[RosterEntry]] = {}  # one roster fetch per site and run
        self.notes: list[str] = []

    def sites_for(self, teams: list[str]) -> list[CollegeSite]:
        return [s for s in self.sites if any(s.matches(t) for t in teams)]

    def roster(self, site: CollegeSite) -> list[RosterEntry]:
        if site.roster_url not in self._rosters:
            r = self.client.get(site.roster_url)
            r.raise_for_status()
            self._rosters[site.roster_url] = parse_roster(r.text, str(r.url))
        return self._rosters[site.roster_url]

    def collect(self, full_name: str, sport: str, hints: dict) -> list[ProfileDoc]:
        if sport not in SPORTS:
            return []
        docs = []
        for site in self.sites_for(hints.get("teams") or []):
            entry = find_player(self.roster(site), full_name)
            if entry is None:
                self.notes.append(f"{full_name}: nicht im Kader von {site.source_name}")
                continue
            r = self.client.get(entry.url)
            r.raise_for_status()
            fields, lines = extract_bio(r.text)
            text = bio_text(fields, lines)
            if not text:
                continue
            docs.append(
                ProfileDoc(
                    url=entry.url,
                    title=f"{entry.name} – {site.source_name}",
                    text=cap_text(text, self.max_chars),
                    source_name=site.source_name,
                    source_kind="profile",
                    source_type="profile",
                    platform="web",
                    language="en",
                )
            )
        return docs
