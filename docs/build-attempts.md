# 116 relaunches of one Build — 2026-09-20/21

Between 20:36 on 2026-09-20 and 06:31 on 2026-09-21, the paper-build loop
launched build `B-260916-2824` (a three-part feature for an
app, r2, opus) **116 times**. Nothing was built.

One headless run leaves one transcript, so the count is exact, and so is the
split between the runs that did something and the runs that did not:

```
$ ls -l ~/.claude/projects/-home-user-ozu-data-agent-B-260916-2824/*.jsonl \
    | awk '{print $5}' | sort -n | uniq -c
    113 108695        # the prompt, then one synthetic turn: refused
      1 1235861
      1 2465451
      1 2701840
```

Three launches really ran — 321, 332 and 120 turns, ~139M tokens between
them. Each one swallowed the whole claude.ai usage window and died mid-build.
The other 113 fired on the five-minute timer, bounced off the already-spent
window in 2-3 seconds, and left. Every one of the 113 ends the same way:

```json
{"content":[{"type":"text","text":"You've hit your session limit · resets 6:30am (UTC)"}],
 "error":"rate_limit","isApiErrorMessage":true,"apiErrorStatus":429,
 "quotaLimits":{"status":"rejected","resetsAt":1789972200,"rateLimitType":"five_hour"}}
```

and a `cost-state` line reporting `totalDuration: 3036` ms.

The vault counted it too, because every state change commits:

```
$ git -C ~/ozu-data/vault log --oneline --since="2026-09-20 20:00" \
    | grep -c "B-260916-2824: auth failed, still approved"
115
```

115 round trips through `approved → executing → approved`, plus the 116th
left mid-flight in `executing` when the timers were stopped. That commit
pair is the loop, written down 115 times, and nobody was looking.

## Why nothing stopped it

**`OZU_MAX_BUILD_RUNS` was never exceeded.** It is checked inside one tick's
loop over queued work (`builds.run_pending`), so every tick got a fresh
budget of 2 and used 1. A per-tick cap on a five-minute timer is not a
budget; 116 ≤ 2 × 116.

**A session limit came back as `AuthError`.** `agent._run_claude` classified
it there because the CLI reports it as an ordinary `api_error` whose story is
only in the message — the same shape as an expired OAuth session.
`builds._execute` answers `AuthError` by putting the Build back to
`approved`, which is right for a broken login (a human fixes it, the work
resumes) and exactly wrong for a spent window (nothing fixes it but time, and
the timer asks again in five minutes).

**Nothing was recorded.** `build_events` had zero rows for this Build and the
`builds` row said `status='executing'`, `branch=NULL`. The status is written
before the run and the outcome after it, so a run killed mid-flight — which
is what a session limit hours into a build amounts to — left the Build
looking like work in progress forever. `_sweep_stale_executing` would have
swept it to `error`, but it only runs at the start of the next builds phase,
and the timers were stopped first.

## The fix

A Build carries its own lifetime budget in its frontmatter, and the whole
phase stands down when the window is gone.

| | what it bounds | default | env |
|---|---|---|---|
| `build_attempts` | launches that consumed a usage window | 3 | `OZU_MAX_BUILD_ATTEMPTS` |
| `build_deferrals` | launches an already-spent window turned away | 8 | `OZU_MAX_BUILD_DEFERRALS` |
| `meta.builds_retry_after` | when the builds phase may run again | the CLI's own reset time | — |

Four things make it hold:

1. **The counter is written before the subprocess starts** and committed with
   the note. A run killed by an OOM, a reboot or a stopped timer has already
   spent its attempt. This is the part that survives a crash, and it is why
   the launch is also recorded in `build_events` as `kind='trace'` at the
   same moment.
2. **A spent window is told apart from a broken login** —
   `agent.UsageLimitError`, matched on the CLI's message (the same reason
   `understand._unavailable` matches the credit-balance 400 on its message,
   invariant 10b), carrying the reset time it names and whether the run
   consumed a window. `spent` comes from the CLI's own numbers: over one turn
   or over a minute of wall clock. Measured against this night — the 113
   refusals reported no turns and 2-3 seconds, the three real runs hundreds
   of turns and 20-40 minutes.
3. **A run that consumed nothing is refunded** and counted as a deferral
   instead. Deferrals are capped too, or a Build waits forever on resets that
   never help it. A plain `AuthError` is refunded and counts nothing: it is
   free, it stops the phase immediately, and notes have always stayed queued
   across a broken login.
4. **Past either cap the Build is `exhausted`** — a status no phase of the
   tick selects, and one that a fresh `@approved` answers with a line of mail
   rather than a run. The ways back are both deliberate: `@input` (a revision
   is a different plan, and only handwriting queues one, so it cannot loop),
   or `ozu builds reset <id>` by hand.

`build_events` now carries two kinds. `mail` is what the owner reads in the next
edition; `trace` is the run ledger — every launch, every outcome — and is
never printed. 116 relaunches would have been 116 lines of paper.

Pinned by six tests in `tests/test_builds.py`, of which
`test_a_repeatedly_failing_build_cannot_exceed_its_cap` is the one that runs
forty ticks with the window coming back each time and asserts three launches.
