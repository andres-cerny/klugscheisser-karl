"""v5 phase 7: more feeds (Swiss regional, public broadcasters), multilingual women's-hockey filter."""

from datetime import UTC, datetime

from klugscheisser.classify.base import SOURCE_KIND_EMOJI
from klugscheisser.collectors.rss import DEFAULT_FEEDS, Article, clean_link, is_womens_hockey


def art(title: str, summary: str = "", url: str = "https://x.example/a") -> Article:
    return Article(
        feed="T",
        source_type="other_media",
        url=url,
        source_kind="public",
        title=title,
        summary=summary,
        published_at=datetime.now(UTC),
    )


def test_multilingual_womens_hockey_filter():
    assert is_womens_hockey(art("Damkronorna föll mot Finland i ishockey-VM"))
    assert is_womens_hockey(art("Naisleijonat voitti Kanadan jääkiekon MM-kisoissa"))
    assert is_womens_hockey(art("České hokejistky porazily Švýcarsko"))
    assert is_womens_hockey(art("Hockey féminin: les Suissesses battues"))
    assert is_womens_hockey(art("Hockey su ghiaccio: la nazionale femminile svizzera"))
    assert not is_womens_hockey(art("Tre Kronor vann mot Tjeckien i ishockey"))  # men's team
    assert not is_womens_hockey(art("Naisten jalkapallo: Suomi voitti"))  # women's football


def test_feed_list_and_labels():
    names = {f.name for f in DEFAULT_FEEDS}
    assert {"SVT Sport", "RTS Sport", "Yle Urheilu", "ČT sport", "ORF Sport"} <= names
    assert not names & {"BBC Sport ice hockey", "Sporza", "DR Sporten", "Zuger Zeitung"}
    full = {f.name for f in DEFAULT_FEEDS if f.fetch_article}
    assert full <= {"SRF", "SVT Sport", "ARD Sportschau Eishockey", "ARD Sportschau Wintersport"}
    assert SOURCE_KIND_EMOJI["public"] == ("📺", "öffentlich-rechtlich")
    assert {f.language for f in DEFAULT_FEEDS} >= {"de", "fr", "it", "sv", "fi", "cs"}


def test_tracking_parameters_removed():
    assert clean_link("https://www.rts.ch/sport/hockey/123.html?rts_source=rss_t") == (
        "https://www.rts.ch/sport/hockey/123.html"
    )
    assert clean_link("https://a.ch/x?id=5&utm_source=rss") == "https://a.ch/x?id=5"
