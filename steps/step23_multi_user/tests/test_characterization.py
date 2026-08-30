"""What the code does today, written down before any of it moves.

These are *characterization* tests, and they are a different animal from the
rest of the suite.  An ordinary test says what the code **should** do, and is
written from the requirement.  A characterization test says what the code
**currently does**, is written from the code, and is allowed to pin behaviour
nobody would choose on purpose.  Chapter 4 already shipped one without naming
it: `test_several_edits_to_one_file_all_land` pins "the last edit wins", which
is a known defect.

They exist because "refactor" means "behaviour does not change", and nothing
can be shown not to have changed unless something describes it first.  Three
mutations of `default_tools()` -- rename a key, drop an entry, add an entry --
each left all 125 tests green before this file existed.  `default_tools` was
mentioned by exactly zero of them.

FA-02 in FAULTS.md.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from minicodex.agent import Agent
from minicodex.approval import AllowAll, Session
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.tools import TOOL_SCHEMAS, default_tools

FIXTURES = Path(__file__).parent / "fixtures"


def _canonical(obj: Any) -> str:
    """One formatting, so a diff shows content changes and nothing else."""
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# FA-02  the schema table, whole
# ---------------------------------------------------------------------------


def test_FA_02_the_entire_schema_is_pinned_not_only_its_descriptions() -> None:
    """`test_F03_10_descriptions_are_pinned` covers every description.

    It does not cover anything else: types, `required` lists, the nesting, the
    order tools appear in.  All of those are sent to the model on every single
    turn, so all of them are behaviour.  This pins the bytes.

    To regenerate deliberately:
        python -c "import json,minicodex.tools as t; \
print(json.dumps(t.TOOL_SCHEMAS, indent=2, ensure_ascii=False))" \
> tests/fixtures/tool_schemas.json
    """
    expected = (FIXTURES / "tool_schemas.json").read_text(encoding="utf-8")
    assert _canonical(TOOL_SCHEMAS) == expected


# ---------------------------------------------------------------------------
# FA-02  the two tables have to agree, and today nothing says so
# ---------------------------------------------------------------------------


def _advertised() -> set[str]:
    return {tool["function"]["name"] for tool in TOOL_SCHEMAS}


def test_FA_02_every_advertised_tool_has_a_handler(tmp_path: Path) -> None:
    """Otherwise the model calls it and is told the tool does not exist.

    Measured: with `apply_patch` removed from the handler table only, a model
    that calls it receives

        Error: no tool named 'apply_patch'. Available tools: read_file,
        run_shell. Call one of those instead.

    which is the message chapter 0 wrote for a model that invents a tool name
    (F00-06).  A wiring mistake arrives wearing the model's clothes, and the
    model duly obeys it and stops trying.
    """
    missing = _advertised() - set(default_tools(tmp_path))
    assert not missing, f"advertised to the model but not runnable: {sorted(missing)}"


def test_FA_02_every_handler_is_advertised(tmp_path: Path) -> None:
    """The other direction is worse, because there is no error at all.

    A handler the schema never mentions is a capability the model is never
    told about, so it is never called, and nothing anywhere reports it.  The
    tool is simply absent, and the only symptom is the agent being a bit worse
    at its job.
    """
    extra = set(default_tools(tmp_path)) - _advertised()
    assert not extra, f"runnable but never shown to the model: {sorted(extra)}"


# ---------------------------------------------------------------------------
# FA-02  a whole run, pinned turn by turn
# ---------------------------------------------------------------------------


class ScriptedModel:
    """A model with no opinions: it replays a fixed list of turns.

    Deterministic on purpose.  The transcript below is the thing being pinned,
    so anything that could vary between runs -- a real model, a clock, a
    subprocess -- would make the pin meaningless.
    """

    def __init__(self, turns: list[list[ToolCallDelta] | str]) -> None:
        self.turns = turns
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for call in turn:
                yield call
        yield Completed("stop")


def _call(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    """`index` positions the call within its turn; `call_id` identifies it globally.

    They are separate because the wire keeps them separate: `index` restarts at
    0 every turn, and both providers issue ids that never repeat.  The history
    would in fact accept a repeated id -- it only requires ids to be unique
    among the calls currently unanswered -- but a transcript that reuses
    `call_0` every turn would be teaching a habit no real provider has.
    """
    return ToolCallDelta(
        call_id=call_id,
        index=index,
        name=name,
        arguments=json.dumps(arguments),
    )


async def test_FA_02_a_whole_run_is_pinned_message_by_message(tmp_path: Path) -> None:
    """Every request body the loop produces across a three-turn run.

    This is the artefact the refactor is measured against.  It covers, in one
    run, the things the loop is responsible for: several calls in one turn,
    one result per call in the order the calls were made, a tool that fails,
    the turn-budget note appearing at the right moment, and stopping on a turn
    that has prose and no calls.

    `run_shell` is deliberately not exercised: it spawns a POSIX shell, and a
    transcript that only pins on one platform pins nothing on the other.

    Chapter 5 note.  This test went red the moment the approval gate was wired
    in, and it was right to: `default_tools(tmp_path)` builds a default
    `Session`, the default is `read-only` with `DenyAll`, and `apply_patch` was
    refused.  That is the fail-closed default working, and it is pinned by
    `test_F05_00_the_default_session_can_read_and_nothing_else` below.

    Here the session is made explicitly permissive so this transcript keeps
    measuring the thing it was written to measure -- the loop.  Adding a
    feature is allowed to change behaviour; it is not allowed to change
    behaviour *quietly*, and the difference between the two is whether a red
    test got a new fixture or a new argument.

    Chapter 11 note.  This is the first time the fixture itself was replaced.
    The turn-budget note was reworded, so the pinned request bodies changed,
    and that is a deliberate behaviour change: four runs out of five ended a
    budget-exhausted task with an answer of zero characters, and the sentence
    chapter 0 wrote is part of why.  The fixture was regenerated on purpose and
    the diff was read line by line -- one system message differs, and nothing
    else in the transcript moved.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    model = ScriptedModel(
        [
            [
                _call("call_a1", 0, "read_file", path="a.py"),
                _call("call_a2", 1, "read_file", path="missing.py"),
            ],
            [
                _call(
                    "call_b1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}],
                )
            ],
            "Changed x to 2.",
        ]
    )

    tools = default_tools(tmp_path, Session(mode="workspace-write", approver=AllowAll()))
    result = await Agent(model, tools, max_turns=4).run("change x to 2 in a.py")

    actual = {
        "requests": model.sent,
        "final_text": result.final_text,
        "stop_reason": result.stop_reason,
        "turns_used": result.turns_used,
        "file_after": (tmp_path / "a.py").read_text(encoding="utf-8"),
    }
    expected = (FIXTURES / "golden_transcript.json").read_text(encoding="utf-8")
    assert _canonical(actual) == expected


async def test_F05_00_the_default_session_can_read_and_nothing_else(tmp_path: Path) -> None:
    """The same run with the default session, which is the one a caller gets
    by forgetting to pass anything.

    A fail-closed default is only a claim until something runs without one.
    This is that something: identical script, no `Session` argument, and the
    edit does not land.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    model = ScriptedModel(
        [
            [
                _call(
                    "call_b1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}],
                )
            ],
            "I could not change it.",
        ]
    )

    await Agent(model, default_tools(tmp_path), max_turns=4).run("change x to 2 in a.py")

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 1\n"
    answered = model.sent[1][-1]["content"]
    assert answered.startswith("Permission denied:"), answered
