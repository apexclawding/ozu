# Set up Ozu with Claude Code

This file is written for **Claude Code**, working with a person who wants Ozu
running for themselves. It is a runbook: you (Claude) drive the steps, the
person makes the choices, creates the accounts and types the secrets. A
person reading it directly can follow it too; every step says what to run.

The person starts it by pasting this into Claude Code on the Linux machine
Ozu will run on:

```
Set up Ozu for me. Clone https://github.com/apexclawding/ozu to ~/ozu if it
is not there yet, then read ~/ozu/docs/setup-with-claude.md and follow it
with me step by step.
```

Plan for about an hour, most of it waiting on accounts. The minimum that
works is steps 1 to 7: handwriting in, Markdown notes out. Steps 8 to 12 add
paper coming back, Builds, a backed-up vault, Claude access and voice
capture, each on its own.

## Ground rules for you, Claude

These hold for the whole session. Repeat the relevant one to the person when
it applies.

1. **Never read a secret.** Do not `cat`, `grep -v`, `sed -n` or open
   `~/ozu-data/.env`, and never ask the person to paste a key, password or
   token into this chat. Secrets go into `.env` by the person, in a second
   terminal (`nano ~/ozu-data/.env`). You may check that a value is *set*
   without seeing it:
   `grep -cE '^ANTHROPIC_API_KEY=sk-ant-.{20,}' ~/ozu-data/.env` prints 1 or 0.
   Settings that are not secret (paths, project names, model ids) you may
   write yourself with `sed -i`.
2. **Ask before anything that installs, spends or sends.** `sudo apt`,
   enabling linger, a first vision call (a few cents), any email to the
   tablet, any push to GitHub. Say what will happen and wait for a yes.
3. **One step at a time, and verify it.** Each step below ends with a check.
   Do not start the next step until the check passes. If it fails, use the
   troubleshooting table at the end before improvising.
4. **Keep a progress file** at `~/ozu-data/SETUP-PROGRESS.md`: one line per
   finished step with the date and what was chosen (never a secret). If the
   session is interrupted, read it first and resume where it stops.
5. **Interactive commands run in the person's terminal.** `rclone config`,
   `claude setup-token`, `gh auth login` and editors need a real terminal.
   Give the exact command and wait for the person to say it is done. In
   Claude Code they can also prefix a non-interactive command with `!` to run
   it in this session.
6. **Don't change Ozu's code to get past a step.** If something in the repo
   is wrong, say so and note it in the progress file; the person can open an
   issue.

## Step 0 — Ask first

Ask these together, before touching anything, and record the answers in the
progress file. The defaults are fine for a first run.

| Question | Why it matters | Default |
|---|---|---|
| Which tablet? A colour one (Kindle Scribe Colorsoft) or monochrome? | Actions come only from **red** ink. On a monochrome tablet everything else works, and actions come from ticks and printed lines instead. | Kindle Scribe |
| Where do its PDF exports land? Google Drive, OneDrive, Dropbox, another folder? | Ozu pulls with rclone, which speaks to all of them. | Google Drive |
| Which projects do you write about? Two to five short names. | Each becomes an `@tag` that files notes into its own folder. | `ideas` only |
| Your timezone? | Editions arrive at 07:00 and 20:00 **host** time; most servers run UTC. | the server's |
| Do you want paper back on the tablet? | Needs a Send-to-Kindle address and an SMTP account (step 8). | yes |
| Do you want `@claude` to propose and build code in a repo of yours? | Needs the `claude` CLI logged in on this machine and a repo (step 9). | later |
| Anything you must never send to an API, like work notes? | The work wall is a backstop, not protection: never export those notebooks. | — |

## Step 1 — Check the machine

Run these and report the results in one short table:

```bash
cat /etc/os-release | head -2; uname -m
python3 --version; python3 -c "import venv, ensurepip" && echo venv-ok
git --version; rclone version 2>/dev/null | head -1 || echo "no rclone"
systemctl --user is-system-running 2>&1; loginctl show-user "$USER" -p Linger
timedatectl | grep -E "Time zone"; df -h ~ | tail -1
claude --version 2>/dev/null || echo "no claude CLI on PATH"
```

What has to be true:

- **Linux with systemd** and a user session (`systemctl --user` answers).
- **Python 3.12 or newer, with the venv module.** On Ubuntu and Debian,
  `venv-ok` missing means `sudo apt install python3-venv` (ask first).
  Ubuntu 24.04 has 3.12.
- **git**, and **rclone** (`sudo apt install rclone`, or
  `curl https://rclone.org/install.sh | sudo bash` for a newer one; ask first).
- About 2 GB free: page images accumulate slowly, a few MB a day.

**Check:** every line above is green, or the person has agreed to what you
will install.

## Step 2 — Install

```bash
[ -d ~/ozu ] || git clone https://github.com/apexclawding/ozu ~/ozu
~/ozu/deploy/install.sh
```

The installer must be run from `~/ozu`: the systemd units point there. It
creates the venv, `~/ozu-data/.env` (copied from `.env.example`, chmod 600),
the vault at `~/ozu-data/vault`, installs the timers, and enables linger so
they survive logout (it may ask for sudo for that).

**It also starts the timers, before anything is configured.** Stop them now
so they don't fail every five minutes while you work; step 11 starts them:

```bash
systemctl --user stop ozu-tick.timer ozu-nightly.timer ozu-morning.timer ozu-evening.timer
```

**Check:** `~/ozu/.venv/bin/ozu status` prints a status block with zero
pages, and `ls -l ~/ozu-data/.env` shows `-rw-------`.

## Step 3 — The Anthropic API key

The vision calls are billed to an API key, separately from any Claude
subscription: about $0.065 per page read.

1. The person creates a key at <https://console.anthropic.com> → API keys,
   adds a little credit, and (recommended) sets a monthly spend limit.
2. In their second terminal: `nano ~/ozu-data/.env`, replace the
   `ANTHROPIC_API_KEY=` value, save.

**Check (no secret shown, no tokens spent):**

```bash
grep -cE '^ANTHROPIC_API_KEY=sk-ant-.{20,}' ~/ozu-data/.env
~/ozu/.venv/bin/python -c "from ozu.config import get_config; get_config(); import anthropic; anthropic.Anthropic().models.list(limit=1); print('key ok')"
```

Ozu's own loader reads `.env` into the process. Don't `source` the file in a
shell instead: an unquoted value with a space in it is run as a command.

## Step 4 — Connect the export folder (rclone)

1. The person exports one notebook from the tablet first, so the folder
   exists: the notebook's share or export option, as **PDF**, to the cloud
   drive (a Kindle Scribe sends to a "Kindle Scribe" folder in Google Drive
   once Drive is linked on the device). **In colour**: a greyscale export
   throws away every ink colour.
2. In their second terminal: `rclone config` → `n` (new remote) → name it
   `gdrive` → type `drive`. Leave client id and secret empty. Scope: choose
   **read-only** (`drive.readonly`); Ozu only copies files down.
   On a server with no browser, answer **n** to "Use web browser to
   automatically authenticate?" and run the `rclone authorize "drive"`
   command it prints **on a computer with a browser**, then paste the token
   it returns back into the server's prompt.
3. Find the folder together:

```bash
rclone lsd gdrive:
rclone lsf "gdrive:Kindle Scribe" --max-depth 2 | head
```

4. Write the remote into `.env` (not a secret, so you may do it), quoting
   nothing:

```bash
sed -i 's|^OZU_RCLONE_REMOTE=.*|OZU_RCLONE_REMOTE=gdrive:Kindle Scribe|' ~/ozu-data/.env
```

**Before the first pull, count what is there.** If the person has been
exporting for a while, the folder already holds every old notebook, and the
first run will read them: up to 40 pages a run and 120 a day, at about
$0.065 a page. Count first and say the number:

```bash
rclone lsf "gdrive:Kindle Scribe" --recursive --include "*.pdf" | wc -l
```

More than a handful of PDFs: offer a fresh folder holding only what they
want read (they move the test export into it) and point the remote there.

**Check:** `~/ozu/.venv/bin/ozu pull` finishes without an error and
`ls ~/ozu-data/raw` shows the exported PDF.

## Step 5 — Projects, and what must never be exported

Write the projects from step 0 (lowercase, comma-separated, no spaces):

```bash
sed -i 's|^OZU_PROJECTS=.*|OZU_PROJECTS=garden,house|' ~/ozu-data/.env
```

`ideas` and `people` always exist. If you ever rename a topic, set
`OZU_TOPIC_ALIASES=old:new` (comma-separated pairs): the old name keeps
working as an `@tag`, a repo key, an ISA topic and an MCP argument, and always
files under `new`.

Explain the work wall in one sentence:
notebooks whose name starts with a prefix in `OZU_WORK_PREFIXES` (default
`work`) are walled off, **but only after the page has been sent to the API**,
so the real rule is: never export a notebook that must not leave the tablet.

**Check:** `grep -E '^OZU_(PROJECTS|WORK_PREFIXES)=' ~/ozu-data/.env`.

## Step 6 — The first page

Ask the person to write one page in a new notebook, at their normal speed.
Any name works except one starting with **"Ozu "**: that prefix marks
Ozu's own documents coming back annotated, and such a page is read as
annotation, not as notes. `Inbox` is a good name. On the page:

- two or three lines in **black**, a real thought;
- one line in **red**: a task (it must come back as an action);
- one line in **blue**: an idea;
- a small labelled boxes-and-arrows sketch;
- one of their `@project` tags.

Then export it in colour. Tell them it costs one vision call, a few cents,
and wait for a yes. Then:

```bash
cd ~/ozu-data && ~/ozu/.venv/bin/ozu run && ~/ozu/.venv/bin/ozu status
ls -t ~/ozu-data/vault/topics/*/*.md | head -3
```

Open the newest note(s) and walk through them with the person:

- Is the **transcription** right, compared with the page in their hand?
- Did the **red line become an action** (`ozu status` counts it)?
- Did the `@tag` file it under `topics/<project>/`?
- Did the sketch become a description or a mermaid diagram?

`confidence` below 0.8 sets `needs_review: true`: the system saying it
struggled, which is what it should do.

**Check:** at least one note, one open action, the right folder.

## Step 7 — The vault is theirs

Show them `~/ozu-data/vault`: plain Markdown, one note per idea, a folder per
project, readable in any editor or Obsidian. They may edit any note by hand.
`ozu index` rebuilds the database from the files at any time, so the files
are the truth. Run `~/ozu/.venv/bin/ozu okf check`; it should report zero
errors.

That is a working Ozu. Ask which of steps 8 to 12 they want now; each is
independent.

## Step 8 — Paper back: Send-to-Kindle

Editions are typeset PDFs emailed to the tablet. They need:

1. **The tablet's address.** amazon.com → Manage Your Content and Devices →
   Preferences → Personal Document Settings → the `@kindle.com` address of
   the tablet.
2. **An approved sender.** Same page → Approved Personal Document E-mail
   List → add the address Ozu will send from.
3. **An SMTP account for that address.** With Gmail: turn on 2-step
   verification, then create an **app password** (Google Account → Security →
   App passwords). Never the account's real password.
4. In the second terminal, the person sets `OZU_KINDLE_EMAIL`,
   `OZU_SMTP_HOST` (`smtp.gmail.com`), `OZU_SMTP_PORT` (`587`),
   `OZU_SMTP_USER` and `OZU_SMTP_PASS`.

**Check (presence only):**
`grep -cE '^OZU_(KINDLE_EMAIL|SMTP_USER|SMTP_PASS)=.+' ~/ozu-data/.env`
prints 3. Then, **after a yes**, send one edition now:

```bash
cd ~/ozu-data && ~/ozu/.venv/bin/ozu edition extra
```

It arrives on the tablet as "Ozu Extra …" within a few minutes. If it does
not, the sender is almost always missing from Amazon's approved list.

## Step 9 — Builds and research (`@claude`, `@research`)

These run headless Claude Code on this machine, billed to the person's
Claude subscription, not the API key.

1. **A login that survives the timers.** The interactive login can expire
   and only refreshes interactively. In their terminal: `claude
   setup-token`, then set `CLAUDE_CODE_OAUTH_TOKEN=` in `.env` (second
   terminal; it is a secret).
2. **Map a repo.** Pick a small repo of theirs to try it on. This machine's
   git must be able to push to it (`git ls-remote <url>` succeeds). Write the
   mapping (not a secret):
   `sed -i 's|^OZU_CLAUDE_REPOS=.*|OZU_CLAUDE_REPOS=kettle=git@github.com:them/kettle.git|' ~/ozu-data/.env`
   The project name becomes an `@tag` too.
3. **Optional, for pull requests:** a fine-grained GitHub token with
   *Pull requests: read and write* on that repo only, as `OZU_GITHUB_TOKEN`.
   Without it a Build pushes its branch and reports a compare link.

**Check:** the person writes `@kettle @claude` beside an idea and exports.
Run `~/ozu/.venv/bin/ozu tick`. Within a few minutes `ozu builds` lists a
Build in `proposed`, and with step 8 done, "Ozu Build r1 - …" arrives on the
tablet. Nothing touches the repo until they write `@approved` on it.

## Step 10 — Back up the vault (and read it on a phone)

1. The person creates an **empty private** repository on GitHub, e.g.
   `ozu-vault`.
2. Then (ask before the push):

```bash
cd ~/ozu-data/vault
git branch -M main
git remote add origin git@github.com:them/ozu-vault.git
git push -u origin main
```

The timers push after every run from then on. On a phone, Obsidian with its
Git plugin clones the same repo.

**Check:** the repo on GitHub shows the notes from step 6.

## Step 11 — Time zone, and start

Editions fire at 07:00 and 20:00 in the **host's** time zone. If the server
runs UTC and the person does not, either change the host zone (affects
everything on the machine; ask) with
`sudo timedatectl set-timezone Europe/Paris`, or tell them their editions
arrive at those times UTC.

Start the timers:

```bash
systemctl --user start ozu-tick.timer ozu-nightly.timer ozu-morning.timer ozu-evening.timer
systemctl --user list-timers | grep ozu
loginctl show-user "$USER" -p Linger    # Linger=yes, or the timers die at logout
```

**Check:** four timers listed with a next run time; after five minutes
`journalctl --user -u ozu-tick -n 20` shows a clean tick.

## Step 12 — Claude, reading the vault (MCP)

From Claude Code on the person's own computer, over ssh (no port opened,
the ssh key is the auth):

```bash
claude mcp add ozu -s user -- ssh their-server "cd ~/ozu && exec .venv/bin/ozu mcp"
```

Then in any project: "what did I write about the garden?" Claude gets
`search_notes`, `get_note`, `graph_neighbors`, `timeline`, `open_actions`
and `capture`.

Claude.ai (web and phone) needs HTTPS: a domain, a TLS proxy in front of
loopback (`deploy/caddy-mcp.conf`), `OZU_MCP_TOKEN` and `OZU_MCP_PUBLIC_URL`
set, and `systemctl --user enable --now ozu-mcp`. Offer it as a separate
session; it is the one step that exposes anything to the internet.

**Optional, later:** voice memos and learning from YouTube need a Mac and a
NAS; if this checkout has `capture/`, its README is the runbook.

## Finish

Write the summary into `~/ozu-data/SETUP-PROGRESS.md` and show it:
what works now, what was skipped, when the first edition arrives, and the
three things to remember:

1. Export **in colour**, and never export a notebook that must stay private.
2. **Red** is the only ink that makes a task; a tick on a printed box closes
   one.
3. `@claude` never runs anything until they write `@approved` by hand.

`CONVENTIONS.md` is the full ink language; `README.md` the commands.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `install.sh`: `ensurepip is not available` | `sudo apt install python3-venv` (or `python3.12-venv`), then rerun. |
| `systemctl --user`: `Failed to connect to bus` | No user session in this shell (common over `su`). Log in as the user over ssh directly, or `export XDG_RUNTIME_DIR=/run/user/$(id -u)`. |
| `ozu pull`: `didn't find section in config file` | The remote name in `OZU_RCLONE_REMOTE` doesn't match `rclone listremotes`. |
| The first run read old notebooks | The export folder was not empty. Point `OZU_RCLONE_REMOTE` at a folder holding only what should be read; pages already read are never read again. |
| A test page became "annotation", not notes | The notebook's name starts with "Ozu ". Rename it on the tablet and export again. |
| `ozu pull` copies nothing | Wrong folder: `rclone lsf gdrive: --max-depth 2` and fix the path. A space in the folder name is fine, unquoted. |
| `ozu run` read the page but no action appeared | The export was greyscale, or the red was faint. Re-export in colour; the page is new only if it changed, so write a fresh test page. |
| `authentication_error` on the first run | The key is wrong or has no credit. Check the console; re-paste in `.env`. |
| Edition never reaches the tablet | The sender is not on Amazon's approved list, or `OZU_KINDLE_EMAIL` is the account email instead of the device's `@kindle.com` address. `ls ~/ozu-data/editions` shows it was built. |
| `@claude` proposal never arrives | `ozu builds` and `journalctl --user -u ozu-tick -n 50`. Usually: the project is not in `OZU_CLAUDE_REPOS`, or the headless login expired (step 9.1). |
| Timers stop after logout | Linger is off: `sudo loginctl enable-linger $USER`. |
| A value in `.env` acts strangely | No comments after a value: neither systemd nor the loader strips ` # …`. |
