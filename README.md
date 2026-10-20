# Ozu

**Write on e-paper. Claude does the rest, and if your input is needed, write it again.**

Ozu turns handwritten pages from an e-ink tablet into a Markdown knowledge
vault that Claude can read from anywhere, and then writes back to you on
paper. Twice a day a typeset *edition* lands on the tablet: what your ink
became, tasks it closed, research it did overnight, and code it proposes to
write. You answer in ink. A tick closes a task. `@approved`, written by hand,
runs a code change on a branch and opens a pull request.

Most "second brain" tools are one-way: notes go in and nothing comes back.
In Ozu the paper is both the input and the control surface, so nothing an
agent does to your code happens without a page you read and a pen stroke
you made.

```
 you write             Ozu reads                  you get back
 ─────────             ─────────                  ────────────
 a notebook page  ──►  vision model ──► vault/  ──► Ozu Morning / Evening (PDF)
 (PDF export)          (one call per     *.md        what your ink became
                        new page)        + SQLite     ☐ open actions, receipts
                                         + MCP        research briefs
                                                      Build proposals
 ink on an edition ──► annotation pass ──► ticks close actions
                                          @input revises a Build
                                          @approved runs it → branch → PR
```

It has run every day through 2026 for one person, one Kindle Scribe
Colorsoft and a 4-core VPS: SQLite and files, no Redis, no queue, no JVM.

## The ink language when color is available

Four colours and one tag character. Everything downstream comes from these
marks (full reference: [`CONVENTIONS.md`](CONVENTIONS.md)).

| Ink | Means | What happens |
|---|---|---|
| **Black** | capture | becomes note text |
| **Red** | action | the *only* thing that creates a task |
| **Blue** | an idea worth developing | linked to older thinking and resurfaced |
| **Green** | personal | routes to `people/` |
| `@garden` | a project tag | files the content under that project |
| `✓` on a printed `☐` | done | closes the action; receipt in the next edition |
| `@research` | a question | an overnight brief that cites your own notes |
| `@claude` | build this | opens a **Build**: a plan comes back on paper |
| a video saved to a playlist | learn this | a brief on the evening sheet, back again after 2, 7 and 30 days ([`capture/`](capture)) |
| `ozu learn add <link or PDF>` | learn this | the same, for an article, a post or a document — figures included |

Red is exclusive on purpose. If anything task-shaped became a task, red would
stop meaning anything.

## The Build loop: approving pull requests with a pen

1. Write an idea, tag it `@kettle @claude`, and export the page.
2. Within five minutes a document called `Ozu Build r1 - <your idea>` lands
   on the tablet: what Claude understood, a precise requirement, a
   step-by-step plan, the risks, what it will *not* do, and its open
   questions. It was written by a read-only run that can see the repo and
   your vault but cannot change anything.
3. Answer in ink. `@input` plus your words produces revision `r2`.
   `@approved` (optionally `@opus`, `@sonnet` or `@fable`) runs it once, in a
   fresh clone, on branch `build/<id>`, and opens a PR. `@drop` closes it.
4. The result arrives in the next edition with the agent's report and a
   printed `☐ Review PR #12` line. Tick it when you have.

What executes is always text you read. The agent's tools are scoped per run
(file access pinned to its own clone, an explicit Bash allowlist, `git push`
denied, no installs), and every Build carries a lifetime attempt budget
written *before* it launches. That last rule exists because one Build once
relaunched itself 116 times overnight: [`docs/build-attempts.md`](docs/build-attempts.md).

Set `OZU_ISA_TOPICS` for a project and its proposals take a different shape:
the ideal state, the decisions, and numbered steps that each name the check
that proves them. The result then reports every step as pass, fail or not
verified, so the plan you approved is also the test it is graded against.

## A wiki that argues back

Learning briefs say what one video said. The wiki says what you know about a
subject across every video, page of ink, voice memo and research brief that
touched it. Every two-hourly cycle a read-only run folds a few new notes into
concept pages in `topics/wiki/`: each claim cites the notes it came from,
disagreements between sources are kept and named, and a page says how the
subject touches your own projects.

On Sundays the evening edition carries a *Wiki this week* sheet with what
changed. Write beside a claim to correct it, or strike a line through it to
drop it; your ink outranks every brief, and the next cycle applies it.

## What it measures instead of assuming

The design decisions were made against real pages, and the numbers are in the
repo.

- **67 % of returning pages carried no new ink.** They are now compared with
  the copy that was sent and never read by the model (`ink.py`, D-09).
- **A cheaper model reads returning documents.** On ticks it found 27 of 27,
  none invented; the strongest model is kept for notebook pages, the
  irreplaceable capture (D-10).
- **Lowering reasoning effort invents ticks.** At `low`, the model read a
  *printed* receipt line as a fresh handwritten tick. Both calls stay at
  `high`, at under two cents a page ([`docs/effort-sweep.md`](docs/effort-sweep.md)).
- **14 characters lost across 16 documents** (`€`, `②③④`, `≤`) led to a
  renderer rewrite with a round-trip gate on every printed character
  ([`docs/glyph-loss.md`](docs/glyph-loss.md)).

The rules these produced, and the incidents behind them, are in
[`DESIGN.md`](DESIGN.md).

## Set it up with Claude Code

On the Linux machine Ozu will run on, open Claude Code and paste:

```
Set up Ozu for me. Clone https://github.com/apexclawding/ozu to ~/ozu if it
is not there yet, then read ~/ozu/docs/setup-with-claude.md and follow it
with me step by step.
```

Claude checks the machine, installs, and walks you through the accounts one
at a time: the API key, your tablet's export folder, a first test page, and
then, if you want them, paper back on the tablet, Builds, a vault backup and
Claude access. You type every secret yourself, in your own terminal; the
runbook forbids Claude from reading them. About an hour, most of it waiting
on accounts. The runbook is [`docs/setup-with-claude.md`](docs/setup-with-claude.md);
the manual steps are below.

## Install

You need Linux with systemd, Python 3.12, an
[Anthropic API key](https://console.anthropic.com), and a folder your tablet
exports PDFs into that `rclone` can read (the Kindle Scribe exports to Google
Drive). Optional: a Send-to-Kindle address and SMTP account for delivery
back to the device, the [`claude` CLI](https://docs.claude.com/en/docs/claude-code)
for `@claude` and `@research`, and a GitHub token for pull requests.

```bash
git clone https://github.com/<you>/ozu ~/ozu
~/ozu/deploy/install.sh          # venv, ~/ozu-data/.env, vault, systemd timers
nano ~/ozu-data/.env             # ANTHROPIC_API_KEY, OZU_RCLONE_REMOTE, …
~/ozu/.venv/bin/ozu run          # first run by hand: pull → read → vault
~/ozu/.venv/bin/ozu status
```

Every setting is documented in [`.env.example`](.env.example). Then write one
page in black, red and blue with an `@tag` and a small sketch, export it in
colour, and run `ozu run` again.

Documents arrive named "Ozu Morning …", "Ozu Build r2 - …" and so on. The
name is configuration: `OZU_BRAND` sets what goes out, and
`OZU_DIGEST_PREFIXES` lists every name a returning document is recognised by,
case-insensitively. If your device already holds documents sent under another
name, list it there too, so ink written on them still routes home.

### Commands

```
ozu run                  the 2-hourly cycle: pull, read, vault, research, wiki
ozu tick                 the 5-minute paper loop: pull, read, Builds
ozu edition morning      build and send an edition now (also evening | extra)
ozu learn                brief the videos and articles waiting in the vault now
ozu learn add <url|file> read an article, an X/Threads/LinkedIn post or a PDF into the queue
ozu learn probe <list>   can this server read these links? (reads, writes nothing)
ozu wiki                 wiki status; `wiki run` now; `wiki requeue [--all]`
ozu builds               list Builds; `builds split|reset|simulate`
ozu index                rebuild the database from the Markdown, any time
ozu requeue error        re-read pages that failed
ozu okf check            vault conformance gate (Open Knowledge Format)
ozu lint [folder]        read-only lint of the learning loop (default topics/learning)
ozu mcp [--http]         MCP server for Claude Code / Claude.ai
```

## One more thing: talk to it, and learn from what you watch

Paper is one front door. [`capture/`](capture) adds two more to the same
vault, and neither sends your audio anywhere:

- **Voice memos.** Record on your phone, drop the file in a NAS folder, and a
  Mac transcribes it on-device ([`transcribe/`](transcribe), Whisper on
  Apple Silicon) and pushes a note into the vault. It is searchable over MCP
  like any handwritten page.
- **Learn from YouTube.** Save a video to a playlist. A small container on
  the NAS fetches its captions, and the brief Ozu writes from them lands on
  the evening edition's Learn sheet, then comes back after 2, 7 and 30 days.
- **Learn from what you read.** `ozu learn add <link>` does the same for a
  blog post, a news article, a Substack, an X, Threads or LinkedIn post, or
  a PDF. The server reads it — text through trafilatura, figures (charts,
  diagrams, screenshots, a post's image) saved as images beside the note,
  a PDF's figure pages rendered — and the brief run opens the figures with
  the same read-only, vault-only permission it uses for transcripts. Figures
  are git-ignored (`assets/learning/`), so the vault does not grow by a
  gigabyte a year. Anything it cannot read (a paywall, a bot wall) comes
  back on the sheet as "could not be read", not silence. Fetching is
  guarded: public addresses only, checked at every redirect and pinned for
  the connection, so a link can never reach a service on the server's
  loopback. Run `ozu learn probe` on a list of links you read to see what
  your server can reach before relying on it.

```
ozu/
├── src/ozu/      the core: paper → vault → Claude → paper   (Linux server)
├── capture/      voice memos + learn from YouTube            (Mac + NAS)
└── transcribe/   on-device audio → Markdown                  (Apple Silicon)
```

Each part has its own README, `pyproject.toml` and tests, and CI runs each
on the machine it needs.

## Use your vault from Claude

The vault is plain Markdown in git, an [Open Knowledge Format](docs/okf.md)
bundle you can open in Obsidian. Over MCP, Claude gets `search_notes`,
`get_note`, `graph_neighbors`, `timeline`, `open_actions` and `capture`
(writing a note back from any conversation).

```json
{"mcpServers": {"ozu": {"command": "ssh", "args": ["your-server",
  "cd ~/ozu && exec .venv/bin/ozu mcp"]}}}
```

Over ssh, the key is the auth and no port is exposed. For Claude.ai, `ozu mcp
--http` serves on loopback behind a bearer token. Put a TLS proxy in front
(`deploy/caddy-mcp.conf`).

## Costs

- **Vision:** about $0.065 per page read (input is mostly the page image).
  Unchanged pages are deduplicated by image hash, so re-exporting a notebook
  costs only its new pages, and returning pages with no ink cost nothing.
- **Builds, research and learning briefs** run on the `claude` CLI's own
  login, not the API key.

## Limits, honestly

- **One person, one device, one box.** Ozu is a working personal system, not
  a product. Expect to read code.
- **Colour matters.** Actions come only from red ink, so on a monochrome
  tablet everything works except creating actions by colour.
- **Delivery is Send-to-Kindle.** Other tablets can sync the PDFs in
  `~/ozu-data/editions` some other way, but that path is not built.
- **The work wall is post-API** (see invariant 2). If a notebook must never
  leave the device, do not export it.

## Development

```bash
pip install -e ".[dev]"
pytest -q                          # hermetic: the vision call is mocked
python tools/render_sample.py      # every document type → build/sample/
```

`render_sample.py` builds every document from synthetic fixtures and checks
that every printed character survives the round trip. Look at the pages, not
just the numbers: covers are also written at library-thumbnail scale.

## Roadmap

Open items, and why each waits: [`ROADMAP.md`](ROADMAP.md).

## License

MIT. See [`LICENSE`](LICENSE). To contribute, see [`CONTRIBUTING.md`](CONTRIBUTING.md); to report a vulnerability, see [`SECURITY.md`](SECURITY.md).
