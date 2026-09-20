"""Signed-in browsers: one table, persisted to disk, with two clocks.

An in-process dict with a TTL and a lazy sweep, like `PendingOAuthTable`
(oauth.py) and the console's other per-process tables: one `serve` process
owns everything it is doing, from the approval broker to the running turns to
the event channel.

**Sessions survive a restart.**  The per-request cost is a timestamp
assignment and a dirty flag -- the table is saved when its contents change,
at most once per second.  A session that outlives what it refers to is
answered by `auth.resolve`: a session whose account is gone is revoked on
first touch, and an approval broker or a running turn was never recoverable
across a restart anyway -- the shutdown hook fails them all and the turn
files land as `interrupted`, so a surviving cookie points at nothing
dangerous, only at threads the account already owns.

**The token is stored hashed** -- SHA-256, not scrypt, and the difference is
the whole reason `accounts.py` uses scrypt: a password is low-entropy and
guessable, so verifying one must be made expensive on purpose.  A session
token is 32 random bytes from `secrets`; there is nothing to guess, and this
runs on every single request.  A table of raw tokens on disk would be one
careless `cat` away from handing out live credentials.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .persistence import atomic_write_json

#: The cookie's name. Prefixed because it is not the only cookie a browser on
#: this origin might carry, and unprefixed `session` is what every other tool
#: on `localhost:8000` also calls its cookie.
SESSION_COOKIE = "minicodex_session"

#: Twelve hours from sign-in, regardless of activity. The bound that cannot be
#: extended by using the session: a stolen cookie has an expiry that the thief
#: refreshing it cannot push out.
ABSOLUTE_TTL_SECONDS = 12 * 60 * 60

#: Two hours since the last request. The bound that closes a session somebody
#: walked away from. Both are needed -- idle alone never ends a session that is
#: being used by the wrong person, absolute alone signs out somebody who is in
#: the middle of working.
IDLE_TTL_SECONDS = 2 * 60 * 60


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Session:
    """One signed-in browser."""

    token_hash: str
    account_key: str
    created: float
    last_seen: float
    #: Free-text, for the "your sessions" list only. Truncated on the way in,
    #: because it is attacker-controlled and it is about to be rendered.
    user_agent: str = ""

    #: A stable public handle. The hash's first twelve characters: enough to
    #: name one session in a revoke request, and not enough to be one.
    @property
    def id(self) -> str:
        return self.token_hash[:12]

    def expired(self, now: float) -> bool:
        return now - self.created > ABSOLUTE_TTL_SECONDS or now - self.last_seen > IDLE_TTL_SECONDS

    def public(self, *, current: bool = False) -> dict[str, object]:
        return {
            "id": self.id,
            "created": self.created,
            "last_seen": self.last_seen,
            "user_agent": self.user_agent,
            "current": current,
        }


@dataclass
class SessionTable:
    """Every browser currently signed in to this process.

    `_path` is where the table persists (optional: `None` keeps the whole
    thing in memory, which is what every test wants), and `_dirty`/`_saved_at`
    implement the debounce: state is written when it changed and at most once
    per second, so a burst of requests costs one write, not one per request.

    All reads and writes of `_by_hash` run under one lock, because the
    mutating routes are synchronous `def` handlers running in anyio worker
    threads: a lock-free read-modify-write cycle here can drop a concurrent
    sign-in or revocation.
    """

    _by_hash: dict[str, Session] = field(default_factory=dict)
    _path: Path | None = None
    _dirty: bool = False
    _saved_at: float = 0.0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- persistence ---------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> SessionTable:
        """Read the table back, dropping anything already expired.

        A missing file is an empty table -- the first boot.  A damaged one is
        an empty table too, but loudly: `sessions.json` is a credential store
        the server itself wrote, so unreadable means truncated by a crash or
        tampered with, and silently starting over would hide which.  The
        atomic-write below makes truncation a one-in-a-million event rather
        than an argument for refusing to boot.
        """
        table = cls(_path=path)
        if not path.exists():
            return table
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw["sessions"]
            # Explicit check, not an assert: asserts are stripped under `python
            # -O`, and this is a security-relevant type boundary, not a debug aid.
            if not isinstance(entries, list):
                raise TypeError("sessions is not a list")
        except (OSError, ValueError, KeyError, TypeError):
            import logging

            logging.getLogger(__name__).warning(
                "%s unreadable; starting with no sessions (old cookies will not work)", path
            )
            return table
        now = time.time()
        for entry in entries:
            try:
                session = Session(
                    token_hash=str(entry["token_hash"]),
                    account_key=str(entry["account_key"]),
                    created=float(entry["created"]),
                    last_seen=float(entry["last_seen"]),
                    user_agent=str(entry.get("user_agent", ""))[:200],
                )
            except (KeyError, TypeError, ValueError):
                continue
            if not session.expired(now):
                table._by_hash[session.token_hash] = session
        return table

    def _persist(self, *, force: bool = False) -> None:
        """Write the table if it changed and the debounce window has passed.

        The shutdown hook in app.py calls this with `force=True`, which is the
        only write that matters on the way down: a clean stop keeps every
        session, a kill loses at most one second of logins.
        """
        if self._path is None or not self._dirty:
            return
        now = time.time()
        if not force and now - self._saved_at < _PERSIST_DEBOUNCE_SECONDS:
            return
        payload = {
            "saved_at": now,
            "sessions": [
                {
                    "token_hash": s.token_hash,
                    "account_key": s.account_key,
                    "created": s.created,
                    "last_seen": s.last_seen,
                    "user_agent": s.user_agent,
                }
                for s in self._by_hash.values()
            ],
        }
        assert self._path is not None
        atomic_write_json(self._path, payload)
        self._dirty = False
        self._saved_at = now

    def flush(self) -> None:
        """Force a write if there is anything unsaved. See `_persist`."""
        self._persist(force=True)

    # -- lifecycle -----------------------------------------------------------

    def create(self, account_key: str, *, user_agent: str = "") -> tuple[str, Session]:
        """Mint a token and return it exactly once.

        The raw token leaves this method and is never stored, so this is the
        only moment it exists anywhere except the browser's cookie jar. A
        `Set-Cookie` header is written from the first element; everything
        afterwards works from the second.
        """
        with self._lock:
            self.sweep()
            token = secrets.token_urlsafe(32)
            now = time.time()
            session = Session(
                token_hash=hash_token(token),
                account_key=account_key,
                created=now,
                last_seen=now,
                user_agent=user_agent[:200],
            )
            self._by_hash[session.token_hash] = session
            self._dirty = True
            self._persist()
        return token, session

    def touch(self, token: str) -> Session | None:
        """Look a cookie up and move the idle clock, or answer `None`.

        Every rejection returns the identical `None`: expired, revoked, never
        issued and typed by hand are one answer, so a route that returns it
        cannot tell a caller which tokens exist.

        Moving `last_seen` marks the table dirty but the debounce absorbs it:
        a busy server writes once a second, not once per request, and the
        idle clock losing the last second of activity on a crash is a
        non-event.
        """
        with self._lock:
            session = self._by_hash.get(hash_token(token))
            if session is None:
                return None
            now = time.time()
            if session.expired(now):
                # Drop it here rather than waiting for a sweep: this is the moment
                # the expiry is known, and leaving it in the table means the next
                # `sessions_for` call still lists it as active.
                self._by_hash.pop(session.token_hash, None)
                self._dirty = True
                self._persist()
                return None
            if now - session.last_seen > 1.0:
                session.last_seen = now
                self._dirty = True
                self._persist()
            return session

    def revoke(self, token: str) -> bool:
        with self._lock:
            gone = self._by_hash.pop(hash_token(token), None) is not None
            if gone:
                self._dirty = True
                self._persist()
            return gone

    def revoke_id(self, account_key: str, session_id: str) -> bool:
        """Revoke by public id, and only within one account.

        `session_id` arrives from a browser, so the account is *not* taken from
        it: the caller passes the account it already authenticated, and a
        session belonging to somebody else is simply not found. Without that
        pair, "sign out this device" is "sign out anybody's device".
        """
        with self._lock:
            for key, session in list(self._by_hash.items()):
                if session.id == session_id and session.account_key == account_key:
                    del self._by_hash[key]
                    self._dirty = True
                    self._persist()
                    return True
            return False

    def revoke_account(self, account_key: str) -> int:
        """Every session of one account. A password change ends all of them."""
        with self._lock:
            doomed = [k for k, s in self._by_hash.items() if s.account_key == account_key]
            for key in doomed:
                del self._by_hash[key]
            if doomed:
                self._dirty = True
                self._persist()
            return len(doomed)

    # -- reading -------------------------------------------------------------

    def sessions_for(self, account_key: str) -> list[Session]:
        with self._lock:
            self.sweep()
            return sorted(
                (s for s in self._by_hash.values() if s.account_key == account_key),
                key=lambda s: s.created,
                reverse=True,
            )

    def sweep(self) -> int:
        with self._lock:
            now = time.time()
            doomed = [k for k, s in self._by_hash.items() if s.expired(now)]
            for key in doomed:
                del self._by_hash[key]
            if doomed:
                self._dirty = True
                self._persist()
            return len(doomed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_hash)


#: How long a change waits before it must reach disk.  One second is far
#: inside the tolerance of "a restart signs out at most the last second's
#: logins", and far outside the granularity of a per-request write.
_PERSIST_DEBOUNCE_SECONDS = 1.0


__all__ = [
    "ABSOLUTE_TTL_SECONDS",
    "IDLE_TTL_SECONDS",
    "SESSION_COOKIE",
    "Session",
    "SessionTable",
    "hash_token",
]
