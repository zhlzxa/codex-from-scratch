# Comment and Docstring Style

This repository was forked from a tutorial whose comments carried the book's
narrative — chapter numerals, fault numbers, measurement tables. That content
is writing, not information a maintainer needs next to the code. This guide
is the rule for the one-time conversion, and for every change after it.

**Only three kinds of content stay in code:**

1. **Constraints and invariants** — rules the code cannot express, whose
   violation causes real damage. Examples: why the quota claim happens
   synchronously before `create_task`; the semantic difference between the
   `_UNSET` sentinel and `None` for a sandbox; why audit write failures must
   propagate; why `asyncio.TimeoutError` (not the builtin) is required on
   3.10.
2. **"Why not the tempting alternative"** — name the alternative and give
   one sentence of reason, not a paragraph. Examples: `to_thread` vs a
   direct blocking call; `hmac.compare_digest` vs `==`.
3. **Non-obvious mechanism facts** — write them only where reading the code
   would lead to a wrong conclusion. Examples: bwrap reports its own setup
   failures with `exit 1`; `proc.wait()` hangs unless the abandoned pipe is
   closed first.

**Moved out of code (into `docs/history.md`):**

- Chronological narrative ("Chapter 12 dodged it by…", "chapter 20 built
  Owner for it and could not use it").
- Fault-number indexes (F05-04, F17-11, LIVE-01…) and lists of which tests
  pin what.
- Measurement data (7x tokens, 0/12 vs 12/12 hangs, 830,400 levels, 2/3
  violation rates).
- Per-mechanism comparisons with codex (async spawn vs sync loop, the
  AGENTS.md role difference, missing transport fallback, etc.).

**Format discipline:**

- Module docstring ≤ 15 lines: what this is, where its boundary is, done.
- Function docstring ≤ 6 lines: the contract only (argument semantics,
  return, failure modes). Skip it when the signature already says it.
- Inline comments ≤ 2 lines; longer than that, promote to a docstring or
  delete.
- Never reference "Chapter N" or a fault number; cross-file references use
  `module.symbol`.
- Migration rule: where an old comment mixes narrative with a live
  constraint, keep only the constraint sentence and move the rest to
  `docs/history.md` (organised by topic; fidelity is not required).
- The only places that carry the historical narrative are `docs/history.md`
  and the deployment handbook (`README-LIVE.md`).
