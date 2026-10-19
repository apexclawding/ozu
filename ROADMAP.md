# Roadmap

Open items, each with the reason it is not done yet.

## A weekly project board, rewritten from what actually happened

**Status:** next.

Every week a read-only run rewrites a `MASTER.md` board in the vault: what
is being built, what went quiet, what is next, citing notes and Builds by
id. It writes the board from the week's notes, the open actions and a
development brief, and ink on the Sunday sheet corrects the next edition,
the same loop as the wiki.

Why it waits: the development brief is the hard part. The obvious source,
every git repository on the machine and the coding sessions that touched
them, assumes one person's layout of one box. Before it can ship, the brief
needs a configuration that says which repositories count and where their
plans live. The wiki needed neither, which is why it shipped first.

## Extension seams

**Status:** open.

Ozu has no plugin points yet, so a deployment that wants features of its
own has to patch the core, and then every fix to the core has to be made
twice. The seams it needs: edition sections a plugin can add, headless run
kinds a plugin can register, and note types beyond the built-in ones.
Ledgers must still never be dropped (`DESIGN.md`, invariant 1), so a plugin
adds tables and never migrates Ozu's.

Why it waits: seams are easiest to get right against a second real
deployment. Designed for one, they would bake in its shape.
