"""The audit log: who made this server do what, and when.

A multi-user server that runs commands and edits files owes its operators an
answer to "who did that" that does not depend on anybody's memory.  The
rollout files record what the *agent* did; the recording records what the
*model* saw; neither records the account driving the turn, and neither is
written to be read by anybody but the agent itself.

**One JSON object per line, appended.**  The same shape as `history.jsonl` in
codex's `message-history` crate: a single `write` of one line, opened in
append mode, so a reader that arrives mid-write sees a prefix of the file and
never a torn record.  This is deliberately *not* the accounts.json pattern --
atomic-replace writes a new file on every change, which is right for a file
whose latest content is the point, and wrong for a log whose entire content is
the point.

**Write failures are fatal to the turn, on purpose.**  A dropped audit record
is not "less logging", it is a hole in the only timeline an operator gets after
the fact -- and the moment a hole is possible, every quiet record becomes
unprovable.  `append` therefore lets the OSError out; `run_turn`'s existing
catch-all turns it into a visible error on the thread's socket, which is the
honest failure mode for a system that would rather stop than lie.

**What is in, and what is not.**  Login, bootstrap, turn start (with account
and workspace), each approved permission request, each command with its
outcome, each patch with its file list, and the server-side refusals -- quota
exhaustion, login throttling, workspace bounds.  Not the *content* of
conversations or tool outputs: the rollouts already hold those, this file is
the index of who made them happen, and duplicating bodies here doubles the
blast radius of a directory nobody was meant to read in full.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

#: The event names actually written.  Kept as constants so a typo in a caller
#: is a `NameError` at import time rather than a mystery string in the file;
#: a caller spelling it differently than the audit file's readers expect is
#: the failure mode here.
EVENT_LOGIN = "login"
EVENT_BOOTSTRAP = "bootstrap"
EVENT_TURN_START = "turn_start"
EVENT_COMMAND = "command"
EVENT_COMMAND_DONE = "command_done"
EVENT_PATCH = "patch"
EVENT_PERMISSION = "permission"
EVENT_DENIED = "denied"


class AuditLog:
    """Append-only JSONL, one line per event, one file per server."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, actor: str, **fields: Any) -> None:
        """One record. Raises on failure; never swallows.

        `actor` is the account key, or `"-"` where there is none yet (a failed
        login attempt has an account name but no session; the name goes in
        `detail`, the actor stays anonymous).
        """
        record = {"ts": round(time.time(), 3), "event": event, "actor": actor, **fields}
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        # `open` per append rather than a held handle: this file is read by
        # humans and log shippers, on a server that may be restarted by anyone
        # with terminal access, and a held handle across all of that is one
        # missed close away from records stuck in a buffer that never flushed.
        # One `os.write` at a time keeps an append atomic against other
        # writers on POSIX; this process is the only writer either way.
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict[str, Any]]:
        """Every record, oldest first. Used by tests and by operators' eyes."""
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        with open(self.path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return records

    def __len__(self) -> int:
        return len(self.read_all())


__all__ = [
    "EVENT_BOOTSTRAP",
    "EVENT_COMMAND",
    "EVENT_COMMAND_DONE",
    "EVENT_DENIED",
    "EVENT_LOGIN",
    "EVENT_PATCH",
    "EVENT_PERMISSION",
    "EVENT_TURN_START",
    "AuditLog",
]
