"""What chapter 10 measured, and how.

Run one section at a time:

    uv run python probe_subagent.py naive       # the fifteen-line version, real API
    uv run python probe_subagent.py leak        # F10-03, no network
    uv run python probe_subagent.py cwd         # F10-01, no network
    uv run python probe_subagent.py race        # F10-02, no network, 30 trials
    uv run python probe_subagent.py depth       # F10-06, no network
    uv run python probe_subagent.py hang        # F10-07, no network
    uv run python probe_subagent.py rollout     # F10-12, no network
    uv run python probe_subagent.py serial      # F10-08, no network
    uv run python probe_subagent.py worktree    # F10-11, real git, no network
    uv run python probe_subagent.py contract    # F10-04, real API
    uv run python probe_subagent.py length      # F10-05, real API
    uv run python probe_subagent.py failure     # F10-10, real API

Anything with "real API" costs money and needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from minicodex import system_prompt
from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, Session, permissions_block, request_upgrade
from minicodex.compaction import Sizer
from minicodex.history import History, ToolResult
from minicodex.model import (
    OPENAI_BASE_URL,
    ChatCompletionsModel,
    Completed,
    TextDelta,
    ToolCallDelta,
)
from minicodex.rollout import RolloutError, RolloutWriter, SessionMeta, new_session_id, rollout_path
from minicodex.scheduler import STATEFUL, Footprint
from minicodex.tools import TOOL_SCHEMAS, default_tools, footprint_of

MODEL = "gpt-4o-mini"
ROOT = Path(__file__).resolve().parent
TRIALS = 30


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


async def _retry(make: Any, attempts: int = 4) -> Any:
    """Run one sample, surviving a dropped connection.

    Not a fix for anything -- chapter 12 is where retries are designed.  This
    is here because a probe that dies on request 40 of 60 wastes the first 39,
    and `httpx.ConnectError` / `RemoteProtocolError` both happened while these
    numbers were being collected.
    """
    for attempt in range(attempts):
        try:
            return await make()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if attempt == attempts - 1:
                raise
            print(f"    (retrying after {type(exc).__name__})", file=sys.stderr)
            await asyncio.sleep(2 * (attempt + 1))


def _model(tools: list[dict] | None = None) -> ChatCompletionsModel:
    return ChatCompletionsModel(
        base_url=OPENAI_BASE_URL,
        model=MODEL,
        api_key=_key(),
        tools=tools if tools is not None else list(TOOL_SCHEMAS),
    )


# -- the fifteen-line version ------------------------------------------------

SPAWN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": "Hand a self-contained task to a second agent and get its answer back.",
        "parameters": {
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string", "description": "What it should do."}},
        },
    },
}


async def naive() -> None:
    """The whole idea, with nothing defended."""
    session = Session(mode="read-only", approver=AllowAll())

    async def spawn_agent(args: dict) -> str:
        child = Agent(_model(), default_tools(root=ROOT, session=session), max_turns=6)
        result = await child.run(args["task"])
        return result.final_text

    tools = {**default_tools(root=ROOT, session=session), "spawn_agent": spawn_agent}
    parent = Agent(_model([*TOOL_SCHEMAS, SPAWN_SCHEMA]), tools, max_turns=6)
    result = await parent.run(
        "Use spawn_agent twice, once per file, to find out what "
        "src/minicodex/scheduler.py and src/minicodex/paths.py each do. "
        "Then answer in two sentences, one per file."
    )
    print(result.final_text)
    print(f"\n[{result.stop_reason} after {result.turns_used} turn(s)]")
    for item in result.history.items:
        print(f"  {type(item).__name__:18} {len(str(getattr(item, 'text', '') or ''))} chars")


# -- F10-03: what a full history costs, and what is in it --------------------


def _parent_history() -> History:
    """A parent five turns in, shaped like a real one.

    The system note is what `__main__` builds: the system prompt plus the
    permission block.  The tool results are real file contents, because the
    thing being measured is how big a history gets once an agent has read
    anything at all.
    """
    session = Session(mode="workspace-write", approver=AllowAll())
    history = History()
    history.add_system_note(
        f"{system_prompt().rstrip()}\n\n{permissions_block(session, can_request=True)}"
    )
    history.add_user(
        "The retry logic in model.py must not retry a 400. Fix it, and do not "
        "touch the streaming parser while you are in there."
    )
    for index, name in enumerate(["scheduler.py", "paths.py", "shell_parse.py"], 1):
        call = ToolCall(f"call_{index}", "read_file", {"path": f"src/minicodex/{name}"}, "{}")
        history.add_assistant(f"Reading {name}.", (call,))
        history.add_tool_result(call.call_id, (ROOT / "src" / "minicodex" / name).read_text())
    return history


def leak() -> None:
    sizer = Sizer(tools=tuple(TOOL_SCHEMAS))
    history = History()
    parent = _parent_history()
    del history

    task = "Read src/minicodex/model.py and list every place it raises."
    everything = [*parent.to_wire("chat_completions"), {"role": "user", "content": task}]
    contract = [{"role": "user", "content": task}]

    print(f"parent history           {len(parent.items):>3} items")
    print(f"whole history to child   {sizer.messages(everything):>6} tokens")
    print(f"task only                {sizer.messages(contract):>6} tokens")
    print(
        f"ratio                    {sizer.messages(everything) / sizer.messages(contract):>6.0f}x"
    )
    print("\nwhat travels that the child's task does not mention:")
    for message in parent.to_wire("chat_completions"):
        content = str(message.get("content") or "")
        head = content.replace("\n", " ")[:68]
        print(f"  {message['role']:<10} {len(content):>6} chars  {head}")


# -- F10-01: state the parent never wrote down -------------------------------


async def cwd() -> None:
    session = Session(mode="workspace-write", approver=AllowAll())
    parent_tools = default_tools(root=ROOT, session=session)

    print("parent:", (await parent_tools["run_shell"]({"command": "cd src"})).strip() or "(ok)")
    print("parent:", (await parent_tools["run_shell"]({"command": "pwd"})).strip())

    child_tools = default_tools(root=ROOT, session=session)
    print("child: ", (await child_tools["run_shell"]({"command": "pwd"})).strip())

    parent_session = Session(mode="read-only", approver=AllowAll())
    print("\nparent session before upgrade:", parent_session.describe())
    await request_upgrade(parent_session, needs="write-files", why="the task edits files")
    print("parent session after upgrade: ", parent_session.describe())
    print("a child built with default_tools(root):", Session().describe())


# -- a model that does exactly what it is told -------------------------------


class ScriptedModel:
    """Replays a fixed list of turns. Same shape as chapters 7 and 8 use."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.calls = 0

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.calls += 1
        turn = self.turns[min(self.calls - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")


def _spawn_schema() -> list[dict[str, Any]]:
    return [*TOOL_SCHEMAS, SPAWN_SCHEMA]


# -- F10-02: two children, one file ------------------------------------------


async def race() -> None:
    """Does chapter 8's scheduler already cover a tool it has never heard of?"""
    for label, spawn_footprint in [
        ("spawn is STATEFUL (what tools.footprint_of already returns)", STATEFUL),
        ("spawn declares Footprint(reads={root}) -- 'it only reads'", None),
    ]:
        lost = 0
        for trial in range(TRIALS):
            with tempfile.TemporaryDirectory() as tmp:
                # Set up through a thread: ASYNC240 rejects blocking filesystem
                # calls inside `async def`, and this probe is the one place a
                # scratch directory is built there.  The rule is right even
                # here -- a `resolve()` on a network path blocks the loop for
                # everything, including the two children this section is timing.
                root = await asyncio.to_thread(lambda: Path(tmp).resolve())
                await asyncio.to_thread(
                    (root / "target.py").write_text, "one\ntwo\n", encoding="utf-8"
                )
                session = Session(mode="workspace-write", approver=AllowAll())

                async def child(
                    args: dict[str, Any], root: Path = root, session: Session = session
                ):
                    word = args["task"]
                    model = ScriptedModel(
                        [
                            [
                                (
                                    "c1",
                                    "apply_patch",
                                    {
                                        "edits": [
                                            {
                                                "path": "target.py",
                                                "old_text": word,
                                                "new_text": word.upper(),
                                            }
                                        ]
                                    },
                                )
                            ],
                            "done",
                        ]
                    )
                    agent = Agent(model, default_tools(root=root, session=session))
                    return (await agent.run(word)).final_text

                def footprint(
                    call: ToolCall,
                    root: Path = root,
                    declared: Footprint | None = spawn_footprint,
                ) -> Footprint:
                    if call.name == "spawn_agent":
                        if declared is not None:
                            return declared
                        return Footprint(reads=frozenset({str(root)}))
                    return footprint_of(call, root=root)

                parent_model = ScriptedModel(
                    [
                        [
                            ("p1", "spawn_agent", {"task": "one"}),
                            ("p2", "spawn_agent", {"task": "two"}),
                        ],
                        "both done",
                    ]
                )
                tools = {**default_tools(root=root, session=session), "spawn_agent": child}
                parent = Agent(parent_model, tools, footprint_of=footprint)
                await parent.run("edit both")

                text = (root / "target.py").read_text(encoding="utf-8")
                if text != "ONE\nTWO\n":
                    lost += 1
                    if trial == 0:
                        print(f"    first failure: {text!r}")
        print(f"{lost:>3}/{TRIALS} lost updates  --  {label}")


# -- F10-06: a child that spawns a child that spawns a child -----------------


async def depth() -> None:
    reached = 0

    begin = time.monotonic()

    async def spawn(args: dict[str, Any]) -> str:
        nonlocal reached
        reached += 1
        if reached % 200 == 0:
            print(f"  depth {reached} after {time.monotonic() - begin:.1f}s")
        model = ScriptedModel([[("c", "spawn_agent", {"task": "again"})], "done"])
        agent = Agent(model, {"spawn_agent": spawn})
        return (await agent.run(args["task"])).final_text

    model = ScriptedModel([[("p", "spawn_agent", {"task": "go"})], "done"])
    parent = Agent(model, {"spawn_agent": spawn})
    try:
        await asyncio.wait_for(parent.run("go"), timeout=10)
    except TimeoutError:
        print(f"wait_for(10s) fired; reached depth {reached}")
        return
    except RecursionError:
        print(f"RecursionError reached my except clause at depth {reached}")
        return
    print(f"finished on its own at depth {reached}")


# -- F10-07: a child that never comes back -----------------------------------


async def hang() -> None:
    started = asyncio.Event()

    async def sleeper(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(3600)
        return "never"

    async def spawn(args: dict[str, Any]) -> str:
        model = ScriptedModel([[("c", "sleep", {})], "done"])
        agent = Agent(model, {"sleep": sleeper})
        return (await agent.run(args["task"])).final_text

    model = ScriptedModel([[("p", "spawn_agent", {"task": "go"})], "done"])
    parent = Agent(model, {"spawn_agent": spawn})
    begin = time.monotonic()
    try:
        result = await asyncio.wait_for(parent.run("go"), timeout=3)
    except TimeoutError:
        print(f"TimeoutError after {time.monotonic() - begin:.1f}s")
        return
    print(f"wait_for returned a RunResult after {time.monotonic() - begin:.1f}s")
    print(f"  stop_reason  {result.stop_reason}")
    print(f"  final_text   {result.final_text!r}")
    print(f"  child ran    {started.is_set()}")
    for item in result.history.items:
        if isinstance(item, ToolResult):
            print(f"  ToolResult   {item.content!r}")


# -- F10-12: one file, two agents --------------------------------------------


def rollout_probe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        meta = SessionMeta(session_id=new_session_id(), created=time.time())
        parent = RolloutWriter(rollout_path(meta.session_id, directory), meta)
        try:
            RolloutWriter(rollout_path(meta.session_id, directory), meta)
        except RolloutError as exc:
            print(f"RolloutError: {exc}")
        else:
            print("no error: two writers on one file")
        finally:
            parent.release()


# -- F10-08: what serialising sub-agents costs -------------------------------


async def serial() -> None:
    async def slow(args: dict[str, Any]) -> str:
        await asyncio.sleep(1.0)
        return "ok"

    async def spawn(args: dict[str, Any]) -> str:
        model = ScriptedModel([[("c", "slow", {})], "done"])
        return (await Agent(model, {"slow": slow}).run(args["task"])).final_text

    for label, fp in [("STATEFUL", STATEFUL), ("no declared conflict", Footprint())]:
        model = ScriptedModel(
            [
                [("p1", "spawn_agent", {"task": "a"}), ("p2", "spawn_agent", {"task": "b"})],
                "done",
            ]
        )
        parent = Agent(model, {"spawn_agent": spawn}, footprint_of=lambda call, fp=fp: fp)
        begin = time.monotonic()
        await parent.run("go")
        print(f"two 1.0s sub-agents, spawn = {label:<20} {time.monotonic() - begin:.2f}s")


# -- F10-11: what isolation would actually cost ------------------------------


def worktree() -> None:
    def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
        )

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        git("config", "user.email", "probe@example.com", cwd=repo)
        git("config", "user.name", "probe", cwd=repo)
        (repo / "target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "initial", cwd=repo)

        begin = time.monotonic()
        for name in ("a", "b"):
            git("worktree", "add", "-q", "-b", name, str(Path(tmp) / name), "main", cwd=repo)
        setup = time.monotonic() - begin
        print(f"two worktrees created in {setup * 1000:.0f}ms")
        print(f"disk: {sum(f.stat().st_size for f in Path(tmp).rglob('*') if f.is_file()):,} bytes")

        for name, body in (("a", "    return 2\n"), ("b", "    return 3\n")):
            path = Path(tmp) / name / "target.py"
            path.write_text(f"def f():\n{body}", encoding="utf-8")
            git("commit", "-qam", f"{name} edit", cwd=Path(tmp) / name)

        merged = git("merge", "a", cwd=repo)
        print(f"\nmerge a: rc={merged.returncode} {merged.stdout.strip().splitlines()[0]}")
        merged = git("merge", "b", cwd=repo)
        print(f"merge b: rc={merged.returncode}")
        print(merged.stdout.strip())
        print((repo / "target.py").read_text(encoding="utf-8"))


# -- F10-04: a constraint the child was never told ---------------------------

SAMPLES = 5


def _watch(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Real tools, plus a log of what was actually called."""
    session = Session(mode="workspace-write", approver=AllowAll())
    seen: list[str] = []
    real = default_tools(root=root, session=session)

    def wrap(name: str, fn: Any) -> Any:
        async def handler(args: dict[str, Any]) -> str:
            seen.append(f"{name}({json.dumps(args, ensure_ascii=False)[:70]})")
            return await fn(args)

        return handler

    return {name: wrap(name, fn) for name, fn in real.items()}, seen


CONSTRAINT = (
    "This machine's shell is broken: run_shell fails in ways that look like "
    "real results. Do not use run_shell for anything."
)
FIND_TASK = "Find every place in src/minicodex that calls os.killpg, and say which file it is in."


async def contract() -> None:
    arms: list[tuple[str, str, str | None, bool]] = [
        ("task only", FIND_TASK, None, True),
        ("constraint in the task", f"{FIND_TASK}\n\nConstraints:\n- {CONSTRAINT}", None, True),
        ("constraint as a system note", FIND_TASK, CONSTRAINT, True),
        ("no run_shell in the table", FIND_TASK, None, False),
    ]
    for label, task, note, with_shell in arms:
        violations = 0
        correct = 0
        for sample in range(SAMPLES):
            tools, seen = _watch(ROOT)
            if not with_shell:
                tools = {k: v for k, v in tools.items() if k != "run_shell"}
            child = Agent(
                _model([s for s in TOOL_SCHEMAS if s["function"]["name"] in tools]),
                tools,
                max_turns=5,
                instructions=note,
            )
            result = await _retry(lambda child=child, task=task: child.run(task))
            if any(s.startswith("run_shell") for s in seen):
                violations += 1
            if "shell.py" in result.final_text:
                correct += 1
            if sample == 0:
                print(f"    calls: {seen}")
                print(f"    said:  {result.final_text[:160]!r}")
        print(
            f"{violations:>2}/{SAMPLES} used the broken shell,"
            f" {correct}/{SAMPLES} named shell.py  --  {label}"
        )


# -- F10-05: how much comes back ---------------------------------------------

EXPLAIN = "Explain what src/minicodex/scheduler.py does. Read it first."
SHAPE = "Answer in at most three sentences. No preamble, no lists, no code."


async def length() -> None:
    big = "List every module in src/minicodex and say in one line what each one does."
    for label, task in [
        ("task only", EXPLAIN),
        ("task + output shape", f"{EXPLAIN}\n\n{SHAPE}"),
        ("bigger task + same shape", f"{big}\n\n{SHAPE}"),
    ]:
        sizes = []
        for _ in range(SAMPLES):
            tools, _seen = _watch(ROOT)
            child = Agent(_model(), tools, max_turns=5)
            result = await _retry(lambda child=child, task=task: child.run(task))
            sizes.append(len(result.final_text))
        print(f"{label:<24} chars: {sorted(sizes)}  median {sorted(sizes)[len(sizes) // 2]}")


# -- F10-10: a child that ran out of road ------------------------------------

BIG_TASK = (
    "Work out, by reading the files, whether src/minicodex/shell.py can leave "
    "an orphan process behind on Windows. Read shell.py, then paths.py, then "
    "policy.py, then answer."
)


async def failure() -> None:
    """Does the parent notice that a truncated sub-agent is not an answer?"""
    for label, wrap in [
        ("bare final_text", lambda text, turns: text),
        (
            "outcome label first",
            lambda text, turns: (
                f"[sub-agent: turn budget exhausted after {turns} turns -- it did not finish. "
                f"Its last words were not a conclusion.]\n\n{text}"
            ),
        ),
    ]:
        answered_anyway = 0
        hedged = 0
        for sample in range(SAMPLES):
            child_tools, child_seen = _watch(ROOT)
            returned: list[str] = []

            async def spawn(
                args: dict[str, Any],
                child_tools: Any = child_tools,
                returned: Any = returned,
                wrap: Any = wrap,
            ) -> str:
                child = Agent(_model(), child_tools, max_turns=1)
                result = await child.run(args["task"])
                returned.append(
                    f"{result.stop_reason}/{result.turns_used}: {result.final_text[:120]!r}"
                )
                return wrap(result.final_text, result.turns_used)

            parent_tools, parent_seen = _watch(ROOT)
            parent = Agent(
                _model([*TOOL_SCHEMAS, SPAWN_SCHEMA]),
                {**parent_tools, "spawn_agent": spawn},
                max_turns=4,
            )
            result = await _retry(
                lambda parent=parent: parent.run(
                    f"Use spawn_agent for this, then tell me the answer: {BIG_TASK}"
                )
            )
            said = result.final_text.lower()
            admits = any(
                w in said
                for w in ("did not finish", "not finish", "incomplete", "budget", "unable to")
            )
            if not parent_seen and not admits:
                answered_anyway += 1
            if admits:
                hedged += 1
            if sample == 0:
                print(f"    child returned  {returned}")
                print(f"    child called    {len(child_seen)} tool(s)")
                print(f"    parent called   {parent_seen}")
                print(f"    parent said     {result.final_text[:200]!r}")
        print(
            f"{answered_anyway:>2}/{SAMPLES} passed it on as an answer,"
            f" {hedged}/{SAMPLES} said it was unfinished  --  {label}"
        )


SECTIONS = {
    "naive": naive,
    "contract": contract,
    "length": length,
    "failure": failure,
    "leak": leak,
    "cwd": cwd,
    "race": race,
    "depth": depth,
    "hang": hang,
    "rollout": rollout_probe,
    "serial": serial,
    "worktree": worktree,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SECTIONS:
        print(__doc__)
        return 1
    section = SECTIONS[argv[1]]
    if asyncio.iscoroutinefunction(section):
        asyncio.run(section())
    else:
        section()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
