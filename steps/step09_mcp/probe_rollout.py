"""What actually survives when the process does not exit politely.

Four questions this chapter cannot answer by reasoning:

1. If the history is written when `run()` returns, what is on disk when it does
   not return?
2. Does killing a process mid-write really produce half a line, or does the OS
   write whole lines and the worry evaporate?
3. What does a second process appending to the same file do to the first one's
   records?
4. Where does a crashed session actually end -- and is that a place a
   conversation is allowed to end?

Run:  uv run python probe_rollout.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# --- 1: save-at-the-end -----------------------------------------------------

NAIVE = r"""
import json, sys, time
# The obvious first version: keep the history in memory, write it out at the end.
history = [{"role": "user", "content": "add a retry to the client"}]
for turn in range(50):
    history.append({"role": "assistant", "content": f"turn {turn}"})
    time.sleep(0.05)
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(history))
print("saved", flush=True)
"""

# --- 2 and 3: what a kill and a second writer do to a file ------------------

CHILD = r"""
import json, sys
path, mode, tag = sys.argv[1], sys.argv[2], sys.argv[3]
# 400KB per line is not a stress test: chapter 6's F06-09 was a 400KB stack
# trace in one tool output, and a rollout line carries the tool output.
size = 400_000 if mode == "big" else 200
n = 4000 if mode == "count" else 100000
with open(path, "a", encoding="utf-8") as fh:
    for i in range(n):
        fh.write(json.dumps({"w": tag, "n": i, "p": "x" * size}) + "\n")
        if mode != "buffered":
            fh.flush()
print("child finished", flush=True)
"""


def _kill_after(args: list[str], seconds: float) -> None:
    proc = subprocess.Popen([sys.executable, "-c", *args])
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def probe_save_at_the_end() -> None:
    print("=== the history is written when the run finishes; the run is killed")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "history.json"
        _kill_after([NAIVE, str(path)], 0.4)
        print(f"    file exists: {path.exists()}")
        print(f"    bytes on disk: {path.stat().st_size if path.exists() else 0}")


def probe_torn_line() -> None:
    for label, mode in [
        ("small lines (200 bytes), flushed", "flush"),
        ("one 400KB line per write, flushed", "big"),
        ("small lines, default buffering, no flush", "buffered"),
    ]:
        print(f"=== killed mid-write: {label}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.jsonl"
            _kill_after([CHILD, str(path), mode, "A"], 0.12)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            good = 0
            for line in lines:
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    break
                good += 1
            print(f"    lines: {len(lines)}   parsed before the first bad one: {good}")
            print(f"    torn tail: {len(lines) != good}")


def probe_two_writers() -> None:
    print("=== two processes appending to one file, 4000 records each")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "shared.jsonl"
        procs = [
            subprocess.Popen([sys.executable, "-c", CHILD, str(path), "count", tag])
            for tag in ("A", "B")
        ]
        for proc in procs:
            proc.wait(timeout=120)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        seen = {"A": 0, "B": 0}
        broken = 0
        for line in lines:
            try:
                seen[json.loads(line)["w"]] += 1
            except (json.JSONDecodeError, KeyError):
                broken += 1
        print("    records written: 8000")
        print(f"    lines on disk:   {len(lines)}   from A: {seen['A']}   from B: {seen['B']}")
        print(f"    unparseable:     {broken}")
        print(f"    records lost:    {8000 - seen['A'] - seen['B']}")


# --- 4: where a crashed session ends ---------------------------------------

AGENT_LIKE = r"""
import json, sys, time
# One rollout line per history item, as chapter 7 writes them.
path = sys.argv[1]
def w(rec):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec) + "\n"); fh.flush()
w({"type": "meta", "session_id": "probe", "version": 2})
w({"type": "user", "text": "add a retry to the client"})
for turn in range(50):
    w({"type": "assistant", "text": "", "tool_calls": [
        {"call_id": f"call_{turn}", "name": "run_shell",
         "arguments": {"command": "pytest"}, "raw_arguments": "{\"command\":\"pytest\"}"}]})
    time.sleep(0.08)   # the tool runs
    w({"type": "tool_result", "call_id": f"call_{turn}", "name": "run_shell", "content": "ok"})
"""


def probe_where_it_ends() -> None:
    print("=== an agent-shaped writer, killed at 20 random-ish moments")
    ends: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as d:
        for i in range(20):
            path = Path(d) / f"s{i}.jsonl"
            _kill_after([AGENT_LIKE, str(path)], 0.30 + i * 0.011)
            last = None
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    last = json.loads(line)["type"]
                except (json.JSONDecodeError, KeyError):
                    last = "damaged"
            ends[last or "empty"] = ends.get(last or "empty", 0) + 1
    for kind, count in sorted(ends.items(), key=lambda kv: -kv[1]):
        legal = "legal place to stop" if kind != "assistant" else "UNSENDABLE: call with no result"
        print(f"    ends on {kind:<12} {count:>2}/20   {legal}")


if __name__ == "__main__":
    probe_save_at_the_end()
    print()
    probe_torn_line()
    print()
    probe_two_writers()
    print()
    probe_where_it_ends()
