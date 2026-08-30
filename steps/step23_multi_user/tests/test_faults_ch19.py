"""Chapter 19: the web console. Every test here is offline.

Nothing in this file opens a socket to anything but itself. The agent runs
against `stub_url` -- the recorded-Ollama server every other chapter uses -- and
the console runs through `TestClient`, which speaks ASGI directly. The one
question that needs a real model, *what a reader waits for when the console
streams per history item rather than per token*, is measured by
`probe_web.py stream` against the real API and is not asserted here.

The faults below divide into two kinds, and the split is the chapter's point.
F19-01 through F19-05 and F19-09 through F19-11 are faults in the console: a
race, a thread, an ordering, a leak, a missing reconnect, a dropped event, an
unvalidated path, a default that is not a consent. F19-06 through F19-08 are
faults in the *checks* -- rules that had been passing for eighteen chapters and
silently stopped covering anything the moment the package grew a subdirectory.
The second kind is the more expensive one, because a green tick kept saying
otherwise.
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from console_login import CONSOLE_ACCOUNT, sign_in
from minicodex.approval import ApprovalReply, ApprovalRequest, Session
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES, Risk
from minicodex.tenancy import Owner
from minicodex.web import create_app
from minicodex.web.approver import ApprovalBroker, WebApprover
from minicodex.web.channel import Channel
from minicodex.web.store import DEFAULT_THREAD_SETTINGS, SEED_PROVIDERS, Store, validate_settings

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"
FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


@pytest.fixture
def console(tmp_path: Path) -> Any:
    # As a context manager, so the app's lifespan runs and one event loop
    # survives between requests. Without that, a background turn is finished
    # before the next request arrives and "is this thread busy" is never true
    # -- which would make F19-01's tests pass against the broken code too.
    #
    # `home` and `sign_in` are chapter 23's. The console has no anonymous
    # surface any more, so every test in this module is now a signed-in test --
    # which is the honest description of what it always was.
    with TestClient(create_app(tmp_path / "console", home=tmp_path / "home")) as client:
        sign_in(client)
        yield client


@pytest.fixture
def hanging_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `run_turn` with one that never finishes.

    The race in F19-01 is about *when* the claim is written relative to the
    response, so the test needs a turn that is still running when the second
    request arrives. A real one against the stub is over in milliseconds; this
    one is over when the test is.
    """
    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _thread(client: TestClient, workspace: Path, **settings: Any) -> str:
    response = client.post(
        "/api/threads",
        json={"workspace": str(workspace), "title": "test", "settings": settings},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


class Recorder:
    """A socket that just remembers what it was sent, in order."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_text(self, data: str) -> None:
        # A real socket awaits on the first byte, and that await is exactly
        # what let the old fan-out reorder. Yielding here means a test that
        # passes cannot be passing by accident of never suspending.
        await asyncio.sleep(0)
        self.sent.append(json.loads(data))


class Uneven:
    """A socket that takes *longer* on the events that were emitted earlier.

    `Recorder` suspends for the same length of time on every send, and that
    turns out to be why the fan-out mutation survived: a hundred tasks that
    each yield once resume in the order they were created, so
    `create_task`-per-event still delivers in order and the assertion holds.
    The test was measuring the scheduler's FIFO habit, not the pump.

    Real sockets are not uniform -- the first write on a connection pays for a
    handshake the tenth does not -- and the delay here is keyed off the event's
    own number rather than off call order, so it does not depend on which task
    happens to start first. Under the awaited pump the sends are sequential and
    arrive in order regardless; under a fan-out they arrive backwards.
    """

    def __init__(self, count: int) -> None:
        self.sent: list[dict[str, Any]] = []
        self._count = count

    async def send_text(self, data: str) -> None:
        event = json.loads(data)
        await asyncio.sleep(0.002 * (self._count - 1 - event["n"]))
        self.sent.append(event)


# ---------------------------------------------------------------------------
# F19-01  a turn is claimed before it is started, not inside it
# ---------------------------------------------------------------------------


def test_F19_01_the_busy_flag_is_claimed_before_the_task_is_created(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """Two messages arriving together must not start two runs on one thread.

    The original tested `thread_id in _busy` in the handler and did `_busy.add`
    inside the coroutine passed to `create_task` -- which does not begin
    running until the handler has returned. Both requests therefore passed the
    check, and two `RolloutWriter`s opened on the same directory.

    Asserted through the app rather than on the set, because the bug is *when*
    the set is written relative to the response, and only a request can see
    that ordering.
    """
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")

    first = console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    second = console.post(f"/api/threads/{thread_id}/messages", json={"text": "two"})

    assert first.status_code == 202
    assert second.status_code == 409
    assert "still answering" in second.json()["detail"]


def test_F19_01_a_rejected_second_message_does_not_release_the_first(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """The 409 path must not run the `finally` that clears the claim."""
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "two"})
    # Still claimed by the first, which is what makes the third a 409 too.
    third = console.post(f"/api/threads/{thread_id}/messages", json={"text": "three"})
    assert third.status_code == 409


def test_F19_01_settings_cannot_change_underneath_a_running_turn(
    console: Any, workspace: Path, hanging_turn: None
) -> None:
    """`run_turn` reads the settings once, at the top. Editing them mid-turn
    would silently apply to the *next* one while the UI showed the new value as
    though it were in force now."""
    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    console.post(f"/api/threads/{thread_id}/messages", json={"text": "one"})
    response = console.patch(
        f"/api/threads/{thread_id}", json={"settings": {"sandbox_mode": "full-access"}}
    )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# F19-02  an approval future is resolved on the loop that created it
# ---------------------------------------------------------------------------


async def test_F19_02_the_broker_resolves_a_future_from_another_thread() -> None:
    """`asyncio.Future` is not thread-safe, and a `def` FastAPI route runs in a
    worker thread. Calling `set_result` from there is a data race that the loop
    usually wins -- so the failure is rare, unreproducible, and looks like a
    turn that hung until the 600-second approval timeout.

    The route is `async def` now, and the broker goes through
    `call_soon_threadsafe` regardless, which is what this pins: resolving from
    a thread that is not the loop's still delivers exactly one result.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")

    reply = ApprovalReply(True, "ls")
    done = await asyncio.get_running_loop().run_in_executor(
        None, lambda: broker.resolve("abc", reply)
    )
    assert done is True
    assert await asyncio.wait_for(future, 2.0) == reply


async def test_F19_02_a_second_answer_to_the_same_approval_is_refused() -> None:
    """Two clicks, or a click and a retry, must not resolve a future twice --
    `set_result` on a done future raises `InvalidStateError` inside whatever
    was unlucky enough to call it."""
    broker = ApprovalBroker()
    broker.register("abc")
    assert broker.resolve("abc", ApprovalReply(True, "ls")) is True
    assert broker.resolve("abc", ApprovalReply(False, "ls")) is False


async def test_F19_02_an_approval_that_is_never_answered_times_out_as_a_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing closed, and bounded. An approval nobody answers must not hold
    the turn open for ever (F02-01, one layer up)."""
    from minicodex.web import approver as approver_module

    monkeypatch.setattr(approver_module, "APPROVAL_TIMEOUT_SECONDS", 0.05)
    events: list[dict[str, Any]] = []
    web_approver = WebApprover(ApprovalBroker(), events.append)

    reply = await web_approver.ask(
        ApprovalRequest(what="rm -rf /", reason="because", risk=Risk.WRITE, suggested_rule=None)
    )
    assert reply.approved is False
    assert [e["type"] for e in events] == ["approval_request", "approval_timeout"]


async def test_F19_02_a_reply_that_lost_the_race_with_the_timeout_is_refused() -> None:
    """The half of `resolve`'s guard that `pop` was hiding.

    `test_..._a_second_answer_is_refused` above looks like it covers
    `if future is None or future.done()`. It does not: `resolve` *pops*, so the
    second call never finds a future at all and returns on the first half of
    that `or`. Deleting `or future.done()` left it green -- caught by
    `probe_mutations_ch19.py`, not by reading it.

    The case the second half is actually for is narrower and real. When
    `asyncio.wait_for` gives up it **cancels** the future, and `ask` only then
    calls `discard`. A reply that arrives in that window finds a future which is
    still in the table and already done, and `set_result` on it raises
    `InvalidStateError` -- inside the HTTP route, for a click the person was
    entitled to make.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")
    future.cancel()  # exactly what `wait_for` does when the approval times out

    assert broker.resolve("abc", ApprovalReply(True, "ls")) is False


async def test_F19_02_a_reply_from_a_worker_thread_goes_through_the_loop() -> None:
    """The thread hop this module's docstring promises, asserted as a mechanism.

    The assertion is on *how* the future was resolved rather than on the reply,
    and that is deliberate rather than lazy. `future.set_result` called from an
    anyio worker thread is a data race, and a data race is precisely the thing
    that does not fail when you test it: it returns the right answer here every
    time, and loses in production once. So there is nothing to observe in the
    outcome -- the only honest evidence is that the call was handed to the loop
    instead of made on the spot.

    `future.set_result in scheduled` rather than `scheduled` being non-empty:
    `asyncio.to_thread` uses `call_soon_threadsafe` for its own bookkeeping, so
    a spy that only counts calls stays green with the hop deleted.
    """
    broker = ApprovalBroker()
    future = broker.register("abc")
    loop = asyncio.get_running_loop()

    scheduled: list[Any] = []
    original = loop.call_soon_threadsafe

    def spy(callback: Any, *args: Any) -> Any:
        scheduled.append(callback)
        return original(callback, *args)

    loop.call_soon_threadsafe = spy  # type: ignore[method-assign]
    try:
        assert await asyncio.to_thread(broker.resolve, "abc", ApprovalReply(True, "ls")) is True
        assert (await future).approved is True
    finally:
        loop.call_soon_threadsafe = original  # type: ignore[method-assign]

    assert future.set_result in scheduled, (
        "a reply answered on a worker thread must reach the future through "
        "loop.call_soon_threadsafe, not by touching it directly"
    )


# ---------------------------------------------------------------------------
# F19-03  events reach a socket in the order they happened
# ---------------------------------------------------------------------------


async def test_F19_03_events_arrive_in_the_order_they_were_emitted() -> None:
    """The fault this whole module exists for.

    `for socket in sockets: create_task(socket.send_text(payload))` schedules
    one independent coroutine per event per socket. Each suspends on its first
    await, and the loop resumes them in whatever order it likes -- so a
    `history_item` can arrive after the `turn_complete` that came after it.
    Nothing raises. The transcript is simply wrong, sometimes.

    A hundred events rather than three: with three, an out-of-order run is
    likely enough to pass by luck often enough to look green.
    """
    channel = Channel()
    socket = Recorder()
    channel.subscribe("t1", socket)

    for i in range(100):
        channel.emit("t1", {"type": "history_item", "n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in socket.sent] == list(range(100))


async def test_F19_03_order_holds_when_the_early_events_are_the_slow_ones() -> None:
    """The same fault, with the luck taken out.

    The test above passes with the fan-out put back, because every send costs
    the same and the loop resumes same-cost tasks in creation order. This one
    makes the first event the slowest, which is the case a fan-out gets wrong
    and an awaited pump cannot: it does not schedule the second send until the
    first has returned.
    """
    count = 8
    channel = Channel()
    socket = Uneven(count)
    channel.subscribe("t1", socket)

    for i in range(count):
        channel.emit("t1", {"type": "history_item", "n": i})
    await asyncio.sleep(0.2)

    assert [e["n"] for e in socket.sent] == list(range(count))


async def test_F19_03_every_subscriber_sees_the_same_order() -> None:
    channel = Channel()
    one, two = Recorder(), Recorder()
    channel.subscribe("t1", one)
    channel.subscribe("t1", two)

    for i in range(50):
        channel.emit("t1", {"n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in one.sent] == [e["n"] for e in two.sent] == list(range(50))


async def test_F19_03_a_socket_that_raises_is_dropped_without_taking_the_pump_down() -> None:
    """One dead browser tab must not stop the other tab's transcript."""

    class Broken:
        async def send_text(self, data: str) -> None:
            raise ConnectionResetError("gone")

    channel = Channel()
    good = Recorder()
    channel.subscribe("t1", Broken())
    channel.subscribe("t1", good)

    for i in range(5):
        channel.emit("t1", {"n": i})
    await asyncio.sleep(0.05)

    assert [e["n"] for e in good.sent] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# F19-04  the subscriber table does not grow for ever
# ---------------------------------------------------------------------------


async def test_F19_04_the_last_unsubscribe_drops_the_thread_entirely() -> None:
    """`sockets.discard(ws)` leaves the empty set behind, one per thread ever
    opened, for the life of the process. Invisible on a laptop; a slow leak on
    anything left running."""
    channel = Channel()
    socket = Recorder()
    channel.subscribe("t1", socket)
    assert channel.subscriber_count("t1") == 1

    channel.unsubscribe("t1", socket)
    await asyncio.sleep(0.05)

    assert channel.subscriber_count("t1") == 0
    assert channel._sockets == {}
    assert channel._queues == {}
    assert channel._pumps == {}


async def test_F19_04_emitting_to_a_thread_nobody_watches_is_not_an_error() -> None:
    """The turn keeps running when the tab closes; the browser rebuilds what it
    missed from the rollout file on reconnect."""
    channel = Channel()
    channel.emit("nobody", {"type": "notice", "text": "still working"})
    await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# F19-05  a reconnect has something to reconnect *to*
# ---------------------------------------------------------------------------


def test_F19_05_the_full_transcript_is_rebuildable_from_the_rollout_file(
    console: TestClient, workspace: Path
) -> None:
    """The frontend's reconnect is only survivable because the socket is not
    the only copy: `GET /api/threads/{id}` reads the same rollout file the
    events were derived from. This pins the endpoint that makes it possible.
    """
    thread_id = _thread(console, workspace)
    body = console.get(f"/api/threads/{thread_id}").json()
    assert "items" in body and "busy" in body and "settings" in body


def test_F19_05_the_frontend_reconnects_rather_than_only_listening() -> None:
    """The old console set `onmessage` and nothing else, so any dropped
    connection left the page reading `thinking…` for ever with no recovery but
    F5 -- and said nothing at all while the turn kept running server-side.

    A source assertion, which is unusual here and deliberate: the browser
    behaviour itself is not reachable from pytest, but "there is a close
    handler at all" is exactly the thing that was missing, and it is cheap to
    keep true.
    """
    source = (FRONTEND / "src" / "useThread.ts").read_text(encoding="utf-8")
    assert "socket.onclose" in source, "no close handler: a dropped socket is permanent"
    assert "onerror" in source
    assert "BACKOFF" in source, "reconnecting in a tight loop is its own fault"
    assert "resync" in source, "reconnecting without resyncing loses every event in the gap"


# ---------------------------------------------------------------------------
# F19-06  the layer checker can see a subpackage at all
# ---------------------------------------------------------------------------


def _load_checker() -> Any:
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "scripts" / "check_layers.py"
    spec = importlib.util.spec_from_file_location("check_layers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_layers"] = module
    spec.loader.exec_module(module)
    return module


def test_F19_06_the_checker_walks_subpackages_not_just_the_top_level() -> None:
    """`pkg.glob("*.py")` was correct for eighteen chapters because the package
    was flat. Chapter 19 added `web/`, and every rule in `check_layers.py`
    stopped applying to eight new modules -- while still reporting success, in
    the lint step, on every pull request.
    """
    checker = _load_checker()
    names = {name for name, _package, _path in checker.iter_modules(SRC)}
    assert "minicodex.web" in names
    assert "minicodex.web.runtime" in names
    assert "minicodex.web.channel" in names
    assert "minicodex.agent" in names, "the flat modules are still covered"


def test_F19_06_the_import_graph_includes_the_subpackage() -> None:
    checker = _load_checker()
    graph = checker.build_graph(SRC)
    assert "minicodex.web.routes" in graph
    assert graph["minicodex.web.routes"], "a module with no edges is a module nothing read"


# ---------------------------------------------------------------------------
# F19-07  ...including the relative imports a subpackage is written with
# ---------------------------------------------------------------------------


def test_F19_07_relative_imports_are_edges_too(tmp_path: Path) -> None:
    """`node.level == 0` skipped every `from .foo import x`.

    Fixing the glob alone was not enough and the second half was easy to miss:
    the subpackage became visible, and the checker still saw eight modules with
    no edges between them, because subpackages are written in relative imports.
    Two holes, one symptom -- the code was there and the rules were not looking.
    """
    checker = _load_checker()
    module = tmp_path / "thing.py"
    module.write_text(
        "from .approver import ApprovalBroker\nfrom . import events\nfrom ..agent import Wiring\n",
        encoding="utf-8",
    )
    found = checker.intra_package_imports(module, "minicodex.web")
    assert "minicodex.web.approver" in found
    assert "minicodex.web.events" in found
    assert "minicodex.agent" in found


def test_F19_07_the_console_modules_really_do_use_relative_imports() -> None:
    """Guards the test above from becoming vacuous: if the console were later
    rewritten in absolute imports, the relative-import path would stop being
    exercised by anything real and could rot unnoticed."""
    found = False
    for path in (SRC / "web").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = found or any(
            isinstance(node, ast.ImportFrom) and node.level for node in ast.walk(tree)
        )
    assert found


# ---------------------------------------------------------------------------
# F19-08  nothing below the console imports the console
# ---------------------------------------------------------------------------


def test_F19_08_no_module_below_the_console_imports_it() -> None:
    """The console is an entry point, beside `__main__`, not a layer.

    It was written the other way round first: `web/store.py` reached up to
    `__main__.PROVIDERS` for two default model names, which is the cycle
    `__main__ -> web -> web.app -> web.routes -> web.store -> __main__`. It
    imported fine, because Python tolerates a cycle that resolves at call time,
    and `scripts/check_layers.py` only found it once it could see the
    subpackage at all (F19-06).
    """
    checker = _load_checker()
    assert checker.check_forbidden(SRC) == []
    assert checker.check_cycles(SRC) == []


def test_F19_08_the_seeded_providers_agree_with_the_cli() -> None:
    """The price of breaking that cycle: two places name the same two models.

    Chapter 3's repair for exactly this shape -- when two places must agree and
    neither may import the other, the cheapest way to make them agree is a test
    that compares them.
    """
    from minicodex.__main__ import PROVIDERS

    seeded = {p["provider"]: (p["base_url"], p["model"]) for p in SEED_PROVIDERS}
    assert seeded == {kind: (url, model) for kind, (url, model) in PROVIDERS.items()}


# ---------------------------------------------------------------------------
# F19-09  the three marks reach the browser
# ---------------------------------------------------------------------------


async def test_F19_09_marks_are_emitted_as_their_own_event(tmp_path: Path) -> None:
    """`compacted`, `budget_exhausted` and `interrupted` are the only signal
    that the conversation on screen is no longer the conversation being sent.
    The backend emitted all three and the frontend dropped the whole event
    type, so a run could compact half its history away in silence.
    """
    from minicodex.rollout import SessionMeta, new_session_id
    from minicodex.web.events import StreamingRolloutWriter

    events: list[dict[str, Any]] = []
    meta = SessionMeta(
        session_id=new_session_id(),
        created=0.0,
        cwd=str(tmp_path),
        provider="ollama",
        model="m",
        sandbox_mode="read-only",
        approval_policy="on-request",
    )
    writer = StreamingRolloutWriter(tmp_path / "s.jsonl", meta, events.append)
    try:
        writer.mark("compacted", generation=1, replaced=12)
    finally:
        writer.release()

    assert events == [
        {"type": "mark", "mark": "compacted", "payload": {"generation": 1, "replaced": 12}}
    ]


def test_F19_09_the_frontend_renders_every_mark_the_agent_writes() -> None:
    """The three marks `agent.py` can write, each with something to say.

    A frontend assertion for the same reason as F19-05's: the drop was a
    missing branch, and a missing branch is exactly what a source check can
    still see.
    """
    source = (FRONTEND / "src" / "components" / "Chat.tsx").read_text(encoding="utf-8")
    agent = (SRC / "agent.py").read_text(encoding="utf-8")
    for mark in ("compacted", "budget_exhausted", "interrupted"):
        assert f'"{mark}"' in agent, f"agent.py no longer writes {mark}; update this test"
        assert mark in source, f"the console drops the {mark} mark"


# ---------------------------------------------------------------------------
# F19-10  a workspace path is validated where it enters the program
# ---------------------------------------------------------------------------


def test_F19_10_a_relative_workspace_is_refused(console: TestClient) -> None:
    response = console.post("/api/threads", json={"workspace": "src/minicodex"})
    assert response.status_code == 400
    assert "absolute" in response.json()["detail"]


def test_F19_10_a_workspace_that_does_not_exist_is_refused(
    console: TestClient, tmp_path: Path
) -> None:
    """`run_turn` used to `mkdir(parents=True)` whatever it was handed, so one
    typo created a directory tree anywhere the server user could write and then
    started an agent inside it."""
    target = tmp_path / "typpo" / "project"
    response = console.post("/api/threads", json={"workspace": str(target)})
    assert response.status_code == 400
    assert not target.exists(), "a rejected path must not be created on the way to being rejected"
    # The *message*, not just the status. Deleting the `exists()` check leaves
    # the `is_dir()` one below it, which also answers 400 for a path that is
    # not there -- so a test that only reads the status cannot tell the two
    # apart, and mutation testing reported the check as dead when it is not.
    # Asserting the sentence makes the two branches distinguishable.
    assert "no such directory" in response.json()["detail"]


def test_F19_10_a_file_is_not_a_workspace(console: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("hello", encoding="utf-8")
    response = console.post("/api/threads", json={"workspace": str(target)})
    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_F19_10_an_invented_sandbox_mode_is_refused(console: TestClient, workspace: Path) -> None:
    """`Session.mode` is a `Literal` nothing checks at runtime, and an unknown
    mode does not fail -- it falls through every branch in `judge_command` and
    lands on whichever default that branch has. Not "denied": unpredictable.
    """
    response = console.post(
        "/api/threads",
        json={"workspace": str(workspace), "settings": {"sandbox_mode": "yolo"}},
    )
    assert response.status_code == 400

    with pytest.raises(ValueError):
        validate_settings({"approval_policy": "sometimes"})


def test_F19_10_the_ui_offers_exactly_what_the_core_implements(console: TestClient) -> None:
    """The list in the browser comes from `policy.py`, so a mode can never be
    offered that the core does not have, nor a new one hidden."""
    body = console.get("/api/policy").json()
    assert body["sandbox_modes"] == list(SANDBOX_MODES)
    assert body["approval_policies"] == list(APPROVAL_POLICIES)


# ---------------------------------------------------------------------------
# F19-11  a default is not a consent
# ---------------------------------------------------------------------------


def test_F19_11_memory_and_skills_are_off_until_asked_for() -> None:
    """F17-11, in a browser.

    Switching memory on changes what every later turn sends; switching writing
    on starts a second model call beside every turn, on the same quota, reading
    transcripts nobody has re-read. A switch that defaults to on has not been
    consented to -- it has been assumed.
    """
    assert DEFAULT_THREAD_SETTINGS["memory"] is False
    assert DEFAULT_THREAD_SETTINGS["remember"] is False
    assert DEFAULT_THREAD_SETTINGS["skills"] is False
    assert DEFAULT_THREAD_SETTINGS["sandbox_mode"] == "read-only"


def test_F19_11_reading_and_writing_memory_are_separate_switches(
    console: TestClient, workspace: Path
) -> None:
    """`--remember` without `--memory` is a real configuration and the console
    has to be able to express it, or the two consents have been merged into
    one behind the user's back."""
    thread_id = _thread(console, workspace, remember=True)
    settings = console.get(f"/api/threads/{thread_id}").json()["settings"]
    assert settings["remember"] is True
    assert settings["memory"] is False


def test_F19_11_writing_memory_is_confirmed_a_second_time() -> None:
    """codex's TUI puts `Enable memories?` behind a confirmation, with `Reset
    all memories?` next to it. Both, here, for the same reason."""
    source = (FRONTEND / "src" / "components" / "ThreadSettings.tsx").read_text(encoding="utf-8")
    assert "confirmingRemember" in source, "writing memory is switched on with one click"


def test_F19_11_everything_it_learned_can_be_erased_from_where_it_was_enabled(
    console: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """The path in this test was wrong for four chapters, and it was green.

    It used to write `<workspace>/.minicodex/memories/MEMORY.md` and assert
    that "forget everything" removed it, which passed because the route deleted
    that same wrong directory. No turn has ever read it: memory moved to the
    home directory in chapter 16, and `runtime.load_feature_dirs` has loaded it
    from there since this console was built. So the requirement this test names
    was never actually under test, and the bug it should have caught is F23-05.

    The requirement is unchanged. Only the directory is corrected, to the one
    this owner's turns actually read.
    """
    memories = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home").memories()
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("v1\n\n## thing\nbody\n", encoding="utf-8")
    thread_id = _thread(console, workspace, memory=True)

    response = console.post(f"/api/threads/{thread_id}/memory/forget-all")
    assert response.status_code == 200
    assert "MEMORY.md" in response.json()["removed"]
    assert not (memories / "MEMORY.md").exists()


# ---------------------------------------------------------------------------
# The console is a *user* of the agent, not a fork of it
# ---------------------------------------------------------------------------


def test_the_web_runtime_builds_the_same_system_prompt_as_the_cli() -> None:
    """`web/runtime._instructions` is a copy of `__main__._instructions`,
    because importing the original would be the F19-08 edge again. A copy that
    nothing compares is a copy that drifts -- chapter 3's tool descriptions,
    one more time.
    """
    from minicodex.__main__ import _instructions as cli_instructions
    from minicodex.composition import local_tools
    from minicodex.memory import Memory
    from minicodex.plan import TaskPlan, plan_toolset
    from minicodex.skills import Skill, Skills
    from minicodex.web.runtime import _instructions as web_instructions

    # Every flag, not just the defaults. The first version of this test called
    # both functions with no tools and no features, so the four conditional
    # paragraphs -- plan, memory, skills, note -- were compared to nothing and
    # either copy could have lost one without a word.
    #
    # `Memory`/`Skills` values rather than the bools this used to pass: after
    # chapter 16's rewrite the memory paragraph names the directory it
    # describes, so a bool no longer carries enough to build one (F16-11).
    memories = (None, Memory(directory=Path("memories"), summary="a summary"))
    skill_sets = (
        None,
        Skills(
            directory=Path("skills"),
            skills=(Skill(name="one", description="a skill", body="body", path=Path("one")),),
        ),
    )
    for mode in SANDBOX_MODES:
        for policy in APPROVAL_POLICIES:
            for memory in memories:
                for skills in skill_sets:
                    session = Session(mode=mode, policy=policy)
                    tools = local_tools(Path.cwd(), session).plus(plan_toolset(TaskPlan()))
                    assert web_instructions(session, tools, memory, skills) == cli_instructions(
                        session, tools, memory, skills
                    )


async def test_the_console_reports_plan_and_permission_changes_without_a_core_hook() -> None:
    """The plan and the sandbox mode are not history items, so nothing writes
    them to the rollout -- but the browser has to see both: a plan that only
    appears when the turn ends is not a plan you can follow, and a mid-turn
    `request_permissions` escalation that shows up nowhere is the console
    lying about what the agent may currently do.

    Derived by comparing against the last value after each item is durable,
    rather than by a callback added to `agent.py`. That is the whole reason
    `probe_web.py core-diff` can report zero.
    """
    from minicodex.plan import PlanStep, TaskPlan
    from minicodex.web.runtime import _derived

    events: list[dict[str, Any]] = []
    plan, session = TaskPlan(), Session(mode="read-only", policy="on-request")
    check = _derived(plan, session, events.append)

    check()
    assert events == [], "nothing changed, so nothing is said"

    plan.steps = (PlanStep("write it", "in_progress"),)
    check()
    assert events[-1]["type"] == "plan_updated"
    assert events[-1]["steps"] == [{"text": "write it", "status": "in_progress"}]

    session.mode = "workspace-write"  # what `request_upgrade` does, in place
    check()
    assert events[-1] == {
        "type": "session_mode_changed",
        "from": "read-only",
        "mode": "workspace-write",
        "policy": "on-request",
    }

    check()
    assert len(events) == 2, "an unchanged plan must not re-announce itself every item"


def test_the_console_never_constructs_its_own_agent() -> None:
    """Interlude B's rule, which the console is the first new caller to test:
    a second construction site is how a child ended up without a recorder,
    without compaction and without the scheduler, silently, for a chapter."""
    checker = _load_checker()
    assert checker.check_one_agent_construction(SRC) == []


def test_the_store_survives_a_corrupt_file(tmp_path: Path) -> None:
    """A settings file that comes back unreadable must degrade to the defaults,
    not take the server down on start-up."""
    store = Store(tmp_path)
    store.add("providers.json", {"name": "x"})
    (tmp_path / "providers.json").write_text("{ not json", encoding="utf-8")
    assert store.providers() == []


def test_the_store_writes_atomically(tmp_path: Path) -> None:
    """No half-written file, and no `.tmp` left behind to be read as one."""
    store = Store(tmp_path)
    store.add("threads.json", {"workspace": "/tmp", "title": "t"})
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "threads.json").read_text(encoding="utf-8"))


def test_an_api_key_is_never_sent_back_to_the_browser(
    console: TestClient,
) -> None:
    """It has to be stored -- there is nowhere else for a typed key to live --
    but a GET that returns it puts it in every browser cache and devtools log
    that ever renders the settings page."""
    console.post(
        "/api/providers",
        json={
            "name": "secret",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model": "m",
            "api_key": "sk-do-not-echo-this",
        },
    )
    body = console.get("/api/providers").text
    assert "sk-do-not-echo-this" not in body
    assert '"api_key":true' in body.replace(" ", "")


def test_the_console_reads_every_mcp_field_the_form_can_set(
    console: TestClient,
) -> None:
    """`env`, `cwd` and the two timeouts were read by `run_turn` from the first
    version and settable by nobody, so every server ran on the defaults for
    ever."""
    created = console.post(
        "/api/mcp",
        json={
            "name": "notes",
            "command": "python -m server --flag",
            "env": {"MCP_NOTES_DB": "/tmp/notes.json"},
            "cwd": "/tmp",
            "startup_timeout": 5.0,
            "tool_timeout": 7.0,
        },
    ).json()
    assert created["command"] == ["python", "-m", "server", "--flag"]
    assert created["env"] == {"MCP_NOTES_DB": "/tmp/notes.json"}
    assert created["cwd"] == "/tmp"
    assert created["startup_timeout"] == 5.0
    assert created["tool_timeout"] == 7.0


def test_the_approval_card_can_reach_both_remember_scopes(console: TestClient) -> None:
    """The backend accepted `project` from the first version and the card only
    ever offered `session`, so the durable half of chapter 5's rule store was
    unreachable from a browser."""
    source = (FRONTEND / "src" / "components" / "Chat.tsx").read_text(encoding="utf-8")
    assert '"project"' in source and '"session"' in source

    rejected = console.post(
        "/api/approvals/nope/respond",
        json={"approved": True, "command": "ls", "remember": "forever"},
    )
    assert rejected.status_code == 400


def test_the_plugins_tab_says_it_is_not_real(console: TestClient) -> None:
    """An empty list would imply the tab had looked and found nothing. There is
    no plugin module in this package to look with."""
    assert console.get("/api/plugins").json() == {"supported": False, "items": []}


def test_the_skills_tab_is_real_now(console: TestClient, workspace: Path) -> None:
    """It used to say the subsystem did not exist. `skills.py` has existed
    since chapter 18, and copy that is wrong about its own repository is worse
    than a missing tab."""
    skills = workspace / ".minicodex" / "skills" / "run-tests"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: run-tests\ndescription: how this project runs its tests\n---\n\n"
        "uv run pytest\n",
        encoding="utf-8",
    )
    thread_id = _thread(console, workspace, skills=True)

    body = console.get(f"/api/threads/{thread_id}/skills").json()
    assert body["supported"] is True
    assert [s["name"] for s in body["items"]] == ["run-tests"]
    assert "uv run pytest" in body["items"][0]["body"]
