"""LIVE-04: wrong guesses cost time, not just a 401.

Pre-fix, `/api/auth/login` answered every attempt at full rate: F23-06 had
made each failure cost the same milliseconds, but nothing capped how many
failures a source could buy.  The tests here pin the cap and the shape of it
-- pair-scoped, failure-scoped, wiped by success, checked before the hash,
and reachable through the real route.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from minicodex.web import create_app
from minicodex.web.throttle import (
    DEFAULT_MAX_FAILURES,
    LOCKOUT_SECONDS,
    LoginThrottle,
    Throttled,
)

# -- the unit underneath ------------------------------------------------------


def test_LIVE_04_limit_not_tripped_below_the_cap() -> None:
    throttle = LoginThrottle()
    for _ in range(DEFAULT_MAX_FAILURES - 1):
        throttle.record_failure("alice", "10.0.0.1", now=1000.0)
    throttle.check("alice", "10.0.0.1", now=1001.0)  # no raise


def test_LIVE_04_cap_trips_the_lockout() -> None:
    throttle = LoginThrottle()
    for i in range(DEFAULT_MAX_FAILURES):
        throttle.record_failure("alice", "10.0.0.1", now=1000.0 + i)
    with pytest.raises(Throttled) as exc:
        throttle.check("alice", "10.0.0.1", now=1000.0 + DEFAULT_MAX_FAILURES)
    assert exc.value.retry_after >= LOCKOUT_SECONDS - 1


def test_LIVE_04_lockout_is_pair_scoped() -> None:
    """A locked pair does not lock the account for everyone else.

    Per-account lockouts hand an attacker a denial-of-service on a chosen
    user for free: guess at their name until it trips.  The pair means the
    attacker burns only their own source.
    """
    throttle = LoginThrottle()
    for i in range(DEFAULT_MAX_FAILURES):
        throttle.record_failure("alice", "10.0.0.1", now=1000.0 + i)
    throttle.check("alice", "10.0.0.2", now=1001.0)  # different source: fine
    throttle.check("bob", "10.0.0.1", now=1001.0)  # different account: fine


def test_LIVE_04_success_wipes_the_record() -> None:
    throttle = LoginThrottle()
    for i in range(DEFAULT_MAX_FAILURES - 1):
        throttle.record_failure("alice", "10.0.0.1", now=1000.0 + i)
    throttle.record_success("alice", "10.0.0.1")
    for i in range(DEFAULT_MAX_FAILURES - 1):
        throttle.record_failure("alice", "10.0.0.1", now=2000.0 + i)
    throttle.check("alice", "10.0.0.1", now=2005.0)  # back under the cap


def test_LIVE_04_lockout_outlives_window_drain() -> None:
    """Waiting out the window is not waiting out the lockout.

    The attempts age out of the failure window in fifteen minutes; the
    lockout refuses for fifteen minutes *from the trip*.  An attacker pacing
    guesses at one per window-minus-a-second must still stop at the trip.
    """
    throttle = LoginThrottle()
    start = 1_000_000.0
    for i in range(DEFAULT_MAX_FAILURES):
        throttle.record_failure("alice", "10.0.0.1", now=start + i * 60.0)
    with pytest.raises(Throttled):
        throttle.check("alice", "10.0.0.1", now=start + DEFAULT_MAX_FAILURES * 60.0 + 5)


# -- the route ----------------------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> Any:
    return create_app(tmp_path / "console", home=tmp_path / "home")


def test_LIVE_04_repeated_wrong_passwords_hit_429_with_retry_after(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real endpoint, at the real limit, with the real header.

    `AccountStore.verify` is stubbed rather than paid `DEFAULT_MAX_FAILURES`
    times: scrypt is priced per call on purpose, and this test's subject is
    the route's counting, not the hash.
    """
    from minicodex.web import accounts as accounts_mod

    monkeypatch.setattr(accounts_mod.AccountStore, "verify", lambda self, k, p: None)
    with TestClient(app) as client:
        for _ in range(DEFAULT_MAX_FAILURES):
            response = client.post("/api/auth/login", json={"key": "alice", "password": "x"})
            assert response.status_code == 401
        tripped = client.post("/api/auth/login", json={"key": "alice", "password": "x"})
        assert tripped.status_code == 429
        assert int(tripped.headers["Retry-After"]) >= 1


def test_LIVE_04_throttle_check_runs_before_the_hash(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A throttled source never reaches `verify` -- no scrypt cost for it.

    The whole point of hashing expensively is to price the guesses of someone
    allowed to guess; a source that is not allowed must not buy any hashing
    at all.  `verify` raises if it is reached while locked out.
    """

    def _refuse(self: Any, key: str, password: str) -> None:
        raise AssertionError("verify must not run for a throttled source")

    from minicodex.web import accounts as accounts_mod

    monkeypatch.setattr(accounts_mod.AccountStore, "verify", _refuse)
    with TestClient(app) as client:
        console = client.app.state.console
        # Trip the pair the way the route does, at the real current time, so
        # the lockout is live when the request arrives.
        now = time.time()
        for i in range(DEFAULT_MAX_FAILURES):
            console.logins.record_failure("alice", "testclient", now=now + i)
        tripped = client.post("/api/auth/login", json={"key": "alice", "password": "x"})
        assert tripped.status_code == 429


def test_LIVE_04_success_still_signs_in_between_failures(app: Any) -> None:
    """One wrong password never locks a real user out."""
    with TestClient(app) as client:
        token = client.app.state.console.accounts.issue_bootstrap_token()
        first = client.post(
            "/api/auth/bootstrap",
            json={"token": token, "key": "tester", "password": "a-perfectly-fine-password"},
        )
        assert first.status_code == 200, first.text
        wrong = client.post("/api/auth/login", json={"key": "tester", "password": "nope"})
        assert wrong.status_code == 401
        right = client.post(
            "/api/auth/login", json={"key": "tester", "password": "a-perfectly-fine-password"}
        )
        assert right.status_code == 200
