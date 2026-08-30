"""Signed-in browsers: one table, in this process, with two clocks.

Chapter 22 needed somewhere to keep an OAuth attempt between two HTTP requests
and built `PendingOAuthTable` -- an in-process dict with a TTL and a lazy
sweep. This is the same shape holding a longer-lived thing, and the shape is
not a coincidence: the console's whole model is that one `serve` process owns
everything it is doing, which is already true of the approval broker, the
running turns and the event channel.

**Sessions are not persisted, deliberately.** A restart signs everybody out.
The alternative -- a `sessions.json` next to `accounts.json` -- buys
convenience and costs a second credential file on disk that has to be written
on almost every request, because the idle clock below moves when a request
arrives. It would also outlive the things it refers to: a session that
survives a restart points at approvals that no longer exist and turns that are
no longer running. The console is a tool you start; being signed out when you
restart it is the behaviour, not a limitation of it.

**The token is stored hashed even though the table is in memory**, which looks
like superstition until you ask where the table gets rendered. `GET
/api/auth/sessions` lists the caller's own sessions so that "sign out
everywhere" can exist, and a table of raw tokens is one careless
serialisation, one `repr()` in a traceback, one debug log line away from
handing out live credentials. Hashed, that mistake is not available.

The hash is a single SHA-256, not scrypt, and the difference is the whole
reason `accounts.py` uses scrypt: a password is low-entropy and guessable, so
verifying one must be made expensive on purpose. A session token is 32 random
bytes from `secrets`; there is nothing to guess, and this runs on every single
request.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field

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
    """Every browser currently signed in to this process."""

    _by_hash: dict[str, Session] = field(default_factory=dict)

    # -- lifecycle -----------------------------------------------------------

    def create(self, account_key: str, *, user_agent: str = "") -> tuple[str, Session]:
        """Mint a token and return it exactly once.

        The raw token leaves this method and is never stored, so this is the
        only moment it exists anywhere except the browser's cookie jar. A
        `Set-Cookie` header is written from the first element; everything
        afterwards works from the second.
        """
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
        return token, session

    def touch(self, token: str) -> Session | None:
        """Look a cookie up and move the idle clock, or answer `None`.

        Every rejection returns the identical `None`: expired, revoked, never
        issued and typed by hand are one answer, for chapter 22's reason
        (F22-02) -- a route that distinguishes them tells a caller which
        tokens exist.
        """
        session = self._by_hash.get(hash_token(token))
        if session is None:
            return None
        now = time.time()
        if session.expired(now):
            # Drop it here rather than waiting for a sweep: this is the moment
            # the expiry is known, and leaving it in the table means the next
            # `sessions_for` call still lists it as active.
            self._by_hash.pop(session.token_hash, None)
            return None
        session.last_seen = now
        return session

    def revoke(self, token: str) -> bool:
        return self._by_hash.pop(hash_token(token), None) is not None

    def revoke_id(self, account_key: str, session_id: str) -> bool:
        """Revoke by public id, and only within one account.

        `session_id` arrives from a browser, so the account is *not* taken from
        it: the caller passes the account it already authenticated, and a
        session belonging to somebody else is simply not found. Without that
        pair, "sign out this device" is "sign out anybody's device".
        """
        for key, session in list(self._by_hash.items()):
            if session.id == session_id and session.account_key == account_key:
                del self._by_hash[key]
                return True
        return False

    def revoke_account(self, account_key: str) -> int:
        """Every session of one account. A password change ends all of them."""
        doomed = [k for k, s in self._by_hash.items() if s.account_key == account_key]
        for key in doomed:
            del self._by_hash[key]
        return len(doomed)

    # -- reading -------------------------------------------------------------

    def sessions_for(self, account_key: str) -> list[Session]:
        self.sweep()
        return sorted(
            (s for s in self._by_hash.values() if s.account_key == account_key),
            key=lambda s: s.created,
            reverse=True,
        )

    def sweep(self) -> int:
        now = time.time()
        doomed = [k for k, s in self._by_hash.items() if s.expired(now)]
        for key in doomed:
            del self._by_hash[key]
        return len(doomed)

    def __len__(self) -> int:
        return len(self._by_hash)


__all__ = [
    "ABSOLUTE_TTL_SECONDS",
    "IDLE_TTL_SECONDS",
    "SESSION_COOKIE",
    "Session",
    "SessionTable",
    "hash_token",
]
