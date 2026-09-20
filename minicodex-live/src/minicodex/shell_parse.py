"""Splitting one command string into the pieces a policy can judge.

The model sends `run_shell` a single string.  Deciding whether that string is
safe by looking at its prefix does not work, for three
separate ways the obvious improvement -- tokenise it, then judge each command
-- does not work either.

`shlex` is a tokeniser, not a shell parser.  Where the two disagree, they
disagree *silently*, and every disagreement found was in the dangerous
direction: `shlex` saw less than bash ran.

    'ls\\nrm -f payload.txt'      shlex: ['ls', 'rm', '-f', 'payload.txt']
                                  bash:  two commands; the file was deleted

    'echo `rm -f payload.txt`'    shlex: ['echo', '`rm', '-f', 'payload.txt`']
                                  bash:  substitution ran; the file was deleted

    'echo a#b; rm -f payload.txt' shlex: ['echo', 'a']
                                  bash:  `#` mid-word is not a comment; the
                                         file was deleted

The third is the worst: `shlex` treats `#` as starting a comment and drops the
entire rest of the line, so a checker looking at the first word sees `echo` and
approves a command whose tail it never saw.  All three were verified by running
bash on a real file and checking whether it survived.

Patching them one at a time is a losing game -- each fix is a guess that the
list of disagreements is now complete.  So the question is inverted.  Instead
of listing constructs we refuse, `_MODELLED` lists the characters whose meaning
this module actually implements; anything else makes the command *unknown*.

`None` does not mean "dangerous".  It means "this module cannot say", which the
policy layer turns into "ask the user" -- never into "allow".  A construct
nobody thought of therefore defaults to a question, not to an approval.  Same
rule as the shell's environment allowlist, and for the same reason: a blocklist
is only as good as the imagination of whoever wrote it.

codex draws the line in the same place.  Its own prompt template
(`prompts/templates/permissions/approval_policy/on_request.md`) tells the model:

    Commands that use more advanced shell features like redirection (>, >>, <),
    substitutions ($(...), ...), environment variables (FOO=bar), or wildcard
    patterns (*, ?) will not be evaluated against rules, to limit the scope of
    what an approved rule allows.
"""

from __future__ import annotations

import shlex
import string

# The four operators that sequence commands without changing what any of them
# means.  codex splits on exactly these (plus subshell boundaries, which land
# in the unknown bucket here).
SEPARATORS = frozenset({";", "&&", "||", "|"})

# `punctuation_chars=True` makes `shlex` emit runs of these as their own tokens.
# Anything it emits that is made only of them and is not in `SEPARATORS` is an
# operator we do not model -- a bare `&` backgrounds, `;;` is a case terminator.
_PUNCTUATION = frozenset(";|&<>()")

# Every character this module claims to understand.  Deliberately short:
# growing it is a decision someone has to make on purpose, and every addition
# needs an answer to "what does bash do with this, and does the tokeniser
# agree?"
#
#   -_./          paths and flags
#   =             `--include=x`; a leading `FOO=bar` is caught separately below
#   :,+@%~        version specifiers, ranges, emails, git format strings
#   '"            quoting; an unbalanced one raises and lands in `None`
#   ;|&           the separators, assembled by the tokeniser
#
# Not here, on purpose: `` ` `` $ ( ) < > * ? [ ] { } ! \ # and newline.
_MODELLED = frozenset(string.ascii_letters + string.digits + " \t" + "-_./=:,+@%~'\";|&")


def segments(command: str) -> list[list[str]] | None:
    """Split `command` into independently judgeable word lists.

    Returns `None` when the command contains anything this module does not
    model.  Callers must treat `None` as "cannot be auto-approved", not as
    "reject": a `curl ... | sh` and a `grep 'foo.*bar' .` both land here, and
    only a human can tell them apart.
    """
    if not command.strip():
        return None

    if any(character not in _MODELLED for character in command):
        return None

    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError:
        # "No closing quotation" -- the string is not a command yet.  This was
        # learned the expensive way: an exception raised where the model
        # can see it ends the session, so this becomes a `None` and the policy
        # layer turns it into a question.
        return None

    out: list[list[str]] = [[]]
    for token in tokens:
        if token in SEPARATORS:
            out.append([])
        elif set(token) <= _PUNCTUATION:
            # `;;`, `&`, `|&` and friends: punctuation the tokeniser grouped
            # into something that is not one of our four operators.  The first
            # version of this line also tested `token in _MODELLED`, which is a
            # set of single characters -- so `";;"` was never in it and the
            # branch never ran.  `ls;;rm` came back as one segment whose middle
            # word was `;;`.
            return None
        else:
            out[-1].append(token)

    if any(not segment for segment in out):
        # A leading, trailing or doubled separator.  Rare, and the shapes it
        # takes (`;; `, `| |`) are ones bash reads differently from this loop.
        return None

    for segment in out:
        # `FOO=bar cmd` runs `cmd` with a modified environment, which can change
        # what `cmd` does without changing any word this module inspects --
        # `GIT_SSH_COMMAND=... git fetch` being the sharp example.
        if "=" in segment[0]:
            return None

    return out
