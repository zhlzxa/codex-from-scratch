# Security

Report privately: **security@example.invalid**. Not an issue, not a pull
request, not a discussion thread.

## What counts as a security bug here, specifically

This program runs commands and edits files on behalf of a model that reads
untrusted text. Three of its boundaries are the ones worth attacking, and all
three have failed before:

* **The approval gate.** Anything that reaches a subprocess without passing
  `approval.decide()`. Chapter 5 of the book this came from deleted a second
  route to `ShellSession.run()` for exactly this reason; `tests/test_boundaries.py`
  asserts it stays deleted.
* **The workspace boundary.** Anything that reads or writes outside the root:
  `../`, a symlink, an absolute path that survives `paths.resolve()`. This one
  shipped broken for a chapter (F04-12) with 98 tests green.
* **Injection through content.** A file, a tool result, an MCP server's output
  or a memory entry that is treated as an instruction rather than as data.
  `memory` is the sharpest of these — it is written by a model, read by a
  model, and injected into every subsequent request.

## What is out of scope

The sandbox is **an approval gate, not a sandbox**. There is no OS-level
isolation: a command the user approves can do anything the user can do. That
is documented rather than defended, and "an approved command did something
bad" is not a vulnerability in this program.

## What you will get

An acknowledgement within a week, and a fix or an explicit "will not fix" with
reasons. If it is a "will not fix", it goes in `SECURITY.md` under known
limitations, because an undocumented known limitation is the same thing as a
secret.
