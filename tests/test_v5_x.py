"""v5 phase 8: X posts via the xAI Responses API (x_search). Offline.

The fixture below is NOT a recorded API response. It is built by hand from the documented format
(docs.x.ai/developers/tools/citations: ``output_text`` with ``url_citation`` annotations;
/tools/x-search and the Responses API reference: ``usage.server_side_tool_usage_details`` with
``x_posts_fetched``, ``cost_in_usd_ticks``). Replace it with a real recording after the first live run.
"""

import json
import os
from datetime import UTC, date, datetime

import httpx
import pytest

from klugscheisser.collectors.x_grok import (
    DEFAULT_MODEL,
    MAX_HANDLES,
    OEMBED_URL,
    RESPONSES_URL,
    GrokXSearch,
    XPost,
    cost_usd,
    parse_oembed,
    parse_response,
)
from klugscheisser.config import Settings

MODEL_JSON = {
    "posts": [
        {
            "url": "https://x.com/i/status/1975000000000000001",
            "author_handle": "testa_beispiel",
            "text": "Back on snow in Saas-Fee! First downhill training block done.",
            "posted_at": "2026-09-28T14:05:00Z",
        },
        {
            "url": "https://x.com/srfsport/status/1975000000000000002",
            "author_handle": "srfsport",
            "text": "Testa Beispiel fährt im Training Bestzeit.",
            "posted_at": "2026-09-29",
        },
        {  # not cited -> must be dropped
            "url": "https://x.com/someone/status/1975000000000000099",
            "author_handle": "someone",
            "text": "invented",
            "posted_at": "2026-09-30T08:00:00Z",
        },
    ]
}
TEXT = json.dumps(MODEL_JSON)

RESPONSE = {
    "id": "5808284d-ae14-9981-9289-73515f67ebda",
    "object": "response",
    "model": DEFAULT_MODEL,
    "status": "completed",
    "output": [
        {"type": "x_search_call", "id": "xs_1", "status": "completed"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [
                {
                    "type": "output_text",
                    "text": TEXT,
                    "logprobs": [],
                    "annotations": [
                        {
                            "type": "url_citation",
                            "url": "https://x.com/i/status/1975000000000000001",
                            "start_index": 0,
                            "end_index": 10,
                            "title": "1",
                        },
                        {
                            "type": "url_citation",
                            "url": "https://x.com/srfsport/status/1975000000000000002",
                            "start_index": 11,
                            "end_index": 20,
                            "title": "2",
                        },
                        {  # duplicate of 1 -> once
                            "type": "url_citation",
                            "url": "https://x.com/testa_beispiel/status/1975000000000000001",
                            "start_index": 21,
                            "end_index": 30,
                            "title": "1",
                        },
                        {  # cited but not in the model's JSON -> url only
                            "type": "url_citation",
                            "url": "https://x.com/i/status/1975000000000000003",
                            "start_index": 31,
                            "end_index": 40,
                            "title": "3",
                        },
                        {  # not an X post -> ignored
                            "type": "url_citation",
                            "url": "https://www.srf.ch/sport/ski-alpin/testa",
                            "start_index": 41,
                            "end_index": 50,
                            "title": "4",
                        },
                    ],
                }
            ],
        },
    ],
    "usage": {
        "input_tokens": 12000,
        "output_tokens": 800,
        "total_tokens": 12800,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens_details": {"reasoning_tokens": 300},
        "num_server_side_tools_used": 2,
        "num_sources_used": 0,
        "cost_in_usd_ticks": 1_250_000_000,  # 0.125 USD
        "server_side_tool_usage_details": {
            "web_search_calls": 0,
            "x_search_calls": 2,
            "x_posts_fetched": 18,
            "x_users_fetched": 0,
            "code_interpreter_calls": 0,
            "file_search_calls": 0,
            "mcp_calls": 0,
            "document_search_calls": 0,
            "image_generation_calls": 0,
        },
    },
}


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, xai_api_key="test-key", **kw)


def _client(seen: list[httpx.Request], payload: dict = RESPONSE) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url) == RESPONSES_URL
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    def flush(self) -> None:
        pass


def test_build_request_x_search_tool():
    g = GrokXSearch(_settings(), client=httpx.Client())
    handles = ["@testa_beispiel", " ", *[f"h{i}" for i in range(30)]]
    body = g.build_request("Testa Beispiel", handles, date(2026, 9, 1), 5)
    tool = body["tools"][0]
    assert tool["type"] == "x_search"
    assert tool["from_date"] == "2026-09-01"
    assert tool["allowed_x_handles"][0] == "testa_beispiel"
    assert len(tool["allowed_x_handles"]) == MAX_HANDLES
    assert body["model"] == DEFAULT_MODEL
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["store"] is False
    assert "Testa Beispiel" in body["input"][0]["content"]
    plain = g.build_request("Testa Beispiel", None, None, 5)["tools"][0]
    assert plain == {"type": "x_search"}


def test_search_person_parses_cited_posts_only():
    seen: list[httpx.Request] = []
    g = GrokXSearch(_settings(), client=_client(seen))
    posts = g.search_person("Testa Beispiel", ["testa_beispiel"], date(2026, 9, 1), limit=10)
    assert seen[0].headers["Authorization"] == "Bearer test-key"
    assert [p.status_id for p in posts] == [
        "1975000000000000001",
        "1975000000000000002",
        "1975000000000000003",
    ]
    first, second, third = posts
    assert first == XPost(
        url="https://x.com/testa_beispiel/status/1975000000000000001",
        author="testa_beispiel",
        text="Back on snow in Saas-Fee! First downhill training block done.",
        posted_at=datetime(2026, 9, 28, 14, 5, tzinfo=UTC),
    )
    assert not first.text_verbatim
    assert second.author == "srfsport" and second.posted_at == datetime(2026, 9, 29, tzinfo=UTC)
    assert third.text is None and third.author is None and third.url.endswith("/i/status/1975000000000000003")
    assert all(p.text != "invented" for p in posts)


def test_limit_and_non_json_output():
    assert len(parse_response(RESPONSE, 2)) == 2
    broken = json.loads(json.dumps(RESPONSE))
    broken["output"][1]["content"][0]["text"] = "Here are the posts [[1]](https://x.com/i/status/1)"
    posts = parse_response(broken, 10)
    assert len(posts) == 3 and all(p.text is None for p in posts)


def test_top_level_citations_are_used():
    data = {
        "output": [],
        "citations": ["https://x.com/i/user/123", "https://twitter.com/fis_alpine/status/42"],
    }
    assert parse_response(data, 10) == [
        XPost(url="https://x.com/fis_alpine/status/42", author="fis_alpine", text=None, posted_at=None)
    ]


def test_missing_key_raises():
    g = GrokXSearch(Settings(_env_file=None, xai_api_key=None), client=httpx.Client())
    with pytest.raises(RuntimeError):
        g.search_person("Testa Beispiel")


def test_cost_exact_and_estimate():
    assert cost_usd(RESPONSE["usage"]) == pytest.approx(0.125)
    usage = {k: v for k, v in RESPONSE["usage"].items() if k != "cost_in_usd_ticks"}
    # 12000 * 1.25e-6 + 800 * 2.5e-6 + 18 * 0.005
    assert cost_usd(usage) == pytest.approx(0.015 + 0.002 + 0.09)
    assert cost_usd(None) == 0.0


def test_cost_recorded_only_with_session():
    g = GrokXSearch(_settings(), client=_client([]))
    g.search_person("Testa Beispiel")  # no session: nothing to record, no error
    s = FakeSession()
    g.search_person("Testa Beispiel", session=s)  # type: ignore[arg-type]
    (row,) = s.added
    assert row.source == "xai" and row.units == 18 and row.cost_usd == pytest.approx(0.125)


OEMBED = {
    "url": "https://x.com/testa_beispiel/status/1975000000000000001",
    "author_name": "Testa Beispiel",
    "author_url": "https://x.com/testa_beispiel",
    "html": '<blockquote class="twitter-tweet" data-dnt="true"><p lang="en" dir="ltr">Back on snow in '
    "Saas-Fee!<br><br>First downhill training block done &amp; happy. "
    '<a href="https://t.co/abc">pic.twitter.com/abc</a></p>&mdash; Testa Beispiel (@testa_beispiel) '
    '<a href="https://x.com/testa_beispiel/status/1975000000000000001?ref_src=twsrc%5Etfw">'
    "September 28, 2026</a></blockquote>\n",
    "type": "rich",
    "provider_name": "X",
    "version": "1.0",
}


def test_parse_oembed_gives_verbatim_text():
    post = XPost(
        url="https://x.com/i/status/1975000000000000001", author=None, text="model text", posted_at=None
    )
    out = parse_oembed(OEMBED, post)
    assert out.text_verbatim
    assert out.author == "testa_beispiel"
    assert (
        out.text
        == "Back on snow in Saas-Fee!\n\nFirst downhill training block done & happy. pic.twitter.com/abc"
    )
    assert out.posted_at == datetime(2026, 9, 28, tzinfo=UTC)
    assert out.url == OEMBED["url"]


def test_fetch_oembed_404_keeps_post():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(OEMBED_URL)
        return httpx.Response(404)

    g = GrokXSearch(_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    post = XPost(url="https://x.com/i/status/1", author=None, text=None, posted_at=None)
    assert g.fetch_oembed(post) is post


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("XAI_API_KEY"), reason="needs XAI_API_KEY (paid call)")
def test_live_search_person():
    """Paid call (a few cents). Note: tests/conftest.py blanks XAI_API_KEY, so this only runs when that
    blanking is bypassed on purpose (e.g. running the file with a separate conftest)."""
    g = GrokXSearch(Settings())
    posts = g.search_person("Lara Gut-Behrami", None, date.today().replace(day=1), limit=3)
    assert all(p.url.startswith("https://x.com/") and p.status_id for p in posts)
