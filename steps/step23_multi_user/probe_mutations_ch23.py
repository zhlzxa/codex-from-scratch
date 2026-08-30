"""Do chapter 23's tests fail when chapter 23's code is wrong?

Same script as chapters 9 through 22: break one line, run the suite, see
whether anything turns red.

The targets are the four decisions this chapter is made of. **The gate**
(`auth.py`): does it actually refuse, does it cover the socket as well as the
routes, does it check the origin, does it re-read the account behind a live
session. **The separation** (`app.py`, `routes.py`): the per-owner store and
the per-account broker, and the memory directory that four chapters got wrong.
**The credentials** (`accounts.py`, `sessions.py`): the constant-work miss, the
one-time bootstrap token, the expiry clocks, the paired session revoke.
**The quota** (`quota.py`): charged on claim, counted per account.

Nothing here mutates `hashlib.scrypt` or anything else in the standard
library's crypto: that is a dependency this chapter calls, not code it wrote --
the same line chapters 21 and 22 drew around the MCP SDK.

    uv run python probe_mutations_ch23.py
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
    # ---- F23-01: the gate denies by default -----------------------------------
    (
        "src/minicodex/web/auth.py",
        "the gate lets an unauthenticated request through instead of refusing",
        "        if auth is None:",
        "        if False:",
    ),
    (
        "src/minicodex/web/auth.py",
        "every path becomes public, not just the three on the list",
        "        if not path.startswith(GUARDED_PREFIXES) or path in PUBLIC_PATHS:",
        "        if True:",
    ),
    (
        "src/minicodex/web/auth.py",
        "a session whose account was disabled keeps working",
        "    if account is None or account.disabled:",
        "    if account is None:",
    ),
    # ---- F23-02/F23-03: the socket is behind the same door --------------------
    (
        "src/minicodex/web/auth.py",
        "the gate skips websockets -- BaseHTTPMiddleware's behaviour, restored",
        '        if scope["type"] not in ("http", "websocket"):',
        '        if scope["type"] != "http":',
    ),
    (
        "src/minicodex/web/auth.py",
        "a cross-origin websocket handshake is accepted",
        '        if scope["type"] == "websocket" and not same_origin(headers):',
        "        if False:",
    ),
    (
        "src/minicodex/web/auth.py",
        "an Origin from another host counts as same-origin",
        "    return urlsplit(origin).netloc.lower() == host.lower()",
        "    return True",
    ),
    # ---- F23-04: separate namespaces, not permission checks -------------------
    (
        "src/minicodex/web/app.py",
        "every account shares one record store again",
        "            root = self.data_dir if owner.key is None else "
        "self.data_dir / TENANTS_DIR / owner.key",
        "            root = self.data_dir",
    ),
    (
        "src/minicodex/web/app.py",
        "every account shares one approval broker again",
        "        broker = self._brokers.get(account_key)",
        '        broker = self._brokers.get("")',
    ),
    (
        "src/minicodex/web/sessions.py",
        "a session is revoked by id alone, without checking whose it is",
        "            if session.id == session_id and session.account_key == account_key:",
        "            if session.id == session_id:",
    ),
    (
        "src/minicodex/web/routes.py",
        "the websocket subscribes without checking the thread belongs to the caller",
        "    if console.store_for(auth.owner).thread(thread_id) is None:",
        "    if False:",
    ),
    # ---- F23-05: the panel and the agent read one directory -------------------
    (
        "src/minicodex/web/routes.py",
        "the memory panel goes back to the workspace directory nothing reads",
        "    directory = auth.owner.memories()\n    try:",
        '    directory = Path(_console(request).require_thread(auth.owner, thread_id)["workspace"])'
        ' / ".minicodex" / "memories"\n    try:',
    ),
    (
        "src/minicodex/web/runtime.py",
        "a turn loads whatever memory the default owner has, not this owner's",
        "            loaded = load_memory(owner.memories())",
        "            loaded = load_memory(DEFAULT_OWNER.memories())",
    ),
    # ---- F23-06: a miss costs what a hit costs --------------------------------
    (
        "src/minicodex/web/accounts.py",
        "an unknown account answers without hashing anything -- the timing leak",
        "            verify_password(password, _dummy_hash())\n            return None",
        "            return None",
    ),
    (
        "src/minicodex/web/accounts.py",
        "the password comparison stops at the first differing byte",
        "    return hmac.compare_digest(computed, expected)",
        "    return computed == expected",
    ),
    # ---- the first account, and the sessions it gets --------------------------
    (
        "src/minicodex/web/accounts.py",
        "the bootstrap token still works after it has been redeemed",
        "        self.bootstrap_token = None\n        return account",
        "        return account",
    ),
    (
        "src/minicodex/web/accounts.py",
        "any string is accepted as the bootstrap token",
        "        if not hmac.compare_digest(token, expected):",
        "        if False:",
    ),
    (
        "src/minicodex/web/accounts.py",
        "an unreadable accounts.json is treated as an empty one",
        '            raise AccountError(\n                f"{self.path} is unreadable; '
        'refusing to treat it as empty"\n            ) from None',
        "            return []",
    ),
    (
        "src/minicodex/web/sessions.py",
        "a session never expires",
        "        return now - self.created > ABSOLUTE_TTL_SECONDS "
        "or now - self.last_seen > IDLE_TTL_SECONDS",
        "        return False",
    ),
    (
        "src/minicodex/web/sessions.py",
        "the raw token is kept in the table instead of its hash",
        "            token_hash=hash_token(token),",
        "            token_hash=token,",
    ),
    # ---- F23-07: revoking has to stop the work ---------------------------------
    (
        "src/minicodex/web/routes.py",
        "signing out everywhere leaves this account's turns running",
        "        if key.startswith(prefix) and not task.done():",
        "        if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "changing a password leaves every other session signed in",
        "    _revoke_everything(console, auth.account.key)\n    account =",
        "    account =",
    ),
    # ---- F23-08: the quota is charged when a turn is claimed -------------------
    (
        "src/minicodex/web/quota.py",
        "the hourly window is never enforced",
        "        if len(starts) >= limits.turns_per_hour:",
        "        if False:",
    ),
    (
        "src/minicodex/web/quota.py",
        "the concurrency limit is never enforced",
        "        if running >= limits.concurrent_turns:",
        "        if False:",
    ),
    (
        "src/minicodex/web/quota.py",
        "a claim is not recorded, so the hour never fills up",
        "        starts.append(now)",
        "        pass",
    ),
    (
        "src/minicodex/web/routes.py",
        "a turn refused for want of a provider still spends the hour",
        "        console.quota.refund(auth.account.key)",
        "        pass",
    ),
    # ---- F23-11: the wiring check asks the commands, not the file --------------
    (
        "tests/test_packaging.py",
        "the CI wiring check goes back to searching the workflow's whole text",
        r"""    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return "\n".join(
        step["run"]
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if isinstance(step.get("run"), str)
    )""",
        """    return workflow.read_text(encoding="utf-8")""",
    ),
    # ---- F23-09: a workspace root is a boundary --------------------------------
    (
        "src/minicodex/web/routes.py",
        "an account's workspace roots are not enforced",
        "    if roots and not any(_within(resolved, Path(root)) for root in roots):",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "'inside a root' becomes a string prefix again",
        "        return path.resolve().is_relative_to(root.expanduser().resolve())",
        "        return str(path.resolve()).startswith(str(root.expanduser().resolve()))",
    ),
]

ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, _, _, _ in MUTATIONS}

# `test_packaging.py` as well as this chapter's own file, because F23-11 lives
# there: the wiring check and its regression test are about CI, which is where
# `test_F_1_05_every_mutation_script_runs_somewhere` has lived since chapter 12.
# It costs 3.7 seconds a run, which is cheaper than moving a test away from the
# subject it is about.
SUITES = ["tests/test_faults_ch23.py", "tests/test_packaging.py"]


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
                timeout=600,
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
