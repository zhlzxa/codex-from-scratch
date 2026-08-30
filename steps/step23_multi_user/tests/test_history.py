"""What the history must refuse, pinned so it stays refused.

Fault IDs match FAULTS.md.  Each of these was answered with HTTP 200 by Ollama
and HTTP 400 by OpenAI on 2026-08-06; the point of this file is that neither
answer is needed, because the history never gets into that state.
"""

from __future__ import annotations

import pytest

from minicodex.agent_types import ToolCall
from minicodex.history import History, HistoryError

CALL = ToolCall("call_1", "read_file", {"path": "a.py"}, '{"path":"a.py"}')
CALL2 = ToolCall("call_2", "read_file", {"path": "b.py"}, '{"path":"b.py"}')


def answered_history() -> History:
    h = History()
    h.add_user("what is in a.py?")
    h.add_assistant("Let me look.", [CALL])
    h.add_tool_result("call_1", "contents")
    return h


# ---------------------------------------------------------------------------
# F01-02  an issued call with no result
# ---------------------------------------------------------------------------


def test_F01_02_unanswered_call_blocks_the_next_request() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("Let me look.", [CALL])

    with pytest.raises(HistoryError) as excinfo:
        h.to_wire()

    assert "call_1 (read_file)" in str(excinfo.value)


def test_F01_02_the_error_names_every_unanswered_call() -> None:
    """OpenAI's own message names them; ours does too, one turn earlier."""
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL, CALL2])
    h.add_tool_result("call_1", "contents")

    with pytest.raises(HistoryError) as excinfo:
        h.to_wire()

    assert "call_2" in str(excinfo.value)
    assert "call_1" not in str(excinfo.value)


def test_F01_02_the_assistant_cannot_speak_over_an_unanswered_call() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL])

    with pytest.raises(HistoryError, match="unanswered"):
        h.add_assistant("ignoring that, then")


# ---------------------------------------------------------------------------
# F01-06  the same call answered twice
# ---------------------------------------------------------------------------


def test_F01_06_a_call_cannot_be_answered_twice() -> None:
    h = answered_history()

    with pytest.raises(HistoryError, match="no unanswered call"):
        h.add_tool_result("call_1", "different")


def test_F01_06_a_result_for_an_unknown_id_is_refused() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL])

    with pytest.raises(HistoryError) as excinfo:
        h.add_tool_result("call_NOPE", "stray")

    assert "call_NOPE" in str(excinfo.value)
    assert "call_1" in str(excinfo.value), "the error should say what it was waiting for"


def test_F01_06_two_calls_with_the_same_id_in_one_turn_are_refused() -> None:
    h = History()
    h.add_user("go")

    with pytest.raises(HistoryError, match="duplicate call_id"):
        h.add_assistant("", [CALL, CALL])


# ---------------------------------------------------------------------------
# F01-04  the history was stored in one provider's dialect
# ---------------------------------------------------------------------------


def test_F01_04_chat_completions_dialect() -> None:
    wire = answered_history().to_wire("chat_completions")

    assert wire == [
        {"role": "user", "content": "what is in a.py?"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
    ]


def test_F01_04_ollama_native_dialect() -> None:
    """Arguments as an object, results matched by name -- verified 2026-08-06."""
    wire = answered_history().to_wire("ollama_native")

    assert wire == [
        {"role": "user", "content": "what is in a.py?"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
        },
        {"role": "tool", "tool_name": "read_file", "content": "contents"},
    ]


def test_F01_04_the_same_history_renders_to_both() -> None:
    """The point of the separation: one conversation, two wire formats."""
    h = answered_history()

    a = h.to_wire("chat_completions")
    b = h.to_wire("ollama_native")

    assert a != b
    assert [m["role"] for m in a] == [m["role"] for m in b]


def test_F01_07_system_notes_are_not_user_messages() -> None:
    """The turn-budget warning is something the code knows, not something the
    user said.  Chapter 0 wrote it with role 'system' by hand; now the type
    system remembers which is which."""
    h = History()
    h.add_user("go")
    h.add_system_note("1 turn left")

    wire = h.to_wire()
    assert [m["role"] for m in wire] == ["user", "system"]
