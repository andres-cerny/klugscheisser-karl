from starlette.testclient import TestClient

from klugscheisser.config import Settings
from klugscheisser.mcp_server.server import build_app

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "mcp-protocol-version": "2025-06-18",
}
CALL_STATUS = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "status", "arguments": {}},
}


def test_features_off_without_keys():
    s = Settings(_env_file=None, apify_token="", typesafe_api_key=None)
    assert not any(s.features.values())


def test_feature_on_with_key():
    s = Settings(_env_file=None, apify_token="x")
    assert s.features["apify"] is True


def test_auth_rejects_missing_and_wrong_token(db):
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.post("/mcp", json=CALL_STATUS, headers=HEADERS).status_code == 401
        bad = {**HEADERS, "Authorization": "Bearer nope"}
        assert c.post("/mcp", json=CALL_STATUS, headers=bad).status_code == 401
        assert c.get("/healthz").status_code == 200


def test_status_tool_with_header_and_query_token(db):
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        r = c.post("/mcp", json=CALL_STATUS, headers={**HEADERS, "Authorization": "Bearer secret"})
        assert r.status_code == 200
        result = r.json()["result"]["structuredContent"]
        assert result["quelle"].startswith("klugscheisser_athlete")
        assert result["counts"]["athletes"] == 0
        assert c.post("/mcp?token=secret", json=CALL_STATUS, headers=HEADERS).status_code == 200


def test_foreign_host_rejected(db):
    app = build_app(Settings(_env_file=None))
    with TestClient(app, base_url="http://evil.example.com") as c:
        assert c.post("/mcp", json=CALL_STATUS, headers=HEADERS).status_code in (400, 421)


def test_x_api_key_and_whitespace_tolerated(db):
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        assert c.post("/mcp", json=CALL_STATUS, headers={**HEADERS, "X-API-Key": "secret"}).status_code == 200
        assert (
            c.post(
                "/mcp", json=CALL_STATUS, headers={**HEADERS, "Authorization": "Bearer secret "}
            ).status_code
            == 200
        )


def test_oauth_discovery_paths_are_plain_404(db):
    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/.well-known/mcp-connector.json",
        ):
            r = c.get(path)
            assert r.status_code == 404 and "www-authenticate" not in r.headers


def test_access_log_redacts_query_token(db, caplog):
    import logging

    app = build_app(Settings(_env_file=None, mcp_auth_token="secret"))
    with (
        caplog.at_level(logging.INFO, logger="klugscheisser.access"),
        TestClient(app, base_url="http://127.0.0.1:8000") as c,
    ):
        c.post("/mcp?token=secret", json=CALL_STATUS, headers=HEADERS)
    assert "token=***" in caplog.text and "secret" not in caplog.text


def test_perplexity_origin_and_tunnel_host_allowed(db):
    app = build_app(Settings(_env_file=None, mcp_allowed_hosts=["abc.trycloudflare.com"]))
    with TestClient(app, base_url="https://abc.trycloudflare.com") as c:
        r = c.post("/mcp", json=CALL_STATUS, headers={**HEADERS, "Origin": "https://www.perplexity.ai"})
        assert r.status_code == 200
        r = c.post("/mcp", json=CALL_STATUS, headers={**HEADERS, "Origin": "https://evil.example"})
        assert r.status_code == 403
