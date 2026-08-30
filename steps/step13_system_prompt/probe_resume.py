"""Does the model need to be told it was interrupted?

The file-level questions in `probe_rollout.py` are settled by looking at bytes.
This one is not: after a crash, the recovered history is *silent* about the turn
that was dropped.  It reads as a conversation in which the last thing that
happened is the last thing on the page -- and the thing that was dropped is the
`apply_patch` that may or may not have already run.

The measurement: what does the model do first when told to carry on?

    verify   read the file / run a command to find out what the state is
    assume   patch (or re-patch) straight away

A: the recovered history, as chapter 1 would rebuild it, with nothing added.
B: the same history plus the system note from `interrupted_note(dropped)`.

Needs OPENAI_API_KEY.  Costs a few cents.

    uv run python probe_resume.py [samples]
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from minicodex.agent_types import ToolCall
from minicodex.history import History
from minicodex.model import OPENAI_BASE_URL
from minicodex.rollout import environment_note, interrupted_note
from minicodex.tools import TOOL_SCHEMAS

MODEL = "gpt-4o-mini"

TASK = (
    "In client.py, add a retry around the send() call, then run pytest and tell "
    "me whether it passes."
)

FILE = """\
import httpx

def send(payload):
    return httpx.post("https://example.invalid/v1", json=payload)
"""


def recovered_history(note: str | None) -> History:
    """What is on disk after a crash during `apply_patch`, loaded back.

    The assistant message that issued the patch call, and the call itself, are
    gone -- they were dropped because the call had no result.  The patch may
    nonetheless have been written: the tool ran, the process died before the
    result was recorded.
    """
    history = History()
    history.add_system_note(
        "You are a coding agent working in a user's repository. "
        "You can read files, run shell commands, and edit files with apply_patch."
    )
    history.add_user(TASK)
    history.add_assistant(
        "Let me look at the file first.",
        [
            ToolCall(
                "call_read",
                "read_file",
                {"path": "client.py"},
                '{"path":"client.py"}',
            )
        ],
    )
    history.add_tool_result("call_read", FILE)
    if note is not None:
        history.add_system_note(note)
    history.add_user("carry on")
    return history


def classify(calls: tuple[ToolCall, ...], text: str) -> str:
    if not calls:
        return "no-call"
    first = calls[0]
    if first.name in {"read_file"}:
        return "verify"
    if first.name == "run_shell":
        command = str((first.arguments or {}).get("command", ""))
        head = command.strip().split()[0] if command.strip() else ""
        if head in {"cat", "head", "less", "grep", "ls", "git", "sed", "tail", "type"}:
            return "verify"
        return "assume(run)"
    if first.name == "apply_patch":
        return "assume(patch)"
    return f"other({first.name})"


def one(history: History) -> str:
    """One request, non-streaming.

    Deliberately not `ChatCompletionsModel`: this probe is measuring the model,
    not the client, and the assembled-from-fragments path is already pinned by
    chapter 1's tests.  Fewer moving parts between the question and the answer.
    """
    payload = {
        "model": MODEL,
        "messages": history.to_wire(),
        "tools": list(TOOL_SCHEMAS),
    }
    # This machine drops roughly one TLS handshake in three.  Chapter 12 is
    # about doing this properly; here it is three lines so the measurement can
    # happen at all.
    for attempt in range(12):
        try:
            response = httpx.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json=payload,
                timeout=60,
            )
            break
        except httpx.ConnectError:
            if attempt == 11:
                raise
            time.sleep(2)
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    calls = tuple(
        ToolCall(
            c["id"],
            c["function"]["name"],
            json.loads(c["function"]["arguments"] or "{}"),
            c["function"]["arguments"],
        )
        for c in message.get("tool_calls") or ()
    )
    return classify(calls, message.get("content") or "")


def ab(samples: int) -> None:
    arms = {
        "A no note": None,
        "B interrupted note": interrupted_note(2),
        # A note that is true, neutral, and carries no warning.  If this moves
        # the number as much as B does, then what B bought was not the
        # information -- it was the fact that a system message appeared at all,
        # and the wording in `interrupted_note` is decoration.
        "C placebo note": "This session was resumed from a file on disk.",
        # B's claim, minus the count.  Splits "something happened" from "two
        # specific messages are missing".
        "D vague warning": ("The previous session ended without finishing its last turn."),
    }
    for label, note in arms.items():
        results = [one(recovered_history(note)) for _ in range(samples)]
        tally: dict[str, int] = {}
        for r in results:
            tally[r] = tally.get(r, 0) + 1
        verified = sum(v for k, v in tally.items() if k == "verify")
        print(f"=== {label}")
        print(f"    {json.dumps(tally)}")
        print(f"    verified first: {verified}/{samples}")


def environment(samples: int) -> None:
    """F07-07: the session is resumed somewhere else.

    The history is full of relative paths that meant something in the old
    working directory.  Same question: does saying so change the first move?
    """
    from minicodex.rollout import SessionMeta

    note = environment_note(
        SessionMeta("old", cwd="/repo/service-a", model=MODEL, sandbox_mode="workspace-write"),
        SessionMeta("new", cwd="/repo/service-b", model=MODEL, sandbox_mode="workspace-write"),
    )
    assert note is not None
    for label, extra in {"A no note": None, "B environment note": note}.items():
        results = [one(recovered_history(extra)) for _ in range(samples)]
        tally: dict[str, int] = {}
        for r in results:
            tally[r] = tally.get(r, 0) + 1
        print(f"=== env {label}")
        print(f"    {json.dumps(tally)}")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    ab(count)
    environment(count)
