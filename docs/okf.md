# The vault as an Open Knowledge Format bundle

Ozu's vault is an [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format)
v0.2 bundle: a directory of Markdown files with YAML frontmatter, laid out so
an agent can find what it needs in two reads. OKF standardises two things,
and Ozu adopts both.

1. **Layout.** `index.md` in every folder lists what is there; `log.md` at
   the root is a dated history. Both names are reserved.
2. **Frontmatter.** `type` is required; `title`, `description`, `tags` are
   recommended; v0.2 adds provenance as queryable fields: `generated: { by,
   at }`, `sources: [...]`, `status`, `verified`.

OKF does not prescribe a taxonomy of types, a link syntax, a storage layer or
a runtime, and consumers must tolerate unknown keys. So Ozu's own keys
(`id`, `source`, `related`, `related_why`, `needs_review`, the `claude:`/`research:` run
ledgers, the `build:` state) ride along as extensions.

## What Ozu writes

| field | value |
|---|---|
| `type` | Note (a handwritten page) · Voice Memo · Transcript · Learning · Research · Build · Build Report · Concept (a wiki page) · Guide, derived from the `source:` prefix by `okf.type_for` |
| `description` | one sentence; the index line |
| `generated` | `{ by: ozu/<model>, at: <ISO-8601 UTC> }` |
| `sources` | the page image, or the note that asked |
| `status: draft` | set when transcription confidence was below 0.8 (`needs_review`) |
| `related_why` | why each link in `related` (or each `[[id]]` in the body) exists: one sentence for every link, or a map id → reason. An Ozu extension; the db's edge reasons derive from it, so a rebuild keeps them |

A note's filename is its `id`, so `[[id]]` wikilinks resolve in Obsidian and
in any tool that follows the same rule.

## Why the indexes are derived

`index.md` and `log.md` are **regenerated from the notes** by `ozu index`
and after every write, never appended to. This is the single-writer answer to
a real problem: once two producers append to the same registry with no lock,
it drifts. With derived views, anything may *add a note* (the pipeline, the
MCP `capture` tool, a script on another machine, you in a text editor) and
the indexes catch up on the next write.

## Commands

```bash
ozu okf check            # conformance gate: exits non-zero on a break
ozu okf export [dir]     # a pure-OKF copy (wikilinks → relative Markdown links)
ozu okf viz [html]       # render the export with OKF's reference viewer
```

`ozu okf viz` uses the viewer from a separate checkout of the OKF repository
(`OZU_OKF_HOME`), so its dependencies never enter Ozu's environment.
