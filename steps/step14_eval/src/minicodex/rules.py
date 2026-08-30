"""Approvals the user does not have to give twice.

A prompt on every command is a prompt nobody reads.  The failure mode is not
that the user gets annoyed; it is that after the fifteenth `pytest -q` they
stop reading the command and start reading the shape of the prompt, and the
sixteenth one -- the one that was different -- gets the same reflex `y`.  An
approval mechanism that is answered reflexively has the same security value as
no approval mechanism, and costs more.

So an approval can be remembered.  Which immediately creates the opposite
problem, and it is worse: a remembered rule is a decision that keeps applying
long after everyone has forgotten making it.  Three things keep that honest.

**Scope.**  `session` dies with the process.  `project` is written to disk and
survives, and is offered second, because the cost of a too-broad rule scales
with how long it lives.

**Attribution.**  Every rule records when it was created and which command
produced it.  "Why is `cargo test` allowed?" has an answer.

**A refusal to write the dangerous ones.**  `remember()` rejects rules that
would hand over more than the command that prompted them -- an interpreter, a
destructive command, or a bare tool name that covers all of its subcommands.
codex's prompt gives the model the same list from the other side, under the
heading "Banned prefix_rules": not `["python3"]`, not `["python", "-"]`, and
"NEVER provide a prefix_rule argument for destructive commands like rm".
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from minicodex.policy import INTERPRETERS, NETWORK, WRITES

Scope = Literal["session", "project"]

# Programs whose risk is decided by the word after them.  A rule naming only
# the program hands over every subcommand it has, including the ones added in
# next year's release: `("git",)` covers `git push --force`, `("npm",)` covers
# `npm publish`.
#
# The first version of this refused *every* one-word rule unless the program
# was on the read-only list, which was tidier and wrong.  It made `("pytest",)`
# impossible, and `pytest` is the single most common thing a developer gets
# asked about -- so the mechanism built to end approval fatigue could not be
# applied to the command that causes it.
#
# `("pytest",)` is allowed, and it is a real grant: pytest runs whatever is in
# `conftest.py` and takes flags this project has never seen.  There is no way
# to make that decision safely on the user's behalf, which is the reason a rule
# records who made it and when, and why `minicodex forget` exists.
SUBCOMMAND_TOOLS = NETWORK | {"git"}

DEFAULT_RULES_PATH = Path(".minicodex") / "rules.json"


class RuleRefused(ValueError):
    """`remember()` would have stored a rule broader than the command it came from."""


@dataclass(frozen=True)
class Rule:
    """A prefix that auto-approves any segment starting with these words.

    Words, not a string.  A string prefix is the fault this chapter opens with:
    `"git status"` is a prefix of `"git status; rm -rf /"`.  Words are matched
    against a segment that the parser has already split, so there is nothing
    left in the segment for a separator to hide in.
    """

    words: tuple[str, ...]
    scope: Scope
    # Why it exists, kept verbatim.  A rule with no story behind it is a rule
    # nobody can decide whether to revoke.
    prompted_by: str
    created_at: str

    def matches(self, segment: list[str]) -> bool:
        return tuple(segment[: len(self.words)]) == self.words

    def describe(self) -> str:
        return (
            f"{' '.join(self.words)}  [{self.scope}, added {self.created_at} "
            f"for: {self.prompted_by}]"
        )

    def to_json(self) -> dict[str, object]:
        return {
            "words": list(self.words),
            "scope": self.scope,
            "prompted_by": self.prompted_by,
            "created_at": self.created_at,
        }


def check_rule(words: tuple[str, ...]) -> None:
    """Raise `RuleRefused` if this prefix would give away more than one command.

    Separate from `remember()` so the approval prompt can decide *before*
    offering "always" whether that option is even on the table.  Offering a
    choice and then refusing it is how you teach someone to ignore the refusal.
    """
    if not words:
        raise RuleRefused("an empty rule matches every command")

    name = words[0].rsplit("/", 1)[-1]
    if name in INTERPRETERS:
        raise RuleRefused(
            f"{name!r} takes a program as an argument, so a rule for it approves "
            "every program that will ever be passed to it"
        )
    if name in WRITES:
        raise RuleRefused(
            f"{name!r} destroys things, and the arguments are where the damage lives; "
            "approve each one"
        )
    if len(words) == 1 and name in SUBCOMMAND_TOOLS:
        raise RuleRefused(
            f"a one-word rule for {name!r} covers every subcommand it has, "
            "including the ones you have not seen yet -- name the subcommand too"
        )


class RuleStore:
    """Session rules in memory, project rules on disk.

    Loading is best-effort in the same sense the recorder is: a rules file that
    someone hand-edited into invalid JSON must not stop the agent from
    starting.  It must not silently grant anything either, so the failure mode
    is an empty store -- every command asks -- and not a partial one.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._rules: list[Rule] = []
        if path is not None and path.exists():
            self._rules.extend(_load(path))

    def __len__(self) -> int:
        return len(self._rules)

    def all(self) -> list[Rule]:
        return list(self._rules)

    def allows(self, segment: list[str]) -> Rule | None:
        return next((rule for rule in self._rules if rule.matches(segment)), None)

    def allows_every(self, segments: list[list[str]]) -> bool:
        """Every segment, not any segment.

        `git status | rm -rf /` has a segment covered by a `git status` rule
        and one that is not.  Approving on `any` would auto-run the second.
        """
        return bool(segments) and all(self.allows(segment) for segment in segments)

    def remember(self, words: tuple[str, ...], *, scope: Scope, prompted_by: str) -> Rule:
        check_rule(words)
        rule = Rule(
            words=words,
            scope=scope,
            prompted_by=prompted_by,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self._rules.append(rule)
        if scope == "project":
            self._save()
        return rule

    def forget(self, index: int) -> Rule:
        """Revoke by the number `--list-rules` printed.

        Revocation is not a nicety.  A rule that can only be removed by finding
        and editing a JSON file is a rule that stays.
        """
        rule = self._rules.pop(index)
        if rule.scope == "project":
            self._save()
        return rule

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        kept = [rule.to_json() for rule in self._rules if rule.scope == "project"]
        self.path.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")


def _load(path: Path) -> list[Rule]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []

    rules: list[Rule] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("words"), list):
            continue
        words = tuple(str(word) for word in item["words"])
        try:
            # A rule that would be refused today is refused on load too.  The
            # file is writable by whoever owns the machine, and "it was already
            # in the file" is not a reason to honour `["python3"]`.
            check_rule(words)
        except RuleRefused:
            continue
        rules.append(
            Rule(
                words=words,
                scope="project",
                prompted_by=str(item.get("prompted_by", "(unrecorded)")),
                created_at=str(item.get("created_at", "(unrecorded)")),
            )
        )
    return rules
