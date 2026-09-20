"""The session on disk: what was said, in the order it was said.

The recorder writes *requests and responses* -- what crossed the model
boundary, for a human reading it afterwards.  This writes *history items* --
the facts the loop keeps in memory -- in a shape that can be loaded back into
a `History` and continued.  The two files answer different questions, and
merging them would make the recovery path depend on a debugging aid nobody
promised to keep stable (see `recorder.py` and docs/history.md).

Three properties:

* **Append-only.**  A file rewritten from scratch on every turn is a file
  that can be lost on any turn.  Appending means the worst case is a missing
  tail, and a missing tail is recoverable.

* **One JSON document per line.**  A reader that stops at the first line it
  cannot parse still has everything before it.

* **One writer.**  Two processes appending to one file is the configuration
  that corrupted it in measurement.  Refused with a lock file rather than
  tolerated.

`History` is not asked to load itself.  It is rebuilt through its own
`add_*` methods, so a rollout that ends in the middle of a turn cannot become
an invalid conversation: the same invariant that refuses to build one in
memory refuses to load one from disk.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolCall
from minicodex.history import (
    AssistantMessage,
    DeveloperNote,
    History,
    HistoryError,
    HistoryItem,
    SystemNote,
    ToolResult,
    UserMessage,
)

DEFAULT_DIR = Path(".minicodex") / "sessions"

# Bumped once: version 1 stored a tool call's parsed `arguments` and not the
# `raw_arguments` string the model actually sent.  Re-rendering a resumed
# history therefore produced different bytes from the original -- valid JSON,
# same meaning, different string -- which keeping `raw_arguments` around
# exists to avoid.  The old files are still readable; see `_migrate`.
ROLLOUT_VERSION = 2


# One message for one condition -- there used to be two, which was two things
# to grep for and two things to keep in step.
_NO_HEADER = "{path}: no session header; not a rollout file"


class RolloutError(RuntimeError):
    """The session file cannot be used as asked."""


@dataclass(frozen=True)
class SessionMeta:
    """The environment the session was recorded in.

    Stored because a resumed session is not the same run: the model can be
    different, the working directory can be different, the sandbox mode can
    be different, and the history says nothing about any of them.  What is
    done with the difference is `environment_note`'s problem, not this one's.
    """

    session_id: str
    version: int = ROLLOUT_VERSION
    created: float = 0.0
    cwd: str = ""
    provider: str = ""
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    forked_from: str | None = None
    forked_at: int | None = None
    # The session that spawned this one, for a sub-agent.  Additive fields do
    # not bump ROLLOUT_VERSION: old files stay readable and new files stay
    # readable by old code, because the field is optional and unknown keys are
    # dropped.  Additive is not breaking; reinterpreting is.
    parent: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "created": self.created,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "forked_from": self.forked_from,
            "forked_at": self.forked_at,
            "parent": self.parent,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SessionMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def describe(self) -> str:
        where = self.cwd or "?"
        return f"{self.session_id} | {self.model or '?'} | {self.sandbox_mode or '?'} | {where}"


def new_session_id() -> str:
    """Sortable, unique enough, and readable in `ls`.

    Time first so that listing a directory is listing a history.  The pid is
    what makes two agents started in the same second land in different files --
    which matters because the alternative is the two-writer corruption above.
    """
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


# -- serialising one history item ------------------------------------------


def dump_item(item: HistoryItem) -> dict[str, Any]:
    """One history item as the JSON-able record the session file stores.

    Public because three layers write the same shape -- this module to disk,
    the web console to the browser stream and to the thread view -- and a
    private name imported across the package boundary is a private name in
    spirit only.
    """
    if isinstance(item, UserMessage):
        return {"type": "user", "text": item.text}
    if isinstance(item, SystemNote):
        return {"type": "system_note", "text": item.text}
    if isinstance(item, DeveloperNote):
        return {"type": "developer_note", "text": item.text}
    if isinstance(item, ToolResult):
        return {
            "type": "tool_result",
            "call_id": item.call_id,
            "name": item.name,
            "content": item.content,
        }
    if isinstance(item, AssistantMessage):
        return {
            "type": "assistant",
            "text": item.text,
            "tool_calls": [
                {
                    "call_id": c.call_id,
                    "name": c.name,
                    "arguments": c.arguments,
                    # The string the model sent, byte for byte.  Kept so a
                    # history can be re-sent without a lossy round trip;
                    # a rollout that drops it re-introduces exactly that loss
                    # one process boundary later.
                    "raw_arguments": c.raw_arguments,
                }
                for c in item.tool_calls
            ],
        }
    raise AssertionError(f"unserialisable history item: {item!r}")  # pragma: no cover


def _load_item(record: dict[str, Any]) -> HistoryItem:
    kind = record.get("type")
    if kind == "user":
        return UserMessage(record["text"])
    if kind == "system_note":
        return SystemNote(record["text"])
    if kind == "developer_note":
        return DeveloperNote(record["text"])
    if kind == "tool_result":
        return ToolResult(record["call_id"], record["name"], record["content"])
    if kind == "assistant":
        calls = tuple(
            ToolCall(
                c["call_id"],
                c["name"],
                c.get("arguments"),
                c["raw_arguments"],
            )
            for c in record.get("tool_calls", ())
        )
        return AssistantMessage(record.get("text", ""), calls)
    raise RolloutError(f"unknown record type {kind!r}")


def _migrate(record: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring one record forward to `ROLLOUT_VERSION`.

    A migration per version step, applied in order, each one small enough to
    read.  The alternative -- refusing to open old files -- makes the user's
    problem worse: their session is not corrupt, it is merely older than the
    program.
    """
    if version < 2 and record.get("type") == "assistant":
        record = dict(record)
        record["tool_calls"] = [
            # Version 1 had no `raw_arguments`.  Re-encoding the parsed object
            # is the best available reconstruction and is *not* the original
            # string: key order and whitespace are this program's, not the
            # model's.  It is written down here rather than pretended away.
            {**c, "raw_arguments": c.get("raw_arguments") or json.dumps(c.get("arguments") or {})}
            for c in record.get("tool_calls", ())
        ]
    return record


# -- writing ----------------------------------------------------------------


class RolloutWriter:
    """Appends history items to one file, and refuses to share it.

    The lock is a separate file created with `O_EXCL`, not an advisory lock on
    the rollout itself: it has to work on Windows, where the POSIX locking
    calls do not exist, and it has to be inspectable -- a stale lock names the
    pid that left it, so the user can decide rather than guess.
    """

    def __init__(self, path: Path, meta: SessionMeta, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.meta = meta
        self.enabled = enabled
        self._lock_fd: int | None = None
        self._items = 0
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write({"type": "meta", **meta.to_json()})

    # -- lock ---------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _acquire_lock(self) -> None:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RolloutError(
                f"{self.path} is already open by another minicodex (pid {holder or '?'}). "
                f"Resume it there, or delete {self.lock_path} if that process is gone."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                self.lock_path.unlink()
            except OSError:  # pragma: no cover - someone else already cleaned up
                pass

    def __enter__(self) -> RolloutWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- appending ----------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        # `newline=""` because the default on Windows turns every "\n" into
        # "\r\n", and a torn "\r\n" pair is the one corruption two concurrent
        # writers actually produced in measurement.  One writer makes that
        # unreachable; writing "\n" makes it unreachable twice.
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, item: HistoryItem) -> None:
        self._write({"type_version": ROLLOUT_VERSION, **dump_item(item)})
        self._items += 1

    def extend(self, items: Iterable[HistoryItem]) -> None:
        for item in items:
            self.append(item)

    def mark(self, kind: str, **payload: Any) -> None:
        """Record something that is not a history item -- a turn boundary, an abort.

        Kept in the same file rather than a second one: the only thing that makes
        "the run stopped here" useful is its position relative to the items, and
        two files have no shared order.
        """
        self._write({"type": "mark", "mark": kind, "ts": time.time(), **payload})


NULL_WRITER = RolloutWriter(Path(os.devnull), SessionMeta("null"), enabled=False)


# -- reading ----------------------------------------------------------------


@dataclass
class Rollout:
    meta: SessionMeta
    items: list[HistoryItem] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the file stopped making sense partway through.
    truncated_at: int | None = None
    path: Path | None = None

    def history(self) -> tuple[History, int]:
        """Rebuild, dropping any trailing turn that was never finished.

        Returns the history and the number of items dropped.  The rule is not
        "find the last complete turn" as a special case: the history refuses
        anything invalid anyway, so the loading rule is the simpler one --
        *replay, and remember the last point at which nothing was outstanding.*
        Where that point falls out of `History`'s own invariant rather than
        being computed separately, which means it cannot disagree with it.
        """
        history = History()
        settled: list[HistoryItem] = []
        for item in self.items:
            try:
                _add(history, item)
            except HistoryError:
                # The file contains something no conversation can contain --
                # a result for a call that is not there, most likely because
                # the assistant message ahead of it was lost.  Stop; what is
                # already settled is still a conversation.
                break
            if not history.unanswered():
                settled = list(history.items)

        dropped = len(self.items) - len(settled)
        if dropped:
            history = History()
            for item in settled:
                _add(history, item)
        return history, dropped


def _add(history: History, item: HistoryItem) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, DeveloperNote):
        history.add_developer_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)
    else:  # pragma: no cover
        raise AssertionError(item)


def read_rollout(path: Path) -> Rollout:
    """Load a session file, tolerating a damaged tail.

    Line by line, and the first line that does not parse ends the file.  Not
    "skip the bad line and carry on": a gap in the middle of a conversation is
    a conversation with a hole in it, and a hole is exactly the shape
    `History`'s invariant exists to reject.  Stopping keeps a prefix, which is a real
    conversation, over a filtered file, which is a plausible-looking fiction.
    """
    path = Path(path)
    if not path.exists():
        raise RolloutError(f"no session file at {path}")

    meta: SessionMeta | None = None
    items: list[HistoryItem] = []
    marks: list[dict[str, Any]] = []
    truncated_at: int | None = None

    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                truncated_at = lineno
                break
            if not isinstance(record, dict):
                truncated_at = lineno
                break
            if record.get("type") == "meta":
                meta = SessionMeta.from_json(record)
                continue
            if meta is None:
                raise RolloutError(_NO_HEADER.format(path=path))
            if record.get("type") == "mark":
                marks.append(record)
                if record.get("mark") == "compacted":
                    # Everything before this point was replaced in memory by the
                    # summary that follows it.  Replaying it would undo the
                    # compaction on every resume -- the session would come back
                    # at the size that made it compact in the first place.
                    items.clear()
                continue
            try:
                items.append(_load_item(_migrate(record, meta.version)))
            except (RolloutError, KeyError):
                truncated_at = lineno
                break

    if meta is None:
        raise RolloutError(_NO_HEADER.format(path=path))
    return Rollout(meta=meta, items=items, marks=marks, truncated_at=truncated_at, path=path)


def list_sessions(directory: Path = DEFAULT_DIR) -> list[Rollout]:
    """Every readable session, newest first.  Unreadable ones are skipped, not raised."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        try:
            out.append(read_rollout(path))
        except RolloutError:
            continue
    return out


# -- forking ----------------------------------------------------------------


def fork(source: Path, *, upto: int | None = None, directory: Path | None = None) -> Path:
    """Start a new session from a prefix of an old one.

    Copies records into a new file.  Not "point the new session at the old file
    and remember an offset": two sessions sharing a file is the two-writer
    configuration again, and the second one's turns would appear in the first
    one's replay.  Copying a conversation costs kilobytes.
    """
    original = read_rollout(source)
    directory = Path(directory) if directory is not None else Path(source).parent
    kept = original.items if upto is None else original.items[:upto]

    meta = replace(
        original.meta,
        session_id=new_session_id(),
        version=ROLLOUT_VERSION,
        created=time.time(),
        forked_from=original.meta.session_id,
        forked_at=len(kept),
    )
    target = directory / f"{meta.session_id}.jsonl"
    with RolloutWriter(target, meta) as writer:
        writer.extend(kept)
    return target


# -- what changed since the session was written -----------------------------


@dataclass(frozen=True)
class ResumeOutcome:
    """What resuming from a previous session produced.

    `notes` carries the human-readable lines the caller prints or emits, in
    the order they were decided; the caller cannot reorder them into
    meaninglessness because it did not have to assemble them.
    """

    history: History
    dropped: int
    meta: SessionMeta
    notes: tuple[str, ...]


def resume_from_rollout(path: Path, current: SessionMeta) -> ResumeOutcome:
    """Load a previous session for continuation, with its caveats attached.

    The shared shape of the CLI's `--resume` and the console's "next turn in
    this thread": rebuild the history (dropping any trailing incomplete
    turn), tell the user about damage and discards, add the environment note
    if the session's surroundings changed, and record the fork link.

    A resumed session is written to a *new* file rather than appended to the
    old one.  Appending would mean the recovered prefix and the discarded
    tail share a file, so the next recovery would have to rediscover which
    of the two it was looking at.
    """
    loaded = read_rollout(path)
    history, dropped = loaded.history()
    notes: list[str] = []
    if loaded.truncated_at is not None:
        notes.append(
            f"session file damaged from line {loaded.truncated_at}; using what precedes it"
        )
    if dropped:
        history.add_system_note(interrupted_note(dropped))
        notes.append(f"resumed: {dropped} incomplete message(s) discarded")
    note = environment_note(loaded.meta, current)
    if note is not None:
        history.add_system_note(note)
        notes.append("environment changed since this session was recorded")
    return ResumeOutcome(history, dropped, loaded.meta, tuple(notes))


def environment_note(meta: SessionMeta, current: SessionMeta) -> str | None:
    """One sentence per thing that is no longer what the history assumes.

    Returns None when nothing moved.  The model is told rather than protected:
    the history is full of paths relative to a directory that may not be this
    one, and of files it read under a permission set it may no longer have.
    Neither of those is recoverable by this program, and both are obvious to the
    model the moment it is told.
    """
    fields = [
        ("working directory", meta.cwd, current.cwd),
        ("model", meta.model, current.model),
        ("sandbox mode", meta.sandbox_mode, current.sandbox_mode),
        ("approval policy", meta.approval_policy, current.approval_policy),
    ]
    changed = [(label, was, now) for label, was, now in fields if was and now and was != now]
    if not changed:
        return None
    lines = [f"- {label}: was {was!r}, now {now!r}" for label, was, now in changed]
    return (
        "This session was resumed and the environment is not the one it was "
        "recorded in:\n"
        + "\n".join(lines)
        + "\nRe-check anything above that your earlier steps depended on before "
        "relying on it."
    )


def interrupted_note(dropped: int | None = None) -> str:
    """What the model is told about the turn that never finished.

    Without it the transcript reads as if the last thing in it simply
    happened; the wording and the count are pinned by measurement (see
    docs/history.md) -- the count is the part the model cannot know, and the
    advice is deliberately minimal.

    Two wordings for two situations.  Live interruption: the tools were
    answered and the answers say they were cut off, so there is no count to
    give.  Recovery from disk: whole messages are missing.
    """
    if dropped is None:
        return (
            "You were interrupted by the user during the previous turn. Any tool "
            "call answered with 'interrupted' may have run partially, or not at all."
        )
    return (
        "The previous session ended without finishing its last turn. "
        f"{dropped} message(s) were discarded because they were incomplete."
    )


def replay(history: History, item: HistoryItem) -> None:
    """Put one stored item back into a live history, through the front door."""
    _add(history, item)


def rollout_path(session_id: str, directory: Path = DEFAULT_DIR) -> Path:
    return Path(directory) / f"{session_id}.jsonl"


def resolve(reference: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept a path, a session id, or `last`.

    `last` skips sub-agent sessions.  A run that spawns two sub-agents leaves
    three files, and the two newest are the children -- so "resume the last
    session" would silently continue the last thing a sub-agent said.  A
    sub-agent's session is still resumable **by id**: it is only excluded
    from the guess.
    """
    if reference == "last":
        sessions = [r for r in list_sessions(directory) if r.meta.parent is None]
        if not sessions:
            raise RolloutError(f"no sessions in {directory}")
        assert sessions[0].path is not None
        return sessions[0].path
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    candidate = rollout_path(reference, directory)
    if candidate.exists():
        return candidate
    raise RolloutError(f"no session {reference!r} (looked in {directory})")
