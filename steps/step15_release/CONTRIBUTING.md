# Contributing

**Bug reports, reproductions and analysis: yes, please.**
**Pull requests: not accepted.**

That second line is unusual enough to deserve the reasoning rather than just
the rule, because the rule is easy to read as rudeness and it is not.

## Why no pull requests

codex makes the same choice, and the arithmetic is the same one. Reviewing a
change to an agent means deciding whether it is correct, and for most of what
this program does "correct" is not visible in the diff:

* A change to a tool description, a system prompt or an error message is a
  change to **model behaviour**. The only way to know what it did is to run a
  task set against a provider — twice, with and without — and read the samples
  that got worse. Chapter 14 of the book this came from is about how expensive
  that is and how easy it is to fool yourself doing it.
* A change to the approval or sandbox code is a security change, and the
  failure mode of getting it wrong is somebody else's repository.
* A change anywhere near context, compaction or memory alters **every future
  request** a user makes, silently, in a direction no unit test observes.

An unpaid maintainer who merges those on reading is not reviewing, they are
guessing on a stranger's behalf. Declining the whole category is more honest
than merging some of them badly, and it costs a contributor an afternoon
rather than costing every user a regression.

## What is genuinely more useful than a patch

1. **A recording.** `minicodex --version` ends with the path of the file that
   reproduces your last run. `minicodex replay <that file>` re-runs the whole
   conversation offline, against any version, with no API key. A report with
   one attached is a report that can be turned into a test in an afternoon; a
   report without one is a conversation.
2. **A measurement.** "This got worse" with two arms and a sample count beats
   a fix, because the fix is the cheap half. See `probe_eval.py`.
3. **A fault nobody had listed.** Roughly half of what this project has fixed
   was found by somebody deliberately looking for silence, not by an error.

## Before you open one

* Read the recording first. It contains the contents of every file the agent
  read. Credentials are redacted by key name; source code is not.
* Say which version. `minicodex --version` prints it, and 0.0.1 is not enough
  information — that number was on fourteen different builds.

## Security

Do not open an issue. See `SECURITY.md`.
