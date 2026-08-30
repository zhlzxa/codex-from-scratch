"""Signing a test client in, in one place.

Chapter 23 closed the console's last anonymous route, so every console test
written before it now starts with a login. That is three lines, and three lines
copied into two test modules is how they drift -- `tests/mcp_http_stub.py` is
here for the same reason.

The password cost is lowered for the whole session by the `cheap_password_
hashing` fixture in `conftest.py`; see its docstring for why that is safe and
why there is no production knob for it.
"""

from __future__ import annotations

from typing import Any

#: The account every console fixture in this suite signs in as.
CONSOLE_ACCOUNT = "tester"
CONSOLE_PASSWORD = "chapter-23-test-password"


def bootstrap_payload(console: Any, *, key: str = CONSOLE_ACCOUNT) -> dict[str, str]:
    """The body `POST /api/auth/bootstrap` wants, with a freshly issued token.

    Issued from the store rather than parsed out of `serve`'s printed banner:
    the banner is what a person reads, and a test that scraped it would be
    testing the print statement.
    """
    token = console.accounts.issue_bootstrap_token()
    assert token is not None, "this console already has an account"
    return {"token": token, "key": key, "password": CONSOLE_PASSWORD}


def sign_in(client: Any, *, key: str = CONSOLE_ACCOUNT, password: str = CONSOLE_PASSWORD) -> None:
    """Bootstrap the first account if there is none, then sign this client in."""
    console = client.app.state.console
    if console.accounts.empty():
        response = client.post("/api/auth/bootstrap", json=bootstrap_payload(console, key=key))
    else:
        response = client.post("/api/auth/login", json={"key": key, "password": password})
    assert response.status_code == 200, response.text


async def sign_in_async(api: Any, *, key: str = CONSOLE_ACCOUNT) -> None:
    """The same thing for the tests that drive the app over `ASGITransport`.

    Separate rather than shared, because the only common part is the URL: this
    one holds the app already and has to `await`, the one above reaches through
    `TestClient.app` and does not.
    """
    console = api._transport.app.state.console
    response = await api.post("/api/auth/bootstrap", json=bootstrap_payload(console, key=key))
    assert response.status_code == 200, response.text
