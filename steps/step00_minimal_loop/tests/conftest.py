from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest

from minicodex import stub_ollama

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
    server = HTTPServer(("127.0.0.1", port), stub_ollama._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
