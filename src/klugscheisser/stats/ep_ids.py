"""Find missing Elite Prospects ids from free sources (2026-11-01), without requesting Elite Prospects.

1. Stored links: items, facts and stations of the person that link `eliteprospects.com/player/<id>`.
2. Wikidata (property P2481 "Elite Prospects player ID", checked 2026-11-01): the person's own Wikidata item when
   Karl has its id, else female ice hockey players whose label is her name.

An id is only stored when the namesake check is solid: the person's own Wikidata item, or a Wikidata/linked
candidate whose birth date equals Karl's (or, without a birth date in Karl, a single candidate of the same
nationality). Everything else goes to the list for Ondrej (`fraglich`).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m

log = logging.getLogger(__name__)

ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "klugscheisser-athlete/0.1 (https://github.com/andres-cerny/klugscheisser_athlete)"
EP_LINK = re.compile(r"eliteprospects\.com/player/(\d+)")

BY_QID = """
SELECT ?p ?ep ?birth WHERE {
  VALUES ?p { %(values)s }
  ?p wdt:P2481 ?ep .
  OPTIONAL { ?p wdt:P569 ?birth }
}
"""
# female (Q6581072) ice hockey players (Q11774891) with an EP id and one of these labels/aliases
BY_NAME = """
SELECT ?p ?name ?ep ?birth ?country WHERE {
  VALUES ?name { %(values)s }
  ?p rdfs:label|skos:altLabel ?name .
  ?p wdt:P2481 ?ep; wdt:P21 wd:Q6581072; wdt:P106 wd:Q11774891 .
  OPTIONAL { ?p wdt:P569 ?birth }
  OPTIONAL { ?p wdt:P27/wdt:P298 ?country }
}
"""
ISO3_TO_IOC = {"CHE": "SUI", "DEU": "GER", "DNK": "DEN", "NLD": "NED", "LVA": "LAT", "SVN": "SLO"}


@dataclass
class Candidate:
    ep_id: str
    how: str  # "wikidata_eigenes_item" | "wikidata_name" | "gespeicherter_link"
    birth: date | None = None
    nation: str | None = None
    url: str | None = None


@dataclass
class Result:
    athlete_id: int
    name: str
    ep_id: str | None = None
    how: str | None = None
    why: str | None = None
    candidates: list[Candidate] = field(default_factory=list)


def stored_links(s: Session, ids: list[int]) -> dict[int, list[Candidate]]:
    out: dict[int, list[Candidate]] = defaultdict(list)
    pat = "%eliteprospects.com/player/%"
    q_items = (
        select(m.ItemAthlete.athlete_id, m.Item.url, m.Item.text)
        .join(m.Item, m.Item.id == m.ItemAthlete.item_id)
        .where(
            m.ItemAthlete.athlete_id.in_(ids),
            or_(m.Item.url.like(pat), m.Item.text.like(pat)),
        )
    )
    for aid, url, text in s.execute(q_items):
        found = set(EP_LINK.findall(url or ""))
        if not found:  # a text that links several players says nothing about which one is hers
            hits = set(EP_LINK.findall(text or ""))
            found = hits if len(hits) == 1 else set()
        for ep in found:
            out[aid].append(Candidate(ep, "gespeicherter_link", url=url))
    for model in (m.Fact, m.CareerStint):
        for aid, url in s.execute(
            select(model.athlete_id, model.source_url).where(
                model.athlete_id.in_(ids), model.source_url.like(pat)
            )
        ):
            for ep in EP_LINK.findall(url or ""):
                out[aid].append(Candidate(ep, "gespeicherter_link", url=url))
    return out


def _sparql(client: httpx.Client, query: str) -> list[dict]:
    resp = client.get(ENDPOINT, params={"query": query, "format": "json"})
    resp.raise_for_status()
    return resp.json()["results"]["bindings"]


def _v(row: dict, k: str) -> str | None:
    return (row.get(k) or {}).get("value")


def _d(v: str | None) -> date | None:
    try:
        return date.fromisoformat(v[:10]) if v else None
    except ValueError:
        return None


def wikidata_candidates(
    client: httpx.Client, athletes: list[m.Athlete]
) -> tuple[dict[int, list[Candidate]], int]:
    out: dict[int, list[Candidate]] = defaultdict(list)
    requests = 0
    by_qid = {a.wikidata_qid: a.id for a in athletes if a.wikidata_qid}
    qids = list(by_qid)
    for i in range(0, len(qids), 50):
        values = " ".join(f"wd:{q}" for q in qids[i : i + 50])
        requests += 1
        for row in _sparql(client, BY_QID % {"values": values}):
            q = (_v(row, "p") or "").rsplit("/", 1)[-1]
            if q in by_qid and (ep := _v(row, "ep")):
                out[by_qid[q]].append(
                    Candidate(ep, "wikidata_eigenes_item", _d(_v(row, "birth")), url=_v(row, "p"))
                )
    names: dict[str, list[int]] = defaultdict(list)
    for a in athletes:
        if a.id not in out:
            names[a.full_name].append(a.id)
    keys = list(names)
    for i in range(0, len(keys), 40):
        values = " ".join(
            f'"{n.replace(chr(34), "")}"@{lang}' for n in keys[i : i + 40] for lang in ("en", "de")
        )
        requests += 1
        for row in _sparql(client, BY_NAME % {"values": values}):
            name, ep = _v(row, "name"), _v(row, "ep")
            nation = _v(row, "country")
            for aid in names.get(name or "", []):
                if ep and ep not in {c.ep_id for c in out[aid]}:
                    out[aid].append(
                        Candidate(
                            ep,
                            "wikidata_name",
                            _d(_v(row, "birth")),
                            ISO3_TO_IOC.get(nation or "", nation),
                            _v(row, "p"),
                        )
                    )
    return out, requests


def decide(a: m.Athlete, cands: list[Candidate]) -> Result:
    r = Result(a.id, a.full_name, candidates=cands)
    ids = {c.ep_id for c in cands}
    if not cands:
        r.why = "nichts gefunden"
        return r
    own = [c for c in cands if c.how == "wikidata_eigenes_item"]
    if len({c.ep_id for c in own}) == 1:
        r.ep_id, r.how = own[0].ep_id, own[0].how
        return r
    if a.birth_date:
        same = {c.ep_id for c in cands if c.birth == a.birth_date}
        if len(same) == 1:
            r.ep_id, r.how = same.pop(), "geburtsdatum_gleich"
            return r
        r.why = "kein Kandidat mit gleichem Geburtsdatum" if not same else "mehrere Kandidaten"
        return r
    same_nation = {c.ep_id for c in cands if c.nation and c.nation == a.nationality}
    if len(ids) == 1 and len(same_nation) == 1:
        r.ep_id, r.how = same_nation.pop(), "einziger_kandidat_gleiche_nation"
        return r
    r.why = "Karl kennt kein Geburtsdatum" + (" (mehrere Kandidaten)" if len(ids) > 1 else "")
    return r


def find_ids(
    s: Session, athlete_ids: list[int], client: httpx.Client | None = None, store: bool = False
) -> dict:
    """Look up EP ids for these persons (those without one). Stores only solid matches when `store`."""
    athletes = [
        a
        for a in s.scalars(select(m.Athlete).where(m.Athlete.id.in_(athlete_ids)))
        if not a.eliteprospects_id
    ]
    order = {aid: i for i, aid in enumerate(athlete_ids)}
    athletes.sort(key=lambda a: order.get(a.id, 0))
    cands = stored_links(s, [a.id for a in athletes])
    requests, error = 0, None
    try:
        client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)
        wd, requests = wikidata_candidates(client, athletes)
        for aid, cs in wd.items():
            cands[aid].extend(cs)
    except Exception as exc:  # stored links still count
        log.warning("ep ids: wikidata failed: %s", exc)
        error = str(exc)
    taken = set(
        s.scalars(select(m.Athlete.eliteprospects_id).where(m.Athlete.eliteprospects_id.is_not(None)))
    )
    found, unclear = [], []
    for a in athletes:
        r = decide(a, cands.get(a.id, []))
        if r.ep_id and r.ep_id in taken:
            r.why, r.ep_id = f"EP-ID {r.ep_id} gehört schon einer anderen Person in Karl", None
        if r.ep_id:
            found.append(r)
            if store:
                a.eliteprospects_id = r.ep_id
        elif r.candidates:
            unclear.append(r)
    if store:
        s.flush()
    return {
        "gesucht": len(athletes),
        "gefunden": [f"{r.athlete_id} {r.name}: {r.ep_id} ({r.how})" for r in found],
        "fraglich": [
            f"{r.athlete_id} {r.name}: {r.why}; Kandidaten "
            + ", ".join(
                f"{c.ep_id} ({c.how}, geb. {c.birth or '?'}, {c.nation or '?'})" for c in r.candidates
            )
            for r in unclear
        ],
        "ohne_kandidat": len(athletes) - len(found) - len(unclear),
        "wikidata_anfragen": requests,
        "fehler": error,
        **({} if store else {"dry_run": "nichts gespeichert (mit --yes speichern)"}),
    }
