# Perplexity: custom remote connector

Status 2026-10-03. Claude (claude.ai custom connector) is tested and works. Perplexity is not tested yet;
this guide is based on Perplexity's help center plus third-party reports. Items marked *(unverified)* still
need a real test.

Sources: [Adding Custom Remote Connectors](https://www.perplexity.ai/help-center/en/articles/13915507-adding-custom-remote-connectors),
[Troubleshooting connectors](https://www.perplexity.ai/help-center/en/articles/2026091802-troubleshooting-connectors),
[What are Projects?](https://www.perplexity.ai/help-center/en/articles/10352961-what-are-projects).

## Requirements

- Perplexity Pro, Max or Enterprise. Use the website or the desktop app (the phone app may not show custom connectors).
- The server must be reachable over HTTPS. For testing use the Cloudflare quick tunnel (below); for real use a
  stable domain (named Cloudflare Tunnel or a hosted server).
- The MCP access token from `.env` (`MCP_AUTH_TOKEN`).

## Setup (for the moderator)

1. Open <https://www.perplexity.ai/account/connectors> (profile → Settings → Connectors).
2. Click **+ Custom connector** → **Remote**.
3. **Name:** `Athlete Brief` (short; one user reported problems with dashes).
4. **MCP Server URL:** `https://<tunnel-or-domain>/mcp`
5. **Advanced:**
   - **Authentication:** **API Key** → paste only the token (no "Bearer", no spaces or line breaks).
   - **Transport:** **Streamable HTTP**.
6. Tick the risk acknowledgement → **Add**. Click the card and make sure it is enabled.
7. If saving fails with `MCP server returned HTTP 401`: edit the connector, set **Authentication: None** and use
   `https://<tunnel-or-domain>/mcp?token=<token>` as the URL. (The server accepts both.)

### Use it in a Project (formerly "Space")

1. Create a Project, e.g. "Eishockey Frauen".
2. Project **Settings → Connectors**: select `Athlete Brief` (re-authorise if asked; personal connectors are not
   automatically available inside a Project).
3. Project **Instructions**: paste the block from [client-instructions.md](client-instructions.md) (max 8,000 characters).
4. In chats, make sure sources are **not** set to "Web only", otherwise connectors are not called.
   If a normal search ignores the connector, run the question as a **Computer** task *(unverified whether needed on Pro)*.

### After changes

- New or changed tools on the server: remove and re-add the connector (Perplexity caches the tool list).
- New tunnel URL: edit the connector URL, and restart the server with the new host allowed (see below).

## Local test with a Cloudflare quick tunnel

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000          # prints https://<random>.trycloudflare.com
MCP_ALLOWED_HOSTS='["<random>.trycloudflare.com"]' uv run klugscheisser serve
curl https://<random>.trycloudflare.com/healthz          # {"ok": true}
```

The quick-tunnel URL changes on every restart. For daily use set up a named tunnel on your own domain
(`cloudflared tunnel create athlete-brief`, DNS route, fixed hostname). Perplexity also supports Cloudflare
Access service tokens (`CF-Access-Client-Id` / `CF-Access-Client-Secret`) as an extra network layer.

## What the server does for Perplexity compatibility

| Behaviour | Why |
|---|---|
| Token accepted as `Authorization: Bearer`, `X-API-Key` or `?token=` | Perplexity does not document which header its "API Key" option sends (most likely Bearer). |
| Token compared after trimming whitespace, in constant time | Pasted keys with trailing newlines broke other servers. |
| `/.well-known/*` answers plain `404` without auth | Avoids pushing Perplexity into an OAuth / dynamic-registration flow we don't offer. |
| Stateless Streamable HTTP, JSON responses | Session-affinity errors (HTTP 419) hit other servers behind load balancers. |
| No Origin required; Perplexity origins on the allowlist | Calls come server-to-server from datacenter IPs. |
| Compact event view (~17 KB), text content alongside structured content | Response size limits are undocumented. |
| Access log with method, path, status, host, origin, user agent, auth header *name*, client IP; query token redacted | To learn what Perplexity actually sends on the first real test. |

## Observed in the first real test (2026-10-03)

- Registration works with **Authentication: API Key** + **Streamable HTTP** on a trycloudflare.com URL.
- Perplexity sends the key as `Authorization: Bearer <key>`, no Origin header, user agent `Perplexity-MCP/1.0`,
  from AWS datacenter IPs (e.g. 44.221.x.x). It does not probe `/.well-known/*` when API Key is selected.
- A normal search question ("Ich gehe an die Frauen-WM …") did **not** call the connector: Perplexity answered from
  web search and assumed football. The connector must be enabled for the chat/Project, and questions should say
  "Eishockey" (or the Project instructions set that context).

## Still open

- Are custom connectors used in normal Search on Pro, or only in Computer tasks?
- Per-call timeout and response size limit.
