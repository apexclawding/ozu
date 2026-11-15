# Roadmap

Open items, each with the reason it is not done yet.

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
