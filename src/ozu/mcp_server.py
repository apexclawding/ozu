"""MCP server: the graph as tools for Claude Code (stdio) and Claude.ai (http).

Governance default: every tool is scoped to the personal vault. The work vault
is reachable only via explicit vault="work" AND OZU_ALLOW_WORK_MCP=1 in the env,
and only from get_note and search_notes. Every filter is an allow-list: a note
whose label is not exactly `personal` (or `work`, opted in) is never served,
so a label nobody migrated fails closed.

Run:  ozu mcp            # stdio, for Claude Code (.mcp.json)
      ozu mcp --http     # streamable-http, bearer token required

Over HTTP every request must carry `Authorization: Bearer $OZU_MCP_TOKEN`.
Without OZU_MCP_TOKEN set, `--http` refuses to start: an unauthenticated
endpoint is how the whole vault leaks, and defaulting to open is worse than
not running. stdio needs no token — it is a pipe to a local process.

With OZU_MCP_OAUTH=1 the HTTP server is also its own OAuth authorization
server, so claude.ai connectors can sign in (mcp_oauth.py, docs/mcp-oauth.md).
OAuth tokens get `vault:read` only; the static bearer keeps every tool.
"""
from __future__ import annotations

import functools
import os
import secrets
import sys
from pathlib import Path
from urllib.parse import urlparse

try:  # mcp SDK 1.x
    from mcp.server.fastmcp import FastMCP as _Server
except ImportError:  # mcp SDK 2.x renamed the high-level server
    from mcp.server.mcpserver import MCPServer as _Server

from .config import canonical_topic, get_config
from . import db, vault as vault_mod

SCOPE = "ozu"

# Tools are collected here and registered onto a server built in main(),
# because auth is a constructor argument: the stdio server must not demand a
# token and the HTTP one must, and that is decided after import.
_TOOLS: list = []


def _tool(fn):
    _TOOLS.append(fn)
    return fn


# Who may call what. Deny by default: an OAuth token (scope vault:read) may
# call only a tool registered with access "read"; anything else, `capture`
# included, needs the static bearer's scope, which OAuth never issues. The
# class is recorded per *registered* name, by register_tool, and in OAuth
# mode the server refuses to start if any registered tool has no class.
FULL_SCOPE = SCOPE            # == mcp_oauth.FULL_SCOPE
READ_SCOPE = "vault:read"     # == mcp_oauth.READ_SCOPE
READ_TOOLS = frozenset({"search_notes", "get_note", "graph_neighbors", "timeline",
                        "open_actions"})
ACCESS = ("read", "write")


def _token_scopes():
    """The scopes of the current request's access token, or None when the
    request carries none (stdio, or a direct call)."""
    try:
        from mcp.server.auth.middleware.auth_context import get_access_token
    except ImportError:  # an SDK without auth support cannot have authenticated anything
        return None
    tok = get_access_token()
    return None if tok is None else set(tok.scopes)


try:  # a refusal the SDK reports as a tool error, not as a crash
    from mcp.server.mcpserver.exceptions import ToolError as _Refused
except ImportError:  # mcp SDK 1.x
    try:
        from mcp.server.fastmcp.exceptions import ToolError as _Refused
    except ImportError:
        _Refused = PermissionError


def _access(server) -> dict:
    return server.__dict__.setdefault("_ozu_access", {})


def _allowed(server, name: str, scopes) -> bool:
    if FULL_SCOPE in scopes:
        return True
    return READ_SCOPE in scopes and _access(server).get(name) == "read"


def register_tool(server, fn, *, access: str, name: str | None = None):
    """Register `fn` as a tool together with its access class: "read" (an
    OAuth `vault:read` session may call it) or "write" (only the static
    bearer may). This is the one public way for another module to add a tool
    (e.g. from a `register(server, ...)` hook): a tool added with a bare
    `server.tool()` has no class, which OAuth mode refuses to start with,
    rather than serving it open. The check runs at call time on the name the
    tool is registered under."""
    if access not in ACCESS:
        raise ValueError(f"access must be one of {ACCESS}")
    name = name or fn.__name__
    # The SDK keeps the first tool registered under a name and drops the
    # second; a second class for that name would then re-class the first
    # tool (a write tool served as "read"). One name, one class, ever.
    if name in _access(server) or name in registered_tool_names(server):
        raise ValueError(f"a tool named {name!r} is already registered")
    _access(server)[name] = access
    auth_on = server.__dict__.get("_ozu_auth_on", False)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        scopes = _token_scopes()
        if scopes is None and auth_on:
            raise _Refused("no access token on this request")
        if scopes is not None and not _allowed(server, name, scopes):
            raise _Refused(f"{name} requires scope "
                           f"{READ_SCOPE if access == 'read' else FULL_SCOPE}")
        return fn(*args, **kwargs)
    server.tool(name=name)(wrapper)
    return wrapper


def registered_tool_names(server) -> set[str]:
    return {t.name for t in server._tool_manager.list_tools()}


def check_tools_classified(server) -> None:
    """OAuth mode: every registered tool must have come through
    register_tool, or the server does not start."""
    loose = sorted(registered_tool_names(server) - set(_access(server)))
    if loose:
        raise SystemExit(
            "OAuth is on, but these tools were registered without an access class: "
            + ", ".join(loose) + ".\nRegister them with "
            "mcp_server.register_tool(server, fn, access='read'|'write') so an OAuth "
            "session can be refused them; `ozu mcp --http` will not start otherwise.")


def _con():
    return db.connect(get_config().db_path)


def _work_allowed() -> bool:
    return os.environ.get("OZU_ALLOW_WORK_MCP") == "1"


def _vault_ok(vault: str) -> str:
    """Check the vault a *caller* asked for. Allow-list shaped: `personal`
    passes, `work` passes only with the opt-in, and anything else is refused
    rather than quietly read as `personal` — an unknown name is a caller
    mistake, and answering it would hide that."""
    if vault == "personal":
        return vault
    if vault == "work":
        if not _work_allowed():
            raise ValueError("work vault is not exposed over MCP (set OZU_ALLOW_WORK_MCP=1 to override)")
        return vault
    raise ValueError(f"unknown vault {vault!r}: only 'personal' (or 'work' with the opt-in) is served")


def _label_served(label: str) -> bool:
    """Whether a note *stored* with this label may leave over MCP. Only an
    exact `personal`, or an exact `work` with the opt-in; any other label (a
    legacy name nobody migrated, a typo, an empty string) fails closed."""
    return label == "personal" or (label == "work" and _work_allowed())


@_tool
def search_notes(query: str, vault: str = "personal", limit: int = 8) -> list[dict]:
    """Full-text search over the knowledge vault. Returns id, title, topic, summary, snippet."""
    rows = db.search_notes(_con(), query, _vault_ok(vault), limit)
    return [dict(r) for r in rows]


@_tool
def get_note(note_id: str) -> str:
    """Return the full Markdown of a note by id."""
    row = _con().execute("SELECT path, vault FROM notes WHERE id=?", (note_id,)).fetchone()
    # A note that may not be served is answered exactly like a missing one,
    # so the reply is no oracle for which ids exist behind the wall.
    if not row or not _label_served(row["vault"]):
        return f"Note {note_id} not found."
    return Path(row["path"]).read_text()


@_tool
def graph_neighbors(note_id: str, depth: int = 1, edge_type: str = "") -> list[dict]:
    """Walk the knowledge graph around a note. edge_type filters (related|supports|contradicts|prerequisite|evolved-into)."""
    return db.neighbors(_con(), note_id, depth, edge_type or None, vault="personal")


@_tool
def timeline(topic: str, since: str = "") -> list[dict]:
    """Chronological notes for a topic/project (since = YYYY-MM-DD, optional)."""
    con = _con()
    q = "SELECT id, created, title, summary FROM notes WHERE vault='personal' AND topic=?"
    params: list = [canonical_topic(topic)]
    if since:
        q += " AND created >= ?"
        params.append(since)
    return [dict(r) for r in con.execute(q + " ORDER BY created", params).fetchall()]


@_tool
def open_actions() -> list[dict]:
    """All open action items extracted from red ink."""
    rows = _con().execute(
        "SELECT a.id, a.text, n.title AS note FROM actions a"
        " JOIN notes n ON n.id=a.note_id"
        " WHERE a.done=0 AND n.vault='personal' ORDER BY a.created DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@_tool
def capture(text: str, title: str = "", topic: str = "ideas") -> str:
    """Write a thought INTO the graph from a Claude conversation. Creates an atomic note."""
    cfg = get_config()
    topic = canonical_topic(topic)
    vault_mod.scaffold(cfg.vault_dir)
    result = {
        "notes": [{
            "title": title or text[:50],
            "slug": (title or text[:50]),
            "topic": topic,
            "tags": [topic],
            "summary": text[:140],
            "body": text,
            "related": [],
            "actions": [],
        }],
        "page_no": 0,
    }
    ids = vault_mod.write_notes(result, notebook="claude-capture", page_row_id=None,
                                image_path=_placeholder_image(), needs_review=False)
    return f"Captured as {ids[0]}"


def _placeholder_image() -> Path:
    cfg = get_config()
    p = cfg.pages_dir / "claude-capture.png"
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        # 1x1 transparent PNG
        p.write_bytes(bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"))
    return p


class StaticToken:
    """One shared secret. Not OAuth: there is one holder of this graph and one
    token, sent as a bearer header. The SDK does the rest — reading the header,
    answering 401, and advertising WWW-Authenticate. compare_digest because a
    token check that returns early leaks the token one byte at a time."""

    def __init__(self, token: str):
        self._token = token

    async def verify_token(self, token: str):
        from mcp.server.auth.provider import AccessToken
        if secrets.compare_digest(token, self._token):
            return AccessToken(token=token, client_id="ozu", scopes=[SCOPE])
        return None


def build_server(token: str = "", oauth=None) -> "_Server":
    """The server with its tools attached. A token turns on bearer auth; an
    `oauth` provider (mcp_oauth.GitHubOAuthProvider, which also answers for
    the token) turns on the OAuth endpoints as well."""
    kwargs = {}
    if oauth is not None:
        from mcp.server.auth.settings import (AuthSettings, ClientRegistrationOptions,
                                              RevocationOptions)
        kwargs = dict(
            auth_server_provider=oauth,
            auth=AuthSettings(
                issuer_url=oauth.issuer, resource_server_url=oauth.resource,
                # Every token, the static bearer included, carries vault:read;
                # what more a tool needs is checked per call (register_tool, _gate_tools).
                required_scopes=[READ_SCOPE],
                client_registration_options=ClientRegistrationOptions(
                    enabled=True, valid_scopes=[READ_SCOPE], default_scopes=[READ_SCOPE]),
                revocation_options=RevocationOptions(enabled=True),
                validate_token_resource=True),
        )
    elif token:
        from mcp.server.auth.settings import AuthSettings
        cfg = get_config()
        # A static bearer token is a shared secret, not a token issued for an
        # audience: StaticToken returns no `resource`, and comparing the whole
        # secret is the entire check. Newer SDKs ask us to say so, and from
        # 3.0 default to refusing any token whose resource is not
        # `resource_server_url` — which would 401 every request. Older SDKs
        # (the floor is mcp 1.2) have no such field, so it is only passed
        # where it exists.
        extra = ({"validate_token_resource": False}
                 if "validate_token_resource" in AuthSettings.model_fields else {})
        kwargs = dict(
            token_verifier=StaticToken(token),
            # The server has to call itself by the URL the client dialled, or
            # the metadata it advertises on 401 points somewhere unreachable.
            auth=AuthSettings(issuer_url=cfg.mcp_public_url,
                              resource_server_url=cfg.mcp_public_url,
                              required_scopes=[SCOPE], **extra),
        )
    server = _Server("ozu-graph", **kwargs)
    server._ozu_auth_on = bool(kwargs)
    for fn in _TOOLS:
        register_tool(server, fn, access="read" if fn.__name__ in READ_TOOLS else "write")
    if oauth is not None:
        oauth.install(server)
        _gate_tools(server)
    return server


def _gate_tools(server) -> None:
    """Two more layers in OAuth mode, both keyed on the called name and
    denying by default: tools/list shows a session only what it may call,
    and tools/call refuses before the tool is even looked up, so a tool
    that skipped register_tool is not reachable over OAuth either."""
    listed, called = server.list_tools, server.call_tool

    async def list_tools():
        scopes = _token_scopes()
        tools = await listed()
        return tools if scopes is None else [t for t in tools
                                             if _allowed(server, t.name, scopes)]

    async def call_tool(name, arguments, context=None):
        scopes = _token_scopes()
        if scopes is None or not _allowed(server, name, scopes):
            raise _Refused(f"{name} is not available to this token")
        return await called(name, arguments, context)
    server.list_tools, server.call_tool = list_tools, call_tool


def oauth_app(server, oauth):
    """The ASGI app OAuth mode serves: the SDK's streamable-http app (with
    DNS-rebinding protection) behind mcp_oauth.OAuthEdge."""
    from . import mcp_oauth
    cfg = get_config()
    app = server.streamable_http_app(transport_security=transport_security(),
                                     host=cfg.mcp_host)
    return mcp_oauth.OAuthEdge(app, oauth, server.settings.auth)


def transport_security():
    """DNS-rebinding protection, kept on and told which names we answer to.

    Behind Caddy the Host header is the public name, not the loopback bind the
    server listens on, and the SDK rejects the mismatch with 421 — a failure
    that cannot appear until the endpoint is actually fronted, which is why it
    surfaced only after the DNS record existed. Naming the hosts is the fix;
    turning the protection off is not.
    """
    from mcp.server.transport_security import TransportSecuritySettings
    cfg = get_config()
    hosts = {f"{cfg.mcp_host}:{cfg.mcp_port}", "127.0.0.1:*", "localhost:*"}
    origins = set()
    public = urlparse(cfg.mcp_public_url)
    if public.hostname:
        hosts.add(public.netloc)           # exactly as a client sends it
        hosts.add(f"{public.hostname}:*")  # ...and with any explicit port
        origins.add(f"{public.scheme}://{public.netloc}")
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=sorted(hosts),
                                     allowed_origins=sorted(origins))


def main() -> None:
    cfg = get_config()
    if "--http" not in sys.argv:
        build_server().run()  # stdio: a pipe to a local process, no token
        return
    if not cfg.mcp_token:
        raise SystemExit(
            "OZU_MCP_TOKEN is unset, so `ozu mcp --http` will not start.\n"
            "Every tool here reads the vault, and an open port is the whole\n"
            "vault. Generate one with `python3 -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\"`\n"
            f"and add it to {cfg.home / '.env'} as "
            "OZU_MCP_TOKEN=... (no inline comment — the value would keep it).")
    from . import mcp_oauth
    if mcp_oauth.enabled(cfg):  # refuses to start half-configured
        oauth = mcp_oauth.GitHubOAuthProvider(mcp_oauth.settings_from_config(cfg),
                                              static_token=cfg.mcp_token)
        server = build_server(cfg.mcp_token, oauth=oauth)
        check_tools_classified(server)
        import uvicorn
        app = oauth_app(server, oauth)
        mcp_oauth.quiet_access_log()
        uvicorn.run(app, host=cfg.mcp_host, port=cfg.mcp_port, log_level="info")
        return
    server = build_server(cfg.mcp_token)
    kwargs = {"transport": "streamable-http",
              "transport_security": transport_security()}
    # Detect by capability, not by version: `settings` exists in both SDK
    # majors, but only 1.x carries host/port on it. 2.x dropped those fields
    # and takes them as run() kwargs instead. If an SDK ever refuses
    # transport_security here, let it fail loudly at startup — serving without
    # rebinding protection is not the quieter option, it is the worse one.
    if "host" in getattr(type(server.settings), "model_fields", {}):  # mcp 1.x
        server.settings.host, server.settings.port = cfg.mcp_host, cfg.mcp_port
    else:  # mcp 2.x
        kwargs |= {"host": cfg.mcp_host, "port": cfg.mcp_port}
    server.run(**kwargs)


if __name__ == "__main__":
    main()
