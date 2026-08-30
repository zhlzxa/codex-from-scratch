"""Do chapter 22's tests fail when chapter 22's code is wrong?

Same script as chapters 9 through 21: break one line, run the suite, see
whether anything turns red.

The targets are this chapter's own additions -- the dual-shape validation
`add_mcp` repeats at the web layer (F22-04), the record-to-config
translation shared by `routes.py` and `runtime.py` (`mcp_config.py`), the
pending-attempt table's state lookup and expiry (F22-02/F22-03), and the
turn-time dispatch that refuses to attempt an interactive OAuth login from
inside a turn (F22-01). Nothing here mutates the `mcp` SDK's own OAuth
machinery (`OAuthContext`, `PKCEParameters`, the `mcp.client.auth.utils`
free functions) -- that is a dependency this chapter drives, not code it
wrote, the same line chapter 21's own probe already drew.

    uv run python probe_mutations_ch22.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

MUTATIONS: list[tuple[str, str, str, str]] = [
    # ---- F22-04: add_mcp's own dual-shape validation --------------------------
    (
        "src/minicodex/web/routes.py",
        "add_mcp accepts a command and a url together instead of refusing",
        "    if has_command and has_url:",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "add_mcp accepts neither a command nor a url instead of refusing",
        "    if not has_command and not has_url:",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "add_mcp accepts a bearer token and OAuth together instead of refusing",
        "        if body.bearer_token and body.oauth:",
        "        if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "a bearer token is echoed back to the browser instead of redacted",
        'if record.get("bearer_token"):',
        "if False:",
    ),
    # ---- mcp_config.py: one record -> the config that actually connects ------
    (
        "src/minicodex/web/mcp_config.py",
        "a record's own shape is ignored -- everything dispatches as stdio",
        'return "remote" if record.get("url") else "stdio"',
        'return "stdio"',
    ),
    (
        "src/minicodex/web/mcp_config.py",
        "a raw bearer token is never folded into the Authorization header",
        "    if token:",
        "    if False:",
    ),
    (
        "src/minicodex/web/mcp_config.py",
        "a bearer token and OAuth configured together is accepted instead of refused",
        "    if token and oauth_spec:",
        "    if False:",
    ),
    # ---- F22-02/F22-03: the pending-attempt table ------------------------------
    (
        "src/minicodex/web/oauth.py",
        "an expired pending attempt is never swept -- F22-03's bug",
        "        expired = [state for state, p in self._table.items()"
        " if now - p.created_at > self._ttl]",
        "        expired = []",
    ),
    (
        "src/minicodex/web/oauth.py",
        "pop() reads a pending attempt without removing it -- a state becomes reusable",
        "        return self._table.pop(state, None)",
        "        return self._table.get(state, None)",
    ),
    (
        "src/minicodex/web/routes.py",
        "the callback route trusts an unknown state instead of rejecting it",
        "    if pending is None:",
        "    if False:",
    ),
    # ---- F22-01: a turn never attempts an interactive OAuth login -------------
    (
        "src/minicodex/web/runtime.py",
        "resolve_mcp_configs stops checking whether a remote OAuth server has a token",
        '        if record_kind(s) == "remote" and config.oauth is not None:',
        "        if False:",
    ),
    (
        "src/minicodex/web/runtime.py",
        "a server with a token on file is treated as never connected",
        "            if await storage.get_tokens() is None:",
        "            if True:",
    ),
]

ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, _, _, _ in MUTATIONS}

SUITES = ["tests/test_faults_ch22.py"]


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
        print(f"  {caught:>3} test(s) fail  <-  {label}")
        if not caught:
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
