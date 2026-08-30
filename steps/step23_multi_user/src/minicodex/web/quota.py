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
handler for the same class of reason (F19-01), and this claim goes in the same
place, next to it.

**Turns, not tokens, and that is a scope decision rather than an oversight.**
A token or cost quota is what a hosted product would want, and this program
does not have the number it would need: `model.py` does parse the server's
reported `prompt_tokens` / `completion_tokens`, but the only thing that
consumes them is `tokens.Calibration`, which keeps a *ratio* and throws the
totals away. Metering somebody against `Calibration.correct()` -- an estimate
that is documented as useless for the first two turns of a session -- would be
a number with a decimal point and no defence. Counting turns is exact, is
known at the only moment enforcement can happen, and does not require pushing
a billing concern down into the agent.

**In memory, like the session table**, and for the same reason: this process
owns what it is currently doing. A restart forgives the last hour, which is
the wrong behaviour for a bill and an acceptable one for a fairness device on
a tool you can restart yourself.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

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
    """When each account last started a turn. One deque per account."""

    _starts: dict[str, deque[float]] = field(default_factory=dict)

    def _window(self, account_key: str, now: float) -> deque[float]:
        starts = self._starts.setdefault(account_key, deque())
        # Swept here rather than on a timer, chapter 22's trade (F22-03): the
        # only moment the answer matters is the moment somebody asks.
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

    def refund(self, account_key: str) -> None:
        """Undo the most recent claim, for a turn that never started.

        Exactly one caller: `send_message` claims the quota and then discovers
        the thread has no provider configured, or the store rejects it. That is
        not a turn -- nothing was spent -- and leaving the claim would let a
        misconfigured console spend somebody's hour on 400s.

        A turn that *starts* and then fails is not refunded, which is the
        distinction the docstring at the top of this file is about.
        """
        starts = self._starts.get(account_key)
        if starts:
            starts.pop()

    def snapshot(self, account_key: str, limits: Limits, *, running: int) -> dict[str, int]:
        """What the console shows next to the account name."""
        starts = self._window(account_key, time.time())
        return {
            "running": running,
            "concurrent_limit": limits.concurrent_turns,
            "used_this_hour": len(starts),
            "hourly_limit": limits.turns_per_hour,
        }


__all__ = [
    "DEFAULT_CONCURRENT_TURNS",
    "DEFAULT_TURNS_PER_HOUR",
    "WINDOW_SECONDS",
    "Limits",
    "QuotaExceeded",
    "QuotaLedger",
]
