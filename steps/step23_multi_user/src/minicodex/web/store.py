"""Local, file-backed state for the console: providers, MCP servers, threads.

Plain JSON files under one data directory, one process, one lock. This is a
local dev tool driving one agent from one browser, not a multi-user service --
`steps/step07_resume` and `steps/step08_concurrency` already show what a real
multi-writer store needs (an append-only file, an `O_EXCL` lock, single-writer
discipline), and reaching for that here would be solving a problem this
deployment does not have.

What it *does* borrow from those chapters is the durability discipline, because
that part is not about concurrency: `_save` writes a temporary file, fsyncs it,
renames it over the target and then fsyncs the directory. The rename is atomic
on both platforms this book runs on; without the fsyncs it is atomic with
respect to *other processes* and not with respect to power loss, which is the
distinction chapter 7 spends a section on (F07-03). A settings file that comes
back empty after a crash is a console that silently reverts to `read-only`,
and a permission that quietly reverts is worse than one that was never set.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES

#: What the console offers on a fresh install, one entry per provider the CLI
#: knows about.
#:
#: Not imported from `__main__.PROVIDERS`, and that is not a style preference:
#: it was written that way first and `scripts/check_layers.py` reported an
#: import cycle, `__main__ -> web -> web.app -> web.routes -> web.store ->
#: __main__`. `__main__` is the layer *above* this one, and a lower layer
#: reaching up for a constant is how the cycle chapter 1 opened with (F01-08)
#: gets rebuilt with different names.
#:
#: So the base URLs come from `model.py`, which is where provider facts already
#: live, and the two model names are declared here -- with a test asserting
#: this table and `__main__.PROVIDERS` still agree. Chapter 3 made the same
#: repair for the tool descriptions: when two places must say the same thing
#: and neither can import the other, the cheapest way to make them agree is a
#: test that compares them.
SEED_PROVIDERS: tuple[dict[str, Any], ...] = (
    {
        "name": "Ollama (local)",
        "provider": "ollama",
        "base_url": OLLAMA_BASE_URL,
        "model": "gemma4:31b-cloud",
    },
    {
        "name": "OpenAI",
        "provider": "openai",
        "base_url": OPENAI_BASE_URL,
        "model": "gpt-4o-mini",
    },
)

#: Where `minicodex serve` keeps everything it owns, under the working
#: directory's `.minicodex/` like every other file this program writes.
DEFAULT_DATA_DIR = Path(".minicodex") / "console"

#: Per-thread defaults. `read-only` and `on-request` are the CLI's defaults
#: (`__main__._add_permission_flags`) and they are the console's too: a browser
#: is not a reason to start with more permission than a terminal does.
#: Memory and skills default **off** for the reason chapter 17 gives in F17-11
#: -- switching either on changes what every later turn sends, and a default-on
#: switch is not a consent. The console asks twice before memory writing, which
#: is the same shape codex's TUI uses for `Enable memories?`.
DEFAULT_THREAD_SETTINGS: dict[str, Any] = {
    "sandbox_mode": "read-only",
    "approval_policy": "on-request",
    "context_window": None,
    "memory": False,
    "remember": False,
    "skills": False,
}


#: The files a pre-chapter-23 console wrote straight into its data directory.
#: `sessions/` is a directory rather than a file and is moved with them.
LEGACY_RECORDS = ("threads.json", "providers.json", "mcp.json", "sessions")


def adopt_legacy_records(data_dir: Path, owner_key: str) -> list[str]:
    """Move a single-tenant console's records under the first account.

    Four chapters of this console wrote `threads.json` and its friends into the
    top of the data directory, because there was one of everybody. Chapter 23
    puts them under `tenants/<key>/`, and doing that without moving the old
    ones means the operator's first sign-in shows an empty console with all
    their sessions still on disk one directory up -- which reads as data loss
    whether or not it is.

    Called exactly once, from the bootstrap route, when the first account is
    created. Not on every start: a console that adopts stray files at the top
    of its data directory whenever it finds them would hand the *next* account
    whatever the previous one left behind.

    A rename, not a copy, and it stops at the first target that already exists
    rather than merging -- there is nothing here that knows how to reconcile
    two `threads.json` files, and pretending otherwise is how one of them is
    lost quietly.
    """
    target = data_dir / "tenants" / owner_key
    moved: list[str] = []
    for name in LEGACY_RECORDS:
        source = data_dir / name
        if not source.exists() or (target / name).exists():
            continue
        target.mkdir(parents=True, exist_ok=True)
        source.rename(target / name)
        moved.append(name)
    return moved


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Reject a sandbox mode or policy the core does not define.

    The frontend renders these from `GET /api/policy`, so a bad value can only
    arrive from a handwritten request -- but `Session.mode` is a `Literal` that
    nothing checks at runtime, and an unknown mode does not fail: it falls
    through every branch in `policy.judge_command` and lands on the default.
    Which default that is depends on the branch, so an invented mode is not
    "denied", it is *unpredictable*. Checked here, where the string enters the
    program, rather than where it is eventually compared.
    """
    merged = {**DEFAULT_THREAD_SETTINGS, **settings}
    if merged["sandbox_mode"] not in SANDBOX_MODES:
        raise ValueError(f"unknown sandbox mode {merged['sandbox_mode']!r}")
    if merged["approval_policy"] not in APPROVAL_POLICIES:
        raise ValueError(f"unknown approval policy {merged['approval_policy']!r}")
    window = merged["context_window"]
    if window is not None:
        window = int(window)
        if window <= 0:
            raise ValueError("context window must be a positive number of tokens")
        merged["context_window"] = window
    for flag in ("memory", "remember", "skills"):
        merged[flag] = bool(merged[flag])
    return merged


class Store:
    """Every JSON file the console owns, under one directory.

    An object rather than module-level globals, because `minicodex serve
    --data-dir` exists and because the tests need a store per `tmp_path`. The
    module-level version of this was written first and could not be tested
    twice in one interpreter without leaking state between the two runs.
    """

    def __init__(self, data_dir: Path = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self._lock = threading.RLock()

    # -- file plumbing -------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.data_dir / name

    def _load(self, name: str, default: Any) -> Any:
        path = self._path(name)
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt file degrades to the default rather than taking the
            # server down on import. Chapter 16's `load` makes the same call
            # for the same reason: the absence of a thing is not an error.
            return default

    def _save(self, name: str, data: Any) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(path)
        # The rename itself is a directory entry, and it is not durable until
        # the directory is. Best-effort: Windows refuses `O_RDONLY` on a
        # directory, and a console that will not start on Windows is a worse
        # fault than a settings file that loses a second of writes there.
        try:
            fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- generic list records ------------------------------------------------

    def all(self, name: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load(name, []))

    def get(self, name: str, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((r for r in self._load(name, []) if r["id"] == item_id), None)

    def add(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            records = list(self._load(name, []))
            record = {"id": _new_id(), **record}
            records.append(record)
            self._save(name, records)
            return record

    def update(self, name: str, item_id: str, **changes: Any) -> dict[str, Any] | None:
        with self._lock:
            records = list(self._load(name, []))
            updated = None
            for record in records:
                if record["id"] == item_id:
                    record.update(changes)
                    updated = record
            # Only write when something actually changed. The first version
            # rewrote the file on every miss, so a 404 was indistinguishable
            # from a successful update in the file's mtime.
            if updated is not None:
                self._save(name, records)
            return updated

    def delete(self, name: str, item_id: str) -> bool:
        with self._lock:
            records = list(self._load(name, []))
            kept = [r for r in records if r["id"] != item_id]
            if len(kept) == len(records):
                return False
            self._save(name, kept)
            return True

    # -- providers -----------------------------------------------------------

    def providers(self) -> list[dict[str, Any]]:
        return self.all("providers.json")

    def active_provider(self) -> dict[str, Any] | None:
        return next((p for p in self.providers() if p.get("active")), None)

    def activate_provider(self, provider_id: str) -> bool:
        with self._lock:
            records = list(self._load("providers.json", []))
            if not any(r["id"] == provider_id for r in records):
                return False
            for record in records:
                record["active"] = record["id"] == provider_id
            self._save("providers.json", records)
            return True

    def seed_providers(self) -> None:
        """Something runnable on first launch, so the console is not empty.

        Both entries are switched **off** until one is chosen: the console
        refuses to send anything without an active provider, which is one
        deliberate click rather than a default that quietly picks a model and a
        billing account for you.

        Neither carries a key. The OpenAI entry reads `OPENAI_API_KEY` at call
        time (`runtime.resolve_api_key`), so a key never reaches this file from
        the environment -- clearing the form in the browser cannot copy it in.
        """
        if self.providers():
            return
        for seed in SEED_PROVIDERS:
            self.add("providers.json", {**seed, "api_key": None, "active": False})

    # -- threads -------------------------------------------------------------

    def thread_dir(self, thread_id: str) -> Path:
        return self.sessions_dir / thread_id

    def new_thread(
        self, workspace: str, title: str, settings: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        record = self.add(
            "threads.json",
            {
                "workspace": workspace,
                "title": title,
                "created": time.time(),
                "settings": validate_settings(settings or {}),
            },
        )
        self.thread_dir(record["id"]).mkdir(parents=True, exist_ok=True)
        return record

    def thread(self, thread_id: str) -> dict[str, Any] | None:
        record = self.get("threads.json", thread_id)
        if record is None:
            return None
        # Older records predate a setting; merging the defaults in on read
        # means a new switch does not have to migrate the file.
        record["settings"] = {**DEFAULT_THREAD_SETTINGS, **record.get("settings", {})}
        return record

    def workspaces(self) -> list[dict[str, Any]]:
        """Group threads by working directory -- the shape the sidebar wants."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in sorted(
            self.all("threads.json"), key=lambda r: r.get("created", 0), reverse=True
        ):
            record["settings"] = {**DEFAULT_THREAD_SETTINGS, **record.get("settings", {})}
            grouped.setdefault(record["workspace"], []).append(record)
        return [{"path": path, "threads": items} for path, items in grouped.items()]
