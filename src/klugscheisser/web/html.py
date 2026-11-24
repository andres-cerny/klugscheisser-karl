"""Server-rendered HTML in plain Python (no template engine dependency). Everything user/data supplied is escaped.

v9 (design "Variant C", docs/design): Barlow / Barlow Condensed, ink + one cobalt accent, amber only for
Vorsicht; a small set of inline stroke icons instead of emoji labels; one responsive template for phone and
desktop (phone: Info | Chat switch; desktop: chat always next to the page).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape, unescape
from typing import Any
from urllib.parse import urlparse

HTMX = "https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Barlow:ital,wght@0,400;0,500;0,600;0,700;1,400"
    "&family=Barlow+Condensed:wght@600;700&display=swap"
)
APP_NAME = "Klugscheisser Karl"
OLD_AFTER = timedelta(days=7)  # data older than this is shown in amber in the "Stand" line


def e(value: Any) -> str:
    """Escape for HTML. Scraped names sometimes carry entities as text ("St.&nbsp;Cloud"): those are decoded
    first, so the page shows "St. Cloud" instead of the raw entity (and nothing is ever double-escaped)."""
    text = "" if value is None else str(value)
    if "&" in text and ";" in text:
        text = unescape(text).replace("\xa0", " ")
    return escape(text, quote=True)


def safe_url(url: str | None) -> str:
    """Only http(s) links are rendered as links (data comes from scraped sources)."""
    if url and urlparse(url).scheme in ("http", "https"):
        return e(url)
    return "#"


# --- icons ---------------------------------------------------------------------------------------------

ICONS = {
    "search": '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    "more": '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    "ext": '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    "down": '<path d="M6 9l6 6 6-6"/>',
    "right": '<path d="M9 6l6 6-6 6"/>',
    "send": '<path d="M4 12l16-8-6 16-3-7z"/>',
    "refresh": '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
    "instagram": '<rect x="3.5" y="3.5" width="17" height="17" rx="5"/><circle cx="12" cy="12" r="4"/>'
    '<path d="M17.2 6.8v.01"/>',
    "medal": '<circle cx="12" cy="15" r="5"/><path d="M8.5 11.5L6 3h4l2 5 2-5h4l-2.5 8.5"/>',
    "trophy": '<path d="M8 4h8v5a4 4 0 0 1-8 0zM8 6H5a3 3 0 0 0 3 4M16 6h3a3 3 0 0 1-3 4M12 13v4M9 20h6"/>',
    "alert": '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18v.5"/>',
    "pin": '<path d="M12 16v6M8 3h8l-1 6 3 4H6l3-4z"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/>',
    "expand": '<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
    "shrink": '<path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 4-6 8-6s8 2 8 6"/>',
    "mic": '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/>',
    "speaker": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 9a4 4 0 0 1 0 6M19 6.5a8 8 0 0 1 0 11"/>',
    "slow": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 12h4"/>',
    "copy": '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
    "check": '<path d="M5 12l5 5 9-10"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "back": '<path d="M15 6l-6 6 6 6"/>',
    "list": '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
    "book": '<path d="M4 5a2 2 0 0 1 2-2h13v15H6a2 2 0 0 0-2 2zM4 20a2 2 0 0 0 2 2h13v-4"/>',
    "shield": '<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/>',
    "calendar": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "stats": '<path d="M4 20h16M7 16v-5M12 16V6M17 16v-8"/>',
}


def icon(name: str, size: int = 18, label: str = "") -> str:
    """Inline stroke icon (currentColor). Decorative unless `label` is given."""
    a11y = f'role="img" aria-label="{e(label)}"' if label else 'aria-hidden="true"'
    return (
        f'<svg class="i" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" {a11y}>{ICONS[name]}</svg>'
    )


CSS = """
:root{--bg:#fff;--ink:#0D1117;--ink2:#2A3039;--grey:#545C68;--line:#E2E5EA;--line2:#C9CED6;--soft:#F3F4F6;
--chip:#ECEEF2;--chip-fg:#3A414C;--accent:#1B44E0;--accent-hi:#0F2C9E;--accent-soft:#C9D2FF;--on-accent:#fff;
--warn-bg:#FFF4D9;--warn-fg:#6B4300;--warn-line:#EBD39A;--gold-bg:#FBEFC8;--gold-fg:#6B5200;
--silver-bg:#E7EAEE;--silver-fg:#454C56;--bronze-bg:#F3E6D8;--bronze-fg:#7A4A1E;--ok:#1B6B3A;--err:#B3261E;
--shadow:0 10px 30px rgba(13,17,23,.14);--top:61px;--segh:58px}
/* anchor jumps (Springen zu …) land below the sticky header */
html{scroll-padding-top:calc(var(--top) + 12px)}
@media (prefers-color-scheme:dark){:root{--bg:#0F1318;--ink:#E8EAED;--ink2:#D3D7DD;--grey:#A2A9B4;
--line:#262C34;--line2:#3A414C;--soft:#1A1F26;--chip:#232932;--chip-fg:#C9CED6;--accent:#8EA6FF;
--accent-hi:#B5C5FF;--accent-soft:#3A4A8A;--on-accent:#0D1117;--warn-bg:#33270F;--warn-fg:#F2C46B;
--warn-line:#5A4519;--gold-bg:#3A3112;--gold-fg:#F0D68A;--silver-bg:#2B3038;--silver-fg:#D3D7DD;
--bronze-bg:#3A2A1C;--bronze-fg:#E9B98C;--ok:#5CC27E;--err:#FF8A80;--shadow:0 10px 30px rgba(0,0,0,.5)}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:400 16px/1.5 'Barlow',system-ui,sans-serif;
overflow-x:hidden}
a{color:var(--accent)}a:hover{color:var(--accent-hi)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}
[hidden]{display:none!important}
svg.i{flex:none;display:block}
.sr{position:absolute!important;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
main{max-width:1280px;margin:0 auto;padding:16px 16px 72px}main.narrow{max-width:860px}
h1{font:700 28px/1.15 'Barlow',sans-serif;margin:8px 0 6px}
h2{font:700 13px/1.2 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase;margin:28px 0 8px}
h3{font:600 15px/1.3 'Barlow',sans-serif;margin:16px 0 6px}
.meta{color:var(--grey);font-size:15px}.small{font-size:13px;line-height:1.45;color:var(--grey)}
.err{color:var(--err)}form.inline{display:inline}
/* header */
header.top{position:sticky;top:0;z-index:30;background:var(--bg);border-bottom:1px solid var(--line)}
header.top .in{max-width:1280px;margin:0 auto;padding:8px 16px;display:flex;align-items:center;gap:12px 24px}
.logo{display:inline-flex;align-items:center;gap:9px;min-height:44px;min-width:44px;flex:none;
font:700 18px/1 'Barlow',sans-serif;color:var(--ink);text-decoration:none;white-space:nowrap}
.logo:hover{color:var(--ink)}
.logo-k{display:flex;align-items:center;justify-content:center;width:30px;height:30px;border-radius:7px;
background:var(--ink);color:var(--bg);font:700 18px/1 'Barlow Condensed',sans-serif}
/* the two places: Suche | Chat (always visible) */
.mainnav{display:flex;gap:4px;flex:none}
.mainnav a{display:inline-flex;align-items:center;gap:7px;min-height:44px;padding:0 12px;border-radius:8px;
font:600 15px/1 'Barlow',sans-serif;color:var(--ink);text-decoration:none;white-space:nowrap}
.mainnav a:hover{background:var(--soft);color:var(--ink)}
.mainnav a[aria-current=page]{background:var(--soft);box-shadow:inset 0 -2.5px 0 var(--accent)}
.mainnav a[aria-current=page] .i{color:var(--accent)}
header.top .gsearch{display:none}
@media (min-width:900px){header.top .gsearch{display:block}}
.gsearch{position:relative;flex:1 1 320px;max-width:560px;min-width:0;margin:0}
.gsearch .gs-i{position:absolute;left:12px;top:13px;color:var(--grey);pointer-events:none}
.gsearch input{width:100%;height:44px;border:1.5px solid var(--line2);border-radius:8px;padding:0 12px 0 38px;
font:400 15px/1 'Barlow',sans-serif;color:var(--ink);background:var(--soft)}
.gsearch input:focus{outline:2px solid var(--accent);outline-offset:-1px;background:var(--bg)}
.gsearch.big input{height:54px;font-size:17px;padding-left:44px}.gsearch.big .gs-i{top:17px;left:14px}
.suggest{position:absolute;left:0;right:0;top:100%;background:var(--bg);border:1px solid var(--line);
border-radius:10px;margin-top:4px;box-shadow:var(--shadow);overflow:hidden;z-index:40}
.suggest:empty{display:none}
.suggest a,.suggest button{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;text-align:left;
font:500 15px/1.3 'Barlow',sans-serif;padding:8px 14px;text-decoration:none;color:var(--ink);border:0;
border-top:1px solid var(--line);background:none;cursor:pointer}
.suggest>:first-child{border-top:0}.suggest a:hover,.suggest button:hover,.suggest a:focus{background:var(--soft)}
.suggest .nm{flex:1;min-width:0}.suggest .k{color:var(--grey);font-size:13px;font-weight:400}
.suggest .kind{flex:none;width:58px;font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
.suggest .ask-q,.suggest .all{color:var(--accent)}.suggest .all .nm{font-weight:600}.suggest button:focus{background:var(--soft)}
details.more{position:relative;margin-left:auto}
details.more>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:6px;min-height:44px;
padding:0 10px;border-radius:8px;font:600 14px/1 'Barlow',sans-serif;white-space:nowrap}
details.more>summary::-webkit-details-marker{display:none}
details.more>summary:hover,details.more[open]>summary{background:var(--soft)}
.menu{position:absolute;right:0;top:110%;background:var(--bg);border:1px solid var(--line);border-radius:12px;
box-shadow:var(--shadow);min-width:260px;max-width:calc(100vw - 24px);z-index:40;padding:6px 0}
.menu .mh{padding:10px 16px 4px;font:700 12px/1 'Barlow Condensed',sans-serif;letter-spacing:.08em;
text-transform:uppercase;color:var(--grey)}
.menu a,.menu button{display:flex;align-items:center;gap:10px;width:100%;min-height:44px;padding:0 16px;
text-decoration:none;color:var(--ink);font:500 15px/1.2 'Barlow',sans-serif;background:none;border:0;
cursor:pointer;text-align:left}
.menu a:hover,.menu button:hover{background:var(--soft)}.menu a.cur{font-weight:700}
.menu hr{border:0;border-top:1px solid var(--line);margin:6px 0}
@media (max-width:560px){.logo{font-size:15px}.more-l{display:none}header.top .in{gap:6px}
.mainnav a{padding:0 10px}}
@media (max-width:440px){.logo-t{display:none}}
/* inputs and buttons */
input[type=search],input[type=text],input[type=password],textarea,select{font:inherit;font-size:15px;
color:var(--ink);background:var(--bg);border:1.5px solid var(--line2);border-radius:8px}
input[type=text],input[type=password],textarea{width:100%;padding:10px 12px}
select{padding:6px 8px;min-height:36px}
input:focus,textarea:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px}
textarea{min-height:44px;resize:vertical}textarea.briefing{min-height:180px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:40px;padding:0 14px;
border-radius:8px;border:1.5px solid var(--line2);background:var(--bg);color:var(--ink);
font:600 14px/1.2 'Barlow',sans-serif;text-decoration:none;cursor:pointer}
.btn:hover{background:var(--soft);color:var(--ink)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.btn.primary:hover{background:var(--accent-hi);border-color:var(--accent-hi)}
.btn.ghost{border-color:transparent;background:none}.btn.on{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.btn.small{min-height:32px;padding:0 10px;font-size:13px}.btn[disabled]{opacity:.55;cursor:default}
.lnk{all:unset;cursor:pointer;display:inline-flex;align-items:center;gap:5px;font:600 13px/1 'Barlow',sans-serif;
color:var(--ink);min-height:28px;padding:0 6px;border-radius:6px}.lnk:hover{background:var(--soft)}
.lnk:focus-visible{outline:2px solid var(--accent)}
@media (max-width:899px){.btn{min-height:44px}}
.src{display:inline-block;flex:none;max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
vertical-align:middle;font:500 12px/1 'Barlow',sans-serif;color:var(--chip-fg);background:var(--chip);
border-radius:4px;padding:5px 7px;text-decoration:none}
a.src:hover{color:var(--ink)}
.chip{display:inline-block;font:500 12px/1 'Barlow',sans-serif;padding:4px 7px;border-radius:4px;
background:var(--chip);color:var(--chip-fg)}
.tier{display:inline-block;font:500 11px/1 'Barlow',sans-serif;padding:3px 5px;border-radius:4px;
border:1px solid var(--line2);color:var(--grey);white-space:nowrap;vertical-align:middle}
.tier-1{color:var(--ink2)}
.badge-ok{color:var(--ok);font-weight:600}.badge-prov{color:var(--warn-fg);font-weight:600}
.translated{color:var(--grey);margin-top:.2rem}
.indicator{display:none}.htmx-request .indicator,.htmx-request.indicator{display:block}
.progress{margin:12px 0;padding:10px 14px;border-radius:10px;background:var(--soft);font-size:14px}
ul.lines{list-style:none;padding:0;margin:0}ul.lines li{padding:10px 0;border-bottom:1px solid var(--line)}
ul.lines li:last-child{border-bottom:0}
.copy{all:unset;cursor:pointer;display:inline-flex;vertical-align:middle;color:var(--grey);padding:3px;
border-radius:4px;margin-left:2px}.copy:hover{background:var(--soft)}
.say{display:inline-flex;gap:2px;vertical-align:middle;margin-left:6px}
.say button{all:unset;cursor:pointer;display:flex;align-items:center;justify-content:center;width:36px;height:36px;
border-radius:8px;color:var(--grey)}.say button:hover{background:var(--soft);color:var(--ink)}
.say button.busy{opacity:.45}.say button:focus-visible{outline:2px solid var(--accent)}
.aussprache{font-size:13px;color:var(--grey)}.aussprache:empty{display:none}
/* section heads, chips, segmented switch */
.sh{display:flex;align-items:center;gap:8px;padding:4px 0 2px;margin-top:20px}
.sh h2{margin:0;color:var(--ink)}.sh .n{font:500 13px/1 'Barlow',sans-serif;color:var(--grey)}
.chips{display:flex;gap:6px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:4px 0 0}
.pbar{position:sticky;top:calc(var(--top) + var(--segh));z-index:19;background:var(--bg);margin:0 -16px;
padding:0 16px;box-shadow:0 1px 0 var(--line)}.pbar .chips{margin:0}
.chips::-webkit-scrollbar{display:none}
.fchip{all:unset;cursor:pointer;flex:none;min-height:44px;display:flex;align-items:center}
.fchip .in{display:flex;align-items:center;gap:6px;height:34px;padding:0 13px;border-radius:17px;
border:1.5px solid var(--line2);background:var(--bg);color:var(--ink);font:600 14px/1 'Barlow',sans-serif}
.fchip .n{font-weight:500;color:var(--grey);font-variant-numeric:tabular-nums}
.fchip[aria-pressed=true] .in{background:var(--ink);border-color:var(--ink);color:var(--bg)}
.fchip[aria-pressed=true] .n{color:var(--accent-soft)}
.fchip:focus-visible .in{outline:2px solid var(--accent);outline-offset:2px}
.seg-wrap{position:sticky;top:var(--top);z-index:20;background:var(--bg);padding:6px 0;height:var(--segh)}
.seg{display:flex;gap:4px;padding:3px;background:var(--soft);border-radius:11px}
.seg button{all:unset;box-sizing:border-box;cursor:pointer;flex:1 1 0;min-width:0;min-height:40px;display:flex;
align-items:center;justify-content:center;gap:6px;padding:0 8px;border-radius:8px;font:600 15px/1 'Barlow',sans-serif;
color:var(--ink)}.seg button .l{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
dialog .seg-wrap,dialog .seg{height:auto}
.seg button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.seg button:focus-visible{outline:2px solid var(--accent)}
/* "Stand" line + refresh */
.stand{display:flex;align-items:center;gap:8px;font:400 12px/1.4 'Barlow',sans-serif;color:var(--grey)}
.stand .st{flex:1;min-width:0}.stand .old{color:var(--warn-fg);background:var(--warn-bg);border-radius:3px;
padding:0 3px}.stand form{margin:0;flex:none}
.stand .lnk{color:var(--ink)}
#refresh-status:empty{display:none}#refresh-status{font-size:13px}
@media (max-width:899px){.stand .lnk{width:44px;height:44px;justify-content:center;margin:-10px -10px -10px 0;
padding:0}.stand .lnk .lbl{display:none}}
/* person header */
.ph{display:flex;gap:14px;align-items:flex-start;padding:4px 0 14px}
.ph-photo{flex:none;width:64px}.ph-photo .sb-photo{margin:0;max-width:none;width:64px}
.ph-photo img{display:block;width:64px;height:64px;object-fit:cover;object-position:50% 20%;border-radius:8px;
background:var(--chip)}
.ph-photo figcaption{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.ph-photo .sb-wait,.ph-ph,.ph-photo:empty::before{width:64px;height:64px;border-radius:8px;background:var(--chip);
display:flex;align-items:center;justify-content:center;font:700 22px/1 'Barlow Condensed',sans-serif;
letter-spacing:.04em;color:var(--chip-fg)}
.ph-photo:empty::before{content:attr(data-ini)}
.ph-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.ph h1{margin:0;font:700 30px/1 'Barlow Condensed',sans-serif;letter-spacing:.01em;text-transform:uppercase;
display:flex;flex-wrap:wrap;align-items:center;gap:0 4px}
.ph-facts{display:flex;flex-wrap:wrap;align-items:center;gap:2px 16px}
.ph-line{font:600 15px/1.25 'Barlow',sans-serif}
.ig{display:inline-flex;align-items:center;gap:5px;min-height:28px;text-decoration:none;
font:600 14px/1 'Barlow',sans-serif;color:var(--accent)}.ig .v{font-weight:500;color:var(--grey)}
.plinks{display:flex;flex-wrap:wrap;align-items:center;gap:0 16px}
.pl{display:inline-flex;align-items:center;gap:5px;min-height:32px;text-decoration:none;
font:600 14px/1 'Barlow',sans-serif;color:var(--accent)}.pl .v{font-weight:500;color:var(--grey)}
.pl:hover{text-decoration:underline}
.ph-stack{display:flex;flex-direction:column;gap:10px;padding-bottom:12px;border-bottom:1px solid var(--line)}
/* Vorsicht: one calm line, opens into the reports */
details.vline{background:var(--warn-bg);border-radius:8px;overflow:hidden}
.vline>summary{list-style:none;cursor:pointer;min-height:44px;display:flex;align-items:center;gap:8px;
padding:0 12px;color:var(--warn-fg)}
.vline>summary::-webkit-details-marker{display:none}
.vline .vt{flex:none;font:700 13px/1 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase}
.vline .vs{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
font:600 14px/1.2 'Barlow',sans-serif;color:var(--ink)}
.vline .vd{flex:none;font:500 13px/1.2 'Barlow',sans-serif;font-variant-numeric:tabular-nums}
details[open]>summary .chev{transform:rotate(180deg)}
.vrow{display:flex;align-items:center;gap:10px;min-height:44px;padding:6px 12px;border-top:1px solid var(--warn-line);
text-decoration:none;color:var(--ink)}
.vrow:hover{color:var(--ink);background:rgba(107,67,0,.05)}
.vrow .t{flex:1;min-width:0;font:500 14px/1.35 'Barlow',sans-serif}.vrow .t .x{display:block;font-size:13px;
color:var(--grey)}
.vrow .e{color:var(--warn-fg)}
.vnote{padding:8px 12px;border-top:1px solid var(--warn-line);font-size:12px;color:var(--warn-fg)}
@media (max-width:420px){.vline .vd .y{display:none}}
/* page grid: info + chat */
.pg[data-view=chat] .pg-info{display:none}.pg[data-view=info] .pg-chat{display:none}
.pg[data-view=kader] .pg-chat{display:none}
.pg-chat{margin-top:4px}
.info-main{min-width:0}
/* drawer: Steckbrief & Karriere (open as a column on wide screens) */
.drawer{margin:8px 0 4px}
.drawer-btn{all:unset;box-sizing:border-box;cursor:pointer;width:100%;min-height:56px;display:flex;
align-items:center;gap:12px;padding:10px 14px;border:1px solid var(--line2);border-radius:10px}
.drawer-btn .t{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.drawer-btn .t b{font:600 16px/1.2 'Barlow',sans-serif}
.drawer-btn .t span{font:400 13px/1.3 'Barlow',sans-serif;color:var(--grey);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.drawer-btn:focus-visible{outline:2px solid var(--accent)}
.drawer-btn[aria-expanded=true] .chev{transform:rotate(180deg)}
.drawer-body{display:none;padding:6px 2px 8px}.drawer.open .drawer-body{display:block}
.drawer h2{margin:18px 0 6px}.drawer-body>:first-child{margin-top:8px}
.bio{margin:4px 0 0;font-size:14px;color:var(--ink2)}
ul.mini{list-style:none;margin:0;padding:0}
ul.mini li{display:flex;gap:10px;padding:5px 0}
ul.mini .d{flex:none;width:88px;font:500 13px/1.35 'Barlow',sans-serif;font-variant-numeric:tabular-nums;
color:var(--grey)}
ul.mini .v{flex:1;min-width:0;font:400 14px/1.35 'Barlow',sans-serif;color:var(--ink2)}
ul.mini .v small{display:block;font-size:12px;color:var(--grey)}
/* Steckbrief card (web.card) */
.steckbrief{margin:0}
.sb-top{display:block}
.sb-photo{margin:0 0 10px;max-width:160px}.sb-photo img{display:block;width:100%;height:auto;border-radius:8px;
background:var(--chip)}
.sb-photo figcaption{font-size:11px;line-height:1.35;color:var(--grey);margin-top:4px;overflow-wrap:anywhere}
.sb-photo figcaption a{color:inherit}.sb-wait{aspect-ratio:4/5;border-radius:8px;background:var(--chip)}
.sb-facts{margin:0;display:grid;grid-template-columns:1fr;gap:0}
.sb-facts>div{display:grid;grid-template-columns:7.5em minmax(0,1fr);gap:0 10px;padding:5px 0;align-items:baseline}
.sb-facts dt{font:600 12px/1.2 'Barlow',sans-serif;color:var(--grey)}
.sb-facts dd{margin:0;font:400 14px/1.4 'Barlow',sans-serif;color:var(--ink2);overflow-wrap:anywhere}
.steckbrief a.src,.drawer a.src{font-size:11px;padding:3px 5px;margin-left:2px;max-width:110px}
.sb-diff{margin-top:2px}
.sb-intro{margin:12px 0 4px;font:italic 400 14px/1.5 'Barlow',sans-serif;color:var(--ink2)}
.sb-intro a.src{font-style:normal}
.sb-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.sb-career{width:100%;border-collapse:collapse;font-size:14px}
.sb-career thead{display:none}
.sb-career td{padding:5px 10px 5px 0;vertical-align:top;color:var(--ink2);line-height:1.35}
.sb-career tr{display:grid;grid-template-columns:96px minmax(0,1fr);column-gap:10px;padding:6px 0;
border-bottom:1px solid var(--line)}.sb-career tr td{padding:0}
.sb-career td.sb-srcs{grid-column:2;padding-top:3px}.sb-career td.sb-srcs:empty{display:none}
.sb-career .sb-srcs a.src{margin:0 4px 2px 0}
.sb-scroll:not(.all) tr.x{display:none}
.sb-season{width:96px;white-space:nowrap;font:500 13px/1.35 'Barlow',sans-serif;font-variant-numeric:tabular-nums;
color:var(--grey)!important}
.sb-career .lg{display:block;font-size:12px;color:var(--grey)}
.sb-career a{color:var(--ink2)}.sb-career .chip{margin-left:4px}
.sb-more{margin:8px 0 4px;font-size:14px}.sb-more summary{cursor:pointer;color:var(--grey);min-height:36px}
@media (max-width:420px){.sb-photo{max-width:132px}.sb-facts>div{grid-template-columns:6.5em minmax(0,1fr)}}
/* Erfolge */
.erf{display:grid;grid-template-columns:minmax(0,1fr)}
.erf a{display:flex;flex-direction:column;align-items:flex-start;gap:5px;min-height:44px;padding:9px 0;
border-bottom:1px solid var(--line);text-decoration:none;color:var(--ink);min-width:0}
.erf a:hover .t{text-decoration:underline}
.erf .t{width:100%;min-width:0;font:500 15px/1.3 'Barlow',sans-serif}
.erf .r{flex:none;display:flex;align-items:center;gap:6px;color:var(--grey)}.erf .r .src{max-width:160px}
.erf .x,.erf .xp{display:none}.erf.all .x,.erf.all .xp{display:flex}
.medal{flex:none;display:inline-flex;align-items:center;gap:5px}
.ml{font:600 12px/1 'Barlow',sans-serif;border-radius:4px;padding:4px 6px}
.ml.gold{background:var(--gold-bg);color:var(--gold-fg)}.medal.gold{color:var(--gold-fg)}
.ml.silver{background:var(--silver-bg);color:var(--silver-fg)}.medal.silver{color:var(--silver-fg)}
.ml.bronze{background:var(--bronze-bg);color:var(--bronze-fg)}.medal.bronze{color:var(--bronze-fg)}
.kl{flex:none;font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
.more-btn{all:unset;cursor:pointer;display:flex;align-items:center;gap:6px;min-height:44px;
font:600 14px/1 'Barlow',sans-serif;color:var(--ink)}.more-btn:focus-visible{outline:2px solid var(--accent)}
.erf .t .y{display:block;overflow:hidden;text-overflow:ellipsis;font:400 13px/1.3 'Barlow',sans-serif;
color:var(--grey);font-variant-numeric:tabular-nums}
/* v11 Statistik: tabs Klub | Nationalteam | Junioren, Playoffs toggle, wide tables scroll sideways */
.st-form{display:flex;flex-direction:column;gap:2px;margin:6px 0 4px;font:400 15px/1.4 'Barlow',sans-serif;
color:var(--ink2)}.st-form b{font-weight:600;color:var(--ink)}
.st-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px 14px;margin:8px 0 4px}
.st-tabs{display:flex;gap:3px;padding:3px;background:var(--soft);border-radius:10px;max-width:100%;overflow-x:auto}
.st-tabs button{all:unset;box-sizing:border-box;cursor:pointer;min-height:36px;padding:0 12px;display:flex;
align-items:center;border-radius:7px;font:600 14px/1 'Barlow',sans-serif;color:var(--ink);white-space:nowrap}
.st-tabs button[aria-pressed=true]{background:var(--ink);color:var(--bg)}
.st-tabs button:focus-visible{outline:2px solid var(--accent)}
.st-one{display:flex;align-items:center;min-height:36px;padding:0 9px;font:600 14px/1 'Barlow',sans-serif}
.st-po{display:inline-flex;align-items:center;gap:6px;min-height:36px;font:600 14px/1 'Barlow',sans-serif;
cursor:pointer}.st-po input{width:18px;height:18px;margin:0;accent-color:var(--ink)}
.st-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table.st{width:100%;border-collapse:collapse;font:400 14px/1.3 'Barlow',sans-serif;font-variant-numeric:tabular-nums}
.st th{padding:6px;text-align:right;white-space:nowrap;font:600 12px/1.2 'Barlow',sans-serif;color:var(--grey);
border-bottom:1px solid var(--line2)}.st th.l{text-align:left}
.st td{padding:7px 6px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line);color:var(--ink2)}
.st td.s,.st td.tm,.st td.lg{text-align:left}.st td.s{font-weight:500;color:var(--grey)}
.st th:first-child,.st td:first-child{position:sticky;left:0;background:var(--bg);padding-left:0}
.st td.tm{max-width:12em;overflow:hidden;text-overflow:ellipsis}.st td.lg{font-size:13px;color:var(--grey)}
.st tr.cur td{background:var(--soft);font-weight:600;color:var(--ink)}.st tr.cur td.s{color:var(--accent)}
.st tr.x{display:none}.st.all tr.x{display:table-row}
.st-tot td.tm{white-space:normal;min-width:9em}.st-tot td.tm .lg{display:block;font-size:12px;color:var(--grey);
font-weight:400}
.stats h3{margin:14px 0 2px;font:600 14px/1.2 'Barlow',sans-serif;color:var(--ink)}
.st-more>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:6px;min-height:44px;
font:600 14px/1 'Barlow',sans-serif}.st-more>summary::-webkit-details-marker{display:none}
.st-src{margin:6px 0 0;font-size:12px;color:var(--grey)}.st-src a{display:inline-flex;align-items:center;gap:4px;
color:var(--grey)}
@media (max-width:899px){.st-tabs button,.st-po{min-height:44px}}
/* fun facts */
details.ff{border-bottom:1px solid var(--line)}
.ff>summary{list-style:none;display:flex;align-items:center;gap:10px;min-height:48px;padding:6px 0;cursor:pointer;
font:500 15px/1.4 'Barlow',sans-serif;color:var(--ink)}
.ff>summary::-webkit-details-marker{display:none}.ff>summary .t{flex:1;min-width:0}
.ff>summary .chev{color:var(--grey)}
.ff .body{display:flex;flex-direction:column;gap:6px;padding:0 0 12px}
.ff blockquote{margin:0;font:italic 400 14px/1.45 'Barlow',sans-serif;color:var(--grey)}
.ff .de{font-size:13px;color:var(--grey)}
.srcl{display:inline-flex;align-items:center;gap:5px;min-height:32px;text-decoration:none;
font:600 13px/1.2 'Barlow',sans-serif}
.ff-foot{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;padding-top:6px;font-size:13px;color:var(--grey)}
/* timeline */
.tl-m{display:flex;align-items:center;gap:10px;padding:18px 0 6px;font:700 13px/1 'Barlow Condensed',sans-serif;
letter-spacing:.08em;text-transform:uppercase;color:var(--ink)}
.tl-m::after{content:"";flex:1;height:1px;background:var(--line)}
a.tl{display:flex;align-items:flex-start;min-height:56px;padding:10px 0;border-bottom:1px solid var(--line);
text-decoration:none;color:var(--ink)}
a.tl:hover .t{text-decoration:underline}
.tl .d{flex:none;width:52px;padding-top:2px;font:600 13px/1.2 'Barlow',sans-serif;
font-variant-numeric:tabular-nums;color:var(--grey)}
.tl.up .d{color:var(--accent)}
.tl .rail{flex:none;position:relative;width:14px;align-self:stretch;border-left:2px solid var(--line);margin:-10px 0}
.tl .rail::before{content:"";position:absolute;left:-5.5px;top:17px;width:9px;height:9px;border-radius:50%;
background:var(--ink);box-shadow:0 0 0 3px var(--bg)}
.tl.up .rail::before{background:var(--accent)}.tl.warn .rail::before{background:var(--warn-fg)}
.tl .b{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.tl .t{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
font:600 15px/1.3 'Barlow',sans-serif;color:var(--ink);overflow-wrap:anywhere}
.tl .s{display:flex;gap:6px;align-items:baseline;min-width:0}
.tl .k{flex:none;font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
.tl .k.v{color:var(--warn-fg);background:var(--warn-bg);border-radius:3px;padding:2px 4px}
.tl .s .x{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font:400 13px/1.3 'Barlow',sans-serif;
color:var(--grey)}
.tl .r{flex:none;display:flex;align-items:center;gap:6px;padding-left:10px;padding-top:1px;color:var(--grey)}
.tl .r .src{max-width:72px}
.empty{color:var(--grey);font-size:14px;padding:10px 0}
/* bottom "Daten" section (admin things, out of Fabi's main flow) */
details.daten{margin-top:36px;border-top:1px solid var(--line)}
details.daten>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;min-height:48px;
font:700 13px/1 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase}
details.daten>summary::-webkit-details-marker{display:none}
details.daten>summary .n{font:400 13px/1 'Barlow',sans-serif;letter-spacing:0;text-transform:none;color:var(--grey)}
.daten .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
.sources li{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
/* chat (own page and next to a person/team) */
.chat{display:flex;flex-direction:column}
.msgs{display:flex;flex-direction:column;gap:12px;padding:12px 0}
.msg.user{align-self:flex-end;max-width:85%;background:var(--ink);color:var(--bg);border-radius:12px 12px 2px 12px;
padding:9px 12px;font:500 14px/1.4 'Barlow',sans-serif;white-space:pre-wrap;margin-top:6px}
.msg.bot{font:400 15px/1.5 'Barlow',sans-serif;padding-bottom:10px;border-bottom:1px solid var(--line)}
.msg.bot h2{margin:12px 0 4px}
.chat-empty{color:var(--grey);font-size:14px;padding:8px 0}
.chatfoot{position:sticky;bottom:0;z-index:5;background:var(--bg);border-top:1px solid var(--line);
padding:10px 0 12px;display:flex;flex-direction:column;gap:8px}
.nextq .lbl{font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
.nextq .qs{display:flex;flex-wrap:wrap;gap:0 6px}
.nq{all:unset;cursor:pointer;min-height:44px;display:inline-flex;align-items:center}
.nq span{border:1px solid var(--line2);border-radius:15px;padding:6px 11px;font:500 13px/1.2 'Barlow',sans-serif;
color:var(--ink);background:var(--bg)}
.nq:hover span{background:var(--soft)}.nq:focus-visible span{outline:2px solid var(--accent);outline-offset:2px}
.ask{display:flex;gap:8px;align-items:flex-end}.ask textarea::placeholder{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.ask textarea{flex:1;min-width:0;height:44px;min-height:44px;max-height:45vh;overflow-y:auto;resize:none;line-height:1.4}
/* stand-alone chat page (new and existing chat): full window, input pinned at the bottom */
.chatpage .chat{min-height:calc(100dvh - var(--top) - 96px)}.chatpage .msgs{flex:1}
.chat-empty .recent{margin-top:20px}.chat-empty .recent h2{margin-top:0}
.send,.mic{flex:none;width:44px;height:44px;border-radius:8px;display:flex;align-items:center;justify-content:center;
cursor:pointer;padding:0}
.send{border:0;background:var(--accent);color:var(--on-accent)}.send:hover{background:var(--accent-hi)}
.mic{border:1.5px solid var(--line2);background:var(--bg);color:var(--ink)}
.mic.on{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
/* v12 dictation: red pulsing mic while listening, small level bars, DE/EN switch, live (grey) words */
.micbox{flex:none;display:flex;align-items:center;gap:2px}
.mic-lang{all:unset;cursor:pointer;width:36px;height:44px;border-radius:8px;display:flex;align-items:center;
justify-content:center;font:600 12px/1 'Barlow',sans-serif;letter-spacing:.04em;color:var(--grey)}
.mic-lang:hover{background:var(--soft)}.mic-lang:focus-visible{outline:2px solid var(--accent)}
.mic-lvl{visibility:hidden;display:flex;align-items:center;gap:2px;width:11px;height:20px}
.mic-lvl i{display:block;width:3px;height:4px;border-radius:2px;background:var(--err)}
.micbox.wait .mic{border-color:var(--err)}
.micbox.on .mic{background:var(--err);border-color:var(--err);color:#fff;animation:micpulse 1.4s ease-in-out infinite}
.micbox.on .mic-lvl{visibility:visible}
.micbox.on .mic-lvl i{animation:miclvl 1.2s ease-in-out infinite}
.micbox.on .mic-lvl i:nth-child(2){animation-delay:.2s}.micbox.on .mic-lvl i:nth-child(3){animation-delay:.4s}
.micbox.speech .mic{animation-duration:.7s}.micbox.speech .mic-lvl i{animation-name:miclvl2;animation-duration:.5s}
.micbox.flash .mic-lvl i{animation:micflash .3s ease-out}
.micbox.lvl .mic-lvl i{animation:none;height:calc(4px + var(--lvl,0) * 16px)}
@keyframes micpulse{0%,100%{box-shadow:0 0 0 0 rgba(179,38,30,.45)}50%{box-shadow:0 0 0 7px rgba(179,38,30,0)}}
@keyframes miclvl{0%,100%{height:4px}50%{height:9px}}
@keyframes miclvl2{0%,100%{height:5px}50%{height:16px}}
@keyframes micflash{0%{height:18px}100%{height:5px}}
@media (prefers-reduced-motion:reduce){.micbox .mic,.micbox .mic-lvl i{animation:none!important}}
.mic-live{font:400 15px/1.4 'Barlow',sans-serif;color:var(--grey);padding:0 2px;overflow-wrap:anywhere}
.mic-live.note{color:var(--ink2);font-size:13px}
.chatfoot .hint{margin:0}@media (max-width:899px){.chatfoot .hint{display:none}}
.ch-head{display:flex;align-items:center;gap:8px;padding:6px 0 8px;border-bottom:1px solid var(--line)}
.ch-head h2{margin:0;color:var(--ink)}.ch-head .ctx{flex:1;min-width:0;font:500 13px/1.2 'Barlow',sans-serif;
color:var(--grey);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ib{all:unset;cursor:pointer;width:44px;height:44px;border-radius:8px;display:flex;align-items:center;
justify-content:center;color:var(--ink)}.ib:hover{background:var(--soft)}.ib:focus-visible{outline:2px solid var(--accent)}
.ch-big{display:none}
.answer .lines li{line-height:1.5}.answer em{color:var(--grey)}
.answer .vor{background:var(--warn-bg);border-radius:8px;padding:2px 12px;margin:10px 0}
.answer .vor h2{color:var(--warn-fg);margin:10px 0 2px}.answer .vor li{border-color:var(--warn-line)}
.rating{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:8px 0 2px}
.rating .btn{min-height:36px}.rating input{flex:1 1 180px;min-width:0;padding:6px 10px}
.pg-chat .rating input{flex-basis:100%}
/* roster (team / event) */
.eyebrow{font:700 13px/1 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase;color:var(--grey)}
.rh{display:flex;flex-direction:column;gap:6px;padding:4px 0 12px;border-bottom:1px solid var(--line)}
.rh h1{margin:2px 0 0;font:700 24px/1.15 'Barlow',sans-serif}.rh .rm{font:600 14px/1.3 'Barlow',sans-serif}
.rh .acts{display:flex;flex-wrap:wrap;gap:6px}
.prov{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;background:var(--soft);border-radius:10px;
margin:12px 0 4px}
.prov .tag{flex:none;margin-top:1px;font:600 13px/1 'Barlow',sans-serif;background:var(--bg);
border:1px solid var(--line2);border-radius:4px;padding:5px 7px}.prov p{margin:0;font:400 14px/1.4 'Barlow',sans-serif}
.ev-rest{margin-top:28px}.evg{margin:10px 0 0}.evg .gl{display:block;margin:0 0 4px;font:600 12px/1.2 'Barlow',sans-serif;color:var(--grey);text-transform:uppercase;letter-spacing:.04em}
.evt{display:flex;flex-wrap:wrap;gap:6px;list-style:none;margin:0;padding:0}.evt li{display:flex;align-items:center;gap:6px}
.evt a{display:inline-flex;align-items:center;min-height:44px;padding:0 14px;border:1px solid var(--line);border-radius:8px;color:var(--ink);text-decoration:none;font:500 15px/1.2 'Barlow',sans-serif}.evt a:hover{border-color:var(--ink)}
@media (min-width:900px){.evt a{min-height:38px}}
.team-h{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:26px 0 0}
.team-h h2{margin:0;font:700 20px/1.2 'Barlow',sans-serif;letter-spacing:0;text-transform:none}
.team-h h2 a{display:inline-flex;align-items:center;min-height:44px;color:var(--ink);text-decoration:none}.team-h h2 a:hover{text-decoration:underline}
ul.roster{list-style:none;margin:0;padding:0}
.roster li{position:relative;display:grid;grid-template-columns:34px minmax(0,1fr) auto;
grid-template-areas:"no nm cl" ". ak ak";align-items:center;column-gap:12px;min-height:52px;padding:4px;
border-bottom:1px solid var(--line)}
.roster .no{grid-area:no;font:700 20px/1 'Barlow Condensed',sans-serif;font-variant-numeric:tabular-nums}
.roster .no.none{font:600 18px/1 'Barlow',sans-serif;color:var(--grey)}
.roster .nm{grid-area:nm;min-width:0;display:flex;align-items:center;gap:6px}
.roster .nm a{font:600 16px/1.2 'Barlow',sans-serif;color:var(--ink);text-decoration:none;overflow:hidden;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow-wrap:anywhere}
.roster .nm a::after{content:"";position:absolute;inset:0}
.roster .nm a:hover{text-decoration:underline}
.roster .ps{grid-area:ps;display:none;font-size:14px;color:var(--grey)}
.roster .cl{grid-area:cl;max-width:120px;font-size:13px;color:var(--grey);white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.roster .ak{grid-area:ak;min-width:0;display:flex}.roster .ak:empty{display:none}
.roster .ak a{position:relative;z-index:1;display:inline-flex;align-items:center;gap:5px;min-width:0;max-width:100%;
min-height:28px;text-decoration:none;font-size:13px;color:var(--grey)}
.roster .ak a span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.roster .ak a.v{color:var(--warn-fg);background:var(--warn-bg);border-radius:4px;padding:0 6px}
.roster .ak a.lp{display:none}
.roster li.hd{display:none}
/* dialog / proposals / home */
dialog{border:0;border-radius:16px;padding:0;width:min(560px,calc(100vw - 24px));max-height:calc(100vh - 40px);
background:var(--bg);color:var(--ink);box-shadow:0 20px 60px rgba(0,0,0,.35)}
dialog::backdrop{background:rgba(0,0,0,.4)}
dialog .in{padding:18px 20px 20px}dialog h2{margin:0 0 10px;font-size:15px}
dialog label{display:block;font-size:14px;margin:10px 0 4px;color:var(--grey)}
dialog .seg{margin-bottom:6px}
.proposal{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:12px 0;background:var(--bg)}
.proposal .sum{margin:6px 0 8px}.proposal ul{margin:6px 0;padding-left:18px}.proposal ul li{margin:3px 0}
.proposal>.small,.proposal>details{margin-top:6px}.proposal summary{cursor:pointer}
.proposal .yesno{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
@media (max-width:699px){dialog .in{padding:16px 14px 18px}.proposal{padding:10px 12px}}
/* start page: two clearly separate places, Suche and Chat */
.start{display:grid;grid-template-columns:minmax(0,1fr);gap:14px;max-width:980px;margin:12px auto 8px}
.card{border:1px solid var(--line);border-radius:14px;padding:16px 16px 14px;background:var(--bg)}
.card h1,.card .card-h{display:flex;align-items:center;gap:10px;margin:0 0 4px;
font:700 24px/1.15 'Barlow',sans-serif;letter-spacing:0;text-transform:none;color:var(--ink)}
.card h1 .i,.card .card-h .i{color:var(--accent)}
.card .meta{margin:0 0 12px;font-size:15px;line-height:1.45}
.card .gsearch{max-width:none;margin:0}
.card .acts{display:flex;flex-wrap:wrap;gap:4px 8px;margin:8px 0 0}
.card-c{background:var(--soft);border-color:transparent}
.askbox textarea{background:var(--bg)}
.recent li a .nm{display:flex;flex-direction:column;gap:2px}
.recent .nm .t{overflow:hidden;text-overflow:ellipsis;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.recent .nm .sub{font:400 13px/1.3 'Barlow',sans-serif;color:var(--grey)}
.recent .allc{margin-top:10px}
@media (min-width:900px){.start{grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:20px;margin-top:20px}
.card{padding:20px 22px 18px}}
/* search result page */
.results .hint{margin:0 0 6px}
.rgroup h2{display:flex;align-items:center;gap:6px;margin:18px 0 2px}
.rgroup h2 .n{font:500 13px/1 'Barlow',sans-serif;color:var(--grey);letter-spacing:0}
.suggest-list .also{margin:-4px 0 0 68px;padding:0 0 8px;border-bottom:1px solid var(--line);font-size:13px;color:var(--grey)}
.suggest-list .also a{display:inline;min-height:0;padding:0;border:0;font:500 13px/1.6 'Barlow',sans-serif;color:var(--accent)}
.suggest-list a:has(+ .also){border-bottom:0}
.rgroup .kind{display:none}.rgroup .also{margin-left:50px}
.askcard{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px;padding:12px 14px;margin:4px 0 8px;
border-radius:10px;background:var(--soft)}.askcard p{margin:0;flex:1 1 220px;font-size:15px}
.noresult{margin:6px 0 0;padding:16px;border:1px dashed var(--line2);border-radius:12px}
.noresult h2{margin:0 0 8px;font:700 18px/1.25 'Barlow',sans-serif;letter-spacing:0;text-transform:none;
overflow-wrap:anywhere}
.dym{margin:0 0 10px;font-size:17px}.dym a{font-weight:600;display:inline-block;padding:6px 0}
.tips{margin:0 0 12px;padding-left:18px;color:var(--grey);font-size:14px}.tips li{margin:3px 0}
.noresult .acts{display:flex;flex-wrap:wrap;gap:8px}
.cols{display:grid;grid-template-columns:minmax(0,1fr);gap:4px 40px;max-width:980px;margin:0 auto}
.recent ul{list-style:none;margin:0;padding:0}
.recent li{display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--line)}
.recent li a{flex:1;min-width:0;display:flex;align-items:center;gap:10px;min-height:48px;text-decoration:none;
color:var(--ink);font:500 15px/1.3 'Barlow',sans-serif}
.recent li a:hover .nm{text-decoration:underline}.recent .nm{min-width:0;overflow:hidden;text-overflow:ellipsis}
.recent li a .i{color:var(--grey)}.recent .ctx{color:var(--grey);font-weight:400;font-size:13px;white-space:nowrap}
.recent .when{flex:none;color:var(--grey);font-size:13px;font-variant-numeric:tabular-nums}
.recent li.small{min-height:48px}
.results{max-width:760px}.results .gsearch{max-width:none;margin:10px 0 14px}
.suggest-list a,.suggest-list button{display:flex;align-items:center;gap:10px;width:100%;min-height:48px;padding:8px 0;
text-align:left;font:500 15px/1.3 'Barlow',sans-serif;color:var(--ink);text-decoration:none;background:none;border:0;
border-bottom:1px solid var(--line);cursor:pointer}
.suggest-list .nm{flex:1;min-width:0}.suggest-list .k{color:var(--grey);font-size:13px;font-weight:400}
.suggest-list .kind{flex:none;width:58px;font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
.suggest-list .ask-q{color:var(--accent)}
.acc{display:flex;color:var(--accent)}
@media (min-width:700px){.cols{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}
/* wide screens */
@media (min-width:900px){
 :root{--top:61px}
 .ph h1{font-size:40px}.ph-photo,.ph-photo .sb-photo{width:72px}
 .ph-photo img,.ph-photo .sb-wait,.ph-ph,.ph-photo:empty::before{width:72px;height:72px}
 .rh h1{font-size:32px}.rh .rm{font-size:16px}
 .seg-wrap{display:none}
 .pg{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:28px;align-items:start}
 .pg.chat-big{grid-template-columns:minmax(0,1fr) minmax(0,1.5fr)}
 .stand{font-size:13px}
 .pg[data-view] .pg-info,.pg[data-view] .pg-chat{display:block}
 .pg-chat{position:sticky;top:calc(var(--top) + 16px);height:calc(100vh - var(--top) - 32px);margin:0;
 border:1px solid var(--line);border-radius:12px;display:flex!important;flex-direction:column;overflow:hidden}
 .pg-chat .ch-head{padding:4px 4px 4px 16px}
 .pg-chat .ib{width:36px;height:36px}.ch-big{display:flex}
 .pg-chat .chat{flex:1;min-height:0}
 .pg-chat .msgs{flex:1;min-height:0;overflow-y:auto;padding:16px}
 .pg-chat .chatfoot{position:static;padding:10px 16px 14px}
 :root{--segh:0px}.pbar{margin:0;padding:4px 0}
 .fchip{min-height:40px}.fchip .in{height:32px;padding:0 10px;gap:5px}.pbar .chips{gap:4px}
 .erf{grid-template-columns:repeat(2,minmax(0,1fr));column-gap:24px}.erf .xp{display:flex}.erf-all:not(.erf-w){display:none}
 .tl .t{display:block;white-space:nowrap;text-overflow:ellipsis}
 .tl .r .src{max-width:110px}
 .roster li{grid-template-columns:44px minmax(0,1.2fr) minmax(0,.8fr) minmax(0,1fr) minmax(0,1.3fr);
 grid-template-areas:"no nm ps cl ak";min-height:48px}
 .roster .ps{display:block}.roster .cl{max-width:none}.roster .ak:empty{display:flex}
 .roster .ak a.lp{display:inline-flex}
 .roster li.hd{display:grid;min-height:0;padding:10px 4px 8px;border-bottom:1.5px solid var(--ink);
 font:600 12px/1 'Barlow',sans-serif;color:var(--grey)}
}
@media (min-width:1280px){.pg:not(.chat-big){grid-template-columns:minmax(0,1fr) 380px}}
@media (min-width:1180px){
 /* ui-loop r3: the Steckbrief is an open section in the main column (fact grid, career as a table), no longer a
    long narrow left column */
 .pg:not(.chat-big) .drawer{margin:4px 0 0}
 .pg:not(.chat-big) .drawer-btn{display:none}
 .pg:not(.chat-big) .drawer-body{display:block;padding:0}
 .pg:not(.chat-big) .drawer-body>.steckbrief>h2:first-child{margin-top:24px}
 .pg:not(.chat-big) .sb-facts{grid-template-columns:repeat(3,minmax(0,1fr));gap:0 24px}
 .pg:not(.chat-big) .sb-facts>div{grid-template-columns:1fr;align-content:start;gap:2px;padding:7px 0;
 border-bottom:1px solid var(--line)}
 .pg:not(.chat-big) .sb-career tr{display:table-row}
 .pg:not(.chat-big) .sb-career td{padding:7px 12px 7px 0;border-bottom:1px solid var(--line)}
 .pg:not(.chat-big) .sb-career td.sb-srcs{padding-top:5px;text-align:right;width:40%}
 .pg:not(.chat-big) .sb-scroll:not(.all) tr.x{display:none}
 .pg:not(.chat-big) ul.mini{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 24px}
}
/* phone: touch targets of at least 44 px (source chips at least 32 px, they sit in dense rows) */
@media (max-width:899px){.say button{width:44px;height:44px}.lnk,select{min-height:44px}.srcl{min-height:44px}
.copy{padding:12px}a.src{line-height:22px;padding-top:5px;padding-bottom:5px}
.proposal summary{padding:12px 0}.proposal .small a{display:inline-block;padding:8px 0}}
/* v12 prosa answer (web chat, AnswerResult.stil == "prosa") */
.answer.prosa .ameta{margin-bottom:6px}
.answer.prosa .ap{margin:6px 0 10px;font:400 16px/1.55 'Barlow',sans-serif;color:var(--ink)}
.answer.prosa .ap .as{margin-right:.2em}
a.qref{font:600 12px/1 'Barlow',sans-serif;text-decoration:none;color:var(--accent);vertical-align:super;
padding:0 1px;margin-left:1px}a.qref:hover{color:var(--accent-hi);text-decoration:underline}
.answer.prosa .atab{overflow-x:auto;-webkit-overflow-scrolling:touch;margin:8px 0 12px;border:1px solid var(--line);
border-radius:8px}
.answer.prosa table{border-collapse:collapse;width:100%;font:400 14px/1.4 'Barlow',sans-serif}
.answer.prosa th{text-align:left;font:600 12px/1.2 'Barlow',sans-serif;color:var(--grey);background:var(--soft);
padding:8px 10px;white-space:nowrap}
.answer.prosa td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top;color:var(--ink2)}
.answer.prosa .qc{width:1%;white-space:nowrap;text-align:right}
.answer.prosa h2.qh{font:600 13px/1.2 'Barlow',sans-serif;color:var(--grey);text-transform:uppercase;
letter-spacing:.04em;margin:14px 0 4px}
.answer.prosa ol.qlist{list-style:none;padding:0;margin:0}
.answer.prosa ol.qlist li{padding:8px 0;border-bottom:1px solid var(--line);font-size:14px;scroll-margin-top:calc(var(--top) + 8px)}
.answer.prosa ol.qlist li:target{background:var(--soft)}
.answer.prosa .qhead{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.answer.prosa .qn{font:600 12px/1 'Barlow',sans-serif;color:var(--accent)}
.answer.prosa .qz{color:var(--ink2);font-style:italic;margin-top:2px}
.answer.prosa .vchip{background:var(--warn-bg);color:var(--warn-fg)}
.answer.prosa details.amore{margin-top:6px}
.answer.prosa details.amore>summary{cursor:pointer;min-height:36px;display:flex;align-items:center;
font:500 13px/1.2 'Barlow',sans-serif;color:var(--grey)}
.rueckfrage{align-self:flex-start;max-width:92%;background:var(--soft);border-radius:12px 12px 12px 2px;
padding:9px 12px;margin:6px 0}
.rueckfrage p{margin:0 0 4px;font:500 15px/1.45 'Barlow',sans-serif;color:var(--ink)}
.rueckfrage .qs{display:flex;flex-wrap:wrap;gap:0 6px}
.answer.prosa .nextq{margin-top:8px}
@media (max-width:899px){.apers a{display:inline-block;padding:8px 0;margin:-8px 0}a.qref{padding:6px 3px}.answer.prosa details.amore>summary{min-height:44px}}
/* ui-loop r2: every sideways scroller (chips, stat tables, tabs, answer tables) fades out where more is hidden */
.xs{--fl:0px;--fr:0px;-webkit-mask-image:linear-gradient(to right,transparent 0,#000 var(--fl),#000 calc(100% - var(--fr)),transparent 100%);
mask-image:linear-gradient(to right,transparent 0,#000 var(--fl),#000 calc(100% - var(--fr)),transparent 100%)}
.xs.xl{--fl:24px}.xs.xr{--fr:40px}
.st-scroll.xs.xl,.atab.xs.xl{--fl:0px}
.st-scroll.xl .st th:first-child,.st-scroll.xl .st td:first-child{box-shadow:6px 0 6px -6px rgba(13,17,23,.35)}
.xs-hint{display:none;margin:4px 0 0;font:500 12px/1.3 'Barlow',sans-serif;color:var(--grey)}
.xs.xr+.xs-hint{display:block}
/* stat tables on the phone: league under the team (one column less) */
.st td.tm .lg2{display:none}
.st td.tm .rs{display:block;font-size:11px;font-weight:400;color:var(--grey)}
@media (max-width:599px){.st .c-lg{display:none}.st td.tm .lg2{display:block;font-size:12px;font-weight:400;
color:var(--grey);overflow:hidden;text-overflow:ellipsis}.st td.tm{max-width:9.5em}}
/* timeline: section head, older months collapsed */
.tlw:not(.open) .tl-old,.tlw.open>.tl-more{display:none}
.tlw.cut:not(.open)~#ig{display:none}
.tl-more{margin-top:4px}
.tl .s .s2{display:none;max-width:110px;padding:2px 5px;font-size:11px;line-height:1.2}
@media (max-width:599px){.tl .r .src{display:none}.tl .s .s2{display:inline-block;align-self:center}}
/* ui-loop r3: login and "not found" pages: one calm card */
main.bare{max-width:460px;padding-top:40px}
.gate{border:1px solid var(--line);border-radius:14px;padding:22px 20px 18px;background:var(--bg)}
.gate h1{margin:0 0 6px}.gate .meta{margin:0 0 16px}
.gate input[type=password]{width:100%;margin:0 0 12px}
.gate .btn{width:100%;justify-content:center;min-height:48px}
.gate .acts{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.gate .acts .btn{width:auto;flex:1 1 140px}
.gate .err{margin:0 0 10px;font-weight:600}
/* ui-loop r3: Spiel-Brief (header like team pages, jump bar, chips, both rosters side by side on wide screens) */
.bhead{padding:4px 0 6px}.bhead h1{margin:4px 0 2px}.bhead .rm{font:500 15px/1.3 'Barlow',sans-serif;color:var(--grey)}
.bhead .small{margin:6px 0 0}
.bjump{margin:6px 0 4px}.bjump a.fchip{text-decoration:none}
.blinks,.bgames,.bplayers{list-style:none;margin:0;padding:0}
.blinks li{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 10px;padding:9px 0;border-bottom:1px solid var(--line)}
.blinks .t{font:600 15px/1.3 'Barlow',sans-serif}.blinks .t a{color:var(--ink)}
.blinks .x2{font-size:14px;color:var(--ink2)}.blinks .srcs{display:flex;gap:4px}
@media (min-width:600px){.blinks .srcs{margin-left:auto}}
.blinks:not(.all) li.x{display:none}
.bgames li{display:flex;align-items:center;gap:10px;min-height:44px;border-bottom:1px solid var(--line)}
.bgames .d{flex:none;width:88px;font:500 13px/1.3 'Barlow',sans-serif;color:var(--grey);font-variant-numeric:tabular-nums}
.bgames .t{flex:1;min-width:0}
a.bi{display:flex;align-items:center;gap:8px;min-height:36px;padding:3px 0;color:var(--ink2);text-decoration:none;
font:400 14px/1.35 'Barlow',sans-serif}
a.bi .t{flex:1;min-width:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
a.bi .k{font:600 12px/1 'Barlow',sans-serif;color:var(--grey);margin-right:6px}a.bi .k.v{color:var(--warn-fg)}
a.bi.warn{background:var(--warn-bg);border-radius:6px;padding:3px 8px;margin:2px 0}
a.bi:hover .t{color:var(--accent)}
.bnews a.bi{border-bottom:1px solid var(--line);min-height:44px}
.bteams{display:grid;grid-template-columns:minmax(0,1fr);gap:0 32px}
.bside .small{margin:2px 0 4px}
.bplayers li.bg{font:700 12px/1 'Barlow Condensed',sans-serif;letter-spacing:.08em;text-transform:uppercase;
color:var(--grey);padding:16px 0 6px;border-bottom:1.5px solid var(--ink)}
.bplayers li.bg .n{font-weight:500;letter-spacing:0}
li.bp{padding:8px 0;border-bottom:1px solid var(--line)}
li.bp .l1{display:flex;align-items:center;gap:6px}
li.bp .nm{font:600 16px/1.3 'Barlow',sans-serif;color:var(--ink)}
li.bp .say button{width:32px;height:32px}
li.bp .l2{font:400 14px/1.6 'Barlow',sans-serif;color:var(--ink2)}
@media (min-width:1000px){.bteams{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (max-width:899px){li.bp .say button{width:44px;height:44px}
/* name = tap target as high as the speaker buttons; less row padding keeps the list as compact */
li.bp{padding:2px 0 6px}li.bp .nm{display:inline-flex;align-items:center;min-height:44px}}
/* ui-loop r3: Quellen as compact cards */
.qlist{list-style:none;margin:6px 0 0;padding:0;display:grid;grid-template-columns:minmax(0,1fr);gap:8px}
.qlist li{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.qlist .qh{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.qlist .qh a{font:600 15px/1.3 'Barlow',sans-serif;color:var(--ink)}
.qon,.qoff{font:600 11px/1 'Barlow',sans-serif;padding:4px 7px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em}
.qon{background:color-mix(in srgb,var(--ok) 14%,transparent);color:var(--ok)}.qoff{background:var(--chip);color:var(--grey)}
.qlist .qt{font-size:14px;color:var(--ink2);margin:2px 0}
@media (min-width:900px){.qlist{grid-template-columns:repeat(2,minmax(0,1fr))}}
/* ui-loop r3: person rows in search results / suggestions: photo or initials, name, fact line, tier badge */
.av{flex:none;width:40px;height:40px;border-radius:50%;overflow:hidden;background:var(--chip);display:flex;
align-items:center;justify-content:center;font:700 14px/1 'Barlow Condensed',sans-serif;letter-spacing:.04em;
color:var(--chip-fg)}
.av img{display:block;width:100%;height:100%;object-fit:cover;object-position:50% 20%}
.suggest .av{width:32px;height:32px;font-size:12px}
a.pr .nm .t{display:block;font-weight:600;color:var(--ink)}a.pr .nm .k{display:block;margin-top:1px}
.av.ic{color:var(--grey);background:var(--soft)}
a.pr .rb{flex:none;font:500 12px/1.2 'Barlow',sans-serif;color:var(--grey);text-align:right;max-width:9em}
.suggest a.pr .rb{display:none}
@media (max-width:599px){a.pr .rb{display:none}}
/* ui-loop r3: "Auf einen Blick" (person page): tiles + the newest entries, every value with its source chip */
.glance{margin:14px 0 4px;padding:12px 14px 6px;border:1px solid var(--line);border-radius:12px;background:var(--soft)}
.glance h2.gl-h{margin:0 0 10px}
.gl-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 16px}
.gl-t{min-width:0;display:flex;flex-direction:column;gap:2px}
.gl-l{display:block;font:600 12px/1.2 'Barlow Condensed',sans-serif;letter-spacing:.06em;text-transform:uppercase;
color:var(--grey)}
.gl-v{font:600 16px/1.25 'Barlow',sans-serif;color:var(--ink);overflow-wrap:anywhere}
.gl-v a{color:var(--ink);text-decoration-color:var(--line2)}
.gl-s{font:400 13px/1.3 'Barlow',sans-serif;color:var(--grey)}
.gl-src{display:flex;flex-wrap:wrap;gap:4px;margin-top:2px}.gl-src .src{background:var(--bg)}
.gl-news{margin-top:12px;padding-top:10px;border-top:1px solid var(--line)}
a.gl-n{display:flex;align-items:center;gap:10px;min-height:44px;padding:4px 0;color:var(--ink);text-decoration:none}
a.gl-n+a.gl-n{border-top:1px solid var(--line)}
a.gl-n .d{flex:none;width:44px;font:500 13px/1.3 'Barlow',sans-serif;color:var(--grey);font-variant-numeric:tabular-nums}
a.gl-n .t{flex:1;min-width:0;font:500 15px/1.3 'Barlow',sans-serif;display:-webkit-box;-webkit-line-clamp:2;
-webkit-box-orient:vertical;overflow:hidden}
a.gl-n .k{font:600 12px/1 'Barlow',sans-serif;color:var(--grey);margin-right:6px}
a.gl-n .k.v{color:var(--warn-fg)}a.gl-n .src{background:var(--bg)}
a.gl-n:hover .t{color:var(--accent)}
@media (min-width:900px){.gl-grid{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}}
@media (max-width:899px){.pg-chat .ch-head .acc,.pg-chat .ch-head h2,.pg-chat .ch-head .ctx{display:none}
.pg-chat .ch-head{justify-content:flex-end;border-bottom:0;padding:0}.pg-chat .ch-head:not(:has(a.ib)){display:none}
.pg-chat .ch-head a.ib{width:auto;gap:6px;padding:0 10px;font:600 14px/1 'Barlow',sans-serif;color:var(--ink)}
.pg-chat .ch-head a.ib .ibl{display:inline}}
.ib .ibl{display:none}
details.daten>summary .dt{flex:none;white-space:nowrap}
details.daten>summary .n{flex:1;min-width:0;line-height:1.3}
"""

JS = """
function copyLine(btn){const t=btn.getAttribute('data-copy');const old=btn.innerHTML;
 (navigator.clipboard?navigator.clipboard.writeText(t):Promise.reject()).then(
 ()=>{btn.innerHTML=ICON_CHECK;setTimeout(()=>btn.innerHTML=old,1200)},()=>{prompt('Kopieren:',t)})}
const STEPS=['Suche Quellen …','Lese Dokumente …','Prüfe Zitate …','Formuliere Antwort …'];
document.addEventListener('htmx:beforeRequest',ev=>{const p=ev.detail.elt.querySelector&&
 ev.detail.elt.closest('.pending')?ev.detail.elt.querySelector('.ptext'):null;if(!p)return;let i=0;
 p.textContent=STEPS[0];clearInterval(window._steps);
 window._steps=setInterval(()=>{i=Math.min(i+1,STEPS.length-1);p.textContent=STEPS[i]},4000)});
document.addEventListener('htmx:afterRequest',()=>{if(window._steps)clearInterval(window._steps)});
/* v7/v12: dictation with the browser's own speech recognition (free, nothing goes through our server);
   hidden where the browser has none. Keyboard dictation on the phone works in any case.
   v12 (Part I): keeps listening until the mic button or ~3 s of silence; recognised words go into the field
   (black, after what was already typed, cursor at the end), words still being recognised show grey in a line
   above it. "Ich höre zu": the button turns red and pulses from `audiostart`, stronger while speech is heard,
   the small bars next to it flash with every recognised word; short vibration on start and stop.
   DE/EN switch next to the mic (de-CH / en-US), remembered in this browser. */
const MIC_SILENCE_MS=3000,MIC_FIRST_MS=8000;  // stop after this much silence (before the first word: longer)
const MIC_ANALYSER=false;  // live level from a second mic stream (Web Audio); off: may break recognition on iPhone
const MIC_LANGS={de:'de-CH',en:'en-US'};
const MIC_ERRORS={'no-speech':'Nichts gehört. Nochmals auf das Mikrofon tippen und sprechen.',
 'not-allowed':'Mikrofon nicht erlaubt. Bitte in den Browser-Einstellungen freigeben.',
 'service-not-allowed':'Diktieren ist in diesem Browser gesperrt. Die Diktiertaste der Tastatur geht trotzdem.',
 'audio-capture':'Kein Mikrofon gefunden.','network':'Diktieren braucht eine Internetverbindung.',
 'language-not-supported':'Diese Sprache kann der Browser nicht diktieren.'};
function micLang(){try{return localStorage.getItem('micLang')==='en'?'en':'de'}catch(e){return 'de'}}
function micLangShow(){const l=micLang();document.querySelectorAll('.mic-lang').forEach(x=>{
 x.textContent=l.toUpperCase();x.title='Diktieren auf '+(l==='en'?'Englisch':'Deutsch')+' (tippen: wechseln)';
 x.setAttribute('aria-label',x.title)})}
function buzz(p){try{if(navigator.vibrate)navigator.vibrate(p)}catch(e){}}
/* Android Chrome repeats earlier words in later final results: keep the longest version once */
function micJoin(parts){const out=[];for(const p of parts){const t=(p||'').trim();if(!t)continue;
 const last=out.length?out[out.length-1]:'';const a=last.toLowerCase(),b=t.toLowerCase();
 if(last&&b.startsWith(a))out[out.length-1]=t;else if(!(last&&a.endsWith(b)))out.push(t)}return out.join(' ')}
/* optional (MIC_ANALYSER): real sound level from a second mic stream; returns a stop function */
function micAnalyser(box){let stream=null,ctx=null,raf=0,dead=false;
 navigator.mediaDevices.getUserMedia({audio:true}).then(st=>{if(dead){st.getTracks().forEach(t=>t.stop());return}
  stream=st;ctx=new (window.AudioContext||window.webkitAudioContext)();const an=ctx.createAnalyser();an.fftSize=256;
  ctx.createMediaStreamSource(st).connect(an);const buf=new Uint8Array(an.fftSize);box.classList.add('lvl');
  const tick=()=>{an.getByteTimeDomainData(buf);let peak=0;for(const v of buf)peak=Math.max(peak,Math.abs(v-128));
   box.style.setProperty('--lvl',Math.min(1,peak/64).toFixed(2));raf=requestAnimationFrame(tick)};tick()})
  .catch(()=>{});
 return()=>{dead=true;cancelAnimationFrame(raf);box.classList.remove('lvl');
  if(stream)stream.getTracks().forEach(t=>t.stop());if(ctx)ctx.close()}}
function addMics(root){const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR)return;
 (root||document).querySelectorAll('textarea[name=frage],textarea[data-mic]').forEach(ta=>{
  if(ta.dataset.micDone)return;ta.dataset.micDone='1';
  const box=document.createElement('span');box.className='micbox';
  box.innerHTML="<button type='button' class='mic-lang'></button>"+
   "<span class='mic-lvl' aria-hidden='true'><i></i><i></i><i></i></span><button type='button' class='mic' title='Diktieren' "+
   "aria-label='Diktieren' aria-pressed='false'>"+ICON_MIC+"</button>";
  ta.insertAdjacentElement('afterend',box);
  const live=document.createElement('div');live.className='mic-live';live.hidden=true;
  live.setAttribute('aria-live','polite');(ta.closest('.ask')||ta).insertAdjacentElement('beforebegin',live);
  const b=box.querySelector('.mic'),lb=box.querySelector('.mic-lang');micLangShow();
  const ph=ta.getAttribute('placeholder')||'';
  let rec=null,base='',finals=[],interim='',timer=0,noteTimer=0,heard=false,started=false,sent=false,err='',
   stopLevel=null;
  const note=(t,cls)=>{clearTimeout(noteTimer);live.textContent=t;live.hidden=!t;live.classList.toggle('note',!!cls);
   if(cls)noteTimer=setTimeout(()=>{live.hidden=true},6000)};
  const arm=ms=>{clearTimeout(timer);timer=setTimeout(()=>{if(rec)try{rec.stop()}catch(e){}},ms)};
  const flash=()=>{box.classList.remove('flash');void box.offsetWidth;box.classList.add('flash');
   setTimeout(()=>box.classList.remove('flash'),300)};
  const show=()=>{if(sent)return;ta.value=base+micJoin(finals);note(interim);
   if(document.activeElement===ta){const n=ta.value.length;ta.setSelectionRange(n,n)}ta.scrollTop=ta.scrollHeight;
   ta.dispatchEvent(new Event('input',{bubbles:true}))};
  const listening=()=>{if(started||!rec)return;started=true;box.classList.add('on');ta.placeholder='Ich höre zu …';
   buzz(30);arm(MIC_FIRST_MS);if(MIC_ANALYSER&&navigator.mediaDevices)stopLevel=micAnalyser(box)};
  const stop=()=>{if(rec)try{rec.stop()}catch(e){}};
  if(ta.form)ta.form.addEventListener('submit',()=>{if(rec){sent=true;stop()}});
  lb.onclick=()=>{try{localStorage.setItem('micLang',micLang()==='en'?'de':'en')}catch(e){}micLangShow();stop()};
  b.onclick=()=>{if(rec){stop();return}
   const r=new SR();rec=r;r.lang=MIC_LANGS[micLang()];r.interimResults=true;r.continuous=true;r.maxAlternatives=1;
   base=ta.value.trim()?ta.value.trimEnd()+' ':'';finals=[];interim='';heard=false;started=false;sent=false;err='';
   box.classList.add('wait');b.setAttribute('aria-pressed','true');b.setAttribute('aria-label','Diktieren beenden');
   note('');
   r.onstart=()=>setTimeout(listening,800);  // browsers without audiostart
   r.onaudiostart=listening;
   r.onspeechstart=()=>{listening();box.classList.add('speech');arm(MIC_FIRST_MS)};  // first words may come late
   r.onspeechend=()=>{box.classList.remove('speech');arm(MIC_SILENCE_MS)};
   r.onresult=ev=>{if(rec!==r)return;heard=true;finals=[];interim='';
    for(let i=0;i<ev.results.length;i++){const x=ev.results[i];if(x.isFinal)finals.push(x[0].transcript);
     else interim+=x[0].transcript}
    interim=interim.trim();show();flash();arm(MIC_SILENCE_MS)};
   r.onerror=ev=>{err=ev.error||''};
   r.onend=()=>{if(rec!==r)return;clearTimeout(timer);rec=null;if(stopLevel){stopLevel();stopLevel=null}
    if(interim){finals.push(interim);interim=''}  // words not finalised when we stopped: keep them
    if(heard)show();box.classList.remove('wait','on','speech','flash');b.setAttribute('aria-pressed','false');
    b.setAttribute('aria-label','Diktieren');ta.placeholder=ph;if(started)buzz([20,40,20]);
    const msg=err==='aborted'||(err==='no-speech'&&heard)?'':MIC_ERRORS[err]||'';
    if(msg)note(msg,true);else note('');if(!sent)ta.focus()};
   try{r.start()}catch(e){rec=null;box.classList.remove('wait');b.setAttribute('aria-pressed','false')}}})}
document.addEventListener('DOMContentLoaded',()=>addMics());
document.addEventListener('htmx:afterSwap',ev=>addMics(ev.detail.target));
/* v6 step 7: name pronunciation (made on first click, then stored; slow = slower playback) */
/* the project prefix of this page (/p/<key>), for URLs built here in the script */
const KP=(location.pathname.match(/^\\/p\\/[^/]+(?=\\/)/)||[''])[0];
function say(btn,slow){const id=btn.closest('.say').dataset.id;if(btn.classList.contains('busy'))return;
 btn.classList.add('busy');
 fetch(KP+'/person/'+id+'/aussprache.wav').then(r=>{
  if(!r.ok){btn.title='Keine Aussprache verfügbar';throw new Error()}
  const rate=slow?0.6:1;return r.blob().then(b=>{
   const a=new Audio(URL.createObjectURL(b));a.playbackRate=rate;return a.play()})})
  .catch(()=>{}).finally(()=>btn.classList.remove('busy'));
 const box=document.getElementById('aussprache-'+id);if(box&&!box.dataset.loaded&&window.htmx){box.dataset.loaded=1;
  htmx.ajax('GET',KP+'/person/'+id+'/aussprache',{target:box,swap:'innerHTML'})}}
function grow(ta){ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight+2,innerHeight*0.45)+'px'}
document.addEventListener('input',ev=>{if(ev.target.matches&&ev.target.matches('.ask textarea'))grow(ev.target)});
addEventListener('resize',()=>document.querySelectorAll('.ask textarea').forEach(t=>{if(t.value)grow(t)}));
document.addEventListener('reset',ev=>{ev.target.querySelectorAll('.ask textarea').forEach(t=>t.style.height='')},true);
/* phone (touch, no hover): Enter is a new line, send with the button; computer: Enter sends */
const TOUCH=matchMedia('(hover:none) and (pointer:coarse)').matches;
function sendOnEnter(ev){if(TOUCH)return;if(ev.key==='Enter'&&!ev.shiftKey&&!ev.isComposing){ev.preventDefault();
 const f=ev.target.form;if(f.requestSubmit)f.requestSubmit();else f.submit()}}
/* search field: names, teams, events. A question is offered to the chat (suggestion row, result page), never sent by itself */
/* suggestions: arrow keys move through the rows, Escape closes them */
document.addEventListener('keydown',ev=>{const t=ev.target;if(!t.closest)return;const form=t.closest('.gsearch');
 if(!form)return;const box=form.querySelector('.suggest');const rows=box?[...box.querySelectorAll(':scope>a,:scope>button')]:[];
 if(ev.key==='Escape'){if(box)box.innerHTML='';form.querySelector('input').focus();return}
 if(ev.key!=='ArrowDown'&&ev.key!=='ArrowUp'||!rows.length)return;ev.preventDefault();
 const i=rows.indexOf(document.activeElement);
 if(ev.key==='ArrowDown')rows[Math.min(i+1,rows.length-1)].focus();
 else if(i<=0)form.querySelector('input').focus();else rows[i-1].focus()});
function searchSubmit(form){const inp=form.querySelector('input[name=q]');return !!(inp.value||'').trim()}
function askChat(v){const f=document.createElement('form');f.method='post';f.action='/chat';
 const i=document.createElement('input');i.type='hidden';i.name='frage';i.value=v;f.appendChild(i);
 document.body.appendChild(f);f.submit()}
/* phone: Info | Chat (Kader | Chat) switch; desktop shows both */
function setView(btn,view){const pg=document.querySelector('.pg');if(!pg)return;pg.dataset.view=view;
 btn.parentElement.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',b===btn?'true':'false'));
 try{const u=new URL(location.href);if(view==='chat')u.searchParams.set('tab','chat');else u.searchParams.delete('tab');
  history.replaceState(null,'',u.pathname+u.search)}catch(e){}
 if(view==='chat')scrollChat();else window.scrollTo({top:0})}
/* filter chips: show only one kind of entry (client side) */
function setFilter(btn,f){const box=document.getElementById('info');if(!box)return;box.dataset.f=f;
 btn.parentElement.querySelectorAll('.fchip').forEach(b=>b.setAttribute('aria-pressed',b===btn?'true':'false'));
 box.querySelectorAll('[data-k]').forEach(el=>{el.hidden=f!=='alle'&&!el.dataset.k.split(' ').includes(f)});
 box.querySelectorAll('[data-sec]').forEach(sec=>{const own=sec.dataset.sec.split(' ');
  const rows=sec.querySelectorAll('[data-k]');
  sec.hidden=f!=='alle'&&!own.includes(f)&&(!rows.length||[...rows].every(r=>r.hidden))});
 box.querySelectorAll('.tl-m').forEach(h=>{let n=h.nextElementSibling,any=false;
  while(n&&!n.classList.contains('tl-m')){if(!n.hidden&&n.dataset.k)any=true;n=n.nextElementSibling}h.hidden=!any});
 if(f==='erfolge'){const er=box.querySelector('.erf');if(er){er.classList.add('all');const b=er.parentElement.querySelector('.erf-all');if(b)b.hidden=true}}
 if(f==='steckbrief'){const d=box.querySelector('.drawer');const b=d&&d.querySelector('.drawer-btn');if(b&&!d.classList.contains('open'))toggleOpen(b,'.drawer')}
 if(f!=='alle')box.querySelectorAll('.tlw').forEach(w=>w.classList.add('open'));
 /* the bar is sticky: when scrolled down, jump back to the start of the content below it */
 const bar=btn.closest('.pbar'),main=box.querySelector('.info-main');if(!bar||!main)return;
 const y=main.getBoundingClientRect().top+scrollY-stickyTop();
 if(scrollY>y)window.scrollTo({top:y})}
function stickyTop(){let top=0;document.querySelectorAll('header.top,.seg-wrap').forEach(el=>{
 if(getComputedStyle(el).position==='sticky'&&el.offsetParent!==null)top+=el.offsetHeight});return top}
function moreRows(btn){const t=btn.previousElementSibling;if(t)t.classList.add('all');btn.hidden=true}
function tlMore(btn){btn.closest('.tlw').classList.add('open')}
/* sideways scrollers: fade + hint on the side where more is hidden (class xs) */
const XS_SEL='.chips,.st-scroll,.st-tabs,.atab,.sb-scroll,.xs';
function xsUpdate(el){const m=el.scrollWidth-el.clientWidth;el.classList.toggle('xl',m>2&&el.scrollLeft>2);
 el.classList.toggle('xr',m>2&&m-el.scrollLeft>2)}
const xsObs=window.ResizeObserver?new ResizeObserver(es=>es.forEach(x=>xsUpdate(x.target))):null;
function xsInit(root){(root||document).querySelectorAll(XS_SEL).forEach(el=>{if(el.dataset.xs)return;
 el.dataset.xs='1';el.classList.add('xs');el.addEventListener('scroll',()=>xsUpdate(el),{passive:true});
 if(xsObs){xsObs.observe(el);if(el.firstElementChild)xsObs.observe(el.firstElementChild)}xsUpdate(el)})}
document.addEventListener('DOMContentLoaded',()=>xsInit());
document.addEventListener('htmx:afterSwap',ev=>xsInit(ev.detail.target));
addEventListener('resize',()=>document.querySelectorAll('.xs').forEach(xsUpdate));
function toggleOpen(btn,sel){const box=btn.closest(sel);const open=!box.classList.contains('open');
 box.classList.toggle('open',open);btn.setAttribute('aria-expanded',open?'true':'false')}
function showAll(btn){const er=btn.parentElement.querySelector('.erf');er.classList.add('all');btn.hidden=true}
/* v11 Statistik: one table pane per team kind and phase, chosen by tab + Playoffs toggle */
function statShow(sec){let any=false;sec.querySelectorAll('.st-pane').forEach(p=>{
  const on=p.dataset.g===sec.dataset.g&&p.dataset.p===sec.dataset.p;p.hidden=!on;if(on)any=true});
 sec.querySelector('.st-empty').hidden=any}
function statTab(btn,g){const sec=btn.closest('.stats');sec.dataset.g=g;
 btn.parentElement.querySelectorAll('button').forEach(b=>b.setAttribute('aria-pressed',b===btn?'true':'false'));
 statShow(sec)}
function statPhase(cb){const sec=cb.closest('.stats');sec.dataset.p=cb.checked?'playoffs':'regular';statShow(sec)}
function statAll(btn){btn.previousElementSibling.querySelector('table').classList.add('all');btn.hidden=true}
/* desktop: chat wider (~60 %), remembered in this browser */
function chatBig(btn,on){const pg=document.querySelector('.pg');if(!pg)return;
 if(on===undefined)on=!pg.classList.contains('chat-big');pg.classList.toggle('chat-big',on);
 document.querySelectorAll('.ch-big').forEach(b=>{b.setAttribute('aria-pressed',on?'true':'false');
  b.setAttribute('aria-label',on?'Chat verkleinern':'Chat vergrössern');b.title=on?'Verkleinern':'Vergrössern';
  b.innerHTML=on?ICON_SHRINK:ICON_EXPAND});
 try{localStorage.setItem('chatBig',on?'1':'0')}catch(e){}}
document.addEventListener('DOMContentLoaded',()=>{let on=false;try{on=localStorage.getItem('chatBig')==='1'}catch(e){}
 if(on)chatBig(null,true)});
/* "Nächste Fragen": a chip sends its question */
function askNext(btn){const panel=btn.closest('.chat');const ta=panel&&panel.querySelector('textarea[name=frage]');
 if(!ta)return;ta.value=btn.dataset.q;const f=ta.form;if(f.requestSubmit)f.requestSubmit();else f.submit()}
function openAdd(kind,name){const d=document.getElementById('add');if(!d)return;
 addKind(d.querySelector('.seg button[data-kind='+(kind||'person')+']'));
 if(name){d.querySelector('input[name=name]').value=name;d.querySelector('textarea[name=request]').value=name}
 d.querySelector('#add-result').innerHTML='';const m=document.querySelector('details.more');if(m)m.open=false;
 d.showModal()}
function addKind(btn){const d=btn.closest('dialog');const k=btn.dataset.kind;
 d.querySelectorAll('.seg button').forEach(b=>b.setAttribute('aria-pressed',b===btn?'true':'false'));
 d.querySelectorAll('form.addform').forEach(f=>f.hidden=f.dataset.kind!==k)}
function scrollChat(){const msgs=document.getElementById('msgs');if(!msgs||!msgs.querySelector('.msg'))return;
 const users=msgs.querySelectorAll('.msg.user');const last=users.length?users[users.length-1]:msgs.lastElementChild;
 if(msgs.offsetParent===null)return;
 if(getComputedStyle(msgs).overflowY==='auto'){
  msgs.scrollTop+=last.getBoundingClientRect().top-msgs.getBoundingClientRect().top-12;return}
 const top=12+stickyTop();
 window.scrollTo({top:last.getBoundingClientRect().top+window.scrollY-top})}
document.addEventListener('htmx:afterSettle',ev=>{const p=ev.detail.pathInfo||{};
 if(/^(\\/p\\/[^/]+)?\\/chat/.test(String(p.requestPath||'')))scrollChat()});
document.addEventListener('DOMContentLoaded',scrollChat);
document.addEventListener('htmx:afterSwap',ev=>{const t=ev.detail.target;
 if(t&&t.id==='add-result'&&t.firstElementChild)t.firstElementChild.scrollIntoView({block:'start',behavior:'smooth'})});
document.addEventListener('click',ev=>{const m=document.querySelector('details.more[open]');
 if(m&&!m.contains(ev.target))m.open=false});
"""


def js() -> str:
    """Page script with the few icons it inserts itself."""
    icons = {"MIC": "mic", "CHECK": "check", "EXPAND": "expand", "SHRINK": "shrink"}
    consts = "".join(f"const ICON_{k}={icon(v)!r};" for k, v in icons.items())
    return consts + JS


# --- page frame ----------------------------------------------------------------------------------------


def more_menu(extra: str = "") -> str:
    """v9 "Mehr": project switcher, project page, sources, ratings, chats, add — admin things live here."""
    from klugscheisser import projects

    cur = projects.current()
    projs = "".join(
        f'<a href="/p/{e(p.key)}/"{" class=cur aria-current=true" if p.key == cur.key else ""}>{e(p.name)}'
        f"{' <span class=small>(archiviert)</span>' if not p.active else ''}</a>"
        for p in projects.all_projects()
    )
    return f"""<details class="more"><summary title="Mehr: Projekt, Quellen, Einstellungen">{icon("more")}
<span class="more-l">Mehr</span></summary><div class="menu">
{extra}
<div class="mh" title="Projekt wechseln">Projekt wechseln</div>{projs}
<a href="/projekt">Projekt: Briefing &amp; Einstellungen</a><hr>
<a href="/chats">{icon("list", 16)}Alle Chats</a>
<button type="button" onclick="openAdd('person')">{icon("plus", 16)}Person hinzufügen</button>
<button type="button" onclick="openAdd('team')">{icon("plus", 16)}Team hinzufügen</button><hr>
<a href="/quellen">Quellen</a><a href="/bewertungen">Bewertungen</a>
</div></details>"""


def main_nav(nav: str) -> str:
    """The two places of the app, always visible (phone and desktop): Suche (find a person, team, event and
    open its page) and Chat (ask Karl a question in full sentences)."""
    items = (
        ("suche", "/suche", "search", "Suche", "Person, Team oder Anlass suchen"),
        ("chat", "/chat", "chat", "Chat", "Karl eine Frage stellen"),
    )
    cur = ' aria-current="page"'
    links = "".join(
        f'<a class="nav-{key}" href="{href}" title="{title}"{cur if key == nav else ""}>'
        f"{icon(ic, 18)}<span>{label}</span></a>"
        for key, href, ic, label, title in items
    )
    return f'<nav class="mainnav" aria-label="Hauptnavigation">{links}</nav>'


def layout(
    title: str,
    body: str,
    nav: str = "",
    wide: bool = False,
    search: bool = True,
    menu: str = "",
    bare: bool = False,
) -> str:
    """The page frame: logo, the two places (Suche | Chat), on wide screens a quick search field, "Mehr" menu.
    `nav` marks the current place ("suche" / "chat"); `search=False` hides the quick field (the page has its own).
    `bare` (login, unknown address): logo only — nothing that needs a login or names projects."""
    box = search_box(top=True) if search and not bare else ""
    nav_html = "" if bare else main_nav(nav)
    menu_html = "" if bare else more_menu(menu)
    dialog = "" if bare else add_dialog()
    main_cls = ' class="wide"' if wide else (' class="bare"' if bare else "")
    favicon = (
        "data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 32 32%22>"
        "<rect width=%2232%22 height=%2232%22 rx=%227%22 fill=%22%230D1117%22/><text x=%2216%22 y=%2223%22 "
        "font-family=%22Arial%22 font-weight=%22700%22 font-size=%2219%22 fill=%22white%22 "
        "text-anchor=%22middle%22>K</text></svg>"
    )
    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<link rel="icon" href="{favicon}">
<title>{e(title)} · {APP_NAME}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="{FONTS}" rel="stylesheet">
<style>{CSS}</style>
<script src="{HTMX}" defer></script>
<script>{js()}</script>
</head><body>
<header class="top"><div class="in">
<a class="logo" href="/" aria-label="{APP_NAME}: Startseite"><span class="logo-k" aria-hidden="true">K</span><span class="logo-t">{APP_NAME}</span></a>
{nav_html}
{box}
{menu_html}
</div></header>
<main{main_cls}>{body}</main>
{dialog}
</body></html>"""


def search_box(value: str = "", autofocus: bool = False, top: bool = False) -> str:
    """Search for names, teams and events (suggestions while typing). Questions belong in the chat: the
    suggestions offer «… im Chat fragen» and the result page says so, nothing jumps there by itself."""
    fid = "q" if top else "qh"
    return f"""<form class="gsearch{"" if top else " big"}" action="/suche" method="get" role="search"
 onsubmit="return searchSubmit(this)"><label class="sr" for="{fid}">Suche: Person, Team oder Anlass</label>
<span class="gs-i">{icon("search")}</span>
<input id="{fid}" type="search" name="q" value="{e(value)}" placeholder="Person, Team oder Anlass suchen"
 autocomplete="off" enterkeyhint="search"{" autofocus" if autofocus else ""}
 hx-get="/suche" hx-trigger="input changed delay:200ms" hx-target="next .suggest" hx-swap="innerHTML">
<div class="suggest"></div></form>"""


def add_dialog() -> str:
    """Popup to add a person or a whole team: proposal first, then Ja / Nein / Präzisieren."""
    from klugscheisser.sports import current_spec

    # 2026-11-24: examples in the words of the project (men's floorball: Stürmer, Nationalteam)
    if current_spec().men:
        ex_name, ex_det, ex_team = (
            "Vorname Nachname",
            "schwedischer Stürmer, spielt bei IBF Falun, Jahrgang 1999",
            "das schwedische Nationalteam",
        )
    else:
        ex_name, ex_det, ex_team = (
            "Hanna Olsson",
            "schwedische Stürmerin, spielt bei Frölunda, Jahrgang 1999",
            "das schwedische Frauen-Nationalteam",
        )
    hx = (
        'hx-post="/hinzufuegen" hx-target="#add-result" hx-swap="innerHTML" hx-disabled-elt="find button" '
        'hx-indicator="#add-progress"'
    )
    return f"""<dialog id="add" aria-labelledby="add-h"><div class="in">
<h2 id="add-h">Hinzufügen</h2>
<div class="seg" role="group" aria-label="Was hinzufügen"><button type="button" data-kind="person" aria-pressed="true"
 onclick="addKind(this)">Person</button>
<button type="button" data-kind="team" aria-pressed="false" onclick="addKind(this)">Team / mehrere</button></div>
<form class="addform" data-kind="person" method="post" action="/hinzufuegen" {hx}>
<input type="hidden" name="kind" value="person">
<label for="add-name">Name</label><input id="add-name" type="text" name="name" required placeholder="z. B. {ex_name}">
<label for="add-ig">Instagram (optional)</label><input id="add-ig" type="text" name="instagram" placeholder="@handle oder Link">
<label for="add-det">Details (optional)</label>
<textarea id="add-det" name="details" placeholder="z. B. {ex_det}"></textarea>
<p><button class="btn primary" type="submit">Vorschlag holen</button>
<button class="btn ghost" type="button" onclick="this.closest('dialog').close()">Abbrechen</button></p>
</form>
<form class="addform" data-kind="team" method="post" action="/hinzufuegen" {hx} hidden>
<input type="hidden" name="kind" value="team">
<label for="add-team">Welches Team?</label>
<textarea id="add-team" name="request" required placeholder="z. B. {ex_team}"></textarea>
<p class="small">Trainer und Staff werden mit aufgenommen.</p>
<p><button class="btn primary" type="submit">Vorschlag holen</button>
<button class="btn ghost" type="button" onclick="this.closest('dialog').close()">Abbrechen</button></p>
</form>
<div class="indicator progress" id="add-progress">Suche in Wikidata, Wikipedia und im Web … (bis 30 s)</div>
<div id="add-result"></div>
</div></dialog>"""


# --- small parts ---------------------------------------------------------------------------------------


def initials(name: str) -> str:
    """'Saskia Maurer' -> 'SM' (photo stand-in in search rows and the person header)."""
    parts = [x for x in (name or "").split() if x[:1].isalpha()]
    return ((parts[0][0] + parts[-1][0]) if len(parts) > 1 else (parts[0][:2] if parts else "?")).upper()


def copy_button(text: str) -> str:
    return (
        f'<button class="copy" type="button" title="Zeile kopieren" aria-label="Zeile kopieren" '
        f'data-copy="{e(text)}" onclick="copyLine(this)">{icon("copy", 14)}</button>'
    )


def meaning(prefix: str | None) -> str:
    """'📸 (von ihr selbst) 🎉 (Freude)' -> 'von ihr selbst · Freude': the words, not the emoji."""
    import re

    words = re.findall(r"\(([^)]*)\)", prefix or "")
    return " · ".join(words)


def short_label(quelle: str | None) -> str:
    """'[SRF, 03.10.]' -> 'SRF'; '[Instagram @lara, 01.10.]' -> '@lara' (the chip next to a row)."""
    label = (quelle or "Quelle").strip("[]").strip()
    head, _, tail = label.rpartition(", ")
    if head and (tail[:1].isdigit() or tail == "ohne Datum"):
        label = head
    for prefix in ("Instagram ", "X "):
        if label.startswith(prefix + "@"):
            label = label[len(prefix) :]
    return label or "Quelle"


def source_link(item: dict[str, Any]) -> str:
    label = (item.get("quelle") or "Quelle").strip("[]")
    return (
        f'<a class="src" href="{safe_url(item.get("url"))}" target="_blank" rel="noopener" title="{e(label)}">'
        f"{e(short_label(label))}</a>"
    )


def fact_of(item: dict[str, Any], limit: int = 220) -> str:
    fact = item.get("titel") or item.get("zusammenfassung") or ""
    if len(fact) < 25 and item.get("bildbeschreibung"):
        fact = f"{fact} – Bild: {item['bildbeschreibung']}".strip(" –")
    if len(fact) > limit:
        fact = fact[:limit].rsplit(" ", 1)[0] + " […]"
    return fact


def markdown_line(item: dict[str, Any], fact: str | None = None) -> str:
    """The same line the MCP tools produce: emoji (meaning) + fact + Markdown source link."""
    return f"{item.get('zeile_prefix', '')} {fact if fact is not None else fact_of(item)} {item.get('quelle_link', '')}".strip()


def ago_old(dt: datetime | None, now: datetime | None = None) -> bool:
    return dt is None or (now or datetime.now(UTC)) - dt > OLD_AFTER


def stand_groups(times: dict[str, datetime | None]) -> list[tuple[list[str], datetime | None]]:
    """Sources fetched on the same day (and not old) share one entry with the OLDEST time of the group:
    'Instagram, News, Resultate heute 05:08' instead of three times nearly the same. Old ones stay apart."""
    from klugscheisser.domain.labels import local

    out: list[tuple[list[str], datetime | None]] = []
    for label, dt in times.items():
        prev = out[-1] if out else None
        same = (
            prev is not None
            and dt is not None
            and prev[1] is not None
            and not ago_old(dt)
            and not ago_old(prev[1])
            and local(dt).date() == local(prev[1]).date()
        )
        if same:
            prev[0].append(label)
            out[-1] = (prev[0], min(prev[1], dt))
        else:
            out.append(([label], dt))
    return out


def stand_line(times: dict[str, datetime | None], target: str, status_html: str = "", first: str = "") -> str:
    """'Stand: Instagram heute 13:47 · News … · Resultate …' (older than 7 days in amber) + refresh."""
    from klugscheisser.mcp_server.present import relative_de

    parts = [first] if first else []
    for labels, dt in stand_groups(times):
        text = f"{e(', '.join(labels))} {e(relative_de(dt))}"
        parts.append(f'<span class="old" title="älter als 7 Tage">{text}</span>' if ago_old(dt) else text)
    return f"""<div class="stand"><span class="st">Stand: {" · ".join(parts)}</span>
<form method="post" action="/aktualisieren" hx-post="/aktualisieren" hx-target="#refresh-status" hx-swap="innerHTML"
 hx-disabled-elt="find button"><input type="hidden" name="target" value="{e(target)}">
<button class="lnk" type="submit" aria-label="Aktualisieren" title="Instagram und News neu holen">{icon("refresh", 14)}
<span class="lbl">Aktualisieren</span></button></form></div>
<div id="refresh-status">{status_html}</div>"""


def poll_fragment(job_id: int, text: str) -> str:
    return (
        f'<div class="small" hx-get="/aktualisieren/{job_id}" hx-trigger="load delay:3s" hx-swap="outerHTML">'
        f"{e(text)}</div>"
    )


def view_switch(views: list[tuple[str, str]], current: str) -> str:
    """Phone: big segmented buttons (Info | Chat); hidden on wide screens where both are visible."""
    buttons = "".join(
        f"<button type='button' aria-pressed='{'true' if key == current else 'false'}' "
        f"onclick=\"setView(this,'{key}')\"><span class='l'>{e(label)}</span></button>"
        for key, label in views
    )
    return f"<div class='seg-wrap'><div class='seg' role='group' aria-label='Ansicht'>{buttons}</div></div>"


def filter_chips(filters: list[tuple[str, str, int]]) -> str:
    chips = "".join(
        f"<button type='button' class='fchip' aria-pressed='{'true' if i == 0 else 'false'}' "
        f"onclick=\"setFilter(this,'{key}')\"><span class='in'>{e(label)}<span class='n'>{n}</span></span></button>"
        for i, (key, label, n) in enumerate(filters)
        if n or i == 0
    )
    return f"<div class='chips' role='toolbar' aria-label='Filter'>{chips}</div>"


def vorsicht_line(items: list[dict[str, Any]]) -> str:
    """Vorsicht as ONE calm line (newest topic, date, number of reports) that opens into the linked reports."""
    if not items:
        return ""
    newest = max(items, key=lambda i: i.get("published_at") or "")
    topic = newest.get("titel_de") or newest.get("titel") or newest.get("zusammenfassung") or "Heikles Thema"
    date = newest.get("datum") or ""
    day, _, year = date.rpartition(".") if date.count(".") == 2 else (date, "", "")
    when = f"{e(day)}.<span class='y'>{e(year)}</span>" if year else e(date)
    n = len(items)
    rows = "".join(
        f"<a class='vrow' href='{safe_url(i.get('url'))}' target='_blank' rel='noopener'>"
        f"<span class='t'>{e(fact_of(i, 160))}"
        + (
            f"<span class='x'>Deutsch: {e(i.get('titel_de') or i.get('zusammenfassung_de'))}</span>"
            if i.get("titel_de") or i.get("zusammenfassung_de")
            else ""
        )
        + f"</span><span class='src'>{e(short_label(i.get('quelle')))}</span><span class='e'>{icon('ext', 14)}</span></a>"
        for i in items
    )
    return (
        f"<details class='vline' id='vorsicht'><summary aria-label='Vorsicht: {e(topic)}, {e(date)}, {n} Berichte'>"
        f"{icon('alert', 16)}<span class='vt'>Vorsicht</span><span class='vs'>{e(topic)}</span>"
        f"<span class='vd'>{when} · {n} {'Bericht' if n == 1 else 'Berichte'}</span>"
        f"<span class='chev'>{icon('down')}</span></summary>{rows}"
        "<div class='vnote'>Heikle Themen (Gesundheit, Privatleben, Konflikte): nur mit Quelle und Vorsicht "
        "verwenden.</div></details>"
    )


def say_buttons(athlete_id: int, nationality: str | None = None, recorded: bool = False) -> str:
    """Normal / slow pronunciation of a person's name (v6 step 7); none for German-language names unless a real
    recording is stored (v11: Wikimedia or Elite Prospects)."""
    from klugscheisser.pronounce import offered

    if not (recorded or offered(nationality)):
        return ""
    return (
        f'<span class="say" data-id="{int(athlete_id)}">'
        f'<button type="button" onclick="say(this,false)" title="Aussprache anhören" aria-label="Aussprache anhören">'
        f"{icon('speaker')}</button>"
        f'<button type="button" onclick="say(this,true)" title="Langsam anhören" aria-label="Langsam anhören">'
        f"{icon('slow')}</button></span>"
    )


def tier_badge(tier: int | None, sources: int | None = None, short: bool = False) -> str:
    """v8: 'Tier 1 · 64 Quellen' next to a name (nothing for tier 3). v9 `short`: just 'T1' in dense lists."""
    from klugscheisser.tiers import badge

    text = badge(tier or 3, sources)
    if not text:
        return ""
    label = f"T{int(tier or 3)}" if short else text
    return (
        f"<span class='tier tier-{int(tier or 3)}' title='Wie tief wir suchen: {e(text)}'>{e(label)}</span>"
    )
