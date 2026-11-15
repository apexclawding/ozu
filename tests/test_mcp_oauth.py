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

import pytest

pytest.importorskip("mcp.server.auth.provider")
from starlette.testclient import TestClient  # noqa: E402

from ozu.mcp_oauth import http_lib  # noqa: E402

httpx = http_lib()   # the client the SDK ships (httpx2 on mcp 2.x)

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


def make_env(tmp_path, monkeypatch, **settings_kw):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("OZU_MCP_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("OZU_MCP_OAUTH_REDIRECT_URIS", REDIRECT)
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
        github_client_id="fake-gh-client", github_client_secret=GH_SECRET,
        github_login=PINNED_LOGIN, github_user_id=PINNED_ID,
        **{"db_path": tmp_path / "ozu" / "mcp-oauth.db", "public_url": PUBLIC, "redirect_uris": [REDIRECT], "registration_open": True,
           **settings_kw})
    provider = mcp_oauth.GitHubOAuthProvider(settings, static_token=STATIC,
                                             http_factory=gh.factory, clock=clock)
    server = srv.build_server(STATIC, oauth=provider)
    app = srv.oauth_app(server, provider)
    _plant(srv, tmp_path, "work")
    return type("Env", (), dict(gh=gh, clock=clock, provider=provider, srv=srv, server=server,
                                app=app, mod=mcp_oauth, tmp=tmp_path, cfg=config.get_config()))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Dynamic registration open, as the DCR tests need. The default posture
    (closed, a pre-registered client) has its own fixture below."""
    e = make_env(tmp_path, monkeypatch)
    with TestClient(e.app, base_url=PUBLIC) as client:
        e.c = client
        yield e
    import ozu.config as config
    config._config = None


@pytest.fixture()
def closed(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, registration_open=False)
    with TestClient(e.app, base_url=PUBLIC) as client:
        e.c = client
        yield e
    import ozu.config as config
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


def get_code(e, name="Claude"):
    cid = register(e.c, name=name).json()["client_id"]
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


def grant(e, name="Claude"):
    cid, code = get_code(e, name)
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
    assert res["isError"] and "not available to this token" in json.dumps(res)
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
    # Re-sent at once by the same client (two requests in flight): the same pair.
    again = refresh(env.c, cid, tok["refresh_token"])
    assert again.status_code == 200 and again.json() == new
    env.clock.t += env.mod.REFRESH_GRACE + 1
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
    assert env.mod.cli(["tokens", "list"], env.cfg) == 0
    out = capsys.readouterr().out
    assert "active" in out and "revoked" in out
    assert tok["access_token"] not in out and tok["refresh_token"] not in out
    assert env.mod.cli(["tokens", "revoke", "--all"], env.cfg) == 0
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
    for k in list(OAUTH_ENV) + ["OZU_MCP_OAUTH_REDIRECT_URIS", "OZU_MCP_OAUTH_REGISTRATION",
                                "OZU_MCP_STATIC_BEARER_REMOTE", "OZU_MCP_GRANT_HOOK"]:
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
    ("OZU_MCP_OAUTH_REDIRECT_URIS", "http://evil.example/cb"),
    ("OZU_MCP_OAUTH_REGISTRATION", "yes"), ("OZU_MCP_STATIC_BEARER_REMOTE", "true"),
    ("OZU_MCP_GRANT_HOOK", '"unclosed')])
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


# ------------------------------------------------- security review, 2026-11-14

def _asgi(app, method, path, chunks=(), headers=()):
    """Drive the ASGI app by hand, counting how much of the body it read."""
    import asyncio
    sent, read = [], [0]
    pending = list(chunks)

    async def receive():
        if not pending:
            return {"type": "http.request", "body": b"", "more_body": False}
        read[0] += 1
        body = pending.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(msg):
        sent.append(msg)

    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": b"", "scheme": "http", "server": ("127.0.0.1", 8399),
             "client": ("127.0.0.1", 5), "http_version": "1.1", "root_path": "",
             "headers": [(b"host", b"127.0.0.1:8399"), *headers]}
    asyncio.run(app(scope, receive, send))
    return next(m["status"] for m in sent if m["type"] == "http.response.start"), read[0]


@pytest.mark.parametrize("method,path", [("POST", "/oauth/consent"), ("GET", "/oauth/start"),
                                         ("GET", "/oauth/github/callback")])
def test_added_routes_stop_reading_an_oversized_body(tmp_path, monkeypatch, method, path):
    """Finding 1: one unauthenticated 300 MB form went into memory whole."""
    e = make_env(tmp_path, monkeypatch)
    chunk = b"a" * 4096
    status, read = _asgi(e.app, method, path, [chunk] * 1000, headers=[
        (b"content-type", b"application/x-www-form-urlencoded")])
    assert status == 413
    assert read <= e.mod.MAX_BODY // len(chunk) + 1   # stopped at the cap, not at 4 MB
    status, read = _asgi(e.app, method, path, [chunk], headers=[
        (b"content-type", b"application/x-www-form-urlencoded"),
        (b"content-length", b"300000000")])
    assert status == 413 and read == 0                # refused on the declared size alone


def test_an_unclassified_tool_refuses_start_and_is_unreachable(tmp_path, monkeypatch):
    """Finding 2: deny by default, on the registered name."""
    e = make_env(tmp_path, monkeypatch)
    e.srv.check_tools_classified(e.server)            # the built-ins are all classified

    @e.server.tool(name="sneaky")
    def sneaky() -> str:
        return "should not be served"
    with pytest.raises(SystemExit) as exc:
        e.srv.check_tools_classified(e.server)
    assert "sneaky" in str(exc.value) and "register_tool" in str(exc.value)
    # Even if it were served, the call gate refuses it to an OAuth session.
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        at = tok["access_token"]
        sid = session(c, at)
        assert "sneaky" not in tool_names(c, at, sid)
        assert call(c, at, sid, "sneaky")["isError"]
        assert "should not be served" in json.dumps(call(c, STATIC, session(c, STATIC), "sneaky"))


def test_register_tool_is_the_way_in_for_other_modules(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch)

    def meaning_search(query: str) -> str:
        return f"read {query}"

    def note_writer(text: str) -> str:
        return "wrote"
    e.srv.register_tool(e.server, meaning_search, access="read")
    e.srv.register_tool(e.server, note_writer, access="write", name="add_note")
    e.srv.check_tools_classified(e.server)
    with pytest.raises(ValueError):
        e.srv.register_tool(e.server, note_writer, access="admin")
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        at = tok["access_token"]
        sid = session(c, at)
        assert "read x" in json.dumps(call(c, at, sid, "meaning_search", query="x"))
        assert call(c, at, sid, "add_note", text="t")["isError"]
        assert "wrote" in json.dumps(call(c, STATIC, session(c, STATIC), "add_note", text="t"))


def test_registration_is_closed_by_default(closed):
    """Finding 3b: no registration endpoint unless deliberately opened."""
    asm = closed.c.get("/.well-known/oauth-authorization-server").json()
    assert "registration_endpoint" not in asm
    assert asm["authorization_response_iss_parameter_supported"] is True
    assert register(closed.c).status_code == 404
    # A timed window from the CLI opens it, and it closes by itself.
    assert closed.mod.cli(["registration", "open", "5"], closed.cfg) == 0
    assert closed.c.get("/.well-known/oauth-authorization-server").json()[
        "registration_endpoint"] == PUBLIC + "/register"
    assert register(closed.c).status_code == 201
    closed.clock.t += 5 * 60 + 1
    assert register(closed.c).status_code == 404
    assert closed.mod.cli(["registration", "open", "999"], closed.cfg) == 0  # capped at 60 min
    closed.clock.t += 61 * 60
    assert register(closed.c).status_code == 404


def _static_client(e, method="post"):
    import contextlib
    import io
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert e.mod.cli(["client", "create", "--name", "Claude", "--auth", method], e.cfg) == 0
    text = out.getvalue()
    return (re.search(r"client id:\s+(\S+)", text).group(1),
            re.search(r"client secret:\s+(\S+)", text).group(1))


def _static_code(e, cid):
    state = to_github(e, cid)
    page = e.c.get("/oauth/github/callback?" + urlencode({"code": "c", "state": state}))
    assert page.status_code == 200
    assert "If you did not just press Connect in Claude yourself, press Deny." in page.text
    return parse_qs(urlparse(approve(e, page).headers["location"]).query)["code"][0]


def test_a_preregistered_client_needs_its_secret(closed):
    """Finding 3a/3c: the code of a confidential client is worthless without
    the secret, so a phished approval of someone else's link yields nothing."""
    cid, secret = _static_client(closed)
    code = _static_code(closed, cid)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": cid, "code_verifier": VERIFIER}
    assert closed.c.post("/token", data=form).status_code == 401
    assert closed.c.post("/token", data={**form, "client_secret": "wrong"}).status_code == 401
    r = closed.c.post("/token", data={**form, "client_secret": secret})
    assert r.status_code == 200 and r.json()["scope"] == "vault:read"
    # Refresh needs it too.
    rt = r.json()["refresh_token"]
    assert refresh(closed.c, cid, rt).status_code == 401
    assert closed.c.post("/token", data={"grant_type": "refresh_token", "refresh_token": rt,
                                         "client_id": cid, "client_secret": secret}
                         ).status_code == 200


def test_basic_auth_client_and_client_cli(closed, capsys):
    cid, secret = _static_client(closed, "basic")
    code = _static_code(closed, cid)
    r = closed.c.post("/token", auth=(cid, secret), data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
        "client_id": cid, "code_verifier": VERIFIER})
    assert r.status_code == 200
    capsys.readouterr()
    assert closed.mod.cli(["client", "list"], closed.cfg) == 0
    out = capsys.readouterr().out
    assert cid in out and "static" in out and secret not in out
    assert closed.mod.cli(["client", "revoke", cid], closed.cfg) == 0
    assert closed.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + r.json()["access_token"]}).status_code == 401


def test_grant_hook_gets_facts_never_secrets_and_never_blocks(tmp_path, monkeypatch):
    """Finding 3e."""
    out = tmp_path / "hook.json"
    script = ("import json,os,sys;json.dump({k:v for k,v in os.environ.items()},"
              "open(sys.argv[1],'w'))")
    e = make_env(tmp_path, monkeypatch, grant_hook=[sys.executable, "-c", script, str(out)])
    monkeypatch.setenv("SOME_SECRET_IN_THE_SERVER_ENV", "do-not-pass")
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        monkeypatch.chdir(tmp_path)
        cid, tok = grant(e, name="Claude $(touch pwned)\n`touch pwned2`")  # attacker text

    for _ in range(100):
        if out.exists() and out.stat().st_size:
            break
        time.sleep(0.05)
    env_seen = json.loads(out.read_text())
    assert env_seen["OZU_GRANT_EVENT"] == "new_grant" and env_seen["OZU_GRANT_CLIENT_ID"] == cid
    assert env_seen["OZU_GRANT_CLIENT_NAME"] == "Claude $(touch pwned) `touch pwned2`"
    assert "SOME_SECRET_IN_THE_SERVER_ENV" not in env_seen
    assert not any(tok["access_token"] in v or GH_SECRET in v or STATIC in v
                   for v in env_seen.values())
    assert not (tmp_path / "pwned").exists() and not (tmp_path / "pwned2").exists()


@pytest.mark.parametrize("hook", [["/nonexistent/hook"], [sys.executable, "-c", "import sys; sys.exit(3)"],
                                  [sys.executable, "-c", "import time; time.sleep(30)"]])
def test_a_failing_or_slow_hook_does_not_touch_the_grant(tmp_path, monkeypatch, hook):
    e = make_env(tmp_path, monkeypatch, grant_hook=hook)
    monkeypatch.setattr(e.mod, "HOOK_TIMEOUT", 1)
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        t0 = time.time()
        grant(e)
        assert time.time() - t0 < 5


def test_pending_limits_refuse_cleanly_and_spare_a_trusted_client(env, monkeypatch):
    """Finding 4 (the refusal was a FrozenInstanceError -> server_error) and 5."""
    cid = register(env.c).json()["client_id"]
    # Requests that reached the consent page are never evicted, so a client
    # holding its cap of those is refused, cleanly.
    for _ in range(env.mod.MAX_PENDING_PER_CLIENT):
        consent_page(env, cid)
    r = authorize(env.c, cid)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["temporarily_unavailable"] and q["state"] == ["client-st"]
    # Global cap: fresh clients are blocked, a client that already got a token is not.
    monkeypatch.setattr(env.mod, "MAX_PENDING", 4)
    env.clock.t += env.mod.PENDING_TTL + 1
    owner, _ = grant(env)
    flood = [register(env.c).json()["client_id"] for _ in range(2)]
    for f in flood:
        for _ in range(2):
            authorize(env.c, f)
    fresh = register(env.c).json()["client_id"]
    r = authorize(env.c, fresh)
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == ["temporarily_unavailable"]
    assert urlparse(authorize(env.c, owner).headers["location"]).path == "/oauth/start"


def test_anonymous_authorize_calls_cannot_lock_the_owner_out(env):
    """Second review, B: a client id is public, and three /authorize calls with
    no cookie and no sign-in filled the client's cap for ten minutes."""
    cid = register(env.c).json()["client_id"]
    first = urlparse(authorize(env.c, cid).headers["location"])
    for _ in range(env.mod.MAX_PENDING_PER_CLIENT - 1):
        authorize(env.c, cid)
    # The owner's Connect still goes through, all the way to a token ...
    state = to_github(env, cid)
    page = env.c.get("/oauth/github/callback?" + urlencode({"code": "c", "state": state}))
    assert page.status_code == 200 and "Approve" in page.text
    # ... the oldest unsigned request paid for it ...
    r = env.c.get(first.path + "?" + first.query, follow_redirects=False)
    assert r.status_code != 302
    # ... and a flood cannot evict a request the owner has signed in for.
    for _ in range(env.mod.MAX_PENDING_PER_CLIENT * 2):
        authorize(env.c, cid)
    assert approve(env, page).status_code in (302, 303)


def test_a_client_nobody_approved_dies_with_the_registration_window(closed):
    """Second review, C: the window bounded creating a client, not using it."""
    assert closed.mod.cli(["registration", "open", "5"], closed.cfg) == 0
    stranger = register(closed.c).json()["client_id"]
    mine = register(closed.c).json()["client_id"]
    assert urlparse(authorize(closed.c, stranger).headers["location"]).path == "/oauth/start"
    closed.c.cookies.clear()
    state = to_github(closed, mine)
    page = closed.c.get("/oauth/github/callback?" + urlencode({"code": "c", "state": state}))
    closed.clock.t += 5 * 60 - 10
    code = parse_qs(urlparse(approve(closed, page).headers["location"]).query)["code"][0]
    closed.clock.t += 20
    r = authorize(closed.c, stranger)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["unauthorized_client"]
    # A request approved inside the window still completes after it closes,
    # and the client that got a grant keeps working.
    assert token(closed.c, mine, code).status_code == 200
    assert urlparse(authorize(closed.c, mine).headers["location"]).path == "/oauth/start"


def test_a_tool_name_cannot_be_registered_twice(tmp_path, monkeypatch):
    """Second review, A: the SDK keeps the first tool under a name, so a second
    registration only changed the class and a write tool was served as read."""
    e = make_env(tmp_path, monkeypatch)

    def capture(text: str) -> str:
        return "read-only lookalike"
    with pytest.raises(ValueError):
        e.srv.register_tool(e.server, capture, access="read")
    with pytest.raises(ValueError):
        e.srv.register_tool(e.server, capture, access="write", name="capture")
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        at = tok["access_token"]
        assert call(c, at, session(c, at), "capture", text="x")["isError"]


def test_static_bearer_is_refused_through_the_proxy(env):
    """Finding 6, both directions."""
    xff = {"X-Forwarded-For": "203.0.113.9"}
    r = env.c.post("/mcp", json={}, headers={"Authorization": f"Bearer {STATIC}", **xff})
    assert r.status_code == 401 and "not accepted through the proxy" in r.text
    r = env.c.post("/mcp", json={}, headers={"Authorization": f"Bearer {STATIC}",
                                             "Forwarded": "for=203.0.113.9"})
    assert r.status_code == 401
    assert session(env.c, STATIC)                       # loopback, no forwarding header
    _, tok = grant(env)
    r = rpc(env.c, tok["access_token"], "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"}})
    assert r.status_code == 200                         # OAuth tokens are meant for the proxy


def test_static_bearer_through_the_proxy_can_be_allowed(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, static_remote=True)
    with TestClient(e.app, base_url=PUBLIC) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}}, headers={
            "Authorization": f"Bearer {STATIC}", "X-Forwarded-For": "203.0.113.9",
            "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200


def test_oauth_off_ignores_forwarding_headers(env):
    srv = env.srv
    app = srv.build_server(STATIC).streamable_http_app(
        transport_security=srv.transport_security())
    with TestClient(app, base_url=PUBLIC) as c:
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}}, headers={
            "Authorization": f"Bearer {STATIC}", "X-Forwarded-For": "203.0.113.9",
            "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200


def _parallel(app, requests):
    import asyncio

    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as c:
            return await asyncio.gather(*(c.post("/token", data=d) for d in requests))
    return asyncio.run(go())


def test_parallel_code_redemption_yields_one_grant(env):
    cid, code = get_code(env)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
            "client_id": cid, "code_verifier": VERIFIER}
    rs = _parallel(env.app, [form] * 16)
    assert sorted(r.status_code for r in rs).count(200) == 1


def test_parallel_refresh_keeps_the_grant(env):
    """Finding 7: sixteen refreshes in flight at once used to revoke the grant."""
    cid, tok = grant(env)
    rs = _parallel(env.app, [{"grant_type": "refresh_token", "client_id": cid,
                              "refresh_token": tok["refresh_token"]}] * 16)
    assert {r.status_code for r in rs} == {200}
    pairs = {(r.json()["access_token"], r.json()["refresh_token"]) for r in rs}
    assert len(pairs) == 1
    at = pairs.pop()[0]
    assert env.c.post("/mcp", json={}, headers={"Authorization": f"Bearer {at}"}
                      ).status_code != 401
    # Another client presenting it inside the window gets nothing.
    other = register(env.c).json()["client_id"]
    assert refresh(env.c, other, tok["refresh_token"]).status_code == 400


def test_loopback_redirects_are_opt_in(tmp_path, monkeypatch):
    e = make_env(tmp_path, monkeypatch, allow_loopback=True)
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        for uri in ("http://127.0.0.1:9999/cb", "http://localhost:3118/callback"):
            r = register(c, uris=[uri])
            assert r.status_code == 201, uri
        assert register(c, uris=["http://192.168.1.5:9999/cb"]).status_code == 400
        cid = register(c, uris=["http://127.0.0.1:9999/cb"]).json()["client_id"]
        r = authorize(c, cid, redirect="http://127.0.0.1:9999/cb")
        start = urlparse(r.headers["location"])
        r = c.get(start.path + "?" + start.query, follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query, keep_blank_values=True)
        e.gh.challenge = q["code_challenge"][0]
        page = c.get("/oauth/github/callback?" + urlencode({"code": "x", "state": q["state"][0]}))
        assert "a program on your own computer" in page.text


def test_prune_forgets_dead_rows_and_unapproved_clients(env):
    cid, tok = grant(env)
    nobody = register(env.c).json()["client_id"]
    env.c.post("/revoke", data={"token": tok["access_token"], "client_id": cid,
                                "client_secret": ""})
    env.clock.t += 31 * 86400
    register(env.c)     # any write prunes
    from ozu.mcp_oauth import connect
    con = connect(env.provider.s.db_path)
    assert con.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM families").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM codes").fetchone()[0] == 0
    clients = {r[0] for r in con.execute("SELECT client_id FROM clients")}
    # The approved client stays (it reconnects with its cached id); the one
    # nobody approved is gone.
    assert cid in clients and nobody not in clients


@pytest.mark.parametrize("ended", ["expired", "revoked_one", "revoked_by_client"])
def test_an_approved_client_reconnects_after_its_grant_ends(closed, capsys, ended):
    """Ultrareview 1: the prune deleted a used client once its grant had
    ended, inside the very /authorize it was reconnecting with, and the owner
    got "Application unknown" after the GitHub sign-in."""
    assert closed.mod.cli(["registration", "open", "5"], closed.cfg) == 0
    cid = register(closed.c).json()["client_id"]
    _, page = consent_page(closed, cid)
    code = parse_qs(urlparse(approve(closed, page).headers["location"]).query)["code"][0]
    tok = token(closed.c, cid, code).json()
    assert closed.mod.cli(["registration", "close"], closed.cfg) == 0
    if ended == "expired":
        closed.clock.t += closed.mod.FAMILY_TTL + 1
    elif ended == "revoked_one":
        capsys.readouterr()
        closed.mod.cli(["tokens", "list"], closed.cfg)
        family = capsys.readouterr().out.split()[0]
        assert closed.mod.cli(["tokens", "revoke", family], closed.cfg) == 0
    else:
        closed.c.post("/revoke", data={"token": tok["access_token"], "client_id": cid,
                                       "client_secret": ""})
    assert closed.c.post("/mcp", json={}, headers={
        "Authorization": "Bearer " + tok["access_token"]}).status_code == 401
    closed.c.cookies.clear()
    _, page = consent_page(closed, cid)
    assert page.status_code == 200 and "Approve" in page.text, page.text
    code = parse_qs(urlparse(approve(closed, page).headers["location"]).query)["code"][0]
    assert token(closed.c, cid, code).status_code == 200


def test_a_client_forgotten_mid_authorize_is_refused_before_github(env):
    """The SDK loads the client, then authorize() prunes: a client that
    crosses CLIENT_UNUSED_TTL in between is refused at once, not after a
    GitHub sign-in."""
    import asyncio

    from mcp.server.auth.provider import AuthorizationParams, AuthorizeError
    cid = register(env.c).json()["client_id"]
    client = asyncio.run(env.provider.get_client(cid))
    env.clock.t += env.mod.CLIENT_UNUSED_TTL + 1
    params = AuthorizationParams(state="s", scopes=["vault:read"], code_challenge=CHALLENGE,
                                 redirect_uri=REDIRECT, redirect_uri_provided_explicitly=True,
                                 resource=RESOURCE)
    with pytest.raises(AuthorizeError) as exc:
        asyncio.run(env.provider.authorize(client, params))
    assert exc.value.error == "unauthorized_client"


def test_configured_urls_match_in_their_canonical_form(tmp_path, monkeypatch):
    """Ultrareview 2: an allowlisted redirect or public URL spelled with an
    uppercase host, a default port or no trailing slash passed the start-up
    check and then never matched what the SDK hands back."""
    e = make_env(tmp_path, monkeypatch, public_url="http://127.0.0.1:8399/",
                 redirect_uris=["https://Claude.AI:443/api/mcp/auth_callback"])
    assert e.provider.issuer == PUBLIC
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        assert tok["scope"] == "vault:read"
        assert e.provider._is_our_resource("HTTP://127.0.0.1:8399/mcp/")
        assert not e.provider._is_our_resource(RESOURCE + "?x=1")
    # The config path: the same spellings start, and a URL pydantic cannot
    # parse is refused at start-up rather than at sign-in.
    from types import SimpleNamespace
    cfg = SimpleNamespace(mcp_github_client_id="fake-gh-client", mcp_github_client_secret=GH_SECRET, mcp_github_login=PINNED_LOGIN, mcp_github_user_id=str(PINNED_ID), mcp_public_url_set=True, mcp_public_url="https://OZU.example.com:443", mcp_github_url="https://github.com", mcp_github_api_url="https://api.github.com", mcp_oauth_redirect_uris=["https://claude.ai:443/api/mcp/auth_callback"], mcp_oauth_allow_loopback=False, mcp_oauth_registration="0", mcp_static_remote="0", mcp_grant_hook="", mcp_oauth_db=tmp_path / "x.db", home=tmp_path)
    s = e.mod.settings_from_config(cfg)
    assert s.public_url == "https://ozu.example.com"
    assert s.redirect_uris == [REDIRECT]
    cfg.mcp_oauth_redirect_uris = ["https://claude.ai:99999/cb"]
    with pytest.raises(SystemExit) as exc:
        e.mod.settings_from_config(cfg)
    assert "OZU_MCP_OAUTH_REDIRECT_URIS" in str(exc.value)


def test_a_flood_cannot_cancel_a_sign_in_on_github(env):
    """Ultrareview 3: anonymous /authorize calls evicted the owner's request
    while the owner was on GitHub's page."""
    cid = register(env.c).json()["client_id"]
    state = to_github(env, cid)
    for _ in range(env.mod.MAX_PENDING_PER_CLIENT * 3):
        authorize(env.c, cid)          # a steady flood, none of it opened in a browser
    page = env.c.get("/oauth/github/callback?" + urlencode({"code": "c", "state": state}))
    assert page.status_code == 200 and "Approve" in page.text
    # Requests abandoned on GitHub's page are evicted once the grace is
    # over, so the cap still frees up; inside it, a new one waits.
    other = register(env.c).json()["client_id"]
    for _ in range(env.mod.MAX_PENDING_PER_CLIENT):
        to_github(env, other)
    r = authorize(env.c, other)
    assert parse_qs(urlparse(r.headers["location"]).query)["error"] == [
        "temporarily_unavailable"]
    env.clock.t += env.mod.GITHUB_SIGNIN_GRACE + 1
    assert urlparse(authorize(env.c, other).headers["location"]).path == "/oauth/start"


def test_the_refresh_grace_cache_holds_seconds_not_months(env):
    """Ultrareview 4: only the failure path pruned it, so a healthy client
    left every plaintext pair it was ever issued in memory."""
    cid, tok = grant(env)
    rt = tok["refresh_token"]
    for _ in range(5):
        r = refresh(env.c, cid, rt)
        assert r.status_code == 200
        rt = r.json()["refresh_token"]
        env.clock.t += env.mod.REFRESH_GRACE + 1
    assert len(env.provider._grace) == 1


def test_registration_open_works_before_the_server_ever_ran(tmp_path, monkeypatch):
    """Ultrareview 5: with no state file yet, `registration open` printed
    "no OAuth state yet", exited 0 and opened nothing."""
    from types import SimpleNamespace

    from ozu import mcp_oauth
    cfg = SimpleNamespace(mcp_oauth_db=tmp_path / "fresh" / "mcp-oauth.db",
                          mcp_oauth_registration="0")
    assert mcp_oauth.cli(["tokens", "list"], cfg) == 0 and not cfg.mcp_oauth_db.exists()
    assert mcp_oauth.cli(["registration", "open", "5"], cfg) == 0
    e = make_env(tmp_path, monkeypatch, registration_open=False, db_path=cfg.mcp_oauth_db)
    assert e.provider.registration_is_open()


def test_a_hook_that_cannot_start_does_not_fail_the_grant(tmp_path, monkeypatch):
    """Ultrareview 6: Thread.start() raising after the grant committed gave
    the client a 500, and its retry revoked the grant as a code replay."""
    import threading
    e = make_env(tmp_path, monkeypatch, grant_hook=[sys.executable, "-c", "pass"])

    real_start = threading.Thread.start

    def no_hook_thread(self):
        if self.name == "ozu-grant-hook":
            raise RuntimeError("can't start new thread")
        return real_start(self)
    monkeypatch.setattr(threading.Thread, "start", no_hook_thread)
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        sid = session(c, tok["access_token"])
        assert not call(c, tok["access_token"], sid, "open_actions")["isError"]


def test_an_async_tool_runs_through_register_tool(tmp_path, monkeypatch):
    """Ultrareview 7: the sync wrapper returned an async tool's coroutine
    unawaited."""
    e = make_env(tmp_path, monkeypatch)

    async def slow_search(query: str) -> str:
        return f"awaited {query}"
    e.srv.register_tool(e.server, slow_search, access="read")
    with TestClient(e.app, base_url=PUBLIC) as c:
        e.c = c
        _, tok = grant(e)
        at = tok["access_token"]
        assert "awaited x" in json.dumps(call(c, at, session(c, at), "slow_search", query="x"))


def test_requests_do_not_rerun_the_schema_setup(env, monkeypatch):
    """Ultrareview 8: every transaction re-ran mkdir, chmod and the schema."""
    calls = []
    real = env.mod.connect
    monkeypatch.setattr(env.mod, "connect", lambda p: calls.append(p) or real(p))
    _, tok = grant(env)
    at = tok["access_token"]
    call(env.c, at, session(env.c, at), "open_actions")
    assert calls == []


def test_the_server_and_the_issuer_share_one_set_of_scopes(env):
    """Ultrareview 9."""
    assert env.srv.READ_SCOPE is env.mod.READ_SCOPE
    assert env.srv.FULL_SCOPE is env.mod.FULL_SCOPE == env.srv.SCOPE


def test_both_revoke_commands_revoke_and_forget_the_client(env, capsys):
    """Ultrareview 10: one helper behind `client revoke` and `tokens revoke
    --client`."""
    for argv in (["client", "revoke"], ["tokens", "revoke", "--client"]):
        cid, tok = grant(env)
        capsys.readouterr()
        assert env.mod.cli(argv + [cid], env.cfg) == 0
        assert "revoked 1 grant(s)" in capsys.readouterr().out
        assert env.c.post("/mcp", json={}, headers={
            "Authorization": "Bearer " + tok["access_token"]}).status_code == 401
        assert authorize(env.c, cid).status_code == 400   # forgotten


def test_main_starts_the_edge_with_the_log_filter(monkeypatch, tmp_path):
    """The real start-up path, short of binding a port."""
    import uvicorn

    from ozu import mcp_oauth, mcp_server
    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    logging.getLogger("uvicorn.access").filters[:] = []
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "home"))
    for k, v in {**OAUTH_ENV, "OZU_MCP_PUBLIC_URL": "http://127.0.0.1:8399"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["ozu", "--http"])
    import ozu.config as config
    config._config = None
    try:
        mcp_server.main()
    finally:
        config._config = None
    assert isinstance(seen["app"], mcp_oauth.OAuthEdge)
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8321
    assert any(isinstance(f, mcp_oauth.RedactQuery)
               for f in logging.getLogger("uvicorn.access").filters)
    assert (tmp_path / "home" / "mcp-oauth.db").exists()
    # A tool registered past register_tool stops the start.
    real = mcp_server.build_server

    def with_a_loose_tool(*a, **k):
        server = real(*a, **k)
        server.tool(name="loose")(lambda: "x")
        return server
    monkeypatch.setattr(mcp_server, "build_server", with_a_loose_tool)
    msg = _main_with(monkeypatch, tmp_path, **{**OAUTH_ENV,
                                               "OZU_MCP_PUBLIC_URL": "http://127.0.0.1:8399"})
    assert "loose" in msg
