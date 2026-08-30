"""Do chapter 17's tests fail when chapter 17's code is wrong?

Same script as chapters 9 through 16.  Three of the entries below are
mutations this chapter's first draft actually shipped -- the select-then-update
claim, the usage table subtracted instead of rebuilt, and the secret count that
counted regexes -- so the list starts as a record of real mistakes rather than
as a list of invented ones.

    uv run python probe_mutations_ch17.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- claiming (F17-03) ---------------------------------------------------
    (
        "memory_jobs.py",
        "the claim is a select followed by an update, outside any transaction",
        '        self.db.execute("BEGIN IMMEDIATE")',
        "        pass",
    ),
    (
        "memory_jobs.py",
        "an expired lease is never taken back, so a killed run loses the session",
        "                \"    OR (state = 'claimed' AND lease_until <= ?) \"",
        '                "    OR (0 AND ? > 0) "',
    ),
    (
        "memory_jobs.py",
        "the attempt is counted on failure instead of on claim",
        "                    \"UPDATE jobs SET state='claimed', attempts=attempts+1, lease_until=?, \"",  # noqa: E501
        "                    \"UPDATE jobs SET state='claimed', attempts=attempts, lease_until=?, \"",  # noqa: E501
    ),
    (
        "memory_jobs.py",
        "a failing job comes back forever instead of giving up",
        "        if attempts >= MAX_ATTEMPTS:",
        "        if False:",
    ),
    (
        "memory_jobs.py",
        "failure retries immediately, with no backoff",
        "            (stamp + BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), detail[:500], stamp, session_id),",  # noqa: E501
        "            (stamp, detail[:500], stamp, session_id),",
    ),
    (
        "memory_jobs.py",
        "every session is eligible, including the ones the program had with itself",
        "        if rollout.meta.parent or rollout.meta.session_id in exclude:",
        "        if False:",
    ),
    (
        "memory_jobs.py",
        "a session that is still being written is eligible",
        '        if path.with_suffix(path.suffix + ".lock").exists():',
        "        if False:",
    ),
    # ---- what the extractor is asked (F17-04) --------------------------------
    (
        "memory_write.py",
        "the extraction prompt goes back to the quota wording measured at 0/3 no-ops",
        'STAGE1_SYSTEM = f"""\\',
        'STAGE1_SYSTEM = STAGE1_QUOTA\n_UNUSED_STAGE1 = f"""\\',
    ),
    (
        "memory_write.py",
        "`DURABLE: no` is advisory: the bullets under it are kept anyway",
        '    if verdict is not None and verdict.group(1).lower() == "no":',
        "    if False:",
    ),
    (
        "memory_write.py",
        "a placeholder bullet becomes a memory entry",
        '        if body.lower().strip(" .") in {"none", "n/a", "nothing", "(none)", ""}:',
        '        if body.lower().strip(" .") in {"n/a"}:',
    ),
    (
        "memory_write.py",
        "material outside a known heading is swept into the first section",
        "        if current is None:\n            continue",
        '        if current is None:\n            current = "preferences"',
    ),
    (
        "memory_write.py",
        "an empty extraction still writes a raw file for the merge to read",
        "            if stage1:\n                write_raw(directory, stage1)",
        "            if True:\n                write_raw(directory, stage1)",
    ),
    (
        "memory_write.py",
        "the raw file does not say it is temporary",
        "        f\"<!-- Temporary file: stage-1 output for session {stage1.session_id or '?'}.\",",
        '        "<!--",',
    ),
    # ---- redaction (F17-05) --------------------------------------------------
    (
        "memory_write.py",
        "the transcript is sent to the model unscrubbed",
        "    text, secrets = scrub(text)",
        "    secrets = 0",
    ),
    (
        "memory_write.py",
        "the model's own answer is written to disk unscrubbed",
        "    answer, more = scrub(answer)",
        "    more = 0",
    ),
    (
        "memory_write.py",
        "a credential named as one but not shaped like one gets through",
        "    text = _SECRET_ASSIGNMENT.sub(_assignment, text)",
        "    pass",
    ),
    (
        "memory_write.py",
        "one secret matched by two patterns is reported as two",
        "        if match.group(2) == REDACTED:",
        "        if False:",
    ),
    (
        "memory_write.py",
        "the merge answer is written without being scrubbed",
        "    summary, hits_a = scrub(summary.strip())",
        "    summary, hits_a = summary.strip(), 0",
    ),
    # ---- forgetting (F17-06, F17-07) -----------------------------------------
    (
        "memory_write.py",
        "forgetting is by age, which deletes the entry that is old and used",
        "        scored.append((-cited, -last_used, -first_seen, index, entry))",
        "        scored.append((first_seen, -last_used, -cited, index, entry))",
    ),
    (
        "memory_write.py",
        "an entry with no citations is dropped whatever its age",
        "        elif cited == 0 and age_days > unused_days:",
        "        elif cited == 0:",
    ),
    (
        "memory_write.py",
        "an entry nothing has recorded yet is treated as ancient rather than new",
        '        first_seen = float(row.get("first_seen", 0) or 0) or now\n'
        "        age_days = max(0.0, (now - first_seen) / 86400.0)",
        '        first_seen = float(row.get("first_seen", 0) or 0)\n'
        "        age_days = max(0.0, (now - first_seen) / 86400.0)",
    ),
    (
        "memory_write.py",
        "the cap is not a cap",
        "        if len(kept) >= max_entries:",
        "        if False:",
    ),
    (
        "memory_write.py",
        "the writer trims the summary with its own ruler, not the reader's",
        "    summary = trim_summary(summary.strip(), kept.kept)",
        "    summary = trim_summary(summary.strip())",
    ),
    (
        "memory_write.py",
        "the file is rewritten in ranking order, so every merge reshuffles the diff",
        "    kept.sort(key=lambda e: order[e.entry_id])",
        "    pass",
    ),
    (
        "memory_write.py",
        "usage rows for entries that no longer exist are kept",
        "    updated: dict[str, dict[str, Any]] = {}",
        "    updated: dict[str, dict[str, Any]] = dict(counts)",
    ),
    (
        "memory_write.py",
        "the resident half is left at whatever length the merge produced",
        "    summary = trim_summary(summary.strip(), kept.kept)",
        "    summary = summary.strip()",
    ),
    # ---- the single writer (F17-09) ------------------------------------------
    (
        "memory_write.py",
        "the merge lock is not taken",
        "    fd = _lock(lock_path)",
        "    fd = -1",
    ),
    (
        "memory_write.py",
        "a merge that produced nothing replaces the memory with nothing",
        "    if not entries and not summary.strip():",
        "    if False:",
    ),
    (
        "memory_write.py",
        "a merge answer with no markers is treated as the body",
        "    if SUMMARY_MARK not in text or BODY_MARK not in text:",
        "    if False:",
    ),
    (
        "memory_write.py",
        "a note may be written straight into the body file",
        "        path = propose(Path(directory), body)",
        '        path = Path(directory) / BODY_FILE\n        path.write_text(body, encoding="utf-8")',  # noqa: E501
    ),
    (
        "memory_write.py",
        "one session may propose as many notes as it likes",
        "        if written >= limit:",
        "        if False:",
    ),
    (
        "memory_write.py",
        "a note overwrites the one before it",
        "        if not path.exists():",
        "        if True:",
    ),
    # ---- feeding on its own output (F17-10) ----------------------------------
    (
        "memory_write.py",
        "the extractor is shown the note the same session proposed",
        "                if call.name == NOTE_NAME:\n                    continue",
        "                if False:\n                    continue",
    ),
    (
        "memory_write.py",
        "the extractor is shown the program's own system notes, memory included",
        "        if isinstance(item, UserMessage):",
        "        if isinstance(item, (UserMessage, SystemNote)):",
    ),
    # ---- quota and consent (F17-02, F17-11) ----------------------------------
    (
        "memory_write.py",
        "the writer runs whatever is left of the quota",
        "    if headroom is not None and headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:",
        "    if False:",
    ),
    (
        "memory_write.py",
        "an unknown headroom is read as no headroom",
        "    if headroom is not None and headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:",
        "    if headroom is None or headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:",
    ),
    (
        "composition.py",
        "the note tool is present whether or not the user asked to be remembered",
        "    if remember is not None:",
        "    if True:",
    ),
    (
        "model.py",
        "the rate-limit headers are never read",
        "                self.rate_limit = RateLimit.from_headers(resp.headers)",
        "                self.rate_limit = None",
    ),
    # ---- the memory root went global (F17-12) --------------------------------
    (
        "memory.py",
        "the memory directory is back inside whatever repository is the cwd",
        'MINICODEX_HOME = Path.home() / ".minicodex"',
        'MINICODEX_HOME = Path(".minicodex")',
    ),
    (
        "memory_write.py",
        "the merge lock is per-repository again, so two checkouts both hold it",
        'MERGE_LOCK = MINICODEX_HOME / "memory_merge.lock"',
        'MERGE_LOCK = Path(".minicodex") / "memory_merge.lock"',
    ),
    (
        "memory_jobs.py",
        "the jobs database is per-repository, so one session is extracted once per checkout",
        'DEFAULT_JOBS_PATH = MINICODEX_HOME / "memory_jobs.sqlite3"',
        'DEFAULT_JOBS_PATH = Path(".minicodex") / "memory_jobs.sqlite3"',
    ),
    (
        "memory_jobs.py",
        "the jobs database moves inside the memory directory the merge diffs",
        'DEFAULT_JOBS_PATH = MINICODEX_HOME / "memory_jobs.sqlite3"',
        'DEFAULT_JOBS_PATH = MINICODEX_HOME / "memories" / "memory_jobs.sqlite3"',
    ),
    # ---- forgetting ranks on an unreliable signal (F17-13) -------------------
    (
        "memory_write.py",
        "zero citations alone is enough to forget an entry",
        "        elif cited == 0 and age_days > unused_days:",
        "        elif cited == 0:",
    ),
    (
        "memory_write.py",
        "an entry nobody has a record of is treated as ancient rather than as new",
        '        first_seen = float(row.get("first_seen", 0) or 0) or now\n'
        "        age_days = max(0.0, (now - first_seen) / 86400.0)",
        '        first_seen = float(row.get("first_seen", 0) or 0)\n'
        "        age_days = max(0.0, (now - first_seen) / 86400.0)",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch17.py",
    "tests/test_faults_ch16.py",
    "tests/test_faults_ch07.py",
    "tests/test_schemas.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = SRC / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header"],
                timeout=900,
                capture_output=True,
                text=True,
                # Not `text=True` alone: that decodes with the *locale's*
                # codec, which on a zh-CN Windows box is GBK, and a suite
                # whose failure output contains CJK then dies with a
                # `UnicodeDecodeError` inside subprocess's reader thread --
                # leaving `result.stdout` as `None` and the counting below
                # crashing on it. Chapter 2's F02-07 rule ("a bad byte should
                # come back slightly wrong, not as an exception that discards
                # the whole read") applies to this program's own tools too.
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
