"""Articles, posts and documents as learning sources — the same loop as a pinned video.

A link (a blog post, a news article, a Substack, an X / Threads / LinkedIn post) or a
file (a PDF, a screenshot) becomes a source note under `topics/learning/` with
`learn: requested`, exactly the shape a capture bridge gives a YouTube transcript.
From there nothing is new: `learn.work_pending()` briefs it, the evening sheet carries
it, the +2/+7/+30 returns, the wiki reads the brief. Only the reader is new, and it is
here.

Reading happens in this code, never in the model. The brief run keeps the learning
run's whole allowlist — `Read` on the vault and nothing else — because the page is
fetched, the text extracted and the figures saved before any model sees it; a prompt
hidden in an article can at worst produce a bad brief. Figures matter — a chart or a screenshot is often the content — so they are
saved as image files the run can open with the same vault-scoped `Read`. They live in
`assets/learning/<note id>/`, which the vault's `.gitignore` keeps out of git: three
articles a day of figures would add a gigabyte a year to every clone of the vault.

Fetching is guarded because a URL is an instruction to this machine: http(s) on the
default ports only, every hop of a redirect resolved here and refused unless every
address is public (the box serves things on loopback), and the connection pinned to
the address that was checked (`curl --resolve`), so a DNS answer that changes between
the check and the connect cannot reach loopback either. Size and time caps, and an
honest user agent — measured on 2026-10-20, a browser UA unlocked nothing the honest
one did not. The client is the system `curl`, not a Python HTTP library, for a measured
reason: Substack's Cloudflare front answered Python's TLS stack 403 and curl 200 on the
same URL, headers and HTTP version alike. `-q` keeps any `~/.curlrc` out of it.

Readers, by what the measurements showed:
- a page: trafilatura for the text (the best extractor measured), but our own figure
  pass over the article's DOM, because trafilatura kept 0 of 30 images on xda;
- Substack: Cloudflare answers 403 to a datacenter IP, its own `/api/v1/posts/<slug>`
  answers the full body of a free post;
- X: the first-party syndication endpoint cuts a long post at 280 characters, so the
  post is read through FixTweet's API (`OZU_X_READER=syndication` keeps it first-party,
  at the cost of the cut);
- Threads: a JavaScript app, so its share text and image (`og:`), where the image is
  usually the content;
- LinkedIn: the post's own commentary block and the images of its card, not the page
  (whose text is mostly other people's comments); a `/pulse/` article is a page;
- a PDF: the text layer through `textlayer.extract()` (never `get_text()`), and the
  pages that carry images or charts rendered as figures; a scanned PDF is read as
  page images.

Something unreadable is still written down — a note with `learn: skipped` and a
`skip_reason` — so it reaches the evening sheet's "could not be read" lines, as a
skipped video does, rather than vanishing.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import html as htmllib
import ipaddress
import json
import logging
import os
import re
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import fitz

from .config import get_config
from . import db, indexer, okf, textlayer, vault

log = logging.getLogger("ozu.articles")

FOLDER = "learning"
ASSETS = Path("assets") / "learning"
UA = "Mozilla/5.0 (compatible; OzuReader/1.0; personal learning tool)"
ACCEPT_IMG = "image/jpeg,image/png,image/gif;q=0.9,*/*;q=0.1"   # no WebP: MuPDF cannot shrink it
TIMEOUT = 20.0
MAX_HOPS = 8
MAX_PAGE_BYTES = 40 * 1024 * 1024
MAX_IMG_BYTES = 8 * 1024 * 1024
MAX_FIGURES = 12
MAX_CANDIDATES = 30
FIG_MIN_SIDE, FIG_MIN_AREA = 200, 120_000     # below this an image is an icon or a thumbnail
FIG_MAX_SIDE = 1400                            # long side after downscaling
MAX_PDF_PAGES = 200
SCANNED_PAGES = 30                             # page images kept for a PDF with no text layer
MIN_WORDS = 40
WORDS_PER_MIN = 230

TRACKING = re.compile(r"^(utm_.*|fbclid|gclid|dclid|msclkid|igshid|mc_cid|mc_eid|_hsenc|_hsmi|"
                      r"ref|ref_src|ref_url|s|si|t|share|rcm|trk|trackingId)$", re.I)
BLOCKED = [
    ("a Cloudflare challenge", re.compile(r"<title>\s*Just a moment|cf-chl-|challenge-platform", re.I)),
    ("a bot check", re.compile(r"<title>\s*(One moment, please|Attention Required|Access denied)", re.I)),
    ("a login wall", re.compile(r"(log|sign) in to (continue|see|view)", re.I)),
]


class Unreadable(Exception):
    """Why a source could not be read — the sentence the owner sees."""


@dataclasses.dataclass
class Figure:
    data: bytes
    ext: str
    caption: str = ""
    origin: str = ""          # URL, or "p. 7" for a PDF page


@dataclasses.dataclass
class Extract:
    kind: str                 # article | post | pdf | image
    title: str
    text: str = ""            # Markdown; may carry [figure N] markers
    site: str = ""
    author: str = ""
    published: str = ""
    url: str = ""
    figures: list[Figure] = dataclasses.field(default_factory=list)
    pages: int = 0
    partial: str = ""         # why the text may be incomplete, when it may be
    original: bytes | None = None   # a PDF, kept beside the figures

    @property
    def words(self) -> int:
        return len(re.sub(r"\[figure \d+[^\]]*\]", "", self.text).split())


# ---------------------------------------------------------------- URLs and the network

def canonical(url: str) -> str:
    """One spelling per article, so the same link sent twice is one note: lower-case
    host, no fragment, no tracking parameters, no trailing slash, x.com for twitter."""
    p = urlsplit(url.strip())
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    host = {"twitter.com": "x.com", "threads.net": "threads.com"}.get(host, host)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(((p.scheme or "https").lower(), host, path, query, ""))


def _resolve(host: str) -> list[str]:
    return [i[4][0] for i in socket.getaddrinfo(host, None)]


def _check_target(url: str) -> str:
    """The address to connect to — refused unless every address the name has is
    public. Returned so the connection goes exactly there."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https"):
        raise Unreadable(f"not a web link ({p.scheme or 'no scheme'})")
    if p.port not in (None, 80, 443):
        raise Unreadable(f"port {p.port} is not fetched")
    host = p.hostname or ""
    try:
        addrs = _resolve(host)
    except (socket.gaierror, UnicodeError):
        raise Unreadable(f"{host} does not resolve") from None
    if not addrs:
        raise Unreadable(f"{host} does not resolve")
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            raise Unreadable(f"{host} is not a public address — refused")
    return addrs[0]


CURL_ERRORS = {6: "does not resolve", 7: "refused the connection", 28: "did not answer in time",
               35: "failed the TLS handshake", 47: "redirected too often", 52: "sent nothing back",
               56: "dropped the connection", 60: "has a certificate that does not verify",
               63: "sent more than the cap"}


def _curl(url: str, addr: str, *, accept: str, cap: int) -> tuple[int, str, str, bytes]:
    """One request, no redirects followed, to `addr` only. (status, content type,
    location, body)."""
    p = urlsplit(url)
    port = p.port or (443 if p.scheme == "https" else 80)
    pin = f"[{addr}]" if ":" in addr else addr
    with tempfile.NamedTemporaryFile("r", suffix=".hdr") as hdr:
        cmd = ["curl", "-q", "-sS", "--proto", "=http,https", "--max-redirs", "0",
               "--connect-timeout", "10", "--max-time", str(int(TIMEOUT)), "--compressed",
               "--resolve", f"{p.hostname}:{port}:{pin}", "--max-filesize", str(cap),
               "-A", UA, "-H", f"Accept: {accept}", "-H", "Accept-Language: en,fr;q=0.8,*;q=0.5",
               "-D", hdr.name, "-o", "-", url]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        chunks, size = [], 0
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > cap:
                proc.kill()
                proc.wait()
                raise Unreadable(f"over the {cap // 1048576} MB cap")
            chunks.append(chunk)
        err = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait()
        if code:
            why = CURL_ERRORS.get(code, f"could not be fetched (curl {code})")
            raise Unreadable(f"{p.hostname} {why}")
        blocks = [b for b in re.split(r"\r?\n\r?\n", hdr.read()) if b.strip()]
    head = blocks[-1].splitlines() if blocks else []
    try:
        status = int(head[0].split()[1])
    except (IndexError, ValueError):
        raise Unreadable(f"{p.hostname} sent no HTTP status ({err.strip()[:80]})") from None
    fields = {}
    for line in head[1:]:
        k, _, v = line.partition(":")
        fields[k.strip().lower()] = v.strip()
    ctype = fields.get("content-type", "").split(";")[0].strip().lower()
    return status, ctype, fields.get("location", ""), b"".join(chunks)


def fetch(url: str, *, accept: str = "*/*", cap: int = MAX_PAGE_BYTES):
    """GET with every redirect hop checked and pinned. Returns (final_url,
    content_type, bytes, status). Raises Unreadable with a reason a person can act on."""
    for _ in range(MAX_HOPS):
        addr = _check_target(url)
        status, ctype, location, data = _curl(url, addr, accept=accept, cap=cap)
        if status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        return url, ctype, data, status
    raise Unreadable("too many redirects")


def _get_ok(url: str, **kw) -> tuple[str, str, bytes]:
    final, ctype, data, status = fetch(url, **kw)
    if status >= 400:
        text = data[:20000].decode("utf-8", "replace")
        sign = next((why for why, rx in BLOCKED if rx.search(text)), None)
        raise Unreadable(f"{urlsplit(final).hostname} answered {status}"
                         + (f" with {sign}" if sign else ""))
    return final, ctype, data


def _get_json(url: str) -> dict:
    _, _, data = _get_ok(url, accept="application/json")
    try:
        return json.loads(data)
    except ValueError:
        raise Unreadable(f"{urlsplit(url).hostname} did not return JSON") from None


# ---------------------------------------------------------------- images

def _webp_size(b: bytes) -> tuple[int, int] | None:
    if b[:4] != b"RIFF" or b[8:12] != b"WEBP":
        return None
    chunk = b[12:16]
    if chunk == b"VP8 " and len(b) >= 30:
        return int.from_bytes(b[26:28], "little") & 0x3FFF, int.from_bytes(b[28:30], "little") & 0x3FFF
    if chunk == b"VP8L" and len(b) >= 25:
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(b) >= 30:
        return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    return None


def figure_from_bytes(data: bytes, *, caption: str = "", origin: str = "",
                      min_side: int = FIG_MIN_SIDE, min_area: int = FIG_MIN_AREA) -> Figure | None:
    """A figure worth keeping, downscaled — or None for an icon, a thumbnail or
    something that is not an image. MuPDF has no WebP decoder, and Claude reads
    WebP, so a WebP is kept as sent when its header says it is big enough."""
    size = _webp_size(data)
    if size:
        w, h = size
        if min(w, h) < min_side or w * h < min_area:
            return None
        return Figure(data, ".webp", caption, origin)
    try:
        doc = fitz.open(stream=data)
        if doc.is_pdf or doc.page_count != 1:
            return None
        page = doc[0]
        w, h = page.rect.width, page.rect.height
        if min(w, h) < min_side or w * h < min_area:
            return None
        scale = min(1.0, FIG_MAX_SIDE / max(w, h))
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return Figure(pix.tobytes("jpeg", jpg_quality=80), ".jpg", caption, origin)
    except Exception:  # noqa: BLE001 — anything MuPDF cannot open is not a figure
        return None


def _download_figures(cands: list[tuple[str, str]]) -> tuple[list[Figure], dict[str, int]]:
    """(url, caption) in document order → kept figures, and url → figure number."""
    out, seen, number = [], set(), {}
    for src, caption in cands[:MAX_CANDIDATES]:
        if len(out) >= MAX_FIGURES:
            break
        try:
            _, ctype, data = _get_ok(src, accept=ACCEPT_IMG, cap=MAX_IMG_BYTES)
        except Unreadable:
            continue
        digest = hashlib.sha1(data).hexdigest()
        if digest in seen or not (ctype.startswith("image/") or ctype in ("", "application/octet-stream")):
            continue
        fig = figure_from_bytes(data, caption=caption, origin=src)
        if fig:
            seen.add(digest)
            out.append(fig)
            number[src] = len(out)
    return out, number


_SKIP_IMG = re.compile(r"logo|avatar|icon|sprite|emoji|gravatar|badge|pixel|spacer|1x1|placeholder|"
                       r"\.svg(\?|$)|data:", re.IGNORECASE)


def _best_src(img) -> str:
    for attr in ("data-srcset", "srcset"):
        val = img.get(attr)
        if val:
            best, best_w = "", -1
            for part in val.split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                w = int(re.sub(r"\D", "", bits[1]) or 0) if len(bits) > 1 else 0
                if w >= best_w:
                    best, best_w = bits[0], w
            if best:
                return best
    for attr in ("data-src", "data-lazy-src", "data-original", "data-delayed-url", "src"):
        if img.get(attr):
            return img.get(attr)
    return ""


_NOT_CONTENT = re.compile(r"related|recommend|promo|teaser|newsletter|sidebar|share|social|"
                          r"comment|advert|\bad[s-]|banner|subscribe|author-box|widget|popular|"
                          r"more-from|read-next|outbrain|taboola", re.IGNORECASE)
_IMG_FILE = re.compile(r"\.(jpe?g|png|gif|webp|avif)(\?|$)", re.IGNORECASE)


def _norm(t: str) -> str:
    """Letters and digits only: the extractor's Markdown (`**`, `#`, links) must
    not stop a paragraph from matching the page it came from."""
    return " ".join(re.sub(r"[\W_]+", " ", t).split()).lower()


def _figure_candidates(root, base: str, text: str) -> list[tuple[str, str]]:
    """Images of the article itself, in reading order, with their captions.

    The largest `<article>` of a page also holds its related posts, its promos and
    its teasers (measured: domo-blog, watson). So an image is kept only when it sits
    between the first and the last paragraph the text extractor kept (or directly
    above the first, or directly after the last), is not inside
    a block named like furniture, and is not a link to another page — a teaser is an
    image wrapped in a link to the next article; a content image links to nothing or
    to its own full-size file."""
    for xpath in ("//article", "//*[@itemprop='articleBody']", "//main", "//body"):
        found = root.xpath(xpath)
        if found:
            container = max(found, key=lambda e: len(e.text_content()))
            break
    else:
        return []
    kept = _norm(text)
    order, first, last = [], None, None
    for el in container.iter():
        if not isinstance(el.tag, str):
            continue
        if el.tag in ("p", "li", "h2", "h3", "blockquote", "pre", "td"):
            t = _norm(el.text_content())
            if len(t) >= 30 and t[:50] in kept:
                last = len(order)
                first = len(order) if first is None else first
            order.append(None)
        elif el.tag == "img":
            order.append(el)
    if first is None:
        return []
    if first and order[first - 1] is not None:      # the lead image, just above the text
        first -= 1
    while last + 1 < len(order) and order[last + 1] is not None:   # a closing figure, before any other text
        last += 1
    out, seen = [], set()
    for img in order[first:last + 1]:
        if img is None:
            continue
        ancestors = []                  # inside the container only: its own class may be "widget"
        for a in img.iterancestors():
            if a is container:
                break
            ancestors.append(a)
        if any(a.tag in ("aside", "nav", "footer", "header", "form") for a in ancestors):
            continue
        if any(_NOT_CONTENT.search(f"{a.get('class', '')} {a.get('id', '')}") for a in ancestors):
            continue
        link = next((a for a in ancestors if a.tag == "a" and a.get("href")), None)
        if link is not None and not _IMG_FILE.search(link.get("href")):
            continue
        src = _best_src(img)
        if not src or _SKIP_IMG.search(src):
            continue
        src = htmllib.unescape(urljoin(base, src))
        same_file = src.split("?")[0]   # a gallery repeats one photo at several sizes
        if same_file in seen:
            continue
        seen.add(same_file)
        caption = ""
        fig = next((a for a in ancestors if a.tag == "figure"), None)
        if fig is not None:
            cap = fig.find(".//figcaption")
            if cap is not None:
                caption = " ".join(cap.text_content().split())
        out.append((src, htmllib.unescape(caption or (img.get("alt") or "").strip())))
    return out


def _mark_figures(text: str, number: dict[str, int], base: str) -> str:
    """trafilatura writes images as ![alt](src); keep the ones we kept as a numbered
    marker the brief can cite, drop the rest."""
    def repl(m):
        n = number.get(htmllib.unescape(urljoin(base, m.group(2))))
        return f"[figure {n}]" if n else ""
    return re.sub(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", repl, text)


# ---------------------------------------------------------------- readers

def _meta(html: str, name: str) -> str:
    m = re.search(rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"', html)
    if not m:
        m = re.search(rf'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="{re.escape(name)}"', html)
    return htmllib.unescape(m.group(1)).strip() if m else ""


def _metas(html: str, name: str) -> list[str]:
    return [htmllib.unescape(v) for v in re.findall(
        rf'<meta[^>]+(?:property|name)="{re.escape(name)}"[^>]+content="([^"]*)"', html)]


def read_page(url: str, html: str) -> Extract:
    import lxml.html
    import trafilatura
    sign = next((why for why, rx in BLOCKED if rx.search(html[:20000])), None)
    text = trafilatura.extract(html, url=url, output_format="markdown", include_images=True,
                               include_tables=True, include_links=False, favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html, default_url=url)
    try:
        root = lxml.html.fromstring(html)
        cands = _figure_candidates(root, url, text)
    except Exception:  # noqa: BLE001 — a page lxml cannot parse still has its text
        cands = []
    figures, number = _download_figures(cands)
    ex = Extract(kind="article", url=url, text=_mark_figures(text, number, url).strip(),
                 title=(meta.title if meta else "") or _meta(html, "og:title") or url,
                 site=(meta.sitename if meta else "") or urlsplit(url).hostname or "",
                 author=(meta.author if meta else "") or "",
                 published=(meta.date if meta else "") or "", figures=figures)
    if sign and ex.words < 150:
        raise Unreadable(f"{urlsplit(url).hostname} showed {sign}")
    return ex


def read_substack(url: str) -> Extract:
    """The post through Substack's own API: the page is Cloudflare-walled from a
    datacenter address, the API is not (measured 2026-10-20)."""
    p = urlsplit(url)
    slug = p.path.split("/p/", 1)[1].strip("/").split("/")[0]
    d = _get_json(f"https://{p.hostname}/api/v1/posts/{slug}")
    body = d.get("body_html") or ""
    page = f"<html><body><article><h1>{htmllib.escape(d.get('title') or '')}</h1>{body}</article></body></html>"
    ex = read_page(url, page)
    ex.title = d.get("title") or ex.title
    ex.site = (d.get("publication") or {}).get("name") or p.hostname or ""
    bylines = d.get("publishedBylines") or []
    ex.author = ", ".join(b.get("name", "") for b in bylines if b.get("name")) or ex.author
    ex.published = (d.get("post_date") or "")[:10]
    if d.get("truncated_body_text") or d.get("audience") not in (None, "everyone"):
        ex.partial = "a subscriber-only post: the free preview only"
    return ex


X_POST = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)")


def read_x(url: str) -> Extract:
    user, pid = X_POST.match(urlsplit(url).path).groups()
    if os.environ.get("OZU_X_READER", "fxtwitter") == "syndication":
        return _read_x_syndication(url, pid)
    try:
        t = _get_json(f"https://api.fxtwitter.com/{user}/status/{pid}").get("tweet") or {}
    except Unreadable:
        return _read_x_syndication(url, pid)
    if not t.get("text") and not t.get("article"):
        raise Unreadable("the post has no text (deleted, private or age-gated)")
    author = t.get("author") or {}
    parts = [t.get("text") or ""]
    art = t.get("article") or {}
    if art:
        parts.insert(0, f"## {art.get('title', '')}\n\n{art.get('preview_text', '')}".strip())
    quote = t.get("quote") or {}
    if quote.get("text"):
        qa = (quote.get("author") or {}).get("screen_name", "")
        parts.append(f"> Quoting @{qa}: {quote['text']}")
    photos = [m.get("url") for m in ((t.get("media") or {}).get("photos") or []) if m.get("url")]
    figures, _ = _download_figures([(p, "image in the post") for p in photos])
    if figures:
        parts.append(" ".join(f"[figure {i}]" for i in range(1, len(figures) + 1)))
    name = author.get("name") or user
    return Extract(kind="post", url=url, site="X", author=f"{name} (@{author.get('screen_name') or user})",
                   title=f"{name} on X: {_headline(t.get('text') or art.get('title') or '')}",
                   text="\n\n".join(p for p in parts if p), published=str(t.get("created_at") or "")[:16],
                   figures=figures,
                   partial="an X Article: its preview only" if art else "")


def _read_x_syndication(url: str, pid: str) -> Extract:
    d = _get_json(f"https://cdn.syndication.twimg.com/tweet-result?id={pid}&token=x")
    if not d.get("text"):
        raise Unreadable("the post has no text (deleted, private or age-gated)")
    user = d.get("user") or {}
    photos = [m.get("media_url_https") for m in d.get("mediaDetails") or [] if m.get("type") == "photo"]
    figures, _ = _download_figures([(p, "image in the post") for p in photos if p])
    return Extract(kind="post", url=url, site="X", author=f"{user.get('name', '')} (@{user.get('screen_name', '')})",
                   title=f"{user.get('name') or 'X'} on X: {_headline(d['text'])}", text=d["text"],
                   published=str(d.get("created_at") or "")[:16], figures=figures,
                   partial="a long post, cut at 280 characters by X's embed" if d.get("note_tweet") else "")


def read_threads(url: str, html: str) -> Extract:
    text = _meta(html, "og:description") or _meta(html, "description")
    title = _meta(html, "og:title")                    # "Name (@user) on Threads"
    figures, _ = _download_figures([(u, "image in the post") for u in _metas(html, "og:image")])
    if not text and not figures:
        raise Unreadable("Threads showed no text for this post (private, or removed)")
    author = re.sub(r"\s+on Threads$", "", title)
    if figures:
        text += "\n\n" + " ".join(f"[figure {i}]" for i in range(1, len(figures) + 1))
    return Extract(kind="post", url=url, site="Threads", author=author,
                   title=f"{author or 'Threads'}: {_headline(text)}", text=text.strip(), figures=figures,
                   partial="Threads shares only the post's opening; replies and long posts are cut")


def read_linkedin_post(url: str, html: str) -> Extract:
    import lxml.html
    root = lxml.html.fromstring(html)
    body = root.xpath("//*[@data-test-id='main-feed-activity-card__commentary']")
    if not body:
        return read_page(url, html)            # not a post card: read it as a page
    text = body[0].text_content().strip()
    card = next((a for a in body[0].iterancestors() if a.tag == "article"), None)
    cands = []
    if card is not None:
        for img in card.iter("img"):
            src = _best_src(img)
            if "media.licdn.com/dms/image" in src and "profile" not in src:
                cands.append((htmllib.unescape(src), (img.get("alt") or "").strip()))
    figures, _ = _download_figures(cands)
    if figures:
        text += "\n\n" + " ".join(f"[figure {i}]" for i in range(1, len(figures) + 1))
    parts = [x.strip() for x in _meta(html, "og:title").split("|")]
    who = next((x for x in parts if re.search(r"\bposted\b|\bshared\b", x)), "")
    author = re.sub(r"^[^\w]+|\s+(posted|shared)\b.*$", "", who) or _meta(html, "author")
    return Extract(kind="post", url=url, site="LinkedIn", author=author,
                   title=f"{author or 'LinkedIn'}: {_headline(text)}", text=text, figures=figures)


def read_pdf(data: bytes, *, url: str = "", name: str = "") -> Extract:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:  # noqa: BLE001
        raise Unreadable("the PDF does not open") from None
    if doc.needs_pass:
        raise Unreadable("the PDF is password-protected")
    if doc.page_count > MAX_PDF_PAGES:
        raise Unreadable(f"{doc.page_count} pages is over the {MAX_PDF_PAGES}-page cap — send a chapter")
    pages = [textlayer.extract(p) for p in doc]
    words = sum(len(t.split()) for t in pages)
    scanned = words < 25 * doc.page_count
    figures = []
    if scanned:
        for page in list(doc)[:SCANNED_PAGES]:
            figures.append(_render_page(page, "scanned page"))
        text = ""
    else:
        text = "\n\n".join(f"### p. {i}\n\n{t.strip()}" for i, t in enumerate(pages, 1) if t.strip())
        scored = []
        for page in doc:
            area = page.rect.width * page.rect.height or 1
            img = sum(abs(fitz.Rect(i["bbox"])) for i in page.get_image_info()) / area
            drawn = len(page.get_drawings())
            if img > 0.08 or drawn > 40:            # a picture, or a vector chart
                scored.append((img + min(drawn, 400) / 400, page.number))
        keep = sorted(n for _, n in sorted(scored, reverse=True)[:MAX_FIGURES])
        figures = [_render_page(doc[n], f"figure page {n + 1}") for n in keep]
    md = doc.metadata or {}
    first = next((ln.strip() for t in pages for ln in t.splitlines() if len(ln.strip()) > 8), "")
    title = (md.get("title") or "").strip() or first[:120] or Path(name).stem or "Untitled PDF"
    ex = Extract(kind="pdf", url=url, title=title, text=text, author=(md.get("author") or "").strip(),
                 site=(urlsplit(url).hostname or "") if url else "", pages=doc.page_count,
                 figures=figures, original=data)
    if scanned:
        ex.partial = f"a scanned PDF with no text layer: read from {min(doc.page_count, SCANNED_PAGES)} page images"
    return ex


def _render_page(page, caption: str) -> Figure:
    scale = min(1.6, FIG_MAX_SIDE / max(page.rect.width, page.rect.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
    return Figure(pix.tobytes("jpeg", jpg_quality=75), ".jpg", caption, f"p. {page.number + 1}")


def read_image(data: bytes, *, caption: str = "", name: str = "") -> Extract:
    fig = figure_from_bytes(data, caption=caption, origin=name, min_side=120, min_area=40_000)
    if not fig:
        raise Unreadable("the image is too small or not an image")
    title = _headline(caption) or Path(name).stem or "Image"
    return Extract(kind="image", title=title, text=(caption + "\n\n[figure 1]").strip(), figures=[fig])


def _headline(text: str, n: int = 70) -> str:
    line = next((ln.strip(" #*>") for ln in (text or "").splitlines() if ln.strip(" #*>")), "")
    return line if len(line) <= n else line[:n].rsplit(" ", 1)[0] + "…"


def read_url(url: str) -> Extract:
    """Dispatch by site, then by what the server says it sent."""
    p = urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    if host in ("x.com", "twitter.com") and X_POST.match(p.path):
        return read_x(url)
    if host.endswith(".substack.com") and "/p/" in p.path:
        return read_substack(url)
    final, ctype, data = _get_ok(url, accept="text/html,application/pdf;q=0.9,*/*;q=0.5")
    if ctype == "application/pdf" or data[:5] == b"%PDF-":
        return read_pdf(data, url=final)
    if ctype.startswith("image/"):
        return read_image(data, name=Path(urlsplit(final).path).name)
    if "html" not in ctype and "xml" not in ctype and ctype:
        raise Unreadable(f"{ctype} is not something this reads")
    html = data.decode("utf-8", "replace")
    fhost = (urlsplit(final).hostname or "").lower().removeprefix("www.")
    if fhost in ("threads.com", "threads.net"):
        return read_threads(final, html)
    if fhost.endswith("linkedin.com") and re.match(r"/(posts|feed/update)/", urlsplit(final).path):
        return read_linkedin_post(final, html)
    return read_page(final, html)


def read_file(path: Path, *, caption: str = "", name: str = "") -> Extract:
    data = path.read_bytes()
    name = name or path.name
    if data[:5] == b"%PDF-":
        return read_pdf(data, name=name)
    return read_image(data, caption=caption, name=name)


# ---------------------------------------------------------------- the note

URL_RE = re.compile(r"https?://[^\s<>\"']+")


def first_url(text: str) -> str:
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,;:!?)]}»\"'") if m else ""


def _learning_notes(vault_dir: Path):
    for p in sorted((vault_dir / "topics" / FOLDER).glob("*.md")):
        if p.name in okf.RESERVED:
            continue
        fm, _ = okf.parse_frontmatter(p.read_text())
        yield p, fm


def find_existing(vault_dir: Path, source: str) -> tuple[Path, dict] | None:
    for p, fm in _learning_notes(vault_dir):
        if str(fm.get("source") or "") == source:
            return p, fm
    return None


def _ensure_ignored(vault_dir: Path) -> None:
    """`assets/learning/` stays out of git — before the first file lands in it,
    or the next `catch_up` commits it as a hand edit."""
    gi = vault_dir / ".gitignore"
    lines = gi.read_text().splitlines() if gi.exists() else []
    if "/assets/learning/" not in lines:
        gi.write_text("\n".join(lines + ["/assets/learning/"]) + "\n")


def _reading_time(words: int) -> str:
    return f"{max(1, round(words / WORDS_PER_MIN))} min read" if words else ""


def _describe(ex: Extract, today: str) -> str:
    bits = [{"article": "Article", "post": f"{ex.site} post", "pdf": "PDF", "image": "Image"}[ex.kind]]
    bits.append(ex.site if ex.kind in ("article", "pdf") and ex.site else "")
    bits.append(ex.author)
    bits.append(f"{ex.pages} pages" if ex.pages else (f"{ex.words} words" if ex.words else ""))
    if ex.figures:
        bits.append(f"{len(ex.figures)} figure{'s' if len(ex.figures) > 1 else ''}")
    return ", ".join(b for b in bits if b) + f", fetched {today}. Learning brief pending."


def capture(target: str, *, origin: str = "cli", caption: str = "", name: str = "",
            today: str | None = None) -> dict:
    """Read a URL or a local file into a `learn: requested` source note.

    Returns {status: queued | duplicate | skipped, id, title, kind, reason, partial,
    figures, words}. A duplicate is the existing note, untouched — the same link sent
    twice is one note and one brief."""
    cfg = get_config()
    today = today or dt.date.today().isoformat()
    vault.scaffold(cfg.vault_dir)
    path = Path(target).expanduser()
    is_file = not URL_RE.match(target) and path.is_file()
    if is_file:
        source = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()[:16]}"
        url = ""
    else:
        url = canonical(target)
        source = f"web:{hashlib.sha256(url.encode()).hexdigest()[:16]}"
    hit = find_existing(cfg.vault_dir, source)
    if hit and str(hit[1].get("learn") or "") != "skipped":
        p, fm = hit
        return {"status": "duplicate", "id": fm.get("id"), "title": fm.get("title"),
                "kind": fm.get("kind"), "learn": fm.get("learn"), "reason": "", "path": str(p)}
    try:
        ex = read_file(path, caption=caption, name=name) if is_file else read_url(url)
        if ex.words < MIN_WORDS and not ex.figures:
            raise Unreadable("nothing readable on the page (it may need JavaScript or a login)")
        reason = ""
    except Unreadable as e:
        ex, reason = Extract(kind="article", title=(name or url or target)[:120], url=url), str(e)
    except Exception as e:  # noqa: BLE001 — a reader bug must still leave a note and a reply
        log.exception("reading %s failed", target)
        ex, reason = Extract(kind="article", title=(name or url or target)[:120], url=url), \
            f"the reader failed ({type(e).__name__})"
    if hit:                       # a skipped one, tried again: replace it
        hit[0].unlink()
    return _write(cfg, ex, source=source, origin=origin, reason=reason, today=today)


def _write(cfg, ex: Extract, *, source: str, origin: str, reason: str, today: str) -> dict:
    topic_dir = vault._topic_dir(cfg.vault_dir, FOLDER)
    note_id, path = vault._unique_id(cfg.vault_dir, topic_dir, vault._slugify(ex.title), today)
    asset_dir = cfg.vault_dir / ASSETS / note_id
    rel_assets = (ASSETS / note_id).as_posix()
    files = []
    if not reason and (ex.figures or ex.original):
        _ensure_ignored(cfg.vault_dir)
        asset_dir.mkdir(parents=True, exist_ok=True)
        for i, fig in enumerate(ex.figures, 1):
            (asset_dir / f"fig-{i:02d}{fig.ext}").write_bytes(fig.data)
            files.append(f"fig-{i:02d}{fig.ext}")
        if ex.original:
            (asset_dir / "original.pdf").write_bytes(ex.original)

    words = ex.words
    fields = dict(id=note_id, title=ex.title, description="", tags=["learning", ex.kind],
                  created=today, updated=today, source=source, kind=ex.kind,
                  url=ex.url, site=ex.site, author=ex.author, published=ex.published,
                  words=words, reading_time=_reading_time(words), pages=ex.pages or None,
                  figures=len(files), assets=rel_assets if files or ex.original else None,
                  partial=ex.partial or None, origin=origin,
                  learn="skipped" if reason else "requested",
                  skip_reason=reason or None, related=[])
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    fields["description"] = (f"Could not be read: {reason}." if reason else _describe(ex, today))
    body = []
    if ex.url:
        body.append(ex.url)
    if ex.partial:
        body.append(f"*Partial: {ex.partial}.*")
    if reason:
        body.append(f"Could not be read: {reason}.")
    else:
        body.append(ex.text or "(no text — the figures are the content)")
        if files:
            body.append("## Figures\n\n" + "\n".join(
                f"- [figure {i}] `{rel_assets}/{f}`"
                + (f" — {ex.figures[i - 1].caption}" if ex.figures[i - 1].caption else "")
                + (f" ({ex.figures[i - 1].origin})" if ex.figures[i - 1].origin else "")
                for i, f in enumerate(files, 1)))
        if ex.original:
            body.append(f"Original: `{rel_assets}/original.pdf`")
    sources = [{"id": "article", "resource": ex.url, "title": ex.title, "author": ex.author or None}] \
        if ex.url else None
    vault._emit(cfg.vault_dir, path, body="\n\n".join(body), generated_by="ozu/articles",
                fields=fields, sources=sources)
    con = db.connect(cfg.db_path)
    indexer.index_file(con, path)
    con.commit()
    state = "skipped" if reason else "queued"
    vault._git_commit(cfg.vault_dir, f"learn source: {note_id} ({state})")
    log.info("%s %s (%s, %d words, %d figures)%s", state, note_id, ex.kind, words, len(files),
             f": {reason}" if reason else "")
    return {"status": state, "id": note_id, "title": ex.title, "kind": ex.kind, "reason": reason,
            "partial": ex.partial, "figures": len(files), "words": words, "path": str(path)}


def probe(urls: list[str]) -> list[dict]:
    """Read each URL as `capture` would, write nothing: the step-0 measurement of
    whether this host can read what the owner sends (`ozu learn probe`)."""
    out = []
    for u in urls:
        row = {"url": u}
        try:
            ex = read_url(canonical(u))
            row.update(ok=ex.words >= MIN_WORDS or bool(ex.figures), kind=ex.kind, words=ex.words,
                       figures=len(ex.figures), pages=ex.pages, title=ex.title[:80],
                       partial=ex.partial)
        except Unreadable as e:
            row.update(ok=False, reason=str(e))
        out.append(row)
    return out
