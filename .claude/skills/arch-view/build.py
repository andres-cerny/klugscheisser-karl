#!/usr/bin/env python3
"""Build one self-contained HTML page from ARCHITECTURE.md (no LLM, deterministic).

Usage:
    python3 .claude/skills/arch-view/build.py --out <path> [--ref origin/main] [--live <hash>] [--title "..."]
                                              [--no-fetch] [--arch <file>] [--repo <dir>]

ARCHITECTURE.md is read from <ref> (git show <ref>:ARCHITECTURE.md), so the page always shows what is on
main, not the local working state. Only when the file is not on <ref> yet is the working-tree copy used
(the header then says "draft"). --arch <file> reads a given file instead.
Exit code 1 when there is no component table; warnings (table <-> diagram <-> flows <-> details mismatches)
are printed to stderr and counted in the last line ("N warnings").

Expected ARCHITECTURE.md format (full spec in .claude/skills/arch-view/SKILL.md):
- "## Overview" (text), one ```mermaid flowchart (subgraphs and edge labels welcome),
- a markdown table whose header starts with "Component", with the columns
  Component | Responsibility | Runs where (optional) | Key files | Depends on;
  the first cell is "`node_id` Display name" and every diagram node has one row,
- optional "## Main flows": "### <flow name>" followed by a numbered list; each step names component ids in
  backticks (the first one is the step's component),
- "## External dependencies", and a line "Last updated: ...",
- optional "## Component details": one "### `node_id` Name" subsection per component, with
  "#### How it works" (prose + numbered steps, shown in the panel; a step may end with " — via <tech>", shown
  as a muted label), "#### Stack & settings" (small tables: Uses | For what, Setting | Default | Meaning; shown
  in the panel) and/or "#### In the code" (function-level notes, collapsed together with key files and code
  dependencies). A subsection without "####" headings counts as "In the code". Markdown: lists, numbered
  lists, tables, code, bold, links, short code blocks.
- optional "## Services ..." section (intro text + one table): rendered full width under the diagram.
Any other "## " section is rendered as a note.
Node ids: letters and digits only (no "_" or "-"), so diagram edges can be matched in the rendered SVG.
Git is used to read the file, for the header ("As of <hash> on <branch>", optional live hash) and to build
GitHub/GitLab links from `git remote get-url origin`. Python standard library only.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from urllib.parse import quote

MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&"
    "family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap"
)


# ---------------------------------------------------------------- git helpers


def git(root: str, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


@dataclass
class Remote:
    kind: str  # github | gitlab | none
    base: str = ""  # https://github.com/owner/repo
    name: str = ""

    def file_url(self, path: str, branch: str, is_dir: bool) -> str:
        p = quote(path.strip("/"))
        if self.kind == "github":
            return f"{self.base}/{'tree' if is_dir else 'blob'}/{quote(branch)}/{p}"
        if self.kind == "gitlab":
            return f"{self.base}/-/{'tree' if is_dir else 'blob'}/{quote(branch)}/{p}"
        return path


def parse_remote(url: str, fallback_name: str) -> Remote:
    url = url.strip()
    m = re.match(r"^(?:https?://(?:[^@/]+@)?|ssh://git@|git@)([^/:]+)[:/](.+?)(?:\.git)?/?$", url)
    if not m:
        return Remote("none", name=fallback_name)
    host, path = m.group(1).lower(), m.group(2)
    name = path.rsplit("/", 1)[-1]
    if host == "github.com":
        return Remote("github", f"https://github.com/{path}", name)
    if host == "gitlab.com" or host.startswith("gitlab."):
        return Remote("gitlab", f"https://{host}/{path}", name)
    return Remote("none", name=name or fallback_name)


# ---------------------------------------------------------------- markdown parsing


@dataclass
class Component:
    id: str
    name: str
    responsibility: str
    runs: str
    files: list[tuple[str, str]]  # (label, target as written)
    deps: list[str]


@dataclass
class Flow:
    name: str
    steps: list[str]  # markdown text of each step


@dataclass
class Doc:
    overview: str = ""
    mermaid: str = ""
    components: list[Component] = field(default_factory=list)
    components_note: str = ""
    flows: list[Flow] = field(default_factory=list)
    external: str = ""
    extra: list[tuple[str, str]] = field(default_factory=list)  # (heading, markdown)
    details: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {"how", "stack", "code": md}
    services: str = ""
    last_updated: str = ""


LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
CODE_RE = re.compile(r"`([^`]+)`")


def split_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    cells, buf, in_code = [], "", False
    for ch in s:
        if ch == "`":
            in_code = not in_code
        if ch == "|" and not in_code:
            cells.append(buf.strip())
            buf = ""
        else:
            buf += ch
    cells.append(buf.strip())
    return cells


def parse_table(lines: list[str]) -> list[Component]:
    header = [c.lower() for c in split_row(lines[0])]

    def col(*names: str) -> int:
        for i, h in enumerate(header):
            if any(n in h for n in names):
                return i
        return -1

    ci, ri = col("component"), col("responsib")
    wi, fi, di = col("runs", "where"), col("key file", "files"), col("depends")
    out = []
    for line in lines[2:]:
        cells = split_row(line)

        def get(i: int, cells: list[str] = cells) -> str:
            return cells[i] if 0 <= i < len(cells) else ""

        m = re.match(r"^\s*`([^`]+)`\s*(.*)$", get(ci))
        if not m:
            print(f"warning: table row without `node_id`: {get(ci)!r}", file=sys.stderr)
            continue
        deps = [d.strip(" `") for d in re.split(r"[,;]", get(di))]
        deps = [d for d in deps if d and d not in ("—", "-", "–", "none", "n/a")]
        out.append(
            Component(
                id=m.group(1).strip(),
                name=m.group(2).strip() or m.group(1).strip(),
                responsibility=get(ri),
                runs="" if get(wi) in ("—", "-") else get(wi),
                files=LINK_RE.findall(get(fi)),
                deps=deps,
            )
        )
    return out


def parse_flows(lines: list[str]) -> list[Flow]:
    flows: list[Flow] = []
    for line in lines:
        h = re.match(r"^#{3,6}\s+(.+)$", line)
        if h:
            flows.append(Flow(h.group(1).strip(), []))
            continue
        s = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if s and flows:
            flows[-1].steps.append(s.group(1).strip())
        elif line.startswith((" ", "\t")) and line.strip() and flows and flows[-1].steps:
            flows[-1].steps[-1] += " " + line.strip()
    return [f for f in flows if f.steps]


def parse_details(lines: list[str]) -> dict[str, dict[str, str]]:
    """'### `id` Name' subsections → {id: {"how": md, "code": md}} (code fences kept)."""
    out: dict[str, dict[str, list[str]]] = {}
    cur: dict[str, list[str]] | None = None
    part = "code"
    fence = False
    for line in lines:
        if line.lstrip().startswith("```"):
            fence = not fence
        h = None if fence else re.match(r"^#{3}\s+`([^`]+)`", line)
        sub = None if fence else re.match(r"^#{4}\s+(.+?)\s*$", line)
        if h:
            cur = out.setdefault(h.group(1).strip(), {"how": [], "stack": [], "code": []})
            part = "code"
        elif sub and cur is not None:
            t = sub.group(1).lower()
            part = (
                "how"
                if ("how" in t or "overview" in t)
                else "stack"
                if ("stack" in t or "setting" in t)
                else "code"
            )
        elif cur is not None:
            cur[part].append(line)
    res: dict[str, dict[str, str]] = {}
    for k, v in out.items():
        d = {p: "\n".join(x).strip() for p, x in v.items() if "\n".join(x).strip()}
        if d:
            res[k] = d
    return res


def parse_doc(text: str) -> Doc:
    doc = Doc()
    m = re.search(r"^Last updated:.*$", text, re.M)
    if m:
        doc.last_updated = m.group(0).strip()
    m = re.search(r"```mermaid\s*\n(.*?)```", text, re.S)
    if m:
        doc.mermaid = m.group(1).rstrip()

    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)

    def clean(lines: list[str]) -> list[str]:
        out, fence = [], False
        for ln in lines:
            if ln.startswith("```"):
                fence = not fence
                continue
            if fence or ln.startswith("# ") or ln.startswith("Last updated:"):
                continue
            out.append(ln)
        return out

    for heading, lines in sections:
        h = heading.lower()
        if h.startswith("main flow") or h == "flows":
            doc.flows = parse_flows(lines)
            continue
        if h.startswith("component detail"):
            doc.details = parse_details(lines)
            continue
        table_start = next(
            (i for i, ln in enumerate(lines) if ln.lstrip().startswith("|") and "component" in ln.lower()), -1
        )
        if table_start >= 0 and not doc.components:
            j = table_start
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            doc.components = parse_table(lines[table_start:j])
            note = "\n".join(clean(lines[:table_start] + lines[j:])).strip()
            if h.startswith("overview") or not heading:
                doc.overview = (doc.overview + "\n\n" + note).strip()
            else:
                doc.components_note = note
            continue
        body = "\n".join(clean(lines)).strip()
        if not heading or h.startswith("overview"):
            doc.overview = (doc.overview + "\n\n" + body).strip()
        elif h.startswith("external"):
            doc.external = body
        elif h.startswith("services"):
            doc.services = body
        elif body:
            doc.extra.append((heading, body))
    return doc


EDGE_RE = re.compile(
    r"([A-Za-z0-9_][\w]*)\s*(?:[\[\(\{>][^\n]*?[\]\)\}])?\s*"  # from (optional shape)
    r"(?:-->|-\.->|==>|---|-\.-|===)\s*(?:\|([^|]*)\|)?\s*"  # arrow + optional |label|
    r"([A-Za-z0-9_][\w]*)"
)
EDGE_TEXT_RE = re.compile(r"([A-Za-z0-9_][\w]*)\s*--\s+([^->|]+?)\s+-->\s*([A-Za-z0-9_][\w]*)")


def mermaid_graph(src: str) -> tuple[set[str], list[dict]]:
    """Node ids and edges ({from, to, label}) of a flowchart. Subgraph ids are not nodes."""
    ids: set[str] = set()
    edges: list[dict] = []
    for line in src.splitlines():
        s = line.strip()
        if not s or s.startswith("%%"):
            continue
        if re.match(r"^(flowchart|graph|subgraph|end|classDef|class|style|click|linkStyle|direction)\b", s):
            continue
        found = False
        for rx in (EDGE_TEXT_RE, EDGE_RE):
            for m in rx.finditer(s):
                a, label, b = (m.group(1), m.group(2), m.group(3))
                edges.append({"from": a, "to": b, "label": (label or "").strip().strip('"')})
                ids.update((a, b))
                found = True
            if found:
                break
        if not found:
            m = re.match(r"^([A-Za-z0-9_][\w]*)\s*[\[\(\{>]", s)
            if m:
                ids.add(m.group(1))
    return ids, edges


# ---------------------------------------------------------------- tiny markdown → HTML


class Linker:
    def __init__(self, root: str, remote: Remote, ref: str, branch: str):
        self.root, self.remote, self.ref, self.branch = root, remote, ref, branch
        self._dir: dict[str, bool] = {}

    def is_dir(self, path: str) -> bool:
        p = path.strip("/")
        if p not in self._dir:
            t = git(self.root, "cat-file", "-t", f"{self.ref}:{p}", check=False) if p else "tree"
            self._dir[p] = (
                (t == "tree") if t else (path.endswith("/") or os.path.isdir(os.path.join(self.root, p)))
            )
        return self._dir[p]

    def href(self, target: str) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            return target
        path = target.split("#", 1)[0]
        if path.startswith("./"):
            path = path[2:]
        return self.remote.file_url(path, self.branch, self.is_dir(path))


def inline_md(text: str, linker: Linker, ids: set[str] | None = None) -> str:
    """Inline markdown. With `ids`, `code` that names a component becomes a button that opens it."""
    parts = re.split(r"(`[^`]+`)", text)
    out = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            inner = part[1:-1]
            if ids and inner in ids:
                out.append(
                    f'<button type="button" class="chip" data-open="{html.escape(inner, True)}">'
                    f"{html.escape(inner)}</button>"
                )
            else:
                out.append(f"<code>{html.escape(inner)}</code>")
            continue
        pieces, last = [], 0
        for m in LINK_RE.finditer(part):
            pieces.append(html.escape(part[last : m.start()]))
            label = CODE_RE.sub(r"\1", m.group(1))
            pieces.append(
                f'<a href="{html.escape(linker.href(m.group(2)), quote=True)}" target="_blank" rel="noopener">'
                f"{html.escape(label)}</a>"
            )
            last = m.end()
        pieces.append(html.escape(part[last:]))
        s = "".join(pieces)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
        out.append(s)
    return "".join(out)


VIA_RE = re.compile(r"^(.*?)\s+[—–]\s+via\s+(.+)$", re.S)


def li_md(item: str, linker: Linker, ordered: bool) -> str:
    """A list item; in numbered lists a trailing " — via <tech>" becomes a muted label."""
    m = VIA_RE.match(item) if ordered else None
    if not m:
        return inline_md(item, linker)
    return (
        f'<span class="step">{inline_md(m.group(1).rstrip(" ."), linker)}</span> '
        f'<span class="via"><span class="via-k">via</span> {inline_md(m.group(2), linker)}</span>'
    )


def table_md(rows: list[str], linker: Linker) -> str:
    """Markdown table → <table class="md">; every cell carries its header as data-label (stacked on phones)."""
    head = split_row(rows[0])
    body = [split_row(r) for r in rows[1:] if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
    cls = f"md cols-{len(head)}"
    th = "".join(f'<th scope="col">{inline_md(h, linker)}</th>' for h in head)
    trs = []
    for r in body:
        tds = "".join(
            f'<td data-label="{html.escape(plain(head[i]) if i < len(head) else "", True)}">{inline_md(c, linker)}</td>'
            for i, c in enumerate(r)
        )
        trs.append(f"<tr>{tds}</tr>")
    return f'<div class="tbl"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{"".join(trs)}</tbody></table></div>'


def block_md(text: str, linker: Linker) -> str:
    out: list[str] = []
    para: list[str] = []
    items: list[str] = []
    table: list[str] = []
    kind = ["ul"]

    def flush_para() -> None:
        if para:
            out.append(f"<p>{inline_md(' '.join(para), linker)}</p>")
            para.clear()

    def flush_list() -> None:
        if items:
            tag = kind[0]
            out.append(
                f"<{tag}>" + "".join(f"<li>{li_md(i, linker, tag == 'ol')}</li>" for i in items) + f"</{tag}>"
            )
            items.clear()

    def flush_table() -> None:
        if table:
            out.append(table_md(table, linker))
            table.clear()

    fence: list[str] | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            if fence is None:
                flush_para()
                flush_list()
                fence = []
            else:
                out.append(f"<pre><code>{html.escape(chr(10).join(fence))}</code></pre>")
                fence = None
            continue
        if fence is not None:
            fence.append(raw)
            continue
        if line.lstrip().startswith("|"):
            flush_para()
            flush_list()
            table.append(line)
            continue
        flush_table()
        if not line.strip():
            flush_para()
            flush_list()
            continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        n = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        h = re.match(r"^(#{3,6})\s+(.*)$", line)
        if h:
            flush_para()
            flush_list()
            out.append(f"<h4>{inline_md(h.group(2), linker)}</h4>")
        elif m or n:
            flush_para()
            want = "ul" if m else "ol"
            if items and kind[0] != want:
                flush_list()
            kind[0] = want
            items.append((m or n).group(1))
        elif items and raw.startswith((" ", "\t")):
            items[-1] += " " + line.strip()
        else:
            flush_list()
            para.append(line.strip())
    if fence is not None:
        out.append(f"<pre><code>{html.escape(chr(10).join(fence))}</code></pre>")
    flush_table()
    flush_para()
    flush_list()
    return "\n".join(out)


def plain(text: str) -> str:
    """Markdown inline → plain text (for data passed to the script)."""
    text = LINK_RE.sub(r"\1", text)
    text = CODE_RE.sub(r"\1", text)
    return re.sub(r"\*\*?(.+?)\*\*?", r"\1", text)


# ---------------------------------------------------------------- page


CSS = r"""
/* Layout: header strip, flow buttons + diagram (own horizontal scroll), then components list | notes; details panel fixed right (desktop) or bottom sheet (phone). */
:root {
  --bg: #eef2f6;
  --surface: #ffffff;
  --surface-2: #f6f8fa;
  --fg: #142030;
  --fg-muted: #526274;
  --line: #c8d2dd;
  --line-strong: #8fa1b4;
  --grid: #dfe6ee;
  --accent: #1d5bb8;
  --accent-soft: #e3ecf9;
  --flow: #a65f00;
  --flow-soft: #fdf0d9;
  --flow-line: #d98a12;
  --flow-ink: #ffffff;
  --focus: #1d5bb8;
  --node-fill: #ffffff;
  --node-text: #142030;
  --cluster: #f9fbfd;
  --edge: #5f7388;
  --shadow: #14203026;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b141e;
    --surface: #111d2a;
    --surface-2: #0f1925;
    --fg: #d9e3ee;
    --fg-muted: #8ea1b5;
    --line: #253649;
    --line-strong: #3e5670;
    --grid: #142334;
    --accent: #6ea8ff;
    --accent-soft: #16294a;
    --flow: #f2b84b;
    --flow-soft: #2e2410;
    --flow-line: #e0a23a;
    --flow-ink: #1a1203;
    --focus: #8cbcff;
    --node-fill: #13243a;
    --node-text: #d9e3ee;
    --cluster: #0e1a27;
    --edge: #7f96ad;
    --shadow: #00000080;
    color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #0b141e;
  --surface: #111d2a;
  --surface-2: #0f1925;
  --fg: #d9e3ee;
  --fg-muted: #8ea1b5;
  --line: #253649;
  --line-strong: #3e5670;
  --grid: #142334;
  --accent: #6ea8ff;
  --accent-soft: #16294a;
  --flow: #f2b84b;
  --flow-soft: #2e2410;
  --flow-line: #e0a23a;
  --flow-ink: #1a1203;
  --focus: #8cbcff;
  --node-fill: #13243a;
  --node-text: #d9e3ee;
  --cluster: #0e1a27;
  --edge: #7f96ad;
  --shadow: #00000080;
  color-scheme: dark;
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--fg);
  font: 400 15px/1.5 "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  overflow-x: hidden;
}
.page {
  max-width: 1280px;
  margin: 0 auto;
  padding-left: max(16px, env(safe-area-inset-left, 0px));
  padding-right: max(16px, env(safe-area-inset-right, 0px));
  padding-block: 20px 48px;
}
@media (min-width: 1000px) {
  body.panel-open .page { padding-right: calc(400px + 24px); max-width: none; }
}
a { color: var(--accent); text-underline-offset: 2px; }
code { font-family: "IBM Plex Mono", ui-monospace, monospace; font-size: 0.9em; overflow-wrap: anywhere; }
:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.num { font-variant-numeric: tabular-nums; font-family: "IBM Plex Mono", ui-monospace, monospace; }

/* header */
.top { border-bottom: 1px solid var(--line-strong); padding-bottom: 14px; margin-bottom: 18px; }
.eyebrow { font: 500 12px/1.2 "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-muted); margin: 0 0 6px; }
h1 { font-size: 28px; line-height: 1.15; font-weight: 600; margin: 0 0 10px; letter-spacing: -0.01em; }
.status { display: flex; flex-wrap: wrap; gap: 6px 18px; margin: 0; padding: 0; list-style: none; font-size: 13px; color: var(--fg-muted); }
.status strong { color: var(--fg); font-weight: 500; }
.status .warn { color: var(--flow); font-weight: 500; }
h2 { font-size: 13px; font-weight: 500; font-family: "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-muted); margin: 0 0 10px; }
h4 { font-size: 15px; margin: 14px 0 4px; font-weight: 600; }

/* flow toolbar */
.flowbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0 0 10px; }
.flowbar .lbl { font: 500 12px/1 "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-muted); margin-right: 4px; }
.fbtn { font: 500 14px/1.2 "IBM Plex Sans", sans-serif; padding: 8px 12px; border: 1px solid var(--line-strong); background: var(--surface); color: var(--fg); cursor: pointer; }
.fbtn:hover { border-color: var(--accent); }
.fbtn[aria-pressed="true"] { background: var(--flow-soft); border-color: var(--flow-line); color: var(--fg); box-shadow: inset 0 -3px 0 var(--flow-line); }
.fbtn.clear { color: var(--fg-muted); }

/* diagram */
.diagram-wrap { border: 1px solid var(--line); background-color: var(--surface-2);
  background-image: linear-gradient(var(--grid) 1px, transparent 1px), linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size: 24px 24px; }
.diagram { overflow-x: auto; overflow-y: hidden; padding: 16px; min-height: 120px; -webkit-overflow-scrolling: touch; }
.diagram svg { display: block; height: auto; margin: 0 auto; }
.diagram-note { margin: 0; padding: 12px 16px; font-size: 14px; color: var(--fg-muted); }
.hint { font-size: 12px; color: var(--fg-muted); margin: 6px 0 0; }
.diagram g.node { cursor: pointer; }
.diagram g.node:focus { outline: none; }
.diagram g.node:focus-visible :is(rect, path, polygon, circle, ellipse) { stroke: var(--focus) !important; stroke-width: 3px !important; }
.diagram g.node.is-selected :is(rect, path, polygon, circle, ellipse) { stroke: var(--accent) !important; stroke-width: 3px !important; fill: var(--accent-soft) !important; }
.diagram svg.flow-active g.node:not(.flow-on),
.diagram svg.flow-active .edgePath:not(.flow-on),
.diagram svg.flow-active path.flowchart-link:not(.flow-on),
.diagram svg.flow-active .edgeLabel:not(.flow-on) { opacity: 0.22; }
.diagram svg.flow-active g.node.flow-on :is(rect, path, polygon, circle, ellipse):not(.fnum-dot) { stroke: var(--flow-line) !important; stroke-width: 2.5px !important; fill: var(--flow-soft) !important; }
.diagram svg.flow-active path.flowchart-link.flow-on, .diagram svg.flow-active .edgePath.flow-on path { stroke: var(--flow-line) !important; stroke-width: 2.5px !important; }
.diagram .fnum-dot { fill: var(--flow-line); stroke: none; }
.diagram .fnum-txt { fill: var(--flow-ink); font: 600 12px "IBM Plex Mono", monospace; }
@media (prefers-reduced-motion: no-preference) {
  .diagram svg.flow-active g.node.flow-on { animation: flow-in 420ms ease-out both; animation-delay: calc(var(--i, 0) * 260ms); }
  @keyframes flow-in { from { opacity: 0.22; } to { opacity: 1; } }
}

/* flow steps under the diagram */
.steps { margin: 12px 0 0; padding: 12px 16px; background: var(--surface); border: 1px solid var(--line); border-left: 3px solid var(--flow-line); }
.steps[hidden] { display: none; }
.steps h3 { margin: 0 0 8px; font-size: 16px; font-weight: 600; }
.steps ol, .flows ol { margin: 0; padding-left: 22px; }
.steps li, .flows li { margin: 0 0 6px; font-size: 14px; }
.steps li::marker, .flows li::marker { font: 500 13px "IBM Plex Mono", monospace; color: var(--flow); }

/* lower sections */
.lower { display: grid; gap: 28px; margin-top: 28px; }
@media (min-width: 1000px) { .lower { grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr); align-items: start; } }
.comp-list { list-style: none; margin: 0; padding: 0; border-top: 1px solid var(--line); }
.comp-row { display: grid; grid-template-columns: 8.5em minmax(0, 1fr); gap: 2px 14px; width: 100%; text-align: left;
  padding: 10px 8px; border: 0; border-bottom: 1px solid var(--line); background: transparent; color: inherit; font: inherit; cursor: pointer; }
.comp-row:hover { background: var(--surface); }
.comp-row[aria-current="true"] { background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }
.cid { font: 500 13px/1.4 "IBM Plex Mono", monospace; color: var(--accent); overflow-wrap: anywhere; }
.cname { font-weight: 600; }
.cname small { font-weight: 400; color: var(--fg-muted); font-size: 13px; margin-left: 6px; }
.cresp { grid-column: 2 / 3; color: var(--fg-muted); font-size: 14px; }
@media (max-width: 640px) {
  .comp-row { grid-template-columns: minmax(0, 1fr); }
  .cresp { grid-column: 1 / 2; }
  .cname small { display: block; margin-left: 0; }
  h1 { font-size: 23px; }
}
.prose { font-size: 14px; max-width: 72ch; }
.prose p { margin: 0 0 10px; }
.prose ul { margin: 0 0 12px; padding-left: 18px; }
.prose li { margin: 0 0 4px; }
.side > section + section, .lower section + section { margin-top: 28px; }
.flows h4 { margin: 14px 0 6px; display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 10px; }
.linkbtn { font: 500 12px/1 "IBM Plex Mono", monospace; padding: 5px 7px; border: 1px solid var(--line-strong); background: var(--surface-2); color: var(--accent); cursor: pointer; }
.note { font-size: 13px; color: var(--fg-muted); margin: 10px 0 0; }
.prose pre { margin: 0 0 12px; padding: 10px 12px; background: var(--surface-2); border: 1px solid var(--line);
  font: 13px/1.45 "IBM Plex Mono", ui-monospace, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.prose pre code { font-size: inherit; }
.tech { border-bottom: 1px solid var(--line); padding: 0 8px; }
.tech > summary { cursor: pointer; padding: 6px 0 8px calc(8.5em + 14px); font: 500 12px/1.2 "IBM Plex Mono", monospace;
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--fg-muted); }
.tech > summary { list-style: none; }
.tech > summary::-webkit-details-marker { display: none; }
.tech > summary::before { content: "+ "; }
.tech[open] > summary::before { content: "\2212  "; }
.tech ul, .tech-body ul { list-style: square; }
.prose ol { margin: 0 0 12px; padding-left: 22px; }
.prose ol > li::marker { font: 500 13px "IBM Plex Mono", monospace; color: var(--accent); }
.how-body { margin-top: 2px; }
.how-body p:first-child { color: var(--fg); }
details.code { margin: 4px 0 4px; border-top: 1px dashed var(--line); padding-top: 4px; }
details.code > summary { cursor: pointer; list-style: none; padding: 6px 0; font: 500 11px/1.2 "IBM Plex Mono", monospace;
  letter-spacing: 0.06em; text-transform: uppercase; color: var(--fg-muted); }
details.code > summary::-webkit-details-marker { display: none; }
details.code > summary::before { content: "+ "; }
details.code[open] > summary::before { content: "\2212  "; }
details.code > .prose { font-size: 13px; color: var(--fg-muted); }
.tech > summary:hover { color: var(--accent); }
.tech[open] > summary { color: var(--accent); }
.tech > .prose { padding: 0 0 12px calc(8.5em + 14px); max-width: none; }
.comp-list li:has(> .tech) > .comp-row { border-bottom: 0; }
@media (max-width: 640px) { .tech > summary, .tech > .prose { padding-left: 0; } }
.tech-toggle { margin-top: 22px; font: 500 13px/1 "IBM Plex Mono", monospace; padding: 8px 10px; border: 1px solid var(--line-strong);
  background: var(--surface-2); color: var(--accent); cursor: pointer; width: 100%; text-align: left; }
.tech-toggle[aria-expanded="true"] { background: var(--accent-soft); }
.tech-body { margin-top: 12px; font-size: 13.5px; }
.tech-body ul { padding-left: 16px; }
.tech-body[hidden] { display: none; }
@media print { .tech > .prose { display: block; } .panel, .flowbar { display: none; } }
/* "via" labels after How-it-works steps */
.prose ol > li .via { display: inline; margin-left: 2px; font-size: 12.5px; color: var(--fg-muted); }
.prose ol > li .via-k { font: 500 10.5px/1 "IBM Plex Mono", monospace; letter-spacing: 0.06em; text-transform: uppercase;
  padding: 2px 5px; margin-right: 3px; border: 1px solid var(--line-strong); background: var(--surface-2); color: var(--accent);
  vertical-align: 1px; white-space: nowrap; }
.prose ol > li .via code { font-size: 0.92em; }

/* markdown tables (Stack & settings, Services); stacked into cards when narrow */
.tbl { margin: 0 0 12px; max-width: 100%; }
table.md { width: 100%; border-collapse: collapse; font-size: 13px; line-height: 1.4; }
table.md th { text-align: left; font: 500 11px/1.3 "IBM Plex Mono", monospace; letter-spacing: 0.05em; text-transform: uppercase;
  color: var(--fg-muted); padding: 6px 8px; border-bottom: 1px solid var(--line-strong); }
table.md td { padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; word-break: normal; }
table.md td:first-child { font-weight: 500; }
.prose h5.sub, .stack-body + h5 { font: 500 12px/1.2 "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-muted); margin: 14px 0 6px; }
.services { margin-top: 28px; }
.prose.wide { max-width: none; }
.services table.md td:first-child { width: 17%; }
.services table.md td:nth-child(2) { width: 33%; }
@media (max-width: 760px) {
  table.md thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
  table.md, table.md tbody, table.md tr, table.md td { display: block; width: auto !important; }
  table.md tr { padding: 8px 0; border-bottom: 1px solid var(--line); }
  table.md td { border: 0; padding: 1px 0; }
  table.md td:first-child { font-weight: 600; padding-bottom: 3px; }
  table.md td:not(:first-child)::before { content: attr(data-label) ": "; font: 500 10.5px/1.2 "IBM Plex Mono", monospace;
    letter-spacing: 0.05em; text-transform: uppercase; color: var(--fg-muted); }
}
/* the panel is narrow on every screen: always stacked there */
.panel table.md thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); }
.panel table.md, .panel table.md tbody, .panel table.md tr, .panel table.md td { display: block; width: auto; }
.panel table.md tr { padding: 7px 0; border-bottom: 1px solid var(--line); }
.panel table.md td { border: 0; padding: 1px 0; }
.panel table.md td:first-child { font-weight: 600; padding-bottom: 3px; }
.panel table.md td:not(:first-child)::before { content: attr(data-label) ": "; font: 500 10.5px/1.2 "IBM Plex Mono", monospace;
  letter-spacing: 0.05em; text-transform: uppercase; color: var(--fg-muted); }
.panel table.md.cols-2 td:not(:first-child)::before { content: none; }
@media (max-width: 760px) { table.md.cols-2 td:not(:first-child)::before { content: none; } }
.tech-body > h5:first-child { margin-top: 4px; }


/* chips (component ids) */
.chip { font: 500 13px/1 "IBM Plex Mono", monospace; padding: 4px 6px; border: 1px solid var(--line-strong); background: var(--surface-2);
  color: var(--accent); cursor: pointer; vertical-align: 1px; }
.chip:hover { border-color: var(--accent); }
.chips { display: flex; flex-wrap: wrap; gap: 6px; }

/* panel */
.panel { position: fixed; z-index: 10; background: var(--surface); color: var(--fg); border: 1px solid var(--line-strong);
  box-shadow: 0 8px 28px var(--shadow); display: flex; flex-direction: column; }
.panel[hidden] { display: none; }
@media (min-width: 1000px) { .panel { top: 0; right: 0; bottom: 0; width: 400px; border-width: 0 0 0 1px; } }
@media (max-width: 999px) {
  .panel { left: 0; right: 0; bottom: 0; max-height: 72vh; border-width: 1px 0 0 0; padding-bottom: env(safe-area-inset-bottom, 0px); }
}
.panel-head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 16px 12px; border-bottom: 1px solid var(--line);
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.panel-head .titles { flex: 1; min-width: 0; }
.panel-head h3 { margin: 2px 0 0; font-size: 20px; line-height: 1.2; font-weight: 600; }
.panel-close { flex: none; font: 500 13px/1 "IBM Plex Mono", monospace; padding: 8px 10px; border: 1px solid var(--line-strong);
  background: var(--surface-2); color: var(--fg); cursor: pointer; }
.panel-body { overflow-y: auto; padding: 14px 16px 24px;
  padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }
.panel-body h5 { font: 500 12px/1.2 "IBM Plex Mono", monospace; letter-spacing: 0.08em; text-transform: uppercase; color: var(--fg-muted); margin: 18px 0 6px; }
.panel-body p { margin: 0; }
.runs { display: inline-block; margin-top: 10px; font-size: 13px; padding: 4px 8px; background: var(--accent-soft); color: var(--fg); }
.files, .moves { list-style: none; margin: 0; padding: 0; }
.files li { padding: 3px 0; font-family: "IBM Plex Mono", monospace; font-size: 13px; overflow-wrap: anywhere; }
.moves li { padding: 5px 0; border-bottom: 1px solid var(--line); font-size: 14px; display: flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; }
.moves .what { color: var(--fg-muted); }
.empty { color: var(--fg-muted); font-size: 13px; }
@media (prefers-reduced-motion: no-preference) {
  .panel { animation: panel-in 160ms ease-out; }
  @keyframes panel-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .comp-row { transition: background-color 120ms; }
}
"""

JS = r"""
(function () {
  var DATA = JSON.parse(document.getElementById('arch-data').textContent);
  var byId = {};
  DATA.components.forEach(function (c) { byId[c.id] = c; });
  var usedBy = {};
  DATA.components.forEach(function (c) {
    c.deps.forEach(function (d) { (usedBy[d] = usedBy[d] || []).push(c.id); });
  });
  var panel = document.getElementById('panel');
  var diagram = document.getElementById('diagram');
  var nodeEls = {}, edgeEls = [];
  var current = null, activeFlow = -1, lastTrigger = null, techOpen = false;

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === 'class') e.className = attrs[k]; else e.setAttribute(k, attrs[k]);
    });
    if (text != null) e.textContent = text;
    return e;
  }
  function chip(id) {
    var b = el('button', { type: 'button', 'class': 'chip', 'data-open': id, 'aria-label': 'Open ' + (byId[id] ? byId[id].name : id) }, id);
    return b;
  }
  function chips(ids) {
    var wrap = el('div', { 'class': 'chips' });
    ids.forEach(function (d) { wrap.appendChild(byId[d] ? chip(d) : el('span', { 'class': 'chip' }, d)); });
    return wrap;
  }
  function moves(list, dirKey) {
    var ul = el('ul', { 'class': 'moves' });
    list.forEach(function (e) {
      var li = el('li');
      li.appendChild(chip(e[dirKey]));
      if (e.label) li.appendChild(el('span', { 'class': 'what' }, e.label));
      ul.appendChild(li);
    });
    return ul;
  }

  function openPanel(id, trigger) {
    var c = byId[id];
    if (!c) return;
    current = id;
    if (trigger) lastTrigger = trigger;
    var body = document.getElementById('panel-body');
    document.getElementById('panel-id').textContent = c.id;
    document.getElementById('panel-title').textContent = c.name;
    body.textContent = '';
    body.appendChild(el('p', null, c.responsibility));
    if (c.runs) body.appendChild(el('p', { 'class': 'runs' }, 'Runs: ' + c.runs));
    if (c.how) {
      body.appendChild(el('h5', null, 'How it works'));
      var how = el('div', { 'class': 'prose how-body' });
      how.innerHTML = c.how;  // built by build.py from ARCHITECTURE.md (escaped markdown)
      body.appendChild(how);
    }
    if (c.stack) {
      body.appendChild(el('h5', null, 'Stack & settings'));
      var st = el('div', { 'class': 'prose stack-body' });
      st.innerHTML = c.stack;  // built by build.py from ARCHITECTURE.md (escaped markdown)
      body.appendChild(st);
    }
    var ins = DATA.edges.filter(function (e) { return e.to === id; });
    var outs = DATA.edges.filter(function (e) { return e.from === id; });
    if (ins.length) { body.appendChild(el('h5', null, 'Gets from')); body.appendChild(moves(ins, 'from')); }
    if (outs.length) { body.appendChild(el('h5', null, 'Sends to')); body.appendChild(moves(outs, 'to')); }
    var fl = DATA.flows.map(function (f, i) {
      var nums = [];
      f.steps.forEach(function (s, j) { if (s.ids.indexOf(id) >= 0) nums.push(j + 1); });
      return { i: i, name: f.name, nums: nums };
    }).filter(function (x) { return x.nums.length; });
    if (fl.length) {
      body.appendChild(el('h5', null, 'Takes part in'));
      var ul = el('ul', { 'class': 'moves' });
      fl.forEach(function (x) {
        var li = el('li');
        var b = el('button', { type: 'button', 'class': 'linkbtn', 'data-flow': String(x.i) }, x.name);
        li.appendChild(b);
        li.appendChild(el('span', { 'class': 'what' }, (x.nums.length > 1 ? 'steps ' : 'step ') + x.nums.join(', ')));
        ul.appendChild(li);
      });
      body.appendChild(ul);
    }
    var ub = usedBy[c.id] || [];
    body.appendChild(el('h5', null, 'Used by'));
    body.appendChild(ub.length ? chips(ub) : el('p', { 'class': 'empty' }, 'nothing in the table'));
    // code details fold: In the code, key files, code dependencies (files are secondary, so folded away)
    var tid = 'tech-' + c.id;
    var tb = el('button', { type: 'button', 'class': 'tech-toggle', 'aria-expanded': techOpen ? 'true' : 'false', 'aria-controls': tid },
      techOpen ? 'Hide code details' : 'Show code details');
    var box = el('div', { 'class': 'prose tech-body', id: tid });
    if (c.code) {
      box.appendChild(el('h5', null, 'In the code'));
      var cd = el('div');
      cd.innerHTML = c.code;  // built by build.py from ARCHITECTURE.md (escaped markdown)
      box.appendChild(cd);
    }
    box.appendChild(el('h5', null, 'Key files'));
    var files = el('ul', { 'class': 'files' });
    c.files.forEach(function (f) {
      var li = el('li');
      li.appendChild(el('a', { href: f.href, target: '_blank', rel: 'noopener' }, f.label));
      files.appendChild(li);
    });
    if (!c.files.length) files.appendChild(el('li', { 'class': 'empty' }, 'none (outside the code)'));
    box.appendChild(files);
    box.appendChild(el('h5', null, 'Code depends on'));
    box.appendChild(c.deps.length ? chips(c.deps) : el('p', { 'class': 'empty' }, 'nothing'));
    box.hidden = !techOpen;
    tb.addEventListener('click', function () {
      techOpen = box.hidden;
      box.hidden = !techOpen;
      tb.setAttribute('aria-expanded', techOpen ? 'true' : 'false');
      tb.textContent = techOpen ? 'Hide code details' : 'Show code details';
    });
    body.appendChild(tb);
    body.appendChild(box);
    panel.hidden = false;
    document.body.classList.add('panel-open');
    body.scrollTop = 0;
    markSelected(true);
    document.getElementById('panel-title').focus();
  }
  function closePanel() {
    panel.hidden = true;
    document.body.classList.remove('panel-open');
    current = null;
    markSelected(false);
    if (lastTrigger && document.contains(lastTrigger)) lastTrigger.focus();
  }
  function scrollToNode(g) {
    var r = g.getBoundingClientRect(), d = diagram.getBoundingClientRect();
    if (r.left < d.left || r.right > d.right) diagram.scrollLeft += (r.left - d.left) - d.width / 2 + r.width / 2;
  }
  function markSelected(scroll) {
    Object.keys(nodeEls).forEach(function (id) { nodeEls[id].classList.toggle('is-selected', id === current); });
    document.querySelectorAll('.comp-row').forEach(function (b) {
      b.setAttribute('aria-current', b.dataset.id === current ? 'true' : 'false');
    });
    if (scroll && current && nodeEls[current]) scrollToNode(nodeEls[current]);
  }

  // ---- flows
  var SVGNS = 'http://www.w3.org/2000/svg';
  function setFlow(i) {
    activeFlow = (i === activeFlow) ? -1 : i;
    document.querySelectorAll('.fbtn[data-flow]').forEach(function (b) {
      b.setAttribute('aria-pressed', Number(b.dataset.flow) === activeFlow ? 'true' : 'false');
    });
    document.querySelectorAll('.steps').forEach(function (s) { s.hidden = Number(s.dataset.flow) !== activeFlow; });
    paintFlow();
  }
  function paintFlow() {
    var svg = diagram.querySelector('svg');
    if (!svg) return;
    svg.querySelectorAll('.fnum').forEach(function (n) { n.parentNode.removeChild(n); });
    svg.querySelectorAll('.flow-on').forEach(function (n) { n.classList.remove('flow-on'); n.style.removeProperty('--i'); });
    if (activeFlow < 0) { svg.classList.remove('flow-active'); return; }
    svg.classList.add('flow-active');
    var f = DATA.flows[activeFlow];
    var order = [];
    f.steps.forEach(function (s) { s.ids.forEach(function (id) { if (order.indexOf(id) < 0) order.push(id); }); });
    var firstStep = {};
    f.steps.forEach(function (s, j) { s.ids.forEach(function (id) { if (!(id in firstStep)) firstStep[id] = j + 1; }); });
    order.forEach(function (id, k) {
      var g = nodeEls[id];
      if (!g) return;
      g.classList.add('flow-on');
      g.style.setProperty('--i', String(k));
      if (firstStep[id]) {
        var bb = g.getBBox();
        var n = document.createElementNS(SVGNS, 'g');
        n.setAttribute('class', 'fnum');
        n.setAttribute('aria-hidden', 'true');
        var c = document.createElementNS(SVGNS, 'circle');
        c.setAttribute('class', 'fnum-dot');
        c.setAttribute('cx', bb.x + 2); c.setAttribute('cy', bb.y + 2); c.setAttribute('r', 10);
        var t = document.createElementNS(SVGNS, 'text');
        t.setAttribute('class', 'fnum-txt');
        t.setAttribute('x', bb.x + 2); t.setAttribute('y', bb.y + 6); t.setAttribute('text-anchor', 'middle');
        t.textContent = String(firstStep[id]);
        c.style.fill = 'var(--flow-line)'; c.style.stroke = 'none';
        t.style.fill = 'var(--flow-ink)'; t.style.fontSize = '12px'; t.style.fontWeight = '600';
        t.style.fontFamily = '"IBM Plex Mono", monospace';
        n.appendChild(c); n.appendChild(t); g.appendChild(n);
      }
    });
    edgeEls.forEach(function (e) {
      if (order.indexOf(e.from) >= 0 && order.indexOf(e.to) >= 0) e.els.forEach(function (x) { x.classList.add('flow-on'); });
    });
    if (order[0] && nodeEls[order[0]]) scrollToNode(nodeEls[order[0]]);
  }

  document.addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('[data-open],[data-flow],.comp-row') : null;
    if (!t) return;
    if (t.classList.contains('comp-row')) openPanel(t.dataset.id, t);
    else if (t.hasAttribute('data-open')) openPanel(t.getAttribute('data-open'), t);
    else if (t.hasAttribute('data-flow') && t.tagName === 'BUTTON') {
      var i = Number(t.getAttribute('data-flow'));
      if (t.classList.contains('linkbtn') && activeFlow === i) return;
      setFlow(i);
      if (t.classList.contains('linkbtn')) document.getElementById('diagram-top').scrollIntoView({ block: 'start' });
    }
  });
  document.getElementById('panel-close').addEventListener('click', closePanel);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !panel.hidden) closePanel(); });

  // ---- mermaid
  function token(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  var renderCount = 0;
  function showNote(msg) {
    diagram.textContent = '';
    diagram.appendChild(el('p', { 'class': 'diagram-note' }, msg));
  }
  function render() {
    if (!window.mermaid) { showNote('The diagram could not be loaded. All components and flows are listed below.'); return; }
    var src = document.getElementById('arch-src').textContent.replace(/<\\\/script/gi, '<' + '/script');
    var font = '"IBM Plex Sans", system-ui, sans-serif';
    try {
      window.mermaid.initialize({
        startOnLoad: false, securityLevel: 'strict', theme: 'base', fontFamily: font,
        flowchart: { htmlLabels: true, curve: 'basis', padding: 10, nodeSpacing: 30, rankSpacing: 48, subGraphTitleMargin: { top: 4, bottom: 8 } },
        themeVariables: {
          fontFamily: font, fontSize: '14px', background: token('--surface-2'),
          primaryColor: token('--node-fill'), primaryTextColor: token('--node-text'),
          primaryBorderColor: token('--line-strong'), secondaryColor: token('--surface'),
          tertiaryColor: token('--cluster'), lineColor: token('--edge'), textColor: token('--fg'),
          mainBkg: token('--node-fill'), nodeBorder: token('--line-strong'), clusterBkg: token('--cluster'),
          clusterBorder: token('--line-strong'), titleColor: token('--fg-muted'),
          edgeLabelBackground: token('--surface-2'), darkMode: token('color-scheme') === 'dark'
        }
      });
    } catch (e) { showNote('The diagram could not be rendered. All components and flows are listed below.'); return; }
    renderCount += 1;
    Promise.resolve(window.mermaid.render('arch-svg-' + renderCount, src)).then(function (out) {
      diagram.innerHTML = out.svg;
      if (out.bindFunctions) out.bindFunctions(diagram);
      var svg = diagram.querySelector('svg');
      if (svg && svg.viewBox && svg.viewBox.baseVal && svg.viewBox.baseVal.width) {
        var w = svg.viewBox.baseVal.width;
        svg.style.width = '100%';
        svg.style.maxWidth = w + 'px';
        svg.style.minWidth = Math.min(w, 860) + 'px';
      }
      wire();
    }).catch(function () {
      showNote('The diagram could not be rendered. All components and flows are listed below.');
      var stray = document.getElementById('darch-svg-' + renderCount);
      if (stray && stray.parentNode) stray.parentNode.removeChild(stray);
    });
  }
  function edgeKey(idStr) {
    // mermaid edge ids: L_<from>_<to>_<n> (v11) or L-<from>-<to>-<n> (older); ids here have no _ or -
    var m = /^L[_-](.+?)[_-](.+?)[_-]\d+$/.exec(idStr || '');
    return m ? m[1] + '>' + m[2] : null;
  }
  function wire() {
    nodeEls = {}; edgeEls = [];
    diagram.querySelectorAll('g.node').forEach(function (g) {
      var m = /(?:^|-)flowchart-(.+)-\d+$/.exec(g.id || '');
      var id = m && m[1];
      if (!id || !byId[id]) return;
      nodeEls[id] = g;
      g.setAttribute('tabindex', '0');
      g.setAttribute('role', 'button');
      g.setAttribute('aria-label', byId[id].name);
      g.addEventListener('click', function () { openPanel(id, g); });
      g.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openPanel(id, g); }
      });
    });
    var groups = {};
    diagram.querySelectorAll('[id^="L_"], [id^="L-"], [data-id^="L_"], [data-id^="L-"]').forEach(function (x) {
      var k = edgeKey(x.id && /^L[_-]/.test(x.id) ? x.id : x.getAttribute('data-id'));
      if (!k) return;
      var target = x.closest('.edgeLabel') || x;
      (groups[k] = groups[k] || []).push(target);
    });
    // edge labels carry no id: pair them with the edge paths by position when the counts match
    var paths = diagram.querySelectorAll('.edgePaths > path, .edgePaths > g > path');
    var labels = diagram.querySelectorAll('.edgeLabels > .edgeLabel');
    if (paths.length === labels.length) {
      for (var i = 0; i < paths.length; i++) {
        var k = edgeKey(paths[i].id);
        if (k && groups[k] && groups[k].indexOf(labels[i]) < 0) groups[k].push(labels[i]);
      }
    }
    DATA.edges.forEach(function (e) {
      var els = groups[e.from + '>' + e.to] || [];
      edgeEls.push({ from: e.from, to: e.to, els: els });
    });
    markSelected(false);
    paintFlow();
  }
  render();
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  if (mq) { if (mq.addEventListener) mq.addEventListener('change', render); else if (mq.addListener) mq.addListener(render); }
  new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
"""


def row_details(d: dict[str, str]) -> str:
    """Fold under a component row: How it works + Stack & settings, with the code notes nested one level deeper."""
    code = (
        f'<details class="code"><summary>Code details</summary><div class="prose">{d["code"]}</div></details>'
        if d.get("code")
        else ""
    )
    stack = (
        f'<h5 class="sub">Stack &amp; settings</h5><div class="stack-body">{d["stack"]}</div>'
        if d.get("stack")
        else ""
    )
    if d.get("how") or stack:
        return (
            f'<details class="tech"><summary>How it works</summary><div class="prose">{d.get("how", "")}{stack}{code}</div>'
            "</details>"
        )
    if code:
        return f'<details class="tech"><summary>Code details</summary><div class="prose">{d["code"]}</div></details>'
    return ""


def build_html(doc: Doc, linker: Linker, edges: list[dict], meta: dict) -> str:
    e = html.escape
    comps = doc.components
    ids = {c.id for c in comps}
    details_html = {
        k: {p: block_md(md, linker) for p, md in v.items()} for k, v in doc.details.items() if k in ids
    }
    flows = [
        {
            "name": f.name,
            "steps": [{"ids": [x for x in CODE_RE.findall(s) if x in ids]} for s in f.steps],
        }
        for f in doc.flows
    ]
    data = {
        "components": [
            {
                "id": c.id,
                "name": c.name,
                "responsibility": plain(c.responsibility),
                "runs": plain(c.runs),
                "files": [{"label": plain(lbl), "href": linker.href(t)} for lbl, t in c.files],
                "deps": c.deps,
                "how": details_html.get(c.id, {}).get("how", ""),
                "stack": details_html.get(c.id, {}).get("stack", ""),
                "code": details_html.get(c.id, {}).get("code", ""),
            }
            for c in comps
        ],
        "edges": [x for x in edges if x["from"] in ids and x["to"] in ids],
        "flows": flows,
    }
    data_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    mermaid_src = doc.mermaid.replace("</script", "<\\/script")

    status = [f'<li>As of <strong class="num">{e(meta["head"])}</strong> on {e(meta["branch"])}</li>']
    if meta.get("live"):
        behind = meta["live_behind"]
        cls = ' class="warn"' if behind else ""
        status.append(
            f'<li>Live runs <strong class="num">{e(meta["live"])}</strong> '
            f"(<span{cls}>{behind} commit{'' if behind == 1 else 's'} behind {e(meta['branch'])}</span>)</li>"
        )
    if doc.last_updated:
        status.append(f"<li>{inline_md(doc.last_updated, linker)}</li>")
    if meta.get("draft"):
        status.append(
            f'<li><span class="warn">Draft: ARCHITECTURE.md is not on {e(meta["branch"])} yet</span></li>'
        )

    flowbar = ""
    steps_boxes = ""
    flows_static = ""
    if doc.flows:
        btns = "".join(
            f'<button type="button" class="fbtn" data-flow="{i}" aria-pressed="false">{e(f.name)}</button>'
            for i, f in enumerate(doc.flows)
        )
        flowbar = f'<div class="flowbar" role="group" aria-label="Show a flow"><span class="lbl">Show a flow</span>{btns}</div>'
        for i, f in enumerate(doc.flows):
            lis = "".join(f"<li>{inline_md(s, linker, ids)}</li>" for s in f.steps)
            steps_boxes += f'<div class="steps" data-flow="{i}" hidden aria-live="polite"><h3>{e(f.name)}</h3><ol>{lis}</ol></div>'
            flows_static += (
                f'<h4>{e(f.name)} <button type="button" class="linkbtn" data-flow="{i}">show in diagram</button></h4>'
                f"<ol>{lis}</ol>"
            )
        flows_static = f'<section class="flows"><h2>Main flows</h2>{flows_static}</section>'

    rows = []
    for c in comps:
        runs = f"<small>{inline_md(c.runs, linker)}</small>" if c.runs else ""
        rows.append(
            f'<li><button type="button" class="comp-row" data-id="{e(c.id, True)}" aria-current="false">'
            f'<span class="cid">{e(c.id)}</span><span class="cname">{e(c.name)}{runs}</span>'
            f'<span class="cresp">{inline_md(c.responsibility, linker)}</span></button>'
            + row_details(details_html.get(c.id, {}))
            + "</li>"
        )
    extra = "".join(
        f'<section><h2>{e(h)}</h2><div class="prose">{block_md(body, linker)}</div></section>'
        for h, body in doc.extra
    )
    note = f'<p class="note">{inline_md(doc.components_note, linker)}</p>' if doc.components_note else ""
    services = (
        f'<section class="services" aria-labelledby="h-services"><h2 id="h-services">Services &amp; what they\'re for</h2>'
        f'<div class="prose wide">{block_md(doc.services, linker)}</div></section>'
        if doc.services
        else ""
    )
    external = (
        f'<section><h2>External dependencies</h2><div class="prose">{block_md(doc.external, linker)}</div></section>'
        if doc.external
        else ""
    )

    return f"""<title>{e(meta["title"])}</title>
<meta name="claude:viewport-fit-sides" content="cover">
<style>{CSS}</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="{e(FONTS_URL, True)}">
<div class="page">
  <header class="top">
    <p class="eyebrow">{e(meta["repo"])} · architecture map</p>
    <h1>{e(meta["title"])}</h1>
    <ul class="status">{"".join(status)}</ul>
  </header>
  <main>
    <section aria-labelledby="h-diagram" id="diagram-top">
      <h2 id="h-diagram">What runs where, and how data moves</h2>
      {flowbar}
      <div class="diagram-wrap"><div class="diagram" id="diagram" tabindex="-1"><p class="diagram-note">Rendering diagram…</p></div></div>
      <p class="hint">Select a box or a row below for details. Arrows are labelled with what moves.</p>
      {steps_boxes}
    </section>
    {services}
    <div class="lower">
      <div>
        <section aria-labelledby="h-comps">
          <h2 id="h-comps">Components</h2>
          <ul class="comp-list">{"".join(rows)}</ul>
          {note}
        </section>
        {flows_static}
      </div>
      <div class="side">
        <section><h2>Overview</h2><div class="prose">{block_md(doc.overview, linker)}</div></section>
        {extra}
        {external}
      </div>
    </div>
  </main>
</div>
<aside class="panel" id="panel" hidden aria-labelledby="panel-title">
  <div class="panel-head">
    <div class="titles"><p class="eyebrow" id="panel-id"></p><h3 id="panel-title" tabindex="-1"></h3></div>
    <button type="button" class="panel-close" id="panel-close" aria-label="Close details">Close</button>
  </div>
  <div class="panel-body" id="panel-body"></div>
</aside>
<script type="text/plain" id="arch-src">{mermaid_src}</script>
<script type="application/json" id="arch-data">{data_json}</script>
<script src="{MERMAID_URL}"></script>
<script>{JS}</script>
"""


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="output HTML file")
    ap.add_argument(
        "--ref",
        default="origin/main",
        help="ref shown in the header and used for links (default origin/main)",
    )
    ap.add_argument("--live", help="commit that runs live (header shows how far behind it is)")
    ap.add_argument("--title", help='page title (default "<repo> Architecture")')
    ap.add_argument("--no-fetch", action="store_true", help="skip git fetch")
    ap.add_argument("--arch", help="path to ARCHITECTURE.md (default: repo root)")
    ap.add_argument("--repo", default=".", help="any path inside the repository (default: cwd)")
    ap.add_argument("--since", help=argparse.SUPPRESS)  # accepted for older callers, no longer used
    a = ap.parse_args()

    root = git(a.repo, "rev-parse", "--show-toplevel")
    if not a.no_fetch:
        r = subprocess.run(["git", "-C", root, "fetch", "--quiet"], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"warning: git fetch failed, using local refs ({r.stderr.strip()})", file=sys.stderr)
    draft = False
    if a.arch:
        with open(a.arch, encoding="utf-8") as f:
            text = f.read()
    else:
        r = subprocess.run(
            ["git", "-C", root, "show", f"{a.ref}:ARCHITECTURE.md"], capture_output=True, text=True
        )
        if r.returncode == 0:
            text = r.stdout
        else:
            local = os.path.join(root, "ARCHITECTURE.md")
            if not os.path.exists(local):
                print(f"error: ARCHITECTURE.md is neither on {a.ref} nor in {root}", file=sys.stderr)
                return 1
            print(
                f"note: ARCHITECTURE.md is not on {a.ref} yet; using the working-tree copy (shown as draft)",
                file=sys.stderr,
            )
            with open(local, encoding="utf-8") as f:
                text = f.read()
            draft = True
    doc = parse_doc(text)
    if not doc.components:
        print("error: no component table found in ARCHITECTURE.md", file=sys.stderr)
        return 1

    remote = parse_remote(git(root, "remote", "get-url", "origin", check=False), os.path.basename(root))
    full = git(root, "rev-parse", "--symbolic-full-name", a.ref, check=False)
    if full.startswith("refs/remotes/"):
        branch = full[len("refs/remotes/") :].split("/", 1)[-1]  # origin/main -> main
    elif full.startswith("refs/heads/"):
        branch = full[len("refs/heads/") :]  # local branch, slashes kept (docs/arch-update-...)
    else:
        branch = a.ref  # a commit or tag
    linker = Linker(root, remote, a.ref, branch)

    # consistency checks: table ↔ diagram ↔ flows
    node_ids, edges = mermaid_graph(doc.mermaid)
    table_ids = [c.id for c in doc.components]
    problems = 0
    for cid in table_ids:
        if cid not in node_ids:
            print(f"warning: table id {cid!r} not found in the mermaid diagram", file=sys.stderr)
            problems += 1
    for nid in sorted(node_ids - set(table_ids)):
        print(f"warning: diagram node {nid!r} has no table row", file=sys.stderr)
        problems += 1
    for c in doc.components:
        for d in c.deps:
            if d not in table_ids:
                print(f"warning: {c.id} depends on unknown id {d!r}", file=sys.stderr)
                problems += 1
    for fl in doc.flows:
        for n, s in enumerate(fl.steps, 1):
            if not any(x in table_ids for x in CODE_RE.findall(s)):
                print(f"warning: flow {fl.name!r} step {n} names no component id", file=sys.stderr)
                problems += 1

    if doc.details:
        for cid in table_ids:
            if cid not in doc.details:
                print(f"warning: no component details for {cid!r}", file=sys.stderr)
                problems += 1
        for cid in sorted(set(doc.details) - set(table_ids)):
            print(f"warning: component details for unknown id {cid!r}", file=sys.stderr)
            problems += 1

    meta = {
        "branch": branch,
        "head": git(root, "rev-parse", "--short", a.ref),
        "repo": remote.name or os.path.basename(root),
        "title": a.title or f"{remote.name or os.path.basename(root)} Architecture",
        "draft": draft,
    }
    if a.live:
        meta["live"] = git(root, "rev-parse", "--short", a.live)
        meta["live_behind"] = int(git(root, "rev-list", "--count", f"{a.live}..{a.ref}") or 0)

    page = build_html(doc, linker, edges, meta)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(page)
    print(
        f"wrote {a.out}: {len(doc.components)} components, {len(edges)} edges, {len(doc.flows)} flows, "
        f"{problems} warnings"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
