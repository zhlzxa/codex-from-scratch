# minicodex — step -1: a package that installs

No agent yet. A package that installs, runs, tests and ships.

```bash
uv sync --all-extras
uv run minicodex --version
uv run pytest
```

`src/minicodex/prompts/system.md` is unused by any code path. It is here so
that a broken wheel fails a test instead of failing a user.
