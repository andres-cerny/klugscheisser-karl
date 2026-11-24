#!/usr/bin/env python3
"""Page checker for the /ui-loop (docs/ui-loop.md): visits every non-chat page of project `eishockey` on the
preview instance at phone and desktop size, finds objective UI problems, tests forgiving search, writes a JSON
report + full-page screenshots and prints one summary line. Score = number of problems (lower is better);
it is a probe for bugs, not the goal.

    uv run --no-project --with playwright==1.63.0 python scripts/ui_loop/check_pages.py \
        --out data/ui-loop-runs/<stamp>/round-0 [--seed 0] [--base http://127.0.0.1:8010]

Read-only towards the app: GET requests only (the browser aborts every POST except the login), never chat,
refresh, "Alles neu laden", fun-fact generation or the TTS audio. Probe pages and search probes are picked from
the preview database (`--seed` rotates them so the loop does not overfit one page or query).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import random
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from urllib.parse import quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preview

HERE = Path(__file__).resolve().parent
VIEWPORTS = {"phone": (390, 844), "desktop": (1440, 900)}
SKIP_PATH = re.compile(
    r"^/(p/[^/]+/)?(chat|chats|frage|aktualisieren|alles-neu|logout|login|vorbereitung|bewertung/)"
    r"|/aussprache\.wav|/funfacts|/tier$|/hinzufuegen/\d+/(ja|nein|praezisieren)"
)
SLOW_MS = 3000
TAP_MIN = 32


# --- probes from the preview database (read only) ------------------------------------------------


def q(sql: str) -> list[list[str]]:
    r = subprocess.run(
        ["psql", "-h", preview.SOCKET, "-d", preview.PREVIEW_DB, "-AtF", "\t", "-c", sql],
        capture_output=True,
        text=True,
    )
    if r.returncode:
        raise SystemExit(f"psql: {r.stderr.strip()}")
    return [ln.split("\t") for ln in r.stdout.splitlines() if ln]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s).lower()).strip()


def typo(name: str, rnd: random.Random) -> str:
    """One realistic typo in the longest word: drop, double or swap a letter (never the first one)."""
    words = name.split()
    i = max(range(len(words)), key=lambda k: len(words[k]))
    w = words[i]
    if len(w) < 5:
        return name
    j = rnd.randrange(1, len(w) - 2)
    kind = rnd.choice(["drop", "swap", "double"])
    if kind == "drop":
        w = w[:j] + w[j + 1 :]
    elif kind == "swap":
        w = w[:j] + w[j + 1] + w[j] + w[j + 2 :]
    else:
        w = w[: j + 1] + w[j] + w[j + 1 :]
    words[i] = w
    return " ".join(words)


def pick_probes(seed: int) -> dict:
    rnd = random.Random(seed)
    people = q(
        "select a.id, a.full_name, a.tier, count(ia.item_id) from athletes a "
        "left join item_athletes ia on ia.athlete_id = a.id group by a.id order by count(ia.item_id) desc"
    )
    people = [(int(i), n, int(t), int(c)) for i, n, t, c in people]
    tier1 = [p for p in people if p[2] == 1][:15] or people[:15]
    chosen = rnd.sample(tier1, min(3, len(tier1)))
    little = [p for p in people if 0 < p[3] <= 3] or [p for p in people if p[3] == 0]
    if little:
        chosen.append(rnd.choice(little))
    empty = [p for p in people if p[3] == 0]  # round 3: a person with no stored item at all (empty states)
    if empty:
        chosen.append(rnd.choice(empty))
    longest = sorted(people, key=lambda p: -len(p[1]))[:5]
    if longest:
        chosen.append(rnd.choice(longest))
    seen, persons = set(), []
    for p in chosen:
        if p[0] not in seen:
            seen.add(p[0])
            persons.append(p)
    teams = q(
        "select t.id, t.name, t.kind from teams t where exists (select 1 from event_entries e where e.team_id = t.id) "
        "order by (t.key = 'sui-women') desc, t.id"
    )
    team_ids = [int(teams[0][0])] if teams else []
    others = [int(t[0]) for t in teams[1:]]
    if others:
        team_ids.append(rnd.choice(others))
    events = q("select key from events where kind <> 'game' order by starts_at desc nulls last limit 8")
    event_keys = [events[0][0]] if events else []
    if len(events) > 1:
        event_keys.append(rnd.choice(events[1:])[0])
    adds = q("select id from add_requests order by id desc limit 1")
    # search probes generated from real names (rotate with the seed) + the fixed cases file
    search = []
    for _pid, name, _, _ in persons[:3]:
        search.append({"q": typo(name, rnd), "expect": name, "why": "Tippfehler"})
    with_accent = [p for p in people[:200] if strip_accents(p[1]) != p[1]]
    if with_accent:
        p = rnd.choice(with_accent)
        search.append({"q": strip_accents(p[1]), "expect": p[1], "why": "ohne Akzente/Umlaute"})
    for _pid, name, _, _ in rnd.sample(tier1, min(2, len(tier1))):
        last = name.split()[-1]
        search.append({"q": last[: max(4, len(last) - 2)], "expect": name, "why": "Teil des Nachnamens"})
    p = rnd.choice(tier1) if tier1 else None
    if p and len(p[1].split()) >= 2:
        first, *rest = p[1].split()
        search.append({"q": f"{' '.join(rest)} {first}".lower(), "expect": p[1], "why": "Reihenfolge, klein"})
    return {
        "seed": seed,
        "persons": [{"id": p[0], "name": p[1], "tier": p[2], "items": p[3]} for p in persons],
        "teams": team_ids,
        "events": event_keys,
        "add_request": int(adds[0][0]) if adds else None,
        "search": search,
    }


def page_list(probes: dict) -> list[tuple[str, str]]:
    """(name, path) — all under /p/eishockey so the project is fixed."""
    P = "/p/eishockey"
    first = probes["persons"][0]["name"] if probes["persons"] else "Stalder"
    pages = [
        ("home", f"{P}/"),
        ("suche", f"{P}/suche?q={quote(first.split()[-1])}"),
        ("suche-leer", f"{P}/suche?q={quote('xqzw')}"),
        ("quellen", f"{P}/quellen"),
        ("brief", f"{P}/brief?a=SUI&b=CAN"),
        ("bewertungen", f"{P}/bewertungen"),
        ("projekt", f"{P}/projekt"),
    ]
    for p in probes["persons"]:
        pages.append((f"person-{p['id']}", f"{P}/person/{p['id']}"))
    if probes["persons"]:
        pid = probes["persons"][0]["id"]
        pages.append((f"posts-{pid}", f"{P}/person/{pid}/posts?offset=0"))
    for t in probes["teams"]:
        pages.append((f"team-{t}", f"{P}/team/{t}"))
    for k in probes["events"]:
        pages.append((f"event-{k}", f"{P}/event/{quote(k)}"))
    if probes["add_request"]:
        pages.append((f"hinzufuegen-{probes['add_request']}", f"{P}/hinzufuegen/{probes['add_request']}"))
    # round 3: the chat tab of a person (phone view), a missing person and an unknown address (both 404 on purpose)
    if probes["persons"]:
        pages.append(
            (f"person-{probes['persons'][0]['id']}-chat", f"{P}/person/{probes['persons'][0]['id']}?tab=chat")
        )
    pages.append(("person-fehlt", f"{P}/person/99999999"))
    pages.append(("adresse-fehlt", f"{P}/gibt-es-nicht"))
    return pages


EXPECTED_STATUS = {"person-fehlt": 404, "adresse-fehlt": 404}
LOGGED_OUT = [("login", "/login")]  # opened in a fresh browser context without the session


# --- in-page checks (one JS function, returns findings) -------------------------------------------

CHECK_JS = r"""
({phone, tapMin}) => {
  const out = {overflow: [], overlap: [], clipped: [], tap: [], images: [], entities: [], scroll: []};
  const vw = document.documentElement.clientWidth;
  const visible = (el) => {
    // checkVisibility also hides content of closed <details>, hidden="until-found" and content-visibility
    if (el.checkVisibility && !el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})) return false;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.getClientRects().length > 0;
  };
  const sel = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) s += '.' + cls.join('.');
    const p = el.parentElement;
    if (p && p !== document.body) {
      let ps = p.tagName.toLowerCase();
      const pc = (p.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean)[0];
      if (pc) ps += '.' + pc;
      s = ps + ' > ' + s;
    }
    return s;
  };
  const txt = (el) => (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 60);
  // visible part of an element after clipping by scroll/hidden ancestors
  const clipCache = new Map();
  const clipOf = (el) => {
    if (!el || el === document.documentElement) return {l: -1e9, t: -1e9, r: 1e9, b: 1e9};
    if (clipCache.has(el)) return clipCache.get(el);
    let c = clipOf(el.parentElement);
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') {
      const r = el.getBoundingClientRect();
      c = {l: Math.max(c.l, r.left), t: Math.max(c.t, r.top), r: Math.min(c.r, r.right), b: Math.min(c.b, r.bottom)};
    }
    if (cs.position === 'fixed') c = {l: -1e9, t: -1e9, r: 1e9, b: 1e9};
    clipCache.set(el, c);
    return c;
  };
  const clip = (r, c) => ({l: Math.max(r.left, c.l), t: Math.max(r.top, c.t), r: Math.min(r.right, c.r), b: Math.min(r.bottom, c.b)});
  const fixedAnc = (el) => { for (let e = el; e; e = e.parentElement) { const p = getComputedStyle(e).position; if (p === 'fixed' || p === 'sticky') return true; } return false; };

  // 1. horizontal overflow of the page
  if (document.documentElement.scrollWidth > vw + 1) {
    const wide = [];
    for (const el of document.body.querySelectorAll('*')) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.right > vw + 1 && clipOf(el.parentElement).r > vw + 1) wide.push({sel: sel(el), right: Math.round(r.right)});
    }
    // keep the outermost offenders only
    out.overflow.push({scrollWidth: document.documentElement.scrollWidth, clientWidth: vw, elements: wide.slice(0, 5)});
  }

  // 2. overlapping text lines / controls (different elements, neither inside the other)
  const boxes = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = walker.nextNode())) {
    if (!n.textContent.trim()) continue;
    const el = n.parentElement;
    if (!el || !visible(el) || ['SCRIPT', 'STYLE', 'NOSCRIPT', 'OPTION'].includes(el.tagName) || fixedAnc(el)) continue;
    const range = document.createRange();
    range.selectNodeContents(n);
    const c = clipOf(el);
    for (const r of range.getClientRects()) {
      const b = clip(r, c);
      if (b.r - b.l > 2 && b.b - b.t > 2) boxes.push({el, ...b});
    }
  }
  for (const el of document.querySelectorAll('a, button, input:not([type=hidden]), select, textarea, img, svg, [role=button]')) {
    if (!visible(el) || fixedAnc(el)) continue;
    if (el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    // per line fragment: a link that wraps has one box per line, not one big rectangle
    const c = clipOf(el.parentElement);
    for (const r of el.getClientRects()) {
      const b = clip(r, c);
      if (b.r - b.l > 2 && b.b - b.t > 2) boxes.push({el, ...b, ctl: true});
    }
  }
  boxes.sort((a, b) => a.t - b.t);
  const seenPair = new Set();
  for (let i = 0; i < boxes.length; i++) {
    const a = boxes[i];
    for (let j = i + 1; j < boxes.length && boxes[j].t < a.b; j++) {
      const b = boxes[j];
      if (a.el === b.el || a.el.contains(b.el) || b.el.contains(a.el)) continue;
      // decorative icon placed inside a text field (search glass) is intended
      const isField = (e) => ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.tagName);
      const isIcon = (e) => e.tagName.toLowerCase() === 'svg' || (e.tagName === 'IMG' && !txt(e));
      if ((isField(a.el) && isIcon(b.el)) || (isField(b.el) && isIcon(a.el))) continue;
      const w = Math.min(a.r, b.r) - Math.max(a.l, b.l), h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
      if (w <= 2 || h <= 2) continue;
      const small = Math.min((a.r - a.l) * (a.b - a.t), (b.r - b.l) * (b.b - b.t));
      if (w * h < 0.2 * small) continue;  // touching line boxes are fine
      const key = sel(a.el) + ' | ' + sel(b.el);
      if (seenPair.has(key)) continue;
      seenPair.add(key);
      out.overlap.push({a: sel(a.el), at: txt(a.el), b: sel(b.el), bt: txt(b.el), area: Math.round(w * h)});
    }
  }

  // 3. clipped text (overflow hidden, no deliberate ellipsis / line clamp)
  for (const el of document.body.querySelectorAll('*')) {
    if (!visible(el) || !el.innerText || !el.innerText.trim()) continue;
    const cs = getComputedStyle(el);
    const hidX = cs.overflowX === 'hidden' || cs.overflowX === 'clip';
    const hidY = cs.overflowY === 'hidden' || cs.overflowY === 'clip';
    if (!hidX && !hidY) continue;
    if (cs.textOverflow === 'ellipsis' || cs.webkitLineClamp !== 'none' && cs.webkitLineClamp) continue;
    if (el.clientWidth < 4 || el.clientHeight < 4) continue;  // collapsed on purpose
    const x = hidX && el.scrollWidth > el.clientWidth + 2;
    const y = hidY && el.scrollHeight > el.clientHeight + 4;
    if (x || y) out.clipped.push({sel: sel(el), text: txt(el), dir: x ? 'x' : 'y', over: x ? el.scrollWidth - el.clientWidth : el.scrollHeight - el.clientHeight});
  }

  // 4. tap targets on the phone (inline links inside running text are exempt, like WCAG 2.5.8)
  if (phone) {
    for (const el of document.querySelectorAll('a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button]')) {
      if (!visible(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width >= tapMin && r.height >= tapMin) continue;
      if (el.tagName === 'A') {
        const blk = el.closest('p, li, td, dd, blockquote, figcaption, .small');
        if (blk && blk.innerText.trim().length > el.innerText.trim().length + 15) continue;
      }
      // "stretched link": an absolutely positioned ::before/::after covers a bigger box (e.g. the whole row)
      let stretched = false;
      for (const pseudo of ['::after', '::before']) {
        const ps = getComputedStyle(el, pseudo);
        if (!ps.content || ps.content === 'none' || ps.position !== 'absolute') continue;
        let cb = el;  // containing block: the nearest positioned box (the link itself if positioned)
        while (cb && getComputedStyle(cb).position === 'static') cb = cb.parentElement;
        if (cb) { const cr = cb.getBoundingClientRect(); if (cr.width >= tapMin && cr.height >= tapMin) stretched = true; }
      }
      if (stretched) continue;
      // a bigger clickable parent counts (e.g. icon inside a row link)
      const host = el.parentElement && el.parentElement.closest('a[href], button, label, summary');
      if (host && host !== el) { const hr = host.getBoundingClientRect(); if (hr.width >= tapMin && hr.height >= tapMin) continue; }
      out.tap.push({sel: sel(el), text: txt(el), w: Math.round(r.width), h: Math.round(r.height)});
    }
  }

  // 4b. raw HTML entities shown as text ("St.&nbsp;Cloud"): double-escaped data
  { const m = (document.body.innerText || '').match(/.{0,30}&(nbsp|amp|quot|lt|gt|apos|#\d+|#x[0-9a-f]+);.{0,30}/i);
    if (m) out.entities.push({text: m[0]}); }

  // 4c. sideways scroll boxes with hidden content need a visible hint (fade mask / shadow), else the cut-off
  //     columns or chips look like the end of the content
  for (const el of document.body.querySelectorAll('*')) {
    const cs = getComputedStyle(el);
    if (cs.overflowX !== 'auto' && cs.overflowX !== 'scroll') continue;
    if (!visible(el) || el.scrollWidth <= el.clientWidth + 8 || !(el.innerText || '').trim()) continue;
    const mask = cs.maskImage || cs.webkitMaskImage || 'none';
    if (mask === 'none') out.scroll.push({sel: sel(el), text: txt(el), hidden: el.scrollWidth - el.clientWidth});
  }

  // 5. broken images
  for (const img of document.images) {
    if (img.complete && img.getAttribute('src') && img.naturalWidth === 0) out.images.push({src: img.currentSrc || img.src, sel: sel(img)});
  }
  return out;
}
"""


# --- ignore list ------------------------------------------------------------------------------------


def load_ignore(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [r for r in json.loads(path.read_text(encoding="utf-8")) if isinstance(r, dict)]


def ignored(p: dict, rules: list[dict]) -> str | None:
    detail = json.dumps(p.get("detail", ""), ensure_ascii=False)
    for r in rules:
        if r.get("type") and r["type"] != p["type"]:
            continue
        if r.get("page") and not re.search(r["page"], p.get("page", "")):
            continue
        if r.get("viewport") and r["viewport"] != p.get("viewport"):
            continue
        if r.get("match") and not re.search(r["match"], detail):
            continue
        return r.get("why", "ignore")
    return None


# --- main ---------------------------------------------------------------------------------------------


def login(context, base: str, password: str) -> None:
    page = context.new_page()
    page.goto(f"{base}/login", wait_until="domcontentloaded")
    form = page.locator("form[action='/login']")
    form.locator("input[name=password]").fill(password)
    with page.expect_navigation():
        form.locator("button[type=submit]").click()
    if "/login" in urlparse(page.url).path:
        raise SystemExit("Anmeldung fehlgeschlagen (WEB_PASSWORD falsch?)")
    page.close()


def launch(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        return pw.chromium.launch(executable_path="/usr/bin/chromium")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=f"http://{preview.HOST}:{preview.PORT}")
    ap.add_argument("--out", required=True, help="Ordner für report.json + Screenshots")
    ap.add_argument("--seed", type=int, default=0, help="wählt andere Personen/Suchproben (pro Runde ändern)")
    ap.add_argument("--ignore", default=str(HERE / "ignore.json"))
    ap.add_argument("--search-cases", default=str(HERE / "search_cases.json"))
    ap.add_argument("--viewports", default="phone,desktop")
    ap.add_argument("--max-links", type=int, default=400)
    ap.add_argument("--no-screenshots", action="store_true")
    args = ap.parse_args(argv)

    from playwright.sync_api import sync_playwright

    out = Path(args.out).resolve()
    (out / "shots").mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    origin = urlparse(base).netloc
    probes = pick_probes(args.seed)
    fixed = (
        json.loads(Path(args.search_cases).read_text(encoding="utf-8"))
        if Path(args.search_cases).is_file()
        else []
    )
    probes["search"] = fixed + probes["search"]
    pages = page_list(probes)
    rules = load_ignore(Path(args.ignore))
    problems: list[dict] = []
    page_info: list[dict] = []
    links: dict[str, str] = {}

    def add(type_: str, page: str, viewport: str | None, detail) -> None:
        problems.append({"type": type_, "page": page, "viewport": viewport, "detail": detail})

    password = preview.web_password()
    with sync_playwright() as pw:
        browser = launch(pw)
        for vp in args.viewports.split(","):
            w, h = VIEWPORTS[vp]
            ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=1,
                is_mobile=vp == "phone",
                has_touch=vp == "phone",
                locale="de-CH",
            )

            def guard(route):  # read-only: no POST except the login, nothing that starts work
                req = route.request
                u = urlparse(req.url)
                if u.netloc == origin and (
                    (req.method != "GET" and u.path != "/login")
                    or (SKIP_PATH.search(u.path) and u.path != "/login")
                ):
                    return route.abort("blockedbyclient")
                return route.continue_()

            ctx.route("**/*", guard)
            out_ctx = browser.new_context(
                viewport={"width": w, "height": h},
                device_scale_factor=1,
                is_mobile=vp == "phone",
                has_touch=vp == "phone",
                locale="de-CH",
            )
            out_ctx.route("**/*", guard)
            login(ctx, base, password)
            for name, path in pages + [(n, p_) for n, p_ in LOGGED_OUT]:
                logged_out = (name, path) in LOGGED_OUT
                page = (out_ctx if logged_out else ctx).new_page()
                errors: list[str] = []
                failed: list[str] = []
                bad: list[str] = []
                page.on("console", lambda m, e=errors: m.type == "error" and e.append(m.text[:300]))
                page.on("pageerror", lambda ex, e=errors: e.append(f"pageerror: {str(ex)[:300]}"))
                page.on(
                    "requestfailed",
                    lambda r, f=failed: (
                        (r.failure or "") != "net::ERR_BLOCKED_BY_CLIENT"
                        and f.append(f"{r.method} {r.url[:200]} ({r.failure})")
                    ),
                )
                page.on(
                    "response",
                    lambda r, b=bad: (
                        r.status >= 400
                        and urlparse(r.url).netloc == origin
                        and b.append(f"{r.status} {r.url[:200]}")
                    ),
                )
                t0 = time.monotonic()
                try:
                    resp = page.goto(base + path, wait_until="load", timeout=30000)
                    status = resp.status if resp else 0
                    with contextlib.suppress(Exception):  # long-polling htmx parts never go idle
                        page.wait_for_load_state("networkidle", timeout=8000)
                except Exception as exc:
                    add("load_error", name, vp, str(exc)[:300])
                    page.close()
                    continue
                load_ms = int((time.monotonic() - t0) * 1000)
                # scroll once through the page so lazy content and images load
                page.evaluate(
                    "async () => { for (let y = 0; y < document.body.scrollHeight; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 40)); } window.scrollTo(0, 0); }"
                )
                page.wait_for_timeout(300)
                if status != EXPECTED_STATUS.get(name, 200):
                    add("http_status", name, vp, status)
                if load_ms > SLOW_MS:
                    add("slow_page", name, vp, f"{load_ms} ms")
                for e in errors:
                    if name in EXPECTED_STATUS and "status of 404" in e:
                        continue  # the browser logs the page's own (intended) 404
                    add("console_error", name, vp, e)
                for f in failed:
                    add("failed_request", name, vp, f)
                for b in bad:
                    if not b.split(" ", 1)[1].startswith(base + path):
                        add("failed_request", name, vp, b)
                found = page.evaluate(CHECK_JS, {"phone": vp == "phone", "tapMin": TAP_MIN})
                for o in found["overflow"]:
                    add("h_overflow", name, vp, o)
                for o in found["overlap"]:
                    add("overlap", name, vp, o)
                for o in found["clipped"]:
                    add("text_clipped", name, vp, o)
                seen_tap = set()
                for o in found["tap"]:
                    k = (o["sel"], o["text"])
                    if k not in seen_tap:
                        seen_tap.add(k)
                        add("tap_target", name, vp, o)
                for o in found["images"]:
                    add("broken_image", name, vp, o)
                for o in found["entities"]:
                    add("html_entity", name, vp, o)
                for o in found["scroll"]:
                    add("scroll_hint", name, vp, o)
                for href in page.eval_on_selector_all("a[href]", "els => els.map(a => a.href)"):
                    u = urlparse(href)
                    if u.netloc == origin and u.scheme in ("http", "https") and not SKIP_PATH.search(u.path):
                        links.setdefault(href.split("#")[0], name)
                shot = None
                if not args.no_screenshots:
                    shot = out / "shots" / f"{name}-{vp}.png"
                    page.screenshot(path=str(shot), full_page=True)
                page_info.append(
                    {
                        "page": name,
                        "path": path,
                        "viewport": vp,
                        "status": status,
                        "load_ms": load_ms,
                        "screenshot": str(shot) if shot else None,
                    }
                )
                page.close()

            out_ctx.close()
            if vp == args.viewports.split(",")[0]:
                # links (same origin, GET only) and search probes, once
                for href, src in list(links.items())[: args.max_links]:
                    try:
                        r = ctx.request.get(href, max_redirects=5, timeout=20000)
                        if r.status >= 400:
                            add("broken_link", src, None, f"{r.status} {href}")
                    except Exception as exc:
                        add("broken_link", src, None, f"{href}: {str(exc)[:120]}")
                page = ctx.new_page()
                for case in probes["search"]:
                    page.goto(f"{base}/p/eishockey/suche?q={quote(case['q'])}", wait_until="load")
                    hits = page.eval_on_selector_all(
                        ".suggest-list a",
                        "els => els.map(a => { const nm = a.querySelector('.nm');"
                        " return ((nm && nm.firstChild ? nm.firstChild.textContent : a.textContent) || '').trim(); })",
                    )
                    expected = case["expect"] if isinstance(case["expect"], list) else [case["expect"]]
                    top = [norm(h) for h in hits[:5]]
                    if not any(norm(x) in h for x in expected for h in top):
                        add(
                            "search_miss",
                            "suche",
                            None,
                            {
                                "q": case["q"],
                                "expect": case["expect"],
                                "why": case.get("why", ""),
                                "got": hits[:5],
                            },
                        )
                page.close()
            ctx.close()
        browser.close()

    counted, skipped = [], []
    for p in problems:
        why = ignored(p, rules)
        (skipped if why else counted).append({**p, **({"ignored": why} if why else {})})
    by_type = Counter(p["type"] for p in counted)
    report = {
        "base": base,
        "seed": args.seed,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "score": len(counted),
        "by_type": dict(by_type.most_common()),
        "probes": probes,
        "pages": page_info,
        "problems": counted,
        "ignored": skipped,
        "links_checked": min(len(links), args.max_links),
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    line = (
        f"PROBLEME {len(counted)}"
        + (" = " + ", ".join(f"{k} {v}" for k, v in by_type.most_common()) if counted else "")
        + f" (ignoriert {len(skipped)}; {len(page_info)} Seitenansichten; Bericht {out / 'report.json'})"
    )
    (out / "summary.txt").write_text(line + "\n", encoding="utf-8")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
