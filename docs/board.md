# The weekly project board

`MASTER.md`, at the root of the personal vault, is a board of what you are
building: what changed this week, what went quiet (and for how many weeks),
and one next step per project. Once a week the 2-hourly cycle rewrites it,
continuing last week's edition, from:

- the notes written since the last edition;
- the open actions (red ink not yet ticked);
- the Builds and where each stands;
- a **development brief**: what happened in code, as facts.

Learning briefs and wiki pages link to the board when they say "this
touches your work", so it is worth keeping current even if you never open
it.

## Turning it on

```
OZU_BOARD=1
OZU_BOARD_REPOS=ozu=~/code/ozu,site=~/code/site#docs/plan.md|TODO.md
```

`OZU_BOARD_REPOS` names the repositories that count, as comma-separated
`name=path` pairs. After a `#`, `|`-separated plan files, relative to the
repository; without one, `ROADMAP.md`, `PLAN.md` and `TODO.md`, whichever
exist. For each repository the brief lists:

- the week's commits (date, short hash, subject; at most 15, merges left
  out), or the date of the last one when there were none;
- whether each plan file changed this week;
- what the plans still list as open: every unchecked `- [ ]` item, and every
  `##` item with the `**Status:**` line under it.

The pipeline reads the repositories with `git`; the board run never does.
Its fence is the vault, read-only (DESIGN.md, invariant 11f).

**Name personal repositories only.** The board is a personal note and is
printed on the Sunday sheet; a work repository belongs on neither
(invariant 2, the wall).

A deployment that already has a brief of its own can use it instead:
`OZU_BOARD_BRIEF_CMD` is a command line, run without a shell, whose standard
output is the brief (cut at 24,000 characters; it gets two minutes). When
it is set, `OZU_BOARD_REPOS` is not read. If the command fails, the board is
written without a brief and says so.

`OZU_BOARD_MODEL` picks the model (empty: the CLI's default).

## When it runs

In the 2-hourly cycle, when the board's `updated` date is seven days old or
more (or there is no board yet). One attempt a day: a failed run (a bad
reply, a timeout) waits until tomorrow. `ozu board` rewrites it now if a
week old; `ozu board --force` regardless. The command works even with
`OZU_BOARD=0`, which only keeps the cycle from running it.

## Correcting it on paper

On Sunday, when the board has a new edition, the evening edition's "Wiki
this week" sheet opens with the board's one-line summary and its id
(`page: …`). Write beside it and re-export: your ink comes back as a note,
and the next edition applies it.

## The file

The board keeps the id it has (a vault with an existing `MASTER.md` keeps
its own; a new one is `master-project-board`), its title and its `created`
date. `type: Board`, `source: board:weekly`. Every id it cites that exists
becomes an edge; an id that does not exist loses its brackets.
