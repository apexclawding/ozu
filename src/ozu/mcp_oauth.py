"""OAuth for `ozu mcp --http`, so claude.ai connectors can sign in.

Design, threat model and runbook: docs/mcp-oauth.md. In short: this process
is its own authorization server (the MCP SDK serves /authorize, /token,
/register, /revoke and the metadata; this module is the provider behind
them), GitHub is used only to prove that the one pinned account is at the
browser, and every token is an opaque random string stored hashed in a small
SQLite file. OAuth tokens carry `vault:read` only; `capture` needs the
static bearer's scope.

Off unless OZU_MCP_OAUTH=1, and then it refuses to start when any required
setting is missing. No value that grants anything (a token, a code, a client
secret, the GitHub app secret) is ever logged or put in a response body.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

READ_SCOPE = "vault:read"   # the only scope OAuth ever issues
FULL_SCOPE = "ozu"          # the static bearer's scope: every tool, `capture` included

PENDING_TTL = 600            # /authorize -> consent
CODE_TTL = 120               # authorization code, single use
ACCESS_TTL = 3600            # access token
REFRESH_TTL = 30 * 86400     # refresh token, rotated on every use
FAMILY_TTL = 90 * 86400      # one consent's chain of tokens, whatever the refreshes
CLIENT_UNUSED_TTL = 3600     # a registered client that never got a token is forgotten
MAX_REGISTRATIONS_PER_HOUR = 10
MAX_CLIENTS = 50
MAX_PENDING = 30             # per PENDING_TTL, for clients that never got a token
MAX_PENDING_PER_CLIENT = 3   # per PENDING_TTL, for every client
GITHUB_SIGNIN_GRACE = 120    # s: a request on GitHub's sign-in page is not evicted for this long
MAX_BODY = 16 * 1024         # every route this module adds; a form here is a few hundred bytes
REFRESH_GRACE = 5            # s: the same client re-sending a just-rotated refresh token
HOOK_TIMEOUT = 10            # s: OZU_MCP_GRANT_HOOK is killed after this
REGISTRATION_WINDOW_MAX = 60  # min: `ozu mcp registration open` never longer
MAX_REDIRECT_URIS = 4
MAX_URI_LEN = 512
MAX_NAME_LEN = 100
HTTP_TIMEOUT = 10.0

LOOPBACK = {"127.0.0.1", "localhost", "::1"}

log = logging.getLogger("ozu.mcp_oauth")


def canonical_url(url: str) -> str:
    """`url` as the SDK will hand it back: parsed through pydantic's AnyUrl,
    the type of every URL in the SDK's models, so the host is lowercased (and
    punycoded), a default port dropped and a bare origin given its `/`. Every
    configured URL is stored in this form and every incoming one compared in
    it, or `https://Claude.ai:443/...` in the config would pass the start-up
    check and then never match. Raises ValueError on a URL it cannot parse."""
    from pydantic import AnyUrl, ValidationError
    try:
        return str(AnyUrl(url))
    except ValidationError as exc:
        raise ValueError("not a URL") from exc


def _canonical_or_none(url: str) -> str | None:
    try:
        return canonical_url(url)
    except ValueError:
        return None


def http_lib():
    """The HTTP client the MCP SDK itself ships: `httpx2` from mcp 2.x on
    (same API), `httpx` before. No dependency of our own."""
    try:
        import httpx2 as lib
    except ImportError:
        import httpx as lib
    return lib


# --------------------------------------------------------------- settings

@dataclass
class OAuthSettings:
    public_url: str
    github_client_id: str
    github_client_secret: str = field(repr=False)
    github_login: str
    github_user_id: int
    db_path: Path
    redirect_uris: list[str]
    allow_loopback: bool = False
    github_url: str = "https://github.com"
    github_api_url: str = "https://api.github.com"
    registration_open: bool = False      # OZU_MCP_OAUTH_REGISTRATION=1
    grant_hook: list[str] = field(default_factory=list)
    static_remote: bool = False          # OZU_MCP_STATIC_BEARER_REMOTE=1

    def __post_init__(self):
        # Canonical once, here, so issuer, resource and the redirect
        # allowlist compare like with like (see canonical_url).
        self.public_url = canonical_url(self.public_url).rstrip("/")
        self.redirect_uris = [canonical_url(u) for u in self.redirect_uris]


def enabled(cfg) -> bool:
    """OZU_MCP_OAUTH: `1` on, unset/empty/`0` off, anything else refused, so a
    typo like `true` neither silently enables nor silently disables."""
    if cfg.mcp_oauth in ("", "0"):
        return False
    if cfg.mcp_oauth == "1":
        return True
    raise SystemExit("OZU_MCP_OAUTH must be 1 (on) or 0/unset (off); "
                     "`ozu mcp --http` will not guess.")


def _url_problem(name: str, url: str, *, path_ok: bool) -> str | None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return f"{name} must be an absolute http(s) URL"
    if p.scheme == "http" and p.hostname not in LOOPBACK:
        return f"{name} must be https (plain http only on loopback, for tests)"
    if p.query or p.fragment or p.username or p.password:
        return f"{name} must not carry a query, a fragment or credentials"
    if not path_ok and p.path not in ("", "/"):
        return f"{name} must be an origin with no path (e.g. https://ozu.example.com)"
    if _canonical_or_none(url) is None:
        return f"{name} is not a URL the MCP SDK can parse"
    return None


def settings_from_config(cfg) -> OAuthSettings:
    """The OAuth settings, or SystemExit naming every missing or malformed
    one. Names only: a value is never echoed, it may be a secret."""
    problems = []
    required = {
        "OZU_MCP_GITHUB_CLIENT_ID": cfg.mcp_github_client_id,
        "OZU_MCP_GITHUB_CLIENT_SECRET": cfg.mcp_github_client_secret,
        "OZU_MCP_GITHUB_LOGIN": cfg.mcp_github_login,
        "OZU_MCP_GITHUB_USER_ID": cfg.mcp_github_user_id,
    }
    problems += [f"{k} is unset" for k, v in required.items() if not v]
    if cfg.mcp_github_user_id and not re.fullmatch(r"[0-9]{1,20}", cfg.mcp_github_user_id):
        problems.append("OZU_MCP_GITHUB_USER_ID must be the numeric GitHub user id")
    if not cfg.mcp_public_url_set:
        problems.append("OZU_MCP_PUBLIC_URL is unset (it is the issuer and the "
                        "GitHub callback base)")
    else:
        problems.append(_url_problem("OZU_MCP_PUBLIC_URL", cfg.mcp_public_url, path_ok=False))
    problems.append(_url_problem("OZU_MCP_GITHUB_URL", cfg.mcp_github_url, path_ok=True))
    problems.append(_url_problem("OZU_MCP_GITHUB_API_URL", cfg.mcp_github_api_url, path_ok=True))
    if not cfg.mcp_oauth_redirect_uris and not cfg.mcp_oauth_allow_loopback:
        problems.append("OZU_MCP_OAUTH_REDIRECT_URIS is empty and loopback is off: "
                        "no client could ever be registered")
    for uri in cfg.mcp_oauth_redirect_uris:
        if _url_problem("x", uri, path_ok=True):
            problems.append("OZU_MCP_OAUTH_REDIRECT_URIS has an entry that is not an "
                            "https (or loopback http) URL without query or fragment")
            break
    for name, val in (("OZU_MCP_OAUTH_REGISTRATION", cfg.mcp_oauth_registration),
                      ("OZU_MCP_STATIC_BEARER_REMOTE", cfg.mcp_static_remote)):
        if val not in ("0", "1"):
            problems.append(f"{name} must be 0 or 1")
    hook: list[str] = []
    try:
        hook = shlex.split(cfg.mcp_grant_hook)
    except ValueError:
        problems.append("OZU_MCP_GRANT_HOOK is not a parseable command line")
    try:  # the provider API this module implements is the SDK 2.x one
        from mcp.server.auth.provider import AccessToken
        from mcp.server.auth.settings import ClientRegistrationOptions  # noqa: F401
        if "subject" not in AccessToken.model_fields:
            raise ImportError
        http_lib()
    except ImportError:
        problems.append("the installed mcp SDK has no OAuth provider API this "
                        "server supports (needs mcp>=2)")
    problems = [p for p in problems if p]
    if problems:
        raise SystemExit(
            "OAuth is on (OZU_MCP_OAUTH=1) but `ozu mcp --http` will not start "
            "half-configured:\n  - " + "\n  - ".join(problems)
            + f"\nSet them in {cfg.home / '.env'} (see docs/mcp-oauth.md).")
    return OAuthSettings(
        public_url=cfg.mcp_public_url,
        github_client_id=cfg.mcp_github_client_id,
        github_client_secret=cfg.mcp_github_client_secret,
        github_login=cfg.mcp_github_login,
        github_user_id=int(cfg.mcp_github_user_id),
        db_path=cfg.mcp_oauth_db,
        redirect_uris=list(cfg.mcp_oauth_redirect_uris),
        allow_loopback=cfg.mcp_oauth_allow_loopback,
        github_url=cfg.mcp_github_url.rstrip("/"),
        github_api_url=cfg.mcp_github_api_url.rstrip("/"),
        registration_open=cfg.mcp_oauth_registration == "1",
        grant_hook=hook,
        static_remote=cfg.mcp_static_remote == "1",
    )


# ------------------------------------------------------------------ store

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY, info TEXT NOT NULL, created REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'dcr');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending (
    txn TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    stage TEXT NOT NULL, gh_state TEXT, nonce TEXT, consent TEXT,
    gh_verifier TEXT, gh_id INTEGER, gh_login TEXT,
    created REAL NOT NULL, expires REAL NOT NULL, started REAL);
CREATE TABLE IF NOT EXISTS codes (
    code TEXT PRIMARY KEY, client_id TEXT NOT NULL, data TEXT NOT NULL,
    expires REAL NOT NULL, used REAL, family TEXT);
CREATE TABLE IF NOT EXISTS families (
    family TEXT PRIMARY KEY, client_id TEXT NOT NULL, client_name TEXT,
    subject TEXT, scopes TEXT NOT NULL, resource TEXT NOT NULL,
    created REAL NOT NULL, expires REAL NOT NULL, refreshed REAL, revoked REAL);
CREATE TABLE IF NOT EXISTS tokens (
    token TEXT PRIMARY KEY, kind TEXT NOT NULL, family TEXT NOT NULL,
    expires REAL NOT NULL, used REAL);
"""


def _h(value: str) -> str:
    """What is stored for every secret value: its SHA-256. A copy of the state
    file yields no usable token, code or nonce."""
    return hashlib.sha256(value.encode()).hexdigest()


def _same(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and secrets.compare_digest(a.encode(), b.encode())


def _open(db_path: Path) -> sqlite3.Connection:
    """A connection to a state file connect() has already created and
    migrated. Read-write but never create: a file deleted under a running
    server is an error, not a fresh one made with the process umask and no
    schema."""
    con = sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def connect(db_path: Path) -> sqlite3.Connection:
    """Create the state file 0600 if needed, bring its schema up to date and
    return a connection to it. Once per process (and per CLI command); each
    request's transaction then uses _open."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        os.close(os.open(db_path, os.O_CREAT | os.O_WRONLY, 0o600))
    os.chmod(db_path, 0o600)
    con = sqlite3.connect(db_path, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(clients)")}
    if "kind" not in cols:  # a state file from before pre-registered clients
        con.execute("ALTER TABLE clients ADD COLUMN kind TEXT NOT NULL DEFAULT 'dcr'")
    if "started" not in {r[1] for r in con.execute("PRAGMA table_info(pending)")}:
        con.execute("ALTER TABLE pending ADD COLUMN started REAL")  # when it went to GitHub
    return con


# --------------------------------------------------------------- provider

class _Upstream(Exception):
    """GitHub could not confirm an identity. Carries no response body."""


class GitHubOAuthProvider:
    """The SDK's OAuthAuthorizationServerProvider, backed by SQLite, with
    GitHub as the identity check. Also answers for the static bearer, because
    the SDK takes one provider *or* one verifier, not both."""

    def __init__(self, settings: OAuthSettings, static_token: str = "",
                 http_factory=None, clock=time.time):
        self.s = settings
        self.static_token = static_token
        self._http_factory = http_factory
        self._now = clock
        # Just-rotated refresh tokens -> the pair they were rotated into, held
        # in memory for REFRESH_GRACE seconds and never written to disk.
        self._grace: dict[str, tuple[float, str, object]] = {}
        connect(settings.db_path).close()  # create it 0600 now, not on first request

    # ---- names
    @property
    def issuer(self) -> str:
        return self.s.public_url

    @property
    def resource(self) -> str:
        return self.s.public_url + "/mcp"

    @property
    def callback_url(self) -> str:
        return self.s.public_url + "/oauth/github/callback"

    @property
    def secure_cookie(self) -> bool:
        return urlparse(self.s.public_url).scheme == "https"

    @property
    def cookie_name(self) -> str:
        return "__Host-ozu_oauth" if self.secure_cookie else "ozu_oauth"

    @contextmanager
    def _db(self):
        """One short transaction: committed on exit, rolled back on an
        exception, and the connection closed either way."""
        con = _open(self.s.db_path)
        try:
            with con:
                yield con
        finally:
            con.close()

    def _http(self):
        if self._http_factory:
            return self._http_factory()
        return http_lib().AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=False)

    def redirect_allowed(self, uri: str) -> bool:
        if len(uri) > MAX_URI_LEN:
            return False
        if _canonical_or_none(uri) in self.s.redirect_uris:
            return True
        if not self.s.allow_loopback:
            return False
        p = urlparse(uri)
        return (p.scheme == "http" and p.hostname in LOOPBACK and not p.username
                and not p.password and not p.fragment)

    def _is_our_resource(self, resource: str) -> bool:
        p = urlparse(resource.strip())
        if p.query or p.fragment:
            return False
        norm = _canonical_or_none(resource.strip())
        return norm is not None and norm.rstrip("/") in (self.issuer, self.resource)

    # ---- clients (DCR)
    async def get_client(self, client_id: str):
        with self._db() as con:
            return self._client(con, client_id)

    def _client(self, con, client_id: str):
        from mcp.shared.auth import OAuthClientInformationFull
        row = con.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            return None
        if not row["used"] and row["created"] < self._now() - CLIENT_UNUSED_TTL:
            con.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
            return None
        info = OAuthClientInformationFull.model_validate_json(row["info"])
        # A client whose redirect is no longer on the allowlist is treated as
        # unknown: the SDK then answers 400 instead of redirecting to it.
        if not all(self.redirect_allowed(str(u)) for u in info.redirect_uris or []):
            return None
        return info

    async def register_client(self, client_info) -> None:
        from mcp.server.auth.provider import RegistrationError
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or len(uris) > MAX_REDIRECT_URIS:
            raise RegistrationError("invalid_client_metadata",
                                    f"between 1 and {MAX_REDIRECT_URIS} redirect_uris")
        if not all(self.redirect_allowed(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri",
                                    "redirect_uri is not allowed by this server")
        if client_info.client_name and len(client_info.client_name) > MAX_NAME_LEN:
            raise RegistrationError("invalid_client_metadata", "client_name too long")
        if set((client_info.scope or READ_SCOPE).split()) != {READ_SCOPE}:
            raise RegistrationError("invalid_client_metadata",
                                    f"the only scope is {READ_SCOPE}")
        if not set(client_info.grant_types) <= {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "unsupported grant_types")
        client_info.scope = READ_SCOPE
        now = self._now()
        if not self.registration_is_open():
            log.warning("client registration refused: registration is closed")
            raise RegistrationError("invalid_client_metadata", "registration is closed")
        with self._db() as con:
            self._prune(con)
            recent = con.execute("SELECT COUNT(*) FROM clients WHERE created>?",
                                 (now - 3600,)).fetchone()[0]
            total = con.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
            full = recent >= MAX_REGISTRATIONS_PER_HOUR or total >= MAX_CLIENTS
            if not full:
                con.execute("INSERT INTO clients(client_id, info, created) VALUES(?,?,?)",
                            (client_info.client_id, client_info.model_dump_json(), now))
        # The SDK's errors are frozen dataclasses, which contextlib cannot
        # re-raise through a `with`; every raise stays outside the transaction.
        if full:
            log.warning("client registration refused: limit reached")
            raise RegistrationError("invalid_client_metadata",
                                    "registration limit reached; try again later")
        log.info("registered client %s name=%r redirect_hosts=%s", client_info.client_id,
                 client_info.client_name, sorted({urlparse(u).hostname for u in uris}))

    # ---- /authorize
    async def authorize(self, client, params) -> str:
        from mcp.server.auth.provider import AuthorizeError
        redirect = str(params.redirect_uri)
        if not self.redirect_allowed(redirect):  # get_client already refuses these
            raise AuthorizeError("invalid_request", "redirect_uri not allowed")
        scopes = params.scopes or [READ_SCOPE]
        if set(scopes) != {READ_SCOPE}:
            raise AuthorizeError("invalid_scope", f"the only scope is {READ_SCOPE}")
        if params.resource and not self._is_our_resource(params.resource):
            raise AuthorizeError("invalid_target", "resource is not this server")
        now = self._now()
        txn = secrets.token_urlsafe(32)
        reg_open = self.registration_is_open()
        closed = False
        with self._db() as con:
            self._prune(con)
            since = now - PENDING_TTL
            trusted = con.execute("SELECT used, kind FROM clients WHERE client_id=?",
                                  (client.client_id,)).fetchone()
            # A registration window bounds when a client may be created; this
            # bounds how long one nobody ever approved stays usable. Without
            # it a client registered during a ten-minute window could show
            # the owner a consent page for the rest of its hour. A client the
            # prune just forgot (the SDK loaded it a moment earlier) is
            # refused here too, before the owner is sent through GitHub for
            # an "Application unknown".
            unknown = trusted is None
            closed = unknown or bool(trusted["kind"] == "dcr" and not trusted["used"]
                                     and not reg_open)
            # At its cap a client loses a request instead of being refused: a
            # client id is public, and three anonymous /authorize calls every
            # ten minutes would otherwise keep the owner from ever
            # connecting. The victim is the oldest request nobody has opened
            # in a browser ('new'), else the oldest one that went to GitHub
            # more than GITHUB_SIGNIN_GRACE ago, so a flood cannot cancel a
            # sign-in while the owner is on GitHub's page. A request that
            # reached the consent page (the pinned account signed in) is
            # never evicted.
            mine = con.execute("SELECT COUNT(*) FROM pending WHERE client_id=? AND created>?",
                               (client.client_id, since)).fetchone()[0]
            if mine >= MAX_PENDING_PER_CLIENT and not closed:
                cur = con.execute(
                    "DELETE FROM pending WHERE txn=(SELECT txn FROM pending WHERE client_id=?"
                    " AND (stage='new' OR (stage='github' AND started<?))"
                    " ORDER BY stage='github', created, rowid LIMIT 1)",
                    (client.client_id, now - GITHUB_SIGNIN_GRACE))
                mine -= cur.rowcount
            # A client that already obtained a token (or a pre-registered one)
            # is exempt from the global cap, so a flood of fresh clients cannot
            # lock the owner's own connector out; every client has its own cap.
            others = 0 if (not unknown and trusted["used"]) else con.execute(
                "SELECT COUNT(*) FROM pending WHERE created>?", (since,)).fetchone()[0]
            full = mine >= MAX_PENDING_PER_CLIENT or others >= MAX_PENDING
            if not full and not closed:
                con.execute(
                    "INSERT INTO pending(txn, client_id, params, stage, created, expires)"
                    " VALUES(?,?,?,?,?,?)",
                    (_h(txn), client.client_id, json.dumps({
                        "state": params.state, "scopes": [READ_SCOPE],
                        "code_challenge": params.code_challenge, "redirect_uri": redirect,
                        "explicit": params.redirect_uri_provided_explicitly,
                        "resource": self.resource,
                    }), "new", now, now + PENDING_TTL))
        if unknown:  # raised outside the transaction: the SDK's errors are frozen
            log.warning("authorization refused: client %s was forgotten", client.client_id)
            raise AuthorizeError("unauthorized_client", "this client is no longer registered")
        if closed:
            log.warning("authorization refused: client %s was never approved and "
                        "registration is closed", client.client_id)
            raise AuthorizeError("unauthorized_client",
                                 "this client was never approved and registration is closed")
        if full:
            log.warning("authorization refused: too many pending")
            raise AuthorizeError("temporarily_unavailable", "too many pending authorizations")
        return f"{self.issuer}/oauth/start?txn={txn}"

    def _pending(self, con, column: str, value: str | None, stage: str):
        if not value or len(value) > 256:
            return None
        row = con.execute(f"SELECT * FROM pending WHERE {column}=? AND stage=?",
                          (_h(value), stage)).fetchone()
        if row and row["expires"] < self._now():
            con.execute("DELETE FROM pending WHERE txn=?", (row["txn"],))
            return None
        return row

    # ---- the browser-facing routes (mounted unauthenticated, by design)
    async def start(self, request):
        from starlette.responses import RedirectResponse
        with self._db() as con:
            row = self._pending(con, "txn", request.query_params.get("txn"), "new")
            if not row:
                return _page("Authorization expired",
                             "<p>This authorization is unknown or has expired. "
                             "Start again from the application.</p>", 400)
            gh_state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(48)
            cur = con.execute(
                "UPDATE pending SET stage='github', gh_state=?, nonce=?, gh_verifier=?,"
                " started=? WHERE txn=? AND stage='new'",
                (_h(gh_state), _h(nonce), verifier, self._now(), row["txn"]))
            if cur.rowcount != 1:
                return _page("Authorization expired", "<p>Start again.</p>", 400)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        from urllib.parse import urlencode
        url = f"{self.s.github_url}/login/oauth/authorize?" + urlencode({
            "client_id": self.s.github_client_id, "redirect_uri": self.callback_url,
            "state": gh_state, "scope": "", "allow_signup": "false",
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        resp = RedirectResponse(url, status_code=302, headers=_NO_STORE)
        resp.set_cookie(self.cookie_name, nonce, max_age=PENDING_TTL, path="/",
                        secure=self.secure_cookie, httponly=True, samesite="lax")
        return resp

    async def github_callback(self, request):
        q = request.query_params
        nonce = request.cookies.get(self.cookie_name)
        with self._db() as con:
            row = self._pending(con, "gh_state", q.get("state"), "github")
            if not row or not _same(_h(nonce or ""), row["nonce"]):
                return _page("Sign-in not recognised",
                             "<p>This sign-in does not belong to an authorization "
                             "started in this browser, or it has expired. Nothing was "
                             "granted.</p>", 400)
            cur = con.execute("UPDATE pending SET stage='exchanging' WHERE txn=? AND "
                              "stage='github'", (row["txn"],))
            if cur.rowcount != 1:
                return _page("Sign-in not recognised", "<p>Nothing was granted.</p>", 400)
        txn_h, verifier = row["txn"], row["gh_verifier"]
        if q.get("error") or not q.get("code"):
            self._drop(txn_h)
            return _page("GitHub did not sign you in",
                         "<p>GitHub returned no sign-in. Nothing was granted.</p>", 400)
        try:
            gh_id, gh_login = await self._github_identity(q["code"], verifier)
        except _Upstream as exc:
            self._drop(txn_h)
            log.warning("github identity check failed (%s); nothing granted", exc)
            return _page("GitHub could not be reached",
                         "<p>GitHub could not confirm who you are, so nothing was "
                         "granted. Try again later.</p>", 502)
        if gh_id != self.s.github_user_id or gh_login.lower() != self.s.github_login.lower():
            self._drop(txn_h)
            log.warning("github identity refused: id=%s login=%r is not the pinned account",
                        gh_id, gh_login)
            return _page("Not allowed",
                         "<p>This GitHub account may not approve access to this "
                         "vault. Nothing was granted.</p>", 403)
        log.info("github identity accepted: id=%s login=%r", gh_id, gh_login)
        consent = secrets.token_urlsafe(32)
        with self._db() as con:
            con.execute("UPDATE pending SET stage='consent', consent=?, gh_id=?, gh_login=?,"
                        " gh_verifier=NULL WHERE txn=?", (_h(consent), gh_id, gh_login, txn_h))
        client = await self.get_client(row["client_id"])
        if client is None:
            self._drop(txn_h)
            return _page("Application unknown", "<p>Nothing was granted.</p>", 400)
        params = json.loads(row["params"])
        return _consent_page(client, params["redirect_uri"], gh_login, consent,
                             q["state"])

    async def consent(self, request):
        from starlette.responses import RedirectResponse
        from mcp.server.auth.provider import construct_redirect_uri
        form = await request.form()
        nonce = request.cookies.get(self.cookie_name)
        txn_key = form.get("flow")
        with self._db() as con:
            row = self._pending(con, "gh_state", txn_key if isinstance(txn_key, str) else None,
                                "consent")
            ok = (row is not None and _same(_h(nonce or ""), row["nonce"])
                  and _same(_h(str(form.get("consent", ""))), row["consent"]))
            if ok:
                ok = con.execute("DELETE FROM pending WHERE txn=? AND stage='consent'",
                                 (row["txn"],)).rowcount == 1
        if not ok:
            return _page("Consent not recognised",
                         "<p>This consent is unknown, expired or was already used. "
                         "Nothing was granted.</p>", 400)
        params = json.loads(row["params"])
        if form.get("action") != "approve":
            log.info("consent denied for client %s", row["client_id"])
            url = construct_redirect_uri(params["redirect_uri"], error="access_denied",
                                         state=params["state"], iss=self.issuer)
        else:
            code = "ozuc_" + secrets.token_urlsafe(32)
            with self._db() as con:
                con.execute("INSERT INTO codes(code, client_id, data, expires) VALUES(?,?,?,?)",
                            (_h(code), row["client_id"], json.dumps({
                                **params, "subject": f"github:{row['gh_id']}"}),
                             self._now() + CODE_TTL))
            log.info("consent approved for client %s by github id=%s",
                     row["client_id"], row["gh_id"])
            url = construct_redirect_uri(params["redirect_uri"], code=code,
                                         state=params["state"], iss=self.issuer)
        resp = RedirectResponse(url, status_code=303, headers=_NO_STORE)
        resp.delete_cookie(self.cookie_name, path="/", secure=self.secure_cookie,
                           httponly=True, samesite="lax")
        return resp

    def _drop(self, txn_h: str) -> None:
        with self._db() as con:
            con.execute("DELETE FROM pending WHERE txn=?", (txn_h,))

    async def _github_identity(self, code: str, verifier: str) -> tuple[int, str]:
        """GitHub code -> (id, login). The GitHub token is used for one call
        and then deleted at GitHub; it is never stored."""
        httpx = http_lib()
        gh_token = None
        try:
            async with self._http() as http:
                try:
                    r = await http.post(
                        f"{self.s.github_url}/login/oauth/access_token",
                        headers={"Accept": "application/json"},
                        data={"client_id": self.s.github_client_id,
                              "client_secret": self.s.github_client_secret,
                              "code": code, "redirect_uri": self.callback_url,
                              "code_verifier": verifier})
                    body = r.json() if r.status_code == 200 else {}
                    gh_token = body.get("access_token") if isinstance(body, dict) else None
                    if not isinstance(gh_token, str) or not gh_token:
                        raise _Upstream(f"token exchange status={r.status_code}")
                    u = await http.get(f"{self.s.github_api_url}/user", headers={
                        "Accept": "application/vnd.github+json",
                        "Authorization": f"Bearer {gh_token}"})
                    if u.status_code != 200:
                        raise _Upstream(f"user lookup status={u.status_code}")
                    user = u.json()
                    gh_id, login = user.get("id"), user.get("login")
                    if type(gh_id) is not int or not isinstance(login, str):
                        raise _Upstream("user lookup returned no id")
                    return gh_id, login
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    raise _Upstream(type(exc).__name__) from None
                finally:
                    if gh_token:
                        try:
                            await http.request(
                                "DELETE",
                                f"{self.s.github_api_url}/applications/"
                                f"{self.s.github_client_id}/token",
                                auth=(self.s.github_client_id, self.s.github_client_secret),
                                json={"access_token": gh_token})
                        except httpx.HTTPError:
                            log.info("could not delete the GitHub token (best effort)")
        except httpx.HTTPError as exc:
            raise _Upstream(type(exc).__name__) from None

    # ---- codes and tokens
    async def load_authorization_code(self, client, authorization_code: str):
        from mcp.server.auth.provider import AuthorizationCode
        with self._db() as con:
            row = con.execute("SELECT * FROM codes WHERE code=?",
                              (_h(authorization_code),)).fetchone()
            if not row:
                return None
            if row["used"]:
                # RFC 6749 4.1.2: a code used twice revokes what it produced.
                if row["family"]:
                    self._revoke_family(con, row["family"])
                log.warning("authorization code replayed; grant %s revoked", row["family"])
                return None
        if row["client_id"] != client.client_id:
            return None
        d = json.loads(row["data"])
        return AuthorizationCode(
            code=authorization_code, scopes=d["scopes"], expires_at=row["expires"],
            client_id=row["client_id"], code_challenge=d["code_challenge"],
            redirect_uri=d["redirect_uri"], redirect_uri_provided_explicitly=d["explicit"],
            resource=d["resource"], subject=d["subject"])

    async def exchange_authorization_code(self, client, authorization_code):
        from mcp.server.auth.provider import TokenError
        now = self._now()
        family = secrets.token_hex(8)
        token = None
        with self._db() as con:
            cur = con.execute("UPDATE codes SET used=?, family=? WHERE code=? AND used IS NULL"
                              " AND expires>=?",
                              (now, family, _h(authorization_code.code), now))
            if cur.rowcount == 1:
                token = self._grant(con, client, authorization_code, family, now)
        if token is None:
            raise TokenError("invalid_grant", "authorization code is not valid")
        log.info("issued grant %s to client %s (%r) scope=%s access_ttl=%ss",
                 family, client.client_id, client.client_name,
                 " ".join(authorization_code.scopes), ACCESS_TTL)
        run_grant_hook(self.s.grant_hook, {
            "OZU_GRANT_EVENT": "new_grant", "OZU_GRANT_ID": family,
            "OZU_GRANT_CLIENT_ID": client.client_id,
            "OZU_GRANT_CLIENT_NAME": client.client_name or "",
            "OZU_GRANT_TIME": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))})
        return token

    def _grant(self, con, client, authorization_code, family: str, now: float):
        con.execute(
            "INSERT INTO families(family, client_id, client_name, subject, scopes, resource,"
            " created, expires) VALUES(?,?,?,?,?,?,?,?)",
            (family, client.client_id, client.client_name, authorization_code.subject,
             " ".join(authorization_code.scopes), authorization_code.resource or self.resource,
             now, now + FAMILY_TTL))
        con.execute("UPDATE clients SET used=1 WHERE client_id=?", (client.client_id,))
        return self._issue(con, family, authorization_code.scopes, now + FAMILY_TTL)

    def _issue(self, con, family: str, scopes: list[str], family_expires: float):
        from mcp.shared.auth import OAuthToken
        now = self._now()
        access = "ozua_" + secrets.token_urlsafe(32)
        refresh = "ozur_" + secrets.token_urlsafe(32)
        con.execute("INSERT INTO tokens(token, kind, family, expires) VALUES(?,?,?,?)",
                    (_h(access), "access", family, min(now + ACCESS_TTL, family_expires)))
        con.execute("INSERT INTO tokens(token, kind, family, expires) VALUES(?,?,?,?)",
                    (_h(refresh), "refresh", family, min(now + REFRESH_TTL, family_expires)))
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    def _live(self, con, token: str, kind: str):
        """(token row, family row) for a token that is unrevoked and unexpired;
        a rotated refresh token presented again revokes its family."""
        row = con.execute("SELECT * FROM tokens WHERE token=? AND kind=?",
                          (_h(token), kind)).fetchone()
        if not row:
            return None
        fam = con.execute("SELECT * FROM families WHERE family=?", (row["family"],)).fetchone()
        now = self._now()
        if not fam or fam["revoked"] or fam["expires"] < now or row["expires"] < now:
            return None
        if kind == "refresh" and row["used"]:
            if self._graced(token) is not None:
                return row, fam   # the same token re-sent within the grace: not a reuse
            self._revoke_family(con, fam["family"])
            log.warning("rotated refresh token reused; grant %s revoked", fam["family"])
            return None
        return row, fam

    async def load_refresh_token(self, client, refresh_token: str):
        from mcp.server.auth.provider import RefreshToken
        with self._db() as con:
            live = self._live(con, refresh_token, "refresh")
        if not live or live[1]["client_id"] != client.client_id:
            return None
        row, fam = live
        return RefreshToken(token=refresh_token, client_id=fam["client_id"],
                            scopes=fam["scopes"].split(), expires_at=int(row["expires"]),
                            resource=fam["resource"], subject=fam["subject"])

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]):
        from mcp.server.auth.provider import TokenError
        now = self._now()
        token = None
        with self._db() as con:
            live = self._live(con, refresh_token.token, "refresh")
            if live and con.execute("UPDATE tokens SET used=? WHERE token=? AND used IS NULL",
                                    (now, _h(refresh_token.token))).rowcount == 1:
                fam = live[1]
                con.execute("UPDATE families SET refreshed=? WHERE family=?",
                            (now, fam["family"]))
                token = self._issue(con, fam["family"], scopes or fam["scopes"].split(),
                                    fam["expires"])
            if token is not None:
                # The pair this token was rotated into, kept in memory only,
                # for a client that sends the same refresh twice at once.
                self._graced(None)   # drop expired pairs, so the cache holds seconds, not months
                self._grace[_h(refresh_token.token)] = (now + REFRESH_GRACE,
                                                        client.client_id, token)
        if token is None:
            graced = self._graced(refresh_token.token)
            if graced is not None and graced[0] == client.client_id:
                log.info("refresh re-sent within %ss; same pair returned", REFRESH_GRACE)
                return graced[1]
            # raised outside the transaction, so a reuse revocation sticks
            raise TokenError("invalid_grant", "refresh token is not valid")
        log.info("refreshed grant %s for client %s", fam["family"], client.client_id)
        return token

    def _graced(self, refresh_token: str | None):
        """Drop the expired pairs, then (client_id, pair) for `refresh_token`
        if it was rotated less than REFRESH_GRACE ago. Called on every
        rotation too, so no plaintext pair outlives its few seconds."""
        now = self._now()
        for k in [k for k, v in self._grace.items() if v[0] < now]:
            del self._grace[k]
        if refresh_token is None:
            return None
        hit = self._grace.get(_h(refresh_token))
        return None if hit is None else (hit[1], hit[2])

    async def load_access_token(self, token: str):
        from mcp.server.auth.provider import AccessToken
        if self.static_token and _same(token, self.static_token):
            return AccessToken(token=token, client_id="ozu", scopes=[FULL_SCOPE, READ_SCOPE],
                               resource=self.resource)
        if not token.startswith("ozua_") or len(token) > 128:
            return None
        with self._db() as con:
            live = self._live(con, token, "access")
            if live and self._client(con, live[1]["client_id"]) is None:
                live = None
        if not live:
            return None
        row, fam = live
        return AccessToken(token=token, client_id=fam["client_id"],
                           scopes=fam["scopes"].split(), expires_at=int(row["expires"]),
                           resource=fam["resource"], subject=fam["subject"])

    async def revoke_token(self, token) -> None:
        with self._db() as con:
            row = con.execute("SELECT family FROM tokens WHERE token=?",
                              (_h(token.token),)).fetchone()
            if row:
                self._revoke_family(con, row["family"])
                log.info("grant %s revoked by its client", row["family"])

    def _revoke_family(self, con, family: str) -> None:
        con.execute("UPDATE families SET revoked=? WHERE family=? AND revoked IS NULL",
                    (self._now(), family))
        self._grace.clear()   # rare; never hand out a pair of a revoked grant

    def registration_is_open(self) -> bool:
        """OZU_MCP_OAUTH_REGISTRATION=1, or a window `ozu mcp registration open`
        set that has not run out."""
        if self.s.registration_open:
            return True
        with self._db() as con:
            row = con.execute("SELECT value FROM meta WHERE key='registration_until'").fetchone()
        return bool(row) and float(row["value"]) > self._now()

    def _prune(self, con) -> None:
        """Forget what can no longer be used. Codes are kept an hour past
        expiry (replay detection), revoked or ended grants 30 days (the CLI
        lists them), dynamically registered clients nobody ever approved."""
        now = self._now()
        con.execute("DELETE FROM pending WHERE expires<?", (now,))
        con.execute("DELETE FROM codes WHERE expires<?", (now - 3600,))
        con.execute("DELETE FROM tokens WHERE expires<? OR family IN (SELECT family FROM "
                    "families WHERE revoked IS NOT NULL OR expires<?)", (now, now))
        con.execute("DELETE FROM families WHERE COALESCE(revoked, expires)<?",
                    (now - 30 * 86400,))
        # Only a client nobody ever approved is forgotten by itself. One the
        # owner approved stays, with or without a live grant: when its grant
        # ends (90 days) or is revoked it comes back through /authorize with
        # its cached client_id, and deleting it then left the owner at
        # "Application unknown" after the GitHub sign-in, with registration
        # (closed by default) the only way back. `ozu mcp client revoke`
        # removes one; MAX_CLIENTS bounds them.
        con.execute("DELETE FROM clients WHERE kind='dcr' AND used=0 AND created<?",
                    (now - CLIENT_UNUSED_TTL,))

    # ---- wiring
    def install(self, server) -> None:
        """Mount the three browser routes. The SDK mounts custom routes
        without authentication, which is what a sign-in page needs."""
        for path, method, handler in (("/oauth/start", "GET", self.start),
                                      ("/oauth/github/callback", "GET", self.github_callback),
                                      ("/oauth/consent", "POST", self.consent)):
            server.custom_route(path, methods=[method])(_body_limited(handler))


# ------------------------------------------------------------------ pages

_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _page(title: str, body: str, status: int = 200, form_to: str = ""):
    from starlette.responses import HTMLResponse
    csp = ("default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
           f"base-uri 'none'; form-action 'self'{(' ' + form_to) if form_to else ''}")
    doc = (f"<!doctype html><html lang=en><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>{html.escape(title)}</title>"
           "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:34rem;margin:2rem auto;"
           "padding:0 1rem}.host{font-size:1.6rem;font-weight:700}code{word-break:break-all}"
           "button{font-size:1rem;padding:.5rem 1.2rem;margin-right:.5rem}</style>"
           f"<h1>{html.escape(title)}</h1>{body}</html>")
    return HTMLResponse(doc, status_code=status, headers={
        **_NO_STORE, "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": csp})


def _consent_page(client, redirect_uri: str, gh_login: str, consent: str, flow: str):
    p = urlparse(redirect_uri)
    e = html.escape
    local = p.hostname in LOOPBACK
    warn = ("<p><b>This sends access to a program on your own computer.</b> Approve "
            "only if you just started this from Claude Code or another local client.</p>"
            if local else "")
    body = (
        f"<p>Signed in on GitHub as <b>{e(gh_login)}</b>.</p>"
        f"<p>The application <b>{e(client.client_name or 'unnamed')}</b> "
        f"(client <code>{e(client.client_id)}</code>) asks to <b>read</b> your notes "
        f"(scope <code>{READ_SCOPE}</code>). It cannot write to the vault.</p>"
        f"<p>Access will be sent to:<br><span class=host>{e(p.hostname or '')}</span><br>"
        f"<code>{e(redirect_uri)}</code></p>{warn}"
        "<p><b>If you did not just press Connect in Claude yourself, press Deny.</b></p>"
        "<form method=post action='/oauth/consent'>"
        f"<input type=hidden name=flow value='{e(flow, quote=True)}'>"
        f"<input type=hidden name=consent value='{e(consent, quote=True)}'>"
        "<button name=action value=approve>Approve</button>"
        "<button name=action value=deny>Deny</button></form>")
    return _page("Allow access to your Ozu vault?", body, 200,
                 form_to=f"{p.scheme}://{p.netloc}")


# ------------------------------------------------------------------- logs

class RedactQuery(logging.Filter):
    """uvicorn's access log prints the request line with its query string,
    which on these routes carries the GitHub code, the state and the flow id.
    Keep the path, drop the query."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) \
                and "?" in args[2]:
            record.args = args[:2] + (args[2].split("?", 1)[0] + "?…",) + args[3:]
        return True


def quiet_access_log() -> None:
    logging.getLogger("uvicorn.access").addFilter(RedactQuery())


# ------------------------------------------------------- edge, hook, body

def _body_limited(handler):
    """A custom route with the SDK's own body cap in front of it. The SDK
    caps the routes it mounts (/token answers 413) but not custom ones, and
    an unauthenticated 300 MB form was read whole into memory before this."""
    from starlette.routing import request_response
    from mcp.server.transport_security import RequestBodyLimitMiddleware
    return RequestBodyLimitMiddleware(request_response(handler), MAX_BODY)


def _env_value(v: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(v))[:200]


def run_grant_hook(argv: list[str], facts: dict[str, str]) -> None:
    """OZU_MCP_GRANT_HOOK: run `argv` (already split, never through a shell)
    in the background with only non-secret facts in its environment. It can
    neither block nor fail a grant: a crash, a non-zero exit or a hang past
    HOOK_TIMEOUT is logged and nothing else happens."""
    if not argv:
        return
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ") if k in os.environ}
    env.update({k: _env_value(v) for k, v in facts.items()})

    def run():
        try:
            r = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=HOOK_TIMEOUT, check=False)
            if r.returncode:
                log.warning("grant hook exited with status %s", r.returncode)
        except subprocess.TimeoutExpired:
            log.warning("grant hook killed after %ss", HOOK_TIMEOUT)
        except (OSError, ValueError) as exc:
            log.warning("grant hook could not run (%s)", type(exc).__name__)

    # The grant is already committed when this runs: a thread that cannot be
    # started (RLIMIT_NPROC, TasksMax, memory) must not turn it into a 500,
    # whose retry would then look like a code replay and revoke it.
    try:
        threading.Thread(target=run, name="ozu-grant-hook", daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - nothing here may fail the grant
        log.warning("grant hook could not be started (%s)", type(exc).__name__)


class OAuthEdge:
    """The outermost ASGI layer in OAuth mode, in front of the SDK's app.

    - The static bearer never expires and carries every tool, so it is
      refused on any request that came through the reverse proxy (one that
      carries X-Forwarded-For or Forwarded) unless OZU_MCP_STATIC_BEARER_REMOTE=1.
      This relies on the proxy always setting X-Forwarded-For (Caddy does,
      and deploy/caddy-mcp.conf sets it explicitly): a client cannot remove a
      header the proxy adds.
    - It serves the authorization-server metadata itself, so that it can say
      `authorization_response_iss_parameter_supported` and advertise the
      registration endpoint only while registration is open.
    - While registration is closed, /register answers 404.
    """

    def __init__(self, app, provider: "GitHubOAuthProvider", auth_settings):
        self.app, self.p, self.auth = app, provider, auth_settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse, Response
        headers = Headers(scope=scope)
        path, method = scope.get("path", ""), scope.get("method", "")
        auth = headers.get("authorization", "")
        if (not self.p.s.static_remote and self.p.static_token
                and ("x-forwarded-for" in headers or "forwarded" in headers)
                and auth[:7].lower() == "bearer " and _same(auth[7:], self.p.static_token)):
            log.warning("static bearer refused on a proxied request")
            prm = self.p.issuer + "/.well-known/oauth-protected-resource/mcp"
            return await JSONResponse(
                {"error": "invalid_token",
                 "error_description": "this token is not accepted through the proxy"},
                status_code=401, headers={"WWW-Authenticate": (
                    'Bearer error="invalid_token", error_description="this token is not '
                    f'accepted through the proxy", resource_metadata="{prm}"')})(
                scope, receive, send)
        if path == "/.well-known/oauth-authorization-server" and method == "GET":
            return await self._metadata()(scope, receive, send)
        if path.rstrip("/") == "/register" and not self.p.registration_is_open():
            return await Response("Not Found", status_code=404)(scope, receive, send)
        return await self.app(scope, receive, send)

    def _metadata(self):
        from mcp.server.auth.json_response import PydanticJSONResponse
        from mcp.server.auth.routes import build_metadata
        from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
        md = build_metadata(
            self.auth.issuer_url, self.auth.service_documentation_url,
            ClientRegistrationOptions(enabled=self.p.registration_is_open(),
                                      valid_scopes=[READ_SCOPE]),
            RevocationOptions(enabled=True))
        md.authorization_response_iss_parameter_supported = True
        return PydanticJSONResponse(content=md, headers={
            "Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"})


def create_client(db_path: Path, name: str, redirect_uris: list[str],
                  auth_method: str = "client_secret_post") -> tuple[str, str]:
    """A pre-registered confidential client: (client_id, client_secret). The
    secret is stored as issued (the SDK's client check compares the stored
    value, so a hash cannot satisfy it); the file is 0600 and the secret is
    worth nothing without a code, which needs the owner's GitHub sign-in."""
    from mcp.shared.auth import OAuthClientInformationFull
    cid, secret = "ozu-" + secrets.token_hex(8), secrets.token_urlsafe(32)
    now = time.time()
    info = OAuthClientInformationFull(
        client_id=cid, client_secret=secret, client_id_issued_at=int(now),
        client_secret_expires_at=0, client_name=name, redirect_uris=redirect_uris,
        token_endpoint_auth_method=auth_method, scope=READ_SCOPE,
        grant_types=["authorization_code", "refresh_token"], response_types=["code"])
    con = connect(db_path)
    with con:
        con.execute("INSERT INTO clients(client_id, info, created, used, kind)"
                    " VALUES(?,?,?,1,'static')", (cid, info.model_dump_json(), now))
    con.close()
    log.info("pre-registered client %s name=%r", cid, name)
    return cid, secret


# -------------------------------------------------------------------- CLI

def _fmt(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


USAGE = """usage: ozu mcp tokens list | tokens revoke <grant-id> | tokens revoke --client <id>
                     | tokens revoke --all
       ozu mcp client create [--name NAME] [--auth post|basic] | client list
                     | client revoke <client-id>
       ozu mcp registration open [MINUTES] | registration close | registration status"""


def cli(args: list[str], cfg) -> int:
    """`ozu mcp tokens|client|registration ...`. Prints ids and metadata,
    never a token; `client create` prints its new secret once."""
    db_path = cfg.mcp_oauth_db
    group, rest = (args[0], args[1:]) if args else ("", [])
    if group == "client" and rest[:1] == ["create"]:
        name, method = "Claude", "client_secret_post"
        it = iter(rest[1:])
        for a in it:
            if a == "--name":
                name = next(it, name)[:MAX_NAME_LEN]
            elif a == "--auth":
                method = {"post": "client_secret_post",
                          "basic": "client_secret_basic"}.get(next(it, ""), "")
        if not method or not cfg.mcp_oauth_redirect_uris:
            print(USAGE if not method else "OZU_MCP_OAUTH_REDIRECT_URIS is empty")
            return 2
        cid, secret = create_client(db_path, name, list(cfg.mcp_oauth_redirect_uris), method)
        print(f"client id:     {cid}\nclient secret: {secret}\n"
              f"redirect URIs: {', '.join(cfg.mcp_oauth_redirect_uris)}\n"
              f"auth method:   {method}\n"
              "The secret is shown this once. Enter both in the connector's settings.")
        return 0
    # A registration window is written before the server ever ran (the
    # runbook opens one, then starts it), so that group creates the state
    # file; the others have nothing to show or revoke without one.
    if group != "registration" and not db_path.exists():
        print("no OAuth state yet (OAuth has never been on)")
        return 0
    con = connect(db_path)
    now = time.time()
    try:
        if group == "client" and rest == ["list"]:
            for r in con.execute("SELECT * FROM clients ORDER BY created"):
                info = json.loads(r["info"])
                live = con.execute("SELECT COUNT(*) FROM families WHERE client_id=? AND "
                                   "revoked IS NULL AND expires>=?",
                                   (r["client_id"], now)).fetchone()[0]
                print(f"{r['client_id']}  {r['kind']:6} name={info.get('client_name')!r} "
                      f"auth={info.get('token_endpoint_auth_method')} "
                      f"created={_fmt(r['created'])} live_grants={live}")
            return 0
        if group == "client" and len(rest) == 2 and rest[0] == "revoke":
            with con:
                n, gone = _revoke_client(con, rest[1], now)
            print(f"client {'removed' if gone else 'not found'}; revoked {n} grant(s)")
            return 0
        if group == "registration" and rest[:1] == ["open"]:
            minutes = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 10
            minutes = max(1, min(minutes, REGISTRATION_WINDOW_MAX))
            with con:
                con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES"
                            "('registration_until', ?)", (str(now + 60 * minutes),))
            log.info("dynamic registration opened for %d min from the CLI", minutes)
            print(f"registration open until {_fmt(now + 60 * minutes)} ({minutes} min)")
            return 0
        if group == "registration" and rest in (["close"], ["status"]):
            if rest == ["close"]:
                with con:
                    con.execute("DELETE FROM meta WHERE key='registration_until'")
            row = con.execute("SELECT value FROM meta WHERE key='registration_until'").fetchone()
            always = cfg.mcp_oauth_registration == "1"
            until = float(row["value"]) if row else 0
            print("registration: " + ("open (OZU_MCP_OAUTH_REGISTRATION=1)" if always else
                                      f"open until {_fmt(until)}" if until > now else "closed"))
            return 0
        if group == "tokens":
            return _tokens(con, rest, now)
    finally:
        con.close()
    print(USAGE)
    return 2


def _revoke_client(con, client_id: str, now: float) -> tuple[int, bool]:
    """Revoke every grant of a client and forget it: (grants revoked, whether
    the client existed). `client revoke` and `tokens revoke --client`."""
    n = con.execute("UPDATE families SET revoked=? WHERE client_id=? AND revoked IS NULL",
                    (now, client_id)).rowcount
    gone = con.execute("DELETE FROM clients WHERE client_id=?", (client_id,)).rowcount
    log.info("client %s %s from the CLI; revoked %d grant(s)", client_id,
             "removed" if gone else "not found", n)
    return n, bool(gone)


def _tokens(con, args: list[str], now: float) -> int:
    if args[:1] == ["list"]:
        rows = con.execute("SELECT * FROM families ORDER BY created").fetchall()
        for f in rows:
            status = ("revoked" if f["revoked"] else "expired" if f["expires"] < now
                      else "active")
            print(f"{f['family']}  {status:8} client={f['client_id']} "
                  f"name={f['client_name']!r} subject={f['subject']} scope={f['scopes']} "
                  f"created={_fmt(f['created'])} refreshed={_fmt(f['refreshed'])} "
                  f"ends={_fmt(f['expires'])}")
        print(f"{len(rows)} grant(s)")
        return 0
    if args[:1] == ["revoke"] and len(args) >= 2:
        with con:
            if args[1] == "--all":
                n = con.execute("UPDATE families SET revoked=? WHERE revoked IS NULL",
                                (now,)).rowcount
            elif args[1] == "--client" and len(args) == 3:
                n, _ = _revoke_client(con, args[2], now)
            else:
                n = con.execute("UPDATE families SET revoked=? WHERE family=? AND "
                                "revoked IS NULL", (now, args[1])).rowcount
        log.info("revoked %d grant(s) from the CLI", n)
        print(f"revoked {n} grant(s)")
        return 0
    print(USAGE)
    return 2
