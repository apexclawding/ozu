# Design

The rules Ozu keeps, and why. Each one exists because something went wrong
without it, and most are pinned by a test. Violating one is wrong even when
the suite passes. The code cites them by number ("invariant 5") and by
decision id ("D-07"). The numbers are stable, so gaps are deliberate.

## Invariants

**1. The Markdown vault is the source of truth.** The SQLite database is
derived, and `ozu index` rebuilds it from the files at any time. The
exceptions are the *ledgers*: tables that record what happened in the world
and cannot be re-derived from Markdown (`pages` dedup, `actions`, `mailed`,
`receipts`, `build_events`, `delivered`, `meta`). Ledgers are migrated in
place and never dropped.

The derived tables are exactly what a rebuild of the files gives. Every
writer writes its note, then calls `indexer.index_file(con, path)` — the
same function `rebuild` loops over — and never `upsert_note`/`add_edge`
directly. Anything a writer knows goes in the file first: an edge's reason
is `related_why` (one sentence for every link, or a map id → reason), so a
rebuild keeps it. `rebuild` is one transaction, so the MCP server never sees
an empty graph mid-rebuild. `tests/test_index.py` indexes live, rebuilds,
and compares.

**2. The work wall.** Employer content routes to a separate, walled vault:
no links, no actions, never served over MCP. A page with work content
anywhere on it is walled in full. Be honest about what this protects: the
`work_flag` is set *by the model*, so a page has already been sent to the
API by the time it is classified. The wall protects the vault, the index
and MCP. It cannot prevent the API call, and only not exporting a work
notebook does that. The wall reaches outbound documents too: an edition
counts a walled notebook's unread pages but never names it.

**3. MCP over HTTP requires a bearer token.** `ozu mcp --http` refuses to
start without `OZU_MCP_TOKEN` and answers 401 to anything without it, so it
fails closed. The bind stays on loopback; remote access is a TLS proxy in
front of it (`deploy/caddy-mcp.conf`). `OZU_MCP_PUBLIC_URL` feeds both the
401's metadata URL and the allowed-Host list. Behind a proxy, a wrong value
gets a **421** on authenticated requests only, so it looks healthy from
outside. Fix a 421 by setting the URL, never by disabling DNS-rebinding
protection.

**4. Secrets live in `~/ozu-data/.env` only** (chmod 600). They are never
committed and never echoed into logs.

**5. Red ink is the only thing that creates an action.** If anything
task-shaped became an action, red would stop meaning anything. It follows
that no page prints a `☐` that nothing could close (D-04).

**5b. Both vision calls stay at `effort: high`.** Measured in
[`docs/effort-sweep.md`](docs/effort-sweep.md): at `low`, an annotated page
read its own *printed* receipt line `[x] Buy garden hose` back as a fresh
handwritten tick. Printed-vs-handwritten is the first distinction to
collapse when effort drops, and editions print open action text, so a cheap
read can close an action nobody ticked. The saving is under two cents a
page.

**6. `CONVENTIONS.md` and the two vision prompts change together.** The
prompts are what enforce the ink language; the doc is what a person learns
it from. The annotation prompt accepts both `☐` and `[ ]` as an open action,
because a document can come back annotated months after it was printed.

**7. Builds push branches, never main.** A headless run's ledger is
Markdown (frontmatter plus a companion note), never db-only, and there is one
run per note, ever. Runs use the `claude` CLI's own login, and their
environment is an allowlist (`agent.RUN_ENV_KEYS`): a shell's basics and
`CLAUDE_CODE_OAUTH_TOKEN`, nothing from `~/ozu-data/.env`. A work run executes
tests it may have written and a research run reads the web; either could
print its environment into a file the pipeline then pushes.

**8. Headless runs are confined by path-scoped allowlists, built per run.**
Claude Code confines Bash to the working directory by itself but *not* the
file tools: a bare `Read` in an allowlist opens `~/ozu-data/.env`, and a bare
`Write` writes anywhere the user can. So `agent.scoped()` pins `Read`/`Edit`
to the run's own directory (`Read` also governs Glob/Grep, `Edit` also
governs Write), Bash is an explicit list of `<prefix>:*` rules, and
`Bash(git push:*)` is *denied*: deny beats allow, so branch-only is enforced
rather than requested. Research runs are pinned to the vault, which is also
what keeps a poisoned web page from reaching secrets or the work vault.
Never put a bare file tool in an allowlist.

**8b. A run that only returns text cannot write, whatever its allowlist
says.** `acceptEdits` approves edits *and* filesystem Bash inside the working
directory regardless of `--allowedTools`: measured on 2026-10-01, a run
allowed only a scoped `Read` wrote a file, edited one and ran `touch`, and
learning runs had already written stray files into the vault, which the
post-run commit pushed. So `_run_claude` is read-only by default
(`--permission-mode dontAsk` plus an explicit deny of
Bash/Edit/Write/MultiEdit/NotebookEdit), and only the two runs that change
code, the `@claude` work run and Build execution, pass `writes=True`. Learn,
research, the wiki and a Build proposal never do; `tests/test_agent.py` pins
that list at the source. Every headless run also starts with
`--strict-mcp-config` and no MCP server: Ozu's own server carries `capture`,
a vault write, and may be registered for your interactive sessions.

**9. A run cannot install anything.** "The agent runs the tests" only holds
where dependencies already exist. Do not allow `pip install` / `npm
install`: install scripts are arbitrary code running as you, which is what
the allowlist exists to prevent.

**10. A PDF text layer is read through `textlayer.extract()`, never
`page.get_text()`.** The text layer is a contract: the annotation prompt
uses it as the printed-side reference a tick is matched against. A real
book face emits ligatures, so `Confirm the official fee` comes back as
`Conﬁrm the oﬃcial fee`. The fold is U+FB00–U+FB06 and nothing else. It is
**not** NFKC, which would also flatten `②③④` to bare digits
([`docs/glyph-loss.md`](docs/glyph-loss.md)).

**10b. A page is never spent by the reader being down.** A failed vision call
is either "this page is bad" (a refusal, a truncation, unparseable JSON →
`error`, which only `ozu requeue` revives) or "the endpoint is down" (rate
limit, 5xx, connection, and the credit-balance 400, matched on its message).
The second leaves the page `new` and **stops the run**, since every page
behind it would fail the same way. This is not theoretical: an API credit ran
out once and stranded thirteen pages as `error` for four days. The other half
of that failure was silence, so an edition now goes out for trouble alone and
says what it could not read.

**11. A learning run's allowlist is `Read(//<vault>/**)` and nothing else.**
A transcript is third-party text, captions nobody here wrote, so the run
that briefs it gets no web tools, no Bash and no Edit, ever. A prompt hidden
in the captions can produce a bad brief and nothing more. Do not "improve"
the brief by giving the run the web; `tests/test_learn.py` pins the allowlist
verbatim. The Learn sheet prints no `☐` (invariant 5): red ink creates an
action on it exactly as anywhere else.

**11c. Every Build carries a lifetime attempt budget, written before the run
starts.** A per-tick cap on a five-minute timer is no bound at all: one Build
was once launched **116 times** overnight under a cap of 2 that was never
exceeded ([`docs/build-attempts.md`](docs/build-attempts.md)). So the budget
lives in the Build's frontmatter, is committed *before* the subprocess
starts (a run killed mid-flight has already paid), and past the cap the
Build is `exhausted`, a status nothing selects. A spent usage window is
told apart from a broken login, and stands the whole phase down until the
reset time the CLI names. Do not "fix" a stuck Build by raising the cap: the
cap is what stops the loop.

**11d. A split hands out no approvals.** `ozu builds split` carves an
approved Build that is too big for one usage window into parts, by text,
with no model call. The parts are born `proposed` and travel to paper again.
Approval has exactly one origin, ink read from a page, so what executes is
always text the owner read.

**11e. The wiki run reads the vault and nothing else, and writes nothing.**
`wiki.py` compiles concept pages (`topics/wiki/`, `type: Concept`) from your
ink, voice, learning briefs and research briefs, a small batch every cycle.
The run's whole allowlist is `Read(//<vault>/**)`, the same fence as
invariant 11 and for the same reason: what it reads includes briefs of
third-party captions. It returns JSON; the pipeline writes the files, keeps
`created`, owns each page's `## Changelog`, refuses any page id that is not
`wiki-<slug>`, and drops the brackets from any cited id that does not exist
rather than mint a broken edge. A failed run marks its batch `error` and
rests until the next day. The Sunday sheet prints no `☐` (invariant 5): a
correction is a note, never an action, and a struck-through claim is the one
strike on paper that means anything, so the conventions (6) carry it.
`wiki_fed` is a ledger (invariant 1). `tests/test_wiki.py` pins the
allowlist verbatim.

**12. The vault is an OKF v0.2 bundle, and `okf.py` is the only module that
knows the format** ([`docs/okf.md`](docs/okf.md)). Every note carries
`type`, `description`, `generated` and `sources`. `index.md` (in every
folder) and `log.md` are derived views, regenerated after every write and
never appended to. A note's filename is its id. Frontmatter is strict YAML
written by `okf.dump_frontmatter`; never hand-roll one.

## Decisions

Each one was made against a measurement on real pages. The numbers live in
the module docstrings and in `docs/`.

| id | decision | where |
|---|---|---|
| D-01 | The text layer of every printed document must round-trip exactly. Two gates: *chars* (nothing dropped) and *intact* (long tokens unbroken). | `tests/test_textlayer.py`, `docs/glyph-loss.md` |
| D-03 | Body pages are greyscale. Hierarchy is weight, size, rule and space, so nothing is lost on a monochrome panel. | `layout.py` |
| D-04 | `☐` is printed only beside something a tick can close; no drawn mark is an unfilled rectangle, because an outlined square is a checkbox to anything reading the image. | `layout.py` |
| D-05 | Never `background-color` in the stylesheet: MuPDF's Story repaints it at the top of every following page. Fills are drawn in `render()`. | `layout.py`, `docs/glyph-loss.md` |
| D-06 | Every document opens with a cover sized for the device's library thumbnail (~126 px wide). It is a label, so nothing appears only there. | `layout.py` |
| D-07 | Two editions a day instead of a document per event (57 documents in 19 days had filled the library). Each section is its own Story: `page-break-before` hangs Story. | `edition.py` |
| D-08 | Cover blocks are tinted per kind, light with ink-coloured type, and never red, because red is the action ink. | `layout.py` |
| D-09 | A returning page with no new ink is never read: it is compared with the copy that was sent. Measured, 67 % of returning pages carried nothing. Every uncertainty resolves to reading the page. | `ink.py` |
| D-10 | Two vision jobs, two models: the strongest model reads notebook pages (the irreplaceable capture); a cheaper one reads returning documents, where it measured identical on ticks, directives and transcription. | `config.py`, `understand.py` |
| D-11 | A tick is checked against the ink beside its printed box, and the check never closes anything by itself: a completion with no ink beside its line, or ink nobody read, comes back on paper as an open line with the reason. | `ink.py`, `understand.py` |
| D-12 | No text is lost at a page break. MuPDF's Story can lay a block past the page foot and count it as placed, so it is clipped silently (measured: 2-4 of every 140 positions near the foot, after any kind of block). Every block carries an id so an overflow is visible and the page is placed again shorter; every finished PDF is read back against the text that went in, and one missing anything raises `LostTextError` and is deleted, never sent. | `layout.py`, `tests/test_pagebreak.py` |
