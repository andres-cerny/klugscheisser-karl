# Meta Graph API (Business Discovery) setup

Goal: give `GraphBusinessDiscoverySource` a token and an IG user ID, so it can read
public Instagram professional accounts through the official API.

Checked against Meta docs on 2026-10-03. Sources are listed at the end.

## What our code expects

- File: `src/klugscheisser/collectors/instagram/sources.py`, class `GraphBusinessDiscoverySource`.
- Host and version: `https://graph.facebook.com/<META_GRAPH_API_VERSION>` (default v24.0; Facebook Login
  variant, not `graph.instagram.com`). Latest is v26.0; bump the setting before v24.0 runs out.
- One call per handle:
  `GET /{META_IG_BUSINESS_USER_ID}?fields=business_discovery.username(<handle>){...}&access_token=...`
- Profile fields: `username,name,biography,followers_count,media_count,website`.
- Media fields: `media.limit(N){id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count}`.
- Settings (`src/klugscheisser/config.py`): `META_GRAPH_TOKEN`, `META_IG_BUSINESS_USER_ID`, `INSTAGRAM_SOURCE`.
- `features["graph_business_discovery"]` is ON only when both META values are set.

## Key facts first

- **No App Review needed** for our use. Standard Access (the default, in development mode)
  covers people with a role on the app or on the business portfolio that owns it.
  App Review and Business Verification are only for Advanced Access, i.e. other people's accounts.
- **Permissions** for Business Discovery: `instagram_basic`, `instagram_manage_insights`,
  `pages_read_engagement`. If your Page access comes through Business Manager (always true
  for a System User), also `ads_read` or `ads_management`.
- **Token**: a user token lives 1 hour, a long-lived user token about 60 days.
  A System User token can be non-expiring. That is the one we want.
- **Rate limit**: Business Discovery uses Platform Rate Limits, not the Instagram BUC limit.
  App limit is `200 × daily active users` calls per rolling hour. With one user that is roughly 200 calls/hour.

## Short path (recommended)

Do steps 1, 2 and 3a.1–4 below (account, Page, app, Explorer token), then in your SSH terminal:

1. `add-secret meta META_APP_ID`, `add-secret meta META_APP_SECRET`, `add-secret meta META_SHORT_TOKEN`
   (the Explorer token lives only 1 hour: do this right after generating it).
2. `cd ~/projects/klugscheisser_athlete && with-secrets meta -- uv run python scripts/meta_token_setup.py ~/.config/secrets/meta.env`

The script swaps the token for a long-lived one, finds the Instagram account ID, tests one Business Discovery
call, and saves `META_GRAPH_TOKEN` + `META_IG_BUSINESS_USER_ID` (Page token preferred, it usually never expires).
It prints only OK/not OK and the expiry date, never a token.

## 1. Prerequisites

1. **Instagram professional account** (Creator or Business). Use one you own, e.g. a project account.
   - Instagram app > Settings > Account type and tools > Switch to professional account.
   - Verify: the profile shows "Professional dashboard".
2. **Facebook Page linked to that Instagram account.**
   - In the Page: Settings > Linked accounts > Instagram > Connect. Or from Meta Business Suite.
   - Verify: Page settings show the Instagram account as connected.
   - Your Facebook user must have full control (or at least Insights + Content tasks) on the Page.
3. **Meta developer account.** Ondřej is already registered at developers.facebook.com. Nothing to do.
4. **Business portfolio** (needed only for the System User token in step 3b).
   - https://business.facebook.com/latest/settings > create a portfolio if none exists.
   - Add the Page and the Instagram account as assets: Accounts > Pages / Instagram accounts > Add.
   - Business Verification is not needed for our use.

## 2. Create the app

1. Open https://developers.facebook.com/apps/creation/.
2. App details: name (e.g. `klugscheisser-ig`), contact email. Next.
3. Use case: **Manage messaging & content on Instagram**. Next.
   - Use cases cannot be removed later. Do not add unrelated ones.
4. Business: select your business portfolio (or "I don't want to connect a business portfolio yet"
   if you skip the System User path). Next > Go to dashboard.
5. In the dashboard, open the use case > Customize. Choose the **API setup with Facebook login** variant.
   Add these optional permissions: `instagram_basic`, `instagram_manage_insights`,
   `pages_show_list`, `pages_read_engagement`, `ads_read`, `business_management`.
6. Leave the app in **Development** mode. Do not submit for App Review.
7. App settings > Basic: note the **App ID** and **App Secret** (needed for the token exchange).
   Keep the secret out of chats and git.

Verify: the app dashboard shows the use case and the permissions above with "Ready for testing" / Standard Access.

Why development mode is enough (Instagram Platform overview, "Access levels"):
> "If your app only serves your Instagram professional account or an account you manage, Standard Access is all your app needs."

## 3. Get a token

### 3a. Quick path: user token, then long-lived (≈60 days)

Good for a first test. It expires, so use 3b for real operation.

1. Open the Graph API Explorer: https://developers.facebook.com/tools/explorer/.
2. Right panel: **Meta App** = your app. **User or Page** = User Token.
3. Add permissions: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`,
   `pages_read_engagement`, `ads_read`, `business_management`.
4. Click **Generate Access Token**. In the Facebook dialog, select the Page and the Instagram
   account, then allow.
5. Verify: run `me?fields=id,name` in the Explorer. It returns your user.
   Paste the token into https://developers.facebook.com/tools/debug/accesstoken/ and check the scopes.
6. Exchange it for a long-lived token, in your own terminal (the call includes the App Secret):

```bash
curl -sG "https://graph.facebook.com/v24.0/oauth/access_token" \
  --data-urlencode "grant_type=fb_exchange_token" \
  --data-urlencode "client_id=<APP_ID>" \
  --data-urlencode "client_secret=<APP_SECRET>" \
  --data-urlencode "fb_exchange_token=<SHORT_LIVED_USER_TOKEN>"
```

Response: `{"access_token":"<LONG_LIVED_USER_TOKEN>","token_type":"bearer","expires_in":5183944}`.
`5183944` seconds ≈ 60 days. Renew before it expires; an expired token cannot be exchanged.

### 3b. Recommended: System User token (does not expire)

1. Open https://business.facebook.com/latest/settings.
2. Accounts > **Apps**: make sure your app is in this portfolio (Add > connect an app ID).
3. Users > **System users** > **Add**. Name it `klugscheisser`. Role: Employee is enough.
   - Note: Meta says "Not all businesses have access to system users."
4. Select the system user > **Assign assets**:
   - the Facebook Page (task: Content/Insights, or full control),
   - the Instagram account,
   - the app (Manage app / develop).
5. Click **Generate new token**. Select the app.
   - Token expiration: choose **Never** (Meta now recommends the 60-day option; "Never" is still offered
     unless your business is required to use expiring tokens).
   - Permissions: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`,
     `pages_read_engagement`, `ads_read`, `business_management`.
6. Click **Generate token**. Copy it straight into `.env` (step 6). It is shown only once.
7. Verify: https://developers.facebook.com/tools/debug/accesstoken/ shows "Type: System User",
   "Expires: Never" and the scopes above.

If the app does not appear in the token dialog, the system user has no role on the app (step 4).
If that still fails, use 3a and renew every ~50 days.

## 4. Find the IG Business User ID

Run these with the token from step 3. Replace the placeholders.

```bash
# 4.1 Pages you can access -> note the id of the Page linked to Instagram
curl -sG "https://graph.facebook.com/v24.0/me/accounts" \
  --data-urlencode "fields=id,name,instagram_business_account" \
  --data-urlencode "access_token=<TOKEN>"

# 4.2 If instagram_business_account was missing above, ask the Page directly
curl -sG "https://graph.facebook.com/v24.0/<PAGE_ID>" \
  --data-urlencode "fields=instagram_business_account{id,username}" \
  --data-urlencode "access_token=<TOKEN>"
```

Expected: `"instagram_business_account": {"id": "1784...", "username": "..."}`.
The `1784…` number is `META_IG_BUSINESS_USER_ID`. It is not your Facebook user ID or Page ID.

Verify: `GET /<IG_USER_ID>?fields=username` returns your Instagram username.

## 5. Verify Business Discovery

Same request shape as our code. Uses `--data-urlencode`, so the braces need no escaping.

```bash
curl -sG "https://graph.facebook.com/v24.0/<IG_USER_ID>" \
  --data-urlencode "fields=business_discovery.username(swissicehockeyfederation){username,name,followers_count,media_count,media.limit(3){id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count}}" \
  --data-urlencode "access_token=<TOKEN>"
```

Expected: JSON with `business_discovery.username`, `followers_count` and three items in `media.data`.

Typical errors:

| Error | Meaning |
|---|---|
| `(#10) ... permission` or `(#200)` | A permission is missing. Check scopes in the token debugger. Add `ads_read` if the Page is managed via Business Manager. |
| `(#110) Invalid user id` / `Cannot query users by their username` | The target is not a professional account, or the handle is wrong. |
| `OAuthException code 190` | Token expired or revoked. |
| `code 4` | App rate limit reached. Wait. |

To avoid pasting the token in the terminal, load it from `.env` (after step 6):
`set -a; source .env; set +a`, then use `$META_GRAPH_TOKEN` and `$META_IG_BUSINESS_USER_ID`.

## 6. Put it in `.env`

```dotenv
META_GRAPH_TOKEN=<SYSTEM_USER_OR_LONG_LIVED_TOKEN>
META_IG_BUSINESS_USER_ID=<1784...>
INSTAGRAM_SOURCE=graph
```

`INSTAGRAM_SOURCE=graph` in the block above is only for a first test. For real operation use
`hybrid` (or leave `auto`): see `docs/instagram-sources.md`.

| `INSTAGRAM_SOURCE` | Result |
|---|---|
| `auto` | Hybrid if Apify and both META values are set; else Apify; else Graph; else fixtures. |
| `hybrid` | Graph first for own posts + profiles of professional accounts; the rest in one Apify call; tagged posts (mentions) via Apify. |
| `apify` | Apify only (old behaviour, the rollback). |
| `graph` | Graph only: own posts of professional accounts. No tagged posts. |
| `fixture` | Offline test data. |

## 7. Limits

- **Professional accounts only.** Personal (private or public) accounts are not visible.
  Age-gated accounts return nothing.
- **Own posts only.** No tagged posts, no stories, no comments text. Only public fields.
- **No direct media access.** A `GET` on a returned media ID fails. `media_url` is omitted for reels
  with licensed music or with downloads turned off.
- **Rate limit.** Platform Rate Limits: `200 × daily active users` calls per rolling hour for the app.
  We have one user, so plan for roughly 200 calls/hour. Our code makes one call per handle and page (25 posts), capped by `META_GRAPH_MAX_CALLS_PER_RUN` (150/hour).
  The `X-App-Usage` response header shows usage in percent. Error code 4 means throttled.
- **Token expiry.** User token: 1 hour. Long-lived user token: ~60 days, renew before expiry.
  System User token: never, unless revoked, the system user loses the Page, or the password/role changes.
- **Expired token.** Error 190 is logged as an error ("Meta token expired") and the hybrid source sends
  every handle to Apify for the rest of the hour, so costs go back to the old level until the token is renewed.
- **API version.** Setting `META_GRAPH_API_VERSION` (default v24.0). Bump before it is retired.
- **Privacy.** Never paste the token or App Secret into chats, issues, commits or screenshots.
  It goes only into `.env` (git-ignored). If it leaks: Business Settings > System users >
  Revoke tokens, then generate a new one.

## 8. What Claude does once it is in `.env`

Claude never reads or prints `.env` values. After you say "done":

1. `uv run klugscheisser features` → expect `graph_business_discovery   ON`.
2. Restart the server (settings are read once at start): stop it, then `uv run klugscheisser serve`.
3. Test fetch for one athlete, no news:
   `uv run klugscheisser ingest --athlete "<name>" --no-news --max-posts 3`
   Expect `instagram source: graph` and a non-zero post count for an athlete with a professional account.
4. If the count is 0, run the step-5 curl with `$META_GRAPH_TOKEN` from `.env` to see the real error.

## Sources

- Business Discovery guide: https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery
- IG User Business Discovery reference (permissions): https://developers.facebook.com/documentation/instagram-platform/instagram-graph-api/reference/ig-user/business_discovery
- Instagram Platform overview (access levels, App Review, rate limits): https://developers.facebook.com/docs/instagram-platform/overview
- Get started with Instagram API with Facebook Login (Page → IG user ID): https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/get-started
- Create an app (use cases): https://developers.facebook.com/docs/development/create-an-app
- Long-lived access tokens: https://developers.facebook.com/docs/facebook-login/guides/access-tokens/get-long-lived
- System user tokens: https://developers.facebook.com/docs/business-management-apis/system-users/install-apps-and-generate-tokens
- Add system users (Business Suite UI): https://www.facebook.com/business/help/503306463479099
- Rate limits: https://developers.facebook.com/docs/graph-api/overview/rate-limiting
- Graph API versions: https://developers.facebook.com/docs/graph-api/changelog
- Tools: Graph API Explorer https://developers.facebook.com/tools/explorer/ ,
  Access Token Debugger https://developers.facebook.com/tools/debug/accesstoken/
