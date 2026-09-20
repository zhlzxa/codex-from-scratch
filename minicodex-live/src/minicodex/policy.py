"""What a command is allowed to do, and who gets asked.

Two knobs, borrowed from codex because the split is the right one:

  `SandboxMode`     what the agent may do without anyone being asked
  `ApprovalPolicy`  what happens to everything else

They are separate because they answer different questions.  "May this agent
write files?" is about the task.  "Is there a human at the keyboard right now?"
is about the session.  A CI run wants `read-only` + `never`; a developer
watching the terminal wants `workspace-write` + `on-request`.  Folding them
into one setting makes the four useful combinations unreachable.

Both are string literals, not classes.  Three values that carry no behaviour
of their own are data; wrapping each in a class so `judge_command` can call
`mode.check()` would put three files where three strings do.  The dispatch
below is a `match`, in one place, and stays readable at three values.  If a
fourth arrives with real behaviour behind it, that is the moment to
reconsider -- the third repetition, not the second.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from minicodex.shell_parse import SEPARATORS, segments

SandboxMode = Literal["read-only", "workspace-write", "full-access"]
ApprovalPolicy = Literal["never", "on-request", "unless-trusted"]

SANDBOX_MODES: tuple[SandboxMode, ...] = ("read-only", "workspace-write", "full-access")
APPROVAL_POLICIES: tuple[ApprovalPolicy, ...] = ("never", "on-request", "unless-trusted")


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Risk(Enum):
    """What a single command segment does, worst case.

    Ordered: a command is as risky as the riskiest thing in it, and `max()`
    over this enum is how the segments of one command are combined.
    """

    READ = 0
    UNKNOWN = 1
    WRITE = 2
    NETWORK = 3
    INTERPRETER = 4

    def __lt__(self, other: Risk) -> bool:
        return self.value < other.value


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    risk: Risk
    # One sentence, written for two readers: the human at the approval prompt,
    # who needs to know what they are agreeing to, and the model, which gets it
    # back as tool output when the answer is no.
    reason: str


# -- the tables ------------------------------------------------------------
#
# Every table below is an allowlist except `INTERPRETERS`, and that one is not
# an exception so much as the proof of the rule: a command not on any list is
# UNKNOWN, which asks.  `INTERPRETERS` exists to stop a *rule* from ever being
# remembered for one of these, which is a different question -- see
# `rules.remember`.

# Commands that read and print.  Deliberately short.  Anything that takes a
# `--output`, an `-exec` or a `-i` belongs elsewhere, which is why `find`,
# `sed` and `rg` are absent: they are read-only *most* of the time, and "most
# of the time" is not a property an allowlist can express.
READ_ONLY = frozenset(
    {
        "basename",
        "cat",
        "cd",
        "date",
        "dirname",
        "echo",
        "false",
        "grep",
        "head",
        "ls",
        "nl",
        "pwd",
        "sort",
        "tail",
        "tree",
        "true",
        "uname",
        "uniq",
        "wc",
        "which",
        "whoami",
    }
)

# Commands whose whole purpose is to change something on disk.
WRITES = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "kill",
        "ln",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "shred",
        "tee",
        "touch",
        "truncate",
    }
)

# Commands that can move bytes off this machine, or pull code onto it.
# `pip` and `npm` are here whatever their subcommand: `pip download` fetches,
# `pip install` fetches *and executes* setup code.
NETWORK = frozenset(
    {
        "cargo",
        "curl",
        "gh",
        "nc",
        "ncat",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "rsync",
        "scp",
        "sftp",
        "ssh",
        "telnet",
        "uv",
        "wget",
        "yarn",
    }
)

# Anything that takes a program as an argument.  This cannot be handled by
# parsing: `python -c 'open("x","w")'` tokenises into three clean words, and
# no amount of shell analysis reaches inside the third one.  The interpreter
# is the boundary; past it, this module is blind.
#
# codex says the same thing in its own prompt, from the other direction --
# under "Banned prefix_rules": do not request `["python3"]`, `["python", "-"]`,
# "or other similar prefixes that would allow arbitrary scripting".
INTERPRETERS = frozenset(
    {
        "ash",
        "awk",
        "bash",
        "csh",
        "dash",
        "deno",
        "env",
        "eval",
        "exec",
        "fish",
        "irb",
        "ksh",
        "node",
        "perl",
        "php",
        "python",
        "python2",
        "python3",
        "ruby",
        "sh",
        "source",
        "tclsh",
        "xargs",
        "zsh",
    }
)

# -- git, which is not one command ----------------------------------------

# `git` on its own says nothing.  `git status` reads; `git push --force`
# rewrites someone else's history.  codex resolves this the same way, in
# `shell-command/src/command_safety/is_safe_command.rs`.
GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {"blame", "branch", "diff", "log", "ls-files", "rev-parse", "show", "status"}
)

# Subcommands that talk to a remote.  `git push --force` is the example the
# fault list names, and classifying it as a local write would understate it by
# a lot: the damage is on a server, and it is not undone by editing a file back.
GIT_NETWORK_SUBCOMMANDS = frozenset({"clone", "fetch", "pull", "push", "remote", "submodule"})

# Global options that appear *before* the subcommand and change what git
# operates on or runs.  `git -C /elsewhere status` reads a different
# repository; `git -c core.pager='rm -rf /' log` runs a command.  A checker
# that finds `status` and stops has approved neither of those.
GIT_UNSAFE_GLOBAL_OPTIONS = (
    "-C",
    "-c",
    "-p",
    "--paginate",
    "--git-dir",
    "--work-tree",
    "--exec-path",
    "--namespace",
    "--config-env",
    "--super-prefix",
)

# `git branch` lists branches; `git branch -d x` deletes one.  Same subcommand,
# and the difference is entirely in the arguments.
GIT_BRANCH_READ_ONLY_FLAGS = frozenset(
    {"--list", "-l", "--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose"}
)


def _git_risk(words: list[str]) -> Risk:
    for index, argument in enumerate(words[1:], start=1):
        if argument in GIT_UNSAFE_GLOBAL_OPTIONS or any(
            argument.startswith(f"{option}=") for option in GIT_UNSAFE_GLOBAL_OPTIONS
        ):
            return Risk.UNKNOWN
        if argument.startswith("-"):
            continue
        # The first bare word after `git` and its global options is the
        # subcommand.  `index` rather than `words.index(argument)`: the latter
        # finds the *first* occurrence, and `git log log` is a real thing to
        # type.
        if argument in GIT_NETWORK_SUBCOMMANDS:
            return Risk.NETWORK
        if argument not in GIT_READ_ONLY_SUBCOMMANDS:
            return Risk.WRITE
        rest = words[index + 1 :]
        if argument == "branch" and not all(
            flag in GIT_BRANCH_READ_ONLY_FLAGS or flag.startswith("--format=") for flag in rest
        ):
            return Risk.WRITE
        return Risk.READ
    return Risk.READ  # bare `git`, which prints usage


def classify(words: list[str]) -> Risk:
    """The worst thing one segment could do."""
    name = words[0].rsplit("/", 1)[-1]
    if name in INTERPRETERS:
        return Risk.INTERPRETER
    if name in NETWORK:
        return Risk.NETWORK
    if name in WRITES:
        return Risk.WRITE
    if name == "git":
        return _git_risk(words)
    if name in READ_ONLY:
        return Risk.READ
    return Risk.UNKNOWN


# -- putting it together ---------------------------------------------------

# What each sandbox mode lets a *shell command* do without asking.
#
# Note what `workspace-write` does not contain: `Risk.WRITE`.  That is not an
# oversight, it is the edge of what this layer can do.  "Write inside the
# workspace" is a statement about *where* bytes land, and for a shell command
# there is no way to find that out short of running it -- `rm -rf $HOME`,
# `rm -rf build` and `rm -rf ../../..` are the same shape, and a path argument
# is only a string until the shell expands it.
#
# The thing that enforces "inside the workspace" is an OS sandbox: seatbelt on
# macOS, landlock on Linux, a job object on Windows (this project ships bwrap
# on Linux; see `sandbox.py`).  Rather than let `workspace-write` quietly mean
# "write anywhere", a writing shell command asks in every mode except
# `full-access` -- the gap says its own name.
_SHELL_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ}),
    "workspace-write": frozenset({Risk.READ}),
    "full-access": frozenset(Risk),
}

# The same table, for a session whose shell the OS sandbox is actually
# confining (`sandbox.unavailable()` is None).  Three differences, and each
# is a thing the kernel now knows that the parser never could:
#
#   `workspace-write` gains WRITE   -- the bind set decides where bytes land,
#                                      so "inside the workspace" is enforced
#                                      rather than hoped for.
#   `workspace-write` gains
#   INTERPRETER                     -- `python -c "open(...)"` is the reason
#                                      WRITE could not be granted, and it is
#                                      confined by the same bind set.
#   `read-only` gains INTERPRETER   -- for the same reason, one mode down: an
#                                      interpreter under `--ro-bind / /` with
#                                      no writable hole cannot write anywhere.
#
# NETWORK stays out of both.  `--unshare-net` means a network command cannot
# reach anything, so it is not *dangerous* -- but it will fail in a way the
# model reads as a broken environment and retries forever.  Asking is how the
# user gets the chance to say "yes, and turn the network on".
_CONFINED_ALLOWED_BY_MODE: dict[SandboxMode, frozenset[Risk]] = {
    "read-only": frozenset({Risk.READ, Risk.INTERPRETER}),
    "workspace-write": frozenset({Risk.READ, Risk.WRITE, Risk.INTERPRETER}),
    "full-access": frozenset(Risk),
}

# `apply_patch` is the other half of that sentence.  Every path it touches goes
# through `paths.resolve()`, which resolves symlinks and `..` and then refuses
# anything outside the root -- so for this one tool, "inside the workspace" is
# a property the code can actually establish, and `workspace-write` means
# exactly what it says.
_WRITE_TOOL_ALLOWED_BY_MODE: dict[SandboxMode, bool] = {
    "read-only": False,
    "workspace-write": True,
    "full-access": True,
}

_WHY = {
    Risk.READ: "reads",
    Risk.UNKNOWN: "is not on any list, so what it does is unknown",
    Risk.WRITE: "changes files",
    Risk.NETWORK: "can reach the network",
    Risk.INTERPRETER: "runs an interpreter, which can do anything",
}


def judge_command(
    command: str,
    *,
    mode: SandboxMode,
    policy: ApprovalPolicy,
    confined: bool = False,
) -> Verdict:
    """Decide what happens to one `run_shell` command.

    Remembered rules are applied by the caller (`approval.gate`), not here, so
    that this function stays a pure statement of policy and can be tested
    without a rule store.

    `confined` says an OS sandbox is enforcing this mode -- the difference
    between the two mode tables below, and the reason `workspace-write` can
    allow writes at all.  With the kernel holding the boundary, the approval
    prompt for a bounded write stops being a way of finding out where bytes
    land and becomes a ritual.

    Defaults to `False`, so a caller who has not thought about it asks rather
    than allows.
    """
    parts = segments(command)
    if parts is None:
        return _apply_policy(
            Risk.UNKNOWN,
            policy,
            "this command uses shell syntax the checker does not model "
            "(a newline, a substitution, a redirect, a wildcard or a quote it "
            "could not close), so no part of it was judged",
        )

    # A command is as safe as its least safe segment.  This is the whole answer
    # to `git status; rm -rf /`: the second segment is judged too.
    worst = max((classify(part) for part in parts), default=Risk.UNKNOWN)
    culprit = next(part for part in parts if classify(part) == worst)
    reason = f"{culprit[0]!r} {_WHY[worst]}"

    allowed = _SHELL_ALLOWED_BY_MODE[mode]
    if confined:
        allowed = _CONFINED_ALLOWED_BY_MODE[mode]

    if worst in allowed:
        if policy == "unless-trusted" and worst is not Risk.READ:
            return Verdict(Decision.ASK, worst, reason)
        return Verdict(Decision.ALLOW, worst, reason)
    return _apply_policy(worst, policy, f"{reason}, which {mode} does not permit")


def judge_write(*, mode: SandboxMode, policy: ApprovalPolicy) -> Verdict:
    """`apply_patch` does exactly one thing, so there is nothing to classify."""
    if _WRITE_TOOL_ALLOWED_BY_MODE[mode]:
        if policy == "unless-trusted":
            return Verdict(Decision.ASK, Risk.WRITE, "editing files")
        return Verdict(Decision.ALLOW, Risk.WRITE, "editing files")
    return _apply_policy(Risk.WRITE, policy, f"editing files, which {mode} does not permit")


def _apply_policy(risk: Risk, policy: ApprovalPolicy, reason: str) -> Verdict:
    # `never` does not mean "allow"; it means there is nobody to ask.  Turning
    # an unanswerable question into a denial is the only honest option, and the
    # model has to be able to tell that denial apart from a failing command --
    # see `tool_errors.permission_error`.
    if policy == "never":
        return Verdict(Decision.DENY, risk, f"{reason}, and there is nobody to ask")
    return Verdict(Decision.ASK, risk, reason)


def policy_tables() -> dict[str, list[str]]:
    """Every table, in one shape, so a test can pin all of them at once.

    An allowlist that drifts costs containment: an entry added to `READ_ONLY`
    in a hurry looks exactly like an entry that belongs there.
    """
    return {
        "READ_ONLY": sorted(READ_ONLY),
        "WRITES": sorted(WRITES),
        "NETWORK": sorted(NETWORK),
        "INTERPRETERS": sorted(INTERPRETERS),
        "GIT_READ_ONLY_SUBCOMMANDS": sorted(GIT_READ_ONLY_SUBCOMMANDS),
        "GIT_NETWORK_SUBCOMMANDS": sorted(GIT_NETWORK_SUBCOMMANDS),
        "GIT_UNSAFE_GLOBAL_OPTIONS": sorted(GIT_UNSAFE_GLOBAL_OPTIONS),
        "GIT_BRANCH_READ_ONLY_FLAGS": sorted(GIT_BRANCH_READ_ONLY_FLAGS),
        "SEPARATORS": sorted(SEPARATORS),
        # Both mode tables: the difference between them is a security
        # property.  An entry quietly added to the confined table grants an
        # unprompted capability to every sandboxed session.
        "SHELL_ALLOWED_BY_MODE": [
            f"{mode}: {', '.join(sorted(r.name for r in risks))}"
            for mode, risks in sorted(_SHELL_ALLOWED_BY_MODE.items())
        ],
        "CONFINED_ALLOWED_BY_MODE": [
            f"{mode}: {', '.join(sorted(r.name for r in risks))}"
            for mode, risks in sorted(_CONFINED_ALLOWED_BY_MODE.items())
        ],
    }
