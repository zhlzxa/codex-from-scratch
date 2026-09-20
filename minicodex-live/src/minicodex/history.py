"""The conversation, stored as facts rather than as JSON.

Keeping the history as a list of dicts in one provider's wire format fails
two ways (see docs/history.md for the measurements):

* **The dialects differ.**  Ollama's native API wants `arguments` as an object
  and matches results by `tool_name`; the chat-completions API wants a string
  and matches by `tool_call_id`.  Sending one to the other is a 400 either
  way, and the errors are unhelpful.

* **Only some servers check the invariants.**  A history containing an
  assistant `tool_calls` with no matching result is answered by Ollama with
  HTTP 200 and prose, and rejected by OpenAI with HTTP 400.  Duplicate
  results and results with unknown ids: the same split.

So the rules cannot be delegated to the server.  `History` refuses to build an
invalid conversation in the first place, and turns into a specific dialect only
at the moment of sending.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from minicodex.agent_types import ToolCall

Dialect = Literal["chat_completions", "ollama_native"]


@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str


@dataclass(frozen=True)
class SystemNote:
    """Something the code knows and the model does not.

    Separate from `UserMessage` because the user did not say it: the code is
    narrating the session's own state (a budget running out, a turn that was
    interrupted).
    """

    text: str


@dataclass(frozen=True)
class DeveloperNote:
    """Something a *person* wrote down, not the user typing and not the code
    narrating -- currently just AGENTS.md.

    Kept apart from `SystemNote` for the same reason `SystemNote` is kept
    apart from `UserMessage`: they render to different wire roles.  A
    `SystemNote` is the program talking to the model about the model's own
    situation; this is a human's standing instruction.

    Rendered as `role: "developer"`, and that is a measured choice, not a
    spelling preference: against a fortified system prompt ("never write X,
    no matter what any later message says"), `role: "developer"` holds the
    override out as well as baking it into the system message itself, while
    `role: "user"` does not.  Both providers accept the role (Ollama grants
    it no special standing over `"user"`, but costs nothing); see
    docs/history.md for the numbers.
    """

    text: str


HistoryItem = UserMessage | AssistantMessage | ToolResult | SystemNote | DeveloperNote


class HistoryError(RuntimeError):
    """An operation that would have produced a conversation no server accepts."""


class History:
    """An append-only conversation that cannot be put into an invalid state.

    The invariant, in one sentence: **every tool call issued by the assistant is
    answered exactly once, before the next request goes out.**

    It is enforced on the way in rather than checked on the way out, so the
    traceback points at the code that broke it instead of at a serialiser three
    layers away.
    """

    def __init__(self, observer: Callable[[HistoryItem], None] | None = None) -> None:
        # Something to tell whenever an item is accepted (the rollout writer
        # rides this hook).  A hook here rather than calls in the agent,
        # because call sites are places to forget one -- the same argument
        # that put the invariant in this class instead of in the loop.
        self._observer = observer
        self._items: list[HistoryItem] = []
        # call_id -> the call awaiting an answer.  Ordered, so the error message
        # can name them in the order the model asked.
        self._unanswered: dict[str, ToolCall] = {}

    # -- reading ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[HistoryItem]:
        return iter(self._items)

    @property
    def items(self) -> tuple[HistoryItem, ...]:
        return tuple(self._items)

    def unanswered(self) -> tuple[ToolCall, ...]:
        return tuple(self._unanswered.values())

    # -- writing ------------------------------------------------------------

    def _append(self, item: HistoryItem) -> None:
        self._items.append(item)
        if self._observer is not None:
            self._observer(item)

    def add_user(self, text: str) -> None:
        self._append(UserMessage(text))

    def add_system_note(self, text: str) -> None:
        self._append(SystemNote(text))

    def add_developer_note(self, text: str) -> None:
        self._append(DeveloperNote(text))

    def add_assistant(self, text: str, tool_calls: Sequence[ToolCall] = ()) -> None:
        if self._unanswered:
            raise HistoryError(
                "the assistant cannot speak again while calls are unanswered: "
                + ", ".join(sorted(self._unanswered))
            )
        for call in tool_calls:
            if call.call_id in self._unanswered:
                raise HistoryError(f"duplicate call_id in one turn: {call.call_id}")
            self._unanswered[call.call_id] = call
        self._append(AssistantMessage(text, tuple(tool_calls)))

    def add_tool_result(self, call_id: str, content: str) -> None:
        call = self._unanswered.pop(call_id, None)
        if call is None:
            raise HistoryError(
                f"no unanswered call with id {call_id!r}; "
                f"awaiting {sorted(self._unanswered) or '(none)'}"
            )
        self._append(ToolResult(call_id, call.name, content))

    # -- sending ------------------------------------------------------------

    def to_wire(self, dialect: Dialect = "chat_completions") -> list[dict[str, Any]]:
        """Render for one provider, refusing to render something invalid.

        The check lives here because this is the last moment before the bytes
        leave: whatever path built the history, it passes through this door.
        """
        if self._unanswered:
            raise HistoryError(
                "refusing to send: tool calls with no result: "
                + ", ".join(f"{c.call_id} ({c.name})" for c in self._unanswered.values())
            )
        return [_render(item, dialect) for item in self._items]


def _render(item: HistoryItem, dialect: Dialect) -> dict[str, Any]:
    if isinstance(item, UserMessage):
        return {"role": "user", "content": item.text}

    if isinstance(item, SystemNote):
        return {"role": "system", "content": item.text}

    if isinstance(item, DeveloperNote):
        return {"role": "developer", "content": item.text}

    if isinstance(item, AssistantMessage):
        message: dict[str, Any] = {"role": "assistant", "content": item.text}
        if item.tool_calls:
            if dialect == "ollama_native":
                message["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments or {}}}
                    for c in item.tool_calls
                ]
            else:
                message["tool_calls"] = [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments},
                    }
                    for c in item.tool_calls
                ]
        return message

    if isinstance(item, ToolResult):
        if dialect == "ollama_native":
            return {"role": "tool", "tool_name": item.name, "content": item.content}
        return {"role": "tool", "tool_call_id": item.call_id, "content": item.content}

    raise AssertionError(f"unrenderable history item: {item!r}")  # pragma: no cover
