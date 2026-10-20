"""Articles, posts and documents into the learning loop (articles.py).

The network is faked at the one place it is touched — `articles._curl` (one
request, one pinned address) and `articles._resolve` (the name lookup the
guard decides on) — so the guard, the readers, the figure pass, the note, the
assets and the git boundary are all real. Fixture pages, images and PDFs are
built in the test with PyMuPDF; nothing is downloaded."""
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import fitz
import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from test_pipeline import env  # noqa: F401,E402 — shared fixture

PUBLIC_IP = "93.184.216.34"
PRIVATE_HOSTS = {"evil.example": "127.0.0.1", "lan.example": "192.168.1.10"}


def png(w=800, h=500, shade=180) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h), False)
    pix.clear_with(shade)
    return pix.tobytes("png")


@pytest.fixture()
def web(monkeypatch):
    """url → (status, content type, location, body). Anything else is a 404."""
    from ozu import articles
    pages: dict[str, tuple] = {}
    calls: list[tuple[str, str]] = []

    def fake_resolve(host):
        return [PRIVATE_HOSTS.get(host, PUBLIC_IP)]

    def fake_curl(url, addr, *, accept, cap):
        calls.append((url, addr))
        status, ctype, location, body = pages.get(url, (404, "text/html", "", b"<html>nope</html>"))
        if len(body) > cap:
            raise articles.Unreadable("over the cap")
        return status, ctype, location, body

    monkeypatch.setattr(articles, "_resolve", fake_resolve)
    monkeypatch.setattr(articles, "_curl", fake_curl)
    web.pages, web.calls = pages, calls
    return web


PARAS = [
    "Segmenting the home lab from the smart home network stopped the broadcast storms that "
    "had been knocking the hub offline every few days.",
    "The trick was a dedicated VLAN for the lab, with an mDNS reflector so casting still works "
    "across the boundary without opening everything else.",
    "After a week the hub had not dropped once, and the lab could be rebooted at will without "
    "taking the lights with it, which was the whole point of the exercise.",
]


def article_html(extra_body="") -> str:
    return f"""<html><head><title>I segmented my home lab</title>
<meta property="og:title" content="I segmented my home lab">
<meta name="author" content="Joe Example"></head><body>
<header><img src="/logo.png" alt="site logo"></header>
<article class="w-article widget">
  <h1>I segmented my home lab</h1>
  <p>{PARAS[0]}</p>
  <figure><img src="/img/vlan-diagram.png" alt="diagram"><figcaption>The VLAN layout</figcaption></figure>
  <p>{PARAS[1]}</p>
  <a href="/another-article/"><img src="/img/teaser.png" alt="read next"></a>
  <img src="/img/tiny-icon.png" alt="">
  <p>{PARAS[2]}</p>
  <img data-src="/img/switch.png" src="/img/placeholder.gif" alt="the switch">
  <div class="related-posts"><img src="/img/related.png"></div>
  {extra_body}
</article>
<aside><img src="/img/sidebar.png"></aside>
<div class="more"><img src="/img/after-the-article.png"><p>Unrelated footer text that is long enough to count.</p></div>
</body></html>"""


def serve_article(web, url="https://blog.example/posts/home-lab"):
    web.pages[url] = (200, "text/html", "", article_html().encode())
    for name, size in (("vlan-diagram.png", (900, 600)), ("switch.png", (800, 450)),
                       ("teaser.png", (800, 450)), ("related.png", (800, 450)),
                       ("sidebar.png", (800, 450)), ("after-the-article.png", (800, 450)),
                       ("tiny-icon.png", (32, 32))):
        web.pages[f"https://blog.example/img/{name}"] = (200, "image/png", "", png(*size, shade=len(name) * 7 % 255))
    web.pages["https://blog.example/logo.png"] = (200, "image/png", "", png(600, 300))
    return url


def frontmatter(path: Path) -> dict:
    from ozu import okf
    return okf.parse_frontmatter(Path(path).read_text())[0]


# ---------------------------------------------------------------- URLs and the guard

def test_canonical_drops_tracking_fragment_and_spelling():
    from ozu.articles import canonical
    assert canonical("https://WWW.Blog.example/a/b/?utm_source=x&id=7&fbclid=y#top") == \
        "https://blog.example/a/b?id=7"
    assert canonical("https://twitter.com/karpathy/status/1") == "https://x.com/karpathy/status/1"
    assert canonical("https://www.threads.net/@a/post/B") == "https://threads.com/@a/post/B"


@pytest.mark.parametrize("url,why", [
    ("http://evil.example/", "not a public address"),
    ("http://lan.example/admin", "not a public address"),
    ("http://blog.example:8321/", "port 8321"),
    ("file:///etc/passwd", "not a web link"),
    ("ftp://blog.example/x", "not a web link"),
])
def test_the_guard_refuses_before_connecting(web, url, why):
    from ozu import articles
    with pytest.raises(articles.Unreadable, match=why):
        articles.fetch(url)
    assert web.calls == []


def test_a_redirect_to_loopback_is_refused_at_the_hop(web):
    from ozu import articles
    web.pages["https://blog.example/go"] = (302, "text/html", "http://evil.example:80/mcp", b"")
    with pytest.raises(articles.Unreadable, match="not a public address"):
        articles.fetch("https://blog.example/go")
    assert [u for u, _ in web.calls] == ["https://blog.example/go"]


def test_every_request_is_pinned_to_the_checked_address(web):
    from ozu import articles
    web.pages["https://blog.example/a"] = (301, "text/html", "/b", b"")
    web.pages["https://blog.example/b"] = (200, "text/html", "", b"<html>ok</html>")
    final, _, body, status = articles.fetch("https://blog.example/a")
    assert (final, status, body) == ("https://blog.example/b", 200, b"<html>ok</html>")
    assert {addr for _, addr in web.calls} == {PUBLIC_IP}


def test_curl_is_called_with_the_pin_and_without_curlrc(monkeypatch):
    """The real _curl, with Popen captured: the flags are the guard."""
    from ozu import articles
    seen = {}

    class FakeProc:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            hdr = cmd[cmd.index("-D") + 1]
            Path(hdr).write_text("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n\r\n")
            import io
            self.stdout, self.stderr = io.BytesIO(b"<html>hi</html>"), io.BytesIO(b"")

        def wait(self):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(articles.subprocess, "Popen", FakeProc)
    status, ctype, _, body = articles._curl("https://blog.example/x", "93.184.216.34",
                                            accept="*/*", cap=1000)
    cmd = seen["cmd"]
    assert (status, ctype, body) == (200, "text/html", b"<html>hi</html>")
    assert cmd[:2] == ["curl", "-q"]                       # -q must come first to skip ~/.curlrc
    assert cmd[cmd.index("--resolve") + 1] == "blog.example:443:93.184.216.34"
    assert cmd[cmd.index("--max-redirs") + 1] == "0"
    assert cmd[cmd.index("--proto") + 1] == "=http,https"


# ---------------------------------------------------------------- a page

def test_an_article_keeps_its_own_figures_and_nothing_else(env, web):
    from ozu import articles
    r = articles.capture(serve_article(web), origin="test:1")
    assert r["status"] == "queued" and r["kind"] == "article"
    fm = frontmatter(r["path"])
    assert fm["type"] == "Article" and fm["learn"] == "requested" and fm["origin"] == "test:1"
    assert fm["source"].startswith("web:") and fm["url"] == "https://blog.example/posts/home-lab"
    # the diagram and the lazy-loaded switch; not the teaser (a link to another page), the
    # related block, the sidebar, the image after the text, the icon, or the logo
    assert fm["figures"] == 2
    body = Path(r["path"]).read_text()
    assert "[figure 1]" in body and "The VLAN layout" in body
    assert "vlan-diagram.png" in body and "switch.png" in body
    for absent in ("teaser.png", "related.png", "sidebar.png", "after-the-article.png", "logo.png"):
        assert absent not in body
    assets = env.vault_dir / fm["assets"]
    assert sorted(p.name for p in assets.iterdir()) == ["fig-01.jpg", "fig-02.jpg"]
    assert max(fitz.open(assets / "fig-01.jpg")[0].rect) <= articles.FIG_MAX_SIDE


def test_figures_stay_out_of_git_and_the_note_goes_in(env, web):
    from ozu import articles
    r = articles.capture(serve_article(web))
    v = env.vault_dir
    assert "/assets/learning/" in (v / ".gitignore").read_text().splitlines()
    tracked = subprocess.run(["git", "ls-files"], cwd=v, capture_output=True, text=True).stdout
    assert f"topics/learning/{r['id']}.md" in tracked
    assert "assets/learning" not in tracked
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=v, capture_output=True, text=True).stdout
    assert "assets/learning" not in dirty           # catch_up would commit it as a hand edit


def test_the_same_link_twice_is_one_note(env, web):
    from ozu import articles
    url = serve_article(web)
    first = articles.capture(url)
    again = articles.capture(url + "?utm_source=whatsapp#comments")
    assert again["status"] == "duplicate" and again["id"] == first["id"]
    assert len(list((env.vault_dir / "topics" / "learning").glob("2*.md"))) == 1


def test_a_walled_page_is_written_down_as_skipped_and_reaches_the_sheet(env, web):
    from ozu import articles, db, learn
    url = "https://walled.example/p/x"
    web.pages[url] = (403, "text/html", "", b"<html><title>Just a moment...</title></html>")
    r = articles.capture(url)
    assert r["status"] == "skipped" and "403" in r["reason"] and "Cloudflare" in r["reason"]
    fm = frontmatter(r["path"])
    assert fm["learn"] == "skipped" and "403" in fm["skip_reason"]
    data = learn.gather(db.connect(env.db_path), dt.date.today())
    assert [s["id"] for s in data["skipped"]] == [r["id"]]
    # tried again once it reads: the skipped note is replaced, not duplicated
    serve_article(web, url)
    r2 = articles.capture(url)
    assert r2["status"] == "queued"
    assert not Path(r["path"]).exists() or r2["path"] == r["path"]
    assert len(list((env.vault_dir / "topics" / "learning").glob("2*.md"))) == 1


def test_an_empty_page_is_unreadable_not_a_brief(env, web):
    from ozu import articles
    web.pages["https://app.example/"] = (200, "text/html", "", b"<html><body><div id=root></div></body></html>")
    r = articles.capture("https://app.example/")
    assert r["status"] == "skipped" and "JavaScript" in r["reason"]


# ---------------------------------------------------------------- the social readers

def test_an_x_post_is_read_whole_with_its_image(env, web):
    from ozu import articles
    web.pages["https://api.fxtwitter.com/karpathy/status/42"] = (200, "application/json", "", json.dumps({
        "tweet": {"text": "There's a new kind of coding I call vibe coding. " * 12,
                  "author": {"name": "Andrej Karpathy", "screen_name": "karpathy"},
                  "created_at": "Sun Feb 02 23:17:15 +0000 2025",
                  "media": {"photos": [{"url": "https://pbs.example/shot.png"}]}}}).encode())
    web.pages["https://pbs.example/shot.png"] = (200, "image/png", "", png(1200, 800))
    r = articles.capture("https://twitter.com/karpathy/status/42?s=20")
    fm = frontmatter(r["path"])
    assert (fm["kind"], fm["site"], fm["figures"]) == ("post", "X", 1)
    assert fm["author"] == "Andrej Karpathy (@karpathy)"
    assert fm["url"] == "https://x.com/karpathy/status/42"
    assert "vibe coding" in Path(r["path"]).read_text()


def test_x_first_party_says_when_it_cut_the_post(env, web, monkeypatch):
    from ozu import articles
    monkeypatch.setenv("OZU_X_READER", "syndication")
    web.pages["https://cdn.syndication.twimg.com/tweet-result?id=42&token=x"] = (
        200, "application/json", "", json.dumps({
            "text": "A long post that X's embed cuts at two hundred and eighty characters " * 4,
            "user": {"name": "A", "screen_name": "a"}, "note_tweet": {"id": "n"}}).encode())
    r = articles.capture("https://x.com/a/status/42")
    assert "280" in frontmatter(r["path"])["partial"]


def test_a_threads_post_is_its_share_text_and_image(env, web):
    from ozu import articles
    url = "https://threads.com/@githubprojects/post/Deog"
    web.pages[url] = (200, "text/html", "", b"""<html><head>
<meta property="og:title" content="Github Projects (@githubprojects) on Threads">
<meta property="og:description" content="Open-source SSH gateway for servers and edge devices">
<meta property="og:image" content="https://cdn.example/post.jpg"></head><body></body></html>""")
    web.pages["https://cdn.example/post.jpg"] = (200, "image/png", "", png(1080, 1080))
    r = articles.capture(url)
    fm = frontmatter(r["path"])
    assert r["status"] == "queued"                  # eight words and an image is enough
    assert (fm["kind"], fm["site"], fm["figures"]) == ("post", "Threads", 1)
    assert fm["author"] == "Github Projects (@githubprojects)"
    assert "cut" in fm["partial"]


def test_a_linkedin_post_is_its_commentary_not_the_comments(env, web):
    from ozu import articles
    url = "https://linkedin.com/posts/carl_you-can-now-mod-activity-1"
    web.pages[url] = (200, "text/html", "", f"""<html><head>
<meta property="og:title" content="Build Custom Mods | 🥞 Carl Vellotti posted on the topic | LinkedIn"></head><body>
<article><div><p data-test-id="main-feed-activity-card__commentary">YOU CAN NOW MOD CLAUDE CODE. {PARAS[0]} {PARAS[1]}</p></div>
<img data-delayed-url="https://media.licdn.com/dms/image/feedshare-1.png"></article>
<section class="comments"><p>Great post! Totally agree with everything here, thanks for sharing it.</p>
<img data-delayed-url="https://media.licdn.com/dms/image/comment.png"></section>
</body></html>""".encode())
    web.pages["https://media.licdn.com/dms/image/feedshare-1.png"] = (200, "image/png", "", png(1200, 900))
    r = articles.capture(url)
    fm = frontmatter(r["path"])
    body = Path(r["path"]).read_text()
    assert (fm["kind"], fm["site"], fm["author"], fm["figures"]) == ("post", "LinkedIn", "Carl Vellotti", 1)
    assert "YOU CAN NOW MOD" in body and "Great post" not in body


def test_substack_is_read_through_its_api_and_a_preview_says_so(env, web):
    from ozu import articles
    api = "https://justin.substack.com/api/v1/posts/inside-the-algorithm"
    web.pages[api] = (200, "application/json", "", json.dumps({
        "title": "Inside the Algorithm", "post_date": "2026-03-01T10:00:00Z",
        "publication": {"name": "Justin's Letter"}, "publishedBylines": [{"name": "Justin W"}],
        "audience": "only_paid", "truncated_body_text": "…",
        "body_html": "".join(f"<p>{p}</p>" for p in PARAS)}).encode())
    r = articles.capture("https://justin.substack.com/p/inside-the-algorithm")
    fm = frontmatter(r["path"])
    assert fm["site"] == "Justin's Letter" and fm["author"] == "Justin W"
    assert "preview" in fm["partial"]
    assert not any("substack.com/p/" in u for u, _ in web.calls)    # the walled page is never asked


# ---------------------------------------------------------------- documents

def make_pdf(pages: list[str], image_on: int | None = None, scanned=False) -> bytes:
    doc = fitz.open()
    for i, text in enumerate(pages):
        page = doc.new_page()
        if scanned:
            page.insert_image(page.rect, stream=png(600, 800))
            continue
        page.insert_textbox(fitz.Rect(72, 72, 520, 400), text, fontsize=11)
        if image_on == i:
            page.insert_image(fitz.Rect(72, 420, 520, 760), stream=png(900, 600))
    doc.set_metadata({"title": "Graph Retrieval, Measured", "author": "A. Researcher"})
    return doc.tobytes()


def test_a_pdf_link_keeps_its_text_its_figure_pages_and_the_original(env, web):
    from ozu import articles
    text = " ".join(PARAS) + " "
    url = "https://arxiv.example/pdf/2606.06003"
    web.pages[url] = (200, "application/pdf", "", make_pdf([text * 2, text * 2, text], image_on=1))
    r = articles.capture(url)
    fm = frontmatter(r["path"])
    assert (fm["type"], fm["kind"], fm["pages"], fm["figures"]) == ("Article", "pdf", 3, 1)
    assert fm["title"] == "Graph Retrieval, Measured" and fm["author"] == "A. Researcher"
    body = Path(r["path"]).read_text()
    assert "### p. 1" in body and "### p. 3" in body and "figure page 2" in body
    assert (env.vault_dir / fm["assets"] / "original.pdf").read_bytes()[:5] == b"%PDF-"


def test_a_scanned_pdf_is_read_from_page_images(env, tmp_path):
    from ozu import articles
    f = tmp_path / "scan.pdf"
    f.write_bytes(make_pdf(["", "", ""], scanned=True))
    r = articles.capture(str(f), origin="whatsapp:7", name="Scan of the lease.pdf")
    fm = frontmatter(r["path"])
    assert r["status"] == "queued" and fm["type"] == "Document" and fm["source"].startswith("file:")
    assert fm["figures"] == 3 and "scanned" in fm["partial"]


def test_a_pdf_over_the_page_cap_asks_for_a_chapter(env, web, monkeypatch):
    from ozu import articles
    monkeypatch.setattr(articles, "MAX_PDF_PAGES", 2)
    web.pages["https://docs.example/book.pdf"] = (200, "application/pdf", "", make_pdf(["a"] * 3))
    r = articles.capture("https://docs.example/book.pdf")
    assert r["status"] == "skipped" and "send a chapter" in r["reason"]


def test_a_sent_image_with_a_caption(env, tmp_path):
    from ozu import articles
    f = tmp_path / "shot.png"
    f.write_bytes(png(1170, 2532))
    r = articles.capture(str(f), caption="The pricing page of the competitor", name="shot.png")
    fm = frontmatter(r["path"])
    assert (fm["kind"], fm["figures"]) == ("image", 1)
    assert fm["title"] == "The pricing page of the competitor"


# ---------------------------------------------------------------- the loop

BRIEF = """# Takeaways
- Segment the lab from the house: a broadcast storm in one should never reach the other.

# Techniques, tools, tips
- An mDNS reflector keeps casting working across VLANs [figure 1].

# Where it touches your work
- [[20250101-old-note]]: the same failure mode.

# Try this week
Draw your network on one page.

# Question for the page
What would break first if the lab VLAN went down?"""


def test_an_article_gets_its_own_prompt_and_the_same_allowlist(env, web, monkeypatch):
    from ozu import agent, db, learn
    from ozu import articles
    r = articles.capture(serve_article(web))
    seen = {}

    def fake_run(prompt, workdir, allowed_tools=None, model=None, timeout=None, writes=False):
        seen.update(prompt=prompt, tools=allowed_tools, writes=writes)
        return BRIEF

    monkeypatch.setattr(agent, "_run_claude", fake_run)
    assert learn.work_pending() == 1
    p = seen["prompt"]
    assert "an article the owner sent" in p and "YouTube" not in p
    assert f"topics/learning/{r['id']}.md" in p
    assert "2 figures saved as images in `assets/learning/" in p
    assert "third-party content" in p
    assert seen["tools"] == learn.learn_tools(env.vault_dir)      # Read on the vault, nothing else
    assert seen["writes"] is False
    assert frontmatter(r["path"])["learn"] == "done"
    con = db.connect(env.db_path)
    brief = con.execute("SELECT * FROM notes WHERE (','||tags||',') LIKE '%,claude-learn,%'").fetchone()
    bfm = frontmatter(brief["path"])
    assert bfm["source"] == f"learn:{r['id']}" and bfm["channel"] == "blog.example"
    assert bfm["duration"].endswith("min read")
    assert any(s["id"] == "web" for s in bfm["sources"])
    item = learn.gather(con, dt.date.today())["new"][0]
    assert (item["channel"], item["duration"]) == ("blog.example", bfm["duration"])


def test_lint_knows_articles_are_the_loop(env, web, monkeypatch):
    from ozu import agent, learn, lint
    from ozu import articles
    articles.capture(serve_article(web))
    monkeypatch.setattr(agent, "_run_claude", lambda *a, **k: BRIEF)
    learn.work_pending()
    findings = lint.lint(env.vault_dir)
    assert not [f for f in findings if "not written by the learning loop" in f]
    assert not [f for f in findings if "no Learning note" in f]


def test_the_wiki_never_reads_a_raw_article():
    from ozu import wiki
    assert "web" not in wiki.FEED_SOURCES and "file" not in wiki.FEED_SOURCES
