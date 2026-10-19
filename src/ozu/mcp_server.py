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
"""
from __future__ import annotations

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


def build_server(token: str = "") -> "_Server":
    """The server with its tools attached. A token turns on bearer auth."""
    kwargs = {}
    if token:
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
    for fn in _TOOLS:
        server.tool()(fn)
    return server


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
