"""Central configuration for the Ozu pipeline.

Everything is driven by environment variables (see .env.example).
Load order: real environment > .env file in OZU_HOME > defaults.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        # Strip a trailing ` # comment`. systemd's EnvironmentFile= does not do
        # this, so .env.example keeps comments on their own lines; this only
        # stops a hand-edited .env from silently poisoning a value.
        val = re.split(r"\s+#", val.strip(), maxsplit=1)[0]
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def topic_aliases() -> dict[str, str]:
    """`OZU_TOPIC_ALIASES`: comma-separated `old:new` pairs, lower-cased.

    A malformed entry (no colon, an empty side, or a name aliased to itself)
    is ignored rather than fatal: a typo in `.env` must not stop the tick, and
    the topic then simply keeps its own name. Aliases are one step, never
    chained: `a:b,b:c` reads `a` as `b`."""
    out: dict[str, str] = {}
    for pair in os.environ.get("OZU_TOPIC_ALIASES", "").split(","):
        old, sep, new = pair.partition(":")
        old, new = old.strip().lower(), new.strip().lower()
        if sep and old and new and old != new:
            out[old] = new
    return out


def canonical_topic(topic: str) -> str:
    """The one door every topic passes on its way to a folder, a repo or a
    query. A deployment that renamed a topic keeps reading the old name (ink
    tags, model answers, repo keys, MCP arguments) and it always lands on the
    new one, so the old folder is never reopened. Matching ignores case and
    surrounding space; an unknown topic comes back unchanged."""
    return topic_aliases().get(topic.strip().lower(), topic)


@dataclass
class Config:
    home: Path = field(default_factory=lambda: Path(os.environ.get("OZU_HOME", "~/ozu-data")).expanduser())

    def __post_init__(self) -> None:
        _load_dotenv(self.home / ".env")
        self.vault_dir = Path(os.environ.get("OZU_VAULT_DIR", str(self.home / "vault"))).expanduser()
        self.work_vault_dir = Path(os.environ.get("OZU_WORK_VAULT_DIR", str(self.home / "work-vault"))).expanduser()
        self.raw_dir = self.home / "raw"
        self.pages_dir = self.home / "pages"
        self.db_path = self.home / "ozu.db"
        self.log_dir = self.home / "logs"
        self.digest_dir = self.home / "digests"

        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        # Two vision jobs, two models (D-10, measured 2026-09-20). A notebook
        # page is the irreplaceable original capture and its note-splitting
        # shapes the vault for good, so it stays on the strongest model. A
        # returning "<brand> " document is a narrower job — find the ticks, find
        # the directive, transcribe the margin — and on the live corpus
        # Sonnet matched Opus on every one of those at 40 % of the price.
        self.model = os.environ.get("OZU_MODEL", "claude-opus-5")
        self.annotation_model = os.environ.get("OZU_ANNOTATION_MODEL",
                                               "claude-sonnet-5")
        self.max_pages_per_run = int(os.environ.get("OZU_MAX_PAGES_PER_RUN", "40"))

        # Reasoning effort per call kind — the two are not the same problem.
        # A notebook page is dense handwriting to be read and split; an
        # annotated document is mostly print the model must ignore, with a few
        # words of ink on it. Set from the sweep in docs/effort-sweep.md.
        self.vision_effort = os.environ.get("OZU_VISION_EFFORT", "high")
        self.annotation_effort = os.environ.get("OZU_ANNOTATION_EFFORT", "high")

        # Project tags a handwritten @tag may route to (`@garden` files under
        # topics/garden/). `ideas` and `people` always exist; every repo
        # mapped in OZU_CLAUDE_REPOS is a project tag too.
        self.projects = [canonical_topic(p.strip().lower()) for p in
                         os.environ.get("OZU_PROJECTS", "").split(",") if p.strip()]

        # rclone remote that maps to the Google Drive folder the Scribe exports to
        self.rclone_remote = os.environ.get("OZU_RCLONE_REMOTE", "gdrive:ScribeInbox")

        # Governance: notebook name prefixes that must route to the work vault
        self.work_notebook_prefixes = tuple(
            p.strip().lower()
            for p in os.environ.get("OZU_WORK_PREFIXES", "work").split(",")
            if p.strip()
        )

        # @claude work runs (agent.py): topic=git-url pairs. Only topics
        # mapped here ever trigger a run; everything else stays queued.
        self.claude_repos: dict[str, str] = {}
        for pair in os.environ.get("OZU_CLAUDE_REPOS", "").split(","):
            key, _, url = pair.partition("=")
            if key.strip() and url.strip():
                self.claude_repos[canonical_topic(key.strip().lower())] = url.strip()
        self.claude_bin = os.environ.get("OZU_CLAUDE_BIN", "claude")
        # Projects whose Build proposals are shaped as an ISA (ideal state,
        # decisions, numbered steps that each name their check) instead of
        # the usual requirement-and-plan shape; execution then reports each
        # step as pass, fail or not verified. Off unless named, so the two
        # shapes can be compared project by project.
        self.isa_topics = tuple(
            canonical_topic(t.strip().lower())
            for t in os.environ.get("OZU_ISA_TOPICS", "").split(",")
            if t.strip()
        )
        # Fine-grained PAT (PRs: read-write, scoped to the mapped repos).
        # Absent → work runs fall back to branch + compare link.
        self.github_token = os.environ.get("OZU_GITHUB_TOKEN", "")

        # Weekly digest → Kindle (send_kindle.py). All three of kindle_email,
        # smtp_user, smtp_pass must be set or the digest stays a local file.
        self.kindle_email = os.environ.get("OZU_KINDLE_EMAIL", "")
        self.smtp_host = os.environ.get("OZU_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = int(os.environ.get("OZU_SMTP_PORT", "587"))
        self.smtp_user = os.environ.get("OZU_SMTP_USER", "")
        self.smtp_pass = os.environ.get("OZU_SMTP_PASS", "")
        # The brand is configuration, in both directions. OZU_BRAND is the
        # one source of every *outgoing* name: document file names, the PDF
        # Author, cover kickers, heads and the page-header stamps the ink is
        # routed by (`<brand> Build <id> r2`).
        self.brand = os.environ.get("OZU_BRAND", "").strip() or "Ozu"
        # OZU_DIGEST_PREFIXES is the one source of every *incoming* match: a
        # notebook whose name starts with one of them is system output coming
        # back (an annotation, never new notes), and the Build header and
        # file-name patterns are built from the same list, case-insensitively.
        # A deployment whose device already holds documents under an older
        # name lists both, so ink on either keeps routing home.
        self.digest_prefixes = tuple(
            p.strip().lower()
            for p in os.environ.get("OZU_DIGEST_PREFIXES", "ozu ").split(",")
            if p.strip()
        )
        self.max_claude_runs = int(os.environ.get("OZU_MAX_CLAUDE_RUNS", "2"))
        self.max_research_runs = int(os.environ.get("OZU_MAX_RESEARCH_RUNS", "2"))
        # Builds (builds.py): headless runs per tick across propose/revise/
        # execute, and a daily ceiling on vision calls — the tick fires every
        # five minutes, so a runaway Drive folder must hit a wall the 2-hourly
        # cycle never needed.
        self.max_build_runs = int(os.environ.get("OZU_MAX_BUILD_RUNS", "2"))
        # Per *Build*, for its whole life — not per tick. OZU_MAX_BUILD_RUNS
        # bounds one tick, and the tick fires every five minutes, so on
        # 2026-09-20 one Build was relaunched 116 times overnight without
        # that cap ever being exceeded (docs/build-attempts.md). An attempt
        # is a launch that consumed a usage window; a deferral is a launch
        # turned away by a window already spent, which costs nothing but
        # must still be bounded or the timer hammers forever.
        self.max_build_attempts = int(
            os.environ.get("OZU_MAX_BUILD_ATTEMPTS", "3"))
        self.max_build_deferrals = int(
            os.environ.get("OZU_MAX_BUILD_DEFERRALS", "8"))
        self.max_pages_per_day = int(os.environ.get("OZU_MAX_PAGES_PER_DAY", "120"))
        # Learning briefs (learn.py): one headless run per pinned video's
        # transcript, capped per cycle like the other run kinds.
        self.max_learn_runs = int(os.environ.get("OZU_MAX_LEARN_RUNS", "3"))
        # The wiki (wiki.py): one headless run per cycle folds at most this
        # many new notes into at most this many concept pages. Small batches
        # keep each page rewrite reviewable in one diff; a backlog drains a
        # batch every two hours.
        self.max_wiki_notes = int(os.environ.get("OZU_MAX_WIKI_NOTES", "6"))
        self.max_wiki_pages = int(os.environ.get("OZU_MAX_WIKI_PAGES", "5"))
        self.claude_timeout = int(os.environ.get("OZU_CLAUDE_TIMEOUT", "1800"))
        # Headless run kinds → the CLI model each asks for. These bill the
        # box's claude.ai login, not the API key (invariant 7), so the choice
        # buys usage headroom and speed rather than money. Empty means "the
        # CLI's own default", which is what every one of them used before
        # 2026-09-20. Only `learn` is rightsized by default: summarising a
        # transcript it has already been handed, with no web and a
        # vault-scoped Read, is the one that asks least.
        self.run_models = {
            kind: os.environ.get(f"OZU_{kind.upper()}_MODEL", default)
            for kind, default in (("propose", ""), ("research", ""),
                                  ("learn", "claude-sonnet-5"), ("wiki", ""))
        }
        # Model tags the owner can write beside @approved, → the CLI ids they
        # mean. The default (no tag) is Opus. Per-model timeouts because a
        # Fable run on a real task can legitimately be long; opus/sonnet
        # inherit OZU_CLAUDE_TIMEOUT unless set explicitly.
        self.build_models = {
            "opus": "claude-opus-5",
            "sonnet": "claude-sonnet-5",
            "fable": "claude-fable-5-1",
        }
        self.build_default_model = "opus"
        self.claude_timeouts = {
            "opus": int(os.environ.get("OZU_CLAUDE_TIMEOUT_OPUS",
                                       str(self.claude_timeout))),
            "sonnet": int(os.environ.get("OZU_CLAUDE_TIMEOUT_SONNET",
                                         str(self.claude_timeout))),
            "fable": int(os.environ.get("OZU_CLAUDE_TIMEOUT_FABLE",
                                        str(2 * self.claude_timeout))),
        }

        # Ink detection (ink.py, D-09): where to find the copy we sent of a
        # returning "<brand> " document, and the knobs. The directories are every
        # place this pipeline writes a document; OZU_SENT_DIRS adds folders
        # of other tools that send "<brand> " documents to the same device.
        self.ink_detect = os.environ.get("OZU_INK_DETECT", "1") != "0"
        self.ink_threshold = int(os.environ.get("OZU_INK_THRESHOLD", "100"))

        self.agent_dir = self.home / "agent"
        self.builds_dir = self.home / "builds"
        self.research_dir = self.home / "research"
        self.mail_dir = self.home / "mail"
        self.learn_dir = self.home / "learn"
        self.editions_dir = self.home / "editions"
        extra = [Path(p).expanduser() for p in
                 os.environ.get("OZU_SENT_DIRS", "").split(",") if p.strip()]
        self.sent_dirs = extra or [
            self.builds_dir, self.editions_dir, self.research_dir,
            self.mail_dir, self.learn_dir, self.digest_dir,
        ]

        # MCP over HTTP. The token is the only thing between the graph and
        # whoever can reach the port, so `ozu mcp --http` refuses to start
        # without it (mcp_server.main) rather than serving the vault openly.
        # public_url is what the server calls itself in its OAuth metadata —
        # it must be the URL the client dialled, or discovery points nowhere.
        self.mcp_host = os.environ.get("OZU_MCP_HOST", "127.0.0.1")
        self.mcp_port = int(os.environ.get("OZU_MCP_PORT", "8321"))
        self.mcp_token = os.environ.get("OZU_MCP_TOKEN", "")
        self.mcp_public_url = os.environ.get(
            "OZU_MCP_PUBLIC_URL", f"http://{self.mcp_host}:{self.mcp_port}")

    def ensure_dirs(self) -> None:
        for d in (self.home, self.raw_dir, self.pages_dir, self.log_dir,
                  self.digest_dir):
            d.mkdir(parents=True, exist_ok=True)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config


def brand() -> str:
    """The name every outgoing document carries (`OZU_BRAND`, default
    `Ozu`). Read through the config singleton at call time, so a test that
    sets the env and resets `_config` sees its own brand."""
    return get_config().brand


def incoming_prefix_re() -> str:
    """A regex alternation of `OZU_DIGEST_PREFIXES`, each escaped, for
    patterns that must recognise a returning document under any name this
    deployment has used. Compile it with `re.IGNORECASE`: the device keeps
    whatever case the name was sent in.

    The prefix must start a word — no letter or digit right before it, so
    "Kozu" is not "ozu" — and it carries no trailing whitespace: each caller
    spells the gap to its next word as spaces or tabs only, never any
    whitespace, which would let a match run across a line break of a page's
    text layer."""
    alts = [re.escape(p) for p in get_config().digest_prefixes] or [re.escape(brand())]
    return "(?<![A-Za-z0-9])(?:" + "|".join(alts) + ")"
