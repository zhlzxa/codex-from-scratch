"""Regression coverage for console tenant boundaries and OAuth rendering."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from console_login import sign_in
from minicodex.agent import Agent, RunResult
from minicodex.history import UserMessage
from minicodex.memory_write import WriteReport
from minicodex.rollout import RolloutWriter, SessionMeta
from minicodex.tenancy import Owner
from minicodex.web import runtime
from minicodex.web.app import create_app
from minicodex.web.approver import ApprovalBroker
from minicodex.web.store import DEFAULT_THREAD_SETTINGS


def write_session(path: Path, text: str) -> None:
    meta = SessionMeta(
        session_id=path.stem,
        created=time.time(),
        cwd=str(path.parent),
        provider="openai",
        model="stub",
        sandbox_mode="read-only",
        approval_policy="never",
    )
    with RolloutWriter(path, meta) as writer:
        writer.extend([UserMessage(text)])


@pytest.fixture
def console_client(tmp_path):
    app = create_app(tmp_path / "console", home=tmp_path / "home")
    with TestClient(app) as client:
        sign_in(client)
        yield client


@pytest.mark.parametrize("reference", ["last", "own-session"])
def test_fork_resolves_owned_references(console_client, tmp_path, reference):
    client = console_client
    thread = client.post("/api/threads", json={"workspace": str(tmp_path)}).json()
    owner = Owner("tester", tmp_path / "home")
    store = client.app.state.console.store_for(owner)
    source = store.thread_dir(thread["id"]) / "own-session.jsonl"
    write_session(source, "owned history")
    before = source.read_bytes()
    response = client.post(f"/api/threads/{thread['id']}/fork", json={"session": reference})
    assert response.status_code == 200, response.text
    copied = client.get(f"/api/threads/{response.json()['id']}").json()
    assert copied["items"] == [{"type": "user", "text": "owned history"}]
    assert source.read_bytes() == before
    assert len(list(source.parent.glob("*.jsonl"))) == 1


@pytest.mark.parametrize("reference_kind", ["absolute", "traversal", "foreign-id", "symlink"])
def test_fork_rejects_foreign_sessions(console_client, tmp_path, reference_kind):
    client = console_client
    thread = client.post("/api/threads", json={"workspace": str(tmp_path)}).json()
    console = client.app.state.console
    store = console.store_for(Owner("tester", tmp_path / "home"))
    foreign_store = console.store_for(Owner("other", tmp_path / "home"))
    foreign_thread = foreign_store.new_thread(str(tmp_path), "foreign", DEFAULT_THREAD_SETTINGS)
    foreign = foreign_store.thread_dir(foreign_thread["id"]) / "foreign-session.jsonl"
    write_session(foreign, "private foreign history")
    reference = {
        "absolute": str(foreign),
        "traversal": "../foreign-session",
        "foreign-id": "foreign-session",
        "symlink": "linked-session",
    }[reference_kind]
    if reference_kind == "symlink":
        try:
            (store.thread_dir(thread["id"]) / "linked-session.jsonl").symlink_to(foreign)
        except OSError:
            pytest.skip("symlink creation is unavailable")
    before = store.all("threads.json")
    response = client.post(f"/api/threads/{thread['id']}/fork", json={"session": reference})
    assert response.status_code == 400
    assert store.all("threads.json") == before
    assert foreign.exists()


def test_oauth_callback_escapes_provider_controlled_text(console_client, tmp_path):
    client = console_client
    payload = "<script>window.reviewProbe=1</script>"
    client.app.state.console.mcp_oauth.put(
        SimpleNamespace(
            state="valid-state",
            created_at=time.time(),
            owner=Owner("tester", tmp_path / "home"),
            server_name=payload,
        )
    )
    response = client.get(
        "/api/mcp/oauth/callback",
        params={
            "state": "valid-state",
            "error": "denied",
            "error_description": payload,
        },
    )
    assert response.status_code == 200
    assert payload not in response.text
    assert response.text.count("&lt;script&gt;") == 2


@pytest.mark.parametrize("mode", ["read-only", "workspace-write", "full-access"])
async def test_runtime_keeps_mcp_and_memory_bound_to_owner(tmp_path, monkeypatch, mode):
    connections = []
    pipelines = []

    async def connect(configs, registry, **kwargs):
        connections.append(kwargs)
        return []

    async def pipeline(model, **kwargs):
        pipelines.append(kwargs)
        return WriteReport()

    async def answer(self, question):
        return RunResult(final_text="done", stop_reason="completed", turns_used=1)

    monkeypatch.setattr(runtime, "connect", connect)
    monkeypatch.setattr(runtime, "run_pipeline", pipeline)
    monkeypatch.setattr(Agent, "run", answer)
    for key in ("alice", "bob"):
        owner = Owner(key, tmp_path / "home")
        await runtime.run_turn(
            thread_dir=tmp_path / key / "thread",
            workspace_root=tmp_path,
            question="probe",
            settings={**DEFAULT_THREAD_SETTINGS, "sandbox_mode": mode, "remember": True},
            provider_name="openai",
            base_url="http://unused",
            model="stub",
            api_key=None,
            mcp_servers=[{"name": "stdio", "command": ["unused"]}],
            broker=ApprovalBroker(),
            emit=lambda event: None,
            owner=owner,
        )
        connection = connections[-1]
        assert connection["owner"] == owner
        if mode == "full-access":
            assert connection["sandbox"] is None
        else:
            assert connection["sandbox"].spec.mode == mode
        assert pipelines[-1]["directory"] == owner.memories()
        assert pipelines[-1]["jobs_path"] == owner.jobs_db()
        assert pipelines[-1]["lock_path"] == owner.merge_lock()
    assert pipelines[0]["jobs_path"] != pipelines[1]["jobs_path"]
