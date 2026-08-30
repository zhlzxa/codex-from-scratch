---
name: add-a-fault
description: How to add a new entry to FAULTS.md, including the numbering and the "found by" symbols. Read this before writing anything into FAULTS.md.
---

# Adding a fault to FAULTS.md

A fault goes in the catalogue only after it has actually happened. A
hypothesis that was never reproduced is not an entry; if it was checked and
did not reproduce, it is an entry that says so, in those words.

## The number

`FNN-MM`, where `NN` is the chapter that found it and `MM` counts within
that chapter, starting at 01. Numbers are never reused and never renumbered
-- the chapters cite each other by number, and a renumber silently rewrites
those citations.

## The row

| ID | Fault | Found | Root cause and fix |

`Found` is one symbol:

| Symbol | Means |
|---|---|
| 🔴 | crashed |
| 🟡 | silent: a wrong answer nobody was told about |
| 🔵 | only showed up after a long run |
| 🟢 | a boundary test found it |
| 🟠 | the tooling that measures something was measuring something else |
| 🟣 | found by reading the code |
| ⚪ | found by a linter or by a surviving mutation |
| ⚫ | reasoned about before it happened, or reported by a user |

## The two rules people get wrong

1. **Say the number.** "Often" is not a finding. "3 of 18 runs" is.
2. **Say what did not reproduce.** A chapter where every predicted fault came
   true is a chapter that did not test its predictions.

Update the `Total:` line at the end of the file and the distribution table
under it in the same commit. They are checked by hand and they drift.
