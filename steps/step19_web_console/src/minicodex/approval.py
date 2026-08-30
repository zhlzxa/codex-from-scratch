"""The one door.

`policy.py` says what a command is.  `rules.py` says what has already been
agreed.  This module is where they meet a human, and -- more importantly --
it is the *only* place any of that happens.

That single-entry-point property is the abstraction this chapter pays for, and
it is the third of the three exceptions from chapter -1: a rule that is not
enforced in one place is a rule that eight call sites maintain separately.
`run_shell` and `apply_patch` both need it today; every tool added after this
chapter will need it too, and the way to make that automatic is to leave no
second route to the subprocess.

The other two exceptions apply as well, which is unusual and worth naming:
the command string is model output crossing a trust boundary (exception 2),
and "nothing executes unapproved" is an invariant (exception 3).  Three out of
three is why this is not a premature abstraction at the first call site.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from minicodex.policy import (
    ApprovalPolicy,
    Decision,
    Risk,
    SandboxMode,
    judge_command,
    judge_write,
)
from minicodex.rules import RuleRefused, RuleStore, Scope, check_rule
from minicodex.shell_parse import segments
from minicodex.tool_errors import permission_error


@dataclass
class Session:
    """What this conversation is currently allowed to do.

    Mutable, and the only mutable thing in this module.  It has to be: the
    whole point of `request_permissions` is that the answer to "may I use the
    network" can be different at turn 9 than it was at turn 1.  Freezing it and
    rebuilding it would mean threading a new object back out through every
    tool handler, for no gain -- there is one of these per conversation and one
    conversation per process.

    `rules` and `approver` live here too rather than being passed alongside,
    because they have exactly the same lifetime and passing four things that
    always travel together is how you end up with a call site that forgets one.
    """

    mode: SandboxMode = "read-only"
    policy: ApprovalPolicy = "on-request"
    rules: RuleStore = field(default_factory=RuleStore)
    # Fails closed.  See `DenyAll`.
    approver: Approver = field(default_factory=lambda: DenyAll())

    def describe(self) -> str:
        return f"sandbox_mode={self.mode}, approval_policy={self.policy}"


@dataclass(frozen=True)
class ApprovalRequest:
    what: str
    reason: str
    risk: Risk
    # The prefix worth offering as a rule, or None when no rule may be made for
    # this command.  Computed before the prompt is drawn, so the prompt never
    # offers an option that would then be refused.
    suggested_rule: tuple[str, ...] | None


@dataclass(frozen=True)
class ApprovalReply:
    approved: bool
    # What the user actually agreed to run, which is not always what was asked.
    command: str
    remember: Scope | None = None


class Approver(Protocol):
    """Anything that can answer a yes/no about running something.

    A Protocol for the same reason `Model` is one: the tests need a
    deterministic implementation today, not hypothetically.  Structural typing
    means the test doubles below inherit nothing.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    # The command to actually run.  Not the same string that came in when the
    # user edited it at the prompt.
    command: str
    # Set when the answer was no.  Written for the model, and deliberately not
    # shaped like an ordinary tool error -- see `tool_errors.permission_error`.
    denial: str | None = None
    # Set when the user changed the command.  Prepended to whatever the command
    # produced, because a model that is not told will report on the command it
    # asked for.
    note: str | None = None


def _looks_like_a_subcommand(word: str) -> bool:
    """`test`, `pr`, `run` yes.  `-q`, `tests/test_patch.py`, `--all` no.

    The distinction matters in both directions.  Including a flag makes the
    rule so specific that `pytest -x` asks again, which rebuilds the fatigue
    the rule existed to remove.  Including a path makes it specific to one
    file, with the same result.
    """
    return bool(word) and not word.startswith("-") and "/" not in word and "." not in word


def _suggested_rule(command: str) -> tuple[str, ...] | None:
    """The prefix worth remembering, or None if none is.

    The program plus up to two subcommand-shaped words after it.  This is the
    shape of codex's own examples -- `["cargo", "test"]`, `["gh", "pr", "check"]`,
    `["npm", "run", "dev"]` -- and it lands between the two ways of getting this
    wrong: `("cargo",)` is every subcommand cargo will ever have, and
    `("cargo", "test", "--all-features")` will not match the next invocation.
    """
    parts = segments(command)
    if parts is None or len(parts) != 1:
        # A multi-segment command is a composition, and a rule made from its
        # first words would silently cover whatever the user pipes into it
        # next time.
        return None

    words = [parts[0][0]]
    for word in parts[0][1:3]:
        if not _looks_like_a_subcommand(word):
            break
        words.append(word)

    try:
        check_rule(tuple(words))
    except RuleRefused:
        return None
    return tuple(words)


async def gate_command(command: str, session: Session) -> GateResult:
    verdict = judge_command(command, mode=session.mode, policy=session.policy)

    if verdict.decision is Decision.ALLOW:
        return GateResult(True, command)

    # Rules are consulted after the policy and before the human.  They can turn
    # a question into a yes; they can never turn a denial into a yes, because
    # `never` means there was nobody there to make the rule mean anything.
    parts = segments(command)
    if verdict.decision is Decision.ASK and parts is not None and session.rules.allows_every(parts):
        return GateResult(True, command)

    if verdict.decision is Decision.DENY:
        return GateResult(False, command, denial=_denial(command, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=command,
            reason=verdict.reason,
            risk=verdict.risk,
            suggested_rule=_suggested_rule(command),
        )
    )
    if not reply.approved:
        return GateResult(False, command, denial=_denial(command, "the user declined", session))

    if reply.remember is not None:
        # The rule is made from what the user *agreed to*, not from what the
        # model asked for.  Editing the command and choosing "always" otherwise
        # remembers the version that was rejected.
        remembered = _suggested_rule(reply.command)
        if remembered is not None:
            session.rules.remember(remembered, scope=reply.remember, prompted_by=reply.command)

    note = None
    if reply.command != command:
        note = (
            f"Note: the user changed your command before running it. "
            f"You asked for: {command!r}. What actually ran: {reply.command!r}. "
            "The output below is from the command that ran."
        )
    return GateResult(True, reply.command, note=note)


async def gate_write(describe: str, session: Session) -> GateResult:
    verdict = judge_write(mode=session.mode, policy=session.policy)
    if verdict.decision is Decision.ALLOW:
        return GateResult(True, describe)
    if verdict.decision is Decision.DENY:
        return GateResult(False, describe, denial=_denial(describe, verdict.reason, session))

    reply = await session.approver.ask(
        ApprovalRequest(
            what=describe, reason=verdict.reason, risk=verdict.risk, suggested_rule=None
        )
    )
    if not reply.approved:
        return GateResult(False, describe, denial=_denial(describe, "the user declined", session))
    return GateResult(True, describe)


# -- asking for more -------------------------------------------------------

# What `request_permissions` may be asked for, and what granting it means.
UPGRADES: dict[str, SandboxMode] = {
    "write-files": "workspace-write",
    "unrestricted": "full-access",
}


async def request_upgrade(session: Session, *, needs: str, why: str) -> str:
    """Raise the session's permissions, if a human says so.

    This exists because of the deadlock it prevents.  Without it, an agent that
    hits a wall has two options, and both are bad: keep retrying the thing that
    is refused, or stop and report failure on a task it could have finished.
    Neither is "ask", because until now there was nothing to ask with.

    `why` is required and is shown to the user verbatim.  A request with no
    stated reason is one the user can only answer by guessing.
    """
    if needs not in UPGRADES:
        return permission_error(
            f"there is no permission called {needs!r}",
            do_this=f"Ask for one of: {', '.join(sorted(UPGRADES))}.",
        )
    if not why.strip():
        return permission_error(
            "a permission request has to say what it is for",
            do_this='Send why="I need to run the test suite, which writes to .pytest_cache".',
        )

    target = UPGRADES[needs]
    if _rank(target) <= _rank(session.mode):
        return f"Already granted: {session.describe()}. Nothing changed; go ahead."

    if session.policy == "never":
        return permission_error(
            "there is nobody to ask in this session",
            do_this=(
                "Permissions cannot change here. Finish what you can within "
                f"{session.mode}, and say plainly in your final answer what you could not do."
            ),
        )

    reply = await session.approver.ask(
        ApprovalRequest(
            what=f"raise permissions to {target}",
            reason=f"the agent says: {why.strip()}",
            risk=Risk.UNKNOWN,
            suggested_rule=None,
        )
    )
    if not reply.approved:
        return permission_error(
            "the user did not grant that",
            do_this=(
                f"Do not ask again for the same thing. Work within {session.mode}, "
                "and if the task cannot be finished that way, say so and stop."
            ),
        )

    session.mode = target
    return f"Granted. Now: {session.describe()}."


def _rank(mode: SandboxMode) -> int:
    return ("read-only", "workspace-write", "full-access").index(mode)


# -- telling the model ------------------------------------------------------

_MODE_MEANING: dict[SandboxMode, str] = {
    "read-only": (
        "you can read files and run commands that only read. Editing files and "
        "running commands that change anything both need approval."
    ),
    "workspace-write": (
        "you can read files and edit files inside this repository with "
        "`apply_patch`. A *shell command* that changes anything still needs "
        "approval, because a shell command's paths cannot be checked in advance."
    ),
    "full-access": "nothing is restricted. Be careful; nobody is checking after you.",
}

_POLICY_MEANING: dict[ApprovalPolicy, str] = {
    # Does not name `request_permissions`, even to say it will not work: under
    # `never` the tool is still in the schema, and a sentence containing the
    # name is a sentence that can be read as an instruction to use it.
    "never": "there is nobody to ask. Anything the sandbox does not already allow is refused.",
    "on-request": "a human is here and will be asked about anything the sandbox does not allow.",
    "unless-trusted": (
        "a human is here and will be asked about everything except commands that only read."
    ),
}


def permissions_block(session: Session, *, can_request: bool = True) -> str:
    """Render the current permission state for the system prompt.

    Generated from the same constants the gate uses, never typed out twice.
    Chapter 3 found a defaulted number copied into a description and going
    stale; a permission state copied into a prompt would go stale the first
    time `request_permissions` succeeded, and the model would be told it cannot
    do the thing it just asked for and got.

    `can_request` exists because the probe caught this block lying.  The first
    version ended "or call `request_permissions`" unconditionally.  Run against
    a real model with that tool deliberately absent, gemma4 called it anyway,
    2 samples out of 3 -- and in the real agent that lands on chapter 0's
    "no tool named 'request_permissions'" error, which is the message written
    for a model that *invented* a name.  It did not invent it; the prompt told
    it to.  A prompt that names a tool the model does not have is a prompt that
    spends a turn of the budget teaching it a lie.
    """
    from minicodex import permissions_prompt

    if session.policy == "never":
        what_to_do = (
            "Nothing can change that in this session, so do not ask. Work within "
            "the current permissions and say plainly what you could not do."
        )
    elif can_request:
        what_to_do = (
            "When you see one, either do the task another way, or call "
            "`request_permissions` with what you need and why."
        )
    else:
        what_to_do = "When you see one, do the task another way, or stop and explain."

    return permissions_prompt().format(
        mode=session.mode,
        mode_meaning=_MODE_MEANING[session.mode],
        policy=session.policy,
        policy_meaning=_POLICY_MEANING[session.policy],
        what_to_do=what_to_do,
    )


def _denial(what: str, reason: str, session: Session) -> str:
    """The message the model gets when the answer is no.

    The last sentence depends on the policy, and that dependency was measured
    rather than designed.  The first version always ended "or call
    request_permissions".  Under `never` there is nobody to grant anything, and
    gpt-4o-mini responded to the sentence by sending

        run_shell({"command": "request_permissions"})

    -- trying to run the tool as a shell command, because the message named it
    and nothing said where it lived.  Naming a capability is an instruction to
    use it, so it is only named when using it can work.  The same mistake in
    the same chapter, found the same way: see `permissions_block`.
    """
    if session.policy == "never":
        do_this = (
            "This is a permissions decision, not a failure of the command, and nothing "
            "in this session can change it -- running it again will be refused again. "
            "Do the task a way the current permissions allow, or stop and say plainly "
            "what you could not do."
        )
    else:
        do_this = (
            "This is a permissions decision, not a failure of the command -- running "
            "it again unchanged will be refused again. Either do the task a way the "
            "current permissions allow, or call the request_permissions tool to ask "
            "for what you need and say why."
        )
    return permission_error(f"that was not run, because {reason}", you_sent=what, do_this=do_this)


# -- implementations -------------------------------------------------------


class DenyAll:
    """The default, and the reason there is a default at all.

    An `Agent` constructed without an approver must not be an `Agent` that runs
    everything.  Failing closed makes a forgotten wire-up show up as a task
    that cannot act, which somebody notices, rather than as a sandbox that is
    not there, which nobody does.
    """

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(False, request.what)


class AllowAll:
    """For tests that are about something else, and for `--yes`."""

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return ApprovalReply(True, request.what)


class CliApprover:
    """Ask on the terminal.

    `input()` blocks, and a blocking call inside `async def` stalls the whole
    event loop -- the same rule chapter 1 hit with `Path.read_text()` and
    chapter 2 with `subprocess.Popen`.  Here it is arguably harmless, since
    there is nothing to do while a human decides, but ruff's ASYNC rules do not
    know that and neither will the next reader.  `to_thread` costs one line.
    """

    def __init__(self, *, stream_in=None, stream_out=None) -> None:
        self._in = stream_in
        self._out = stream_out

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        return await asyncio.to_thread(self._ask_blocking, request)

    def _ask_blocking(self, request: ApprovalRequest) -> ApprovalReply:
        import sys

        out = self._out or sys.stdout
        read = self._in.readline if self._in is not None else sys.stdin.readline

        print(f"\n  the agent wants to run:\n    {request.what}", file=out)
        print(f"  {request.reason}", file=out)
        choices = ["[y] once", "[n] no", "[e] edit"]
        if request.suggested_rule is not None:
            prefix = " ".join(request.suggested_rule)
            choices[1:1] = [
                f"[s] always this session ({prefix})",
                f"[p] always in this project ({prefix})",
            ]
        print("  " + "  ".join(choices), file=out)
        out.flush()

        answer = (read() or "n").strip().lower()

        if answer == "e":
            print("  new command: ", file=out, end="")
            out.flush()
            edited = (read() or "").strip()
            # An empty edit is not an approval of the original.  Treating it as
            # one turns a slip of the return key into a yes.
            return ApprovalReply(bool(edited), edited or request.what)
        if answer == "s" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="session")
        if answer == "p" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="project")
        return ApprovalReply(answer == "y", request.what)
