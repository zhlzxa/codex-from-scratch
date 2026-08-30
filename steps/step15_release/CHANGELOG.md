# Changelog

What changed, for someone deciding whether to upgrade. Not a commit log —
`git log` already exists and nobody reads it for this.

Two kinds of breaking change are listed separately, because they break
different people:

* **Breaking (API)** — code that imports `minicodex` stops working.
* **Breaking (files)** — a `.minicodex/` directory written by an older version
  stops being readable. This one is not covered by the 0.x escape hatch: a
  user's files do not read the version number.

## 0.1.0 — 2026-08-16

The first release with a version number that means anything. Everything before
this called itself `0.0.1`, including the release that changed the recording
format.

### Breaking (CLI)

* `minicodex forget N` is now `minicodex rules --forget N`. The old spelling
  works and warns; **removed in 0.3.0**. `forget` acquired a second meaning in
  0.0.1's memory work (`memory --forget-all`) and the two erase unrelated
  things.
* `--yes` is now `--dangerously-approve-all`. Old spelling works and warns;
  **removed in 0.3.0**. It reads as the answer to the question on screen and
  it is the answer to every question the run will ask.
* `minicodex` with no subcommand now prints help on **stderr** and exits **2**
  instead of printing it on stdout and exiting 0.

### Fixed

* A hanging sub-agent was not stopped on Python 3.10 — `asyncio.wait_for`
  raises a different class there, and the handler caught the builtin. 3.10 is
  in the test matrix as of this release, which is how it was found.
* The retry loop asked for a backoff after its last attempt: a failing call
  slept for up to 30 further seconds and announced `attempt 5 of 4` before
  giving up.
* `minicodex replay` crashed with `KeyError: 'attempt'` on a recording made
  before retries existed, instead of replaying it. Recordings that old are now
  read; the missing field means "one attempt", which is not in doubt.

### Added

* `minicodex --version` prints the environment a bug report needs, ending with
  the path of the recording that reproduces the last run.
* The wheel declares a licence, a homepage and an issue tracker, and its long
  description is written for someone who has just installed it.
* `CONTRIBUTING.md`, `SECURITY.md` and an issue template.

### Known

* Recordings written before the 0.0.1 release that added replay support cannot
  be replayed: the tool call arguments were never written to disk. They are
  refused with a message that says so. Eleven of this project's own fourteen
  past versions produce such files.
