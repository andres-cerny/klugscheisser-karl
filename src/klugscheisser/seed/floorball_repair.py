"""2026-11-24: one-off repair of a floorball project loaded before the seed fixes (`seed floorball-repair`).

1. Club countries: clubs the old loader stored as Swiss although the roster says otherwise
   (`floorball.repair_club_countries`).
2. Fun facts from the seeds, without duplicating rows that were translated to German in place (the English seed
   text is kept as `value.text_en`):
   - a stored seed fun fact that is only a research note ("profile rows start at World Games 2025 (incomplete)")
     is hidden: kind `research_note`, status `unconfirmed` (nothing is deleted);
   - one whose research remarks were cut away gets the cleaned text (German from the translations file);
   - fun facts the old loader skipped (a source line "(same article)", "(as above)" …) or that a cleaned note
     yields anew are added in German, with the English text as `value.text_en`.
German texts come from a JSON file {English: German} (no model call here); an item without one is reported and
not stored, so no English text reaches a German project. Dry run by default.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from klugscheisser.db import models as m
from klugscheisser.seed import floorball as fb
from klugscheisser.seed.floorball_info import is_research_roster, normalize_roster, note_fun_fact


@dataclass
class Expected:
    athlete: m.Athlete
    text: str  # English seed text (the stable key)
    url: str
    source: str | None
    date: str | None
    unverified: bool


@dataclass
class RepairReport:
    club_countries: list[tuple] = field(default_factory=list)
    hidden: list[str] = field(default_factory=list)
    cleaned: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    need_german: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        return {k: len(v) for k, v in self.__dict__.items()}


def expected_fun_facts(s: Session, seeds_dir: Path, event_key: str) -> tuple[list[Expected], set[str]]:
    """What `seed floorball` stores as fun facts today (roster notes after the research-note filter, funfacts.md
    with resolved links), per person. Also returns the funfacts.md texts (never filtered)."""
    out: list[Expected] = []
    for rp in sorted((seeds_dir / "rosters").glob("*.yaml")):
        raw = yaml.safe_load(rp.read_text(encoding="utf-8")) or {}
        if not is_research_roster(raw) or "not_qualified" in str(raw.get("status") or ""):
            continue
        data = normalize_roster(raw, event_key)
        for p in data.get("players") or []:
            a = fb._find(s, p["name"]) if p.get("name") else None
            if a is None:
                continue
            for f in p.get("funfacts") or []:
                out.append(
                    Expected(
                        a,
                        f["text"],
                        f.get("source_url") or data.get("source_url") or "",
                        f.get("source") or data.get("source"),
                        f.get("date"),
                        bool(f.get("unverified")),
                    )
                )
    md_texts: set[str] = set()
    md = seeds_dir / "funfacts.md"
    if md.exists():
        items, _ = fb.parse_funfacts_md(md.read_text(encoding="utf-8"))
        for f in items:
            md_texts.add(f["text"])
            name = f.get("person")
            a = fb._find(s, name) if name else None
            for person in [a] if a is not None else fb._mentioned(s, f["text"]):
                out.append(
                    Expected(
                        person, f["text"], f["url"], f.get("source"), f.get("date"), bool(f.get("unverified"))
                    )
                )
    return out, md_texts


def _cleaned(text_en: str) -> str | None:
    """The stored English note after today's research-note filter (old rows of a list note: "['a', 'b']")."""
    parts: list[str] = [text_en]
    if text_en.startswith("[") and text_en.endswith("]"):
        try:
            parts = [str(x) for x in ast.literal_eval(text_en)]
        except (ValueError, SyntaxError):
            parts = [text_en]
    kept = [k for k in (note_fun_fact(x) for x in parts) if k]
    return "; ".join(kept) if kept else None


def repair(
    s: Session, seeds_dir: Path, event_key: str, german: dict[str, str], apply: bool = False
) -> RepairReport:
    rep = RepairReport(club_countries=fb.repair_club_countries(s, seeds_dir, event_key, apply=apply))
    expected, md_texts = expected_fun_facts(s, seeds_dir, event_key)
    rows = list(s.scalars(select(m.Fact).where(m.Fact.kind == "fun_fact", m.Fact.source == fb.SEED_SOURCE)))
    done: set[tuple[int, str]] = set()  # (person, English text) already covered by a cleaned row
    for f in rows:
        v = dict(f.value or {})
        en = str(v.get("text_en") or f.value_text)
        if en in md_texts:
            continue  # a curated funfacts.md item: never filtered
        new = _cleaned(en)
        name = s.get(m.Athlete, f.athlete_id).full_name
        if new is None:
            rep.hidden.append(f"{name}: {en[:90]}")
            if apply:
                v["hidden"] = "Recherche-Notiz, kein Fun Fact (2026-11-24)"
                f.value, f.kind, f.status = v, "research_note", "unconfirmed"
            continue
        if new == en:
            continue
        de = german.get(new)
        if de is None:
            rep.need_german.append(new)
            continue
        rep.cleaned.append(f"{name}: {new[:90]}")
        done.add((f.athlete_id, new))
        if apply:
            v.update({"text_en_raw": en, "text_en": new, "text": de})
            f.value, f.value_text = v, de
    s.flush()
    for e in expected:
        if (e.athlete.id, e.text) in done:
            continue
        done.add((e.athlete.id, e.text))
        known = s.scalar(
            select(m.Fact.id).where(
                m.Fact.athlete_id == e.athlete.id,
                m.Fact.kind.in_(("fun_fact", "research_note")),
                (m.Fact.value_text == e.text)
                | (m.Fact.value["text_en"].as_string() == e.text)
                | (m.Fact.value["text_en_raw"].as_string() == e.text),
            )
        )
        if known:
            continue
        de = german.get(e.text)
        if de is None:
            rep.need_german.append(e.text)
            continue
        rep.added.append(f"{e.athlete.full_name}: {de[:90]}")
        if apply:
            fb.add_fact(
                s,
                e.athlete,
                "fun_fact",
                de,
                e.url,
                source_name=e.source,
                when=e.date,
                extra={"text_en": e.text},
                unverified=e.unverified,
            )
    rep.need_german = list(dict.fromkeys(rep.need_german))
    s.flush()
    return rep


def load_german(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    return {str(k): str(v) for k, v in json.loads(Path(path).read_text(encoding="utf-8")).items() if v}
