"""Login throttling: wrong guesses cost time, not just a 401.

A constant-time comparison of an unlimited number of guesses is an unlimited
number of guesses.  This module bounds that.

**Per (account, source), not per account alone.**  Throttling per account
alone hands an attacker a denial-of-service on a chosen user for free -- guess
at their name until the lockout trips, and they cannot sign in either.  The
pair means an attacker burns only their own source.  The flip side is that one
attacker behind many sources still gets many tries, which is true of every
source-scoped defence and is why this is a rate limit and not a substitute for
a strong password.

**Failure-scoped, with a successful login wiping the record.**  Correct
sign-ins leave no state behind, so a user who mistypes twice a day never meets
the limiter.  A wrong guess is what costs a slot in the window.

**The source is the socket's peer, resolved by the caller.**  This module
never reads headers: `X-Forwarded-For` is an assertion made by whoever is
forwarding, and trusting it when nobody is forwarding is an IP-spoofing
primitive with a nice name.  A deployment behind a trusted proxy can widen the
semantics later, in one place, with the proxy's own configuration to point at.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: How many failed attempts one (account, source) pair gets per window.
DEFAULT_MAX_FAILURES = 5

#: The window those attempts must fit into, in seconds.
FAILURE_WINDOW_SECONDS = 15 * 60

#: How long a tripped limit refuses further attempts, regardless of the
#: window: a lockout that ends the moment one attempt ages out of the window
#: lets an attacker pace guesses at exactly the rate the limit names.
LOCKOUT_SECONDS = 15 * 60


class Throttled(Exception):
    """This (account, source) has spent its failures; come back later.

    `retry_after` is a real number of seconds -- the lockout's remaining life
    -- so a well-behaved client's wait is exactly as long as it must be.
    """

    def __init__(self, *, retry_after: int) -> None:
        super().__init__(f"too many failed attempts; try again in {retry_after} seconds")
        self.retry_after = retry_after


@dataclass
class _Failures:
    """One pair's failed attempts, and when its lockout ends."""

    at: list[float] = field(default_factory=list)
    locked_until: float = 0.0


@dataclass
class LoginThrottle:
    """Failed logins, by (account key, source) pair.

    Pairs are evicted lazily: any `check`/`record_failure` first drops every
    pair whose window and lockout have both expired, so unauthenticated
    traffic cannot grow the table without bound -- an attacker rotating
    source addresses pays one entry each, and each entry dies within
    `FAILURE_WINDOW_SECONDS + LOCKOUT_SECONDS` of its last failure.

    All mutations run under one lock, because the routes that record failures
    are synchronous `def` handlers on anyio worker threads.
    """

    _pairs: dict[tuple[str, str], _Failures] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _sweep(self, now: float) -> None:
        dead = [
            key
            for key, record in self._pairs.items()
            if record.locked_until < now
            and all(now - t > FAILURE_WINDOW_SECONDS for t in record.at)
        ]
        for key in dead:
            del self._pairs[key]

    def check(self, account_key: str, source: str, *, now: float | None = None) -> None:
        """Raise `Throttled` if this pair is locked out.

        Called *before* the password is hashed, so a throttled attacker pays
        no scrypt cost either -- the point of hashing expensively is to price
        the guesses of someone allowed to guess.
        """
        moment = time.time() if now is None else now
        with self._lock:
            self._sweep(moment)
            record = self._pairs.get((account_key, source))
            if record is None:
                return
            if moment < record.locked_until:
                raise Throttled(retry_after=max(1, int(record.locked_until - moment) + 1))

    def record_failure(self, account_key: str, source: str, *, now: float | None = None) -> int:
        """Count one wrong password, tripping the lockout at the limit.

        Returns the pair's current failure count, which is exactly one number:
        the audit log may record it, and nothing else may show it -- it is one
        bit away from "how close did they get".
        """
        moment = time.time() if now is None else now
        with self._lock:
            self._sweep(moment)
            record = self._pairs.setdefault((account_key, source), _Failures())
            record.at = [t for t in record.at if moment - t <= FAILURE_WINDOW_SECONDS]
            record.at.append(moment)
            if len(record.at) >= DEFAULT_MAX_FAILURES and record.locked_until < moment:
                record.locked_until = moment + LOCKOUT_SECONDS
                record.at = []
            return len(record.at)

    def record_success(self, account_key: str, source: str) -> None:
        """A correct password wipes the pair's slate.

        The record is deleted rather than emptied: a pair that never fails
        again should not stay in memory forever, and an absent record is the
        cheapest kind there is.
        """
        with self._lock:
            self._pairs.pop((account_key, source), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._pairs)


__all__ = [
    "DEFAULT_MAX_FAILURES",
    "FAILURE_WINDOW_SECONDS",
    "LOCKOUT_SECONDS",
    "LoginThrottle",
    "Throttled",
]
