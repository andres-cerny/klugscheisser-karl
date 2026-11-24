"""MCP noise: which items reach "⚠️ Vorsicht" / "⭐ Zu beachten", compact event roster, roster wording."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from klugscheisser.db import models as m
from klugscheisser.mcp_server import caution, present

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
ME = 7


def _item(
    iid: int,
    days_ago: int,
    *,
    kind: str = "media",
    platform: str = "web",
    source_type: str = "web",
    category: str = "injury",
    vorsicht: bool = True,
    nw: int = 4,
    main: bool | None = None,
    athlete: int = ME,
) -> m.Item:
    it = m.Item(
        id=iid,
        source_type=source_type,
        platform=platform,
        source_kind=kind,
        url=f"https://example.org/{iid}",
        content_hash=str(iid),
        published_at=NOW - timedelta(days=days_ago),
        text=f"Bericht {iid}",
    )
    it.classification = m.Classification(
        relevant_p=0.9,
        relevant=True,
        category=category,
        sensitive_p=0.8,
        vorsicht=vorsicht,
        newsworthiness=nw,
        classifier="test",
        classifier_version="1",
    )
    it.athletes = [m.ItemAthlete(athlete_id=athlete, match_kind="name", main=main)]
    return it


def _pick(items, keep=lambda i: i.classification.vorsicht, window=21):
    return [(i.id, note) for i, note in caution.select(items, ME, keep, window, now=NOW)]


def test_recent_official_and_media_items_pass():
    items = [
        _item(1, 3, kind="official"),
        _item(2, 5, kind="media"),
        _item(3, 2, kind="self", platform="instagram"),
    ]
    assert [i for i, _ in _pick(items)] == [1, 2, 3]


def test_fan_and_social_posts_need_a_trusted_report_on_the_same_topic():
    joke = _item(1, 17, kind="web", platform="x", source_type="other_media", category="team_coach")
    fan = _item(2, 4, kind="fan", platform="instagram", source_type="tagged_post")
    assert _pick([joke, fan]) == []  # "NJJ profile pic curse": an X user's joke alone is never Vorsicht
    report = _item(3, 5, kind="media")  # a media report on her injury one day before the fan post
    assert {i for i, _ in _pick([fan, report])} == {2, 3}
    far = _item(4, 12, kind="media")  # same topic, but a week away: not the same story
    assert [i for i, _ in _pick([fan, far])] == [4]


def test_unknown_web_sites_count_as_media_but_not_social_accounts():
    article = _item(1, 3, kind="web", platform="web")
    tweet = _item(2, 3, kind="web", platform="x", category="controversy")  # nothing backs it
    assert [i for i, _ in _pick([article, tweet])] == [1]


def test_side_persons_are_left_out():
    # a photographer's own surgery post that tags her next to others: Jev marked her as not the main person
    tagged = _item(1, 2, kind="media", platform="instagram", source_type="tagged_post", main=False)
    assert _pick([tagged]) == []
    assert _pick([_item(2, 2, main=True)]) == [(2, None)]


def test_old_items_drop_except_the_newest_injury_report_labelled_with_its_date():
    old_injury = _item(1, 56)
    older_injury = _item(2, 60)
    old_joke = _item(3, 40, category="private_life")
    picked = _pick([old_injury, older_injury, old_joke])
    assert [i for i, _ in picked] == [1]
    assert "älterer Bericht vom 24.08.2026" in picked[0][1]


def test_old_injury_report_is_replaced_by_a_newer_status():
    update = _item(2, 30, vorsicht=False)  # newer injury report (e.g. "back in training"), not flagged
    assert _pick([_item(1, 56), update]) == []
    assert _pick([_item(1, 56), _item(3, 5)]) == [(3, None)]  # a recent report: the old one is not needed


def test_profile_pages_are_not_news():
    wiki = _item(1, 2, kind="media", platform="wikipedia", source_type="profile", category="result", nw=5)
    assert _pick([wiki], keep=lambda i: i.classification.newsworthiness >= 3) == []


def test_window_is_configurable():
    items = [_item(1, 25, kind="official")]
    assert _pick(items, window=21) == [
        (1, "älterer Bericht vom 24.09.2026; seither kein neuer Bericht zur Verletzung in unseren Quellen")
    ]
    assert _pick(items, window=30) == [(1, None)]


def test_old_report_note_is_rendered_next_to_the_link():
    line = present.line_for(
        {
            "zeile_prefix": "📰",
            "titel": "Stalder verletzt",
            "quelle_link": "[Blick, 24.08.](u)",
            "hinweis_alt": "älterer Bericht vom 24.08.2026",
        }
    )
    assert line.endswith("[Blick, 24.08.](u) _(älterer Bericht vom 24.08.2026)_")


def test_athlete_events_say_im_aufgebot_and_skip_a_date_already_in_the_name():
    text = present.render_markdown(
        {
            "athletin": {"name": "Alina Müller"},
            "anlaesse": [
                {
                    "anlass": "IIHF Frauen-WM 2026",
                    "datum": "06.11.2026",
                    "kader_status": "provisional",
                    "url": "u1",
                },
                {
                    "anlass": "EHT, Södertälje (28.–31.10.2026)",
                    "datum": "28.10.2026",
                    "kader_status": "confirmed",
                },
            ],
        }
    )
    assert "- IIHF Frauen-WM 2026 (06.11.2026): im Aufgebot (provisorisch) [" in text
    assert "- EHT, Södertälje (28.–31.10.2026): im Aufgebot (bestätigt)" in text
    assert "Kader confirmed" not in text and "Kader provisional" not in text


def test_event_roster_lines_without_handles_and_posts_by_default():
    team = {
        "team": "Schweiz",
        "kader_status": "provisorisch",
        "spielerinnen": [
            {
                "name": "Andrea Brändli",
                "position": "Torhüterin",
                "nummer": 20,
                "verein": {"team": "PWHL Detroit"},
            }
        ],
    }
    text = present.render_markdown({"teams": [team]})
    assert "- Andrea Brändli (Torhüterin, #20, PWHL Detroit)\n" in text
    assert "instagram.com" not in text
