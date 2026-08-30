# minicodex

A small coding agent: it reads files, runs commands, edits code, and asks
before doing anything it was not allowed to do.

```bash
pip install minicodex          # or: uv tool install minicodex
minicodex --version
minicodex ask "what does src/app.py do?" --provider openai
```

By default it may **read**, and nothing else. Anything that writes or reaches
the network is a question:

```bash
minicodex ask "add a test for parse_config and run it" \
    --provider openai --sandbox-mode workspace-write
```

## Where it puts things

Everything it keeps lives in `.minicodex/` in the directory you ran it from,
and nothing leaves that directory:

| Path | What it is |
|---|---|
| `.minicodex/sessions/` | one file per conversation; `minicodex ask --resume last` continues one |
| `.minicodex/recordings/` | what was sent to the model and what came back — **the file to attach to a bug report** |
| `.minicodex/rules.json` | approvals you told it to remember; `minicodex rules` lists them |
| `.minicodex/memories/` | off unless you pass `--memory`; `minicodex memory` shows it, `--forget-all` deletes it |

`.minicodex/recordings/` contains the contents of every file the agent read.
Credentials are redacted by key name; your source code is not. Read one before
you attach it.

## Reporting something

```bash
minicodex --version           # paste this
```

The last line of that output names the recording of your most recent run.
Attach it: `minicodex replay <that file>` re-runs the whole conversation
offline, against any version, with no API key — which is the difference
between a report somebody can act on and one they cannot.

Issues: <https://github.com/example/minicodex/issues>

**Bug reports and analysis are welcome. Pull requests are not accepted** —
see `CONTRIBUTING.md` for why, and for what to send instead.

## Compatibility

Python 3.10+. Version 0.x: the Python API may change in any release. The files
in `.minicodex/` are treated more carefully than that — a release that cannot
read one written by an older version is a breaking change, and `CHANGELOG.md`
says so under **Breaking**.

MIT licensed.
