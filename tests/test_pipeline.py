"""End-to-end pipeline tests with the Claude vision call mocked.

Covers: page dedup on re-export, atomic note writing + registry, governance
routing to the work vault, edge creation, index rebuild from files, digest
building, and MCP tool functions.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OZU_HOME", str(tmp_path / "ozu"))
    monkeypatch.setenv("OZU_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OZU_WORK_VAULT_DIR", str(tmp_path / "work-vault"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    import ozu.config as config
    config._config = None  # reset singleton
    for m in ("ozu.db", "ozu.ingest", "ozu.vault", "ozu.understand",
              "ozu.indexer", "ozu.digest"):
        if m in sys.modules:
            importlib.reload(sys.modules[m])
    return config.get_config()


def make_pdf(path: Path, pages: list[str]):
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=14)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


MOCK_RESULT = {
    "transcription": "Kettle pricing idea -> freemium with pantry scan paywall",
    "drawing_description": "",
    "mermaid": "",
    "confidence": 0.95,
    "work_flag": False,
    "build_directive": {"kind": "none", "model": None},
    "build_ref": None,
    "notes": [{
        "title": "Kettle freemium pricing",
        "slug": "kettle-freemium-pricing",
        "topic": "kettle",
        "tags": ["kettle", "pricing"],
        "summary": "Freemium model gated on pantry scanning.",
        "body": "Freemium with pantry scan behind paywall. [[20250101-old-note]]",
        "related": [],
        "related_reason": "",
        "actions": ["Check competitor pricing"],
        "claude_request": False,
        "research_request": False,
    }],
    "completions": [],
}


def prompt_json_block(path: Path) -> dict:
    """The JSON block a prompt asks for, as data. The prompts double their
    braces for str.format, so undo that before reading it."""
    text = path.read_text()
    start = text.index("Respond with ONLY a JSON object")
    block = text[text.index("{{", start):text.index("\nRules:", start)]
    return json.loads(block.replace("{{", "{").replace("}}", "}"))


def test_schema_matches_the_prompts(env):
    """The prompts say what the fields mean; PAGE_SCHEMA makes the API enforce
    them. Nothing keeps the two honest but this — CONVENTIONS.md, both prompts
    and the docs are supposed to move together, and a schema is easy to forget."""
    from ozu import understand
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    for name in ("vision_prompt.md", "annotation_prompt.md"):
        asked = prompt_json_block(prompts / name)
        assert set(asked) == set(understand.PAGE_SCHEMA["properties"]), name
        assert set(understand.PAGE_SCHEMA["required"]) == set(asked), name
        assert set(asked["notes"][0]) == set(
            understand.NOTE_SCHEMA["properties"]), name
        assert set(understand.NOTE_SCHEMA["required"]) == set(asked["notes"][0]), name


def test_mock_result_has_the_shape_the_api_now_guarantees(env):
    """Every test in the suite runs on MOCK_RESULT. If it drifts from the
    schema the suite passes on a page shape the API can no longer return."""
    from ozu import understand
    assert set(MOCK_RESULT) == set(understand.PAGE_SCHEMA["properties"])
    assert set(MOCK_RESULT["notes"][0]) == set(understand.NOTE_SCHEMA["properties"])


class FakeResp:
    def __init__(self, text="", stop_reason="end_turn", category=None):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.stop_details = type("D", (), {"category": category})()
        self.usage = type("U", (), {"input_tokens": 1200, "output_tokens": 340})()


def stub_client(monkeypatch, resp, seen=None):
    from ozu import understand

    def create(**kw):
        if seen is not None:
            seen.update(kw)
        return resp

    monkeypatch.setattr(understand, "_client",
                        lambda: type("C", (), {"messages": type("M", (), {
                            "create": staticmethod(create)})()})())


def test_analyze_page_asks_the_api_to_enforce_the_schema(env, monkeypatch, tmp_path):
    """No fences to strip and no brace-hunting: the response is JSON or the
    request failed."""
    from ozu import understand
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    seen = {}
    stub_client(monkeypatch, FakeResp(json.dumps(MOCK_RESULT)), seen)
    assert understand.analyze_page(img, "Inbox", 1)["confidence"] == 0.95
    fmt = seen["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is understand.PAGE_SCHEMA
    assert not hasattr(understand, "_parse_json")


def test_a_declined_page_says_so_and_stays_queued(env, monkeypatch, tmp_path):
    from ozu import understand
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    stub_client(monkeypatch, FakeResp("", stop_reason="refusal", category="cyber"))
    with pytest.raises(RuntimeError, match="declined this page .cyber."):
        understand.analyze_page(img, "Inbox", 1)


def test_a_truncated_page_names_the_ceiling_it_hit(env, monkeypatch, tmp_path):
    from ozu import understand
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    stub_client(monkeypatch, FakeResp('{"transcription":', stop_reason="max_tokens"))
    with pytest.raises(RuntimeError, match="max_tokens"):
        understand.analyze_page(img, "Inbox", 1)


def test_ingest_dedup(env):
    from ozu import ingest, db
    make_pdf(env.raw_dir / "Inbox" / "nb.pdf", ["page one", "page two"])
    assert ingest.ingest() == 2
    # cumulative re-export: same two pages + one new
    make_pdf(env.raw_dir / "Inbox" / "nb.pdf", ["page one", "page two", "page three"])
    assert ingest.ingest() == 1
    con = db.connect(env.db_path)
    assert con.execute("SELECT COUNT(*) c FROM pages").fetchone()["c"] == 3


def test_process_and_vault(env, monkeypatch):
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "ideas.pdf", ["an idea"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    assert understand.process_pending() == 1

    note = next((env.vault_dir / "topics" / "kettle").glob("*kettle-freemium-pricing*.md"))
    text = note.read_text()
    assert "needs_review: false" in text and "Freemium" in text
    # the derived views: a folder index in OKF's list form, the root pointing at it
    index = (env.vault_dir / "topics" / "kettle" / "index.md").read_text()
    assert "# Note" in index and "* [Kettle freemium pricing](" in index
    assert "(topics/kettle/index.md)" in (env.vault_dir / "index.md").read_text()
    assert "**Creation**: [Kettle freemium pricing]" in (env.vault_dir / "log.md").read_text()
    assert not (env.vault_dir / "INDEX.md").exists()

    con = db.connect(env.db_path)
    assert con.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"] == 1
    assert db.search_notes(con, "pantry")[0]["title"] == "Kettle freemium pricing"


def test_governance_work_routing(env, monkeypatch):
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Work" / "work.pdf", ["confidential"])
    ingest.ingest()
    work_result = dict(MOCK_RESULT)
    work_result["notes"] = [dict(MOCK_RESULT["notes"][0], topic="career",
                               related=["20250101-x"], actions=["leak?"])]
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: work_result)
    understand.process_pending()
    # note landed in work vault, NOT personal
    assert list((env.work_vault_dir / "topics" / "career").glob("*.md"))
    assert not (env.vault_dir / "topics" / "career").exists()
    con = db.connect(env.db_path)
    row = con.execute("SELECT vault FROM notes").fetchone()
    assert row["vault"] == "work"
    # governance: no edges, no actions from work content
    assert con.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"] == 0
    assert con.execute("SELECT COUNT(*) c FROM actions").fetchone()["c"] == 0


def test_indexer_rebuild_and_wikilinks(env, monkeypatch):
    from ozu import ingest, understand, indexer, db
    make_pdf(env.raw_dir / "Inbox" / "a.pdf", ["x"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    env.db_path.unlink()  # simulate losing the derived db
    assert indexer.rebuild() == 1
    con = db.connect(env.db_path)
    edges = con.execute("SELECT * FROM edges").fetchall()
    assert any(e["dst"] == "20250101-old-note" for e in edges)  # wikilink → edge


def test_digest_builds(env, monkeypatch):
    from ozu import ingest, understand, digest
    make_pdf(env.raw_dir / "Inbox" / "d.pdf", ["y"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    text = digest.build_digest()
    assert "Ozu weekly digest" in text
    assert "Kettle freemium pricing" in text
    assert "Check competitor pricing" in text


def test_mcp_tools(env, monkeypatch):
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "m.pdf", ["z"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    import ozu.mcp_server as srv
    importlib.reload(srv)
    hits = srv.search_notes("freemium")
    assert hits and hits[0]["title"] == "Kettle freemium pricing"
    full = srv.get_note(hits[0]["id"])
    assert "Freemium" in full
    with pytest.raises(ValueError):
        srv.search_notes("x", vault="work")  # work vault blocked by default
    out = srv.capture("Test thought from Claude", title="Test thought", topic="ideas")
    assert out.startswith("Captured as")
    assert srv.timeline("kettle")


def test_http_mcp_refuses_to_start_without_a_token(env, monkeypatch):
    """Every tool on this server reads the vault, so an open port is the whole
    vault. Failing to start is the only safe default."""
    import ozu.mcp_server as srv
    importlib.reload(srv)
    monkeypatch.delenv("OZU_MCP_TOKEN", raising=False)
    import ozu.config as config
    config._config = None
    monkeypatch.setattr(srv.sys, "argv", ["ozu", "mcp", "--http"])
    with pytest.raises(SystemExit, match="OZU_MCP_TOKEN"):
        srv.main()


def test_token_turns_on_auth_and_stdio_leaves_it_off(env, monkeypatch):
    """stdio is a pipe to a local process and needs no token; http must not
    be reachable without one."""
    import ozu.mcp_server as srv
    importlib.reload(srv)
    assert srv.build_server().settings.auth is None
    assert srv.build_server("tok").settings.auth is not None
    assert len(srv._TOOLS) == 6  # the decorator still registers every tool


def test_a_static_token_is_not_checked_for_an_audience(env, monkeypatch):
    """StaticToken issues no `resource`. An SDK that checked one against
    `resource_server_url` (the default from mcp 3.0) would refuse the real
    token on every request, so the server says it does not, and the SDK's
    deprecation warning about leaving it unset never fires."""
    import warnings
    import ozu.mcp_server as srv
    from mcp.server.auth.settings import AuthSettings
    importlib.reload(srv)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        auth = srv.build_server("tok").settings.auth
    if "validate_token_resource" in AuthSettings.model_fields:
        assert auth.validate_token_resource is False


def test_rebinding_protection_knows_the_public_name(env, monkeypatch):
    """Behind a proxy the Host header is the public name, not the loopback
    bind — the SDK 421s that mismatch, and no local test can see it because
    locally the two are the same. Protection stays on; the names are listed."""
    monkeypatch.setenv("OZU_MCP_PUBLIC_URL", "https://ozu.example.com")
    import ozu.config as config
    config._config = None
    import ozu.mcp_server as srv
    importlib.reload(srv)
    s = srv.transport_security()
    assert s.enable_dns_rebinding_protection is True
    assert "ozu.example.com" in s.allowed_hosts       # as a browser sends it
    assert "127.0.0.1:8321" in s.allowed_hosts        # and the local bind
    assert "https://ozu.example.com" in s.allowed_origins


def test_the_token_is_compared_whole(env, monkeypatch):
    """A prefix must not pass, and comparison is constant-time."""
    import asyncio
    import ozu.mcp_server as srv
    importlib.reload(srv)
    v = srv.StaticToken("s3cret-token")
    assert asyncio.run(v.verify_token("s3cret-token")) is not None
    assert asyncio.run(v.verify_token("s3cret")) is None
    assert asyncio.run(v.verify_token("s3cret-token-and-more")) is None
    assert asyncio.run(v.verify_token("")) is None


def test_digest_written_to_disk_not_sent(env, monkeypatch):
    """Digest delivery is local: a file under ~/ozu-data/digests, no network call."""
    from ozu import ingest, understand, digest
    make_pdf(env.raw_dir / "Inbox" / "dd.pdf", ["y"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()

    path = digest.run()
    assert path.exists()
    assert path.parent == env.digest_dir
    assert "Ozu weekly digest" in path.read_text()
    # No messaging gateway is reachable from this module any more.
    assert not hasattr(digest, "_send_whatsapp")


def test_init_does_not_create_work_vault(env):
    """Employer content stays on-device: `init` scaffolds vault only."""
    from ozu import vault
    env.ensure_dirs()
    vault.scaffold(env.vault_dir)
    assert env.vault_dir.exists()
    assert not env.work_vault_dir.exists()


def test_work_vault_scaffolds_on_demand_as_backstop(env, monkeypatch):
    """If a work page slips through anyway, it still lands behind the wall."""
    from ozu import ingest, understand
    assert not env.work_vault_dir.exists()
    make_pdf(env.raw_dir / "Work" / "p.pdf", ["confidential"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    assert env.work_vault_dir.exists(), "work content must not fall back to vault"
    assert not list(env.vault_dir.glob("topics/**/*.md")), "work content leaked into vault"


def test_scribe_text_layer_captured_and_passed_to_vision(env, monkeypatch):
    """A PDF with a text layer becomes a transcription prior for the model."""
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "ocr.pdf", ["freemium pantry scan"])
    ingest.ingest()

    con = db.connect(env.db_path)
    row = con.execute("SELECT ocr_text FROM pages ORDER BY id DESC LIMIT 1").fetchone()
    assert "freemium pantry scan" in row["ocr_text"]

    seen = {}

    def fake(image_path, notebook, page_no, ocr_text="", kind="ink"):
        seen["ocr_text"] = ocr_text
        return dict(MOCK_RESULT)

    monkeypatch.setattr(understand, "analyze_page", fake)
    understand.process_pending()
    assert "freemium pantry scan" in seen["ocr_text"]

    prompt = understand._build_prompt("Inbox", 1, seen["ocr_text"])
    assert "freemium pantry scan" in prompt
    assert "never as ground truth" in prompt


def test_vision_prompt_handles_missing_text_layer(env):
    """Image-only exports still render a valid prompt."""
    from ozu import understand
    prompt = understand._build_prompt("Inbox", 1, "")
    assert understand.NO_OCR in prompt
    assert "{ocr_hint}" not in prompt


def test_placeholder_api_key_is_rejected_clearly(env, monkeypatch):
    """An unfilled .env must fail loudly, not as an opaque 401 mid-run."""
    from ozu import understand
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    import ozu.config as config
    config._config = None
    with pytest.raises(RuntimeError, match="placeholder"):
        understand._client()


def test_page_footer_is_not_treated_as_an_ocr_prior(env):
    """A Scribe export with no Convert-to-text still has a '1 of 1' footer."""
    from ozu.ingest import _useful_ocr
    assert _useful_ocr("1 of 1") == ""
    assert _useful_ocr("  2 of 7 ") == ""
    assert _useful_ocr("short") == ""
    real = "Atlas v3 combining v1 and v2, one login for B2B and B2C"
    assert _useful_ocr(real) == real


def test_skipped_page_is_never_reingested(env, monkeypatch):
    """Purged pages keep their hash as a tombstone: the Scribe re-exports whole
    notebooks cumulatively, so a deleted-but-forgotten page would come back."""
    from ozu import ingest, db
    pdf = env.raw_dir / "Inbox" / "cumulative.pdf"
    make_pdf(pdf, ["sensitive page"])
    assert ingest.ingest() == 1

    con = db.connect(env.db_path)
    con.execute("UPDATE pages SET status='skipped'")
    con.commit()

    # Same notebook re-exported unchanged — the page must not come back.
    assert ingest.ingest() == 0
    rows = con.execute("SELECT status FROM pages").fetchall()
    assert [r["status"] for r in rows] == ["skipped"]


def test_kindle_export_suffix_stripped_from_notebook_name(env):
    """Amazon exports as '<Name>  - Kindle.pdf'; the suffix must not become
    part of the notebook name, which feeds source refs and the work prefix rule."""
    from ozu.ingest import _notebook_name
    raw = env.raw_dir
    assert _notebook_name(raw / "Inbox  - Kindle.pdf", raw) == "Inbox"
    # a re-export: Drive keeps the old file and numbers the new one
    assert _notebook_name(raw / "Inbox  - Kindle (1).pdf", raw) == "Inbox"
    assert _notebook_name(raw / "Inbox  - Kindle (12).pdf", raw) == "Inbox"
    assert _notebook_name(raw / "Work  - Kindle.pdf", raw) == "Work"
    assert _notebook_name(raw / "Inbox.pdf", raw) == "Inbox"
    # A folder per notebook still wins over the filename.
    assert _notebook_name(raw / "Inbox" / "whatever  - Kindle.pdf", raw) == "Inbox"


def test_work_prefix_still_matches_after_normalisation(env, monkeypatch):
    """Governance must not be weakened by the rename: 'Work  - Kindle' -> 'Work'."""
    from ozu import ingest, understand
    make_pdf(env.raw_dir / "Work  - Kindle.pdf", ["confidential"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    assert env.work_vault_dir.exists(), "work notebook must still route behind the wall"


def test_asset_filename_uses_normalised_notebook_name(env):
    """Asset names are permanent in note frontmatter — keep Amazon's suffix out."""
    from ozu import ingest
    make_pdf(env.raw_dir / "Inbox  - Kindle.pdf", ["hello"])
    ingest.ingest()
    imgs = list(env.pages_dir.rglob("*.png"))
    assert imgs, "expected a rendered page"
    assert all("Kindle" not in i.name for i in imgs), [i.name for i in imgs]
    assert imgs[0].name.startswith("Inbox-p001-")


def make_pdf_with_footer(path: Path, pages: list[tuple[str, str]]):
    """Real Scribe exports carry Amazon's page-position footer at the bottom."""
    doc = fitz.open()
    for body, footer in pages:
        page = doc.new_page()
        page.insert_text((72, 72), body, fontsize=14)
        page.insert_text((280, page.rect.height - 18), footer, fontsize=9)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    doc.close()


def test_footer_change_does_not_break_dedup(env):
    """The same ink under "1", "1 of 4", "1 of 9" is the same page. Found
    live: one page was processed three times under three footers."""
    from ozu import ingest
    make_pdf_with_footer(env.raw_dir / "Inbox" / "a.pdf", [("same ink", "1")])
    assert ingest.ingest() == 1
    make_pdf_with_footer(env.raw_dir / "Inbox" / "b.pdf",
                         [("same ink", "1 of 4"), ("new ink", "2 of 4")])
    assert ingest.ingest() == 1  # only the genuinely new page


def test_ledger_rehash_migrates_old_hashes_in_place(env):
    """Existing rows carry full-bitmap hashes; the scheme change must not
    make their pages look unseen. Pages table is never dropped."""
    import hashlib
    from ozu import ingest, db
    pdf = env.raw_dir / "Inbox" / "old.pdf"
    make_pdf_with_footer(pdf, [("existing ink", "1")])

    # enqueue under the OLD scheme: hash of the full bitmap, footer included
    doc = fitz.open(pdf)
    pix = doc[0].get_pixmap(dpi=ingest.DPI)
    doc.close()
    old_hash = hashlib.sha256(pix.samples).hexdigest()
    con = db.connect(env.db_path)
    db.enqueue_page(con, old_hash, str(pdf), "Inbox", 1, "x.png", "")
    con.execute("UPDATE pages SET status='processed'")
    con.commit()

    assert ingest.ingest() == 0  # rehash ran; the page is still "seen"
    assert db.get_meta(con, "hash_scheme") == ingest.HASH_SCHEME
    row = con.execute("SELECT sha256 FROM pages").fetchone()
    assert row["sha256"] != old_hash  # migrated in place, not dropped


def test_checkmark_closes_matching_action(env, monkeypatch):
    """Red creates, ✓ closes: a completion line on a later page marks the
    fuzzy-matching open action done — exact wording not required."""
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "c1.pdf", ["page with action"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()  # creates "Check competitor pricing"

    make_pdf(env.raw_dir / "Inbox" / "c2.pdf", ["page with checkmark"])
    ingest.ingest()
    done_page = dict(MOCK_RESULT, notes=[], completions=["checked the competitor pricing"])
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: done_page)
    understand.process_pending()

    con = db.connect(env.db_path)
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 1


def test_checkmark_with_no_match_leaves_actions_alone(env, monkeypatch):
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "c3.pdf", ["a"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    make_pdf(env.raw_dir / "Inbox" / "c4.pdf", ["b"])
    ingest.ingest()
    stray = dict(MOCK_RESULT, notes=[], completions=["water the plants"])
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: stray)
    understand.process_pending()
    con = db.connect(env.db_path)
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 0


def test_detailed_checkmark_closes_terse_action(env):
    """Found live 2026-08-24: a ✓ line with extra detail ("Bought garden
    hose, it's a 30m reel with a brass nozzle") left its terse action open —
    the detail diluted the old score (2/6) and "bought" can't prefix-match
    "buy". The other actions stand in for the competition it had that day."""
    from ozu import db
    con = db.connect(env.db_path)
    db.add_action(con, "n1", "Buy garden hose")
    db.add_action(con, "n2", "Fix shed latch")
    db.add_action(con, "n3", "Fix water pump filter motor")
    db.add_action(con, "n4", "Sand porch steps")

    closed = db.close_matching_action(
        con, "Bought garden hose, it's a 30m reel with a brass nozzle")
    assert closed == "Buy garden hose"
    still_open = [r["text"] for r in
                  con.execute("SELECT text FROM actions WHERE done=0")]
    assert len(still_open) == 3 and "Buy garden hose" not in still_open


def test_one_shared_verb_does_not_close(env):
    """Coverage scoring must not let a lone verb overlap close a short
    action: "Called the bank about the card" is not "Call plumber"."""
    from ozu import db
    con = db.connect(env.db_path)
    db.add_action(con, "n1", "Call plumber")
    assert db.close_matching_action(con, "Called the bank about the card") is None
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 0


def test_walled_page_cannot_close_personal_actions(env, monkeypatch):
    """Governance: a ✓ on a work page must not touch the personal ledger."""
    from ozu import ingest, understand, db
    make_pdf(env.raw_dir / "Inbox" / "c5.pdf", ["a"])
    ingest.ingest()
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: dict(MOCK_RESULT))
    understand.process_pending()
    make_pdf(env.raw_dir / "Work" / "c6.pdf", ["work page"])
    ingest.ingest()
    walled = dict(MOCK_RESULT, work_flag=True, notes=[],
              completions=["check competitor pricing"])
    monkeypatch.setattr(understand, "analyze_page", lambda *a, **k: walled)
    understand.process_pending()
    con = db.connect(env.db_path)
    assert con.execute("SELECT done FROM actions").fetchone()["done"] == 0
    # nor may it leave a ✓ receipt — walled pages are inert for mail too
    assert con.execute("SELECT COUNT(*) c FROM receipts").fetchone()["c"] == 0


def test_mapped_repos_become_known_project_tags(env, monkeypatch):
    """Wiring a repo in OZU_CLAUDE_REPOS must teach the vision pass its tag
    without a prompt edit."""
    import ozu.config as config
    monkeypatch.setenv("OZU_CLAUDE_REPOS",
                       "atlas=git@x:o/atlas-v3.git,ozu=git@x:o/ozu.git")
    config._config = None
    from ozu import understand
    prompt = understand._build_prompt("Inbox", 1)
    assert "atlas" in prompt and "ozu" in prompt and "kettle" in prompt
