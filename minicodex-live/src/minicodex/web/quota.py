"""How much of this server one account may use at once, and per hour.

A quota is not a security control -- the gate in `auth.py` is -- and it is not
billing either. It is the answer to a smaller question that only appears once a
server has more than one user: **whose turn gets to run when they both want
one.** Without it, a single account can start a turn on every thread it owns
and hold the process's event loop, its subprocesses and its model budget for
as long as it likes, and the second person's first experience of the console
is that it is slow for reasons nobody can see.

**Both limits are charged at claim time, before any work happens.** Charging
on completion is the mistake that looks like fairness: a turn that fails costs
the same model call, the same subprocess and the same seconds as one that
succeeds, so a quota that only counts successes is a quota a retry loop does
not have. `routes.send_message` already claims `busy` synchronously in the
handler for the same class of reason, and this claim goes in the same place,
next to it.

**Turns, not tokens, and that is a scope decision rather than an oversight.**
A token or cost quota is what a hosted product would want, and this program
does not have the number it would need: the only consumer of the server's
reported token counts is `tokens.Calibration`, which keeps a *ratio* and
throws the totals away. Metering somebody against
`Calibration.correct()` -- an estimate documented as useless for the first
two turns of a session -- would be a number with a decimal point and no
defence. Counting turns is exact, is known at the only moment enforcement
can happen, and does not push a billing concern down into the agent.

**Persisted.**  A restart that forgives the last hour is acceptable for a
fairness device on a tool you restart yourself; on a server somebody else
keeps running it is a loophole -- stop/start resets the window, and the
stop/start can be forced by anyone who can reach the host.  The window is
written next to the sessions table (same debounced, atomic-replace pattern,
same one-second bound on what a crash can lose).
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .persistence import atomic_write_json

#: How many turns one account may have in flight. Two rather than one because
#: reviewing a finished thread while another one works is the normal way to use
#: this console, and rather than "as many as you like" because each in-flight
#: turn is a model connection, a rollout writer and possibly a subprocess.
DEFAULT_CONCURRENT_TURNS = 2

#: And how many it may start per hour. Sized against the slowest useful thing:
#: sixty turns an hour is one a minute, sustained, which no person types and a
#: runaway frontend reaches in seconds.
DEFAULT_TURNS_PER_HOUR = 60

WINDOW_SECONDS = 60 * 60


@dataclass(frozen=True)
class Limits:
    """One account's allowance. Per account, so it can be raised for one.

    Stored on the account record rather than in this module, so that "give
    alice more" is a change to `accounts.json` and not a redeploy -- and so
    that the defaults above are what a *new* account gets, not what every
    account is stuck with.
    """

    concurrent_turns: int = DEFAULT_CONCURRENT_TURNS
    turns_per_hour: int = DEFAULT_TURNS_PER_HOUR

    @classmethod
    def merge(cls, raw: dict[str, int] | None) -> Limits:
        raw = raw or {}
        return cls(
            concurrent_turns=int(raw.get("concurrent_turns", DEFAULT_CONCURRENT_TURNS)),
            turns_per_hour=int(raw.get("turns_per_hour", DEFAULT_TURNS_PER_HOUR)),
        )


class QuotaExceeded(Exception):
    """Refused, with the two things the caller actually needs.

    `retry_after` is seconds and is a real number rather than a constant: for
    the hourly window it is the age of the oldest turn still in it, so a
    frontend that waits exactly that long succeeds exactly once.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class QuotaLedger:
    """When each account last started a turn. One deque per account.

    `_path` is where the windows persist (`None` = memory only, the tests'
    shape).  Writes follow the sessions table's pattern: dirty flag, one-second
    debounce, atomic replace.  `_starts` is guarded by a lock for the same
    reason the session table's rows are: the claiming routes run on worker
    threads.
    """

    _starts: dict[str, deque[float]] = field(default_factory=dict)
    _path: Path | None = None
    _dirty: bool = False
    _saved_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> QuotaLedger:
        """Read the windows back; entries older than the window are dropped
        by `_window` on first touch, so no sweeping is needed here."""
        ledger = cls(_path=path)
        if not path.exists():
            return ledger
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw["starts"]
            # Explicit check, not an assert: asserts are stripped under
            # `python -O`, and this is a data-shape boundary.
            if not isinstance(entries, dict):
                raise TypeError("starts is not an object")
        except (OSError, ValueError, KeyError, TypeError):
            import logging

            logging.getLogger(__name__).warning(
                "%s unreadable; starting with empty quota windows", path
            )
            return ledger
        for key, starts in entries.items():
            try:
                ledger._starts[str(key)] = deque(float(t) for t in starts)
            except (TypeError, ValueError):
                continue
        return ledger

    def _persist(self, *, force: bool = False) -> None:
        if self._path is None or not self._dirty:
            return
        now = time.time()
        if not force and now - self._saved_at < _PERSIST_DEBOUNCE_SECONDS:
            return
        payload = {"saved_at": now, "starts": {k: list(v) for k, v in self._starts.items()}}
        assert self._path is not None
        atomic_write_json(self._path, payload)
        self._dirty = False
        self._saved_at = now

    def flush(self) -> None:
        """Force a write if there is anything unsaved."""
        self._persist(force=True)

    # -- accounting ----------------------------------------------------------

    def _window(self, account_key: str, now: float) -> deque[float]:
        with self._lock:
            starts = self._starts.setdefault(account_key, deque())
            # Swept here rather than on a timer: the only moment the answer
            # matters is the moment somebody asks.
            while starts and now - starts[0] > WINDOW_SECONDS:
                starts.popleft()
            return starts

    def claim(self, account_key: str, limits: Limits, *, running: int) -> None:
        """Charge one turn, or raise.

        `running` is passed in rather than counted here, and that is the seam
        that keeps this class testable: the console's `busy` set is keyed by
        thread and this ledger has never heard of threads. The caller -- which
        does know which threads are this account's -- does the intersection.
        """
        if running >= limits.concurrent_turns:
            raise QuotaExceeded(
                f"you already have {running} turn(s) running "
                f"(limit {limits.concurrent_turns}); wait for one to finish",
                # No useful number: it ends when a turn ends, and this class
                # does not know how long that is. Ten seconds is a polling
                # interval, offered as such.
                retry_after=10,
            )
        with self._lock:
            now = time.time()
            starts = self._window(account_key, now)
            if len(starts) >= limits.turns_per_hour:
                oldest = starts[0]
                raise QuotaExceeded(
                    f"you have started {len(starts)} turns in the last hour "
                    f"(limit {limits.turns_per_hour})",
                    retry_after=max(1, int(WINDOW_SECONDS - (now - oldest)) + 1),
                )
            starts.append(now)
            self._dirty = True
            self._persist()

    def refund(self, account_key: str) -> None:
        """Undo the most recent claim, for a turn that never started.

        Exactly one caller: `send_message` claims the quota and then discovers
        the thread has no provider configured, or the store rejects it. That is
        not a turn -- nothing was spent -- and leaving the claim would let a
        misconfigured console spend somebody's hour on 400s.

        A turn that *starts* and then fails is not refunded: a failed turn
        costs what a successful one costs, which is the point of claiming
        up front.
        """
        with self._lock:
            starts = self._starts.get(account_key)
            if starts:
                starts.pop()
                self._dirty = True
                self._persist()

    def snapshot(self, account_key: str, limits: Limits, *, running: int) -> dict[str, int]:
        """What the console shows next to the account name."""
        starts = self._window(account_key, time.time())
        return {
            "running": running,
            "concurrent_limit": limits.concurrent_turns,
            "used_this_hour": len(starts),
            "hourly_limit": limits.turns_per_hour,
        }


#: How long a change waits before it must reach disk.  One second is far
#: inside the tolerance of "a restart signs out at most the last second's
#: logins", and far outside the granularity of a per-request write.
_PERSIST_DEBOUNCE_SECONDS = 1.0


__all__ = [
    "DEFAULT_CONCURRENT_TURNS",
    "DEFAULT_TURNS_PER_HOUR",
    "WINDOW_SECONDS",
    "Limits",
    "QuotaExceeded",
    "QuotaLedger",
]
