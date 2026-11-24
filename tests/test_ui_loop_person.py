"""ui-loop round 2 (2026-10-17): person page orientation helpers (no database needed)."""

from datetime import UTC, datetime, timedelta

from klugscheisser.web import html as h
from klugscheisser.web import timeline as tl
from klugscheisser.web.person import chat_label


def _items(n: int, now: datetime) -> list[dict]:
    """n news rows, one every 10 days back from now."""
    return [
        {
            "when": now - timedelta(days=10 * i + 1),
            "k": "news",
            "label": "News",
            "title": f"Bericht {i}",
            "sub": "SRF",
            "src": "SRF",
            "url": f"https://www.srf.ch/{i}",
            "extern": True,
            "vorsicht": False,
        }
        for i in range(n)
    ]


def test_timeline_keeps_whole_months_and_collapses_the_rest():
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    items = _items(30, now)
    html = tl.timeline_html(items, now, keep=12)
    shown, _, rest = html.partition("<div class='tl-old'>")
    assert "class='tlw cut'" in html and "Ältere Einträge anzeigen" in html
    n_shown, n_rest = shown.count('class="tl'), rest.count('class="tl')
    assert n_shown >= 12 and n_shown + n_rest == 30
    # a month is never split between the visible and the collapsed part
    months = lambda part: {m for m in tl.MONTHS if f">{m} " in part}  # noqa: E731
    assert not months(shown) & months(rest)
    # every row still links to its source in both parts
    assert html.count('target="_blank" rel="noopener"') == 30


def test_timeline_not_cut_when_little_is_left():
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    html = tl.timeline_html(_items(14, now), now, keep=12)
    assert "tl-old" not in html and "Ältere Einträge" not in html
    assert "tl-old" not in tl.timeline_html(_items(40, now), now)  # without keep: everything, as before


def test_timeline_row_keeps_a_source_chip_on_phone_and_desktop():
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    row = tl.row_html(_items(1, now)[0])
    assert "<span class='src s2'>SRF</span>" in row and "<span class='src'>SRF</span>" in row


def test_stand_groups_same_day_with_oldest_time():
    now = datetime.now(UTC)
    t1, t2 = now - timedelta(seconds=2), now - timedelta(seconds=1)
    old = now - timedelta(days=20)
    groups = h.stand_groups({"Instagram": t1, "News": t2, "Resultate": old})
    assert groups[0] == (["Instagram", "News"], t1)
    assert groups[1] == (["Resultate"], old)  # old ones stay apart (amber)
    assert h.stand_groups({"Instagram": None, "News": t1}) == [(["Instagram"], None), (["News"], t1)]
    line = h.stand_line({"Instagram": t1, "News": t2}, "1")
    assert "Stand: Instagram, News " in line


def test_chat_label_uses_first_name():
    assert chat_label("Saskia Maurer") == "Chat zu Saskia"
    assert chat_label("Nicola Hadraschek-Eisenschmid") == "Chat zu Nicola"
    assert chat_label("") == "Chat"
