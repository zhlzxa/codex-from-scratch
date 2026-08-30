---
name: run-tests
description: How to run this repository's tests, mutation checks and linter. Read this before running or adding any test.
---

# Running the checks in this repository

Everything goes through `uv`. A bare `pytest` or a bare `python` picks up
whatever interpreter is on `PATH`, which on at least two machines is not the
one this project's dependencies are installed into.

## The suite

    uv run pytest -q

Run it from the step directory (the one with `pyproject.toml` in it), not
from the repository root.

## The linter

    uv run ruff check .

Line length is 100. Do not add a `# noqa` to silence a long line that can be
wrapped instead.

## The mutation checks

    uv run python probe_mutations_ch18.py

This edits files under `src/minicodex/` and puts them back when it is done.
If it is interrupted with anything other than Ctrl-C, check `git status`
before committing.

## Adding a test

New tests go in `tests/test_faults_chNN.py` for the chapter that found the
fault, named `test_FNN_MM_what_it_asserts`. A test whose name claims more
than its assertions check is the single most common defect in this
repository's own history -- if the name says "ranks A above B", the fixture
has to be one where B could have won.
