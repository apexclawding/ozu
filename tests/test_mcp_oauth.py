"""OAuth for the MCP server (docs/mcp-oauth.md), end to end through the real
SDK app, with GitHub faked by an httpx MockTransport: no network, every path
under tmp_path.

Values below are obviously fake on purpose; none looks like a real secret.
"""
import base64
import hashlib
import importlib
import json
import logging
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

pytest.importorskip("mcp.server.auth.provider")
from starlette.testclient import TestClient  # noqa: E402

from test_mcp_wall import MINE, TOKEN, WALLED, _plant  # noqa: E402

PUBLIC = "http://127.0.0.1:8399"
RESOURCE = PUBLIC + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
STATIC = "static-bearer-for-tests"
GH_SECRET = "fake-gh-app-secret"
GH_TOKEN = "fake-gh-user-token"
PINNED_ID, PINNED_LOGIN = 4242, "vault-owner"
VERIFIER = "v" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


class FakeGitHub:
    """github.com + api.github.com, as the provider uses them."""

    def __init__(self):
        self.user = {"id": PINNED_ID, "login": PINNED_LOGIN}
        self.down = False
        self.challenge = None
        self.deleted = []
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/login/oauth/access_token":
            form = parse_qs(request.content.decode())
            ok = (form["client_secret"] == [GH_SECRET]
                  and form["redirect_uri"] == [PUBLIC + "/oauth/github/callback"]
                  and self.challenge == base64.urlsafe_b64encode(hashlib.sha256(
                      form["code_verifier"][0].encode()).digest()).decode().rstrip("="))
            return httpx.Response(200, json={"access_token": GH_TOKEN} if ok
                                  else {"error": "bad_verification_code"})
        if request.url.path == "/user":
            if request.headers["authorization"] != f"Bearer {GH_TOKEN}":
                return httpx.Response(401, json={})
            return httpx.Response(200, json=self.user)
        if request.method == "DELETE" and request.url.path.endswith("/token"):
            self.deleted.append(json.loads(request.content)["access_token"])
            return httpx.Response(204)
        return httpx.Response(404)

    def factory(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("OZU_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    for k in ("OZU_ALLOW_WORK_MCP", "OZU_MCP_OAUTH", "OZU_MCP_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    import ozu.config as config
    config._config = None
    import ozu.db
    importlib.reload(ozu.db)
    import ozu.mcp_server as srv
    importlib.reload(srv)
    from ozu import mcp_oauth
    gh, clock = FakeGitHub(), Clock()
    settings = mcp_oauth.OAuthSettings(
        public_url=PUBLIC, github_client_id="fake-gh-client", github_client_secret=GH_SECRET,
        github_login=PINNED_LOGIN, github_user_id=PINNED_ID,
        db_path=tmp_path / "ozu" / "mcp-oauth.db", redirect_uris=[REDIRECT])
    provider = mcp_oauth.GitHubOAuthProvider(settings, static_token=STATIC,
                                             http_factory=gh.factory, clock=clock)
    server = srv.build_server(STATIC, oauth=provider)
    app = server.streamable_http_app(transport_security=srv.transport_security())
    _plant(srv, tmp_path, "work")
    with TestClient(app, base_url=PUBLIC) as client:
        yield type("Env", (), dict(c=client, gh=gh, clock=clock, provider=provider,
                                   srv=srv, mod=mcp_oauth, tmp=tmp_path))
    config._config = None


# ------------------------------------------------------------------ steps

def register(c, uris=(REDIRECT,), name="Claude"):
    return c.post("/register", json={
        "redirect_uris": list(uris), "client_name": name, "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})


def authorize(c, client_id, redirect=REDIRECT, **extra):
    q = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect,
         "code_challenge": CHALLENGE, "code_challenge_method": "S256", "state": "client-st",
         "scope": "vault:read", "resource": RESOURCE, **extra}
    return c.get("/authorize?" + urlencode(q), follow_redirects=False)


def to_github(e, client_id):
    r = authorize(e.c, client_id)
    assert r.status_code == 302, r.text
    start = urlparse(r.headers["location"])
    assert start.path == "/oauth/start"
    r = e.c.get(start.path + "?" + start.query, follow_redirects=False)
    assert r.status_code == 302
    gh = urlparse(r.headers["location"])
    q = parse_qs(gh.query, keep_blank_values=True)
    assert q["code_challenge_method"] == ["S256"] and q["scope"] == [""]
    e.gh.challenge = q["code_challenge"][0]
    return q["state"][0]


def consent_page(e, client_id):
    state = to_github(e, client_id)
    return state, e.c.get("/oauth/github/callback?" + urlencode(
        {"code": "fake-gh-code", "state": state}))


def approve(e, page, action="approve"):
    flow = re.search(r"name=flow value='([^']+)'", page.text).group(1)
    tok = re.search(r"name=consent value='([^']+)'", page.text).group(1)
    return e.c.post("/oauth/consent", data={"flow": flow, "consent": tok, "action": action},
                    follow_redirects=False)


def get_code(e):
    cid = register(e.c).json()["client_id"]
    _, page = consent_page(e, cid)
    assert page.status_code == 200, page.text
    r = approve(e, page)
    assert r.status_code == 303
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT
    assert q["state"] == ["client-st"] and q["iss"] == [PUBLIC]
    return cid, q["code"][0]


def token(c, cid, code, verifier=VERIFIER, redirect=REDIRECT):
    return c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                  "redirect_uri": redirect, "client_id": cid,
                                  "code_verifier": verifier, "resource": RESOURCE})


def refresh(c, cid, rt):
    return c.post("/token", data={"grant_type": "refresh_token", "refresh_token": rt,
                                  "client_id": cid})


def grant(e):
    cid, code = get_code(e)
    r = token(e.c, cid, code)
    assert r.status_code == 200, r.text
    return cid, r.json()


def rpc(c, bearer, method, params=None, session=None, id_=1):
    headers = {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        body["id"] = id_
    return c.post("/mcp", headers=headers, json=body)


def _json(r):
    if r.headers.get("content-type", "").startswith("text/event-stream"):
        data = [ln[5:].strip() for ln in r.text.splitlines() if ln.startswith("data:")]
        return json.loads(data[-1])
    return r.json()


def session(c, bearer):
    r = rpc(c, bearer, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200, (r.status_code, r.text)
    sid = r.headers.get("mcp-session-id")
    rpc(c, bearer, "notifications/initialized", session=sid, id_=None)
    return sid


def call(c, bearer, sid, name, **args):
    r = rpc(c, bearer, "tools/call", {"name": name, "arguments": args}, session=sid, id_=7)
    assert r.status_code == 200, (r.status_code, r.text)
    return _json(r)["result"]


def tool_names(c, bearer, sid):
    return {t["name"] for t in _json(rpc(c, bearer, "tools/list", session=sid, id_=3))
            ["result"]["tools"]}


# ------------------------------------------------------------------ tests

def test_discovery_and_401_point_at_this_server(env):
    r = env.c.post("/mcp", json={})
    assert r.status_code == 401
    assert ('resource_metadata="http://127.0.0.1:8399/.well-known/oauth-protected-resource/mcp"'
            in r.headers["www-authenticate"])
    prm = env.c.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == RESOURCE and prm["scopes_supported"] == ["vault:read"]
    assert prm["authorization_servers"] == [PUBLIC]
    asm = env.c.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["registration_endpoint"] == PUBLIC + "/register"


def test_happy_path_reads_but_cannot_capture(env):
    cid, tok = grant(env)
    assert tok["scope"] == "vault:read" and tok["expires_in"] == 3600
    assert tok["access_token"].startswith("ozua_") and tok["refresh_token"].startswith("ozur_")
    assert env.gh.deleted == [GH_TOKEN]   # GitHub's token was thrown away at GitHub
    at = tok["access_token"]
    sid = session(env.c, at)
    assert "capture" not in tool_names(env.c, at, sid)
    res = call(env.c, at, sid, "get_note", note_id=MINE)
    assert not res.get("isError") and "harmless" in json.dumps(res)
    res = call(env.c, at, sid, "capture", text="should never land")
    assert res["isError"] and "requires scope ozu" in json.dumps(res)
    assert not list((env.tmp / "vault").rglob("*should-never*"))


def test_static_bearer_keeps_every_tool_in_oauth_mode(env):
    sid = session(env.c, STATIC)
    assert "capture" in tool_names(env.c, STATIC, sid)
    res = call(env.c, STATIC, sid, "capture", text="from the box", title="Box note")
    assert not res.get("isError"), res
    assert "Captured as" in json.dumps(res)


def test_walled_content_is_as_unserved_over_oauth_as_over_the_bearer(env):
    _, tok = grant(env)
    at = tok["access_token"]
    calls = [("get_note", {"note_id": WALLED}), ("search_notes", {"query": TOKEN}),
             ("search_notes", {"query": TOKEN, "vault": "work"}),
             ("search_notes", {"query": "Walled"}), ("open_actions", {}),
             ("graph_neighbors", {"note_id": MINE, "depth": 3}),
             ("graph_neighbors", {"note_id": WALLED, "depth": 2}),
             ("timeline", {"topic": "shared"})]
    sa, so = session(env.c, STATIC), session(env.c, at)
    for name, args in calls:
        over_oauth = call(env.c, at, so, name, **args)
        over_bearer = call(env.c, STATIC, sa, name, **args)
        assert TOKEN not in json.dumps(over_oauth).lower(), (name, args)
        assert over_oauth == over_bearer, (name, args)


def test_wrong_github_account_is_refused(env):
    env.gh.user = {"id": 999, "login": PINNED_LOGIN}   # login reused by someone else
    cid = register(env.c).json()["client_id"]
    _, page = consent_page(env, cid)
    assert page.status_code == 403 and "may not approve" in page.text
    env.gh.user = {"id": PINNED_ID, "login": "renamed"}  # right id, renamed: fail closed
    _, page = consent_page(env, cid)
    assert page.status_code == 403


def test_github_down_fails_closed(env):
    cid = register(env.c).json()["client_id"]
    state = to_github(env, cid)
    env.gh.down = True
    r = env.c.get("/oauth/github/callback?" + urlencode({"code": "x", "state": state}))
    assert r.status_code == 502 and "nothing was granted" in r.text.lower()
    env.gh.down = False   # the pending authorization is gone, not resumable
    r = env.c.get("/oauth/github/callback?" + urlencode({"code": "x", "state": state}))
    assert r.status_code == 400


def test_missing_or_wrong_state_or_cookie_is_refused(env):
    cid = register(env.c).json()["client_id"]
    state = to_github(env, cid)
    for q in ({"code": "x"}, {"code": "x", "state": "not-the-state"}):
        assert env.c.get("/oauth/github/callback?" + urlencode(q)).status_code == 400
    env.c.cookies.clear()   # the right state from another browser
    r = env.c.get("/oauth/github/callback?" + urlencode({"code": "x", "state": state}))
    assert r.status_code == 400
    assert ("GET", "/login/oauth/access_token") not in env.gh.calls


def test_consent_needs_its_token_and_is_single_use(env):
    cid = register(env.c).json()["client_id"]
    _, page = consent_page(env, cid)
    flow = re.search(r"name=flow value='([^']+)'", page.text).group(1)
    r = env.c.post("/oauth/consent", data={"flow": flow, "consent": "forged",
                                           "action": "approve"}, follow_redirects=False)
    assert r.status_code == 400
    assert approve(env, page).status_code == 303
    assert approve(env, page).status_code == 400


def test_consent_page_names_client_and_redirect_and_cannot_be_framed(env):
    cid = register(env.c, name="Claude <b>x</b>").json()["client_id"]
    _, page = consent_page(env, cid)
    assert "claude.ai" in page.text and REDIRECT in page.text and cid in page.text
    assert "<b>x</b>" not in page.text   # escaped
    assert page.headers["x-frame-options"] == "DENY"
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "form-action 'self' https://claude.ai" in csp


def test_deny_redirects_access_denied(env):
    cid = register(env.c).json()["client_id"]
    _, page = consent_page(env, cid)
    q = parse_qs(urlparse(approve(env, page, "deny").headers["location"]).query)
    assert q["error"] == ["access_denied"] and "code" not in q


def test_code_replay_is_refused_and_revokes_the_grant(env):
    cid, code = get_code(env)
    first = token(env.c, cid, code)
    assert first.status_code == 200
    again = token(env.c, cid, code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"
    assert env.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + first.json()["access_token"]}).status_code == 401


def test_pkce_verifier_mismatch_is_refused(env):
    cid, code = get_code(env)
    r = token(env.c, cid, code, verifier="w" * 64)
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant"


def test_redirect_uri_must_be_exactly_registered_and_allowlisted(env):
    r = register(env.c, uris=["https://evil.example/cb"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_redirect_uri"
    r = register(env.c, uris=["http://127.0.0.1:9999/cb"])   # loopback is opt-in
    assert r.status_code == 400
    cid = register(env.c).json()["client_id"]
    for other in (REDIRECT + "/", REDIRECT + "?x=1", "https://claude.com/api/mcp/auth_callback"):
        r = authorize(env.c, cid, redirect=other)
        assert r.status_code == 400 and "location" not in r.headers, other
    cid, code = get_code(env)
    r = token(env.c, cid, code, redirect="https://claude.com/api/mcp/auth_callback")
    assert r.status_code == 400


def test_wrong_resource_and_scope_are_refused(env):
    cid = register(env.c).json()["client_id"]
    q = parse_qs(urlparse(authorize(env.c, cid, resource="https://other.example/mcp")
                          .headers["location"]).query)
    assert q["error"] == ["invalid_target"]
    r = authorize(env.c, cid, scope="ozu")
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["invalid_scope"]
    r = env.c.post("/register", json={"redirect_uris": [REDIRECT], "scope": "ozu",
                                      "token_endpoint_auth_method": "none"})
    assert r.status_code == 400


def test_expired_code_and_token_are_refused(env):
    cid, code = get_code(env)
    env.clock.t += 121
    assert token(env.c, cid, code).json()["error"] == "invalid_grant"
    env.clock.t -= 121
    _, tok = grant(env)
    at = tok["access_token"]
    assert env.c.post("/mcp", json={}, headers={"Authorization": f"Bearer {at}"}) \
        .status_code != 401
    env.clock.t += 3601
    assert env.c.post("/mcp", json={}, headers={"Authorization": f"Bearer {at}"}) \
        .status_code == 401


def test_refresh_rotates_and_reuse_kills_the_grant(env):
    cid, tok = grant(env)
    r = refresh(env.c, cid, tok["refresh_token"])
    assert r.status_code == 200
    new = r.json()
    assert new["refresh_token"] != tok["refresh_token"]
    old = refresh(env.c, cid, tok["refresh_token"])
    assert old.status_code == 400 and old.json()["error"] == "invalid_grant"
    # Reuse of a rotated token is theft or a bug either way: the whole grant dies.
    assert refresh(env.c, cid, new["refresh_token"]).json()["error"] == "invalid_grant"
    assert env.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + new["access_token"]}).status_code == 401


def test_revoked_token_is_refused_endpoint_and_cli(env, capsys):
    cid, tok = grant(env)
    # The SDK's revocation model requires the client_secret field even for a
    # public client, so a public client sends it empty.
    r = env.c.post("/revoke", data={"token": tok["access_token"], "client_id": cid,
                                    "client_secret": ""})
    assert r.status_code == 200
    assert env.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + tok["access_token"]}).status_code == 401
    assert refresh(env.c, cid, tok["refresh_token"]).status_code == 400

    cid, tok = grant(env)
    db = env.provider.s.db_path
    assert env.mod.cli(["list"], db) == 0
    out = capsys.readouterr().out
    assert "active" in out and "revoked" in out
    assert tok["access_token"] not in out and tok["refresh_token"] not in out
    assert env.mod.cli(["revoke", "--all"], db) == 0
    assert env.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + tok["access_token"]}).status_code == 401


def test_registration_grants_nothing_and_is_bounded(env):
    r = register(env.c)
    cid = r.json()["client_id"]
    assert r.status_code == 201
    # A registered client alone gets no token: there is no code to redeem.
    r = token(env.c, cid, "ozuc_made-up")
    assert r.status_code == 400
    assert register(env.c, uris=[REDIRECT] * 5).status_code == 400
    assert register(env.c, name="n" * 101).status_code == 400
    for _ in range(env.mod.MAX_REGISTRATIONS_PER_HOUR - 1):
        assert register(env.c).status_code == 201
    assert register(env.c).status_code == 400
    # Never used, so forgotten after an hour; the limit then frees up.
    env.clock.t += env.mod.CLIENT_UNUSED_TTL + 1
    assert authorize(env.c, cid).status_code == 400
    assert register(env.c).status_code == 201


def test_no_secret_reaches_the_logs_or_a_body(env, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = []
    cid = register(env.c).json()["client_id"]
    state, page = consent_page(env, cid)  # the consent page carries its own form fields
    consent_tok = re.search(r"name=consent value='([^']+)'", page.text).group(1)
    r = approve(env, page)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = token(env.c, cid, code).json()
    bodies.append(refresh(env.c, cid, tok["refresh_token"]).text)
    env.gh.user = {"id": 1, "login": "x"}
    bodies.append(consent_page(env, cid)[1].text)
    env.gh.user = {"id": PINNED_ID, "login": PINNED_LOGIN}
    env.gh.down = True
    bodies.append(consent_page(env, cid)[1].text)
    secrets_ = [GH_SECRET, GH_TOKEN, STATIC, code, tok["access_token"], tok["refresh_token"],
                consent_tok, state, "fake-gh-code"]
    # The test client's own request lines are not the server's logs.
    server_logs = "\n".join(r.getMessage() for r in caplog.records
                            if PUBLIC not in r.getMessage())
    text = server_logs + "".join(bodies)
    assert "issued grant" in caplog.text and "github identity accepted" in caplog.text
    leaked = [s for s in secrets_ if s in text]
    assert not leaked
    assert GH_SECRET not in repr(env.provider.s)


def test_access_log_query_strings_are_dropped():
    from ozu.mcp_oauth import RedactQuery
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                            ("1.2.3.4:5", "GET", "/oauth/github/callback?code=c0de&state=s",
                             "1.1", 200), None)
    RedactQuery().filter(rec)
    assert "c0de" not in rec.getMessage() and "/oauth/github/callback?" in rec.getMessage()


def test_state_file_is_private_and_holds_no_token(env):
    import stat
    _, tok = grant(env)
    db = env.provider.s.db_path
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    raw = db.read_bytes()
    assert tok["access_token"].encode() not in raw and tok["refresh_token"].encode() not in raw


# ------------------------------------------------------- off / half-configured

OAUTH_ENV = {"OZU_MCP_OAUTH": "1", "OZU_MCP_TOKEN": STATIC,
             "OZU_MCP_PUBLIC_URL": "https://ozu.example.com",
             "OZU_MCP_GITHUB_CLIENT_ID": "fake-gh-client",
             "OZU_MCP_GITHUB_CLIENT_SECRET": GH_SECRET,
             "OZU_MCP_GITHUB_LOGIN": PINNED_LOGIN, "OZU_MCP_GITHUB_USER_ID": str(PINNED_ID)}


def _main_with(monkeypatch, tmp_path, **env):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "home"))
    for k in list(OAUTH_ENV) + ["OZU_MCP_OAUTH_REDIRECT_URIS"]:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["ozu", "--http"])
    import ozu.config as config
    from ozu import mcp_server
    config._config = None
    try:
        with pytest.raises(SystemExit) as exc:
            mcp_server.main()
    finally:
        config._config = None
    return str(exc.value)


@pytest.mark.parametrize("missing", [k for k in OAUTH_ENV
                                     if k not in ("OZU_MCP_OAUTH", "OZU_MCP_TOKEN")])
def test_half_configured_oauth_refuses_to_start(monkeypatch, tmp_path, missing):
    env = {k: v for k, v in OAUTH_ENV.items() if k != missing}
    msg = _main_with(monkeypatch, tmp_path, **env)
    assert missing in msg and "will not start" in msg
    assert GH_SECRET not in msg and STATIC not in msg


@pytest.mark.parametrize("key,value", [
    ("OZU_MCP_OAUTH", "true"), ("OZU_MCP_GITHUB_USER_ID", "vault-owner"),
    ("OZU_MCP_PUBLIC_URL", "http://ozu.example.com"),
    ("OZU_MCP_PUBLIC_URL", "https://ozu.example.com/mcp"),
    ("OZU_MCP_OAUTH_REDIRECT_URIS", "http://evil.example/cb")])
def test_malformed_oauth_settings_refuse_to_start(monkeypatch, tmp_path, key, value):
    msg = _main_with(monkeypatch, tmp_path, **{**OAUTH_ENV, key: value})
    assert key in msg and GH_SECRET not in msg


def test_oauth_off_adds_no_route(env, monkeypatch):
    srv = env.srv
    app = srv.build_server(STATIC).streamable_http_app(
        transport_security=srv.transport_security())
    with TestClient(app, base_url=PUBLIC) as c:
        for path in ("/.well-known/oauth-authorization-server", "/oauth/start"):
            assert c.get(path).status_code == 404
        assert c.post("/register", json={}).status_code == 404
        assert c.post("/mcp", json={}).status_code == 401
        # An OAuth-minted token means nothing to the bearer-only server.
        assert c.post("/mcp", json={}, headers={
            "Authorization": "Bearer ozua_anything"}).status_code == 401
