"""Who may use this console, and how the first one gets in.

Twenty-two chapters of this program answered "who is this" with silence,
correctly, because the answer was always "the person at the keyboard".
Chapter 19 put it behind a socket and chapter 20 wrote down the type that
would eventually hold the answer (`tenancy.Owner`) while stating plainly that
it was a seam and not a system: "there is no registration here, no session, no
password, no quota, and `Owner.key` is trusted completely by whoever
constructs it. A server that derives an owner from an unauthenticated request
header has moved the leak, not closed it."

This module is the thing that constructs it, and therefore the thing that has
to earn the trust.

**Why not "sign in with GitHub", given that chapter 22 just built OAuth.**
Two reasons, and the first is the one worth remembering. OAuth 2.0 is an
*authorization* protocol: what a completed flow gives you is a token that acts
on a resource, and "the holder of this token can read a GitHub repository" is
not the same proposition as "the person in front of this browser is
`alice`". Treating one as the other is a documented class of bug -- a token
minted for any other client, or lifted from any other application, logs in.
The second reason is smaller and decisive on its own: chapter 22 measured that
GitHub publishes no `registration_endpoint` (F21-08b), so a GitHub login would
require whoever runs this console to register an OAuth App *before the first
person can sign in at all*. A self-hosted tool that cannot be started without
paperwork on someone else's website is not self-hosted.

So: local accounts, a password, a session cookie. codex has no opinion to
borrow here -- it is a CLI and a TUI, it authenticates *to* ChatGPT and never
*for* anybody -- and this is one of the few places in this book where there is
no upstream to compare against.

**The one thing not hand-rolled.** This book's line is "hand-roll what you are
teaching, depend on what you are not", and password hashing is not what this
chapter teaches; it is also the single worst candidate for a from-scratch
implementation in the whole program. `hashlib.scrypt` is RFC 7914 in the
standard library, so there is no dependency to add and no KDF to invent. The
code below chooses parameters and an encoding around it and does no
cryptography of its own.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicodex.tenancy import Owner, OwnerError

from .quota import Limits

# scrypt cost. 2**14 is the interactive-login figure from RFC 7914 §2 and
# costs 128 * N * r = 16 MiB of memory per verification, which is the point --
# an attacker with a stolen `accounts.json` pays that per guess.
#
# Written into every stored hash rather than only read from here, so raising
# them later does not invalidate the passwords already on disk: `verify` takes
# the cost from the string it is checking, and only `hash_password` reads these
# names. That is the difference between a parameter and a constant.
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32

#: The shortest password this console accepts. Length only -- no character
#: classes, which push people towards `Passw0rd!` and are worth less than four
#: more characters. NIST SP 800-63B dropped composition rules for the same
#: reason.
MIN_PASSWORD_LENGTH = 10

#: Format: `scrypt$<n>$<r>$<p>$<salt-b64>$<key-b64>`. Self-describing on
#: purpose: a hash that does not carry its own parameters is a hash that can
#: only be checked by the version of the code that wrote it.
_ENCODING = "scrypt"


class AccountError(ValueError):
    """A registration or a login this module refuses."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, *, n: int | None = None) -> str:
    # `None` and a lookup rather than `n: int = SCRYPT_N`, because a default
    # argument is evaluated once at import and would freeze the cost into this
    # function. Read here, the module attribute is still the single place the
    # figure lives -- and raising it is a one-line change that takes effect on
    # the next password written, with every existing hash still verifiable
    # because it carries the cost it was written with.
    n = SCRYPT_N if n is None else n
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AccountError(f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return f"{_ENCODING}${n}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, encoded: str) -> bool:
    """Check a password against a stored hash, in constant time.

    `hmac.compare_digest` rather than `==` because the second one stops at the
    first differing byte, and the number of bytes it got through is a fact
    about the secret that a caller can time. That is textbook, and it is also
    not the interesting comparison in this file -- see `AccountStore.verify`,
    where the leak is *whether the account exists*, which no amount of
    constant-time byte comparison down here can hide.
    """
    try:
        kind, n, r, p, salt, key = encoded.split("$")
        if kind != _ENCODING:
            return False
        expected = _unb64(key)
        computed = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_unb64(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        # A malformed or unrecognised hash is a failed login, not a crash. One
        # unparseable line in `accounts.json` must not be able to take the
        # login route down for everybody else.
        return False
    return hmac.compare_digest(computed, expected)


#: A real hash of a password nobody knows, verified against when the account
#: does not exist so that the miss costs the same as the hit. Built once, on
#: first use rather than at import, because it is one full scrypt run and this
#: module is imported by things that never authenticate anybody.
_DUMMY: list[str] = []


def _dummy_hash() -> str:
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(32)))
    return _DUMMY[0]


# -- the record ---------------------------------------------------------------

#: An account key is an owner key: it becomes `~/.minicodex/tenants/<key>/` and
#: `<data-dir>/tenants/<key>/`, so it is validated by `tenancy.Owner` itself
#: rather than by a second pattern that could drift away from it. Reserved
#: separately are the few names that would collide with the console's own
#: files if they ever became directory names next to them.
RESERVED_KEYS = frozenset({"tenants", "sessions", "accounts", "admin", "api", "ws"})

_HANDLE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class Account:
    """One person who may sign in, and the tenancy they get when they do."""

    key: str
    password_hash: str
    created: float
    #: The first account, and anyone it promotes. An admin may list accounts,
    #: create them and disable them; it is deliberately *not* "can read
    #: everyone's threads", because there is no route that does that -- an
    #: escalation this console cannot perform is better than one it can perform
    #: for the right people.
    admin: bool = False
    disabled: bool = False
    #: Absolute paths this account may open a workspace under. Empty means
    #: unrestricted, which is right for the single-operator install this
    #: console started as and wrong the moment there are two accounts -- see
    #: `routes._validated_workspace`.
    workspace_roots: tuple[str, ...] = ()
    #: Overrides for `quota.Limits`, empty for the defaults. A tuple of pairs
    #: rather than a `Limits` because it is a slice of a JSON file, and because
    #: "this account said nothing about `turns_per_hour`" has to keep meaning
    #: "whatever the default is now" after the default changes.
    limits: tuple[tuple[str, int], ...] = ()

    def quota_limits(self) -> Limits:
        return Limits.merge(dict(self.limits))

    def owner(self, home: Path | None = None) -> Owner:
        """This account's tenancy, optionally rooted somewhere other than `~`.

        `home` exists because a server is not obliged to keep its tenants in
        the operator's home directory -- `minicodex serve --home /srv/tenants`
        is a reasonable thing to want, and a test that must not write into the
        real `~/.minicodex` needs exactly the same hook. `None` keeps chapter
        20's default, which is the path the CLI has always used.
        """
        return Owner(self.key) if home is None else Owner(self.key, home=home)

    def public(self) -> dict[str, Any]:
        """What the browser is allowed to know. No hash, ever."""
        return {
            "key": self.key,
            "admin": self.admin,
            "disabled": self.disabled,
            "created": self.created,
            "workspace_roots": list(self.workspace_roots),
            "limits": dict(self.limits),
        }


def _from_json(raw: dict[str, Any]) -> Account:
    return Account(
        key=raw["key"],
        password_hash=raw["password_hash"],
        created=float(raw.get("created", 0.0)),
        admin=bool(raw.get("admin", False)),
        disabled=bool(raw.get("disabled", False)),
        workspace_roots=tuple(raw.get("workspace_roots") or ()),
        limits=tuple(sorted((raw.get("limits") or {}).items())),
    )


def _to_json(account: Account) -> dict[str, Any]:
    return {
        "key": account.key,
        "password_hash": account.password_hash,
        "created": account.created,
        "admin": account.admin,
        "disabled": account.disabled,
        "workspace_roots": list(account.workspace_roots),
        "limits": dict(account.limits),
    }


# -- the store ----------------------------------------------------------------


@dataclass
class AccountStore:
    """`accounts.json`, and the bootstrap token that exists while it is empty.

    Not a `Store` table. `Store` is the console's *per-owner* record store
    after this chapter, and the list of owners cannot live inside one of them;
    the two also want different durability, because a half-written
    `threads.json` costs a session and a half-written `accounts.json` costs
    everybody the ability to log in.
    """

    path: Path
    #: Printed once at startup while there is nobody to log in as. Held in
    #: memory only: a bootstrap token written to disk is a password file with
    #: the door left open, and a restart is the right way to get a new one.
    bootstrap_token: str | None = field(default=None, repr=False)

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # Deliberately *not* the `_load` degradation `store.py` uses. There
            # a corrupt file means an empty list of threads; here it would mean
            # an empty list of accounts, which this class treats as "nobody has
            # registered yet" -- and that would turn a truncated write into a
            # console that hands out a fresh bootstrap token to whoever asks.
            raise AccountError(
                f"{self.path} is unreadable; refusing to treat it as empty"
            ) from None
        return list(data) if isinstance(data, list) else []

    def _write(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(records, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    # -- reading -------------------------------------------------------------

    def all(self) -> list[Account]:
        return [_from_json(raw) for raw in self._read()]

    def get(self, key: str) -> Account | None:
        return next((a for a in self.all() if a.key == key), None)

    def empty(self) -> bool:
        return not self._read()

    # -- writing -------------------------------------------------------------

    def create(
        self,
        key: str,
        password: str,
        *,
        admin: bool = False,
        workspace_roots: tuple[str, ...] = (),
    ) -> Account:
        key = key.strip().lower()
        if not _HANDLE.match(key):
            raise AccountError(
                f"{key!r} is not a usable account name: 1-64 characters of "
                "a-z, 0-9, hyphen or underscore, starting with a letter or digit"
            )
        if key in RESERVED_KEYS:
            raise AccountError(f"{key!r} is reserved")
        try:
            Owner(key)
        except OwnerError as exc:  # pragma: no cover - _HANDLE is the stricter of the two
            raise AccountError(str(exc)) from exc
        records = self._read()
        if any(r["key"] == key for r in records):
            raise AccountError(f"account {key!r} already exists")
        account = Account(
            key=key,
            password_hash=hash_password(password),
            created=time.time(),
            admin=admin,
            workspace_roots=workspace_roots,
        )
        records.append(_to_json(account))
        self._write(records)
        return account

    def update(self, key: str, **changes: Any) -> Account | None:
        records = self._read()
        found = None
        for raw in records:
            if raw["key"] == key:
                raw.update(changes)
                found = _from_json(raw)
        if found is not None:
            self._write(records)
        return found

    def set_password(self, key: str, password: str) -> Account | None:
        return self.update(key, password_hash=hash_password(password))

    # -- the login ------------------------------------------------------------

    def verify(self, key: str, password: str) -> Account | None:
        """The one place a password is checked.

        The hash comparison inside `verify_password` is constant time, and on
        its own that buys nothing here: with a plain `if account is None:
        return None` in front of it, an unknown name answers in microseconds
        and a known name answers in the tens of milliseconds scrypt costs, so
        the *timing of the miss* enumerates the account list. Which is worse
        than it sounds for this console, because an account key is also a
        directory name and a tenancy.

        So a miss verifies the password against a hash of something nobody
        knows and throws the answer away. Every login now costs one scrypt run,
        which is exactly the property being paid for.
        """
        account = self.get(key.strip().lower())
        if account is None:
            verify_password(password, _dummy_hash())
            return None
        if not verify_password(password, account.password_hash):
            return None
        if account.disabled:
            return None
        return account

    # -- first run ------------------------------------------------------------

    def issue_bootstrap_token(self) -> str | None:
        """A one-time token, in memory, only while there is nobody to log in as.

        The alternatives are worse in a way that is worth spelling out. A
        default password (`admin`/`admin`) is a published credential on every
        install that skipped the second step. An unauthenticated "create the
        first account" route is the same thing without the password: whoever
        reaches the port first becomes the operator, and on a machine with any
        other user on it that is not necessarily you. A token printed to the
        terminal that started the process binds the first account to whoever
        can read that terminal, which is the same person who decided to start
        the server. Jupyter reached this conclusion first; the reasoning is
        general.
        """
        if not self.empty():
            self.bootstrap_token = None
            return None
        if self.bootstrap_token is None:
            self.bootstrap_token = secrets.token_urlsafe(24)
        return self.bootstrap_token

    def redeem_bootstrap(self, token: str, key: str, password: str) -> Account:
        expected = self.bootstrap_token
        if expected is None or not self.empty():
            raise AccountError("this console already has an account; sign in instead")
        if not hmac.compare_digest(token, expected):
            raise AccountError("that is not the bootstrap token this server printed")
        account = self.create(key, password, admin=True)
        # Spent. A token that still works after it has been used is a second
        # password with none of a password's protections.
        self.bootstrap_token = None
        return account


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "RESERVED_KEYS",
    "SCRYPT_N",
    "Account",
    "AccountError",
    "AccountStore",
    "hash_password",
    "verify_password",
]
