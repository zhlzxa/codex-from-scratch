"""Whose data is this.

For nineteen chapters the answer was "the person at the keyboard", and it was
never written down anywhere because it never had to be.  One machine, one
home directory, one `~/.minicodex/`.  Every path in the program is either
relative to a repository or absolute under that home, and both spellings mean
the same person.

Chapter 19 put the agent behind an HTTP server, and chapter 16 had just moved
memory from the repository to the home directory -- correctly, because codex
keeps it at `codex_home.join("memories")` and a memory is a fact about a
person rather than about a checkout.  Put those two changes together and
`web/runtime.py:load_feature_dirs` ends up documenting this, in a docstring
that reads as a design note:

    Memory is global -- `~/.minicodex/memories` ... -- so every workspace this
    server hosts shares one, exactly as every project on one machine shares
    one in codex.

Every sentence there is true, and the conclusion is a data leak.  "Every
project on one machine" is one person's projects.  "Every workspace this
server hosts" is *everybody's*.  The port was faithful; the trust model moved
underneath it, and nothing in the type system noticed because there was never
a type for "who".

That is what this module adds, and all it adds.  `Owner` is a name plus the
paths that belong to it.  The default owner resolves to exactly the paths
chapters 16 and 17 already use, byte for byte, so the CLI is unchanged and
every test written before this chapter still describes the program it was
written for.

**Scope, stated because its absence would look like an oversight**: this is
the *seam*, not a multi-user system.  There is no registration here, no
session, no password, no quota, and `Owner.key` is trusted completely by
whoever constructs it.  A server that derives an owner from an unauthenticated
request header has moved the leak, not closed it.  Authentication and quotas
are chapter 23; this chapter's job is to make sure that when they arrive there
is somewhere to put them.

They arrived.  `web/accounts.py` is the thing that constructs an `Owner`, and
`web/auth.py` is what makes constructing one require a password first.  Nothing
in this file changed to make that possible, which was the point of writing it
three chapters early.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from minicodex.memory import MINICODEX_HOME

# What a key may contain.  Deliberately far narrower than "a valid filename":
# this string becomes a path segment, and the whole value of the seam is lost
# if `..` or an absolute path can travel through it.  Chapter 18 learned this
# on a skill name that arrived from the model (F18-04, F18-12) and closed it
# by looking the name up in an already-parsed table rather than rebuilding a
# path from it.  Here there is no table to look up in -- an owner key is
# genuinely new data -- so the check is on the characters themselves.
#
# Lower case only, and that half is a *containment* rule rather than a taste
# in names.  Two owners are separated by nothing except the spelling of one
# path segment, and on a case-insensitive filesystem two spellings are one
# directory.  Measured on this machine (NTFS): `mkdir alice` then
# `mkdir Alice` does not make a second directory, it raises `IOException` and
# leaves `alice` -- so `Owner("Alice")` and `Owner("alice")` would read and
# write each other's memories, on Windows and on a default macOS volume,
# while staying correctly separate on Linux.  A leak that appears only on
# some filesystems is the worst shape this chapter has to offer: it would
# pass every test on the machine the server does not run on.
#
# Anyone widening this pattern later has to keep that property.  Allowing
# upper case reopens the leak; the fix would be to case-fold the key before
# it ever becomes a path, not to trust the filesystem to keep them apart.
_SAFE_KEY = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,63}\Z")

# Where per-owner state lives when there is more than one owner.  A single
# extra directory level, not a parallel tree: `~/.minicodex/tenants/<key>/`
# holds exactly what `~/.minicodex/` holds for the single-tenant case, so the
# two layouts differ by a prefix and nothing else.  Anything that reads one
# can read the other.
TENANTS_DIR = "tenants"


class OwnerError(ValueError):
    """An owner key this module refuses to turn into a path."""


@dataclass(frozen=True)
class Owner:
    """One tenant, and where its state lives.

    `key=None` is the single-tenant case and is the default everywhere, which
    is what keeps this chapter backward compatible: `Owner().memories()` is
    `~/.minicodex/memories`, the exact path `memory.DEFAULT_MEMORY_DIR` names.

    Frozen, and the paths are methods rather than fields, because an `Owner`
    is an identity and not a bag of directories -- two `Owner("alice")` values
    built in different modules must be equal and must resolve identically.
    """

    key: str | None = None
    home: Path = MINICODEX_HOME

    def __post_init__(self) -> None:
        if self.key is None:
            return
        if not _SAFE_KEY.match(self.key):
            raise OwnerError(
                f"owner key {self.key!r} is not usable as a directory name: "
                "use 1-64 characters of a-z, 0-9, hyphen or underscore, "
                "starting with a letter or digit"
            )

    def root(self) -> Path:
        """The directory every other path in this class hangs off."""
        if self.key is None:
            return self.home
        return self.home / TENANTS_DIR / self.key

    # -- the four things that were global ------------------------------------

    def memories(self) -> Path:
        """Chapter 16's read path, and chapter 17's write target."""
        return self.root() / "memories"

    def jobs_db(self) -> Path:
        """Chapter 17's claim/lease table.

        Per owner for the same reason the memory directory is: the table
        records which sessions have already been turned into memory, and one
        table shared by two people would let one person's extraction mark
        another person's session as done.
        """
        return self.root() / "memory_jobs.sqlite3"

    def merge_lock(self) -> Path:
        """Chapter 17's single-writer lock.

        The sharpest of the four.  One lock for the whole machine is correct
        when there is one memory directory: it is what stops two sessions
        merging into the same files at once.  With one lock and *many* memory
        directories it stops being a correctness device and becomes a queue --
        owner A's merge blocks owner B's, for no reason except that they share
        a server.
        """
        return self.root() / "memory_merge.lock"

    def mcp_tokens(self) -> Path:
        """Chapter 21's OAuth credentials for remote MCP servers.

        The fourth thing to hang off an owner, and the first that is a
        *credential* rather than data.  That is a sharper version of the same
        question chapter 20 asked: a leaked memory is embarrassing, a leaked
        token acts on someone's behalf.

        Added as one method, which is the evidence that the seam was drawn in
        the right place -- nothing else in `mcp_oauth.py` knows whether the
        deployment has one user or a hundred.
        """
        return self.root() / "mcp_tokens.json"

    def describe(self) -> str:
        who = "single-tenant" if self.key is None else f"owner {self.key!r}"
        return f"{who} at {self.root()}"


#: The owner the CLI runs as.  A module-level value rather than a default
#: argument so that `is DEFAULT_OWNER` is a meaningful question -- "did this
#: caller think about tenancy, or inherit it".
DEFAULT_OWNER = Owner()


__all__ = [
    "DEFAULT_OWNER",
    "TENANTS_DIR",
    "Owner",
    "OwnerError",
]
