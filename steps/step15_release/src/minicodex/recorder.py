"""A written record of everything that crosses the model boundary.

The first question when an agent misbehaves is always "what did the model
actually receive", and it is unanswerable after the process exits: the history
lived in memory and the memory is gone.  So it gets written down as it happens.

Deliberately dumb.  Append-only JSONL, one event per line, and it never raises
into its caller -- a debugging aid that can crash the program it exists to
debug is worse than no debugging aid at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".minicodex") / "recordings"

# Recordings get pasted into bug reports.  Values under these keys never reach
# disk.  Key-name based, so a credential hidden inside an innocent-looking
# value still gets through; that limit is documented rather than hidden.
_REDACTED_KEYS = frozenset({"api_key", "authorization", "token", "secret", "password"})

_REDACTED = "<redacted>"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in _REDACTED_KEYS else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class Recorder:
    """Append-only JSONL sink for model-boundary events.

    Not a logger.  Logs are prose for a human watching a terminal; this is a
    machine-readable transcript that chapter 14 replays to turn an intermittent
    failure into a deterministic test.
    """

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._seq = 0
        self._lock = threading.Lock()
        if path is None:
            path = DEFAULT_DIR / f"session-{int(time.time())}.jsonl"
        self.path = Path(path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, payload: dict[str, Any]) -> int | None:
        """Append one event.  Returns its sequence number, None if disabled."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            seq = self._seq
            event = {"seq": seq, "ts": time.time(), "kind": kind, "payload": _redact(payload)}
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
                    # The transcript is worth having *because* the process
                    # crashed.  Buffered writes lose the last few events, which
                    # are the ones worth reading.
                    os.fsync(fh.fileno())
            except OSError:
                return seq
        return seq

    def read_all(self) -> list[dict[str, Any]]:
        """Read the transcript back, tolerating a truncated final line.

        A process killed mid-write leaves half a JSON object behind.  One
        document per line means the surviving prefix is still readable; chapter
        7 turns that property into the whole recovery mechanism.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break
        return events


NULL_RECORDER = Recorder(path=Path(os.devnull), enabled=False)
