"""Durability plumbing shared by every console state file.

`accounts.json`, `sessions.json` and `quota.json` all need the same write:
a temporary file, fsynced, renamed over the target, and the rename itself
made durable by fsyncing the directory.  Three copies of that sequence had
drifted in exactly the part that matters on power loss -- the directory
fsync, which only the threads table kept -- so the sequence lives once now.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write `payload` as JSON such that a crash cannot leave a torn file.

    The rename is atomic with respect to other processes on both platforms
    this runs on; the two fsyncs are what make it atomic with respect to
    power loss.  The directory fsync is best-effort: Windows refuses to open
    a directory, and a console that will not start there is a worse fault
    than losing a second of writes.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


__all__ = ["atomic_write_json"]
