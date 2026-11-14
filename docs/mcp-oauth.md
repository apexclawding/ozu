# OAuth for the MCP server

`ozu mcp --http` has always taken one static bearer token (`OZU_MCP_TOKEN`).
That works for any client that can send a header (Claude Code, scripts), but
claude.ai custom connectors, on the web and in the phone app, sign in only by
OAuth. This document is the design, the threat model and the runbook for the
optional OAuth mode that lets them in. It is written before the code, and it
is kept accurate with it: `src/ozu/mcp_oauth.py` and `tests/test_mcp_oauth.py`.

**OAuth is off unless configured.** With `OZU_MCP_OAUTH` unset, nothing in
this document exists: no extra route, no state file, and the static bearer
behaves exactly as before.

## What was verified, and where

Checked on 2026-11-14 against the sources below, not from memory.

| Claim | Source |
|---|---|
| MCP servers MUST serve RFC 9728 protected-resource metadata (PRM) and advertise it in the 401's `WWW-Authenticate: Bearer resource_metadata=...` | MCP spec 2025-11-25, *Authorization* (modelcontextprotocol.io/specification/2025-11-25/basic/authorization) |
| Clients MUST use PKCE S256 and refuse an AS whose metadata lacks `code_challenge_methods_supported` | same |
| Clients MUST send RFC 8707 `resource`; servers MUST accept only tokens issued for themselves | same |
| AS MUST match redirect URIs exactly; MUST show the redirect host on consent; DCR is a MAY, kept "for backwards compatibility" | same |
| AS MUST rotate refresh tokens for public clients; SHOULD issue short-lived access tokens | same |
| Insufficient scope SHOULD be a 403 with `error="insufficient_scope"` | same |
| claude.ai (web, Desktop, mobile) redirects to `https://claude.ai/api/mcp/auth_callback`; Anthropic says it may move to `https://claude.com/api/mcp/auth_callback`, so allow both | claude.com/docs/connectors/building/authentication; support.claude.com article 11503834 |
| Claude Code uses a loopback redirect on an ephemeral port | claude.com/docs/connectors/building/authentication |
| Claude registers by DCR as a **public** client (`token_endpoint_auth_method: none`) unless CIMD or Anthropic-held credentials are set up | same |
| Claude sends S256 PKCE on every request, requests the PRM's `scopes_supported` (or the 401's `scope`), refreshes on 401 and up to 5 min before expiry, expects `invalid_grant` for a dead refresh token and the rotated refresh token in the same response | same |
| The PRM `resource` must equal **exactly** the URL the user types into Claude, path included | same |
| Discovery, registration and token endpoints get a 10 s budget; refresh 30 s | same |
| Anthropic's egress is `160.79.104.0/21` | same |
| GitHub OAuth apps accept PKCE (S256 only) since 2025-07-14 | github.blog/changelog/2025-07-14-pkce-support-for-oauth-and-github-app-authentication |
| SDK behaviour (handlers, what each endpoint checks, what the provider must check) | the installed `mcp` 2.3.0 source: `mcp/server/auth/{provider,settings,routes}.py`, `handlers/{authorize,token,register,revoke}.py`, `middleware/{bearer_auth,client_auth}.py`, `mcp/shared/auth.py`, `mcp/server/mcpserver/server.py` |

**Not verified:** whether claude.ai sends `resource` on the authorization
request (Anthropic's page does not say; the spec says it MUST, so a missing
`resource` is accepted and bound to this server); whether claude.ai checks
the RFC 9207 `iss` parameter (it is sent anyway); the real connector flow
end to end (that needs the public endpoint open, which is a human step, see
*Turning it on*).

### What the SDK does and what it leaves to us

From the `mcp` 2.3.0 source:

- `/authorize` (GET or POST): validates the request shape, requires
  `code_challenge` and only accepts `S256`, loads the client, matches
  `redirect_uri` **exactly** against the registered list, checks scopes
  against the client's registered scope, then calls `provider.authorize()`
  and 302s to whatever URL it returns. An unknown client or a bad redirect
  is a 400 JSON body, never a redirect.
- `/token`: authenticates the client (`none`, `client_secret_post` or
  `client_secret_basic`; secrets compared with `hmac.compare_digest`), checks
  the grant type is registered, then for a code: client match, expiry,
  `redirect_uri` equal to the one used at `/authorize`, PKCE S256. It does
  **not** consume the code, and does **not** pass the token request's
  `resource` to the provider: single use and audience are the provider's job.
  For a refresh: client match, expiry, scope subset; rotation is the
  provider's job.
- `/register`: parses RFC 7591 metadata, refuses `private_key_jwt`, refuses
  scopes outside `valid_scopes`, mints a UUID client id and, unless the
  method is `none`, a secret. It has no rate limit and no redirect policy:
  both are the provider's job, in `register_client()`.
- `/revoke`: client-authenticated, looks the token up, calls
  `revoke_token()` if the token belongs to that client.
- Bearer middleware: calls the verifier, refuses an expired token, and with
  `validate_token_resource=True` refuses a token whose `resource` is not
  `resource_server_url`. `required_scopes` is checked on every `/mcp` request
  (403 `insufficient_scope` otherwise).
- `custom_route()` routes are mounted **without** authentication, by design;
  the GitHub callback and the consent page are custom routes.
- One server takes either an `auth_server_provider` or a `token_verifier`,
  not both. So in OAuth mode the provider's `load_access_token()` also
  answers for the static bearer.

## The flow

```
 Claude                Ozu (this server)                    GitHub
   |  POST /mcp (no token)  |                                  |
   |<-- 401 + resource_metadata                                |
   |  GET PRM, AS metadata  |                                  |
   |  POST /register ------>|  (grants nothing)                |
   |  browser: /authorize?client_id,redirect_uri,               |
   |     code_challenge(S256),state,scope,resource              |
   |----------------------->|  checks -> pending authorization  |
   |<-- 302 /oauth/start?txn                                    |
   |  /oauth/start -------->|  sets browser cookie             |
   |<-- 302 github.com/login/oauth/authorize?state=S,PKCE ----->|
   |                        |<-- /oauth/github/callback?code,S  |
   |                        |  POST access_token (secret, verifier)
   |                        |  GET /user -> id, login           |
   |                        |  id == pinned id AND login == pinned login?
   |                        |  DELETE the GitHub token (best effort)
   |<-- consent page (client name, client id, redirect host + URI, scope)
   |  POST /oauth/consent (approve) ->| code (single use, 120 s)|
   |<-- 303 redirect_uri?code,state,iss                         |
   |  POST /token (code, verifier) -->| access 1 h + refresh    |
   |  POST /mcp  Bearer access ------>| read tools only         |
```

GitHub is used only to prove who is at the browser. Ozu requests **no
GitHub scope** (an empty `scope`, which still lets `/user` return the public
profile with the numeric `id`), sends PKCE to GitHub as well, never stores
the GitHub token, and deletes it at GitHub right after the identity check
(best effort: a failure there is logged and does not block the flow).

## Endpoints in OAuth mode

All are served by the one process on loopback and reach the internet only
through the reverse proxy. Every response from the routes Ozu adds carries
`Cache-Control: no-store`; the HTML pages also carry
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a CSP with
`default-src 'none'; frame-ancestors 'none'` and `form-action` limited to
this origin plus the one redirect origin of that authorization.

| Path | Auth | Returns | On failure |
|---|---|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | none | PRM: `resource` = `<public>/mcp`, `authorization_servers` = [`<public>`], `scopes_supported` = [`vault:read`] | n/a |
| `GET /.well-known/oauth-authorization-server` | none | RFC 8414 metadata (SDK): S256 only, `registration_endpoint`, `revocation_endpoint` | n/a |
| `POST /register` | none | 201 client info (SDK) | 400 `invalid_redirect_uri` (not on the allowlist), 400 `invalid_client_metadata` (too many URIs, name too long, scope other than `vault:read`, registration limit reached) |
| `GET/POST /authorize` | none | 302 to `/oauth/start?txn=...` | 400 JSON for unknown client / redirect not registered (never redirects); otherwise a redirect to the client with `invalid_scope`, `invalid_target` (resource is not this server), `temporarily_unavailable` (too many pending authorizations) |
| `GET /oauth/start?txn=` | none | sets the binding cookie, 302 to GitHub | 400 page "unknown or expired authorization" |
| `GET /oauth/github/callback` | none | consent page (200) | 400 page for a missing/unknown/expired `state` or a missing cookie; 403 page "this GitHub account may not approve access" (pending deleted); 502 page "GitHub could not be reached; nothing was granted" (pending deleted); GitHub's own `error=` is answered with a 400 page and the client gets nothing |
| `POST /oauth/consent` | cookie + consent token | approve: 303 to `redirect_uri?code&state&iss`; deny: 303 to `redirect_uri?error=access_denied&state&iss` | 400 page on anything missing, expired or not matching; nothing is issued |
| `POST /token` | client auth per registration (SDK) | 200 `{access_token, refresh_token, expires_in, scope}` | 400 `invalid_grant` (unknown, expired, replayed, wrong verifier, revoked), 400 `invalid_request` (redirect mismatch), 401 `invalid_client` |
| `POST /revoke` | client auth (SDK) | 200 always (RFC 7009) | 401 for a bad client |
| `POST/GET/DELETE /mcp` | Bearer | MCP | 401 + `WWW-Authenticate` with `resource_metadata`; 403 `insufficient_scope` |

No error body or page ever carries a token, a code, a client secret, the
GitHub app secret or a GitHub response body.

## Decisions

**Opaque tokens, stored hashed, in SQLite — not JWTs.** One user, one box:
nothing needs to verify a token without asking this process, and revocation
has to be instant and certain. Every code, access token and refresh token is
`secrets.token_urlsafe(32)` (256 bits) with a short prefix (`ozua_`, `ozur_`,
`ozuc_`) so a leaked one is recognisable by secret scanners; only its
SHA-256 is stored. A stolen copy of the state file therefore yields no usable
token. The file is `<OZU_HOME>/mcp-oauth.db`, created `0600`, separate from
`ozu.db` (which is a rebuildable index and must stay so). No signing key
exists, so there is none to leak or rotate. Exception: DCR client secrets
are stored as issued, because the SDK's client authenticator compares the
stored value; claude.ai registers as a public client and gets none, and a
client secret alone grants nothing without a code and its PKCE verifier.

**No new dependency.** The SDK supplies the OAuth endpoints; the HTTP
client the SDK itself depends on (`httpx2` from mcp 2.x, an API-compatible
continuation of `httpx`; `httpx` if that is what is installed) talks to GitHub; `sqlite3`, `secrets`, `hashlib` and
`html` do the rest. OAuth mode needs the SDK 2.x provider API (it checks for
it and refuses to start otherwise); the static bearer still works on 1.x.

**Lifetimes.** Pending authorization (from `/authorize` to consent): 10 min.
Authorization code: 120 s, single use. Access token: 1 h. Refresh token:
30 days, rotated on every use, and every grant (a *family*: one consent's
chain of access and refresh tokens) ends 90 days after consent whatever the
refreshes, so the owner signs in with GitHub again at least quarterly.

**Replay and reuse.** A code presented twice revokes the whole family it
produced (RFC 6749 §4.1.2). A refresh token presented after it was rotated
revokes the whole family (OAuth 2.1 refresh-token reuse detection): either
the client or a thief holds a stale token, and the safe answer is to make
both sign in again. Cost: two concurrent refreshes by the same client also
kill the family; the client then re-authorizes.

**DCR policy: registration grants nothing.** Anyone can register, so a
registered client is just a name and a redirect list. It can obtain a code
only through a consent that the pinned GitHub account approves in a browser.
On top of that:

- redirect URIs must be on an allowlist, matched exactly:
  `OZU_MCP_OAUTH_REDIRECT_URIS` (default: the two claude.ai/claude.com
  callbacks); loopback `http://127.0.0.1|localhost|[::1]:<port>/...` only
  with `OZU_MCP_OAUTH_ALLOW_LOOPBACK=1` (Claude Code). The allowlist is
  checked at registration and again at `/authorize`. This is what closes the
  open-redirect and most of the consent-phishing surface: a code can only be
  delivered to Claude (or to the owner's own machine).
- at most 4 redirect URIs, 512 characters each, client name 100 characters;
  the SDK caps the request body.
- a client never used to obtain a token is forgotten 1 hour after it
  registered; at most 10 registrations per hour and 50 live clients in all
  (global counters: there is one user, and the proxy's client IP is not
  trusted). A flood can therefore block a new registration for an hour, and
  nothing more.
- scope is `vault:read` and nothing else.

Client ID Metadata Documents are not supported in v1: they make the server
fetch an arbitrary URL (SSRF on a box that serves other loopback services),
and claude.ai falls back to DCR when the metadata does not advertise them.

**Identity: one GitHub account, pinned by id and login.** The callback
accepts the GitHub user only if its numeric `id` equals
`OZU_MCP_GITHUB_USER_ID` **and** its `login` equals `OZU_MCP_GITHUB_LOGIN`
(case-insensitive). The id is what matters: logins can be renamed and a freed
login can be taken by someone else. Requiring both means a rename stops
OAuth (fail closed) instead of silently following the account.

**Scopes and what an OAuth token may call.** Two scopes exist:

- `ozu` — the static bearer's scope, all tools including `capture`. It is
  never issued over OAuth (registration and `/authorize` accept only
  `vault:read`).
- `vault:read` — `search_notes`, `get_note`, `graph_neighbors`, `timeline`,
  `open_actions`. `capture` writes to the vault and is refused.

Enforcement is at **call time, server side**: every tool is wrapped when it
is registered, and the wrapper checks the scopes of the request's access
token (from the SDK's auth context) before the tool body runs. `capture`
requires `ozu`; the read tools require `vault:read` or `ozu`. If the server
was built with auth and a call somehow arrives without a token, the wrapper
refuses (fail closed). `tools/list` also hides `capture` from an OAuth
session, but that is cosmetic; the wrapper is the control. A refused call is
a tool error ("requires scope ozu"), not an HTTP 403, because the MCP session
is already authorized for its read tools. In OAuth mode `required_scopes` is
`vault:read`, and the static bearer carries both `ozu` and `vault:read`, so
it passes the same middleware and keeps its full tool set.

**The wall is untouched.** OAuth adds no tool and no path into the data: an
OAuth call runs the very same tool bodies after the scope check, so
`_vault_ok` and `_label_served` apply exactly as over the bearer. (The
`OZU_ALLOW_WORK_MCP` opt-in therefore applies to OAuth sessions too, exactly
as to the bearer; leave it off.)

**Audience.** Tokens are issued for `<public>/mcp`. A `resource` on the
authorization request must be that URL or `<public>` itself (trailing slash
ignored), or the request is refused with `invalid_target`; a missing one is
bound to `<public>/mcp`. The verifier sets the token's resource and the
server runs with `validate_token_resource=True`, so the SDK also refuses any
token not issued for this resource.

**Cookie binding and CSRF.** `/oauth/start` sets a random nonce in an
HttpOnly, SameSite=Lax cookie (`__Host-` prefixed and `Secure` when the
public URL is https). The GitHub `state` is a separate random value, bound to
the pending authorization; the callback needs both the `state` and the
cookie of the browser that started the flow, and the consent POST needs the
cookie, the flow's `state` and a one-time consent token embedded in the page
(the pending row is deleted as the POST is accepted, so a consent cannot be
replayed). Only hashes of these values are stored; the PKCE verifier sent to
GitHub is kept as is until the callback, then cleared.

**Mix-up.** There is one authorization server, it names itself as `issuer`,
and the final redirect carries `iss=<public>` (RFC 9207).

**Logs.** One audit line per event, never a value that grants anything:
registered client (id, name, redirect hosts); GitHub identity accepted or
refused (GitHub id and login); consent approved or denied; grant issued (family id, client id, client
name, scope, expiry); refreshed; revoked; replay or reuse detected. uvicorn's
access log would print query strings (the GitHub `code`, `state`, the
`txn`), so in OAuth mode a filter replaces every query string in it with
`?…`.

**GitHub down.** Any failure talking to GitHub (connection, timeout, non-200,
a body without an integer `id`) ends that authorization with a 502 page and
issues nothing. Already issued tokens keep working until they expire (they
do not depend on GitHub); a refresh does not call GitHub either.

### SDK behaviour worth knowing

- The SDK's `TokenError`, `AuthorizeError` and `RegistrationError` are frozen
  dataclasses; `contextlib` cannot re-raise them through a `with` block (it
  assigns `__traceback__`). Every raise in the provider is therefore outside
  its database transaction.
- `/revoke`'s request model declares `client_secret` without a default, so a
  public client must send the field, empty, or get a 400. That is the SDK's,
  not ours; revocation from the CLI does not depend on it.
- Hiding `capture` from `tools/list` relies on the 2.x server calling
  `self.list_tools()`; on 1.x it would still be listed, and still refused.

## Threat model

Assets: the vault's personal notes (read), the ability to write a note
(`capture`), the walled vault (never served). Adversary: anyone on the
internet, including someone who knows this server's URL, can register
clients, and can send the owner links.

| Threat | Control |
|---|---|
| Someone calls `/mcp` without a token | 401 (SDK middleware); unchanged from the bearer mode |
| Stolen access token | 1 h lifetime, audience-bound, stored hashed; `ozu mcp tokens revoke` kills it at once; read-only scope |
| Stolen refresh token | rotation + reuse detection revokes the family; 90-day family cap; revocable from the CLI |
| Stolen state file | only hashes of tokens and codes; no signing key |
| Authorization-code interception | PKCE S256 mandatory (SDK), code single use, 120 s, replay revokes the family; code delivered only to allowlisted redirect URIs |
| Open redirect | exact redirect match (SDK) + allowlist at registration and at `/authorize`; unknown client or bad redirect is a 400 page, never a redirect |
| CSRF / login CSRF on the GitHub round trip | `state` bound to the pending authorization, plus the browser cookie, plus a one-time consent token on the POST |
| Consent phishing (attacker registers "Claude" and sends the owner a link) | the consent page names the client, its id, the redirect **host** in large type and the full URI; only allowlisted redirects exist; only the pinned GitHub account can approve |
| Someone else's GitHub account | id and login must both match the pinned values; anyone else gets a 403 page and nothing is stored |
| GitHub login renamed or reused | the numeric id is pinned |
| DCR abuse (flooding, huge metadata, odd redirects) | registration grants nothing; allowlist; size caps; 1 h expiry of unused clients; 10/hour and 50 total |
| Pending-authorization flooding | 30 per 10 min, 10 min expiry |
| Clickjacking the consent page | `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| Mix-up | single AS, `iss` in the redirect |
| `capture` reached over OAuth | scope never issuable; call-time check in the tool wrapper |
| Walled content over OAuth | same tool bodies, same `_vault_ok` / `_label_served`; tested |
| Secrets in logs or error bodies | no value that grants anything is logged; access-log query strings stripped; tested on captured logs and bodies |
| Half-configured server | refuses to start, naming the missing settings (never their values) |
| Token passthrough | Ozu never forwards a client token anywhere; the GitHub token is its own and is deleted |

Out of scope: an attacker with shell access as the user running Ozu (they
can read `.env` anyway), and a compromised claude.ai account (it holds the
tokens by design; revoke from the CLI).

## Configuration

| Setting | Meaning |
|---|---|
| `OZU_MCP_OAUTH` | `1` turns OAuth on; unset, empty or `0` is off; anything else refuses to start |
| `OZU_MCP_PUBLIC_URL` | **required**, the origin clients dial, e.g. `https://ozu.example.com`; https, or http on loopback for tests; no path |
| `OZU_MCP_GITHUB_CLIENT_ID` / `OZU_MCP_GITHUB_CLIENT_SECRET` | **required**, the GitHub OAuth app |
| `OZU_MCP_GITHUB_LOGIN` | **required**, the one GitHub login allowed |
| `OZU_MCP_GITHUB_USER_ID` | **required**, its numeric id (digits only) |
| `OZU_MCP_OAUTH_REDIRECT_URIS` | optional, comma-separated exact redirect URIs; default the claude.ai and claude.com callbacks |
| `OZU_MCP_OAUTH_ALLOW_LOOPBACK` | optional, `1` also allows loopback redirects (Claude Code) |
| `OZU_MCP_GITHUB_URL` / `OZU_MCP_GITHUB_API_URL` | optional, default `https://github.com` / `https://api.github.com`; https or loopback only (tests, GitHub Enterprise) |

`OZU_MCP_TOKEN` stays required for `--http` in both modes, and the static
bearer keeps working unchanged.

**Two settings move together, as before:** `OZU_MCP_PUBLIC_URL` is the
issuer, the base of the PRM's `resource` (`<public>/mcp`), the GitHub
callback (`<public>/oauth/github/callback`) and the allowed-Host list of
`transport_security()`. If it does not match the proxy's public name, the SDK
answers 421 to authenticated requests (and only to those), and the GitHub
app's callback URL no longer matches either.

## What the reverse proxy must forward

Everything under the public name, unchanged, to the loopback port:
`/mcp`, `/.well-known/oauth-protected-resource/mcp`,
`/.well-known/oauth-authorization-server`, `/authorize`, `/token`,
`/register`, `/revoke`, `/oauth/start`, `/oauth/github/callback`,
`/oauth/consent`. It must pass the `Host` header as received (Caddy does)
and must not cache. `deploy/caddy-mcp.conf` forwards the whole site, so it
needs no new rule; do not add basic auth in front of it.

## Turning it on

1. **Create a GitHub OAuth app** (GitHub → Settings → Developer settings →
   OAuth Apps → New OAuth App), under the account that will sign in.
   Homepage URL: `https://ozu.example.com`. Authorization callback URL:
   `https://ozu.example.com/oauth/github/callback` (your public URL plus
   that path, exactly). Leave device flow off. Generate a client secret.
2. **Find the numeric user id:** `curl -s https://api.github.com/users/<login>`
   and read `"id"` (or `gh api user --jq .id` when signed in as that account).
3. **Set, in `<OZU_HOME>/.env`** (no inline comments, file mode 600):
   `OZU_MCP_OAUTH=1`, `OZU_MCP_PUBLIC_URL=https://ozu.example.com`,
   `OZU_MCP_GITHUB_CLIENT_ID=...`, `OZU_MCP_GITHUB_CLIENT_SECRET=...`,
   `OZU_MCP_GITHUB_LOGIN=<login>`, `OZU_MCP_GITHUB_USER_ID=<digits>`.
   Add `OZU_MCP_OAUTH_ALLOW_LOOPBACK=1` only if Claude Code will sign in by
   OAuth too (it can keep using the static bearer instead).
4. **Restart the server** (e.g. `systemctl --user restart ozu-mcp`) and read
   its log: it refuses to start, naming what is missing, if anything is.
5. **Proxy:** install `deploy/caddy-mcp.conf` with your name, then
   `sudo caddy validate --config /etc/caddy/Caddyfile` **before**
   `sudo systemctl reload caddy`.
6. **Check from another machine:**
   - `curl -si https://ozu.example.com/mcp -X POST` → `401` with
     `WWW-Authenticate: Bearer ... resource_metadata="https://ozu.example.com/.well-known/oauth-protected-resource/mcp"`.
   - `curl -s https://ozu.example.com/.well-known/oauth-protected-resource/mcp`
     → `"resource":"https://ozu.example.com/mcp"`.
   - `curl -s https://ozu.example.com/.well-known/oauth-authorization-server`
     → `"code_challenge_methods_supported":["S256"]` and a `registration_endpoint`.
   - **The 421 pitfall:** these are all unauthenticated, so they succeed
     even when `OZU_MCP_PUBLIC_URL` is wrong. Make one *authenticated* call
     (the static bearer from a trusted machine:
     `curl -si -X POST https://ozu.example.com/mcp -H "Authorization: Bearer $OZU_MCP_TOKEN" -H 'Accept: application/json, text/event-stream' -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'`):
     a 421 means the public URL does not match the Host the proxy forwards.
     Fix the URL; never disable DNS-rebinding protection.
7. **Add the connector in claude.ai:** Settings → Connectors → Add custom
   connector, URL `https://ozu.example.com/mcp` (with `/mcp`: the PRM
   resource must equal what you type), no client id or secret (Claude
   registers itself). Connect: GitHub asks you to sign in or authorize the
   app, then Ozu's consent page names the client and `claude.ai`; approve.
   Connectors added on the web are also available in the phone app.
8. **Confirm:** `ozu mcp tokens list` shows one active grant for "Claude";
   in a chat, ask Claude to search your notes; asking it to capture a note
   is refused.

### Revoking

`ozu mcp tokens list` shows every grant (family id, client, created, last
refreshed, expiry, status), never a token value. `ozu mcp tokens revoke
<family-id>` revokes one grant, `ozu mcp tokens revoke --client <client-id>`
revokes every grant of a client and forgets it, `ozu mcp tokens revoke
--all` revokes everything. Revocation is immediate, without a restart: every
request looks its token up. Removing the connector in claude.ai does not
revoke anything on this side; do both.

### Turning it off

Set `OZU_MCP_OAUTH=0` (or remove it) and restart: the OAuth routes are gone
and every OAuth token is refused (only the static bearer is accepted). Then
close the public route in the proxy (validate, reload) if nothing else uses
it, and delete or revoke the GitHub OAuth app. `mcp-oauth.db` can be deleted;
it is recreated empty when OAuth is next turned on.
