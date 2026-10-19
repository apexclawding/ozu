# Ink conventions

The writing language the pipeline understands. Four ink colours and one tag
character; everything downstream — actions, routing, connections — comes from
these marks.

**This file, `prompts/vision_prompt.md` and `prompts/annotation_prompt.md`
must change together.** This is the human-readable statement of the
conventions; the prompts are what enforce them — the vision prompt on
notebook pages, the annotation prompt on "Ozu " documents coming back.


## The four inks

| Ink | Means | What the system does |
|---|---|---|
| **Black** | Normal capture — the default | Becomes note text. Most of what you write. |
| **Red** | Action | The *only* ink that creates an action item. Lands in open actions and the weekly digest. |
| **Blue** | Idea worth developing | Most likely to be linked to older thinking and resurfaced later. |
| **Green** | Personal / family | Routes to `people/` rather than a project. |

Red is exclusive: nothing else on a page becomes an action, however
task-shaped it reads. "Decide the name" written in black stays in the note
body. This is deliberate — if anything task-shaped became an action, red would
stop meaning anything.

## Closing an action: the checkmark

Red creates; **✓ closes**. On any *fresh* Inbox page, draw a checkmark and
rewrite the task's words — `✓ service the bike` — in any colour.
The nightly run matches it against your open actions and marks the best
match done; the digest stops nagging about it.

Never tick the original *notebook* page: a page is only read once, and
editing it makes a near-duplicate page instead of an update. Fresh page,
checkmark, done — or tick the printed action line on any "Ozu " document,
which is built for exactly that (next section).

## Annotating what comes back

Every Ozu document opens with a **cover**: the day in large type, what kind of
document it is, and one line of what is inside — so the Scribe's library shows
you which is which without opening any of them. It repeats nothing; skip it
and you have missed nothing. It is an ordinary page otherwise: ink on it is
read like ink anywhere else, and a Build's id is printed on it, so a decision
written on the cover routes home like one written on page 3.

Every document the pipeline sends you is named "Ozu …": the two daily
**editions** (Ozu Morning at 07:00, Ozu Evening at 20:00, each several
sections: what your ink became, Build results, the Learn sheet, the weekly
digest and the wiki sheet on Sundays, and a *Safe to delete* list), plus Build proposals and research briefs as
documents of their own. Write on any of them and re-export to Drive; the annotation
pass reads the ink and leaves the printed text alone — your own output
never rounds a trip back into the vault.

- **Tick a printed `☐` action line** — any colour — and that action
  closes, receipt in the next edition. The one place a ✓ needs no rewritten
  task words: the print already carries them. A `☑` line is one that is
  already closed; nothing to do. (Some documents print
  `[ ]` for the open box — tick those exactly the same way, they still work.)
  Every tick is also checked against the page itself: if a line was read as
  ticked but no ink sits beside its box — or ink sits beside a box and was
  not read — nothing closes, and the next edition prints that line open
  again with the reason. Tick it once more to close it.
- **Red ink creates an action**, exactly as in a notebook. Red creates,
  ✓ closes — the grammar is the same everywhere.
- **Margin writing becomes a note**, filed by @tag or content like any
  notebook page. `@claude` and `@research` work in margins too — annotate a
  build report to open the follow-up Build.
- **On a "Ozu Build" document — or the pages of a Build result inside an
  edition** (they print the Build id in the running header), `@input`,
  `@approved` (+ `@opus` / `@sonnet` / `@fable`) and `@drop` drive the
  build conversation — see "Answering a Build" below.
- **The last section of every edition, "Safe to delete from the Scribe"**,
  is not something to annotate: it names the documents on the device that
  are finished with (a superseded revision, a done Build's proposal, an
  earlier edition, a brief older than thirty days). Delete them from the
  device when you like — export any page you have written on first.
- **On the Sunday "Wiki this week" sheet** (inside the evening edition),
  every concept page prints its id (`page: wiki-…`). Write beside a claim
  to correct it; **draw a line through a claim to drop it** — the one place
  a strike-through means something. Your ink comes back as a note, and the
  next cycle's wiki run applies it: your ink outranks every brief the page
  was compiled from.
- Underlines and stars alone are emphasis, not content — add words when you
  want a mark to mean something. A strike-through anywhere but the wiki
  sheet is emphasis too.

Annotate across re-exports freely: pages are deduped on their image, so a
page you add ink to is re-read (old ticks are recognised as already closed
and stay quiet) and untouched pages cost nothing. One caveat: a re-read
page's *margin notes* are written again — finish a page's marginalia before
exporting when you can.

## The @ tag

`@project` anywhere on the page files the surrounding content under that
project. Known projects:

the ones you list in `OZU_PROJECTS` (say `@garden` · `@house`), plus
`@ideas` (the fallback) and `@people`.

Every repo wired for `@claude` is automatically a project tag — with
`kettle` mapped, `@kettle` files under the kettle topic, and
`@kettle @claude` opens a Build in its repo.

Untagged pages are routed by content, landing in `ideas` when the model can't
tell. Place the tag beside the thinking it belongs to — on a page covering two
projects, use two tags.

## The @claude directive — a conversation, not a trigger

`@claude` is not a project, and it no longer builds anything unattended.
Written beside an idea, it opens a **Build**: within about five minutes a
document named `Ozu Build B-260906-7f3a r1` lands on the Scribe, your
idea's title on its cover band — what the
pipeline understood, a precise requirement, a step-by-step plan with the
files it expects to touch, risks, what it will *not* do, and open questions.
Nothing runs until you answer that document in ink.

- The content still routes by its real tag: `@kettle @claude` files the note
  under `kettle` *and* proposes a Build in the kettle repo.
- **No project tag?** It still proposes — under `ideas`, reading only your
  vault, with "which repo does this belong to?" as the first open question.
  Answer it with `@input kettle` (or any mapped project) on the proposal.
  There is no unattended path left; every `@claude` goes through paper.
- Red ink is untouched by this: `@claude` opens a conversation, never an
  action. One Build per note — a fresh idea is a fresh page.
- **A project in `OZU_ISA_TOPICS`** gets its proposal in a different shape,
  an ISA: the ideal state, the decisions taken, and numbered steps that each
  end with the check that proves them. You answer it with the same words,
  and the result reports every step as pass, fail or not verified — so the
  plan you approved is also the test the result is graded against.

## Answering a Build — the exact words

Every page of a Build PDF prints its id (`Ozu Build B-260906-7f3a r2`) in
the header, and so do the pages of a Build result inside an edition
(`Ozu Build B-260906-7f3a result`); that printed id is what routes your ink
back to the right Build, so annotate any of those pages, in any colour,
and re-export to Drive. The file is named for the idea first — `Ozu Build
r2 - Week-ahead note (B-260906-7f3a)` — so the library grid shows the
revision and the title, not an id.

| Write | What happens |
|---|---|
| `@input` + your words | Your answers and changes are folded into the requirement and plan; revision `r2` (`r3`, …) arrives on the Scribe. Loop as often as you like. |
| `@approved` | The Build executes as proposed — branch `build/<id>`, pushed (never main), PR opened, and the result comes back as a section of the next edition with the report and a printed `☐ Review PR #…` line. |
| `@approved @sonnet` | Same, choosing the builder: `@opus` (default), `@sonnet`, or `@fable` for the long, hard ones. |
| `@input … @approved` | Both on one page is fine: the input is applied first, then it executes — the result says so. |
| `@drop` | The Build closes, nothing executes; one confirmation line in the next edition. |

The grammar around it is unchanged: margin writing that is *not* a directive
becomes normal notes filed under the project and linked to the Build; red
ink still creates actions; a tick on the printed `☐ Review PR …` line closes
it. The decision box on the document prints these same words — printed tags
do nothing, only your handwriting counts.

**One execution per approval, ever.** A second `@approved` on an executed
Build answers with a line in the next edition, never a second run. A failed
Build (`error` in the next edition) is the exception: a fresh `@approved`
on its page re-arms it — up to three launches in the Build's whole life.
After that the edition says the Build has stopped, and `@approved` only
earns a line: `@input` is the way on, because a new plan deserves a fresh
try and the old one has had every one it gets.

A Build that was **split** — carved into smaller Builds because it could not
finish in one sitting — is retired the same way. Its document stays on the
device and is still worth reading, but ink on it changes nothing: the next
edition answers with the ids of the Builds that took over its work, and
those are the documents to annotate.

A page the pipeline cannot match to exactly one
Build — say the file name and your written id disagree — changes nothing:
the ink is filed as a `needs_review` note and the next edition tells you,
because attaching ink to the wrong Build is the one failure this design
refuses.

## The @research directive

`@research` is the second directive: written beside a question, it asks for
an overnight research brief. Claude researches with web access while reading
your vault — the brief cites your own earlier notes by id alongside its web
sources — and the outcome lands twice: a `Research:` note in the vault, and
a typeset PDF sent to the Scribe when Send-to-Kindle is configured.

- Works for any topic — no repo mapping needed, unlike `@claude`.
- Same grammar: routes nothing, creates no action, one run per note ever.
- A vague question gets the most useful interpretation, named in the brief's
  TL;DR — sharpen the ink if the answer sharpened the wrong thing.

## Videos you save — the Learn sheet

With [`capture/`](capture) running, a video saved to your learning playlist
arrives as a transcript note, and the next cycle
writes a brief from it: takeaways, techniques, where it touches your own
notes (cited by id), one exercise and one question. The brief rides the
**Learn** section of the evening edition, then comes back 2, 7 and 30 days
after the first time — four meetings with each idea, then silence.

- The sheet prints ruled lines under the question: answer it in ink, and the
  answer becomes a note like any margin writing.
- It prints no `☐`. Red ink creates an action on it exactly as anywhere else.
- A video too long or with no captions is listed once as skipped, with why.

## Page anatomy

One page becomes several notes, one per distinct idea. A blank line between
unrelated thoughts is what makes the split clean.

Label boxes and arrows in sketches — named elements become a mermaid diagram,
unlabelled ones become "a shape".

Confidence below 0.8 flags the note `needs_review` and surfaces it in the
digest. Illegible passages come through as `[illegible]`, never invented.

## Work content — the hard rule

Never write employer content in a notebook you export: internal names and
figures, personnel moves, confidential plans, named colleagues in a work
context.

The governance wall triggers *after* the page has reached the API for
classification. It protects the vault, index, edges and MCP; it cannot un-send
the page. Only not exporting does that. And a page is walled **in full** — one
work line buries every personal note sharing that page.

Keep `Inbox` personal-only; keep work in a notebook that is never exported.

## Gotchas

- **Don't edit a processed notebook page.** Dedup is on the page image; an
  edited page is a new page, so you get a second note and the first is not
  updated. (Annotating "Ozu " documents is the exception — that loop is
  built for re-reading.)
- **Export in colour.** Grayscale discards every instruction above.
- **Re-exporting a whole notebook is free** — only unseen pages cost anything.
- **Print names and domains.** Cursive invented words are the hardest case.
- **The vault is yours to edit.** Fixing a transcription by hand is expected.
