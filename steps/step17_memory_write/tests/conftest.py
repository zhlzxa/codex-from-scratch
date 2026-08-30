from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest

from minicodex import stub

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def stub_url() -> Iterator[str]:
    """A local server replaying the recorded Ollama responses.

    Real HTTP over a real socket: the client's SSE parsing, status handling and
    connection teardown are all exercised.  Only the model's judgement is fake.
    """
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), stub._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def served_requests(stub_url: str) -> Iterator[list[dict]]:
    """Every request body the stub has served this test, in order.

    A retry is only observable from the other side: "was the same thing sent
    twice" is not a question a client can answer about itself.  The list is
    module state on the stub, so it is cleared here rather than in each test --
    a counter that survives one test into the next reports the previous test's
    retries as this one's.

    Depends on `stub_url`, which chapter 12's own test module *overrides* with
    a private, threaded server. That is not a detail: a chapter whose tests
    deliberately abandon connections cannot share a single-threaded server with
    everybody else, and the symptom of trying was a suite that failed in a
    different place on every run.
    """
    stub.reset()
    yield stub.REQUESTS
    stub.reset()
