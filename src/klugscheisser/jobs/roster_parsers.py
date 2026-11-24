"""Parsers for official WCh roster lists of single federations (used by jobs/roster_watch.py, 2026-10-28).

Each parser turns the readable text of one official page (see roster_watch.article_text) into
[{name, pos, number?, club_text?}]. A parser returns [] when the page does not look like its format, so a page
redesign never loads a half list (roster_watch also refuses lists shorter than MIN_PLAYERS).

- swehockey: swehockey.se article "Truppen / Målvakter / Backar / Forwards", lines "1 Ebba Svensson Träff, Linköping HC"
- jihf_pdf:  JIHF member PDF (text from `pdftotext -layout`): rows "HALLORAN, Rei 2001/03/22 25 Timra IK (SWE)",
             the group sizes "(3)" "(8)" "(12)" in the order GK, DF, FW; staff rows after "STAFF" are left out
- deb_team:  deb-online.de team page block "Kader für die IIHF Women's World Championship 2026": number, first
             name, last name on their own lines under Goalies / Verteidigung / Sturm (block printed twice)
- oehv:      eishockey.at article "TOR / VERTEIDIGUNG / ANGRIFF", lines "ADAMITSCH Anja | 24 Jahre alt | KSV Highlanders"
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unicodedata

SWE_POS = {"målvakter": "G", "backar": "D", "forwards": "F", "forwards:": "F", "anfallare": "F"}
SWE_LINE = re.compile(r"^(\d{1,2})\s+([^,\d][^,]+?),\s*(.+)$")


def parse_swehockey(text: str) -> list[dict]:
    players: list[dict] = []
    pos = None
    for line in text.splitlines():
        key = line.strip().lower()
        if key in SWE_POS:
            pos = SWE_POS[key]
            continue
        if pos is None:
            continue
        hit = SWE_LINE.match(line.strip())
        if hit:
            players.append(
                {
                    "name": hit.group(2).strip(),
                    "pos": pos,
                    "number": int(hit.group(1)),
                    "club_text": hit.group(3).strip(),
                }
            )
        elif players and pos == "F":
            break  # first line after the forwards ("Spelschema …") ends the list
    return players


JIHF_ROW = re.compile(
    r"([A-Z][A-Z'\-]+(?: [A-Z][A-Z'\-]+)*),\s+([A-Z][a-z]+(?:[ \-][A-Z][a-z]+)*)\s+(\d{4})/(\d{1,2})/(\d{1,2})\s+\d{1,2}\s+(.*)$"
)
JIHF_COUNT = re.compile(r"\((\d{1,2})\)")


def parse_jihf_text(text: str) -> list[dict]:
    """Rows in PDF order; positions from the group sizes (the GK/DF/FW labels sit in the middle of each group)."""
    text = unicodedata.normalize("NFKC", text)  # full-width digits "(３)" → "(3)"
    head = text.split("STAFF")[0]
    rows, counts = [], []
    for line in head.splitlines():
        counts += [int(c) for c in JIHF_COUNT.findall(line) if not JIHF_ROW.search(line)]
        hit = JIHF_ROW.search(line)
        if hit:
            surname, given = hit.group(1), hit.group(2)
            club = re.split(r"\s{2,}", hit.group(6).strip())[0].strip()
            orig = re.sub(r"^(GK|DF|FW)\s+", "", line[: hit.start()].strip())
            rows.append(
                {
                    "name": f"{given} {surname.title()}",
                    "name_orig": re.sub(r"\s+", " ", orig) or None,
                    "birth": f"{hit.group(3)}-{int(hit.group(4)):02d}-{int(hit.group(5)):02d}",
                    "club_text": club or None,
                }
            )
    if len(counts) != 3 or sum(counts) != len(rows):
        return []  # not the expected layout: never guess positions
    out = []
    for i, r in enumerate(rows):
        r["pos"] = "G" if i < counts[0] else ("D" if i < counts[0] + counts[1] else "F")
        out.append({k: v for k, v in r.items() if v is not None})
    return out


def pdf_text(data: bytes) -> str:
    """Text of a PDF via poppler's `pdftotext -layout` (installed on the server); '' when it is missing."""
    exe = shutil.which("pdftotext")
    if not exe:
        return ""
    r = subprocess.run([exe, "-layout", "-", "-"], input=data, capture_output=True, timeout=60, check=False)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


DEB_POS = {"goalies": "G", "verteidigung": "D", "sturm": "F"}


def parse_deb_team(text: str, marker: str = "Kader für die IIHF Women") -> list[dict]:
    start = text.find(marker)
    if start < 0:
        return []
    lines = [x.strip() for x in text[start:].split("\nStaff")[0].splitlines()[1:] if x.strip()]
    players, seen, pos, i = [], set(), None, 0
    while i < len(lines):
        key = lines[i].lower()
        if key in DEB_POS:
            pos, i = DEB_POS[key], i + 1
            continue
        if pos and lines[i].isdigit() and i + 2 < len(lines):
            name = f"{lines[i + 1]} {lines[i + 2]}"
            if name not in seen:
                seen.add(name)
                players.append({"name": name, "pos": pos, "number": int(lines[i])})
            i += 3
            continue
        i += 1
    return players


OEHV_POS = {"tor": "G", "verteidigung": "D", "angriff": "F"}
OEHV_LINE = re.compile(
    r"^([A-ZÄÖÜ][A-ZÄÖÜß'\- ]+?)\s+([A-ZÄÖÜ][^|]+?)\s*\|\s*\d{1,2}(?: Jahre alt)?\s*\|\s*(.+)$"
)


def parse_oehv(text: str) -> list[dict]:
    players, pos = [], None
    for line in text.splitlines():
        key = line.strip().lower()
        if key in OEHV_POS:
            pos = OEHV_POS[key]
            continue
        hit = OEHV_LINE.match(line.strip()) if pos else None
        if hit:
            surname = "-".join(p.capitalize() for p in hit.group(1).split("-"))
            players.append(
                {"name": f"{hit.group(2).strip()} {surname}", "pos": pos, "club_text": hit.group(3).strip()}
            )
        elif players and pos == "F":
            break
    return players


PARSERS = {"swehockey": parse_swehockey, "deb_team": parse_deb_team, "oehv": parse_oehv}
