"""Do chapter 21's tests fail when chapter 21's code is wrong?

Same script as chapters 9 through 20: break one line, run the suite, see
whether anything turns red.

The targets are chosen to attack this chapter's own code -- config dispatch,
header and bearer-token wiring, the `TokenStorage` implementation, the
restart-safe expiry preload (F21-15), and the sandboxed stdio spawn. Nothing
here mutates the `mcp` SDK's own transport or OAuth flow: that is a
dependency, tested by its own suite, and there is no line of it in this
repository to revert.

    a config naming both command and url is not refused -> the wrong server
                                                              answers silently
    a config naming neither is not refused                -> a KeyError three
                                                              modules away
    env_http_headers accepts a blank value                -> an empty header
                                                              instead of none
    the bearer token is never added                       -> every remote
                                                              call is
                                                              unauthenticated
    bearer_token_env_var + oauth is allowed together       -> which credential
                                                              actually wins is
                                                              undefined
    the token file is never chmod'd                        -> a credential
                                                              readable by
                                                              anyone on the box
    preload never restores the expiry                      -> F21-15's bug,
                                                              reintroduced
    a preset client_id is re-seeded every time              -> masks a client
                                                              id that changed
    the sandboxed argv is never used                        -> a spawned
                                                              server runs
                                                              unconfined
    a wrapped command is discarded                          -> the sandbox
                                                              built an argv
                                                              nobody runs

    uv run python probe_mutations_ch21.py
"""

from __future__ import annotations

import atexit
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MUTATIONS: list[tuple[str, str, str, str]] = [
    # ---- config dispatch ----------------------------------------------------
    (
        "src/minicodex/registry.py",
        "a config naming both 'command' and 'url' is accepted instead of refused",
        "        if has_command and has_url:",
        "        if False:",
    ),
    (
        "src/minicodex/registry.py",
        "a config naming neither 'command' nor 'url' is accepted instead of refused",
        "        if not has_command and not has_url:",
        "        if False:",
    ),
    # ---- headers and the bearer token ----------------------------------------
    (
        "src/minicodex/remote.py",
        "a blank or unset env_http_headers variable still becomes a header",
        "        if value.strip():",
        "        if True:",
    ),
    (
        "src/minicodex/remote.py",
        "the bearer token is never added to the request headers",
        "        if token:",
        "        if False:",
    ),
    (
        "src/minicodex/remote.py",
        "a bearer token and OAuth can both be configured for the same server",
        "        if self.bearer_token_env_var and self.oauth is not None:",
        "        if False:",
    ),
    # ---- TokenStorage: whose disk, at what permissions -----------------------
    (
        "src/minicodex/mcp_oauth.py",
        "the token file is written world-readable -- the chmod is dropped",
        "            os.chmod(self.path, 0o600)",
        "            pass",
    ),
    # ---- F21-15: restart-safe proactive refresh ------------------------------
    (
        "src/minicodex/mcp_oauth.py",
        "preload_tokens never restores the absolute expiry -- F21-15's bug comes back",
        "    provider.context.token_expiry_time = await storage.get_expiry()",
        "    provider.context.token_expiry_time = None",
    ),
    (
        "src/minicodex/mcp_oauth.py",
        "set_tokens stops persisting the absolute expiry alongside the token",
        '        entry["expires_at"] = calculate_token_expiry(tokens.expires_in)',
        '        entry["expires_at"] = None',
    ),
    (
        "src/minicodex/mcp_oauth.py",
        "seed_client_id re-registers even when the same client_id is already stored",
        "        if existing is not None and existing.client_id == client_id:",
        "        if False:",
    ),
    # ---- F21-14: the sandbox chapter 20 built, applied to a spawned server ---
    (
        "src/minicodex/mcp.py",
        "a stdio MCP server is launched unconfined even when a sandbox was given",
        "        if self._sandbox is not None:",
        "        if False:",
    ),
    (
        "src/minicodex/mcp.py",
        "the sandbox-wrapped command is built and then discarded",
        "    if wrapped is None:",
        "    if True:",
    ),
]

ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, _, _, _ in MUTATIONS}

SUITES = [
    "tests/test_faults_ch21.py",
    "tests/test_faults_ch09.py",
]

#: Mutations that change behaviour no offline test on *this* platform can see.
#: Not empty, and the one entry is conditional rather than blanket-excused:
#: `test_F21_10_the_token_file_is_not_world_readable` is `skipif(os.name ==
#: "nt")` because `os.chmod` cannot express POSIX permission bits on Windows
#: at all (chapter 20's own honesty about bubblewrap, one credential file
#: later) -- so on this platform the mutation removing the `chmod` call is
#: genuinely invisible, and on POSIX it is not.
EQUIVALENT: set[str] = (
    {"the token file is written world-readable -- the chmod is dropped"}
    if os.name == "nt"
    else set()
)


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = ROOT / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = ROOT / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header", "-p", "no:warnings"],
                timeout=300,
                capture_output=True,
                text=True,
                # See chapter 19's probe: `text=True` alone decodes with the
                # locale's codec, which on a zh-CN Windows box is GBK, and a
                # suite whose output contains CJK then dies inside subprocess's
                # reader thread -- leaving `result.stdout` as `None`.
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
        mark = "   (equivalent mutant, expected)" if label in EQUIVALENT else ""
        print(f"  {caught:>3} test(s) fail  <-  {label}{mark}")
        if not caught and label not in EQUIVALENT:
            survivors.append(label)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) nothing noticed:")
        for label in survivors:
            print(f"  - {label}")
        raise SystemExit(1)
    print("every mutation was caught.")


if __name__ == "__main__":
    main()
