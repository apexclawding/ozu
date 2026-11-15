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
   |  (client id + secret pre-registered with `ozu mcp client create`;   |
   |   or POST /register while registration is open: grants nothing)       |
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
through the reverse proxy. In OAuth mode the process serves an outer layer,
`OAuthEdge`, in front of the SDK's app (it serves the AS metadata, gates
`/register` and fences the static bearer, below). Every route Ozu adds has
the SDK's own body cap in front of it (16 KB, `MAX_BODY`), which refuses on
a declared `Content-Length` before reading and stops reading a streamed body
at the cap; the SDK caps its own routes (4 MB). Every response from the
routes Ozu adds carries `Cache-Control: no-store`; the HTML pages also carry
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and a CSP with
`default-src 'none'; frame-ancestors 'none'` and `form-action` limited to
this origin plus the one redirect origin of that authorization.

| Path | Auth | Returns | On failure |
|---|---|---|---|
| `GET /.well-known/oauth-protected-resource/mcp` | none | PRM: `resource` = `<public>/mcp`, `authorization_servers` = [`<public>`], `scopes_supported` = [`vault:read`] | n/a |
| `GET /.well-known/oauth-authorization-server` | none | RFC 8414 metadata (built by the SDK's builder, served by `OAuthEdge`, `no-store`): S256 only, `revocation_endpoint`, `authorization_response_iss_parameter_supported: true`; `registration_endpoint` **only while registration is open** | n/a |
| `POST /register` | none | 201 client info (SDK), only while registration is open | **404 while closed (the default)**; 400 `invalid_redirect_uri` (not on the allowlist), 400 `invalid_client_metadata` (too many URIs, name too long, scope other than `vault:read`, limit reached, closed) |
| `GET/POST /authorize` | none | 302 to `/oauth/start?txn=...` | 400 JSON for unknown client / redirect not registered (never redirects); otherwise a redirect to the client with `invalid_scope`, `invalid_target` (resource is not this server), `temporarily_unavailable` (too many pending authorizations) |
| `GET /oauth/start?txn=` | none | sets the binding cookie, 302 to GitHub | 400 page "unknown or expired authorization"; 413 body too large |
| `GET /oauth/github/callback` | none | consent page (200) | 400 page for a missing/unknown/expired `state` or a missing cookie; 403 page "this GitHub account may not approve access" (pending deleted); 502 page "GitHub could not be reached; nothing was granted" (pending deleted); GitHub's own `error=` is answered with a 400 page and the client gets nothing; 413 |
| `POST /oauth/consent` | cookie + consent token | approve: 303 to `redirect_uri?code&state&iss`; deny: 303 to `redirect_uri?error=access_denied&state&iss` | 400 page on anything missing, expired or not matching; 413; nothing is issued |
| `POST /token` | client auth per registration (SDK): the secret for a pre-registered client | 200 `{access_token, refresh_token, expires_in, scope}` | 400 `invalid_grant` (unknown, expired, replayed, wrong verifier, revoked), 400 `invalid_request` (redirect mismatch), 401 `invalid_client` (missing or wrong secret) |
| `POST /revoke` | client auth (SDK) | 200 always (RFC 7009) | 401 for a bad client |
| `POST/GET/DELETE /mcp` | Bearer | MCP | 401 + `WWW-Authenticate` with `resource_metadata`; 401 for the static bearer on a proxied request; 403 `insufficient_scope` |

No error body or page ever carries a token, a code, a client secret, the
GitHub app secret or a GitHub response body.

## Decisions

**Default posture: one pre-registered confidential client, registration
closed.** The consent-phishing finding of the security review (below, under
*Threat model*) is that with open registration an attacker can add a
connector in *their own* claude.ai account, copy the authorize link it
produces and send it to the owner; the owner's approval then delivers a code
to claude.ai's callback carrying the attacker's `state`, and whether
claude.ai completes that flow for the attacker is outside this server's
knowledge. So the default client is created by the owner:
`ozu mcp client create [--name NAME] [--auth post|basic]` prints a client id
and secret **once**; they go into the connector's settings in claude.ai.
That client is confidential: its codes and refresh tokens are worthless
without the secret (the SDK authenticates it at `/token`), so a phished
approval of a link built on this client id yields nothing to anyone who
lacks the secret. Dynamic registration is off
(`OZU_MCP_OAUTH_REGISTRATION=0`): the metadata advertises no
`registration_endpoint` and `/register` answers 404. It can be opened
deliberately: always (`OZU_MCP_OAUTH_REGISTRATION=1`), or for a window that
closes by itself, `ozu mcp registration open [MINUTES]` (default 10, at most
60; `close` and `status` too). The window bounds use as well as creation: a
dynamically registered client that nobody has approved yet is refused at
`/authorize` (`unauthorized_client`) once registration is closed, so a
stranger who registered during the window cannot show a consent page after
it. A request already in flight completes, and a client that obtained a
grant keeps working.

The client secret is **stored as issued**, in the 0600 state file: the SDK's
client authenticator compares the stored value with `hmac.compare_digest`,
and `get_client()` never sees the request, so a hash cannot satisfy it. The
secret alone grants nothing: every code still needs the pinned owner's
GitHub sign-in and consent, and PKCE.

**Opaque tokens, stored hashed, in SQLite — not JWTs.** One user, one box:
nothing needs to verify a token without asking this process, and revocation
has to be instant and certain. Every code, access token and refresh token is
`secrets.token_urlsafe(32)` (256 bits) with a short prefix (`ozua_`, `ozur_`,
`ozuc_`) so a leaked one is recognisable by secret scanners; only its
SHA-256 is stored. A stolen copy of the state file therefore yields no usable
token (client secrets aside, above). The file is `<OZU_HOME>/mcp-oauth.db`,
created `0600`, separate from `ozu.db` (which is a rebuildable index and must
stay so). No signing key exists, so there is none to leak or rotate. Rows
that can no longer be used are pruned on every authorization, registration
and grant: pending authorizations at expiry, codes an hour after expiry
(replay detection), tokens at expiry or with their grant, revoked or ended
grants after 30 days (the CLI lists them until then), and dynamically
registered clients nobody ever approved. A client the owner approved stays
when its grant ends or is revoked, so it can reconnect with the client id it
has cached (registration is closed by default, so it could not register
again); `ozu mcp client revoke` removes one.

**No new dependency.** The SDK supplies the OAuth endpoints; the HTTP
client the SDK itself depends on (`httpx2` from mcp 2.x, an API-compatible
continuation of `httpx`; `httpx` if that is what is installed) talks to
GitHub; `sqlite3`, `secrets`, `hashlib`, `html`, `shlex` and `subprocess`
do the rest. OAuth mode needs the SDK 2.x provider API (it checks for it and
refuses to start otherwise); the static bearer still works on 1.x.

**Lifetimes.** Pending authorization (from `/authorize` to consent): 10 min.
Authorization code: 120 s, single use. Access token: 1 h. Refresh token:
30 days, rotated on every use, and every grant (a *family*: one consent's
chain of access and refresh tokens) ends 90 days after consent whatever the
refreshes, so the owner signs in with GitHub again at least quarterly.

**Replay and reuse.** A code presented twice revokes the whole family it
produced (RFC 6749 §4.1.2). A refresh token presented after it was rotated
revokes the whole family (OAuth 2.1 refresh-token reuse detection), with one
exception: the **same client** presenting the **same** just-rotated token
again within `REFRESH_GRACE` (5 s) gets the **same** new pair back (a client
with two requests in flight; the review proved sixteen parallel refreshes
revoked the grant). The pair is held in process memory only, never on disk,
is dropped on any revocation, and an expired one is dropped on the next
rotation, so the cache holds the last few seconds of refreshes. That is
the whole weakening: inside those 5 s, a thief who holds the old token *and*
the client id receives the pair the legitimate client also holds instead of
triggering the revocation; after
them, reuse revokes as before. Another client id inside the window gets
`invalid_grant` and revokes nothing.

**Dynamic registration, when open: registration grants nothing.** A
registered client is just a name and a redirect list. It can obtain a code
only through a consent that the pinned GitHub account approves in a browser.
On top of that:

- redirect URIs must be on an allowlist, matched exactly after both sides
  are put in the canonical form the SDK uses (lowercase host, no default
  port, `/` on a bare origin), so the configured spelling does not matter:
  `OZU_MCP_OAUTH_REDIRECT_URIS` (default: the two claude.ai/claude.com
  callbacks); loopback `http://127.0.0.1|localhost|[::1]:<port>/...` only
  with `OZU_MCP_OAUTH_ALLOW_LOOPBACK=1` (Claude Code). The allowlist is
  checked at registration and again at `/authorize`, and a client whose
  redirects are no longer all allowed is treated as unknown. This closes the
  open redirect: a code can only be delivered to Claude (or to the owner's
  own machine).
- at most 4 redirect URIs, 512 characters each, client name 100 characters;
  the SDK caps the request body.
- a client never used to obtain a token is forgotten 1 hour after it
  registered; at most 10 registrations per hour and 50 live clients in all
  (global counters: there is one user, and the proxy's client IP is not
  trusted). While registration is open, a flood can therefore keep a *new*
  client from registering for as long as the flood goes on (each hour it
  refills the ten slots); a client that already has a token is unaffected,
  and with registration closed (the default) there is nothing to flood.
- scope is `vault:read` and nothing else.

**Pending authorizations are capped per client and globally.** Every client
may have at most 3 pending authorizations per 10 minutes; all clients that
have *never* obtained a token share a cap of 30. A client that has (and
every pre-registered client) is exempt from the shared cap, so a flood of
fresh clients cannot lock the owner's own connector out of signing in.
At its own cap a client is not refused: a request is dropped to make room,
the oldest one nobody opened in a browser, else the oldest one sent to
GitHub more than 2 minutes ago (`GITHUB_SIGNIN_GRACE`). A client id is
public, so refusing would let three anonymous `/authorize` calls every ten
minutes keep the owner out, and dropping any request would let a steady
flood cancel the owner's sign-in while they are on GitHub's page. A request
that reached the consent page is never dropped. A client holding three
requests that cannot be dropped is refused (`temporarily_unavailable`):
that leaves one way to delay a Connect, someone who knows the client id
sending three requests on to GitHub every 2 minutes; it grants nothing,
and a sign-in already under way is never cancelled.

Client ID Metadata Documents are not supported in v1: they make the server
fetch an arbitrary URL (SSRF on a box that serves other loopback services),
and claude.ai falls back to DCR when the metadata does not advertise them.

**Identity: one GitHub account, pinned by id and login.** The callback
accepts the GitHub user only if its numeric `id` equals
`OZU_MCP_GITHUB_USER_ID` **and** its `login` equals `OZU_MCP_GITHUB_LOGIN`
(case-insensitive). The id is what matters: logins can be renamed and a freed
login can be taken by someone else. Requiring both means a rename stops
OAuth (fail closed) instead of silently following the account.

**Scopes and what an OAuth token may call: deny by default.** Two scopes
exist:

- `ozu` — the static bearer's scope, every tool including `capture`. It is
  never issued over OAuth (registration and `/authorize` accept only
  `vault:read`).
- `vault:read` — only tools registered with access `"read"`:
  `search_notes`, `get_note`, `graph_neighbors`, `timeline`, `open_actions`
  (`mcp_server.READ_TOOLS`). Anything else, `capture` included, is refused.

Each tool's access class is recorded under the name it is **registered**
as, by `mcp_server.register_tool(server, fn, access="read"|"write",
name=None)`. A tool missing from `READ_TOOLS` is classed `"write"`. Three
layers check it, all keyed on the called name and all denying by default:

1. the wrapper `register_tool` puts around the tool (refuses before the body
   runs; also refuses a call with no token when auth is on);
2. in OAuth mode, a gate on `tools/call` that refuses any name not classed
   `"read"` before the SDK even looks the tool up, so a tool that skipped
   `register_tool` is not reachable over OAuth either;
3. at start-up in OAuth mode, `check_tools_classified()` refuses to start if
   any registered tool has no class, naming it.

`tools/list` hides what a session may not call; that is cosmetic. A refused
call is a tool error, not an HTTP 403, because the MCP session is already
authorized for its read tools. In OAuth mode `required_scopes` is
`vault:read`, and the static bearer carries both `ozu` and `vault:read`, so
it passes the same middleware and keeps its full tool set.

**Adding a tool from another module.** A module that registers tools from
a `register(server, ...)` hook calls
`mcp_server.register_tool(server, fn, access="read")` (or `"write"`) instead
of `server.tool()`. With a bare `server.tool()` the server still starts and
serves the tool when OAuth is off (unchanged), and refuses to start in OAuth
mode, naming the tool: never silently open, never silently fatal.
A name can be registered once: a second `register_tool` under an existing
name raises, because the SDK would keep the first tool and only its class
would change.

**The wall is untouched.** OAuth adds no tool and no path into the data: an
OAuth call runs the very same tool bodies after the scope check, so
`_vault_ok` and `_label_served` apply exactly as over the bearer. (The
`OZU_ALLOW_WORK_MCP` opt-in therefore applies to OAuth sessions too, exactly
as to the bearer; confirm it is unset before opening the endpoint.)

**The static bearer is for loopback in OAuth mode.** It never expires and
carries every tool, so `OAuthEdge` refuses it (401) on any request that
carries `X-Forwarded-For` or `Forwarded`, i.e. one that came through the
reverse proxy, unless `OZU_MCP_STATIC_BEARER_REMOTE=1`. This relies on the
proxy **always** setting `X-Forwarded-For`: a client can add the header but
cannot remove one the proxy sets. Caddy sets it by default, and
`deploy/caddy-mcp.conf` spells it out. A proxy configured not to set it would
turn this check off silently. OAuth tokens are unaffected, and with OAuth off
nothing changes.

**Grant notification hook.** `OZU_MCP_GRANT_HOOK` is a command line, split
with `shlex` once at start-up and run **without a shell** on every new grant
(not on refreshes), in a background thread: it can neither block nor fail
the grant (a missing command, a non-zero exit or a run past 10 s is killed
and logged). Its environment is only `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`
plus non-secret facts: `OZU_GRANT_EVENT=new_grant`, `OZU_GRANT_ID` (grant
id), `OZU_GRANT_CLIENT_ID`, `OZU_GRANT_CLIENT_NAME`, `OZU_GRANT_TIME`. The
client name is client-supplied text: it is passed as an environment value
(control characters replaced, 200 characters), never interpolated into a
command line, but a hook script must still treat it as data (quote it).
Nothing in the hook's environment grants anything: no token, no secret, not
the server's environment.

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
GitHub is kept as is until the callback, then cleared. The consent page says,
in bold: "If you did not just press Connect in Claude yourself, press Deny."

**Mix-up.** There is one authorization server, it names itself as `issuer`,
the final redirect carries `iss=<public>` (RFC 9207), and the metadata says
`authorization_response_iss_parameter_supported: true`.

**Logs.** One audit line per event, never a value that grants anything:
registered or pre-registered client (id, name, redirect hosts); GitHub
identity accepted or refused (GitHub id and login); consent approved or
denied; grant issued (family id, client id, client name, scope, expiry);
refreshed; revoked; replay or reuse detected; registration opened; static
bearer refused through the proxy; grant hook failures. uvicorn's access log
would print query strings (the GitHub `code`, `state`, the `txn`), so in
OAuth mode a filter replaces every query string in it with `?…`.

**GitHub down.** Any failure talking to GitHub (connection, timeout, non-200,
a body without an integer `id`) ends that authorization with a 502 page and
issues nothing. Already issued tokens keep working until they expire (they
do not depend on GitHub); a refresh does not call GitHub either.

### SDK behaviour worth knowing

- The SDK's `TokenError`, `AuthorizeError` and `RegistrationError` are frozen
  dataclasses; `contextlib` cannot re-raise them through a `with` block (it
  assigns `__traceback__`, which ends in `FrozenInstanceError` and a
  `server_error`). Every raise in the provider is therefore outside its
  database transaction; the pending-limit refusal in `authorize()` was the
  one that was not, and has a test now.
- Routes added with `custom_route()` get no body limit from the SDK (its own
  routes are wrapped in `RequestBodyLimitMiddleware`); ours are wrapped in
  the same middleware.
- The SDK's metadata builder never sets
  `authorization_response_iss_parameter_supported` and caches the document
  for an hour; `OAuthEdge` serves it instead, built by the same builder.
- `/revoke`'s request model declares `client_secret` without a default, so a
  public client must send the field, empty, or get a 400. That is the SDK's,
  not ours; revocation from the CLI does not depend on it.
- The SDK's metadata lists `client_secret_post` and `client_secret_basic` as
  token auth methods but not `none`, although it registers and serves public
  clients. A pre-registered client uses `client_secret_post` (or `--auth
  basic`), which the metadata does list.
- The `tools/list` filter and the `tools/call` gate rely on the 2.x server
  calling `self.list_tools()` / `self.call_tool()`; OAuth mode requires 2.x.

## Threat model

Assets: the vault's personal notes (read), the ability to write a note
(`capture`), the walled vault (never served). Adversary: anyone on the
internet, including someone who knows this server's URL, can send requests
of any size, can register clients while registration is open, and can send
the owner links.

| Threat | Control |
|---|---|
| Someone calls `/mcp` without a token | 401 (SDK middleware); unchanged from the bearer mode |
| Static bearer leaked and used from the internet | refused on proxied requests in OAuth mode (needs the proxy to set `X-Forwarded-For`) |
| Stolen access token | 1 h lifetime, audience-bound, stored hashed; `ozu mcp tokens revoke` kills it at once; read-only scope |
| Stolen refresh token | rotation + reuse detection revokes the family (except a same-client re-send within 5 s); 90-day family cap; revocable from the CLI; a pre-registered client's refresh also needs its secret |
| Stolen state file | only hashes of tokens and codes; no signing key; pre-registered client secrets are in it as issued (useless without the owner's consent) |
| Authorization-code interception | PKCE S256 mandatory (SDK), code single use, 120 s, replay revokes the family; code delivered only to allowlisted redirect URIs; a pre-registered client's code also needs its secret |
| Open redirect | exact redirect match (SDK) + allowlist at registration and at `/authorize`; unknown client or bad redirect is a 400 page, never a redirect |
| CSRF / login CSRF on the GitHub round trip | `state` bound to the pending authorization, plus the browser cookie, plus a one-time consent token on the POST |
| **Consent phishing** (attacker adds a connector in their own claude.ai, sends the owner its authorize link) | default client is pre-registered and confidential, so the resulting code is useless without the secret; registration closed by default; the consent page names client, id, redirect host and says to Deny anything the owner did not start; the grant hook tells the owner of every new grant. **Residual** while registration is open: the code goes to claude.ai with the attacker's `state`; whether claude.ai would complete it for the attacker is not known here |
| Someone else's GitHub account | id and login must both match the pinned values; anyone else gets a 403 page and nothing is stored |
| GitHub login renamed or reused | the numeric id is pinned |
| Unauthenticated memory exhaustion (huge bodies) | 16 KB cap on every added route (413 before reading), the SDK's cap on its own; 64 KB `request_body` in the Caddy example |
| DCR abuse (flooding, huge metadata, odd redirects) | closed by default (404); when open: registration grants nothing; allowlist; size caps; 1 h expiry of unused clients; 10/hour and 50 total |
| Pending-authorization flooding / lock-out | 3 per client and 30 shared per 10 min; clients that already have a token are exempt from the shared cap; 10 min expiry; refused cleanly (`temporarily_unavailable`) |
| Clickjacking the consent page | `X-Frame-Options: DENY`, `frame-ancestors 'none'` |
| Mix-up | single AS, `iss` in the redirect and advertised |
| `capture` (or any non-read tool) reached over OAuth | scope never issuable; deny-by-default classes by registered name, checked in the wrapper and at `tools/call`; start-up refused with an unclassified tool |
| Walled content over OAuth | same tool bodies, same `_vault_ok` / `_label_served`; tested |
| Secrets in logs or error bodies | no value that grants anything is logged; access-log query strings stripped; grant hook gets facts only; tested on captured logs, bodies and the hook's environment |
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
| `OZU_MCP_OAUTH_REDIRECT_URIS` | optional, comma-separated exact redirect URIs; default the claude.ai and claude.com callbacks; also the redirects a pre-registered client gets |
| `OZU_MCP_OAUTH_ALLOW_LOOPBACK` | optional, `1` also allows loopback redirects (Claude Code) |
| `OZU_MCP_OAUTH_REGISTRATION` | `0` (default) dynamic registration closed except for a CLI window; `1` always open |
| `OZU_MCP_STATIC_BEARER_REMOTE` | `0` (default) the static bearer is refused through the proxy in OAuth mode; `1` accepts it |
| `OZU_MCP_GRANT_HOOK` | optional command line, run without a shell on every new grant (above) |
| `OZU_MCP_GITHUB_URL` / `OZU_MCP_GITHUB_API_URL` | optional, default `https://github.com` / `https://api.github.com`; https or loopback only (tests, GitHub Enterprise) |

`OZU_MCP_TOKEN` stays required for `--http` in both modes; the static bearer
keeps working unchanged on loopback.

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
`/oauth/consent`. It must pass the `Host` header as received (Caddy does),
must **always set `X-Forwarded-For`** (Caddy does; the example says so),
must not cache, and should cap request bodies on the OAuth paths (the
example uses 64 KB). Do not add basic auth in front of it. **Never add a
`log` directive for these paths without stripping query strings and the
`Location` response header**: they carry the flow id, GitHub's code and
state, and the authorization code.

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
   Optionally `OZU_MCP_GRANT_HOOK=<a command that notifies you>`. Add
   `OZU_MCP_OAUTH_ALLOW_LOOPBACK=1` only if Claude Code will sign in by OAuth
   too (it can keep using the static bearer on the box instead). **Confirm
   `OZU_ALLOW_WORK_MCP` is unset.**
4. **Restart the server** (e.g. `systemctl --user restart ozu-mcp`) and read
   its log: it refuses to start, naming what is missing, if anything is (or
   naming a tool registered without an access class).
5. **Create the client:** `ozu mcp client create --name Claude`. Copy the
   client id and secret it prints; the secret is not shown again (if lost,
   `ozu mcp client revoke <id>` and create another).
6. **Proxy:** install `deploy/caddy-mcp.conf` with your name, then
   `sudo caddy validate --config /etc/caddy/Caddyfile` **before**
   `sudo systemctl reload caddy`.
7. **Check from another machine:**
   - `curl -si https://ozu.example.com/mcp -X POST` → `401` with
     `WWW-Authenticate: Bearer ... resource_metadata="https://ozu.example.com/.well-known/oauth-protected-resource/mcp"`.
   - `curl -s https://ozu.example.com/.well-known/oauth-protected-resource/mcp`
     → `"resource":"https://ozu.example.com/mcp"`.
   - `curl -s https://ozu.example.com/.well-known/oauth-authorization-server`
     → `"code_challenge_methods_supported":["S256"]`, no `registration_endpoint`.
   - `curl -s -o /dev/null -w '%{http_code}\n' -X POST https://ozu.example.com/register` → `404`.
   - The static bearer from outside → `401` ("not accepted through the proxy").
   - **The 421 pitfall:** all of the above are answered before the Host
     check, so they succeed even when `OZU_MCP_PUBLIC_URL` is wrong. The
     first *authenticated* MCP request through the proxy is the real check:
     the connector's own (step 8: if Claude lists the tools, there is no
     421), or, before turning OAuth on, the static bearer in bearer-only
     mode. A 421 means the public URL does not match the Host the proxy
     forwards. Fix the URL; never disable DNS-rebinding protection.
8. **Add the connector in claude.ai:** Settings → Connectors → Add custom
   connector, URL `https://ozu.example.com/mcp` (with `/mcp`: the PRM
   resource must equal what you type), and under the connector's advanced
   settings the **OAuth client id and client secret** from step 5. Connect:
   GitHub asks you to sign in or authorize the app, then Ozu's consent page
   names the client and `claude.ai`; approve. Connectors added on the web are
   also available in the phone app.
   - **Not verified:** that claude.ai offers client id / secret fields for a
     custom connector in your plan (Anthropic's developer docs describe
     client credentials for directory connectors; this could not be checked
     for a personal custom connector without opening the endpoint). If the
     token step fails with `invalid_client`, recreate the client with
     `--auth basic` and try again.
   - **If there are no such fields:** open registration for a few minutes,
     connect, close it: `ozu mcp registration open 5`, add the connector
     with no id or secret (Claude registers itself), approve, then
     `ozu mcp registration close`. Residual risk: while the window is open,
     the consent-phishing scenario above applies; a public client's codes
     need no secret. Keep the window short, approve only a flow you just
     started yourself, and watch the grant hook / `ozu mcp tokens list`
     for a grant you do not recognise.
9. **Confirm:** `ozu mcp tokens list` shows one active grant for "Claude";
   in a chat, ask Claude to search your notes; asking it to capture a note
   is refused.

### Revoking

`ozu mcp tokens list` shows every grant (family id, client, created, last
refreshed, expiry, status), never a token value. `ozu mcp tokens revoke
<family-id>` revokes one grant, `ozu mcp tokens revoke --client <client-id>`
revokes every grant of a client and forgets it, `ozu mcp tokens revoke
--all` revokes everything. `ozu mcp client list` shows the clients (never a
secret); `ozu mcp client revoke <client-id>` removes one and revokes its
grants. Revocation is immediate, without a restart: every request looks its
token up. Removing the connector in claude.ai does not revoke anything on
this side; do both.

### Turning it off

Set `OZU_MCP_OAUTH=0` (or remove it) and restart: the OAuth routes are gone
and every OAuth token is refused (only the static bearer is accepted). Then
close the public route in the proxy (validate, reload) if nothing else uses
it, and delete or revoke the GitHub OAuth app. `mcp-oauth.db` can be deleted;
it is recreated empty when OAuth is next turned on.
