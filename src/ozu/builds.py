"""Builds: the paper conversation loop — propose, revise on ink, execute on
approval.

A handwritten @claude no longer builds anything unattended. It opens a
**Build**: a proposal (requirement, plan, risks, open questions) typeset onto
a "<brand> Build <id>" PDF the Scribe receives within a tick. The owner annotates it
in ink — @input folds their answers into a new revision, @approved (optionally
@opus/@sonnet/@fable) executes it on branch `build/<id>` and opens the PR,
@drop closes it. Every state of that conversation lives in one vault note,
`topics/<project>/<build-id>.md`, whose frontmatter (`build:`, `build_rev`,
`build_model`, `build_branch`, `build_pr`, `build_pending`, `build_attempts`,
`build_deferrals`) is the ledger — the `builds` table in ozu.db is derived
from it and rebuilt by `ozu index`, never the other way around (invariants 1
and 7).

A Build also carries its own lifetime attempt budget, because the per-tick
cap never was one: see "the attempt budget" below and docs/build-attempts.md.

Matching a returning page to its Build is deterministic first, model second:
the file name (`<brand> Build r2 - <title> (B-260906-7f3a)`), then the printed text layer
(the same header is stamped into every page), and only then the annotation
reader's `build_ref`. Keys that disagree mean `needs_review`, never a guess —
several Builds are in flight at once and ink must never attach to the wrong
one. Walled pages are inert here as everywhere: they start, revise and
approve nothing.

Execution reuses agent.py wholesale: the same scoped allowlists, the same
`Bash(git push:*)` denial, the same API-key strip (runs bill the box's
claude.ai login), `_open_pr`. One execution per approval, ever — a second
@approved on an executed Build becomes a line in the next edition's mail,
not a second run.
"""
from __future__ import annotations

import datetime as dt
import functools
import logging
import os
import re
import shutil
from pathlib import Path

from .config import brand, canonical_topic, get_config, incoming_prefix_re
from . import agent, db, digest_pdf, edition, indexer, okf, send_kindle, vault

log = logging.getLogger("ozu.builds")

PROPOSE_PROMPT_PATH = (Path(__file__).resolve().parents[2] / "prompts"
                       / "build_propose_prompt.md")
ISA_PROMPT_PATH = PROPOSE_PROMPT_PATH.with_name("build_isa_prompt.md")
WORK_PROMPT_PATH = PROPOSE_PROMPT_PATH.with_name("build_work_prompt.md")


# Appended to the work prompt for a Build proposed as an ISA (see
# config.isa_topics), so execution grades the report against the
# Steps it was handed rather than free-form.
ISA_GRADING_RULE = (
    "\nThis Build was proposed as an ISA: the Steps section is your test "
    "suite. Report each step by number as pass, fail or not verified.\n")

# The Build id: short enough to handwrite, unambiguous enough to grep.
ID_RE = re.compile(r"\bB-\d{6}-[0-9a-f]{4}\b")
# The text-layer key matches only the printed page header ("<brand> Build
# B-… r2"), never a bare mention — mail lines may *talk about* a build, and a
# page of mail must not text-match into one. The stamp is drawn on its own
# (layout.Document.render inserts it in the top margin), so the text layer
# always gives it a line to itself: "<prefix> Build <id> r<n>" or
# "… result", and nothing else. The pattern requires exactly that whole
# line, with spaces or tabs between the words and never a line break, so a
# sentence that names a Build — "the ozu build B-… finished", or a mail line
# wrapped right before "Build" — routes nothing. Built from OZU_DIGEST_PREFIXES,
# case-insensitively: the header was stamped under whatever name the
# deployment used when it was sent, and a case-sensitive pattern would
# silently stop routing ink on every page printed under another spelling.
_ID_PART = r"(B-\d{6}-[0-9a-f]{4})"


def header_re() -> re.Pattern:
    """The page-header pattern, from the prefixes configured right now."""
    return _header_re(incoming_prefix_re())


@functools.lru_cache(maxsize=8)
def _header_re(prefix: str) -> re.Pattern:
    return re.compile(r"^[ \t]*" + prefix + r"[ \t]+Build[ \t]+" + _ID_PART
                      + r"[ \t]+(?:r\d+|result)[ \t]*$",
                      re.IGNORECASE | re.MULTILINE)


def canonical_id(bid: str) -> str:
    """A Build id as it is printed: `B-` upper, hex lower. A header matched
    case-insensitively may come back as `b-260906-7F3A`."""
    return "B-" + bid[2:].lower()
# Handwritten directive tags, stripped from the transcription to leave the
# substance of an @input.
TAG_RE = re.compile(r"@(?:approved|input|drop|opus|sonnet|fable|claude|research)\b",
                    re.I)

STATUSES = ("proposed", "revised", "approved", "executing", "done", "error",
            "dropped", "exhausted", "split")
# Past these, ink can no longer change the Build (done/executing also refuse
# a re-run: one execution per approval, ever).
_EXECUTED = ("done", "executing")

_DESCRIPTION = {
    "proposed": "Build proposal r{rev} — decide on paper",
    "revised": "Build proposal r{rev} — revised, decide on paper",
    "approved": "Approved ({model}) — executes on the next tick",
    "executing": "Executing ({model})",
    "done": "Built with {model} — review the PR",
    "error": "Failed — write @approved on the Build page to retry",
    "dropped": "Dropped — nothing was executed",
    "exhausted": "Stopped after {attempts} attempts — @input to revise it, "
                 "or `ozu builds reset {id}`",
    "split": "Split into successor Builds — see the decision history",
}

# ---------------------------------------------------------------------------
# the attempt budget
#
# A Build carries its own lifetime budget in its frontmatter, because the
# per-tick cap is not a budget at all: OZU_MAX_BUILD_RUNS bounds one tick and
# the tick fires every five minutes, so on the night of 2026-09-20 build
# B-260916-2824 was relaunched 116 times under a cap of 2 that was never once
# exceeded. Three of those launches each ran for hours and swallowed the whole
# claude.ai usage window; the other 113 bounced off the spent window in two
# seconds. Nothing was ever built, and because the counter did not exist and
# no launch was recorded, a run killed mid-flight left no trace and looked
# relaunchable forever.
#
# Two counters, because the two failures cost different things:
#   build_attempts   — launches that consumed a window. Capped hard; past the
#                      cap the Build goes `exhausted`, a terminal status no
#                      phase of the tick selects.
#   build_deferrals  — launches turned away by a window that was already
#                      spent. Free, so not an attempt — but still capped, or
#                      the timer hammers until the window resets.
#
# Both counters are written *before* the subprocess starts and committed with
# the note, so a run killed by OOM, a reboot or a session limit has already
# paid for itself. That is what makes the cap survive a crash — the old loop
# left no trace at all.
#
# Above the per-Build budget sits one gate for the whole phase, RETRY_AFTER
# in `meta`: the first spent window stands the builds phase down until the
# CLI's own reset time. That is what stops the five-minute timer, and it
# covers proposals and revisions too, which draw on the same window and had
# the same unbounded retry.

# The meta key holding the moment the builds phase may run again.
RETRY_AFTER = "builds_retry_after"
# Used when the limit message names no reset time. The window is five hours;
# an hour is short enough to pick the reset up soon after it happens, and long
# enough that a tick every five minutes is no longer a hammer.
BLIND_BACKOFF = dt.timedelta(hours=1)


def _attempts(fm: dict) -> int:
    return int(fm.get("build_attempts") or 0)


def _deferrals(fm: dict) -> int:
    return int(fm.get("build_deferrals") or 0)


def budget_refusal(fm: dict) -> str | None:
    """Why this Build may never be launched again, or None while it may."""
    cfg = get_config()
    if _attempts(fm) >= cfg.max_build_attempts:
        return (f"it has used all {cfg.max_build_attempts} of its execution "
                f"attempts")
    if _deferrals(fm) >= cfg.max_build_deferrals:
        return (f"a spent usage window turned it away "
                f"{cfg.max_build_deferrals} times")
    return None


def stood_down_until(con, now: dt.datetime | None = None) -> dt.datetime | None:
    """The reset the whole builds phase is waiting for, or None to go ahead."""
    raw = str(db.get_meta(con, RETRY_AFTER) or "").strip()
    if not raw:
        return None
    try:
        when = dt.datetime.fromisoformat(raw)
    except ValueError:
        log.warning("unreadable %s %r; ignoring it", RETRY_AFTER, raw)
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return when if when > (now or dt.datetime.now(dt.timezone.utc)) else None


def window_reset(exc: agent.UsageLimitError) -> dt.datetime:
    """When the spent window comes back. The CLI names it in its refusal
    ("resets 6:30am (UTC)"); when it names none, back off blind rather than
    retry in five minutes."""
    return exc.resets_at or (dt.datetime.now(dt.timezone.utc) + BLIND_BACKOFF)


def stand_down(con, exc: agent.UsageLimitError) -> dt.datetime:
    """Stand the whole builds phase down until the window is back."""
    when = window_reset(exc)
    db.set_meta(con, RETRY_AFTER, when.isoformat())
    log.error("the claude.ai usage window is spent (%s); the builds phase "
              "stands down until %s", exc, when.isoformat())
    return when


def _exhaust(con, build: dict, why: str) -> None:
    """Terminal: the Build stops being work. `exhausted` is in no phase's
    query, so the tick cannot pick it up again however often it fires."""
    fm = build["fm"]
    bid, title = str(fm["id"]), str(fm.get("title") or fm["id"])
    fm["build"] = "exhausted"
    fm.pop("build_pending", None)
    _append_history(build, f"### stopped — {dt.date.today().isoformat()} · "
                           f"{why} ({_attempts(fm)} attempts, "
                           f"{_deferrals(fm)} deferrals)")
    _save_build(build, f"build {bid}: exhausted ({why})")
    db.add_build_event(con, bid,
                       f"{bid} ({title}): stopped — {why}. Nothing will "
                       f"relaunch it. Write @input to revise the plan, or "
                       f"run `ozu builds reset {bid}` to hand it the same "
                       f"plan again.")
    log.warning("build %s exhausted: %s", bid, why)


def reset_budget(build_id: str, *, approve: bool = True) -> str:
    """Clear one Build's lifetime budget by hand — the escape hatch for a
    Build that stopped and is worth one more go. `approve` puts it back in
    the queue; without it the Build keeps whatever status it had."""
    cfg = get_config()
    path = find_build_path(build_id)
    if path is None:
        raise SystemExit(f"no Build {build_id!r} in the vault")
    build = load_build(path)
    fm = build["fm"]
    before = (_attempts(fm), _deferrals(fm))
    for key in ("build_attempts", "build_deferrals"):
        fm.pop(key, None)
    if approve:
        fm["build"] = "approved"
        fm["build_model"] = fm.get("build_model") or cfg.build_default_model
        _append_history(build, f"### budget reset — {dt.date.today().isoformat()} "
                               f"· was {before[0]} attempts, {before[1]} "
                               f"deferrals; re-approved by hand")
    else:
        _append_history(build, f"### budget reset — {dt.date.today().isoformat()} "
                               f"· was {before[0]} attempts, {before[1]} deferrals")
    _save_build(build, f"build {build_id}: budget reset")
    log.info("build %s: budget reset (was %s attempts, %s deferrals)",
             build_id, *before)
    return str(fm["build"])


# ---------------------------------------------------------------------------
# ids and note IO

def new_build_id() -> str:
    """B-<yymmdd>-<4 hex>, collision-checked against the vault (the note's
    filename is its id, so the file *is* the reservation)."""
    day = dt.date.today().strftime("%y%m%d")
    while True:
        bid = f"B-{day}-{os.urandom(2).hex()}"
        if find_build_path(bid) is None:
            return bid


def find_build_path(build_id: str) -> Path | None:
    cfg = get_config()
    hits = list(cfg.vault_dir.glob(f"topics/*/{build_id}.md"))
    return hits[0] if hits else None


def load_build(path: Path) -> dict:
    fm, body = okf.parse_frontmatter(path.read_text())
    return {"path": path, "topic": canonical_topic(path.parent.name), "fm": fm, "body": body}


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in (text or "").strip().splitlines())


_SUMMARY_LINE = re.compile(r"^> Summary: .*$", re.M)


def _save_build(build: dict, msg: str) -> None:
    """Rewrite the Build note from its parsed parts and re-derive its db row.
    Frontmatter goes through okf.dump_frontmatter (never hand-rolled), the
    body's `> Summary:` line follows the description, and the vault commits —
    the file is the ledger, so every state change is a commit."""
    cfg = get_config()
    fm = build["fm"]
    fm["updated"] = dt.date.today().isoformat()
    fm["description"] = _DESCRIPTION.get(str(fm.get("build")), "Build").format(
        rev=fm.get("build_rev", 1), model=fm.get("build_model") or "opus",
        attempts=_attempts(fm), id=fm.get("id"))
    body = _SUMMARY_LINE.sub(f"> Summary: {fm['description']}", build["body"],
                             count=1)
    build["body"] = body
    build["path"].write_text(okf.dump_frontmatter(fm) + "\n"
                             + body.lstrip("\n").rstrip() + "\n")
    okf.regenerate(cfg.vault_dir)
    derive_row(db.connect(cfg.db_path), build["path"])
    vault._git_commit(cfg.vault_dir, msg)


def derive_row(con, path: Path, *, commit: bool = True) -> None:
    """The builds table row, re-read from the file it derives from."""
    b = load_build(path)
    fm = b["fm"]
    source = str(fm.get("source") or "")
    db.upsert_build(
        con, build_id=str(fm.get("id")), path=str(path), topic=b["topic"],
        title=str(fm.get("title") or fm.get("id")),
        status=str(fm.get("build") or ""), rev=int(fm.get("build_rev") or 1),
        model=fm.get("build_model"), branch=fm.get("build_branch"),
        pr=fm.get("build_pr"), pending=str(fm.get("build_pending") or ""),
        source_note=source.partition(":")[2] or None,
        updated=str(fm.get("updated") or ""),
        attempts=_attempts(fm), deferrals=_deferrals(fm), commit=commit)


# ---------------------------------------------------------------------------
# body sections: Proposal / Result / Decision history / Original ink

def _section(body: str, title_prefix: str) -> str:
    """Text of the first `## <title…>` section whose title starts with the
    prefix, heading excluded."""
    m = re.search(rf"^## {re.escape(title_prefix)}.*$", body, re.M)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip()


def _replace_section(body: str, title_prefix: str, new_title: str,
                     new_text: str) -> str:
    """Swap one `##` section (title and text) for a new one."""
    m = re.search(rf"^## {re.escape(title_prefix)}.*$", body, re.M)
    if not m:
        return body.rstrip() + f"\n\n## {new_title}\n\n{new_text}\n"
    rest = body[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    tail = rest[nxt.start():] if nxt else ""
    return body[:m.start()] + f"## {new_title}\n\n{new_text}\n\n" + tail


def _append_history(build: dict, entry: str) -> None:
    """One `### …` entry at the end of the Decision history section."""
    history = _section(build["body"], "Decision history")
    build["body"] = _replace_section(
        build["body"], "Decision history", "Decision history",
        (history + "\n\n" + entry).strip())


def proposal_of(build: dict) -> str:
    return _section(build["body"], "Proposal")


def history_of(build: dict) -> str:
    return _section(build["body"], "Decision history")


def ink_of(build: dict) -> str:
    return _section(build["body"], "Original ink")


# ---------------------------------------------------------------------------
# matching: deterministic first, model second

def match_build(notebook: str, printed_text: str,
                build_ref: str | None) -> tuple[str | None, str]:
    """(build_id, which-keys-agreed) or (None, why-not).

    Keys, strongest first: the document/file name, the printed page header in
    the text layer, the annotation reader's build_ref. Every non-null key must
    name the same Build; any disagreement is (None, ...) — with several Builds
    in flight, a page matches with certainty or not at all."""
    keys: dict[str, set[str]] = {}
    m = ID_RE.search(notebook or "")
    if m:
        keys["filename"] = {m.group(0)}
    text_ids = {canonical_id(i) for i in header_re().findall(printed_text or "")}
    if text_ids:
        keys["textlayer"] = text_ids
    ref = str(build_ref or "").strip()
    if ref:
        if ID_RE.fullmatch(ref):
            keys["build_ref"] = {ref}
        else:
            return None, f"build_ref {ref!r} is not a Build id"
    if not keys:
        return None, "no key"
    agreed = set.union(*keys.values())
    if len(agreed) > 1:
        detail = "; ".join(f"{k}={'/'.join(sorted(v))}"
                           for k, v in sorted(keys.items()))
        return None, f"keys disagree: {detail}"
    return agreed.pop(), "+".join(sorted(keys))


def _ink_text(result: dict) -> str:
    """The substance of the handwriting: transcription minus the directive
    tags and any quoted Build id."""
    text = TAG_RE.sub("", result.get("transcription") or "")
    text = ID_RE.sub("", text)
    return text.strip(" \t\n-—:·")


def route_annotation(con, page, result: dict) -> tuple[str | None, str | None]:
    """One annotated page, routed. Returns (build_id, disposition):
    (None, None) when the page has nothing to do with a Build; (id, 'margin')
    when matched but the ink is ordinary marginalia (notes will link to the
    Build); (id, <kind>) when a directive was applied; (None, 'unmatched')
    when a build page could not be matched with certainty — the ink is then
    preserved as a needs_review note and reported in the next Ozu Mail.

    Callers guarantee the page is not walled (governance: work pages never
    start, revise or approve a Build)."""
    d = result.get("build_directive") or {}
    kind = d.get("kind") or "none"
    model = d.get("model") or None
    build_id, how = match_build(page["notebook"], page["ocr_text"] or "",
                                result.get("build_ref"))

    if build_id is None:
        if how == "no key" and kind in ("none", "propose"):
            return None, None          # an ordinary Ozu document, ordinary ink
        # a build page (or build-directed ink) that cannot be pinned to one
        # Build: preserve the ink, flag it, and say so in the mail
        log.warning("page %s of %r unmatched (%s, directive=%s)",
                    page["page_no"], page["notebook"], how, kind)
        db.add_build_event(con, None,
                           f"A page of “{page['notebook']}” could not be "
                           f"matched to a Build ({how}); its ink was filed as a "
                           f"note flagged needs_review and nothing was changed.")
        if not result.get("notes") and _ink_text(result):
            result["notes"] = [{
                "title": f"Unmatched build ink from {page['notebook']}",
                # NOT tagged 'build': that tag keeps Build notes out of Ozu
                # Mail, and this note exists precisely to reach the mail
                "slug": "unmatched-build-ink", "topic": "ideas",
                "tags": ["build-ink"],
                "summary": "Ink from a Build page the pipeline could not match.",
                "body": _ink_text(result), "related": [], "related_reason": "",
                "actions": [], "claude_request": False, "research_request": False,
            }]
        return None, "unmatched"

    path = find_build_path(build_id)
    if path is None:
        log.warning("page names Build %s but no such note exists", build_id)
        db.add_build_event(con, build_id,
                           f"A page named Build {build_id}, but no such Build "
                           f"exists in the vault (a simulate document, or a "
                           f"deleted note?). Filed as needs_review.")
        return None, "unmatched"

    log.info("page %s of %r matched Build %s by %s (directive=%s)",
             page["page_no"], page["notebook"], build_id, how, kind)
    if kind in ("none", "propose"):
        # margin ink (or a @claude asking for a fresh Build of a margin idea):
        # normal notes, linked to this Build via related
        return build_id, "margin"
    apply_directive(con, load_build(path), kind, model, result)
    return build_id, kind


# ---------------------------------------------------------------------------
# the state machine

def apply_directive(con, build: dict, kind: str, model: str | None,
                    result: dict) -> None:
    """One handwritten directive against one Build. Terminal states answer
    with a Ozu Mail line instead of changing anything — one execution per
    approval, ever, and a dropped Build stays dropped."""
    cfg = get_config()
    fm = build["fm"]
    bid, title = str(fm.get("id")), str(fm.get("title") or "")
    status = str(fm.get("build") or "")
    today = dt.date.today().isoformat()

    if status == "split":
        # The parent's document is still on the device and will be annotated
        # for months. Its work now lives in the successors, so ink here is
        # answered rather than applied — attaching it to a Build that will
        # never run is the same failure as attaching it to the wrong one.
        kids = ", ".join(str(k) for k in (fm.get("build_split_into") or []))
        db.add_build_event(con, bid,
                           f"{bid} ({title}): this Build was split; its work "
                           f"is in {kids or 'its successors'}. Annotate those "
                           f"documents instead — ink on this one changes "
                           f"nothing.")
        return

    if kind == "input":
        if status in _EXECUTED:
            db.add_build_event(con, bid,
                               f"{bid} ({title}): @input arrived after execution — "
                               f"nothing changed; write the follow-up as a fresh "
                               f"@claude page.")
            return
        if status == "dropped":
            db.add_build_event(con, bid,
                               f"{bid} ({title}): @input on a dropped Build — it "
                               f"stays closed; a fresh @claude page restarts the idea.")
            return
        text = _ink_text(result)
        if not text:
            db.add_build_event(con, bid,
                               f"{bid} ({title}): @input had no readable text — "
                               f"nothing was folded in.")
            return
        _append_history(build, f"### input — {today}\n\n{_quote(text)}")
        fm["build_pending"] = "revise"
        _save_build(build, f"build {bid}: input received")
        log.info("build %s: input recorded, revision queued", bid)

    elif kind == "approve":
        if status in _EXECUTED:
            db.add_build_event(con, bid,
                               f"{bid} ({title}): already executed — a second "
                               f"@approved never re-runs a Build. "
                               f"PR: {fm.get('build_pr') or 'none'}.")
            return
        if status == "dropped":
            db.add_build_event(con, bid,
                               f"{bid} ({title}): @approved on a dropped Build — it "
                               f"stays closed; a fresh @claude page restarts the idea.")
            return
        refusal = budget_refusal(fm)
        if refusal:
            # The budget is the whole point: an approval that re-armed a
            # Build past its cap would hand the five-minute timer back the
            # loop the cap exists to stop.
            db.add_build_event(con, bid,
                               f"{bid} ({title}): @approved changes nothing — "
                               f"{refusal}. Write @input to revise the plan "
                               f"(a revision earns a fresh budget), or run "
                               f"`ozu builds reset {bid}`.")
            return
        text = _ink_text(result)
        if text:
            _append_history(build,
                            f"### input — {today} (applied before approval)\n\n"
                            f"{_quote(text)}")
        fm["build_model"] = model or fm.get("build_model") or cfg.build_default_model
        fm["build"] = "approved"
        fm.pop("build_pending", None)
        _append_history(build, f"### approved — {today} · {fm['build_model']}")
        _save_build(build, f"build {bid}: approved ({fm['build_model']})")
        log.info("build %s: approved, model=%s", bid, fm["build_model"])

    elif kind == "drop":
        if status in _EXECUTED:
            db.add_build_event(con, bid,
                               f"{bid} ({title}): already executed; @drop changes "
                               f"nothing — close the PR on GitHub if unwanted.")
            return
        if status == "dropped":
            return                     # a re-read of the same page; stay quiet
        fm["build"] = "dropped"
        fm.pop("build_pending", None)
        _append_history(build, f"### dropped — {today}")
        _save_build(build, f"build {bid}: dropped")
        db.add_build_event(con, bid,
                           f"{bid} ({title}): dropped — nothing was executed.")
        log.info("build %s: dropped", bid)


# ---------------------------------------------------------------------------
# the three run phases: propose, revise, execute

def propose_tools(workdir: Path, vault_dir: Path) -> str:
    """A propose run reads the clone and the vault and searches the web —
    and that is all. No Edit, no Bash: the run only ever produces text, so
    a prompt hidden in a web page can at worst shape a proposal the owner reads
    with their own eyes before anything runs."""
    scopes = [workdir] if Path(workdir) == Path(vault_dir) else [workdir, vault_dir]
    return ",".join([*(rule for s in scopes
                       for rule in agent.scoped(("Read",), s)),
                     "WebSearch", "WebFetch"])


def _clone_for_reading(repo: str, build_id: str, tag: str) -> Path:
    cfg = get_config()
    workdir = cfg.agent_dir / f"{tag}-{build_id}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.parent.mkdir(parents=True, exist_ok=True)
    agent._git(["clone", "--quiet", "--depth", "1", repo, str(workdir)], cwd=None)
    return workdir


def _propose_run(build_id: str, topic: str, title: str, ink: str,
                 revision_context: str = "") -> str:
    """One read-only proposal run; returns the proposal markdown."""
    cfg = get_config()
    repo = cfg.claude_repos.get(canonical_topic(topic))
    repo_line = (f"Mapped repository: {repo} (a fresh read-only clone is your "
                 f"working directory)" if repo else
                 f"No repository is mapped for '{topic}' — propose from the "
                 f"idea and the vault alone, and ask which repo this belongs "
                 f"to in Open questions.")
    workdir, cleanup = cfg.vault_dir, None
    if repo:
        try:
            workdir = cleanup = _clone_for_reading(repo, build_id, "propose")
        except Exception as e:
            # a proposal can still be made from the ink and the vault; the
            # plan just says the repo was unreadable
            log.warning("clone for proposal %s failed (%s); proposing "
                        "from the vault alone", build_id, e)
            repo_line = (f"Mapped repository: {repo} — but the clone failed, "
                         f"so propose from the idea and the vault alone and "
                         f"flag the unread repo under Risks.")
            workdir = cfg.vault_dir
    prompt_path = (ISA_PROMPT_PATH if canonical_topic(topic) in cfg.isa_topics
                   else PROPOSE_PROMPT_PATH)
    prompt = prompt_path.read_text().format(
        build_id=build_id, topic=topic, title=title, repo_line=repo_line,
        ink=ink, revision_context=revision_context,
        date=dt.date.today().isoformat())
    try:
        return agent._run_claude(
            prompt, workdir,
            allowed_tools=propose_tools(workdir, cfg.vault_dir),
            model=cfg.run_models.get("propose") or None,
            timeout=cfg.claude_timeouts.get(cfg.build_default_model))
    finally:
        if cleanup is not None:
            shutil.rmtree(cleanup, ignore_errors=True)


_UNSAFE_IN_NAME = re.compile(r'[\\/:*?"<>|]+')
TITLE_IN_NAME = 40


def pdf_name(bid: str, rev: int, title: str) -> str:
    """`<brand> Build r2 - <title> (B-…)`: the library grid truncates from the
    right, so what survives is the revision and the idea, not an id nobody
    remembers. The id stays in the name because `match_build`'s strongest
    key reads it from there (ID_RE), and the whole thing stays filename-safe
    on every OS the file crosses."""
    clean = " ".join(_UNSAFE_IN_NAME.sub(" ", title).split())
    if len(clean) > TITLE_IN_NAME:
        clean = clean[:TITLE_IN_NAME].rsplit(" ", 1)[0].rstrip(" -,;:") or clean[:TITLE_IN_NAME]
    return f"{brand()} Build r{rev} - {clean or bid} ({bid})"


def _send_proposal_pdf(build: dict) -> None:
    """Typeset and send one revision. Best-effort like research delivery —
    the note is already the ledger; a failed send is retried by hand, never
    by re-proposing. A delivered revision is recorded so the next edition
    can name the one it superseded as safe to delete."""
    cfg = get_config()
    fm = build["fm"]
    bid, rev = str(fm["id"]), int(fm.get("build_rev") or 1)
    title = str(fm.get("title") or bid)
    cfg.builds_dir.mkdir(parents=True, exist_ok=True)
    pdf = cfg.builds_dir / f"{pdf_name(bid, rev, title)}.pdf"
    try:
        digest_pdf.build_propose_pdf({
            "id": bid, "title": title,
            "topic": build["topic"], "rev": rev,
            "proposal": proposal_of(build), "history": history_of(build),
            "date": dt.date.today().isoformat(),
        }, pdf)
        if send_kindle.send_pdf(pdf):
            edition.record(db.connect(cfg.db_path), pdf.stem, "build",
                           build_id=bid, rev=rev)
    except Exception:
        log.warning("proposal PDF/send failed for %s; the Build note is in "
                    "the vault", bid, exc_info=True)


def _propose_note(con, note) -> str:
    """A build-requested note becomes a proposed Build: one read-only run,
    one Build note, one PDF to the Scribe. Returns the new Build id; the
    caller flips the source note's ledger line to `proposed-build <id>` so
    nothing fires twice."""
    cfg = get_config()
    topic, today = note["topic"], dt.date.today().isoformat()
    build_id = new_build_id()
    ink = agent._note_body(Path(note["path"]))
    report = _propose_run(build_id, topic, note["title"], ink)

    topic_dir = vault._topic_dir(cfg.vault_dir, topic)
    path = topic_dir / f"{build_id}.md"
    body = (f"Proposed from [[{note['id']}]].\n\n"
            f"## Proposal (r1)\n\n{report.strip() or '(no proposal returned)'}\n\n"
            f"## Decision history\n\n### r1 proposed — {today}\n\n"
            f"## Original ink\n\n{_quote(ink)}")
    fields = dict(
        id=build_id, title=note["title"],
        description=_DESCRIPTION["proposed"].format(rev=1, model="opus"),
        tags=["build", topic], created=today, updated=today,
        source=f"build:{note['id']}",
        build="proposed", build_rev=1, related=[note["id"]],
        related_why="build proposal")
    vault._emit(cfg.vault_dir, path, fields=fields, body=body,
                generated_by="ozu/builds",
                sources=vault._note_source(dict(note)))
    indexer.index_file(con, path)      # the note, its edge and its Build row
    con.commit()

    build = load_build(path)
    _send_proposal_pdf(build)
    log.info("build %s proposed from %s (%s)", build_id, note["id"], topic)
    return build_id


def _revise(con, row) -> None:
    """Fold the recorded @input into a new revision: one read-only run, the
    Proposal section replaced, rev+1, a new PDF."""
    build = load_build(Path(row["path"]))
    fm = build["fm"]
    bid = str(fm["id"])
    rev = int(fm.get("build_rev") or 1) + 1
    context = ("\nTHIS IS A REVISION. The previous proposal and the decision "
               "history — the handwritten @input answers are decisions and "
               "the latest ones win — are below. Produce the full revised "
               "proposal, folding every input in.\n\n"
               f"<previous_proposal>\n{proposal_of(build)}\n</previous_proposal>\n\n"
               f"<decision_history>\n{history_of(build)}\n</decision_history>\n")
    report = _propose_run(bid, build["topic"], str(fm.get("title") or bid),
                          ink_of(build), revision_context=context)
    build["body"] = _replace_section(build["body"], "Proposal",
                                     f"Proposal (r{rev})",
                                     report.strip() or "(no proposal returned)")
    _append_history(build, f"### r{rev} revised — {dt.date.today().isoformat()}")
    fm["build"] = "revised"
    fm["build_rev"] = rev
    fm.pop("build_pending", None)
    # A revision is a different plan, written by hand on paper, so it starts
    # its own budget. This is the paper way back from `exhausted`, and it
    # cannot loop: only ink queues a revision.
    fm.pop("build_attempts", None)
    fm.pop("build_deferrals", None)
    _save_build(build, f"build {bid}: revised (r{rev})")
    _send_proposal_pdf(build)
    log.info("build %s revised to r%d", bid, rev)


def _execute(con, row) -> None:
    """One approved Build, executed once: clone, branch build/<id>, the
    approved proposal as the contract, push, PR, and a result section in the
    next edition. agent.py's containment applies unchanged."""
    cfg = get_config()
    build = load_build(Path(row["path"]))
    fm = build["fm"]
    bid, topic = str(fm["id"]), build["topic"]
    title = str(fm.get("title") or bid)
    today = dt.date.today().isoformat()
    model_key = str(fm.get("build_model") or cfg.build_default_model)
    model_id = cfg.build_models.get(model_key,
                                    cfg.build_models[cfg.build_default_model])
    repo = cfg.claude_repos.get(canonical_topic(topic))
    if not repo:
        fm["build"] = "error"
        _append_history(build, f"### failed — {today} · no repo mapped for "
                               f"'{topic}'")
        _save_build(build, f"build {bid}: error (no repo)")
        db.add_build_event(con, bid,
                           f"{bid} ({title}): no repo is mapped for '{topic}' — "
                           f"add it to OZU_CLAUDE_REPOS, then write @approved "
                           f"on the Build page again.")
        return

    refusal = budget_refusal(fm)
    if refusal:
        _exhaust(con, build, refusal)
        return

    # The launch is written to the note and committed *before* the subprocess
    # starts, so a run killed mid-flight — OOM, reboot, a session limit hours
    # in — has already paid for its attempt. The old loop recorded nothing
    # until the run came back, which is why 116 relaunches left no trace.
    fm["build"] = "executing"
    fm["build_attempts"] = _attempts(fm) + 1
    attempt = _attempts(fm)
    _save_build(build, f"build {bid}: executing ({model_key}, attempt "
                       f"{attempt}/{cfg.max_build_attempts})")
    db.add_build_event(con, bid,
                       f"launch {attempt}/{cfg.max_build_attempts} — "
                       f"{model_key} on build/{bid}", kind="trace")

    branch = f"build/{bid}"
    workdir = cfg.agent_dir / bid
    try:
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.parent.mkdir(parents=True, exist_ok=True)
        agent._git(["clone", "--quiet", repo, str(workdir)], cwd=None)
        base = agent._git(["symbolic-ref", "--short", "HEAD"], cwd=workdir)
        before = agent._head(workdir)
        agent._git(["checkout", "-q", "-b", branch], cwd=workdir)

        prompt = WORK_PROMPT_PATH.read_text().format(
            repo=repo, branch=branch, build_id=bid, topic=topic, title=title,
            proposal=proposal_of(build), history=history_of(build),
            original_ink=ink_of(build))
        if canonical_topic(topic) in cfg.isa_topics:
            prompt += ISA_GRADING_RULE
        report = agent._run_claude(
            prompt, workdir, model=model_id, writes=True,
            timeout=cfg.claude_timeouts.get(model_key, cfg.claude_timeout))
    except agent.UsageLimitError as e:
        # The window is the scarce thing. A run that consumed one keeps its
        # attempt; a run that merely bounced off a window already spent cost
        # nothing, so it is refunded and counted as a deferral instead —
        # bounded too, or a Build waits forever on a reset that never helps.
        until = window_reset(e)
        if not e.spent:
            fm["build_attempts"] = _attempts(fm) - 1
            fm["build_deferrals"] = _deferrals(fm) + 1
        db.add_build_event(
            con, bid,
            f"usage window "
            f"{'consumed by this run' if e.spent else 'already spent'}; "
            f"retry after {until:%Y-%m-%d %H:%M} UTC — now "
            f"{_attempts(fm)} attempts, {_deferrals(fm)} deferrals",
            kind="trace")
        refusal = budget_refusal(fm)
        if refusal:
            _exhaust(con, build, refusal)
        else:
            _append_history(build, f"### deferred — {today} · the usage window "
                                   f"was spent; waiting for "
                                   f"{until:%Y-%m-%d %H:%M} UTC")
            fm["build"] = "approved"
            _save_build(build, f"build {bid}: usage window spent, still approved")
        raise
    except agent.AuthError:
        # transient by policy and free: a broken login costs no window, so
        # the attempt is refunded and the Build waits, exactly as a queued
        # note does, for a human to re-authenticate.
        fm["build_attempts"] = _attempts(fm) - 1
        fm["build"] = "approved"
        _save_build(build, f"build {bid}: auth failed, still approved")
        db.add_build_event(con, bid, "claude auth is broken; attempt refunded",
                           kind="trace")
        raise
    except Exception as e:
        log.exception("build %s execution failed", bid)
        fm["build"] = "error"
        _append_history(build, f"### failed — {today} · {str(e)[:200]}")
        _save_build(build, f"build {bid}: error")
        db.add_build_event(con, bid,
                           f"{bid} ({title}): execution failed "
                           f"({str(e)[:160]}) — fix the cause, then write "
                           f"@approved on the Build page again "
                           f"({cfg.max_build_attempts - _attempts(fm)} of "
                           f"{cfg.max_build_attempts} attempts left).")
        db.add_build_event(con, bid, f"failed — {str(e)[:160]}", kind="trace")
        return

    pushed = agent._head(workdir) not in (None, before)
    if pushed:
        agent._git(["push", "--quiet", "-u", "origin", branch], cwd=workdir)
    gh = agent._github_repo(repo)
    review_url = None
    if pushed and gh:
        try:
            review_url = agent._open_pr(gh, base, branch,
                                        {"id": bid, "topic": topic,
                                         "title": title}, report)
        except Exception as e:
            log.warning("PR creation failed for %s, falling back to compare: %s",
                        bid, e)
        review_url = review_url or f"https://github.com/{gh}/compare/{base}...{branch}"

    fm["build"] = "done"
    if pushed:
        fm["build_branch"] = branch
    if review_url:
        fm["build_pr"] = review_url
    result_md = report.strip() or "(the agent returned no report)"
    build["body"] = _replace_section(build["body"], "Result", "Result",
                                     result_md)
    outcome = review_url or ("no commits pushed" if not pushed
                             else f"pushed {branch}, no PR")
    _append_history(build, f"### executed — {today} · {model_key} · {outcome}")
    _save_build(build, f"build {bid}: done")

    db.add_build_event(con, bid, f"done — {outcome}", kind="trace")

    m = re.search(r"/pull/(\d+)$", review_url or "")
    action = (f"Review PR #{m.group(1)} — {title}" if m else
              f"Review branch {branch} — {title}" if pushed else
              f"Read the report of {bid} — no commits were pushed")
    db.add_action(con, bid, action)

    # The result rides as a section of the next edition — the `builds` row
    # is now `done` and not yet in `mailed`, which is exactly what
    # edition._results() looks for. No document of its own any more.
    log.info("build %s executed: pushed=%s pr=%s", bid, pushed, review_url)


def _sweep_stale_executing(con) -> None:
    """A Build still `executing` when this phase starts is a corpse: every
    execution is synchronous inside the pipeline lock, so a live run cannot
    be observed from here — only one whose process died (OOM, reboot, a kill
    while the window was gone). Left alone it would refuse @approved forever
    ('already executed'); flip it to error, which the ink can re-arm — or to
    exhausted when that corpse was the Build's last attempt, because the
    attempt was counted before the run started and is not given back."""
    for row in con.execute(
            "SELECT * FROM builds WHERE status='executing'").fetchall():
        log.warning("build %s stuck in 'executing' — its run died; sweeping",
                    row["id"])
        build = load_build(Path(row["path"]))
        db.add_build_event(con, row["id"], "died mid-flight; swept",
                           kind="trace")
        refusal = budget_refusal(build["fm"])
        if refusal:
            _exhaust(con, build, f"{refusal} — the last died mid-flight")
            continue
        build["fm"]["build"] = "error"
        _append_history(build, f"### failed — {dt.date.today().isoformat()} · "
                               f"the run died mid-flight (host restart?)")
        _save_build(build, f"build {row['id']}: stale executing → error")
        db.add_build_event(con, row["id"],
                           f"{row['id']} ({row['title']}): the execution died "
                           f"mid-flight — nothing may have been pushed; write "
                           f"@approved on the Build page to run it again.")


def run_pending() -> int:
    """The builds phase of a tick: propose, then revise, then execute, all
    drawing on one OZU_MAX_BUILD_RUNS budget of headless runs. A broken
    claude auth stops the whole phase and leaves everything queued, exactly
    as agent.work_pending does; a spent usage window stands the phase down
    until the CLI's own reset time, because a tick every five minutes cannot
    make a window come back sooner."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    _sweep_stale_executing(con)
    until = stood_down_until(con)
    if until:
        log.info("builds phase stood down until %s — the usage window is "
                 "spent; nothing launched", until.isoformat())
        return 0
    budget = cfg.max_build_runs
    ran = 0
    try:
        for note in con.execute(
                "SELECT * FROM notes WHERE claude='build-requested'"
                " AND vault='personal' ORDER BY created, id").fetchall():
            if ran >= budget:
                break
            ran += 1
            try:
                bid = _propose_note(con, note)
                vault.set_directive_status(Path(note["path"]),
                                           f"proposed-build {bid}")
                con.execute("UPDATE notes SET claude=? WHERE id=?",
                            (f"proposed-build {bid}", note["id"]))
                con.commit()
            except agent.AuthError:
                raise
            except Exception:
                log.exception("proposal for %s failed", note["id"])
                vault.set_directive_status(Path(note["path"]), "error")
                con.execute("UPDATE notes SET claude='error' WHERE id=?",
                            (note["id"],))
                con.commit()
                db.add_build_event(con, None,
                                   f"Proposing a Build for “{note['title']}” "
                                   f"failed — see the journal; write @claude on a "
                                   f"fresh page to retry.")
            vault._git_commit(cfg.vault_dir, f"build proposal: {note['id']}")

        for row in con.execute(
                "SELECT * FROM builds WHERE pending='revise' ORDER BY id").fetchall():
            if ran >= budget:
                break
            ran += 1
            try:
                _revise(con, row)
            except agent.AuthError:
                raise                      # pending stays; retried after re-auth
            except Exception:
                log.exception("revision of %s failed", row["id"])
                b = load_build(Path(row["path"]))
                b["fm"]["build"] = "error"
                b["fm"].pop("build_pending", None)
                _save_build(b, f"build {row['id']}: revision error")
                db.add_build_event(con, row["id"],
                                   f"{row['id']} ({row['title']}): folding your "
                                   f"@input in failed — see the journal; write "
                                   f"@approved to build the last proposal, or "
                                   f"@input again to retry.")

        for row in con.execute(
                "SELECT * FROM builds WHERE status='approved' ORDER BY id").fetchall():
            if ran >= budget:
                break
            ran += 1
            _execute(con, row)             # errors are per-build inside
    except agent.UsageLimitError as e:
        # Subclass first: a spent window and a broken login both stop the
        # phase, but only one of them comes back on its own.
        stand_down(con, e)
    except agent.AuthError as e:
        log.error("claude auth is broken (%s); leaving builds queued — "
                  "re-authenticate on the VPS", e)
    if ran:
        log.info("build runs this tick: %d", ran)
    return ran


# ---------------------------------------------------------------------------
# split: one Build too big for a window becomes several

def split(parent_id: str, parts: list[Path]) -> list[str]:
    """Carve one Build into several, without a model call.

    A Build too big to finish in one usage window is not a Build with a bad
    plan — it is several Builds sharing one. `_propose_note` cannot make
    them: it runs a propose model call, which writes a *new* plan from the
    ink and so renegotiates what was already settled on paper. The parts are
    therefore text, cut from the parent's approved proposal by hand.

    Each part file is frontmatter carrying `title:`, then the proposal
    markdown that part holds. Every successor inherits the parent's decision
    history and original ink **whole**: the answers written beside an
    approval are decisions, and which of them binds which part is not this
    function's judgement to make.

    Successors are born `proposed` and go to the Scribe like any proposal,
    so `apply_directive` stays the only originator of an approval. A split
    hands nobody an approval — it hands the owner three documents to tick."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    ppath = find_build_path(parent_id)
    if ppath is None:
        raise SystemExit(f"no Build {parent_id!r} in the vault")
    parent = load_build(ppath)
    pfm = parent["fm"]
    prev = int(pfm.get("build_rev") or 1)
    today = dt.date.today().isoformat()
    history, ink = history_of(parent), ink_of(parent)

    specs = []
    for path in parts:
        fm, body = okf.parse_frontmatter(Path(path).read_text())
        title = str(fm.get("title") or "").strip()
        if not title or not body.strip():
            raise SystemExit(f"{path}: a part needs `title:` frontmatter and a body")
        specs.append((title, body.strip()))

    made: list[str] = []
    for i, (title, proposal) in enumerate(specs, 1):
        bid = new_build_id()
        note_body = (
            f"Part {i} of {len(specs)}, carved from [[{parent_id}]] r{prev} — "
            f"the proposal the owner approved in ink. The proposal below is that "
            f"proposal's own words for this part; the decision history and the "
            f"original ink are the parent's, entire.\n\n"
            f"## Proposal (r1)\n\n{proposal}\n\n"
            f"## Decision history\n\n{history}\n\n"
            f"### split {i}/{len(specs)} — {today} · carved from "
            f"[[{parent_id}]] r{prev}, which could not finish in one usage "
            f"window. No part of the approved plan was rewritten. This Build "
            f"is `proposed`: it runs when it is approved in ink, like any "
            f"other.\n\n"
            f"## Original ink\n\n{ink}")
        path = vault._topic_dir(cfg.vault_dir, parent["topic"]) / f"{bid}.md"
        inherited = okf.related_why(pfm)     # the parent's links, with their reasons
        why = {str(r): inherited.get(str(r)) or inherited.get("*")
               for r in (pfm.get("related") or [])}
        why = {k: v for k, v in why.items() if v} | {parent_id: "split from"}
        fields = dict(
            id=bid, title=title,
            description=_DESCRIPTION["proposed"].format(rev=1, model="opus"),
            tags=["build", parent["topic"]], created=today, updated=today,
            source=str(pfm.get("source") or f"build:{parent_id}"),
            build="proposed", build_rev=1,
            build_model=pfm.get("build_model") or cfg.build_default_model,
            build_split_from=parent_id,
            related=[parent_id, *(r for r in (pfm.get("related") or []))],
            related_why=why)
        vault._emit(cfg.vault_dir, path, fields=fields, body=note_body,
                    generated_by="ozu/builds",
                    sources=pfm.get("sources"))
        indexer.index_file(con, path)  # the note, its edges and its Build row
        con.commit()
        _send_proposal_pdf(load_build(path))
        db.add_build_event(con, bid, f"split {i}/{len(specs)} of {parent_id}",
                           kind="trace")
        vault._git_commit(cfg.vault_dir, f"build {bid}: split {i}/{len(specs)} "
                                         f"of {parent_id}")
        made.append(bid)
        log.info("build %s: part %d/%d of %s (%s)", bid, i, len(specs),
                 parent_id, title)

    pfm["build"] = "split"
    pfm["build_split_into"] = made
    pfm.pop("build_pending", None)
    _append_history(parent, f"### split — {today} · this Build could not "
                            f"finish in one usage window, so its approved r{prev} "
                            f"proposal was carved into {', '.join(made)}. "
                            f"Nothing was rewritten and nothing was executed "
                            f"here; each successor is `proposed` and waits for "
                            f"its own ink.")
    _save_build(parent, f"build {parent_id}: split into {', '.join(made)}")
    db.add_build_event(con, parent_id,
                       f"{parent_id} ({pfm.get('title')}): too big for one "
                       f"usage window, so it was split into {len(made)} Builds "
                       f"— {', '.join(made)}. Each is on its way to the Scribe "
                       f"as its own proposal; approve them one at a time. "
                       f"Nothing was executed and nothing was rewritten.")
    return made


# ---------------------------------------------------------------------------
# simulate: the document, without a vision call or a live Build

_SIMULATED_PLAN = """# What I understood
This is a simulated proposal, generated by `ozu builds simulate` from the note
below — no model read any ink and no Build exists in the vault. It exercises
every element the real document uses.

# Requirement
{body}

# Plan
1. First concrete step, one sentence, naming the part of the codebase it touches.
2. Second step — **bold** and *italic* render as weight and slope, never as asterisks.
3. Third step, long enough to wrap on the A5 column so the hanging indent shows.

# Files I expect to touch
- src/example/module.py — the core change
- tests/test_module.py — the behaviour gets a test

# Risks
- The one thing most likely to need your judgment.
- A second, smaller risk.

# What I will not do
- No scope beyond the note; no refactors it does not require.

# Open questions
- A question answerable in one handwritten line?"""


def simulate(note_id: str) -> Path:
    """Typeset a proposal PDF from an existing vault note, without a vision
    call or a headless run, and send it if Send-to-Kindle is configured — so
    the document design can be judged on the Scribe before the first live
    page. Nothing is written to the vault: annotating a simulate document
    files as an unmatched needs_review note, by design."""
    cfg = get_config()
    con = db.connect(cfg.db_path)
    note = con.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if note is None:
        raise SystemExit(f"no note {note_id!r} in the index — run `ozu index`?")
    body = agent._note_body(Path(note["path"]))
    bid = new_build_id()
    cfg.builds_dir.mkdir(parents=True, exist_ok=True)
    pdf = cfg.builds_dir / f"{pdf_name(bid, 1, note['title'])}.pdf"
    digest_pdf.build_propose_pdf({
        "id": bid, "title": note["title"], "topic": note["topic"], "rev": 1,
        # .replace, not .format — a note body with braces must not crash this
        "proposal": _SIMULATED_PLAN.replace("{body}",
                                            body or "(the note has no body)"),
        "history": f"### r1 proposed — {dt.date.today().isoformat()} (simulated)",
        "date": dt.date.today().isoformat(),
    }, pdf)
    sent = send_kindle.send_pdf(pdf)
    if sent:
        # recorded as `simulate`, so the next edition lists it as dead: a
        # simulated document has nothing behind it to keep it for
        edition.record(con, pdf.stem, "simulate", build_id=bid)
    log.info("simulated proposal %s from %s → %s (sent=%s)",
             bid, note_id, pdf, sent)
    return pdf
