"""Chapter 6: one or more tests per fault, named after it.

The naming is the point: `grep -rn F06_07 .` finds the fault entry, the code and
the test that pins it, and none of the three can be quietly removed.
"""

from __future__ import annotations

import httpx
import pytest

from minicodex.agent import COMPACT_AT, Agent
from minicodex.agent_types import ToolCall
from minicodex.compaction import (
    MAX_ITEM_TOKENS,
    Protected,
    Sizer,
    SummaryRequest,
    boundaries,
    clip_item,
    compact,
    plan,
    render_transcript,
    unused_call_ids,
)
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryError,
    SystemNote,
    ToolResult,
    UserMessage,
)
from minicodex.model import ChatCompletionsModel, Completed, TextDelta, Usage
from minicodex.tokens import (
    CHARS_PER_TOKEN,
    PER_MESSAGE_TOKENS,
    Calibration,
    UncountableContent,
    estimate_messages,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def history_with(pairs: int, *, output: str = "ok", lead: bool = True) -> History:
    h = History()
    if lead:
        h.add_system_note("You are a coding agent.")
        h.add_user("Which Python version does this project support?")
    for i in range(pairs):
        call_id = f"call_{i}"
        h.add_assistant("", [ToolCall(call_id, "run_shell", {}, '{"command": "ls"}')])
        h.add_tool_result(call_id, output)
    return h


async def constant_summary(request: SummaryRequest) -> str:
    return "## Goal\nfind out\n## Done\nran ls\n## Open\nnothing\n"


def sse_transport(body: str) -> httpx.MockTransport:
    """Serve one canned server-sent-events response to the real client.

    The point is that the bytes take the same path they take in production:
    through `stream()`, its status check, its `aiter_lines`, and its parser.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# F06-01 / F06-02 / F06-03  --  every naive cut rule, and what is actually legal
# ---------------------------------------------------------------------------


def test_F06_01_02_03_boundaries_match_what_the_server_accepts():
    """The cut points this code calls legal are the ones the API answered 200 to.

    Recorded against gpt-4o-mini, 2026-08-10 (`probe_cut_points.py`): indices
    0, 1, 2, 4 and 6 returned 200; 3, 5 and 7 returned 400 with `messages with
    role 'tool' must be a response to a preceeding message with 'tool_calls'`.
    """
    items = history_with(3).items
    assert len(items) == 8
    assert boundaries(items) == (0, 1, 2, 4, 6, 8)


def test_F06_01_dropping_the_oldest_half_is_right_only_by_parity():
    """The listed fault says half-cutting orphans a result.  On an even history
    it does not -- which is worse, because it means the bug ships."""
    even = history_with(3).items  # 8 items, half is index 4: legal
    assert len(even) // 2 in boundaries(even)

    odd = history_with(3)
    odd.add_assistant("thinking out loud")  # 9 items; half is index 4, still legal
    odd_items = odd.items
    assert len(odd_items) // 2 in boundaries(odd_items)
    # ...and keeping the last four lands on a result, which is a 400.  Neither
    # rule knows which case it is in, because neither rule looks.
    assert len(odd_items) - 4 not in boundaries(odd_items)


def test_F06_02_token_count_cut_lands_inside_a_turn():
    items = history_with(3).items
    # "Keep roughly the last 5 messages", which is what a token-driven cut
    # degenerates into, is illegal here and legal one message either side.
    assert 3 not in boundaries(items)
    assert 4 in boundaries(items)


def test_F06_03_keep_last_n_can_start_on_a_result():
    items = history_with(3).items
    assert unused_call_ids(items[-1:]) == ("call_2",)
    assert unused_call_ids(items[-2:]) == ()


async def test_F06_01_rebuilding_an_illegal_cut_raises_locally():
    """The invariant from chapter 1 is what makes this a local failure.

    An orphaned result does not reach a provider and come back as a 400 -- the
    replay refuses it here, naming the id.
    """
    items = history_with(3).items
    broken = History()
    with pytest.raises(HistoryError) as excinfo:
        broken.add_tool_result(items[3].call_id, "orphan")
    assert "call_a" not in str(excinfo.value)
    assert "no unanswered call" in str(excinfo.value)


# ---------------------------------------------------------------------------
# F06-06  --  the protected region
# ---------------------------------------------------------------------------


def test_F06_06_protected_prefix_is_notes_then_first_user_message():
    items = history_with(2).items
    assert Protected.of(items).count == 2
    assert isinstance(items[0], SystemNote)
    assert isinstance(items[1], UserMessage)


def test_F06_06_later_system_notes_are_not_protected():
    """Chapter 0's turn-budget warning is a SystemNote and is worth one turn."""
    h = history_with(1)
    h.add_system_note("You have 2 turn(s) left.")
    h.add_user("carry on")
    assert Protected.of(h.items).count == 2


async def test_F06_06_user_question_survives_compaction():
    h = history_with(12, output="x" * 300)
    result = await compact(h, summarise=constant_summary, budget=600)
    assert isinstance(result.history.items[0], SystemNote)
    assert isinstance(result.history.items[1], UserMessage)
    assert "Which Python version" in result.history.items[1].text


async def test_F06_06_compaction_never_produces_an_invalid_history():
    for pairs in range(1, 14):
        h = history_with(pairs, output="y" * 200)
        result = await compact(h, summarise=constant_summary, budget=500)
        assert unused_call_ids(result.history.items) == ()
        result.history.to_wire()  # raises if a call went unanswered


# ---------------------------------------------------------------------------
# F06-04 / F06-05  --  what the summary is asked for
# ---------------------------------------------------------------------------


async def test_F06_04_summariser_is_given_the_region_being_destroyed():
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Done\nnothing\n"

    h = history_with(12, output="z" * 300)
    await compact(h, summarise=capture, budget=600)
    assert len(seen) == 1
    assert "RESULT OF run_shell" in seen[0].transcript


async def test_F06_04_summariser_is_also_given_the_protected_prefix():
    """Found by measurement, not by review.

    Without `context`, gpt-4o-mini wrote `## Goal: Record the change in
    CHANGELOG.md` for a session whose actual goal was adding retry logic --
    3/3.  It had been ordered to produce a Goal section and shown everything
    except the goal, so it inferred one from the most recent work.
    """
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Goal\nx\n"

    h = history_with(12, output="z" * 300)
    await compact(h, summarise=capture, budget=600)
    assert "Which Python version does this project support?" in seen[0].context
    # ...and it is context, not material: it stays in the history verbatim.
    assert "RESULT OF" not in seen[0].context


def test_F06_05_prompt_requires_an_artefact_for_every_finished_step():
    from minicodex import compaction_prompt

    text = compaction_prompt()
    for heading in ("## Goal", "## Done", "## Decisions", "## Constraints", "## Open"):
        assert heading in text
    assert "must not be repeated" in text
    assert "A step with no artefact named is not done." in text


def test_F06_05_render_transcript_keeps_the_tool_that_produced_each_result():
    h = history_with(1, output="Python 3.13.0")
    text = render_transcript(h.items)
    assert "RESULT OF run_shell:\nPython 3.13.0" in text
    assert "ASSISTANT CALLS run_shell" in text


# ---------------------------------------------------------------------------
# F06-07  --  the estimate, and the correction
# ---------------------------------------------------------------------------


def test_F06_07_estimate_counts_the_tool_schemas():
    """The first version did not, and the schemas are re-sent every turn."""
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "run_shell", "description": "x" * 400}}]
    assert estimate_messages(messages, tools) > estimate_messages(messages) + 90


def test_F06_07_estimate_counts_a_cost_per_message_not_just_per_character():
    """Also a mutation-testing find: setting `PER_MESSAGE_TOKENS = 0` left all
    247 tests green, so the framing cost was a constant nothing depended on.

    Measured: a 37-character user message reports 16 prompt tokens, where the
    content alone accounts for 9.  Splitting the same text across more messages
    therefore costs more, and an agent history is many small messages.
    """
    one = [{"role": "user", "content": "abcd" * 20}]
    many = [{"role": "user", "content": "abcd" * 4} for _ in range(5)]
    assert sum(len(m["content"]) for m in one) == sum(len(m["content"]) for m in many)
    assert estimate_messages(many) > estimate_messages(one)
    assert estimate_messages(many) - estimate_messages(one) == PER_MESSAGE_TOKENS * 4


def test_F06_07_calibration_is_inert_until_the_server_speaks():
    calibration = Calibration()
    assert calibration.calibrated is False
    assert calibration.ratio == 1.0
    assert calibration.correct(500) == 500
    assert "uncalibrated" in calibration.describe()


def test_F06_07_calibration_uses_the_reported_number():
    calibration = Calibration()
    calibration.observe(estimated=700, actual=1007)  # measured turn 3
    assert calibration.correct(700) == 1007
    assert "x1.44" in calibration.describe()


@pytest.mark.parametrize("actual", [0, -1])
def test_F06_07_a_missing_usage_field_cannot_zero_the_ratio(actual):
    """A provider that omits usage parses as 0, and `estimate * 0` is a budget
    that says every request is free."""
    calibration = Calibration()
    calibration.observe(estimated=500, actual=actual)
    assert calibration.ratio == 1.0


def test_F06_07_usage_must_be_requested_explicitly():
    """Measured: a streaming request returns 0 usage chunks without this, and
    1 with it.  Nothing errors -- the calibration source simply never arrives."""
    body = ChatCompletionsModel().request_body([{"role": "user", "content": "hi"}])
    assert body["stream_options"] == {"include_usage": True}
    off = ChatCompletionsModel(report_usage=False).request_body([])
    assert "stream_options" not in off


async def test_F06_07_the_usage_chunk_has_no_choices():
    """The chunk carrying usage arrives with `"choices": []`.

    `chunk["choices"][0]` worked for five chapters and becomes an IndexError
    the moment usage is switched on -- after the answer has already streamed.

    Driven through the real `stream()` over a mock transport.  The first
    version of this test mirrored the parsing loop into the test file, which
    made it green with the guard deleted from the module: mutation testing
    caught it, and it is the same fault as chapter 5's -- a check that does not
    reach the thing it claims to check.
    """
    body = (
        'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":77,"completion_tokens":9}}\n\n'
        "data: [DONE]\n\n"
    )
    model = ChatCompletionsModel(transport=sse_transport(body))
    events = [event async for event in model.stream([{"role": "user", "content": "hi"}])]
    assert Usage(77, 9) in events
    assert TextDelta("hi") in events
    assert Completed("stop") in events


async def test_F06_07_the_agent_feeds_usage_back_into_the_estimate():
    class ModelWithUsage:
        tools = ()

        async def stream(self, messages):
            yield TextDelta("done")
            yield Usage(1000, 5)
            yield Completed("stop")

    agent = Agent(ModelWithUsage())
    await agent.run("hello")
    assert agent.calibration.calibrated
    assert agent.calibration.ratio > 1


# ---------------------------------------------------------------------------
# F06-10  --  content this module does not model
# ---------------------------------------------------------------------------


def test_F06_10_multimodal_content_is_refused_not_guessed():
    """`len()` of a two-part content list is 2, which over four is 0.

    An image counted as free is worse than an uncounted one: the budget fires
    late *and* reports that everything is fine.
    """
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }
    ]
    with pytest.raises(UncountableContent) as excinfo:
        estimate_messages(messages)
    # The message names the gap (only text is modelled) rather than a fault id.
    assert "only text is modelled" in str(excinfo.value)


def test_F06_10_absent_content_is_zero_not_an_error():
    assert estimate_messages([{"role": "assistant", "content": None}]) >= 0


# ---------------------------------------------------------------------------
# F06-09  --  one item bigger than the whole window
# ---------------------------------------------------------------------------


def test_F06_09_an_oversized_result_is_clipped_at_both_ends():
    body = "TOP: ValueError\n" + "middle\n" * 5000 + "BOTTOM: 3 failed\n"
    clipped = clip_item(ToolResult("c1", "run_shell", body), max_tokens=100)
    assert "TOP: ValueError" in clipped.content
    assert "BOTTOM: 3 failed" in clipped.content
    assert "characters omitted by compaction" in clipped.content
    assert len(clipped.content) < len(body) / 10


def test_F06_09_only_results_are_clipped():
    note = SystemNote("x" * 100_000)
    assert clip_item(note, max_tokens=10) is note


async def test_F06_09_a_single_huge_result_is_clipped_even_when_nothing_can_be_dropped():
    """Dropping older turns cannot help when the thing that does not fit is the
    most recent turn, and the most recent turn is the one that must be kept."""
    h = history_with(1, output="q" * 200_000)
    # A budget that comfortably holds a clipped result but not a raw one: the
    # only way to satisfy it is to shrink the item rather than drop the turn.
    result = await compact(h, summarise=constant_summary, budget=MAX_ITEM_TOKENS * 2)
    kept = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert kept, "the most recent turn was dropped instead of being clipped"
    assert len(kept[0].content) <= MAX_ITEM_TOKENS * 4 + 200
    assert "characters omitted by compaction" in kept[0].content


# ---------------------------------------------------------------------------
# F06-08  --  the summariser itself fails
# ---------------------------------------------------------------------------


async def test_F06_08_a_failed_summariser_degrades_instead_of_raising():
    async def unreachable(request: SummaryRequest) -> str:
        raise ConnectionError("summariser unreachable")

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=unreachable, budget=600)
    assert result.degraded
    result.history.to_wire()  # still a legal conversation


async def test_F06_08_the_model_is_told_that_context_was_lost():
    async def unreachable(request: SummaryRequest) -> str:
        raise TimeoutError("no response in 30s")

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=unreachable, budget=600)
    note = result.history.items[2].text
    assert "could not be generated" in note
    assert "TimeoutError" in note
    assert "Do not assume any earlier step succeeded" in note
    assert "message(s) were discarded unread" in note


async def test_F06_08_an_empty_summary_counts_as_a_failure():
    """A summariser that returns "" is not a very short summary; it is a
    silently destroyed transcript."""

    async def says_nothing(request: SummaryRequest) -> str:
        return "   \n  "

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=says_nothing, budget=600)
    assert result.degraded


# ---------------------------------------------------------------------------
# F06-12  --  the summary is over budget
# ---------------------------------------------------------------------------


async def test_F06_12_an_oversized_summary_is_trimmed():
    async def verbose(request: SummaryRequest) -> str:
        return "PATH /repo/src/thing.py\n" * 1000

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=verbose, budget=600, summary_budget=100)
    assert result.plan.drops > 0, "nothing was dropped, so nothing was summarised"
    assert "truncated to fit its budget" in result.summary
    assert Sizer().text(result.summary) <= 120


async def test_F06_12_the_summary_budget_is_reserved_before_the_cut_is_chosen():
    """Choosing a cut and then discovering the summary does not fit is a
    decision that cannot be revisited: the messages are already gone."""
    items = history_with(40, output="w" * 300).items
    tight = plan(items, budget=3000, summary_budget=1500)
    loose = plan(items, budget=3000, summary_budget=0)
    assert tight.cut > loose.cut
    assert tight.drops > loose.drops


@pytest.mark.parametrize(
    ("pairs", "chars", "budget", "summary_budget"),
    [
        (1, 20, 1, 700),  # nothing fits at all: the fallback at the end of plan()
        (12, 300, 1450, 400),  # a cut fits and costs more than it saves: the
    ],  # guard inside the loop.  This one is 1099 tokens, so a
)  # 400-token summary buys back less than it costs.
async def test_a_compaction_that_would_not_save_anything_does_nothing(
    pairs, chars, budget, summary_budget
):
    """Found by the property test at seed 235: compacting an 89-token history
    produced a 93-token one.

    The summary has a fixed cost, so a small dropped region is a net loss --
    and a net loss is not merely wasted, it is a loop: the estimate stays above
    the trigger and the next turn compacts again.

    Both branches, because the first version of this test only reached the
    fallback.  Mutation testing found that: disabling the guard *inside* the
    loop left every test green.  And the property run that found the bug in the
    first place used 5000 cases, while the default is 200 -- seed 235 is not in
    the set that runs on an ordinary `pytest`.  A property test that catches
    something is not the same as a test that keeps catching it.
    """
    small = history_with(pairs, output="w" * chars)
    before = Sizer().messages(small.to_wire())
    result = await compact(
        small, summarise=constant_summary, budget=budget, summary_budget=summary_budget
    )
    assert result.plan.drops == 0
    assert result.plan.saving == 0
    assert Sizer().messages(result.history.to_wire()) == before
    assert result.summary == ""


def test_F06_12_the_summary_is_measured_with_the_same_sizer_as_the_cut():
    """The bug this pins: `plan()` used the calibrated estimate and the summary
    trimmer used a bare `len(text) // 4`, so the reservation and the thing
    filling it were measured with different rulers."""
    dense = Sizer(ratio=2.0)
    text = "x" * 4000
    assert dense.text(text) == 2000
    assert Sizer().text(text) == 1000
    assert len(dense.clip_text(text, 100)) < len(Sizer().clip_text(text, 100))


# ---------------------------------------------------------------------------
# F06-13  --  summaries of summaries
# ---------------------------------------------------------------------------


async def test_F06_13_generations_are_counted():
    h = history_with(12, output="w" * 300)
    first = await compact(h, summarise=constant_summary, budget=600)
    assert first.generation == 1

    grown = first.history
    for i in range(12):
        call_id = f"more_{i}"
        grown.add_assistant("", [ToolCall(call_id, "run_shell", {}, "{}")])
        grown.add_tool_result(call_id, "w" * 300)
    second = await compact(grown, summarise=constant_summary, budget=600)
    assert second.generation == 2


async def test_F06_13_the_previous_summary_is_carried_forward_not_re_summarised():
    """What stops decay compounding: generation N+1 is told that generation N's
    content has already survived one compaction and must be preserved."""
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Constraints\nmust stay on Python 3.9\n"

    h = history_with(12, output="w" * 300)
    first = await compact(h, summarise=capture, budget=600)
    assert seen[0].established is None
    assert seen[0].generation == 0

    grown = first.history
    for i in range(12):
        call_id = f"more_{i}"
        grown.add_assistant("", [ToolCall(call_id, "run_shell", {}, "{}")])
        grown.add_tool_result(call_id, "w" * 300)
    await compact(grown, summarise=capture, budget=600)
    assert seen[1].established is not None
    assert "Python 3.9" in seen[1].established
    assert seen[1].generation == 1


def test_F06_13_the_prompt_tells_the_model_established_facts_outrank_age():
    from minicodex import compaction_prompt

    text = compaction_prompt()
    assert "<established>" in text
    assert "Do not compress it further" in text
    assert "because it looks old" in text


# ---------------------------------------------------------------------------
# F06-11  --  when compaction is allowed to happen
# ---------------------------------------------------------------------------


async def test_F06_11_compaction_happens_between_turns_and_not_inside_one():
    """Enforced by where the call is, not by a flag.

    `_maybe_compact` is reachable only from the top of the loop.  Nothing in
    `_collect` can reach it, so a stream cannot be interrupted by a history
    that changes underneath it.
    """
    from minicodex.model import ToolCallDelta

    class Chatty:
        tools = ()

        def __init__(self):
            self.turn = 0

        async def stream(self, messages):
            self.turn += 1
            if self.turn <= 3:
                yield TextDelta("working")
                yield ToolCallDelta(f"c{self.turn}", 0, "run_shell", '{"command": "ls"}')
                yield Completed("tool_calls")
            else:
                yield TextDelta("done")
                yield Completed("stop")

    async def big_tool(args):
        return "o" * 4000

    compactions: list[str] = []

    async def watch(request: SummaryRequest) -> str:
        compactions.append(request.transcript)
        return "## Done\nsome work\n"

    agent = Agent(
        Chatty(),
        {"run_shell": big_tool},
        context_window=1500,
        summariser=watch,
        max_turns=6,
    )
    result = await agent.run("go")
    assert result.compactions
    # One compaction per turn at most, and each one observed a whole number of
    # turns: no transcript ends on an unanswered call.
    for transcript in compactions:
        assert transcript.count("ASSISTANT CALLS") == transcript.count("RESULT OF")


async def test_F06_11_no_compaction_below_the_trigger():
    class Quiet:
        tools = ()

        async def stream(self, messages):
            yield TextDelta("hi")
            yield Completed("stop")

    async def never(request: SummaryRequest) -> str:  # pragma: no cover
        raise AssertionError("compaction fired below the trigger")

    agent = Agent(Quiet(), context_window=1_000_000, summariser=never)
    result = await agent.run("hello")
    assert result.compactions == ()


def test_F06_11_the_trigger_and_the_target_are_not_the_same_number():
    """Compacting down to the trigger point means compacting again next turn."""
    from minicodex.agent import COMPACT_TO

    assert COMPACT_TO < COMPACT_AT


# ---------------------------------------------------------------------------
# the shape of the module itself
# ---------------------------------------------------------------------------


def test_chars_per_token_is_the_prose_figure_not_a_tuned_one():
    """4.0 is what English prose measured at (4200 chars / 1008 tokens).

    It is deliberately the optimistic end: `Calibration` moves it, and a
    pessimistic starting point wastes the whole window on turn one, before any
    observation exists to correct it.
    """
    assert CHARS_PER_TOKEN == 4.0


async def test_compaction_shrinks_the_thing_it_was_called_about():
    h = history_with(20, output="v" * 400)
    before = Sizer().messages(h.to_wire())
    result = await compact(h, summarise=constant_summary, budget=1500)
    after = Sizer().messages(result.history.to_wire())
    assert after < before / 2
    assert result.plan.fits


def test_plan_reports_when_nothing_can_be_made_to_fit():
    """A budget smaller than the protected prefix is unsatisfiable, and saying
    so beats raising: the caller still has to send something."""
    items = history_with(4, output="u" * 200).items
    assert plan(items, budget=1).fits is False


async def test_an_assistant_message_with_no_calls_is_a_boundary():
    h = history_with(2)
    h.add_assistant("here is what I found")
    assert len(h.items) in boundaries(h.items)
    assert isinstance(h.items[-1], AssistantMessage)
