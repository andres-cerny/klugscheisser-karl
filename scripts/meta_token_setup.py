"""One-time Meta Graph setup: turn a 1-hour Graph API Explorer token into a lasting one and store it.

Run by Ondrej (never prints a token):
    with-secrets meta -- uv run python scripts/meta_token_setup.py ~/.config/secrets/meta.env

Needs META_APP_ID, META_APP_SECRET, META_SHORT_TOKEN in the environment (see docs/meta-graph-setup.md).
Writes META_GRAPH_TOKEN and META_IG_BUSINESS_USER_ID into the given env file (replacing old values).
Prefers the Page token (usually does not expire); falls back to the ~60-day user token.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

VERSION = os.environ.get("META_GRAPH_API_VERSION", "v24.0")
API = f"https://graph.facebook.com/{VERSION}"
TEST_HANDLE = "swissicehockeywomen"


def get(client: httpx.Client, path: str, **params) -> dict:
    r = client.get(f"{API}/{path}", params=params)
    data = r.json()
    if r.status_code >= 400:
        err = data.get("error") or {}
        sys.exit(f"Meta error on {path.split('?')[0]}: code {err.get('code')} – {err.get('message')}")
    return data


def works(client: httpx.Client, token: str, ig_id: str) -> bool:
    r = client.get(
        f"{API}/{ig_id}",
        params={
            "fields": f"business_discovery.username({TEST_HANDLE}){{username,media_count}}",
            "access_token": token,
        },
    )
    return r.status_code == 200 and "business_discovery" in r.json()


def expiry(client: httpx.Client, token: str, app_token: str) -> str:
    d = get(client, "debug_token", input_token=token, access_token=app_token).get("data") or {}
    ts = d.get("expires_at") or 0
    return "never" if ts == 0 else datetime.fromtimestamp(ts, UTC).date().isoformat()


def write_env(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    lines = [ln for ln in lines if ln.split("=", 1)[0] not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        app_id, secret, short = (
            os.environ[k] for k in ("META_APP_ID", "META_APP_SECRET", "META_SHORT_TOKEN")
        )
    except KeyError as exc:
        sys.exit(f"missing {exc.args[0]}: add it with add-secret meta {exc.args[0]}")
    app_token = f"{app_id}|{secret}"
    with httpx.Client(timeout=30) as c:
        long_user = get(
            c,
            "oauth/access_token",
            grant_type="fb_exchange_token",
            client_id=app_id,
            client_secret=secret,
            fb_exchange_token=short,
        )["access_token"]
        pages = (
            get(
                c,
                "me/accounts",
                fields="id,name,access_token,instagram_business_account",
                access_token=long_user,
            ).get("data")
            or []
        )
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            sys.exit(
                f"{len(pages)} Page(s) found, none linked to an Instagram professional account (setup step 2)."
            )
        page = linked[0]
        ig_id = page["instagram_business_account"]["id"]
        print(f"Page: {page['name']} -> Instagram account id {ig_id}")
        for label, token in (("Page token", page["access_token"]), ("user token", long_user)):
            if works(c, token, ig_id):
                write_env(
                    Path(sys.argv[1]).expanduser(),
                    {"META_GRAPH_TOKEN": token, "META_IG_BUSINESS_USER_ID": ig_id},
                )
                print(
                    f"OK: {label} works (test @{TEST_HANDLE}), expires: {expiry(c, token, app_token)}. Saved."
                )
                return
            print(f"{label}: business_discovery test failed")
    sys.exit("Neither token can read @swissicehockeywomen: check the app permissions (setup step 4).")


if __name__ == "__main__":
    main()
