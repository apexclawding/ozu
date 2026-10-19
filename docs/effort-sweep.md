# Effort sweep — 2026-08-25

The question was whether the vision and annotation calls need `effort: high`,
and specifically whether annotation pages could run cheaper: they are mostly
blank paper with a few words of ink, so the guess in the build queue was that
a lower setting would do.

**The guess was wrong, and backwards.** Annotation is the call that most needs
high effort. Both defaults stay at `high`.

Run against real handwritten pages, at
`low`, `medium` and `high`, on `claude-opus-5`. Walled pages excluded.

## What it costs

Output tokens per page (this includes thinking, which is what effort moves):

| page | low | medium | high |
|---|---|---|---|
| ink — one note, one ✓ | 368 | 362 | 377 |
| ink — two notes, six actions | 706 | 777 | 846 |
| annotation — mail, one margin note | 515 | 791 | 899 |
| annotation — research brief, three margin notes | 1323 | 1566 | 2079 |

So `high` costs roughly 20–75% more output than `low`, on a base of a few
hundred to two thousand tokens. At Opus 5 rates that is **under two cents a
page**, against an input cost dominated by the page image either way. The
whole latency range is 5–25s per page, on a pipeline that runs every two
hours. There is very little to win here.

## What it breaks

On the annotated **mail** page, `low` reported a completion that was never
written by hand:

```
low     completions=['Buy garden hose']      ← wrong
medium  completions=['Buy garden hose']      ← wrong (first run; right on the second)
high    completions=[]                       ← right, both runs
```

`Buy garden hose` is on that page because the mail **printed** it, in the "Your
checkmarks" receipt: `[x] Buy garden hose`. It is the system reporting a tick
the owner made days earlier. At low effort the model read its own printed receipt
back as a fresh handwritten tick.

This is the exact distinction `annotation_prompt.md` exists to enforce —
printed text is context, handwriting is content — and it is the first thing to
collapse when effort drops. That makes sense in hindsight: on an annotation
page the hard work is not reading dense ink, it is *declining* to read the
print surrounding it, and declining is the part that needs thinking.

Why it did not cause damage this time: the action was already closed, so
`matches_closed_action` treated it as a re-read and stayed quiet. The
dangerous case is the one that has not happened yet — mail also prints
**misses in red, with the nearest open actions named**. A low-effort read of
that page could report one of those printed candidates as a completion and
close an action that was never ticked. Receipts exist so a ✓ never silently
does nothing; a fabricated ✓ closing real work is the same wound the other way
round.

`low` also fabricated on ink: it transcribed the hose completion as
`"...a 30m <brand> (<other brand>?) reel"`, inventing a parenthetical guess at a
brand name that neither other level produced.

## Decision

Both stay `high`. `OZU_VISION_EFFORT` and `OZU_ANNOTATION_EFFORT` exist so the
setting is visible and changeable, not because either should be lowered.

If this is revisited, the test that matters is not transcription agreement —
it is **completions on an annotated document that prints action text**. Any
level that invents a completion there is disqualified regardless of what it
saves. Re-run on a few such pages and read the completion text, not the count.

## Sample size

Four pages, two of each kind, two runs of the divergent case. Small. The
annotation finding reproduced on both runs at `low`; `medium` failed one of
two, which is enough to distrust it and not enough to characterise it.
