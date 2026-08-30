"""Chapter 5: approval and sandboxing.

One test per fault in FAULTS.md, named after it.  Three of these -- the
newline, the backtick and the `#` -- are not on the chapter's fault list at
all: they were found by feeding a checker that already handled `;` the things
next to `;`, and every one of them was a silent bypass verified against real
bash.

    grep -rn "F05_01" tests/
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from minicodex.approval import (
    AllowAll,
    ApprovalReply,
    ApprovalRequest,
    CliApprover,
    DenyAll,
    Session,
    gate_command,
    permissions_block,
    request_upgrade,
)
from minicodex.policy import Decision, Risk, judge_command, judge_write, policy_tables
from minicodex.rules import RuleRefused, RuleStore, check_rule
from minicodex.shell import ShellSession
from minicodex.shell_parse import segments
from minicodex.tool_errors import ERROR_PREFIX, PERMISSION_PREFIX, permission_error, tool_error
from minicodex.tools import ToolContext, default_tools, request_permissions, run_shell


def _decision(command: str, *, mode="read-only", policy="on-request") -> Decision:
    return judge_command(command, mode=mode, policy=policy).decision


# ---------------------------------------------------------------------------
# F05-01  string prefix matching is defeated by a separator
# ---------------------------------------------------------------------------


def test_F05_01_a_prefix_allowlist_approves_the_whole_line() -> None:
    """The naive version, kept so the fault is reproducible, not just described.

    `str.startswith` answers a question about the first N characters. Whether a
    command is safe is a question about all of them.
    """
    safe_prefixes = ("ls", "cat", "git status")
    assert "git status; rm -rf /".startswith(safe_prefixes)


def test_F05_01_every_segment_is_judged_not_only_the_first() -> None:
    assert segments("git status; rm -rf /") == [["git", "status"], ["rm", "-rf", "/"]]
    assert _decision("git status; rm -rf /") is Decision.ASK
    assert _decision("git status") is Decision.ALLOW


@pytest.mark.parametrize("separator", [";", "&&", "||", "|"])
def test_F05_01_all_four_separators_split(separator: str) -> None:
    assert _decision(f"ls {separator} rm -rf /") is Decision.ASK


def test_F05_01_a_separator_without_spaces_still_splits() -> None:
    """`shlex.split()` would return `['git', 'status;rm', ...]`.

    `punctuation_chars=True` is what makes the separator its own token, and it
    is the entire reason this module does not use the one-liner.
    """
    assert segments("git status;rm -rf /") == [["git", "status"], ["rm", "-rf", "/"]]


# ---------------------------------------------------------------------------
# Not on the list: three ways a tokeniser sees less than bash runs
# ---------------------------------------------------------------------------


def test_F05_01_a_newline_is_a_separator_bash_honours_and_shlex_eats() -> None:
    """`ls\\nrm -rf x`: shlex reports one command called `ls`; bash runs two.

    Verified against real bash on a real file, in probe_shell_safety.py: the
    file was gone afterwards. Without the character check this reaches
    `Decision.ALLOW`, because the segment's first word is on the read-only list.
    """
    import shlex

    lex = shlex.shlex("ls\nrm -rf x", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["ls", "rm", "-rf", "x"], "shlex swallows the newline"

    assert segments("ls\nrm -rf x") is None
    assert _decision("ls\nrm -rf x") is Decision.ASK


def test_F05_01_a_backtick_is_glued_into_a_word() -> None:
    import shlex

    lex = shlex.shlex("echo `rm -rf x`", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["echo", "`rm", "-rf", "x`"], "the substitution is invisible"

    assert segments("echo `rm -rf x`") is None
    assert _decision("echo `rm -rf x`") is Decision.ASK


def test_F05_01_a_hash_mid_word_hides_the_rest_of_the_line() -> None:
    """The worst of the three, because what disappears is unbounded.

    `shlex` treats `#` as starting a comment. bash only does so at the start of
    a word. So `echo a#b; rm -rf x` tokenises to `['echo', 'a']` -- one
    segment, first word on the allowlist, and the `rm` is not merely
    misjudged, it was never seen.
    """
    import shlex

    lex = shlex.shlex("echo a#b; rm -rf x", posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    assert list(lex) == ["echo", "a"], "everything after the # is gone"

    assert segments("echo a#b; rm -rf x") is None
    assert _decision("echo a#b; rm -rf x") is Decision.ASK


def test_F05_01_an_unmodelled_character_asks_rather_than_allows() -> None:
    """The rule that closed all three at once, stated directly.

    An allowlist of characters, not a blocklist of constructs: something
    nobody thought of defaults to a question. Chapter 2 made the same call
    about the subprocess environment, for the same reason.
    """
    for command in (
        "cat a > b",
        "cat < a",
        "echo $(id)",
        "echo $HOME",
        "rm *.py",
        "ls [ab]*",
        "FOO=bar env",
        "ls\ttab",  # a tab is modelled -- this one is allowed, as a control
    ):
        parsed = segments(command)
        if command == "ls\ttab":
            assert parsed == [["ls", "tab"]]
        else:
            assert parsed is None, command


def test_F05_01_an_unclosed_quote_becomes_a_question_not_an_exception() -> None:
    """`shlex` raises `ValueError: No closing quotation`.

    Chapter 0's rule: an exception where the model can see it ends the session.
    """
    assert segments('ls "unterminated') is None
    assert _decision('ls "unterminated') is Decision.ASK


def test_F05_01_punctuation_that_is_not_one_of_the_four_is_unknown() -> None:
    assert segments("ls;;rm") is None
    assert segments("sleep 30 & ls") is None
    assert segments("; ls") is None
    assert segments("ls ;") is None


# ---------------------------------------------------------------------------
# F05-02  git allowed wholesale, including push --force
# ---------------------------------------------------------------------------


def test_F05_02_git_is_judged_by_its_subcommand() -> None:
    assert _decision("git status") is Decision.ALLOW
    assert _decision("git log --oneline") is Decision.ALLOW
    assert _decision("git push --force") is Decision.ASK
    assert _decision("git reset --hard") is Decision.ASK


def test_F05_02_a_remote_subcommand_is_network_not_a_local_write() -> None:
    """`git push --force` damages a server. Calling it a file edit understates it,
    and `workspace-write` would then have let it through."""
    assert judge_command("git push --force", mode="workspace-write", policy="on-request").risk is (
        Risk.NETWORK
    )


def test_F05_02_global_options_before_the_subcommand_are_not_skipped() -> None:
    """`git -C /elsewhere status` reads a different repository.
    `git -c core.pager=... log` runs a command of its own choosing.

    A checker that scans for a known subcommand and stops has approved both.
    """
    assert _decision("git -C /elsewhere status") is Decision.ASK
    assert _decision("git --git-dir=/other/.git log") is Decision.ASK


def test_F05_02_git_branch_is_read_only_only_with_read_only_flags() -> None:
    assert _decision("git branch") is Decision.ALLOW
    assert _decision("git branch --show-current") is Decision.ALLOW
    assert _decision("git branch -d feature") is Decision.ASK
    assert _decision("git branch newbranch") is Decision.ASK


# ---------------------------------------------------------------------------
# F05-03  ../.. and symlinks defeat string comparison
# ---------------------------------------------------------------------------


def test_F05_03_a_relative_climb_is_refused(tmp_path: Path) -> None:
    """Inherited from chapter 4, which fixed it by resolving before judging.

    Re-tested here rather than reimplemented: the finding worth recording is
    that this was already closed, and the way to know that is a test, not a
    reading of the code.
    """
    from minicodex.paths import resolve

    path, error = resolve("../../../../etc/passwd", tmp_path)
    assert path is None
    assert error is not None and "outside the repository" in error


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="platform has no symlinks")
def test_F05_03_a_symlink_pointing_out_is_refused(tmp_path: Path) -> None:
    """The case a string comparison cannot see at all.

    `root/link` starts with `root`, character for character, and points
    somewhere else entirely. `Path.resolve()` follows it, which is why chapter
    4's "resolve first, judge second" happens to cover this too.
    """
    from minicodex.paths import resolve

    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret\n", encoding="utf-8")
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation needs privileges on this platform")

    assert str(root / "link").startswith(str(root)), "a string comparison says yes"

    path, error = resolve("link", root)
    assert path is None, "resolve() follows the link and says no"
    assert error is not None and "outside the repository" in error


# ---------------------------------------------------------------------------
# F05-04  an interpreter defeats every amount of shell parsing
# ---------------------------------------------------------------------------


def test_F05_04_an_interpreter_tokenises_perfectly_cleanly() -> None:
    """There is nothing wrong with the parse. That is the point."""
    assert segments("python script.py") == [["python", "script.py"]]
    assert _decision("python script.py") is Decision.ASK
    assert _decision("python script.py", mode="workspace-write") is Decision.ASK


@pytest.mark.parametrize("interpreter", ["python", "python3", "node", "perl", "ruby", "sh", "bash"])
def test_F05_04_no_interpreter_is_ever_auto_allowed(interpreter: str) -> None:
    """Asserts the risk, not only the decision.

    Mutation testing caught this: deleting the `INTERPRETERS` branch from
    `classify` left every test green, because an unclassified command falls
    through to `UNKNOWN`, which also asks. Same decision, different sentence --
    and the sentence is what the human at the prompt reads before saying yes.
    "'python' runs an interpreter, which can do anything" and "'python' is not
    on any list" are not the same warning.
    """
    for mode in ("read-only", "workspace-write"):
        verdict = judge_command(f"{interpreter} thing.txt", mode=mode, policy="on-request")
        assert verdict.decision is Decision.ASK
        assert verdict.risk is Risk.INTERPRETER
        assert "interpreter" in verdict.reason


def test_F05_04_an_interpreter_hidden_behind_a_pipe_is_still_found() -> None:
    assert _decision("cat setup.py | python") is Decision.ASK


# ---------------------------------------------------------------------------
# F05-05  curl exfiltrates; pip install pulls a malicious package
# ---------------------------------------------------------------------------


def test_F05_05_network_commands_are_their_own_category() -> None:
    for command in ("curl https://x", "wget https://x", "pip install requests", "npm install"):
        verdict = judge_command(command, mode="workspace-write", policy="on-request")
        assert verdict.decision is Decision.ASK, command
        assert verdict.risk is Risk.NETWORK, command


def test_F05_03_workspace_write_does_not_let_a_shell_command_write(tmp_path: Path) -> None:
    """The most consequential decision in this chapter, and mutation testing
    found nothing was testing it.

    `workspace-write` means "write inside the workspace". For `apply_patch`
    that is enforceable -- every path goes through `paths.resolve()`. For a
    shell command it is not: `rm -rf build` and `rm -rf ~` are the same shape,
    and the expansion happens after we have stopped looking. Enforcing it
    needs an OS sandbox, which this chapter does not build.

    So a writing shell command asks in `workspace-write` too, and the two
    tools deliberately differ. Flipping `_SHELL_ALLOWED_BY_MODE` to include
    WRITE -- which is the natural-looking "fix" for the asymmetry -- left all
    202 tests green before this existed.
    """
    assert _decision("rm -rf /", mode="workspace-write") is Decision.ASK
    assert _decision("rm -rf ~", mode="workspace-write") is Decision.ASK
    assert _decision("mv src dst", mode="workspace-write") is Decision.ASK

    # ...while the tool whose paths we control does not ask.
    assert judge_write(mode="workspace-write", policy="on-request").decision is Decision.ALLOW


def test_F05_03_full_access_is_the_only_mode_that_lets_a_shell_write() -> None:
    """And it is called `full-access` so that nobody chooses it by accident."""
    assert _decision("rm -rf /", mode="full-access") is Decision.ALLOW
    assert _decision("rm -rf /", mode="read-only") is Decision.ASK


def test_F05_05_network_is_not_reachable_by_widening_the_file_sandbox() -> None:
    """`workspace-write` is about files. Nothing about it should imply network,
    and the fault list has a real incident behind this one (marked user report)."""
    assert _decision("curl https://x", mode="workspace-write") is Decision.ASK


# ---------------------------------------------------------------------------
# F05-06  approval fatigue
# ---------------------------------------------------------------------------


async def test_F05_06_without_memory_the_same_command_asks_every_time() -> None:
    class Counting:
        def __init__(self) -> None:
            self.asked = 0

        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            self.asked += 1
            return ApprovalReply(True, request.what)

    approver = Counting()
    session = Session(mode="read-only", approver=approver)
    for _ in range(5):
        await gate_command("pytest -q", session)
    assert approver.asked == 5


async def test_F05_06_a_remembered_rule_stops_the_asking() -> None:
    class Counting:
        def __init__(self) -> None:
            self.asked = 0

        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            self.asked += 1
            return ApprovalReply(True, request.what, remember="session")

    approver = Counting()
    session = Session(mode="read-only", approver=approver)
    for _ in range(5):
        result = await gate_command("pytest -q", session)
        assert result.allowed
    assert approver.asked == 1, "asked once, then remembered"
    assert len(session.rules) == 1


async def test_F05_06_the_rule_covers_the_category_not_the_exact_string() -> None:
    """A rule that only matches the byte-identical command rebuilds the fatigue
    it was added to remove: `pytest -q` and `pytest -x` would each need a yes."""
    session = Session(mode="read-only", approver=AllowAll())
    session.rules.remember(("pytest",), scope="session", prompted_by="pytest -q")
    assert (await gate_command("pytest -x tests/", session)).allowed


async def test_F05_06_a_rule_covering_one_segment_does_not_cover_the_others() -> None:
    """`allows_every`, not `allows_any`."""
    session = Session(mode="read-only", approver=DenyAll())
    session.rules.remember(("pytest",), scope="session", prompted_by="pytest -q")
    result = await gate_command("pytest -q | rm -rf /", session)
    assert not result.allowed


# ---------------------------------------------------------------------------
# F05-07  a remembered rule is too broad and unattributable
# ---------------------------------------------------------------------------


def test_F05_07_a_rule_records_where_it_came_from() -> None:
    store = RuleStore()
    rule = store.remember(("cargo", "test"), scope="session", prompted_by="cargo test --all")
    assert rule.prompted_by == "cargo test --all"
    assert rule.created_at
    assert "cargo test" in rule.describe()
    assert "cargo test --all" in rule.describe()


def test_F05_07_a_rule_can_be_revoked() -> None:
    store = RuleStore()
    store.remember(("cargo", "test"), scope="session", prompted_by="cargo test")
    assert len(store) == 1
    store.forget(0)
    assert len(store) == 0


def test_F05_07_an_interpreter_rule_is_refused() -> None:
    """codex bans the same prefixes from the other side, in its prompt:
    not `["python3"]`, not `["python", "-"]`."""
    with pytest.raises(RuleRefused, match="every program"):
        check_rule(("python3", "-c"))


def test_F05_07_a_destructive_rule_is_refused() -> None:
    with pytest.raises(RuleRefused, match="arguments are where the damage lives"):
        check_rule(("rm", "-rf"))


def test_F05_07_a_bare_tool_name_rule_is_refused() -> None:
    """`["git"]` approves `git push --force`. `["cargo"]` approves `cargo publish`."""
    with pytest.raises(RuleRefused, match="every subcommand"):
        check_rule(("git",))
    check_rule(("git", "status"))  # naming the subcommand is fine
    check_rule(("ls",))  # a read-only command has no dangerous subcommands


def test_F05_07_project_rules_survive_and_are_re_checked_on_load(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    RuleStore(path).remember(("cargo", "test"), scope="project", prompted_by="cargo test")
    assert len(RuleStore(path)) == 1

    # The file is editable by anyone who owns the machine. "It was already in
    # the file" is not a reason to honour a rule that would be refused today.
    path.write_text(
        json.dumps([{"words": ["python3"], "prompted_by": "hand-edited"}]), encoding="utf-8"
    )
    assert len(RuleStore(path)) == 0


def test_F05_07_a_corrupt_rules_file_grants_nothing(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{not json", encoding="utf-8")
    assert len(RuleStore(path)) == 0


async def test_F05_07_session_rules_do_not_reach_disk(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    store = RuleStore(path)
    store.remember(("pytest",), scope="session", prompted_by="pytest -q")
    assert not path.exists()


# ---------------------------------------------------------------------------
# F05-08  the user edits the command and the model is never told
# ---------------------------------------------------------------------------


async def test_F05_08_an_edited_command_is_the_one_that_runs() -> None:
    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "pytest tests/test_patch.py")

    result = await gate_command("pytest", Session(approver=Editing()))
    assert result.allowed
    assert result.command == "pytest tests/test_patch.py"


async def test_F05_08_the_model_is_told_the_command_changed() -> None:
    """Without this the model reads the output as the result of what it asked
    for, and reports on a command that never ran."""

    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "pytest tests/test_patch.py")

    result = await gate_command("pytest", Session(approver=Editing()))
    assert result.note is not None
    assert "pytest tests/test_patch.py" in result.note
    assert "'pytest'" in result.note


async def test_F05_08_the_note_reaches_the_tool_output(tmp_path: Path) -> None:
    class Editing:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "echo edited")

    ctx = ToolContext(
        root=tmp_path,
        shell=ShellSession(),
        session=Session(approver=Editing()),
    )
    # `rm`, not `echo`: a command that is already allowed never reaches the
    # approver, so it can never be edited. The first version of this test used
    # `echo` and passed a note-free output straight through.
    out = await run_shell(ctx, {"command": "rm -rf everything"})
    assert "the user changed your command" in out
    assert "edited" in out


async def test_F05_08_a_remembered_rule_is_built_from_what_was_agreed() -> None:
    """Editing the command and choosing "always" otherwise remembers the
    version that was just rejected."""

    class EditingAndRemembering:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            return ApprovalReply(True, "cargo test", remember="session")

    session = Session(approver=EditingAndRemembering())
    await gate_command("cargo publish", session)
    assert [rule.words for rule in session.rules.all()] == [("cargo", "test")]


def test_F05_08_an_empty_edit_is_not_an_approval() -> None:
    """A slip of the return key must not become a yes."""
    import io

    approver = CliApprover(stream_in=io.StringIO("e\n\n"), stream_out=io.StringIO())
    reply = approver._ask_blocking(
        ApprovalRequest(what="rm -rf /", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert not reply.approved


def test_F05_08_a_closed_stdin_is_a_no() -> None:
    """`readline()` on an exhausted stream returns `''`. Falling through to
    "approved" there would make a piped, non-interactive run approve
    everything -- silently, and only in production."""
    import io

    approver = CliApprover(stream_in=io.StringIO(""), stream_out=io.StringIO())
    reply = approver._ask_blocking(
        ApprovalRequest(what="rm -rf /", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert not reply.approved


def test_F05_08_the_prompt_offers_a_rule_only_when_one_could_be_made() -> None:
    """Offering "always" and then refusing it teaches people to ignore refusals."""
    import io

    out = io.StringIO()
    CliApprover(stream_in=io.StringIO("n\n"), stream_out=out)._ask_blocking(
        ApprovalRequest(what="rm -rf x", reason="", risk=Risk.WRITE, suggested_rule=None)
    )
    assert "always" not in out.getvalue()

    out = io.StringIO()
    CliApprover(stream_in=io.StringIO("n\n"), stream_out=out)._ask_blocking(
        ApprovalRequest(
            what="cargo test", reason="", risk=Risk.UNKNOWN, suggested_rule=("cargo", "test")
        )
    )
    assert "always" in out.getvalue()


# ---------------------------------------------------------------------------
# F05-09  a permission denial read as a business error, retried forever
# ---------------------------------------------------------------------------


def test_F05_09_a_denial_does_not_look_like_an_ordinary_error() -> None:
    ordinary = tool_error("the file was not found", do_this="Try another path.")
    denied = permission_error("that is not allowed", do_this="Ask for permission.")
    assert ordinary.startswith(ERROR_PREFIX)
    assert denied.startswith(PERMISSION_PREFIX)
    assert not denied.startswith(ERROR_PREFIX)


async def test_F05_09_a_denial_says_retrying_will_not_help() -> None:
    """The three-part shape from chapter 3, with the third part carrying the
    one instruction that changes the outcome."""
    result = await gate_command("curl https://x", Session(approver=DenyAll()))
    assert result.denial is not None
    assert "refused again" in result.denial
    assert "request_permissions tool" in result.denial


async def test_F05_09_under_never_the_denial_does_not_name_the_tool() -> None:
    """Found by the probe. With `request_permissions` named in the denial and
    absent from the tool list, gpt-4o-mini sent

        run_shell({"command": "request_permissions"})

    -- it tried to run the tool as a shell command. Naming a capability is an
    instruction to use it, so it is named only where using it can work. The
    same mistake appeared independently in `permissions_block`.
    """
    result = await gate_command("curl https://x", Session(policy="never"))
    assert result.denial is not None
    assert "request_permissions" not in result.denial
    assert "nothing in this session can change it" in result.denial


async def test_F05_09_the_denial_names_what_was_refused_and_why() -> None:
    result = await gate_command("curl https://x", Session(policy="never"))
    assert result.denial is not None
    assert "curl" in result.denial
    assert "network" in result.denial
    assert "read-only" in result.denial


# ---------------------------------------------------------------------------
# F05-10  the model does not know its own permissions
# ---------------------------------------------------------------------------


def test_F05_10_the_prompt_states_the_current_mode_and_policy() -> None:
    block = permissions_block(Session(mode="read-only", policy="on-request"))
    assert "read-only" in block
    assert "on-request" in block


def test_F05_10_the_prompt_is_generated_from_the_state_not_copied() -> None:
    """A permission state typed into a prompt goes stale the first time
    `request_permissions` succeeds -- and then the model is told it cannot do
    the thing it just asked for and got."""
    session = Session(mode="read-only")
    assert "read-only" in permissions_block(session)
    session.mode = "workspace-write"
    block = permissions_block(session)
    assert "workspace-write" in block
    assert "read-only" not in block


def test_F05_10_the_prompt_explains_the_denial_prefix() -> None:
    """The prompt and `permission_error` have to agree on the marker, or the
    instruction points at something the model never sees."""
    assert PERMISSION_PREFIX in permissions_block(Session())


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
@pytest.mark.parametrize("policy", ["never", "on-request", "unless-trusted"])
def test_F05_10_every_combination_renders(mode, policy) -> None:
    """Nine combinations, one missing dict entry away from a KeyError in the
    middle of a run."""
    assert permissions_block(Session(mode=mode, policy=policy)).strip()


def test_F05_10_the_prompt_does_not_name_a_tool_that_is_not_there() -> None:
    """Found by the probe, not by reading. With `request_permissions` removed
    from the tool list but still named in this block, gemma4 called it 2 samples
    out of 3 -- straight into chapter 0's "no tool named X" error, which was
    written for a model that made the name up. It did not make it up."""
    assert "request_permissions" not in permissions_block(Session(), can_request=False)
    assert "request_permissions" in permissions_block(Session(), can_request=True)


def test_F05_10_under_never_the_prompt_does_not_offer_an_escalation() -> None:
    """`never` means nobody is there. Telling the model to ask anyway spends a
    turn on a question that cannot be answered."""
    block = permissions_block(Session(policy="never"))
    assert "request_permissions" not in block
    assert "do not ask" in block


# ---------------------------------------------------------------------------
# F05-11  no way to ask for more, task deadlocks
# ---------------------------------------------------------------------------


async def test_F05_11_the_agent_can_ask_for_more_and_get_it() -> None:
    session = Session(mode="read-only", approver=AllowAll())
    assert judge_write(mode=session.mode, policy=session.policy).decision is Decision.ASK

    out = await request_upgrade(session, needs="write-files", why="the task is to edit a file")
    assert "Granted" in out
    assert session.mode == "workspace-write"
    assert judge_write(mode=session.mode, policy=session.policy).decision is Decision.ALLOW


async def test_F05_11_a_refused_request_says_not_to_ask_again() -> None:
    session = Session(mode="read-only", approver=DenyAll())
    out = await request_upgrade(session, needs="write-files", why="I want to")
    assert out.startswith(PERMISSION_PREFIX)
    assert "Do not ask again" in out
    assert session.mode == "read-only"


async def test_F05_11_a_request_must_say_what_it_is_for() -> None:
    session = Session(mode="read-only", approver=AllowAll())
    out = await request_upgrade(session, needs="write-files", why="   ")
    assert out.startswith(PERMISSION_PREFIX)
    assert session.mode == "read-only"


async def test_F05_11_under_never_there_is_nobody_to_ask() -> None:
    """And saying so is better than a prompt that cannot appear."""
    session = Session(mode="read-only", policy="never", approver=AllowAll())
    out = await request_upgrade(session, needs="unrestricted", why="anything")
    assert "nobody to ask" in out
    assert session.mode == "read-only"


async def test_F05_11_asking_for_something_already_held_changes_nothing() -> None:
    session = Session(mode="full-access", approver=DenyAll())
    out = await request_upgrade(session, needs="write-files", why="whatever")
    assert "Already granted" in out
    assert session.mode == "full-access"


async def test_F05_11_the_tool_wrapper_needs_both_arguments(tmp_path: Path) -> None:
    ctx = ToolContext(root=tmp_path, shell=ShellSession(), session=Session(approver=AllowAll()))
    out = await request_permissions(ctx, {"needs": "write-files"})
    assert out.startswith(ERROR_PREFIX)


# ---------------------------------------------------------------------------
# The gate is the only door
# ---------------------------------------------------------------------------


def test_F05_00_the_shell_has_no_ungated_entry_point() -> None:
    """`shell.run_shell` used to take a session and an argument dict and call
    `ShellSession.run()` with no approval anywhere. It was deleted rather than
    left as a shortcut for a future caller to find."""
    import minicodex.shell as shell

    assert not hasattr(shell, "run_shell")


async def test_F05_00_every_tool_that_acts_goes_through_the_gate(tmp_path: Path) -> None:
    """Both acting tools, denied, with nothing to show for it.

    A per-tool test would pass just as well with one of them wired up and the
    other forgotten. This asserts the property over the table, so a tool added
    in chapter 8 that forgets the gate turns this red.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = default_tools(tmp_path, Session(mode="read-only", approver=DenyAll()))

    out = await tools["run_shell"]({"command": "rm -rf /"})
    assert out.startswith(PERMISSION_PREFIX)

    out = await tools["apply_patch"](
        {"edits": [{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}]}
    )
    assert out.startswith(PERMISSION_PREFIX)
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"


async def test_F05_00_reading_never_needs_approval(tmp_path: Path) -> None:
    """The other half: a sandbox that blocks reading blocks the agent from
    working at all, and the user turns it off."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    tools = default_tools(tmp_path, Session(mode="read-only", approver=DenyAll()))
    assert await tools["read_file"]({"path": "a.py"}) == "x = 1\n"


# ---------------------------------------------------------------------------
# The tables themselves
# ---------------------------------------------------------------------------

# Chapter 3 pinned the tool descriptions; a description that drifts costs
# accuracy. These cost containment: an entry added to READ_ONLY in a hurry
# looks exactly like an entry that belongs there, and nothing else in the suite
# would notice.
EXPECTED_TABLES = {
    "GIT_BRANCH_READ_ONLY_FLAGS": [
        "-a",
        "-l",
        "-r",
        "-v",
        "-vv",
        "--all",
        "--list",
        "--remotes",
        "--show-current",
        "--verbose",
    ],
    "GIT_NETWORK_SUBCOMMANDS": ["clone", "fetch", "pull", "push", "remote", "submodule"],
    "GIT_READ_ONLY_SUBCOMMANDS": [
        "blame",
        "branch",
        "diff",
        "log",
        "ls-files",
        "rev-parse",
        "show",
        "status",
    ],
    "GIT_UNSAFE_GLOBAL_OPTIONS": [
        "-C",
        "-c",
        "-p",
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--paginate",
        "--super-prefix",
        "--work-tree",
    ],
    "INTERPRETERS": [
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
    ],
    "NETWORK": [
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
    ],
    "READ_ONLY": [
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
    ],
    "SEPARATORS": [";", "&&", "|", "||"],
    "WRITES": [
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
    ],
}


def test_F05_07_the_policy_tables_are_pinned() -> None:
    actual = policy_tables()
    for name, expected in EXPECTED_TABLES.items():
        assert sorted(actual[name]) == sorted(expected), name
    assert set(actual) == set(EXPECTED_TABLES)


def test_F05_04_no_command_is_in_two_tables() -> None:
    """Overlap is not caught by anything else, and `classify` checks in a fixed
    order -- so an entry in two tables silently takes whichever comes first."""
    tables = policy_tables()
    names = ["READ_ONLY", "WRITES", "NETWORK", "INTERPRETERS"]
    for i, first in enumerate(names):
        for second in names[i + 1 :]:
            overlap = set(tables[first]) & set(tables[second])
            assert not overlap, f"{first} and {second} both contain {sorted(overlap)}"
