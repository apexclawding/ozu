# Security

## Reporting a vulnerability

Please report it privately. On this repository's **Security** tab, choose
**Report a vulnerability** (GitHub private vulnerability reporting). Please do
not open a public issue or pull request for it, and do not post details
anywhere public until it is fixed.

Say what you found, how to reproduce it, and what it lets an attacker do. A
minimal example is enough.

## What is in scope

Ozu reads handwriting into a private knowledge vault and lets agents act on
it, so the interesting failures are about who can read the vault and what an
agent can be talked into doing:

- The MCP HTTP endpoint and its bearer token: any way to reach it, or to be
  served vault content, without the token.
- The work wall: anything that lets walled (work) content into the main
  vault, its graph, its actions, its outbound documents or the MCP server.
- Headless agent runs and their allowlists: a way for text a run reads (a
  web page, captions, a note) to make it write outside its own directory,
  push to `main`, install code, or read secrets.
- Anything else that could leak vault content or credentials, including logs,
  generated documents and the `capture/` and `transcribe/` parts.

Out of scope: problems that need an attacker who already has shell access as
the user running Ozu, and issues in third-party services or dependencies
themselves (report those upstream).

## What to expect

Ozu is a personal project kept by one maintainer. Reports are read on a best
effort basis: I will acknowledge a report within a reasonable time, tell you
what I decide, and credit you in the fix if you want that. There is no bounty
and no service-level promise.
